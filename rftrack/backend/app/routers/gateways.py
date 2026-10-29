"""接入机 CRUD，挂在中继（位置）之下。一台 = 一个跑 serve.mjs 的进程。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..models import Gateway, Relay
from ..schemas import GatewayCreate, GatewayOut, GatewayUpdate
from ..services.dialer import probe_gateway
from ..services.hierarchy import (
    ensure_gateway_deletable,
    flush_or_conflict,
    get_or_404,
    require_relay,
)

router = APIRouter(prefix="/api/gateways", tags=["gateways"])


def get_gateway(session: Session, gateway_id: int) -> Gateway:
    return get_or_404(session, Gateway, gateway_id, "gateway")


def as_out(gateway: Gateway) -> GatewayOut:
    """site_id 不在 gateway 上存，读的时候经 relay → site 推导出来。"""
    return GatewayOut.model_validate(gateway).model_copy(
        update={
            "site_id": gateway.relay.site_id,
            "relay_name": gateway.relay.name,
        }
    )


@router.get("", response_model=list[GatewayOut])
def list_gateways(
    relay: int | None = None,
    site: int | None = None,
    session: Session = Depends(get_session),
) -> list[GatewayOut]:
    """`?relay=` / `?site=` 都是范围参数，短名与 nodes/endpoints 一致。"""
    query = select(Gateway).order_by(Gateway.gateway_id)
    if relay is not None:
        query = query.where(Gateway.relay_id == relay)
    if site is not None:
        query = query.join(Relay, Gateway.relay_id == Relay.relay_id).where(
            Relay.site_id == site
        )
    return [as_out(gateway) for gateway in session.scalars(query)]


@router.post("", response_model=GatewayOut, status_code=status.HTTP_201_CREATED)
def create_gateway(
    payload: GatewayCreate, session: Session = Depends(require_writable_session)
) -> GatewayOut:
    require_relay(session, payload.relay_id, must_be_active=True)
    gateway = Gateway(**payload.model_dump())
    session.add(gateway)
    flush_or_conflict(
        session, "duplicate_gateway_name", relay_id=payload.relay_id, name=payload.name
    )
    return as_out(gateway)


@router.get("/{gateway_id}", response_model=GatewayOut)
def read_gateway(gateway_id: int, session: Session = Depends(get_session)) -> GatewayOut:
    return as_out(get_gateway(session, gateway_id))


@router.post("/{gateway_id}/probe")
def probe(
    gateway_id: int,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """连通性实测：拨 base_url、取 /api/endpoints、报真实延迟与失败原因。"""
    gateway = get_gateway(session, gateway_id)
    return probe_gateway(session, gateway, request.app.state.transport, request.app.state.settings)


@router.patch("/{gateway_id}", response_model=GatewayOut)
def update_gateway(
    gateway_id: int, payload: GatewayUpdate, session: Session = Depends(require_writable_session)
) -> GatewayOut:
    """`dial_state`/`dial_reason`/`last_seen` 是 probe 的产出，不在可写字段里。"""
    gateway = get_gateway(session, gateway_id)
    changes = payload.model_dump(exclude_unset=True)
    if "base_url" in changes and changes["base_url"] != gateway.base_url:
        # 地址一改，之前测出来的连通性就不再描述它现在的状态
        gateway.dial_state = None
        gateway.dial_reason = None
        gateway.last_seen = None
    for field, value in changes.items():
        setattr(gateway, field, value)
    flush_or_conflict(
        session, "duplicate_gateway_name", relay_id=gateway.relay_id, name=gateway.name
    )
    return as_out(gateway)


@router.delete("/{gateway_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_gateway(
    gateway_id: int, session: Session = Depends(require_writable_session)
) -> Response:
    gateway = get_gateway(session, gateway_id)
    ensure_gateway_deletable(session, gateway)
    session.delete(gateway)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
