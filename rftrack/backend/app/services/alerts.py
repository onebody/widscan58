"""S8 的告警这一侧：把**已落库的判定**（`plugin_finding`）按规则推成**告警候选**（`alert`）。

三条边界在本文件落死：

1. **规则不产生读数**。它消费的是插件已经写下的判定行，匹配条件只有
   `plugin_id + kind + scope`——阈值不进规则，S7 已经把每支插件的判据收进配置层
   （`config_layer` 的 `anomaly_rise_db` 等四键），规则里再放一份阈值就是两处真相。
2. **增量游标，不回放历史**。引擎按 `finding_id > 游标` 读新判定；冷启动（进程起起来）
   时游标直接跳到当前最大值——引擎重启不重放旧判定。旧判定在它写下的那一刻就该被
   当时的引擎处理掉，重启后把一小时的判定一口气推成告警，运营者看到的是一墙过期警报。
3. **去重靠部分唯一索引兜底**。同一 `(规则, 接入机, 节点, 槽位)` 在还有一条未解决告警时
   只存在一行：窗口内合并（`count` 涨、`last_ts` 推进、数值与证据取最新一次命中），
   窗口外先自动解决旧行再开新行。索引（`uq_alert_dedup`）是最后的闸，这里的逻辑是
   常态路径——判据两个方向各一条，见 `tests/test_alerts.py`。

`AlertEngine` 与 `RetentionRunner`/`PluginRuntime` 同族：`run_once` 同步可测，后台循环
在线程池里跑，两本账（累计 `errors` + 只说现在时的 `last_error`），`problem()` 只在
"上一轮真的失败"时说话。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import Database
from ..models import (
    RULE_SCOPES,
    Alert,
    AlertRule,
    Gateway,
    Node,
    PluginFinding,
    PluginRegistry,
    Site,
    to_utc,
    utcnow,
)
from . import config_layer

LOG = logging.getLogger("rftrack.alerts")

#: 一轮最多处理多少条新判定。引擎节拍 2 s，500 条是"最忙的一拍也只占库一小会儿"的界；
#: 没搬完的下一拍接着搬（游标还没推进到那里），不丢也不阻塞。
TICK_FINDING_LIMIT = 500

#: `dedup_key` 的形状：`r<规则>:g<接入机>:n<节点>:s<槽位>`，缺席的段写 `-`。
#: 160 字符的列对这些数字绰绰有余；形状稳定是给测试与页面当"这两条是不是同一件事"的判据用。
def dedup_key_for(rule: AlertRule, finding: PluginFinding) -> str:
    return "r{rule}:g{gw}:n{node}:s{slot}".format(
        rule=rule.rule_id,
        gw=finding.gateway_id if finding.gateway_id is not None else "-",
        node=finding.node_id if finding.node_id is not None else "-",
        slot=finding.slot if finding.slot is not None else "-",
    )


def matching_rules(session: Session, finding: PluginFinding) -> list[AlertRule]:
    """这条判定激活哪些规则：插件+判定种类对上，场地与作用域也都要对上。

    作用域判据与判定行自带的身份列一一对应（不 join）：`site` 看 `finding.site_id`，
    `gateway` 看 `finding.gateway_id`，`node` 看 `finding.node_id`。判定行没带 relay 身份，
    所以规则词表里也没有 relay（`RULE_SCOPES` 的注释）。
    """
    rows = session.execute(
        select(AlertRule).where(
            AlertRule.enabled.is_(True),
            AlertRule.plugin_id == finding.plugin_id,
            AlertRule.kind == finding.kind,
        )
    ).scalars()
    out: list[AlertRule] = []
    for rule in rows:
        if rule.site_id is not None and rule.site_id != finding.site_id:
            continue
        if rule.scope == "global":
            out.append(rule)
        elif rule.scope == "site":
            if rule.scope_id == finding.site_id:
                out.append(rule)
        elif rule.scope == "gateway":
            if finding.gateway_id is not None and rule.scope_id == finding.gateway_id:
                out.append(rule)
        elif rule.scope == "node":
            if finding.node_id is not None and rule.scope_id == finding.node_id:
                out.append(rule)
    return out


@dataclass
class ApplyReport:
    """一轮里每个动作各发生了几次。`merged` 与 `resolved` 是两件事：合并是窗口内，
    解决是窗口外开新行前先把旧行收掉——测试分别数。"""

    created: int = 0
    merged: int = 0
    resolved: int = 0


def apply_rule(
    session: Session, rule: AlertRule, finding: PluginFinding, *, now: datetime
) -> str:
    """一条判定 × 一条规则：合并进未解决的旧行，或开新行（必要时先收旧行）。

    回 "created"/"merged"/"resolved"，调用方计数。**窗口外的旧行必须先落 `resolved`
    并 `flush()`**：部分唯一索引只放行 `status != 'resolved'` 的键，而 SQLAlchemy 的
    默认落库顺序是先 INSERT 后 UPDATE——不 flush 就先撞索引。
    """
    key = dedup_key_for(rule, finding)
    existing = session.execute(
        select(Alert).where(Alert.dedup_key == key, Alert.status != "resolved")
    ).scalar_one_or_none()

    if existing is not None:
        window = rule.dedup_window_s
        if window is not None and finding.ts - existing.last_ts > timedelta(seconds=window):
            existing.status = "resolved"
            session.flush()
            _create_alert(session, rule, finding, key=key, now=now)
            return "resolved"
        existing.count += 1
        existing.last_ts = finding.ts
        # 一次命中覆盖一次：这一行现在是"这件事最新一次的样子"。数值列可空不写 0
        # 的纪律在这里同样成立——最新的判定没带数值，就让它如实回到 None。
        existing.metric = finding.metric
        existing.value = finding.value
        existing.threshold = finding.threshold
        existing.source = finding.source
        existing.evidence_json = finding.evidence_json
        return "merged"

    _create_alert(session, rule, finding, key=key, now=now)
    return "created"


def _create_alert(
    session: Session,
    rule: AlertRule,
    finding: PluginFinding,
    *,
    key: str,
    now: datetime,
) -> Alert:
    alert = Alert(
        rule_id=rule.rule_id,
        site_id=finding.site_id,
        gateway_id=finding.gateway_id,
        node_id=finding.node_id,
        slot=finding.slot,
        kind=finding.kind,
        metric=finding.metric,
        value=finding.value,
        threshold=finding.threshold,
        ts=finding.ts,
        last_ts=finding.ts,
        count=1,
        status="open",
        source=finding.source,
        dedup_key=key,
        # 证据是**拷贝**不是引用：判定行会被保留任务清掉，告警上的证据必须活过它。
        evidence_json=finding.evidence_json,
    )
    session.add(alert)
    session.flush()
    return alert


def site_alert_defaults(session: Session, settings: Settings, site_id: int) -> dict:
    """一个场地的 `alert_defaults` 生效对象（缺失/坏 JSON 都如实当"没设"）。"""
    site = session.get(Site, site_id)
    if site is None:
        return {}
    effective = config_layer.effective_for_site(session, settings, site)
    raw = (effective.get("alert_defaults") or {}).get("value")
    return raw if isinstance(raw, dict) else {}


def rule_dedup_window(
    session: Session, settings: Settings, site_id: int | None, requested: int | None
) -> int | None:
    """规则建下来最终用的去重窗口：请求里带了就用请求的；没带落到场地 `alert_defaults`
    （`alert_defaults` 的第一个真实读者）。都没有就是 None = 只要旧行没解决就永不新开。
    """
    if requested is not None:
        return requested
    if site_id is None:
        return None
    return site_alert_defaults(session, settings, site_id).get("dedup_window_s")


# --- 规则的写入侧校验 --------------------------------------------------------


def validate_rule_payload(session: Session, payload: dict) -> str | None:
    """建/改规则的合法性检查。回 None 合法，否则回稳定 reason（422 用）。

    校验落服务层而不是模型层：`scope_id` 多态不建外键是**故意的**（与 `plugin_registry`
    同一条裁定），但"指向存在的东西"这一步不能因此免掉——指着一个不存在的节点号的
    规则永远不响，运营者只会觉得"插件坏了"。
    """
    name = payload.get("name")
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        return "out_of_range"
    plugin_id = payload.get("plugin_id")
    if not isinstance(plugin_id, str) or not plugin_id:
        return "wrong_type"
    # 外键故意不在这条规则上（scope_id 多态同款裁定），但 plugin_id 是**普通外键**，
    # 让 IntegrityError 变 500 不如在这里说出"注册表里没有这支插件"。
    if session.get(PluginRegistry, plugin_id) is None:
        return "unknown_plugin"
    if not isinstance(payload.get("kind"), str) or not payload["kind"]:
        return "wrong_type"
    scope = payload.get("scope", "site")
    if scope == "relay":
        # 判定行没带 relay 身份（models 的 RULE_SCOPES 注释）：明确拒，而不是当成未知词。
        return "scope_unsupported"
    if scope not in RULE_SCOPES:
        return "unknown_scope"
    scope_id = payload.get("scope_id")
    if scope == "global":
        if scope_id is not None:
            return "scope_id_redundant"
    elif scope_id is None:
        return "scope_id_required"
    else:
        if not isinstance(scope_id, int) or isinstance(scope_id, bool):
            return "wrong_type"
        if scope == "site" and session.get(Site, scope_id) is None:
            return "unknown_scope_id"
        if scope == "gateway" and session.get(Gateway, scope_id) is None:
            return "unknown_scope_id"
        if scope == "node" and session.get(Node, scope_id) is None:
            return "unknown_scope_id"
    site_id = payload.get("site_id")
    if site_id is not None:
        if not isinstance(site_id, int) or isinstance(site_id, bool):
            return "wrong_type"
        if session.get(Site, site_id) is None:
            return "unknown_site"
        # 场地专属规则锁 site 作用域时只能锁自己：锁别的场地等于"A 场地的规则惊动 B 场地"，
        # 契约原话的否定面。全局规则（site_id=None）锁任何存在的场地都行。
        if scope == "site" and site_id != scope_id:
            return "scope_mismatch"
    window = payload.get("dedup_window_s")
    if window is not None and (not isinstance(window, int) or isinstance(window, bool) or window < 1):
        return "out_of_range"
    if not isinstance(payload.get("enabled", True), bool):
        return "wrong_type"
    return None


# --- 引擎 -------------------------------------------------------------------


class AlertEngine:
    """按节拍把新判定推成告警的后台循环：与 `RetentionRunner` 同一套起合与记账。

    游标只活在内存里：冷启动跳到"当前最大 finding_id"是**申报过的边界**（重启不重放
    历史），不是丢数据——判定行本身还在，丢的只是"把它们再推一遍告警"这一次机会。
    """

    def __init__(self, db: Database, settings: Settings, *, log=LOG) -> None:
        self.db = db
        self.settings = settings
        self.log = log
        self.interval_s = settings.alert_interval_s
        self.runs = 0
        self.errors = 0
        #: 与 `RetentionRunner.last_error` 同一条纪律：`errors` 只会涨，说不了"现在还坏着吗"；
        #: 成功一轮就把它清空，健康检查要的是后者。
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
        return self.settings.alert_engine_enabled

    def run_once(self, *, now: datetime | None = None) -> ApplyReport:
        """同步的一轮：测试与手动触发走这里，不经事件循环。

        冷启动先建游标再空手而归（`skipped=True` 的那一轮不是失败，是"决定从现在开始看"）。
        成功才动计数——一轮炸在半路时游标没推进，下一轮会把它没搬完的接着搬。
        """
        moment = to_utc(now or utcnow())
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
                for rule in matching_rules(session, finding):
                    outcome = apply_rule(session, rule, finding, now=moment)
                    if outcome == "created":
                        report.created += 1
                    elif outcome == "merged":
                        report.merged += 1
                    else:
                        # "resolved" = 收掉旧行**并**开了新行：两个计数都要动，
                        # 否则 created 永远数不到窗口外重开的那一半。
                        report.resolved += 1
                        report.created += 1
            if findings:
                self.cursor = findings[-1].finding_id
        self.runs += 1
        self.last_error = None
        self.last_run_at = utcnow()
        self.findings_seen += len(findings)
        self.report.created += report.created
        self.report.merged += report.merged
        self.report.resolved += report.resolved
        self.last_report = {
            "skipped": None,
            "findings": len(findings),
            "pending_more": more,
            "created": report.created,
            "merged": report.merged,
            "resolved": report.resolved,
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
                # 匹配与落告警都是同步 SQL：线程池里跑，别让 /ws/live 陪着一墙判定等。
                await asyncio.get_running_loop().run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败绝不终止循环
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("alert engine tick failed: %s", self.last_error)
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
        """只在"上一轮真的失败"时说话（与 `RetentionRunner.problem()` 同一条纪律）。"""
        if self.last_error is None:
            return None
        return f"上一轮告警规则判定失败：{self.last_error}（累计 {self.errors} 轮失败）"

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
            "merged": self.report.merged,
            "resolved": self.report.resolved,
            "last_tick": self.last_report,
            "problem": self.problem(),
        }
