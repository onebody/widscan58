"""依赖注入：库会话按请求开合。

两条会话依赖的区别只有一件事：**这台实例有没有资格写**。

- `get_session`：读侧。
- `require_writable_session`：写侧。汇聚实例（`instance_role=aggregator`，局域网只读汇聚）
  一律 405 拒掉——它的角色定义就是"看别人，不改任何东西"，改配置、建场地、切板卡模式
  都不例外。这条判据读的是 `effective_instance_role()`，与健康检查/拨号循环同一个口径。

S4 裁定的"权限"只有这一层约束，**不含 token**（token 归 S11）。所以这里没有 403：
403 意味着"你是谁所以不行"，而今天的拒绝与你是谁无关，是这台实例的角色不行。

第三条 `require_site_param` 与会话无关，管的是"读哪个场地的东西"——它存在的理由见函数本身。
"""

from __future__ import annotations

from typing import Iterator

from fastapi import HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from .database import Database
from .services.hierarchy import unprocessable
from .services.instance import effective_instance_role


def get_database(request: Request) -> Database:
    return request.app.state.db


def get_session(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.scope() as session:
        yield session


def require_writable_session(request: Request) -> Iterator[Session]:
    db: Database = request.app.state.db
    with db.scope() as session:
        role = effective_instance_role(session, request.app.state.settings.instance_role)
        if role == "aggregator":
            raise HTTPException(
                status.HTTP_405_METHOD_NOT_ALLOWED,
                {
                    "reason": "aggregator_read_only",
                    "instance_role": role,
                    "detail": "汇聚实例只读：配置与层级只在场地实例上改",
                },
            )
        yield session


def require_site_param(site: int | None = Query(default=None)) -> int:
    """必填的 `?site=` 一律走这道依赖。

    S5 建前端时补的收口（方案 §4 申报④）：`Query(...)` 也能拦住缺参，但它吐的是
    FastAPI 自己的校验体（`detail` 是个数组），而全站其余错误都是 `{"reason": …}`。
    页面既然按 `reason` 分支处理，就不该为这一个端点写特例——参数名写错的那句
    `hint` 也是给页面看的，不是给我们自己看的。
    """
    if site is None:
        raise unprocessable(
            "site_required", param="site", hint="缺少查询参数 ?site=：布局按场地隔离，不跨场地列"
        )
    return site
