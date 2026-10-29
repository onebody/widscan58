"""插件的 HTTP 入口（契约 §4 的 `plugins` 那一行：可查看 / 可启停 / 可配置，状态·日志·错误可观察）。

规则都在 `services/plugin_admin.py`，这里只做参数校验、编排与状态码。三件事由这一层负责说清：

- **启停不等于在跑**。`enable` 之后注册表那一行立刻是 1，而运行态还要等运行时的下一拍 `refresh()`
  （≤ `TICK_INTERVAL_S`）；响应里 `enabled` 与 `status` 是两个字段，页面不该把它们合成一句话。
- **读的是本实例的运行时**。`/state` 与列表里那几条队列取自 `app.state.plugins`——与采集侧投喂的是
  同一个对象。换成"端点自己 new 一个"的话，它看到的队列永远是空的，而那不是空而是假。
- **`?site=` 在这里不存在**。插件是**实例级**的登记（一支插件一条队列链），作用域才把它切到某个
  场地或某台接入机；所以列表是全实例的，而"这一支管哪一片"由 `scope`/`scope_id` 两列说。

响应体故意不套 `response_model`：那二十来个字段在 `plugin_admin.as_view()` 里已经有一份，第二份
只会漂移（同 `routers/bridge.py` 与 S6 那几条读侧的做法）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..schemas import PluginConfigUpdate
from ..services import plugin_admin

router = APIRouter(prefix="/api/plugins", tags=["plugins"])


@router.get("")
def list_plugins(request: Request, session: Session = Depends(get_session)) -> dict[str, Any]:
    """代码里那几支插件在库里的样子，外加它们此刻的队列。"""
    return plugin_admin.list_payload(
        session, request.app.state.settings, request.app.state.plugins
    )


@router.get("/{plugin_id}/state")
def plugin_state(
    plugin_id: str, request: Request, session: Session = Depends(get_session)
) -> dict[str, Any]:
    """一支插件的运行态。计数器来自库里那一行，队列来自本实例的运行时。"""
    return plugin_admin.state_payload(
        session, request.app.state.settings, plugin_id, request.app.state.plugins
    )


@router.get("/{plugin_id}/logs")
def plugin_logs(
    plugin_id: str,
    session: Session = Depends(get_session),
    limit: int | None = Query(default=None),
) -> dict[str, Any]:
    """状态跃迁与错误流水，倒序。`total` 是全量条数，不是本页条数。"""
    return plugin_admin.logs_payload(session, plugin_id, limit=limit)


@router.post("/{plugin_id}/enable")
def enable(
    plugin_id: str, request: Request, session: Session = Depends(require_writable_session)
) -> dict[str, Any]:
    """启用一支插件；对一支正在退避的插件，这也是唯一能把重启计数打回 0 的入口。"""
    row, changed = plugin_admin.enable_plugin(session, plugin_id)
    return _action(session, request, row, changed)


@router.post("/{plugin_id}/disable")
def disable(
    plugin_id: str, request: Request, session: Session = Depends(require_writable_session)
) -> dict[str, Any]:
    """停用一支插件。只改登记，不替运行时宣布它已经停下来。"""
    row, changed = plugin_admin.disable_plugin(session, plugin_id)
    return _action(session, request, row, changed)


@router.put("/{plugin_id}/config")
def put_config(
    plugin_id: str,
    payload: PluginConfigUpdate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict[str, Any]:
    """整份替换那三列可编辑的值：作用域、作用域指向、队列上限。"""
    row, changed = plugin_admin.configure_plugin(
        session,
        plugin_id,
        scope=payload.scope,
        scope_id=payload.scope_id,
        queue_limit=payload.queue_limit,
    )
    return _action(session, request, row, changed)


def _action(
    session: Session, request: Request, row: Any, changed: bool
) -> dict[str, Any]:
    """写侧三个端点共用的一份响应：那一行的现状 + 这次到底改没改。

    `changed` 是给页面看的：它决定要不要刷一条"已启用"的提示。幂等的 POST 点了第二次不是错误，
    但也不该假装做了什么。
    """
    view = plugin_admin.view_of(session, request.app.state.settings, row, request.app.state.plugins)
    return {**view, "changed": changed}
