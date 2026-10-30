"""S8-2 告警规则引擎：判定按规则落告警候选、去重两方向、场地隔离、规则 CRUD 与处置状态机。

判据来自契约 §4 与方案 §7 S8 行，不是这里发明的：

- 规则锁到场地，A 场地阈值不得惊动 B 场地（场地隔离的告警版，S4 已在观测/布局维立过同一条）。
- 去重键唯一落在部分唯一索引上，两个方向各一条：同键第二条进不去（引擎合并），
  resolved 之后同键能再开。
- `alert_defaults` 的两个真实读者：建规则缺省去重窗口、silence 缺省时长——都从场地的
  生效值落，没有配置层不掺和的代码兜底。
- 处置状态机从严：ack 只认 open，silence 认 open/ack，resolve 是唯一终点；乱序点击 422。
"""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from types import SimpleNamespace

from app.config import Settings
from app.main import create_app
from app.models import Alert, AlertRule, Node, PluginFinding, to_utc
from app.services.alerts import AlertEngine

BASE = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def seeded(client):
    """每个用例先铺一层真层级：site 1 + relay + 两台接入机。

    `plugin_finding.gateway_id` / `node_id` 都是带 RESTRICT 的外键（database.py 开着
    `PRAGMA foreign_keys=ON`），判定行里的身份必须是库里真实存在的，否则测的是
    IntegrityError 不是规则引擎。gw1/gw2 的主键按创建顺序是 1/2。
    """
    site = client.post("/api/sites", json={"name": "主场", "slug": "home"}).json()
    assert site["site_id"] == 1, site.text
    relay = client.post("/api/relays", json={"site_id": 1, "name": "起点塔"}).json()
    assert relay["relay_id"] == 1, relay.text
    gw1 = client.post(
        "/api/gateways",
        json={"relay_id": 1, "name": "pit-a", "kind": "usb-bridge"},
    ).json()
    gw2 = client.post(
        "/api/gateways",
        json={"relay_id": 1, "name": "pit-b", "kind": "usb-bridge"},
    ).json()
    # 判定行常引用 node 1：node_id 也是 RESTRICT 外键，没有这一行 FK 先炸。
    with client.app.state.db.scope() as session:
        session.add(
            Node(
                gateway_id=gw1["gateway_id"],
                display_name="n1",
                transport_kind="usb-native",
                transport_addr="FT:01",
            )
        )
        session.commit()
    return SimpleNamespace(
        site_id=1, relay_id=relay["relay_id"], gw1=gw1["gateway_id"], gw2=gw2["gateway_id"]
    )


def add_finding(
    client,
    *,
    site_id=1,
    gateway_id=None,
    node_id=None,
    slot=None,
    plugin_id="anomaly-detector",
    kind="anomaly_rise",
    offset_s=0.0,
    value=-40.0,
    threshold=-85.0,
    source="real",
    evidence=None,
) -> int:
    """直接写一条判定：引擎的输入侧不是本阶段的被测物（S7 已测），这里摆数据用。"""
    with client.app.state.db.scope() as session:
        finding = PluginFinding(
            ts=BASE + timedelta(seconds=offset_s),
            site_id=site_id,
            gateway_id=gateway_id,
            node_id=node_id,
            slot=slot,
            plugin_id=plugin_id,
            producer=f"{plugin_id}@1.0.0",
            kind=kind,
            metric="rssi" if value is not None else None,
            value=value,
            threshold=threshold,
            source=source,
            evidence_json=evidence,
        )
        session.add(finding)
        session.commit()
        return finding.finding_id


def add_alert(client, **kw) -> int:
    """直接摆一条告警（处置端点的测试不走引擎）。"""
    defaults = dict(
        rule_id=1,
        site_id=1,
        kind="anomaly_rise",
        ts=BASE,
        last_ts=BASE,
        status="open",
        source="real",
        dedup_key="r1:g1:n1:s0",
        evidence_json='{"nodes": [{"node_id": 1, "peak_dbfs": -40}]}',
    )
    defaults.update(kw)
    with client.app.state.db.scope() as session:
        # alert.rule_id 是 RESTRICT 外键：没有规则行就先摆一条（处置端点的测试不走引擎）。
        if session.get(AlertRule, defaults["rule_id"]) is None:
            session.add(
                AlertRule(
                    name="摆位规则",
                    plugin_id="anomaly-detector",
                    kind=defaults["kind"],
                    scope="global",
                )
            )
            session.commit()
        alert = Alert(**defaults)
        session.add(alert)
        session.commit()
        return alert.alert_id


def make_rule(client, **kw):
    body = {
        "name": "干扰抬升",
        "plugin_id": "anomaly-detector",
        "kind": "anomaly_rise",
        "scope": "site",
        "scope_id": 1,
        **kw,
    }
    if "site_id" not in body and body["scope"] == "site":
        body["site_id"] = body["scope_id"]
    response = client.post("/api/alert-rules", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def make_site(client, slug, name):
    response = client.post("/api/sites", json={"name": name, "slug": slug})
    assert response.status_code == 201, response.text
    return response.json()


def put_site_defaults(client, site_id: int, defaults: dict) -> None:
    row = client.put(
        f"/api/settings/site/{site_id}", json={"settings": {"alert_defaults": defaults}}
    )
    assert row.status_code == 200, row.text


def rows_of(client, model):
    with client.app.state.db.scope() as session:
        return list(session.execute(select(model)).scalars().all())


# --- 引擎：判定 → 告警 -------------------------------------------------------


def test_cold_start_does_not_replay_history(client):
    """引擎冷启动跳到当前最大 finding_id：重启不重放旧判定（申报过的边界）。"""
    add_finding(client, site_id=1)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    report = engine.run_once()
    assert (report.created, report.merged, report.resolved) == (0, 0, 0)
    assert engine.status()["last_tick"]["skipped"] == "cold_start"
    assert rows_of(client, Alert) == []


def test_finding_becomes_alert_with_copied_evidence(client):
    add_finding(
        client,
        site_id=1,
        gateway_id=1,
        node_id=1,
        slot=0,
        value=-40.0,
        threshold=-85.0,
        evidence='{"nodes": [{"node_id": 1, "peak_dbfs": -40}]}',
    )
    make_rule(client)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0  # 测试里跳过冷启动：判据是"摆下的判定要被处理"
    engine.run_once()

    alerts = rows_of(client, Alert)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.count == 1
    assert alert.status == "open"
    assert alert.source == "real"
    assert alert.value == -40.0
    assert alert.threshold == -85.0
    # SQLite 的 DATETIME 不存偏移（to_utc 的 docstring）：读回来是 naive，按 UTC 贴回再比。
    assert to_utc(alert.ts) == BASE and to_utc(alert.last_ts) == BASE
    # 证据是**拷贝**：判定行将来被保留任务清掉，告警上的证据必须还活着。
    import json as _json

    assert _json.loads(alert.evidence_json) == {"nodes": [{"node_id": 1, "peak_dbfs": -40}]}
    assert engine.status()["created"] == 1


def test_merge_within_window_updates_count_value_and_last_ts(client):
    make_rule(client, dedup_window_s=300)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0, value=-40.0)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=60, value=-35.0)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    report = engine.run_once()

    assert (report.created, report.merged, report.resolved) == (1, 1, 0)
    alerts = rows_of(client, Alert)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.count == 2
    assert alert.value == -35.0, "合并后这一行说的是最新一次命中"
    assert to_utc(alert.last_ts) == BASE + timedelta(seconds=60)
    assert to_utc(alert.ts) == BASE, "首次命中时刻不动：页面上的'响了多久'靠它"


def test_window_elapsed_resolves_old_and_opens_new(client):
    """窗口过了：旧行自动 resolved，新行 open、count 从 1 重新数（R23 方向一的引擎侧）。"""
    make_rule(client, dedup_window_s=10)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=3600)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    report = engine.run_once()

    assert (report.created, report.merged, report.resolved) == (2, 0, 1)
    alerts = sorted(rows_of(client, Alert), key=lambda a: to_utc(a.ts))
    assert [a.status for a in alerts] == ["resolved", "open"]
    assert [a.count for a in alerts] == [1, 1]


def test_null_window_merges_forever(client):
    make_rule(client, dedup_window_s=None)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=86400)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    alerts = rows_of(client, Alert)
    assert len(alerts) == 1 and alerts[0].count == 2 and alerts[0].status == "open"


def test_dedup_key_unique_index_both_directions(client, db_path):
    """R23 的数据库侧钉子：同键第二条被索引拒，resolved 后同键能再开。"""
    make_rule(client)
    add_alert(client, dedup_key="r1:g1:n1:s0")
    with sqlite3.connect(db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO alert (rule_id, site_id, kind, ts, last_ts, count, status,"
                " source, dedup_key) VALUES (1, 1, 'anomaly_rise', '2026-10-30 12:00:00',"
                " '2026-10-30 12:00:00', 1, 'open', 'real', 'r1:g1:n1:s0')"
            )
        conn.commit()
        conn.execute(
            "UPDATE alert SET status = 'resolved' WHERE dedup_key = 'r1:g1:n1:s0'"
        )
        conn.execute(
            "INSERT INTO alert (rule_id, site_id, kind, ts, last_ts, count, status,"
            " source, dedup_key) VALUES (1, 1, 'anomaly_rise', '2026-10-30 13:00:00',"
            " '2026-10-30 13:00:00', 1, 'open', 'real', 'r1:g1:n1:s0')"
        )
        conn.commit()
        rows = conn.execute("SELECT status FROM alert ORDER BY ts").fetchall()
        assert rows == [("resolved",), ("open",)]


def test_rule_scope_and_site_isolation(client):
    """A 场地的规则不惊动 B：场地专属规则只吃自己场地的判定；实例级规则两边都管。"""
    make_site(client, "away", "客场")
    make_rule(client, site_id=1)  # 场地专属
    add_finding(client, site_id=2, gateway_id=1)
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    assert rows_of(client, Alert) == [], "B 场地的判定撞在 A 场地的规则上不该响"

    add_finding(client, site_id=1, gateway_id=1)
    engine.run_once()
    assert [a.site_id for a in rows_of(client, Alert)] == [1]

    # 实例级规则（site_id=None + global 作用域）：对所有场地生效。
    make_rule(client, site_id=None, scope="global", scope_id=None, name="全局干扰")
    add_finding(client, site_id=2, gateway_id=1)
    engine.run_once()
    assert sorted(a.site_id for a in rows_of(client, Alert)) == [1, 2]


def test_scope_gateway_matches_by_finding_identity(client, seeded):
    """作用域判据读的是判定行自带的身份列，不 join：gateway 看那一列。"""
    add_finding(client, site_id=1, gateway_id=seeded.gw2)
    add_finding(client, site_id=1, gateway_id=seeded.gw1)
    make_rule(client, scope="gateway", scope_id=seeded.gw1, site_id=None, name="gw1")
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    assert [a.gateway_id for a in rows_of(client, Alert)] == [seeded.gw1]


def test_scope_node_matches_by_finding_identity(client, seeded):
    """node 作用域只吃带 node 身份的判定：接入机级判定（node=None）不指认到节点。"""
    add_finding(client, site_id=1, gateway_id=seeded.gw1, node_id=1)
    add_finding(client, site_id=1, gateway_id=seeded.gw1)
    make_rule(client, scope="node", scope_id=1, site_id=None, name="node1")
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    assert [a.node_id for a in rows_of(client, Alert)] == [1]


def test_scope_global_matches_everything(client, seeded):
    add_finding(client, site_id=1, gateway_id=seeded.gw2)
    make_rule(client, scope="global", scope_id=None, site_id=None, name="global")
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    assert len(rows_of(client, Alert)) == 1


def test_disabled_rule_and_mismatched_kind_stay_silent(client):
    make_rule(client)
    with client.app.state.db.scope() as session:
        rule = session.get(AlertRule, 1)
        rule.enabled = False
        session.commit()
    add_finding(client, site_id=1, kind="anomaly_rise")
    add_finding(client, site_id=1, kind="threshold_crossing")
    engine = AlertEngine(client.app.state.db, client.app.state.settings)
    engine.cursor = 0
    engine.run_once()
    assert rows_of(client, Alert) == [], "停用的规则与对不上 kind 的判定都不该响"


def test_engine_lifecycle_and_status(client):
    from dataclasses import replace

    # conftest 把引擎默认关了（HTTP 测试不跑后台），这里换一把开了开关的钥匙直接测循环。
    live_settings = replace(client.app.state.settings, alert_engine_enabled=True)
    engine = AlertEngine(client.app.state.db, live_settings)
    assert engine.enabled

    async def scenario():
        await engine.start()
        assert engine._task is not None and not engine._task.done()
        await engine.stop()
        assert engine._task is None

    asyncio.run(scenario())
    status = engine.status()
    assert status["problem"] is None and status["errors"] == 0

    closed = AlertEngine(client.app.state.db, client.app.state.settings)
    asyncio.run(closed.start())
    assert closed._task is None, "关掉的引擎不起循环，健康检查里 enabled=false 不算故障"


# --- 规则 CRUD ---------------------------------------------------------------


def test_create_rule_defaults_window_from_site_alert_defaults(client):
    """alert_defaults 的第一个真实读者：请求没带窗口，落场地生效值。"""
    put_site_defaults(client, 1, {"dedup_window_s": 120})
    rule = make_rule(client)
    assert rule["dedup_window_s"] == 120

    rule = make_rule(client, name="自带窗口", dedup_window_s=30)
    assert rule["dedup_window_s"] == 30, "请求里带了就用请求的"

    rule = make_rule(client, name="实例级", site_id=None)
    assert rule["dedup_window_s"] is None, "实例级规则没有'它的场地'，直接落 NULL"

    # 没配 alert_defaults 的场地：NULL（永不新开），不是藏在代码里的某个数。
    away = make_site(client, "away", "客场")
    rule = make_rule(client, name="客场规则", scope_id=away["site_id"], site_id=away["site_id"])
    assert rule["dedup_window_s"] is None


def test_create_rule_validation_reasons(client):
    cases = [
        ({"scope": "relay", "scope_id": 1}, "scope_unsupported"),
        ({"scope": "universe", "scope_id": 1}, "unknown_scope"),
        ({"scope": "site", "scope_id": None}, "scope_id_required"),
        ({"scope": "global", "scope_id": 1, "site_id": None}, "scope_id_redundant"),
        ({"scope": "gateway", "scope_id": 999, "site_id": None}, "unknown_scope_id"),
        ({"scope": "node", "scope_id": 999, "site_id": None}, "unknown_scope_id"),
        ({"scope": "site", "scope_id": 999}, "unknown_scope_id"),
        ({"plugin_id": "no-such-plugin"}, "unknown_plugin"),
        ({"dedup_window_s": 0}, "out_of_range"),
        ({"name": ""}, "out_of_range"),
        ({"site_id": 999}, "unknown_site"),
        ({"scope": "site", "scope_id": 1, "site_id": 999}, "unknown_site"),
    ]
    for overrides, reason in cases:
        body = {"name": "干扰抬升", "plugin_id": "anomaly-detector", "kind": "anomaly_rise",
                "scope": "site", "scope_id": 1, "site_id": 1, **overrides}
        row = client.post("/api/alert-rules", json=body)
        assert row.status_code == 422, (overrides, row.status_code, row.text)
        assert row.json()["detail"]["reason"] == reason, overrides
    assert rows_of(client, AlertRule) == [], "校验拒绝的规则一行都不许落"


def test_site_scoped_rule_cannot_lock_another_site(client):
    """契约原话的否定面：A 场地专属规则锁 B 场地 = 422 scope_mismatch。"""
    away = make_site(client, "away", "客场")
    row = client.post(
        "/api/alert-rules",
        json={"name": "错位规则", "plugin_id": "anomaly-detector", "kind": "anomaly_rise",
              "scope": "site", "scope_id": 1, "site_id": away["site_id"]},
    )
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "scope_mismatch"


def test_aggregator_cannot_write_rules_or_dispositions(db_path):
    writer = TestClient(create_app(Settings(db_path=db_path, dialer_enabled=False)))
    # autouse 的 seeded 夹具已经在同一份库上建过 slug "home"：这里的写侧 app 换个 slug。
    site = make_site(writer, "agg", "主场")
    make_rule(writer, site_id=site["site_id"], scope_id=site["site_id"])
    alert_id = add_alert(writer, site_id=site["site_id"])

    reader = TestClient(
        create_app(Settings(db_path=db_path, dialer_enabled=False, instance_role="aggregator"))
    )
    assert reader.get("/api/alert-rules").status_code == 200
    assert reader.get(f"/api/alerts/{alert_id}").status_code == 200
    for call in (
        lambda: reader.post("/api/alert-rules", json={"name": "x", "plugin_id": "anomaly-detector",
                                                      "kind": "anomaly_rise", "scope": "global"}),
        lambda: reader.post(f"/api/alerts/{alert_id}/ack"),
        lambda: reader.post(f"/api/alerts/{alert_id}/silence", json={"seconds": 60}),
        lambda: reader.post(f"/api/alerts/{alert_id}/resolve", json={"note": "看了"}),
    ):
        row = call()
        assert row.status_code == 405, (row.status_code, row.text)
        assert row.json()["detail"]["reason"] == "aggregator_read_only"


def test_update_rule_revalidates(client):
    make_rule(client)
    row = client.patch("/api/alert-rules/1", json={"name": "改名", "enabled": False})
    assert row.status_code == 200
    assert row.json()["name"] == "改名" and row.json()["enabled"] is False

    row = client.patch("/api/alert-rules/1", json={"scope": "relay"})
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "scope_unsupported"
    assert client.get("/api/alert-rules").json()["rows"][0]["scope"] == "site", "拒绝的改动不许落"

    row = client.patch("/api/alert-rules/999", json={"name": "x"})
    assert row.status_code == 404


def test_delete_rule_blocks_while_alerts_exist(client):
    """告警行 RESTRICT 挂在规则上：只要还挂着告警（哪怕已解决），规则就删不动。"""
    make_rule(client)
    alert_id = add_alert(client, rule_id=1, dedup_key="r1:g1:n1:s0")
    row = client.delete("/api/alert-rules/1")
    assert row.status_code == 409
    assert row.json()["detail"]["reason"] == "rule_in_use"

    client.post(f"/api/alerts/{alert_id}/resolve", json={"note": "证据不足"})
    assert client.delete("/api/alert-rules/1").status_code == 409, (
        "解决了也不放行：'为什么响'的账本不许跟着规则一起消失"
    )

    make_rule(client, name="干净规则", scope="global", scope_id=None, site_id=None)
    assert client.delete("/api/alert-rules/2").status_code == 204
    assert [r["rule_id"] for r in client.get("/api/alert-rules").json()["rows"]] == [1]


# --- 告警读侧与处置 ----------------------------------------------------------


def test_list_alerts_site_scoped(client):
    away = make_site(client, "away", "客场")
    add_alert(client, site_id=1, dedup_key="r1:g1:n1:s0")
    add_alert(client, site_id=away["site_id"], dedup_key="r1:g1:n1:s1")
    # 摆位时刻是固定的 BASE：列表默认窗口是"最近一段"，这里显式把窗口框到它身上。
    window = {
        "from": "2026-10-30T11:00:00+00:00",
        "to": "2026-10-30T13:00:00+00:00",
    }

    home = client.get("/api/alerts", params={"site": 1, **window}).json()
    assert [r["site_id"] for r in home["rows"]] == [1]

    theirs = client.get("/api/alerts", params={"site": away["site_id"], **window}).json()
    assert [r["site_id"] for r in theirs["rows"]] == [away["site_id"]]

    # 两个场地、没设当前场地：缺省没有"该是谁"的事实，422 说要 ?site=。
    row = client.get("/api/alerts")
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "site_required"


def test_alert_detail_evidence_and_limitations(client):
    alert_id = add_alert(client, source="mock")
    row = client.get(f"/api/alerts/{alert_id}")
    assert row.status_code == 200
    body = row.json()
    assert body["evidence"]["value"] == {"nodes": [{"node_id": 1, "peak_dbfs": -40}]}
    assert body["evidence"]["error"] is None
    assert body["review"] == {"value": None, "error": None}
    notes = "\n".join(body["limitations"])
    assert "mock" in notes, "mock 来路的告警必须自己说出不是实时"
    assert "复核" in notes
    assert "距离" in notes

    row = client.get(f"/api/alerts/{alert_id}", params={"site": 2})
    assert row.status_code == 404, "带了别的场地的 ?site= 就当这条不存在"
    assert client.get("/api/alerts/999").status_code == 404


def test_list_filters_status_rule_and_unknown_node(client):
    add_alert(client, dedup_key="r1:g1:n1:s0")
    acked = add_alert(client, dedup_key="r1:g1:n2:s0")
    client.post(f"/api/alerts/{acked}/ack")
    window = {"from": "2026-10-30T11:00:00+00:00", "to": "2026-10-30T13:00:00+00:00"}

    rows = client.get("/api/alerts", params={"site": 1, "status": "open", **window}).json()["rows"]
    assert len(rows) == 1
    rows = client.get("/api/alerts", params={"site": 1, "status": "ack", **window}).json()["rows"]
    assert len(rows) == 1
    assert client.get(
        "/api/alerts", params={"site": 1, "status": "bogus"}
    ).status_code == 422
    assert client.get("/api/alerts", params={"site": 1, "node": 99}).status_code == 404
    assert client.get(
        "/api/alerts", params={"site": 1, "rule": 7, **window}
    ).json()["rows"] == []


def test_ack_transition_is_strict(client):
    alert_id = add_alert(client)
    assert client.post(f"/api/alerts/{alert_id}/ack").json()["status"] == "ack"
    row = client.post(f"/api/alerts/{alert_id}/ack")
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "wrong_status"
    assert client.post(f"/api/alerts/{alert_id}/resolve", json={"note": "处理了"}).json()[
        "status"
    ] == "resolved"
    row = client.post(f"/api/alerts/{alert_id}/silence", json={"seconds": 60})
    assert row.status_code == 422, "resolved 是终点，之后谁都不许再动"


def test_silence_uses_site_default_then_explicit(client):
    """alert_defaults 的第二个真实读者：不给时长就落场地生效值，没有配置就 422。"""
    alert_id = add_alert(client)
    row = client.post(f"/api/alerts/{alert_id}/silence")
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "silence_duration_required"

    put_site_defaults(client, 1, {"silence_s": 300})
    body = client.post(f"/api/alerts/{alert_id}/silence").json()
    assert body["status"] == "silenced"
    until = datetime.fromisoformat(body["silenced_until"])
    delta = until - datetime.now(timezone.utc)
    assert 290 < delta.total_seconds() <= 300, f"缺省时长应是 300 s，实际 {delta}"

    body = client.post(f"/api/alerts/{alert_id}/resolve", json={"note": "重开"}).json()
    assert body["status"] == "resolved"
    reopened = add_alert(client, dedup_key="r1:g2:n2:s1")
    body = client.post(f"/api/alerts/{reopened}/silence", json={"seconds": 60}).json()
    delta = datetime.fromisoformat(body["silenced_until"]) - datetime.now(timezone.utc)
    assert 55 < delta.total_seconds() <= 60

    # until：带时区的未来时刻可以；naive 与过去时刻都拒。用远期时刻，别赌现在的钟点。
    client.post(f"/api/alerts/{reopened}/resolve", json={"note": "重开"})
    fresh = add_alert(client, dedup_key="r1:g3:n3:s2")
    body = client.post(
        f"/api/alerts/{fresh}/silence",
        json={"until": "2027-01-01T00:00:00+00:00"},
    ).json()
    assert body["silenced_until"] == "2027-01-01T00:00:00+00:00"
    row = client.post(
        f"/api/alerts/{fresh}/resolve", json={"note": "重开"}
    )
    fresh2 = add_alert(client, dedup_key="r1:g4:n4:s3")
    row = client.post(
        f"/api/alerts/{fresh2}/silence", json={"until": "2026-10-30T13:00:00"}
    )
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "wrong_type"
    fresh3 = add_alert(client, dedup_key="r1:g5:n5:s4")
    row = client.post(
        f"/api/alerts/{fresh3}/silence", json={"until": "2020-01-01T00:00:00+00:00"}
    )
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "out_of_range"
    row = client.post(f"/api/alerts/{fresh3}/resolve", json={"note": "清场"})
    # 时长合法性先于状态机判：负数秒数到哪儿都是 out_of_range。
    row = client.post(f"/api/alerts/{fresh3}/silence", json={"seconds": -1})
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "out_of_range"


def test_resolve_records_review_note(client):
    """§10.5 复核与推测分列：算法在 evidence，人写下的那句话在 review。"""
    alert_id = add_alert(client)
    body = client.post(f"/api/alerts/{alert_id}/resolve", json={"note": "频谱图对过了，是隔壁摄像机"}).json()
    assert body["status"] == "resolved"
    detail = client.get(f"/api/alerts/{alert_id}").json()
    assert detail["review"]["value"]["note"] == "频谱图对过了，是隔壁摄像机"
    assert detail["review"]["value"]["action"] == "resolve"
    assert detail["reviewed_at"] is not None

    body = client.post(f"/api/alerts/{add_alert(client, dedup_key='r9:g9:n9:s9')}/resolve")
    assert body.status_code == 200
    assert body.json()["reviewed_at"] is None, "不带 note 的解决只改状态，不动复核列"
    row = client.post(f"/api/alerts/999/resolve", json={"note": "x"})
    assert row.status_code == 404


# --- alert_defaults 的校验层 -------------------------------------------------


def test_alert_defaults_member_keys_are_validated(client):
    """键拼错或值不合法在校验层拦下：读者只会按'没设'处理，那种失效不能静默。

    settings 端点把逐键原因包进 `invalid_settings` 的 `errors` 清单，reason 在条目里。"""
    row = client.put(
        "/api/settings/site/1",
        json={"settings": {"alert_defaults": {"dedup_window_s": 120, "silence_s": 300}}},
    )
    assert row.status_code == 200
    for member, reason in (
        ({"bogus": 1}, "unknown_member"),
        ({"silence_s": 0}, "out_of_range"),
        ({"dedup_window_s": True}, "out_of_range"),
    ):
        row = client.put(
            "/api/settings/site/1", json={"settings": {"alert_defaults": member}}
        )
        assert row.status_code == 422, (member, row.text)
        assert row.json()["detail"]["reason"] == "invalid_settings"
        assert {"key": "alert_defaults", "reason": reason} in row.json()["detail"]["errors"]
