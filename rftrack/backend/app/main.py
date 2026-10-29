"""FastAPI 应用工厂：一个进程一套库，测试各自建实例，不起全局单例。

后台拨号循环跟着应用一起起合（lifespan），所以"进程活着节点表就在更新"这句话
不需要任何人再手动点一次。测试实例默认 `dialer_enabled=false`（见 tests/conftest.py），
免得后台 HTTP 混进断言。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

import migrate

from . import APP_VERSION
from .config import Settings
from .database import Database
from .routers import bridge, gateways, health, nodes, relays, sites, ws
from .services.dialer import DialCoordinator, HttpTransport
from .services.ws_bridge import BridgeHub, WebsocketFactory


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

    app.include_router(health.router)
    app.include_router(sites.router)
    app.include_router(relays.router)
    app.include_router(gateways.router)
    app.include_router(nodes.router)
    app.include_router(bridge.router)
    app.include_router(ws.router)
    return app
