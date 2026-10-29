"""采集层：把透传字节折成"每频道槽位一个点"，分发给内存环、桶摘要与事件表。

这里是 S3 那两件"能力齐备但未接线"的东西（`frame_parser` 与 `store`）的接线处。
四条决定各自否掉了一个看起来更顺手的做法，都写在代码里改不动的位置上：

- **观察而不是占用**：字节来自 `BridgeHub` 的**观察者钩子**，只在"有页面正连着这块板"时
  看得见。所以本层永不为采集自己开第二条上游、也永不长期握着端点——那样 ②a 的
  RotorHazard 会被一直停着（R21），并直接违反"不得破坏原有功能"。
  由此得到一条必须讲出口的推论：**没人在看 = 没有字节**，因此采集层不得把"没字节"说成
  "板子没了"（那是 `dialer`/`ingest` 的 `lost` 的活）。
- **断流事件的成立范围**：`node_offline` 只在**这条流还开着、却连续 `capture_silence_s`
  秒没出一个帧**时落一行；`node_online` 只在"我们已为它落过 offline"且重新有帧时落一行。
  会话结束撤状态、**不补 offline**——"页面关了"不是"板子拔了"，写成事件就是编造观测。
- **`ep` 到 `transport_addr` 找桥认领**：桥的 `id` 与它自己的 `path` 不总是同一个串
  （`web/serve.mjs` 的 uart 条目：`id` 用未解析的 `--rh-serial`，`path` 是 `realpath` 之后的
  `ttyAMA0`）。节点行存的是 `path`，所以"把 `ep` 去掉前缀当地址"会让 ②a 的板子
  **永远归不了属**。唯一权威映射就是桥的端点表 + `ingest.endpoint_addr`（落库用的同一个函数），
  这里按网关缓存、按时刷新，认不出来就如实计数，不猜。
- **事件循环上一个字节的 SQL 都不做**：解析、入环、进累加器全在循环上（纯内存，µs 级），
  读写库与 HTTP 一律交线程，而且**线程只回一份新数据、由循环侧整份换掉引用**。
  SD 卡上一次 `executemany` 能到几百毫秒，留在循环里就是 `/ws/bridge` 与 `/ws/live` 一起卡住。

队列形态沿用 §5 的裁定：**每接入机一条有界队列 + 一个共享消费池**。池里一个 worker 一次只
认领一条队列、最多搬 `capture_batch_chunks` 块。认领的理由是正确性而不是公平：一条端点的
`FrameStream` 是**有状态**的（半截帧留在它的缓冲里），两个 worker 同喂一条端点就会把一帧
撕成两处，表现出来是"CRC 错误一直在涨"这种最难倒推的现象。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from time import monotonic
from typing import Any, Protocol

from sqlalchemy import select

from ..config import Settings
from ..database import Database
from ..models import Gateway, Node, Relay, SignalEvent, utcnow
from . import frame_parser, ingest, retention
from .dialer import ENDPOINTS_PATH, bridge_url, classify
from .instance import effective_instance_role
from .store import GatewayQueue, SampleStore
from .ws_bridge import Link

LOG = logging.getLogger("rftrack.capture")

#: 帧类型 → 名字，只用于计数与状态呈现。`spectrum`/`telemetry`/`rawiq` 在 S6 不入库
#: （它们不是"每槽位一个点"那种形态），但**必须数得出来**——否则"没接线"与"没数据"
#: 在页面上长成同一个样子，正是规范 §2.3 要避免的那类歧义。
FRAME_NAMES = {
    frame_parser.FRAME_METRICS: "metrics",
    frame_parser.FRAME_SPECTRUM: "spectrum",
    frame_parser.FRAME_TELEMETRY: "telemetry",
    frame_parser.FRAME_RAWIQ: "rawiq",
    frame_parser.FRAME_LOG: "log",
    frame_parser.FRAME_LEVELS: "levels",
}


class EndpointResolver(Protocol):
    """`ep` → 节点行里的 `transport_addr`。认不出来就少给几个键，不猜一个地址。"""

    def addrs_for(self, gateway_id: int, eps: tuple[str, ...]) -> dict[str, str]: ...


class LivePublisher(Protocol):
    """`/ws/live` 的收件口。实现**必须非阻塞**（扇出在它自己的队列里做）。

    返回真正收到这条消息的订阅数：采集层要能说出"这些点有没有人接"，发完就当成功是不行的。
    """

    def publish(self, site_id: int, message: dict[str, Any]) -> int: ...


@dataclass(frozen=True)
class NodeRef:
    """一行节点事实。`site_id` 经 `gateway → relay → site` 推导，与 `routers/nodes.py` 同一条链。"""

    node_id: int
    site_id: int
    gateway_id: int
    transport_addr: str
    display_name: str


@dataclass(frozen=True)
class _PendingFrame:
    """一帧还没认领上的东西：整帧存，不按点存。

    理由是"迟到归属的那一帧必须和实时的一帧长成同一个样子出去"。按点存就只能自己拼发布
    载荷（`seq/mode/loKhz/tMs` 是帧级的），要么少字段、要么另开一条发布路径——两条路早晚对不上。
    """

    points: tuple[dict[str, Any], ...]
    decoded: dict[str, Any]
    moment: datetime


@dataclass
class _Endpoint:
    """一条正在被观察的端点的运行态。只在事件循环上读写，所以不需要锁。"""

    gateway_id: int
    ep: str
    source: str = "real"
    transport_addr: str | None = None
    stream: frame_parser.FrameStream = field(default_factory=frame_parser.FrameStream)
    pending: deque = field(default_factory=deque)
    #: `pending` 里现在攒着多少个**点**（不是帧）。预算按点算，所以这个数得自己跟着，
    #: 免得每次挤兑都要把整条缓冲数一遍。
    held: int = 0
    #: 已经折进全局总数的份额。`FrameStream` 的计数是**这条流的生命周期累计**，
    #: 而流随会话一起没；不记这两个数，`_process` 的增量折一次、`_forget_endpoint`
    #: 再折一次，同一批 CRC 错误就会被算两遍（总数比真相大，是最容易被当成"线不好"的假证据）。
    crc_seen: int = 0
    resync_seen: int = 0
    last_frame_mono: float | None = None
    last_seq: int | None = None
    #: `silent` 说的是"**已经落过** node_offline"，不是"此刻安静"：断流与恢复都按跃迁落行，
    #: 一轮里安静三次也只写一行——事件表记的是发生过什么，不是我们轮询了几次。
    silent: bool = False


@dataclass(frozen=True)
class _Chunk:
    """队列里的一条：一次上游读到的原始字节。钩子只投递，解析在消费侧。"""

    gateway_id: int
    ep: str
    chunk: bytes
    source: str
    transport_addr: str | None = None


@dataclass
class _Job:
    """一轮里"要写库的东西"。循环侧装满它，线程只读它，写回结果再由循环侧结算。

    两栏都是**纯数据**（`dict`），不是 ORM 对象：线程写失败后这批要留在 `_retry_*` 里到下一轮，
    而挂过 session 的对象经一次 rollback 就处在"不确定是不是还能 add"的状态——到那时丢的是
    事件行，页面上表现为"断流没记录"，比慢一百倍更难查。
    """

    rows: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    #: 与 `events` 同序的主键：行落库之后才拿得到，用来让 `/ws/live` 那条事件与
    #: `GET /api/events` 里那一行是**同一个对象**（见 `_publish_event`）。
    event_ids: list[int] = field(default_factory=list)
    summary_written: int = 0
    error: str | None = None


def endpoint_id_addr(ep: str) -> str | None:
    """从 `ep` 本身推得出的地址只有 `node:` 一种——③ 的远端节点没有 `path`，
    `ingest.endpoint_addr` 对它回的就是 `id` 本身，这里与它同口径，不另发明规则。
    """
    return ep if ep.startswith("node:") else None


def public_point(point: dict[str, Any]) -> dict[str, Any]:
    """把环里那一格换成可直接 JSON 化的形态：`ts` 从 `datetime` 变成 ISO 串。

    环里存 `datetime` 而不是字符串，是因为趋势要按时间比较；发出去必须是字符串，
    因为 WS 帧里没有 datetime 这种东西。一个字段两种形态是刻意的，两处都有名字。
    """
    moment = point["ts"]
    return {**point, "ts": moment.isoformat() if isinstance(moment, datetime) else moment}


class BridgeEndpointResolver:
    """照桥自己的 `/api/endpoints` 认领地址；按网关缓存整表，到期重取。

    表拉不通时**继续用上一次的答案**：一次网络抖动不该让已经归好属的端点退回未归属，
    而"多久没成功刷新"另有 `last_error` 与 `failures` 说话。
    """

    def __init__(self, db: Database, transport, settings: Settings) -> None:
        self.db = db
        self.transport = transport
        self.settings = settings
        self.ttl_s = settings.capture_endpoint_ttl_s
        self._tables: dict[int, tuple[dict[str, str], float]] = {}
        self.last_error: str | None = None
        self.fetches = 0
        self.failures = 0

    def _table(self, gateway_id: int) -> dict[str, str]:
        cached = self._tables.get(gateway_id)
        if cached is not None and monotonic() - cached[1] < self.ttl_s:
            return cached[0]
        try:
            with self.db.scope() as session:
                gateway = session.get(Gateway, gateway_id)
                if gateway is None:
                    raise LookupError("gateway_gone")
                result = self.transport.fetch(
                    bridge_url(gateway, ENDPOINTS_PATH), self.settings.probe_timeout_s
                )
            ok, reason, _detail, endpoints = classify(result)
            if not ok:
                raise LookupError(reason)
            addrs = {}
            for entry in endpoints or []:
                try:
                    addrs[str(entry.get("id"))] = ingest.endpoint_addr(entry)
                except Exception:  # 一条认领不了不许带走整张表
                    continue
            self.fetches += 1
            self.last_error = None
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            addrs = cached[0] if cached is not None else {}
        self._tables[gateway_id] = (addrs, monotonic())
        return addrs

    def addrs_for(self, gateway_id: int, eps: tuple[str, ...]) -> dict[str, str]:
        table = self._table(gateway_id)
        return {ep: table[ep] for ep in eps if ep in table}


class CapturePipeline:
    """采集的全部运行态：钩子 → 每接入机一条队列 → 消费池 → 两只桶 + 摘要 + 事件。

    起合方式与 `DialCoordinator`/`RetentionRunner` 一致（lifespan 里 `start()`/`stop()`）。
    内部纪律是**单线程可变状态**：`_endpoints`/`_queues`/`aggregator`/`store` 只在事件循环上
    被改，线程只做 SQL 与 HTTP 并**返回一份新数据**，由循环侧整份换掉引用（`_refs`）。
    周期节拍因此走 `run_in_executor(None, self.tick)`：轮到它跑的时候，循环侧只有 `_worker`
    在改内存，而那几步的写全部发生在同一个线程池序列里——真需要一口气跑完整条链的场合
    （`stop()` 的收尾、测试）也都先把 worker 停掉再调 `tick()`。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        store: SampleStore | None = None,
        resolver: EndpointResolver | None = None,
        transport=None,
        publisher: LivePublisher | None = None,
        aggregator: retention.SummaryAggregator | None = None,
        log=LOG,
    ) -> None:
        self.db = db
        self.settings = settings
        self.log = log
        self.store = store or SampleStore.from_settings(settings)
        self.aggregator = aggregator or retention.SummaryAggregator.from_settings(settings)
        self.resolver = resolver or BridgeEndpointResolver(db, transport, settings)
        self.publisher = publisher
        self._endpoints: dict[tuple[int, str], _Endpoint] = {}
        self._queues: dict[int, GatewayQueue] = {}
        self._refs: dict[tuple[int, str], NodeRef] = {}
        self._claimed: set[int] = set()
        #: 两个"发布出去就只读"的快照，见 `_refresh_queues()` 与 `_count_skipped()`。
        self._queue_stats: tuple[dict[str, Any], ...] = ()
        self._skipped_snapshot: dict[str, int] = {}
        self._work = asyncio.Event()
        self._stopping: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []
        # --- 计数：每一条都得能在 /api/health 里指认，"看起来在跑"不是可交付的事实 ---
        self.frames = 0
        self.text_lines = 0
        self.crc_errors = 0
        self.resync_bytes = 0
        self.skipped_frames: dict[str, int] = {}
        self.points_stored = 0
        #: 板子报的档位下标不在 `0..slots_per_node-1` 里的点数。固件今天造不出这种数
        #: （`PROFILE_MAX_CHANNELS 8`），所以这一格是"非合规字节流"的闸，不是常态计数。
        self.slots_out_of_range = 0
        self.dropped_chunks = 0
        self.pending_points = 0
        self.published = 0
        self.published_readers = 0
        self.publisher_errors = 0
        #: 事件另立一个数：`published` 是"帧消息"的条数，两件事混在一个数里就都说不清。
        self.events_published = 0
        self.unattributed = 0
        self.attributed_late = 0
        self.pending_dropped = 0
        self.gateways_refused = 0
        self.observer_errors = 0
        #: 归属表没刷成功过几轮。不是故障申报，只是"新登记的板子可能还没被认领"的线索。
        self.node_table_failures = 0
        self.ticks = 0
        self.flush_errors = 0
        self.events_written = 0
        self.events_dropped = 0
        self.last_flush_at: datetime | None = None
        self.last_error: str | None = None
        self.running = False
        self.role: str | None = None
        self._retry_rows: list[dict[str, Any]] = []
        self._retry_events: list[dict[str, Any]] = []

    # --- 生命周期 -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.capture_enabled

    async def start(self) -> None:
        """汇聚角色不采集：那一台根本没有桥，字节进不来。

        与 `dialer` 同口径——"这台按设计没有采集循环"是**形态**不是**故障**，所以
        `problem()` 在这种情况下回 `None`，而 `status()` 里写明 `refused="aggregator_role"`。
        """
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        self.role = await loop.run_in_executor(None, self._read_role)
        if self.role == "aggregator":
            return
        self._stopping = asyncio.Event()
        self.running = True
        workers = self.settings.capture_consumer_limit
        self._tasks = [
            *[asyncio.create_task(self._worker(i), name=f"capture-w{i}") for i in range(workers)],
            asyncio.create_task(self._flusher(), name="capture-flush"),
        ]

    async def stop(self) -> None:
        if self._stopping is not None:
            self._stopping.set()
        self._work.set()
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        was_running = self.running
        self.running = False
        #: 正常收摊前把最后攒着的格子写下去——"进程重启后最后一个桶凭空没了"是不好查的。
        #: 放在 worker 全停之后：这条尾拍已经没有别的任务在改内存。
        if was_running:
            try:
                await self.flush_cycle()
            except Exception as exc:  # 关停阶段的写失败已经没有下一轮可以重试
                self.log.warning("capture final flush failed: %s", exc)

    def _read_role(self) -> str:
        with self.db.scope() as session:
            return effective_instance_role(session, self.settings.instance_role)

    # --- 上游钩子（`BridgeHub` 调用；同步、非阻塞、绝不往外抛）----------------

    def tap(self, link: Link, payload: bytes) -> None:
        """一条透传字节。透传是给原页面用的，所以采集这边出任何问题都只该动自己的计数。

        队列建不出来（超限）、内存吃紧、钩子本身抛异常——任何一种都不该让那块板的字节
        停止到达浏览器。`observer_errors` 就是"我们本来想丢多少"的数字。

        没启用或没在跑（含角色是汇聚、或 lifespan 还没走到 `start()`）时**一个字都不收**：
        关掉的采集不该替别人攒字节，攒到队列上限还挤掉的是下一轮真正要采的东西。
        """
        if not (self.enabled and self.running):
            return
        try:
            self._offer(
                _Chunk(gateway_id=link.gateway_id, ep=link.ep, chunk=payload, source="real")
            )
        except Exception as exc:
            self.observer_errors += 1
            self.log.warning("capture tap failed: %s", exc)

    def untap(self, link: Link) -> None:
        """会话结束：撤掉这条端点的流状态与安静判定，**不补断流事件**（理由见模块注释）。"""
        self._forget_endpoint((link.gateway_id, link.ep))

    def feed(
        self,
        gateway_id: int,
        ep: str,
        chunk: bytes,
        *,
        source: str = "real",
        transport_addr: str | None = None,
    ) -> None:
        """直接喂字节的口：回放与测试用，下游与 `tap` 完全同一条。

        `transport_addr` 是给回放留的——录制文件自己声明了它当时的地址，没有（也不该有）
        一台桥可以去认领它。现场字节一律留 `None`，走认领那条路。

        与 `tap` 唯一的区别：这里**不看 `capture_enabled`**。那个开关管的是"要不要挂在透传上
        偷偷收字节"，而调用 `feed` 的人已经点名要把这段数据喂进去了。
        """
        self._offer(
            _Chunk(
                gateway_id=gateway_id,
                ep=ep,
                chunk=chunk,
                source=source,
                transport_addr=transport_addr,
            )
        )

    def _offer(self, item: _Chunk) -> None:
        queue = self._queue_for(item.gateway_id)
        if queue is None:
            self.gateways_refused += 1
            return
        if queue.offer(item) == "dropped_oldest":
            # 每台的丢帧数在 `queues` 里各有一条，总数还要一个：运营者先问"一共丢了多少"，
            # 再问"是哪一台丢的"。
            self.dropped_chunks += 1
            # 丢发生在投递侧，快照就得在投递侧换：`_drain` 只在搬得动的时候刷新，而"满到开始
            # 丢"恰恰是搬不动的时候——那边不刷，健康检查里的 `dropped` 会停在创建那一刻的 0。
            # 没丢的常规一轮不换快照，这条热路径的代价就只是每帧一次整数比较。
            self._refresh_queues()
        self._work.set()

    def _refresh_queues(self) -> None:
        """把每条队列的计数发布成一份**新**元组。

        旧的从不原地改：`/api/health` 在另一个线程里迭代它，而这边随时可能新增一条队列
        （第一次有人连新板子）。迭代时长度变了就是 500，而那个时刻正是最想知道答案的时刻。
        """
        self._queue_stats = tuple(queue.stats() for queue in self._queues.values())

    def _forget_endpoint(self, key: tuple[int, str]) -> None:
        endpoint = self._endpoints.pop(key, None)
        if endpoint is None:
            return
        # 会话结束时缓冲里可能还有没出仓的点（页只开了不到一拍）。已经认领上就当场交出去——
        # 这一步不碰库，用的就是循环上那份 `_refs`；还认领不上就只能丢，而丢必须进
        # `pending_dropped`，否则"短会话没数据"又是一件只能靠猜才能发现的事。
        self._release(endpoint, self._node_for(endpoint))
        # 只折还没算过的那一截（理由见 `_Endpoint.crc_seen`）。
        self.crc_errors += endpoint.stream.crc_errors - endpoint.crc_seen
        self.resync_bytes += endpoint.stream.resync_bytes - endpoint.resync_seen

    def _queue_for(self, gateway_id: int) -> GatewayQueue | None:
        """每接入机一条队列，条数受 `max_gateways` 这道闸管。

        超限**不新建**并计数：静默多开一条就把"总并发有硬闸"那句话作废了。
        """
        queue = self._queues.get(gateway_id)
        if queue is not None:
            return queue
        if len(self._queues) >= self.settings.max_gateways:
            return None
        queue = GatewayQueue(gateway_id, self.settings.queue_limit)
        self._queues[gateway_id] = queue
        self._refresh_queues()
        return queue

    # --- 消费池 ---------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            gateway_id = self._claim()
            if gateway_id is not None:
                try:
                    self._drain(gateway_id)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # 一条端点炸了不许带走 worker：那会让全场停摆
                    self.log.warning("capture worker %s failed: %s", index, exc)
                finally:
                    self._claimed.discard(gateway_id)
                    self._work.set()  # 这条队列可能还有货，别的 worker 接着搬
                continue
            # 没活可干才等。0.25 s 的兜底不是节流：`clear()` 与下一次 `set()` 之间进来的
            # 字节必须还能在下一轮被看到，纯靠事件会有丢唤醒的窗口。
            try:
                await asyncio.wait_for(self._work.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
            self._work.clear()

    async def _flusher(self) -> None:
        """按节拍跑一轮：摘要落库、事件落库、归属刷新、断流判定都在这一步。"""
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self.settings.capture_flush_interval_s
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.flush_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败绝不终止节拍（与 retention 的循环同一条纪律）
                self.log.warning("capture tick failed: %s", exc)

    def _claim(self) -> int | None:
        """认领一条**没被认领且有货**的队列。全在同步代码里，所以不会撕裂。"""
        for gateway_id, queue in self._queues.items():
            if gateway_id in self._claimed or queue.depth == 0:
                continue
            self._claimed.add(gateway_id)
            return gateway_id
        return None

    def _drain(self, gateway_id: int) -> int:
        queue = self._queues[gateway_id]
        moved = 0
        for _ in range(self.settings.capture_batch_chunks):
            try:
                item = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._process(item)
            moved += 1
        self._refresh_queues()
        return moved

    def drain_all(self) -> int:
        """同步把每条队列**搬空**，返回搬了多少块（一次 `_drain` 只搬一批）。

        给不起事件循环的调用方用（测试，以及 S6-4 的离线回放）：worker 池那条路要先有
        `start()` 才会搬字节，而这类调用方就是"一口气跑完、不要后台任务"。前提与 `tick()`
        同一条：同一时刻不许有 worker 也在改这些结构。
        """
        total = 0
        for queue in list(self._queues.values()):
            while queue.depth:
                moved = self._drain(queue.gateway_id)
                if not moved:
                    break  # 队列说有货但取不出（只可能是别人也在取）：不跟它死磕
                total += moved
        return total

    def _process(self, item: _Chunk) -> None:
        key = (item.gateway_id, item.ep)
        endpoint = self._endpoints.get(key)
        if endpoint is None:
            endpoint = self._endpoints[key] = _Endpoint(
                gateway_id=item.gateway_id,
                ep=item.ep,
                transport_addr=item.transport_addr or endpoint_id_addr(item.ep),
            )
        # `mock` 只能被加上、不能被洗掉：与 `SummaryAggregator` 的来源纪律同一条。
        if item.source == "mock":
            endpoint.source = "mock"
        before_crc = endpoint.stream.crc_errors
        before_resync = endpoint.stream.resync_bytes
        items = endpoint.stream.push(item.chunk)
        for piece in items:
            if piece.kind == "text":
                self.text_lines += 1
                continue
            self._frame(endpoint, piece)
        # 只要这条流还在吐东西（帧或文本行）就说明板子活着：只数采样帧的话，
        # 一个"只发频谱帧"或"只发日志行"的会话会被断流判定误报成 node_offline。
        if items:
            endpoint.last_frame_mono = monotonic()
        self.crc_errors += endpoint.stream.crc_errors - before_crc
        self.resync_bytes += endpoint.stream.resync_bytes - before_resync
        endpoint.crc_seen = endpoint.stream.crc_errors
        endpoint.resync_seen = endpoint.stream.resync_bytes

    def _count_skipped(self, name: str) -> None:
        """按帧类型名计数，并**整份换出**一份给健康检查读的快照。

        每帧多复制一个最多六项的字典，代价是纳秒级；换来的是"另一个线程迭代它时长度不会变"。
        省掉这次复制换来的省，正好被一次 500 的健康检查赔光。
        """
        self.skipped_frames[name] = self.skipped_frames.get(name, 0) + 1
        self._skipped_snapshot = dict(self.skipped_frames)

    def _frame(self, endpoint: _Endpoint, piece: frame_parser.StreamItem) -> None:
        self.frames += 1
        decoded = frame_parser.decode(piece.type, piece.payload)
        if decoded is None or "channels" not in decoded:
            self._count_skipped(FRAME_NAMES.get(piece.type, f"unknown_{piece.type}"))
            return
        moment = utcnow()
        points = [_point(c, decoded, moment, endpoint.source) for c in decoded["channels"]]
        # 越界档位**落库就再也读不出来**：`/api/samples/summary` 与 `?slot=` 两道闸都按
        # `slots_per_node` 拒（`routers/samples.py:45`），页面按同一个口径过滤（`channel-matrix.js:52`）。
        # 在这里拦下并数出来，挡掉的正是"往 SD 卡写一批永远查不到的行"（R11 的写放大）。
        # 环、摘要、`/ws/live` 三条路都从这一份 `points` 出发，所以越界的档位在页面上根本不存在，
        # `slots_out_of_range` 是它唯一的痕迹；一帧里一个能存的档都没有就整帧不进账——否则既发布
        # 一个恒空的节点，又把零点条目塞进按**点数**记预算的待归属队列（那条队列不按条目数记）。
        inside = [p for p in points if 0 <= p["id"] < self.settings.slots_per_node]
        self.slots_out_of_range += len(points) - len(inside)
        endpoint.last_seq = decoded["seq"]
        if not inside:
            return
        self._attribute(endpoint, inside, decoded, moment)

    def _attribute(
        self,
        endpoint: _Endpoint,
        points: list[dict[str, Any]],
        decoded: dict[str, Any],
        moment: datetime,
    ) -> None:
        """找到节点行就分发；找不到就整帧进待归属缓冲。

        归不了属的点**不落摘要、不发布**——"不知道是谁的点"进任何一个查询口径都是编造，
        而它有 `unattributed` / `pending_dropped` 两个数字可以说话。攒着而不是丢掉的理由：
        桥的端点表是按 `capture_endpoint_ttl_s` 刷新的，"刚连上还没认领"是常态，
        直接丢就等于每块板每次都少几秒数据。
        """
        node = self._node_for(endpoint)
        if node is not None:
            self._emit(node, endpoint, points, decoded, moment)
            return
        # 攒的是整帧，挤的也是整帧：预算 4 个点而一帧 3 个点时，缓冲里只留得下最近那一帧，
        # 被挤掉的 3 个点进 `pending_dropped`——宁可少一帧，也不要发出一条只有半个帧的消息。
        endpoint.pending.append(_PendingFrame(tuple(points), decoded, moment))
        endpoint.held += len(points)
        self.pending_points += len(points)
        self.unattributed += len(points)
        budget = self.settings.capture_pending_per_endpoint
        while endpoint.held > budget:
            dropped = endpoint.pending.popleft()
            endpoint.held -= len(dropped.points)
            self.pending_points -= len(dropped.points)
            self.pending_dropped += len(dropped.points)

    def _node_for(self, endpoint: _Endpoint) -> NodeRef | None:
        """这条端点当前对应哪一行节点。没认领上、或认领到的地址不在归属表里，都回 `None`。"""
        if endpoint.transport_addr is None:
            return None
        return self._refs.get((endpoint.gateway_id, endpoint.transport_addr))

    def _release(self, endpoint: _Endpoint, node: NodeRef | None) -> int:
        """倒空一条端点的待归属缓冲，返回交出去的点数。

        `node` 为 `None` 时的丢弃只发生在**这条流已经不在了**的那条调用路上（`_forget_endpoint`）：
        还活着的端点走 `attribute_pending`，那边认领不上就继续攒着，一个点都不丢。
        """
        if not endpoint.pending:
            return 0
        frames = list(endpoint.pending)
        held = endpoint.held
        endpoint.pending.clear()
        endpoint.held = 0
        self.pending_points -= held
        if node is None:
            self.pending_dropped += held
            return 0
        moved = 0
        for buffered in frames:
            self._emit(node, endpoint, list(buffered.points), buffered.decoded, buffered.moment)
            self.attributed_late += len(buffered.points)
            moved += len(buffered.points)
        return moved

    def _emit(
        self,
        node: NodeRef,
        endpoint: _Endpoint,
        points: list[dict[str, Any]],
        decoded: dict[str, Any],
        moment: datetime,
    ) -> None:
        for point in points:
            self.store.push(node.gateway_id, node.transport_addr, point["id"], point)
            self.aggregator.add(
                site_id=node.site_id,
                gateway_id=node.gateway_id,
                node_id=node.node_id,
                slot=point["id"],
                ts=moment,
                rssi=point["rssi"],
                ema=point["ema"],
                source=endpoint.source,
            )
        self.points_stored += len(points)
        if self.publisher is None:
            return
        try:
            readers = self.publisher.publish(
                node.site_id,
                {
                    "kind": "samples",
                    "ts": moment.isoformat(),
                    "site_id": node.site_id,
                    "gateway_id": node.gateway_id,
                    "node_id": node.node_id,
                    "transport_addr": node.transport_addr,
                    "display_name": node.display_name,
                    "ep": endpoint.ep,
                    "source": endpoint.source,
                    "seq": decoded["seq"],
                    "mode": decoded["mode"],
                    "loKhz": decoded["loKhz"],
                    "tMs": decoded["tMs"],
                    "channels": [public_point(p) for p in points],
                },
            )
        except Exception as exc:  # 发布口炸了不许停在半帧上：数据已经入了环与累加器
            self.publisher_errors += 1
            self.log.warning("live publish failed: %s", exc)
            return
        self.published += 1
        self.published_readers += readers

    # --- 一轮：内存侧准备 → 线程侧读写 → 内存侧结算 ---------------------------

    def prepare(self) -> _Job:
        """攒出这一轮要写库的东西：摘要格子 + 断流/恢复事件。只在循环上调。

        上一轮没写成的（`_retry_*`）先接进来：`drain()` 已经把累加器清空，不接就等于丢了。
        重发是安全的——库里那一批并没有落进去，而 `upsert_summaries` 按主键合并。
        """
        rows = self._retry_rows + self.aggregator.drain()
        return _Job(rows=rows, events=self._transitions())

    def _transitions(self) -> list[dict[str, Any]]:
        """断流与恢复：只在"这条流还开着"的前提下说话。

        回来的是可直接 `SignalEvent(**row)` 的字典，`payload` 仍是字典而不是已拼好的 JSON 串：
        这一批要能原样在 `_retry_events` 里活到下一轮，也能在写成果之后直接进 `/ws/live`。
        """
        events = list(self._retry_events)
        claimed = {(e["gateway_id"], e["node_id"], e["kind"]) for e in events}
        now = monotonic()
        for endpoint in self._endpoints.values():
            if endpoint.last_frame_mono is None:
                continue
            node = self._node_for(endpoint)
            if node is None:
                continue  # 归不了属就没有可指的主体，也就没有可写的事件
            silent = now - endpoint.last_frame_mono > self.settings.capture_silence_s
            if silent == endpoint.silent:
                continue
            kind = "node_offline" if silent else "node_online"
            if (node.gateway_id, node.node_id, kind) in claimed:
                continue
            events.append(
                {
                    "ts": utcnow(),
                    "site_id": node.site_id,
                    "gateway_id": node.gateway_id,
                    "node_id": node.node_id,
                    "kind": kind,
                    "source": endpoint.source,
                    "payload": {
                        "ep": endpoint.ep,
                        "silence_s": round(now - endpoint.last_frame_mono, 3),
                        "last_seq": endpoint.last_seq,
                        "silence_budget_s": self.settings.capture_silence_s,
                    },
                }
            )
            claimed.add((node.gateway_id, node.node_id, kind))
            endpoint.silent = silent
        limit = self.settings.capture_event_limit
        if len(events) > limit:
            # 丢的是**最旧**的：断流这种事，最新那一条才是运营者要看的。
            self.events_dropped += len(events) - limit
            events = events[-limit:]
        return events

    def read_refs(self) -> dict[tuple[int, str], NodeRef]:
        """一次查询拿回全部节点行与场地归属。可以在任何线程里跑，只回一份新字典。"""
        with self.db.scope() as session:
            rows = session.execute(
                select(
                    Node.node_id,
                    Node.gateway_id,
                    Node.transport_addr,
                    Node.display_name,
                    Relay.site_id,
                )
                .join(Gateway, Node.gateway_id == Gateway.gateway_id)
                .join(Relay, Gateway.relay_id == Relay.relay_id)
            ).all()
        return {
            (gateway_id, addr): NodeRef(
                node_id=node_id,
                site_id=site_id,
                gateway_id=gateway_id,
                transport_addr=addr,
                display_name=display_name,
            )
            for node_id, gateway_id, addr, display_name, site_id in rows
        }

    def read_refs_safe(self) -> tuple[dict[tuple[int, str], NodeRef] | None, str | None]:
        """线程侧那一步的外壳：拉不动就回 `(None, 错误)`，由 `apply_refs` 决定沿用旧表。

        与 `BridgeEndpointResolver` 同一条纪律——一次抖动不该把已经认好的归属冲掉。
        """
        try:
            return self.read_refs(), None
        except Exception as exc:
            return None, f"{type(exc).__name__}: {exc}"

    def apply_refs(
        self, refs: dict[tuple[int, str], NodeRef] | None, ref_error: str | None = None
    ) -> None:
        """整份替换而不是逐键更新：`ingest` 会把消失的节点转 `lost`、把重插的板换成新
        `node_id`，留着旧行就等于把摘要写到一个已经不存在的节点上。

        `refs is None` 是上面那条读失败的路：这一轮沿用上一份表，只把失败次数加一。整轮放弃
        反而更糟——那等于"读归属失败 → 这一拍攒的摘要一行都不落"，而攒着的是别人的字节。
        """
        if ref_error is not None:
            self.node_table_failures += 1
            self.log.warning("capture node table refresh failed: %s", ref_error)
            return
        self._refs = refs if refs is not None else {}

    def resolve_addrs(self, keys: tuple[tuple[int, str], ...]) -> dict[tuple[int, str], str]:
        """线程侧：按网关找桥认领 `ep` → 地址。返回的只是"桥说了什么"，不改任何状态。"""
        by_gateway: dict[int, set[str]] = {}
        for gateway_id, ep in keys:
            by_gateway.setdefault(gateway_id, set()).add(ep)
        found: dict[tuple[int, str], str] = {}
        for gateway_id, eps in by_gateway.items():
            for ep, addr in self.resolver.addrs_for(gateway_id, tuple(sorted(eps))).items():
                found[(gateway_id, ep)] = addr
        return found

    def apply_addrs(self, addrs: dict[tuple[int, str], str]) -> None:
        for (gateway_id, ep), addr in addrs.items():
            endpoint = self._endpoints.get((gateway_id, ep))
            if endpoint is not None:
                endpoint.transport_addr = addr

    def attribute_pending(self) -> int:
        """把已经能归属的点交出去。只在循环上调，用的就是当前这份 `_refs`。

        走的是 `_emit` 那**唯一一条**分发路：迟到归属的帧也要在 `/ws/live` 上出现，
        否则"页刚连上那几秒没数"就变成了 WS 的现象，而现场最常见的连接长度就是几秒。
        """
        moved = 0
        for endpoint in self._endpoints.values():
            node = self._node_for(endpoint)
            if node is None:
                continue  # 还没认领上的**留着**等下一轮，这里一个点都不该丢
            moved += self._release(endpoint, node)
        return moved

    def write(self, job: _Job) -> _Job:
        """线程侧唯一的写：一条语句批量写格子，事件逐行插入，同一个事务里提交。

        同一事务是刻意的：摘要落了、事件没落（或反过来）会让"这块板安静了"与"它的数据停了"
        两件事互相矛盾，而那正是排查时最需要先对上的两条。

        这里**不碰 `job.events` 里的字典**，每行都复制一份再拼 `payload_json`：这批字典同时
        还是 `_retry_events` 的候选，线程改不动循环拥有的东西。主键因此不回写进 `events`，
        而是按同序另放一份 `job.event_ids`——发布那一步要的只是"这条已经落库、它的 id 是几"。
        """
        try:
            with self.db.scope() as session:
                if job.rows:
                    job.summary_written = retention.upsert_summaries(session, job.rows)
                written = []
                for row in job.events:
                    fields = {key: value for key, value in row.items() if key != "payload"}
                    fields["payload_json"] = json.dumps(row["payload"], ensure_ascii=False)
                    record = SignalEvent(**fields)
                    session.add(record)
                    written.append(record)
            # 提交之后再读主键：`scope()` 的 commit 会先 flush，这时 SQLite 才把自增 id 填回对象。
            job.event_ids = [record.event_id for record in written]
            job.error = None
        except Exception as exc:
            job.error = f"{type(exc).__name__}: {exc}"
        return job

    def _publish_event(self, row: dict[str, Any], event_id: int) -> None:
        """行已经落库之后才对外说"出事了"。

        反过来的话，页面会先收到一条"这块板安静了"，而 `GET /api/events` 里查不到它——
        写失败重试那一轮尤其明显，因为那时行还没落。

        外层那个 `kind` 是**帧类型**，与 `samples`、`snapshot` 共用一张词表；事件自己的类型在
        `event.kind`（`node_offline` 那一个）。这两层意思挤在同一个键上出过一次事：早先写的是
        `{"kind": "event", **row}`，`row` 里的 `kind` 把帧类型盖掉，于是 `LiveHub` 的
        `kind == "event"` 永不成立、快照的事件窗永远是空的——分桶投递照发，只有"刚连上的那一页
        第一眼"少了它，而页面上看着只是"事件还没到"。现在事件对象与 REST 那一行同形
        （`event_id`/`kind`/`source`/`payload`/`ts` 都在），页面才敢把两条到达路径当同一条记录。
        """
        if self.publisher is None:
            return
        event = {**row, "event_id": event_id, "ts": row["ts"].isoformat()}
        try:
            readers = self.publisher.publish(
                row["site_id"],
                {"kind": "event", "site_id": row["site_id"], "ts": event["ts"], "event": event},
            )
        except Exception as exc:  # 与样本同一条纪律：发布口炸了不影响已经落库的事实
            self.publisher_errors += 1
            self.log.warning("live event publish failed: %s", exc)
            return
        self.events_published += 1
        self.published_readers += readers

    def settle(self, job: _Job) -> None:
        """结算这一轮：成功就清干净并把事件发出去，失败就把这批原样留给下一轮。"""
        if job.error is not None:
            self.flush_errors += 1
            self.last_error = job.error
            self._retry_rows = job.rows
            self._retry_events = job.events
            self.log.warning("capture flush failed: %s", self.last_error)
            return
        self.ticks += 1
        self.events_written += len(job.events)
        self.last_flush_at = utcnow()
        self.last_error = None
        self._retry_rows = []
        self._retry_events = []
        for row, event_id in zip(job.events, job.event_ids):
            self._publish_event(row, event_id)

    def pending_keys(self) -> tuple[tuple[int, str], ...]:
        """还需要"谁是谁"的那些端点：只有攒了待归属点、又还不知道地址的才值得去问桥。"""
        return tuple(
            (endpoint.gateway_id, endpoint.ep)
            for endpoint in self._endpoints.values()
            if endpoint.pending and endpoint.transport_addr is None
        )

    async def flush_cycle(self) -> dict[str, Any]:
        """`tick()` 的事件循环形态：两处 I/O 交线程，其余几步与 `tick()` 同一批方法、同一顺序。

        分成两个形态的理由只有一个：周期节拍跑的时候 `_worker` 正在同一条循环上改
        `_endpoints`/`aggregator`/`store`，所以**这些步必须在循环上调**；把它们整块丢进
        线程池就等于让两个线程同时写同一批结构，撕出来的样子是"偶发少几行摘要"。
        """
        loop = asyncio.get_running_loop()
        refs, ref_error = await loop.run_in_executor(None, self.read_refs_safe)
        self.apply_refs(refs, ref_error)
        keys = self.pending_keys()
        self.apply_addrs(await loop.run_in_executor(None, self.resolve_addrs, keys))
        attributed = self.attribute_pending()
        job = self.prepare()
        self.settle(await loop.run_in_executor(None, self.write, job))
        return {
            "attributed": attributed,
            "summaries": job.summary_written,
            "events": len(job.events) if job.error is None else 0,
            "error": job.error,
        }

    def tick(self) -> dict[str, Any]:
        """一个完整节拍：读归属 → 认领地址 → 出仓待归属 → 写摘要与事件。

        顺序是有意的。先弄清楚"这块板是谁"，再决定哪些点能进摘要——反过来会出现
        "新登记的节点第一轮被跳过、而断流判定已经把它算成安静"那种自相矛盾的一轮。

        名字里带 `read_/apply_/resolve_/settle` 的那几步是刻意分开的：周期节拍走
        `flush_cycle()`，它把这几步**用 await 串起来**，从而保证"改内存的那几行永远在事件
        循环上跑、碰库的那几行永远在线程上跑"。`tick()` 是把同一串步骤一口气同步跑完的形态，
        只适用于没有 worker 在跑的场合——测试与关停收尾。
        """
        refs, ref_error = self.read_refs_safe()
        self.apply_refs(refs, ref_error)
        self.apply_addrs(self.resolve_addrs(self.pending_keys()))
        attributed = self.attribute_pending()
        job = self.prepare()
        self.write(job)
        self.settle(job)
        return {
            "attributed": attributed,
            "summaries": job.summary_written,
            "events": len(job.events) if job.error is None else 0,
            "error": job.error,
        }

    # --- 对外呈现 -------------------------------------------------------------
    def problem(self) -> str | None:
        """只有"上一轮真的没写成"才说话。

        没启用、汇聚角色、暂时没页连着板——三种都不是故障。把可选件的缺失报成 degraded，
        代价是运营者开始忽略 degraded（`dialer`/`retention` 同一条纪律）。
        """
        if self.last_error is None:
            return None
        return f"采集落库失败：{self.last_error}（累计 {self.flush_errors} 轮）"

    def status(self) -> dict[str, Any]:
        """纯内存快照 + 上一轮的事实。健康检查每 2 s 读一次，所以这里不开库、不 join。

        **这段是在另一个线程里跑的**（`/api/health` 是同步端点，FastAPI 把它丢进线程池）。
        所以凡是"循环那边还在改的容器"，这里只读两类东西：`len()`，和循环侧**整份换掉**的
        快照字典（`_queue_stats`/`_skipped_snapshot`——发布之后绝不原地改）。
        直接迭代在变的字典会偶发 `dictionary changed size during iteration`，而那正好发生在
        "板子很多、有人在盯着健康看"的时候——把一个 500 留在最该说得清的时刻，比少几个字段糟得多。
        """
        return {
            "enabled": self.enabled,
            "running": self.running,
            "refused": "aggregator_role" if self.role == "aggregator" else None,
            "flush_interval_s": self.settings.capture_flush_interval_s,
            "silence_s": self.settings.capture_silence_s,
            "consumers": self.settings.capture_consumer_limit,
            "gateways": len(self._queues),
            "gateways_refused": self.gateways_refused,
            "endpoints": len(self._endpoints),
            "queues": list(self._queue_stats),
            "dropped_chunks": self.dropped_chunks,
            "frames": self.frames,
            "text_lines": self.text_lines,
            "skipped_frames": self._skipped_snapshot,
            "crc_errors": self.crc_errors,
            "resync_bytes": self.resync_bytes,
            "observer_errors": self.observer_errors,
            "points_stored": self.points_stored,
            "slots_out_of_range": self.slots_out_of_range,
            "unattributed": self.unattributed,
            "pending_points": self.pending_points,
            "pending_dropped": self.pending_dropped,
            "attributed_late": self.attributed_late,
            "node_table_failures": self.node_table_failures,
            "published": self.published,
            "events_published": self.events_published,
            "published_readers": self.published_readers,
            "publisher_errors": self.publisher_errors,
            "nodes": len(self._refs),
            "ticks": self.ticks,
            "flush_errors": self.flush_errors,
            "retry_rows": len(self._retry_rows),
            "last_flush_at": self.last_flush_at,
            "last_error": self.last_error,
            "events_written": self.events_written,
            "events_dropped": self.events_dropped,
            "resolver": {
                "fetches": getattr(self.resolver, "fetches", None),
                "failures": getattr(self.resolver, "failures", None),
                "last_error": getattr(self.resolver, "last_error", None),
            },
            "summary": self.aggregator.stats(),
            "ring": self.store.stats(),
            "problem": self.problem(),
        }


def _point(
    channel: dict[str, Any], decoded: dict[str, Any], moment: datetime, source: str
) -> dict[str, Any]:
    """环里的一格 = 解析器给的那个频道对象 + 观测时刻 + 来源。

    刻意不做字段挑选：`channel` 的键就是 `frames.ts` 那一份（`rssi/rssiEma/floor/snr/
    clearFloor/peak/peakOffHz/centroidOffHz/obwLoHz/obwHiHz/flags`），页面读什么这里就有什么。
    槽位用解析器的原词 `id`，不另起一个 `slot` 别名——同一件事两份名字早晚对不上。
    唯一的例外是 `ema`：`sample_summary` 的两列叫 `rssi_*`/`ema_*`，累加器也按这两个词收，
    所以在这里给 `rssiEma` 一个与库表同名的别名，让出仓那段不必再懂帧协议的字段名。
    """
    return {
        **channel,
        "ema": channel["rssiEma"],
        "ts": moment,
        "seq": decoded["seq"],
        "tMs": decoded["tMs"],
        "mode": decoded["mode"],
        "loKhz": decoded["loKhz"],
        "saturated": decoded["saturated"],
        "source": source,
    }
