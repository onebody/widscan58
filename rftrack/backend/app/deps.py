"""依赖注入：库会话按请求开合。

两条会话依赖的区别只有一件事：**这台实例有没有资格写**。

- `get_session`：读侧。
- `require_writable_session`：写侧。汇聚实例（`instance_role=aggregator`，局域网只读汇聚）
  一律 405 拒掉——它的角色定义就是"看别人，不改任何东西"，改配置、建场地、切板卡模式
  都不例外。这条判据读的是 `effective_instance_role()`，与健康检查/拨号循环同一个口径。

S4 裁定的"权限"只有这一层约束，**不含 token**（token 归 S11）。所以这里没有 403：
403 意味着"你是谁所以不行"，而今天的拒绝与你是谁无关，是这台实例的角色不行。

另外两条与会话无关，管的是"读哪个场地的东西"，区别只在缺省时怎么办：

- `require_site_param`：`?site=` 必填（布局这类按场地隔离、没有"本场地"可言的资源）。
- `site_or_current`：`?site=` 可缺省，缺省落到本实例的当前场地（历史与事件取数）。
"""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .database import Database
from .models import Site
from .services.hierarchy import unprocessable
from .services.instance import effective_current_site_id, effective_instance_role


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


def site_or_current(
    request: Request,
    site: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> int:
    """`?site=` 可缺省的读侧：缺省时落到本实例的**当前场地**，绝不落到"全部场地"。

    契约 §4 那句"`?site=` 缺省=当前场地，不默认跨场地全表扫"在这里实现一次，历史取数与事件流
    都走这道。缺省值取自 `effective_current_site_id()`——拨号循环、设置页与健康检查用的就是它，
    在这儿再算一遍优先级就会漂（`current_site_id` 的"库里那一行 > `RFTRACK_SITE_ID`"两层）。

    读不到当前场地时，还有一条与拨号侧同口径的推断：**库里只有一个场地**就不必指定。
    两个以上而没设当前场地时这台实例**没有"缺省该是谁"这个事实**，只能拒绝并说出键名，
    不能挑第一个：多场地实例上"挑第一个"会让一页安静地显示另一个场地的历史。
    """
    if site is not None:
        return site
    settings: Settings = request.app.state.settings
    current = effective_current_site_id(session, settings.current_site_id)
    if current is None:
        only = set(session.scalars(select(Site.site_id)))
        current = only.pop() if len(only) == 1 else None
    if current is None:
        raise unprocessable(
            "site_required",
            param="site",
            hint="缺少 ?site=，而本实例也没设当前场地（current_site_id / RFTRACK_SITE_ID）",
        )
    return current
