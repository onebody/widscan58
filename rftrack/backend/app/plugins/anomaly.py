"""`anomaly-detector`：某个信道格子相对**它自己的安静基线**抬高了。

规则只有一句：`(节点, 格子)` 这个键攒够 `BASELINE_WINDOW` 个读数后，取它们的中位数当基线，
当前读数比基线高出 `anomaly_rise_db` 以上就算一次抬升——并且**只在上升沿**报一次。

每个选择都有对应的"否则会怎样"：

- **相对基线而不是绝对门限**：绝对越限是 `alert-engine` 的活（它看生效的 `threshold_db`）。
  两支插件如果都判绝对值，场地里就会同一条读数长两条几乎一样的判定，而运营者无法分辨哪条
  该看——重复判定与漏判定一样有害。
- **中位数而不是均值**：一帧越界的毛刺会把均值整个拽偏，之后每一帧都"正常"；中位数对
  单点毛刺不敏感。窗口里外都算的话就不是"相对"了，所以基线只看**当前这帧之前**的读数。
- **上升沿触发**：跳频下每块板每秒一帧，持续抬高若每帧都报，判定表就是新的写放大源
  （R11 说的正是 SD 卡只有那一张），而"同一次抬高"本来就该是一条。回落到阈值以下才重新上膛，
  所以真正的第二次事件仍然会说第二次。
- **键里没有场地/接入机**：`node_id` 是全局主键，场地与接入机都由它唯一决定；把它们抄进键里
  只是把同一个事实存三遍，还会在节点被删除重建后留下两份互相矛盾的旧状态。

`BASELINE_WINDOW`（窗口宽度）与 `capacity`（最多跟踪多少个格子）是**算法常数**而不是配置键：
改窗口宽度改的是这条规则本身的含义，不是运营者的偏好；阈值那类才进配置层。
"""

from __future__ import annotations

import statistics
from collections import OrderedDict, deque
from typing import Any, Mapping

from .base import Finding, FrameBatch

#: 一个格子要攒够多少个读数才开始说话。16 帧在跳频档位下是十几秒：再短就把"刚插上网口"
#: 的那几帧当基线了，再长则一次真实的抬高要等太久才被看见。
BASELINE_WINDOW = 16
#: 最多跟踪多少个 `(节点, 格子)`。8 频道 × 8 接入机 × 8 节点 = 512 是这台机器上"能同时存在
#: 的格子数"，超出即视为节点已被替换；条数与丢弃数都从 `stats()` 报出去。
DEFAULT_CAPACITY = 512

KIND = "slot_rise"


class _SlotState:
    __slots__ = ("readings", "elevated")

    def __init__(self, window: int) -> None:
        self.readings: deque[float] = deque(maxlen=window)
        #: 上一帧是否已经处于"抬高"里。上升沿由它判，不是由一个全局计数器判。
        self.elevated = False


class AnomalyDetector:
    """`anomaly-detector` 的实现。只吃 `FrameBatch`，只回 `Finding`，不碰库。"""

    #: 运行时按这份清单去配置层取该场地的生效值：清单外的键不许读，缺键就是运行期 `KeyError`。
    CONFIG_KEYS = ("anomaly_rise_db",)

    def __init__(self, capacity: int = DEFAULT_CAPACITY, window: int = BASELINE_WINDOW) -> None:
        self._window = window
        self._capacity = capacity
        self._states: OrderedDict[tuple[int, int], _SlotState] = OrderedDict()
        self._evicted = 0

    def feed(self, batch: FrameBatch, config: Mapping[str, Any]) -> list[Finding]:
        rise_needed = config["anomaly_rise_db"]
        findings: list[Finding] = []
        for point in batch.points:
            key = (batch.node_id, point.slot)
            state = self._states.get(key)
            if state is None:
                state = self._states[key] = _SlotState(self._window)
                self._evict_overflow()
            else:
                self._states.move_to_end(key)

            elevated = False
            if len(state.readings) >= self._window:
                baseline = statistics.median(state.readings)
                rise = point.rssi - baseline
                elevated = rise >= rise_needed
                if elevated and not state.elevated:
                    findings.append(
                        Finding(
                            kind=KIND,
                            slot=point.slot,
                            metric="rssi",
                            value=point.rssi,
                            threshold=rise_needed,
                            evidence={
                                "baseline_rssi": baseline,
                                "rise_db": rise,
                                "window": self._window,
                            },
                        )
                    )
            state.elevated = elevated
            state.readings.append(point.rssi)
        return findings

    def stats(self) -> dict[str, int]:
        return {"tracked": len(self._states), "evicted": self._evicted}

    def _evict_overflow(self) -> None:
        """超容量丢**最久没被喂到**的那个格子：一块板被拔掉重插后，旧序列比新序列更该让位。"""
        while len(self._states) > self._capacity:
            self._states.popitem(last=False)
            self._evicted += 1
