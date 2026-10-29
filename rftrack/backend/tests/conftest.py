"""S2/S3 后端测试的公共夹具：每个测试一套独立库文件，走真实迁移路径建表。"""

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
    """后台的四个循环在 HTTP 测试里默认关掉。

    生产默认都开着（S3 交付"没人点也自己更新节点表"，S6 交付"过期行自己会被删"和"有人看板子
    就在采集"，S7 交付"启用一支插件判定自己在长"），但测试里让它们跑起来就会：真发 HTTP、和
    `client.app.state.transport` 的假应答抢同一份结果、还会在断言之前改写 `dial_state`，或者把
    刚摆好的历史数据删掉，或者替被测算的透传补上一条它本来没有的数据通路，或者在两次断言之间
    把注册表那一行搬进快照、顺手把测试自己喂进去的那一批发掉。
    四个循环分别由 `tests/test_dial_loop.py`、`tests/test_retention.py`、`tests/test_capture.py`
    与 `tests/test_plugins.py` 直接构造来测。
    """
    return create_app(
        Settings(
            db_path=db_path,
            dialer_enabled=False,
            retention_purge_enabled=False,
            capture_enabled=False,
            plugins_enabled=False,
        )
    )


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client
