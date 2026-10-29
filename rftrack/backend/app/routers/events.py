"""`GET /api/events[/{id}]`：观测事实的读侧——按场地取、详情带证据链与局限。

契约 §4 对这一口提了两条硬要求，两条都不是一句"返回 JSON"能交差的：

- **`?site=` 缺省 = 当前场地，不默认跨场地列**：与 `/api/samples/summary` 同一道依赖、同一份
  `services/history.py` 口径。窗口、行数上限、`?node=` 的场地归属也都从那一份里取，两张图对
  "最近 30 分钟"与"截断了"必须说同一个数。
- **详情必须给证据链与局限说明**：`payload_json` 是采集层当时看到的原始字段（哪个端点、安静了
  几秒、上一帧序号），S8 的人工复核点开的就是它；`limitations` 说的是这条记录**能讲到哪一步**——
  RSSI 一类的读数永远不给距离、方位或坐标（任务书硬边界），这句话必须由后端说，不能让页面猜。

按 id 直读是唯一一个不带场地约束的入口，这是刻意的：主键读不是"跨场地扫表"，而那一条属于哪个
场地就写在返回体里。带了 `?site=` 就核一遍，对不上回 404——404 而不是 422，因为"这个 id 在你
这个场地的列表里根本出现过不了"，页面看到的应该与它实际能拿到的一致。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, site_or_current
from ..models import SignalEvent, Site
from ..services import history
from ..services.hierarchy import get_or_404, not_found

router = APIRouter(prefix="/api/events", tags=["events"])

#: 每一条事件都逃不掉的局限：这一层写的就是"我们的数说明不了什么"。
ALWAYS = ("读数只说明这一频道上有多吵（dBFS 相对量），不给距离、方位或坐标",)


def payload_of(event: SignalEvent) -> dict:
    """证据链：`payload_json` 原样解出来，坏 JSON 如实说坏，不编一个空对象当"没有上下文"。"""
    if event.payload_json is None:
        return {"payload": None, "payload_error": None}
    try:
        parsed = json.loads(event.payload_json)
    except (TypeError, ValueError):
        return {"payload": None, "payload_error": "malformed_json"}
    return {"payload": parsed, "payload_error": None}


def limitations(event: SignalEvent) -> list[str]:
    """这条记录能讲到哪一步：页面上"这块板安静了"这句话的分量由这里决定。"""
    notes = list(ALWAYS)
    if event.source != "real":
        notes.append(f"这一条的来源是 `{event.source}`，不是此刻的实时观测")
    if event.kind == "node_offline":
        notes.append("安静只意味着这段时间里没有帧进来：页面关掉、桥重启都会这样，不等于板子断电")
    if event.kind == "node_online":
        notes.append("恢复只意味着这条流又开始进帧，不解释中间为什么断")
    if event.node_id is None:
        notes.append("这是接入机层面的观测，没有指认到具体节点")
    if event.value is None:
        notes.append("这一条没有触发数值，是状态跃迁而不是一次测量")
    return notes


def as_out(event: SignalEvent, *, with_limitations: bool = False) -> dict:
    out = {
        "event_id": event.event_id,
        "ts": history.iso(event.ts),
        "site_id": event.site_id,
        "gateway_id": event.gateway_id,
        "node_id": event.node_id,
        "slot": event.slot,
        "freq_khz": event.freq_khz,
        "kind": event.kind,
        "metric": event.metric,
        "value": event.value,
        "quality": event.quality,
        "source": event.source,
        **payload_of(event),
    }
    if with_limitations:
        out["limitations"] = limitations(event)
    return out


@router.get("")
def list_events(
    session: Session = Depends(get_session),
    site: int = Depends(site_or_current),
    node: int | None = Query(default=None),
    kind: str | None = Query(default=None),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=history.DEFAULT_ROWS),
) -> dict:
    """这一个场地、这一段窗口里的事件，按时间倒序回（最新在前）。"""
    get_or_404(session, Site, site, "site")
    frm, to, span = history.window(start, end)
    capped = history.row_limit(limit)
    if node is not None:
        history.require_node_in_site(session, site, node)

    query = (
        select(SignalEvent)
        .where(
            SignalEvent.site_id == site,
            SignalEvent.ts >= datetime.fromtimestamp(frm, timezone.utc),
            # 窗口边界是整秒，而 `ts` 带微秒：上界必须取 `to` 那一秒的**末尾**。
            # 直接 `<= to` 会把"这一秒里刚发生的事件"判到窗口外面去（`…:30.123456 > …:30`），
            # 而那正是"刚出事的事件在页面上刷不出来"这种最难复现的形态。
            SignalEvent.ts < datetime.fromtimestamp(to + 1, timezone.utc),
        )
        .order_by(SignalEvent.ts.desc(), SignalEvent.event_id.desc())
        .limit(capped + 1)
    )
    if node is not None:
        query = query.where(SignalEvent.node_id == node)
    if kind is not None:
        query = query.where(SignalEvent.kind == kind)

    rows = list(session.execute(query).scalars())
    truncated = len(rows) > capped
    rows = rows[:capped]
    return {
        "site_id": site,
        "range": {"from": frm, "to": to, "span_s": span},
        "filters": {"node": node, "kind": kind},
        "limit": capped,
        "truncated": truncated,
        "sources": sorted({row.source for row in rows}),
        "rows": [as_out(row) for row in rows],
    }


@router.get("/{event_id}")
def read_event(
    event_id: int,
    session: Session = Depends(get_session),
    site: int | None = Query(default=None),
) -> dict:
    """一条事件的全部上下文：证据链 + 局限说明。"""
    event = get_or_404(session, SignalEvent, event_id, "event")
    if site is not None and event.site_id != site:
        raise not_found("event", event_id)
    return as_out(event, with_limitations=True)
