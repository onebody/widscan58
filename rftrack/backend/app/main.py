"""FastAPI 应用工厂：一个进程一套库，测试各自建实例，不起全局单例。

后台拨号循环跟着应用一起起合（lifespan），所以"进程活着节点表就在更新"这句话
不需要任何人再手动点一次。测试实例默认 `dialer_enabled=false`（见 tests/conftest.py），
免得后台 HTTP 混进断言。

工作台页面挂在**同一个 origin** 的 `/workbench/` 下：同源就不必开 CORS，也不必让页面
去猜后端在哪台机器的哪个端口（方案 §6「运行时探测子路径前缀」说的正是这件事）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

import migrate

from . import APP_VERSION
from .config import Settings
from .database import Database
from .routers import bridge, gateways, health, layouts, nodes, relays, sites, ws
# `settings` 这个名字在本函数里是 `Settings` 实例，模块只能带着别名进来。
from .routers import settings as settings_routes
from .services.dialer import DialCoordinator, HttpTransport
from .services.ws_bridge import BridgeHub, WebsocketFactory


def mount_workbench(app: FastAPI, settings: Settings) -> dict:
    """挂静态工作台，并把"挂没挂、为什么没挂"留在 `app.state` 上给健康检查说。

    没挂**不算故障**：`RFTRACK_WORKBENCH_DIR=` 是"这台实例只提供 API，页面由 serve.mjs
    那边发"的合法部署形态。所以这一节只如实呈现，不进 `problems`——把可选件缺失报成
    degraded，运营者就会开始忽略 degraded，那才是真代价。
    """
    directory = settings.workbench_dir
    if directory is None:
        return {"mounted": False, "reason": "disabled", "dir": None, "index": False}
    exists = directory.is_dir()
    index = (directory / "index.html").is_file()
    if not index:
        return {"mounted": False, "reason": "no_index_html", "dir": str(directory), "index": False}
    app.mount(
        "/workbench", StaticFiles(directory=str(Path(directory)), html=True), name="workbench"
    )
    return {"mounted": True, "reason": None, "dir": str(directory), "index": True}


def create_app(
    settings: Settings | None = None,
    transport: HttpTransport | None = None,
    upstream_factory=None,
) -> FastAPI:
    """`upstream_factory` 是 `/ws/bridge` 连 serve.mjs 的那只手：测试交假的进来，
    真部署用 `websockets`。与 `transport` 同一个理由——不想起临时桥也能测透传。"""
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    owns_transport = transport is None
    app_transport = transport or HttpTransport()
    dialer = DialCoordinator(db, settings, app_transport)
    hub = BridgeHub(upstream_factory or WebsocketFactory())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await dialer.start()
        yield
        await dialer.stop()
        if owns_transport:
            app_transport.close()
        db.dispose()

    app = FastAPI(title="RF Track", version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.transport = app_transport
    app.state.dialer = dialer
    app.state.bridge = hub
    app.state.head_revision = migrate.head_revision()
    #: 结构自检的结果留空，由第一次"版本号说没问题"的健康检查去填（见 routers/health.py）。
    app.state.schema_gaps = None
    #: 页面挂没挂、为什么没挂——`/api/health` 的 `workbench` 一节就是这个字典。
    app.state.workbench = mount_workbench(app, settings)

    app.include_router(health.router)
    app.include_router(sites.router)
    app.include_router(relays.router)
    app.include_router(gateways.router)
    app.include_router(nodes.router)
    app.include_router(settings_routes.router)
    app.include_router(layouts.router)
    app.include_router(bridge.router)
    app.include_router(ws.router)
    return app
