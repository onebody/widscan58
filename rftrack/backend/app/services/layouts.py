"""布局与组件实例的业务规则（契约 §4 的 `layouts` / `widgets` 两行）。

五件事只在这里做一次，router 只负责编排：

1. **布局永远属于一个场地**。`?site=` 是筛选口径，不是权限口子——所有读回来的东西都能
   追到自己的 `site_id`，A 场地的布局永不出现在 B 场地的列表里。
2. **`is_active` 每场地唯一**。切换 = 同一事务里清旧设新；库里那条部分唯一索引只是
   并发下的兜底，不是"先撞了再说"的常规路径。
3. **结构版本比这份代码认识的高 → 降级读、拒绝写**。不 500，也不"顺手把版本号改回来"
   ——那等于把用户精心摆好的格子按我们看不懂的语义重写一遍。
4. **实例配置的生效值走 `config_layer` 那条五层链**，这里不另算一遍优先级。
5. **资源硬闸按参与层级重算一遍**。环形缓冲的容量约束不看"值是从哪层来的"，只看这个对象
   最终生效的那组值——否则低层设一个合法值、上层组合出 18000 个点就算绕过去了。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import LAYOUT_VERSION, LayoutWidget, Relay, Site, WorkbenchLayout
from ..schemas import LayoutCreate, LayoutOut, LayoutUpdate, WidgetOut, WidgetUpdate
from .config_layer import (
    JSON_FIELDS,
    capacity_check,
    effective_for_layout,
    effective_for_widget,
    load_json,
    stage_overrides,
)
from .hierarchy import (
    conflict,
    flush_or_conflict,
    get_or_404,
    not_found,
    require_site,
    unprocessable,
)

#: 结构版本高于它 = 这份布局是新代码画的，老代码只能读不能改。
DEGRADE_REASON = "layout_version_too_new"


def get_layout(session: Session, layout_id: int) -> WorkbenchLayout:
    return get_or_404(session, WorkbenchLayout, layout_id, "layout")


def ensure_writable(layout: WorkbenchLayout) -> None:
    if layout.version > LAYOUT_VERSION:
        raise conflict(
            DEGRADE_REASON,
            layout_id=layout.layout_id,
            version=layout.version,
            supported=LAYOUT_VERSION,
        )


def as_out(session: Session, layout: WorkbenchLayout) -> LayoutOut:
    """回显布局。库里存坏的 `defaults_json` 只标问题、不炸整张列表（规范 §4.4 第七条）。"""
    defaults, problem = load_json(layout.defaults_json)
    degraded = layout.version > LAYOUT_VERSION
    widgets = session.scalars(
        select(LayoutWidget).where(LayoutWidget.layout_id == layout.layout_id)
    )
    return LayoutOut(
        layout_id=layout.layout_id,
        site_id=layout.site_id,
        relay_id=layout.relay_id,
        name=layout.name,
        version=layout.version,
        is_active=layout.is_active,
        defaults=defaults,
        declaration_error=problem,
        degraded=degraded,
        degrade_reason=DEGRADE_REASON if degraded else None,
        widget_count=len(list(widgets)),
        created_at=layout.created_at,
        updated_at=layout.updated_at,
    )


def list_for_site(session: Session, site_id: int) -> list[LayoutOut]:
    get_or_404(session, Site, site_id, "site")
    rows = session.scalars(
        select(WorkbenchLayout)
        .where(WorkbenchLayout.site_id == site_id)
        .order_by(WorkbenchLayout.layout_id)
    )
    return [as_out(session, row) for row in rows]


def _check_relay(session: Session, relay_id: int | None, site_id: int) -> None:
    """布局绑位置时，位置必须属于同一个场地——否则"A 场地的工作台"就成立了。"""
    if relay_id is None:
        return
    relay = get_or_404(session, Relay, relay_id, "relay")
    if relay.site_id != site_id:
        raise conflict("relay_foreign_site", relay_id=relay_id, site_id=site_id)


def create_layout(session: Session, payload: LayoutCreate) -> WorkbenchLayout:
    require_site(session, payload.site_id, must_be_active=True)
    _check_relay(session, payload.relay_id, payload.site_id)
    layout = WorkbenchLayout(
        site_id=payload.site_id, relay_id=payload.relay_id, name=payload.name
    )
    session.add(layout)
    flush_or_conflict(
        session, "duplicate_layout_name", site_id=payload.site_id, name=payload.name
    )
    return layout


def update_layout(
    session: Session, settings: Settings, layout: WorkbenchLayout, payload: LayoutUpdate
) -> WorkbenchLayout:
    """改名 / 改绑位置 / 改这一层的覆写。`defaults` 走替换语义，与实例层一致。"""
    ensure_writable(layout)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") is not None:
        layout.name = changes["name"]
    if "relay_id" in changes:
        _check_relay(session, changes["relay_id"], layout.site_id)
        layout.relay_id = changes["relay_id"]
    if "defaults" in changes:
        stored, _problem = load_json(getattr(layout, JSON_FIELDS["layout"]))
        _apply(
            session,
            settings,
            "layout",
            layout,
            _with_cancels(changes["defaults"] or {}, list(stored or {})),
        )
    flush_or_conflict(
        session, "duplicate_layout_name", site_id=layout.site_id, name=layout.name
    )
    session.flush()
    if "defaults" in changes:
        enforce_capacity(session, settings, layout)
    return layout


def _with_cancels(desired: dict[str, Any], present: list[str]) -> dict[str, Any]:
    """整份替换 = 先显式取消"这次没提到的键"，再设上这次提到的。

    取消走 `None`（`Change.apply` 把它从 JSON 里摘掉），而不是直接覆写整个 JSON 字符串：
    后者会绕过注册表，让一个不认识的值混进这一层。
    """
    updates = {key: None for key in present}
    updates.update(desired)
    return updates


def _apply(
    session: Session, settings: Settings, layer: str, target: Any, updates: dict[str, Any]
) -> None:
    changes, errors = stage_overrides(settings, layer, target, updates)
    if errors:
        raise unprocessable("invalid_settings", errors=errors, layer=layer)
    for change in changes:
        change.apply(target)
    session.flush()


def enforce_capacity(
    session: Session,
    settings: Settings,
    layout: WorkbenchLayout,
    widget: LayoutWidget | None = None,
) -> None:
    """布局/实例层的覆写同样不许绕过环形缓冲（规范 §4.4 第九条）。

    判据与全局/场地层共用 `config_layer.capacity_check`，这里只说清"这个对象参与哪几层"。
    位置在应用之后、返回之前：抛出去整笔事务回滚，一个超限的组合要么整个生效、要么整个
    不生效，不许留下"刷新周期改了、时间范围没改"的半套。
    """
    site = session.get(Site, layout.site_id)
    effective = (
        effective_for_widget(session, settings, site=site, layout=layout, widget=widget)
        if widget is not None
        else effective_for_layout(session, settings, site=site, layout=layout)
    )
    detail = capacity_check(settings, effective)
    if detail:
        raise conflict(detail.pop("reason"), layout_id=layout.layout_id, **detail)


def clone_layout(
    session: Session, layout: WorkbenchLayout, name: str | None
) -> WorkbenchLayout:
    """另存为：连同组件与这一层的覆写一起拷，新布局**不**是激活态。

    组件 id 因此不需要重新发一遍——主键含 `layout_id`，两份布局里可以各有一个 `A`。
    """
    ensure_writable(layout)
    copy = WorkbenchLayout(
        site_id=layout.site_id,
        relay_id=layout.relay_id,
        name=name or f"{layout.name} 副本",
        version=layout.version,
        defaults_json=layout.defaults_json,
    )
    session.add(copy)
    flush_or_conflict(
        session, "duplicate_layout_name", site_id=layout.site_id, name=copy.name
    )
    for widget in session.scalars(
        select(LayoutWidget).where(LayoutWidget.layout_id == layout.layout_id)
    ):
        session.add(
            LayoutWidget(
                instance_id=widget.instance_id,
                layout_id=copy.layout_id,
                plugin_id=widget.plugin_id,
                pos_x=widget.pos_x,
                pos_y=widget.pos_y,
                pos_w=widget.pos_w,
                pos_h=widget.pos_h,
                sort=widget.sort,
                config_json=widget.config_json,
            )
        )
    session.flush()
    return copy


def activate_layout(session: Session, layout: WorkbenchLayout) -> WorkbenchLayout:
    """切换当前布局：只清同场地的旧激活项，别的场地的"当前布局"一根手指都不碰。"""
    ensure_writable(layout)
    rival = session.scalar(
        select(WorkbenchLayout).where(
            WorkbenchLayout.site_id == layout.site_id,
            WorkbenchLayout.is_active.is_(True),
            WorkbenchLayout.layout_id != layout.layout_id,
        )
    )
    if rival is not None:
        rival.is_active = False
    layout.is_active = True
    # 两个请求同时抢"当前布局"时，部分唯一索引只会放过一个：翻成 409，别回 500。
    flush_or_conflict(session, "active_layout_conflict", site_id=layout.site_id)
    return layout


def delete_layout(session: Session, layout: WorkbenchLayout) -> None:
    """删布局连带删它的组件配置行（`cascade="all, delete-orphan"` + FK CASCADE）。

    删的正好是当前布局时**不自动激活下一个**：那会让用户一睁眼工作台变了样。
    "没有当前布局"由前端回落到默认布局，是个可以显式呈现的状态。
    """
    for widget in session.scalars(
        select(LayoutWidget).where(LayoutWidget.layout_id == layout.layout_id)
    ):
        session.delete(widget)
    session.delete(layout)
    session.flush()


# --- 组件实例 ---------------------------------------------------------------


def widget_rows(session: Session, layout: WorkbenchLayout) -> list[LayoutWidget]:
    return list(
        session.scalars(
            select(LayoutWidget)
            .where(LayoutWidget.layout_id == layout.layout_id)
            .order_by(LayoutWidget.sort, LayoutWidget.instance_id)
        )
    )


def as_widget_out(
    session: Session, settings: Settings, layout: WorkbenchLayout, widget: LayoutWidget
) -> WidgetOut:
    config, problem = load_json(widget.config_json)
    site = session.get(Site, layout.site_id)
    return WidgetOut(
        instance_id=widget.instance_id,
        layout_id=widget.layout_id,
        plugin_id=widget.plugin_id,
        pos_x=widget.pos_x,
        pos_y=widget.pos_y,
        pos_w=widget.pos_w,
        pos_h=widget.pos_h,
        sort=widget.sort,
        config=config or {},
        declaration_error=problem,
        effective=effective_for_widget(
            session, settings, site=site, layout=layout, widget=widget
        ),
    )


def list_widgets(
    session: Session, settings: Settings, layout: WorkbenchLayout
) -> list[WidgetOut]:
    return [as_widget_out(session, settings, layout, row) for row in widget_rows(session, layout)]


def put_widget(
    session: Session,
    settings: Settings,
    layout: WorkbenchLayout,
    instance_id: str,
    payload: WidgetUpdate,
) -> LayoutWidget:
    """写入或新建一个组件实例（PUT = upsert，`config` 整份替换）。

    `config` 里没出现的旧键会被显式取消，所以"这层不设了"不需要前端再发一次删除动作；
    显式 `null` 落在同一条取消路径上。
    """
    ensure_writable(layout)
    widget = session.get(LayoutWidget, (instance_id, layout.layout_id))
    if widget is None:
        if payload.plugin_id is None:
            raise unprocessable(
                "plugin_id_required", layout_id=layout.layout_id, instance_id=instance_id
            )
        widget = LayoutWidget(
            instance_id=instance_id, layout_id=layout.layout_id, plugin_id=payload.plugin_id
        )
        session.add(widget)
        session.flush()
    elif payload.plugin_id is not None and payload.plugin_id != widget.plugin_id:
        # 改配置不该顺手把这块格子换成另一种插件：那是"删了再建"，不是一回事。
        raise conflict(
            "plugin_immutable",
            instance_id=instance_id,
            stored=widget.plugin_id,
            requested=payload.plugin_id,
        )

    stored, _problem = load_json(widget.config_json)
    _apply(
        session, settings, "instance", widget, _with_cancels(payload.config, list(stored or {}))
    )
    for field in ("pos_x", "pos_y", "pos_w", "pos_h", "sort"):
        value = getattr(payload, field)
        if value is not None:
            setattr(widget, field, value)
    session.flush()
    enforce_capacity(session, settings, layout, widget)
    return widget


def delete_widget(session: Session, layout: WorkbenchLayout, instance_id: str) -> None:
    widget = session.get(LayoutWidget, (instance_id, layout.layout_id))
    if widget is None:
        raise not_found("widget", instance_id)
    session.delete(widget)
    session.flush()
