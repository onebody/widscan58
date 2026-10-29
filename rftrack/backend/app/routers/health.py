"""`GET /api/health`：版本可比对（R20）、instance_role 兜底、各接入机拨号态如实呈现。

未迁移或落后版本的库必须回 `degraded` + 一条可读的 `problems`，而不是崩溃或装作正常。
`dialer` 一节在 S3 之后来自 `DialCoordinator.status()` 的真实内存状态：没在跑就报没在跑，
没启用就报没启用——这一节是运营者判断"节点表为什么不更新"的第一入口。

`schema_gaps` 一节是 S4 补的：`alembic_version` 只是一行文字，它写着 head 而库里少了
`workbench_layout` 是完全可能的（半路拷过来的库文件）。那种情况下只比版本号的健康检查会
说"schema 最新"，而 `/api/layouts` 正在 500——所以版本号说没问题时再核一次结构事实。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .. import APP_VERSION
from ..deps import get_session
from ..models import Gateway
from ..services.instance import effective_instance_role

import migrate

router = APIRouter(tags=["health"])


def schema_gaps(app) -> list[str]:
    """结构事实一个进程查一次并缓存：schema 在运行期不会自己变，要变得重启。

    只在版本号已经说"我是最新的"时才查——版本号本身就落后时，`problems` 里已经有更该先
    说的结论，那次 PRAGMA 扫描纯属多余（SD 卡上的读放大是 R11 关心的东西）。
    """
    if app.state.schema_gaps is None:
        app.state.schema_gaps = migrate.missing_structure(app.state.settings.db_path)
    return list(app.state.schema_gaps)


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
    gaps = schema_gaps(request.app) if schema_version == head else []
    if gaps:
        problems.append("schema 版本对得上但结构不齐：" + "、".join(gaps))

    gateways = gateway_counts(session)
    if gateways is None:
        problems.append("gateway 表不可读：接入机数量无法统计")

    dialer = request.app.state.dialer.status()
    if dialer["problem"]:
        problems.append(dialer["problem"])

    return {
        "status": "ok" if not problems else "degraded",
        "problems": problems,
        "app_version": APP_VERSION,
        "schema_version": schema_version,
        "expected_schema_version": head,
        "schema_gaps": gaps,
        "instance_role": effective_instance_role(
            session, request.app.state.settings.instance_role
        ),
        "db_path": str(db.path),
        "gateways": gateways,
        "dialer": dialer,
        # `/ws/bridge` 的实时占用：谁在看哪块板、写权在谁手上。没人在连也要说得出口
        # （`sessions: []`）——"这张表还没做"和"现在确实没页在连"是两个不同的结论。
        "bridge": request.app.state.bridge.status(),
        # 页面挂没挂。工作台是可选件，所以这一节只呈现事实、不参与 `status` 判定：
        # 把"这台只提供 API"报成 degraded，运营者就会开始忽略 degraded。
        "workbench": request.app.state.workbench,
    }
