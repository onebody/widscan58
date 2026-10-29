"""插件的输入与输出形状：采集层交进来一批**已归属**的点，插件回一串**还没写库**的判定。

这一层刻意只有形状、没有规则，也没有数据库：规则在 `anomaly.py` / `interference.py` /
`alerting.py`，写库与队列在运行时（S7）。把它们切开的具体理由有两条，都直接来自任务书：

1. **身份不许由插件提供**。`site_id`/`gateway_id`/`node_id`/`source`/`ts` 都在 `FrameBatch` 上，
   写库时由运行时原样盖到 `plugin_finding` 那一行——插件只能对"我看到的这批点"说话，
   不能宣布"这是 A 场地的实时读数"。冒充来路是这套系统里最贵的一类错，形状上堵住比审计便宜。
2. **判定与观测分家**。`Finding` 落的是 `plugin_finding`（推导），不是 `signal_event`（观测）：
   页面上"这块板安静了"和"这条像是干扰"必须是两回事，S8 的人工复核才分得开。

`RulePlugin` 用 `Protocol` 而不是抽象基类：三支插件都是普通类，测试里注入一支崩的插件
也不需要继承任何东西（S7 的运行时测试正是这么干的）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Mapping, Protocol


@dataclass(frozen=True)
class SlotPoint:
    """一个信道格子里的一次读数。

    `slot` 是 **0 基**下标（`0..slots_per_node-1`），与 `channel_plan.plan_json` 的档位下标、
    `routers/samples.py` 的闸、固件 `planner.c` 填的 plan 同一条口径——显示层的 `CH1..CH8`
    是 1 基，那是渲染的事。
    """

    slot: int
    rssi: float
    ema: float


@dataclass(frozen=True)
class FrameBatch:
    """一块板一帧的归属结果：身份 + 时间 + 这一帧的各格子读数。

    `source` 只有 `real`/`mock` 两个观测量取值（回放进来的一律 `mock`，见 `replay.py`）；
    `ts` 是**观测时刻**而不是写库时刻——回放一批历史数据时这两个能差一小时，判定的时间轴
    必须落在观测那一侧。
    """

    site_id: int
    gateway_id: int
    node_id: int
    source: str
    ts: datetime
    seq: int
    points: tuple[SlotPoint, ...]


@dataclass(frozen=True)
class Finding:
    """一支插件的一条判定（尚未写库）。

    `threshold` 存的是**当时生效的那条阈值**（生效值，不是内置默认），否则"为什么这条会响"
    就只能靠猜。`evidence` 与 `signal_event.payload_json` 同一条纪律：不许只留一句人话结论；
    同时它**禁止**出现距离/方向/坐标/置信度这类字段——RSSI 推不出这些东西（任务书 §10.5）。
    """

    kind: str
    slot: int | None = None
    metric: str | None = None
    value: float | None = None
    threshold: float | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)


class RulePlugin(Protocol):
    """一支规则插件的最小面：吃一帧、回若干条判定、说得出自己攒了多少状态。"""

    #: 这支插件要从配置层读哪些键。运行时按它取生效值，`tests/test_plugin_rules.py` 按它
    #: 检查"每个键都注册过、都有平台默认值"——清单外的键不许读，缺键就是运行期 `KeyError`。
    CONFIG_KEYS: ClassVar[tuple[str, ...]]

    def feed(self, batch: FrameBatch, config: Mapping[str, Any]) -> list[Finding]:
        """`config` 是这批数据所属场地的**生效值**（已由运行时从配置层取好）。

        阈值一律从 `config` 读，插件里不许写字面量兜底：配错阈值应该表现为"它不说话了"，
        而不是"它悄悄按代码里那个数说话了"。
        """
        ...

    def stats(self) -> dict[str, int]:
        """状态条数与被丢弃的条数。有界存储必须说得出丢了多少，见各插件的 `capacity`。"""
        ...
