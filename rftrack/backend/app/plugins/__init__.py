"""S7 的内置规则插件：形状在 `base.py`，规则各占一个模块，运行时在 `services/`。

这个包**不做**任何"发现插件"的事：没有入口点、没有目录扫描、没有网络加载（方案 §11 明写不做
插件市场与动态代码加载）。哪三支插件存在，登记侧的事实源是 `app/services/registry.py` 的
`BUILTIN_PLUGINS`；本包的 `IMPLEMENTATIONS` 是实现侧的映射，两边的键集合必须相等——这条由
`tests/test_plugin_rules.py` 钉住，因为"库里有一行但代码里没有实现"是唯一能让两边错开的方式，
而那正是要在测试阶段就拦住、而不是等运行时报错的事。

映射由本包自己拼（而不是让 registry 反向 import 插件模块）：登记是数据库里的那几行，
它不该依赖任何一支插件的导入能不能成功。
"""

from __future__ import annotations

from .alerting import AlertEngine
from .anomaly import AnomalyDetector
from .base import Finding, FrameBatch, RulePlugin, SlotPoint
from .interference import InterferenceCorrelator

#: `plugin_id` -> 实现类。运行时按 `BUILTIN_PLUGINS` 的顺序实例化；键集合相等由测试保证。
#: id 写在这里而不是让类自带一份：同一件事存两遍，迟早会有一遍是过期的。
IMPLEMENTATIONS: dict[str, type[RulePlugin]] = {
    "anomaly-detector": AnomalyDetector,
    "interference-correlator": InterferenceCorrelator,
    "alert-engine": AlertEngine,
}

__all__ = [
    "IMPLEMENTATIONS",
    "Finding",
    "FrameBatch",
    "RulePlugin",
    "SlotPoint",
]
