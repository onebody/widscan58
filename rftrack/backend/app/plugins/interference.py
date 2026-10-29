"""`interference-correlator`：同一场地里**不止一块板**在同一个时间窗内越过了生效阈值。

规则只有一句：把每场地"越过 `threshold_db` 的读数"按观测时刻留在窗口里，窗口内出现
`interference_min_nodes` 个**不同节点**就算一次空间相关——同样只在成立沿报一次。

为什么"不同节点"而不是"不同读数"：一块板八个格子同时抬高说明的是**这块板**看到了强信号
（可能是它自己的天线/位置问题），把它当成"两处干扰"是把一个来源数成两个。去重之后剩下的
才是"多处同时"这一件事。

三条刻意的边界：

- **不猜位置**。证据里只有节点号、各自窗口内的最高读数与格子号——RSSI 推不出距离、方向、
  坐标（任务书 §10.5），而"多个节点同时抬高"本身已经是这套硬件能支持的最强表述。
- **时间轴用观测时刻**（`batch.ts`）而不是 `monotonic()`/墙钟：回放一批历史数据时，"同时"
  必须按录制里的那个"同时"算，否则一段 30 s 的录制会被压成"全都同时"，判定就成了噪声。
- **不看别的插件的判定**。这一版从帧自己维护窗口，因此三支插件互不依赖、可以任意单独启用；
  代价是"越限"这条判据在 `alert-engine` 里还有一份（同一条 `threshold_db`）。等真需要
  "判定引用判定"的证据链时再加那一层，不在这里预建（方案 §11 的 YAGNI 口径）。

窗口宽度与每场地的条数上界是算法/容量常数：改它们改的是这条规则的含义与内存占用，
不是运营者的偏好；阈值与节点数才进配置层。
"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Mapping

from .base import Finding, FrameBatch

KIND = "multi_node_interference"
#: 一个场地在窗里最多留多少条越限读数。5 s 窗口 × 64 块板 × 8 格是"理论满打"的量级，
#: 1024 条足够表达"多节点同时"（去重看的是节点数，不是条数），打满就丢最老的并计数。
DEFAULT_CAPACITY = 1024
#: 同时跟踪多少个场地的窗口。本实例服务一个场地，汇聚/多场地部署才是几位数——16 已经够。
MAX_SITES = 16


@dataclass(frozen=True)
class _Crossing:
    ts: datetime
    node_id: int
    slot: int
    rssi: float


class _SiteWindow:
    """一个场地的在窗越限读数 + "条件是否已经报过"。"""

    def __init__(self, capacity: int) -> None:
        self.readings: deque[_Crossing] = deque(maxlen=capacity)
        self.fired = False
        self.dropped = 0

    def offer(self, crossing: _Crossing) -> None:
        if len(self.readings) == self.readings.maxlen:
            self.dropped += 1
        self.readings.append(crossing)

    def prune(self, cutoff: datetime) -> None:
        while self.readings and self.readings[0].ts < cutoff:
            self.readings.popleft()

    def loudest(self) -> dict[int, _Crossing]:
        """每个节点在本窗口里的最高那条读数——证据按节点说话，不按读数条数说话。"""
        best: dict[int, _Crossing] = {}
        for crossing in self.readings:
            current = best.get(crossing.node_id)
            if current is None or crossing.rssi > current.rssi:
                best[crossing.node_id] = crossing
        return best


class InterferenceCorrelator:
    """`interference-correlator` 的实现。状态按场地分，跨场地永不互相凑数。"""

    #: `threshold_db` 与 `alert-engine` 共用同一条生效阈值（同一个事实只存一份），
    #: 但两支插件由它推出的结论不同：一个说"这格越限了"，一个说"多处同时越限"。
    CONFIG_KEYS = ("threshold_db", "interference_min_nodes", "interference_window_s")

    def __init__(self, capacity: int = DEFAULT_CAPACITY, max_sites: int = MAX_SITES) -> None:
        self._capacity = capacity
        self._max_sites = max_sites
        self._sites: OrderedDict[int, _SiteWindow] = OrderedDict()
        self._site_evictions = 0

    def feed(self, batch: FrameBatch, config: Mapping[str, Any]) -> list[Finding]:
        threshold = config["threshold_db"]
        min_nodes = config["interference_min_nodes"]
        window_s = config["interference_window_s"]

        window = self._window_for(batch.site_id)
        for point in batch.points:
            if point.rssi > threshold:
                window.offer(_Crossing(batch.ts, batch.node_id, point.slot, point.rssi))
        window.prune(batch.ts - timedelta(seconds=window_s))

        loudest = window.loudest()
        if len(loudest) < min_nodes:
            window.fired = False
            return []
        if window.fired:
            return []
        window.fired = True
        return [
            Finding(
                kind=KIND,
                slot=None,
                metric="rssi",
                value=max(crossing.rssi for crossing in loudest.values()),
                threshold=threshold,
                evidence={
                    "nodes": [
                        {
                            "node_id": node_id,
                            "max_rssi": crossing.rssi,
                            "slot": crossing.slot,
                            "at": crossing.ts.isoformat(),
                        }
                        for node_id, crossing in sorted(loudest.items())
                    ],
                    "node_count": len(loudest),
                    "min_nodes": min_nodes,
                    "window_s": window_s,
                },
            )
        ]

    def stats(self) -> dict[str, int]:
        return {
            "sites": len(self._sites),
            "readings": sum(len(w.readings) for w in self._sites.values()),
            "dropped": sum(w.dropped for w in self._sites.values()),
            "site_evictions": self._site_evictions,
        }

    def _window_for(self, site_id: int) -> _SiteWindow:
        window = self._sites.get(site_id)
        if window is not None:
            self._sites.move_to_end(site_id)
            return window
        window = self._sites[site_id] = _SiteWindow(self._capacity)
        while len(self._sites) > self._max_sites:
            self._sites.popitem(last=False)
            self._site_evictions += 1
        return window
