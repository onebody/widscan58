"""`GET /api/health`：版本可比对（R20）、instance_role 缺省为 site、各接入机拨号态如实呈现。"""

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
    # 统计不出来就报 None，不许把"读不到"渲染成"一个都没有"
    assert body["gateways"] is None
    assert body["instance_role"] == "site"
