"""`GET /api/samples/summary`：趋势图的取数口——粒度有档位、跨度有顶、行数有顶。

这一口读的是**摘要表**，不是原始序列：原始 RSSI 只在内存环形缓冲里（方案 §3），所以这里
能给的最细就是一档桶。三条约束都在 `services/history.py` 里，router 只负责把它们拼成一条
带 `site_id` 的查询：

- **`WHERE` 里场地/节点/槽位/桶区间四个约束都落在索引列上**（`ix_summary_site_node_slot_bucket`
  的 `site_id`/`node_id`/`slot`/`ts_bucket`）。`bucket_s` 不在索引里，它是在索引已经命中的那些行
  上再筛一次。`tests/test_samples.py` 拿 `EXPLAIN QUERY PLAN` 钉住这一点：判据是"计划里没有
  `SCAN sample_summary`"，不是"我觉得这个索引够用"。
- **空结果是 200 + `rows: []`**，不是 404。一块刚接上、还没攒够一个桶的板子，页面上要显示
  "尚无数据"；把它报成 404，前端就得为这一个端点写特例。
- **404 只留给"这个 id 在这台实例上不存在"**（场地、节点），而"节点属别的场地"是 422：
  前者是地址写错，后者是选择器串场——两种失败在页面上该说的话不一样。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_session, site_or_current
from ..models import SampleSummary, Site
from ..services import history
from ..services.hierarchy import get_or_404, unprocessable

router = APIRouter(prefix="/api/samples", tags=["samples"])


def write_bucket(settings: Settings) -> int:
    """写入侧此刻用的那一档桶宽。

    不在这儿重算优先级：`retention.SummaryAggregator.from_settings` 取的就是这同一个字段，
    读侧要是自己走一遍配置链，改了全局覆写的那天趋势图就会去问一档库里根本没在写的粒度。
    """
    return settings.summary_bucket_s


def check_slot(settings: Settings, site: int, slot: int) -> None:
    """槽位越界要说得清：`slot=8` 在 8 槽的口径下是第一格之外，空图会被读成"这块板安静了"。"""
    if not 0 <= slot < settings.slots_per_node:
        raise unprocessable(
            "slot_out_of_range",
            param="slot",
            slot=slot,
            maximum=settings.slots_per_node - 1,
            site=site,
        )


@router.get("/summary")
def read_summary(
    request: Request,
    session: Session = Depends(get_session),
    site: int = Depends(site_or_current),
    node: int | None = Query(default=None),
    nodes: str | None = Query(default=None),
    slot: int | None = Query(default=None),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    bucket: int | None = Query(default=None),
    limit: int = Query(default=history.DEFAULT_ROWS),
) -> dict:
    """这个场地、这一档粒度、这一段窗口里的摘要行，按时间升序回。

    `nodes=1,2,3`（逗号分隔）是组合频谱那一路的多节点过滤：每个 id 都走与 `node` 同一条
    `node_not_in_site` 校验，串场的 id 一个都不许混进结果；与单值 `node` 同时给时取并集。
    """
    settings: Settings = request.app.state.settings
    get_or_404(session, Site, site, "site")
    frm, to, span = history.window(start, end)
    capped = history.row_limit(limit)
    stored = tuple(history.bucket_widths(session, site))
    width = history.choose_bucket(bucket, write_bucket(settings), stored)
    node_ids: list[int] = []
    if nodes is not None:
        for part in nodes.split(","):
            part = part.strip()
            if not part:
                continue
            if not part.isdigit():
                raise unprocessable("wrong_type", param="nodes", detail=f"节点 id 必须是整数，收到 {part!r}")
            node_ids.append(int(part))
    if node is not None:
        node_ids.append(node)
    node_ids = list(dict.fromkeys(node_ids))
    for node_id in node_ids:
        history.require_node_in_site(session, site, node_id)
    if slot is not None:
        check_slot(settings, site, slot)

    query = (
        select(SampleSummary)
        .where(
            SampleSummary.site_id == site,
            SampleSummary.ts_bucket >= frm,
            SampleSummary.ts_bucket <= to,
            SampleSummary.bucket_s == width,
        )
        # 先按"最近的桶优先"取，截断才截掉最老的那头；返回前整个倒过来，页面拿到的时间升序。
        .order_by(SampleSummary.ts_bucket.desc(), SampleSummary.node_id, SampleSummary.slot)
        .limit(capped + 1)
    )
    if node_ids:
        query = query.where(SampleSummary.node_id.in_(node_ids))
    if slot is not None:
        query = query.where(SampleSummary.slot == slot)

    rows = list(session.execute(query).scalars())
    truncated = len(rows) > capped
    rows = list(reversed(rows[:capped]))
    return {
        "site_id": site,
        "bucket": width,
        "bucket_widths": list(stored),
        "allowed_buckets": list(history.allowed_buckets(write_bucket(settings), stored)),
        "range": {"from": frm, "to": to, "span_s": span},
        "filters": {"node": node, "nodes": node_ids, "slot": slot},
        "limit": capped,
        "truncated": truncated,
        # 一段历史里混进回放行是允许的事实，但它必须是页面一眼能看见的事实：徽标按这个集合出。
        "sources": sorted({row.source for row in rows}),
        "rows": [history.summary_row(row) for row in rows],
    }
