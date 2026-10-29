"""采集层：字节 → 帧 → 每槽位一个点，再分发到内存环 / 桶摘要 / 事件表。

这个文件形状由四条"必须说得出证据"的口径决定：

- **归属只能靠认领**：`ep` 与节点行的 `transport_addr` 不是同一个串（uart 端点：`id` 用未解析
  的 `--rh-serial`，`path` 是 `realpath` 之后的 `ttyAMA0`）。这里断言的是"桥说了什么我们用什么"，
  顺带钉死一条：去掉前缀猜地址的实现会让 ②a 的板子永远归不了属。
- **没人在看就没有字节**：钩子只在有页面连着这块板时才有字节可给，所以"没字节"绝不能写成
  `node_offline`。会话结束撤状态、不补事件——那是本层最容易自觉"顺手补一条"的地方。
- **MOCK 不许被洗白**：回放帧一旦被真实帧跟上去，来源仍是 mock。这条与 `retention` 的桶纪律同源，
  在这里按"页面上看到的角标"那一端再钉一次。
- **背压要数得出来**：队列满、网关数超限、待归属缓冲挤出——每种丢法各有一个数字，
  绝不静默。

帧字节一律取自 `tests/golden/frames-v1.json`（由 TS 侧权威导出）。手写一段"我觉得对的帧"
等于把协议镜像的第二次实现塞进测试里：固件一改，这里就变成一个会说谎的绿。
"""

import asyncio
import json
import zlib
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Gateway, SampleSummary, SignalEvent
from app.services import capture as capture_module
from app.services.capture import CapturePipeline
from app.services.frame_parser import (
    CHANNEL,
    FRAME_LEVELS,
    FRAME_LOG,
    FRAME_METRICS,
    FRAME_SPECTRUM,
    METRICS_HDR,
)
from app.services.ingest import reconcile
from app.services.ws_bridge import BridgeHub, Link, UpstreamClosed

GOLDEN = Path(__file__).parent / "golden" / "frames-v1.json"
if not GOLDEN.exists():
    pytest.skip(
        "golden/frames-v1.json 不存在：先跑 "
        "`node --experimental-strip-types rftrack/tools/export_golden_frames.mjs`",
        allow_module_level=True,
    )

CASES = {case["name"]: case for case in json.loads(GOLDEN.read_text())["cases"]}

#: ② 系列的两种出口：uart 条目的 `id` 与 `path` 故意不同名，正是认领那条路要处理的形状。
SERIAL_EP = "serial:/dev/widscan58-A"
SERIAL_ADDR = "/dev/widscan58-A"
UART_EP = "uart:/dev/serial0"
UART_ADDR = "/dev/ttyAMA0"


def frame(type_code: int, case: str) -> bytes:
    """`0xA5 'R' type ver u16len payload crc32`——与 `frame_parser` 同一套字节序。"""
    payload = bytes.fromhex(CASES[case]["hex"])
    head = bytes([0xA5, 0x52, type_code, 1]) + len(payload).to_bytes(2, "little")
    return head + payload + (zlib.crc32(head + payload) & 0xFFFFFFFF).to_bytes(4, "little")


METRICS = frame(FRAME_METRICS, "metrics_full_3ch")
LEVELS = frame(FRAME_LEVELS, "levels_mode_1")
SPECTRUM = frame(FRAME_SPECTRUM, "spectrum_n_3")
LOG = frame(FRAME_LOG, "log_frame_no_decoder")


def metrics_with_ids(ids: list[int]) -> bytes:
    """把 golden 那帧的**档位下标**换成给定的数，其余字节一个不动（CRC 重算）。

    只动这一字节：这条判据要测的就是下标本身越界。自己拼一帧等于把协议的第二份实现写进
    测试里，固件一改它就变成一个会说谎的绿（本文件文件头那条口径）。
    """
    payload = bytearray(bytes.fromhex(CASES["metrics_full_3ch"]["hex"]))
    for j, value in enumerate(ids):
        payload[METRICS_HDR.size + j * CHANNEL.size] = value
    head = bytes([0xA5, 0x52, FRAME_METRICS, 1]) + len(payload).to_bytes(2, "little")
    return head + payload + (zlib.crc32(head + payload) & 0xFFFFFFFF).to_bytes(4, "little")


class FakeResolver:
    """桥的端点表：只回答"桥说了什么"，认不出的一个键都不给。"""

    def __init__(self, tables: dict[int, dict[str, str]] | None = None) -> None:
        self.tables = tables or {}
        self.calls: list[tuple[int, tuple[str, ...]]] = []

    def addrs_for(self, gateway_id: int, eps: tuple[str, ...]) -> dict[str, str]:
        table = self.tables.get(gateway_id, {})
        self.calls.append((gateway_id, eps))
        return {ep: table[ep] for ep in eps if ep in table}


class FakePublisher:
    """/ws/live 的收件口替身：记下每条消息落在哪个场地，好把"按场地扇出"钉在这里。"""

    def __init__(self, readers: int = 1, *, boom: bool = False) -> None:
        self.messages: list[tuple[int, dict]] = []
        self.readers = readers
        self.boom = boom

    def publish(self, site_id: int, message: dict) -> int:
        if self.boom:
            raise RuntimeError("subscriber gone")
        self.messages.append((site_id, message))
        return self.readers


class Clock:
    """替掉 `capture.monotonic`：断流判定按墙上时间走就没法测"安静 6 秒"这件事。"""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BrokenDB:
    """写库必炸的库：只用来证明"这一轮没写成"之后那批东西还在。"""

    @contextmanager
    def scope(self):
        raise RuntimeError("disk I/O error")
        yield  # pragma: no cover  —— 语法上必需的那一句，永远不会跑到


@pytest.fixture
def chain(client):
    """两个场地、各一台接入机、各一块板：场地维的归属与隔离必须有第二家可对拍。"""
    nodes = {}
    for slug, name, endpoint, ep, addr in (
        ("main", "主场", "A", SERIAL_EP, SERIAL_ADDR),
        ("away", "客场", "B", UART_EP, UART_ADDR),
    ):
        site = client.post("/api/sites", json={"name": name, "slug": slug}).json()
        relay = client.post(
            "/api/relays", json={"site_id": site["site_id"], "name": f"{name}塔"}
        ).json()
        gateway = client.post(
            "/api/gateways",
            json={"relay_id": relay["relay_id"], "name": f"pi-{slug}", "kind": "usb-bridge"},
        ).json()
        with client.app.state.db.scope() as session:
            reconcile(
                session,
                session.get(Gateway, gateway["gateway_id"]),
                [
                    {
                        "id": ep,
                        "kind": "serial" if addr.startswith("/dev/widscan") else "uart",
                        "label": f"widscan58-{endpoint}",
                        "path": addr,
                        "online": True,
                        "busy": False,
                    }
                ],
                Settings(max_nodes_per_gateway=8),
            )
        listed = client.get(f"/api/nodes?gateway={gateway['gateway_id']}").json()
        row = listed[0]
        assert row["transport"]["addr"] == addr, "节点行的地址口径与 `ingest` 不同源了"
        nodes[slug] = {
            "site_id": site["site_id"],
            "gateway_id": gateway["gateway_id"],
            "node_id": row["node_id"],
            "ep": ep,
            "addr": addr,
            "name": row["display_name"],
        }
    return nodes


def make_pipeline(client, *, resolver=None, publisher=None, **overrides) -> CapturePipeline:
    """一套挂在被测库上的采集管道：后台任务不起，节拍由测试自己一脚一脚踩。"""
    settings = Settings(
        db_path=client.app.state.settings.db_path,
        dialer_enabled=False,
        retention_purge_enabled=False,
        capture_silence_s=5.0,
        **overrides,
    )
    return CapturePipeline(
        client.app.state.db,
        settings,
        resolver=resolver if resolver is not None else FakeResolver(),
        publisher=publisher,
    )


def run(client, pipeline, *chunks: tuple[int, str, bytes]) -> dict:
    """喂字节 → 搬队列 → 踩一拍。测试里这就是"完整的一轮采集"。"""
    for gateway_id, ep, chunk in chunks:
        pipeline.feed(gateway_id, ep, chunk)
    pipeline.drain_all()
    return pipeline.tick()


def summary_rows(client) -> list[SampleSummary]:
    with client.app.state.db.scope() as session:
        return list(
            session.execute(
                select(SampleSummary).order_by(
                    SampleSummary.node_id, SampleSummary.slot, SampleSummary.ts_bucket
                )
            ).scalars()
        )


def event_rows(client) -> list[SignalEvent]:
    with client.app.state.db.scope() as session:
        return list(session.execute(select(SignalEvent).order_by(SignalEvent.event_id)).scalars())


# --- 归属：认得出来的才叫数据 -------------------------------------------------


def test_a_metrics_frame_becomes_ring_points_summary_rows_and_one_live_message(client, chain):
    """一帧三个频道 → 环里三格、库里三行摘要、总线上一条消息，数字与帧里的一字不差。"""
    main = chain["main"]
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
    )

    result = run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))

    assert result == {"attributed": 3, "summaries": 3, "events": 0, "error": None}
    rows = summary_rows(client)
    assert [row.slot for row in rows] == [2, 4, 7]
    assert (rows[0].rssi_last, rows[0].ema_last) == (-36.0, -35.5)
    assert (rows[0].sample_count, rows[0].ema_count) == (1, 1)
    assert {row.node_id for row in rows} == {main["node_id"]}
    assert {row.site_id for row in rows} == {main["site_id"]}
    assert {row.gateway_id for row in rows} == {main["gateway_id"]}
    assert pipeline.store.series(main["gateway_id"], main["addr"], 2)[0]["rssi"] == -36.0

    (site_id, message), = publisher.messages
    assert site_id == main["site_id"]
    assert message["kind"] == "samples"
    assert message["node_id"] == main["node_id"]
    assert message["display_name"] == main["name"]
    assert [channel["id"] for channel in message["channels"]] == [2, 4, 7]
    # 发出去的东西必须可直接 JSON 化：环里存 datetime，线上就只能存字符串。
    assert isinstance(message["channels"][0]["ts"], str)


def test_the_addr_comes_from_the_bridge_not_from_a_guess_about_the_id(client, chain):
    """②a 的板子：节点行落在 `ttyAMA0`，`ep` 却是 `uart:/dev/serial0`。

    这条断言看着只是"归属成功"，实际钉的是实现方式：任何"去掉 `uart:` 前缀当地址"的写法
    在这里会归到一条不存在的地址上，摘要一行都不落，而页面上表现为"接了板子没数据"。
    """
    away = chain["away"]
    pipeline = make_pipeline(
        client, resolver=FakeResolver({away["gateway_id"]: {away["ep"]: away["addr"]}})
    )

    assert run(client, pipeline, (away["gateway_id"], away["ep"], METRICS))["summaries"] == 3
    assert {row.node_id for row in summary_rows(client)} == {away["node_id"]}


def test_points_from_an_endpoint_nobody_claimed_wait_instead_of_disappearing(client, chain):
    """认领不上时攒着，不丢也不落到别人头上；桥下一轮说了话，这批点才进摘要。"""
    main = chain["main"]
    resolver = FakeResolver()
    pipeline = make_pipeline(client, resolver=resolver)
    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()

    assert pipeline.tick() == {"attributed": 0, "summaries": 0, "events": 0, "error": None}
    assert summary_rows(client) == []
    status = pipeline.status()
    assert (status["unattributed"], status["pending_points"]) == (3, 3)

    resolver.tables[main["gateway_id"]] = {main["ep"]: main["addr"]}
    assert pipeline.tick()["attributed"] == 3
    assert pipeline.status()["attributed_late"] == 3
    # 攒着的那批进的是同一份摘要口径，不是另开一条"迟到数据"的路。
    assert {row.node_id for row in summary_rows(client)} == {main["node_id"]}


def test_a_bounded_pending_buffer_reports_what_it_evicted(client, chain):
    """待归属缓冲有上限，挤出去的必须数得出来——否则"少了几秒数据"是查不出来的。

    预算 4 个点、一帧 3 个点 ⇒ 缓冲里只留得下最近那一帧。按整帧挤是刻意的：迟到归属的那一帧
    要原样在 `/ws/live` 上发出去，攒半个帧就只能发一条缺槽位的消息。
    """
    main = chain["main"]
    pipeline = make_pipeline(client, capture_pending_per_endpoint=4)

    for _ in range(3):
        pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()

    status = pipeline.status()
    assert status["pending_points"] == 3
    assert status["unattributed"] == 9
    assert status["pending_dropped"] == 6
    # 环形预算之外还得有这道闸：`max_gateways` 超出的接入机整个不收，并说得出收了几台。
    assert status["gateways"] == 1


# --- 来源：回放的数据不能伪装成实时的 ----------------------------------------


def test_a_replayed_frame_stays_mock_even_after_real_bytes_arrive(client, chain):
    """同一块板先回放再收真字节，来源也不许被洗成 `real`。

    这是 S6 出口判定里"MOCK 与真实可区分"最硬的一条：区分不了，页面上一切数字都失去意义。
    """
    main = chain["main"]
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
    )

    pipeline.feed(main["gateway_id"], main["ep"], METRICS, source="mock")
    pipeline.feed(main["gateway_id"], main["ep"], METRICS, source="real")
    pipeline.drain_all()
    pipeline.tick()

    rows = summary_rows(client)
    assert rows and {row.source for row in rows} == {"mock"}, "真实帧把回放洗成了实时"
    assert {message["source"] for _, message in publisher.messages} == {"mock"}


def test_levels_frames_leave_the_ema_columns_null(client, chain):
    """RSSI 模式一帧都不带 EMA：`ema_*` 必须是 NULL，不能拿 RSSI 顶上去。"""
    main = chain["main"]
    pipeline = make_pipeline(client, publisher=FakePublisher())
    pipeline.feed(main["gateway_id"], main["ep"], LEVELS, transport_addr=main["addr"])
    pipeline.drain_all()
    pipeline.tick()

    rows = summary_rows(client)
    assert [row.slot for row in rows] == [0, 1]
    assert {row.sample_count for row in rows} == {1}
    assert {row.ema_count for row in rows} == {0}
    assert {row.ema_mean for row in rows} == {None}
    assert {row.slot: row.rssi_last for row in rows} == {0: -36.0, 1: -57.5}


def test_frames_that_are_not_channel_points_are_counted_not_stored(client, chain):
    """频谱与日志帧在 S6 不入库，但**必须数得出来**："没接线"与"没数据"不能长成同一个样子。"""
    main = chain["main"]
    pipeline = make_pipeline(client, publisher=FakePublisher())
    pipeline.feed(main["gateway_id"], main["ep"], SPECTRUM)
    pipeline.feed(main["gateway_id"], main["ep"], LOG)
    pipeline.feed(main["gateway_id"], main["ep"], b"OK\n")
    pipeline.drain_all()
    pipeline.tick()

    status = pipeline.status()
    assert status["skipped_frames"] == {"spectrum": 1, "log": 1}
    # `frames` 数的是"解出来的帧"，文本行不算帧：两个数各说各的事。
    assert status["frames"] == 2
    assert status["text_lines"] == 1
    assert status["points_stored"] == 0
    assert summary_rows(client) == []
    # 桥的日志行只是数一下，绝不进环：那不是"某个槽位的一个观测值"。
    assert pipeline.store.series(main["gateway_id"], main["addr"], 0) == []


def test_a_channel_index_the_backend_cannot_read_back_is_counted_not_stored(client, chain):
    """越界档位**落库就再也读不出来**，所以不落库，但要数得出来。

    这条判据不是想象中的：golden 夹具一度把 `id=8` 写进那帧三通道字节里，回放就真演过一条
    "页面画不出、REST 也不肯读"的档。口径有三处权威，且互相咬合——`channel.id` 是 **0-based
    档位下标**（`firmware/src/scan/planner.c:67-68` 填的就是 plan 数组下标，
    `profile.h:8` 的 `PROFILE_MAX_CHANNELS 8` 圈死合法域 0..7），读回侧按 `slots_per_node`
    拒（`routers/samples.py:45`），**显示**才是 1-based（`channelPlanDialog.ts:132` 的 `CH${j+1}`）。
    夹具那边已改回合规下标；这一条钉的是采集侧：越界的数不该有落点，也不该静默。
    """
    main = chain["main"]
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
    )

    result = run(client, pipeline, (main["gateway_id"], main["ep"], metrics_with_ids([2, 4, 9])))

    assert result == {"attributed": 2, "summaries": 2, "events": 0, "error": None}
    assert [row.slot for row in summary_rows(client)] == [2, 4]
    status = pipeline.status()
    assert status["points_stored"] == 2
    assert status["slots_out_of_range"] == 1
    assert pipeline.store.series(main["gateway_id"], main["addr"], 9) == []
    # 实时窗也没有它（`capture.py` 的 `channels` 就是这批点）：越界档位在**环、摘要、推送**三处
    # 都没有落点，只留下 `slots_out_of_range` 这一个数。页面上不会出现一行"画得出来但点不进趋势"
    # 的假可点——那比少一行更难解释。
    (site_id, message), = publisher.messages
    assert [channel["id"] for channel in message["channels"]] == [2, 4]


def test_the_slot_bound_follows_the_setting_not_a_hardcoded_eight(client, chain):
    """把 `slots_per_node` 调到 4，落点就只剩 4 档：这道闸认的是配置，不是固件那一头的 8。

    未来"固件报 40+ 档而场地只配 8 档"（M3）走的就是同一条路径——那时候越界的数不该
    悄悄攒进 SD 卡（R11 的写放大），而该在这里被数出来。
    """
    main = chain["main"]
    pipeline = make_pipeline(
        client, slots_per_node=4,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
    )

    run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))

    assert [row.slot for row in summary_rows(client)] == [2]
    assert pipeline.status()["slots_out_of_range"] == 2
    assert pipeline.status()["points_stored"] == 1


def test_a_frame_of_only_unreadable_channels_leaves_nothing_queued(client, chain):
    """一帧里一个能存的档都没有 → 待归属缓冲一个点都不攒。

    那条队列的预算按**点数**记（`capture_pending_per_endpoint`），不按条目数：留着"零点但
    带一帧"的条目，就等于给一条不合规字节流开了一个无界队列——这恰好是本阶段最不该顺手造成的东西。
    """
    main = chain["main"]
    publisher = FakePublisher()
    pipeline = make_pipeline(client, publisher=publisher)  # 不给归属表：本来一个点都落不了

    pipeline.feed(main["gateway_id"], main["ep"], metrics_with_ids([9, 10, 11]))
    pipeline.drain_all()
    pipeline.tick()

    status = pipeline.status()
    assert status["frames"] == 1 and status["slots_out_of_range"] == 3
    assert status["pending_points"] == 0 and status["unattributed"] == 0
    assert status["points_stored"] == 0
    assert publisher.messages == []
    assert summary_rows(client) == []


# --- 断流：只在"这条流还开着"的前提下说话 ------------------------------------


def test_silence_writes_offline_then_the_next_frame_writes_online(client, chain, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(capture_module, "monotonic", clock)
    main = chain["main"]
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}})
    )

    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    assert pipeline.tick()["events"] == 0

    clock.advance(6.0)
    assert pipeline.tick()["events"] == 1
    clock.advance(30.0)
    # 一直安静只写一行：事件表记的是"发生过什么"，不是"我们轮询了几次"。
    assert pipeline.tick()["events"] == 0

    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    assert pipeline.tick()["events"] == 1

    rows = event_rows(client)
    assert [row.kind for row in rows] == ["node_offline", "node_online"]
    assert {row.site_id for row in rows} == {main["site_id"]}
    assert {row.node_id for row in rows} == {main["node_id"]}
    assert {row.source for row in rows} == {"real"}
    payload = json.loads(rows[0].payload_json)
    assert payload["ep"] == main["ep"]
    assert payload["silence_s"] == pytest.approx(6.0, abs=0.001)
    assert payload["silence_budget_s"] == 5.0
    assert payload["last_seq"] == 7


def test_closing_the_last_page_writes_no_offline(client, chain, monkeypatch):
    """"页面关了"不是"板子拔了"：撤状态、不补事件，也不许留下孤儿运行态。"""
    clock = Clock()
    monkeypatch.setattr(capture_module, "monotonic", clock)
    main = chain["main"]
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}})
    )
    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    pipeline.tick()

    clock.advance(60.0)
    pipeline.untap(Link(gateway_id=main["gateway_id"], ep=main["ep"], url="ws://x/serial"))

    assert pipeline.tick()["events"] == 0
    assert event_rows(client) == []
    assert pipeline.status()["endpoints"] == 0


def test_the_events_of_one_round_are_capped_oldest_first(client, chain, monkeypatch):
    """一轮里事件多于上限时丢的是**最旧**的，且丢了多少要说出来。"""
    clock = Clock()
    monkeypatch.setattr(capture_module, "monotonic", clock)
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver(),
        capture_pending_per_endpoint=1,
        capture_event_limit=1,
    )
    gateways = [chain["main"], chain["away"]]
    for board in gateways:
        pipeline.feed(board["gateway_id"], board["ep"], METRICS, transport_addr=board["addr"])
    pipeline.drain_all()
    pipeline.apply_refs(pipeline.read_refs())

    clock.advance(6.0)
    job = pipeline.prepare()
    assert len(job.events) == 1
    assert pipeline.status()["events_dropped"] == 1


# --- 背压与失败：每种丢法各有一个数字 ----------------------------------------


def test_bytes_from_a_refused_gateway_are_counted_not_stored(client, chain):
    """`max_gateways` 是硬闸：超出的那台整个不收，静默多开一条就把"有硬闸"这句话作废了。"""
    main, away = chain["main"], chain["away"]
    pipeline = make_pipeline(client, max_gateways=1, queue_limit=4)

    for _ in range(3):
        pipeline.feed(main["gateway_id"], main["ep"], METRICS)
        pipeline.feed(away["gateway_id"], away["ep"], METRICS)

    status = pipeline.status()
    assert status["gateways"] == 1
    assert status["gateways_refused"] == 3
    assert [entry["gateway_id"] for entry in status["queues"]] == [main["gateway_id"]]


def test_a_full_queue_drops_the_oldest_and_says_so(client, chain):
    """队列满时丢最旧的、并留下总数：字节进得来但搬不动，本来就是"现场比我们能吃的快"。"""
    main = chain["main"]
    pipeline = make_pipeline(client, queue_limit=2, capture_batch_chunks=1)

    for _ in range(5):
        pipeline.feed(main["gateway_id"], main["ep"], METRICS)

    status = pipeline.status()
    assert status["dropped_chunks"] == 3
    assert status["queues"][0]["dropped"] == 3
    assert status["queues"][0]["peak"] == 2

    assert pipeline.drain_all() == 2
    assert pipeline.status()["frames"] == 2


def test_a_flush_that_failed_keeps_its_rows_for_the_next_round(client, chain):
    """写库失败的一轮不许把摘要与事件变没：整批退回，下一轮原样再写。"""
    main = chain["main"]
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}})
    )
    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    # 归属先备好，好让这一拍**真的攒出格子**——否则测的是"没东西可写"，不是"写失败"。
    pipeline.apply_refs(pipeline.read_refs())

    good_db = pipeline.db
    pipeline.db = BrokenDB()
    result = pipeline.tick()
    assert result["error"].startswith("RuntimeError: disk I/O error")
    assert summary_rows(client) == []
    status = pipeline.status()
    assert (status["retry_rows"], status["ticks"], status["flush_errors"]) == (3, 0, 1)
    assert "采集落库失败" in status["problem"]
    # 读归属也一起失败了：这一轮沿用了上一份表（所以上面那 3 格还归得出属），只留一个计数。
    assert status["node_table_failures"] == 1

    pipeline.db = good_db
    assert pipeline.tick()["summaries"] == 3
    assert len(summary_rows(client)) == 3
    after = pipeline.status()
    assert (after["retry_rows"], after["last_error"], after["problem"]) == (0, None, None)
    # 退回重试的那批不能变成双份：同一格靠 `(node_id, slot, ts_bucket)` 主键合并。
    assert sum(row.sample_count for row in summary_rows(client)) == 3


def test_a_broken_publisher_costs_a_counter_and_nothing_else(client, chain):
    """发布口炸了，环与摘要照常落库——"没人能收到"与"没采到"是两个完全不同的结论。"""
    main = chain["main"]
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=FakePublisher(boom=True),
    )

    assert run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))["summaries"] == 3
    status = pipeline.status()
    assert (status["publisher_errors"], status["published"]) == (1, 0)
    assert status["points_stored"] == 3


# --- 与透传的接缝：观察者不许影响那块板 --------------------------------------


class FakeSocket:
    """一条对客 websocket：收进来的字节从这里出去，页面发的命令从这里进来。"""

    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.closed: tuple[int, str] | None = None
        self._in: asyncio.Queue = asyncio.Queue()

    async def receive(self) -> dict:
        return await self._in.get()

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._in.put_nowait({"type": "websocket.disconnect", "code": code})

    async def leave(self) -> None:
        self._in.put_nowait({"type": "websocket.disconnect", "code": 1000})


class FakeLink:
    """一条假上游：测试往里推字节，`closed` 记下桥被还回了几次钥匙。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.closed: tuple[int, str] | None = None
        self._in: asyncio.Queue = asyncio.Queue()

    async def send(self, payload: bytes) -> None:
        pass

    async def recv(self) -> bytes:
        item = await self._in.get()
        if isinstance(item, UpstreamClosed):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._in.put_nowait(UpstreamClosed(code, reason))

    def push(self, payload: bytes) -> None:
        self._in.put_nowait(payload)


class FakeFactory:
    def __init__(self) -> None:
        self.created: list[FakeLink] = []

    async def connect(self, url: str) -> FakeLink:
        link = FakeLink(url)
        self.created.append(link)
        return link


async def until(predicate, *, cap: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while not predicate():
        if loop.time() - started > cap:
            return False
        await asyncio.sleep(0.01)
    return True


def test_capture_only_sees_bytes_while_a_page_is_watching(client, chain):
    """起真的 worker 与真的节拍：透传一路照旧，采集在页走之后就把那块板的运行态撤干净。

    节拍间隔压到 50 ms 是这条测试的重点所在——它让"认领 → 出仓 → 落库"这段真的由后台循环
    跑完，而不是靠测试自己踩 `tick()`。现场最常见的失败形状恰恰是"手工踩一拍都对，
    后台节拍少一拍"。
    """
    main = chain["main"]
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        capture_flush_interval_s=0.05,
    )
    hub = BridgeHub(FakeFactory(), observer=pipeline)
    link = Link(gateway_id=main["gateway_id"], ep=main["ep"], url="ws://x/serial?ep=y")
    socket = FakeSocket()

    async def scenario() -> None:
        await pipeline.start()
        assert pipeline.status()["running"] is True
        serving = asyncio.create_task(hub.serve(socket, link))
        assert await until(lambda: len(hub.factory.created) == 1)
        board = hub.factory.created[0]
        board.push(METRICS)
        assert await until(lambda: pipeline.status()["frames"] == 1)
        # 透传没有被采集改一个字：页面拿到的就是桥发出的那一段。
        assert socket.sent == [METRICS]
        # 后台节拍自己把它归了属：等到这里才关页，摘要就已经是它写下的了（不是 `stop()` 补的）。
        assert await until(lambda: pipeline.status()["points_stored"] == 3)
        await socket.leave()
        assert await until(lambda: hub.status()["sessions"] == [])
        await serving
        await pipeline.stop()

    asyncio.run(scenario())

    # 摘要已经落了：`stop()` 的收尾拍只是再确认一遍，进程重启不该凭空少一个桶。
    assert len(summary_rows(client)) == 3
    assert pipeline.status()["running"] is False
    assert pipeline.status()["endpoints"] == 0
    assert hub.factory.created[0].closed == (1000, "no more pages")


def test_an_observer_that_throws_never_stops_the_bytes(client, chain):
    """钩子炸了只动自己的计数：那块板的字节必须继续到达浏览器。"""
    class Exploding:
        def __init__(self) -> None:
            self.taps = 0

        def tap(self, link, payload: bytes) -> None:
            self.taps += 1
            raise RuntimeError("queue exploded")

        def untap(self, link) -> None:
            raise RuntimeError("untap exploded")

    observer = Exploding()
    hub = BridgeHub(FakeFactory(), observer=observer)
    link = Link(gateway_id=1, ep=SERIAL_EP, url="ws://x/serial?ep=y")
    socket = FakeSocket()

    async def scenario() -> None:
        serving = asyncio.create_task(hub.serve(socket, link))
        assert await until(lambda: len(hub.factory.created) == 1)
        hub.factory.created[0].push(METRICS)
        hub.factory.created[0].push(METRICS)
        assert await until(lambda: len(socket.sent) == 2)
        await socket.leave()
        assert await until(lambda: hub.status()["sessions"] == [])
        await serving

    asyncio.run(scenario())

    assert observer.taps == 2
    assert socket.sent == [METRICS, METRICS]
    assert hub.status()["observer_failures"] == 3  # 两次 tap + 一次 untap


def test_a_disabled_pipeline_collects_nothing_even_through_the_hook(client, chain):
    """`capture_enabled=0` 是合法部署形态：钩子还在，但一个字节都不收、也不报故障。"""
    main = chain["main"]
    pipeline = make_pipeline(client, capture_enabled=False)
    pipeline.running = True  # 就算"在跑"，没启用也不该收

    pipeline.tap(Link(gateway_id=main["gateway_id"], ep=main["ep"], url="ws://x"), METRICS)

    status = pipeline.status()
    assert (status["enabled"], status["gateways"], status["endpoints"]) == (False, 0, 0)
    assert status["problem"] is None


def test_the_aggregator_instance_refuses_to_capture(client, chain):
    """汇聚实例按设计没有桥：不采集是**形态**，不是 degraded。"""
    pipeline = make_pipeline(client, instance_role="aggregator")

    async def scenario() -> None:
        await pipeline.start()
        await pipeline.stop()

    asyncio.run(scenario())

    status = pipeline.status()
    assert status["running"] is False
    assert status["refused"] == "aggregator_role"
    assert status["problem"] is None


# --- 呈现：/api/health 里那一节 ----------------------------------------------


def test_health_carries_the_capture_section(client):
    """测试实例默认关掉采集，那时这一节要明说关掉了——而不是给一个看着像在跑的空壳。"""
    section = client.get("/api/health").json()["capture"]
    assert section["enabled"] is False
    assert section["running"] is False
    assert section["problem"] is None
    assert section["queues"] == []


def test_a_capture_failure_shows_up_as_a_problem(client):
    """上一轮没写成必须进 `problems`：否则"库在写"与"库写不进"在健康检查里长得一样。"""
    capture = client.app.state.capture
    capture.last_error = "OperationalError: database is locked"
    capture.flush_errors = 2

    body = client.get("/api/health").json()

    assert body["status"] == "degraded"
    assert any("database is locked" in problem for problem in body["problems"])
    assert body["capture"]["flush_errors"] == 2

    capture.last_error = None
    assert client.get("/api/health").json()["status"] == "ok"


def test_crc_errors_are_counted_once_across_a_reconnect(client, chain):
    """帧计数是"这条流的生命周期累计"：撤流时不记"已经折进去多少"，同一批错误会被算两遍。

    总数比真相大是最坏的一种假证据——它会让人去查线，而线没问题。
    """
    main = chain["main"]
    pipeline = make_pipeline(client, resolver=FakeResolver())
    link = Link(gateway_id=main["gateway_id"], ep=main["ep"], url="ws://x")
    broken = bytearray(METRICS)
    broken[-1] ^= 0xFF

    def totals() -> tuple[int, int, int]:
        status = pipeline.status()
        return (status["crc_errors"], status["resync_bytes"], status["frames"])

    pipeline.feed(main["gateway_id"], main["ep"], bytes(broken))
    pipeline.drain_all()
    first = totals()
    assert first[0] == 1, "改坏 CRC 的那一帧必须被认出来"

    pipeline.untap(link)
    assert totals() == first, "撤一条流不许改任何总数"

    # 重连是另一条流：它自己的错误加在总数上，而不是把上一条的份额再数一遍。
    pipeline.feed(main["gateway_id"], main["ep"], bytes(broken))
    pipeline.drain_all()
    assert totals() == (first[0] * 2, first[1] * 2, 0)
