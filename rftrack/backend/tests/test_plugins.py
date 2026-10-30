"""S7 插件运行时：注册表、每 (插件, 网关) 有界队列、supervisor 退避重启、判定落库与启停端点。

这个文件逐条把任务书 §10.4 的五句话变成断言（可查看/启停/配置、状态·日志·错误可观察、队列有界、
崩溃不拖垮采集、切布局不停任务），外加方案 §7 S7 行补的两条：**切场地也不停任务**、
**A 场地队列打满不得吃掉 B 场地配额**。

写在前面的一条口径：`signal_event` 的 docstring 明写"写它的必须是采集层本身，推理结果属
S7/S8 的表"，所以插件的判定落 `plugin_finding`，**不混写观测事实表**——页面上"这块板安静了"
（观测）与"这条像是干扰"（推导）必须是两回事，S8 的复核才分得开。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.main import create_app
from app.models import (
    PluginFinding,
    PluginLog,
    PluginRegistry,
    PluginState,
    to_utc,
)
from app.plugins import IMPLEMENTATIONS
from app.plugins.base import Finding, FrameBatch, SlotPoint
from app.services import registry
from app.services.history import MAX_ROWS, iso
from app.services.plugin_runtime import (
    CONFIG_TTL_S,
    DRAIN_CHUNK,
    EXECUTABLE_SCOPES,
    RESTART_LIMIT,
    TICK_INTERVAL_S,
)
from test_samples import add_site

BUILTINS = ["alert-engine", "anomaly-detector", "interference-correlator"]


def rows_of(client, model):
    with client.app.state.db.scope() as session:
        return list(session.execute(select(model)).scalars().all())


def moment(offset_ms: int = 0) -> datetime:
    return datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc) + timedelta(
        milliseconds=offset_ms
    )


def one_batch(**fields) -> FrameBatch:
    """一帧"(slot 0 上 −40 dBFS，越过了 `threshold_db` 的 −85)"。

    `alert-engine` 读绝对越限，所以单帧就该说话（不像 `anomaly-detector` 要先攒基线）——
    运行时这一层的测试要的是"有一条判定可查"，不是"这条规则挑得好"，后者在
    `test_plugin_rules.py`。
    """
    defaults = dict(
        site_id=1, gateway_id=1, node_id=1, source="real", ts=moment(0), seq=1
    )
    defaults.update(fields)
    return FrameBatch(points=(SlotPoint(slot=0, rssi=-40.0, ema=-40.0),), **defaults)


def enable_plugin(client, plugin_id: str, **fields) -> None:
    """直接把注册表那一行改成启用：S7-6 的启停端点还没建，先按库里的真相摆位。"""
    with client.app.state.db.scope() as session:
        row = session.get(PluginRegistry, plugin_id)
        row.enabled = True
        for name, value in fields.items():
            setattr(row, name, value)


def runtime_for(client, *, implementations=None, **overrides):
    """构造**可以手工起循环**的运行时。`overrides` 改的是 `Settings`（队列上限、角色这类部署事实）。

    这里显式把 `plugins_enabled` 拨回 `True`：夹具关的是**应用自己**那四条后台循环（免得它们在
    断言之间改库），而这个 helper 造的是测试自己 `await start()` 的运行时——跟着夹具一起关着，
    那几条生命周期用例就在测一条永不执行的早退。要测开关本身请显式传 `plugins_enabled=False`。

    `implementations` 是注入位：supervisor 那几条测试需要一支**会崩的**插件，而往代码里放一支
    崩的内置插件是不可想象的（它会被真用户启用来）——注入的类不是"被 mock 掉的依赖"，它就是
    被测对象之一（`plugins/__init__.py` 里那张表本来就是运行期唯一的实现映射）。
    """
    from app.services.plugin_runtime import PluginRuntime

    changes: dict = {"plugins_enabled": True, **overrides}
    return PluginRuntime(
        client.app.state.db,
        replace(client.app.state.settings, **changes),
        implementations=implementations,
    )


def add_node(client, gateway_id: int, name: str) -> int:
    """在同一台接入机下再摆一块板：相关型规则只有两块以上才说话。"""
    from app.models import Node

    with client.app.state.db.scope() as session:
        node = Node(
            gateway_id=gateway_id,
            display_name=name,
            transport_kind="usb-native",
            transport_addr=f"/dev/{name}",
            status="active",
        )
        session.add(node)
        session.flush()
        return node.node_id


def add_gateway(client, site_id: int, slug: str, addr: str) -> dict:
    """在指定场地里再摆一台接入机（连带一块板）：作用域锁到网关时要有第二台可比。"""
    relay = client.post("/api/relays", json={"site_id": site_id, "name": f"{slug}塔"}).json()
    gateway = client.post(
        "/api/gateways",
        json={
            "relay_id": relay["relay_id"],
            "name": f"pi-{slug}",
            "kind": "usb-bridge",
            "base_url": f"http://192.168.99.{addr}:8080",
        },
    ).json()
    return {
        "relay": relay["relay_id"],
        "gateway": gateway["gateway_id"],
        "node": add_node(client, gateway["gateway_id"], f"{slug}板"),
    }


def state_of(client, plugin_id: str) -> PluginState:
    return {r.plugin_id: r for r in rows_of(client, PluginState)}[plugin_id]


class AlwaysFails:
    """一支每次 `feed()` 都抛的插件：supervisor 的输入就是一支坏的规则，不需要更多剧情。"""

    CONFIG_KEYS = ("threshold_db",)

    def __init__(self) -> None:
        self.calls = 0

    def feed(self, batch, config):
        self.calls += 1
        raise RuntimeError("规则里炸了")

    def stats(self) -> dict[str, int]:
        return {"calls": self.calls}


class CountsFeeds:
    """每吃一帧说一个递增的数：用来区分"还是原来那个实例"与"换了个新的"。

    注入位要能说出实例的身份才有用。数判定条数不行——换实例后第一帧照样落一条；只有"它自己
    记得吃过几帧"这种**跨帧的记忆**才能把"基线被清零"这件事变成一条会红的断言。
    """

    CONFIG_KEYS = ()

    def __init__(self) -> None:
        self.seen = 0

    def feed(self, batch, config):
        self.seen += 1
        return [Finding(kind="counted", value=float(self.seen))]

    def stats(self) -> dict[str, int]:
        return {"seen": self.seen}


def test_upgrade_creates_the_four_plugin_tables(db_path):
    """`alembic upgrade head` 之后四张插件表都在。

    建表本身由 `tests/test_migrations.py` 的结构对拍钉住（create_all 路径 vs 迁移路径逐 PRAGMA
    事实相等）；这条只管"这一阶段的表确实存在"，缺表时 `migrate.missing_structure()` 会把它
    报进 `/api/health` 的 `schema_gaps`，所以这里用同一个判据的来源——库里的 sqlite_master。
    """
    with sqlite3.connect(db_path) as conn:
        have = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"plugin_registry", "plugin_state", "plugin_log", "plugin_finding"} <= have, (
        f"缺插件表，实际表：{sorted(have)}"
    )


def test_app_startup_syncs_the_builtin_registry_with_everything_disabled(client):
    """启动即把代码里那三支内置插件登记好，且**默认全是停用**。

    "全部 `enabled=0` 即等效不存在"是 S7 的回滚判据，它成立的前提就是：新装的实例里没有任何
    插件在跑。这条测试钉的正是"默认值"，而不是"能关掉"。
    """
    assert [row.plugin_id for row in sorted(rows_of(client, PluginRegistry), key=lambda r: r.plugin_id)] == BUILTINS
    assert all(row.enabled is False for row in rows_of(client, PluginRegistry))
    states = {row.plugin_id: row.status for row in rows_of(client, PluginState)}
    assert states == dict.fromkeys(BUILTINS, "stopped")


def test_registry_sync_is_idempotent_and_never_rewires_enabled(client):
    """同步第二遍不能长出重复行，也不能把用户已经启用的插件悄悄关掉。

    后半句才是重点：启动时"库里没有对应实现的行原样留着"这条纪律，如果顺手把所有行的
    `enabled` 复位，就等于每次重启都免费吃掉一次用户配置——和 S4 那套"配置层优先级"直接冲突。
    """
    with client.app.state.db.scope() as session:
        before = session.get(PluginRegistry, "anomaly-detector")
        before.enabled = True
        before.queue_limit = 64
        session.flush()

    with client.app.state.db.scope() as session:
        registry.sync(session)
        registry.sync(session)

    with client.app.state.db.scope() as session:
        rows = list(session.execute(select(PluginRegistry)).scalars().all())
        kept = session.get(PluginRegistry, "anomaly-detector")
    assert len(rows) == 3, f"重复同步长出了 {len(rows)} 行"
    assert kept.enabled is True and kept.queue_limit == 64


def test_registry_row_without_a_builtin_implementation_is_logged(client):
    """库里多出一行代码里没有的实现时，要留一条流水可查，而不是静默忽略。

    不做动态代码加载（方案 §11）意味着这一行**永远不会跑起来**；如果它连一条 log 都不留，
    用户看到的就只有"我加的插件怎么不见了"，而那与"插件市场"的期待只差一个字，必须能说清。
    """
    with client.app.state.db.scope() as session:
        session.add(
            PluginRegistry(
                plugin_id="not-a-real-plugin",
                name="来历不明",
                version="0.0.1",
                enabled=True,
            )
        )
    with client.app.state.db.scope() as session:
        registry.sync(session)

    logs = rows_of(client, PluginLog)
    assert [row.plugin_id for row in logs] == ["not-a-real-plugin"], (
        f"没有为无实现的行记 log，实际：{[(row.plugin_id, row.event) for row in logs]}"
    )
    assert logs[0].level == "warning"


def test_plugin_queue_limit_defaults_and_reads_env():
    """`plugin_queue_limit` 是"这一支插件没写自己的上限时跟随的那个值"，必须可 env 覆盖。

    Pi 上可用内存约 460 MB（方案 §5 实测），所以队列容量属于部署事实而不是代码常数——
    同一份代码在工作站上可以宽、在 Pi 上要紧。消费它的是 S7 的运行时（`queue_limit is NULL`
    的那一支插件走它），这一条只钉"配得进来、默认是几"。
    """
    assert Settings().plugin_queue_limit == 64
    assert Settings.from_env({"RFTRACK_PLUGIN_QUEUE_LIMIT": "24"}).plugin_queue_limit == 24


def test_plugin_queue_limit_below_one_is_rejected_at_construction():
    """上限配成 0 或负数要在构造时就拦住：那等于让插件一条都看不见，而现象只是"插件没产出"。

    与 `live_queue_limit`、`capture_event_limit` 同一条纪律——"配了之后循环会立刻坏掉"的数
    不留到运行期发现（`config.py` 的 `__post_init__` 注释里写着理由）。
    """
    for bad in (0, -1):
        with pytest.raises(ValueError, match="plugin_queue_limit"):
            Settings(plugin_queue_limit=bad)


def test_an_enabled_plugin_writes_a_finding_whose_identity_comes_from_the_batch(client):
    """启用的插件：喂一帧进去，库里就该有一条判定，而**身份那一截是运行时盖上去的**。

    `Finding` 这个形状里没有 `site_id`/`node_id`/`source`（`plugins/base.py` 的第一条纪律），
    所以判定行上的这五样只能来自 `FrameBatch`。这条测试就是那条纪律的执行处：插件说不了
    "这是 A 场地的实时读数"，因为只有采集层交进来的东西才有来路。
    `producer` 必须是 `rule:<plugin_id>@<version>`——页面上"这是谁说的"只有一个问法。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)

    runtime.refresh()
    assert runtime.offer(one_batch(site_id=where["site"], gateway_id=where["gateway"],
                                   node_id=where["node"])) == "queued"
    runtime.run_once()

    rows = rows_of(client, PluginFinding)
    assert len(rows) == 1, f"判定条数不对：{[(r.kind, r.value) for r in rows]}"
    row = rows[0]
    assert (row.plugin_id, row.producer, row.kind) == (
        "alert-engine",
        "rule:alert-engine@0.1.0",
        "threshold_crossing",
    )
    assert (row.site_id, row.gateway_id, row.node_id, row.slot) == (
        where["site"],
        where["gateway"],
        where["node"],
        0,
    )
    assert (row.source, row.metric, row.value, row.threshold) == ("real", "rssi", -40.0, -85.0)
    # 读回来是 naive：SQLite 存 UTC 但丢掉偏移量，`models.to_utc` 是本项目既有的口径。
    assert to_utc(row.ts) == moment(0), "判定时刻没落在观测那一侧（写库时刻会让回放的时间轴错位）"

    state = {r.plugin_id: r for r in rows_of(client, PluginState)}["alert-engine"]
    assert (state.status, state.processed, state.produced) == ("running", 1, 1)


def test_a_disabled_plugin_is_never_fed_and_holds_nothing(client):
    """全部停用 = 等效不存在：投递口一个字节都不攒，库里既没判定也没状态变化。

    这是 S7 整段的回滚判据（方案 §7 S7 行："全部 `enabled=0` 即等效不存在"），所以它测的不是
    "不产出判定"这一件结果，而是**整条通路都不参与**——不建队列、不占内存、不动 `plugin_state`。
    否则"关掉了但还在攒字节"会在 Pi 上表现为内存一直在涨，而那与"插件有 bug"长得一模一样。
    """
    add_site(client, "alpha", "A场", 5)
    runtime = runtime_for(client)

    runtime.refresh()
    assert runtime.offer(one_batch()) == "disabled"
    assert runtime.run_once()["written"] == 0

    assert rows_of(client, PluginFinding) == []
    assert rows_of(client, PluginLog) == []
    assert {r.status for r in rows_of(client, PluginState)} == {"stopped"}


def test_the_queue_capacity_follows_the_global_key_until_the_plugin_sets_its_own(client):
    """注册表那一行留空就跟随全局键，写数字就只听它自己的——两条路都得真的建出那个容量。

    `queue_limit` 是用户能改的五样之一，改的是"这一支插件每台接入机最多攒多少批"。留 NULL 的
    意思是"跟着部署走"（Pi 上要紧、工作站可以宽），所以这条同时钉住两件事：**NULL 不是 0**
    （否则等于关掉），以及**数字不是摆设**（写进去必须换掉真实容量）。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, plugin_queue_limit=8)

    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"]))
    [entry] = runtime.status()["queues"]
    assert (entry["gateway_id"], entry["limit"]) == (where["gateway"], 8)

    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").queue_limit = 3
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], seq=2))
    assert runtime.status()["queues"][0]["limit"] == 3


def test_reconfiguring_a_running_plugin_keeps_its_memory_and_its_batches(client):
    """改队列上限/作用域换的是运输参数：旧队列里已经交进来的批要搬完，实例的基线要留着。

    `asyncio.Queue` 的 maxsize 建好就不能改，所以参数一变必然要换队列。换的那一刻有两个坑，
    现象都长得像"插件忽然安静了"：旧队列随对象一起消失（用户改一个数字，吞掉几批数据），
    以及顺手重建实例把 `anomaly-detector` 的基线清零（它从此又要"安静几秒"才开始说话）。
    两条一起钉，因为它们出自同一行代码。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": CountsFeeds})
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))

    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").queue_limit = 3
    runtime.refresh()

    assert [r.value for r in rows_of(client, PluginFinding)] == [1.0], "改参数那一刻，队列里那批没被搬完"
    assert rows_of(client, PluginLog) == [], "没吞数据就不该额外记一条流水"

    runtime.offer(
        one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=2, ts=moment(100))
    )
    runtime.run_once()
    assert [r.value for r in rows_of(client, PluginFinding)] == [1.0, 2.0], "换了实例：它的记忆被清零了"


def test_changing_the_scope_takes_effect_on_the_very_next_batch(client):
    """改作用域必须下一批就生效，而旧筛子筛进来的那几批要先搬完。

    运行态的复用判据只看队列上限的话，"把这支插件从整场缩到 A 场地"这件事就会在页面上写成已保存、
    实际一动不动——它继续吃 B 场地的帧，而阈值那一层的来源链看着完全正确。这一条把两个方向都钉住：
    改完之后的 A 场地批不再进队列，B 场地的进。
    """
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    main = add_gateway(client, a["site"], "a2", 7)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": CountsFeeds})
    runtime.refresh()
    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=main["gateway"], node_id=main["node"])
    ) == "queued"

    with client.app.state.db.scope() as session:
        row = session.get(PluginRegistry, "alert-engine")
        row.scope, row.scope_id = "site", b["site"]
    runtime.refresh()

    assert [r.value for r in rows_of(client, PluginFinding)] == [1.0], "旧作用域下的那批没被搬完"
    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=main["gateway"], node_id=main["node"],
                  seq=2, ts=moment(100))
    ) == "out_of_scope", "作用域改了，运行态还在按旧的筛"
    assert runtime.offer(
        one_batch(site_id=b["site"], gateway_id=b["gateway"], node_id=b["node"],
                  seq=3, ts=moment(200))
    ) == "queued"


def test_a_reconfigure_that_swallows_batches_says_so(client, monkeypatch):
    """搬不动就得留数字：改参数那一步读不动配置时，那几批是真没了，不是"这一轮没产出"。

    与停用那两条同一条纪律的三个方向里剩下的一个——停用、参数变、收摊都可能撞上一次读库失败，
    而页面上"插件没动"与"数据被吞了"必须是两句不同的话。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))

    def boom(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "_config_for", boom)
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").queue_limit = 5
    runtime.refresh()

    assert [(r.event, json.loads(r.detail_json)) for r in rows_of(client, PluginLog)] == [
        ("reconfigured", {"discarded_batches": 1})
    ]
    assert runtime.status()["batches_lost"] == 1


def test_a_full_queue_drops_the_oldest_batch_and_leaves_the_count_behind(client):
    """队列打满时丢最旧那批，而丢了几批必须留在库里可查。

    不 backpressure 的理由与 `store.GatewayQueue` 一模一样：投递口在采集的事件循环上，一等
    就把透传卡住（那是"网页还连着但数据停格"）。只断言"没丢"是不够的，所以要正面读出**丢的
    是最旧**：活下来的应该是 `seq=2` 那一帧，它的观测时刻会成为唯一那条判定的时间。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine", queue_limit=2)
    runtime = runtime_for(client)
    runtime.refresh()

    outcomes = [
        runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=i,
                                ts=moment(i * 100)))
        for i in range(4)
    ]
    assert outcomes == ["queued", "queued", "dropped_oldest", "dropped_oldest"]
    assert runtime.status()["dropped_batches"] == 2

    runtime.run_once()
    state = {r.plugin_id: r for r in rows_of(client, PluginState)}["alert-engine"]
    assert (state.processed, state.dropped) == (2, 2)
    rows = rows_of(client, PluginFinding)
    assert len(rows) == 1, f"30 s 抑制窗口内只该有一条：{[(r.value, r.ts) for r in rows]}"
    assert to_utc(rows[0].ts) == moment(200), "丢的不是最旧那批"
    assert json.loads(state.stats_json)[str(where["gateway"])] == {
        "depth": 0,
        "peak": 2,
        "dropped": 2,
        "limit": 2,
    }



def test_a_plugin_locked_to_a_site_never_queues_another_sites_frames(client):
    """S7 那一行"规则作用域可锁到场地"落在这里：别的场地一块板都不进它的队列。

    阈值本来就能按场地分（配置层），作用域管的是另一件事——**数据进不进这支插件**。两件叠起来
    才是"A 场地的规则不会被 B 场地的数吵醒"：只锁阈值的话 B 的帧仍然会喂进同一个实例，
    `anomaly-detector` 的基线就被别的场地的电平带着走，而页面上看不出它到底在看哪块板。
    """
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    # 场地 1 的第一台接入机编号也是 1（自增撞上来了），拿它测"按场地筛"就会让"按网关筛"的
    # 写法蒙混过关。另起一台，`gateway_id` 与 `site_id` 才分得开。
    main = add_gateway(client, a["site"], "a2", 7)
    assert main["gateway"] != a["site"]
    enable_plugin(client, "alert-engine", scope="site", scope_id=a["site"])
    runtime = runtime_for(client)
    runtime.refresh()

    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=main["gateway"], node_id=main["node"])
    ) == "queued"
    assert runtime.offer(
        one_batch(site_id=b["site"], gateway_id=b["gateway"], node_id=b["node"], ts=moment(100))
    ) == "out_of_scope"

    runtime.run_once()
    assert {r.site_id for r in rows_of(client, PluginFinding)} == {a["site"]}
    assert state_of(client, "alert-engine").processed == 1
    assert runtime.status()["batches_out_of_scope"] == 1


def test_a_plugin_locked_to_a_gateway_skips_its_sibling_under_the_same_site(client):
    """作用域收到接入机这一档时，筛的是网关而不是场地。

    两批数据同属 A 场地、只差身份里那一截 `gateway_id`——只有真的按网关筛，"调试一支规则时
    不被整个场地的量吵"这个用法才成立。写在场地那一档上测不出来（上一条已覆盖）。
    """
    a = add_site(client, "alpha", "A场", 5)
    sibling = add_gateway(client, a["site"], "beta", 6)
    enable_plugin(client, "alert-engine", scope="gateway", scope_id=a["gateway"])
    runtime = runtime_for(client)
    runtime.refresh()

    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=a["gateway"], node_id=a["node"])
    ) == "queued"
    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=sibling["gateway"], node_id=sibling["node"],
                  ts=moment(100))
    ) == "out_of_scope"

    runtime.run_once()
    assert {r.gateway_id for r in rows_of(client, PluginFinding)} == {a["gateway"]}


def test_a_scope_the_runtime_cannot_execute_claims_nothing(client):
    """库里手写一行 `scope=relay`：运行时认不了它就一块板都不看，而不是悄悄当全局跑。

    配错该表现为"它不说话了"（`config_layer` 那条纪律的插件侧版本），写侧的 422 拦不住历史库里
    已经躺着的那一行（旧备份带回的、照着文档手写的），所以认不了的那一档在投递侧也必须认不出。
    今天执行得了的是 `global`/`site`/`gateway`：`FrameBatch` 上就带着这三档的身份，而 relay 那一档
    需要一次网关→位置的映射，把它放进热路径就违反了"投递侧不碰库"（模块注释第 1 条）。
    """
    a = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine", scope="relay", scope_id=a["gateway"])
    runtime = runtime_for(client)
    runtime.refresh()

    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=a["gateway"], node_id=a["node"])
    ) == "out_of_scope"
    runtime.run_once()
    assert state_of(client, "alert-engine").processed == 0
    assert rows_of(client, PluginFinding) == []


def test_a_crashing_plugin_is_marked_failing_without_silencing_the_others(client):
    """插件崩了只该让它自己停下来：另一支照常出判定，采集那边一个字节的投递都没断。

    规范 §10.4 那句"插件崩溃不拖垮采集"在这里有两个可断言的面：**别的插件还在说话**（同一轮里
    健康那支的判定落了库），以及**崩那支留下了可查的现场**（`failing` + 错误文本 + 一条
    `crashed` 流水）。只把异常吞掉不留现场是最坏的写法——现象会只剩"这支插件不工作了"。
    """
    from app.plugins.interference import InterferenceCorrelator

    where = add_site(client, "alpha", "A场", 5)
    second = add_node(client, where["gateway"], "板2")
    enable_plugin(client, "alert-engine")
    enable_plugin(client, "interference-correlator")
    runtime = runtime_for(
        client,
        implementations={
            "alert-engine": AlwaysFails,
            "interference-correlator": InterferenceCorrelator,
        },
    )

    runtime.refresh()
    for node_id, index in ((where["node"], 0), (second, 1)):
        assert runtime.offer(
            one_batch(gateway_id=where["gateway"], site_id=where["site"], node_id=node_id,
                      seq=index, ts=moment(index * 100))
        ) == "queued"
    runtime.run_once()

    broken = state_of(client, "alert-engine")
    assert (broken.status, broken.errors) == ("failing", 1)
    assert broken.last_error and "规则里炸了" in broken.last_error
    logs = [(r.plugin_id, r.event) for r in rows_of(client, PluginLog)]
    assert ("alert-engine", "crashed") in logs, logs

    healthy = state_of(client, "interference-correlator")
    assert healthy.status == "running" and healthy.produced == 1
    assert [(r.plugin_id, r.kind) for r in rows_of(client, PluginFinding)] == [
        ("interference-correlator", "multi_node_interference")
    ]


def test_all_three_builtin_plugins_survive_one_tick_together(client):
    """启用全部内置插件的**新装实例**必须三支都在跑——2026-10-30 真机上它是两支当场停摆。

    生效配置的缓存早先按"这一拍谁先搬队"截断，于是读一把键的 `anomaly-detector` 把该场地的缓存
    建成 `['anomaly_rise_db']`，后面两支各自 `KeyError` 在自己的第一把键上。这条用例的覆盖力全部
    来自两点，缺一就退回假绿：

    - **真实现**，不是注入的假插件——只有真的才带着真 `CONFIG_KEYS`，而键集各不相同；
    - **同一次 `run_once()` 里三支都被喂**——`_runs` 的迭代序就是注册表行序（元组序），
      所以"小键集的先跑"这个致命顺序是自然成立的，不用手工摆。

    判据不看"有没有判定"（单帧凑不齐基线、也凑不齐两处同时），只看**没有人带着错误停下来**。
    """
    where = add_site(client, "alpha", "A场", 5)
    for plugin_id in BUILTINS:
        enable_plugin(client, plugin_id)
    runtime = runtime_for(client)
    runtime.refresh()

    assert runtime.offer(
        one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=1, ts=moment(0))
    ) == "queued"
    runtime.run_once()

    for plugin_id in BUILTINS:
        state = state_of(client, plugin_id)
        assert (state.status, state.errors, state.last_error) == ("running", 0, None), plugin_id
    assert runtime.errors == 0 and runtime.last_error is None
    assert runtime.status()["failing_plugins"] == []
    # 缓存那份是**整张生效表**，不是某一家的键集——截断过一次就是这次的成因。
    assert "threshold_db" in runtime._configs[where["site"]][0]
    assert "anomaly_rise_db" in runtime._configs[where["site"]][0]


def test_a_key_the_registry_cannot_supply_is_the_runtimes_own_failure(client):
    """插件声明了配置层给不出的键：算运行时的错，不算那支规则的错。

    分界的意义是"运营者该去查哪里"。让它变成插件 `failing`，他会去调阈值——而阈值根本没被读到；
    而 `_config_for` 里那个 `if key in effective` 的静默过滤更糟：插件在自己内部 `KeyError`，
    流水里只留下一个键名，看不出是声明与注册表对不上。现在它抛在边界上，进 `batches_lost` 与
    `problem()`，库里那一行一个字都不动。
    """

    class DeclaresAnImpossibleKey:
        CONFIG_KEYS = ("threshold_db", "not_a_registry_key")

        def feed(self, batch, config):
            raise AssertionError("拿不到配置就不该被喂")

    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": DeclaresAnImpossibleKey})
    client.app.state.plugins = runtime
    runtime.refresh()
    assert runtime.offer(
        one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=1, ts=moment(0))
    ) == "queued"

    runtime.run_once()

    state = state_of(client, "alert-engine")
    assert (state.status, state.errors) == ("stopped", 0), "配置层的缺口被记成了插件的失败"
    assert runtime.batches_lost == 1 and runtime.errors == 1
    assert "not_a_registry_key" in (runtime.last_error or "")
    assert runtime.status()["failing_plugins"] == []
    assert "not_a_registry_key" in client.get("/api/health").json()["plugins"]["problem"]


def test_a_plugin_that_stopped_retrying_makes_health_say_so(client):
    """退避耗尽之后健康检查必须继续说话：那是**现在**坏着，不是历史上有过失败。

    真机上这一条是 11 次轮询全 `ok`——耗尽后它不再尝试，于是每轮都"没有新失败"，只看 `last_error`
    的 `problem()` 把两支永久停摆的插件读成了正常。判据要连"复位"那一半一起测：用户点名停用之后
    这句必须收回去，否则 degraded 会变成没人信的常驻噪声。
    """

    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": AlwaysFails})
    # 把健康检查指到**这一支真的跑过退避的**运行时上：夹具那个 app 自带的四条后台循环是关着的，
    # `app.state.plugins` 那份内存态从没崩过，用它断言"页面看不看得见"等于什么都没测。
    client.app.state.plugins = runtime
    runtime.refresh()
    for seq in (1, 2, 3, 4):
        assert runtime.offer(
            one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=seq,
                      ts=moment(seq * 1000))
        ) == "queued"
        runtime.run_once(now=moment((seq - 1) * 60_000))

    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["plugins"]["failing_plugins"] == [
        {
            "plugin_id": "alert-engine",
            "last_error": "RuntimeError: 规则里炸了",
            # 四帧之间隔着一分钟，退避（1/2/5/30 s）早到点，所以每一次都是"到点重试又崩"，
            # 到第四次就是耗尽——这一句必须是 False，否则页面会说"再等等它自己会好"。
            "retrying": False,
        }
    ]
    assert body["plugins"]["problem"] in body["problems"]
    assert "alert-engine" in body["plugins"]["problem"]
    assert "退避已耗尽" in body["plugins"]["problem"]

    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False
    runtime.refresh()
    # 空一拍才收回：`last_error` 说的是"运行时最近一次失败"，规则崩也算（`problem()` 那句明写了
    # "规则崩溃与库读写都算"），停用这个动作本身不该替它改口——后台循环跑成一轮干净的才该。
    runtime.run_once(now=moment(600_000))
    body = client.get("/api/health").json()
    assert body["plugins"]["failing_plugins"] == []
    assert body["plugins"]["problem"] is None, "停用并跑干净之后还在报 degraded"


def test_a_repeatedly_crashing_plugin_gets_three_restarts_and_then_goes_quiet(client):
    """退避重启有上限，而且"没到点"与"耗尽之后"这两段都必须真的不动。

    三段各有各的现象，少一段都是只能靠读代码才知道的行为：

    - **没到点不重启**：否则"退避"只是句空话，崩—重启会按每轮的节奏往 `plugin_log` 里写行，
      而那正是 R11 在 SD 卡上要防的写放大； queued 的那一帧也得住在队列里（`depth` 说它在）。
    - **到了点真重启**：`restarts` 在涨、流水里有 `restarting`，而且重启换的是**新实例**——
      带着坏状态重启等于原地再崩一次。
    - **耗尽之后安静**：停在 `failing`、`next_retry_at` 清空，此后任意一轮都不再动任何计数。
      没有这一段，"上限 3 次"就只是注释里的一句话。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": AlwaysFails})
    runtime.refresh()
    start = moment(0)

    def offer(seq: int) -> None:
        assert runtime.offer(
            one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=seq,
                      ts=moment(seq * 100))
        ) == "queued"

    offer(1)
    runtime.run_once(now=start)
    state = state_of(client, "alert-engine")
    assert (state.status, state.errors, state.restarts) == ("failing", 1, 0)
    assert to_utc(state.next_retry_at) == start + timedelta(seconds=1)

    offer(2)
    runtime.run_once(now=start + timedelta(milliseconds=500))
    state = state_of(client, "alert-engine")
    assert (state.errors, state.processed) == (1, 1), "退避没到点就被喂了第二帧"
    assert runtime.status()["queues"][0]["depth"] == 1

    for restart, at in ((1, 2), (2, 5), (3, 30)):
        # 每一轮都得有新数据可喂：没有数据的那一轮根本走不到 `feed()`，"重启"就没被检验过。
        offer(at)
        runtime.run_once(now=start + timedelta(seconds=at))
        state = state_of(client, "alert-engine")
        assert (state.restarts, state.errors) == (restart, restart + 1)

    assert state.next_retry_at is None, "耗尽后还留着下一次可尝试时刻"
    events = [r.event for r in rows_of(client, PluginLog) if r.plugin_id == "alert-engine"]
    assert events.count("crashed") == 4
    assert events.count("restarting") == 3
    assert events.count("backoff_exhausted") == 1

    before = (state.errors, state.restarts, state.processed)
    runtime.run_once(now=start + timedelta(hours=1))
    after = state_of(client, "alert-engine")
    assert (after.errors, after.restarts, after.processed) == before, "放弃之后还在自己重启"


def test_an_aggregator_instance_refuses_to_run_plugins(client):
    """汇聚实例不跑插件：那一台没有桥，也没有属于本地产出的东西可推。

    与 `capture`/`dialer` 同口径——按设计没有这一环是**形态**不是**故障**，所以 `problem()`
    必须回 None（把可选件报成 degraded 的代价是运营者开始忽略 degraded）。另一半是"它不攒队列"：
    汇聚角色连 `refresh()` 都不该走，否则 `offer()` 会替一支永不消费的插件把内存吃掉。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, instance_role="aggregator")

    async def scenario():
        await runtime.start()
        await runtime.stop()

    asyncio.run(scenario())
    status = runtime.status()
    assert status["refused"] == "aggregator_role"
    assert status["running"] is False
    assert runtime.problem() is None
    assert runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"])) == "disabled"
    assert runtime.status()["queues"] == []


def test_the_started_runtime_consumes_offered_batches_on_its_own(client):
    """"启用了就会自己跑"这句话的执行者是后台循环，不是测试里手工调的那句 `run_once()`。

    判据取的是 `findings_written`（内存里的那个数），不是直接查库：单线程的 `asyncio.run` 里
    插一条 SQL 会把事件循环停住，而 `run_once()` 正跑在线程池那根线程上——测试会把"我没等到"
    误报成"它没跑"。断言落库只在停循环之后做。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.tick_interval_s = 0.01

    async def scenario():
        await runtime.start()
        runtime.offer(one_batch(gateway_id=where["gateway"], site_id=where["site"],
                                node_id=where["node"]))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runtime.findings_written:
                break
        await runtime.stop()

    asyncio.run(scenario())
    assert runtime.running is False
    rows = rows_of(client, PluginFinding)
    assert [(r.plugin_id, r.kind) for r in rows] == [("alert-engine", "threshold_crossing")]
    state = state_of(client, "alert-engine")
    assert state.status == "stopped", "进程收摊了还说自己在 running，页面上就是假的活动状态"
    assert [(r.plugin_id, r.event) for r in rows_of(client, PluginLog)] == [
        ("alert-engine", "stopped")
    ]


def test_a_mock_batch_produces_a_mock_finding(client):
    """回放/模拟数据推出的判定必须带着 `mock`——否则一条推论会冒充实时。

    `PluginFinding.source` 的列默认是 `real`，所以"运行时忘了写这一列"的现象是一屏看着像实时的
    推论，而库里没有任何线索指向"这批点来自一份录制文件"。这条测的正是那一列**必须被显式写**。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()

    assert runtime.offer(
        one_batch(gateway_id=where["gateway"], site_id=where["site"], node_id=where["node"],
                  source="mock")
    ) == "queued"
    runtime.run_once()
    [row] = rows_of(client, PluginFinding)
    assert row.source == "mock"


def test_the_producer_version_tracks_the_registry_row(client):
    """`producer` 里那个版本号取自注册表那一行，不是代码里写死的一串。

    将来 `registry.py` 把版本升到 0.2.0，旧判定仍写着 0.1.0、新判定必须跟着变。写死的版本串在
    这一天到来时不会红，只会把新产物静默挂到旧版本上——那正好抹平了"这条是谁说的"里唯一能追责
    的那一截。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").version = "0.2.0"
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], site_id=where["site"],
                            node_id=where["node"]))
    runtime.run_once()
    [row] = rows_of(client, PluginFinding)
    assert row.producer == "rule:alert-engine@0.2.0"


class CarriesBadState:
    """一支**崩在自己攒的状态上**的插件：第一帧好，之后每一帧都抛。

    它存在的唯一理由是换实例这件事要可测：一支每次都崩的插件（`AlwaysFails`）在"换没换新实例"
    两种实现下现象一模一样，所以那条判据必须找一支"旧实例继续崩、新实例第一帧就好"的插件。
    """

    CONFIG_KEYS = ("threshold_db",)

    def __init__(self) -> None:
        self.seen = 0

    def feed(self, batch, config):
        self.seen += 1
        if self.seen > 1:
            raise RuntimeError(f"第 {self.seen} 帧踩到坏状态")
        point = batch.points[0]
        return [
            Finding(
                kind="carried_state",
                slot=point.slot,
                metric="rssi",
                value=point.rssi,
                threshold=config["threshold_db"],
                evidence={"seen": self.seen},
            )
        ]

    def stats(self) -> dict[str, int]:
        return {"seen": self.seen}


def test_a_restart_hands_the_plugin_a_fresh_instance(client):
    """退避重启换的是**新实例**：旧实例那份把它压垮的状态不该跟着活过重启。

    现象要能分开才谈得上断言：不换实例时重启后那一帧照样崩（`failing` 继续加深），换了就出判定
    并回到 `running`。所以这里用的是一支"第一帧好、之后都抛"的插件，而不是每次都抛的那支——
    后者在两种实现下长得分毫不差，测了等于没测。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": CarriesBadState})
    runtime.refresh()
    start = moment(0)

    def offer(seq: int) -> None:
        assert runtime.offer(
            one_batch(gateway_id=where["gateway"], node_id=where["node"], seq=seq,
                      ts=moment(seq * 100))
        ) == "queued"

    offer(1)
    runtime.run_once(now=start)
    assert state_of(client, "alert-engine").produced == 1

    offer(2)
    runtime.run_once(now=start + timedelta(seconds=1))
    broken = state_of(client, "alert-engine")
    assert (broken.status, broken.errors) == ("failing", 1)

    offer(3)
    runtime.run_once(now=start + timedelta(seconds=3))
    after = state_of(client, "alert-engine")
    assert after.status == "running", "重启后还在崩，等于没换新实例"
    assert after.produced == 2, f"新实例没把第三帧推成判定：{after.produced}"
    assert after.restarts == 1 and after.errors == 1


def test_stopping_the_runtime_does_not_explain_away_a_failing_plugin(client):
    """收摊只把"还在跑"的落回 `stopped`，`failing` 得原样留着。

    崩溃是这支插件自己的事实，不是"这个进程结束了"就能一笔勾销的东西。写成 stopped 的现象是
    "重启一下它就好了"，而那一轮它多半又立刻崩回去——现场被清掉的地方正是最难查的地方。
    复位留给用户的动作（S7-6 的启用端点），而不是留给关停。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, implementations={"alert-engine": AlwaysFails})

    async def scenario():
        await runtime.start()
        runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if runtime.findings_written or runtime.errors:
                break
        await runtime.stop()

    asyncio.run(scenario())
    state = state_of(client, "alert-engine")
    assert (state.status, state.errors) == ("failing", 1)
    assert "stopped" not in [r.event for r in rows_of(client, PluginLog)]


def test_disabling_a_running_plugin_lands_its_state_back_on_stopped(client):
    """停用一支正在跑的插件：运行时把它从"该投喂谁"里摘掉的那一刻，库里那一行要跟着说 stopped。

    只等用户重启或者等 `POST /disable` 顺手写是不够的——`refresh()` 才是"这一支不再被消费"这个
    事实真正发生的地方，而页面上的状态必须跟着事实，不是跟着按钮。`failing` 在这里要被覆盖：
    用户点名要它停，那就是复位现场的那个动作（与上一条测试正好互为边界）。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))
    runtime.run_once()
    assert state_of(client, "alert-engine").status == "running"

    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False
    runtime.run_once()

    assert runtime.status()["enabled_plugins"] == []
    state = state_of(client, "alert-engine")
    assert state.status == "stopped"
    logs = [(r.plugin_id, r.event) for r in rows_of(client, PluginLog)]
    assert ("alert-engine", "disabled") in logs, logs
    assert state.produced == 1, "停用不该顺手抹掉它已经产出的计数"


def test_disabling_a_plugin_drains_the_batches_already_handed_over(client):
    """停用不许吞掉已经交进来的批：队列里剩的要在这一拍搬完落库。

    方案 §4 那一行「停用的插件队列必须排空且不丢计数」落在运行时的 retire 那一步，而不是
    `POST /disable` 里顺手 while 一下：端点改的是库里那一行，而"哪些批还在内存里"只有运行时知道，
    它又必须在自己那一拍上跑（`_runs` 与队列只在那条 executor 序列里被改）。

    时间序也要一起钉住：三批只落一条判定是 `alert_hold_s` 的抑制窗（规则侧的事），但**留下的是
    最早那一批**——排空如果倒着搬，页面上那条判定的时刻就会是最后一批，而判定的时间轴正是
    S8 复核要读的那一栏。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    for offset in (0, 100, 200):
        runtime.offer(
            one_batch(
                site_id=where["site"],
                gateway_id=where["gateway"],
                node_id=where["node"],
                ts=moment(offset),
            )
        )
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False

    runtime.run_once()

    state = state_of(client, "alert-engine")
    assert state.status == "stopped"
    assert (state.processed, state.produced) == (3, 1)
    rows = rows_of(client, PluginFinding)
    assert len(rows) == 1, f"排空后只剩 {len(rows)} 条判定，三批都该被搬过"
    assert to_utc(rows[0].ts) == moment(0), "排空必须按交进来的先后搬，倒着搬会把时间轴挪到最后一批"
    disabled = [r for r in rows_of(client, PluginLog) if r.event == "disabled"]
    assert json.loads(disabled[0].detail_json) == {"discarded_batches": 0}
    assert runtime.status()["enabled_plugins"] == []
    assert runtime.status()["batches_lost"] == 0


def test_a_disable_that_cannot_drain_says_how_many_were_left_behind(client, monkeypatch):
    """排空搬不动时（写库读库都算），剩下的批数必须写进那条流水，不能只说"已停止"。

    与上一条配成一对：一条钉"排空了"，一条钉"没排空也要报数"。少了后一句，停用就成了一件
    可能静默吞数据的事——而吞掉的那些恰恰是"我停用之前那几秒到底采到了没有"唯一能问的东西。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False

    def boom(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "_config_for", boom)
    runtime.run_once()

    assert state_of(client, "alert-engine").status == "stopped"
    disabled = [r for r in rows_of(client, PluginLog) if r.event == "disabled"]
    assert json.loads(disabled[0].detail_json) == {"discarded_batches": 1}
    assert runtime.status()["batches_lost"] == 1
    assert rows_of(client, PluginFinding) == []


def test_a_disable_drains_a_deeper_queue_than_one_round_carries(client):
    """队列比一轮搬运的定额深时，停用要把它**全部**搬完，而不是搬一轮就宣布停止。

    `DRAIN_CHUNK` 是给 `run_once()` 的封顶（一轮不能长时间占着线程池里那根线程），不是给排空
    封顶的：一支默认 64 批容量的队列打满后，一次停用只搬 32 批的话，剩下那 32 批就随实例一起没了，
    而"停用前那几十秒采到的东西"正是这一列存在的全部理由。轮数按容量顶到最坏，搬完为止。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    total = DRAIN_CHUNK + 8
    for index in range(total):
        runtime.offer(
            one_batch(
                site_id=where["site"],
                gateway_id=where["gateway"],
                node_id=where["node"],
                ts=moment(index * 100),
            )
        )
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False

    runtime.run_once()

    state = state_of(client, "alert-engine")
    assert state.processed == total, "比一轮定额深的那些批没被搬完"
    assert state.produced == 1, "越限判定受 `alert_hold_s` 抑制：一批都没被漏搬，但同窗内只说一句"
    assert runtime.status()["batches_lost"] == 0
    disabled = [r for r in rows_of(client, PluginLog) if r.event == "disabled"]
    assert json.loads(disabled[0].detail_json) == {"discarded_batches": 0}


def test_a_disable_that_loses_batches_is_reported_even_when_the_row_was_already_stopped(
    client, monkeypatch
):
    """库里那一行已经是 `stopped`（端点先写的），运行时排空时仍吞了批——这一条流水还是得写。

    去重只该去"没数据损失"的那种重复：`POST /disable` 把状态写成 stopped 之后、下一次 `refresh()`
    之前，采集还在往那支插件的队列里交批（`offer()` 读的是内存快照，它不知道端点刚改了库）。
    这些批因为停用而消失时，页面上唯一说得出这件事的就是这条流水——把它按"状态没变"省掉，
    吞数据就变成一件无人说过的事。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))
    with client.app.state.db.scope() as session:
        session.get(PluginRegistry, "alert-engine").enabled = False
        session.get(PluginState, "alert-engine").status = "stopped"

    def boom(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "_config_for", boom)
    runtime.run_once()

    disabled = [r for r in rows_of(client, PluginLog) if r.event == "disabled"]
    assert [json.loads(r.detail_json) for r in disabled] == [{"discarded_batches": 1}]


def test_a_runtime_database_failure_is_not_blamed_on_the_plugin(client, monkeypatch):
    """库读不动是**运行时**的故障，不许记成插件崩溃。

    分不清这一层，现象就是"一次 `database is locked` 让三支好插件一起进入退避"，而运营者会去
    查规则——查不出任何东西，因为规则没错。所以这里两边都要钉住：`plugin_state` 一个字段都没动
    （没有 `failing`、没有 `errors`、没有流水），而运行时自己说得出"坏了、丢了几批"。

    丢的那几批必须数得出来：搬出队列之后才失败，数据回不去（塞回队尾会把时间序打乱，规则插件
    的窗口就毁了）。不数它，"读库失败一轮"在页面上的表现就是"这一轮插件没产出"。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))

    def boom(*_args):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(runtime, "_config_for", boom)
    runtime.run_once()

    state = state_of(client, "alert-engine")
    assert (state.status, state.errors) == ("stopped", 0)
    assert rows_of(client, PluginLog) == []
    assert "database is locked" in runtime.problem()
    assert runtime.status()["batches_lost"] == 1


def test_a_failing_write_loses_the_batches_without_blaming_the_plugin(client, db_path):
    """写不进库同样是运行时的故障：这条走的是真 SQL，不是把 `_write` 换成会抛的假函数。

    与上一条分开钉是因为失败的位置不同：读配置失败时一行都还没攒，而写失败时**判定与状态在同一个
    事务里**，一起回滚。所以这里要钉的不只是"插件没被记成 failing"，还有"它的计数也没往前走"——
    要是只回滚判定、状态照写，页面上就会出现"这批处理过了但一条判定都没有"这种自相矛盾的事实。
    """
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client)
    runtime.refresh()
    runtime.offer(one_batch(gateway_id=where["gateway"], node_id=where["node"]))

    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE plugin_finding")
    runtime.run_once()

    state = state_of(client, "alert-engine")
    assert (state.status, state.errors, state.processed) == ("stopped", 0, 0)
    assert runtime.status()["batches_lost"] == 1
    assert "plugin_finding" in runtime.problem()


# --- S7-6：启停 / 状态 / 日志 / 配置的 HTTP 入口 --------------------------------


def plugin_view(client, plugin_id: str) -> dict:
    """从 `GET /api/plugins` 里取一支插件那一行（页面看到的那一份，不是库里那一份）。"""
    return {row["plugin_id"]: row for row in client.get("/api/plugins").json()["plugins"]}[
        plugin_id
    ]


def set_state(client, plugin_id: str, **fields) -> None:
    """把运行态那一行摆成 supervisor 会摆的样子：启停端点要处理的输入是**已有的状态**。"""
    with client.app.state.db.scope() as session:
        state = session.get(PluginState, plugin_id)
        for name, value in fields.items():
            setattr(state, name, value)


def add_row_without_implementation(client, plugin_id: str = "not-a-real-plugin") -> str:
    """直接往注册表插一行代码里没有实现的插件（旧备份带回、或照文档手写的那种）。"""
    with client.app.state.db.scope() as session:
        session.add(PluginRegistry(plugin_id=plugin_id, name="来历不明", version="0.0.1"))
    return plugin_id


def test_the_plugin_page_lists_the_builtin_registry_in_code_order(client):
    """列表顺序 = 代码里那张表的顺序，`supported_scopes` 与运行时共用一份事实源。

    为什么不按 `plugin_id` 排：`registry.BUILTIN_PLUGINS` 的注释写了"元组顺序即页面顺序"，新增
    一支 append 在末尾。按 id 排序会让新插件插到别人中间，而用户的肌肉记忆跟着那个位置。
    """
    payload = client.get("/api/plugins").json()
    assert payload["instance_role"] == "site"
    assert payload["supported_scopes"] == list(EXECUTABLE_SCOPES)
    assert [row["plugin_id"] for row in payload["plugins"]] == [
        item.plugin_id for item in registry.BUILTIN_PLUGINS
    ]
    for row in payload["plugins"]:
        assert row["implementation"] is True
        assert row["enabled"] is False, "S7 的回滚形态：装完一个都没在跑"
        assert row["keys"] == list(IMPLEMENTATIONS[row["plugin_id"]].CONFIG_KEYS)
        assert row["scope"] == "global" and row["scope_executable"] is True
        assert row["queue_limit"] is None
        assert row["effective_queue_limit"] == client.app.state.settings.plugin_queue_limit
        assert row["status"] == "stopped" and row["queues"] == []


def test_a_registry_row_with_no_implementation_still_shows_on_the_page(client):
    """代码里没有实现的那一行照样列出来，并说清"它不会跑"。

    `registry.sync()` 为它记了一条 warning，但那是流水；用户在插件页找的是自己写的那一行——列表里
    没有它的话，唯一的解释就只剩"我加的东西被吞了"。`status` 是 null：没有实现就没有运行态，
    给它补一行等于假装它能跑（同 `registry.sync` 第 3 条纪律）。
    """
    plugin_id = add_row_without_implementation(client)

    rows = client.get("/api/plugins").json()["plugins"]
    assert [row["plugin_id"] for row in rows][-1] == plugin_id, "内置的在前，来历不明的排最后"
    extra = {row["plugin_id"]: row for row in rows}[plugin_id]
    assert extra["implementation"] is False
    assert extra["status"] is None and extra["queues"] == []
    assert (extra["errors"], extra["processed"]) == (
        None,
        None,
    ), "没有状态行就是没有：补一个 0 会读成“它跑过、处理了 0 批”"


def test_enabling_a_plugin_records_the_transition_once(client):
    """`enable` 改的是注册表那一行，流水只在真的翻了面时记一条。

    重复点击不该把 `plugin_log` 变成点击计数器（这张表记的是状态跃迁与错误）。端点也刻意**不**在这里
    把状态写成 `running`——那一刻还没到，真开始跑的是运行时下一轮的 `refresh()`（≤ 一个节拍）。
    页面上"已启用"与"在跑"必须是两句话。
    """
    body = client.post("/api/plugins/alert-engine/enable").json()
    assert (body["plugin_id"], body["enabled"], body["changed"]) == ("alert-engine", True, True)
    assert plugin_view(client, "alert-engine")["enabled"] is True
    assert state_of(client, "alert-engine").status == "stopped", "还没跑，别报成 running"

    assert client.post("/api/plugins/alert-engine/enable").json()["changed"] is False
    assert [(r.plugin_id, r.event, r.level) for r in rows_of(client, PluginLog)] == [
        ("alert-engine", "enabled", "info")
    ]


def test_enabling_a_plugin_in_backoff_gives_it_another_chance(client):
    """已经启用着、但 supervisor 已经放弃的那一支：再点一次启用要能把它捞回来。

    这是"重复启用不记流水"必须留的那个例外——注册表那一行本来就是 1，`changed` 是 False，如果端点
    因此什么都不做，用户就没有第二个入口能把 `restarts` 打回 0：只能先停用再启用，而停用会真的收摊、
    把队列里剩下的东西搬完丢掉。累计 `errors` 不清：那是"这支插件历史上坏过几次"的审计数字，
    不是重试配额。
    """
    enable_plugin(client, "alert-engine")
    set_state(
        client,
        "alert-engine",
        status="failing",
        restarts=RESTART_LIMIT,
        next_retry_at=moment(60_000),
        errors=2,
        processed=5,
        produced=1,
    )

    body = client.post("/api/plugins/alert-engine/enable").json()
    assert (body["changed"], body["status"]) == (False, "stopped")

    state = state_of(client, "alert-engine")
    assert (state.restarts, state.next_retry_at) == (0, None)
    assert (state.errors, state.processed, state.produced) == (2, 5, 1), "累计数不是重试配额"
    assert [(r.event, r.detail_json) for r in rows_of(client, PluginLog)] == [("retried", None)]


def test_enabling_a_running_plugin_writes_nothing(client):
    """正在跑的那一支被再点一次启用：一个字段都不该动。

    与上一条只差一个 `status`，所以两条都要留着：把"不是 running"当成"没在跑"统一清一遍，就会在
    用户多点一下的时候把正在跑的插件抹成"没开始过"（`started_at` 清空、`restarts` 归零），而它
    下一秒还在产出判定。
    """
    enable_plugin(client, "alert-engine")
    set_state(client, "alert-engine", status="running", started_at=moment(0), processed=9)

    client.post("/api/plugins/alert-engine/enable")

    state = state_of(client, "alert-engine")
    assert (state.status, to_utc(state.started_at), state.processed) == ("running", moment(0), 9)
    assert rows_of(client, PluginLog) == []


def test_disabling_does_not_claim_the_plugin_has_stopped(client):
    """禁用只把注册表那一行改成 0，**不**替运行时宣布"已经停了"。

    端点不许去搬队列——那会撞进执行线程正在用的那份运行态（模块注释第 1 条与 `refresh()` 的分工），
    所以队列还没搬完时页面上就该继续显示 `running`，那是真的。`disabled` 这条流水说的是"运营者让它
    停"，而"它停下来了、搬完几批没"另有运行时自己写的 `stopped_consuming`。
    """
    enable_plugin(client, "alert-engine")
    set_state(client, "alert-engine", status="running", started_at=moment(0))

    body = client.post("/api/plugins/alert-engine/disable").json()
    assert (body["enabled"], body["changed"]) == (False, True)
    assert state_of(client, "alert-engine").status == "running"
    assert [(r.event, r.level) for r in rows_of(client, PluginLog)] == [("disabled", "info")]

    assert client.post("/api/plugins/alert-engine/disable").json()["changed"] is False
    assert len(rows_of(client, PluginLog)) == 1, "重复停用同样不该多一条流水"


def test_the_state_endpoint_shows_the_queues_of_this_instances_runtime(client):
    """`/state` 里那几条队列读的是**本实例此刻**的运行时，不是上次落库的快照。

    `plugin_state.stats_json` 是上一轮 `_write` 留下的，最坏情况滞后一个节拍；页面上"这条队列现在堵不堵"
    必须说此刻的事实。这条同时钉住接线方式：运行时挂在 `app.state.plugins` 上，端点与采集侧投喂的是
    同一个对象——端点自己去 new 一个的话，它看到的队列永远是空的。
    """
    where = add_site(client, "alpha", "A场", 5)
    client.post("/api/plugins/alert-engine/enable")
    runtime = client.app.state.plugins
    assert runtime is not None, "运行时挂在 app.state 上"
    runtime.refresh()
    runtime.offer(
        one_batch(site_id=where["site"], gateway_id=where["gateway"], node_id=where["node"])
    )

    state = client.get("/api/plugins/alert-engine/state").json()
    assert (state["plugin_id"], state["enabled"], state["status"]) == (
        "alert-engine",
        True,
        "stopped",
    )
    assert [q["gateway_id"] for q in state["queues"]] == [where["gateway"]]
    assert state["queues"][0]["depth"] == 1
    assert state["queues"][0]["limit"] == client.app.state.settings.plugin_queue_limit
    assert (state["processed"], state["produced"], state["dropped"]) == (0, 0, 0)
    assert state["started_at"] is None and state["last_error"] is None


def test_the_logs_endpoint_is_newest_first_and_says_the_total(client):
    """日志倒序 + `total` 是全量条数：页面要能说"还有 120 条没显示"。

    只有列表没有总数的话，"最近 50 条"与"一共 50 条"在页面上长一个样，而运营者判断的正是"这支插件
    是不是在反复坏"。缺省值写进响应里，页面不必把 50 这个常数再复制一遍。
    """
    with client.app.state.db.scope() as session:
        for n in range(5):
            session.add(
                PluginLog(plugin_id="alert-engine", ts=moment(n * 1000), event=f"e{n}")
            )

    payload = client.get("/api/plugins/alert-engine/logs?limit=2").json()
    assert (payload["plugin_id"], payload["limit"], payload["total"]) == ("alert-engine", 2, 5)
    assert [row["event"] for row in payload["logs"]] == ["e4", "e3"]
    assert payload["logs"][0]["ts"] == iso(moment(4000))

    whole = client.get("/api/plugins/alert-engine/logs").json()
    assert whole["limit"] == 50
    assert len(whole["logs"]) == 5


def test_a_log_request_that_asks_for_an_impossible_limit_is_refused(client):
    """`?limit=` 越界回 422，与 `/api/events`、`/api/samples` 同一个 reason、同一个上界。

    复用 `history.row_limit` 而不是在这儿另立一套（`limit_out_of_range` + `maximum`）：静默夹紧的代价
    由页面付——它以为自己取了 20 万条，画出来的却是最后 2000 条。全站三种取数口对同一个参数用两种
    口径的话，页面就得为插件页写特例，那正是 S5 申报④要消掉的东西。
    """
    for bad in ("0", "-1", "2001"):
        row = client.get(f"/api/plugins/alert-engine/logs?limit={bad}")
        assert row.status_code == 422, (bad, row.text)
        assert row.json()["detail"] == {
            "reason": "limit_out_of_range",
            "param": "limit",
            "maximum": MAX_ROWS,
        }, bad


def test_a_log_whose_detail_is_not_json_says_so(client):
    """库里存坏的 `detail_json` 不该让整个日志接口 500，也不该静默变成空。

    与 `routers/events.py` 的 `payload_error` 同一条纪律：读侧不许猜，也不把坏东西原样吐给页面
    （这一列只由我们自己的 `json.dumps` 写过，读不出来就是库坏了或旧备份带回的，那是要报的异常，
    不是要显示的内容）。
    """
    with client.app.state.db.scope() as session:
        session.add(
            PluginLog(
                plugin_id="alert-engine",
                ts=moment(0),
                level="error",
                event="crashed",
                detail_json="{不是 JSON",
            )
        )

    row = client.get("/api/plugins/alert-engine/logs").json()["logs"][0]
    assert (row["event"], row["level"]) == ("crashed", "error")
    assert row["detail"] is None
    assert row["detail_error"] == "malformed_json"


def test_the_config_endpoint_locks_a_plugin_and_logs_the_change(client):
    """`PUT …/config` 能改的只有三样：作用域、作用域指向、队列上限。

    `name`/`version`/`summary` 由代码那一份覆写（可追责，不是可编辑）；`config_json` 今天没有读者——
    插件取的生效值走配置层（默认 < 全局 < 场地），往这一列写东西只会造出"看着像生效值、其实没人读"
    的假配置。同样的值再 PUT 一次不多写一条流水（页面每次保存都会发一次整份替换）。
    """
    b = add_site(client, "beta", "B场", 6)
    payload = {"scope": "site", "scope_id": b["site"], "queue_limit": 3}
    row = client.put("/api/plugins/alert-engine/config", json=payload)
    assert row.status_code == 200, row.text

    body = row.json()
    assert (body["scope"], body["scope_id"], body["scope_executable"]) == (
        "site",
        b["site"],
        True,
    )
    assert (body["queue_limit"], body["effective_queue_limit"], body["changed"]) == (3, 3, True)
    stored = plugin_view(client, "alert-engine")
    assert (stored["scope"], stored["queue_limit"]) == ("site", 3)
    assert [(r.event, r.level) for r in rows_of(client, PluginLog)] == [("configured", "info")]

    again = client.put("/api/plugins/alert-engine/config", json=payload).json()
    assert again["changed"] is False
    assert len(rows_of(client, PluginLog)) == 1, "同样的值再 PUT 一次不该再多一条流水"


def test_the_config_endpoint_can_release_a_plugin_back_to_global(client):
    """`PUT` 是整份替换：只说 `scope=global` 就是把作用域与自设上限都放下。

    页面上"改回跟随全局"这个按钮必须存在且有明确的语义——`queue_limit` 缺省是 NULL（跟随
    `plugin_queue_limit`），不是"保持原值"，否则用户没法把它清掉。
    """
    a = add_site(client, "alpha", "A场", 5)
    client.put("/api/plugins/alert-engine/config", json={"scope": "site", "scope_id": a["site"]})

    body = client.put("/api/plugins/alert-engine/config", json={"scope": "global"}).json()
    assert (body["scope"], body["scope_id"]) == ("global", None)
    assert (body["queue_limit"], body["effective_queue_limit"]) == (
        None,
        client.app.state.settings.plugin_queue_limit,
    )


def test_the_config_endpoint_refuses_every_kind_of_misaligned_scope(client):
    """四种配错各回一种 reason：配错要表现成"改不动并被说清"，而不是"改成了另一个意思"。

    - `relay`：schema 里是合法字符串，但批上没有位置身份，今天没有执行者（`EXECUTABLE_SCOPES`）。
    - 拿网关 id 当 site：`scope_id` 是多态指向、没有外键，只能按 `scope` 去对应的表里查这一行在不在。
    - `global` 还带 `scope_id`：这两列会互相矛盾，读侧按谁都不确定。
    - `queue_limit < 1`：0 等于"这支插件一条都看不见"，与 `Settings` 构造期同一条拒。
    """
    a = add_site(client, "alpha", "A场", 5)
    second = add_gateway(client, a["site"], "beta", 6)
    assert second["gateway"] != a["site"], "本例靠'网关 id 不是任何场地的 id'成立"
    with client.app.state.db.scope() as session:
        #: 旧库里已经躺着的那种行：`relay` 在 schema 里合法，写侧现在拒了，读侧得能指出它没有执行者。
        session.get(PluginRegistry, "anomaly-detector").scope = "relay"
        session.get(PluginRegistry, "anomaly-detector").scope_id = second["relay"]
    stale = plugin_view(client, "anomaly-detector")
    assert (stale["scope"], stale["scope_executable"]) == ("relay", False)

    for payload, reason in (
        ({"scope": "relay", "scope_id": 1}, "scope_unsupported"),
        ({"scope": "site", "scope_id": second["gateway"]}, "scope_target_missing"),
        ({"scope": "global", "scope_id": a["site"]}, "scope_id_not_allowed"),
        ({"scope": "global", "queue_limit": 0}, "invalid_queue_limit"),
    ):
        row = client.put("/api/plugins/alert-engine/config", json=payload)
        assert row.status_code == 422, (payload, row.text)
        detail = row.json()["detail"]
        assert detail["reason"] == reason, payload
        if reason == "scope_unsupported":
            assert detail["supported"] == list(EXECUTABLE_SCOPES)

    untouched = plugin_view(client, "alert-engine")
    assert (untouched["scope"], untouched["scope_id"], untouched["queue_limit"]) == (
        "global",
        None,
        None,
    )
    assert rows_of(client, PluginLog) == [], "四种配错都不该留下'已经改好'的痕迹"


def test_a_plugin_with_no_implementation_cannot_be_enabled(client):
    """启用一支代码里没有实现的插件回 409，而不是"启用了然后什么都不知道"。

    这一行是 `registry.sync()` 明说"永远不会跑"的那种（不做动态代码加载）。让 `enable` 成功而运行时
    默默跳过它，页面上就只剩一条永远不动的"已启用"——与插件市场的差别只剩一个字。停用不拒：那一行
    本来就是 1 的话，把它改成 0 是真能少一份内存的工作，运行时也不会为它搬任何东西。
    """
    plugin_id = add_row_without_implementation(client)

    row = client.post(f"/api/plugins/{plugin_id}/enable")
    assert row.status_code == 409, row.text
    assert row.json()["detail"] == {"reason": "no_implementation", "plugin_id": plugin_id}
    assert client.post(f"/api/plugins/{plugin_id}/disable").status_code == 200

    missing_state = client.get(f"/api/plugins/{plugin_id}/state")
    assert missing_state.status_code == 404
    assert missing_state.json()["detail"]["kind"] == "plugin_state"

    for call in (
        lambda: client.post("/api/plugins/no-such-plugin/enable"),
        lambda: client.get("/api/plugins/no-such-plugin/state"),
        lambda: client.get("/api/plugins/no-such-plugin/logs"),
        lambda: client.put("/api/plugins/no-such-plugin/config", json={"scope": "global"}),
    ):
        unknown = call()
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["detail"]["kind"] == "plugin"


def test_the_aggregator_instance_refuses_plugin_writes(db_path):
    """插件的启停与配置在汇聚实例上一律 405，读侧照开。

    与 `test_settings_isolation.py` 那条同一条纪律，只是资源换成了插件：汇聚实例的角色定义就是
    "看别人，不改任何东西"，而它存在的理由正是能把各场地的插件状态读出来看。分两处写是因为端点各自
    注册，漏一个就是"页面能点、点了被拒"。
    """
    writer = TestClient(create_app(Settings(db_path=db_path, dialer_enabled=False)))
    # 登记同步本来由 lifespan 做，而这里的 `TestClient` 不进上下文管理器（不起后台循环）。
    # 直接调一次 `sync` 而不是把整个 lifespan 拉起来：这一条要测的是写权限，不是启动顺序。
    with writer.app.state.db.scope() as session:
        registry.sync(session)
    assert writer.post("/api/plugins/alert-engine/enable").status_code == 200

    reader = TestClient(
        create_app(
            Settings(db_path=db_path, dialer_enabled=False, instance_role="aggregator")
        )
    )
    assert reader.get("/api/plugins").json()["instance_role"] == "aggregator"
    assert reader.get("/api/plugins/alert-engine/state").status_code == 200
    assert reader.get("/api/plugins/alert-engine/logs").status_code == 200
    for call in (
        lambda: reader.post("/api/plugins/alert-engine/enable"),
        lambda: reader.post("/api/plugins/alert-engine/disable"),
        lambda: reader.put("/api/plugins/alert-engine/config", json={"queue_limit": 4}),
    ):
        row = call()
        assert row.status_code == 405, row.text
        assert row.json()["detail"]["reason"] == "aggregator_read_only"

    written = {p["plugin_id"]: p for p in writer.get("/api/plugins").json()["plugins"]}
    assert written["alert-engine"]["enabled"] is True
    assert written["alert-engine"]["queue_limit"] is None, "拒了就是没写"


# --- S7-7：总开关、lifespan 起停与 `/api/health` 的 `plugins` 节 -----------------


def app_without_other_loops(db_path, **overrides):
    """只留插件循环的实例：拨号、保留清理、采集在这些用例里全是干扰项。

    三个开关在生产默认全开、`tests/conftest.py` 里默认全关，这里显式写出来是因为这一节测的
    正是**起停本身**——环境里多一条会在断言中途改库的循环，失败就说不清是谁干的。
    """
    return create_app(
        Settings(
            db_path=db_path,
            dialer_enabled=False,
            retention_purge_enabled=False,
            capture_enabled=False,
            **overrides,
        )
    )


def test_the_plugin_runtime_switch_defaults_on_and_reads_env():
    """`RFTRACK_PLUGINS_ENABLED=0` 得是合法部署形态，回滚不该要求人挨个停用插件。

    S7 的回滚判据是"关掉之后采集链路与插件上线前一模一样"，而现场能做的只有改环境变量重启；
    要它成立，开关必须能一个字节的注册表都不读地撤掉运输层。默认开：S7 交付的形态本来就是
    "注册表里有三行、一个都没启用"，开关只是那三层里最外面的一层。
    """
    assert Settings().plugins_enabled is True
    assert Settings.from_env({}).plugins_enabled is True, (
        "env 里没写就得是开的：默认改了等于把 S7 交付的形态撤掉"
    )
    assert Settings.from_env({"RFTRACK_PLUGINS_ENABLED": "0"}).plugins_enabled is False
    assert Settings.from_env({"RFTRACK_PLUGINS_ENABLED": "1"}).plugins_enabled is True


def test_a_switched_off_runtime_never_touches_the_registry(client):
    """开关关掉时 `start()` 直接返回：注册表里有启用中的行也不启动、不刷新、不接批。"""
    where = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, plugins_enabled=False)

    async def scenario():
        await runtime.start()

    asyncio.run(scenario())
    assert (runtime.running, runtime.role, runtime.refreshes) == (False, None, 0)
    offered = runtime.offer(
        one_batch(site_id=where["site"], gateway_id=where["gateway"], node_id=where["node"])
    )
    assert offered == "disabled"
    assert runtime.status()["enabled"] is False


def test_starting_the_app_runs_the_plugin_loop_and_health_reports_it(db_path):
    """进程起来之后，"启用一支插件"到"库里有一条判定"之间不该再有任何人手动点一次。

    这条是 S7 的"没人点也自己跑"验收：lifespan 里没挂上循环、或者挂了但节拍坏了，
    现象都只是"插件页说它在跑，判定一条也没落"——而那与"规则挑得不对"长得一模一样。
    """
    app = app_without_other_loops(db_path)
    #: 节拍压到 20 ms：这条测的是"循环真的在跑"，不是"1 s 这个数字对不对"（真值待 S9 压测）。
    app.state.plugins.tick_interval_s = 0.02
    with TestClient(app) as http:
        where = add_site(http, "alpha", "A场", 5)
        assert http.post("/api/plugins/alert-engine/enable").status_code == 200
        batch = one_batch(
            site_id=where["site"], gateway_id=where["gateway"], node_id=where["node"]
        )
        deadline = time.monotonic() + 5.0
        offered = "disabled"
        # 等的是"投喂被接下了"而不是 `refreshes>=1`：启用只改库里那一行，下一拍 `refresh()`
        # 才把它放进快照，在那之前 `offer()` 一律回 `disabled`。
        while time.monotonic() < deadline and offered != "queued":
            offered = app.state.plugins.offer(batch)
            time.sleep(0.01)
        assert offered == "queued", "5 s 内节拍没把启用中的插件放进快照：lifespan 没起循环"
        section: dict = {}
        while time.monotonic() < deadline:
            section = http.get("/api/health").json()["plugins"]
            if section["findings_written"] and rows_of(http, PluginFinding):
                break
            time.sleep(0.01)
        assert rows_of(http, PluginFinding), "批接下了却一条判定都没落：循环没有在消费"
        assert (section["enabled"], section["running"], section["role"]) == (True, True, "site")
        assert section["enabled_plugins"] == ["alert-engine"]
        assert section["refreshes"] >= 2, "只刷新了一次，说明循环压根没跑第二轮"
        assert section["problem"] is None
        assert http.get("/api/health").json()["status"] == "ok"
        state = state_of(http, "alert-engine")
        assert state.status == "running"
        assert state.produced >= 1
    assert app.state.plugins.running is False, "退出上下文后循环必须收摊"
    with app.state.db.scope() as session:
        assert session.get(PluginState, "alert-engine").status == "stopped", (
            "进程停了而库里还写着 running：下一轮排查只能靠猜"
        )


def test_a_switched_off_runtime_shows_an_honest_plugins_section(client):
    """没启用不等于没这一节：`/api/health` 要能回答"这台为什么一条判定也没有"。

    整份比对而不是挑几个键——这一节是运营者判断"插件页说在跑、库里为什么没行"的唯一现场，
    多一个没人读的键、少一个该说的数，都比多写两行断言贵。`status == "ok"` 同上一条测试的
    理由：把合法部署形态报成 degraded，等于教运营者忽略 degraded。
    """
    body = client.get("/api/health").json()
    assert body["plugins"] == {
        "enabled": False,
        "running": False,
        "role": None,
        "refused": None,
        "tick_interval_s": TICK_INTERVAL_S,
        "enabled_plugins": [],
        "failing_plugins": [],
        "drain_chunk": DRAIN_CHUNK,
        "config_ttl_s": CONFIG_TTL_S,
        "refreshes": 0,
        "batches_offered": 0,
        "dropped_batches": 0,
        "batches_refused": 0,
        "batches_out_of_scope": 0,
        "batches_lost": 0,
        "findings_written": 0,
        "errors": 0,
        "last_run_at": None,
        "last_error": None,
        "queues": [],
        "problem": None,
    }
    assert body["status"] == "ok"


def test_a_runtime_that_cannot_write_makes_health_degraded(client):
    """运行时真的坏了就必须出现在 `problems` 里——"插件在跑"这句话得有代价。

    直接改内存态而不是真把库锁住：这一条测的是**健康检查读不读这个结论**，失败本身由下面
    那条节拍用例与 `run_once` 的失败分组覆盖。
    """
    runtime = client.app.state.plugins
    runtime.errors = 3
    runtime.batches_lost = 12
    runtime.last_error = "OperationalError: database is locked"
    body = client.get("/api/health").json()
    assert body["status"] == "degraded"
    assert body["plugins"]["problem"] in body["problems"]
    assert any("locked" in p for p in body["problems"])


def test_a_failing_tick_is_counted_and_not_only_written_to_the_log(db_path):
    """节拍里抛出来的一轮要进 `errors`/`last_error`，否则上一条测试在生产上永远不成立。

    `_loop` 现在只 `log.warning` 一句：`refresh()` 读不到库（锁、盘满、缺表）时页面上只剩
    "插件不产出"，而真话只在 stdout 里。计上来的意义是健康检查能替运营者说出"运行时坏了"，
    并且下一轮跑成之后 `run_once` 自己把这句收回去。
    """
    app = app_without_other_loops(db_path)

    def boom(*_args):
        raise RuntimeError("库读不动")

    app.state.plugins.tick_interval_s = 0.02
    app.state.plugins.run_once = boom
    with TestClient(app):
        deadline = time.monotonic() + 5.0
        status: dict = {}
        while time.monotonic() < deadline:
            status = app.state.plugins.status()
            if status["errors"]:
                break
            time.sleep(0.01)
        assert status["errors"] >= 1, "节拍里抛出来的异常只进了日志，健康检查看不见"
        assert "RuntimeError: 库读不动" in status["last_error"]
        assert status["problem"] is not None, "坏了就得在 problem 里说话"
        assert status["running"] is True, "一轮失败不许把循环自己带走"


def test_missing_plugin_tables_skip_the_loop_instead_of_crashing_startup(db_path):
    """库里没有那四张表时循环整段不起："先去跑 migrate.py" 由健康检查说，不是由启动崩溃说。

    `registry.sync` 外面那层 `has_table` 守的是写，而运行时 `start()` 在起循环**之前**就要
    `refresh()` 读一次——那一读撞的是 `no such table`，炸掉的是 lifespan。版本号写着 head 而表
    不在（拷过来/Stamp 过的库）正是 S4-9 立"版本号与结构是两件事"的场景，所以这里按结构事实判。
    连表都没有的空库由 `test_health_does_not_crash_on_an_unmigrated_database` 覆盖，它走默认
    `plugins_enabled=True`，同一条守卫。
    """
    with sqlite3.connect(db_path) as conn:
        for table in ("plugin_log", "plugin_finding", "plugin_state", "plugin_registry"):
            conn.execute(f"DROP TABLE {table}")
    app = app_without_other_loops(db_path)
    with TestClient(app) as http:
        body = http.get("/api/health").json()
    assert body["status"] == "degraded"
    assert "plugin_registry" in body["schema_gaps"], body["schema_gaps"]
    section = body["plugins"]
    assert (section["enabled"], section["running"]) == (True, False)
    assert section["enabled_plugins"] == []
    assert section["problem"] is None, "缺表是「没迁移」，不是运行时坏了"


def test_the_plugin_loop_is_started_last_and_stopped_first(db_path):
    """起停的**顺序**也要有断言：少一行 `await`、或者把它挪到采集之后，前面每条用例都照样绿。

    这条只记录 lifespan 按什么顺序 `await` 了谁（把方法包一层，不改变行为），因为它要钉的就是
    那句话：运行时消费的是采集层那侧的产物，所以它最后起、最先停——停晚了，它那一拍会正撞上
    采集最后那次刷与保留清理的写。
    """
    app = app_without_other_loops(db_path)
    order: list[str] = []

    def trace(target, label: str, kind: str) -> None:
        original = getattr(target, kind)

        async def recorded():
            order.append(f"{kind}:{label}")
            await original()

        setattr(target, kind, recorded)

    for name in ("capture", "replay", "plugins"):
        surface = getattr(app.state, name)
        trace(surface, name, "start")
        trace(surface, name, "stop")
    with TestClient(app):
        pass
    assert order == [
        "start:capture",
        "start:replay",
        "start:plugins",
        "stop:plugins",
        "stop:replay",
        "stop:capture",
    ], f"起停顺序不是「最后起的先停」：{order}"


# --- S7-8：规范 §10.4 的"切布局/切场地不停任务"与"配额不互吃" --------------------


def queue_of(runtime, gateway_id: int, plugin_id: str = "alert-engine") -> dict:
    """从运行时的对外快照里取某 (插件, 网关) 那条队列的统计。

    读 `status()` 而不是 `run.queues[...]`：健康检查与插件页在另一个线程里看到的就是这份，而
    S7-8 要钉的三件事（队列没被换掉、别的场地没被牵连、配额没被互吃）说的都是"页面看得见的
    数字"——绕过对外口径去读内部对象，测试绿了页面照样可以是错的。
    """
    for row in runtime.status()["queues"]:
        if row["plugin_id"] == plugin_id and row["gateway_id"] == gateway_id:
            return row
    raise AssertionError(f"没有 ({plugin_id}, gateway {gateway_id}) 这条队列")


def test_activating_another_layout_leaves_the_running_plugin_exactly_as_it_was(client):
    """任务书 §10.4 那句"切换布局不得中断正在运行的任务"——插件这一半落在这里。

    判据不是"切完布局队列还在"（`refresh()` 每轮都照样把它建出来），而是**抑制记忆还在**：
    `alert-engine` 只在"距上次越限超过 `alert_hold_s`"时长新候选，所以同一个 `(节点, 格子)` 的
    三帧跨过两次切布局只该有**一条**判定。换了实例就变成两条——那是页面上最难看出来的一种坏：
    队列、计数、状态、流水全都绿，只有判定悄悄多了一条。
    """
    a = add_site(client, "alpha", "A场", 5)
    enable_plugin(client, "alert-engine", queue_limit=8)
    runtime = runtime_for(client)
    runtime.refresh()

    for i in range(2):
        assert runtime.offer(
            one_batch(
                site_id=a["site"],
                gateway_id=a["gateway"],
                node_id=a["node"],
                seq=i + 1,
                ts=moment(i * 100),
            )
        ) == "queued"
    runtime.run_once()

    first = client.post("/api/layouts", json={"site_id": a["site"], "name": "比赛日"}).json()
    client.post(f"/api/layouts/{first['layout_id']}/activate")
    second = client.post("/api/layouts", json={"site_id": a["site"], "name": "训练日"}).json()
    client.post(f"/api/layouts/{second['layout_id']}/activate")

    assert runtime.offer(
        one_batch(
            site_id=a["site"],
            gateway_id=a["gateway"],
            node_id=a["node"],
            seq=3,
            ts=moment(200),
        )
    ) == "queued"
    runtime.run_once()

    queue = queue_of(runtime, a["gateway"])
    # `peak` 只到 2 而 `received` 到 3：这是**同一条**队列（新建的那条两个数都是 1）。
    assert (queue["peak"], queue["received"], queue["limit"]) == (2, 3, 8), queue
    assert runtime.status()["enabled_plugins"] == ["alert-engine"]
    assert len(rows_of(client, PluginFinding)) == 1, (
        "切布局把插件实例换掉了：抑制记忆没了，同一件事被报了两遍"
    )
    assert state_of(client, "alert-engine").processed == 3
    assert rows_of(client, PluginLog) == [], "切布局不该在任何地方留下『我重启过』的痕迹"


def test_switching_the_workbench_site_does_not_stop_the_other_sites_plugins(client):
    """方案 §7 S7 行补的那条：**切场地也不停任务**。

    这里做的是"换到 B 场地"真的会做的那几件事（读 B 的生效配置、把本实例的当前场地写成 B、
    读 B 的节点、激活 B 的布局），而不是只发一个 `?site=B` 的 GET——因为唯一可能咬人的就是
    那几次写：`current_site_id` 一改，读侧的缺省场地、布局的绑定、配置层的落点全跟着换。运行时
    必须对它毫无感觉：插件看的是批自己带的作用域，不是"页面上正在显示哪个场地"。

    判据同上一条，还是抑制记忆：A 的第三帧（200 ms 后）仍然只有一条判定，而 B 的第一帧另开一条
    ——两个场地的队列在同一份快照里并存。
    """
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    enable_plugin(client, "alert-engine", queue_limit=8)
    runtime = runtime_for(client)
    runtime.refresh()

    for i in range(2):
        runtime.offer(
            one_batch(
                site_id=a["site"],
                gateway_id=a["gateway"],
                node_id=a["node"],
                seq=i + 1,
                ts=moment(i * 100),
            )
        )
    runtime.run_once()

    version = client.get("/api/settings/global").json()["version"]
    assert client.get(f"/api/settings/site/{b['site']}").status_code == 200
    switched = client.put(
        "/api/settings/global",
        json={
            "version": version,
            "settings": {"current_site_id": b["site"]},
            "confirm": ["current_site_id"],
        },
    )
    assert switched.status_code == 200, switched.text
    assert client.get("/api/nodes", params={"site": b["site"]}).status_code == 200
    layout = client.post("/api/layouts", json={"site_id": b["site"], "name": "B场主屏"}).json()
    client.post(f"/api/layouts/{layout['layout_id']}/activate")

    assert runtime.offer(
        one_batch(
            site_id=a["site"],
            gateway_id=a["gateway"],
            node_id=a["node"],
            seq=3,
            ts=moment(200),
        )
    ) == "queued"
    assert runtime.offer(
        one_batch(
            site_id=b["site"],
            gateway_id=b["gateway"],
            node_id=b["node"],
            seq=1,
            ts=moment(300),
        )
    ) == "queued"
    runtime.run_once()

    assert (queue_of(runtime, a["gateway"])["received"], queue_of(runtime, a["gateway"])["dropped"]) == (3, 0)
    assert queue_of(runtime, b["gateway"])["received"] == 1
    assert runtime.status()["enabled_plugins"] == ["alert-engine"]
    rows = rows_of(client, PluginFinding)
    assert {row.site_id for row in rows} == {a["site"], b["site"]}, [
        (row.site_id, row.value, row.ts) for row in rows
    ]
    assert len(rows) == 2, "A 的第三帧被算成第二条判定，等于切场地把实例换了"
    assert rows_of(client, PluginLog) == []


def test_a_saturated_site_a_queue_never_borrows_site_b_capacity(client):
    """方案 §7 S7 行的另一句：**A 场地队列打满不得吃掉 B 场地的配额**。

    这条防的是一种很具体的实现走形：把队列做成"每插件一条全局队列"，容量自然是一个共享数字，
    A 场地一闹就把 B 的格子挤掉——页面上看到的是"B 场地的判定少了"，而 B 场地并没有任何异常。
    今天的形状是每 `(插件, 网关)` 一条，`max_gateways` 管的是**条数**而不是总深度，所以这里的
    判据必须是两侧的计数彼此无关：A 侧 `dropped=4`、B 侧 `dropped=0` 且照样进得来。
    """
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    enable_plugin(client, "alert-engine", queue_limit=2)
    runtime = runtime_for(client)
    runtime.refresh()

    outcomes = [
        runtime.offer(
            one_batch(
                site_id=a["site"],
                gateway_id=a["gateway"],
                node_id=a["node"],
                seq=i + 1,
                ts=moment(i * 100),
            )
        )
        for i in range(6)
    ]
    assert outcomes == ["queued", "queued"] + ["dropped_oldest"] * 4
    assert runtime.status()["dropped_batches"] == 4

    assert runtime.offer(
        one_batch(
            site_id=b["site"],
            gateway_id=b["gateway"],
            node_id=b["node"],
            seq=1,
            ts=moment(600),
        )
    ) == "queued", "B 场地的批被 A 的满载挤掉了"

    assert queue_of(runtime, a["gateway"]) == {
        "gateway_id": a["gateway"],
        "plugin_id": "alert-engine",
        "limit": 2,
        "depth": 2,
        "dropped": 4,
        "peak": 2,
        "received": 6,
    }
    assert queue_of(runtime, b["gateway"]) == {
        "gateway_id": b["gateway"],
        "plugin_id": "alert-engine",
        "limit": 2,
        "depth": 1,
        "dropped": 0,
        "peak": 1,
        "received": 1,
    }

    runtime.run_once()
    rows = rows_of(client, PluginFinding)
    assert {row.site_id for row in rows} == {a["site"], b["site"]}, (
        "满载的那侧把另一侧的判定也一起吞了"
    )
    assert runtime.status()["batches_out_of_scope"] == 0


# --- S7-10：队列条数硬闸说"我拒了"，就得让人看得见的拒了几次 ---------------------


def test_the_queue_count_gate_refuses_the_extra_gateway_and_counts_it(client):
    """`queue_for()` 那条 `len(queues) >= max_gateways` 的闸此前只有**后果**没有**账**。

    任务书 §10.4 要的是"队列有界"并且**背压看得见**。同一条链上另外两种失去数据都有计数
    （`dropped_batches`、`batches_lost`），只有"这台网关压根没分到队列"落在 `_Run` 里自增、
    而 `status()` 不往外说——于是页面上看得见的是"两批进来、一批处理了、一批哪儿都不在"，
    而那正是最容易被读成"插件在偷懒"的一种缺数。判据分三层：投递侧回的那个串、对外快照里的
    数、以及被拒那批**确实没变成判定**。
    """
    a = add_site(client, "alpha", "A场", 5)
    b = add_site(client, "beta", "B场", 6)
    enable_plugin(client, "alert-engine")
    runtime = runtime_for(client, max_gateways=1)
    runtime.refresh()

    assert runtime.offer(
        one_batch(site_id=a["site"], gateway_id=a["gateway"], node_id=a["node"], seq=1, ts=moment(0))
    ) == "queued"
    assert runtime.offer(
        one_batch(site_id=b["site"], gateway_id=b["gateway"], node_id=b["node"], seq=1, ts=moment(0))
    ) == "refused"

    status = runtime.status()
    assert status["batches_refused"] == 1, status
    assert [row["gateway_id"] for row in status["queues"]] == [a["gateway"]], (
        "被拒的那台还是分到了队列，闸根本没关上"
    )
    assert status["batches_offered"] == 2, "被拒不等于没交过来：投递侧的账要连着算"

    runtime.run_once()
    assert state_of(client, "alert-engine").processed == 1, (
        "被拒的那批不该悄悄被处理掉"
    )
    assert {row.gateway_id for row in rows_of(client, PluginFinding)} == {a["gateway"]}, (
        "被拒那台网关的判定落了库，等于硬闸只拦了一半"
    )
