"""双场地下的"A 不影响 B"逐处判定（S4 门槛判据第二条）。

夹具按定案的拓扑摆最小心机：**两个场地，每个场地两个中继位置、每位置一台接入机、
每场地一份激活布局 + 一个同名格子**。每一条判定都得先有能对拍的对象：单场地验不出跨场地
串场，多场地但没有同名格子又验不出"实例 id 只在布局内唯一"——所以这两件事都得在夹具里就位。

清单里今天有实现的是六处：设置、布局、组件配置、节点范围、事件读取（`GET /api/events?site=`）、
按场地的 WS 分桶（`/ws/live?site=`）。后两处 S4 时挂着 skip，S6 落了表就补上。只剩告警**显式
skip**：规则引擎属 S8，而表还不存在时"读回空列表"永远成立——那是最坏的假绿，不能拿它冒充
隔离成功。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from app.models import Node, SignalEvent
from app.services.live import SNAPSHOT

from test_ws_bridge import Page, until

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
    # S6 之后这两个键真的有人执行：`retention.purge_site` 读的就是这里的生效值，DELETE 带
    # `site_id`（`tests/test_retention.py` 钉住"A 收紧不清 B"与"天数来自层级链"）。
    # 所以这里改钉**标志位已清**——留着 "S6" 就等于页面继续显示"执行者还没上线"。
    assert schema["retention_events_days"]["pending_stage"] is None
    assert schema["retention_summary_days"]["pending_stage"] is None
    # 反向的一半同样要说：桶宽与 RAW IQ 落库今天仍只有生效值，不许被邻近的绿测试带着清掉。
    assert schema["summary_bucket_s"]["pending_stage"] == "S9"
    assert schema["rawiq_persist"]["pending_stage"] == "S9"


# --- 二：布局 ---------------------------------------------------------------


def test_layouts_are_only_listed_and_switched_within_their_own_site(rig):
    for which in ("A", "B"):
        rows = rig.client.get("/api/layouts", params={"site": rig.sites[which]["site_id"]}).json()
        assert [row["name"] for row in rows] == [f"{which}台"]
        assert all(row["site_id"] == rig.sites[which]["site_id"] for row in rows)

    # 没带 `?site=` 就是"把两个场地的布局混在一起"：接口不给这个口子。
    response = rig.client.get("/api/layouts")
    assert response.status_code == 422
    # S5 收口：形状必须是全站那一套 `{"reason": …}`。FastAPI 默认的校验数组在这里
    # 一样能拦住缺参，但页面就得为这一个端点写特例——那正是它出错的开始。
    assert response.json() == {
        "detail": {
            "reason": "site_required",
            "param": "site",
            "hint": "缺少查询参数 ?site=：布局按场地隔离，不跨场地列",
        }
    }


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


def test_switching_back_to_an_older_layout_clears_the_active_one_first(rig):
    """切回"id 比当前激活项小"的那一份，也必须切得动。

    上一条评论的切换走的是反方向（新建的副本 id 更大），所以它一直是绿的：清旧与置新在
    同一次 flush 里，两条 ORM UPDATE 的先后**由主键决定**，而 SQLite 的部分唯一索引
    `uq_layout_active_site` 按语句即时校验。主键大的那条先被置 False、主键小的那条后置 True
    才碰巧没事；反过来就先撞成 `IntegrityError`，再被翻成 409 `active_layout_conflict`。
    真实页面天天在撞它：另存为之后切回原布局（S5 冒烟实测 409）。
    """
    first_id = rig.layouts["A"]  # 建场时就激活的那一份，id 最小
    newer = rig.client.post(
        "/api/layouts", json={"site_id": rig.sites["A"]["site_id"], "name": "A场 后建的"}
    ).json()
    assert rig.client.post(f"/api/layouts/{newer['layout_id']}/activate").status_code == 200

    back = rig.client.post(f"/api/layouts/{first_id}/activate")
    assert back.status_code == 200, back.json()
    assert back.json()["is_active"] is True

    a_rows = rig.client.get("/api/layouts", params={"site": rig.sites["A"]["site_id"]}).json()
    assert [row["name"] for row in a_rows if row["is_active"]] == ["A台"]
    assert next(row for row in a_rows if row["name"] == "A场 后建的")["is_active"] is False


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


# --- 五、六：S6 落表之后各钉一次 ---------------------------------------------


def add_event(rig: Rig, which: str, *, source: str = "real") -> int:
    """摆一条观测事实：写侧归采集层，这里只测读侧的场地过滤。"""
    with rig.client.app.state.db.scope() as session:
        row = SignalEvent(
            site_id=rig.sites[which]["site_id"],
            gateway_id=rig.gateways[which][0],
            kind="node_offline",
            source=source,
            payload_json=json.dumps({"note": f"{which} 的板安静了"}),
        )
        session.add(row)
        session.flush()
        return row.event_id


def test_events_written_under_a_are_never_returned_for_b(rig):
    """判据：`GET /api/events?site=` 必须按场地过滤。S4 挂这条 skip 时表还不存在，S6 落地就补上。"""
    mine = add_event(rig, "A")
    theirs = add_event(rig, "B", source="mock")

    def ids(which: str) -> list[int]:
        row = rig.client.get("/api/events", params={"site": rig.sites[which]["site_id"]})
        assert row.status_code == 200, row.json()
        return [item["event_id"] for item in row.json()["rows"]]

    assert ids("A") == [mine]
    assert ids("B") == [theirs]
    # 反向也不许通：拿着 B 的 id 到 A 的场地下读，应当像它不存在。
    probe = rig.client.get(f"/api/events/{theirs}", params={"site": rig.sites["A"]["site_id"]})
    assert probe.status_code == 404


def live_event(site_id: int, event_id: str) -> dict:
    """一帧与 `capture._publish_event` 同构的事件：外层是帧类型，记录在 `event` 里。"""
    return {
        "kind": "event",
        "site_id": site_id,
        "ts": "2026-10-29T00:00:00+00:00",
        "event": {"event_id": event_id, "site_id": site_id, "kind": "node_offline"},
    }


def test_a_ws_subscription_for_one_site_receives_no_frames_from_the_other(rig):
    """判据：A 场地的 `/ws/live` 订阅收不到 B 场地的事件。

    S4 当时挂 skip 的理由站得住：`/ws/bridge/{ep}` 是按**接入机**划分的透传，跨场地读同一条桥
    本来就不可能，但那是实现的巧合，不是这条判据的证明。`/ws/live?site=` 是按场地分桶的那一口，
    所以这里正反两个方向各钉一次，并且钉的是"对面一个字都没有"，不是"对面只收到自己的"。
    """
    hub = rig.client.app.state.live
    main, away = rig.sites["A"]["site_id"], rig.sites["B"]["site_id"]

    async def events_only(page: Page) -> list[str]:
        """页面已经排出、还没读走的那几条里的事件 id。"""
        out = []
        while not page._from_app.empty():
            message = json.loads((await page.receive())["text"])
            assert message["kind"] == "event", message
            out.append(message["event"]["event_id"])
        return out

    async def take_snapshot(page: Page) -> None:
        """订阅建立时这层会先回一帧快照：先吃掉它，后面的判定才只关于事件。"""
        message = await page.receive()
        assert message["type"] == "websocket.send", message
        assert json.loads(message["text"])["kind"] == SNAPSHOT

    async def scenario():
        async with Page(rig.client.app, f"/ws/live?site={main}") as home, Page(
            rig.client.app, f"/ws/live?site={away}"
        ) as guest:
            await home.wait_accepted()
            await guest.wait_accepted()
            await take_snapshot(home)
            await take_snapshot(guest)

            hub.publish(main, live_event(main, "mine"))
            assert await until(lambda: home._from_app.qsize() == 1)
            assert guest._from_app.empty(), "主场的事件漏进了客场的桶"

            hub.publish(away, live_event(away, "theirs"))
            assert await until(lambda: guest._from_app.qsize() == 1)

            assert await events_only(home) == ["mine"]
            assert await events_only(guest) == ["theirs"]

    asyncio.run(scenario())


def test_alerts_and_their_state_do_not_cross_sites(client):
    """判据：A 场地的告警不得出现在 B 的列表或计数里。（S8-2 解封：规则引擎已在）

    复用 `test_alerts.py` 的摆位工具：A 场地专属规则 + A/B 各一条判定，引擎跑过之后
    两边的列表必须各自只有自己的告警；A 的处置（ack）也不得改写 B 的任何一行。"""
    from test_alerts import add_finding, make_rule

    site = client.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
    relay = client.post("/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}).json()
    gateway = client.post(
        "/api/gateways",
        json={"relay_id": relay["relay_id"], "name": "pit", "kind": "usb-bridge"},
    ).json()
    away = client.post("/api/sites", json={"name": "客场", "slug": "away"}).json()
    make_rule(client, site_id=site["site_id"], scope_id=site["site_id"])
    add_finding(client, site_id=site["site_id"], gateway_id=gateway["gateway_id"])
    add_finding(client, site_id=away["site_id"], gateway_id=gateway["gateway_id"])

    from app.services.alerts import AlertEngine

    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()

    # 摆位时刻固定在 BASE：列表默认窗口是"最近一段"，显式把窗口框过去。
    window = {"from": "2026-10-30T11:00:00+00:00", "to": "2026-10-30T13:00:00+00:00"}
    home = client.get("/api/alerts", params={"site": site["site_id"], **window}).json()["rows"]
    theirs = client.get(
        "/api/alerts", params={"site": away["site_id"], **window}
    ).json()["rows"]
    assert [r["site_id"] for r in home] == [site["site_id"]], "客场判定不该在主场的规则下响"
    assert theirs == [], "A 场地的规则在 B 的列表里什么都不能有"

    home_id = home[0]["alert_id"]
    assert client.post(f"/api/alerts/{home_id}/ack").json()["status"] == "ack"
    assert (
        client.get("/api/alerts", params={"site": away["site_id"], **window}).json()["rows"]
        == []
    ), (
        "主场处置自己的告警之后，客场的计数仍然必须一动不动"
    )
