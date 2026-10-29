"""回放数据源：录制文件 → `feed(source="mock")` → 摘要 / `/ws/live`。

这个文件形状由四条"必须说得出证据"的口径决定：

- **来源不可洗白**：`source != "mock"` 的文件在读的那一步就整份拒收；演进去的点在
  `sample_summary.source` 与 `/ws/live` 的消息里都必须是 `mock`。S6 出口判据"MOCK 与真实可
  区分"落到这两处才算数，写在注释里不算。
- **说不清归属就一个字不喂**：地址在库里没有节点行、或同址属两台接入机，都是拒绝 + 点名，
  不是"挑一台"。理由与 `/ws/bridge` 的 `gateway` 必填同一条：猜错的那一台，页面上看着完全正常。
- **可选件缺失不是故障**：没配目录、角色是汇聚——`problem()` 回 `None`，`status()` 说清理由。
  把没做的事报成 degraded，运营者就会开始忽略 degraded（`dialer`/`retention`/`capture` 同源）。
- **节拍不许漂**：等待按"录制起点 + `t_ms`"的绝对时刻，收摊信号一条路就能打断整段播放。
  断言用的是"等到条件成立"，不是"睡固定的秒数然后假设演完了"——后者在慢机器上会假绿。

帧字节与录制文件由 `rftrack/tools/make_replay_recording.py` 现产（形状取自
`tests/golden/frames-v1.json` 的 `metrics_full_3ch`），这样"工具产出"与"读取端认的格式"
每次都被同一条测试钉在一起；手写一份"我觉得像的 JSON"就等于放了一条两边各自漂移的通路。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Gateway, SampleSummary, SignalEvent
from app.services.capture import CapturePipeline
from app.services.ingest import reconcile
from app.services.replay import SOURCE, ReplayError, ReplayRunner, load

TOOL_PATH = Path(__file__).parents[2] / "tools" / "make_replay_recording.py"
if not TOOL_PATH.is_file():
    pytest.skip(f"{TOOL_PATH} 不存在：录制产出器是这些测试的依赖", allow_module_level=True)


def import_tool():
    """按文件路径加载产出器：它在 `rftrack/tools/` 下，不是包里的模块。"""
    spec = importlib.util.spec_from_file_location("make_replay_recording", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


TOOL = import_tool()

MAIN_ADDR = "/dev/widscan58-A"
AWAY_ADDR = "/dev/ttyAMA0"


class StubResolver:
    """回放不经过桥的端点表（地址由录制自己声明），所以认领那条路在这里不该被走到。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def addrs_for(self, gateway_id: int, eps: tuple[str, ...]) -> dict[str, str]:
        self.calls.append((gateway_id, eps))
        return {}


class FakePublisher:
    """/ws/live 的收件口替身：这一轮要看的是"发出去的那条消息里 source 是什么"。"""

    def __init__(self) -> None:
        self.messages: list[tuple[int, dict]] = []

    def publish(self, site_id: int, message: dict) -> int:
        self.messages.append((site_id, message))
        return 1


def seed(client, slug: str, name: str, addr: str, *, kind: str = "serial") -> dict:
    """一套 场地→位置→接入机→节点，节点行由 `reconcile` 生成（节点唯一的事实来源）。"""
    site = client.post("/api/sites", json={"name": name, "slug": slug}).json()
    relay = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": f"{name}塔"}
    ).json()
    gateway = client.post(
        "/api/gateways",
        json={"relay_id": relay["relay_id"], "name": f"pi-{slug}", "kind": "usb-bridge"},
    ).json()
    ep = f"{kind}:{addr}"
    with client.app.state.db.scope() as session:
        reconcile(
            session,
            session.get(Gateway, gateway["gateway_id"]),
            [{"id": ep, "kind": kind, "label": f"widscan58-{slug}", "path": addr,
              "online": True, "busy": False}],
            Settings(max_nodes_per_gateway=8),
        )
    row = client.get(f"/api/nodes?gateway={gateway['gateway_id']}").json()[0]
    assert row["transport"]["addr"] == addr
    return {
        "site_id": site["site_id"],
        "gateway_id": gateway["gateway_id"],
        "node_id": row["node_id"],
        "ep": ep,
        "addr": addr,
    }


@pytest.fixture
def chain(client):
    """两块板、两个场地：场地维的隔离与"同址两台"那种形状都要有第二家才对拍。"""
    return {
        "main": seed(client, "main", "主场", MAIN_ADDR),
        "away": seed(client, "away", "客场", AWAY_ADDR, kind="uart"),
    }


def stack(client, directory, publisher=None, **overrides) -> tuple[CapturePipeline, ReplayRunner]:
    """一条不起后台 HTTP 的采集 + 挂在它上面的回放；节拍压到 50 ms，好让测试等得起。"""
    settings = Settings(
        db_path=client.app.state.settings.db_path,
        dialer_enabled=False,
        retention_purge_enabled=False,
        capture_flush_interval_s=0.05,
        # 断流预算放大：这些测试要看的是"喂进去的东西落在哪"，不是断流判定（那条在
        # `test_capture.py` 里已经钉过），60 s 保证一份 2 s 的录制不会顺带造出一条事件。
        capture_silence_s=60.0,
        replay_dir=directory,
        **overrides,
    )
    pipeline = CapturePipeline(
        client.app.state.db, settings, resolver=StubResolver(), publisher=publisher
    )
    return pipeline, ReplayRunner(client.app.state.db, settings, pipeline)


async def _until(predicate, *, cap: float = 3.0) -> None:
    """正面等待：条件成立就返回，超了就报错——"睡一会儿再看看"不是判据。"""
    deadline = time.monotonic() + cap
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"等待的条件在 {cap} s 内没有成立")
        await asyncio.sleep(0.02)


def drive(pipeline, runner, predicate, *, cap: float = 8.0) -> tuple[dict, dict]:
    """起两条循环 → 等条件 → 按收摊顺序停。返回 `(replay.status(), capture.status())`。

    `cap` 默认给到 8 s：这里多数条件要**整段录制演完**才可能成立，上限只是"卡死了要报错"
    的保险，不该压着录制的墙钟时长——机器一忙就红的绿不是绿。
    """

    async def scenario():
        await pipeline.start()
        await runner.start()
        try:
            await _until(lambda: predicate(runner, pipeline), cap=cap)
        finally:
            await runner.stop()
            await pipeline.stop()
        return runner.status(), pipeline.status()

    return asyncio.run(scenario())


def write_recording(directory: Path, name: str, **kwargs) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return TOOL.write(TOOL.synthesize(**kwargs), directory, name)


def summaries(client, node_id: int | None = None) -> list[SampleSummary]:
    with client.app.state.db.scope() as session:
        query = select(SampleSummary)
        if node_id is not None:
            query = query.where(SampleSummary.node_id == node_id)
        return list(session.scalars(query))


# --- 工具 ↔ 读取端 ------------------------------------------------------------


def test_the_tool_output_is_a_recording_the_runner_can_play(tmp_path):
    path = write_recording(
        tmp_path, "synthetic-2s", seconds=2.0, hz=5.0, addr=MAIN_ADDR, chunk_bytes=40
    )
    recording = load(path)

    assert (recording.name, recording.ep) == ("synthetic-2s", "replay:synthetic-2s")
    assert recording.transport_addr == MAIN_ADDR
    # `--chunk-bytes` 切的是同一帧的碎片：块数多、总字节数与不切时一致。
    whole = load(write_recording(tmp_path, "whole", seconds=2.0, hz=5.0, addr=MAIN_ADDR))
    assert len(recording.chunks) > len(whole.chunks)
    assert sum(len(body) for _, body in recording.chunks) == sum(
        len(body) for _, body in whole.chunks
    )
    assert recording.span_s == pytest.approx(1.8)
    assert "合成" in recording.notes

    # 同一组参数两次产出逐字节相同：冒烟脚本要能钉住"第 9 帧是 −30.0 dB"这种断言，
    # 而不是"看起来在动"。这里比的是同参数的两次，不是两种切法。
    first = json.dumps(TOOL.synthesize(seconds=2.0, hz=5.0, addr=MAIN_ADDR), sort_keys=True)
    second = json.dumps(TOOL.synthesize(seconds=2.0, hz=5.0, addr=MAIN_ADDR), sort_keys=True)
    assert first == second


def test_the_sweep_peaks_where_the_formula_says_it_does(tmp_path):
    """电平是闭式公式：峰值帧、首帧、EMA 落后，全部可以在测试里精确对上。"""
    recording = load(
        write_recording(tmp_path, "peak", seconds=4.0, hz=5.0, addr=MAIN_ADDR)
    )
    from app.services import frame_parser

    stream = frame_parser.FrameStream()
    frames = [
        frame_parser.decode(item.type, item.payload)
        for _, chunk in recording.chunks
        for item in stream.push(chunk)
        if item.kind != "text"
    ]
    assert (len(frames), stream.crc_errors, stream.resync_bytes) == (20, 0, 0)
    assert [f["seq"] for f in frames] == list(range(20))
    assert frames[0]["channels"][0]["rssi"] == TOOL.FAR_DB
    assert frames[9]["channels"][0]["rssi"] == TOOL.NEAR_DB
    assert frames[19]["channels"][0]["rssi"] == TOOL.FAR_DB
    # EMA 必须**落后**于瞬时值：上升段里它比 `rssi` 弱、趋势图上两条线不重合，才叫平滑过。
    assert frames[9]["channels"][0]["rssiEma"] < frames[9]["channels"][0]["rssi"]
    # 第 2 条信道照夹具原值：不动的东西就该一直是不动的，别为了"好看"给它加摆动。
    assert {f["channels"][2]["rssi"] for f in frames} == {-1.5}
    # 不许动的字段：一个都不许被脚本编造出来。
    assert {f["loKhz"] for f in frames} == {2410437}
    assert {c["id"] for f in frames for c in f["channels"]} == {2, 4, 7}


def test_the_cli_writes_the_file_the_reader_expects(tmp_path):
    """CLI 那一条路也要有证据：S6-9 的冒烟就是这么起的，参数解析错了这里先红。"""
    out = tmp_path / "replay"
    result = subprocess.run(
        [sys.executable, str(TOOL_PATH), "--addr", MAIN_ADDR, "--out-dir", str(out),
         "--seconds", "3", "--hz", "5", "--name", "cli"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "source=mock" in result.stdout
    recording = load(out / "cli.json")
    assert recording.transport_addr == MAIN_ADDR
    assert len(recording.chunks) == 15


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda m: m.update(source="real"), "source_not_mock"),
        (lambda m: m.update(source="live"), "source_not_mock"),
        (lambda m: m.update(format="other-thing"), "format_unsupported"),
        (lambda m: m.pop("transport_addr"), "addr_missing"),
        (lambda m: m.update(transport_addr="   "), "addr_missing"),
        (lambda m: m.update(chunks=[]), "chunks_empty"),
        (lambda m: m.update(chunks=[{"t_ms": 0}]), "chunk_malformed"),
        (lambda m: m.update(chunks=[{"t_ms": 5, "bytes": "!!!"}]), "chunk_malformed"),
        (lambda m: m.update(chunks=[{"t_ms": 9, "bytes": "AAA="},
                                    {"t_ms": 8, "bytes": "AAA="}]), "timestamps_backwards"),
        (lambda m: m.update(chunks=[{"t_ms": True, "bytes": "AAA="}]), "timestamps_backwards"),
    ],
)
def test_load_refuses_every_shape_that_could_wash_or_break_a_recording(
    tmp_path, mutate, reason
):
    path = tmp_path / "bad.json"
    manifest = TOOL.synthesize(seconds=1.0, hz=2.0, addr=MAIN_ADDR)
    mutate(manifest)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ReplayError) as caught:
        load(path)
    assert caught.value.reason == reason


def test_a_file_that_is_not_json_at_all_is_unreadable_and_says_so(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ReplayError) as caught:
        load(path)
    assert caught.value.reason.startswith("unreadable:")


# --- 播进去之后的事实 --------------------------------------------------------


def test_a_replayed_recording_lands_in_summaries_labeled_mock(client, chain, tmp_path):
    total = 10  # 2 s × 5 Hz
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path)

    replay_status, capture_status = drive(
        pipeline, runner, lambda r, p: r.chunks_fed == total and p.status()["frames"] == total
    )

    assert replay_status["running"] is False
    assert replay_status["refused"] is None
    assert replay_status["source"] == SOURCE
    assert [row["name"] for row in replay_status["recordings"]] == ["r"]
    assert replay_status["rejected"] == [] and replay_status["unmatched"] == {}
    assert replay_status["problem"] is None
    # 第一帧多半赶在归属表刷新之前喂进来，走的是待归属缓冲、下一拍整帧交出：`unattributed`
    # 是**累计**数，所以这里断的是"进过缓冲的都全部交出去了"，不押"第几帧会赶上刷新"那种时序。
    assert capture_status["pending_dropped"] == 0
    assert capture_status["unattributed"] == capture_status["attributed_late"]
    assert capture_status["pending_points"] == 0
    assert capture_status["frames"] == total
    assert capture_status["points_stored"] == total * 3

    rows = summaries(client, chain["main"]["node_id"])
    assert rows, "一格摘要都没有"
    assert {row.site_id for row in rows} == {chain["main"]["site_id"]}
    assert {row.gateway_id for row in rows} == {chain["main"]["gateway_id"]}
    assert {row.slot for row in rows} == {2, 4, 7}
    # 这一条是 S6 出口判据"MOCK 与真实可区分"在库里的落点。
    assert {row.source for row in rows} == {"mock"}
    assert sum(row.sample_count for row in rows) == total * 3
    assert sum(row.ema_count for row in rows) == total * 3
    # 另一个场地一个字都不该有：回放喂的是明确的一块板。
    assert summaries(client, chain["away"]["node_id"]) == []


def test_a_replayed_frame_is_published_to_live_as_mock(client, chain, tmp_path):
    """`/ws/live` 那条消息上必须带着 `source`：页面角标读的就是它。"""
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    publisher = FakePublisher()
    pipeline, runner = stack(client, tmp_path, publisher=publisher)

    drive(pipeline, runner, lambda r, p: len(publisher.messages) >= 3)

    assert publisher.messages, "一帧都没发布"
    assert {site for site, _ in publisher.messages} == {chain["main"]["site_id"]}
    assert {m["source"] for _, m in publisher.messages} == {"mock"}
    assert {m["transport_addr"] for _, m in publisher.messages} == {MAIN_ADDR}
    assert {m["ep"] for _, m in publisher.messages} == {"replay:r"}
    assert {m["node_id"] for _, m in publisher.messages} == {chain["main"]["node_id"]}
    # 频道对象照解析器原样出去：页面要读的字段一个都不许在这条路上被挑掉。
    channel = publisher.messages[0][1]["channels"][0]
    assert {"id", "rssi", "rssiEma", "ema", "flags", "floor", "snr", "ts", "seq"} <= set(channel)
    assert {c["id"] for _, m in publisher.messages for c in m["channels"]} == {2, 4, 7}


def test_a_replayed_recording_does_not_fabricate_stream_events(client, chain, tmp_path):
    """2 s 的录制演完就停：断流预算放大到 60 s，所以这一轮**不该**有任何事件行。

    钉的是"演完了"与"板子拔了"两件事不许混：前者只该在 `replay.done` 里说话。
    """
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path)

    replay_status, _ = drive(pipeline, runner, lambda r, p: r.chunks_fed == 10)

    assert replay_status["done"] == ["r"], "放完不循环的那份必须点名说'演完了'"
    assert replay_status["problem"] is None
    with client.app.state.db.scope() as session:
        assert session.scalars(select(SignalEvent)).all() == []


# --- 说不清归属的那几条 ------------------------------------------------------


def test_a_recording_claiming_to_be_real_is_refused_before_it_feeds(client, chain, tmp_path):
    path = write_recording(tmp_path, "r", seconds=1.0, hz=2.0, addr=MAIN_ADDR)
    manifest = json.loads(path.read_text())
    manifest["source"] = "real"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    pipeline, runner = stack(client, tmp_path)

    async def scenario():
        await pipeline.start()
        await runner.start()
        try:
            return runner.status(), pipeline.status()
        finally:
            await runner.stop()
            await pipeline.stop()

    replay_status, capture_status = asyncio.run(scenario())
    assert replay_status["rejected"] == [{"file": "r.json", "reason": "source_not_mock"}]
    assert replay_status["recordings"] == []
    assert replay_status["chunks_fed"] == 0
    assert capture_status["frames"] == 0
    assert "读不了" in replay_status["problem"]
    assert summaries(client) == []


def test_a_recording_without_a_node_row_feeds_nothing_and_says_why(client, chain, tmp_path):
    write_recording(tmp_path, "ghost", seconds=2.0, hz=5.0, addr="/dev/not-registered")
    pipeline, runner = stack(client, tmp_path)

    replay_status, capture_status = drive(
        pipeline, runner, lambda r, p: r.status()["unmatched"] != {}
    )

    assert replay_status["unmatched"] == {"ghost": "node_row_missing"}
    assert replay_status["chunks_fed"] == 0
    assert capture_status["frames"] == 0
    assert "没有可喂的节点行" in replay_status["problem"]
    assert summaries(client) == []


def test_an_addr_owned_by_two_gateways_is_not_resolved_by_guessing(client, chain, tmp_path):
    """同址两台是**允许**的既有形态（`test_multi_gateway` 钉着），所以这里没有正确答案。"""
    clash = seed(client, "clash", "撞名场", MAIN_ADDR)
    assert clash["gateway_id"] != chain["main"]["gateway_id"]
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path)

    replay_status, capture_status = drive(
        pipeline, runner, lambda r, p: r.status()["unmatched"] != {}
    )

    assert replay_status["unmatched"] == {"r": "ambiguous_gateway"}
    assert replay_status["chunks_fed"] == 0 and capture_status["frames"] == 0
    assert summaries(client) == []


# --- 可选件的缺失不是故障 ----------------------------------------------------


def test_replay_refuses_when_capture_is_not_running(client, chain, tmp_path):
    """`capture_enabled=0` + 配了回放目录 = 配置矛盾，这一条要说出口而不是沉默。"""
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path, capture_enabled=False)

    async def scenario():
        await pipeline.start()
        await runner.start()
        status = runner.status()
        await runner.stop()
        await pipeline.stop()
        return status

    replay_status = asyncio.run(scenario())
    assert replay_status["refused"] == "capture_disabled"
    assert replay_status["running"] is False
    assert replay_status["recordings"] == []
    assert "采集层没在跑" in replay_status["problem"]


def test_an_aggregator_refuses_replay_without_calling_it_a_fault(client, chain, tmp_path):
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path, instance_role="aggregator")

    async def scenario():
        await pipeline.start()
        await runner.start()
        status = runner.status()
        await runner.stop()
        await pipeline.stop()
        return status

    replay_status = asyncio.run(scenario())
    assert pipeline.role == "aggregator"
    assert replay_status["refused"] == "aggregator_role"
    # 那一台按设计没有采集，也没有自己的桥：这是形态，不是故障（与 capture/dialer 同口径）。
    assert replay_status["problem"] is None


def test_no_directory_configured_is_neither_enabled_nor_a_problem(client, tmp_path):
    pipeline, runner = stack(client, None)
    assert runner.enabled is False
    assert runner.status() == {
        "enabled": False,
        "dir": None,
        "loop": False,
        "running": False,
        "refused": None,
        "source": SOURCE,
        "recordings": [],
        "rejected": [],
        "unmatched": {},
        "done": [],
        "chunks_fed": 0,
        "passes": 0,
        "problem": None,
    }


def test_a_missing_directory_is_named_not_crashed(client, chain, tmp_path):
    pipeline, runner = stack(client, tmp_path / "nope")

    async def scenario():
        await pipeline.start()
        await runner.start()
        status = runner.status()
        await runner.stop()
        await pipeline.stop()
        return status

    replay_status = asyncio.run(scenario())
    assert replay_status["rejected"][0]["reason"] == "dir_missing"
    assert replay_status["problem"].startswith("回放目录不存在")
    assert replay_status["chunks_fed"] == 0


def test_a_broken_file_does_not_stop_the_good_ones_playing(client, chain, tmp_path):
    write_recording(tmp_path, "good", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    (tmp_path / "junk.json").write_text("{oops", encoding="utf-8")
    pipeline, runner = stack(client, tmp_path)

    replay_status, capture_status = drive(
        pipeline, runner, lambda r, p: r.chunks_fed == 10
    )

    assert [row["name"] for row in replay_status["recordings"]] == ["good"]
    assert replay_status["rejected"] == [
        {"file": "junk.json", "reason": "unreadable:JSONDecodeError"}
    ]
    assert replay_status["chunks_fed"] == 10 and capture_status["frames"] == 10


# --- 节拍与收摊 --------------------------------------------------------------


def test_loop_replays_from_the_start_and_counts_passes(client, chain, tmp_path):
    write_recording(tmp_path, "r", seconds=1.0, hz=5.0, addr=MAIN_ADDR)  # 5 帧
    pipeline, runner = stack(client, tmp_path, replay_loop=True)

    replay_status, capture_status = drive(pipeline, runner, lambda r, p: r.passes >= 2, cap=12.0)

    assert replay_status["loop"] is True
    assert replay_status["passes"] >= 2
    assert replay_status["chunks_fed"] >= 10
    assert replay_status["done"] == [], "循环中的录制不许被记成已放完"
    assert capture_status["points_stored"] >= 10


def test_stop_returns_before_a_long_recording_finishes(client, chain, tmp_path):
    """收摊不许等剩下那 30 s：`stop()` 挂在关停路上，就是"服务关不掉"那种故障。"""
    write_recording(tmp_path, "long", seconds=30.0, hz=2.0, addr=MAIN_ADDR)
    pipeline, runner = stack(client, tmp_path)

    async def scenario():
        await pipeline.start()
        await runner.start()
        await _until(lambda: runner.chunks_fed >= 1)
        started = time.monotonic()
        await runner.stop()
        elapsed = time.monotonic() - started
        await pipeline.stop()
        return elapsed, runner.status(), pipeline.status()

    elapsed, replay_status, _ = asyncio.run(scenario())
    assert elapsed < 0.5, f"stop() 花了 {elapsed:.2f} s"
    assert replay_status["running"] is False
    assert replay_status["chunks_fed"] < len(load(tmp_path / "long.json").chunks)
    assert replay_status["passes"] == 0, "中途收摊不该算放完了一遍"


def test_replay_feeds_the_pipeline_without_touching_a_bridge(client, chain, tmp_path):
    """回放这条路一次都不该去敲桥：地址是录制自己声明的，问桥没有意义。"""
    resolver = StubResolver()
    write_recording(tmp_path, "r", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    settings = Settings(
        db_path=client.app.state.settings.db_path,
        dialer_enabled=False,
        retention_purge_enabled=False,
        capture_flush_interval_s=0.05,
        capture_silence_s=60.0,
        replay_dir=tmp_path,
    )
    pipeline = CapturePipeline(client.app.state.db, settings, resolver=resolver)
    runner = ReplayRunner(client.app.state.db, settings, pipeline)

    drive(pipeline, runner, lambda r, p: r.chunks_fed == 10)
    assert resolver.calls == []
