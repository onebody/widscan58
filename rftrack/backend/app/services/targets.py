"""S8 的目标这一侧：把**已落库的判定**（`plugin_finding`）按观测身份聚成**候选目标**
（`candidate_target` + `track_observation`）。

三条边界在本文件落死：

1. **归组规则只有一条，且是声明过的**：同一条判定流 = `(场地, 判定种类, 接入机, 节点,
   槽位)` 五元组相同、且与该目标最近一次观测相差不超过 `MERGE_WINDOW_S`。它不做跨节点
   关联（那需要"这几路是同一个东西"的判据，v1 没有这个判据就不装懂）、不做任何定位
   推断——候选目标在这里只是"同一观测身份上持续出现的判定"的时间线容器。
2. **置信度是声明公式的输出**：`confidence = min(1.0, 0.2 + 0.1 × 关联判定数)`，
   `confidence_basis` 一并写入——两者必须成对出现（写入侧校验），没有算法出处的
   装饰性数字正是任务书 §10.5 禁的。它不是测量精度，页面必须当"这条时间线有多长"读。
3. **证据是拷贝，时间轴以 min/max 为准**。观测行写边时从判定行抄 `evidence_json`
   （判定行会被保留任务清掉，`finding_id` 置 NULL 后这条边还得能讲清楚为什么）；回放
   乱序回填时 `first_seen_at` 取 min、`last_seen_at` 取 max，不假装时间是有序来的。

`TargetEngine` 与 `AlertEngine` 同族：增量游标、冷启动跳到当前最大 `finding_id`
（重启不重放历史，同一条申报边界）、`run_once` 同步可测、两本账、`problem()` 只在
"上一轮真的失败"时说话。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import Database
from ..models import CandidateTarget, PluginFinding, TrackObservation, to_utc, utcnow

LOG = logging.getLogger("rftrack.targets")

#: 一轮最多处理多少条新判定。与 `alerts.TICK_FINDING_LIMIT` 同一个数、同一条理由。
TICK_FINDING_LIMIT = 500

#: 同一观测身份两次判定相隔多久算"新的一个目标"。300 s 是拍板值：插件的抑制窗与
#: 去重窗都以秒计，5 分钟的空档足够把"持续的一件事"和"又来了的一件事"分开。
MERGE_WINDOW_S = 300

#: `producer` 的形状：与 `plugin_finding.producer` 的 `rule:<id>@<version>` 同一族，
#: 前缀换成 `engine:`——写目标的是这台实例自己的引擎，不是哪一支插件。
PRODUCER = "engine:target-associator@1"

#: 置信度公式的两个系数：0.2 + 0.1×关联判定数 —— 有一条判定就算候选（0.3），
#: 每多一条 +0.1，8 条封顶 1.0。
CONFIDENCE_BASE = 0.2
CONFIDENCE_STEP = 0.1


def confidence_for(count: int) -> tuple[float, str]:
    """声明公式的输出：值与"这值怎么来的"必须一起走，谁也不许单飞。"""
    value = min(1.0, CONFIDENCE_BASE + CONFIDENCE_STEP * count)
    basis = f"{CONFIDENCE_BASE} + {CONFIDENCE_STEP}×关联判定数（当前 {count}），封顶 1.0"
    return round(value, 2), basis


def coverage_for(count: int) -> dict:
    """数据覆盖与未知项：写进 `coverage_json` 的就是目标"能讲到哪一步"。"""
    return {
        "observations": count,
        "merge_window_s": MERGE_WINDOW_S,
        "unknowns": [
            "目标位置未知：RSSI 读数推不出距离、方位或坐标，本表不生成任何坐标",
        ],
    }


@dataclass
class ApplyReport:
    """一轮里每个动作各发生了几次。`created` = 开新目标，`joined` = 并进已有目标。"""

    created: int = 0
    joined: int = 0


def apply_finding(session: Session, finding: PluginFinding) -> str:
    """一条判定：并进同身份且还"开着"的目标，或开新目标。回 "created"/"joined"。

    "开着"的判据：该身份最近的一个目标的 `last_seen_at` 距这条判定不超过
    `MERGE_WINDOW_S`（或判定时刻早于它——乱序回填仍算同一件事，时间轴靠 min/max 说真话）。
    """
    # 判定行从库里读回的时刻是 naive（UTC 墙上时间），先贴回 UTC 再参与一切比较。
    ts = to_utc(finding.ts)

    # 身份列在观测行上（candidate_target 不带 gateway/node/slot）：同身份的目标 = 至少
    # 一条观测的五元组对得上，而一个目标里的观测永远同身份（归组键含它们），所以
    # EXISTS 一条即可。取 last_seen_at 最近的一个——目标只和"还开着"的那一个比较。
    existing = session.execute(
        select(CandidateTarget)
        .where(
            CandidateTarget.site_id == finding.site_id,
            CandidateTarget.kind == finding.kind,
            CandidateTarget.target_id.in_(
                select(TrackObservation.target_id).where(
                    TrackObservation.gateway_id == finding.gateway_id,
                    TrackObservation.node_id == finding.node_id,
                    TrackObservation.slot == finding.slot,
                )
            ),
        )
        .order_by(CandidateTarget.last_seen_at.desc(), CandidateTarget.target_id.desc())
        .limit(1)
    ).scalar_one_or_none()

    open_target: CandidateTarget | None = None
    if existing is not None:
        gap = (ts - to_utc(existing.last_seen_at)).total_seconds()
        if gap <= MERGE_WINDOW_S:
            open_target = existing

    obs = TrackObservation(
        target_id=open_target.target_id if open_target else 0,  # 占位：flush 后回填
        finding_id=finding.finding_id,
        site_id=finding.site_id,
        gateway_id=finding.gateway_id,
        node_id=finding.node_id,
        slot=finding.slot,
        ts=ts,
        metric=finding.metric,
        value=finding.value,
        source=finding.source,
        # 证据是**拷贝**不是引用：判定行清掉之后，这条边还得自己讲得清。
        evidence_json=finding.evidence_json,
    )
    if open_target is None:
        target = CandidateTarget(
            site_id=finding.site_id,
            label=None,
            kind=finding.kind,
            producer=PRODUCER,
            review_status="unreviewed",
            first_seen_at=ts,
            last_seen_at=ts,
        )
        session.add(target)
        session.flush()
        obs.target_id = target.target_id
        session.add(obs)
        _refresh_counts(session, target, count=1)
        return "created"

    session.add(obs)
    session.flush()
    if ts < to_utc(open_target.first_seen_at):
        open_target.first_seen_at = ts
    if ts > to_utc(open_target.last_seen_at):
        open_target.last_seen_at = ts
    open_target.updated_at = utcnow()
    _refresh_counts(session, open_target)
    return "joined"


def _refresh_counts(
    session: Session, target: CandidateTarget, *, count: int | None = None
) -> None:
    """重算置信度与覆盖对象。count 不传就现查——写入侧保证 `confidence` 与
    `confidence_basis` 永远成对更新，不给"有值没出处"留路径。"""
    if count is None:
        count = session.scalar(
            select(func.count(TrackObservation.obs_id)).where(
                TrackObservation.target_id == target.target_id
            )
        )
    target.confidence, target.confidence_basis = confidence_for(count)
    target.coverage_json = json.dumps(coverage_for(count), ensure_ascii=False)


# --- 引擎 -------------------------------------------------------------------


class TargetEngine:
    """按节拍把新判定聚成候选目标的后台循环：与 `AlertEngine` 同一套起合与记账。

    游标只活在内存里：冷启动跳到"当前最大 finding_id"是**申报过的边界**（重启不重放
    历史），与 `AlertEngine` 同一句话、同一个理由。
    """

    def __init__(self, db: Database, settings: Settings, *, log=LOG) -> None:
        self.db = db
        self.settings = settings
        self.log = log
        self.interval_s = settings.target_interval_s
        self.runs = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_run_at: datetime | None = None
        self.cursor: int | None = None
        self.findings_seen = 0
        self.report = ApplyReport()
        self.last_report: dict[str, Any] | None = None
        self._task = None
        self._stopping: "asyncio.Event | None" = None

    @property
    def enabled(self) -> bool:
        return self.settings.target_engine_enabled

    def run_once(self, *, now: datetime | None = None) -> ApplyReport:
        """同步的一轮：测试与手动触发走这里，不经事件循环。"""
        del now  # 目标没有"按墙钟收口"的动作，时刻全取判定行自己的 ts
        with self.db.scope() as session:
            if self.cursor is None:
                self.cursor = session.scalar(select(func.max(PluginFinding.finding_id))) or 0
                self.last_run_at = utcnow()
                self.last_report = {"skipped": "cold_start", "findings": 0}
                return ApplyReport()
            findings = list(
                session.execute(
                    select(PluginFinding)
                    .where(PluginFinding.finding_id > self.cursor)
                    .order_by(PluginFinding.finding_id)
                    .limit(TICK_FINDING_LIMIT + 1)
                ).scalars()
            )
            more = len(findings) > TICK_FINDING_LIMIT
            findings = findings[:TICK_FINDING_LIMIT]
            report = ApplyReport()
            for finding in findings:
                outcome = apply_finding(session, finding)
                if outcome == "created":
                    report.created += 1
                else:
                    report.joined += 1
            if findings:
                self.cursor = findings[-1].finding_id
        self.runs += 1
        self.last_error = None
        self.last_run_at = utcnow()
        self.findings_seen += len(findings)
        self.report.created += report.created
        self.report.joined += report.joined
        self.last_report = {
            "skipped": None,
            "findings": len(findings),
            "pending_more": more,
            "created": report.created,
            "joined": report.joined,
        }
        return report

    async def start(self) -> None:
        if not self.enabled:
            return
        self._stopping = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await asyncio.get_running_loop().run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("target engine tick failed: %s", self.last_error)
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
        if self.last_error is None:
            return None
        return f"上一轮目标聚合失败：{self.last_error}（累计 {self.errors} 轮失败）"

    def status(self) -> dict[str, Any]:
        """给 `/api/health` 的那一节。没启用就说没启用，不给"看起来在跑"的空壳。"""
        return {
            "enabled": self.enabled,
            "interval_s": self.interval_s,
            "runs": self.runs,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "cursor": self.cursor,
            "findings_seen": self.findings_seen,
            "created": self.report.created,
            "joined": self.report.joined,
            "last_tick": self.last_report,
            "problem": self.problem(),
        }
