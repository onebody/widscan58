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
from datetime import timedelta
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
            "relay_id": relay["relay_id"],
            "gateway_id": gateway["gateway_id"],
            "node_id": row["node_id"],
            "ep": ep,
            "addr": addr,
            "name": row["display_name"],
        }
    return nodes


@pytest.fixture
def odd_ids(client, chain):
    """一块"三个身份两两不同"的板：批里的 `site_id`/`gateway_id`/`node_id` 不许互相顶。

    `chain` 给的每块板恰好 site==gateway==node（1 与 2），于是"把 `gateway_id` 抄进 `node_id`"
    这类错在那里完全看不出来——同一条断言会照样绿。身份是运行时盖进判定行的唯一凭据，只有用
    一组互不相等的数才测得出抄错了栏。
    """
    main = chain["main"]
    gateway = client.post(
        "/api/gateways",
        json={"relay_id": main["relay_id"], "name": "pi-odd", "kind": "usb-bridge"},
    ).json()
    endpoints = [
        {
            "id": f"serial:/dev/odd-{n}",
            "kind": "serial",
            "label": f"odd-{n}",
            "path": f"/dev/odd-{n}",
            "online": True,
            "busy": False,
        }
        for n in (1, 2)
    ]
    with client.app.state.db.scope() as session:
        reconcile(
            session,
            session.get(Gateway, gateway["gateway_id"]),
            endpoints,
            Settings(max_nodes_per_gateway=8),
        )
    listed = client.get(f"/api/nodes?gateway={gateway['gateway_id']}").json()
    second = [row for row in listed if row["transport"]["addr"] == "/dev/odd-2"][0]
    out = {
        "site_id": main["site_id"],
        "gateway_id": gateway["gateway_id"],
        "node_id": second["node_id"],
        "ep": "serial:/dev/odd-2",
        "addr": "/dev/odd-2",
    }
    assert len({out["site_id"], out["gateway_id"], out["node_id"]}) == 3, out
    return out


def make_pipeline(
    client, *, resolver=None, publisher=None, plugins=None, **overrides
) -> CapturePipeline:
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
        plugins=plugins,
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


class FakeFeeder:
    """插件运行时的收件口替身：只回答两件事——"现在有没有人收"和"这批我怎么处理"。

    `armed` 做成 property 是故意的：采集侧每帧都要问它，测试要数得出**问了几次**，好把
    "全停用时这条热路径上只做一次真值判断"这件事钉成可断言的形状。
    """

    def __init__(self, *, armed: bool = True, outcome: str = "queued", boom: bool = False) -> None:
        self._armed = armed
        self.outcome = outcome
        self.boom = boom
        self.armed_checks = 0
        self.seen: list = []

    @property
    def armed(self) -> bool:
        self.armed_checks += 1
        return self._armed

    def offer(self, batches) -> str:
        self.seen.append(tuple(batches))
        if self.boom:
            raise RuntimeError("runtime gone")
        return self.outcome


def test_an_attributed_frame_is_handed_to_the_plugins_whole(client, odd_ids):
    """一帧归属成功 = 投喂一批：身份来自节点行，读数来自这一帧，两条不许各走一路。

    钉这三个身份字段是因为它们是"判定属谁"的唯一凭据：运行时把它们原样盖进 `plugin_finding`，
    插件自己说的不算（见 `plugins/base.py`）。所以这里要证明的是**归属完成的那一帧**才投喂，
    且投喂的就是那一帧。
    """
    node = odd_ids
    feeder = FakeFeeder()
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({node["gateway_id"]: {node["ep"]: node["addr"]}}),
        publisher=publisher,
        plugins=feeder,
    )

    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.drain_all()
    pipeline.tick()  # 归属表在这一拍才读进来：`_emit` 是这一拍里发生的

    (window,) = feeder.seen
    (batch,) = window  # 单帧也走窗：尾窗由 `prepare()`（tick 兜底）关闭
    assert (batch.site_id, batch.gateway_id, batch.node_id) == (
        node["site_id"],
        node["gateway_id"],
        node["node_id"],
    )
    assert batch.source == "real"
    assert [p.slot for p in batch.points] == [2, 4, 7]
    assert (batch.points[0].rssi, batch.points[0].ema) == (-36.0, -35.5)
    # 与 `/ws/live` 那条消息同一帧、同一时刻：两路口径早晚对不上，是因为它们各自读了一遍时间。
    assert batch.seq == publisher.messages[0][1]["seq"]
    assert batch.ts == pipeline.store.series(node["gateway_id"], node["addr"], 2)[0]["ts"]
    assert pipeline.status()["plugin_batches"] == 1


def test_frames_of_one_node_share_a_feed_window(client, odd_ids):
    """投递按窗攒、到点即关、尾窗由节拍兜底——三条路都有断言，少一条就是只能靠读代码知道的行。

    真机 236 帧/s 逐帧投递时三支插件各丢 87.5%（2026-10-30 实测），攒窗是修它的那一步：
    这里钉住的是它的三个面——同窗同节点合一批、窗到期被下一帧关掉、`tick()` 关掉还没到期的尾窗。
    """
    node = odd_ids
    feeder = FakeFeeder()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({node["gateway_id"]: {node["ep"]: node["addr"]}}),
        plugins=feeder,
    )

    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.drain_all()
    assert feeder.seen == [], "帧在窗里攒着，还没有任何投递"

    pipeline.tick()
    assert len(feeder.seen) == 1 and len(feeder.seen[0]) == 2, "同一节点的两帧合成一窗"

    # 到点即关：把窗起点拨回窗口时长之前，下一帧就该先投旧窗、再开新窗
    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.drain_all()
    ((key, window),) = pipeline._feed_windows.items()
    window.since -= timedelta(seconds=capture_module.FEED_WINDOW_S * 2)
    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.drain_all()
    assert len(feeder.seen) == 2 and len(feeder.seen[1]) == 1
    assert len(pipeline._feed_windows[key].batches) == 1

    pipeline.tick()
    assert len(feeder.seen) == 3, "尾窗由节拍兜底关掉"
    assert pipeline.status()["plugin_batches"] == 3


def test_a_frame_nobody_claimed_is_never_handed_to_a_plugin(client, chain):
    """认不出是谁的点就不许投喂——那等于让插件替一个没有身份的读数产出判定。

    与"归不了属的点不落摘要、不发布"是同一条纪律（`_attribute` 的注释）：投喂是第三条出仓的路，
    漏了它就会出现"摘要里没有、判定里却有"这种只能靠猜才能对齐的现象。
    """
    main = chain["main"]
    feeder = FakeFeeder()
    pipeline = make_pipeline(client, resolver=FakeResolver(), plugins=feeder)

    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    pipeline.tick()

    assert feeder.seen == []
    assert pipeline.status()["plugin_batches"] == 0


def test_a_late_attributed_frame_is_handed_too(client, chain):
    """迟到归属的那一帧也走同一条投喂路：钩子挂在 `_emit`，不是挂在"实时"那一段。

    缓冲里攒的帧一旦认领上就要原样出去（环、摘要、`/ws/live` 三条都是），插件是第四条。少了
    这一条的现象很具体：刚连上的头几秒数据在页面上看得到、在判定里看不到。
    """
    main = chain["main"]
    feeder = FakeFeeder()
    resolver = FakeResolver()
    pipeline = make_pipeline(client, resolver=resolver, plugins=feeder)
    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    assert feeder.seen == []

    resolver.tables[main["gateway_id"]] = {main["ep"]: main["addr"]}
    pipeline.tick()

    (window,) = feeder.seen
    (batch,) = window
    assert batch.node_id == main["node_id"]
    assert [p.slot for p in batch.points] == [2, 4, 7]


def test_a_replayed_frame_reaches_the_plugin_as_mock(client, chain):
    """来源是采集层唯一说得清"这是录的还是live的"的一栏，插件与判定都得跟着它。

    任务书要求模拟数据必须标 `MOCK/DEMO`：这一格要是漏了，回放一整晚就会在判定表里留下一串
    看起来像现场的越限记录。
    """
    main = chain["main"]
    feeder = FakeFeeder()
    pipeline = make_pipeline(
        client, resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        plugins=feeder,
    )

    pipeline.feed(main["gateway_id"], main["ep"], METRICS, source="mock")
    pipeline.drain_all()
    pipeline.tick()

    (window,) = feeder.seen
    (batch,) = window
    assert batch.source == "mock"


def test_everything_disabled_costs_the_capture_one_check_per_frame(client, chain):
    """三支插件全停用 = 采集这边除了问一次"有没有人收"，什么都不做。

    这是 S7 整段回滚判据（"全部停用即等效不存在"）在**热路径**上的形状：如果停用还留着构造批、
    投喂、计数这些动作，那"关掉插件"就只是关掉产出，而不是关掉这件事本身。
    """
    main = chain["main"]
    feeder = FakeFeeder(armed=False)
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
        plugins=feeder,
    )

    result = run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))

    assert feeder.seen == []
    assert feeder.armed_checks == 1
    status = pipeline.status()
    assert (status["plugin_batches"], status["plugin_refused"], status["plugin_errors"]) == (0, 0, 0)
    # 采集本身一个字都没变：关掉插件不改动 S6 的任何产出。
    assert result == {"attributed": 3, "summaries": 3, "events": 0, "error": None}
    assert publisher.messages[0][1]["kind"] == "samples"


def test_a_refused_batch_is_counted_and_the_frame_still_goes_out(client, chain):
    """插件那头收不下，不改采集这头的任何一件事：丢的只是判定的一条输入，且必须数得出来。

    "不得破坏原有功能"落在这一格：透传、环、摘要、`/ws/live` 都不许因为插件的背压而少一条。
    """
    main = chain["main"]
    feeder = FakeFeeder(outcome="refused")
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
        plugins=feeder,
    )

    result = run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))

    status = pipeline.status()
    assert (status["plugin_batches"], status["plugin_refused"]) == (1, 1)
    assert (result["summaries"], len(publisher.messages)) == (3, 1)

    # `dropped_oldest` 不算这里的"拒收"：那一批已经交出去了，丢的是运行时队里最旧的上一批，
    # 主语在运行时那边（它自己的 `dropped_batches` 数着）。混进这里就会两头各数一遍。
    feeder.outcome = "dropped_oldest"
    pipeline.feed(main["gateway_id"], main["ep"], METRICS)
    pipeline.drain_all()
    pipeline.tick()  # 投递按窗攒：这一次的账要等尾窗关掉才出现

    status = pipeline.status()
    assert (status["plugin_batches"], status["plugin_refused"]) == (2, 1)


def test_a_feeder_that_throws_costs_a_counter_and_nothing_else(client, chain):
    """投喂口抛异常不许停在半帧上：判定是采集的**下游**，它坏了轮不到采集停。

    任务书 §10.4 那句"插件故障不得拖垮采集"在代码里就是这一格。这里连"数据已经入了环与累加器"
    都要钉住——发布口在投喂之后，抛出去就会留下一条"环里有、页面上没有"的缝。
    """
    main = chain["main"]
    feeder = FakeFeeder(boom=True)
    publisher = FakePublisher()
    pipeline = make_pipeline(
        client,
        resolver=FakeResolver({main["gateway_id"]: {main["ep"]: main["addr"]}}),
        publisher=publisher,
        plugins=feeder,
    )

    result = run(client, pipeline, (main["gateway_id"], main["ep"], METRICS))

    assert pipeline.status()["plugin_errors"] == 1
    assert pipeline.status()["plugin_batches"] == 0
    assert result["summaries"] == 3
    assert len(publisher.messages) == 1


def test_the_real_runtime_turns_a_captured_frame_into_a_finding(client, odd_ids):
    """接线对拍：采到的字节经**真运行时**落成一条判定，身份与节点行一致。

    上面几条都挂在替身上，而替身会说谎——它签名对了就行，真运行时的 `offer()` 却要认队列、
    认上限、认启用状态。这条用真 `PluginRuntime` 走完整条链，钉的正是"两边说的是同一个协议"：
    判定行上的 `node_id`/`source`/`producer` 都来自运行时盖章，不是插件填的。板子取 `odd_ids`
    （三个身份互不相等），否则"抄错栏"在这条路上也是看不见的。

    这里用 `alert-engine`：它读绝对越限，单帧就说话（`anomaly-detector` 要先攒基线，拿它测接线
    得先喂好几帧，测的就不是这一件事了）。
    """
    from app.models import Node, PluginFinding, PluginRegistry
    from app.services.plugin_runtime import PluginRuntime

    node = odd_ids
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = True
    runtime = PluginRuntime(
        client.app.state.db,
        client.app.state.settings,
        implementations={"alert-engine": _AlertEngineForWiring},
    )
    runtime.refresh()
    pipeline = make_pipeline(
        client, resolver=FakeResolver({node["gateway_id"]: {node["ep"]: node["addr"]}}),
        plugins=runtime,
    )

    pipeline.feed(node["gateway_id"], node["ep"], METRICS)
    pipeline.drain_all()
    pipeline.tick()
    runtime.run_once()

    rows = []
    with client.app.state.db.scope() as session:
        rows = list(session.execute(select(PluginFinding)).scalars().all())
    assert rows, "采到帧、插件启用，却一条判定都没落库"
    assert {row.node_id for row in rows} == {node["node_id"]}
    assert {row.site_id for row in rows} == {node["site_id"]}
    assert {row.gateway_id for row in rows} == {node["gateway_id"]}
    assert {row.source for row in rows} == {"real"}
    assert rows[0].producer.startswith("rule:alert-engine@")
    # 节点身份来自归属那一步，投喂没有另开一条查库的路：`Node` 行仍然是唯一的一份。
    with client.app.state.db.scope() as session:
        assert session.get(Node, node["node_id"]).transport_addr == node["addr"]


class _AlertEngineForWiring:
    """只说话一次的最小插件：这条测试要的是"判定落库的形状"，不是规则本身。

    真 `AlertEngine` 的产出条数随阈值与抑制窗口变，把那些一起钉进来会让这条测试变成
    `test_plugin_rules.py` 的第二份拷贝。
    """

    CONFIG_KEYS = ("threshold_db",)

    def feed(self, batch, config):
        from app.plugins.base import Finding

        return [Finding(kind="wired", slot=batch.points[0].slot, metric="rssi", value=-40.0)]

    def stats(self) -> dict[str, int]:
        return {}
