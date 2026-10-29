"""`alert-engine`：某个格子的读数**绝对越过**生效阈值，收成一条告警候选。

规则只有一句：`rssi > threshold_db` 就是候选，同一 `(节点, 格子)` 在 `alert_hold_s` 内不再重复产出。

- **绝对越限而不是相对离群**：相对基线是 `anomaly-detector` 的活。两者读的键不同
  （`threshold_db` vs `anomaly_rise_db`），说的也不是同一件事——"比它自己平时高"与
  "高过这条线"在页面上必须能分开看，否则运营者没法判断该不该管。
- **产出的是候选而不是告警**：`S8` 的规则表（`alert_rule`）与人工复核决定"这条要不要报给谁、
  算几级、什么时候算平息"。这一版只把"越限"这个事实变成一条可追责的判定，不改 `signal_event`，
  也不假装已经通知过谁。
- **抑制窗按"最近一次越限"起算**：持续压着阈值时不再每 30 s 长一条新候选（那等于把判定表
  写成第二个摘要表，R11 说的正是 SD 卡只有那一张）；停下来超过窗、再越限，才是新的一条。
- **证据带 `ema`**：`rssi` 闪一下而 `ema` 没跟上，与"两者一起压着"是两种完全不同的现场，
  只看 `rssi` 就分辨不出来。`seq` 留作回查那一帧的锚点。

抑制状态的最大条数是容量常数（跳频下每块板 8 格，量级由接入机数决定），不是运营者偏好；
阈值与抑制窗才是配置键。
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Mapping

from .base import Finding, FrameBatch

KIND = "threshold_crossing"
#: 最多记住多少个 `(节点, 格子)` 的上一次越限时刻。8 接入机 × 8 节点 × 8 格 = 512，
#: 1024 留一倍余量；超出即视为节点已被替换，丢最久没喂到的那条并计数。
DEFAULT_CAPACITY = 1024


class AlertEngine:
    """`alert-engine` 的实现。只有"上次越限在什么时候"这一份状态，不存读数。"""

    CONFIG_KEYS = ("threshold_db", "alert_hold_s")

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        self._capacity = capacity
        self._last: OrderedDict[tuple[int, int], datetime] = OrderedDict()
        self._evicted = 0

    def feed(self, batch: FrameBatch, config: Mapping[str, Any]) -> list[Finding]:
        threshold = config["threshold_db"]
        hold = timedelta(seconds=config["alert_hold_s"])
        findings: list[Finding] = []

        for point in batch.points:
            if point.rssi <= threshold:
                continue
            key = (batch.node_id, point.slot)
            previous = self._last.get(key)
            self._last[key] = batch.ts
            self._last.move_to_end(key)
            if previous is not None and batch.ts - previous < hold:
                continue
            findings.append(
                Finding(
                    kind=KIND,
                    slot=point.slot,
                    metric="rssi",
                    value=point.rssi,
                    threshold=threshold,
                    evidence={"ema": point.ema, "seq": batch.seq},
                )
            )
        self._evict_overflow()
        return findings

    def stats(self) -> dict[str, int]:
        return {"tracked": len(self._last), "evicted": self._evicted}

    def _evict_overflow(self) -> None:
        while len(self._last) > self._capacity:
            self._last.popitem(last=False)
            self._evicted += 1
