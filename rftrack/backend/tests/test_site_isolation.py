"""双场地下的"A 不影响 B"逐处判定（S4 门槛判据第二条）。

夹具按定案的拓扑摆最小心机：**两个场地，每个场地两个中继位置、每位置一台接入机、
每场地一份激活布局 + 一个同名格子**。单场地夹具验不出跨场地串场，多场地但没有同名格子
又验不出"实例 id 只在布局内唯一"——所以这两件事都得在夹具里就位。

六处判定里今天有实现的是四处：设置、布局、组件配置、节点范围。事件与告警的表在 S6、
按场地的 WS 订阅在 S5/S7——那两处**显式 skip 并写清缺的是什么**，不拿"读回空列表"冒充
隔离成功（空列表在两张表还不存在时永远成立，那是最坏的假绿）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app.models import Node

PLUGIN = "tile"


@dataclass
class Rig:
    """两个场地各自的 site / relay / gateway / layout id，加一台客户端。"""

    client: TestClient
    sites: dict[str, dict] = field(default_factory=dict)
    relays: dict[str, list[int]] = field(default_factory=dict)
    gateways: dict[str, list[int]] = field(default_factory=dict)
    layouts: dict[str, int] = field(default_factory=dict)

    def settings(self, which: str) -> dict:
        row = self.client.get(f"/api/settings/site/{self.sites[which]['site_id']}")
        assert row.status_code == 200, row.json()
        return row.json()["settings"]

    def tiles(self, which: str) -> dict[str, dict]:
        row = self.client.get(f"/api/layouts/{self.layouts[which]}/widgets")
        assert row.status_code == 200, row.json()
        return {row["instance_id"]: row for row in row.json()}


@pytest.fixture
def rig(client) -> Rig:
    """A/B 两套完整层级；名字与地址都取得能互相区分，断言才写得出口。"""
    made = Rig(client)
    for which, slug in (("A", "alpha"), ("B", "beta")):
        site = client.post("/api/sites", json={"name": f"{which}场", "slug": slug}).json()
        made.sites[which] = site
        made.relays[which] = []
        made.gateways[which] = []
        for position in (1, 2):
            relay = client.post(
                "/api/relays", json={"site_id": site["site_id"], "name": f"{which}塔{position}"}
            ).json()
            made.relays[which].append(relay["relay_id"])
            gateway = client.post(
                "/api/gateways",
                json={
                    "relay_id": relay["relay_id"],
                    "name": f"pi-{slug}-{position}",
                    "kind": "usb-bridge",
                    "base_url": f"http://192.168.99.{position + (0 if which == 'A' else 20)}:8080",
                },
            ).json()
            made.gateways[which].append(gateway["gateway_id"])
        layout = client.post(
            "/api/layouts", json={"site_id": site["site_id"], "name": f"{which}台"}
        ).json()
        made.layouts[which] = layout["layout_id"]
        client.post(f"/api/layouts/{layout['layout_id']}/activate")
    return made


def put_tile(rig: Rig, which: str, instance_id: str, config: dict) -> dict:
    row = rig.client.put(
        f"/api/layouts/{rig.layouts[which]}/widgets/{instance_id}",
        json={"plugin_id": PLUGIN, "config": config},
    )
    assert row.status_code == 200, row.json()
    return row.json()


def write_site(rig: Rig, which: str, settings: dict) -> dict:
    row = rig.client.put(
        f"/api/settings/site/{rig.sites[which]['site_id']}", json={"settings": settings}
    )
    assert row.status_code == 200, row.json()
    return row.json()


# --- 一：场地级设置 -----------------------------------------------------------


def test_site_a_overrides_move_nothing_in_site_b(rig):
    write_site(rig, "A", {"threshold_db": -95, "tz": "Asia/Shanghai", "theme": "light"})

    mine = rig.settings("A")
    assert mine["threshold_db"]["value"] == -95 and mine["threshold_db"]["source"] == "site"

    theirs = rig.settings("B")
    assert theirs["threshold_db"]["value"] == -85
    assert theirs["threshold_db"]["source"] == "default"
    assert theirs["theme"]["value"] == "dark"
    assert theirs["retention_events_days"]["value"] == 7
    # B 除了自己那行结构默认，一个覆写都没落过：不许出现"A 写场地层，B 也跟着变"。
    assert {key: row["source"] for key, row in theirs.items() if row["source"] == "site"} == {
        # 每个场地行都带 `timezone`（NOT NULL 结构列，默认 UTC）：那是"这个场地声明了 UTC"，
        # 不是 A 的写入串过来的。取舍说在 §4 契约里——结构列即声明，全局 tz 只在
        # 没有场地上下文的视图（`/api/settings/global`）里生效，见下一条。
        "tz": "site"
    }
    assert theirs["tz"]["value"] == "UTC"

    # 全局层也没被场地的写入顺手改走。
    global_view = rig.client.get("/api/settings/global").json()["settings"]
    assert global_view["threshold_db"]["value"] == -85
    assert global_view["theme"]["value"] == "dark"
    assert global_view["tz"]["source"] == "default", "没有场地上下文时，tz 才轮到全局/默认"

    # 全局 tz 在那个视图里是真的生效（不是设了一个永远赢不了的值）。
    row = rig.client.put(
        "/api/settings/global", json={"version": 0, "settings": {"tz": "Europe/London"}}
    )
    assert row.status_code == 200, row.json()
    tz = row.json()["settings"]["tz"]
    assert (tz["value"], tz["source"]) == ("Europe/London", "global")
    assert [c["layer"] for c in tz["chain"]] == ["default", "global"]


def test_site_b_can_set_the_same_key_to_a_different_value_without_touching_a(rig):
    write_site(rig, "A", {"threshold_db": -95})
    write_site(rig, "B", {"threshold_db": -70})

    assert rig.settings("A")["threshold_db"]["value"] == -95
    assert rig.settings("B")["threshold_db"]["value"] == -70
    # 后写的 B 不许把 A 的覆写"顺带规范化"掉：两份 site.settings_json 各自独立。
    assert rig.settings("A")["threshold_db"]["source"] == "site"


def test_site_a_retention_days_do_not_apply_to_site_b(rig):
    """保留是按场地覆写的键（`retention_*` 属 SITABLE），A 收紧不能替 B 决定。"""
    write_site(rig, "A", {"retention_events_days": 3, "retention_summary_days": 5})

    theirs = rig.settings("B")
    assert theirs["retention_events_days"]["value"] == 7, "平台默认 7 天"
    assert theirs["retention_summary_days"]["value"] == 30
    assert theirs["retention_events_days"]["source"] == "default"

    schema = {row["key"]: row for row in rig.client.get("/api/settings/schema").json()["keys"]}
    # 说清执行者还没落地：值算得对不等于数据真在被删，这条不许被"测试通过"伪装成已交付。
    assert schema["retention_events_days"]["pending_stage"] == "S6"


# --- 二：布局 ---------------------------------------------------------------


def test_layouts_are_only_listed_and_switched_within_their_own_site(rig):
    for which in ("A", "B"):
        rows = rig.client.get("/api/layouts", params={"site": rig.sites[which]["site_id"]}).json()
        assert [row["name"] for row in rows] == [f"{which}台"]
        assert all(row["site_id"] == rig.sites[which]["site_id"] for row in rows)

    # 没带 `?site=` 就是"把两个场地的布局混在一起"：接口不给这个口子。
    assert rig.client.get("/api/layouts").status_code == 422


def test_switching_the_active_layout_in_a_leaves_bs_active_alone(rig):
    other = rig.client.post(
        "/api/layouts",
        json={"site_id": rig.sites["A"]["site_id"], "name": "A场 决赛台"},
    ).json()
    rig.client.post(f"/api/layouts/{other['layout_id']}/activate")

    a_rows = rig.client.get("/api/layouts", params={"site": rig.sites["A"]["site_id"]}).json()
    assert sorted(row["name"] for row in a_rows if row["is_active"]) == ["A场 决赛台"]
    b_rows = rig.client.get("/api/layouts", params={"site": rig.sites["B"]["site_id"]}).json()
    assert sorted(row["name"] for row in b_rows if row["is_active"]) == ["B台"]


def test_a_layout_cannot_bind_a_relay_that_belongs_to_another_site(rig):
    stolen = rig.client.patch(
        f"/api/layouts/{rig.layouts['A']}", json={"relay_id": rig.relays["B"][0]}
    )
    assert stolen.status_code == 409
    assert stolen.json()["detail"] == {
        "reason": "relay_foreign_site",
        "relay_id": rig.relays["B"][0],
        "site_id": rig.sites["A"]["site_id"],
    }
    # 拒绝之后原来的绑定一个字没变。
    assert rig.client.get(f"/api/layouts/{rig.layouts['A']}").json()["relay_id"] is None


def test_creating_a_layout_with_the_same_name_in_two_sites_is_not_a_conflict(rig):
    """场地内唯一才是规则；"两个场地都想叫默认工作台"是常态，不是撞名。"""
    for which in ("A", "B"):
        row = rig.client.post(
            "/api/layouts", json={"site_id": rig.sites[which]["site_id"], "name": "默认工作台"}
        )
        assert row.status_code == 201, row.json()
    same_name = rig.client.post(
        "/api/layouts", json={"site_id": rig.sites["A"]["site_id"], "name": "默认工作台"}
    )
    assert same_name.status_code == 409
    assert same_name.json()["detail"]["reason"] == "duplicate_layout_name"


# --- 三：组件配置 -----------------------------------------------------------


def test_two_sites_can_each_have_a_tile_named_a_with_different_config(rig):
    put_tile(rig, "A", "A", {"threshold_db": -95})
    put_tile(rig, "B", "A", {"threshold_db": -70})

    assert rig.tiles("A")["A"]["effective"]["threshold_db"]["value"] == -95
    assert rig.tiles("B")["A"]["effective"]["threshold_db"]["value"] == -70
    # 各自的格子追得回自己的场地：串场在这一步就断言掉。
    for which in ("A", "B"):
        layout = rig.client.get(f"/api/layouts/{rig.layouts[which]}").json()
        assert layout["site_id"] == rig.sites[which]["site_id"]


def test_a_layout_level_default_does_not_leak_into_the_other_sites_tiles(rig):
    put_tile(rig, "A", "A", {"threshold_db": -95})
    rig.client.patch(f"/api/layouts/{rig.layouts['A']}", json={"defaults": {"threshold_db": -100}})

    # A 的布局默认压不过 A 自己的格子配置（层级优先，与场地无关）。
    assert rig.tiles("A")["A"]["effective"]["threshold_db"]["value"] == -95
    assert rig.settings("A")["threshold_db"]["value"] == -85, "布局层不得写回场地层"
    # B 的格子只继承"默认 + 全局 + B 场地"：A 的布局层根本不在它的链上生效。
    put_tile(rig, "B", "B2", {})
    tile = rig.tiles("B")["B2"]
    assert tile["effective"]["threshold_db"]["value"] == -85
    assert [c.get("value") for c in tile["effective"]["threshold_db"]["chain"]] == [
        -85,
        None,
        None,
        None,
        None,
    ], "B 的布局层没有 -100 这个覆写"
    assert rig.client.get(f"/api/layouts/{rig.layouts['B']}").json()["defaults"] is None


# --- 四：节点范围 -----------------------------------------------------------


def add_node(rig: Rig, gateway_id: int, addr: str, name: str) -> None:
    """节点行由拨号循环写（S3），这里直接摆数据：这条测的是**读侧的场地过滤**。"""
    with rig.client.app.state.db.scope() as session:
        session.add(
            Node(
                gateway_id=gateway_id,
                display_name=name,
                transport_kind="usb-native",
                transport_addr=addr,
                status="online",
            )
        )


def test_nodes_are_only_listed_under_their_own_site(rig):
    add_node(rig, rig.gateways["A"][0], "/dev/ttyA0", "A 的板")
    add_node(rig, rig.gateways["B"][0], "/dev/ttyB0", "B 的板")

    def names(site: str) -> list[str]:
        row = rig.client.get("/api/nodes", params={"site": rig.sites[site]["site_id"]})
        assert row.status_code == 200, row.json()
        return sorted(node["display_name"] for node in row.json())

    assert names("A") == ["A 的板"]
    assert names("B") == ["B 的板"]
    # 按接入机筛同样不许跨场地：`?gateway=` 指到 B 的机器就只回 B 的那块板。
    row = rig.client.get("/api/nodes", params={"gateway": rig.gateways["B"][0]})
    assert [node["display_name"] for node in row.json()] == ["B 的板"]


# --- 五、六：还没落地的两处，显式挂着 ---------------------------------------


@pytest.mark.skip(reason="事件表属 S6（方案 §3 表结构与 §7 S6 行）：今天没有可查的表")
def test_events_written_under_a_are_never_returned_for_b():
    """判据：`GET /api/events?site=` 必须按场地过滤。表一落地就得把这条补上。"""


@pytest.mark.skip(reason="告警表与规则引擎属 S6/S8")
def test_alerts_and_their_state_do_not_cross_sites():
    """判据：A 场地的告警不得出现在 B 的列表或计数里。"""


@pytest.mark.skip(reason="按场地的 WS 订阅扇出属 S5/S7；今天只有 (gateway, ep) 维度")
def test_a_ws_subscription_for_one_site_receives_no_frames_from_the_other():
    """判据：`/ws/live?site=` 落地后，A 的连接收不到 B 的帧。

    今天的 `/ws/bridge/{ep}` 是"某一块物理板"的透传，天然按接入机划分，跨场地读同一条
    桥本来就不可能——但那是实现的巧合，不是这条判据的证明，所以不当已验。
    """
