"""S6 的历史这一侧：把采样点**聚成桶**、把桶**批量写进库**、把过期的行**按场地删掉**。

四件事各自的存在理由：

1. **桶聚合**（`SummaryAggregator`）：原始 RSSI 序列只活在内存环形缓冲里（`store.py`），
   落库的只有桶摘要。SD 卡只有那一张（R11），逐点写等于把它写穿；桶宽 ≥1 s 是
   `config.py` 里写死的校验而不是偏好。
2. **幂等的批量写**（`upsert_summaries`）：主键是 `(node_id, slot, ts_bucket)`，所以
   "同一个桶再刷一次"必须**合并**而不是撞唯一约束、也不是多长一行。合并要能算准均值，
   就得带上分母——这就是 `sample_count` / `ema_count` 两列的用途（见 models 的注释）。
   一条语句 executemany，而不是循环 `session.add()`：后者在 10 万行时会退化成 10 万次事务边界。
3. **按场地保留**（`purge_all`）：`retention_*` 是 `global`+`site` 两层的键，取值一律走
   `config_layer.effective_for_site`——"哪个值最终生效"只许有一份算法，清理任务不是例外。
   逐场地删（不是一个 `WHERE ts<…` 扫全库）才谈得上"某个场地要留更久就只改它自己"。
4. **时间口径**：比较与删除都在 UTC 墙上时间上做。SQLite 的 `DATETIME(timezone=True)`
   **不存偏移、也不换算**（`to_utc` 的 docstring 里是实测结果），所以 naive 读回来的值
   必须显式当 UTC 看，否则"滞后 X 秒"会算成"滞后 8 小时"。

这里没有"清理日志表"：`retention_log_rows` 对应的那张表还没到（事件流之外没有日志表），
为一个不存在的表写删除逻辑就是编造。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import Database
from ..models import SampleSummary, SignalEvent, Site, to_utc, utcnow
from . import config_layer

#: 结构化日志的落点。`logging` 在本仓库是第一次使用（之前只有 alembic 的 env），
#: 但它比"只把结果留在内存里"多满足一件事：进程重启后，上一轮清理删了多少仍然查得到。
LOG = logging.getLogger("rftrack.retention")

#: 一次 flush 周期里最多攒多少个桶。512 条序列（8×8×8）× 每 5 s 一刷 ≈ 2500 格，
#: 4096 是"再翻一倍"的天花板；到顶之后 `add()` 回 `dropped_full` 并计数，
#: 与 `GatewayQueue` 同一条纪律：**有界、丢多少说得出来、绝不阻塞生产者**。
DEFAULT_MAX_BUCKETS = 4096

TABLE = SampleSummary.__table__

#: `add()` 的三种结局。调用方要能把它们报成数字，不是报成一句"正常"。
ADDED = "added"
DROPPED_FULL = "dropped_full"
SKIPPED_EMPTY = "skipped_empty"


def bucket_start(ts: datetime, bucket_s: int) -> int:
    """把时刻折成桶起点的 unix 秒：`floor(epoch / bucket_s) * bucket_s`。

    `to_utc()` 不是装饰：`datetime.timestamp()` 对 **naive** 值按*本地时区*解释，
    一块跑在 UTC+8 的机器上会把同一个点算进不同的桶。
    """
    if bucket_s < 1:
        raise ValueError(f"桶宽至少 1 s，收到 {bucket_s}")
    epoch = int(to_utc(ts).timestamp())
    return epoch - epoch % bucket_s


@dataclass
class _Bucket:
    """一个 `(node_id, slot, ts_bucket)` 格子的增量状态。

    存 `sum` 而不是 `mean`：合入新点时均值要按点数重算，先除再乘只会把误差放大一遍。
    """

    site_id: int
    gateway_id: int
    bucket_s: int
    source: str
    sample_count: int = 0
    ema_count: int = 0
    rssi_sum: float = 0.0
    rssi_min: float | None = None
    rssi_max: float | None = None
    rssi_last: float | None = None
    ema_sum: float = 0.0
    ema_min: float | None = None
    ema_max: float | None = None
    ema_last: float | None = None
    last_at: datetime | None = None

    def add(self, ts: datetime, rssi: float | None, ema: float | None) -> None:
        if rssi is not None:
            self.sample_count += 1
            self.rssi_sum += rssi
            self.rssi_min = rssi if self.rssi_min is None else min(self.rssi_min, rssi)
            self.rssi_max = rssi if self.rssi_max is None else max(self.rssi_max, rssi)
            self.rssi_last = rssi
        if ema is not None:
            self.ema_count += 1
            self.ema_sum += ema
            self.ema_min = ema if self.ema_min is None else min(self.ema_min, ema)
            self.ema_max = ema if self.ema_max is None else max(self.ema_max, ema)
            self.ema_last = ema
        self.last_at = ts if self.last_at is None or ts > self.last_at else self.last_at

    def as_row(self, node_id: int, slot: int, ts_bucket: int) -> dict[str, Any]:
        """出仓成"能直接 executemany 的一行"。

        `*_last` 取的是桶内最后一个**带该量**的读数：RSSI 模式那一格 `ema_last` 必须是
        NULL——用 `rssi_last` 顶上去就是"把没测过的量画成测过"（规范 §2.3）。
        """
        return {
            "node_id": node_id,
            "slot": slot,
            "ts_bucket": ts_bucket,
            "site_id": self.site_id,
            "gateway_id": self.gateway_id,
            "bucket_s": self.bucket_s,
            "sample_count": self.sample_count,
            "ema_count": self.ema_count,
            "rssi_mean": _mean(self.rssi_sum, self.sample_count),
            "rssi_min": self.rssi_min,
            "rssi_max": self.rssi_max,
            "rssi_last": self.rssi_last,
            "ema_mean": _mean(self.ema_sum, self.ema_count),
            "ema_min": self.ema_min,
            "ema_max": self.ema_max,
            "ema_last": self.ema_last,
            "source": self.source,
            "last_at": self.last_at,
        }


def _mean(total: float, count: int) -> float | None:
    return None if count == 0 else total / count


class SummaryAggregator:
    """`(node_id, slot, ts_bucket)` → 增量统计的有界累加器。"""

    def __init__(self, bucket_s: int, max_buckets: int = DEFAULT_MAX_BUCKETS) -> None:
        self.bucket_s = bucket_s
        self.max_buckets = max_buckets
        self._buckets: dict[tuple[int, int, int], _Bucket] = {}
        self.added = 0
        self.dropped_full = 0
        self.skipped_empty = 0

    @classmethod
    def from_settings(cls, settings: Settings, bucket_s: int | None = None) -> "SummaryAggregator":
        return cls(
            bucket_s if bucket_s is not None else settings.summary_bucket_s,
        )

    @property
    def pending(self) -> int:
        return len(self._buckets)

    @property
    def needs_flush(self) -> bool:
        """该不该现在就刷：到硬上限就必须刷，否则下一格开始要丢数据。"""
        return self.pending >= self.max_buckets

    def add(
        self,
        *,
        site_id: int,
        gateway_id: int,
        node_id: int,
        slot: int,
        ts: datetime,
        rssi: float | None,
        ema: float | None,
        source: str = "real",
    ) -> str:
        moment = to_utc(ts)
        if rssi is None and ema is None:
            # 一个量都没有的"点"不配占一格：占了就等于在库里留一行"有观测但没观测值"。
            self.skipped_empty += 1
            return SKIPPED_EMPTY
        key = (node_id, slot, bucket_start(moment, self.bucket_s))
        bucket = self._buckets.get(key)
        if bucket is None:
            if len(self._buckets) >= self.max_buckets:
                self.dropped_full += 1
                return DROPPED_FULL
            self._buckets[key] = bucket = _Bucket(
                site_id=site_id, gateway_id=gateway_id, bucket_s=self.bucket_s, source=source
            )
        bucket.add(moment, rssi, ema)
        # 后写进来的来源不能把"这是回放数据"这句话洗掉：`mock` 一旦遇上 `real` 仍标 mock，
        # 读侧宁可多打一个 MOCK 角标，也不能把回放数当实时数看。
        if source == "mock":
            bucket.source = "mock"
        self.added += 1
        return ADDED

    def drain(self) -> list[dict[str, Any]]:
        """取出并清空全部格子。刷完还可以再攒同一个键——合并交给 `upsert_summaries`。"""
        rows = [
            bucket.as_row(node_id, slot, ts_bucket)
            for (node_id, slot, ts_bucket), bucket in self._buckets.items()
        ]
        self._buckets.clear()
        return rows

    def stats(self) -> dict[str, Any]:
        return {
            "bucket_s": self.bucket_s,
            "pending": self.pending,
            "max_buckets": self.max_buckets,
            "added": self.added,
            "dropped_full": self.dropped_full,
            "skipped_empty": self.skipped_empty,
        }


def _merge_mean(current, current_count, incoming, incoming_count):
    """两侧都能缺 EMA 的加权合并：`NULL` 那侧直接让位，不是当成 0 参与平均。"""
    return case(
        (current.is_(None), incoming),
        (incoming.is_(None), current),
        else_=(current * current_count + incoming * incoming_count)
        / (current_count + incoming_count),
    )


def _merge_extreme(kind, current, incoming):
    """min/max 合并。**不能**直接用 `max(a, b)`：SQLite 的两参标量函数遇到 NULL 就回 NULL，
    于是"老行没测 EMA、这次测了"会被合并成 NULL——把测到的那个数弄丢了。

    `*_last` 的"谁的更晚"用的是同一批数据里的 `last_at`：桶到上限强制刷、进程重启前的
    最后一刷、回放追赶历史都会让同一个键被后写一次，那时"最后一个读数"必须还是最后一个，
    而不是"最后写的那批里最后一个"。
    """
    return func.coalesce(kind(current, incoming), current, incoming)


def upsert_summaries(session: Session, rows: list[dict[str, Any]]) -> int:
    """一批格子 → 一条 `INSERT … ON CONFLICT DO UPDATE`。返回**送进去的行数**。

    重放同一个桶不会长出新行（幂等），这是 §3 那条"`(node_id,slot,ts_bucket)` 唯一"的实建形态。
    不开事务：调用方（`database.scope()` / 采集层的 flush 任务）决定这一批是否与其他写一起提交。
    """
    if not rows:
        return 0
    stmt = sqlite_insert(SampleSummary)
    ex = stmt.excluded
    # 加权合并要用到**更新前**的 `sample_count` / `ema_count` / `last_at`。SQLite 对
    # `DO UPDATE SET` 的每个表达式都按那一行的原值求值（实测 3.53.1，两种书写顺序结果一致），
    # 所以下面这个字典的键序不承载语义——别把它当成"计数列必须最后写"的约定。
    updates = {
        "sample_count": TABLE.c.sample_count + ex.sample_count,
        "ema_count": TABLE.c.ema_count + ex.ema_count,
        "bucket_s": ex.bucket_s,
        "source": ex.source,
        "rssi_mean": _merge_mean(
            TABLE.c.rssi_mean, TABLE.c.sample_count, ex.rssi_mean, ex.sample_count
        ),
        "rssi_min": _merge_extreme(func.min, TABLE.c.rssi_min, ex.rssi_min),
        "rssi_max": _merge_extreme(func.max, TABLE.c.rssi_max, ex.rssi_max),
        "rssi_last": case((TABLE.c.last_at <= ex.last_at, ex.rssi_last), else_=TABLE.c.rssi_last),
        "ema_mean": _merge_mean(
            TABLE.c.ema_mean, TABLE.c.ema_count, ex.ema_mean, ex.ema_count
        ),
        "ema_min": _merge_extreme(func.min, TABLE.c.ema_min, ex.ema_min),
        "ema_max": _merge_extreme(func.max, TABLE.c.ema_max, ex.ema_max),
        "ema_last": case((TABLE.c.last_at <= ex.last_at, ex.ema_last), else_=TABLE.c.ema_last),
        "last_at": func.max(TABLE.c.last_at, ex.last_at),
    }
    session.execute(
        stmt.on_conflict_do_update(
            index_elements=[TABLE.c.node_id, TABLE.c.slot, TABLE.c.ts_bucket],
            set_=updates,
        ),
        rows,
    )
    return len(rows)


# --- 保留清理 ---------------------------------------------------------------


@dataclass(frozen=True)
class SiteRetention:
    """一个场地的这一轮清理结果。`*_source` 说的是那个天数从哪一层来（规范 §4.2 的来源链）。"""

    site_id: int
    name: str
    events_days: int
    summaries_days: int
    events_source: str
    summaries_source: str
    events_deleted: int
    summaries_deleted: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "site_id": self.site_id,
            "name": self.name,
            "events_days": self.events_days,
            "summaries_days": self.summaries_days,
            "events_source": self.events_source,
            "summaries_source": self.summaries_source,
            "events_deleted": self.events_deleted,
            "summaries_deleted": self.summaries_deleted,
        }


def retention_for(settings: Settings, session: Session, site: Site) -> dict[str, tuple[int, str]]:
    """这个场地实际用的两个天数 + 各自的来源层。

    走 `effective_for_site` 而不是自己读列：`内置默认 < 全局 < 场地` 那条优先级只有一个
    权威实现，清理任务绕过它就等于给同一个键发了第二份真相（将来必然对不上）。
    这里也不再校验"天数不能为负"：负数在那条链上已经被 `config_layer.validate` 拒掉并回落
    到下一层，能到这里的天数一定 ≥0（`tests/test_retention.py` 钉住这条接力）。
    """
    effective = config_layer.effective_for_site(session, settings, site)

    def pick(name: str) -> tuple[int, str]:
        entry = effective.get(name) or {}
        return int(entry["value"]), entry.get("source", "default")

    return {
        "events_days": pick("retention_events_days"),
        "summaries_days": pick("retention_summary_days"),
    }


def purge_site(
    settings: Settings, session: Session, site: Site, *, now: datetime | None = None
) -> SiteRetention:
    """删掉这一个场地超出保留期的事件与摘要，并把删了多少带回去。

    两条 DELETE 都带 `site_id`，走的是 `(site_id, ts)` / `(site_id, node_id, slot, ts_bucket)`
    两个索引的前缀——"按场地覆写保留"如果最后是一条全表扫的 `WHERE ts<…`，那它就不是按场地。
    """
    moment = to_utc(now or utcnow())
    limits = retention_for(settings, session, site)
    events_days, events_source = limits["events_days"]
    summaries_days, summaries_source = limits["summaries_days"]
    events_cutoff = moment - timedelta(days=events_days)
    summaries_cutoff = moment - timedelta(days=summaries_days)

    events_deleted = session.execute(
        delete(SignalEvent).where(
            SignalEvent.site_id == site.site_id, SignalEvent.ts < events_cutoff
        )
    ).rowcount
    summaries_deleted = session.execute(
        delete(SampleSummary).where(
            SampleSummary.site_id == site.site_id,
            SampleSummary.ts_bucket < bucket_start(summaries_cutoff, settings.summary_bucket_s),
        )
    ).rowcount
    return SiteRetention(
        site_id=site.site_id,
        name=site.name,
        events_days=events_days,
        summaries_days=summaries_days,
        events_source=events_source,
        summaries_source=summaries_source,
        events_deleted=events_deleted or 0,
        summaries_deleted=summaries_deleted or 0,
    )


def purge_all(
    settings: Settings, session: Session, *, now: datetime | None = None
) -> list[SiteRetention]:
    """逐场地清理，每个场地一条结构化日志。

    `archived` 的场地**也清**：归档是"不再拨号"，不是"把它的观测永久留在 SD 卡上"。
    真要长期留档属于 §7 S9 的归档导出，那是导出而不是不删。
    """
    moment = to_utc(now or utcnow())
    reports: list[SiteRetention] = []
    for site in session.execute(select(Site).order_by(Site.site_id)).scalars():
        report = purge_site(settings, session, site, now=moment)
        reports.append(report)
        LOG.info("retention %s", json.dumps(report.as_dict(), ensure_ascii=False, sort_keys=True))
    return reports


class RetentionRunner:
    """按节拍跑 `purge_all` 的后台任务：它只做"到点调用 + 把结果留住"这一件事。

    与 `DialCoordinator` 同一套起合方式（lifespan 里 `start()`/`stop()`），默认每小时一次。
    启动时先跑一次：树莓派重启是常态，"重启后库里还压着三个月前的行"不是常态。
    """

    def __init__(self, db: Database, settings: Settings, *, log=LOG) -> None:
        self.db = db
        self.settings = settings
        self.log = log
        self.interval_s = settings.retention_purge_interval_s
        self.runs = 0
        self.errors = 0
        #: 上一轮的错误：`errors` 是累计数，只会越来越大，说不了"现在还坏着吗"；
        #: 健康检查要的是后者，所以成功一轮就把它清空。
        self.last_error: str | None = None
        self.last_run_at: datetime | None = None
        self.last_sites: list[dict[str, Any]] = []
        self._task = None
        self._stopping: "asyncio.Event | None" = None

    @property
    def enabled(self) -> bool:
        return self.settings.retention_purge_enabled

    def run_once(self, *, now: datetime | None = None) -> list[SiteRetention]:
        """同步的一次清理：测试与手动触发都走这里，不经事件循环。

        成功才把计数与 `last_*` 写下去：一轮炸在 `purge_all` 里时，`runs` 不得 +1——
        否则"跑过 N 轮"这句话会把失败的那几轮也算进交付。
        """
        with self.db.scope() as session:
            reports = purge_all(self.settings, session, now=now)
        self.runs += 1
        self.last_error = None
        self.last_run_at = utcnow()
        self.last_sites = [report.as_dict() for report in reports]
        return reports

    async def start(self) -> None:
        if not self.enabled:
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                # 清理是同步 SQL，放到线程里跑：不能在事件循环里删 10 万行，
                # 那会让 /ws/live 的扇出跟着停半秒。
                await asyncio.get_running_loop().run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮清理失败绝不终止循环
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("retention purge failed: %s", self.last_error)
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.interval_s)
            except asyncio.TimeoutError:
                continue

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def problem(self) -> str | None:
        """只在"上一轮真的失败"时说话。

        没启用不是故障（`RFTRACK_RETENTION_PURGE_ENABLED=0` 是合法部署形态，`status` 里
        已经写了 enabled=false）；累计 `errors` 也不是——它只会涨，说不了"现在坏不坏"。
        """
        if self.last_error is None:
            return None
        return f"上一轮保留清理失败：{self.last_error}（累计 {self.errors} 轮失败）"

    def status(self) -> dict[str, Any]:
        """给 `/api/health` 的那一节。没启用就说没启用，不给一个"看起来在跑"的空壳。"""
        return {
            "enabled": self.enabled,
            "interval_s": self.interval_s,
            "runs": self.runs,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "last_sites": self.last_sites,
            "problem": self.problem(),
        }
