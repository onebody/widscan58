"""S3-6 后台拨号循环：`DialCoordinator` 的调度口径。

这里测的是"什么时候拨谁"，不是 HTTP 细节（那在 `test_probe.py`）。四条都不是我想加的
复杂度，是计划里写着的：

- **只拨本场地**：§5 原文"配置里出现远端场地地址直接报错，不静默接受"。多个场地又没指定
  `current_site_id` 时**不猜**，全部拒绝并说明原因。
- **并发有硬闸**：`dial_concurrency` 被计量才算"硬"，所以用 barrier 逼出真实并发再断言上限。
- **A 掉线不影响 B**：一台一条任务、一份自己的退避——这是运营者区分"拔了重插"和"整网不通"
  的唯一依据。
- **`dialer.running` 如实**：S2 那行硬编码的 `False` 必须换成真实状态，停了就不能还在拨。
"""

import asyncio
import threading
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.dialer import DialCoordinator, FetchResult, next_backoff

#: 测试统一用这个轮询间隔，健康机器的"下次等多久"就应当等于它。
POLL = 5.0


def serial(label: str, path: str) -> dict:
    """逐字对齐 serve.mjs `/api/endpoints` 的 serial 条目形状。"""
    return {
        "id": f"serial:{path}",
        "kind": "serial",
        "label": label,
        "path": path,
        "online": True,
        "busy": False,
    }


def uart(path: str) -> dict:
    return {
        "id": f"uart:{path}",
        "kind": "uart",
        "label": "ttyAMA0",
        "path": path,
        "online": True,
        "busy": False,
        "scannable": True,
    }


def answered(*endpoints) -> FetchResult:
    return FetchResult(200, {"endpoints": list(endpoints)}, 12, None)


def link_failed(error: str = "timeout") -> FetchResult:
    return FetchResult(None, None, 3000, error)


class FakeTransport:
    """按 URL 回答：dialer 是并发拨多台，按调用顺序交结果会串味。

    顺手计量"同时在飞几条"——没有这个数字，`dial_concurrency` 这条闸就只是注释。
    """

    def __init__(self, default: FetchResult | None = None, by_url: dict | None = None) -> None:
        self.default = default if default is not None else answered(serial("widscan58-A", "/dev/a"))
        self.by_url = dict(by_url or {})
        self.calls: list[str] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._count = threading.Lock()

    def fetch(self, url: str, timeout_s: float) -> FetchResult:
        with self._count:
            self.calls.append(url)
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            answer = self.by_url.get(url, self.default)
            return answer() if callable(answer) else answer
        finally:
            with self._count:
                self.in_flight -= 1


class RecordingSleep:
    """假 sleep：记下每次"要等多久"，然后立刻让出——不占真实时钟。

    它只交给每台接入机的轮询任务；规划任务用的是真实 `asyncio.sleep`（见
    `dialer._planner_loop`）。这两条曾经共用一个注入口，实测后果是规划被加速到
    1 kHz 猛抽数据库，`stop()` 再也回不来——这条不是风格问题。
    """

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay_s: float) -> None:
        self.delays.append(delay_s)
        await asyncio.sleep(0)


@pytest.fixture
def site(client):
    return client.post("/api/sites", json={"name": "主场", "slug": "home"}).json()


@pytest.fixture
def relay(client, site):
    return client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}
    ).json()


def add_gateway(client, relay, name: str, host: str) -> dict:
    body = {
        "relay_id": relay["relay_id"],
        "name": name,
        "kind": "usb-bridge",
        "base_url": f"http://{host}:8080",
    }
    return client.post("/api/gateways", json=body).json()


def add_second_site(client) -> tuple[dict, dict]:
    site = client.post("/api/sites", json={"name": "客场", "slug": "away"}).json()
    relay = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "计时区"}
    ).json()
    return site, relay


def url_of(host: str) -> str:
    return f"http://{host}:8080/api/endpoints"


def coordinator(client, transport, *, sleep=None, **overrides) -> DialCoordinator:
    """直接构造循环来测调度口径。

    `dialer_enabled=True` 是必须的：公共 `app` 夹具把后台拨号关掉了（见 conftest），
    而这几条测试测的就是循环本身。
    """
    settings = replace(
        client.app.state.settings, dialer_enabled=True, dial_poll_interval_s=POLL, **overrides
    )
    kwargs = {} if sleep is None else {"sleep": sleep}
    return DialCoordinator(client.app.state.db, settings, transport, **kwargs)


def nodes(client) -> list[dict]:
    return client.get("/api/nodes").json()


def gateway_state(client, gateway_id: int) -> dict:
    return client.get(f"/api/gateways/{gateway_id}").json()


def dial_once(client, transport, **overrides) -> dict:
    return asyncio.run(coordinator(client, transport, **overrides).dial_all())


async def wait_for_delays(sleeper: RecordingSleep, count: int, *, cap_s: float = 3.0) -> None:
    """等够 count 次排程。等不到就当场报错——绝不许测试挂在"再等一下也许就有了"里。"""
    started = time.monotonic()
    while len(sleeper.delays) < count:
        if time.monotonic() - started > cap_s:
            raise AssertionError(f"{cap_s} s 内只记下 {len(sleeper.delays)} 次排程：循环没在跑")
        await asyncio.sleep(0.005)


async def spin(co: DialCoordinator, sleeper: RecordingSleep, *, min_rounds: int, cap_s: float = 3.0):
    """跑到 sleeper 至少记下 min_rounds 次，然后一定停下来（finally 关循环）。"""
    await co.start()
    try:
        await wait_for_delays(sleeper, min_rounds, cap_s=cap_s)
    finally:
        await co.stop()


def wait_until(predicate, *, cap_s: float = 3.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < cap_s:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_backoff_doubles_and_stops_at_the_configured_ceiling():
    """退避是纯函数，先单独钉住：拔了重插的体感上限由 `dial_backoff_max_s` 说了算。"""
    delays = [next_backoff(n, base_s=1.0, max_s=30.0) for n in (0, 1, 2, 3, 6, 9)]
    assert delays == [0.0, 1.0, 2.0, 4.0, 30.0, 30.0]


def test_a_site_instance_only_dials_its_own_site(client, relay, site):
    home = add_gateway(client, relay, "pi-home", "192.168.99.11")
    _, away_relay = add_second_site(client)
    away = add_gateway(client, away_relay, "pi-away", "192.168.99.12")
    transport = FakeTransport()
    report = dial_once(client, transport, current_site_id=site["site_id"])
    assert transport.calls == [url_of("192.168.99.11")]
    assert report["targets"] == [{"gateway_id": home["gateway_id"], "name": "pi-home"}]
    assert report["refused"] == [
        {"gateway_id": away["gateway_id"], "name": "pi-away", "reason": "foreign_site"}
    ]


def test_it_refuses_to_guess_the_site_when_several_are_configured(client, relay):
    """没指定 `current_site_id` 而库里又有两个场地：全不拨，而不是挑一个。"""
    add_gateway(client, relay, "pi-home", "192.168.99.11")
    _, away_relay = add_second_site(client)
    add_gateway(client, away_relay, "pi-away", "192.168.99.12")
    transport = FakeTransport()
    report = dial_once(client, transport)
    assert transport.calls == []
    assert report["targets"] == []
    assert {entry["reason"] for entry in report["refused"]} == {"site_ambiguous"}


def test_a_single_site_needs_no_current_site_id(client, relay):
    """最常见的形态：一台实例一个场地，不该强迫运维先配 site id。"""
    add_gateway(client, relay, "pi-home", "192.168.99.11")
    transport = FakeTransport()
    dial_once(client, transport)
    assert transport.calls == [url_of("192.168.99.11")]


def test_the_gateway_count_gate_refuses_by_name_instead_of_silently_skipping(client, relay):
    """超出 `max_gateways` 的那几台必须点名：静默跳过只会得到"加了机器怎么没数据"。"""
    first = add_gateway(client, relay, "pi-1", "192.168.99.11")
    second = add_gateway(client, relay, "pi-2", "192.168.99.12")
    transport = FakeTransport()
    report = dial_once(client, transport, max_gateways=1)
    assert report["targets"] == [{"gateway_id": first["gateway_id"], "name": "pi-1"}]
    assert report["refused"] == [
        {"gateway_id": second["gateway_id"], "name": "pi-2", "reason": "max_gateways"}
    ]
    assert transport.calls == [url_of("192.168.99.11")]


def test_a_gateway_without_an_address_is_never_dialed(client, relay):
    """没填 base_url 是"还没接线"，不是"拨不通"：既不发起请求，也不落下失败计数。"""
    gateway = add_gateway(client, relay, "pi-1", "192.168.99.11")
    client.patch(f"/api/gateways/{gateway['gateway_id']}", json={"base_url": None})
    transport = FakeTransport()
    report = dial_once(client, transport)
    assert transport.calls == []
    assert report["refused"] == [
        {"gateway_id": gateway["gateway_id"], "name": "pi-1", "reason": "no_base_url"}
    ]
    assert gateway_state(client, gateway["gateway_id"])["dial_state"] is None


def test_an_aggregator_refuses_to_dial_at_all(client, relay):
    """汇聚实例只做只读拉取（§5）：它一轮都不该拨，并且如实报"没在跑"。"""
    gateway = add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport()
    co = coordinator(client, transport, instance_role="aggregator")
    report = asyncio.run(co.dial_all())
    assert transport.calls == []
    assert report["refused"] == [
        {"gateway_id": gateway["gateway_id"], "name": "pi-1", "reason": "aggregator_role"}
    ]
    asyncio.run(co.start())
    assert co.status()["running"] is False
    assert nodes(client) == []


def test_one_round_enumerates_endpoints_into_node_rows(client, relay):
    gateway = add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(default=answered(serial("widscan58-A", "/dev/widscan58-A")))
    report = dial_once(client, transport)
    assert report["results"][0]["ok"] is True
    assert report["results"][0]["nodes"]["created"] == ["/dev/widscan58-A"]
    rows = nodes(client)
    assert [(r["display_name"], r["status"]) for r in rows] == [("widscan58-A", "active")]
    assert gateway_state(client, gateway["gateway_id"])["dial_state"] == "online"


def test_the_same_device_path_on_two_gateways_lands_as_two_nodes(client, relay):
    """§7 S3 出口判据的正面：同名 `/dev/widscan58-2` 在两台接入机下就是两行。"""
    first = add_gateway(client, relay, "pi-1", "192.168.99.11")
    second = add_gateway(client, relay, "pi-2", "192.168.99.12")
    both = answered(serial("widscan58-2", "/dev/widscan58-2"))
    transport = FakeTransport(
        by_url={url_of("192.168.99.11"): both, url_of("192.168.99.12"): both}
    )
    dial_once(client, transport)
    rows = nodes(client)
    assert len(rows) == 2
    assert {r["gateway_id"] for r in rows} == {first["gateway_id"], second["gateway_id"]}
    assert {r["transport"]["addr"] for r in rows} == {"/dev/widscan58-2"}


def test_a_uart_endpoint_dialed_by_the_loop_is_marked_as_stopping_rotorhazard(client, relay):
    """R21 的前半：接入即停计时必须是循环落库的数据，不只是代理响应里的字段。"""
    gateway = add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(default=answered(uart("/dev/serial0")))
    dial_once(client, transport)
    row = nodes(client)[0]
    assert row["transport"]["kind"] == "uart-pl011"
    assert row["stops_rh"] is True
    assert row["gateway_id"] == gateway["gateway_id"]


def test_no_more_than_dial_concurrency_bridges_are_dialed_at_once(client, relay):
    """barrier 必须两两同时到达才放行：既证明并发真用上了，也证明上限没被冲破。"""
    barrier = threading.Barrier(2, timeout=5)
    hosts = ("192.168.99.11", "192.168.99.12", "192.168.99.13", "192.168.99.14")

    def paired(path: str):
        def answer() -> FetchResult:
            barrier.wait()
            # 每台交一个自己的端点：空端点表会被 `classify` 判成 `empty_endpoints`，
            # 那条断言就测不到"并发拨成功"而是测了个失败分类。
            return answered(serial("widscan58", path))

        return answer

    for index, host in enumerate(hosts):
        add_gateway(client, relay, f"pi-{index}", host)
    transport = FakeTransport(
        by_url={url_of(host): paired(f"/dev/paired-{index}") for index, host in enumerate(hosts)}
    )
    report = dial_once(client, transport, dial_concurrency=2)
    assert transport.max_in_flight == 2
    assert [r["ok"] for r in report["results"]] == [True] * 4


def test_an_endpoint_table_over_the_limit_is_a_rejection_not_a_link_failure(client, relay):
    """超限是策略拒绝：链路明明通着，就不该升级退避，更不该半落地。"""
    gateway = add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(
        default=answered(serial("a", "/dev/a"), serial("b", "/dev/b"), serial("c", "/dev/c"))
    )
    report = dial_once(client, transport, max_nodes_per_gateway=2)
    outcome = report["results"][0]
    assert outcome["ok"] is True
    assert outcome["rejected"]["reason"] == "max_nodes_per_gateway"
    assert outcome["rejected"]["limit"] == 2
    assert outcome["next_delay_s"] == POLL
    assert gateway_state(client, gateway["gateway_id"])["dial_state"] == "online"
    assert nodes(client) == []


def test_a_dead_bridge_does_not_disturb_the_live_one(client, relay):
    """连续失败到阈值：坏的那台转 unreachable、它的节点连带 lost；好的那台一点没被波及。"""
    good = add_gateway(client, relay, "pi-good", "192.168.99.11")
    bad = add_gateway(client, relay, "pi-bad", "192.168.99.12")
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return answered(serial("bad-board", "/dev/b")) if calls["n"] == 1 else link_failed()

    transport = FakeTransport(
        default=answered(serial("good-board", "/dev/g")),
        by_url={url_of("192.168.99.12"): flaky},
    )
    co = coordinator(client, transport, dial_fails_to_unreachable=2)
    for _ in range(3):
        asyncio.run(co.dial_all())

    assert gateway_state(client, good["gateway_id"])["dial_state"] == "online"
    assert gateway_state(client, bad["gateway_id"])["dial_state"] == "unreachable"
    assert sorted(
        ((r["display_name"], r["status"]) for r in nodes(client)),
        key=lambda row: row[0],
    ) == [("bad-board", "lost"), ("good-board", "active")]
    states = {entry["gateway_id"]: entry for entry in co.status()["gateways"]}
    assert states[bad["gateway_id"]]["failures"] == 2
    assert states[bad["gateway_id"]]["next_delay_s"] == 2.0
    assert states[good["gateway_id"]]["next_delay_s"] == POLL


def test_a_bridge_that_blows_up_does_not_kill_the_loop(client, relay):
    """§5 "异常不得拖垮采集服务"：假桥把自己炸了也只是这次没拨通，下轮照拨。"""

    class Exploding:
        def __init__(self) -> None:
            self.calls = 0

        def fetch(self, url, timeout_s):
            self.calls += 1
            raise TypeError("假桥把自己炸了")

    add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = Exploding()
    sleeper = RecordingSleep()
    co = coordinator(client, transport, sleep=sleeper)
    asyncio.run(spin(co, sleeper, min_rounds=4))
    assert transport.calls >= 4
    # 炸掉算一次失败，所以按退避走——不能因为它不是网络错误就零间隔猛撞。
    assert sleeper.delays[:4] == [1.0, 2.0, 4.0, 8.0]


def test_the_loop_waits_the_computed_backoff_before_retrying(client, relay):
    add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(default=link_failed())
    sleeper = RecordingSleep()
    co = coordinator(client, transport, sleep=sleeper)
    asyncio.run(spin(co, sleeper, min_rounds=5))
    assert sleeper.delays[:5] == [1.0, 2.0, 4.0, 8.0, 16.0]


def test_a_live_bridge_keeps_the_poll_interval_instead_of_backing_off(client, relay):
    add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(default=answered(serial("a", "/dev/a")))
    sleeper = RecordingSleep()
    co = coordinator(client, transport, sleep=sleeper)
    asyncio.run(spin(co, sleeper, min_rounds=4))
    assert set(sleeper.delays[:4]) == {POLL}


def test_stopping_leaves_nothing_dialing(client, relay):
    add_gateway(client, relay, "pi-1", "192.168.99.11")
    transport = FakeTransport(default=answered(serial("a", "/dev/a")))
    sleeper = RecordingSleep()
    co = coordinator(client, transport, sleep=sleeper)

    async def scenario():
        await co.start()
        try:
            await wait_for_delays(sleeper, 2)
        finally:
            await co.stop()
        in_flight = len(transport.calls)
        await asyncio.sleep(0.1)
        return in_flight, len(transport.calls), co.status()["running"]

    before, after, running = asyncio.run(scenario())
    assert running is False
    assert after == before  # 停了就是真停了：没有一条任务还在偷偷拨


def test_health_reports_the_dialer_as_stopped_when_it_was_never_started(client):
    """测试实例默认不起后台拨号，健康检查就照实说"没启用"，不许画成在跑。"""
    body = client.get("/api/health").json()
    assert body["dialer"]["running"] is False
    assert body["dialer"]["enabled"] is False
    assert body["problems"] == []


def test_health_reports_a_running_dialer_and_picks_up_a_new_gateway(db_path):
    """lifespan 起循环后：health 报 running，且新登记的接入机不用重启进程就会被拨到。"""
    transport = FakeTransport(default=answered(serial("widscan58-A", "/dev/a")))
    app = create_app(
        Settings(db_path=db_path, dialer_enabled=True, dial_poll_interval_s=0.05),
        transport=transport,
    )
    with TestClient(app) as c:
        assert c.get("/api/health").json()["dialer"]["targets"] == []
        site = c.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
        relay = c.post("/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}).json()
        add_gateway(c, relay, "pi-1", "192.168.99.11")
        assert wait_until(
            lambda: c.get("/api/health").json()["dialer"]["running"] is True
            and bool(c.get("/api/nodes").json())
        )
        body = c.get("/api/health").json()
        assert body["dialer"]["rounds"] >= 1
        assert [n["display_name"] for n in c.get("/api/nodes").json()] == ["widscan58-A"]
