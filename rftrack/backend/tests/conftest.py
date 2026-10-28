"""S2 后端测试的公共夹具：每个测试一套独立库文件，走真实迁移路径建表。"""

import os
import sys

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
import migrate  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "rftrack.sqlite3"
    result = migrate.run_migrations(path)
    assert result.ok, f"迁移失败：{result.error}"
    return path


@pytest.fixture
def app(db_path):
    return create_app(Settings(db_path=db_path))


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client
