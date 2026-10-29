"""回放数据源：把录制文件按录制时的节拍喂进采集管道，来源一律 `mock`。

S6 出口判据第一条要"用真机录制回放做数据源"。这一层交付的是那条路的**管道**：录制文件 →
`CapturePipeline.feed(source="mock")` → 摘要 / 事件 / `/ws/live`。而**"真机录制"那段今天还不存在**
（M0-3 的 60 s 录制未执行，见任务 #7），所以配套产出器 `rftrack/tools/make_replay_recording.py`
拿对拍夹具里的合成帧造录制，`notes` 里写清它是合成的。这条区别不许被实现抹平——
抹平了就等于用一段假数据替真机验收签字（任务书 §11）。

三条决定各自钉住一件不能妥协的事：

- **来源写死 `mock`**：文件里的 `source` 必须等于 `"mock"`，不然整份拒读（`source_not_mock`）。
  "改一个字段就把回放洗成实时"这种能力，实现里一次都不该存在。
- **归属只认库里已有的节点行**：录制声明的是 `transport_addr`，本层查 `(gateway_id, transport_addr)`。
  查不到（`node_row_missing`）或同一地址属多台接入机（`ambiguous_gateway`）都**一个字节都不喂**，
  并在健康检查里点名。理由与 `/ws/bridge` 的 `gateway` 必填同一条：地址对不上或一对多时，
  "猜一台"会把摘要写到别人的节点上，而页面上看上去完全正常。
- **按绝对毫秒时刻对齐播放**：每块等到"录制起点 + `t_ms`"再喂，而不是"每块 sleep 一次间隔"。
  后者会把一段 60 s 的录制漂成 62 s，而漂移累计超过 `capture_silence_s` 之后，
  断流判定会在演奏中途说话——那是一条只有回放才会有的假事件。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from ..config import Settings
from ..database import Database
from ..models import Node
from .capture import CapturePipeline

LOG = logging.getLogger("rftrack.replay")

#: 录制文件的格式标记：不认的一律拒读，不猜形状。
FORMAT = "rftrack-replay-v1"
#: 本层唯一可能的来源。见模块注释第一条。
SOURCE = "mock"
#: 回放的"端点"名字前缀。它不是桥的 `ep`，但同一套 `(gateway_id, ep)` 键要能不撞真桥的条目。
EP_PREFIX = "replay:"


class ReplayError(ValueError):
    """一份录制读不了。`reason` 是给运营者看的那句原因，不是异常文本。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Recording:
    """一份可播放的录制：一串"相对起点的毫秒 → 一块字节"。"""

    name: str
    path: Path
    transport_addr: str
    notes: str
    chunks: tuple[tuple[int, bytes], ...]

    @property
    def ep(self) -> str:
        return f"{EP_PREFIX}{self.name}"

    @property
    def span_s(self) -> float:
        return round(self.chunks[-1][0] / 1000, 3)


def load(path: Path) -> Recording:
    """把一个 JSON 录制变成 `Recording`。任何一条不合格式都是 `ReplayError`，不抛半截状态。

    校验做在**读**的这一侧而不是播的那一侧：一份洗不掉来源、或者时间戳倒着走的文件，
    最坏应该是"没演"，不该是"演到一半开始往库里写乱数据"。
    """
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReplayError(f"unreadable:{type(exc).__name__}") from exc
    if not isinstance(manifest, dict):
        raise ReplayError("not_an_object")
    if manifest.get("format") != FORMAT:
        raise ReplayError("format_unsupported")
    if manifest.get("source") != SOURCE:
        raise ReplayError("source_not_mock")
    addr = manifest.get("transport_addr")
    if not isinstance(addr, str) or not addr.strip():
        raise ReplayError("addr_missing")
    chunks = _chunks(manifest.get("chunks"))
    return Recording(
        name=path.stem,
        path=path,
        transport_addr=addr,
        notes=str(manifest.get("notes") or ""),
        chunks=chunks,
    )


def _chunks(raw: Any) -> tuple[tuple[int, bytes], ...]:
    """`[{"t_ms": int, "bytes": base64}, ...]`，要求非空、形状对、时间不减。"""
    if not isinstance(raw, list) or not raw:
        raise ReplayError("chunks_empty")
    out: list[tuple[int, bytes]] = []
    previous = -1
    for entry in raw:
        if not isinstance(entry, dict):
            raise ReplayError("chunk_malformed")
        moment = entry.get("t_ms")
        body = entry.get("bytes")
        if not isinstance(moment, int) or isinstance(moment, bool) or moment < previous:
            raise ReplayError("timestamps_backwards")
        if not isinstance(body, str):
            raise ReplayError("chunk_malformed")
        try:
            payload = base64.b64decode(body, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ReplayError("chunk_malformed") from exc
        out.append((moment, payload))
        previous = moment
    return tuple(out)


class ReplayRunner:
    """每个录制一路任务，按录制自己的节拍喂字节。

    与 `dialer`/`retention`/`capture` 同一套纪律：没配目录就不是故障（`enabled=false`），
    起了就每一路都可取消、`stop()` 不挂死，对外只报数不猜。
    """

    def __init__(
        self,
        db: Database,
        settings: Settings,
        pipeline: CapturePipeline,
        *,
        log=LOG,
    ) -> None:
        self.db = db
        self.settings = settings
        self.pipeline = pipeline
        self.log = log
        self.chunks_fed = 0
        self.passes = 0
        self.running = False
        self.role_refused: str | None = None
        # 这四样只在循环上改；对外呈现的是 `_snapshot`（每次整份换掉，见 `_publish`）。
        self._recordings: list[Recording] = []
        self._rejected: list[dict[str, str]] = []
        self._unmatched: dict[str, str] = {}
        #: 一遍放完、且没开循环的那几份。健康检查要用它把"回放结束了"与"板子没接上"分开说。
        self._done: set[str] = set()
        #: 状态那一节走**整份换掉**的快照：`/api/health` 在线程池里读它，而这边在循环上改。
        self._snapshot: dict[str, Any] = {}
        self._publish()
        self._stopping: asyncio.Event | None = None
        self._tasks: list[asyncio.Task] = []

    # --- 生命周期 -------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self.settings.replay_dir is not None

    async def start(self) -> None:
        """采集没在跑就不喂：字节只会堆进一条没人搬的队列，把真正的采集挤兑成丢帧。

        拒绝的理由**照抄采集那边的理由**：`aggregator` 那台按设计没有采集循环（形态不是故障），
        而 `capture_enabled=0` 是配置矛盾。两种都往 `role_refused` 里写清楚，别让运营者对着
        一页"没有数据"猜是哪一种。
        """
        if not self.enabled:
            return
        if not self.pipeline.running:
            self.role_refused = (
                "aggregator_role" if self.pipeline.role == "aggregator" else "capture_disabled"
            )
            self._publish()
            return
        loop = asyncio.get_running_loop()
        self._recordings, self._rejected, self._unmatched = await loop.run_in_executor(
            None, self._load
        )
        self.role_refused = None
        self._done = set()
        self._publish()
        self._stopping = asyncio.Event()
        self._tasks = [
            asyncio.create_task(self._play(recording), name=f"rftrack-replay-{recording.name}")
            for recording in self._recordings
        ]
        self.running = True

    async def stop(self) -> None:
        """先置停止位再摘任务：录制任务里等的是 `_stopping`，两条路都能立刻回来。"""
        was_running = self.running
        if self._stopping is not None:
            self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._stopping = None
        self.running = False
        if was_running:
            self.log.info("replay stopped after %s chunks", self.chunks_fed)

    # --- 线程侧：读目录、查归属 ----------------------------------------------

    def _load(self) -> tuple[list[Recording], list[dict[str, str]], dict[str, str]]:
        """目录里能演的与演不了的两份名单（第三份"还没有归属"由循环侧随后填）。

        整份在**线程里**做（和 `capture` 同一条理由：SD 卡上的一次目录扫描能让循环停住半秒），
        返回的是纯数据，由循环侧换成快照。
        """
        directory = self.settings.replay_dir
        if directory is None:
            return [], [], {}
        if not directory.is_dir():
            return [], [{"file": str(directory), "reason": "dir_missing"}], {}
        recordings: list[Recording] = []
        rejected: list[dict[str, str]] = []
        for path in sorted(directory.glob("*.json")):
            try:
                recordings.append(load(path))
            except ReplayError as exc:
                rejected.append({"file": path.name, "reason": exc.reason})
        return recordings, rejected, {}

    def _addr_index(self) -> dict[str, set[int]]:
        """`transport_addr` → 认领它的接入机集合。库里一对多是**允许**的，所以必须回集合。

        三台接入机都报同一块 `/dev/widscan58-2` 是既有的正常形态（`test_multi_gateway` 钉着），
        这时"回放该喂哪一台"没有答案，只能问运营者，不能由本层抽签。
        """
        with self.db.scope() as session:
            rows = session.execute(select(Node.gateway_id, Node.transport_addr)).all()
        index: dict[str, set[int]] = {}
        for gateway_id, addr in rows:
            index.setdefault(addr, set()).add(gateway_id)
        return index

    # --- 循环侧：一路录制 ------------------------------------------------------

    async def _play(self, recording: Recording) -> None:
        assert self._stopping is not None
        while not self._stopping.is_set():
            index = await asyncio.get_running_loop().run_in_executor(None, self._addr_index)
            gateways = index.get(recording.transport_addr)
            reason = self._refusal(gateways)
            if reason is not None:
                self._mark_unmatched(recording, reason)
                return
            completed = await self._pass(recording, next(iter(gateways)))
            if not completed:
                return
            self.passes += 1
            if not self.settings.replay_loop:
                self._mark_done(recording)
                return

    @staticmethod
    def _refusal(gateways: set[int] | None) -> str | None:
        if not gateways:
            return "node_row_missing"
        if len(gateways) > 1:
            return "ambiguous_gateway"
        return None

    async def _pass(self, recording: Recording, gateway_id: int) -> bool:
        """放一遍。返回 `False` = 中途收到收摊信号，调用方因此不许把它记成"放完了一遍"。"""
        assert self._stopping is not None
        loop = asyncio.get_running_loop()
        started = loop.time()
        for moment_ms, chunk in recording.chunks:
            delay = started + moment_ms / 1000 - loop.time()
            if delay > 0:
                try:
                    await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                    return False  # 等的时候收到了收摊信号
                except asyncio.TimeoutError:
                    pass
            if self._stopping.is_set():
                return False
            self.pipeline.feed(
                gateway_id,
                recording.ep,
                chunk,
                source=SOURCE,
                transport_addr=recording.transport_addr,
            )
            self.chunks_fed += 1
        return True

    # --- 对外呈现 -------------------------------------------------------------

    def _mark_unmatched(self, recording: Recording, reason: str) -> None:
        if self._unmatched.get(recording.name) == reason:
            return
        self._unmatched[recording.name] = reason
        self._publish()
        self.log.warning(
            "replay %s stopped: %s (%s)", recording.name, reason, recording.transport_addr
        )

    def _mark_done(self, recording: Recording) -> None:
        """一遍放完且不循环：记下来，好让"没数据了"有三种说得清的原因。"""
        if recording.name in self._done:
            return
        self._done.add(recording.name)
        self._publish()

    def _publish(self) -> None:
        """换出**一份新**快照，旧的一个字都不改（`capture._refresh_queues` 同一条理由）。"""
        self._snapshot = {
            "recordings": [
                {
                    "name": recording.name,
                    "transport_addr": recording.transport_addr,
                    "ep": recording.ep,
                    "chunks": len(recording.chunks),
                    "span_s": recording.span_s,
                    "notes": recording.notes,
                }
                for recording in self._recordings
            ],
            "rejected": list(self._rejected),
            "unmatched": dict(self._unmatched),
            "done": sorted(self._done),
        }

    def problem(self) -> str | None:
        """只有"配了回放却没演起来"才说话。没配目录不是故障。

        `aggregator_role` 也不报：那一台按设计就没有采集（`capture`/`dialer` 同一条纪律），
        而它连自己的桥都没有，回放本就不该在那儿演——`status().refused` 里写清理由就够了。
        """
        snapshot = self._snapshot
        if self.role_refused == "capture_disabled":
            return "回放目录已配置，但采集层没在跑（capture_enabled=0），一个字节都没喂"
        if any(entry["reason"] == "dir_missing" for entry in snapshot["rejected"]):
            missing = snapshot["rejected"][0]["file"]
            return f"回放目录不存在：{missing}"
        if snapshot["rejected"]:
            return f"{len(snapshot['rejected'])} 份回放文件读不了（见 replay.rejected）"
        if snapshot["unmatched"]:
            return f"{len(snapshot['unmatched'])} 份回放没有可喂的节点行（见 replay.unmatched）"
        return None

    def status(self) -> dict[str, Any]:
        """纯内存快照，可在另一个线程里读（见 `_publish`）。"""
        snapshot = self._snapshot
        return {
            "enabled": self.enabled,
            "dir": None if self.settings.replay_dir is None else str(self.settings.replay_dir),
            "loop": self.settings.replay_loop,
            "running": self.running,
            "refused": self.role_refused,
            "source": SOURCE,
            "recordings": list(snapshot["recordings"]),
            "rejected": list(snapshot["rejected"]),
            "unmatched": dict(snapshot["unmatched"]),
            "done": list(snapshot["done"]),
            "chunks_fed": self.chunks_fed,
            "passes": self.passes,
            "problem": self.problem(),
        }
