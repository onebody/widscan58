"""依赖注入：库会话按请求开合。鉴权在 S2 只做只读 token 的口子（S11 用）。"""

from __future__ import annotations

from typing import Iterator

from fastapi import Request
from sqlalchemy.orm import Session

from .database import Database


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_session(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.scope() as session:
        yield session
