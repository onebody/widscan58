"""`app_config` 那一行是实例级设置的唯一真源；这里是它的唯一读侧。

`effective_instance_role()` 原先长在 `routers/health.py` 里。S3 的拨号循环也必须按同一个
口径决定"我是不是汇聚实例"（汇聚实例不拨号），两处各写一遍就等着漂移——所以挪到这里，
由健康检查与 dialer 共同引用。

兜底规则一条不变：缺行、值为 NULL、表还没建，三种情况一律按 `Settings.instance_role`
（默认 `site`）走，并且不抛异常——未迁移的库也要能回答"我是什么角色"。
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
