"""场地 / 位置 / 接入机三级 CRUD 与删除阻塞（S2 出口判据："三级 CRUD + 失败分支可测"）。

规则来自落地方案 §1 状态机与 §4 API 契约，不是这里发明的：
- 删除只允许 `archived`，且**删场地不清数据**——下面还有东西就 409，绝不级联删。
- "还在拨号的不许消失"优先于"有下级"报出来，运营者才拿得到可执行的下一步。
- archived 的场地/位置禁止再挂接入机。
- 跨场地同名是正常情况，唯一键只在父级内成立。
"""

import sqlite3

def make_site(client, slug="main", name="主场", **kw):
    body = {"name": name, "slug": slug, **kw}
    response = client.post("/api/sites", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def make_relay(client, site_id, name="起点塔"):
    response = client.post("/api/relays", json={"site_id": site_id, "name": name})
    assert response.status_code == 201, response.text
    return response.json()


def make_gateway(client, relay_id, name="pit", **kw):
    body = {"relay_id": relay_id, "name": name, "kind": "usb-bridge", **kw}
    response = client.post("/api/gateways", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def set_dial_state(db_path, gateway_id, state):
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE gateway SET dial_state = ? WHERE gateway_id = ?", (state, gateway_id)
        )
        conn.commit()


def tree(client, site_slug="main", relay_name="起点塔", gateway_name="pit"):
    site = make_site(client, slug=site_slug)
    relay = make_relay(client, site["site_id"], relay_name)
    gateway = make_gateway(client, relay["relay_id"], gateway_name)
    return site, relay, gateway


# --- site -------------------------------------------------------------------


def test_create_then_fetch_a_site(client):
    site = make_site(client, name="主场")
    fetched = client.get(f"/api/sites/{site['site_id']}").json()
    assert fetched["name"] == "主场"
    assert fetched["slug"] == "main"
    assert fetched["status"] == "active"
    assert fetched["timezone"] == "UTC"


def test_list_sites_in_insertion_order(client):
    first = make_site(client, slug="a", name="A")
    second = make_site(client, slug="b", name="B")
    listed = client.get("/api/sites").json()
    assert [s["site_id"] for s in listed] == [first["site_id"], second["site_id"]]


def test_duplicate_slug_is_rejected_with_a_reason(client):
    make_site(client, slug="main")
    response = client.post("/api/sites", json={"name": "另一个", "slug": "main"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "duplicate_slug"


def test_bad_slug_is_a_validation_error(client):
    response = client.post("/api/sites", json={"name": "x", "slug": "Main Camp"})
    assert response.status_code == 422


def test_unknown_site_id_is_404_not_empty(client):
    assert client.get("/api/sites/999").status_code == 404
    assert client.patch("/api/sites/999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/sites/999").status_code == 404


def test_patch_renames_and_archives_a_site(client):
    site = make_site(client)
    response = client.patch(f"/api/sites/{site['site_id']}", json={"name": "旧场地", "status": "archived"})
    assert response.status_code == 200
    assert response.json()["name"] == "旧场地"
    assert response.json()["status"] == "archived"


def test_an_unknown_status_value_is_rejected(client):
    site = make_site(client)
    response = client.patch(f"/api/sites/{site['site_id']}", json={"status": "deleted"})
    assert response.status_code == 422


def test_archived_site_refuses_new_relays(client):
    site = make_site(client)
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})
    response = client.post("/api/relays", json={"site_id": site["site_id"], "name": "看台"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "site_archived"


def test_deleting_an_active_site_is_blocked(client):
    site = make_site(client)
    response = client.delete(f"/api/sites/{site['site_id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "site_not_archived"


def test_deleting_an_archived_site_with_relays_is_blocked(client):
    site, _relay, _gateway = tree(client)
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})
    response = client.delete(f"/api/sites/{site['site_id']}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "site_has_relays"
    assert detail["count"] == 1


def test_a_still_dialling_gateway_blocks_its_whole_chain_from_being_deleted(client, db_path):
    """"还在拨号"必须比"有下级"先报出来：这条顺序是运营者能不能一步看懂的关键。"""
    site, relay, gateway = tree(client)
    set_dial_state(db_path, gateway["gateway_id"], "online")
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})

    for url, reason in (
        (f"/api/sites/{site['site_id']}", "gateways_online"),
        (f"/api/relays/{relay['relay_id']}", "gateways_online"),
        (f"/api/gateways/{gateway['gateway_id']}", "gateways_online"),
    ):
        response = client.delete(url)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["reason"] == reason, url


def test_an_empty_archived_site_deletes_and_then_404s(client):
    site = make_site(client)
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})
    assert client.delete(f"/api/sites/{site['site_id']}").status_code == 204
    assert client.get(f"/api/sites/{site['site_id']}").status_code == 404


# --- relay（物理位置） -------------------------------------------------------


def test_relay_needs_an_existing_site(client):
    response = client.post("/api/relays", json={"site_id": 4242, "name": "起点塔"})
    assert response.status_code == 404


def test_relay_name_is_unique_only_within_its_site(client):
    first = make_site(client, slug="site-a", name="A")
    second = make_site(client, slug="site-b", name="B")
    make_relay(client, first["site_id"], "计时塔")
    assert (
        client.post("/api/relays", json={"site_id": first["site_id"], "name": "计时塔"}).status_code
        == 409
    )
    # 跨场地同名是完全正常的，按名字全局去重会串场地
    assert (
        client.post("/api/relays", json={"site_id": second["site_id"], "name": "计时塔"}).status_code
        == 201
    )


def test_relays_filter_by_site(client):
    first = make_site(client, slug="site-a")
    second = make_site(client, slug="site-b")
    make_relay(client, first["site_id"], "看台")
    make_relay(client, second["site_id"], "起点塔")
    listed = client.get(f"/api/relays?site_id={first['site_id']}").json()
    assert [r["name"] for r in listed] == ["看台"]
    assert listed[0]["site_id"] == first["site_id"]


def test_deleting_a_relay_with_gateways_is_blocked(client):
    _site, relay, _gateway = tree(client)
    response = client.delete(f"/api/relays/{relay['relay_id']}")
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "relay_has_gateways"


def test_relay_delete_stops_working_once_children_are_gone(client):
    site, relay, gateway = tree(client)
    assert client.delete(f"/api/gateways/{gateway['gateway_id']}").status_code == 204
    assert client.delete(f"/api/relays/{relay['relay_id']}").status_code == 204
    assert client.delete(f"/api/sites/{site['site_id']}").status_code == 409  # 还得先 archived
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})
    assert client.delete(f"/api/sites/{site['site_id']}").status_code == 204


# --- gateway（接入机） -------------------------------------------------------


def test_gateway_needs_an_existing_relay(client):
    response = client.post(
        "/api/gateways", json={"relay_id": 9999, "name": "pit", "kind": "usb-bridge"}
    )
    assert response.status_code == 404


def test_gateway_kind_is_an_enum(client):
    _site, relay, _gateway = tree(client)
    response = client.post(
        "/api/gateways", json={"relay_id": relay["relay_id"], "name": "x", "kind": "raspberry"}
    )
    assert response.status_code == 422


def test_archived_relay_refuses_new_gateways(client):
    _site, relay, _gateway = tree(client)
    client.patch(f"/api/relays/{relay['relay_id']}", json={"status": "archived"})
    response = client.post(
        "/api/gateways", json={"relay_id": relay["relay_id"], "name": "new", "kind": "usb-bridge"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "relay_archived"


def test_gateway_name_is_unique_only_within_its_relay(client):
    site = make_site(client, slug="site-a")
    other_site = make_site(client, slug="site-b")
    relay_a = make_relay(client, site["site_id"], "看台")
    relay_b = make_relay(client, other_site["site_id"], "起点塔")
    make_gateway(client, relay_a["relay_id"], "pi-1")
    assert (
        client.post(
            "/api/gateways", json={"relay_id": relay_a["relay_id"], "name": "pi-1", "kind": "usb-bridge"}
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/gateways", json={"relay_id": relay_b["relay_id"], "name": "pi-1", "kind": "usb-bridge"}
        ).status_code
        == 201
    )


def test_gateway_carries_its_derived_site_and_relay(client):
    """节点上不存 site_id，场地一律经 gateway → relay → site 推导后随读接口给出。"""
    site, relay, gateway = tree(client, site_slug="venue", relay_name="计时区", gateway_name="pi-1")
    fetched = client.get(f"/api/gateways/{gateway['gateway_id']}").json()
    assert fetched["site_id"] == site["site_id"]
    assert fetched["relay_name"] == "计时区"
    assert fetched["dial_state"] is None  # 没拨过就是没拨过，不得显示成"离线"
    listed = client.get(f"/api/gateways?site_id={site['site_id']}").json()
    assert [g["gateway_id"] for g in listed] == [gateway["gateway_id"]]


def test_deleting_a_gateway_with_nodes_is_blocked(client, db_path):
    _site, _relay, gateway = tree(client)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO node (node_id, gateway_id, display_name, transport_kind, transport_addr, status, stops_rh) "
            "VALUES (1, ?, 'widscan58-1', 'serial', '/dev/widscan58-a', 'active', 0)",
            (gateway["gateway_id"],),
        )
        conn.commit()
    response = client.delete(f"/api/gateways/{gateway['gateway_id']}")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["reason"] == "gateway_has_nodes"
    assert detail["count"] == 1


def test_dial_state_is_not_writable_through_patch(client, db_path):
    """拨号态是 probe 的产出，开放给 PATCH 就等于允许"手工连上一台机器"。"""
    _site, _relay, gateway = tree(client)
    set_dial_state(db_path, gateway["gateway_id"], "unreachable")
    response = client.patch(
        f"/api/gateways/{gateway['gateway_id']}", json={"dial_state": "online"}
    )
    assert response.status_code == 200
    assert response.json()["dial_state"] == "unreachable"


def test_patching_base_url_invalidates_the_previous_probe(client, db_path):
    """地址一改，之前测出来的连通性就不再描述这台机器现在的状态。"""
    _site, _relay, gateway = tree(client)
    set_dial_state(db_path, gateway["gateway_id"], "online")
    response = client.patch(
        f"/api/gateways/{gateway['gateway_id']}",
        json={"base_url": "http://192.168.99.111:8080"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["base_url"] == "http://192.168.99.111:8080"
    assert body["dial_state"] is None
    assert body["dial_reason"] is None


def test_archiving_a_relay_still_dialling_is_blocked(client, db_path):
    """§1：不允许出现"位置没了但还在拨号"。"""
    _site, relay, gateway = tree(client)
    set_dial_state(db_path, gateway["gateway_id"], "online")
    response = client.patch(f"/api/relays/{relay['relay_id']}", json={"status": "archived"})
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "gateways_online"


def test_archiving_a_quiet_relay_is_allowed(client, db_path):
    _site, relay, gateway = tree(client)
    set_dial_state(db_path, gateway["gateway_id"], "unreachable")
    response = client.patch(f"/api/relays/{relay['relay_id']}", json={"status": "archived"})
    assert response.status_code == 200
    assert response.json()["status"] == "archived"


def test_archived_site_refuses_new_gateways_too(client):
    site, relay, _gateway = tree(client)
    client.patch(f"/api/sites/{site['site_id']}", json={"status": "archived"})
    response = client.post(
        "/api/gateways", json={"relay_id": relay["relay_id"], "name": "late", "kind": "usb-bridge"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["reason"] == "site_archived"


def test_unknown_relay_and_gateway_ids_are_404(client):
    assert client.get("/api/relays/1234").status_code == 404
    assert client.patch("/api/relays/1234", json={"name": "x"}).status_code == 404
    assert client.delete("/api/relays/1234").status_code == 404
    assert client.get("/api/gateways/1234").status_code == 404
    assert client.patch("/api/gateways/1234", json={"name": "x"}).status_code == 404
    assert client.delete("/api/gateways/1234").status_code == 404
