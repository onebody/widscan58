"""`base_url` 只接受局域网地址（§4："**拒绝任何公网/云地址**"，本地化部署定调）。

判定用显式白名单而不是 `ipaddress.is_private`：后者把 100.64.0.0/10（运营商 CGNAT）
和 192.0.2.0/24（文档保留段）也算"私有"，那两类都不是你自己拉线的场地网络。
"""

import pytest

SITE = {"name": "主场", "slug": "main"}
RELAY = {"site_id": 1, "name": "起点塔"}

LAN_OK = [
    "http://192.168.99.111:8080",
    "http://10.0.0.5",
    "http://172.20.3.4:8080",
    "http://169.254.11.22:8080",
    "http://127.0.0.1:8080",
    "http://[::1]:8080",
    "http://[fd00::12]:8080",
    "http://[fe80::1]:8080",
    "http://raspberrypi.local:8080",
    "http://pi-office.lan:8080",
    "http://widscan-arena.home.arpa:8080",
    # 无点的主机名不可能在公网 DNS 上解析，只能是本地/内网名字
    "http://raspberrypi:8080",
    "http://localhost:8080",
]

WAN_REJECTED = [
    "http://203.0.113.9:8080",
    "http://8.8.8.8:53",
    "https://track.example.com",
    "http://mytunnel.ngrok.io",
    "http://shop.cn-hangzhou.aliyuncs.com",
    "http://100.64.0.1:8080",  # CGNAT：不是你拉的线
    "http://192.0.2.10:8080",  # 文档保留段
    "http://192.168.1.1.evil.com/",  # 尾巴是域名，不是内网 IP
    "ftp://192.168.1.5:21",  # 只允许 http/https
    "http://user:pass@192.168.1.5:8080",  # 凭据会漏进日志
    "192.168.1.5:8080",  # 没有 scheme
    "",  # 空串不是"未填写"
]


@pytest.fixture
def relay_id(client):
    site = client.post("/api/sites", json=SITE).json()
    relay = client.post("/api/relays", json={**RELAY, "site_id": site["site_id"]}).json()
    return relay["relay_id"]


def post_gateway(client, relay_id, base_url):
    return client.post(
        "/api/gateways",
        json={"relay_id": relay_id, "name": "pit", "kind": "usb-bridge", "base_url": base_url},
    )


@pytest.mark.parametrize("url", LAN_OK)
def test_lan_addresses_are_accepted(client, relay_id, url):
    response = post_gateway(client, relay_id, url)
    assert response.status_code == 201, response.text
    assert response.json()["base_url"] == url


@pytest.mark.parametrize("url", WAN_REJECTED)
def test_non_lan_addresses_are_rejected_with_an_explanation(client, relay_id, url):
    response = post_gateway(client, relay_id, url)
    assert response.status_code == 422, f"{url} 竟然通过了"
    detail = response.json()["detail"]
    assert any("局域网" in str(item.get("msg", item)) for item in detail)


def test_nothing_is_written_when_the_address_is_rejected(client, relay_id):
    post_gateway(client, relay_id, "https://cloud.example.com")
    assert client.get("/api/gateways").json() == []


def test_a_gateway_may_be_saved_without_an_address_yet(client, relay_id):
    response = client.post(
        "/api/gateways", json={"relay_id": relay_id, "name": "未接线", "kind": "usb-bridge"}
    )
    assert response.status_code == 201
    assert response.json()["base_url"] is None


def test_patching_an_existing_gateway_follows_the_same_rule(client, relay_id):
    gateway = post_gateway(client, relay_id, "http://192.168.99.111:8080").json()
    response = client.patch(
        f"/api/gateways/{gateway['gateway_id']}", json={"base_url": "http://198.51.100.7:8080"}
    )
    assert response.status_code == 422
    assert client.get(f"/api/gateways/{gateway['gateway_id']}").json()["base_url"] == (
        "http://192.168.99.111:8080"
    )
