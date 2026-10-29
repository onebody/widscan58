"""`app_config` 那一行是实例级设置的唯一真源；这里是它的唯一读侧。

`effective_instance_role()` 原先长在 `routers/health.py` 里。S3 的拨号循环也必须按同一个
口径决定"我是不是汇聚实例"（汇聚实例不拨号），两处各写一遍就等着漂移——所以挪到这里，
由健康检查与 dialer 共同引用。

兜底规则一条不变：缺行、值为 NULL、表还没建，三种情况一律按 `Settings` 里的对应字段
（`instance_role` 默认 `site`）走，并且不抛异常——未迁移的库也要能回答"我是什么角色"。

`effective_current_site_id()` 是 S4 补的同一件事的另一半："本实例服务哪个场地"也是库里
那一行说了算，`RFTRACK_SITE_ID` 只是兜底；拨号循环与设置页必须读同一个口径，否则页面上
写着场地 1、循环却按环境变量拨了场地 2。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..models import AppConfig


def effective_instance_role(session: Session, fallback: str) -> str:
    try:
        stored = session.scalar(select(AppConfig.instance_role).where(AppConfig.id == 1))
    except OperationalError:
        session.rollback()
        return fallback
    return stored or fallback


def effective_current_site_id(session: Session, fallback: int | None) -> int | None:
    """本实例服务哪个场地：库里那一行优先，NULL / 缺行 / 列还没迁进来都退回环境变量。

    和上面同一个兜底纪律，也同一个理由：拨号循环不许因为"配置读不出来"而停摆，
    但也不许读不出来就当读到了 0——那一台会静默变成"不限场地"。
    """
    try:
        stored = session.scalar(select(AppConfig.current_site_id).where(AppConfig.id == 1))
    except OperationalError:
        session.rollback()
        return fallback
    return stored if stored is not None else fallback
