"""端点表 → 节点行：身份、能力与"消失不等于删除"。

三条口径来自落地方案 §1/§4/§5，都是硬约束不是风格：
- 身份键 `(gateway_id, transport_addr)`，显示名不参与去重；重复一次同步不许长出第二行。
- **能力只按声明渲染**：serve.mjs 的 `/api/endpoints` 到今天既不报 `fw_version`
  也不报任何协议版本（§7 S2 偏差①），所以这些列必须是 NULL，软件侧一个都不许猜。
- 端点在重枚举里消失 → `lost` + 保留行与 `last_seen`，**绝不删行**：删了就没法回答
  "那块板昨天还在不在"。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import Settings
from app.models import Gateway, Node
from app.services.ingest import mark_gateway_lost, reconcile

SERIAL_A = {
    "id": "serial:/dev/widscan58-A",
    "kind": "serial",
    "label": "widscan58-A",
    "path": "/dev/widscan58-A",
    "online": True,
    "busy": False,
}
SERIAL_B = {**SERIAL_A, "id": "serial:/dev/widscan58-B", "label": "widscan58-B", "path": "/dev/widscan58-B"}
#: ②a 的 UART 出口：serve.mjs 用 realpath 后的路径，且带 scannable（有没有配停 RH 的命令）。
UART = {
    "id": "uart:/dev/serial0",
    "kind": "uart",
    "label": "ttyAMA0",
    "path": "/dev/ttyAMA0",
    "online": True,
    "busy": False,
    "scannable": True,
}
#: ③ 的中继节点：serve.mjs 这条**没有 path**（它不是串口），身份只能落在 id 上。
REMOTE = {"id": "node:pit-2", "kind": "remote", "label": "pit-2", "online": True, "busy": False}


@pytest.fixture
def gw(client):
    site = client.post("/api/sites", json={"name": "主场", "slug": "main"}).json()
    relay = client.post("/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}).json()
    body = {"relay_id": relay["relay_id"], "name": "pi-1", "kind": "usb-bridge"}
    return {**client.post("/api/gateways", json=body).json(), "site": site, "relay": relay}


@pytest.fixture
def second_gw(client, gw):
    """同位置的第二台接入机：多接入机并跑时最要命的是两台报了同名端点。"""
    relay = client.post(
        "/api/relays", json={"site_id": gw["site"]["site_id"], "name": "看台"}
    ).json()
    return client.post(
        "/api/gateways", json={"relay_id": relay["relay_id"], "name": "pi-2", "kind": "usb-bridge"}
    ).json()


def rows(client):
    with client.app.state.db.scope() as session:
        return list(session.execute(select(Node).order_by(Node.node_id)).scalars())


def sync(client, gateway_id, endpoints, settings=None):
    with client.app.state.db.scope() as session:
        gateway = session.get(Gateway, gateway_id)
        result = reconcile(session, gateway, endpoints, settings or Settings())
        session.flush()
        return result


def test_each_declared_kind_lands_on_its_own_transport(client, gw):
    """serial/uart/remote 三套叫法都要翻成 node.transport_kind 里那三个值。"""
    sync(client, gw["gateway_id"], [SERIAL_A, UART, REMOTE])
    got = {row.transport_addr: row.transport_kind for row in rows(client)}
    assert got == {
        "/dev/widscan58-A": "usb-native",
        "/dev/ttyAMA0": "uart-pl011",
        "node:pit-2": "node-ws",
    }


def test_an_endpoint_without_a_path_is_identified_by_its_id(client, gw):
    """remote 端点没有串口路径：地址必须落回 id，不能编一个 `/dev/...` 出来。"""
    result = sync(client, gw["gateway_id"], [REMOTE])
    assert result.seen == ("node:pit-2",)


def test_the_display_name_follows_the_bridge_label(client, gw):
    sync(client, gw["gateway_id"], [SERIAL_A])
    assert rows(client)[0].display_name == "widscan58-A"


def test_the_identity_key_is_gateway_plus_addr(client, gw):
    """同一份端点表喂两次只能有一行：dialer 每轮重枚举都调它，长出重复行会污染全部下游。"""
    sync(client, gw["gateway_id"], [SERIAL_A, UART])
    again = sync(client, gw["gateway_id"], [SERIAL_A, UART])
    assert len(rows(client)) == 2
    assert (again.created, again.updated, again.lost) == ((), (), ())


def test_a_duplicate_address_in_one_table_is_recorded_not_double_written(client, gw):
    """udev 别名与真实设备名各报一次同一个 path：只能有一行，且必须说"我看到过重复"。"""
    result = sync(client, gw["gateway_id"], [SERIAL_A, dict(SERIAL_A, id="serial:/dev/real")])
    assert len(rows(client)) == 1
    assert result.duplicates == ("/dev/widscan58-A",)


def test_the_label_is_refreshed_when_the_bridge_renames_it(client, gw):
    sync(client, gw["gateway_id"], [SERIAL_A])
    sync(client, gw["gateway_id"], [{**SERIAL_A, "label": "起点塔 1 号板"}])
    assert rows(client)[0].display_name == "起点塔 1 号板"


def test_the_same_addr_on_two_gateways_stays_two_nodes(client, gw, second_gw):
    """多接入机并跑：两台都报 `/dev/widscan58-1` 就是两块板，按显示名去重会串位置。"""
    dup = {**SERIAL_A, "id": "serial:/dev/widscan58-1", "label": "widscan58-1", "path": "/dev/widscan58-1"}
    sync(client, gw["gateway_id"], [dup])
    sync(client, second_gw["gateway_id"], [dup])
    landed = rows(client)
    assert len(landed) == 2
    assert {row.gateway_id for row in landed} == {gw["gateway_id"], second_gw["gateway_id"]}


def test_a_vanished_endpoint_turns_lost_and_keeps_its_row(client, gw):
    first = sync(client, gw["gateway_id"], [SERIAL_A, SERIAL_B])
    assert len(first.created) == 2
    second = sync(client, gw["gateway_id"], [SERIAL_A])
    assert second.lost == ("/dev/widscan58-B",)
    landed = {row.transport_addr: row for row in rows(client)}
    assert len(landed) == 2  # 不删行
    assert landed["/dev/widscan58-B"].status == "lost"
    assert landed["/dev/widscan58-B"].last_seen is not None  # "上次还在"是唯一的证据
    assert landed["/dev/widscan58-A"].status == "active"


def test_a_lost_endpoint_that_comes_back_is_active_again(client, gw):
    sync(client, gw["gateway_id"], [SERIAL_A, SERIAL_B])
    sync(client, gw["gateway_id"], [SERIAL_A])
    sync(client, gw["gateway_id"], [SERIAL_A, SERIAL_B])
    assert [row.status for row in rows(client)] == ["active", "active"]


def test_an_enumerated_but_offline_endpoint_is_not_called_active(client, gw):
    """serve.mjs 会列出 `online:false` 的 uart 端点（设备文件当前不在）：
    "列出来了"和"接得上"是两件事，混成 active 就等于报假状态。"""
    sync(client, gw["gateway_id"], [{**UART, "online": False}])
    node = rows(client)[0]
    assert node.status == "offline"
    assert node.last_seen is None


def test_a_gateway_judged_unreachable_marks_all_its_nodes_lost(client, gw):
    """整台接入机判死时，它下面的节点跟着 lost——但行还在。"""
    sync(client, gw["gateway_id"], [SERIAL_A, UART])
    with client.app.state.db.scope() as session:
        assert mark_gateway_lost(session, session.get(Gateway, gw["gateway_id"])) == 2
    assert [row.status for row in rows(client)] == ["lost", "lost"]
    assert len(rows(client)) == 2


def test_marking_lost_does_not_disturb_another_gateway(client, gw, second_gw):
    sync(client, gw["gateway_id"], [SERIAL_A])
    sync(client, second_gw["gateway_id"], [SERIAL_B])
    with client.app.state.db.scope() as session:
        mark_gateway_lost(session, session.get(Gateway, gw["gateway_id"]))
    by_gateway = {row.gateway_id: row.status for row in rows(client)}
    assert by_gateway == {gw["gateway_id"]: "lost", second_gw["gateway_id"]: "active"}


def test_the_version_columns_stay_null_because_the_bridge_declares_none(client, gw):
    """§1 的硬约束：不支持的能力显示 unsupported，不得伪造。
    serve.mjs 现在不吐任何版本字段，所以这里必须是 NULL——填上"1"就是把猜的当测的。"""
    sync(client, gw["gateway_id"], [SERIAL_A])
    node = rows(client)[0]
    assert (node.fw_version, node.protocol_version, node.capabilities_json) == (None, None, None)


def test_a_uart_node_is_written_with_stops_rh_true_at_the_column(client, gw):
    """R21 的前半：②a 会停掉这台接入机的计时。这一位写在列里，不只在校验时补。"""
    sync(client, gw["gateway_id"], [UART, SERIAL_A])
    by_addr = {row.transport_addr: row for row in rows(client)}
    assert by_addr["/dev/ttyAMA0"].stops_rh is True
    assert by_addr["/dev/widscan58-A"].stops_rh is False


def test_reconcile_advertises_the_uart_medium_through_the_read_api(client, gw):
    sync(client, gw["gateway_id"], [UART])
    node = client.get(f"/api/nodes?gateway={gw['gateway_id']}").json()[0]
    assert node["transport"] == {"kind": "uart-pl011", "addr": "/dev/ttyAMA0"}
    assert node["stops_rh"] is True
    assert node["capabilities"]["spectrum"] == {"supported": False, "reason": "undeclared"}


def test_more_endpoints_than_the_gate_is_a_409_and_writes_nothing(client, gw):
    """超限必须明确拒绝并说清是哪些端点，"静默丢掉新的"会让用户以为加了机器没数据。"""
    with pytest.raises(HTTPException) as excinfo:
        sync(client, gw["gateway_id"], [SERIAL_A, SERIAL_B, UART], Settings(max_nodes_per_gateway=2))
    assert excinfo.value.status_code == 409
    detail = excinfo.value.detail
    assert detail["reason"] == "max_nodes_per_gateway"
    assert detail["limit"] == 2
    assert detail["endpoints"] == ["widscan58-A", "widscan58-B", "ttyAMA0"]
    assert rows(client) == []


def test_a_single_endpoint_reconcile_is_idempotent_about_status(client, gw):
    """一轮里既创建又看见：created 与 seen 都该有它，供上层算增量。"""
    result = sync(client, gw["gateway_id"], [SERIAL_A])
    assert (result.created, result.seen) == (("/dev/widscan58-A",), ("/dev/widscan58-A",))
