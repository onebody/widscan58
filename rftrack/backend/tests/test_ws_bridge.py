"""`WS /ws/bridge/{ep}`：让原 SPA 经后端连板（契约 §4 的"字节透传"行）。

三条口径决定这个文件的形状，全部来自"改了就不是透传"：

- **上游就是桥自己的 `/serial?ep=<id>`**：`serial:` / `uart:` / `node:` 三种 id 由桥自己分发
  （`uart:` 会连带停 RotorHazard）。后端不另发明一条路径，也不预先校验 id 存不存在——
  桥自己会用 `unknown endpoint <ep>` 回答，那个理由比我们的猜测可信。
- **只读扇出 + 写命令单写者锁**：同一块板可以几页一起看，但命令只能有一个来源。抢不到
  写权的那页拿到的关闭理由是 `busy`，与桥拒绝第二把钥匙时用的是同一个词。
- **文本帧不透传**：桥的 `readFrames` 明写"1 text / 10 pong: unused"，后端替它收下就是改语义。

为什么这里不用 `TestClient.websocket_connect`：实测 starlette 0.52.1 的 `testclient.py:671`
每调一次就新起一个 blocking portal（**一个全新的事件循环**），两条连接落在两个循环里，
"扇出"与"单写者"根本没法测。所以本文件用 `Page` 直接驱动 ASGI 接口：两个读者共用一个
循环，仍然不起任何临时服务。断线/热拔插属真机待验（§7 S3 行），不在这里冒充已验。
"""

import asyncio
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.ws_bridge import UpstreamClosed

EP = "serial:/dev/widscan58-1"
HOME = "192.168.99.11"
AWAY = "192.168.99.12"


class FakeUpstream:
    """一条假的上游连接：测试往里推字节，从 `sent` 里读页面发来的命令。"""

    def __init__(self, url: str) -> None:
        self.url = url
        self.sent: list[bytes] = []
        self.closed: tuple[int, str] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._in: asyncio.Queue = asyncio.Queue()

    # --- 上游协议那一侧（hub 调用，跑在 app 的循环里）---

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)

    async def recv(self) -> bytes:
        item = await self._in.get()
        if isinstance(item, UpstreamClosed):
            raise item
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)
        self._in.put_nowait(UpstreamClosed(code, reason))

    # --- 测试那一侧：从别的线程投递也要落回 app 的那个循环 ---

    def push(self, payload: bytes) -> None:
        self._post(payload)

    def drop(self, code: int = 1008, reason: str = "busy") -> None:
        self._post(UpstreamClosed(code, reason))

    def _post(self, item) -> None:
        if self._loop is None:
            self._in.put_nowait(item)  # 还没 connect 过：此刻本来就在同一个循环里
        else:
            self._loop.call_soon_threadsafe(self._in.put_nowait, item)


class FakeFactory:
    """记下每一次上游连接：`len(created)` 就是"这条端点被开了几把钥匙"。"""

    def __init__(self) -> None:
        self.created: list[FakeUpstream] = []

    async def connect(self, url: str) -> FakeUpstream:
        link = FakeUpstream(url)
        link._loop = asyncio.get_running_loop()
        self.created.append(link)
        return link

    def link_for(self, host: str) -> FakeUpstream:
        return next(link for link in self.created if urlsplit(link.url).netloc == f"{host}:8080")


@dataclass
class Rig:
    """一套库、一个 app、若干台已登记的接入机。"""

    app: object
    client: TestClient
    factory: FakeFactory
    gateway_ids: list[int] = field(default_factory=list)

    @property
    def gateway_id(self) -> int:
        return self.gateway_ids[0]

    def sessions(self) -> list[dict]:
        return self.app.state.bridge.status()["sessions"]

    async def wait_upstreams(self, count: int = 1) -> "FakeUpstream":
        """等第 count 条上游真的被开出来。

        accept 之后开上游还要先过一次 `to_thread` 的库查询，所以"页面已连上"并不等于
        "钥匙已拧开"。不写这条 helper 的测试会在 `created[0]` 上摸到空列表——那是同步问题，
        不是透传问题，别让它伪装成失败。
        """
        assert await until(lambda: len(self.factory.created) >= count), f"没等到 {count} 条上游"
        return self.factory.created[count - 1]


def make_rig(db_path, *, instance_role: str = "site", hosts=(HOME,)) -> Rig:
    """一套库、一个被测 app、若干台已登记的接入机。

    层级由**另一个站点实例**写进库里，再用被测角色的 app 去读：S4 之后汇聚实例对
    一切写请求回 405，拿被测 app 自己搭脚手架的话，`POST /api/sites` 只会回一个
    没有 `site_id` 的错误体，测试就死在夹具里——那是角色闸门，不是桥的问题。
    这也正是真实形态：汇聚实例的库是场地实例写出来的，它自己不动笔。
    """
    seed_app = create_app(
        Settings(db_path=db_path, dialer_enabled=False, instance_role="site"),
        upstream_factory=FakeFactory(),
    )
    seed = TestClient(seed_app)
    site = seed.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
    relay = seed.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}
    ).json()

    factory = FakeFactory()
    app = create_app(
        Settings(db_path=db_path, dialer_enabled=False, instance_role=instance_role),
        upstream_factory=factory,
    )
    client = TestClient(app)
    rig = Rig(app, client, factory)
    for index, host in enumerate(hosts):
        row = seed.post(
            "/api/gateways",
            json={
                "relay_id": relay["relay_id"],
                "name": f"pi-{index}",
                "kind": "usb-bridge",
                "base_url": f"http://{host}:8080",
            },
        ).json()
        rig.gateway_ids.append(row["gateway_id"])
    return rig


class Page:
    """一个"浏览器标签页"：直接对 ASGI 接口收发 websocket 消息。"""

    def __init__(self, app, url: str) -> None:
        path, _, query = url.partition("?")
        self.scope = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(b"host", b"testserver")],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
            "subprotocols": [],
            "root_path": "",
        }
        self._to_app: asyncio.Queue = asyncio.Queue()
        self._from_app: asyncio.Queue = asyncio.Queue()
        self._app = app
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> "Page":
        self._task = asyncio.create_task(
            self._app(self.scope, self._to_app.get, self._from_app.put)
        )
        await self._to_app.put({"type": "websocket.connect"})
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self._task, 3.0)

    async def send_bytes(self, payload: bytes) -> None:
        await self._to_app.put({"type": "websocket.receive", "bytes": payload})

    async def send_text(self, text: str) -> None:
        await self._to_app.put({"type": "websocket.receive", "text": text})

    async def receive(self, cap: float = 3.0) -> dict:
        """取下一条服务端消息。等不到就报错——测试绝不许挂在"再等等也许就有"里。"""
        return await asyncio.wait_for(self._from_app.get(), cap)

    async def receive_bytes(self, cap: float = 3.0) -> bytes:
        message = await self.receive(cap)
        assert message["type"] == "websocket.send", message
        return message["bytes"]

    async def wait_accepted(self) -> None:
        assert (await self.receive())["type"] == "websocket.accept"

    async def wait_closed(self, cap: float = 3.0) -> tuple[int, str]:
        """等到服务端关闭。拒绝也是"先 accept 再 close"——理由只有这样才能传到页面上。"""
        while True:
            message = await self.receive(cap)
            if message["type"] == "websocket.close":
                return message["code"], message.get("reason", "")


def url_for(ep: str, gateway_id, **extra) -> str:
    query = f"gateway={gateway_id}" if gateway_id is not None else ""
    query += "".join(f"&{key}={value}" for key, value in extra.items())
    return f"/ws/bridge/{ep}" + (f"?{query}" if query else "")


async def until(predicate, *, cap: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while not predicate():
        if loop.time() - started > cap:
            return False
        await asyncio.sleep(0.01)
    return True


# --- 透传本身 -----------------------------------------------------------------


def test_bytes_go_both_ways_and_the_upstream_is_the_bridges_own_serial_path(db_path):
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            await page.wait_accepted()
            link = await rig.wait_upstreams()
            await page.send_bytes(b"R")
            assert await until(lambda: link.sent == [b"R"])
            link.push(b"\xaa\xbb")
            assert await page.receive_bytes() == b"\xaa\xbb"

    asyncio.run(scenario())
    parts = urlsplit(rig.factory.created[0].url)
    assert (parts.scheme, parts.netloc, parts.path) == ("ws", f"{HOME}:8080", "/serial")
    # 端点 id 里带 `:` 和 `/`，编码方式由客户端库决定，但解回来必须一字不差。
    assert parse_qs(parts.query) == {"ep": [EP]}


def test_the_board_is_released_when_the_last_page_leaves(db_path):
    """上游必须关回 1000：桥靠这次断开释放串口，并把 RotorHazard 起回来。"""
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            await page.wait_accepted()
            assert await until(lambda: len(rig.factory.created) == 1)
        link = rig.factory.created[0]
        assert await until(lambda: link.closed is not None)
        assert link.closed[0] == 1000
        assert await until(lambda: rig.sessions() == [])

    asyncio.run(scenario())


# --- 只读扇出 -----------------------------------------------------------------


def test_two_pages_watching_the_same_board_share_one_upstream(db_path):
    rig = make_rig(db_path)

    async def scenario():
        url = url_for(EP, rig.gateway_id)
        async with Page(rig.app, url) as first, Page(rig.app, url) as second:
            await first.wait_accepted()
            await second.wait_accepted()
            link = await rig.wait_upstreams()
            # accept 只说明路由通了，第二页真正进名单要等它跑完 `_join`。不等这一条就推帧，
            # 测到的是"第一页收到了、第二页还在路上"，跟扇出没关系。
            assert await until(lambda: rig.sessions()[0]["subscribers"] == 2)
            link.push(b"\x01")
            assert await first.receive_bytes() == b"\x01"
            assert await second.receive_bytes() == b"\x01"
            # 两页都从同一条上游拿到同一帧之后才断言钥匙数量：
            # 把"扇出多开了第二把"和"上游还没开出来"分成两个不同的失败。
            assert len(rig.factory.created) == 1, "扇出不得再开第二把钥匙"
        assert rig.sessions() == []

    asyncio.run(scenario())


def test_a_reader_that_never_writes_does_not_take_the_writer_slot(db_path):
    """只读页不占写权——这条成立，"多看板同屏"才不必有人一直让着。"""
    rig = make_rig(db_path)

    async def scenario():
        url = url_for(EP, rig.gateway_id)
        async with Page(rig.app, url) as watcher, Page(rig.app, url) as operator:
            await watcher.wait_accepted()
            await operator.wait_accepted()
            link = await rig.wait_upstreams()
            await watcher.send_text("hello")
            await operator.send_bytes(b"S3")
            assert await until(lambda: link.sent == [b"S3"])

    asyncio.run(scenario())


def test_a_text_frame_is_never_forwarded_to_the_board(db_path):
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            await page.wait_accepted()
            link = await rig.wait_upstreams()
            await page.send_text("R")
            await page.send_bytes(b"R")
            assert await until(lambda: link.sent == [b"R"])
            assert rig.sessions()[0]["text_frames_ignored"] == 1
            # 忽略文本帧不构成立即断开：还能继续收板子的数据。
            link.push(b"\x07")
            assert await page.receive_bytes() == b"\x07"

    asyncio.run(scenario())


# --- 写命令单写者锁 -----------------------------------------------------------


def test_the_first_page_to_send_a_command_owns_the_board(db_path):
    rig = make_rig(db_path)

    async def scenario():
        url = url_for(EP, rig.gateway_id)
        async with Page(rig.app, url) as first, Page(rig.app, url) as second:
            await first.wait_accepted()
            await second.wait_accepted()
            await first.send_bytes(b"R")
            # 写权归属以"第一条命令真的被处理过"为准，不靠 create_task 的调度顺序赌：
            # 两个页各跑一个任务，先轮到谁是不定的。不卡这道栅栏，第二页有可能反而先拿到
            # 写权，于是这里等的 busy 关闭永远不来，测试变成偶发超时。
            # `accept` 先于 `_join`（routers/ws.py:23），所以名单可能还是空的——判据要自己兜住。
            assert await until(
                lambda: rig.sessions() and rig.sessions()[0]["writer"] is not None
            )
            await second.send_bytes(b"S1")
            assert await second.wait_closed() == (1008, "busy")
            # 被拒的那页的命令一个字都不许到达板子：否则"单写者"只是记账好看。
            assert await until(lambda: rig.factory.created[0].sent == [b"R"])
            assert rig.sessions()[0]["subscribers"] == 1

    asyncio.run(scenario())


def test_a_second_page_can_take_the_board_after_the_writer_leaves(db_path):
    """写者断开必须把写权还回来，否则这块板从此没人能发命令（桥那边也一直是 scan 态）。"""
    rig = make_rig(db_path)

    async def scenario():
        url = url_for(EP, rig.gateway_id)
        async with Page(rig.app, url) as first:
            await first.wait_accepted()
            await first.send_bytes(b"R")
        assert await until(lambda: rig.sessions() == [])
        async with Page(rig.app, url) as second:
            await second.wait_accepted()
            await second.send_bytes(b"S2")
            assert await until(lambda: len(rig.factory.created) == 2)
            assert rig.factory.created[0].sent == [b"R"]
            assert await until(lambda: rig.factory.created[1].sent == [b"S2"])

    asyncio.run(scenario())


# --- 一条连接只属一个 gateway -------------------------------------------------


def test_the_same_path_on_two_gateways_is_two_boards(db_path):
    """§7 S3 判据的 WS 侧：同名端点落在两个位置是两条上游，写权各自独立。"""
    rig = make_rig(db_path, hosts=(HOME, AWAY))
    shared = "serial:/dev/widscan58-2"

    async def scenario():
        async with Page(rig.app, url_for(shared, rig.gateway_ids[0])) as home, Page(
            rig.app, url_for(shared, rig.gateway_ids[1])
        ) as away:
            await home.wait_accepted()
            await away.wait_accepted()
            assert await until(lambda: len(rig.factory.created) == 2)
            await home.send_bytes(b"A")
            await away.send_bytes(b"B")
            home_link = rig.factory.link_for(HOME)
            away_link = rig.factory.link_for(AWAY)
            assert await until(
                lambda: home_link.sent == [b"A"] and away_link.sent == [b"B"]
            )

    asyncio.run(scenario())


def test_a_connection_cannot_reach_another_gateways_board(db_path):
    """连接绑死在一台接入机上：`gateway` 说的是谁，上游地址就只可能从那台拼出来。"""
    rig = make_rig(db_path, hosts=(HOME, AWAY))

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_ids[0])) as page:
            await page.wait_accepted()
            await page.send_bytes(b"R")
            assert await until(lambda: len(rig.factory.created) == 1)
        assert {urlsplit(link.url).netloc for link in rig.factory.created} == {f"{HOME}:8080"}

    asyncio.run(scenario())


def test_a_bridge_dying_takes_only_its_own_pages(db_path):
    """§7 S3 判据"A 掉线不影响 B"的 WS 侧：一台桥没了，只关它自己那块板上的页。

    这条是"跨接入机"而不是"跨端点"：两条上游都挂在同一个 hub、同一个循环里，
    `_drop` 只要多收一点就会把另一台正在出数的页一起关掉——那正是"后端反而不如直连"。
    """
    rig = make_rig(db_path, hosts=(HOME, AWAY))
    shared = "serial:/dev/widscan58-2"

    async def scenario():
        async with Page(rig.app, url_for(shared, rig.gateway_ids[0])) as home, Page(
            rig.app, url_for(shared, rig.gateway_ids[1])
        ) as away:
            await home.wait_accepted()
            await away.wait_accepted()
            await rig.wait_upstreams(2)
            # 两条上游谁先建出来不保证（两页并发进 `_join`），所以按 host 认，不按序号。
            home_link = rig.factory.link_for(HOME)
            away_link = rig.factory.link_for(AWAY)
            home_link.drop(code=1011, reason="bridge gone")
            assert await home.wait_closed() == (1011, "bridge gone")
            # 另一台不受影响：还能继续收帧，会话表里也只剩它自己。
            away_link.push(b"\x5a")
            assert await away.receive_bytes() == b"\x5a"
            assert await until(lambda: len(rig.sessions()) == 1)
            assert rig.sessions()[0]["gateway_id"] == rig.gateway_ids[1]

    asyncio.run(scenario())


# --- 拒绝的理由必须可读 -------------------------------------------------------


def test_a_connection_without_a_gateway_is_refused_with_a_reason(db_path):
    rig = make_rig(db_path, hosts=(HOME, AWAY))

    async def scenario():
        async with Page(rig.app, f"/ws/bridge/{EP}") as page:
            assert await page.wait_closed() == (1008, "gateway_required")
        assert rig.factory.created == []

    asyncio.run(scenario())


def test_an_unknown_gateway_is_refused_by_id(db_path):
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for(EP, 999)) as page:
            assert await page.wait_closed() == (1008, "gateway_not_found")
        assert rig.factory.created == []

    asyncio.run(scenario())


def test_a_gateway_without_base_url_is_refused_with_a_reason(db_path):
    """没配地址的接入机拨不到，也桥不到——理由要说得出，不能静默断开。"""
    rig = make_rig(db_path)
    cleared = rig.client.patch(f"/api/gateways/{rig.gateway_id}", json={"base_url": None})
    assert cleared.status_code == 200, cleared.json()

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            assert await page.wait_closed() == (1008, "no_base_url")
        assert rig.factory.created == []

    asyncio.run(scenario())


def test_the_aggregator_instance_refuses_to_bridge(db_path):
    """§"角色约束要写成断言而不是约定"：汇聚实例按设计没有桥，也不去碰桥。"""
    rig = make_rig(db_path, instance_role="aggregator")

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            assert await page.wait_closed() == (1008, "aggregator_role")
        assert rig.factory.created == []

    asyncio.run(scenario())


def test_the_bridges_own_close_reason_reaches_the_page(db_path):
    """只转发不改语义：桥说"cannot stop RotorHazard"，页面上就得看到那一句。"""
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for("uart:/dev/serial0", rig.gateway_id)) as page:
            await page.wait_accepted()
            link = await rig.wait_upstreams()
            link.drop(code=1008, reason="cannot stop RotorHazard")
            assert await page.wait_closed() == (1008, "cannot stop RotorHazard")

    asyncio.run(scenario())


# --- 状态可查 -----------------------------------------------------------------


def test_bridge_status_names_the_live_session_and_its_writer(db_path):
    rig = make_rig(db_path)

    async def scenario():
        async with Page(rig.app, url_for(EP, rig.gateway_id)) as page:
            await page.wait_accepted()
            await page.send_bytes(b"R")
            assert await until(lambda: rig.sessions() and rig.sessions()[0]["writer"] is not None)
            session = rig.sessions()[0]
            assert session["gateway_id"] == rig.gateway_id
            assert session["ep"] == EP
            assert session["subscribers"] == 1

    asyncio.run(scenario())


def test_health_carries_the_bridge_section(db_path):
    """没人在连的时候也要说得出口：`sessions: []` 与"这块表没做"是两件事。"""
    rig = make_rig(db_path)
    health = rig.client.get("/api/health").json()
    assert health["bridge"] == {"sessions": [], "upstreams": 0}
