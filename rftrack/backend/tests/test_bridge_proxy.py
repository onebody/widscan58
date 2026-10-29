"""代理指定网关的端点表与 RH/scan 切换（契约 §4 行 `GET /api/endpoints?gateway=` +
`POST /api/board/mode`）。

两条口径决定了这个文件长什么样：

- **只转发不改语义** **[已真机实测]**：对端字段一律原样带出（所以这里故意不套
  `response_model`——套了就等于把我们不认识的字段静默丢掉），切换 RH/scan 的状态码
  与响应体也必须是桥自己给的那个。后端擅自把 409 变成 200，SPA 就再也拦不住
  "这块板正在计时"这条硬约束。
- **多网关后必须带 `gateway` 参数**，否则不知道该切哪台。
- **`medium=uart-pl011` 必须带 `stops_rh=true`**（R21 前半）：让"接入即停计时"
  成为数据而不是 UI 文案。这一位由介质决定，与桥怎么报无关。
"""

import pytest

from app.services.dialer import FetchResult

BRIDGE_ENDPOINTS = [
    {
        "id": "serial:/dev/widscan58-A",
        "kind": "serial",
        "label": "widscan58-A",
        "path": "/dev/widscan58-A",
        "online": True,
        "busy": False,
    },
    {
        "id": "uart:/dev/serial0",
        "kind": "uart",
        "label": "ttyAMA0",
        "path": "/dev/ttyAMA0",
        "online": True,
        "busy": False,
        "scannable": True,
    },
    {"id": "node:pit-2", "kind": "remote", "label": "pit-2", "online": True, "busy": False},
]

BASE_URL = "http://192.168.99.111:8080"


class FakeTransport:
    """GET 走 fetch、POST 走 post：与 `HttpTransport` 同签名，测试不碰网络。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def fetch(self, url, timeout_s):
        self.calls.append(("GET", url, timeout_s))
        return self._next()

    def post(self, url, payload, timeout_s):
        self.calls.append(("POST", url, payload, timeout_s))
        return self._next()

    def _next(self):
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


@pytest.fixture
def bridge_gw(client):
    site = client.post("/api/sites", json={"name": "主场", "slug": "main"}).json()
    relay = client.post("/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}).json()
    gw = client.post(
        "/api/gateways",
        json={"relay_id": relay["relay_id"], "name": "pi-1", "kind": "usb-bridge", "base_url": BASE_URL},
    ).json()
    return {**gw, "site": site, "relay": relay}


def use(client, *results):
    transport = FakeTransport(*results)
    client.app.state.transport = transport
    return transport


def answered(payload=BRIDGE_ENDPOINTS, status_code=200):
    return FetchResult(status_code, {"endpoints": payload} if isinstance(payload, list) else payload, 12, None)


# --- GET /api/endpoints?gateway= --------------------------------------------


def test_the_proxy_requires_the_gateway_param(client, bridge_gw):
    """不带 gateway 就等于"该问哪台接入机"没答案——多网关后这是必答题。"""
    use(client, answered())
    assert client.get("/api/endpoints").status_code == 422


def test_an_unknown_gateway_is_404(client, bridge_gw):
    assert client.get("/api/endpoints?gateway=4242").status_code == 404


def test_a_gateway_without_a_base_url_is_the_same_409_as_probe(client, bridge_gw):
    site = client.post("/api/sites", json={"name": "客场", "slug": "away"}).json()
    relay = client.post("/api/relays", json={"site_id": site["site_id"], "name": "看台"}).json()
    bare = client.post(
        "/api/gateways", json={"relay_id": relay["relay_id"], "name": "pi-9", "kind": "usb-bridge"}
    ).json()
    response = client.get(f"/api/endpoints?gateway={bare['gateway_id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "no_base_url"


def test_every_bridge_field_survives_the_proxy(client, bridge_gw):
    """对端哪天多报一个字段，代理必须照样带出——套了 response_model 就会静默丢掉。"""
    extra = {**BRIDGE_ENDPOINTS[0], "antenna": "SMA-R", "fw_build": "2026-10-20"}
    use(client, answered([extra]))
    listed = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}").json()["endpoints"]
    assert listed[0]["antenna"] == "SMA-R"
    assert listed[0]["fw_build"] == "2026-10-20"
    assert listed[0]["path"] == "/dev/widscan58-A"


def test_a_uart_pl011_endpoint_is_advertised_as_stopping_rotorhazard(client, bridge_gw):
    """R21 前半：这一位由介质决定，桥没说过它也不算——UI 文案不算数据。"""
    use(client, answered())
    listed = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}").json()["endpoints"]
    by_id = {row["id"]: row for row in listed}
    assert by_id["uart:/dev/serial0"]["medium"] == "uart-pl011"
    assert by_id["uart:/dev/serial0"]["stops_rh"] is True
    assert by_id["serial:/dev/widscan58-A"]["medium"] == "usb-native"
    assert by_id["serial:/dev/widscan58-A"]["stops_rh"] is False
    assert by_id["node:pit-2"]["medium"] == "node-ws"
    assert by_id["node:pit-2"]["stops_rh"] is False


def test_the_bridge_own_kind_is_left_verbatim_next_to_our_medium(client, bridge_gw):
    """`kind` 是桥自己的说法、`medium` 是我们的分类；改写前者就丢了排查线索。"""
    use(client, answered())
    listed = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}").json()["endpoints"]
    assert [row["kind"] for row in listed] == ["serial", "uart", "remote"]


def test_an_unrecognized_kind_is_passed_through_instead_of_guessed(client, bridge_gw):
    use(client, answered([{**BRIDGE_ENDPOINTS[0], "kind": "spi"}]))
    row = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}").json()["endpoints"][0]
    assert row["medium"] == "spi"
    assert row["stops_rh"] is False


def test_the_gateway_is_stamped_on_every_returned_row(client, bridge_gw):
    """扇出到多接入机后，读的人必须能一眼看出这行是哪台机器报的。"""
    use(client, answered())
    listed = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}").json()["endpoints"]
    assert {row["gateway_id"] for row in listed} == {bridge_gw["gateway_id"]}


def test_an_unreachable_bridge_is_502_with_the_classified_reason(client, bridge_gw):
    use(client, FetchResult(None, None, 3000, "refused"))
    response = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}")
    assert response.status_code == 502
    body = response.json()["detail"]
    assert body["reason"] == "refused"
    assert "serve.mjs 没起" in body["detail"]


def test_an_empty_table_is_reported_as_a_failure_not_an_empty_list(client, bridge_gw):
    """200 + 空数组和"桥没连上"在 UI 上长得一模一样，必须分开说。"""
    use(client, answered([]))
    response = client.get(f"/api/endpoints?gateway={bridge_gw['gateway_id']}")
    assert response.status_code == 502
    assert response.json()["detail"]["reason"] == "empty_endpoints"


# --- POST /api/board/mode ----------------------------------------------------


def test_mode_switch_forwards_the_body_and_keeps_the_bridge_answer(client, bridge_gw):
    transport = use(client, answered({"state": "scan"}, status_code=200))
    response = client.post(
        f"/api/board/mode?gateway={bridge_gw['gateway_id']}", json={"mode": "scan"}
    )
    assert response.status_code == 200
    assert response.json() == {"state": "scan"}
    assert transport.calls[-1][:3] == ("POST", f"{BASE_URL}/api/mode", {"mode": "scan"})


def test_a_409_from_the_bridge_stays_a_409(client, bridge_gw):
    """板卡归属（rh↔scan 二选一）由桥裁决；后端把它吞成 200 就等于绕过了硬约束。"""
    use(client, answered({"error": "busy"}, status_code=409))
    response = client.post(
        f"/api/board/mode?gateway={bridge_gw['gateway_id']}", json={"mode": "rh"}
    )
    assert response.status_code == 409
    assert response.json() == {"error": "busy"}


def test_an_odd_mode_is_forwarded_and_the_bridge_answers_400(client, bridge_gw):
    """不合法取值由桥自己拒（本地加白名单会让两边语义分叉）。"""
    use(client, answered({"error": "bad mode"}, status_code=400))
    response = client.post(
        f"/api/board/mode?gateway={bridge_gw['gateway_id']}", json={"mode": "banana"}
    )
    assert response.status_code == 400


def test_mode_switch_refuses_to_guess_the_gateway(client, bridge_gw):
    use(client, answered())
    assert client.post("/api/board/mode", json={"mode": "scan"}).status_code == 422
    assert client.post("/api/board/mode?gateway=4242", json={"mode": "scan"}).status_code == 404
