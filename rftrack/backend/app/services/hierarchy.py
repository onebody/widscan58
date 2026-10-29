"""层级三级共用的业务规则。

规则本身来自 `docs/rf-track/RF_Track_落地实施方案与计划.md` §1 状态机与 §4 API 契约，
放在这里是为了让三个 router 保持"只做参数校验和编排"：

- 取不到就是 404，不给空对象；
- archived 的上级禁止再挂下级；
- 删除只在"已归档 + 下面真的空了"时成立，**有下级就 409，绝不级联删数据**；
- "还在拨号"比"还有下级"先报，运营者拿到的才是可执行的下一步。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Gateway, Node, Relay, Site

ModelT = TypeVar("ModelT")


def not_found(kind: str, ident: int) -> HTTPException:
    return HTTPException(
        status.HTTP_404_NOT_FOUND, {"reason": "not_found", "kind": kind, "id": ident}
    )


def conflict(reason: str, **detail) -> HTTPException:
    return HTTPException(status.HTTP_409_CONFLICT, {"reason": reason, **detail})


def unprocessable(reason: str, **detail) -> HTTPException:
    """S4 起用到的第三种口径：**逐字段**说清哪里不对，而不是回一个笼统的 400。

    设置页要靠这份 `errors` 把红点打在具体的键上，所以结构是 `[{key, reason}]` 而不是
    一句人话——客户端不该去解析自然语言。

    用 `..._CONTENT` 而不是 `..._ENTITY`：同一个数字 422，后者在钉住的 starlette 里已
    标废弃（每次访问都刷一条 DeprecationWarning，把真实告警埋掉）。
    """
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reason": reason, **detail})


def get_or_404(session: Session, model: type[ModelT], ident: int, kind: str) -> ModelT:
    obj = session.get(model, ident)
    if obj is None:
        raise not_found(kind, ident)
    return obj


def flush_or_conflict(session: Session, reason: str, **detail) -> None:
    """唯一约束在 INSERT 时就会炸；炸了要翻成 409，不能让启动方看到 500。"""
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise conflict(reason, **detail) from None


def count_rows(session: Session, model, *conditions) -> int:
    return session.scalar(
        select(func.count()).select_from(model).where(*conditions)
    ) or 0


def online_gateway_count(
    session: Session, *, site_id: int | None = None, relay_id: int | None = None
) -> int:
    conditions = [Gateway.dial_state == "online"]
    if relay_id is not None:
        conditions.append(Gateway.relay_id == relay_id)
    if site_id is not None:
        conditions.append(Relay.site_id == site_id)
        return session.scalar(
            select(func.count())
            .select_from(Gateway)
            .join(Relay, Gateway.relay_id == Relay.relay_id)
            .where(*conditions)
        ) or 0
    return count_rows(session, Gateway, *conditions)


# --- 上级必须活着 -----------------------------------------------------------


def require_site(session: Session, site_id: int, *, must_be_active: bool) -> Site:
    site = get_or_404(session, Site, site_id, "site")
    if must_be_active and site.status != "active":
        raise conflict("site_archived", site_id=site_id)
    return site


def require_relay(session: Session, relay_id: int, *, must_be_active: bool) -> Relay:
    relay = get_or_404(session, Relay, relay_id, "relay")
    if must_be_active:
        require_site(session, relay.site_id, must_be_active=True)
        if relay.status != "active":
            raise conflict("relay_archived", relay_id=relay_id)
    return relay


# --- 删除与归档的阻塞条件 ----------------------------------------------------


def ensure_site_deletable(session: Session, site: Site) -> None:
    if site.status != "archived":
        raise conflict("site_not_archived", site_id=site.site_id)
    online = online_gateway_count(session, site_id=site.site_id)
    if online:
        raise conflict("gateways_online", site_id=site.site_id, count=online)
    relays = count_rows(session, Relay, Relay.site_id == site.site_id)
    if relays:
        # 删场地不清数据：要把下面的事件/布局一起带走，请走归档导出后人工处理
        raise conflict("site_has_relays", site_id=site.site_id, count=relays)


def ensure_relay_deletable(session: Session, relay: Relay) -> None:
    online = online_gateway_count(session, relay_id=relay.relay_id)
    if online:
        raise conflict("gateways_online", relay_id=relay.relay_id, count=online)
    gateways = count_rows(session, Gateway, Gateway.relay_id == relay.relay_id)
    if gateways:
        raise conflict("relay_has_gateways", relay_id=relay.relay_id, count=gateways)


def ensure_relay_archivable(session: Session, relay: Relay) -> None:
    online = online_gateway_count(session, relay_id=relay.relay_id)
    if online:
        raise conflict("gateways_online", relay_id=relay.relay_id, count=online)


def ensure_gateway_deletable(session: Session, gateway: Gateway) -> None:
    if gateway.dial_state == "online":
        raise conflict("gateways_online", gateway_id=gateway.gateway_id, count=1)
    nodes = count_rows(session, Node, Node.gateway_id == gateway.gateway_id)
    if nodes:
        raise conflict("gateway_has_nodes", gateway_id=gateway.gateway_id, count=nodes)
