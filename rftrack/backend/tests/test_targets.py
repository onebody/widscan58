"""S8-3 目标聚合：判定按观测身份聚成候选目标、合并窗两向、复核端点、局限如实申报。

判据来自任务书 §7.3/§10.5，不是这里发明的：

- 置信度必须由明确算法计算（`confidence` 与 `confidence_basis` 成对出现，没有出处
  的装饰性数字正是 §10.5 禁的）。
- 不生成无来源坐标：目标两表连列都没有，"不生成"是结构不是纪律。
- 覆盖/未知项/局限：`coverage_json` 没写时详情必须说"局限未申报"，不许渲染成"无局限"。
- 复核与推测分列：算法推测在 `evidence`/`confidence`，复核人话在 `review_json`。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import CandidateTarget, TrackObservation, to_utc
from app.services.targets import MERGE_WINDOW_S, TargetEngine

from test_alerts import BASE, add_finding, seeded, make_site  # noqa: F401  夹具与摆位工具复用

WINDOW = {"from": "2026-10-30T11:00:00+00:00", "to": "2026-10-30T13:00:00+00:00"}


def run_engine(client, *, cold=False) -> TargetEngine:
    """直接构造引擎跑一轮（conftest 把后台关了；这里同步调 run_once）。

    冷启动那轮只建游标不搬数据；测试里要它干活就先置 `cursor=0`（与
    `test_site_isolation.py` 的取法一致），只有专门测冷启动的用例才传 `cold=True`。
    """
    engine = TargetEngine(client.app.state.db, client.app.state.settings)
    if not cold:
        engine.cursor = 0
    engine.run_once()
    return engine


def targets_of(client, **params):
    body = client.get("/api/targets", params={**WINDOW, **params})
    assert body.status_code == 200, body.text
    return body.json()["rows"]


# --- 引擎：归组 --------------------------------------------------------------


def test_cold_start_does_not_replay_history(client):
    """冷启动只建游标不搬历史：重启不重放，旧判定不再变成新目标（申报边界）。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0, evidence='{"who": "old"}')
    engine = run_engine(client, cold=True)
    assert engine.last_report == {"skipped": "cold_start", "findings": 0}
    assert targets_of(client, site=1) == []

    add_finding(client, gateway_id=1, node_id=1, slot=0, evidence='{"who": "new"}')
    engine.run_once()
    rows = targets_of(client, site=1)
    assert len(rows) == 1, "冷启动之后的新判定才进目标，历史那一条不得回放"


def test_finding_becomes_target_with_copied_evidence(client):
    """一条判定 = 一个目标 + 一条观测：证据是拷贝，finding_id 还在（SET NULL 之前）。"""
    add_finding(
        client, gateway_id=1, node_id=1, slot=0, evidence='{"nodes": [{"node_id": 1}]}'
    )
    engine = run_engine(client)
    assert engine.last_report["created"] == 1 and engine.last_report["joined"] == 0

    detail = client.get("/api/targets/1").json()
    assert detail["producer"] == "engine:target-associator@1"
    assert detail["review_status"] == "unreviewed" and detail["reviewed_at"] is None
    assert detail["confidence"] == 0.3, "一条判定：0.3 + 0.1×1"
    assert "当前 1" in detail["confidence_basis"]
    assert detail["coverage"]["value"]["observations"] == 1
    assert detail["coverage"]["error"] is None

    (obs,) = detail["observations"]
    assert obs["finding_id"] == 1
    assert obs["evidence"] == {"value": {"nodes": [{"node_id": 1}]}, "error": None}
    assert obs["ts"] == "2026-10-30T12:00:00+00:00"
    assert to_utc(datetime.fromisoformat(detail["first_seen_at"])) == BASE
    assert detail["last_seen_at"] == detail["first_seen_at"]


def test_findings_within_the_window_join_the_same_target(client):
    """合并窗内同身份的判定并进同一目标：观测追加、置信度按条数重算、时间轴推进。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0, value=-50.0)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=60, value=-45.0)
    run_engine(client)

    rows = targets_of(client, site=1)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.4, "两条判定：0.3 + 0.1×2"
    assert "当前 2" in rows[0]["confidence_basis"]
    detail = client.get("/api/targets/1").json()
    assert detail["coverage"]["value"]["observations"] == 2
    assert [obs["value"] for obs in detail["observations"]] == [-50.0, -45.0]
    assert detail["last_seen_at"] == "2026-10-30T12:01:00+00:00"
    assert detail["first_seen_at"] == "2026-10-30T12:00:00+00:00"


def test_findings_beyond_the_window_open_a_new_target(client):
    """超过 MERGE_WINDOW_S 的空档 = "又来了的一件事"：旧目标留在原地，新目标另开。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0)
    add_finding(
        client, gateway_id=1, node_id=1, slot=0, offset_s=MERGE_WINDOW_S + 1
    )
    engine = run_engine(client)
    assert engine.last_report["created"] == 2 and engine.last_report["joined"] == 0
    rows = targets_of(client, site=1)
    assert len(rows) == 2
    # 列表按最近出没倒序：第二个目标是"最近"的那个。
    assert rows[0]["target_id"] == 2 and rows[0]["confidence"] == 0.3
    assert rows[1]["target_id"] == 1


def test_a_gap_of_exactly_the_merge_window_still_joins(client):
    """空档**恰好等于** MERGE_WINDOW_S 算窗内：边界在闭的一侧，这条用例就是那条边的证词。

    没有它，`<=` 被改成 `<` 的变异照样绿——+1 s 与 +300 s 两条谁也不踩 `gap == 300` 这条线。
    """
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0)
    add_finding(
        client, gateway_id=1, node_id=1, slot=0, offset_s=MERGE_WINDOW_S
    )
    engine = run_engine(client)
    assert engine.last_report["created"] == 1 and engine.last_report["joined"] == 1
    rows = targets_of(client, site=1)
    assert len(rows) == 1
    assert rows[0]["confidence"] == 0.4


def test_a_different_identity_opens_its_own_target(client):
    """归组键是 (场地, 种类, 接入机, 节点, 槽位) 五元组：任一位不同就各是各的目标。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    add_finding(client, gateway_id=1, node_id=1, slot=1)
    add_finding(client, gateway_id=1, node_id=1, slot=0, kind="threshold_crossing")
    add_finding(client, gateway_id=2, node_id=1, slot=0)
    engine = run_engine(client)
    assert engine.last_report["created"] == 4
    assert all(row["confidence"] == 0.3 for row in targets_of(client, site=1))


def test_out_of_order_backfill_widens_the_timeline(client):
    """乱序回填（同一窗内先 +10s 后 0s）仍算同一件事：first_seen 取 min，last_seen 取 max。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=10)
    add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=0)
    run_engine(client)
    detail = client.get("/api/targets/1").json()
    assert detail["first_seen_at"] == "2026-10-30T12:00:00+00:00"
    assert detail["last_seen_at"] == "2026-10-30T12:00:10+00:00"
    assert len(detail["observations"]) == 2
    # 时间线按观测时刻正序：回填的 0s 排在前头，页面画轴不用再排一次。
    assert [obs["ts"] for obs in detail["observations"]] == [
        "2026-10-30T12:00:00+00:00",
        "2026-10-30T12:00:10+00:00",
    ]


def test_confidence_never_travels_without_its_basis(client):
    """§10.5 的硬边界写成判据：任何一条目标都不许有值没出处（也不许有出处没值）。"""
    for offset in (0, 30, 60, 90, 120):
        add_finding(client, gateway_id=1, node_id=1, slot=0, offset_s=offset)
    run_engine(client)
    for row in targets_of(client, site=1):
        assert (row["confidence"] is None) == (row["confidence_basis"] is None)
        assert row["confidence"] is not None, "引擎产的目标必须两个都带"


def test_mock_source_is_visible_in_observations_and_limitations(client):
    """模拟源推的判定聚成的目标，局限说明必须点出来源——不许一条推论冒充实时。"""
    add_finding(client, gateway_id=1, node_id=1, slot=0, source="mock")
    run_engine(client)
    detail = client.get("/api/targets/1").json()
    assert detail["observations"][0]["source"] == "mock"
    assert any("`mock`" in note for note in detail["limitations"])


def test_engine_lifecycle_and_status(client):
    from dataclasses import replace

    live_settings = replace(client.app.state.settings, target_engine_enabled=True)
    engine = TargetEngine(client.app.state.db, live_settings)
    assert engine.enabled

    async def scenario():
        await engine.start()
        assert engine._task is not None and not engine._task.done()
        await engine.stop()
        assert engine._task is None

    asyncio.run(scenario())
    status = engine.status()
    assert status["problem"] is None and status["errors"] == 0
    assert status["last_tick"] is None, "起了循环没跑过一拍，last_tick 就该是 None"

    closed = TargetEngine(client.app.state.db, client.app.state.settings)
    asyncio.run(closed.start())
    assert closed._task is None, "关掉的引擎不起循环，健康检查里 enabled=false 不算故障"


# --- 读侧 --------------------------------------------------------------------


def test_list_targets_is_site_scoped(client):
    """A 场地的目标不得出现在 B 的列表里；只一个场地时 ?site= 可缺省（site_or_current）。"""
    away = make_site(client, "away", "客场")
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    add_finding(client, site_id=away["site_id"], gateway_id=1, node_id=1, slot=0)
    run_engine(client)

    home = targets_of(client, site=1)
    assert [row["site_id"] for row in home] == [1]
    assert [row["site_id"] for row in targets_of(client, site=away["site_id"])] == [
        away["site_id"]
    ]


def test_list_filters_review_status_and_window(client):
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    run_engine(client)
    assert client.post("/api/targets/1/review", json={"decision": "confirmed"}).status_code == 200

    assert [r["review_status"] for r in targets_of(client, site=1, review_status="confirmed")] == [
        "confirmed"
    ]
    assert targets_of(client, site=1, review_status="unreviewed") == []
    row = client.get("/api/targets", params={**WINDOW, "site": 1, "review_status": "maybe"})
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "unknown_status"
    # 窗口框在目标存在之前：空的，而不是把窗口外的也倒出来。
    early = client.get(
        "/api/targets",
        params={"site": 1, "from": "2026-10-30T09:00:00+00:00", "to": "2026-10-30T10:00:00+00:00"},
    )
    assert early.json()["rows"] == []


def test_node_filter_and_unknown_node(client):
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    add_finding(client, gateway_id=1, node_id=None, slot=0, plugin_id="interference-correlator",
                kind="interference_rise")
    run_engine(client)
    assert [r["target_id"] for r in targets_of(client, site=1, node=1)] == [1], (
        "node 过滤走观测行：接入驻留层面的判定（node 空）不该被算到任何节点头上"
    )
    row = client.get("/api/targets", params={**WINDOW, "site": 1, "node": 99})
    assert row.status_code == 404 and row.json()["detail"]["reason"] == "not_found"


def test_detail_carries_timeline_limitations_and_site_guard(client):
    away = make_site(client, "away", "客场")
    add_finding(
        client, gateway_id=1, node_id=1, slot=0, evidence='{"peaks": [-40]}'
    )
    run_engine(client)
    detail = client.get("/api/targets/1").json()
    assert any("不给距离、方位或坐标" in note for note in detail["limitations"])
    assert any("confidence" in note for note in detail["limitations"]), (
        "置信度的局限必须自己说：它是公式输出不是精度"
    )
    assert any("还没有人工复核" in note for note in detail["limitations"])

    row = client.get("/api/targets/1", params={"site": away["site_id"]})
    assert row.status_code == 404, "带 ?site= 对不上就是 404：页面看到的与它能拿到的一致"
    assert client.get("/api/targets/999").status_code == 404


def test_missing_coverage_is_declared_not_absent(client):
    """coverage_json 没写 = "局限未申报"，与"没有局限"是两个结论（模型 docstring 的判据）。

    引擎产的目标总会写 coverage；这里直接摆一行没有 coverage 的，钉的是读侧的行为。
    """
    with client.app.state.db.scope() as session:
        session.add(
            CandidateTarget(
                site_id=1,
                kind="anomaly_rise",
                producer="hand-inserted@0",
                first_seen_at=BASE,
                last_seen_at=BASE,
            )
        )
        session.commit()
    detail = client.get("/api/targets/1").json()
    assert detail["coverage"] == {"value": None, "error": None}
    assert any("局限未申报" in note for note in detail["limitations"])


# --- 复核 --------------------------------------------------------------------


def test_review_confirms_and_can_be_overturned(client):
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    run_engine(client)

    row = client.post("/api/targets/1/review", json={"decision": "confirmed", "note": "看着像干扰"})
    assert row.status_code == 200, row.text
    body = row.json()
    assert body["review_status"] == "confirmed"
    assert body["review"] == {"value": {"decision": "confirmed", "note": "看着像干扰"}, "error": None}
    assert body["reviewed_at"] is not None

    row = client.post("/api/targets/1/review", json={"decision": "rejected", "note": "改判：证据不足"})
    assert row.json()["review_status"] == "rejected", "复核可反复改判：被拒是状态不是删除"
    assert row.json()["review"]["value"]["note"] == "改判：证据不足", "留最近一次的决定"

    # 复核不许碰算法侧：confidence/evidence 是推测，review 是人话，两列各说各的。
    after = client.get("/api/targets/1").json()
    assert after["confidence"] == 0.3 and after["observations"], "复核不得改写时间线与置信度"


def test_review_validates_decision_and_note(client):
    add_finding(client, gateway_id=1, node_id=1, slot=0)
    run_engine(client)
    row = client.post("/api/targets/1/review", json={"decision": "maybe"})
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "unknown_decision"
    row = client.post("/api/targets/1/review", json={})
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "unknown_decision"
    row = client.post("/api/targets/1/review", json={"decision": "confirmed", "note": "长" * 2001})
    assert row.status_code == 422 and row.json()["detail"]["reason"] == "out_of_range"
    assert client.get("/api/targets/1").json()["review_status"] == "unreviewed", "拒绝的复核不许落"


def test_aggregator_cannot_review_targets(db_path):
    # autouse 的 seeded 夹具已在同一份库上铺好层级（slug "home"）。
    writer = TestClient(create_app(Settings(db_path=db_path, dialer_enabled=False)))
    add_finding(writer, gateway_id=1, node_id=1, slot=0)
    run_engine(writer)

    reader = TestClient(
        create_app(Settings(db_path=db_path, dialer_enabled=False, instance_role="aggregator"))
    )
    assert reader.get("/api/targets/1").status_code == 200, "读侧聚合器照常能看"
    row = reader.post("/api/targets/1/review", json={"decision": "confirmed"})
    assert row.status_code == 405
    assert row.json()["detail"]["reason"] == "aggregator_read_only"


# --- health ------------------------------------------------------------------


def test_health_has_an_honest_targets_section(client):
    body = client.get("/api/health").json()
    assert body["targets"] == {
        "enabled": False,
        "interval_s": 2.0,
        "runs": 0,
        "errors": 0,
        "last_run_at": None,
        "last_error": None,
        "cursor": None,
        "findings_seen": 0,
        "created": 0,
        "joined": 0,
        "last_tick": None,
        "problem": None,
    }
    assert body["status"] == "ok", "没启用是合法部署形态，不算故障"


def test_a_failed_target_round_makes_health_degraded(client):
    engine = client.app.state.targets
    engine.errors = 1
    engine.last_error = "OperationalError: database is locked"
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert any("目标聚合失败" in p and "locked" in p for p in body["problems"])
