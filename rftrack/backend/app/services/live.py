"""`/ws/live`：按场地分桶的推送口——当前值 + 事件，扇出非阻塞、队列有界。

契约表里那一行是"当前值 + 事件推送"，加上 S6 出口判据里最硬的那一条（**A 场地的订阅收不到
B 场地的事件**），这层就只有一个几何形状：分区做在**投递**上，不做在页面里。前端拿
`site_id` 过滤看起来一样，但它漏一条就是跨场地串数据，而那种串法在页面上完全看不出来——
和"一条 `/ws/bridge` 连接只属一台接入机"是同一条理由的两种写法。

五条决定：

- **`publish()` 一个字都不 await**：采集层在事件循环上调它，这里要是等一次 socket 写，
  "全场最多 8 块板"就变成"最慢的那一页决定全场节拍"。所以它只做两件事：把消息放进每个订阅者
  自己那条有界队列、唤醒那个订阅者的发送任务。
- **满队列丢最旧并计数，不关页**：`live_queue_limit` 之内留的是最新的那几条——"当前值"要的
  是新鲜，不是完整。把"笔记本合盖了"当成故障来治，代价是运营者正盯着的那一页变成"连不上"。
  丢了几条在 `/api/health` 的 `live.sites[].dropped` 里说得出，不静默。
- **快照只回"我们真的发布过什么"**：新订阅连上来先发一份 `snapshot`（每块板最后那一帧 +
  最近 `live_event_window` 条事件）。它不是从库里读的——读历史是 REST 的活；所以这份快照
  既不会凭空造出一个当前值，也不会因为"库里有一行"就替这块板说"它在动"。
- **离开只能在收侧看见**：这一页不发命令，但 ASGI 的 disconnect 是**读**出来的，不会自己变成
  异常。不去 `receive()`，桶里就会攒下一批"看起来还在收"的死订阅——队列攒满、丢弃计数一直涨，
  而 `/api/health` 报的订阅数是假的。所以发送与看门各一个任务，谁先到就收摊另一个。
- **撤订阅走 `finally`**：正常离开、发送炸、任务被取消，三条路都必须把这一位从桶里摘掉。
  测试断的是"桶空了"而不是"少了一个"——计数写错了也能过后一种断言。

汇聚角色**可以**订：那一台按设计不跑采集（`capture`/`replay` 都拒绝自己），所以它订上之后只会
收到一份空快照并停在"无数据"上。这是形态，不是故障，也不该由这层替它编一个理由出来。
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Mapping

from fastapi import WebSocket, WebSocketDisconnect

from ..config import Settings
from ..database import Database
from ..models import Site, utcnow
from .ws_bridge import Refused

LOG = logging.getLogger("rftrack.live")

#: 快照那条消息的 `kind`：页面用它区分"这是你连上时的第一眼"与"这是新的一帧"。
SNAPSHOT = "snapshot"


def resolve_site(db: Database, query: Mapping[str, str]) -> int:
    """把 `?site=` 落成一个库里确实存在的 `site_id`。说不清就拒绝，不猜。

    必填与 REST 侧同一条理由：一台实例可以管好几个场地，"默认第一个"会让一页安静地显示
    另一个场地的数——那正是最难倒推回去的那种错。

    存在性也在这里查：订了一个不存在的场地 id（手打错了、场地早删了）不会报错，只会永远停在
    "无数据"上，而"无数据"本来是个合法状态。那种时候宁可让连接带理由关掉。
    """
    raw = (query.get("site") or "").strip()
    if not raw:
        raise Refused("site_required")
    try:
        site_id = int(raw)
    except ValueError:
        raise Refused("site_not_found") from None
    with db.scope() as session:
        if session.get(Site, site_id) is None:
            raise Refused("site_not_found")
    return site_id


@dataclass
class _Subscriber:
    """一页在这一层的全部状态：一条有界队列、一个唤醒位、两个数。"""

    site_id: int
    limit: int
    queue: deque = field(default_factory=deque)
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    sent: int = 0
    dropped: int = 0

    def offer(self, message: dict[str, Any]) -> bool:
        """放进队列；满了先挤掉最旧的一条。返回"这次是不是丢了东西"。"""
        overflow = len(self.queue) >= self.limit
        if overflow:
            self.queue.popleft()
            self.dropped += 1
        self.queue.append(message)
        self.wake.set()
        return overflow

    async def wait(self) -> None:
        """等到有东西可发。检查与 `clear()` 之间没有 await，所以不会丢唤醒。"""
        while not self.queue:
            self.wake.clear()
            await self.wake.wait()

    def take(self) -> list[dict[str, Any]]:
        queued, self.queue = list(self.queue), deque()
        return queued


class LiveHub:
    """场地 → 订阅者列表；采集层只管"给这个场地发一条"，不认识任何一页。"""

    def __init__(self, settings: Settings, *, log=LOG) -> None:
        self.settings = settings
        self.log = log
        self._buckets: dict[int, list[_Subscriber]] = {}
        # 快照的两份底：`node_id → 最后那一帧`、以及每场地一条环形事件窗。
        self._latest: dict[int, dict[int, dict[str, Any]]] = {}
        self._events: dict[int, deque] = {}
        # --- 计数：每种损失各有一个数，"看起来在推"不是可交付的事实 ---
        self.published = 0
        self.delivered = 0
        self.dropped = 0
        self.send_errors = 0
        #: 逐场地的订阅数：只在订阅/退订时整份换出，好让健康检查在另一个线程里读得安全。
        self._counts: tuple[dict[str, Any], ...] = ()
        #: 快照里"有几块板"。与 `_counts` 同一个理由：`status()` 不许遍历 `_latest`——
        #: 那个字典正在被事件循环加键，遍历它就是把 `dictionary changed size` 留给最说不清的时刻。
        self._snapshot_nodes = 0

    # --- 发布侧（采集层调用；同步、非阻塞）-----------------------------------

    def publish(self, site_id: int, message: dict[str, Any]) -> int:
        """给这个场地的每一页各放一条。返回收到这条消息的订阅数。

        没有订阅者也照样记快照：`snapshot` 要能回答"这块板最近一帧是什么"，而那一帧很可能
        是上一页还连着时发出去的。
        """
        self._remember(site_id, message)
        bucket = self._buckets.get(site_id) or ()
        for subscriber in bucket:
            if subscriber.offer(message):
                self.dropped += 1
        self.published += 1
        self.delivered += len(bucket)
        # 换出一份新计数：`dropped` 与 `queued` 要能在健康检查里说得出当时的事，
        # 而"当时"就是每一条消息发出去的那一刻。
        self._refresh_counts()
        return len(bucket)

    def _remember(self, site_id: int, message: dict[str, Any]) -> None:
        kind = message.get("kind")
        if kind == "samples":
            node_id = message.get("node_id")
            if node_id is not None:
                if node_id not in self._latest.setdefault(site_id, {}):
                    self._snapshot_nodes += 1
                self._latest[site_id][node_id] = message
        elif kind == "event":
            window = self._events.setdefault(
                site_id, deque(maxlen=max(0, self.settings.live_event_window))
            )
            if window.maxlen:
                window.append(message)

    def snapshot(self, site_id: int) -> dict[str, Any]:
        """这一层"目前知道"的全部：每块板最后那一帧 + 最近几条事件。"""
        return {
            "kind": SNAPSHOT,
            "site_id": site_id,
            "ts": utcnow().isoformat(),
            "samples": [
                self._latest[site_id][node_id]
                for node_id in sorted(self._latest.get(site_id, {}))
            ],
            "events": list(self._events.get(site_id, ())),
        }

    # --- 订阅侧 ---------------------------------------------------------------

    async def serve(self, websocket: WebSocket, site_id: int) -> None:
        """先把这一页登记进桶，然后一直发到它离开。离开一定撤——三条路都是。

        看门那条任务为什么必须存在，写在模块开头的第三条决定里：这一页的 disconnect
        只能从收侧读出来，不会自己变成异常。
        """
        subscriber = self._subscribe(site_id)
        try:
            await websocket.send_json(self.snapshot(site_id))
            # 快照也是这一页收到的一条：`sent` 的口径是"送出去几条"，不是"送出去几条新帧"。
            subscriber.sent += 1
            self._refresh_counts()
            sender = asyncio.create_task(self._send_loop(websocket, subscriber))
            watcher = asyncio.create_task(self._watch_leave(websocket))
            done, pending = await asyncio.wait(
                {sender, watcher}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                # 发送侧的异常要冒出来，不许被"另一侧也结束了"这件事盖掉。
                task.result()
        except WebSocketDisconnect:
            pass
        except RuntimeError as exc:
            # 页面先走一步时 `send_json` 就是这么个说法：撤订阅是正确结局，不是故障。
            # 计数不静默，但也不进 `problem()`——那属于"对面不干了"，我们管不着。
            self.send_errors += 1
            self.log.debug("live send failed for site %s: %s", site_id, exc)
        finally:
            self._unsubscribe(subscriber)

    async def _send_loop(self, websocket: WebSocket, subscriber: _Subscriber) -> None:
        while True:
            await subscriber.wait()
            for message in subscriber.take():
                await websocket.send_json(message)
                subscriber.sent += 1
            # 送出去也是状态变化：`queued` 掉下去、`sent` 上来，健康检查看到的才不是
            # "上一次有人发布时"的旧账。
            self._refresh_counts()

    async def _watch_leave(self, websocket: WebSocket) -> None:
        """读到页面离开为止。它主动发来的东西这层没有语义，收下并忽略。"""
        try:
            while True:
                message = await websocket.receive()
                if message["type"] == "websocket.disconnect":
                    return
        except WebSocketDisconnect:
            return

    def _subscribe(self, site_id: int) -> _Subscriber:
        subscriber = _Subscriber(site_id=site_id, limit=self.settings.live_queue_limit)
        self._buckets.setdefault(site_id, []).append(subscriber)
        self._refresh_counts()
        return subscriber

    def _unsubscribe(self, subscriber: _Subscriber) -> None:
        bucket = self._buckets.get(subscriber.site_id)
        if bucket is None:
            return
        # 按对象摘而不是按 `remove()`：同一页在同一场地理论上只有一条连接，但摘错别人的
        # 那一位是这里最不该发生的事，`discard` 语义（存在才摘、只摘一次）用地址相等来判更稳。
        for index, entry in enumerate(bucket):
            if entry is subscriber:
                del bucket[index]
                break
        if not bucket:
            del self._buckets[subscriber.site_id]
        self._refresh_counts()

    def _refresh_counts(self) -> None:
        """整份换出一份新元组：健康检查在线程里读它，这边在循环上改桶。

        与 `capture._refresh_queues` 同一条理由——省掉这次复制换来的那点开销，正好被一次
        `dictionary changed size during iteration` 赔光，而那发生在最该说得清的时刻。
        """
        self._counts = tuple(
            {
                "site_id": site_id,
                "subscribers": len(bucket),
                "queued": sum(len(entry.queue) for entry in bucket),
                "dropped": sum(entry.dropped for entry in bucket),
                "sent": sum(entry.sent for entry in bucket),
            }
            for site_id, bucket in sorted(self._buckets.items())
        )

    # --- 对外呈现 -------------------------------------------------------------

    def problem(self) -> str | None:
        """这层没有"该说话"的故障：没人订、发得慢、页面走掉，三种都是正常形态。

        唯一会让它卡住的是配置（`live_queue_limit` 在 `Settings` 构造时就拦住 0），
        所以把可选件的闲置报成 degraded 只会让人开始忽略 degraded。
        """
        return None

    def status(self) -> dict[str, Any]:
        """纯内存快照 + 整数计数，可在另一个线程里读（见 `_refresh_counts`）。"""
        return {
            "subscribers": sum(entry["subscribers"] for entry in self._counts),
            "sites": list(self._counts),
            "queue_limit": self.settings.live_queue_limit,
            "event_window": self.settings.live_event_window,
            "published": self.published,
            "delivered": self.delivered,
            "dropped": self.dropped,
            "send_errors": self.send_errors,
            "snapshot_nodes": self._snapshot_nodes,
            "problem": self.problem(),
        }
