"""FastAPI 应用工厂：一个进程一套库，测试各自建实例，不起全局单例。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

import migrate

from . import APP_VERSION
from .config import Settings
from .database import Database
from .routers import gateways, health, nodes, relays, sites
from .services.dialer import HttpTransport


def create_app(
    settings: Settings | None = None, transport: HttpTransport | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    db = Database(settings.db_path)
    owns_transport = transport is None
    app_transport = transport or HttpTransport()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_transport:
            app_transport.close()
        db.dispose()

    app = FastAPI(title="RF Track", version=APP_VERSION, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.transport = app_transport
    app.state.head_revision = migrate.head_revision()

    app.include_router(health.router)
    app.include_router(sites.router)
    app.include_router(relays.router)
    app.include_router(gateways.router)
    app.include_router(nodes.router)
    return app
