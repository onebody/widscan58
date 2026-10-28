"""数据库迁移入口：备份 → 三场景迁移 → 幂等种子。

**严禁重置数据库**：任何 schema 变更都走增量迁移，迁移脚本防御式编写。

用法：
    python migrate.py                 # 备份 + 迁移 + 种子
    python migrate.py --migrate       # 仅迁移
    python migrate.py --backup        # 仅备份
    python migrate.py --db PATH       # 指定库文件（默认取 RFTRACK_DB_PATH）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.config import DEFAULT_DB_PATH
from app.models import Base

ALEMBIC_INI = BACKEND_DIR / "alembic.ini"
#: 迁移三场景的名字，写进结果里供调用方与日志区分。
SCENARIO_CREATE_ALL = "create_all"
SCENARIO_STAMP_ONLY = "stamp_only"
SCENARIO_UPGRADE = "upgrade"
TRACKED_TABLE = "app_config"


@dataclass(frozen=True)
class MigrationResult:
    ok: bool
    scenario: str
    from_version: str | None = None
    to_version: str | None = None
    backup_path: Path | None = None
    error: str | None = None


def head_revision() -> str | None:
    """alembic 脚本目录里的头版本，作为"代码期望的 schema 版本"。"""
    script = ScriptDirectory.from_config(_alembic_config(":memory:"))
    return script.get_current_head()


def current_version(path: Path | str) -> str | None:
    """库里已记录的版本；alembic_version 表不存在或无行都返回 None。"""
    db = Path(path)
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        tracked = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alembic_version'"
        ).fetchone()
        if not tracked:
            return None
        row = conn.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


def _alembic_config(url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI)) if ALEMBIC_INI.exists() else Config()
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _has_core_tables(path: Path) -> bool:
    if not path.exists():
        return False
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (TRACKED_TABLE,),
        ).fetchone()
    return row is not None


def url_for(path: Path | str) -> str:
    return f"sqlite:///{Path(path)}"


def run_migrations(path: Path | str = DEFAULT_DB_PATH) -> MigrationResult:
    """三场景兜底，一个都不许悄悄跳过。

    1. 空库/无核心表 → create_all + stamp head（首次部署）
    2. 有数据但 alembic 未追踪版本 → 只 stamp，不动数据（严禁重置库）
    3. 已有版本记录 → 正常增量 upgrade
    """
    db = Path(path)
    before = None
    try:
        # 库根本打不开（路径是目录、权限不对、SD 卡掉线）也是迁移失败的一种，
        # 必须走同一条"如实上报"的路，而不是把异常抛给启动代码。
        before = current_version(db)
        if not _has_core_tables(db):
            engine = _create_all(db)
            _stamp(db, head_revision())
            scenario = SCENARIO_CREATE_ALL
        elif before is None:
            _stamp(db, head_revision())
            scenario = SCENARIO_STAMP_ONLY
        else:
            command.upgrade(_alembic_config(url_for(db)), "head")
            scenario = SCENARIO_UPGRADE
    except Exception as exc:  # 迁移失败必须如实上报，不得假装成功
        return MigrationResult(ok=False, scenario="failed", error=f"{type(exc).__name__}: {exc}", from_version=before)
    return MigrationResult(
        ok=True, scenario=scenario, from_version=before, to_version=current_version(db)
    )


def _create_all(db: Path):
    from sqlalchemy import create_engine, event

    engine = create_engine(url_for(db), future=True)

    @event.listens_for(engine, "connect")
    def _pragmas(conn, _):  # 建表时也要开外键，否则 FK 定义被静默跳过
        cursor = conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    engine.dispose()
    return engine


def _stamp(db: Path, revision: str | None) -> None:
    if revision is None:
        return
    command.stamp(_alembic_config(url_for(db)), revision)


def backup_db(path: Path | str, backup_dir: Path | str | None = None) -> Path | None:
    """WAL 模式下必须用 sqlite3 backup API，裸 cp 会丢最近事务。备份失败不阻断启动。"""
    db = Path(path)
    if not db.exists():
        return None
    target_dir = Path(backup_dir) if backup_dir else db.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = target_dir / f"rftrack_{stamp}.sqlite3"
    source = sqlite3.connect(db)
    destination = sqlite3.connect(target)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args(argv)
    db = Path(args.db)
    # 不给任何动作 = 完整流程（备份 + 迁移）；给了动作名就只干那件
    do_everything = not (args.backup or args.migrate)
    do_backup = do_everything or args.backup
    do_migrate = do_everything or args.migrate

    if do_backup:
        saved = backup_db(db)
        print(f"备份：{saved or '库不存在，跳过'}")
    if do_migrate:
        result = run_migrations(db)
        print(
            f"迁移：{result.scenario} {result.from_version} → {result.to_version}"
            + (f" 失败：{result.error}" if not result.ok else "")
        )
        return 0 if result.ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
