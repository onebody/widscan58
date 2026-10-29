"""迁移三场景 + 备份：S2 出口判据"**严禁重置库** 的迁移三场景在空库/有数据无版本/正常 upgrade 各验一次"。

还有一条计划里没写但同等重要的：`create_all + stamp` 与 `alembic upgrade head` 是两条
都能把空库变成 schema 的路，两条路必须落到同一个结构上，否则"我这台机器当初走的是哪条"
会变成排查不掉的差异（R20 的同类）。
"""

import sqlite3

import pytest
from sqlalchemy import create_engine

import migrate
from app.models import Base


def _tables(path):
    with sqlite3.connect(path) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _site_count(path):
    if "site" not in _tables(path):
        return None
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT count(*) FROM site").fetchone()[0]


def _insert_site(path, slug):
    """裸 SQL 插入：绕过 ORM 的 python 端 default，时间戳要自己给。"""
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO site (site_id, name, slug, timezone, status, origin, created_at, updated_at) "
            "VALUES (1, ?, ?, 'UTC', 'active', 'local', '2026-10-29 00:00:00+00:00', '2026-10-29 00:00:00+00:00')",
            (slug, slug),
        )
        conn.commit()


def test_empty_database_takes_the_create_all_scenario_and_stamps_head(tmp_path):
    db = tmp_path / "fresh.sqlite3"
    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert result.scenario == migrate.SCENARIO_CREATE_ALL
    assert result.from_version is None
    assert result.to_version == migrate.head_revision()
    assert {"app_config", "site", "relay", "gateway", "node"} <= _tables(db)


def test_existing_data_without_a_version_row_is_stamped_and_untouched(tmp_path):
    """场景 2：库里有业务表有数据、alembic 却没记版本——数据一行不许动，结构要补齐。"""
    db = tmp_path / "legacy.sqlite3"
    Base.metadata.create_all(create_engine(migrate.url_for(db)))
    _insert_site(db, "home")
    assert "alembic_version" not in _tables(db)

    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert result.scenario == migrate.SCENARIO_STAMP_BASELINE_THEN_UPGRADE
    assert result.from_version is None
    assert result.to_version == migrate.head_revision()
    assert _site_count(db) == 1


def test_a_pre_s4_database_without_a_version_row_gets_the_new_tables(tmp_path):
    """这条钉住"场景 2 直接 stamp head"的假绿。

    库是 S2 那版代码建出来的（只有 `001_baseline` 的结构）、有数据、没记版本。旧写法一步
    stamp 到 head，于是 `alembic_version` 里写着 003，而 `workbench_layout` 根本不存在：
    健康检查报"schema 最新"，`/api/layouts` 却在运行时 500。现在先 stamp 基线再增量 upgrade，
    缺的表由防御式迁移补出来，数据照旧一行不动。
    """
    db = tmp_path / "s2_era.sqlite3"
    from alembic import command

    command.upgrade(migrate._alembic_config(migrate.url_for(db)), "001_baseline")
    _insert_site(db, "home")
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM alembic_version")  # "有数据无版本"的形状
        conn.commit()
    assert "workbench_layout" not in _tables(db)

    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert result.scenario == migrate.SCENARIO_STAMP_BASELINE_THEN_UPGRADE
    assert "workbench_layout" in _tables(db) and "layout_widget" in _tables(db)
    assert "local_settings_import" in _tables(db)
    assert _site_count(db) == 1
    assert result.missing == ()


def test_a_pre_s6_database_gets_the_observation_tables_and_their_indexes(tmp_path):
    """S6 的两张表由 `004_events` 补出来，**连索引一起**：只补表不补索引的话，
    结构自检照样绿（它只查表与列），而"按 `site_id` 过滤走全表扫"这条判据就悄悄失效了。
    """
    from alembic import command

    db = tmp_path / "s5_era.sqlite3"
    command.upgrade(migrate._alembic_config(migrate.url_for(db)), "003_import")
    assert "signal_event" not in _tables(db)

    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert {"signal_event", "sample_summary"} <= _tables(db)

    expected = {
        "signal_event": "ix_event_site_ts",
        "sample_summary": "ix_summary_site_node_slot_bucket",
    }
    with sqlite3.connect(db) as conn:
        for table, index in expected.items():
            names = {row[1] for row in conn.execute(f"PRAGMA index_list({table})")}
            assert index in names, (table, sorted(names))
        columns = {
            row[2] for row in conn.execute("PRAGMA index_info('ix_summary_site_node_slot_bucket')")
        }
    assert columns == {"site_id", "node_id", "slot", "ts_bucket"}


def test_a_stamped_head_that_is_missing_tables_reports_honestly(tmp_path):
    """版本号是一行文字，结构是 PRAGMA 事实：对不上就必须 `ok=False` 并说出缺什么。

    版本已经记在 head 时 `upgrade` 是空转——alembic 不重跑用过的脚本，所以掉了的表没有
    任何代码会补回来，旧写法却照样报"schema 最新"。这就是那条假绿的破口。
    """
    db = tmp_path / "lying.sqlite3"
    assert migrate.run_migrations(db).ok
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE node")
        conn.commit()

    result = migrate.run_migrations(db)
    assert result.ok is False
    assert set(result.missing) == {"node"}
    assert "node" in result.error
    # 场景与版本照实说：upgrade 确实跑了（它没有可补的脚本），问题出在结构而不是流程。
    assert result.scenario == migrate.SCENARIO_UPGRADE
    assert result.to_version == migrate.head_revision()


def test_a_missing_table_is_rebuilt_when_its_migration_is_pending(tmp_path):
    """自愈的条件是"那条脚本还没跑过"：版本落后时，防御式迁移会把缺的表补出来。"""
    db = tmp_path / "behind.sqlite3"
    assert migrate.run_migrations(db).ok
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TABLE local_settings_import")
        conn.execute("UPDATE alembic_version SET version_num='002_layout'")
        conn.commit()

    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert "local_settings_import" in _tables(db)
    assert result.to_version == migrate.head_revision()



def test_missing_structure_names_columns_too(tmp_path):
    db = tmp_path / "half.sqlite3"
    assert migrate.run_migrations(db).ok
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE app_config RENAME COLUMN settings_json TO nope_json")
        conn.commit()

    assert migrate.missing_structure(db) == ["app_config.settings_json"]



def test_versioned_database_takes_the_normal_upgrade_scenario(tmp_path):
    """场景 3：已有版本记录 → 正常 upgrade，数据与版本都保持。"""
    db = tmp_path / "current.sqlite3"
    assert migrate.run_migrations(db).ok
    _insert_site(db, "home")

    result = migrate.run_migrations(db)
    assert result.ok, result.error
    assert result.scenario == migrate.SCENARIO_UPGRADE
    assert result.from_version == migrate.head_revision()
    assert result.to_version == migrate.head_revision()
    assert _site_count(db) == 1


def test_re_running_migrations_is_idempotent(tmp_path):
    db = tmp_path / "again.sqlite3"
    assert migrate.run_migrations(db).ok
    _insert_site(db, "home")
    for _ in range(3):
        result = migrate.run_migrations(db)
        assert result.ok, result.error
    assert result.to_version == migrate.head_revision()
    assert _site_count(db) == 1


def test_create_all_path_and_alembic_upgrade_path_produce_the_same_schema(tmp_path):
    """两条建库路径必须落到同一个结构上。

    比的是 PRAGMA 事实（列/外键/唯一约束/索引），不是 DDL 文本：SQLite 不记外键名，
    而两条路径生成的子句顺序天然不同，按文本比只会产生假警报。
    """
    stamped = tmp_path / "via_create_all.sqlite3"
    assert migrate.run_migrations(stamped).ok  # 场景 1 就是 create_all + stamp

    upgraded = tmp_path / "via_alembic.sqlite3"
    from alembic import command

    command.upgrade(migrate._alembic_config(migrate.url_for(upgraded)), "head")

    assert _facts(upgraded) == _facts(stamped)


def _facts(path):
    """{表名: {列, 外键, 唯一约束, 索引}}，全部来自 PRAGMA。"""
    facts = {}
    with sqlite3.connect(path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name!='alembic_version' ORDER BY name"
            )
        ]
        for table in tables:
            # table_info: cid, name, type, notnull, dflt_value, pk
            columns = [
                (row[1], row[2], row[3], row[5])
                for row in conn.execute(f"PRAGMA table_info({table})")
            ]
            # foreign_key_list: id, seq, table, from, to, on_update, on_delete, match
            fks = sorted(
                (row[2], row[3], row[4], row[6])
                for row in conn.execute(f"PRAGMA foreign_key_list({table})")
            )
            listed = list(conn.execute(f"PRAGMA index_list({table})"))
            # index_list: seq, name, unique, origin, partial；origin u=表级 UNIQUE, c=列级 UNIQUE, pk=主键
            uniques = sorted(
                tuple(row[2] for row in conn.execute(f"PRAGMA index_info('{name}')"))
                for _seq, name, unique, origin, _partial in listed
                if unique and origin in ("u", "c", "pk")
            )
            # S6 的判据"按 site_id 过滤不得走全表扫"只认索引，所以索引形状也得进对拍：
            # 名字、唯一性、列序（列序变了，查询就用不上这个索引）。
            indexes = sorted(
                (
                    name,
                    bool(unique),
                    tuple(
                        row[2]
                        for row in conn.execute(f"PRAGMA index_info('{name}')")
                    ),
                )
                for _seq, name, unique, _origin, _partial in listed
            )
            facts[table] = {"columns": columns, "fks": fks, "uniques": uniques, "indexes": indexes}
    return facts


def test_backup_captures_rows_still_sitting_in_the_wal(tmp_path):
    """裸 cp 会丢未落盘的事务：备份必须走 sqlite3 backup API，这条测试就是它的回归线。"""
    db = tmp_path / "rftrack.sqlite3"
    assert migrate.run_migrations(db).ok
    with sqlite3.connect(db) as conn:  # 先进 WAL 模式，与运行时一致
        conn.execute("PRAGMA journal_mode=WAL")
    _insert_site(db, "home")

    saved = migrate.backup_db(db, tmp_path / "backups")
    assert saved is not None and saved.exists()
    with sqlite3.connect(saved) as conn:
        assert conn.execute("SELECT count(*) FROM site").fetchone()[0] == 1


def test_backup_of_a_missing_database_returns_none_instead_of_raising(tmp_path):
    assert migrate.backup_db(tmp_path / "nope.sqlite3") is None


def test_run_migrations_reports_failure_honestly(tmp_path):
    """迁移跑不动时必须 ok=False 带上原因，绝不回一个"看起来成功"的结果。"""
    broken = tmp_path / "broken"
    broken.mkdir()  # 目录冒充库文件：sqlite 打不开它
    result = migrate.run_migrations(broken)
    assert result.ok is False
    assert result.error
    assert result.scenario == "failed"


@pytest.mark.parametrize("flag", ["--backup", "--migrate"])
def test_cli_reports_what_it_did(tmp_path, capsys, flag):
    db = tmp_path / "cli.sqlite3"
    code = migrate.main(["--db", str(db), flag])
    out = capsys.readouterr().out
    assert code == 0
    assert out.strip()
