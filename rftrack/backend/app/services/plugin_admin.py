"""插件的运维视角：列表、状态、日志、启停、配置——这五件事的规则都在这一层。

`routers/plugins.py` 只做参数校验、编排与状态码（全站同一条分工，见 `layouts.py` 的模块注释）。
这一层不认识 HTTP，也不碰运行时那份运行态，三条刻意的分工各对着一个会坏在现场的场景：

1. **启停不搬队列**。队列是执行线程 own 的那份快照，请求线程去搬就会与它撞在同一批数据上
   （`plugin_runtime.refresh()` 里那条纪律的同一条）。所以 `enable`/`disable` 只改注册表那一行，
   运行时下一拍自己看见；"已启用"与"在跑"因此是两句话，各自有据，端点不许替运行时宣布它停了。
2. **`enable` 先问实现映射**。库里有一行不代表它跑得起来（方案 §11 不做动态代码加载）。让"启用
   成功"发生在一支永远跑不起来的插件上，页面上就只剩一条骗人的绿灯。
3. **`config` 只动可编辑的三列**。`name`/`version`/`summary` 由代码那一份覆写（可追责，不是可
   编辑的），`config_json` 今天没有读者——插件取的生效值走配置层那三层，往这一列写东西只会造出
   "看着像生效值、其实没人读"的假配置。

`plugin_log` 只在**真的有事发生**时写：一张记状态跃迁的表如果被点击次数填满，它要防的那件事
（写放大）就回来了，而运营者要找的那几条会被埋掉。唯一的例外是 `retried`，理由写在 `enable_plugin`
里。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import (
    Gateway,
    PluginLog,
    PluginRegistry,
    PluginState,
    Site,
    utcnow,
)
from ..plugins import IMPLEMENTATIONS
from . import registry
from .history import iso, row_limit
from .hierarchy import conflict, not_found, unprocessable
from .instance import effective_instance_role
from .plugin_runtime import EXECUTABLE_SCOPES

#: 日志面板的缺省行数。`history.DEFAULT_ROWS`（600）是给曲线取数用的，日志要的是"最近几十条"；
#: 上界仍然共用 `history.row_limit`（`MAX_ROWS`），不另立一套越界口径。
LOG_LIMIT_DEFAULT = 50

#: `scope` 指向哪张表。`relay` 不在这里也不在 `EXECUTABLE_SCOPES`：批上没有位置身份，判它要查库。
TARGET_MODELS = {"site": Site, "gateway": Gateway}

#: 状态那一列里没有实现的那一行该显示成什么：`null`，不是 0。
_EMPTY_STATE = dict.fromkeys(
    ("status", "started_at", "last_tick_at", "next_retry_at", "errors", "restarts",
     "processed", "produced", "dropped", "last_error"),
    None,
)


def _when(moment: Any) -> str | None:
    return iso(moment) if moment is not None else None


def _state_fields(state: PluginState | None) -> dict[str, Any]:
    """运行态那十列。没有那一行就整片 `null`——"读不到"与"是 0"是两件事（`PluginState` 的注释）。

    无实现的插件正是这种：`registry.sync()` 刻意为它不配状态行，读侧如果补一个 0，页面上就变成
    "它跑过、处理了 0 批"，而那支插件一次都没被实例化过。
    """
    if state is None:
        return dict(_EMPTY_STATE)
    return {
        "status": state.status,
        "started_at": _when(state.started_at),
        "last_tick_at": _when(state.last_tick_at),
        "next_retry_at": _when(state.next_retry_at),
        "errors": state.errors,
        "restarts": state.restarts,
        "processed": state.processed,
        "produced": state.produced,
        "dropped": state.dropped,
        "last_error": state.last_error,
    }


def registry_row(session: Session, plugin_id: str) -> PluginRegistry:
    row = session.get(PluginRegistry, plugin_id)
    if row is None:
        raise not_found("plugin", plugin_id)
    return row


def queues_of(runtime: Any, plugin_id: str) -> list[dict[str, Any]]:
    """本实例运行时**此刻**的每条队列，直接读它那份发布出来的快照。

    不读 `plugin_state.stats_json`：那是上一轮 `_write` 留下的，最坏情况滞后一个节拍，而页面上
    "这条队列堵不堵"要的是现在。运行时不在这个函数里被创建，也不在这里被 refresh——它是
    `create_app` 挂上 `app.state` 的那一个，采集侧投喂的也是同一个。
    """
    return [
        queue
        for queue in runtime.status()["queues"]
        if queue["plugin_id"] == plugin_id
    ]


def as_view(
    row: PluginRegistry,
    settings: Settings,
    *,
    state: PluginState | None,
    queues: list[dict[str, Any]],
) -> dict[str, Any]:
    """一支插件在页面上的完整一行：登记 + 生效容量 + 运行态 + 此刻的队列。

    列表与 `/state` 共用这一份序列化，两处不会长出两个形状（同一件事在两处数字不一样，页面就
    只能挑一处信）。
    """
    return {
        "plugin_id": row.plugin_id,
        "name": row.name,
        "version": row.version,
        "summary": row.summary,
        "enabled": row.enabled,
        "scope": row.scope,
        "scope_id": row.scope_id,
        #: 这一档今天有没有执行者。`false` 时页面上要标出来，别让"已启用"看起来像在跑。
        "scope_executable": row.scope in EXECUTABLE_SCOPES,
        "queue_limit": row.queue_limit,
        "effective_queue_limit": row.queue_limit or settings.plugin_queue_limit,
        "implementation": row.plugin_id in IMPLEMENTATIONS,
        "keys": list(getattr(IMPLEMENTATIONS.get(row.plugin_id), "CONFIG_KEYS", ())),
        **_state_fields(state),
        "queues": queues,
    }


def list_payload(session: Session, settings: Settings, runtime: Any) -> dict[str, Any]:
    """`GET /api/plugins`：先按代码里那张表的顺序，再列库里多出来的行。

    顺序不排 `plugin_id`：`registry.BUILTIN_PLUGINS` 的注释写了"元组顺序即页面顺序"。多出来的行
    排后面并按 id 定序，否则同一份库两次请求给出两种顺序。
    """
    rows = {
        row.plugin_id: row
        for row in list(session.execute(select(PluginRegistry)).scalars().all())
    }
    states = {
        state.plugin_id: state
        for state in list(session.execute(select(PluginState)).scalars().all())
    }
    ordered = [rows[item.plugin_id] for item in registry.BUILTIN_PLUGINS if item.plugin_id in rows]
    ordered += [rows[plugin_id] for plugin_id in sorted(set(rows) - set(registry.BY_ID))]
    return {
        "instance_role": effective_instance_role(session, settings.instance_role),
        "supported_scopes": list(EXECUTABLE_SCOPES),
        "plugins": [
            as_view(
                row,
                settings,
                state=states.get(row.plugin_id),
                queues=queues_of(runtime, row.plugin_id),
            )
            for row in ordered
        ],
    }


def view_of(
    session: Session, settings: Settings, row: PluginRegistry, runtime: Any
) -> dict[str, Any]:
    """那一行的现状。状态行可能没有（无实现的登记行），那不是错误而是 `null`。"""
    return as_view(
        row,
        settings,
        state=session.get(PluginState, row.plugin_id),
        queues=queues_of(runtime, row.plugin_id),
    )


def state_payload(session: Session, settings: Settings, plugin_id: str, runtime: Any) -> dict:
    """`GET …/state`。这一口要求**有**运行态那一行，没有就 404——列表里它是可以没有的。

    分开是因为两页问的是两个问题：列表问"库里登记了什么"，状态面板问"它跑得怎么样"。后者对一支
    从来没有实现的东西回一串 `null` 会让页面写出一片 `??`，而"这支插件没有运行态"本身就该是一句
    能显示出来的话（`kind=plugin_state`，与"没有这个插件"是两种 404）。
    """
    row = registry_row(session, plugin_id)
    if session.get(PluginState, plugin_id) is None:
        raise not_found("plugin_state", plugin_id)
    return view_of(session, settings, row, runtime)


def enable_plugin(session: Session, plugin_id: str) -> tuple[PluginRegistry, bool]:
    """启用一支插件：翻注册表那一行，并把 supervisor 已经放弃的那一支捞回来。

    `retried` 这条流水是"重复动作不记"的唯一例外，因为它是**另一个动作**：那一行本来就是 1，
    如果端点因此什么都不做，用户就没有第二个入口能把 `restarts` 打回 0——只能先停用再启用，而
    停用会真的收摊、把队列里剩下的东西搬完丢掉。累计 `errors` 与 `processed` 不清：那是"这支
    插件历史上坏过几次、处理过几批"的审计数字，不是重试配额。
    """
    row = registry_row(session, plugin_id)
    if row.plugin_id not in IMPLEMENTATIONS:
        raise conflict("no_implementation", plugin_id=row.plugin_id)
    state = session.get(PluginState, plugin_id)
    retried = state is not None and state.status == "failing"
    changed = not row.enabled
    moment = utcnow()
    if changed:
        row.enabled = True
        row.updated_at = moment
    if retried:
        state.status = "stopped"
        state.restarts = 0
        state.next_retry_at = None
        state.updated_at = moment
    if changed or retried:
        session.add(
            PluginLog(
                plugin_id=row.plugin_id,
                level="info",
                event="enabled" if changed else "retried",
                message=(
                    "已在注册表里启用，运行时的下一拍开始投喂。"
                    if changed
                    else "重启计数与退避已清零，这支插件可以再次尝试。"
                ),
            )
        )
    return row, changed


def disable_plugin(session: Session, plugin_id: str) -> tuple[PluginRegistry, bool]:
    """停用一支插件：只把注册表那一行改成 0。

    运行态那几列一个字都不动——队列还没搬完的时候页面上就该继续显示 `running`，那是真的。运行时
    自己收摊时会写 `stopped_consuming`（带着搬完没搬完的批数），与这条 `disabled`（"运营者让它停"）
    说的是两件事。
    """
    row = registry_row(session, plugin_id)
    changed = bool(row.enabled)
    if changed:
        row.enabled = False
        row.updated_at = utcnow()
        session.add(
            PluginLog(
                plugin_id=row.plugin_id,
                level="info",
                event="disabled",
                message="已在注册表里停用；正在跑的队列由运行时自己收摊。",
            )
        )
    return row, changed


def configure_plugin(
    session: Session,
    plugin_id: str,
    *,
    scope: str,
    scope_id: int | None,
    queue_limit: int | None,
) -> tuple[PluginRegistry, bool]:
    """整份替换那三列可编辑的值。四种配错各回一种 reason，拒了就是一个字段都没写。

    校验只能落在这一层：`scope_id` 是多态指向、没有外键（`models.py` 那一列的注释记着为什么），
    所以"这个 id 到底存不存在、在不在 `scope` 说的那张表里"只有查了库才知道。`queue_limit` 的
    下界也在这里而不是交给 Pydantic——后者吐的是 FastAPI 的校验数组，而全站错误体只有一种形状。
    """
    row = registry_row(session, plugin_id)
    if scope not in EXECUTABLE_SCOPES:
        raise unprocessable(
            "scope_unsupported", scope=scope, supported=list(EXECUTABLE_SCOPES)
        )
    if queue_limit is not None and queue_limit < 1:
        raise unprocessable("invalid_queue_limit", param="queue_limit", minimum=1)
    target: int | None = None
    if scope == "global":
        if scope_id is not None:
            raise unprocessable("scope_id_not_allowed", scope=scope, scope_id=scope_id)
    else:
        if scope_id is None or session.get(TARGET_MODELS[scope], scope_id) is None:
            raise unprocessable("scope_target_missing", scope=scope, scope_id=scope_id)
        target = scope_id
    params = (scope, target, queue_limit)
    changed = (row.scope, row.scope_id, row.queue_limit) != params
    if changed:
        row.scope, row.scope_id, row.queue_limit = params
        row.updated_at = utcnow()
        session.add(
            PluginLog(
                plugin_id=row.plugin_id,
                level="info",
                event="configured",
                message="作用域与队列上限已更新，下一批按新参数投喂。",
                detail_json=json.dumps(
                    {"scope": scope, "scope_id": target, "queue_limit": queue_limit}
                ),
            )
        )
    return row, changed


def _detail(row: PluginLog) -> tuple[Any, str | None]:
    if row.detail_json is None:
        return None, None
    try:
        return json.loads(row.detail_json), None
    except (TypeError, ValueError):
        #: 坏 JSON 如实说坏（同 `routers/events.py` 的 `payload_error`），不编一个空对象当"没有详情"。
        return None, "malformed_json"


def _log_row(row: PluginLog) -> dict[str, Any]:
    detail, problem = _detail(row)
    return {
        "log_id": row.log_id,
        "ts": iso(row.ts),
        "level": row.level,
        "event": row.event,
        "message": row.message,
        "detail": detail,
        "detail_error": problem,
    }


def logs_payload(
    session: Session, plugin_id: str, *, limit: int | None = None
) -> dict[str, Any]:
    """`GET …/logs`：倒序、有界，并带上**全量**条数。

    只有列表没有总数的话，"最近 50 条"与"一共 50 条"在页面上长一个样，而运营者判断的正是"这支
    插件是不是在反复坏"。未知插件先 404：回一个空列表会被读成"它没有日志"。
    """
    registry_row(session, plugin_id)
    capped = row_limit(LOG_LIMIT_DEFAULT if limit is None else limit)
    total = session.scalar(
        select(func.count(PluginLog.log_id)).where(PluginLog.plugin_id == plugin_id)
    )
    rows = list(
        session.execute(
            select(PluginLog)
            .where(PluginLog.plugin_id == plugin_id)
            .order_by(PluginLog.ts.desc(), PluginLog.log_id.desc())
            .limit(capped)
        )
        .scalars()
        .all()
    )
    return {
        "plugin_id": plugin_id,
        "limit": capped,
        "total": int(total or 0),
        "logs": [_log_row(row) for row in rows],
    }
