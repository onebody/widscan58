"""Alembic 运行环境：库地址由调用方注入，不在 ini 里写死。

migrate.py 通过 `sqlalchemy.url` 主选项传库路径；命令行直接调用时退回
Settings.from_env()，两条路都指向同一个库，避免"迁移了 A 库、应用读 B 库"。
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import Settings  # noqa: E402
from app.models import Base  # noqa: E402

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers` 的默认值是 True：只要有人在本进程里跑过一次迁移，
    # 之前 `import` 出来的每个 `rftrack.*` logger 就被静默 `disabled`，此后
    # `LOG.info()` 连记录都不会生成。S6 的结构化保留日志正是"清理真的跑过"的唯一凭据，
    # 所以这一句不是可选的整洁，是那条判据的前提（`tests/test_retention.py` 钉住它）。
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or Settings.from_env().database_url


def _configure(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        # SQLite 的 ALTER 能力有限，批量模式才能做列变更
        render_as_batch=True,
        compare_type=True,
        version_table="alembic_version",
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    try:
        with engine.connect() as connection:
            _configure(connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
