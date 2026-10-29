"""S7 的内置插件注册表：代码里那几支插件与库里那几行之间的同步。

**没有动态代码加载**（方案 §11 明写不做插件市场）：`BUILTIN_PLUGINS` 是写死在源码里的一张表，
页面上没有任何入口能往里加东西。所以"库里多出来的一行"（旧备份带回的、或照着文档手写的）
不会被顺手兼容掉——它只会被记一条 warning，然后永远不跑。这句话必须留在库里可查，否则用户
看到的就只有"我加的插件怎么不见了"。

`sync()` 的三条纪律，每条都对着一个真会坏掉的场景：

1. 库里缺的行按代码那一份插进来，**`enabled` 一律 0**。S7 整段的回滚判据是"全部停用即等效
   不存在"，它成立的前提是新装实例里没有任何东西在跑。
2. 已有的行只覆写 `name` / `version` / `summary`（"页面上写着它叫什么、第几版"必须可追责，
   因而不是可编辑的）；`enabled` / `scope` / `scope_id` / `queue_limit` / `config_json` 一概
   不动。"重启一次就把用户配置吃掉"是这类同步函数最经典的错法，而"同步"这个词听起来恰好
   容易顺手做它。
3. 每支**有实现**的插件配一条 `plugin_state`（`status='stopped'`）；没有实现的行不配——它没有
   可运行的东西，给它一个状态等于假装它能跑。

`BUILTIN_PLUGINS` 与真实实现是否一一对应由 S7 的运行时测试钉住（那支测试遍历这张表去实现映射里
取类）；这里只做登记、不 import 插件模块，免得注册表反过来依赖它的消费者。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import PluginLog, PluginRegistry, PluginState, utcnow


@dataclass(frozen=True)
class BuiltinPlugin:
    """一支内置插件在库里该长什么样。"""

    plugin_id: str
    name: str
    version: str
    summary: str


#: 代码里真实存在的插件。**元组顺序即页面顺序**（`routers/plugins.py` 按它序列化，不是按
#: `plugin_id` 排序），新增一支就 append 在末尾，别插中间改别人的位置。阈值等可调参数不在
#: 这里——那属于配置层：`config_layer` 的键注册表是唯一事实源。
BUILTIN_PLUGINS: tuple[BuiltinPlugin, ...] = (
    BuiltinPlugin(
        plugin_id="anomaly-detector",
        name="异常检测",
        version="0.1.0",
        summary="按网关与信道格子看 RSSI 的离群与长时间静默，产出判定不产出事件。",
    ),
    BuiltinPlugin(
        plugin_id="interference-correlator",
        name="干扰关联",
        version="0.1.0",
        summary="把同一场地内多个节点同时抬升的格子关联起来，证据带上每个节点的读数。",
    ),
    BuiltinPlugin(
        plugin_id="alert-engine",
        name="告警候选",
        version="0.1.0",
        summary="按生效阈值把判定收成告警候选；是否成为告警由 S8 的规则与人工复核决定。",
    ),
)

#: `plugin_id` -> 声明。`sync()` 用它判"这一行有没有对应实现"，运行时用它取实现类。
BY_ID: dict[str, BuiltinPlugin] = {item.plugin_id: item for item in BUILTIN_PLUGINS}


def sync(session: Session) -> None:
    """把库里的 `plugin_registry` 对齐到 `BUILTIN_PLUGINS`。幂等，可反复调用。

    写的是调用方的 session：启动时由 `main.py` 在一个 `db.scope()` 里调它，测试可以直接传
    session 进来对拍。返回值刻意是 `None`——"同步了几行"不是要报给谁的事实，库里现在有哪几行
    本身就是事实，读库比读返回值可信。
    """
    rows = {
        row.plugin_id: row
        for row in session.execute(select(PluginRegistry)).scalars().all()
    }

    for item in BUILTIN_PLUGINS:
        row = rows.get(item.plugin_id)
        if row is None:
            session.add(
                PluginRegistry(
                    plugin_id=item.plugin_id,
                    name=item.name,
                    version=item.version,
                    summary=item.summary,
                    enabled=False,
                )
            )
        elif (row.name, row.version, row.summary) != (item.name, item.version, item.summary):
            row.name, row.version, row.summary = item.name, item.version, item.summary
            row.updated_at = utcnow()
        if session.get(PluginState, item.plugin_id) is None:
            session.add(PluginState(plugin_id=item.plugin_id, status="stopped"))

    for plugin_id, row in rows.items():
        if plugin_id in BY_ID:
            continue
        session.add(
            PluginLog(
                plugin_id=row.plugin_id,
                level="warning",
                event="missing_implementation",
                message="库里登记的这一行在代码里没有对应实现，不会启动（本实例不做动态代码加载）。",
            )
        )
