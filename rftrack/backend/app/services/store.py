"""接入层的两只桶：每接入机一条**有界队列** + 一个**内存环形缓冲**。

两者的存在理由都是同一句话：**live 数据可以丢，读循环不能停**。

- 队列用 `asyncio.Queue(maxsize=limit)`（§5 指定），写入只有 `put_nowait` 这一条路。
  满载时丢的是**队首（最旧）**而不是新到的：读循环里出现 `await put()` 会在背压时把
  WebSocket 读取整条卡住，表现出来是"网页还连着但数据停格"，比丢几帧难查一个量级。
- 环形缓冲的键是 `(gateway_id, transport_addr, slot)`——就是节点身份键再加一个频道槽位。
  用这对原生键而不是 `node_id`，是因为 ingest 在写第一帧时可能还没落库；
  而**同名的两个串口分属两台接入机时必须是两条序列**（多接入机并跑的出口判据）。
- 点数预算 = `max_gateways × max_nodes_per_gateway × slots_per_node × ring_depth_per_slot`。
  正常情况下前面两道闸（ingest 的 `max_nodes_per_gateway` 与 dialer 的 `max_gateways`）
  就已经把总量卡住了；`_trim_to_budget` 是兜底，走到它说明有调用方绕过了接入层，
  所以它要留下数字（`dropped_points`）而不是静默涨内存。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from typing import Any

from ..config import Settings

#: (gateway_id, transport_addr, slot) → 一条环形序列。
Key = tuple[int, str, int]


class GatewayQueue:
    """一台接入机一条有界队列（**不是每节点一条**，理由见 §5：按节点排队则队列数随
    节点数线性增长，而且每条队列都得重复解一次帧）。"""

    def __init__(self, gateway_id: int, limit: int) -> None:
        self.gateway_id = gateway_id
        self.limit = limit
        self.dropped = 0
        self.received = 0
        self.peak = 0
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=limit)

    @property
    def depth(self) -> int:
        return self._queue.qsize()

    def offer(self, item: Any) -> str:
        """非阻塞投递。返回 `queued` 或 `dropped_oldest`——调用方不需要 await 任何东西。"""
        self.received += 1
        outcome = "queued"
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass  # 消费者恰好同时取走了一条，本轮没有丢东西
            else:
                self.dropped += 1
                outcome = "dropped_oldest"
        self._queue.put_nowait(item)
        if self._queue.qsize() > self.peak:
            self.peak = self._queue.qsize()
        return outcome

    async def get(self) -> Any:
        """消费侧唯一的取法（共享消费池用它）。"""
        return await self._queue.get()

    def get_nowait(self) -> Any:
        return self._queue.get_nowait()

    def replace(self) -> None:
        """重连会换一条队列：把没读完的旧帧丢掉，但 `dropped` 是这台接入机的累计事实，
        不能跟着清零——否则健康检查里的丢帧数会永远显示 0。"""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    def stats(self) -> dict:
        return {
            "gateway_id": self.gateway_id,
            "limit": self.limit,
            "depth": self.depth,
            "dropped": self.dropped,
            "peak": self.peak,
            "received": self.received,
        }


class SampleStore:
    """每 (接入机, 端点, 槽位) 一条最近 N 点的环形序列，总点数受预算约束。"""

    def __init__(self, depth_per_slot: int, budget_points: int) -> None:
        self.depth_per_slot = depth_per_slot
        self.budget_points = budget_points
        self.dropped_points = 0
        self._points = 0
        # OrderedDict 的迭代序 = "最久没被写过 → 最近被写过"，驱逐时直接取队首。
        self._series: dict[Key, deque] = OrderedDict()

    @classmethod
    def from_settings(cls, settings: Settings, slots_per_node: int | None = None) -> "SampleStore":
        slots = slots_per_node or settings.slots_per_node
        return cls(
            depth_per_slot=settings.ring_depth_per_slot,
            budget_points=settings.max_gateways * settings.max_nodes_per_gateway * slots * settings.ring_depth_per_slot,
        )

    @property
    def points(self) -> int:
        return self._points

    def push(self, gateway_id: int, transport_addr: str, slot: int, point: dict) -> None:
        key = (gateway_id, transport_addr, slot)
        ring = self._series.get(key)
        if ring is not None and len(ring) == ring.maxlen:
            # 槽位到顶：挤掉的是这一格最旧的点，总点数不变。
            self.dropped_points += 1
            self._points -= 1
        elif ring is None:
            self._series[key] = ring = deque(maxlen=self.depth_per_slot)
        ring.append(point)
        self._points += 1
        self._series.move_to_end(key)
        self._trim_to_budget(key)

    def _trim_to_budget(self, protected: Key) -> None:
        while self._points > self.budget_points and len(self._series) > 1:
            stale = next((key for key in self._series if key != protected), None)
            if stale is None:
                return  # 只剩正在写的这一条：预算再小也不能把当前数据丢掉
            ring = self._series.pop(stale)
            self._points -= len(ring)
            self.dropped_points += len(ring)

    def series(self, gateway_id: int, transport_addr: str, slot: int) -> list[dict]:
        """没有这条序列就回空列表——趋势图问一块没数据的板是常态，不是异常。"""
        return list(self._series.get((gateway_id, transport_addr, slot), ()))

    def forget(self, gateway_id: int, transport_addr: str) -> int:
        """端点消失（`lost`）时释放它占的全部槽位。返回交还的点数。

        只清缓冲，不动数据库行：`lost` 的节点还得能回答"它最后一次是什么状态"。
        """
        keys = [key for key in self._series if key[0] == gateway_id and key[1] == transport_addr]
        released = 0
        for key in keys:
            released += len(self._series.pop(key))
        self._points -= released
        return released

    def stats(self) -> dict:
        return {
            "series": len(self._series),
            "points": self._points,
            "budget": self.budget_points,
            "dropped_points": self.dropped_points,
        }
