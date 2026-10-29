"""拨号器：对 serve.mjs 的 HTTP 探测、失败原因分类，以及 S3-6 的后台轮询循环。

分两层：
- 底下是"人工点一次的实测"（`POST /api/gateways/{id}/probe` 与 `probe_gateway()`），
  失败原因必须分类，运营者才知道是网线、进程、还是对端根本不是 serve.mjs。
- 上面是 `DialCoordinator`：一台接入机一条任务，按各自的退避节奏自己重枚举，
  把端点表交给 `ingest.reconcile()` 落库。它存在的理由是 §7 S3 的判据——
  **没人点也要更新**，并且 A 掉线不许波及 B。

两条贯穿全文件的规则：§1"不得因为一次超时就把整台机器判死"（`dial_fails_to_unreachable`），
以及"链路失败与策略拒绝是两件事"（后者不升级退避，见 `_remember()`）。

传输层被抽成 `transport.fetch(url, timeout_s)`：测试直接交进想要的事实，
不需要起一个假 HTTP 服务。
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import asdict, dataclass
from datetime import datetime
from time import perf_counter
from typing import Any, Callable

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import Database
from ..models import Gateway, Relay, Site, utcnow
from .hierarchy import conflict
from .ingest import mark_gateway_lost, reconcile
from .instance import effective_current_site_id, effective_instance_role

#: serve.mjs 现在只公布这两个 HTTP 面；探测取前者，不发明新端点。
ENDPOINTS_PATH = "/api/endpoints"
#: 板卡归属（rh↔scan）切换：状态码与响应体由桥自己裁决，代理不得改写（契约 §4）。
MODE_PATH = "/api/mode"

TRANSPORT_DETAILS = {
    "dns": "名字解析不了：接入机主机名不在本地/内网解析器里",
    "refused": "端口没人应答：serve.mjs 没起、端口不对，或中间被防火墙挡住",
    "timeout": "在预算时间内没回话：机器在但进程没响应，或线路断了",
}


@dataclass(frozen=True)
class FetchResult:
    """一次 HTTP 尝试的事实。`error` 是传输层分类（dns/refused/timeout），
    与 HTTP 状态码互斥：连上了才有状态码。"""

    status_code: int | None
    payload: Any = None
    latency_ms: int = 0
    error: str | None = None


class HttpTransport:
    """真拨号：用 httpx 同步请求，FastAPI 会把它放到线程池里跑。"""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client()

    def close(self) -> None:
        self.client.close()

    def fetch(self, url: str, timeout_s: float) -> FetchResult:
        started = perf_counter()

        def latency() -> int:
            return round((perf_counter() - started) * 1000)

        try:
            response = self.client.get(url, timeout=timeout_s)
        except httpx.TimeoutException:
            return FetchResult(None, None, latency(), "timeout")
        except httpx.ConnectError as exc:
            return FetchResult(None, None, latency(), _classify_connect_error(exc))
        except httpx.HTTPError as exc:
            return FetchResult(None, None, latency(), f"{type(exc).__name__}".lower())
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return FetchResult(response.status_code, payload, latency(), None)

    def post(self, url: str, payload: Any, timeout_s: float) -> FetchResult:
        """转发一次写请求。返回的是**桥给的状态码**，代理层原样交出去。"""
        started = perf_counter()

        def latency() -> int:
            return round((perf_counter() - started) * 1000)

        try:
            response = self.client.post(url, json=payload, timeout=timeout_s)
        except httpx.TimeoutException:
            return FetchResult(None, None, latency(), "timeout")
        except httpx.ConnectError as exc:
            return FetchResult(None, None, latency(), _classify_connect_error(exc))
        except httpx.HTTPError as exc:
            return FetchResult(None, None, latency(), f"{type(exc).__name__}".lower())
        try:
            body = response.json()
        except ValueError:
            body = None
        return FetchResult(response.status_code, body, latency(), None)


def bridge_url(gateway: Gateway, path: str) -> str:
    """拼出这台接入机的桥地址；没配 `base_url` 就是"没测过"，不许假装能拨。"""
    if not gateway.base_url:
        raise conflict("no_base_url", gateway_id=gateway.gateway_id)
    return gateway.base_url.rstrip("/") + path


def _classify_connect_error(exc: httpx.ConnectError) -> str:
    """DNS 失败与端口拒绝是完全不同的处置，得从异常链里分出来。"""
    cause = exc.__cause__
    while cause is not None:
        if isinstance(cause, socket.gaierror):
            return "dns"
        cause = cause.__cause__
    return "refused"


def classify(result: FetchResult) -> tuple[bool, str | None, str, list | None]:
    """把一次抓取的结果翻成 (是否成功, 原因码, 给人看的一句话, 端点列表)。

    计划里写的"版本不符"目前只能以**响应结构不符**的形式落地：serve.mjs 的
    `/api/endpoints` 到今天都不带版本字段（见欠项清单），所以这里认的是
    "对端不是我们认识的那套接口"，等它公布版本后再加一条真正的协商。
    """
    if result.error:
        return False, result.error, TRANSPORT_DETAILS.get(result.error, result.error), None
    if result.status_code is None or not 200 <= result.status_code < 300:
        return False, "http_status", f"对端回了 HTTP {result.status_code}", None
    if not isinstance(result.payload, dict) or not isinstance(
        result.payload.get("endpoints"), list
    ):
        return (
            False,
            "unexpected_response",
            "响应里没有 endpoints 数组：对端不是 serve.mjs，或版本与本实例不兼容",
            None,
        )
    endpoints = result.payload["endpoints"]
    if not endpoints:
        return (
            False,
            "empty_endpoints",
            "连上了但一个端点都没枚举出来：udev 别名没配，或板子没插",
            None,
        )
    return True, None, f"通，枚举到 {len(endpoints)} 个端点", endpoints


def probe_gateway(
    session: Session, gateway: Gateway, transport, settings: Settings
) -> dict:
    """实测并把结果写回网关；不写就算没测过。"""
    if not gateway.base_url:
        raise conflict("no_base_url", gateway_id=gateway.gateway_id)

    url = bridge_url(gateway, ENDPOINTS_PATH)
    result = transport.fetch(url, settings.probe_timeout_s)
    ok, reason, detail, endpoints = classify(result)

    gateway.dial_failures = 0 if ok else gateway.dial_failures + 1
    if ok:
        gateway.dial_state = "online"
        gateway.dial_reason = None
        gateway.last_seen = utcnow()
    else:
        gateway.dial_reason = reason
        if gateway.dial_failures >= settings.dial_fails_to_unreachable:
            gateway.dial_state = "unreachable"
    session.flush()

    return {
        "gateway_id": gateway.gateway_id,
        "url": url,
        "ok": ok,
        "reason": reason,
        "detail": detail,
        "latency_ms": result.latency_ms,
        "dial_state": gateway.dial_state,
        "consecutive_failures": gateway.dial_failures,
        "endpoints": endpoints,
        "endpoints_count": len(endpoints) if endpoints is not None else 0,
    }


def next_backoff(failures: int, *, base_s: float, max_s: float) -> float:
    """连续失败 n 次之后该等多久：base、2×base、4×base……封顶 max。

    `failures <= 0` 回 `0.0`，意思是"不该退避"——调用方看到 0 就改走轮询间隔。
    这里不替调用方猜一个"反正也差不多"的默认值，否则 `test_dial_loop` 里
    "健康机器按 POLL 走"这条断言就没有意义了。
    """
    if failures <= 0:
        return 0.0
    return min(base_s * (2 ** (failures - 1)), max_s)


@dataclass(frozen=True)
class Plan:
    """一轮规划的结果：该拨谁，以及谁不该被拨。

    `refused` 每条都带 `gateway_id`/`name`/`reason`。点名不是给日志好看，是因为
    "我的接入机怎么没被采集"这个问题必须有第二个答案（没拨 vs 故意不拨），
    而静默跳过会让两者长得一模一样。
    """

    targets: tuple[dict, ...] = ()
    refused: tuple[dict, ...] = ()
    role: str = "site"


@dataclass
class _Runtime:
    """一台接入机在循环里的账本（库里的 `dial_failures` 是事实，这里是排程）。"""

    rounds: int = 0
    failures: int = 0
    rejected: int = 0
    lost_nodes: int = 0
    next_delay_s: float | None = None
    last_reason: str | None = None
    last_round_at: datetime | None = None


PROBLEM_BY_REASON = {
    "db_unreadable": "gateway 表读不到：后台拨号循环无法枚举接入机",
    "site_ambiguous": "库里配了多个场地但没指定 RFTRACK_SITE_ID：拨号循环一台都没拨",
}


class DialCoordinator:
    """后台拨号循环：一台接入机一条任务、一条共用的并发闸、一份自己的退避。

    为什么不用"一个循环轮询全表"：§7 S3 判据"A 掉线不影响 B"在共享循环里要靠额外的
    due 时间表才成立，而每台一条任务天生成立。代价是多一条规划任务——按
    `dial_poll_interval_s` 重扫"该拨谁"，换来新登记的接入机不用重启进程就会被拨到、
    被删掉的会立刻停拨。

    三条不能含糊的口径：
    - **汇聚实例一轮都不拨**，并且如实报 `running=false`（它按设计就没有循环）。
    - **策略拒绝不是链路失败**：`reconcile` 的 409（超限、端点无法寻址）说明桥是通的，
      不该攒失败计数、不该升级退避、更不该把半张表落进库。
    - **任何异常都不许穿过任务**：一台桥把自己炸了，最坏只是这一轮没拨通。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        transport,
        *,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.settings = settings
        self.transport = transport
        #: 排程用的等待函数：测试交一个记录器进来，退避序列就能在不等真实时钟的情况下断言。
        self._sleep = sleep
        self._tasks: dict[int, asyncio.Task] = {}
        self._planner: asyncio.Task | None = None
        self._gate: asyncio.Semaphore | None = None
        self._runtime: dict[int, _Runtime] = {}
        self._targets: list[dict] = []
        self._refused: list[dict] = []
        self._role: str | None = None
        self._planned = False
        self._started = False
        self._stopped = False
        self._rounds = 0
        self._planner_error: str | None = None

    # --- 规划 ---------------------------------------------------------------

    def plan(self) -> Plan:
        """这一轮该拨谁。库里有的每一台都要有个说法，不静默跳过。"""
        try:
            with self.db.scope() as session:
                role = effective_instance_role(session, self.settings.instance_role)
                # S4：当前场地以库里那一行为准，环境变量只是未配置时的兜底。
                current = effective_current_site_id(
                    session, self.settings.current_site_id
                )
                rows = self._rows(session)
                # 多个场地又没指定当前场地：不猜，整批拒绝（§5 不让"看起来对"的地址混进来）。
                ambiguous = current is None and len(set(session.scalars(select(Site.site_id)))) > 1
        except OperationalError:
            return self._refuse_all(role=None, reason="db_unreadable", rows=())
        if role == "aggregator":
            return self._refuse_all(role=role, reason="aggregator_role", rows=rows)

        targets: list[dict] = []
        refused: list[dict] = []
        for gateway, site_id in rows:
            ident = {"gateway_id": gateway.gateway_id, "name": gateway.name}
            if ambiguous:
                refused.append({**ident, "reason": "site_ambiguous"})
            elif gateway.status != "active":
                refused.append({**ident, "reason": "gateway_disabled"})
            elif current is not None and site_id != current:
                refused.append({**ident, "reason": "foreign_site"})
            elif not gateway.base_url:
                refused.append({**ident, "reason": "no_base_url"})
            elif len(targets) >= self.settings.max_gateways:
                refused.append({**ident, "reason": "max_gateways"})
            else:
                targets.append(ident)
        return Plan(targets=tuple(targets), refused=tuple(refused), role=role)

    @staticmethod
    def _rows(session: Session) -> list:
        return list(
            session.execute(
                select(Gateway, Relay.site_id)
                .join(Relay, Gateway.relay_id == Relay.relay_id)
                .order_by(Gateway.gateway_id)
            )
        )

    @staticmethod
    def _refuse_all(role: str | None, reason: str, rows) -> Plan:
        refused = [
            {"gateway_id": gateway.gateway_id, "name": gateway.name, "reason": reason}
            for gateway, _ in rows
        ]
        return Plan(refused=tuple(refused), role=role or "site")

    def _adopt(self, plan: Plan) -> None:
        self._targets = list(plan.targets)
        self._refused = list(plan.refused)
        self._role = plan.role
        self._planned = True

    # --- 单台：拨一次并落库 --------------------------------------------------

    def dial_gateway(self, gateway_id: int) -> dict:
        """拨一台并把端点表对账进 node 表。异常一律翻成本轮结果，不往外穿。"""
        try:
            outcome = self._probe(gateway_id)
            if outcome["ok"]:
                outcome.update(self._sync(gateway_id, outcome.pop("endpoints") or []))
            else:
                outcome.pop("endpoints", None)
                outcome.update(self._lose_nodes(gateway_id, outcome["dial_state"]))
        except HTTPException as exc:  # 配置类的拒绝（如 base_url 被清空），不是网络故障
            outcome = self._rejected(gateway_id, exc)
        except Exception as exc:
            outcome = {
                "gateway_id": gateway_id,
                "ok": False,
                "reason": "dial_error",
                "detail": f"{type(exc).__name__}: {exc}",
                "dial_state": None,
                "consecutive_failures": None,
                "endpoints_count": 0,
                "nodes": None,
                "rejected": None,
                "lost_nodes": 0,
            }
        outcome.update(self._remember(gateway_id, outcome))
        return outcome

    def _probe(self, gateway_id: int) -> dict:
        with self.db.scope() as session:
            gateway = session.get(Gateway, gateway_id)
            if gateway is None:
                return {
                    "gateway_id": gateway_id,
                    "ok": False,
                    "reason": "gateway_gone",
                    "detail": "接入机行已经不在了：本轮之后规划任务会停掉这条循环",
                    "dial_state": None,
                    "consecutive_failures": None,
                    "endpoints_count": 0,
                }
            return probe_gateway(session, gateway, self.transport, self.settings)

    def _sync(self, gateway_id: int, endpoints: list[dict]) -> dict:
        """端点对账单独一个事务：被拒绝时回滚的只是这一轮的写入。

        上一轮"这台桥通了、延迟多少"必须留在库里——否则一次配置超限会把连通性
        历史一起抹掉，运营者看到的就成了"从来没拨过"。
        """
        with self.db.scope() as session:
            gateway = session.get(Gateway, gateway_id)
            if gateway is None:
                return {"nodes": None, "rejected": None, "lost_nodes": 0}
            try:
                sync = reconcile(session, gateway, endpoints, self.settings)
            except HTTPException as exc:
                session.rollback()
                return {"nodes": None, "rejected": _detail(exc), "lost_nodes": 0}
            return {
                "nodes": {
                    "created": list(sync.created),
                    "updated": list(sync.updated),
                    "lost": list(sync.lost),
                    "seen": list(sync.seen),
                    "duplicates": list(sync.duplicates),
                },
                "rejected": None,
                "lost_nodes": len(sync.lost),
            }

    def _lose_nodes(self, gateway_id: int, dial_state: str | None) -> dict:
        """只有真判死（`unreachable`）才连带丢节点：§1 不许一次超时就把节点标没。"""
        if dial_state != "unreachable":
            return {"nodes": None, "lost_nodes": 0}
        with self.db.scope() as session:
            gateway = session.get(Gateway, gateway_id)
            changed = mark_gateway_lost(session, gateway) if gateway is not None else 0
        return {"nodes": None, "lost_nodes": changed}

    @staticmethod
    def _rejected(gateway_id: int, exc: HTTPException) -> dict:
        detail = _detail(exc)
        return {
            "gateway_id": gateway_id,
            "ok": True,  # 桥那边通不通这轮没测出来，但至少不是"拨不通"
            "reason": detail.get("reason"),
            "detail": detail,
            "dial_state": None,
            "consecutive_failures": None,
            "endpoints_count": 0,
            "nodes": None,
            "rejected": detail,
            "lost_nodes": 0,
        }

    def _remember(self, gateway_id: int, outcome: dict) -> dict:
        """把本轮事实折成"下次几点再试"。"""
        state = self._runtime.setdefault(gateway_id, _Runtime())
        self._rounds += 1
        state.rounds += 1
        state.last_round_at = utcnow()
        state.last_reason = outcome.get("reason")
        if outcome.get("rejected") is not None:
            state.rejected += 1
            state.failures = 0
            delay = self.settings.dial_poll_interval_s
        elif not outcome["ok"]:
            stored = outcome.get("consecutive_failures")
            # 连续失败数以库里的 `dial_failures` 为准：它与 /api/gateways 显示的是同一个数。
            state.failures = stored if isinstance(stored, int) else state.failures + 1
            delay = next_backoff(
                state.failures,
                base_s=self.settings.dial_backoff_base_s,
                max_s=self.settings.dial_backoff_max_s,
            )
        else:
            state.failures = 0
            delay = self.settings.dial_poll_interval_s
        state.lost_nodes += outcome.get("lost_nodes") or 0
        if delay == 0.0:  # 还没失败过：退避无从谈起，按轮询走
            delay = self.settings.dial_poll_interval_s
        state.next_delay_s = delay
        return {"next_delay_s": delay, "failures": state.failures}

    # --- 并发与循环 ----------------------------------------------------------

    def _gate_or_new(self) -> asyncio.Semaphore:
        if self._gate is None:
            self._gate = asyncio.Semaphore(self.settings.dial_concurrency)
        return self._gate

    async def _dial_slot(self, gateway_id: int) -> dict:
        """一次拨号占一个并发位。HTTP 与写库都在线程里，循环本身不会被阻塞。"""
        async with self._gate_or_new():
            return await asyncio.to_thread(self.dial_gateway, gateway_id)

    async def dial_all(self) -> dict:
        """不等任何排程，把全部目标并发拨一轮（受 `dial_concurrency` 闸）。

        后台循环按每台自己的节奏走；这条是"立刻全拨一次"的入口——手动重拨与测试共用它，
        免得再写一份并发逻辑。
        """
        plan = await asyncio.to_thread(self.plan)
        self._adopt(plan)
        results = await asyncio.gather(
            *(self._dial_slot(entry["gateway_id"]) for entry in plan.targets)
        )
        return {
            "targets": list(plan.targets),
            "refused": list(plan.refused),
            "results": list(results),
        }

    async def _gateway_loop(self, gateway_id: int) -> None:
        """一台一条：拨 → 按算出来的秒数等 → 再拨。别的机器怎样都不影响这条。"""
        while True:
            await self._dial_slot(gateway_id)
            state = self._runtime.get(gateway_id)
            delay = state.next_delay_s if state and state.next_delay_s else self.settings.dial_poll_interval_s
            await self._sleep(delay)

    async def _planner_loop(self) -> None:
        """按轮询节奏重扫"该拨谁"；规划本身出错也要继续扫（否则新机器永远进不来）。

        这里刻意用真实的 `asyncio.sleep`，而不是构造器注入的那个 `sleep`：注入口是给
        "这台下次几点再试"用的（测试要在不等的情况下断言退避序列），规划节奏若跟着它走，
        测试里的"立刻返回"假 sleep 会把重扫变成 1 kHz 的数据库轮询——实测会把线程与连接
        打爆，`stop()` 因此再也回不来。
        """
        while True:
            await asyncio.sleep(self.settings.dial_poll_interval_s)
            try:
                await self.refresh()
                self._planner_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._planner_error = f"{type(exc).__name__}: {exc}"

    async def refresh(self) -> dict:
        """重扫一次：新登记的接入机起任务，被删掉或被拒的停机。"""
        plan = await asyncio.to_thread(self.plan)
        self._adopt(plan)
        wanted = {entry["gateway_id"] for entry in plan.targets}
        for gateway_id in [gid for gid in self._tasks if gid not in wanted]:
            self._tasks.pop(gateway_id).cancel()
        for gateway_id in sorted(wanted - set(self._tasks)):
            self._tasks[gateway_id] = asyncio.create_task(self._gateway_loop(gateway_id))
        return self.status()

    async def start(self) -> dict:
        """起后台循环。两种情况不起任务，并且如实报"没在跑"。

        - `dialer_enabled=false`：这台实例根本没启用采集。
        - 汇聚实例：按设计不拨号，报 running=true 才是撒谎。
        """
        if not self.settings.dialer_enabled:
            return self.status()
        self._started = True
        await self.refresh()
        if self._role == "aggregator":
            return self.status()
        if self._planner is None or self._planner.done():
            self._stopped = False
            self._planner = asyncio.create_task(self._planner_loop())
        return self.status()

    async def stop(self) -> None:
        """取消每一条任务并等它们真退出——停了之后不许还有请求在飞。

        顺序是"先规划、后接入机"，不是随手写的：`while True` 的循环不会自己结束，
        只 gather 不 cancel 会永久等下去（实测 8 s 内一台机器被拨了 1000 多轮）；而规划
        任务如果和接入机任务同时收，它可能正好卡在 `refresh()` 里、把新任务建在已经清空
        的表上，留下一条没人管的循环。先把它摘干净，再起第二步。
        """
        self._stopped = True
        planner, self._planner = self._planner, None
        if planner is not None:
            planner.cancel()
            await asyncio.gather(planner, return_exceptions=True)
        tasks, self._tasks = self._tasks, {}
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)

    # --- 对外呈现 ------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._planner is not None and not self._planner.done()

    def problem(self) -> str | None:
        """一句话解释"节点表为什么不自动更新"；没有需要说的就回 None。

        拒拨分两类：`aggregator_role`/`foreign_site`/`max_gateways`/`no_base_url`/
        `gateway_disabled` 是设计使然或配置本身，不该报故障；
        `site_ambiguous`/`db_unreadable` 是配错或库坏，必须说出来。
        """
        if not self.settings.dialer_enabled:
            return None
        reasons = {entry["reason"] for entry in self._refused}
        for reason in ("db_unreadable", "site_ambiguous"):
            if reason in reasons:
                return PROBLEM_BY_REASON[reason]
        if self._planner_error:
            return f"后台拨号循环的规划任务报错：{self._planner_error}"
        if self._role == "aggregator":
            return None  # 汇聚实例没有循环是设计，不是故障
        if self._started and not self.running and not self._stopped:
            return "后台拨号循环已退出：节点表不会再自动更新"
        return None

    def status(self) -> dict:
        """纯内存状态：健康检查每 2 s 被刷一次，不该为此开一个数据库会话。"""
        return {
            "enabled": self.settings.dialer_enabled,
            "running": self.running,
            "role": self._role,
            "planned": self._planned,
            "rounds": self._rounds,
            "concurrency": self.settings.dial_concurrency,
            "poll_interval_s": self.settings.dial_poll_interval_s,
            "backoff_max_s": self.settings.dial_backoff_max_s,
            "targets": list(self._targets),
            "refused": list(self._refused),
            "gateways": [
                {"gateway_id": gateway_id, **asdict(self._runtime[gateway_id])}
                for gateway_id in sorted(self._runtime)
            ],
            "problem": self.problem(),
        }


def _detail(exc: HTTPException) -> dict:
    """FastAPI 的 `HTTPException.detail` 在我们这套约定里就是 `{"reason": …}`。"""
    return exc.detail if isinstance(exc.detail, dict) else {"reason": str(exc.detail)}
