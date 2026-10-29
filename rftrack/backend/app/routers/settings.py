"""设置系统的对客入口（契约 §4 的 `settings` 两行 + localStorage 一次性导入）。

三条口径决定了这里的形状：

- **读侧回"生效值 + 来源链"，不回"这一层存了什么"**。运营者要的是"现在到底是几、是谁
  定的"；只回原始列值等于让每个页面自己再算一遍优先级，那就是第二份算法。
- **写侧的错误分两类**：值本身不对（含不认识这个键、这层没资格设、`locked` 键想改）→
  422 `invalid_settings` 带 `[{key, reason}]`；与运行状态相冲（版本被别人推进过、
  这个键要重启采集却没确认、要的点数装不进环形缓冲）→ 409 带原因。
- **S4 的"权限"就是层约束**（2026-10-29 裁定，token 归 S11）：`instance_role` 这类
  `locked` 键任何层级都拒写，汇聚实例的一切写都是 405。这里没有 403——今天没人被拒于
  "你是谁"，被拒的都是"这个键/这个角色不行"。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..config import Settings
from ..deps import get_session, require_writable_session
from ..models import Site
from ..schemas import (
    GlobalSettingsUpdate,
    LocalImportCreate,
    SiteSettingsUpdate,
)
from ..services.config_layer import (
    LAYERS,
    capacity_check,
    effective_for_site,
    ensure_app_config,
    read_app_config,
    schema_document,
    spec_for,
    stage_overrides,
    touch,
)
from ..services.instance import effective_instance_role
from ..services.local_import import (
    MAX_PAYLOAD_CHARS,
    detail as import_detail,
    get_row,
    list_rows,
    payload_too_large,
    record_import,
    revert_import,
    summary as import_summary,
)
from ..services.hierarchy import conflict, get_or_404, not_found, unprocessable

router = APIRouter(prefix="/api/settings", tags=["settings"])


# --- 读侧 -------------------------------------------------------------------


def state(session: Session, settings: Settings, site: Site | None = None) -> dict:
    """一份完整的"能看见的当前配置"：版本 + 角色 + 生效值与来源链。

    库里还没有 `app_config` 那一行时回 `version: 0`——它和"有行但全是 NULL"在生效值上
    等价，但在乐观并发上不等价：客户端带着 0 来写，就该把这一行建出来并拿到 1。
    """
    config = read_app_config(session)
    return {
        "version": config.version if config else 0,
        "instance_role": effective_instance_role(session, settings.instance_role),
        "site_id": site.site_id if site else None,
        "settings": effective_for_site(session, settings, site),
    }


@router.get("/global")
def read_global(request: Request, session: Session = Depends(get_session)) -> dict:
    """默认 + 全局两层的生效值（`?site=` 那份在 `/site/{id}`，五层那份在 widgets 上）。"""
    return state(session, request.app.state.settings)


@router.get("/site/{site_id}")
def read_site_settings(
    site_id: int, request: Request, session: Session = Depends(get_session)
) -> dict:
    site = get_or_404(session, Site, site_id, "site")
    return state(session, request.app.state.settings, site)


@router.get("/schema")
def read_schema(request: Request) -> dict:
    """键注册表的对外声明：层级、值域、边界、单位、要不要确认、哪一层还没有执行者。

    前端按这份声明渲染控件（规范 §2.3：**未声明即不支持**），不自己抄候选值。
    """
    return {"layers": list(LAYERS), "keys": schema_document(request.app.state.settings)}


# --- 写侧 -------------------------------------------------------------------


def _needs_confirm(updates: dict[str, Any], confirm: list[str]) -> list[str]:
    """动了"要重启采集"的键却没逐个确认——包括设成 null 取消覆写，那也会换拨号对象。"""
    return [
        key
        for key in updates
        if (spec := spec_for(key)) is not None and spec.requires_restart and key not in confirm
    ]


def _enforce_capacity(settings: Settings, effective: dict[str, dict]) -> None:
    """跨键的资源硬闸：写入后的生效组合必须真的装得进环形缓冲。

    放在应用之后、提交之前——单看一个键永远合法，`refresh_ms=200` 配
    `time_range_s=3600` 才会画出 18000 个点。抛出去整笔回滚，库里不会留下半套配置。
    判据本身在 `config_layer.capacity_check`，布局与组件层用的是同一个口径。
    """
    detail = capacity_check(settings, effective)
    if detail:
        raise conflict(detail.pop("reason"), **detail)


def _write(
    session: Session,
    settings: Settings,
    layer: str,
    target: Any,
    updates: dict[str, Any],
    confirm: list[str],
    *,
    site: Site | None = None,
) -> dict:
    changes, errors = stage_overrides(settings, layer, target, updates)
    if errors:
        raise unprocessable("invalid_settings", errors=errors, layer=layer)
    pending = _needs_confirm(updates, confirm)
    if pending:
        raise conflict("confirm_required", keys=pending)
    for change in changes:
        change.apply(target)
    session.flush()
    snapshot = state(session, settings, site)
    _enforce_capacity(settings, snapshot["settings"])
    return {"state": snapshot, "changes": changes}


@router.put("/global")
def write_global(
    payload: GlobalSettingsUpdate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    """全局层写入。`version` 不匹配就 409：不许把别人刚改的东西静默盖掉。"""
    settings = request.app.state.settings
    config = ensure_app_config(session)
    if payload.version != config.version:
        raise conflict(
            "version_conflict", expected=payload.version, actual=config.version
        )
    target_site = payload.settings.get("current_site_id")
    if isinstance(target_site, int) and session.get(Site, target_site) is None:
        # 指向一个不存在的场地：拨号循环会把每一台都拒成 `foreign_site`，
        # 那是最难查的一种"配置没问题但什么都没在跑"。在这里就拦住。
        raise unprocessable("unknown_site_id", site_id=target_site)
    result = _write(
        session, settings, "global", config, payload.settings, payload.confirm
    )
    if result["changes"]:
        touch(session, config)
    return {
        **result["state"],
        "applied": [change.as_dict() for change in result["changes"]],
    }


@router.put("/site/{site_id}")
def write_site_settings(
    site_id: int,
    payload: SiteSettingsUpdate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    """场地层写入。契约只给全局层配了 `version`，场地层靠来源链回显兜并发。"""
    settings = request.app.state.settings
    site = get_or_404(session, Site, site_id, "site")
    result = _write(
        session, settings, "site", site, payload.settings, confirm=[], site=site
    )
    session.flush()
    return {
        **result["state"],
        "applied": [change.as_dict() for change in result["changes"]],
    }


# --- localStorage 一次性导入 -------------------------------------------------


@router.post("/local-import", status_code=status.HTTP_201_CREATED)
def import_local(
    payload: LocalImportCreate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    """把原 SPA 的 localStorage 导进**全局层**，并留下可回退的痕。

    认不出来的键不会消失：它们原样出现在 `unmapped` 里（含值），映射规则因此只许有这一份。
    整包超过 `MAX_PAYLOAD_CHARS` 直接 413——留痕是要进库的，不设上限等于让一个页面向导
    撑爆 SD 卡。
    """
    if payload_too_large(payload.entries):
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            {"reason": "payload_too_large", "limit_chars": MAX_PAYLOAD_CHARS},
        )
    settings = request.app.state.settings
    config = ensure_app_config(session)
    row, result = record_import(
        session, settings, config, payload.entries, payload.source
    )
    if result["changes"]:
        touch(session, config)
    return import_detail(row)


@router.get("/local-import")
def list_local_imports(session: Session = Depends(get_session)) -> list[dict]:
    """只回计数与来源；原始 payload 在详情里，列表不该把整份 localStorage 拖到页面上。"""
    return [import_summary(row) for row in list_rows(session)]


@router.get("/local-import/{import_id}")
def read_local_import(import_id: int, session: Session = Depends(get_session)) -> dict:
    return import_detail(_import_row(session, import_id))


@router.post("/local-import/{import_id}/revert")
def revert_local_import(
    import_id: int,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> dict:
    """按留痕把全局层写回导入之前；行不删，只标 `reverted_at`。

    多次导入要按时间**倒序**撤销：还原的是这次触及的那几个键，先撤早的那次会把后一次的
    值一起抹掉——这是留痕设计的固有语义，写在响应里而不是藏在代码里。
    """
    row = _import_row(session, import_id)
    if row.reverted_at is not None:
        # `isoformat()` 不是风格问题：SQLite 读回来的 datetime 是 naive 的，塞进 409 的
        # detail 会让 JSONResponse 自己炸 500——拒绝的理由反而成了第二个错误。
        raise conflict(
            "already_reverted", import_id=import_id, reverted_at=row.reverted_at.isoformat()
        )
    settings = request.app.state.settings
    config = ensure_app_config(session)
    result = revert_import(session, settings, config, row)
    if result["restored"]:
        touch(session, config)
    return {"import": import_detail(row), **result}


def _import_row(session: Session, import_id: int):
    row = get_row(session, import_id)
    if row is None:
        raise not_found("local_settings_import", import_id)
    return row
