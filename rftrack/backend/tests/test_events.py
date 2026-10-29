"""`GET /api/events[/{id}]`：事件流的场地过滤、窗口上限，以及详情的证据链与局限。

S4 在 `test_site_isolation.py` 里挂着的那条 skip（"表一落地就得把这条补上"）在这里落地：
`?site=` 必须真的按场地过滤。除此之外这个文件钉三件页面看不出来的事：

- **窗口与行数口径和历史取数完全是同一份**：`range` 与 422 的词都来自 `services/history.py`，
  两张图对"最近 30 分钟"要是各算各的，页面上就会出现"事件说有、趋势说没有"这种查不动的矛盾。
- **详情必须给证据链**：`payload_json` 解不回来就明说 `malformed_json`，不许回一个空对象——
  空的证据链和没有证据链在 S8 的复核页上是完全不同的两件事。
- **局限说明由后端说**：一条 `node_offline` 能讲到哪一步（不等于板子断电）、一条 `mock` 是
  从哪来的（不是此刻的观测），这些话写在响应里，页面才不会自己越讲越大。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event

from app.models import SignalEvent

from test_samples import add_site, now_epoch

INDEX = "ix_event_site_ts"


def add_event(client, where: dict, **over) -> int:
    """摆一条观测事实。`payload` 给字典，落库时拼成 JSON——与采集层同一个形状。"""
    payload = over.pop("payload", None)
    row = SignalEvent(
        site_id=where["site"],
        gateway_id=where["gateway"],
        node_id=over.pop("node_id", where["node"]),
        kind=over.pop("kind", "node_offline"),
        source=over.pop("source", "real"),
        ts=over.pop("ts", datetime.now(timezone.utc)),
        payload_json=None if payload is None else json.dumps(payload, ensure_ascii=False),
        **over,
    )
    with client.app.state.db.scope() as session:
        session.add(row)
        session.flush()
        return row.event_id


@pytest.fixture
def rig(client):
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    return {"A": a, "B": b}


def feed(client, **params) -> dict:
    row = client.get("/api/events", params=params)
    assert row.status_code == 200, row.json()
    return row.json()


# --- 一：场地过滤（S4 挂下来的那条判据）--------------------------------------


def test_a_site_only_ever_sees_its_own_events(rig, client):
    a, b = rig["A"], rig["B"]
    add_event(client, a, payload={"note": "A 的板安静了"})
    add_event(client, b, payload={"note": "B 的板安静了"})

    mine = feed(client, site=a["site"])["rows"]
    theirs = feed(client, site=b["site"])["rows"]
    assert [row["site_id"] for row in mine] == [a["site"]]
    assert [row["site_id"] for row in theirs] == [b["site"]]
    assert {row["node_id"] for row in mine} == {a["node"]}


def test_an_event_of_another_site_is_not_readable_by_id_when_a_site_is_given(rig, client):
    """`?site=` 与 id 对不上就 404：这个端点不能拿来试探别的场地的记录。"""
    other = add_event(client, rig["B"])
    row = client.get(f"/api/events/{other}", params={"site": rig["A"]["site"]})
    assert row.status_code == 404
    assert row.json()["detail"] == {"reason": "not_found", "kind": "event", "id": other}


def test_a_by_id_read_without_a_site_still_answers_and_says_which_site(rig, client):
    """主键直读不是"跨场地扫表"：那条属于哪个场地就写在响应里，不许靠猜。"""
    ident = add_event(client, rig["B"])
    body = client.get(f"/api/events/{ident}")
    assert body.status_code == 200, body.json()
    assert body.json()["site_id"] == rig["B"]["site"]


def test_two_sites_without_a_current_one_are_refused(rig, client):
    row = client.get("/api/events")
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "site_required"


def test_a_node_of_another_site_is_refused_rather_than_answered_with_nothing(rig, client):
    """与历史取数同一个词、同一个码：`services/history.py` 里那条口径只有一处实现。"""
    row = client.get(
        "/api/events", params={"site": rig["A"]["site"], "node": rig["B"]["node"]}
    )
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "node_not_in_site"


# --- 二：窗口、行数、排序 -----------------------------------------------------


def test_the_default_window_is_the_same_fifteen_minutes_the_trend_uses(rig, client):
    a = rig["A"]
    moment = datetime.now(timezone.utc)
    inside = add_event(client, a, ts=moment - timedelta(seconds=100))
    outside = add_event(client, a, ts=moment - timedelta(hours=2))

    body = feed(client, site=a["site"])
    assert body["range"]["span_s"] == 900
    assert [row["event_id"] for row in body["rows"]] == [inside]
    assert outside not in [row["event_id"] for row in body["rows"]]


def test_events_come_back_newest_first(rig, client):
    a = rig["A"]
    moment = datetime.now(timezone.utc)
    first = add_event(client, a, ts=moment - timedelta(minutes=5))
    second = add_event(client, a, ts=moment - timedelta(minutes=1))

    assert [row["event_id"] for row in feed(client, site=a["site"])["rows"]] == [
        second,
        first,
    ]


def test_the_two_history_ends_agree_on_an_explicit_window(rig, client):
    """同一对 `from`/`to` 在两个端点上必须折出同一个区间，否则两张图对不上账。"""
    a = rig["A"]
    moment = datetime.now(timezone.utc)
    start, end = moment - timedelta(minutes=10), moment - timedelta(minutes=2)
    body = feed(client, site=a["site"], **{"from": start.isoformat(), "to": end.isoformat()})
    row = client.get(
        "/api/samples/summary",
        params={"site": a["site"], "from": start.isoformat(), "to": end.isoformat()},
    )
    assert row.status_code == 200, row.json()
    assert body["range"] == row.json()["range"]


def test_truncation_cuts_the_oldest_and_says_so(rig, client):
    a = rig["A"]
    moment = datetime.now(timezone.utc)
    ids = [add_event(client, a, ts=moment - timedelta(seconds=60 * n)) for n in range(5)]
    body = feed(client, site=a["site"], limit=2)
    assert body["limit"] == 2
    assert body["truncated"] is True
    assert [row["event_id"] for row in body["rows"]] == ids[:2]


def test_a_limit_above_the_cap_is_refused_rather_than_clamped(rig, client):
    row = client.get("/api/events", params={"site": rig["A"]["site"], "limit": 0})
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "limit_out_of_range"


def test_a_window_that_runs_backwards_is_an_error_not_an_empty_feed(rig, client):
    moment = datetime.now(timezone.utc)
    row = client.get(
        "/api/events",
        params={
            "site": rig["A"]["site"],
            "from": moment.isoformat(),
            "to": (moment - timedelta(minutes=5)).isoformat(),
        },
    )
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "range_backwards"


def test_no_events_yet_is_an_empty_feed_and_not_a_404(rig, client):
    body = feed(client, site=rig["B"]["site"])
    assert body["rows"] == []
    assert body["truncated"] is False


def test_an_event_written_in_this_very_second_is_still_in_the_window(rig, client):
    """默认窗口的右端是"现在"，不是"上一秒整"：刚发生的事件不许被边界切掉。

    `ts` 带微秒而窗口按整秒算，写成 `ts <= to` 时这一秒内的 `…:30.123456` 比界 `…:30` 大，
    于是"事件刚写完就查不到"——现场看像是刷新没生效。
    """
    ident = add_event(client, rig["A"])
    assert [row["event_id"] for row in feed(client, site=rig["A"]["site"])["rows"]] == [ident]


# --- 三：筛选 ---------------------------------------------------------------


def test_kind_and_node_narrow_the_feed(rig, client):
    a = rig["A"]
    add_event(client, a, kind="node_offline")
    add_event(client, a, kind="node_online")

    online = feed(client, site=a["site"], kind="node_online")
    assert [row["kind"] for row in online["rows"]] == ["node_online"]

    scoped = feed(client, site=a["site"], node=a["node"], kind="node_offline")
    assert [row["kind"] for row in scoped["rows"]] == ["node_offline"]
    assert {row["node_id"] for row in scoped["rows"]} == {a["node"]}


def test_an_unknown_kind_is_an_empty_feed_not_a_404(rig, client):
    """`kind` 没有 CHECK 约束（S8 会加新 kinds），所以不认识的种类只能说"没有"。"""
    assert feed(client, site=rig["A"]["site"], kind="target_suspect")["rows"] == []


# --- 四：详情的证据链与局限 ---------------------------------------------------


def test_the_detail_carries_the_evidence_chain(rig, client):
    a = rig["A"]
    ident = add_event(
        client,
        a,
        payload={"ep": "/dev/ttyUSB0", "silence_s": 6.5, "last_seq": 41, "silence_budget_s": 5.0},
    )
    body = client.get(f"/api/events/{ident}").json()
    assert body["payload"] == {
        "ep": "/dev/ttyUSB0",
        "silence_s": 6.5,
        "last_seq": 41,
        "silence_budget_s": 5.0,
    }
    assert body["payload_error"] is None


def test_a_malformed_payload_is_reported_as_one(rig, client):
    """坏 JSON 要说坏：回 `payload: null` 而不带 `payload_error`，就是"这条确实没有上下文"的假话。"""
    ident = add_event(client, rig["A"])
    with client.app.state.db.scope() as session:
        session.get(SignalEvent, ident).payload_json = "{不是 JSON"
    body = client.get(f"/api/events/{ident}").json()
    assert body["payload"] is None
    assert body["payload_error"] == "malformed_json"


def test_the_limits_say_what_an_offline_event_cannot_claim(rig, client):
    a = rig["A"]
    ident = add_event(client, a, payload={"silence_s": 9.0})
    notes = client.get(f"/api/events/{ident}").json()["limitations"]
    assert any("距离" in note and "坐标" in note for note in notes)
    assert any("不等于板子断电" in note for note in notes)
    assert any("没有触发数值" in note for note in notes)


def test_a_replayed_event_says_so_in_the_detail(rig, client):
    ident = add_event(client, rig["A"], source="mock")
    notes = client.get(f"/api/events/{ident}").json()["limitations"]
    assert any("mock" in note for note in notes)
    assert any("不是此刻" in note for note in notes)


def test_the_list_marks_each_rows_source(rig, client):
    """列表也要一眼分得出 MOCK 与真实：徽标不能只在详情页才有依据。"""
    a = rig["A"]
    add_event(client, a, source="real")
    add_event(client, a, source="mock")
    body = feed(client, site=a["site"])
    assert body["sources"] == ["mock", "real"]
    assert {row["source"] for row in body["rows"]} == {"mock", "real"}


def test_an_event_without_a_node_says_it_is_gateway_level(rig, client):
    ident = add_event(client, rig["A"], node_id=None, kind="gateway_lost")
    body = client.get(f"/api/events/{ident}").json()
    assert body["node_id"] is None
    assert any("没有指认到具体节点" in note for note in body["limitations"])


# --- 五：不许全表扫 -----------------------------------------------------------


def captured_statements(client) -> list[tuple[str, tuple]]:
    calls: list[tuple[str, tuple]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "FROM signal_event" in statement:
            calls.append((statement, tuple(parameters)))

    event.listen(client.app.state.db.engine, "before_cursor_execute", record)
    return calls


def test_the_feed_query_uses_the_site_index_never_a_table_scan(rig, client):
    a = rig["A"]
    moment = datetime.now(timezone.utc)
    for n in range(60):
        add_event(client, a, ts=moment - timedelta(seconds=n * 5))

    calls = captured_statements(client)
    feed(client, site=a["site"])
    assert calls, "端点没走过 signal_event：这条判据测不到东西"

    statement, params = calls[-1]
    with client.app.state.db.engine.connect() as conn:
        plan = [
            row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params)
        ]
    joined = " | ".join(plan)
    assert "SCAN signal_event" not in joined, joined
    assert INDEX in joined, joined


def test_every_feed_statement_is_site_bounded(rig, client):
    add_event(client, rig["A"])
    calls = captured_statements(client)
    feed(client, site=rig["A"]["site"])
    for statement, _params in calls:
        assert "signal_event.site_id = ?" in statement, statement
