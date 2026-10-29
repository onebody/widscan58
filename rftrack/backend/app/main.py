"""FastAPI 应用工厂：一个进程一套库，测试各自建实例，不起全局单例。

后台拨号循环跟着应用一起起合（lifespan），所以"进程活着节点表就在更新"这句话
不需要任何人再手动点一次。保留清理（S6）同理跟着起合：`retention_*` 那两个键在 S4
只是"算得出生效值"，从这里开始才真的有人按它删行。采集层（S6）也在这条链上，但它
不起自己的循环线程去碰串口——它挂在 `/ws/bridge` 的观察者位上，节拍由自己的任务跑。
回放（S6）排在采集之后起、之前停：它只往采集的口里喂字节，采集要是没在跑，喂进去的
只会是一条没人搬的队列。
测试实例默认把后台都关掉（`dialer_enabled=false` + `retention_purge_enabled=false`
+ `capture_enabled=false`，见 tests/conftest.py），免得后台动作混进断言；回放没配目录
时压根不起。

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
from .routers import bridge, events, gateways, health, layouts, nodes, relays, samples, sites, ws
# `settings` 这个名字在本函数里是 `Settings` 实例，模块只能带着别名进来。
from .routers import settings as settings_routes
from .routers import live as live_routes
from .services.capture import CapturePipeline
from .services.dialer import DialCoordinator, HttpTransport
from .services.live import LiveHub
from .services.replay import ReplayRunner
from .services.retention import RetentionRunner
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
    #: `/ws/live` 的扇出口：先建它，采集层才有地方"发一条"。它不认识场地以外的任何维度。
    live = LiveHub(settings)
    #: 采集层挂在透传的观察者位上：它自己不开上游，只在"有页正连着这块板"时看到字节。
    #: 顺序是先建采集、再把它的钩子交给桥——桥那边只认 `tap`/`untap` 两个方法名。
    capture = CapturePipeline(db, settings, transport=app_transport, publisher=live)
    hub = BridgeHub(upstream_factory or WebsocketFactory(), observer=capture)
    #: 保留清理：`retention_*` 那两个键从 S4 起就有生效值，这个 runner 是它们第一次真的删数据。
    retention = RetentionRunner(db, settings)
    #: 回放：唯一一条"没有板子也能把采集整条链跑通"的数据源，来源一律 `mock`（见 replay.py）。
    replay = ReplayRunner(db, settings, capture)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await dialer.start()
        await retention.start()
        await capture.start()
        await replay.start()
        yield
        # 收摊顺序与起来相反：回放先停，它一停采集就不再收新字节；采集先于保留与拨号停，
        # 因为它最后那次刷要写的库与事件都还在；拨号最后停，因为它一停就没有新的节点行可读。
        await replay.stop()
        await capture.stop()
        await retention.stop()
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
    app.state.capture = capture
    app.state.retention = retention
    app.state.replay = replay
    app.state.live = live
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
    app.include_router(samples.router)
    app.include_router(events.router)
    app.include_router(bridge.router)
    app.include_router(ws.router)
    app.include_router(live_routes.router)
    return app
