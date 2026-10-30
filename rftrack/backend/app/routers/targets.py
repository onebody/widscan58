"""`GET /api/targets[/{id}]` 与 `POST /api/targets/{id}/review`：候选目标的读侧与人工复核。

与 `/api/events` 同一份口径，两条硬要求也同源（契约 §4）：

- **`?site=` 缺省 = 当前场地**：`site_or_current` 一道依赖、一份口径；窗口与行数上限走
  `history`，列表按 `last_seen_at` 排（"最近出没"——与告警按 `last_ts` 同一取法）。
- **详情必须给证据链与局限说明**：时间线上每条观测带自己那份证据拷贝；`coverage_json`
  没写就把"局限未申报"如实说出来——"没写局限"和"没有局限"是两个结论，页面不许把
  前者渲染成后者。

复核是写侧（aggregator 405）：`confirmed`/`rejected` 二选一，可反复改判（被拒只是状态
不是删除），note ≤2000 字。复核人话进 `review_json`，算法推测在 `evidence`/`confidence`——
§10.5 的"复核与推测分列"从存储到接口都是两张列，页面没有混的机会。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session, site_or_current
from ..models import CandidateTarget, Site, TrackObservation, utcnow
from ..services import history
from ..services.hierarchy import get_or_404, not_found, unprocessable

router = APIRouter(prefix="/api/targets", tags=["targets"])

#: 每一个候选目标都逃不掉的局限：它是判定聚合出来的"疑似"，不是观测事实，更不是定位。
ALWAYS = (
    "候选目标由插件判定聚合而成（不是观测事实本身）：先看证据链与时间线再复核",
    "读数只说明频段上的相对电平，不给距离、方位或坐标",
    "confidence 是声明公式的输出（见 confidence_basis），只说明关联判定有多少条，不是精度",
)

#: 复核决策的词表。三态里的 `unreviewed` 不在端点上——"还没复核"是初始态，不是一次复核。
REVIEW_DECISIONS = ("confirmed", "rejected")


def _iso_or_none(moment: datetime | None) -> str | None:
    return None if moment is None else history.iso(moment)


def _json_or_error(raw: str | None) -> dict:
    """证据/覆盖/复核 JSON 原样解出来；坏 JSON 如实说坏，不编一个空对象。"""
    if raw is None:
        return {"value": None, "error": None}
    try:
        return {"value": json.loads(raw), "error": None}
    except (TypeError, ValueError):
        return {"value": None, "error": "malformed_json"}


def obs_out(obs: TrackObservation) -> dict:
    return {
        "obs_id": obs.obs_id,
        "finding_id": obs.finding_id,
        "site_id": obs.site_id,
        "gateway_id": obs.gateway_id,
        "node_id": obs.node_id,
        "slot": obs.slot,
        "ts": history.iso(obs.ts),
        "metric": obs.metric,
        "value": obs.value,
        "source": obs.source,
        "evidence": _json_or_error(obs.evidence_json),
    }


def target_out(session: Session, target: CandidateTarget, *, with_detail: bool = False) -> dict:
    coverage = _json_or_error(target.coverage_json)
    out = {
        "target_id": target.target_id,
        "site_id": target.site_id,
        "label": target.label,
        "kind": target.kind,
        "producer": target.producer,
        "confidence": target.confidence,
        "confidence_basis": target.confidence_basis,
        "first_seen_at": history.iso(target.first_seen_at),
        "last_seen_at": history.iso(target.last_seen_at),
        "review_status": target.review_status,
        "reviewed_at": _iso_or_none(target.reviewed_at),
    }
    if not with_detail:
        return out
    timeline = list(
        session.execute(
            select(TrackObservation)
            .where(TrackObservation.target_id == target.target_id)
            .order_by(TrackObservation.ts, TrackObservation.obs_id)
        ).scalars()
    )
    out["coverage"] = coverage
    out["review"] = _json_or_error(target.review_json)
    out["observations"] = [obs_out(obs) for obs in timeline]
    limitations = list(ALWAYS)
    sources = sorted({obs.source for obs in timeline})
    if any(source != "real" for source in sources):
        limitations.append(f"这条目标的观测来源含 `{','.join(sources)}`，不全是此刻的实时判定")
    if coverage["value"] is None:
        limitations.append("coverage_json 没写：这条目标的局限未申报")
    if target.reviewed_at is None:
        limitations.append("还没有人工复核：review_status 的 unreviewed 是初始态不是结论")
    out["limitations"] = limitations
    return out


@router.get("")
def list_targets(
    session: Session = Depends(get_session),
    site: int = Depends(site_or_current),
    review_status: str | None = Query(default=None),
    node: int | None = Query(default=None),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=history.DEFAULT_ROWS),
) -> dict:
    """这一个场地、这一段窗口里的候选目标，按最近出没倒序回。"""
    get_or_404(session, Site, site, "site")
    if review_status is not None and review_status not in ("unreviewed", *REVIEW_DECISIONS):
        raise unprocessable(
            "unknown_status", status=review_status, choices=["unreviewed", *REVIEW_DECISIONS]
        )
    frm, to, span = history.window(start, end)
    capped = history.row_limit(limit)

    query = (
        select(CandidateTarget)
        .where(
            CandidateTarget.site_id == site,
            CandidateTarget.last_seen_at >= datetime.fromtimestamp(frm, timezone.utc),
            CandidateTarget.last_seen_at < datetime.fromtimestamp(to + 1, timezone.utc),
        )
        .order_by(CandidateTarget.last_seen_at.desc(), CandidateTarget.target_id.desc())
        .limit(capped + 1)
    )
    if review_status is not None:
        query = query.where(CandidateTarget.review_status == review_status)
    if node is not None:
        history.require_node_in_site(session, site, node)
        query = query.where(
            CandidateTarget.target_id.in_(
                select(TrackObservation.target_id).where(TrackObservation.node_id == node)
            )
        )

    rows = list(session.execute(query.distinct()).scalars())
    truncated = len(rows) > capped
    rows = rows[:capped]
    return {
        "site_id": site,
        "range": {"from": frm, "to": to, "span_s": span},
        "filters": {"review_status": review_status, "node": node},
        "limit": capped,
        "truncated": truncated,
        "rows": [target_out(session, row) for row in rows],
    }


@router.get("/{target_id}")
def read_target(
    target_id: int,
    session: Session = Depends(get_session),
    site: int | None = Query(default=None),
) -> dict:
    """一个候选目标的全部上下文：时间线（带证据拷贝）+ 覆盖/未知项 + 复核记录 + 局限。"""
    target = get_or_404(session, CandidateTarget, target_id, "target")
    if site is not None and target.site_id != site:
        raise not_found("target", target_id)
    return target_out(session, target, with_detail=True)


@router.post("/{target_id}/review")
def review_target(
    target_id: int,
    payload: dict,
    session: Session = Depends(require_writable_session),
) -> dict:
    """人工复核：`confirmed`/`rejected` + 可选 note。可反复改判——被拒只是状态不是删除，
    改判就是再复核一次，`review_json` 留下的是最近一次的决定。"""
    target = get_or_404(session, CandidateTarget, target_id, "target")
    decision = payload.get("decision")
    if decision not in REVIEW_DECISIONS:
        raise unprocessable(
            "unknown_decision", decision=decision, choices=list(REVIEW_DECISIONS)
        )
    note = payload.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > 2000):
        raise unprocessable("out_of_range", detail="note 必须是 ≤2000 字的文本")
    body = {"decision": decision}
    if note is not None:
        body["note"] = note
    target.review_status = decision
    target.review_json = json.dumps(body, ensure_ascii=False)
    target.reviewed_at = utcnow()
    session.commit()
    return target_out(session, target, with_detail=True)
