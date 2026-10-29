"""engine + session factory + Base：SQLite 走 WAL，外键常开。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import Base, utcnow  # noqa: F401  (Base/utcnow 供 alembic、路由与服务层复用)


def _apply_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


class Database:
    """一个库文件一个实例：测试各自建库，生产由 create_app 建一次。"""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(f"sqlite:///{self.path}", future=True)
        event.listen(self.engine, "connect", _apply_sqlite_pragmas)
        self.session_factory = sessionmaker(
            bind=self.engine, expire_on_commit=False, future=True
        )

    def session(self) -> Session:
        return self.session_factory()

    @contextmanager
    def scope(self) -> Iterator[Session]:
        session = self.session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def schema_version(self) -> str | None:
        """库里真实记录的 alembic 版本；表不存在或无行都返回 None（不抛异常）。"""
        with self.engine.connect() as conn:
            tracked = conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master"
                    " WHERE type='table' AND name='alembic_version'"
                )
            ).first()
            if not tracked:
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return row[0] if row else None

    def has_table(self, name: str) -> bool:
        """库里有没有这张表。与 `schema_version()` 同一条纪律：查不到就是 False，不抛异常。

        存在的理由只有一个：进程对着一个**还没迁移**的库也必须能起来回答健康状态
        （`test_health_does_not_crash_on_an_unmigrated_database` 钉的就是这件事），所以任何
        "启动时顺手写一行"的代码都得先问这一句，而不是等 `no such table` 把 lifespan 炸掉。
        它刻意不看版本号——S4 已经定过调：**版本号与结构事实是两件事**。
        """
        with self.engine.connect() as conn:
            return (
                conn.execute(
                    text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
                    {"n": name},
                ).first()
                is not None
            )

    def dispose(self) -> None:
        self.engine.dispose()


def from_settings(settings: Settings) -> Database:
    return Database(settings.db_path)
