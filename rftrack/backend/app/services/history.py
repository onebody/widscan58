"""历史读侧的公共口径：时间窗、桶宽白名单、行数硬闸、范围参数的场地归属。

`/api/samples/summary` 与 `/api/events` 是同一个工作台上的两张图。它们对"最近 30 分钟"必须
取到同一段区间、对"截断了"必须用同一个词说、对"`?node=` 指到别的场地"必须回同一个 422——
所以这些口径只在这里实现一次。

四条都不是装饰：

- **naive 的时刻一律按 UTC 解释**。方案 §3 前置②记的是一条实测：SQLite 的
  `DATETIME(timezone=True)` 读回来不带偏移。URL 里同样会出现不带 `Z` 的 ISO 串，那必须有一条
  解释规则——否则同一份链接在两只时区的机器上取到两个窗口，而页面上看着都是"有数据"。
- **上限是硬闸**：一次取 `limit + 1` 行来判断"后面还有没有"，而不是先 `count(*)` 再决定取多少。
  后者为了说一句话多扫一遍表，而 SD 卡上的读放大正是 R11 关心的东西。
- **桶宽白名单管的是"页面上能选哪几档"**，生效桶宽与库里存过的档位要额外接受：改了桶宽的
  第一天，库里同时躺着两种粒度，读侧只认新档位就等于让旧历史凭空消失。
- **`?node=` 必须先认场地**：两个端点都把它翻成同一个 422 `node_not_in_site`。"这块板没有历史"
  与"这块板不属于这个场地"在页面上是两句完全不同的话，混起来写就是一张安静的假空图。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Gateway, Node, Relay, SampleSummary, to_utc
from .hierarchy import not_found, unprocessable

#: 趋势图的档位（秒）。刻意是"档位"而不是"任意整数"：任意值会把读侧变成重聚合的入口，
#: 而重聚合就是方案 §3 那条"禁止任意全表扫"要拦的东西。
BUCKET_CHOICES = (1, 2, 5, 15, 30, 60, 300, 900)

#: 一次请求能问出的最长窗口，与设置键 `time_range_s` 的上限同一个数（86 400 s = 一天）。
MAX_SPAN_S = 86_400
#: `?from=`/`?to=` 都不传时的默认窗口：够画一条趋势线，又不至于让默认请求变成"扫全库"。
DEFAULT_SPAN_S = 900
#: 一次能取回的最大行数。8 块板 × 8 槽 × 一天 1 s 桶 = 552 万行，这个数必须存在。
MAX_ROWS = 2000
DEFAULT_ROWS = 600


def epoch(moment: datetime) -> int:
    """时刻 → unix 秒（向下取整）。naive 当 UTC，理由见模块开头。"""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=timezone.utc)
    return int(aware.timestamp())


def iso(moment: datetime) -> str:
    """库里读回来的时刻 → 页面上的 ISO 串。统一带偏移：不带就会被浏览器按本地时区解释。"""
    return (to_utc(moment) or moment).isoformat()


def window(
    start: datetime | None,
    end: datetime | None,
    *,
    now: datetime | None = None,
) -> tuple[int, int, int]:
    """把两个可选的时刻折成一个闭区间 `[from, to]`（unix 秒）与它的跨度。

    四种组合各有语义：都不给 = 最近 `DEFAULT_SPAN_S`；只给 `from` = 从那一刻到现在；
    只给 `to` = 以那一刻结束的 `DEFAULT_SPAN_S`。**倒着的区间是错误，不是空结果**——
    它会以"这块板没有数据"的样子出现在页面上，那是最难倒推回去的一种失败。
    """
    moment = now or datetime.now(timezone.utc)
    to_epoch = epoch(moment) if end is None else epoch(end)
    if start is None:
        span = min(DEFAULT_SPAN_S, to_epoch)
        from_epoch = to_epoch - span
    else:
        from_epoch = epoch(start)
        span = to_epoch - from_epoch
    if span <= 0:
        raise unprocessable(
            "range_backwards",
            param="from",
            hint="`from` 必须早于 `to`：倒过来的区间只会伪装成没有数据",
        )
    if span > MAX_SPAN_S:
        raise unprocessable(
            "span_too_wide", param="to", span_s=span, maximum_s=MAX_SPAN_S
        )
    return from_epoch, to_epoch, span


def row_limit(limit: int) -> int:
    """行数上限：越界是 422，不静默夹紧。

    静默夹紧的代价由页面付——它以为自己取了 20 万行，画出来的却是最后 2000 行，
    而"图为什么从中间开始"在页面上没有任何线索。
    """
    if not 1 <= limit <= MAX_ROWS:
        raise unprocessable("limit_out_of_range", param="limit", maximum=MAX_ROWS)
    return limit


def node_site(session: Session, node_id: int) -> int | None:
    """节点属哪个场地：走 gateway → relay → site 那一条推导链，不在 node 上双写 `site_id`。"""
    return session.scalar(
        select(Relay.site_id)
        .join(Gateway, Gateway.relay_id == Relay.relay_id)
        .join(Node, Node.gateway_id == Gateway.gateway_id)
        .where(Node.node_id == node_id)
    )


def require_node_in_site(session: Session, site: int, node_id: int) -> None:
    """范围参数里的节点必须真的属于这个场地。

    两种失败在页面上该说的话不一样：id 在这台实例上根本不存在是 404（地址写错），
    "这块板是另一个场地的"是 422（选择器串场）。把它翻成"查不到行、回空列表"最坏——
    页面会安静地显示"这块板没有历史"，而那块板只是不属于这个场地。
    """
    owner = node_site(session, node_id)
    if owner is None:
        raise not_found("node", node_id)
    if owner != site:
        raise unprocessable("node_not_in_site", node=node_id, site=site, node_site=owner)


def allowed_buckets(effective: int, stored: tuple[int, ...] = ()) -> tuple[int, ...]:
    """这一版接受哪些桶宽：档位白名单 ∪ 库里正在写的那一档 ∪ 这个场地**实际存过**的档位。

    后两项都不是宽松：改过一次桶宽的库里同时躺着两种粒度，只认"此刻在写的那一档"就等于
    让历史对旧档位闭口不言，而那正是这接口要画的东西。仍然不认"任意整数"——没有存过的档位
    一律拒，读侧就不可能变成重聚合的入口。
    """
    return tuple(sorted({*BUCKET_CHOICES, effective, *stored}))


def choose_bucket(requested: int | None, effective: int, stored: tuple[int, ...] = ()) -> int:
    """选定要读哪一档粒度。没请求就是生效桶宽——那才是"此刻在写"的那一档。"""
    if requested is None:
        return effective
    allowed = allowed_buckets(effective, stored)
    if requested not in allowed:
        raise unprocessable(
            "bucket_unsupported",
            param="bucket",
            requested=requested,
            allowed=list(allowed),
            hint="桶宽只能是档位之一，或当前生效/库里已存过的档位",
        )
    return requested


def bucket_widths(session: Session, site_id: int) -> list[int]:
    """这个场地的摘要里**实际存过**哪几种桶宽。

    桶宽是全局键、不进主键（见 `models.SampleSummary`），所以改一次桶宽就会让两种粒度同存
    一表。读侧不说出这件事，页面就只能猜。走 `ix_summary_site_node_slot_bucket` 的前缀。
    """
    rows = session.execute(
        select(SampleSummary.bucket_s)
        .where(SampleSummary.site_id == site_id)
        .distinct()
        .order_by(SampleSummary.bucket_s)
    ).scalars()
    return list(rows)


def summary_row(row: SampleSummary) -> dict:
    """一行的对外形状：字段名与库里一致，不做二次加工。

    `null` 原样保留——`ema_*` 为空是 `LEVELS` 帧的 RSSI 模式**确实没测 EMA**，写成 0 就是一句
    看得出来的假话，而页面有 `null` 分支可以显示"未测"。
    """
    return {
        "ts_bucket": row.ts_bucket,
        "node_id": row.node_id,
        "slot": row.slot,
        "gateway_id": row.gateway_id,
        "bucket_s": row.bucket_s,
        "sample_count": row.sample_count,
        "ema_count": row.ema_count,
        "rssi_mean": row.rssi_mean,
        "rssi_min": row.rssi_min,
        "rssi_max": row.rssi_max,
        "rssi_last": row.rssi_last,
        "ema_mean": row.ema_mean,
        "ema_min": row.ema_min,
        "ema_max": row.ema_max,
        "ema_last": row.ema_last,
        "source": row.source,
        "last_at": iso(row.last_at),
    }
