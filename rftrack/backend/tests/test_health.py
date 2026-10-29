"""`GET /api/health`：版本可比对（R20）、instance_role 缺省为 site、各接入机拨号态如实呈现。

S4 起再加一条：`schema_version` 与 `expected_schema_version` 相等**不等于**库里结构齐全，
`schema_gaps` 那一节就是专门说这话的。
"""

import sqlite3

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
import migrate


def test_health_reports_the_schema_version_stored_in_the_database(client, db_path):
    body = client.get("/api/health").json()
    with sqlite3.connect(db_path) as conn:
        stored = conn.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    assert body["schema_version"] == stored


def test_health_reports_schema_version_from_db_not_from_code(client, db_path):
    """库落后于代码时，健康检查必须报出库里的真实版本，而不是代码期望的头版本。"""
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE alembic_version SET version_num = '000_stale'")
    body = client.get("/api/health").json()
    assert body["schema_version"] == "000_stale"
    assert body["schema_version"] != migrate.head_revision()
    assert body["status"] == "degraded"
    assert any("schema 版本不一致" in p for p in body["problems"])


def test_health_has_a_non_empty_app_version(client):
    assert client.get("/api/health").json()["app_version"]


def test_instance_role_defaults_to_site_when_config_row_is_missing(client):
    """app_config 一行都没有也必须回 site，而不是 500（S2 出口判据）。"""
    assert client.get("/api/health").json()["instance_role"] == "site"


def test_instance_role_defaults_to_site_when_column_is_null(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO app_config (id, version, instance_role) VALUES (1, 1, NULL)")
    assert client.get("/api/health").json()["instance_role"] == "site"


def test_instance_role_reads_the_database_when_a_config_row_exists(client, db_path):
    with sqlite3.connect(db_path) as conn:
        conn.execute("INSERT INTO app_config (id, version, instance_role) VALUES (1, 1, 'aggregator')")
    assert client.get("/api/health").json()["instance_role"] == "aggregator"


def test_health_reports_no_schema_gaps_on_a_healthy_database(client):
    assert client.get("/api/health").json()["schema_gaps"] == []


def test_health_names_the_missing_table_that_the_version_row_does_not_cover(client, db_path):
    """版本号说 head 而库里少一张表：健康检查要说得出缺哪张，不能只报"schema 最新"。

    掉表必须在**第一次**健康检查之前发生——结论按进程缓存（`app.state.schema_gaps`），
    schema 在运行期不会自己变，所以建完缓存再补表也不该被看见（要变得重启）。
    """
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE node")
    body = client.get("/api/health").json()
    assert body["schema_version"] == body["expected_schema_version"]
    assert body["schema_gaps"] == ["node"]
    assert body["status"] == "degraded"
    assert any("结构不齐" in p and "node" in p for p in body["problems"])


def test_health_counts_gateways_by_dial_state(client, db_path):
    site = client.post("/api/sites", json={"name": "主场", "slug": "main"}).json()
    relay = client.post(
        "/api/relays", json={"site_id": site["site_id"], "name": "起点塔"}
    ).json()
    created = [
        client.post(
            "/api/gateways",
            json={"relay_id": relay["relay_id"], "name": name, "kind": "usb-bridge"},
        ).json()
        for name in ("pit", "stand", "gate", "tower")
    ]
    states = ("online", "unreachable", "dialing")
    with sqlite3.connect(db_path) as conn:
        for state, gateway in zip(states, created):
            conn.execute(
                "UPDATE gateway SET dial_state = ? WHERE gateway_id = ?",
                (state, gateway["gateway_id"]),
            )
    body = client.get("/api/health").json()
    assert body["gateways"] == {
        "total": 4,
        "online": 1,
        "unreachable": 1,
        "dialing": 1,
        "failing": 0,
        "never_probed": 1,
    }


def test_health_does_not_crash_on_an_unmigrated_database(tmp_path):
    empty = tmp_path / "nothing.sqlite3"
    app = create_app(Settings(db_path=empty))
    with TestClient(app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["schema_version"] is None
    assert body["problems"]
    # 版本号都不对时不做结构扫描：缺表结论已经被"先运行 migrate.py"盖过，不该再刷一遍 PRAGMA
    assert body["schema_gaps"] == []
    # 统计不出来就报 None，不许把"读不到"渲染成"一个都没有"
    assert body["gateways"] is None
    assert body["instance_role"] == "site"


# --- 保留清理这一节（S6）------------------------------------------------------


def test_health_says_retention_is_not_running_when_it_never_ran(client):
    """夹具把清理关掉了：这一节必须说"没启用、一轮都没跑"，而不是给一个看着像跑过的壳。"""
    body = client.get("/api/health").json()
    assert body["retention"] == {
        "enabled": False,
        "interval_s": 3600.0,
        "runs": 0,
        "errors": 0,
        "last_run_at": None,
        "last_error": None,
        "last_sites": [],
        "problem": None,
    }
    # 没启用不是故障：`RFTRACK_RETENTION_PURGE_ENABLED=0` 是合法部署形态，
    # 把它报成 degraded 就等于教运营者忽略 degraded。
    assert body["status"] == "ok"


def test_a_failed_purge_round_makes_health_degraded(client):
    """上一轮清理真的失败了，就必须出现在 `problems` 里——"保留在跑"这句话得有代价。

    直接改 runner 的内存态而不是真把库锁住：这一条测的是**健康检查读不读这个结论**，
    失败本身由 `tests/test_retention.py` 的循环用例覆盖。
    """
    runner = client.app.state.retention
    runner.errors = 2
    runner.last_error = "OperationalError: database is locked"
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert any("保留清理失败" in p and "locked" in p for p in body["problems"])
    assert body["retention"]["runs"] == 0, "失败的两轮不得记成跑过两轮"
