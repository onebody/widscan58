"""`GET /api/samples/summary`：桶档位、窗口上限、行数上限与"不许全表扫"。

这个文件要钉住的四件事，每件都对应一种"页面看不出来但数据是假的"失败：

- **窗口与行数都有顶**，而且顶到了要说话：`truncated` 是真的截了才 `True`，`limit` 越界是 422
  而不是"悄悄夹到 2000"。后者让页面以为自己取了全部、画出来的却是最后一段，图上没有线索。
- **桶宽只有档位可选**，但"库里已经存过的那一档"必须还能读：改过一次桶宽的库同时躺着两种粒度，
  只认新档位等于让旧历史凭空消失。
- **取数不得走全表扫**。判据不是"我觉得这条 SQL 该走索引"，是把端点**真的执行过的那条语句**
  拿去 `EXPLAIN QUERY PLAN`：表一大、计划一变，测试就红，而不是等运营者发现趋势图转圈。
- **空与不存在是两件事**：没有行是 200 + `rows: []`（"这块板还没攒够一个桶"），
  场地/节点 id 不存在是 404，而"节点属别的场地"是 422——串场与写错地址该说的话不一样。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event

from app.models import Node, SampleSummary

INDEX = "ix_summary_site_node_slot_bucket"


def now_epoch() -> int:
    return int(datetime.now(timezone.utc).timestamp())


def add_site(client, slug: str, name: str, addr: str) -> dict:
    """一套 site → relay → gateway → node，返回三个 id。节点行由拨号循环写，这里直接摆。"""
    site = client.post("/api/sites", json={"name": name, "slug": slug}).json()
    relay = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": f"{name}塔"}
    ).json()
    gateway = client.post(
        "/api/gateways",
        json={
            "relay_id": relay["relay_id"],
            "name": f"pi-{slug}",
            "kind": "usb-bridge",
            "base_url": f"http://192.168.99.{addr}:8080",
        },
    ).json()
    with client.app.state.db.scope() as session:
        node = Node(
            gateway_id=gateway["gateway_id"],
            display_name=f"{name}的板",
            transport_kind="usb-native",
            transport_addr=f"/dev/tty{slug}",
            status="active",
        )
        session.add(node)
        session.flush()
        node_id = node.node_id
    return {"site": site["site_id"], "gateway": gateway["gateway_id"], "node": node_id}


@pytest.fixture
def rig(client):
    """两个场地各一块板：历史取数的场地过滤只有在真有第二个场地时才验得出来。"""
    return {
        "A": add_site(client, "alpha", "A场", 5),
        "B": add_site(client, "beta", "B场", 6),
    }


def add_summary(
    client,
    where: dict,
    ts_bucket: int,
    *,
    slot: int = 0,
    bucket_s: int = 1,
    source: str = "real",
    **values,
) -> None:
    row = SampleSummary(
        node_id=where["node"],
        slot=slot,
        ts_bucket=ts_bucket,
        site_id=where["site"],
        gateway_id=where["gateway"],
        bucket_s=bucket_s,
        source=source,
        sample_count=values.pop("sample_count", 4),
        ema_count=values.pop("ema_count", 4),
        last_at=datetime.fromtimestamp(ts_bucket, timezone.utc),
        **values,
    )
    with client.app.state.db.scope() as session:
        session.add(row)


def summary(client, **params) -> dict:
    row = client.get("/api/samples/summary", params=params)
    assert row.status_code == 200, row.json()
    return row.json()


# --- 一：取回的形状 ----------------------------------------------------------


def test_rows_come_back_oldest_first_with_their_own_bucket(rig, client):
    a = rig["A"]
    base = now_epoch() - 300
    for offset in (0, 60, 120):
        add_summary(client, a, base + offset, rssi_mean=-80 + offset, rssi_last=-79 + offset)

    rows = summary(client, site=a["site"])["rows"]
    assert [row["ts_bucket"] for row in rows] == [base, base + 60, base + 120]
    assert [row["rssi_mean"] for row in rows] == [-80, -20, 40]
    assert {row["node_id"] for row in rows} == {a["node"]}
    assert all(row["bucket_s"] == 1 for row in rows)


def test_a_site_only_ever_gets_its_own_rows(rig, client):
    a, b = rig["A"], rig["B"]
    base = now_epoch() - 60
    add_summary(client, a, base, rssi_mean=-70)
    add_summary(client, b, base, rssi_mean=-52)

    mine = summary(client, site=a["site"])["rows"]
    theirs = summary(client, site=b["site"])["rows"]
    assert [row["rssi_mean"] for row in mine] == [-70]
    assert [row["rssi_mean"] for row in theirs] == [-52]
    assert {row["node_id"] for row in mine} == {a["node"]}


def test_a_gap_in_the_measurements_stays_a_gap(rig, client):
    """`LEVELS` 的 RSSI 模式一条 EMA 都不带：`ema_*` 为空必须原样回 `null`，不许补 0。"""
    a = rig["A"]
    base = now_epoch() - 60
    add_summary(
        client, a, base, sample_count=5, ema_count=0, rssi_mean=-71.5, ema_mean=None
    )

    row = summary(client, site=a["site"])["rows"][0]
    assert row["ema_mean"] is None
    assert (row["sample_count"], row["ema_count"]) == (5, 0)
    assert row["rssi_mean"] == -71.5


def test_no_rows_yet_is_an_empty_list_and_not_a_404(rig, client):
    """刚接上的板子还没攒够一个桶：页面要显示"尚无数据"，不是"地址错了"。"""
    body = summary(client, site=rig["B"]["site"])
    assert body["rows"] == []
    assert body["truncated"] is False
    assert body["sources"] == []


# --- 二：窗口、行数、截断 ----------------------------------------------------


def test_the_default_window_is_fifteen_minutes_not_the_whole_library(rig, client):
    a = rig["A"]
    moment = now_epoch()
    add_summary(client, a, moment - 100, rssi_mean=-60)
    add_summary(client, a, moment - 3_600, rssi_mean=-61)

    body = summary(client, site=a["site"])
    assert body["range"]["span_s"] == 900
    assert [row["rssi_mean"] for row in body["rows"]] == [-60]


def test_an_explicit_window_selects_both_ends(rig, client):
    a = rig["A"]
    moment = now_epoch()
    start = datetime.fromtimestamp(moment - 300, timezone.utc)
    end = datetime.fromtimestamp(moment - 100, timezone.utc)
    for offset in (600, 300, 200, 100, 20):
        add_summary(client, a, moment - offset)

    body = summary(
        client, site=a["site"], **{"from": start.isoformat(), "to": end.isoformat()}
    )
    assert body["range"] == {
        "from": int(start.timestamp()),
        "to": int(end.timestamp()),
        "span_s": 200,
    }
    # 闭区间：两个端点各自的那一桶都在，窗口外的两头都不在。
    assert [row["ts_bucket"] for row in body["rows"]] == [moment - 300, moment - 200, moment - 100]


def test_a_window_that_runs_backwards_is_an_error_not_an_empty_chart(rig, client):
    moment = now_epoch()
    row = client.get(
        "/api/samples/summary",
        params={
            "site": rig["A"]["site"],
            "from": datetime.fromtimestamp(moment, timezone.utc).isoformat(),
            "to": datetime.fromtimestamp(moment - 60, timezone.utc).isoformat(),
        },
    )
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "range_backwards"


def test_a_window_wider_than_a_day_is_refused(rig, client):
    moment = now_epoch()
    row = client.get(
        "/api/samples/summary",
        params={
            "site": rig["A"]["site"],
            "from": datetime.fromtimestamp(moment - 86_500, timezone.utc).isoformat(),
        },
    )
    assert row.status_code == 422
    detail = row.json()["detail"]
    assert detail["reason"] == "span_too_wide"
    assert detail["maximum_s"] == 86_400


def test_truncation_cuts_the_oldest_rows_and_says_so(rig, client):
    a = rig["A"]
    base = now_epoch() - 300
    for offset in range(0, 300, 60):
        add_summary(client, a, base + offset)

    body = summary(client, site=a["site"], limit=2)
    assert body["limit"] == 2
    assert body["truncated"] is True
    # 截的是窗口里最老的两段，留的是最近的两桶——趋势图的右端不能被丢掉。
    assert [row["ts_bucket"] for row in body["rows"]] == [base + 180, base + 240]


def test_fitting_exactly_is_not_truncated(rig, client):
    a = rig["A"]
    base = now_epoch() - 120
    for offset in (0, 60, 120):
        add_summary(client, a, base + offset)
    assert summary(client, site=a["site"], limit=3)["truncated"] is False


def test_a_limit_above_the_cap_is_refused_rather_than_clamped(rig, client):
    row = client.get("/api/samples/summary", params={"site": rig["A"]["site"], "limit": 20_001})
    assert row.status_code == 422
    assert row.json()["detail"] == {
        "reason": "limit_out_of_range",
        "param": "limit",
        "maximum": 2000,
    }


# --- 三：桶档位 ---------------------------------------------------------------


def test_an_arbitrary_bucket_is_refused_with_the_list_of_what_works(rig, client):
    row = client.get("/api/samples/summary", params={"site": rig["A"]["site"], "bucket": 7})
    assert row.status_code == 422
    detail = row.json()["detail"]
    assert detail["reason"] == "bucket_unsupported"
    assert detail["requested"] == 7
    assert 7 not in detail["allowed"]
    assert 60 in detail["allowed"]


def test_the_default_bucket_is_the_one_the_writer_is_using(rig, client):
    """缺省档位取 `settings.summary_bucket_s`——与 `SummaryAggregator.from_settings` 同一个数。"""
    body = summary(client, site=rig["A"]["site"])
    assert body["bucket"] == client.app.state.settings.summary_bucket_s


def test_a_width_that_is_stored_but_not_a_choice_is_still_readable(rig, client):
    """改过一次桶宽的库里两种粒度并存：旧档位不是白名单档位，也照样必须读得出来。"""
    a = rig["A"]
    base = now_epoch() - 60
    add_summary(client, a, base, rssi_mean=-77)
    add_summary(client, a, base, slot=1, bucket_s=15, rssi_mean=-33)

    body = summary(client, site=a["site"])
    assert body["bucket_widths"] == [1, 15]
    assert [row["rssi_mean"] for row in body["rows"]] == [-77]

    older = summary(client, site=a["site"], bucket=15)
    assert older["bucket"] == 15
    assert [row["rssi_mean"] for row in older["rows"]] == [-33]


def test_filtering_by_bucket_never_mixes_two_granularities(rig, client):
    a = rig["A"]
    base = now_epoch() - 60
    add_summary(client, a, base, bucket_s=1)
    add_summary(client, a, base, slot=1, bucket_s=15)
    assert len(summary(client, site=a["site"], bucket=1)["rows"]) == 1


# --- 四：范围参数与错误口径 ---------------------------------------------------


def test_a_node_of_another_site_is_refused_rather_than_answered_with_nothing(rig, client):
    row = client.get(
        "/api/samples/summary",
        params={"site": rig["A"]["site"], "node": rig["B"]["node"]},
    )
    assert row.status_code == 422
    assert row.json()["detail"] == {
        "reason": "node_not_in_site",
        "node": rig["B"]["node"],
        "site": rig["A"]["site"],
        "node_site": rig["B"]["site"],
    }


def test_a_node_that_does_not_exist_is_404(rig, client):
    row = client.get("/api/samples/summary", params={"site": rig["A"]["site"], "node": 999})
    assert row.status_code == 404
    assert row.json()["detail"] == {"reason": "not_found", "kind": "node", "id": 999}


def test_a_site_that_does_not_exist_is_404(client):
    row = client.get("/api/samples/summary", params={"site": 4242})
    assert row.status_code == 404
    assert row.json()["detail"]["kind"] == "site"


def test_a_slot_outside_the_channel_count_is_refused(rig, client):
    row = client.get(
        "/api/samples/summary", params={"site": rig["A"]["site"], "slot": 8}
    )
    assert row.status_code == 422
    assert row.json()["detail"] == {
        "reason": "slot_out_of_range",
        "param": "slot",
        "slot": 8,
        "maximum": 7,
        "site": rig["A"]["site"],
    }


def test_slot_and_node_narrow_the_rows_together(rig, client):
    a = rig["A"]
    base = now_epoch() - 60
    add_summary(client, a, base, slot=0, rssi_mean=-70)
    add_summary(client, a, base, slot=3, rssi_mean=-55)

    body = summary(client, site=a["site"], node=a["node"], slot=3)
    assert body["filters"] == {"node": a["node"], "nodes": [a["node"]], "slot": 3}
    assert [row["rssi_mean"] for row in body["rows"]] == [-55]


# --- 四b：nodes 参数多节点并集（A 步组合选择层）--------------------------------


def _extra_node(client, where: dict, name: str) -> int:
    """同场地再摆一块板：多节点并集是**场地内**跨板取数，跨场地该被 422 挡住（另有用例）。"""
    with client.app.state.db.scope() as session:
        node = Node(
            gateway_id=where["gateway"],
            display_name=name,
            transport_kind="usb-native",
            transport_addr=f"/dev/tty-extra-{name}",
            status="active",
        )
        session.add(node)
        session.flush()
        return node.node_id


def test_nodes_param_unions_multiple_nodes(rig, client):
    a, b = rig["A"], rig["B"]
    second = _extra_node(client, a, "A场的第二块板")
    base = now_epoch() - 60
    add_summary(client, a, base, slot=0, rssi_mean=-70)
    with client.app.state.db.scope() as session:
        session.add(
            SampleSummary(
                node_id=second,
                slot=0,
                ts_bucket=base,
                site_id=a["site"],
                gateway_id=a["gateway"],
                bucket_s=1,
                source="real",
                sample_count=4,
                ema_count=4,
                rssi_mean=-55,
                last_at=datetime.fromtimestamp(base, timezone.utc),
            )
        )

    body = summary(client, site=a["site"], nodes=f"{a['node']},{second}", slot=0)
    assert body["filters"]["nodes"] == [a["node"], second], "逗号表拆开、去重、按序回显"
    assert sorted(row["rssi_mean"] for row in body["rows"]) == [-70, -55], "同场地两块板的行都回来（分线对比的原料）"
    assert {row["node_id"] for row in body["rows"]} == {a["node"], second}


def test_nodes_param_repeats_collapse_but_stay_in_order(rig, client):
    a = rig["A"]
    body = summary(client, site=a["site"], nodes=f"{a['node']},{a['node']}")
    assert body["filters"]["nodes"] == [a["node"]], "重复 id 合并，不重复校验也不重复过滤"


def test_nodes_param_rejects_node_of_another_site(rig, client):
    a, b = rig["A"], rig["B"]
    row = client.get("/api/samples/summary", params={"site": a["site"], "nodes": f"{a['node']},{b['node']}"})
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "node_not_in_site", "nodes 里的每个 id 都要过场地校验"


def test_nodes_param_rejects_non_numeric_ids(rig, client):
    a = rig["A"]
    row = client.get("/api/samples/summary", params={"site": a["site"], "nodes": "abc"})
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "wrong_type"


# --- 五：`?site=` 缺省落到当前场地 --------------------------------------------


def test_a_single_site_instance_needs_no_site_parameter(client):
    """库里只有一个场地时不必指定：与拨号侧同一条推断，不是"猜第一个"。"""
    a = add_site(client, "solo", "独场", 7)
    add_summary(client, a, now_epoch() - 60, rssi_mean=-42)
    assert [row["rssi_mean"] for row in summary(client)["rows"]] == [-42]


def test_two_sites_without_a_current_one_are_refused(rig, client):
    row = client.get("/api/samples/summary")
    assert row.status_code == 422
    assert row.json()["detail"]["reason"] == "site_required"


def test_the_current_site_key_supplies_the_default(rig, client):
    """`current_site_id` 是"本实例服务哪个场地"的唯一口径，取数缺省也认它。"""
    written = client.put(
        "/api/settings/global",
        json={
            "version": 0,
            "settings": {"current_site_id": rig["B"]["site"]},
            "confirm": ["current_site_id"],
        },
    )
    assert written.status_code == 200, written.json()
    add_summary(client, rig["B"], now_epoch() - 60, rssi_mean=-51)

    body = summary(client)
    assert body["site_id"] == rig["B"]["site"]
    assert [row["rssi_mean"] for row in body["rows"]] == [-51]


# --- 六：来源标记（MOCK 不许混进真实历史里装实时）------------------------------


def test_replayed_rows_keep_their_mock_source(rig, client):
    a = rig["A"]
    base = now_epoch() - 120
    add_summary(client, a, base, rssi_mean=-70)
    add_summary(client, a, base + 60, rssi_mean=-69, source="mock")

    body = summary(client, site=a["site"])
    assert body["sources"] == ["mock", "real"]
    assert [row["source"] for row in body["rows"]] == ["real", "mock"]


# --- 七：不出这个场地、不扫整张表 ----------------------------------------------


def captured_statements(client) -> list[tuple[str, tuple]]:
    """记下端点**真的执行过**的语句：拿它去问计划，测的才是这条 SQL 而不是我重写了一遍。"""
    calls: list[tuple[str, tuple]] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if "FROM sample_summary" in statement:
            calls.append((statement, tuple(parameters)))

    event.listen(client.app.state.db.engine, "before_cursor_execute", record)
    return calls


def test_the_summary_query_uses_the_site_index_never_a_table_scan(rig, client):
    a = rig["A"]
    base = now_epoch() - 300
    for offset in range(0, 300, 60):
        for slot in range(4):
            add_summary(client, a, base + offset, slot=slot)

    calls = captured_statements(client)
    summary(client, site=a["site"], node=a["node"], slot=1)
    assert calls, "端点没走过 sample_summary：这条判据测不到东西"

    statement, params = calls[-1]
    with client.app.state.db.engine.connect() as conn:
        plan = [
            row[3] for row in conn.exec_driver_sql(f"EXPLAIN QUERY PLAN {statement}", params)
        ]
    joined = " | ".join(plan)
    assert "SCAN sample_summary" not in joined, joined
    assert INDEX in joined, joined


def test_every_summary_statement_is_site_bounded(rig, client):
    """端点跑出来的**每一条** sample_summary 语句都带场地约束，一条不许漏。

    判据写成"看语句"而不是"看返回的行"：漏了场地约束的那条查询在只有一个场地的库上照样回对
    的结果，等真有两个场地时它才开始串场——那时已经没人记得是哪条 SQL 的问题了。
    """
    a = rig["A"]
    add_summary(client, a, now_epoch() - 60)
    calls = captured_statements(client)
    summary(client, site=a["site"])
    assert len(calls) >= 2, "档位盘点与取数各一条，少了就是端点没走这两步"
    for statement, _params in calls:
        assert "sample_summary.site_id = ?" in statement, statement
