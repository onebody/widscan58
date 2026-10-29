"""场地 CRUD。删除纪律：只允许 archived、下面必须真空，删场地不清数据。

写侧用 `require_writable_session`：汇聚实例上一切 POST/PATCH/DELETE 都是 405。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..models import Site
from ..schemas import SiteCreate, SiteOut, SiteUpdate
from ..services.hierarchy import ensure_site_deletable, flush_or_conflict, get_or_404

router = APIRouter(prefix="/api/sites", tags=["sites"])


def get_site(session: Session, site_id: int) -> Site:
    return get_or_404(session, Site, site_id, "site")


@router.get("", response_model=list[SiteOut])
def list_sites(session: Session = Depends(get_session)) -> list[Site]:
    return list(session.scalars(select(Site).order_by(Site.site_id)))


@router.post("", response_model=SiteOut, status_code=status.HTTP_201_CREATED)
def create_site(
    payload: SiteCreate, session: Session = Depends(require_writable_session)
) -> Site:
    site = Site(**payload.model_dump())
    session.add(site)
    flush_or_conflict(session, "duplicate_slug", slug=payload.slug)
    return site


@router.get("/{site_id}", response_model=SiteOut)
def read_site(site_id: int, session: Session = Depends(get_session)) -> Site:
    return get_site(session, site_id)


@router.patch("/{site_id}", response_model=SiteOut)
def update_site(
    site_id: int, payload: SiteUpdate, session: Session = Depends(require_writable_session)
) -> Site:
    """`slug` 不可改：它是归档包与汇聚里认场地的键。"""
    site = get_site(session, site_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(site, field, value)
    session.flush()
    return site


@router.delete("/{site_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_site(
    site_id: int, session: Session = Depends(require_writable_session)
) -> Response:
    site = get_site(session, site_id)
    ensure_site_deletable(session, site)
    session.delete(site)
    session.flush()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
