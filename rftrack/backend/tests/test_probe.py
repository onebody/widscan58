"""`POST /api/gateways/{id}/probe`：连通性**实测**（§4："不得只回'已保存'"）。

失败原因必须分类，运营者才知道是网线、进程、还是对端根本不是 serve.mjs。
另外 §1 定了一条硬规则：**一次超时不许把整台机器判死**，所以 unreachable 只在
连续失败到阈值时才落下——这条不是这里想加的复杂度，是计划里写着的。
"""

import pytest

from app.services.dialer import FetchResult

GATEWAY = {"relay_id": 1, "name": "pit", "kind": "usb-bridge", "base_url": "http://192.168.99.111:8080"}

ENDPOINTS = [
    {"id": "serial:/dev/widscan58-A", "kind": "serial", "label": "widscan58-A", "online": True, "busy": False},
    {"id": "uart:/dev/serial0", "kind": "uart", "label": "ttyAMA0", "online": True, "busy": False, "scannable": True},
]


class FakeTransport:
    """拨号器与 HTTP 层解耦：测试把想让它发生的事直接交进去。"""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def fetch(self, url, timeout_s):
        self.calls.append((url, timeout_s))
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def answered(payload=ENDPOINTS, latency_ms=41):
    return FetchResult(status_code=200, payload={"endpoints": payload}, latency_ms=latency_ms, error=None)


def failed(error, latency_ms=3000):
    return FetchResult(status_code=None, payload=None, latency_ms=latency_ms, error=error)


@pytest.fixture
def gateway(client):
    site = client.post("/api/sites", json={"name": "主场", "slug": "main"}).json()
    relay = client.post("/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}).json()
    body = {**GATEWAY, "relay_id": relay["relay_id"]}
    return client.post("/api/gateways", json=body).json()


def probe(client, gateway_id):
    return client.post(f"/api/gateways/{gateway_id}/probe")


def use(client, *results):
    transport = FakeTransport(*results)
    client.app.state.transport = transport
    return transport


def test_successful_probe_reports_latency_and_the_endpoints_it_saw(client, gateway):
    transport = use(client, answered())
    body = probe(client, gateway["gateway_id"]).json()
    assert body["ok"] is True
    assert body["latency_ms"] == 41
    assert body["endpoints_count"] == 2
    assert [e["id"] for e in body["endpoints"]] == [e["id"] for e in ENDPOINTS]
    assert transport.calls[0][0] == "http://192.168.99.111:8080/api/endpoints"


def test_successful_probe_is_written_back_to_the_gateway(client, gateway):
    use(client, answered())
    probe(client, gateway["gateway_id"])
    fetched = client.get(f"/api/gateways/{gateway['gateway_id']}").json()
    assert fetched["dial_state"] == "online"
    assert fetched["dial_reason"] is None
    assert fetched["last_seen"] is not None


@pytest.mark.parametrize(
    "result,reason",
    [
        (failed("dns"), "dns"),
        (failed("refused"), "refused"),
        (failed("timeout"), "timeout"),
        (FetchResult(502, None, 12, None), "http_status"),
        (FetchResult(200, {"serial": True}, 12, None), "unexpected_response"),
        (FetchResult(200, {"endpoints": None}, 12, None), "unexpected_response"),
        (answered(payload=[]), "empty_endpoints"),
    ],
)
def test_every_failure_gets_a_named_reason(client, gateway, result, reason):
    use(client, result)
    body = probe(client, gateway["gateway_id"]).json()
    assert body["ok"] is False
    assert body["reason"] == reason
    assert body["detail"]


def test_the_reason_is_stored_so_the_list_page_can_show_it_without_reprobing(client, gateway):
    use(client, failed("refused"))
    probe(client, gateway["gateway_id"])
    fetched = client.get(f"/api/gateways/{gateway['gateway_id']}").json()
    assert fetched["dial_reason"] == "refused"


def test_a_single_timeout_does_not_declare_the_machine_dead(client, gateway):
    """§1 原文："不得因为一次超时就把整台机器判死"。"""
    use(client, failed("timeout"))
    probe(client, gateway["gateway_id"])
    fetched = client.get(f"/api/gateways/{gateway['gateway_id']}").json()
    assert fetched["dial_reason"] == "timeout"
    assert fetched["dial_state"] is None  # 还没成功拨通过，就照实说没拨通过


def test_two_consecutive_failures_do_mark_it_unreachable(client, gateway):
    transport = use(client, failed("timeout"), failed("timeout"))
    probe(client, gateway["gateway_id"])
    transport.results = [failed("timeout")]
    body = probe(client, gateway["gateway_id"]).json()
    assert body["dial_state"] == "unreachable"
    assert body["consecutive_failures"] == 2


def test_a_success_clears_the_failure_streak(client, gateway):
    transport = use(client, failed("timeout"))
    probe(client, gateway["gateway_id"])
    transport.results = [answered()]
    probe(client, gateway["gateway_id"])
    assert client.get(f"/api/gateways/{gateway['gateway_id']}").json()["dial_state"] == "online"
    # 已在线后再失败一次，只是"这次没拨通"，不改变归属态
    transport.results = [failed("timeout")]
    body = probe(client, gateway["gateway_id"]).json()
    assert body["dial_state"] == "online"
    assert body["reason"] == "timeout"


def test_health_separates_a_failing_gateway_from_one_never_probed(client, gateway):
    """拨过一次没通 ≠ 从没拨过：混成一格，运营者会以为这台机器还没登记。"""
    use(client, failed("timeout"))
    probe(client, gateway["gateway_id"])
    body = client.get("/api/health").json()["gateways"]
    assert body["total"] == 1
    assert body["failing"] == 1
    assert body["never_probed"] == 0


def test_probing_a_gateway_without_an_address_is_refused(client, gateway):
    client.patch(f"/api/gateways/{gateway['gateway_id']}", json={"base_url": None})
    response = probe(client, gateway["gateway_id"])
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "no_base_url"


def test_unknown_gateway_is_404(client):
    use(client, answered())
    assert client.post("/api/gateways/4242/probe").status_code == 404
