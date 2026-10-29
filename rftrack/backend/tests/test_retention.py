"""S6 的历史侧：桶算得对、同一个桶再写不长新行、删是按场地删的。

判据分三处来源：方案 §3（`(node_id, slot, ts_bucket)` 唯一 + "10 万行批量写 < 2 s"
**[需要实机验证]**）、§5（`retention.py` = 桶聚合批量写 + 按场地覆写保留 + 逐场地清理
并打结构化日志），以及 §3 那条前置条件"SQLite 的 `DATETIME` 读回来是 naive"。

最后一条在这里被钉成了一个**实测**断言（`test_sqlite_stores_the_wall_clock...`）而不是
注释里的传说：写进去的偏移量会被丢掉、且不换算，这决定了 `to_utc()` 必须存在。
"""

import asyncio
import json
import logging
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.config import Settings
from app.database import Database
from app.models import (
    Gateway,
    Node,
    Relay,
    SampleSummary,
    SignalEvent,
    Site,
    to_utc,
    utcnow,
)
from app.main import create_app
from app.services import config_layer, retention
from app.services.retention import SummaryAggregator, bucket_start
import migrate

#: 同一瞬间的两种写法：断言"存进去变成了两个值"用的就是这一对。
TZ8 = timezone(timedelta(hours=8))
T0 = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(db_path):
    database = Database(db_path)
    yield database
    database.dispose()


@pytest.fixture
def settings(db_path):
    """清理任务在单测里默认关掉：它一启动就删库，会和摆历史数据的用例抢同一张表。"""
    return Settings(db_path=db_path, dialer_enabled=False, retention_purge_enabled=False)


@pytest.fixture
def stack(db):
    """两个场地，各一条中继→接入机→节点链。跨场地删除没有 A/B 就验不出来。"""
    ids = {}
    with db.scope() as session:
        for slug in ("alpha", "beta"):
            site = Site(name=f"{slug}场", slug=slug)
            session.add(site)
            session.flush()
            relay = Relay(site_id=site.site_id, name=f"{slug}塔")
            session.add(relay)
            session.flush()
            gateway = Gateway(relay_id=relay.relay_id, name=f"pi-{slug}")
            session.add(gateway)
            session.flush()
            node = Node(
                gateway_id=gateway.gateway_id,
                display_name=f"{slug}板",
                transport_kind="usb-native",
                transport_addr=f"/dev/{slug}",
                status="active",
            )
            session.add(node)
            session.flush()
            ids[slug] = {
                "site": site.site_id,
                "gateway": gateway.gateway_id,
                "node": node.node_id,
            }
    return ids


def summary_row(stack, slug, slot=0, ts_bucket=0, **overrides):
    """手写一行摘要，用来精确控制"库里已经有什么"（不经过累加器）。"""
    ids = stack[slug]
    row = {
        "node_id": ids["node"],
        "slot": slot,
        "ts_bucket": ts_bucket,
        "site_id": ids["site"],
        "gateway_id": ids["gateway"],
        "bucket_s": 1,
        "sample_count": 1,
        "ema_count": 1,
        "rssi_mean": -80.0,
        "rssi_min": -80.0,
        "rssi_max": -80.0,
        "rssi_last": -80.0,
        "ema_mean": -80.0,
        "ema_min": -80.0,
        "ema_max": -80.0,
        "ema_last": -80.0,
        "source": "real",
        "last_at": T0 + timedelta(seconds=ts_bucket),
    }
    row.update(overrides)
    return row


def flush(db, rows):
    with db.scope() as session:
        return retention.upsert_summaries(session, rows)


def put_site_days(db, stack, slug, **values):
    """把场地的覆写直接写进列，不走 `PUT /api/settings/site/…`。

    这一轮测的是清理侧读不读得到覆写；HTTP 那条路在 `test_settings_isolation.py`
    与 `test_site_isolation.py` 里各有断言了，这里重复一遍只会让失败原因变模糊。
    """
    with db.scope() as session:
        session.execute(
            Site.__table__.update()
            .where(Site.site_id == stack[slug]["site"])
            .values(**values)
        )


# --- 时间口径：为什么 `to_utc` 必须存在 --------------------------------------


def test_sqlite_stores_the_wall_clock_and_drops_the_utc_offset(db, stack):
    """实测：`DATETIME(timezone=True)` 在 SQLite 上**不换算、也不存偏移**。

    同一个瞬间用 UTC 和 +08:00 写两次，库里得到两个差 8 小时的字符串——所以跨这条边界的
    时刻必须先 `astimezone(utc)`，读回来的 naive 必须重新贴上 UTC。这条断言不是文档装饰：
    "滞后 X 秒"算成"滞后 8 小时"就是忘了它的后果。
    """
    with db.scope() as session:
        for moment in (T0, T0.astimezone(TZ8)):
            session.add(
                SignalEvent(
                    ts=moment,
                    site_id=stack["alpha"]["site"],
                    gateway_id=stack["alpha"]["gateway"],
                    kind="node_online",
                    source="real",
                )
            )
        stored = session.execute(
            select(SignalEvent.ts).order_by(SignalEvent.event_id)
        ).scalars().all()
    assert stored == [
        T0.replace(tzinfo=None),
        T0.astimezone(TZ8).replace(tzinfo=None),
    ], "存的各自是那两个钟面上的数：既没换算成 UTC，也没留下偏移"
    assert stored[0] != stored[1], "同一瞬间存成了两个值：这就是必须显式换算的证据"


def test_to_utc_reads_naive_back_as_utc_and_converts_other_offsets():
    assert to_utc(T0.replace(tzinfo=None)) == T0
    assert to_utc(T0.astimezone(TZ8)) == T0
    assert to_utc(None) is None
    with pytest.raises(TypeError):
        to_utc("2026-10-29 12:00:00")


def test_bucket_start_aligns_to_the_grid_and_ignores_how_the_clock_was_written():
    moment = datetime(2026, 10, 29, 12, 0, 59, tzinfo=timezone.utc)
    epoch = int(moment.timestamp())
    assert bucket_start(moment, 1) == epoch
    assert bucket_start(moment, 5) == epoch - epoch % 5
    # 同一瞬间的三种写法必须落进同一个桶，否则回放和实时会各写一行。
    assert bucket_start(moment.astimezone(TZ8), 5) == bucket_start(moment, 5)
    assert bucket_start(moment.replace(tzinfo=None), 5) == bucket_start(moment, 5)


def test_bucket_start_refuses_a_zero_width():
    """桶宽 0 不是"最细粒度"，是除零：`Settings` 已经拦过一次，这里不许有第二条路。"""
    with pytest.raises(ValueError):
        bucket_start(T0, 0)


# --- 桶累加器 ---------------------------------------------------------------


def test_aggregator_reports_mean_min_max_and_last_separately():
    agg = SummaryAggregator(bucket_s=5)
    ids = {"site_id": 1, "gateway_id": 1, "node_id": 7, "slot": 2}
    for offset, value in ((0, -80.0), (1, -70.0), (2, -90.0)):
        agg.add(**ids, ts=T0 + timedelta(seconds=offset), rssi=value, ema=value + 1)
    assert agg.pending == 1, "三个点同在 5 秒的一格里"
    row = agg.drain()[0]
    assert row["ts_bucket"] == bucket_start(T0, 5)
    assert (row["sample_count"], row["ema_count"]) == (3, 3)
    assert row["rssi_mean"] == pytest.approx((-80 - 70 - 90) / 3)
    assert (row["rssi_min"], row["rssi_max"], row["rssi_last"]) == (-90.0, -70.0, -90.0)
    assert row["ema_last"] == -89.0, "EMA 的「最后一个」跟着自己的那条序列，不是跟着 rssi"
    assert row["last_at"] == T0 + timedelta(seconds=2)
    assert agg.pending == 0, "drain 就是取走"


def test_rssi_only_frames_leave_every_ema_field_null():
    """`LEVELS` 的 RSSI 模式确实不测 EMA（`frame_parser.parse_levels` 回 None）。

    那一格必须是 NULL：写 0 就是"−0 dBFS ≈ 满量程"，是一个很强的假话；
    把 rssi 顶上去更糟——页面会把它当 EMA 画。
    """
    agg = SummaryAggregator(bucket_s=10)
    for offset in range(3):
        agg.add(
            site_id=1, gateway_id=1, node_id=1, slot=0,
            ts=T0 + timedelta(seconds=offset), rssi=-80.0, ema=None,
        )
    row = agg.drain()[0]
    assert row["sample_count"] == 3 and row["ema_count"] == 0
    assert row["rssi_mean"] == -80.0
    assert all(row[key] is None for key in ("ema_mean", "ema_min", "ema_max", "ema_last"))


def test_a_point_that_measured_nothing_does_not_become_a_row():
    agg = SummaryAggregator(bucket_s=1)
    outcome = agg.add(site_id=1, gateway_id=1, node_id=1, slot=0, ts=T0, rssi=None, ema=None)
    assert outcome == "skipped_empty"
    assert agg.pending == 0 and agg.stats()["skipped_empty"] == 1
    assert agg.drain() == []


def test_the_accumulator_is_bounded_and_counts_what_it_refused():
    """到硬上限就**拒收新格并计数**，与 `GatewayQueue` 同一条纪律：不阻塞、不静默。

    丢的是新到的那一格而不是已经封口的旧格，理由很具体：旧桶不会再进点了，留着它下一轮
    还能写进库；丢旧桶等于把已经观测到的数据扔掉，而新桶本来就还没观测完。
    """
    agg = SummaryAggregator(bucket_s=1, max_buckets=2)
    for slot in range(3):
        agg.add(site_id=1, gateway_id=1, node_id=1, slot=slot, ts=T0, rssi=-80.0, ema=None)
    assert agg.dropped_full == 1
    rows = agg.drain()
    assert sorted(row["slot"] for row in rows) == [0, 1]
    assert agg.stats()["dropped_full"] == 1


def test_needs_flush_turns_on_at_the_limit():
    agg = SummaryAggregator(bucket_s=1, max_buckets=1)
    assert agg.needs_flush is False
    agg.add(site_id=1, gateway_id=1, node_id=1, slot=0, ts=T0, rssi=-80.0, ema=None)
    assert agg.needs_flush is True


def test_a_mock_point_in_a_real_bucket_keeps_the_bucket_marked_mock():
    """来源不许被后写的 `real` 洗掉：回放和实时撞进同一格时，宁可多打一个 MOCK 角标。"""
    agg = SummaryAggregator(bucket_s=10)
    agg.add(site_id=1, gateway_id=1, node_id=1, slot=0, ts=T0, rssi=-80.0, ema=None, source="real")
    agg.add(site_id=1, gateway_id=1, node_id=1, slot=0, ts=T0, rssi=-70.0, ema=None, source="mock")
    assert agg.drain()[0]["source"] == "mock"


# --- 批量写：同一个桶再刷一次不长新行 ----------------------------------------


def test_writing_the_same_bucket_twice_merges_instead_of_duplicating(db, stack):
    """§3 那句"`(node_id, slot, ts_bucket)` 唯一"的实建形态：一次刷两份部分和。

    合并必须按点数加权：直接取后一份的均值，等于把先前的观测抹掉。
    顺带钉住 SQLite 的 UPSERT 语义——所有 SET 表达式都按**更新前**的那一行求值，
    所以 `rssi_mean` 里用到的 `sample_count` 不会被同一句里先写的计数列改掉（实测 3.53.1）。
    """
    flush(db, [summary_row(stack, "alpha", ts_bucket=100, sample_count=2,
                          rssi_mean=-80.0, rssi_min=-85.0, rssi_max=-75.0, rssi_last=-75.0,
                          ema_count=0, ema_mean=None, ema_min=None, ema_max=None, ema_last=None)])
    flush(db, [summary_row(stack, "alpha", ts_bucket=100, sample_count=2,
                           rssi_mean=-60.0, rssi_min=-60.0, rssi_max=-60.0, rssi_last=-60.0,
                           ema_count=0, ema_mean=None, ema_min=None, ema_max=None, ema_last=None,
                           last_at=T0 + timedelta(seconds=101))])

    with db.scope() as session:
        rows = session.execute(select(SampleSummary)).scalars().all()
    assert len(rows) == 1, "同一个桶第二次写不得长出新行"
    row = rows[0]
    assert row.sample_count == 4
    assert row.rssi_mean == pytest.approx(-70.0), "两份各 2 点的 -80/-60 加权 = -70"
    assert (row.rssi_min, row.rssi_max) == (-85.0, -60.0)
    assert row.rssi_last == -60.0


def test_the_ema_series_keeps_its_own_denominator(db, stack):
    """先一刷只有 RSSI、后一刷带 EMA：`ema_mean` 的分母必须只是带 EMA 的那几个点。

    只有一个 `sample_count` 时这里会算出 (-80*2 + -50*2)/4 = -65，把"没测过"的那两点
    当成 -80 参与平均——那就是一个算错了但看不出错的数，所以行里另有 `ema_count`。
    """
    flush(db, [
        summary_row(stack, "alpha", ts_bucket=200, sample_count=2, ema_count=0,
                    rssi_mean=-80.0, ema_mean=None, ema_min=None, ema_max=None, ema_last=None),
        summary_row(stack, "alpha", ts_bucket=200, sample_count=2, ema_count=2,
                    rssi_mean=-50.0, ema_mean=-50.0, ema_min=-50.0, ema_max=-50.0,
                    ema_last=-50.0, last_at=T0 + timedelta(seconds=201)),
    ])

    with db.scope() as session:
        row = session.execute(select(SampleSummary)).scalars().one()
    assert (row.sample_count, row.ema_count) == (4, 2)
    assert row.rssi_mean == pytest.approx(-65.0)
    assert row.ema_mean == pytest.approx(-50.0), "EMA 的平均只按它自己的两个点"


def test_a_late_flush_of_older_points_does_not_move_the_last_reading_backwards(db, stack):
    """乱序到达是常态（强制刷、重启前最后一刷、回放追赶）：`*_last` 只认观测时刻。"""
    flush(db, [summary_row(stack, "alpha", ts_bucket=300, rssi_mean=-40.0, rssi_min=-40.0,
                           rssi_max=-40.0, rssi_last=-40.0, ema_mean=-40.0, ema_min=-40.0,
                           ema_max=-40.0, ema_last=-40.0,
                           last_at=T0 + timedelta(seconds=301))])
    flush(db, [summary_row(stack, "alpha", ts_bucket=300, rssi_mean=-90.0, rssi_min=-90.0,
                           rssi_max=-90.0, rssi_last=-90.0, ema_mean=-90.0, ema_min=-90.0,
                           ema_max=-90.0, ema_last=-90.0,
                           last_at=T0 + timedelta(seconds=300))])

    with db.scope() as session:
        row = session.execute(select(SampleSummary)).scalars().one()
    assert row.sample_count == 2
    assert row.rssi_last == -40.0, "后写的那批更早，不得把「最后一个读数」倒退"
    assert row.ema_last == -40.0
    assert row.last_at == (T0 + timedelta(seconds=301)).replace(tzinfo=None)


def test_null_sides_survive_a_merge(db, stack):
    """一侧全 NULL、另一侧有值：合并结果必须是那个有值的，而不是 NULL。

    SQLite 的两参 `max(a,b)` 遇到 NULL 直接回 NULL，所以 min/max 的合并要过 `coalesce`；
    这条测试就是为了钉住那个 `coalesce` 不是多余的。
    """
    nulls = {key: None for key in ("rssi_mean", "rssi_min", "rssi_max", "rssi_last",
                                   "ema_mean", "ema_min", "ema_max", "ema_last")}
    flush(db, [
        summary_row(stack, "alpha", ts_bucket=400, sample_count=0, ema_count=0, **nulls),
        summary_row(stack, "alpha", ts_bucket=400, sample_count=1, ema_count=1,
                    rssi_mean=-77.0, rssi_min=-77.0, rssi_max=-77.0, rssi_last=-77.0,
                    ema_mean=-70.0, ema_min=-70.0, ema_max=-70.0, ema_last=-70.0),
    ])
    with db.scope() as session:
        row = session.execute(select(SampleSummary)).scalars().one()
    assert row.sample_count == 1
    assert (row.rssi_mean, row.rssi_min, row.rssi_max, row.rssi_last) == (-77.0,) * 4
    assert row.ema_mean == pytest.approx(-70.0)


def test_an_empty_batch_writes_nothing(db):
    with db.scope() as session:
        assert retention.upsert_summaries(session, []) == 0


def test_a_batch_of_100k_buckets_writes_in_one_statement(db, stack):
    """§3 的判据原文："10 万行批量写 < 2 s **[需要实机验证]**"——本机实测已补上。

    2026-10-29 在本机（Apple arm64 / CPython 3.14 / SQLite 3.53）实测：一条
    `INSERT … ON CONFLICT DO UPDATE` 送 10 万行 = **1.4 s**（插入路径）与 **1.4 s**
    （同一批再刷一次的合并路径）。断言按 8 s 收，是 5 倍余量，防的是"退化成逐行
    `session.add()`"这种量级变化，不是给 Pi 定标准——Pi 上的数字要等 S9 在真 SD 卡上
    压测才能填，那时候这条判据才真的算过关。
    """
    rows = [
        summary_row(stack, "alpha", slot=index % 8, ts_bucket=1000 + index)
        for index in range(100_000)
    ]
    started = time.perf_counter()
    with db.scope() as session:
        written = retention.upsert_summaries(session, rows)
    elapsed = time.perf_counter() - started
    assert written == 100_000
    with db.scope() as session:
        assert session.execute(select(func.count(SampleSummary.node_id))).scalar() == 100_000
    assert elapsed < 8.0, f"10 万行批量写用了 {elapsed:.2f} s（含提交）"


# --- 按场地保留 -------------------------------------------------------------


def add_event(db, stack, slug, ts, kind="node_offline"):
    ids = stack[slug]
    with db.scope() as session:
        session.add(
            SignalEvent(
                ts=ts, site_id=ids["site"], gateway_id=ids["gateway"], node_id=ids["node"],
                kind=kind, source="real",
            )
        )


def counts(db, stack, slug):
    """这个场地还剩多少事件/摘要行——断言"删了谁"只看这一个数，别看日志。"""
    ids = stack[slug]
    with db.scope() as session:
        return {
            "events": session.execute(
                select(func.count(SignalEvent.event_id)).where(
                    SignalEvent.site_id == ids["site"]
                )
            ).scalar(),
            "summaries": session.execute(
                select(func.count(SampleSummary.site_id)).where(
                    SampleSummary.site_id == ids["site"]
                )
            ).scalar(),
        }


def test_purging_site_a_leaves_site_bs_events_and_summaries_alone(db, settings, stack):
    """判据"逐场地清理"里最硬的一条：删 A 不许碰到 B 的一行。

    两边的数据故意同构，而且卡在"两个保留期的夹缝"里：5 天前的数据**超出** A 收紧后的
    1 天，又**没超出** B 的默认 7 天（摘要同理：5 天 < 30 天）。这样"A 删两行、B 一行没动"
    就只能由"按场地取值、按场地删"解释——换任何别的数据布局，删多了也可能被解释成
    "两场的旧数据本来就不一样"。
    """
    stale_bucket = bucket_start(utcnow() - timedelta(days=5), settings.summary_bucket_s)
    fresh_bucket = bucket_start(utcnow(), settings.summary_bucket_s)
    for slug in ("alpha", "beta"):
        add_event(db, stack, slug, utcnow() - timedelta(days=5))
        flush(db, [
            summary_row(stack, slug, slot=0, ts_bucket=stale_bucket),
            summary_row(stack, slug, slot=1, ts_bucket=fresh_bucket),
        ])
    put_site_days(db, stack, "alpha", retention_events_days=1, retention_summary_days=1)

    with db.scope() as session:
        reports = retention.purge_all(settings, session)

    assert {report.site_id: (report.events_deleted, report.summaries_deleted)
            for report in reports} == {
        stack["alpha"]["site"]: (1, 1),
        stack["beta"]["site"]: (0, 0),
    }
    assert counts(db, stack, "alpha") == {"events": 0, "summaries": 1}
    assert counts(db, stack, "beta") == {"events": 1, "summaries": 2}
    assert [report.events_source for report in reports] == ["site", "default"], \
        "同一条数据、两种命运：来源层必须说清是谁的覆写在起作用"


def test_the_effective_days_come_from_the_layer_chain_with_their_source(db, settings, stack):
    """天数取值走 `effective_for_site`：场地 > 全局 > 内置默认，并且说得出来源层。"""
    with db.scope() as session:
        session.execute(
            Site.__table__.update()
            .where(Site.site_id == stack["beta"]["site"])
            .values(retention_events_days=3)
        )
        config_layer.ensure_app_config(session).retention_summary_days = 90

    with db.scope() as session:
        beta = session.get(Site, stack["beta"]["site"])
        limits = retention.retention_for(settings, session, beta)
    assert limits["events_days"] == (3, "site")
    assert limits["summaries_days"] == (90, "global"), "场地没设的那一格轮到全局"


def test_a_negative_value_falls_back_instead_of_emptying_the_table(db, settings, stack):
    """库里存了个负数天数：`config_layer` 把它拒掉并回落，清理任务因此不必再校验一遍。

    这条接力必须测试：如果哪天有人把"生效值一定 ≥0"改成"读列自己算"，这里就会红，
    而红之前那个负数已经会把整个场地的历史删空。
    """
    put_site_days(db, stack, "alpha", retention_events_days=-5)
    add_event(db, stack, "alpha", utcnow() - timedelta(days=1), kind="node_online")

    with db.scope() as session:
        site = session.get(Site, stack["alpha"]["site"])
        limits = retention.retention_for(settings, session, site)
        report = retention.purge_site(settings, session, site)
    assert limits["events_days"] == (7, "default"), "坏值被拒 → 回落到内置默认 7 天"
    assert report.events_days == 7
    assert report.events_deleted == 0, "1 天前的一条在 7 天保留期内，本来就不该删"


def test_zero_days_means_only_now_and_therefore_clears_the_site(db, settings, stack):
    put_site_days(db, stack, "alpha", retention_events_days=0, retention_summary_days=0)
    add_event(db, stack, "alpha", utcnow() - timedelta(seconds=5), kind="node_online")

    with db.scope() as session:
        report = retention.purge_site(settings, session, session.get(Site, stack["alpha"]["site"]))
    assert report.events_deleted == 1, "0 天 = 只留当下，比它早的全删（`config.py` 里那句话的落点）"


def test_summaries_age_out_by_bucket_not_by_the_wall_clock_string(db, settings, stack):
    """摘要的保留界限折成 `ts_bucket` 比较：这一列本来就是整数，索引也建在它上面。"""
    stale_bucket = bucket_start(utcnow() - timedelta(days=40), settings.summary_bucket_s)
    fresh_bucket = bucket_start(utcnow(), settings.summary_bucket_s)
    flush(db, [
        summary_row(stack, "alpha", slot=0, ts_bucket=stale_bucket),
        summary_row(stack, "alpha", slot=1, ts_bucket=fresh_bucket),
    ])
    put_site_days(db, stack, "alpha", retention_summary_days=30)

    with db.scope() as session:
        report = retention.purge_site(settings, session, session.get(Site, stack["alpha"]["site"]))
    assert (report.summaries_deleted, report.events_deleted) == (1, 0)
    with db.scope() as session:
        assert [row.slot for row in session.execute(select(SampleSummary)).scalars()] == [1]


def test_purge_all_logs_one_structured_line_per_site(db, settings, stack, caplog):
    # 不带 `logger=`：本机实测 `caplog.at_level(INFO, logger="rftrack.retention")` 之后
    # `caplog.records` 仍是空的（连直接 `LOG.info()` 都抓不到），而设到 root 上就抓得到。
    # 取哪个 logger 不是风格问题——按名字过滤留在下一行的推导里做，判据不变。
    with caplog.at_level(logging.INFO):
        with db.scope() as session:
            retention.purge_all(settings, session)
    lines = [record.getMessage() for record in caplog.records if record.name == "rftrack.retention"]
    assert len(lines) == 2, "逐场地清理就得逐场地说话，一条汇总不算"
    payload = json.loads(lines[0].split(" ", 1)[1])
    assert {"site_id", "name", "events_days", "summaries_days", "events_deleted",
            "summaries_deleted", "events_source", "summaries_source"} <= set(payload)


# --- 后台节拍 ---------------------------------------------------------------


def test_the_runner_purges_once_and_keeps_the_last_report(db, settings, stack):
    add_event(db, stack, "alpha", utcnow() - timedelta(days=30))
    runner = retention.RetentionRunner(db, settings)
    reports = runner.run_once()
    assert [report.site_id for report in reports] == [
        stack["alpha"]["site"], stack["beta"]["site"]
    ]
    status = runner.status()
    assert status["enabled"] is False and status["runs"] == 1 and status["errors"] == 0
    assert status["last_sites"][0]["events_deleted"] == 1, "默认 7 天：30 天前的那条就是要被删"
    assert status["last_sites"][1]["events_deleted"] == 0


def test_a_purge_failure_is_counted_and_the_loop_survives(db, settings, stack):
    """一轮清理炸了不许把循环带走：否则"保留策略在跑"这句话只在第一次有效。"""
    runner = retention.RetentionRunner(db, replace(settings, retention_purge_enabled=True))

    def boom(*_args, **_kwargs):
        raise RuntimeError("库被别的进程锁着")

    monkey = pytest.MonkeyPatch()
    monkey.setattr(retention, "purge_all", boom)
    monkey.setattr(runner, "interval_s", 0.01)

    async def scenario():
        await runner.start()
        for _ in range(100):
            await asyncio.sleep(0.01)
            if runner.errors >= 2:
                break
        await runner.stop()
        return runner.status()

    try:
        status = asyncio.run(scenario())
        assert status["errors"] >= 2, "失败要计数，而且下一轮还得再试"
        assert status["last_error"], "累计数说不了「现在还坏着吗」，这一条才说得出"
        assert status["runs"] == 0, "没跑成的一轮不得记成「跑过」"
    finally:
        monkey.undo()


def test_the_app_starts_the_purge_loop_on_its_own(tmp_path):
    """lifespan 真的把清理起来了：判据是"没人点、也没重启，过期行自己会没"。

    这比"runner 存在"强一档：对象挂在 `app.state` 上而没人 `await start()`，前面每一条
    用例都照样绿。节拍调到 50 ms 是为了让等待有界，生产默认仍是每小时一次。
    """
    path = tmp_path / "app.sqlite3"
    assert migrate.run_migrations(path).ok
    app_settings = Settings(
        db_path=path,
        dialer_enabled=False,
        retention_purge_enabled=True,
        retention_purge_interval_s=0.05,
    )
    with TestClient(create_app(app_settings)) as http:
        site = http.post("/api/sites", json={"name": "主场", "slug": "main"}).json()
        relay = http.post("/api/relays", json={"site_id": site["site_id"], "name": "塔"}).json()
        gateway = http.post(
            "/api/gateways", json={"relay_id": relay["relay_id"], "name": "pi-1"}
        ).json()
        with http.app.state.db.scope() as session:
            session.add(
                SignalEvent(
                    ts=utcnow() - timedelta(days=30),
                    site_id=site["site_id"],
                    gateway_id=gateway["gateway_id"],
                    kind="node_online",
                    source="real",
                )
            )
        # 等的是"某一轮真的看见了这个场地并且删了那一条"，不是 `runs>=1`：
        # 启动的第一轮很可能跑在建场之前，那时 `last_sites` 还是空的。
        deadline = time.monotonic() + 5.0
        row = None
        section: dict = {}
        while time.monotonic() < deadline:
            section = http.get("/api/health").json()["retention"]
            row = next(
                (item for item in section["last_sites"]
                 if item["site_id"] == site["site_id"]),
                None,
            )
            if row and row["events_deleted"] == 1:
                break
            time.sleep(0.02)
        assert row is not None, "5 s 内没有一轮清理看见这个场地：节拍或 lifespan 坏了"
        assert section["enabled"] is True and section["runs"] >= 1, "起了进程就得自己跑轮次"
        assert section["problem"] is None, "跑成了就不该有失败结论"
        assert (row["events_days"], row["events_source"]) == (7, "default")
        assert row["summaries_days"] == 30
    with Database(path).scope() as session:
        assert session.execute(
            select(func.count(SignalEvent.event_id))
        ).scalar() == 0, "30 天前的一条在默认 7 天之外：它现在应该在库外"
