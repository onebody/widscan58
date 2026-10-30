"""`/api/alert-rules` 与 `/api/alerts`：告警规则的写侧与告警候选的读/处置侧。

规则写侧的三条口径：

- **聚合器一律 405**（`require_writable_session`）：建规则是改这台实例的行为，汇聚实例
  只看别人不改任何东西——与场地/布局/插件配置同一道闸。
- **校验落服务层**（`alerts.validate_rule_payload`）：`scope_id` 多态不建外键是故意的，
  但"指向存在的东西"不能因此免掉；错误一律 `{"reason": …}`，页面按 reason 分支。
- **去重窗口的缺省来自场地 `alert_defaults.dedup_window_s`**（`alert_defaults` 的第一个
  真实读者）：请求里没带就落场地的生效值，两头都没有才是 NULL（永不新开）。

告警读侧与 `/api/events` 同一份口径：`?site=` 缺省=当前场地（`site_or_current`）、窗口与
行数上限走 `history`、详情带证据链与局限说明。处置（ack/silence/resolve）是写侧：aggregator
405；状态迁移从严——ack 只认 open，silence 认 open/ack，resolve 是唯一终点；silence 不给
时长时落到场地 `alert_defaults.silence_s`（第二个真实读者），两头都没有就 422 说要时长，
**不用代码里的兜底数**（"配置优先于代码兜底"，默认值不该藏在端点里）。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_session, require_writable_session, site_or_current
from ..models import ALERT_STATUSES, Alert, AlertRule, Site, to_utc, utcnow
from ..services import alerts as svc
from ..services import history
from ..services.hierarchy import conflict, get_or_404, not_found, unprocessable

rules_router = APIRouter(prefix="/api/alert-rules", tags=["alerts"])
router = APIRouter(prefix="/api/alerts", tags=["alerts"])

#: 每一条告警都逃不掉的局限：它是一条**候选**，推它的是算法判定，不是观测事实本身。
ALWAYS = (
    "这条告警由插件判定推出（不是直接观测）：先看证据链再处置",
    "读数只说明频段上的相对电平，不给距离、方位或坐标",
)


def rule_out(rule: AlertRule) -> dict:
    return {
        "rule_id": rule.rule_id,
        "name": rule.name,
        "site_id": rule.site_id,
        "plugin_id": rule.plugin_id,
        "kind": rule.kind,
        "scope": rule.scope,
        "scope_id": rule.scope_id,
        "dedup_window_s": rule.dedup_window_s,
        "enabled": rule.enabled,
        "created_at": history.iso(rule.created_at),
        "updated_at": history.iso(rule.updated_at),
    }


def _iso_or_none(moment: datetime | None) -> str | None:
    """`history.iso` 不收 None：可空时刻先在这里守一道，别让"还没静默过"变成 500。"""
    return None if moment is None else history.iso(moment)


def alert_out(alert: Alert, *, with_limitations: bool = False) -> dict:
    out = {
        "alert_id": alert.alert_id,
        "rule_id": alert.rule_id,
        "site_id": alert.site_id,
        "gateway_id": alert.gateway_id,
        "node_id": alert.node_id,
        "slot": alert.slot,
        "kind": alert.kind,
        "metric": alert.metric,
        "value": alert.value,
        "threshold": alert.threshold,
        "ts": history.iso(alert.ts),
        "last_ts": history.iso(alert.last_ts),
        "count": alert.count,
        "status": alert.status,
        "source": alert.source,
        "silenced_until": _iso_or_none(alert.silenced_until),
        "reviewed_at": _iso_or_none(alert.reviewed_at),
    }
    if with_limitations:
        out["evidence"] = _json_or_error(alert.evidence_json)
        out["review"] = _json_or_error(alert.review_json)
        out["limitations"] = _limitations(alert)
    return out


def _json_or_error(raw: str | None) -> dict:
    """证据/复核 JSON 原样解出来；坏 JSON 如实说坏，不编一个空对象。"""
    if raw is None:
        return {"value": None, "error": None}
    try:
        return {"value": json.loads(raw), "error": None}
    except (TypeError, ValueError):
        return {"value": None, "error": "malformed_json"}


def _limitations(alert: Alert) -> list[str]:
    notes = list(ALWAYS)
    if alert.source != "real":
        notes.append(f"这条告警的来源是 `{alert.source}`，不是此刻的实时判定")
    if alert.status == "silenced":
        notes.append("静默只是页面上不催了：判定还在合并计数，到点或解决之前它没被处理")
    if alert.reviewed_at is None:
        notes.append("还没有人工复核记录：ack/静默都不算复核过")
    return notes


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# --- 规则 CRUD ---------------------------------------------------------------


@rules_router.get("")
def list_rules(
    session: Session = Depends(get_session),
    site: int | None = Query(default=None),
) -> dict:
    """规则清单。`?site=` 过滤"对这块地生效的规则"= 场地专属 + 实例级（site_id 为 NULL）；
    实例级规则对每个场地都在管事，从清单里隐掉它，"为什么这块地还在响"就没法查。"""
    query = select(AlertRule).order_by(AlertRule.rule_id)
    if site is not None:
        get_or_404(session, Site, site, "site")
        query = query.where(or_(AlertRule.site_id == site, AlertRule.site_id.is_(None)))
    rows = list(session.execute(query).scalars())
    return {"filters": {"site": site}, "rows": [rule_out(row) for row in rows]}


@rules_router.post("", status_code=201)
def create_rule(
    payload: dict,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    reason = svc.validate_rule_payload(session, payload)
    if reason is not None:
        raise unprocessable(reason, key="alert_rule")
    rule = AlertRule(
        name=payload["name"].strip(),
        site_id=payload.get("site_id"),
        plugin_id=payload["plugin_id"],
        kind=payload["kind"],
        scope=payload.get("scope", "site"),
        scope_id=payload.get("scope_id"),
        # 请求没带窗口（或显式 null）就落场地的 alert_defaults——`alert_defaults` 由此
        # 第一次真的管事，而不是注册表里一个没人读的对象。
        dedup_window_s=svc.rule_dedup_window(
            session, _settings(request), payload.get("site_id"), payload.get("dedup_window_s")
        ),
        enabled=payload.get("enabled", True),
    )
    session.add(rule)
    session.commit()
    return rule_out(rule)


@rules_router.patch("/{rule_id}")
def update_rule(
    rule_id: int,
    payload: dict,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    rule = get_or_404(session, AlertRule, rule_id, "rule")
    merged = {
        "name": rule.name,
        "site_id": rule.site_id,
        "plugin_id": rule.plugin_id,
        "kind": rule.kind,
        "scope": rule.scope,
        "scope_id": rule.scope_id,
        "dedup_window_s": rule.dedup_window_s,
        "enabled": rule.enabled,
        **{key: payload[key] for key in payload if key in _PATCHABLE},
    }
    reason = svc.validate_rule_payload(session, merged)
    if reason is not None:
        raise unprocessable(reason, key="alert_rule")
    for key in _PATCHABLE:
        if key in payload:
            setattr(rule, key, merged[key].strip() if key == "name" else merged[key])
    rule.updated_at = utcnow()
    session.commit()
    return rule_out(rule)


#: 允许改的字段。`site_id`/`plugin_id` 也可改：改完重走整条校验，不是逐字段各改各的。
_PATCHABLE = ("name", "plugin_id", "kind", "scope", "scope_id", "dedup_window_s", "enabled")


@rules_router.delete("/{rule_id}", status_code=204)
def delete_rule(
    rule_id: int,
    session: Session = Depends(require_writable_session),
) -> None:
    rule = get_or_404(session, AlertRule, rule_id, "rule")
    in_use = session.scalar(select(Alert.alert_id).where(Alert.rule_id == rule_id).limit(1))
    if in_use is not None:
        # 告警行 RESTRICT 挂在规则上：有告警在就删不动——删了规则，那些告警的
        # "为什么响"就永远说不清了。先解决/删告警，再来删规则。409 与层级删除阻塞同口径。
        raise conflict(
            "rule_in_use", rule_id=rule_id, hint="这条规则已有告警挂着：先解决或删掉那些告警"
        )
    session.delete(rule)
    session.commit()


# --- 告警的读侧与处置 --------------------------------------------------------


@router.get("")
def list_alerts(
    session: Session = Depends(get_session),
    site: int = Depends(site_or_current),
    status: str | None = Query(default=None),
    rule: int | None = Query(default=None),
    node: int | None = Query(default=None),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=history.DEFAULT_ROWS),
) -> dict:
    """这一个场地、这一段窗口里的告警候选，按最近一次命中倒序回。"""
    get_or_404(session, Site, site, "site")
    if status is not None and status not in ALERT_STATUSES:
        raise unprocessable("unknown_status", status=status, choices=list(ALERT_STATUSES))
    frm, to, span = history.window(start, end)
    capped = history.row_limit(limit)

    query = (
        select(Alert)
        .where(
            Alert.site_id == site,
            # 窗口按 last_ts（最近一次命中）比：合并语义下 ts 是"第一次响"，列表排序与
            # "最近出没"都该看 last_ts——与 events 按 ts 的取法不同是有意的。
            Alert.last_ts >= datetime.fromtimestamp(frm, timezone.utc),
            Alert.last_ts < datetime.fromtimestamp(to + 1, timezone.utc),
        )
        .order_by(Alert.last_ts.desc(), Alert.alert_id.desc())
        .limit(capped + 1)
    )
    if status is not None:
        query = query.where(Alert.status == status)
    if rule is not None:
        query = query.where(Alert.rule_id == rule)
    if node is not None:
        history.require_node_in_site(session, site, node)
        query = query.where(Alert.node_id == node)

    rows = list(session.execute(query).scalars())
    truncated = len(rows) > capped
    rows = rows[:capped]
    return {
        "site_id": site,
        "range": {"from": frm, "to": to, "span_s": span},
        "filters": {"status": status, "rule": rule, "node": node},
        "limit": capped,
        "truncated": truncated,
        "sources": sorted({row.source for row in rows}),
        "rows": [alert_out(row) for row in rows],
    }


@router.get("/{alert_id}")
def read_alert(
    alert_id: int,
    session: Session = Depends(get_session),
    site: int | None = Query(default=None),
) -> dict:
    """一条告警的全部上下文：证据链（拷贝）+ 复核记录 + 局限说明。"""
    alert = get_or_404(session, Alert, alert_id, "alert")
    if site is not None and alert.site_id != site:
        raise not_found("alert", alert_id)
    return alert_out(alert, with_limitations=True)


def _transition(
    alert: Alert, allowed: tuple[str, ...], target: str
) -> None:
    """状态机从严：不在允许集里就 409。页面按 reason 画按钮，乱序点击不该静默成功。"""
    if alert.status not in allowed:
        raise unprocessable(
            "wrong_status",
            alert_id=alert.alert_id,
            current=alert.status,
            allowed=list(allowed),
        )
    alert.status = target


@router.post("/{alert_id}/ack")
def ack_alert(
    alert_id: int,
    session: Session = Depends(require_writable_session),
) -> dict:
    """open → ack：有人看见了。ack 不动复核列（"看见了"不是"复核过"）。"""
    alert = get_or_404(session, Alert, alert_id, "alert")
    _transition(alert, ("open",), "ack")
    session.commit()
    return alert_out(alert)


@router.post("/{alert_id}/silence")
def silence_alert(
    alert_id: int,
    request: Request,
    payload: dict | None = None,
    session: Session = Depends(require_writable_session),
) -> dict:
    """open/ack → silenced：到 `silenced_until` 为止页面上不催。

    时长的三段取值：请求带 `seconds` 就用请求的；没带落到场地 `alert_defaults.silence_s`
    （它的第二个真实读者）；两头都没有就 422——默认时长不该藏在代码里，要让运营者在
    设置页上看见自己改的是哪个数。
    """
    alert = get_or_404(session, Alert, alert_id, "alert")
    body = payload or {}
    seconds = body.get("seconds")
    raw_until = body.get("until")
    now = to_utc(utcnow())
    if raw_until is not None:
        # 原始 dict body 里的 until 是 ISO 字符串，自己解；解析不动或没有时区都拒——
        # naive 的"截止时刻"在跨时区页面上就是把 8 小时说成 0。
        if not isinstance(raw_until, str):
            raise unprocessable("wrong_type", detail="until 必须是 ISO 8601 时刻字符串")
        try:
            parsed = datetime.fromisoformat(raw_until.replace("Z", "+00:00"))
        except ValueError:
            raise unprocessable("wrong_type", detail="until 解析不了 ISO 8601") from None
        if parsed.tzinfo is None:
            raise unprocessable("wrong_type", detail="until 必须带时区偏移")
        end = to_utc(parsed)
        if end <= now:
            raise unprocessable("out_of_range", detail="until 必须在未来")
    else:
        if seconds is None:
            defaults = svc.site_alert_defaults(session, _settings(request), alert.site_id)
            seconds = defaults.get("silence_s")
        if seconds is None:
            raise unprocessable(
                "silence_duration_required",
                alert_id=alert_id,
                hint="请求带 seconds/until，或先在场地设置里配 alert_defaults.silence_s",
            )
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or seconds <= 0
        ):
            raise unprocessable("out_of_range", detail="seconds 必须是正数")
        end = now + timedelta(seconds=seconds)
    _transition(alert, ("open", "ack"), "silenced")
    alert.silenced_until = end
    session.commit()
    return alert_out(alert)


@router.post("/{alert_id}/resolve")
def resolve_alert(
    alert_id: int,
    payload: dict | None = None,
    session: Session = Depends(require_writable_session),
) -> dict:
    """任何未解决状态 → resolved：唯一终点。resolve 是人的决定，带 note 就落进复核列
    （§10.5 复核与推测分列：算法在 evidence，人话在 review），不带就只改状态。"""
    alert = get_or_404(session, Alert, alert_id, "alert")
    _transition(alert, ("open", "ack", "silenced"), "resolved")
    note = (payload or {}).get("note")
    if note is not None:
        if not isinstance(note, str) or len(note) > 2000:
            raise unprocessable("out_of_range", detail="note 必须是 ≤2000 字的文本")
        alert.review_json = json.dumps({"action": "resolve", "note": note}, ensure_ascii=False)
        alert.reviewed_at = utcnow()
    session.commit()
    return alert_out(alert)
