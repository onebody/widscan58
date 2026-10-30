"""插件运行时：注册表 → 每 (插件, 网关) 一条有界队列 → 消费 → 判定落库。

分工决定了形状：`services/registry.py` 只管"库里该有哪几行"，`plugins/*` 只管"给定一帧该不该
说话"，这一层管**中间那截运输**——投递、排队、按场地取生效配置、写库、把运行态落进
`plugin_state`。三层能各自独立测，靠的是它们之间只有 `FrameBatch` / `Finding` 两个数据形状和
一个 `feed()` 调用。

四条纪律，每条都对着一个会坏在现场的场景：

1. **投递侧不碰库**。`offer()` 由采集层在事件循环上每**窗**调一次（一窗 = 同一 (网关, 节点)
   在一个时间窗内的帧列，攒窗在采集层），所以它只读内存里那份由 `refresh()` 整份换过的启用快照；
   注册表那一行改了没有，等下一轮 `run_once()` 再说。这是采集层"循环上一个字节的 SQL 都不做"
   那条纪律的插件侧版本。
2. **没有任何一支启用插件时，`offer()` 一个字节都不攒**。回滚判据"全部 `enabled=0` 即等效不
   存在"要靠这一条成立——关掉的插件继续攒队列，Pi（可用内存约 460 MB，方案 §5）上表现为"内存
   一直在涨，而插件页说它没在跑"，那与"插件有 bug"长成同一个样子。
3. **队列条数有硬闸**：每支插件最多 `settings.max_gateways` 条，超限**不新建**并计数。理由与
   `store.py` 的接入闸同一条：静默多开就把"总并发有硬闸"那句话作废了。
4. **判定行的身份由运行时盖**：`site_id`/`gateway_id`/`node_id`/`source`/`ts` 全部取自
   `FrameBatch`，`producer` 由注册表那一行的 id 与版本拼出来。插件既看不到、也改不了这两样。

消费侧的失败处理服从同一个总前提：**插件坏了不能拖垮采集**（规范 §10.4）——所以 `feed()` 抛出
的任何异常都只进这一层自己的计数与流水，绝不冒泡回投递方。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select

from ..config import Settings
from ..database import Database
from ..models import (
    PluginFinding,
    PluginLog,
    PluginRegistry,
    PluginState,
    Site,
    to_utc,
    utcnow,
)
from ..plugins import IMPLEMENTATIONS
from ..plugins.base import FrameBatch
from . import config_layer
from .instance import effective_instance_role
from .store import GatewayQueue

LOG = logging.getLogger("rftrack.plugins")

#: 作用域里今天真的有执行者的那几档。`relay` 在 schema 里是合法字符串（`plugin_registry.scope`
#: 的多态指向没建外键，所以库里写得进），但批上没有位置身份，判它要查库——见 `_Run.claims`。
#: 写侧按这份清单拒（422 `scope_unsupported`），读侧按它标 `executable`，两边同一份事实源。
EXECUTABLE_SCOPES = ("global", "site", "gateway")

#: 一轮里每条队列最多搬多少批。搬运必须有顶：一条打满的队列若不限量，`run_once()` 就可能长时间
#: 占着线程池里那根线程，而同一个池正被采集层的落库用着。
#: "批"自 2026-10-30 起是**一个窗口**（同一节点 ≤250 ms 的帧列，见 capture.py 的 `FEED_WINDOW_S`），
#: 不再是一帧——真机 236 帧/s 对每拍 32 帧，投递侧丢 87.5%；改窗口后每拍 32 窗对每秒约 4 窗。
DRAIN_CHUNK = 32

# --- supervisor 的节奏 -------------------------------------------------------
#: 崩溃后重启的退避：1 s 起、每次翻倍。与 `dial_backoff_*` 同一条理由——"崩—重启"的频率必须由
#: 这几个数决定，而不是靠散在各处的 sleep；真值待 S9 在 Pi 上压测。
BACKOFF_BASE_S = 1.0
BACKOFF_MAX_S = 30.0
#: 连续重启到几次就放弃（停在 `failing` 不再自己试，直到用户重新启用）。上限 3 是方案 §4
#: 那张状态机图里写死的数字："一直崩的东西自己重启四次还是崩，只是在写日志"。
RESTART_LIMIT = 3

#: 后台循环的节拍。1 s 是"网页上启用一支插件后，多久该在第一块板上看到判定"的体感要求，
#: 也是最坏情况下队列的驻留时间上界；真值待 S9 在 Pi 上压测（每轮固定两次 SELECT）。
TICK_INTERVAL_S = 1.0

#: 每场地的生效配置在内存里留多久。取一次要把 `KEYS` 里所有可覆写的键折一遍层，每帧都折一遍既
#: 没道理也贵；5 s 是"改了阈值几秒内生效"与"热路径不查库"之间的折中，**不是实测值**。
CONFIG_TTL_S = 5.0


def _backoff_s(restarts: int) -> float:
    """已经重启过 N 次之后，下一次崩溃要等多久：1 s、2 s、4 s……封顶 30 s。

    翻倍而不是固定值，与 `dial_backoff_base_s`/`_max_s` 同一条理由：一件每次都坏的东西，
    按固定节奏重启就是在给自己制造日志，而它坏着的时间可能远比重启一次长。
    """
    return min(BACKOFF_BASE_S * (2 ** restarts), BACKOFF_MAX_S)


@dataclass
class _Run:
    """一支**启用中的**插件的运行态：实例 + 每网关一条队列 + 它读的那几把键。"""

    plugin_id: str
    version: str
    instance: Any
    keys: tuple[str, ...]
    queue_limit: int
    #: 队列条数受 `max_gateways` 管，超限就不再新建（拒绝的那几次记在运行时那层的
    #: `batches_refused`）——见模块注释第 3 条。
    queues: dict[int, GatewayQueue] = field(default_factory=dict)
    #: 作用域：注册表那两列的原样。它筛的是"哪些批进我的队列"，与阈值按场地分层是两件事。
    scope: str = "global"
    scope_id: int | None = None
    batches: int = 0
    findings: int = 0
    last_error: str | None = None

    def claims(self, batch: Any) -> bool:
        """这一批归不归这支插件管。只读 `FrameBatch` 上已经带着的身份，**不查库**。

        `relay` 那一档今天执行不了：批上没有位置身份，要判就得每次做一次网关→位置映射，那正好
        把投递热路径变成会读库的那一步（模块注释第 1 条）。写侧在 `routers/plugins.py` 就拒它，
        这里再兜一层是因为历史库里可能已经躺着那样一行——认不了就当它谁都不看，比悄悄当全局跑
        诚实（配错该表现为"它不说话了"）。
        """
        if self.scope == "global":
            return True
        if self.scope == "site":
            return batch.site_id == self.scope_id
        if self.scope == "gateway":
            return batch.gateway_id == self.scope_id
        return False

    def queue_for(self, gateway_id: int, max_gateways: int) -> GatewayQueue | None:
        queue = self.queues.get(gateway_id)
        if queue is not None:
            return queue
        if len(self.queues) >= max_gateways:
            return None
        queue = GatewayQueue(gateway_id, self.queue_limit)
        self.queues[gateway_id] = queue
        return queue


class PluginRuntime:
    """把采集层交来的批推到插件手里，再把插件的判定写进库。

    起合方式与 `RetentionRunner`/`CapturePipeline` 一致（lifespan 里 `start()`/`stop()`），
    而同步的 `run_once()` 是**给测试与手动触发用的那一个口子**：后台循环只是"到点调它"，所以
    它的行为必须与测试里手工调它一模一样——否则测试绿的那条路径在生产上根本没跑过。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        *,
        implementations: dict[str, Any] | None = None,
        log=LOG,
    ) -> None:
        self.db = db
        self.settings = settings
        #: 注入位是给测试用的（要放一支会崩的插件进去）；生产就是 `plugins.IMPLEMENTATIONS`。
        self.implementations = IMPLEMENTATIONS if implementations is None else implementations
        self.log = log
        #: 实例属性而不是常量，是为了让生命周期测试能把等待压到毫秒级（`RetentionRunner` 同法）。
        self.tick_interval_s = TICK_INTERVAL_S
        self._runs: dict[str, _Run] = {}
        self._configs: dict[int, tuple[dict[str, Any], float]] = {}
        #: 启用中且**这一轮崩过**的插件：`plugin_id -> (错误文本, 还会不会自己重试)`。
        #: 它是 `plugin_state.status='failing'` 的内存镜像，不是第二份事实：只在 `_write` 已经
        #: 把库里那一行写成 `failing` 的地方跟着写，退避成功跑干净一轮就跟着销。为什么要镜像——
        #: `status()` 不许开库（健康检查在另一个线程），而 `problem()` 只看 `last_error` 的话，
        #: "它坏了并且不再试了"与"它好了"长得一模一样（2026-10-30 真机两支永久 `failing`、
        #: 11 次轮询全 `ok` 就是这个形状）。
        self._failing: dict[str, tuple[str, bool]] = {}
        #: 只读快照，`offer()`/`run_once()` 换、`status()` 读——与 `CapturePipeline._queue_stats`
        #: 同一条理由：健康检查在另一个线程里迭代它。
        self._queue_stats: tuple[dict[str, Any], ...] = ()
        self.batches_offered = 0
        #: 帧数另记一本：批改成窗口后，"offered vs fed"要用**同一单位**才说得出"一帧都没丢"。
        #: 批（窗口）的账回答运输层挤不挤，帧的账回答判定层有没有看见每一条读数——两个都看。
        self.frames_offered = 0
        self.frames_fed = 0
        self.dropped_batches = 0
        #: 按 (插件, 批) 记：某支插件的队列条数撞上 `max_gateways` 闸、这批没交给它。可以大于
        #: `batches_offered`（三支一起拒就是三次）。队列满是"丢了最旧那批"，这是"这台没排上队"，
        #: 页面上要说的话不同，所以不并进 `dropped_batches`（那个说的是"队列满了，丢的是最旧那批"）。
        self.batches_refused = 0
        #: 交进来但没有任何一支启用插件的作用域认它。与 `dropped_batches`（队列满）分开记：
        #: 那说的是容量，这说的是"这批根本不该给它"——混在一起，页面就会把配错作用域读成过载。
        self.batches_out_of_scope = 0
        self.batches_lost = 0
        self.findings_written = 0
        self.refreshes = 0
        self.errors = 0
        self.last_error: str | None = None
        self.last_run_at: datetime | None = None
        self.running = False
        self.role: str | None = None
        self._stopping: Any = None
        self._task: Any = None

    # --- 生命周期 --------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.plugins_enabled

    async def start(self) -> None:
        """按开关与角色起循环：汇聚实例直接不启动（那一台没有桥，也没有本地产出可推）。

        `plugins_enabled=0` 时**一个字节的库都不读**就返回：S7 的回滚判据是"关掉之后采集链路与
        插件上线前一模一样"，而现场能做的只有改环境变量重启。这种情况不算故障，所以 `problem()`
        在这里回 `None`（与 `capture`/`retention` 同口径）。

        起之前先 `refresh()` 一次，而且走线程：否则从 `start()` 到第一拍 `run_once()` 之间那
        `tick_interval_s` 秒里 `_runs` 是空的，采集侧的 `offer()` 一律回 `disabled`——而刚启动
        那一段恰恰是最想看的"板子活了吗"。
        """
        if not self.enabled:
            return
        loop = asyncio.get_running_loop()
        self.role = await loop.run_in_executor(None, self._read_role)
        if self.role == "aggregator":
            return
        await loop.run_in_executor(None, self.refresh)
        self._stopping = asyncio.Event()
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="plugin-runtime")

    async def stop(self) -> None:
        """收摊：先停循环，再把状态落回 `stopped`。顺序反了会出现"已经写了 stopped、
        循环又跑了一轮"，那一轮把状态改回 running 而进程紧接着就没了。
        """
        if self._stopping is not None:
            self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        was_running = self.running
        self.running = False
        if was_running:
            await asyncio.get_running_loop().run_in_executor(None, self._mark_stopped)

    def _read_role(self) -> str:
        with self.db.scope() as session:
            return effective_instance_role(session, self.settings.instance_role)

    async def _loop(self) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_interval_s)
                return
            except asyncio.TimeoutError:
                pass
            try:
                # 消费是同步 SQL + 同步规则计算，放线程里跑：一支插件在一批上要算几十毫秒，
                # 留在循环上就是 `/ws/live` 与 `/ws/bridge` 一起卡住。
                await asyncio.get_running_loop().run_in_executor(None, self.run_once)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # 一轮失败绝不终止节拍（与 retention/capture 同一条纪律）
                # 失败也要留下数字：`run_once()` 自己会吃掉规则与写库的失败并计数，能冒到这里的
                # 是它**开始前**那一步（`refresh()` 读不到注册表）——那种时候一支插件都没被喂，
                # 只写日志的话，页面上剩下的现象就只有"插件不产出"，与"规则挑得不对"同一个样子。
                self.errors += 1
                self.last_error = f"{type(exc).__name__}: {exc}"
                self.log.warning("plugin runtime tick failed: %s", exc)

    def _mark_stopped(self) -> None:
        """把**这个运行时**在跑的那几支落回 `stopped`，`failing` 的原样留着。

        留着是有意的：崩溃是这支插件自己的事实，不是"这个进程结束了"就能一笔勾销的东西。
        把它写成 stopped 等于把排查现场清掉，而重新启用的入口（S7-6 的 `POST /enable`）本来就
        负责复位。
        """
        with self.db.scope() as session:
            for plugin_id in self._runs:
                state = session.get(PluginState, plugin_id)
                if state is None or state.status in ("stopped", "failing"):
                    continue
                state.status = "stopped"
                state.updated_at = utcnow()
                session.add(
                    PluginLog(
                        plugin_id=plugin_id,
                        level="info",
                        event="stopped",
                        message="运行时收摊，这支插件已停止消费。",
                    )
                )

    # --- 启用快照 --------------------------------------------------------------

    def refresh(self, now: datetime | None = None) -> dict[str, _Run]:
        """按注册表换一次"该投喂谁"。启用、停用、改队列上限、改作用域都在这一步生效。

        仍然启用的那支**沿用同一个实例与同一批队列**：每轮重建会把 `anomaly-detector` 的基线
        清空，表现出来是"它每隔几秒就重新安静一次"。停用的那支整个丢掉（连同它内存里的状态），
        这是刻意的——重新启用意味着"从现在这一刻开始看"，不是"接着上次没说完的话"。

        参数变了（容量或作用域）则是第三种情形：**队列必须换**（`asyncio.Queue` 的 maxsize 建好
        就不能改，而作用域变了之后旧队列里那些批是在旧筛子下交进来的），**实例不能换**（用户改的
        是运输参数，不是"重新认识这块板"）。所以这里换 `_Run` 而把 `instance` 原样带过去，并先把
        旧队列按旧参数搬完——`PUT /api/plugins/{id}/config` 从此不会顺手吞数据。
        """
        with self.db.scope() as session:
            rows = list(
                session.execute(
                    select(PluginRegistry).where(PluginRegistry.enabled.is_(True))
                ).scalars().all()
            )
        runs: dict[str, _Run] = {}
        moment = now or utcnow()
        for row in rows:
            implementation = self.implementations.get(row.plugin_id)
            if implementation is None:
                continue  # 没有实现的行由 `registry.sync()` 记 warning，这里只管跑得起的
            limit = row.queue_limit or self.settings.plugin_queue_limit
            params = (limit, row.scope, row.scope_id)
            previous = self._runs.get(row.plugin_id)
            if previous is None:
                runs[row.plugin_id] = _Run(
                    plugin_id=row.plugin_id,
                    version=row.version,
                    instance=implementation(),
                    keys=tuple(getattr(implementation, "CONFIG_KEYS", ())),
                    queue_limit=limit,
                    scope=row.scope,
                    scope_id=row.scope_id,
                )
                continue
            if (previous.queue_limit, previous.scope, previous.scope_id) == params:
                runs[row.plugin_id] = previous
                continue
            # 参数变了。旧队列里已经交进来的批要按**旧参数**搬完（它们是在旧作用域下交进来的），
            # 而实例原样交给新运行态——改了个容量数字不该把 `anomaly-detector` 的基线清零。
            lost = self._retire({row.plugin_id: previous}, moment)[row.plugin_id]
            runs[row.plugin_id] = replace(
                previous,
                queue_limit=limit,
                scope=row.scope,
                scope_id=row.scope_id,
                queues={},
            )
            if lost:
                self._note_reconfigured(row.plugin_id, lost)
        gone = {
            plugin_id: previous
            for plugin_id, previous in self._runs.items()
            if plugin_id not in runs
        }
        self._runs = runs
        self.refreshes += 1
        # 停用就是复位现场：镜像跟着库里那一行回 `stopped`，不留着一句"它坏着"给已经不管它的页面看。
        for plugin_id in gone:
            self._failing.pop(plugin_id, None)
        self._publish_queues()
        if gone:
            self._mark_disabled(gone, self._retire(gone, moment))
        return runs

    def _retire(self, gone: dict[str, _Run], moment: datetime) -> dict[str, int]:
        """摘掉的那几支先把队列里已经交进来的批搬完，返回每支**没能落成判定**的批数。

        「停用的插件队列必须排空且不丢计数」这条落在这里而不是落在端点上：端点只改库里那一行，
        而批都在内存里。搬完的照常落库（判定与计数一视同仁），搬不动的那些进流水。

        轮数按 `queue_limit / DRAIN_CHUNK` 顶到最坏，不是"多试几次更稳"：一支插件的队列深度有硬闸，
        排空所需轮数是可算的；算完还没空就说明这些轮里都有失败，再试也只是多写几条流水。
        """
        discarded: dict[str, int] = {}
        for plugin_id, run in gone.items():
            rounds = 1 + (run.queue_limit + DRAIN_CHUNK - 1) // DRAIN_CHUNK
            lost = 0
            for _ in range(rounds):
                if not any(queue.depth for queue in run.queues.values()):
                    break
                before = self.batches_lost
                self._consume(run, moment)
                lost += self.batches_lost - before
            discarded[plugin_id] = lost + sum(queue.depth for queue in run.queues.values())
        return discarded

    def _mark_disabled(self, gone: dict[str, _Run], discarded: dict[str, int]) -> None:
        """被摘掉的那几支落回 `stopped`，并留一条 `disabled` 流水（带上没搬完的批数）。

        状态必须跟着"它不再被消费"这个事实，而不是跟着按钮：库里那一行可能是别的页面改的，运行时
        看得见的唯一真相就是它已经不在这份快照里了。这里覆盖 `failing` 是有意的——用户点名停用，
        那就是复位现场的那个动作（与 `stop()` 不覆盖 `failing` 正好互为边界）。

        本来就已停用的行没有再报一次的必要，**除非**它吞下了批：那种情况下"停用"这个动作确实
        让数据没了，而那正是这条流水要留下的东西。

        队列里没搬完的批次随实例一起消失，但**必须留下数字**：`dropped` 那一列的语义是"队列满时
        丢的最旧批次数"（`PluginRegistry.queue_limit` 的注释），把停用时的丢弃混进去等于改掉一列
        的意思，所以它进流水的 `detail_json`。
        """
        with self.db.scope() as session:
            for plugin_id, run in gone.items():
                state = session.get(PluginState, plugin_id)
                left = discarded.get(plugin_id, 0)
                if state is None or (state.status == "stopped" and not left):
                    continue
                state.status = "stopped"
                state.stats_json = json.dumps(
                    {
                        str(gateway_id): {
                            "depth": queue.depth,
                            "peak": queue.peak,
                            "dropped": queue.dropped,
                            "limit": queue.limit,
                        }
                        for gateway_id, queue in run.queues.items()
                    },
                    ensure_ascii=False,
                )
                state.updated_at = utcnow()
                session.add(
                    PluginLog(
                        plugin_id=plugin_id,
                        level="info",
                        event="disabled",
                        message="注册表里那一行不再启用，运行时已停止消费。",
                        detail_json=json.dumps({"discarded_batches": left}),
                    )
                )

    def _note_reconfigured(self, plugin_id: str, lost: int) -> None:
        """改参数（队列上限/作用域）吞掉的批：留一条 `reconfigured`，说清吞了几批。

        只在真吞了东西时才写。参数本身当前是什么，读注册表那一行就知道，不值得为它单独记一条流水
        （`plugin_log` 的纪律是"只记事件"）；而"因为你改了上限，那 12 批没能落成判定"是只有这里
        说得出来的事。
        """
        with self.db.scope() as session:
            session.add(
                PluginLog(
                    plugin_id=plugin_id,
                    level="warning",
                    event="reconfigured",
                    message="改参数换掉了队列，旧队列里没搬完的批次已丢弃。",
                    detail_json=json.dumps({"discarded_batches": lost}),
                )
            )

    def _publish_queues(self) -> None:
        self._queue_stats = tuple(
            {**queue.stats(), "plugin_id": plugin_id}
            for plugin_id, run in self._runs.items()
            for queue in run.queues.values()
        )

    # --- 投递（采集层的热路径）--------------------------------------------------

    @property
    def armed(self) -> bool:
        """现在有没有"至少一支启用插件"值得为它造一批。

        采集侧每帧问一次。它读的是 `refresh()` 整份换掉的那个引用（与 `_queue_stats` 同一条
        纪律：旧的不原地改），所以这个判断在两个线程之间不会撕裂。
        """
        return bool(self._runs)

    def offer(self, batches: Any) -> str:
        """一窗已归属的帧（或单个 `FrameBatch`，视作一帧的窗口——测试直接喂运行时用这个形态）。
        非阻塞、不碰库、绝不抛异常。

        返回值说的是投递这件事本身：`disabled`（没有可投的对象）、`out_of_scope`（启用的插件都
        不认这块板属于它的作用域）、`refused`（这条网关撞上了队列条数硬闸）、`dropped_oldest`
        （至少一条队列满了，丢的是最旧那批）、`queued`。多支插件各自的队列可能给出不同答案，
        所以这个串只取"最该被看到的那一件"，而每条队列的精确数字在 `stats()` 里——一个字符串
        说不出六条队列的情况，硬要它说就会说谎。

        一窗帧共享同一身份（同一 (网关, 节点) 才攒得进一个窗），所以作用域与队列条数硬闸
        按首帧判一次；队列条目是整窗，`dropped_oldest` 丢的也是整窗。
        """
        if isinstance(batches, FrameBatch):
            batches = (batches,)
        if not self._runs:
            return "disabled"
        self.batches_offered += 1
        self.frames_offered += len(batches)
        dropped = False
        accepted = refused = skipped = 0
        first = batches[0]
        for run in self._runs.values():
            if not run.claims(first):
                skipped += 1
                continue
            queue = run.queue_for(first.gateway_id, self.settings.max_gateways)
            if queue is None:
                refused += 1
                continue
            accepted += 1
            if queue.offer(tuple(batches)) == "dropped_oldest":
                dropped = True
        self._publish_queues()
        self.batches_refused += refused
        if dropped:
            self.dropped_batches += 1
            return "dropped_oldest"
        if accepted == 0 and refused:
            return "refused"
        if accepted == 0 and skipped:
            self.batches_out_of_scope += 1
            return "out_of_scope"
        return "queued"

    # --- 消费一轮 --------------------------------------------------------------

    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        """搬一批（每队最多 `DRAIN_CHUNK`）→ 喂插件 → 判定与状态同一个事务落库。

        插件抛出来的异常在这里被吃掉：一支插件坏了不该让别的插件不跑，更不该让采集停。吃掉的前提
        是它得留下数字与错误文本，否则"它不说话了"就只能靠猜（规范 §10.4）。
        """
        self.refresh()
        moment = now or utcnow()
        written = errors = batches = 0
        errors_before = self.errors
        for run in list(self._runs.values()):
            if not self._may_run(run, moment):
                continue
            result = self._consume(run, moment)
            batches += result["batches"]
            written += result["findings"]
            errors += result["errors"]
        self.last_run_at = utcnow()
        self.findings_written += written
        self.errors += errors
        # "现在还坏着吗"要由**这一轮**回答，而 `_consume` 里运行时侧的失败是自己加进
        # `self.errors` 的：只看本轮规则失败数会把那种情况清成"好了"，而它恰恰是最需要留着的那句。
        if not errors and self.errors == errors_before:
            self.last_error = None
        self._publish_queues()
        return {"written": written, "batches": batches, "errors": errors, "plugins": len(self._runs)}

    def _may_run(self, run: _Run, moment: datetime) -> bool:
        """supervisor 的那道闸：这一轮到底能不能喂它。真回 False 时**一个字节都不搬**。

        `failing` 有两种，现象一样但含义相反：`next_retry_at` 是 None 表示"退避用完了，等用户
        重新启用"，有值表示"还没到点"。两种都必须停下来，否则每轮一次崩—重启会按节拍往
        `plugin_log` 里写行——那正是 R11 在 SD 卡上要防的写放大，而制造它的恰好是"为了修复"。
        """
        with self.db.scope() as session:
            state = session.get(PluginState, run.plugin_id)
        if state is None or state.status != "failing":
            return True
        if state.next_retry_at is None:
            return False
        if to_utc(state.next_retry_at) > moment:
            return False
        self._restart(run, state, moment)
        return True

    def _restart(self, run: _Run, state: PluginState, moment: datetime) -> None:
        """到点重启：换一个新实例，并把"重启了第几次"落进流水。

        换实例不是洁癖：崩在规则里的那份状态（比如一个永远凑不齐的窗口）会跟着旧实例活下来，
        于是"重启"变成"原地再崩一次"，而页面上只多出几条 `crashed`。
        """
        run.instance = self.implementations[run.plugin_id]()
        run.last_error = None
        with self.db.scope() as session:
            current = session.get(PluginState, run.plugin_id)
            current.status = "starting"
            current.restarts += 1
            current.updated_at = moment
            session.add(
                PluginLog(
                    plugin_id=run.plugin_id,
                    level="warning",
                    event="restarting",
                    message=f"第 {current.restarts} 次退避重启。",
                )
            )

    def _consume(self, run: _Run, moment: datetime) -> dict[str, int]:
        """一支插件这一轮的消费。返回这一轮的批数、判定数与失败数（都是增量，不是累计）。

        **两类失败分开记**，因为它们的修法完全不同：`feed()` 抛的是规则自己的毛病（记在
        `plugin_state` 上、进退避重启），而取生效配置与写判定抛的是"这台库现在读不动"——后者
        要是也记到插件头上，一次 `database is locked` 就会让三支好插件一起进入退避，而运营者会
        去查规则，什么也查不出来。运行时侧的失败只进自己的计数与 `problem()`。

        搬出队列之后才失败的那些批次**回不去**：塞回队尾会把时间序打乱，而三支规则插件判的都
        是先后（基线、窗口、抑制）。所以它们是丢了的，必须数得出来——不数它，"读库失败一轮"在
        页面上的表现就只是"这一轮插件没产出"，那与"插件坏了"长得一模一样。
        """
        rows: list[dict[str, Any]] = []
        batches = failed = 0
        for queue in list(run.queues.values()):
            for _ in range(DRAIN_CHUNK):
                try:
                    window = queue.get_nowait()
                except asyncio.QueueEmpty:
                    break  # 这条队搬空了
                batches += 1
                try:
                    config = self._config_for(window[0].site_id, run.keys)
                except Exception as exc:
                    return self._lose(run, moment, batches, exc, rows=len(rows))
                crashed = False
                for batch in window:
                    try:
                        findings = run.instance.feed(batch, config)
                    except Exception as exc:
                        failed += 1
                        self._note_error(run, exc)
                        crashed = True
                        break  # 这一窗不再喂：坏状态喂第二帧只会崩在更远的地方
                    self.frames_fed += 1
                    for finding in findings or ():
                        rows.append(self._finding_row(run, batch, finding))
                if crashed:
                    break  # 同一轮不再喂这条队列——旧口径原样保留，只是单位从帧换成了窗
        try:
            self._write(run, rows, moment, batches=batches, findings=len(rows), errors=failed)
        except Exception as exc:
            # `_write` 里已经开过事务，抛出来就是这一轮一行都没落成：判定是推导物，重放一遍
            # 队列也没有了，所以同样只数进 `batches_lost`，不冒充"已写"。
            return self._lose(run, moment, batches, exc, rows=len(rows))
        return {"batches": batches, "findings": len(rows), "errors": failed}

    def _lose(
        self, run: _Run, moment: datetime, batches: int, exc: Exception, *, rows: int = 0
    ) -> dict[str, int]:
        """运行时侧的失败：记在运行时头上，一支插件的状态字段都不碰。"""
        self.batches_lost += batches
        self.errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        self.log.warning(
            "plugin runtime failed while consuming %s: %s（丢掉 %d 批、%d 条判定）",
            run.plugin_id,
            exc,
            batches,
            rows,
        )
        return {"batches": batches, "findings": 0, "errors": 0, "lost": batches}

    def _finding_row(self, run: _Run, batch: Any, finding: Any) -> dict[str, Any]:
        """判定行 = 插件说的话 + 运行时盖的身份。身份那一截**只从 batch 取**，见模块注释第 4 条。"""
        return {
            "ts": batch.ts,
            "site_id": batch.site_id,
            "gateway_id": batch.gateway_id,
            "node_id": batch.node_id,
            "slot": finding.slot,
            "plugin_id": run.plugin_id,
            "producer": f"rule:{run.plugin_id}@{run.version}",
            "kind": finding.kind,
            "metric": finding.metric,
            "value": finding.value,
            "threshold": finding.threshold,
            "source": batch.source,
            "evidence_json": json.dumps(finding.evidence, ensure_ascii=False, default=str),
        }

    def _config_for(self, site_id: int, keys: tuple[str, ...]) -> dict[str, Any]:
        """这批数据所属场地的生效值（默认 < 全局 < 场地），带 `CONFIG_TTL_S` 的缓存。

        缓存的是**整个场地的生效值**，每支插件拿到的那份按它自己声明的 `CONFIG_KEYS` 现切。
        早先这里是"截断完再存"，于是缓存键只剩场地、内容却取决于**这一拍谁先搬队**：读一把键的
        插件先把条目建成那一把，后面读两把三把的各自 `KeyError` 在自己的第一把键上（2026-10-30
        真机：启用全部插件即三支死两支，而 health 全程 `ok`）。少一维缓存键的代价在这里不可见、
        只可复现，所以判据留在测试里：`CONFIG_KEYS` 少的必须先跑。

        声明了而生效值里没有，是代码与键注册表对不上，不是插件的错：抛出来让 `_consume` 把它
        归到**运行时**那本账（`batches_lost` + `last_error` + `problem()`），而不是让规则在
        自己内部炸成一插件 `failing`——那样运营者会去查阈值，而阈值根本没被读到。
        """
        cached = self._configs.get(site_id)
        if cached is None or monotonic() - cached[1] >= CONFIG_TTL_S:
            with self.db.scope() as session:
                site = session.get(Site, site_id)
                effective = config_layer.effective_for_site(session, self.settings, site)
            cached = ({name: row["value"] for name, row in effective.items()}, monotonic())
            self._configs[site_id] = cached
        full = cached[0]
        missing = [key for key in keys if key not in full]
        if missing:
            raise KeyError(
                f"场地 {site_id} 的生效配置里没有 {missing}："
                f"插件声明的 `CONFIG_KEYS` 与配置层对不上"
            )
        return {key: full[key] for key in keys}

    # --- 落库 ------------------------------------------------------------------

    def _write(
        self,
        run: _Run,
        rows: list[dict[str, Any]],
        moment: datetime,
        *,
        batches: int,
        findings: int,
        errors: int,
    ) -> None:
        """判定与运行态同一个事务：只落了判定没落状态，页面上就是"有产出但看着像没跑"。

        没搬东西的一轮不写：`plugin_state.updated_at` 若被一个空转的循环每 T 秒顶一次，"上次真正
        处理过数据是什么时候"就永远显示刚刚——那一列就没有存在意义了。
        """
        if not batches:
            return
        with self.db.scope() as session:
            for row in rows:
                session.add(PluginFinding(**row))
            state = session.get(PluginState, run.plugin_id)
            if state is None:  # `registry.sync()` 只给有实现的插件配状态行，这里补齐
                state = PluginState(plugin_id=run.plugin_id, status="stopped")
                session.add(state)
                session.flush()
            run.batches += batches
            run.findings += findings
            state.errors += errors
            if errors:
                # 崩了就不说"我在跑"：`failing` + 下一次可尝试的时刻是一组，缺一个都会让
                # supervisor 的行为只能靠读代码猜。
                state.status = "failing"
                if state.restarts >= RESTART_LIMIT:
                    # 上限之后只有这一句该说：它不再自己试，`next_retry_at` 清空正是"别再等了"
                    # 的表示——留着旧值会被读成"再过几秒还有一次"。
                    state.next_retry_at = None
                    session.add(
                        PluginLog(
                            plugin_id=run.plugin_id,
                            level="error",
                            event="backoff_exhausted",
                            message=f"退避重启 {state.restarts} 次仍在崩溃，已停止自动重启。",
                        )
                    )
                else:
                    state.next_retry_at = moment + timedelta(seconds=_backoff_s(state.restarts))
                session.add(
                    PluginLog(
                        plugin_id=run.plugin_id,
                        level="error",
                        event="crashed",
                        message=run.last_error,
                    )
                )
            else:
                state.status = "running"
            if state.started_at is None:
                state.started_at = moment
            state.last_tick_at = moment
            state.processed += batches
            state.produced += findings
            state.dropped = sum(queue.dropped for queue in run.queues.values())
            state.last_error = run.last_error
            state.stats_json = json.dumps(
                {
                    str(gateway_id): {
                        "depth": queue.depth,
                        "peak": queue.peak,
                        "dropped": queue.dropped,
                        "limit": queue.limit,
                    }
                    for gateway_id, queue in run.queues.items()
                },
                ensure_ascii=False,
            )
            state.updated_at = moment

        # 镜像跟着**已经提交的那一行**动，不跟着意图动：`_write` 在提交前抛出就等于库里那行没改，
        # 内存里也不该先替它承认"坏着"。`expire_on_commit=False` 让这两个属性在 `with` 块外仍可读。
        if state.status == "failing":
            self._failing[run.plugin_id] = (
                run.last_error or "规则抛了异常，没带回错误文本",
                state.next_retry_at is not None,
            )
        else:
            self._failing.pop(run.plugin_id, None)

    def _note_error(self, run: _Run, exc: Exception) -> None:
        self.last_error = f"{type(exc).__name__}: {exc}"
        run.last_error = self.last_error
        self.log.warning("plugin %s failed: %s", run.plugin_id, exc)

    def status(self) -> dict[str, Any]:
        """给 `/api/health` 的 `plugins` 节：只读内存快照，不开库。

        与 `CapturePipeline.status()` 同一条约束（健康检查在另一个线程里跑），所以队列那一段读的是
        `_publish_queues()` 换出来的那份元组，而不是正在变的 `run.queues`。

        `enabled` 是**这一层开没开**（布尔，与 `dialer`/`retention`/`capture` 每一节同一个键名），
        启用中的插件名单因此叫 `enabled_plugins`：同一个键在两处有两种意思，页面就会把"没启用任何
        插件"读成"运行时关着"，而那正是回滚判据要能区分开的两件事。
        """
        return {
            "enabled": self.enabled,
            "running": self.running,
            "role": self.role,
            "refused": "aggregator_role" if self.role == "aggregator" else None,
            "tick_interval_s": self.tick_interval_s,
            "enabled_plugins": sorted(self._runs),
            "failing_plugins": [
                {
                    "plugin_id": plugin_id,
                    "last_error": last_error,
                    "retrying": retrying,
                }
                for plugin_id, (last_error, retrying) in sorted(self._failing.items())
            ],
            "drain_chunk": DRAIN_CHUNK,
            "config_ttl_s": CONFIG_TTL_S,
            "refreshes": self.refreshes,
            "batches_offered": self.batches_offered,
            "frames_offered": self.frames_offered,
            "frames_fed": self.frames_fed,
            "dropped_batches": self.dropped_batches,
            "batches_refused": self.batches_refused,
            "batches_out_of_scope": self.batches_out_of_scope,
            "batches_lost": self.batches_lost,
            "findings_written": self.findings_written,
            "errors": self.errors,
            "last_run_at": self.last_run_at,
            "last_error": self.last_error,
            "queues": list(self._queue_stats),
            "problem": self.problem(),
        }

    def problem(self) -> str | None:
        """只在"现在真的坏着"时说话，与 `capture`/`retention` 同一条纪律。

        全停用不是故障（那是 S7 的回滚形态）；累计 `errors` 也不是——它只会涨，说不了"现在坏不坏"。

        有两个"现在"：运行时这一层最近一轮的失败（`last_error`，跑成一轮就收回），以及**启用中的
        插件此刻正停在 `failing`**（`_failing` 那份镜像）。第二个是 2026-10-30 真机补的：退避耗尽
        之后它不再尝试，于是每轮都"没有新失败"，只看 `last_error` 的健康检查会把两支永久停摆的插件
        报成 `ok`——"坏了并且不再试了"与"好了"必须是两个不同的现象，否则 degraded 这个字迟早没人信。
        """
        parts: list[str] = []
        if self.last_error is not None:
            parts.append(
                f"插件运行时累计 {self.errors} 次失败（规则崩溃与库读写都算），"
                f"丢掉 {self.batches_lost} 批；最近一次：{self.last_error}"
            )
        if self._failing:
            detail = "、".join(
                f"{plugin_id}"
                f"（{'退避中，到点还会自己重试' if retrying else '退避已耗尽，等用户重新启用'}"
                f"：{last_error}）"
                for plugin_id, (last_error, retrying) in sorted(self._failing.items())
            )
            parts.append(f"{len(self._failing)} 支启用中的插件正处在 failing：{detail}")
        return "；".join(parts) or None
