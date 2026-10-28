"""`GET /api/health`：版本可比对（R20）、instance_role 兜底、各接入机拨号态如实呈现。

未迁移或落后版本的库必须回 `degraded` + 一条可读的 `problems`，而不是崩溃或装作正常。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import APP_VERSION
from ..deps import get_session
from ..models import AppConfig, Gateway

router = APIRouter(tags=["health"])


def effective_instance_role(session: Session, fallback: str) -> str:
    """缺行、值为 NULL、表都还没建，三种情况一律按 fallback（默认 site）走。"""
    try:
        stored = session.scalar(select(AppConfig.instance_role).where(AppConfig.id == 1))
    except OperationalError:
        session.rollback()
        return fallback
    return stored or fallback


def gateway_counts(session: Session) -> dict | None:
    """读不到 gateway 表时返回 None：统计不出来就明说，不得报成一堆 0。"""
    try:
        rows = session.execute(
            select(Gateway.dial_state, func.count()).group_by(Gateway.dial_state)
        ).all()
        failing = session.scalar(
            select(func.count())
            .select_from(Gateway)
            .where(Gateway.dial_state.is_(None), Gateway.dial_failures > 0)
        ) or 0
    except OperationalError:
        session.rollback()
        return None
    states = dict(rows)
    never_probed = states.get(None, 0)
    return {
        "total": sum(states.values()),
        "online": states.get("online", 0),
        "unreachable": states.get("unreachable", 0),
        "dialing": states.get("dialing", 0),
        # 拨过但还没成功过（§1 不允许一次超时就判死，所以它既不是 online 也不是 unreachable）
        "failing": failing,
        "never_probed": never_probed - failing,
    }


@router.get("/api/health")
def health(request: Request, session: Session = Depends(get_session)) -> dict:
    db = request.app.state.db
    schema_version = db.schema_version()
    head = request.app.state.head_revision

    problems: list[str] = []
    if schema_version is None:
        problems.append("库里读不到 alembic_version：先运行 python migrate.py")
    elif schema_version != head:
        problems.append(f"schema 版本不一致：库里 {schema_version}，代码期望 {head}")

    gateways = gateway_counts(session)
    if gateways is None:
        problems.append("gateway 表不可读：接入机数量无法统计")

    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "app_version": APP_VERSION,
        "schema_version": schema_version,
        "expected_schema_version": head,
        "instance_role": effective_instance_role(
            session, request.app.state.settings.instance_role
        ),
        "db_path": str(db.path),
        "gateways": gateways,
        # S2 没有拨号器：如实报未运行，不得画成"已连接"。
        "dialer": {"running": False},
    }
