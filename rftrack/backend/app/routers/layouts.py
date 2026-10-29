"""工作台布局与组件实例的 HTTP 入口（契约 §4 的 `layouts` / `widgets` 两行）。

`?site=` 在列表上是**必填**的：多场地定案后"把所有布局混在一起列"没有对应的界面，
而少了这个参数就等于让 A 场地的运营者看见并可能改动 B 场地的布局。

规则都在 `services/layouts.py`，这里只做参数校验、编排与状态码。
"""

from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..schemas import (
    LayoutClone,
    LayoutCreate,
    LayoutOut,
    LayoutUpdate,
    WidgetOut,
    WidgetUpdate,
)
from ..services import layouts

router = APIRouter(prefix="/api/layouts", tags=["layouts"])


@router.get("", response_model=list[LayoutOut])
def list_layouts(
    site: int = Query(...), session: Session = Depends(get_session)
) -> list[LayoutOut]:
    return layouts.list_for_site(session, site)


@router.post("", response_model=LayoutOut, status_code=status.HTTP_201_CREATED)
def create_layout(
    payload: LayoutCreate, session: Session = Depends(require_writable_session)
) -> LayoutOut:
    layout = layouts.create_layout(session, payload)
    return layouts.as_out(session, layout)


@router.get("/{layout_id}", response_model=LayoutOut)
def read_layout(layout_id: int, session: Session = Depends(get_session)) -> LayoutOut:
    return layouts.as_out(session, layouts.get_layout(session, layout_id))


@router.patch("/{layout_id}", response_model=LayoutOut)
def update_layout(
    layout_id: int,
    payload: LayoutUpdate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> LayoutOut:
    layout = layouts.get_layout(session, layout_id)
    updated = layouts.update_layout(
        session, request.app.state.settings, layout, payload
    )
    return layouts.as_out(session, updated)


@router.delete("/{layout_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_layout(
    layout_id: int, session: Session = Depends(require_writable_session)
) -> Response:
    layouts.delete_layout(session, layouts.get_layout(session, layout_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{layout_id}/clone", response_model=LayoutOut, status_code=status.HTTP_201_CREATED)
def clone_layout(
    layout_id: int,
    session: Session = Depends(require_writable_session),
    payload: LayoutClone | None = Body(default=None),
) -> LayoutOut:
    copy = layouts.clone_layout(
        session, layouts.get_layout(session, layout_id), payload.name if payload else None
    )
    return layouts.as_out(session, copy)


@router.post("/{layout_id}/activate", response_model=LayoutOut)
def activate_layout(
    layout_id: int, session: Session = Depends(require_writable_session)
) -> LayoutOut:
    layout = layouts.get_layout(session, layout_id)
    return layouts.as_out(session, layouts.activate_layout(session, layout))


@router.get("/{layout_id}/widgets", response_model=list[WidgetOut])
def list_widgets(
    layout_id: int, request: Request, session: Session = Depends(get_session)
) -> list[WidgetOut]:
    layout = layouts.get_layout(session, layout_id)
    return layouts.list_widgets(session, request.app.state.settings, layout)


@router.put("/{layout_id}/widgets/{instance_id}", response_model=WidgetOut)
def put_widget(
    layout_id: int,
    instance_id: str,
    payload: WidgetUpdate,
    request: Request,
    session: Session = Depends(require_writable_session),
) -> WidgetOut:
    """一个格子的局部配置。生效值里带五层来源链，"这个阈值是谁定的"在页面上看得见。"""
    layout = layouts.get_layout(session, layout_id)
    widget = layouts.put_widget(
        session, request.app.state.settings, layout, instance_id, payload
    )
    return layouts.as_widget_out(session, request.app.state.settings, layout, widget)


@router.delete(
    "/{layout_id}/widgets/{instance_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_widget(
    layout_id: int, instance_id: str, session: Session = Depends(require_writable_session)
) -> Response:
    layouts.delete_widget(session, layouts.get_layout(session, layout_id), instance_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
