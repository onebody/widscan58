"""规范 §4.4「必须测试的设置行为」十条逐条 pytest 化（S4 门槛）。

这个文件的存在理由：**十条里每一条都是一句会被写坏的实现**。层级折叠、来源链回显、
坏声明降级这些逻辑只要有一处走偏，页面上看不出来——它照常渲染，只是值不对。所以这里
只认接口回的东西，不去读服务的内部状态：

- **判据是"生效值 + 来源"**，不是"库里那一列存了什么"。`source` 说 `instance` 才算局部
  覆写生效，说 `global` 就是全局兜上来的。
- **降级路径也要走 HTTP**：直接把坏 JSON 写进库，再用 `GET` 读，才是"用户在别的机器上
  改坏了 / 老版本写过的行"的真实形状。
- **不许伪绿**：后端今天还没有的机制（按组件的订阅清理）就明写"这条今天无可断言之物"，
  而不是删掉那条要求或改成断言一句空话。

同名键在不同层的落点见 `services/config_layer.py` 的注册表；这里的期望值都按注册表读出来
（`GET /api/settings/schema`），所以注册表改了值域，这个文件会跟着失败，而不是悄悄过期。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import LayoutWidget, WorkbenchLayout

WIDGETABLE_PLUGIN = "tile"


# --- 夹具与简写 ---------------------------------------------------------------


@pytest.fixture
def site(client):
    return client.post("/api/sites", json={"name": "主场", "slug": "home"}).json()


def make_layout(client, site_id: int, name: str = "默认工作台") -> dict:
    row = client.post("/api/layouts", json={"site_id": site_id, "name": name})
    assert row.status_code == 201, row.json()
    return row.json()


def put_widget(client, layout_id: int, instance_id: str, config: dict) -> dict:
    row = client.put(
        f"/api/layouts/{layout_id}/widgets/{instance_id}",
        json={"plugin_id": WIDGETABLE_PLUGIN, "config": config},
    )
    assert row.status_code == 200, row.json()
    return row.json()


def widgets(client, layout_id: int) -> dict[str, dict]:
    """实例 id → 那一格。断言写成 `tiles["A"]["effective"]["threshold_db"]` 才读得懂。"""
    row = client.get(f"/api/layouts/{layout_id}/widgets")
    assert row.status_code == 200, row.json()
    return {row["instance_id"]: row for row in row.json()}


def value(tile: dict, key: str):
    return tile["effective"][key]["value"]


def source(tile: dict, key: str) -> str:
    return tile["effective"][key]["source"]


def write_global(client, settings: dict, version: int | None = None, **extra) -> dict:
    body = {"version": version, "settings": settings, **extra}
    row = client.put("/api/settings/global", json=body)
    assert row.status_code == 200, row.json()
    return row.json()


def current_version(client) -> int:
    return client.get("/api/settings/global").json()["version"]


# --- 第一条：改 A 的阈值不影响 B --------------------------------------------


def test_tile_a_threshold_does_not_move_tile_b(client, site):
    """同布局两格，只给 A 设阈值：B 必须还按上层走，且来源里不许出现 instance。"""
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    put_widget(client, layout["layout_id"], "B", {})

    tiles = widgets(client, layout["layout_id"])
    assert value(tiles["A"], "threshold_db") == -95
    assert source(tiles["A"], "threshold_db") == "instance"
    assert value(tiles["B"], "threshold_db") == -85  # 平台默认
    assert source(tiles["B"], "threshold_db") == "default"
    # 链里五层全出现（这才叫"逐层比过"），但没设的那层只带层名、不带值。
    chain = {c["layer"]: c for c in tiles["B"]["effective"]["threshold_db"]["chain"]}
    assert list(chain) == ["default", "global", "site", "layout", "instance"]
    assert all("value" not in chain[layer] for layer in ("global", "site", "layout", "instance"))


# --- 第二条：同类型组件可以有不同配置 ----------------------------------------


def test_two_tiles_of_the_same_plugin_keep_different_configs(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95, "refresh_ms": 1000})
    put_widget(client, layout["layout_id"], "B", {"threshold_db": -70, "refresh_ms": 500})

    tiles = widgets(client, layout["layout_id"])
    assert value(tiles["A"], "threshold_db") == -95
    assert value(tiles["B"], "threshold_db") == -70
    assert value(tiles["A"], "refresh_ms") == 1000
    assert value(tiles["B"], "refresh_ms") == 500
    # 同 plugin_id 不是同配置的理由：主键是 (instance_id, layout_id)。
    assert tiles["A"]["plugin_id"] == tiles["B"]["plugin_id"] == WIDGETABLE_PLUGIN


# --- 第三条：全局主题同步到跟随全局的组件 ------------------------------------


def test_global_theme_reaches_tiles_that_do_not_declare_one(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "follow", {})
    put_widget(client, layout["layout_id"], "declare", {"theme": "light"})

    write_global(client, {"theme": "light"}, version=0)
    tiles = widgets(client, layout["layout_id"])
    assert value(tiles["follow"], "theme") == "light"
    assert source(tiles["follow"], "theme") == "global"

    write_global(client, {"theme": "dark"}, version=current_version(client))
    tiles = widgets(client, layout["layout_id"])
    # 跟着全局的那格换了；自己声明过的那格不受影响——"同步"不等于"覆盖"。
    assert value(tiles["follow"], "theme") == "dark"
    assert value(tiles["declare"], "theme") == "light"
    assert source(tiles["declare"], "theme") == "instance"


# --- 第四条：全局默认刷新周期不静默覆盖明确的局部周期 ------------------------


def test_global_refresh_default_does_not_silently_override_an_explicit_local_one(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"refresh_ms": 1000})

    write_global(client, {"refresh_ms": 500}, version=0)

    tile = widgets(client, layout["layout_id"])["A"]
    assert value(tile, "refresh_ms") == 1000
    assert source(tile, "refresh_ms") == "instance"
    # 全局那一层设过要看得见：链里保留它的值，只是最终没生效。
    chain = {c["layer"]: c for c in tile["effective"]["refresh_ms"]["chain"]}
    assert chain["global"]["value"] == 500
    # 没设局部的另一格则真的跟着全局走。
    put_widget(client, layout["layout_id"], "B", {})
    assert value(widgets(client, layout["layout_id"])["B"], "refresh_ms") == 500


# --- 第五条：切换布局恢复各自的局部配置 --------------------------------------


def test_switching_layouts_restores_each_layouts_local_config(client, site):
    a = make_layout(client, site["site_id"], "排位赛")
    b = make_layout(client, site["site_id"], "决赛")
    put_widget(client, a["layout_id"], "A", {"threshold_db": -95, "time_range_s": 30})
    put_widget(client, b["layout_id"], "A", {"threshold_db": -70, "time_range_s": 60})

    assert value(widgets(client, b["layout_id"])["A"], "threshold_db") == -70
    assert value(widgets(client, a["layout_id"])["A"], "threshold_db") == -95
    # 同名的实例 id 在两个布局里是两个格子——这正是复合主键要买的东西。
    assert (
        widgets(client, b["layout_id"])["A"]["effective"]["threshold_db"]["source"]
        == "instance"
    )
    # 激活态只决定"页面上是哪一份"，两份配置都存在。
    client.post(f"/api/layouts/{b['layout_id']}/activate")
    assert client.get(f"/api/layouts/{b['layout_id']}").json()["is_active"] is True
    assert value(widgets(client, a["layout_id"])["A"], "threshold_db") == -95


# --- 第六条：刷新后全局与局部都能恢复 ----------------------------------------


def test_a_page_reload_restores_both_global_and_local_settings(client, site, db_path):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    write_global(client, {"theme": "light", "lang": "zh"}, version=0)

    # "刷新"= 新进程读同一套库：不复用 app 的内存状态，才证明真源在库里。
    revived = TestClient(create_app(Settings(db_path=db_path, dialer_enabled=False)))
    global_view = revived.get("/api/settings/global").json()
    assert global_view["settings"]["theme"]["value"] == "light"
    assert global_view["settings"]["lang"]["value"] == "zh"
    assert global_view["version"] == 1

    tiles = revived.get(f"/api/layouts/{layout['layout_id']}/widgets").json()
    tile = next(row for row in tiles if row["instance_id"] == "A")
    assert tile["config"] == {"threshold_db": -95}
    assert tile["effective"]["threshold_db"]["value"] == -95
    assert tile["effective"]["theme"]["value"] == "light"


# --- 第七条：非法配置不会让工作台崩 ------------------------------------------


def test_a_malformed_declaration_degrades_instead_of_crashing_the_workbench(
    client, app, site
):
    """库里存坏 JSON 是"另一台机器写的/老版本写的"的形状，不是异常路径。"""
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    with app.state.db.scope() as session:
        session.get(WorkbenchLayout, layout["layout_id"]).defaults_json = "{坏掉的一半"
        session.get(LayoutWidget, ("A", layout["layout_id"])).config_json = '["不是对象"]'

    row = client.get(f"/api/layouts/{layout['layout_id']}")
    assert row.status_code == 200, row.json()
    assert row.json()["defaults"] is None
    assert row.json()["declaration_error"] == "malformed_json"

    tiles = client.get(f"/api/layouts/{layout['layout_id']}/widgets").json()
    assert len(tiles) == 1, "坏声明不许把整张列表拖下线"
    tile = tiles[0]
    assert tile["declaration_error"] == "not_an_object"
    # 坏的那两层视为未设置：阈值回落到平台默认，而不是 500、也不是 0 或空串。
    assert tile["effective"]["threshold_db"]["value"] == -85
    assert tile["effective"]["threshold_db"]["source"] == "default"
    assert {c["layer"] for c in tile["effective"]["threshold_db"]["rejected"]} == {
        "layout",
        "instance",
    }


# --- 第八条：删除组件清理其配置实例 ------------------------------------------


def test_deleting_a_tile_removes_its_config_row(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    assert client.delete(f"/api/layouts/{layout['layout_id']}/widgets/A").status_code == 204

    assert widgets(client, layout["layout_id"]) == {}
    assert client.get(f"/api/layouts/{layout['layout_id']}").json()["widget_count"] == 0
    # 行真的没了，不是"藏起来"：不带 plugin_id 重建同一格会被拒（格子存在时并不需要它）。
    again = client.put(
        f"/api/layouts/{layout['layout_id']}/widgets/A", json={"config": {"theme": "dark"}}
    )
    assert again.status_code == 422
    assert again.json()["detail"]["reason"] == "plugin_id_required"


def test_deleting_a_layout_takes_its_tiles_with_it(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    assert client.delete(f"/api/layouts/{layout['layout_id']}").status_code == 204
    assert client.get(f"/api/layouts/{layout['layout_id']}").status_code == 404
    assert client.get("/api/layouts", params={"site": site["site_id"]}).json() == []


@pytest.mark.skip(reason="按组件的订阅在 S5/S7 才存在：今天没有可断言之物，不假装测过")
def test_deleting_a_tile_clears_its_subscriptions():
    """§4.4 第八条的后半句。后端目前的订阅只到 (gateway, ep)（`services/ws_bridge.py`），
    还没有"某个组件订阅某路数据"这一层，所以这条只能等 S5 落地后再写。"""


# --- 第九条：局部设置绕不过权限、硬件与资源限制 ------------------------------


def test_a_tile_cannot_escape_protection_hardware_or_resource_limits(client, site):
    layout = make_layout(client, site["site_id"])

    protected = client.put(
        f"/api/layouts/{layout['layout_id']}/widgets/A",
        json={"plugin_id": WIDGETABLE_PLUGIN, "config": {"instance_role": "aggregator"}},
    )
    assert protected.status_code == 422
    assert protected.json()["detail"]["errors"] == [
        {"key": "instance_role", "reason": "protected_key"}
    ]

    hardware = client.put(
        f"/api/layouts/{layout['layout_id']}/widgets/A",
        json={"plugin_id": WIDGETABLE_PLUGIN, "config": {"slots": [0, 1, 2, 3, 4, 5, 6, 7, 8]}},
    )
    assert hardware.status_code == 422
    assert hardware.json()["detail"]["errors"] == [
        {"key": "slots", "reason": "slot_out_of_range"}
    ]

    # 资源限制看的是"这一格最终生效的那组值"，不是"这个键单层合不合法"。
    over = client.put(
        f"/api/layouts/{layout['layout_id']}/widgets/A",
        json={"plugin_id": WIDGETABLE_PLUGIN, "config": {"time_range_s": 3600}},
    )
    assert over.status_code == 409
    detail = over.json()["detail"]
    assert detail["reason"] == "ring_capacity_exceeded"
    assert detail["points"] == 18000 and detail["ring_depth_per_slot"] == 300
    # 拒绝要整笔回滚：库里不许留下半套配置。
    assert widgets(client, layout["layout_id"]) == {}

    layout_level = client.patch(
        f"/api/layouts/{layout['layout_id']}", json={"defaults": {"time_range_s": 3600}}
    )
    assert layout_level.status_code == 409
    assert layout_level.json()["detail"]["reason"] == "ring_capacity_exceeded"


def test_the_slot_domain_is_zero_based_wherever_it_is_read(client, site):
    """档位下标的合法域是 `0..slots_per_node-1`，三处读点必须给同一个答案。

    为什么单独一条：`minimum` 一度在注册表里写成 **1**（把 1 基的显示"第 1 档"当成了 0 基的
    存储下标），而写入闸 `_validate_slots` 一直接受 0。页面按复选控件渲染所以看不出来，但任何
    按 schema 生成表单的消费方都会少给一档——这一档恰好是最常看的那一档（`planner.c:67` 填进
    `.id` 的 plan 下标从 0 开始）。所以这里不只看写入闸，还把**声明**和**读取闸**一起对：
    三处来自不同模块，改一处而不同步另外两处就会红。

    顺带钉住一个不对称：`kind=="slots"` 的 schema `maximum` 是**数量上界（不含）**，
    而 `int`/`float` 的 `maximum` 含进去（`config_layer.py:235` 用 `value > high`）。
    前端 `app.js:790` 正是按"不含"渲染成 `length: high` 个复选框的，口径自洽；这里如实写下来，
    免得下一个人把两种 kind 的 `maximum` 当成同一个语义去读。
    """
    schema = {row["key"]: row for row in client.get("/api/settings/schema").json()["keys"]}
    declared = schema["slots"]
    assert declared["minimum"] == 0, "第 0 档合法：声明成 1 就是让消费方少给一档"

    count = declared["maximum"]
    refused = client.get(f"/api/samples/summary?site={site['site_id']}&slot={count}")
    assert refused.status_code == 422
    detail = refused.json()["detail"]
    assert detail["reason"] == "slot_out_of_range"
    assert detail["maximum"] == count - 1, "读取闸的上界必须是声明的最后一个合法下标"

    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"slots": [0]})
    tiles = widgets(client, layout["layout_id"])
    assert value(tiles["A"], "slots") == [0] and source(tiles["A"], "slots") == "instance"
    put_widget(client, layout["layout_id"], "A", {"slots": [count - 1]})
    assert value(widgets(client, layout["layout_id"])["A"], "slots") == [count - 1]


def test_the_aggregator_instance_refuses_every_write(db_path):
    """S4 的"权限"= 层约束（2026-10-29 裁定，token 归 S11）：汇聚实例一切写 405。"""
    writer = TestClient(create_app(Settings(db_path=db_path, dialer_enabled=False)))
    site = writer.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
    layout = make_layout(writer, site["site_id"])
    put_widget(writer, layout["layout_id"], "A", {"threshold_db": -95})

    reader = TestClient(
        create_app(
            Settings(db_path=db_path, dialer_enabled=False, instance_role="aggregator")
        )
    )
    assert reader.get("/api/settings/global").status_code == 200
    assert reader.get(f"/api/layouts/{layout['layout_id']}/widgets").status_code == 200
    for call in (
        lambda: reader.put("/api/settings/global", json={"version": 1, "settings": {"theme": "light"}}),
        lambda: reader.put(f"/api/settings/site/{site['site_id']}", json={"settings": {"tz": "UTC"}}),
        lambda: reader.post("/api/sites", json={"name": "另一场", "slug": "other"}),
        lambda: reader.patch(f"/api/layouts/{layout['layout_id']}", json={"name": "改了名"}),
        lambda: reader.put(
            f"/api/layouts/{layout['layout_id']}/widgets/A",
            json={"config": {"threshold_db": -70}},
        ),
        lambda: reader.post("/api/settings/local-import", json={"entries": {}}),
    ):
        row = call()
        assert row.status_code == 405, (row.status_code, row.text)
        assert row.json()["detail"] == {
            "reason": "aggregator_read_only",
            "instance_role": "aggregator",
            "detail": "汇聚实例只读：配置与层级只在场地实例上改",
        }
    # 拒了就是没写：场地实例那边一切如旧。
    assert writer.get(f"/api/layouts/{layout['layout_id']}").json()["name"] == "默认工作台"
    assert value(widgets(writer, layout["layout_id"])["A"], "threshold_db") == -95


def test_local_layers_cannot_write_keys_that_only_the_global_layer_owns(client, site):
    """`lang` 这类只有全局层有落点的键，在组件层设就是"这层没资格"。"""
    layout = make_layout(client, site["site_id"])
    row = client.put(
        f"/api/layouts/{layout['layout_id']}/widgets/A",
        json={"plugin_id": WIDGETABLE_PLUGIN, "config": {"lang": "zh", "units": "dbfs"}},
    )
    assert row.status_code == 422
    assert {e["reason"] for e in row.json()["detail"]["errors"]} == {"layer_not_allowed"}


# --- 第十条：改普通 UI 设置不会意外重启采集 ----------------------------------


def test_ordinary_ui_settings_do_not_restart_the_collector(client, app, site, monkeypatch):
    layout = make_layout(client, site["site_id"])
    restarts: list[str] = []
    dialer = app.state.dialer
    monkeypatch.setattr(
        dialer, "start", lambda *a, **k: restarts.append("start"), raising=False
    )
    monkeypatch.setattr(dialer, "stop", lambda *a, **k: restarts.append("stop"), raising=False)

    before = client.get("/api/health").json()["dialer"]
    write_global(
        client,
        {"theme": "light", "lang": "zh", "refresh_ms": 500, "time_range_s": 60},
        version=0,
    )
    client.put(f"/api/settings/site/{site['site_id']}", json={"settings": {"tz": "Asia/Shanghai"}})
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95, "show_average": False})

    assert restarts == []
    assert client.get("/api/health").json()["dialer"] == before
    assert app.state.dialer is dialer, "换了对象就是重启了"


def test_only_the_key_that_changes_the_dial_target_needs_confirmation(client, site):
    """反过来也要成立：要重启的那一个确实要求逐个确认，否则"确认"只是个装饰。"""
    schema = client.get("/api/settings/schema").json()["keys"]
    restarts = [row["key"] for row in schema if row["requires_restart"]]
    assert restarts == ["current_site_id"]

    unconfirmed = client.put(
        "/api/settings/global", json={"version": 0, "settings": {"current_site_id": site["site_id"]}}
    )
    assert unconfirmed.status_code == 409
    assert unconfirmed.json()["detail"]["keys"] == ["current_site_id"]
    assert client.get("/api/settings/global").json()["version"] == 0, "被拒的写不许推进版本"

    confirmed = client.put(
        "/api/settings/global",
        json={
            "version": 0,
            "settings": {"current_site_id": site["site_id"]},
            "confirm": ["current_site_id"],
        },
    )
    assert confirmed.status_code == 200, confirmed.json()
    assert confirmed.json()["settings"]["current_site_id"]["value"] == site["site_id"]


# --- 判据补条：版本冲突与显式 null -------------------------------------------


def test_a_stale_version_is_refused_instead_of_overwriting_the_newer_write(client, site):
    write_global(client, {"theme": "light"}, version=0)
    stale = client.put(
        "/api/settings/global", json={"version": 0, "settings": {"theme": "dark"}}
    )
    assert stale.status_code == 409
    assert stale.json()["detail"] == {"reason": "version_conflict", "expected": 0, "actual": 1}
    assert client.get("/api/settings/global").json()["settings"]["theme"]["value"] == "light"


def test_an_explicit_null_cancels_the_override_and_falls_back_to_the_layer_above(client, site):
    layout = make_layout(client, site["site_id"])
    put_widget(client, layout["layout_id"], "A", {"threshold_db": -95})
    write_global(client, {"threshold_db": -60}, version=0)

    assert value(widgets(client, layout["layout_id"])["A"], "threshold_db") == -95
    # 取消覆写走显式 null（PUT 是整份替换语义，这里演示"同一层不设了"这条路）。
    put_widget(client, layout["layout_id"], "A", {"threshold_db": None})
    tile = widgets(client, layout["layout_id"])["A"]
    assert tile["config"] == {}
    assert value(tile, "threshold_db") == -60
    assert source(tile, "threshold_db") == "global"


def test_the_site_view_inherits_the_global_layer_when_it_sets_nothing(client, site):
    """S4 判据第二条：场地覆写为空时正确继承全局，并把来源链回显出来。"""
    write_global(client, {"theme": "light", "retention_events_days": 14}, version=0)

    view = client.get(f"/api/settings/site/{site['site_id']}").json()
    assert view["site_id"] == site["site_id"]
    theme = view["settings"]["theme"]
    assert theme["value"] == "light" and theme["source"] == "global"
    assert [c["layer"] for c in theme["chain"]] == ["default", "global", "site"]
    assert "value" not in theme["chain"][-1], "没设的那层只出现层名，不编造值"
    assert view["settings"]["retention_events_days"]["value"] == 14

    client.put(
        f"/api/settings/site/{site['site_id']}", json={"settings": {"theme": "dark"}}
    )
    mine = client.get(f"/api/settings/site/{site['site_id']}").json()
    assert mine["settings"]["theme"]["source"] == "site"
    assert mine["settings"]["theme"]["chain"][-1]["value"] == "dark"
