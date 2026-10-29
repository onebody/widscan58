"""`WS /ws/live?site=`：按场地分桶的推送口——S6 出口判据最硬的那一条在这里。

四条"必须说得出证据"的口径决定这个文件的形状：

- **A 场地的订阅收不到 B 场地的事件**：判据写成"给 A 发一条、等到 A 的页面收到它，然后
  B 的页面**一个字都没有**"。只发一条给 B、看 B 收到 B 的那条，是另一种最容易过关的假绿
  （真串场时两条都能收到，测试照样绿），所以正反都要钉。最后一轮直接跑回放→采集→本层，
  钉的是"真数值也走同一条分桶"。
- **快照只回这层真的发布过的东西**：先给 B 发一帧、再让 A 订上，A 的 `samples` 必须是空的。
  这条钉的是"不许从库里捞一行来冒充当前值"——读历史是 REST 的活。
- **背压数得出来**：队列满就丢最旧、计数、页面不许被关掉。发布那一段里没有 `await`，所以
  溢出发生在同一个循环轮次内，是确定的——不靠"发得快一点也许就满了"。
- **离开一定撤**：断的是"桶整条消失"（`sites` 里那个场地没了），不是"订阅数少了一个"——
  后者在计数写错时也能过。三条离开的路（正常关闭、发送对面先走、任务被取消）各钉一次。

参数拒绝那几条走的是与 `/ws/bridge` 同一个口径：**先 accept 再带 reason 关闭**，所以测试读的是
关闭帧里的 `reason`，不是握手失败。为什么这里不用 `TestClient.websocket_connect`：见
`test_ws_bridge.py` 的模块说明——每条连接一个新事件循环，扇出根本没法测。这里直接复用它的
`Page` 与 `until`：同一套"浏览器标签页"，两处断言才可比。
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.capture import CapturePipeline
from app.services.live import SNAPSHOT, LiveHub
from app.services.replay import ReplayRunner

from test_replay import AWAY_ADDR, MAIN_ADDR, StubResolver, seed, write_recording
from test_ws_bridge import Page, until


def event(site_id: int, event_id: str) -> dict:
    """一条与 `capture._publish_event` 同构的事件帧：外层只说"这是一条事件"，记录本身在
    `event` 里，字段名与 `GET /api/events` 那一行一致。

    "同构"这两个字以前是**自称**：helper 发的是 `{"kind": "event", "event_id": …}` 这种平铺，
    而 capture 那边 `{"kind": "event", **row}` 的 `row["kind"]` 会把帧类型盖成 `node_offline`，
    于是 `LiveHub` 的事件窗在真实通路上永远是空的，`kind == "event"` 那几条却全绿。
    现在这层只认这一种形状，`test_a_capture_transition_lands_in_the_next_pages_snapshot`
    拿真实 capture 摆一条进来对拍。
    """
    return {
        "kind": "event",
        "site_id": site_id,
        "ts": "2026-10-29T00:00:00+00:00",
        "event": {"event_id": event_id, "site_id": site_id, "kind": "node_offline", "source": "mock"},
    }


def event_id_of(message: dict):
    """从一条事件帧里取出记录的 id：断言写成一句话，而不是在八个地方各解一次嵌套。"""
    assert message["kind"] == "event", message
    return message["event"]["event_id"]


def sample(site_id: int, node_id: int) -> dict:
    return {"kind": "samples", "node_id": node_id, "site_id": site_id, "source": "mock"}


@pytest.fixture
def sites(client):
    """两个场地：场地维的隔离必须有第二家才对拍，一家验不出串场。"""
    return [
        client.post("/api/sites", json={"name": name, "slug": slug}).json()["site_id"]
        for slug, name in (("main", "主场"), ("away", "客场"))
    ]


async def next_json(page: Page, cap: float = 3.0) -> dict:
    """下一条服务端消息（JSON 解回来）。等不到就报错，绝不挂在"再等等也许就有"里。"""
    message = await page.receive(cap)
    assert message["type"] == "websocket.send", message
    return json.loads(message["text"])


def pending(page: Page) -> int:
    """服务端已经排出、页面还没读走的消息条数——"没收到"这条断言要有地方问。"""
    return page._from_app.qsize()


@asynccontextmanager
async def subscribe(app, url: str):
    """连上并等到 accept 帧。拒绝也是 accept 之后才关，所以这一步两种结局都成立。"""
    async with Page(app, url) as page:
        await page.wait_accepted()
        yield page


class StubSocket:
    """一个只会记账的 websocket 替身：三条离开的路都从 `serve()` 走，页面侧的动作各不同。"""

    def __init__(self, *, boom: bool = False, hang: bool = False) -> None:
        self.sent: list[dict] = []
        self._boom = boom
        self._hang = hang
        self._released = asyncio.Event()

    async def send_json(self, message: dict) -> None:
        if self._boom:
            raise RuntimeError("Cannot call 'send' once a 'disconnect' message was received")
        if self._hang:
            await self._released.wait()
        self.sent.append(message)

    async def receive(self) -> dict:  # pragma: no cover  —— 看门任务先被取消，走不到这里
        await asyncio.Event().wait()
        return {"type": "websocket.disconnect", "code": 1000}


# --- 场地隔离（出口判据）------------------------------------------------------


def test_a_subscriber_never_sees_another_sites_traffic(app, sites):
    """S6 出口判据的原文：A 场地的 `/ws/live` 订阅收不到 B 场地的事件。"""
    main, away = sites

    async def scenario():
        hub = app.state.live
        async with subscribe(app, f"/ws/live?site={main}") as home, subscribe(
            app, f"/ws/live?site={away}"
        ) as guest:
            assert (await next_json(home))["kind"] == SNAPSHOT
            assert (await next_json(guest))["kind"] == SNAPSHOT

            hub.publish(main, event(main, "mine"))
            assert await until(lambda: pending(home) == 1)
            assert event_id_of(await next_json(home)) == "mine"
            assert pending(guest) == 0, "主场的事件漏进了客场的桶"

            hub.publish(away, event(away, "theirs"))
            assert await until(lambda: pending(guest) == 1)
            assert event_id_of(await next_json(guest)) == "theirs"
            assert pending(home) == 0, "客场的事件漏进了主场的桶"

            # 反向再钉一次：主场第二条到了，客场那一位还是一个字都没有。
            hub.publish(main, event(main, "second"))
            assert await until(lambda: pending(home) == 1)
            assert pending(guest) == 0
            assert hub.status()["dropped"] == 0

    asyncio.run(scenario())


def test_the_snapshot_carries_only_the_subscribing_site(app, sites):
    """先给 B 发数据、再让 A 订上：A 的快照必须是空的。

    这条同时钉住"扇出"与"快照"两件事：快照要是从库里读，它会把两个场地的行一起端上来，
    而这一层的定义是"只说我们真的发布过什么"。
    """
    main, away = sites
    hub = app.state.live
    hub.publish(away, sample(away, 11))
    hub.publish(away, event(away, "e"))

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            snapshot = await next_json(page)
            assert (snapshot["kind"], snapshot["site_id"]) == (SNAPSHOT, main)
            assert snapshot["samples"] == []
            assert snapshot["events"] == []

    asyncio.run(scenario())
    assert hub.status()["snapshot_nodes"] == 1, "B 场地那一帧仍该留在 B 的窗里"


def test_a_late_subscriber_catches_up_on_what_was_published(app, sites):
    """已经在发的板子，第二页连上不该看到空白：快照带每块板最后那一帧与最近几条事件。"""
    main, away = sites
    hub = app.state.live
    for node_id in (8, 3, 5):
        hub.publish(main, sample(main, node_id))
    hub.publish(away, sample(away, 99))
    hub.publish(main, event(main, "boom"))

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            snapshot = await next_json(page)
            assert [row["node_id"] for row in snapshot["samples"]] == [3, 5, 8]
            assert [event_id_of(row) for row in snapshot["events"]] == ["boom"]

    asyncio.run(scenario())


def test_the_snapshot_keeps_only_the_most_recent_events(app, sites):
    """事件窗是有界的：留最新 `live_event_window` 条，不是"这台进程活着以来的全部"。"""
    main, _ = sites
    hub = LiveHub(Settings(db_path=app.state.settings.db_path, live_event_window=2))
    app.state.live = hub
    for index in range(5):
        hub.publish(main, event(main, f"e{index}"))

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            snapshot = await next_json(page)
            assert [event_id_of(row) for row in snapshot["events"]] == ["e3", "e4"]
            assert snapshot["samples"] == []

    asyncio.run(scenario())
    assert hub.status()["event_window"] == 2


def test_the_event_window_of_zero_sends_no_events_at_all(app, sites):
    """`live_event_window=0` 是"快照里不带事件"的合法配置，不是"事件一条也发不出去"。"""
    main, _ = sites
    hub = LiveHub(Settings(db_path=app.state.settings.db_path, live_event_window=0))
    app.state.live = hub
    hub.publish(main, event(main, "live"))

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            assert (await next_json(page))["events"] == []
            hub.publish(main, event(main, "after-connect"))
            assert await until(lambda: pending(page) == 1)
            assert event_id_of(await next_json(page)) == "after-connect"

    asyncio.run(scenario())


# --- 参数与拒绝 ---------------------------------------------------------------


def test_a_connection_without_a_site_is_closed_with_a_reason(app):
    async def scenario():
        async with Page(app, "/ws/live") as page:
            assert await page.wait_closed() == (1008, "site_required")

    asyncio.run(scenario())
    assert app.state.live.status()["subscribers"] == 0


def test_a_site_that_does_not_exist_is_closed_with_a_reason(app, sites):
    """手打错的 id 与"这个场地已经删了"都是 `site_not_found`，不是"连上了但一直空"。

    这一条存在的理由是"无数据"本来是个合法状态：不查存在性，运营者只能盯着一页空白猜
    到底是地址写错了，还是板子真没在动。非数字的 `?site=abc` 走同一个理由——它是同一句话。
    """
    missing = max(sites) + 500

    async def scenario():
        async with Page(app, f"/ws/live?site={missing}") as gone:
            assert await gone.wait_closed() == (1008, "site_not_found")
        async with Page(app, "/ws/live?site=abc") as typo:
            assert await typo.wait_closed() == (1008, "site_not_found")

    asyncio.run(scenario())


def test_an_aggregator_instance_may_subscribe_and_gets_an_empty_snapshot(client, sites):
    """汇聚角色**可以**订：那一台按设计不跑采集，所以订上就是一张空快照——这是形态不是故障。

    层级由另一台场地实例写进库里（同 `test_ws_bridge.make_rig` 的那条理由）：S4 之后汇聚实例
    对一切写请求回 405，拿它自己搭夹具的话 `POST /api/sites` 只回一个错误体。
    """
    aggregator = create_app(
        Settings(
            db_path=client.app.state.settings.db_path,
            instance_role="aggregator",
            dialer_enabled=False,
            retention_purge_enabled=False,
            capture_enabled=False,
        )
    )

    async def scenario():
        async with subscribe(aggregator, f"/ws/live?site={sites[0]}") as page:
            snapshot = await next_json(page)
            assert snapshot["samples"] == [] and snapshot["events"] == []
        status = aggregator.state.live.status()
        assert status["problem"] is None and status["subscribers"] == 0

    asyncio.run(scenario())
    # 同一台实例上，写侧照样被角色闸门拦住——"可以订"不等于"可以改"。
    with TestClient(aggregator) as api:
        assert api.get("/api/health").status_code == 200


# --- 背压与撤订阅 -------------------------------------------------------------


def test_a_full_queue_drops_the_oldest_and_never_closes_the_page(db_path, sites):
    """慢页面（合上的笔记本）不该被踢：队列满就丢最旧，并把丢了几条说得出。

    上限取 3 而不是默认的 64：这条测试要的是"溢出发生在同一次发布的循环里"，
    小上限让那一段短到不需要碰运气。`Settings` 是 frozen 的，所以换一台实例而不是改字段。
    """
    main, _ = sites
    limit = 3
    app = create_app(
        Settings(
            db_path=db_path,
            live_queue_limit=limit,
            dialer_enabled=False,
            retention_purge_enabled=False,
            capture_enabled=False,
        )
    )
    hub = app.state.live

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            await next_json(page)
            # 正面等到发送任务把快照发完并回到 `wait()`：之后那一段发布没有 `await`，
            # 溢出就确定发生在同一个循环轮次里。
            assert await until(lambda: hub.status()["sites"][0]["sent"] == 1)
            for index in range(limit + 2):
                hub.publish(main, event(main, f"e{index}"))
            assert hub.status()["dropped"] == 2
            assert hub.status()["sites"][0]["subscribers"] == 1
            assert hub.status()["sites"][0]["queued"] == limit
            received = [await next_json(page) for _ in range(limit)]
            assert [event_id_of(row) for row in received] == [
                f"e{index}" for index in range(2, limit + 2)
            ]
            # 页面还在：丢的是数据，不是连接。
            hub.publish(main, event(main, "after"))
            assert await until(lambda: pending(page) == 1)
            assert event_id_of(await next_json(page)) == "after"

    asyncio.run(scenario())


def test_the_page_leaving_normally_empties_the_bucket(app, sites):
    main, _ = sites
    hub = app.state.live

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            await next_json(page)
            assert {row["site_id"] for row in hub.status()["sites"]} == {main}
        assert hub.status()["sites"] == [], "桶里留下了一个不存在的订阅者"

    asyncio.run(scenario())


def test_a_second_page_leaving_does_not_cut_the_first_one_off(app, sites):
    """同一场地两页，摘掉一条另一条必须还在收：撤订阅按对象摘，不按"这个场地的第几位"。"""
    main, _ = sites
    hub = app.state.live

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as first:
            await next_json(first)
            async with subscribe(app, f"/ws/live?site={main}") as second:
                await next_json(second)
                assert hub.status()["sites"][0]["subscribers"] == 2
            # 第二页正常离开之后，桶里剩的那一位必须是**还活着的那一页**。
            assert hub.status()["subscribers"] == 1
            hub.publish(main, event(main, "still-here"))
            assert await until(lambda: pending(first) == 1)
            assert event_id_of(await next_json(first)) == "still-here"

        assert hub.status()["sites"] == []

    asyncio.run(scenario())


def test_the_three_ways_a_page_can_leave_all_unsubscribe(app, sites):
    """三条路：发送对面先走（RuntimeError）、任务被取消、正常断开。断的都是"桶空了"。"""
    main, _ = sites
    hub = LiveHub(Settings(db_path=app.state.settings.db_path))

    async def gone_mid_send():
        socket = StubSocket(boom=True)
        await hub.serve(socket, main)
        assert hub.status()["send_errors"] == 1
        assert hub.status()["sites"] == []

    async def cancelled():
        socket = StubSocket(hang=True)
        task = asyncio.create_task(hub.serve(socket, main))
        assert await until(lambda: hub.status()["subscribers"] == 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert hub.status()["sites"] == []

    async def normal():
        socket = StubSocket()
        task = asyncio.create_task(hub.serve(socket, main))
        # 先等到这一位真的进了桶：任务在被调度起来之前，`publish` 找不到任何订阅者。
        assert await until(lambda: hub.status()["subscribers"] == 1)
        hub.publish(main, event(main, "one"))
        assert await until(lambda: len(socket.sent) == 2)
        task.cancel()  # 真实进程里这一位是被 `receive()` 的 disconnect 叫醒后自然收摊
        await asyncio.gather(task, return_exceptions=True)
        assert hub.status()["sites"] == []
        assert [row["kind"] for row in socket.sent] == [SNAPSHOT, "event"]

    asyncio.run(gone_mid_send())
    asyncio.run(cancelled())
    asyncio.run(normal())


def test_the_hub_never_waits_on_a_page(app, sites):
    """`publish()` 是同步的：采集层在事件循环上调它，等一次 socket 写就把节拍交给了最慢的页。

    顺带钉两个数：没有订阅者时返回 0 但**照样记快照**（下一页连上看不看得到东西就取决于这句），
    以及 `delivered` 是"消息 × 页"而不是"消息"。
    """
    main, away = sites
    hub = app.state.live
    assert hub.publish(away, sample(away, 4)) == 0
    assert hub.status()["snapshot_nodes"] == 1
    assert hub.published == 1 and hub.delivered == 0

    async def scenario():
        async with subscribe(app, f"/ws/live?site={main}") as page:
            await next_json(page)
            assert hub.publish(main, sample(main, 5)) == 1
            assert hub.published == 2 and hub.delivered == 1
            assert await until(lambda: pending(page) == 1)

    asyncio.run(scenario())


# --- 装配与健康检查 -----------------------------------------------------------


def test_the_capture_layer_publishes_into_the_live_hub(client):
    """接线也要钉住：`app.state.capture.publisher` 就是 `app.state.live`。

    这条不是形式主义——两层各自都测过，中间那根线要是没接上，`/api/health` 的
    `capture.published_readers` 会一直是 0，而页面只会显示"无数据"。
    """
    assert client.app.state.capture.publisher is client.app.state.live
    assert client.app.state.replay.pipeline is client.app.state.capture


def test_health_carries_the_live_section(client, sites):
    main = sites[0]
    body = client.get("/api/health").json()
    assert body["live"]["sites"] == []
    assert body["live"]["problem"] is None
    assert body["status"] == "ok", body["problems"]
    assert body["live"]["queue_limit"] == client.app.state.settings.live_queue_limit

    # 没人在连也要说得出口（`sites: []`）——"这层没做"和"现在确实没人订"是两个结论。
    client.app.state.live.publish(main, sample(main, 2))
    after = client.get("/api/health").json()
    assert after["live"]["published"] == 1
    assert after["live"]["snapshot_nodes"] == 1
    assert after["live"]["delivered"] == 0
    assert after["status"] == "ok"


def test_the_queue_limit_below_one_is_refused_at_construction():
    """`live_queue_limit=0` 等于"订上 `/ws/live` 也永远收不到一条"，这种配置不许活着进运行期。"""
    with pytest.raises(ValueError, match="live_queue_limit"):
        Settings(live_queue_limit=0)
    with pytest.raises(ValueError, match="live_event_window"):
        Settings(live_event_window=-1)


# --- 端到端：回放 → 采集 → 本层 → 两页 ----------------------------------------


def test_replayed_numbers_reach_only_their_own_site(client, tmp_path):
    """S6 的整条数据通路：录制文件 → 采集 → `/ws/live`，两个场地各开一页。

    前面几条用的是手搭的消息字典（那是这一层的契约形状）；这一条不搭——数字由
    `make_replay_recording.py` 现产、由 `capture` 解析落库、由本层分桶，所以"真数值也不会
    串场"这一句第一次有证据。`source` 必须是 `mock`：出口判据的另一半。
    """
    main = seed(client, "main", "主场", MAIN_ADDR)
    away = seed(client, "away", "客场", AWAY_ADDR, kind="uart")
    directory = tmp_path / "rec"
    write_recording(directory, "home-board", seconds=2.0, hz=5.0, addr=MAIN_ADDR)
    write_recording(directory, "away-board", seconds=2.0, hz=5.0, addr=AWAY_ADDR)

    settings = Settings(
        db_path=client.app.state.settings.db_path,
        dialer_enabled=False,
        retention_purge_enabled=False,
        capture_flush_interval_s=0.05,
        capture_silence_s=60.0,
        replay_dir=directory,
    )
    hub = LiveHub(settings)
    pipeline = CapturePipeline(
        client.app.state.db, settings, resolver=StubResolver(), publisher=hub
    )
    runner = ReplayRunner(client.app.state.db, settings, pipeline)
    # 路由只认 `app.state.live`，而这一轮的采集与回放是我们自己起的那一套。
    client.app.state.live = hub

    async def scenario():
        await pipeline.start()
        await runner.start()
        try:
            async with subscribe(
                client.app, f"/ws/live?site={main['site_id']}"
            ) as home, subscribe(
                client.app, f"/ws/live?site={away['site_id']}"
            ) as guest:
                home_rows = [await next_json(home)]
                guest_rows = [await next_json(guest)]
                deadline = asyncio.get_running_loop().time() + 8.0
                while len(home_rows) < 3 or len(guest_rows) < 3:
                    if asyncio.get_running_loop().time() > deadline:
                        raise AssertionError(f"两页各自没收到 3 条：{home_rows} / {guest_rows}")
                    for page, rows in ((home, home_rows), (guest, guest_rows)):
                        while pending(page):
                            rows.append(await next_json(page, cap=0.5))
                    await asyncio.sleep(0.02)
        finally:
            await runner.stop()
            await pipeline.stop()
        return home_rows, guest_rows, runner.status(), pipeline.status()

    home_rows, guest_rows, replay_status, capture_status = asyncio.run(scenario())

    assert replay_status["refused"] is None, replay_status
    assert capture_status["flush_errors"] == 0

    live = [row for row in home_rows if row["kind"] == "samples"]
    assert live, "主场地一页样本都没收到"
    assert {row["site_id"] for row in live} == {main["site_id"]}
    assert {row["node_id"] for row in live} == {main["node_id"]}
    assert {row["source"] for row in live} == {"mock"}
    # 数字是真的：`channels` 由解析器产出，槽位与 golden 帧一致。
    assert {channel["id"] for row in live for channel in row["channels"]} == {2, 4, 7}

    away_live = [row for row in guest_rows if row["kind"] == "samples"]
    assert {row["site_id"] for row in away_live} == {away["site_id"]}
    assert {row["node_id"] for row in away_live} == {away["node_id"]}
    # 两页的快照里也不该有对方那块板。
    assert {row["node_id"] for row in home_rows[0]["samples"]} <= {main["node_id"]}
    assert {row["node_id"] for row in guest_rows[0]["samples"]} <= {away["node_id"]}


def test_a_real_break_in_the_stream_is_in_the_next_pages_snapshot(client, tmp_path):
    """真实采集发的断流事件，必须既当场送到连着的那一页、也留在后一页的快照里。

    这一条是为本轮修掉的那个缺陷写的：`capture` 以前发的是 `{"kind": "event", **row}`，
    `row["kind"]`（`node_offline`）把帧类型盖掉，于是 `LiveHub` 的事件窗**在真实通路上永远是
    空的**——而上面那些用手搭消息的判据全绿，因为手搭的那条确实是 `kind == "event"`。
    判据因此只能由真采集来给：回放放完 → 静默超预算 → 断流一行落库并投递。
    """
    main = seed(client, "main", "主场", MAIN_ADDR)
    directory = tmp_path / "rec"
    write_recording(directory, "home-board", seconds=1.0, hz=10.0, addr=MAIN_ADDR)
    settings = Settings(
        db_path=client.app.state.settings.db_path,
        dialer_enabled=False,
        retention_purge_enabled=False,
        capture_flush_interval_s=0.05,
        capture_silence_s=0.6,
        replay_dir=directory,
    )
    hub = LiveHub(settings)
    pipeline = CapturePipeline(
        client.app.state.db, settings, resolver=StubResolver(), publisher=hub
    )
    runner = ReplayRunner(client.app.state.db, settings, pipeline)
    client.app.state.live = hub
    url = f"/ws/live?site={main['site_id']}"

    def offline(messages: list[dict]) -> list[dict]:
        return [
            message["event"]
            for message in messages
            if message["kind"] == "event" and message["event"]["kind"] == "node_offline"
        ]

    async def scenario():
        await pipeline.start()
        await runner.start()
        try:
            async with subscribe(client.app, url) as page:
                seen: list[dict] = []
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 10.0
                while not offline(seen):
                    if loop.time() > deadline:
                        raise AssertionError(f"没等到断流事件：{[m['kind'] for m in seen]}")
                    while pending(page):
                        seen.append(await next_json(page, cap=0.5))
                    await asyncio.sleep(0.02)
            # 连着的那一页走了；事件窗里那一条该留给下一页。
            async with subscribe(client.app, url) as late:
                snapshot = await next_json(late)
        finally:
            await runner.stop()
            await pipeline.stop()
        return offline(snapshot["events"])

    caught = asyncio.run(scenario())

    assert len(caught) == 1, caught
    event = caught[0]
    assert (event["site_id"], event["node_id"]) == (main["site_id"], main["node_id"])
    # `source` 是 `mock`：回放松的数不许在快照里冒充实时观测（出口判据的另一半）。
    assert event["source"] == "mock"
    assert event["payload"]["silence_budget_s"] == 0.6

    # 同一条记录、两条到达路径：WS 里那个 id 就是库里那一行的主键，REST 认它。
    row = client.get(f"/api/events/{event['event_id']}")
    assert row.status_code == 200, row.json()
    assert row.json()["kind"] == "node_offline"
    assert (row.json()["site_id"], row.json()["node_id"]) == (main["site_id"], main["node_id"])
