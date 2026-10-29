"""§7 S3 出口判据里"多接入机并跑"那一条：≥2 台假网关在同一个进程里一起跑。

各层自己的测试（`test_dial_loop.py`/`test_ingest_nodes.py`/`test_store.py`）都只摆一两台网关，
问的是"这一层怎么处理"。这个文件只问**跨层**的问题——一轮拨号穿过 dialer→ingest→读接口→
两只桶之后，身份还会不会串、一台死了会不会连累别的。判据三条逐条对应：

- **节点身份键不撞名**：写入键只有 `(gateway_id, transport_addr)` 一种，跨网关、跨轮次都不许
  合并或串位（`display_name` 一律不参与去重）。
- **同名 `/dev/widscan58-2` 在两个位置下是两个节点**：位置（`relay_id`）也必须分得开，
  否则前端按位置筛选会把两块板画在同一座塔上。
- **A 掉线不影响 B**：网关态、节点行、端点表代理、板卡归属切换、队列与环形缓冲五个面各自
  独立。WS 侧的同一条判据在 `test_ws_bridge.py`（那边已有两条网关的夹具）。

拨号统一走 `dial_all()`（"立刻全拨一次"，与后台循环共用同一套并发逻辑）；假 transport 按
URL 回答——并发拨多台时按调用顺序交结果会串味。
"""

import asyncio
from dataclasses import replace

import pytest

from app.config import Settings
from app.services.dialer import DialCoordinator, FetchResult
from app.services.store import GatewayQueue, SampleStore

#: 判据原文里的那个路径：三台接入机都报它，落库必须是三行。
SHARED = "/dev/widscan58-2"
TOWER = "192.168.99.11"
STAND = "192.168.99.12"
PIT = "192.168.99.13"
POLL = 5.0


def serial(path: str, label: str = "widscan58-2") -> dict:
    """逐字对齐 serve.mjs `/api/endpoints` 的 serial 条目形状。"""
    return {
        "id": f"serial:{path}",
        "kind": "serial",
        "label": label,
        "path": path,
        "online": True,
        "busy": False,
    }


def answered(*endpoints) -> FetchResult:
    return FetchResult(200, {"endpoints": list(endpoints)}, 12, None)


def link_failed(error: str = "timeout") -> FetchResult:
    return FetchResult(None, None, 3000, error)


class FakeTransport:
    """按 URL 回答，顺带记下每一次请求（GET 与 POST 分开记，代理那两条要用）。"""

    def __init__(self, default: FetchResult | None = None, by_url: dict | None = None) -> None:
        self.default = default if default is not None else answered(serial(SHARED))
        self.by_url = dict(by_url or {})
        self.gets: list[str] = []
        self.posts: list[tuple[str, dict]] = []

    def fetch(self, url: str, timeout_s: float) -> FetchResult:
        self.gets.append(url)
        return self._answer(url)

    def post(self, url: str, payload: dict, timeout_s: float) -> FetchResult:
        self.posts.append((url, payload))
        return self._answer(url)

    def _answer(self, url: str) -> FetchResult:
        answer = self.by_url.get(url, self.default)
        return answer() if callable(answer) else answer


def endpoints_url(host: str) -> str:
    return f"http://{host}:8080/api/endpoints"


def mode_url(host: str) -> str:
    return f"http://{host}:8080/api/mode"


@pytest.fixture
def three_gateways(client):
    """三台接入机分处两个位置：`relay` 维度也参与身份，判据要的就是这个形状。"""
    site = client.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
    tower = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}
    ).json()
    stand = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "看台"}
    ).json()
    rows = {}
    for name, host, relay in (
        ("pi-tower-a", TOWER, tower),
        ("pi-tower-b", PIT, tower),
        ("pi-stand", STAND, stand),
    ):
        body = {
            "relay_id": relay["relay_id"],
            "name": name,
            "kind": "usb-bridge",
            "base_url": f"http://{host}:8080",
        }
        rows[name] = {**client.post("/api/gateways", json=body).json(), "host": host}
    return {"site": site, "relays": {"tower": tower, "stand": stand}, **rows}


def coordinator(client, transport, **overrides) -> DialCoordinator:
    """直接构造循环来拨。公共 `app` 夹具把后台拨号关了，这里要的正是循环自己跑一轮。"""
    settings = replace(
        client.app.state.settings, dialer_enabled=True, dial_poll_interval_s=POLL, **overrides
    )
    return DialCoordinator(client.app.state.db, settings, transport)


def dial_once(client, transport, **overrides) -> dict:
    return asyncio.run(coordinator(client, transport, **overrides).dial_all())


def nodes(client) -> list[dict]:
    return client.get("/api/nodes").json()


def gateway_state(client, gateway_id: int) -> dict:
    return client.get(f"/api/gateways/{gateway_id}").json()


def use_proxy_transport(client, transport) -> None:
    """`/api/endpoints` 与 `/api/board/mode` 读的是 app 上那份 transport，不是构造器注入的那份。"""
    client.app.state.transport = transport


# --- 节点身份键不撞名 ---------------------------------------------------------


def test_three_bridges_reporting_the_same_device_path_make_three_nodes(client, three_gateways):
    dial_once(client, FakeTransport())
    rows = nodes(client)
    assert len(rows) == 3
    # 身份键逐条 distinct：这里长得一模一样，下游所有筛选就都建立在串了的数据上。
    assert len({(r["gateway_id"], r["transport"]["addr"]) for r in rows}) == 3
    assert len({r["node_id"] for r in rows}) == 3
    assert {r["transport"]["addr"] for r in rows} == {SHARED}
    assert {r["gateway_id"] for r in rows} == {
        three_gateways[name]["gateway_id"] for name in ("pi-tower-a", "pi-tower-b", "pi-stand")
    }
    # 三行显示名完全相同：证明去重真的没看名字（看了就会并成一块板，另外两块"消失"）。
    assert {r["display_name"] for r in rows} == {"widscan58-2"}


def test_the_same_path_under_two_positions_lands_on_two_relays(client, three_gateways):
    """判据原文的"两个位置"：按 `gateway` 筛必须各回各的那一块板。"""
    dial_once(client, FakeTransport())
    tower_relay = three_gateways["relays"]["tower"]["relay_id"]
    stand_relay = three_gateways["relays"]["stand"]["relay_id"]
    rows = nodes(client)
    assert {r["relay_id"] for r in rows} == {tower_relay, stand_relay}
    for name in ("pi-tower-a", "pi-tower-b", "pi-stand"):
        gateway_id = three_gateways[name]["gateway_id"]
        mine = client.get(f"/api/nodes?gateway={gateway_id}").json()
        assert len(mine) == 1, f"{name} 应该只有自己那一块板"
        assert mine[0]["gateway_id"] == gateway_id
        assert mine[0]["transport"]["addr"] == SHARED


def test_a_second_round_moves_and_merges_nothing(client, three_gateways):
    """dialer 每轮重枚举都调 ingest；跨轮次也不许长出第二行或把两行并成一行。"""
    transport = FakeTransport()
    dial_once(client, transport)
    ids = {r["node_id"]: (r["gateway_id"], r["status"]) for r in nodes(client)}
    second = asyncio.run(coordinator(client, transport).dial_all())
    # 节点 id 是前端与事件的锚点：第二轮把它们挪位就等于把历史挂错板。
    assert {r["node_id"]: (r["gateway_id"], r["status"]) for r in nodes(client)} == ids
    assert [len(o["nodes"]["created"]) for o in second["results"]] == [0, 0, 0]
    assert [o["nodes"]["updated"] for o in second["results"]] == [[], [], []]


# --- A 掉线不影响 B -----------------------------------------------------------


def test_a_dead_bridge_takes_only_its_own_nodes_with_it(client, three_gateways):
    """连续失败到阈值：坏的那台转 unreachable、它的节点连带 lost；另两台一行都不许被波及。"""
    good_a = three_gateways["pi-tower-a"]["gateway_id"]
    dead = three_gateways["pi-stand"]["gateway_id"]
    seen = {"n": 0}

    def once_alive_then_die():
        # 先让它成功一轮，节点行才存在；否则"判死后连带 lost"测的是零行，等于没测。
        seen["n"] += 1
        return answered(serial(SHARED)) if seen["n"] == 1 else link_failed()

    transport = FakeTransport(default=answered(serial(SHARED)), by_url={endpoints_url(STAND): once_alive_then_die})
    co = coordinator(client, transport, dial_fails_to_unreachable=2)
    for _ in range(3):
        asyncio.run(co.dial_all())

    assert gateway_state(client, good_a)["dial_state"] == "online"
    assert gateway_state(client, dead)["dial_state"] == "unreachable"
    by_gateway = {r["gateway_id"]: r["status"] for r in nodes(client)}
    assert by_gateway[dead] == "lost"
    assert [by_gateway[g] for g in (good_a, three_gateways["pi-tower-b"]["gateway_id"])] == [
        "active",
        "active",
    ]
    # lost 只是状态，不是删除：否则回答不了"那块板昨天还在不在"。
    assert len(by_gateway) == 3


def test_the_node_gate_rejects_only_the_gateway_that_overruns(client, three_gateways):
    """`max_nodes_per_gateway` 是每台的闸：A 超限被拒不能顺手把 B/C 的节点也清掉。"""
    transport = FakeTransport(
        default=answered(serial(SHARED)),
        by_url={
            endpoints_url(TOWER): answered(serial(SHARED), serial("/dev/extra-1"), serial("/dev/extra-2"))
        },
    )
    report = dial_once(client, transport, max_nodes_per_gateway=2)
    outcomes = {entry["gateway_id"]: entry for entry in report["results"]}
    dead = three_gateways["pi-tower-a"]
    assert outcomes[dead["gateway_id"]]["rejected"]["reason"] == "max_nodes_per_gateway"
    assert outcomes[dead["gateway_id"]]["dial_state"] == "online"
    rows = nodes(client)
    assert {r["gateway_id"] for r in rows} == {
        three_gateways["pi-tower-b"]["gateway_id"],
        three_gateways["pi-stand"]["gateway_id"],
    }


def test_a_flooded_queue_and_a_full_ring_do_not_touch_the_other_gateway():
    """两只桶都是"每接入机一份"：起点塔打满不能吃看台的配额，也不能碰它的计数。"""
    tower = GatewayQueue(gateway_id=1, limit=2)
    stand = GatewayQueue(gateway_id=2, limit=8)
    for point in range(10):
        tower.offer(point)
    for point in range(3):
        stand.offer(point)
    assert (tower.depth, tower.dropped) == (2, 8)
    assert (stand.depth, stand.dropped) == (3, 0)

    store = SampleStore(depth_per_slot=4, budget_points=1000)
    for n in range(6):
        store.push(1, SHARED, 0, {"t_ms": n})
        store.push(2, SHARED, 0, {"t_ms": 100 + n})
    assert [row["t_ms"] for row in store.series(1, SHARED, 0)] == [2, 3, 4, 5]
    assert [row["t_ms"] for row in store.series(2, SHARED, 0)] == [102, 103, 104, 105]
    # 端点消失只释放它自己那台的那条序列。
    assert store.forget(1, SHARED) == 4
    assert len(store.series(2, SHARED, 0)) == 4


def test_the_point_budget_actually_fits_every_gateway_at_full_nodes():
    """满配（`max_gateways` 台 × 每台 `max_nodes_per_gateway` 块 × 每块 8 槽）不该触发兜底驱逐：
    走到 `_trim_to_budget` 说明有调用方绕过了接入层，而那两道闸本来就该卡住总量。"""
    settings = Settings(max_gateways=3, max_nodes_per_gateway=2, ring_depth_per_slot=5)
    store = SampleStore.from_settings(settings, slots_per_node=8)
    assert store.budget_points == 3 * 2 * 8 * 5
    for gateway_id in (1, 2, 3):
        for node in (0, 1):
            for slot in range(8):
                for point in range(5):
                    store.push(gateway_id, f"/dev/board-{node}", slot, {"t_ms": point})
    assert store.stats() == {
        "series": 3 * 2 * 8,
        "points": store.budget_points,
        "budget": store.budget_points,
        "dropped_points": 0,
    }


# --- 代理也不串 ---------------------------------------------------------------


def test_the_endpoints_proxy_answers_for_the_named_gateway_only(client, three_gateways):
    """每台报自己的端点表：`?gateway=` 说谁就只可能拿到谁的地址，两台的表不许混。"""
    transport = FakeTransport(
        by_url={
            endpoints_url(TOWER): answered(serial("/dev/tower-board")),
            endpoints_url(STAND): answered(serial("/dev/stand-board"), serial("/dev/stand-uart")),
        }
    )
    use_proxy_transport(client, transport)
    tower = three_gateways["pi-tower-a"]
    stand = three_gateways["pi-stand"]

    tower_rows = client.get(f"/api/endpoints?gateway={tower['gateway_id']}").json()["endpoints"]
    stand_rows = client.get(f"/api/endpoints?gateway={stand['gateway_id']}").json()["endpoints"]
    assert [r["path"] for r in tower_rows] == ["/dev/tower-board"]
    assert [r["path"] for r in stand_rows] == ["/dev/stand-board", "/dev/stand-uart"]
    assert {r["gateway_id"] for r in tower_rows} == {tower["gateway_id"]}
    assert {r["gateway_id"] for r in stand_rows} == {stand["gateway_id"]}
    assert transport.gets == [endpoints_url(TOWER), endpoints_url(STAND)]


def test_the_mode_switch_hits_only_the_named_bridge(client, three_gateways):
    """板卡归属是"哪一块板在计时"的唯一裁决处：切 B 绝不能顺带把 A 也切了。"""
    transport = FakeTransport(default=FetchResult(200, {"state": "scan"}, 12, None))
    use_proxy_transport(client, transport)
    stand = three_gateways["pi-stand"]
    response = client.post(f"/api/board/mode?gateway={stand['gateway_id']}", json={"mode": "scan"})
    assert response.status_code == 200
    assert response.json() == {"state": "scan"}
    assert transport.posts == [(mode_url(STAND), {"mode": "scan"})]
    assert transport.gets == []


def test_one_dead_proxy_does_not_make_the_other_unreadable(client, three_gateways):
    """502 是那一台的结论：另一台照常出表，前端才不会整页变灰。"""
    transport = FakeTransport(
        default=answered(serial(SHARED)),
        by_url={endpoints_url(STAND): link_failed("refused")},
    )
    use_proxy_transport(client, transport)
    tower = three_gateways["pi-tower-a"]
    stand = three_gateways["pi-stand"]
    alive = client.get(f"/api/endpoints?gateway={tower['gateway_id']}").json()["endpoints"]
    assert [r["path"] for r in alive] == [SHARED]
    failed = client.get(f"/api/endpoints?gateway={stand['gateway_id']}")
    assert failed.status_code == 502
    body = failed.json()["detail"]
    assert (body["reason"], body["gateway_id"]) == ("refused", stand["gateway_id"])
    assert "serve.mjs 没起" in body["detail"]


# --- 跨层：一轮拨号之后，健康检查要说清每一台 -----------------


def test_health_names_every_gateway_and_the_live_bridge_sessions(client, three_gateways):
    """多接入机后"当前是哪一台"必须写在数据里：三态各自独立，桥那一节按 gateway 指名。

    `health["dialer"]` 来自 app 自己那份循环（测试实例默认没起），所以"该拨谁/下次几点再试"
    在这里从 `co.status()` 读——两处读的是同一套 `Plan`/`_Runtime` 结构。
    """
    transport = FakeTransport(by_url={endpoints_url(STAND): link_failed()})
    co = coordinator(client, transport, dial_fails_to_unreachable=1)
    asyncio.run(co.dial_all())

    body = client.get("/api/health").json()
    assert body["gateways"]["total"] == 3
    assert (body["gateways"]["online"], body["gateways"]["unreachable"]) == (2, 1)
    assert {entry["gateway_id"] for entry in co.status()["targets"]} == {
        three_gateways[name]["gateway_id"] for name in ("pi-tower-a", "pi-tower-b", "pi-stand")
    }
    states = {entry["gateway_id"]: entry for entry in co.status()["gateways"]}
    assert states[three_gateways["pi-stand"]["gateway_id"]]["failures"] == 1
    assert states[three_gateways["pi-tower-a"]["gateway_id"]]["next_delay_s"] == POLL
    assert states[three_gateways["pi-stand"]["gateway_id"]]["next_delay_s"] < POLL
    assert body["bridge"] == {"sessions": [], "upstreams": 0, "observer_failures": 0}
