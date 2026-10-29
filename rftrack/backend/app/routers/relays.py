"""中继（物理位置）CRUD，挂在场地之下。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..models import Relay
from ..schemas import RelayCreate, RelayOut, RelayUpdate
from ..services.hierarchy import (
    ensure_relay_archivable,
    ensure_relay_deletable,
    flush_or_conflict,
    get_or_404,
    require_site,
)

router = APIRouter(prefix="/api/relays", tags=["relays"])


def get_relay(session: Session, relay_id: int) -> Relay:
    return get_or_404(session, Relay, relay_id, "relay")


@router.get("", response_model=list[RelayOut])
def list_relays(site: int | None = None, session: Session = Depends(get_session)) -> list[Relay]:
    """`?site=` 与 nodes/endpoints 同一套短名（契约 §4）。"""
    query = select(Relay).order_by(Relay.relay_id)
    if site is not None:
        query = query.where(Relay.site_id == site)
    return list(session.scalars(query))


@router.post("", response_model=RelayOut, status_code=status.HTTP_201_CREATED)
def create_relay(
    payload: RelayCreate, session: Session = Depends(require_writable_session)
) -> Relay:
    require_site(session, payload.site_id, must_be_active=True)
    relay = Relay(site_id=payload.site_id, name=payload.name, note=payload.note)
    session.add(relay)
    flush_or_conflict(
        session, "duplicate_relay_name", site_id=payload.site_id, name=payload.name
    )
    return relay


@router.get("/{relay_id}", response_model=RelayOut)
def read_relay(relay_id: int, session: Session = Depends(get_session)) -> Relay:
    return get_relay(session, relay_id)


@router.patch("/{relay_id}", response_model=RelayOut)
def update_relay(
    relay_id: int, payload: RelayUpdate, session: Session = Depends(require_writable_session)
) -> Relay:
    relay = get_relay(session, relay_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("status") == "archived":
        ensure_relay_archivable(session, relay)
    for field, value in changes.items():
        setattr(relay, field, value)
    flush_or_conflict(
        session, "duplicate_relay_name", site_id=relay.site_id, name=relay.name
    )
    return relay


@router.delete("/{relay_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_relay(
    relay_id: int, session: Session = Depends(require_writable_session)
) -> Response:
    relay = get_relay(session, relay_id)
    ensure_relay_deletable(session, relay)
    session.delete(relay)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
