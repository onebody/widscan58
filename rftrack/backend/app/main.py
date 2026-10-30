"""FastAPI 应用工厂：一个进程一套库，测试各自建实例，不起全局单例。

后台拨号循环跟着应用一起起合（lifespan），所以"进程活着节点表就在更新"这句话
不需要任何人再手动点一次。保留清理（S6）同理跟着起合：`retention_*` 那两个键在 S4
只是"算得出生效值"，从这里开始才真的有人按它删行。采集层（S6）也在这条链上，但它
不起自己的循环线程去碰串口——它挂在 `/ws/bridge` 的观察者位上，节拍由自己的任务跑。
回放（S6）排在采集之后起、之前停：它只往采集的口里喂字节，采集要是没在跑，喂进去的
只会是一条没人搬的队列。内置插件注册表（S7）在这条链最前面同步一次；库里连那四张表都
没有时整段跳过——未迁移的库仍然要能起来回答健康检查。插件运行时（S7）起在最后、停在最前：
它消费的是采集层交出来的批，而它自己那一拍读的是注册表，缺表时不起而不是炸掉 lifespan。
测试实例默认把后台都关掉（`dialer_enabled=false` + `retention_purge_enabled=false`
+ `capture_enabled=false` + `plugins_enabled=false`，见 tests/conftest.py），免得后台动作
混进断言；回放没配目录时压根不起。

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
from .routers import (
    bridge,
    events,
    gateways,
    health,
    layouts,
    nodes,
    plugins,
    relays,
    samples,
    sites,
    ws,
)
# `settings` 这个名字在本函数里是 `Settings` 实例，模块只能带着别名进来。
from .routers import settings as settings_routes
from .routers import live as live_routes
# 路由模块与服务模块同名（`alerts`），各自带别名进来。
from .routers import alerts as alert_routes
from .routers import targets as target_routes
from .services import registry
from .services.alerts import AlertEngine
from .services.capture import CapturePipeline
from .services.dialer import DialCoordinator, HttpTransport
from .services.live import LiveHub
from .services.plugin_runtime import PluginRuntime
from .services.replay import ReplayRunner
from .services.retention import RetentionRunner
from .services.targets import TargetEngine
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
    #: 插件运行时先于采集层建：采集侧每帧要问它"现在值不值得建批"。
    #: 建在这里、起在 lifespan 最后（`plugins_enabled` 关掉或库里缺表都不起）：不起循环时它一次
    #: `refresh()` 都没做过，`armed` 是 False，投喂进来的每一批都被原样拒回——与"全部停用"是同一个
    #: 形态，S7 的回滚判据因此不依赖开关本身。
    #: 挂到 `app.state.plugins` 而不是让端点各自 new 一个：`/state` 里的队列必须与采集侧投喂的是
    #: 同一个对象，否则页面看到的永远是空队列。
    plugin_runtime = PluginRuntime(db, settings)
    #: 采集层挂在透传的观察者位上：它自己不开上游，只在"有页正连着这块板"时看到字节。
    #: 顺序是先建采集、再把它的钩子交给桥——桥那边只认 `tap`/`untap` 两个方法名。
    capture = CapturePipeline(
        db, settings, transport=app_transport, publisher=live, plugins=plugin_runtime
    )
    hub = BridgeHub(upstream_factory or WebsocketFactory(), observer=capture)
    #: 保留清理：`retention_*` 那两个键从 S4 起就有生效值，这个 runner 是它们第一次真的删数据。
    retention = RetentionRunner(db, settings)
    #: 告警引擎：读 `plugin_finding` 按规则落 `alert`。它不自己起上游，判定有没有新货由
    #: 游标说了算；建在这里、起在 lifespan 最后（缺 `alert_rule` 表就不起，判据与
    #: `registry.sync` 同一条——未迁移的库必须还能起来回答 `/api/health`）。
    alert_engine = AlertEngine(db, settings)
    #: 目标聚合引擎：读 `plugin_finding` 按观测身份聚成 `candidate_target` 时间线。与告警
    #: 引擎同族同判据：建在这里、起在 lifespan 最后（缺 `candidate_target` 表就不起）。
    target_engine = TargetEngine(db, settings)
    #: 回放：唯一一条"没有板子也能把采集整条链跑通"的数据源，来源一律 `mock`（见 replay.py）。
    replay = ReplayRunner(db, settings, capture)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 内置插件先登记再起后台：supervisor 读的就是这几行，顺序反了它第一眼看到的就是
        # "库里少一支插件"。缺表就整段跳过——未迁移的库必须还能起来回答 `/api/health`
        # （"先去跑 migrate.py"那句话得由健康检查说，不是由启动崩溃说），而缺哪张表
        # `schema_gaps`/`schema_version` 已经报得出来，这里不再另立一套口径。
        if db.has_table("plugin_registry"):
            with db.scope() as session:
                registry.sync(session)
        await dialer.start()
        await retention.start()
        await capture.start()
        await replay.start()
        # 插件循环起在最后：它吃的是采集层那侧的产物，而注册表要先同步、采集层要先存在，
        # 它才有批可消费。缺表就整段跳过（判据与上面 `registry.sync` 同一个）——运行时
        # `start()` 在起循环之前就要 `refresh()` 读一次注册表，那一读撞 `no such table`
        # 炸掉的是整个 lifespan，而"先去跑 migrate.py"这句话该由健康检查说。
        if db.has_table("plugin_registry"):
            await plugin_runtime.start()
        # 告警引擎起在最后：注册表先同步、采集与插件先在跑，它才有新判定可读。
        if db.has_table("alert_rule"):
            await alert_engine.start()
        # 目标聚合引擎最后起：它与告警引擎读同一张 plugin_finding，谁先谁后互不相干，
        # 但排它后面让"告警先看见判定"成为稳定顺序（页面先响铃、后长目标）。
        if db.has_table("candidate_target"):
            await target_engine.start()
        yield
        # 收摊顺序与起来相反：目标引擎先停（它一停就不再消费判定，收摊途中不再有人写
        # 目标两表）；告警引擎随后停；插件运行时先于采集与保留停——它一停就不再有人
        # 和保留清理抢着写库；回放先停，它一停采集就不再收新字节；采集先于保留与拨号停，
        # 因为它最后那次刷要写的库与事件都还在；拨号最后停，因为它一停就没有新的节点行可读。
        await target_engine.stop()
        await alert_engine.stop()
        await plugin_runtime.stop()
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
    app.state.plugins = plugin_runtime
    app.state.retention = retention
    app.state.alerts = alert_engine
    app.state.targets = target_engine
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
    app.include_router(plugins.router)
    app.include_router(alert_routes.rules_router)
    app.include_router(alert_routes.router)
    app.include_router(target_routes.router)
    return app
