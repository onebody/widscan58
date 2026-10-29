"""`/ws/bridge/{ep}` 的上游侧：一个端点一条上游、N 页共享、命令只有一个写者。

三条口径都来自"改了就不是透传"，`web/serve.mjs` 是照着它抄的：

- **上游只用桥自己的说法** `ws://<host>/serial?ep=<id>`。`serial:` / `uart:` / `node:` 三种
  id 由桥自己分发，`uart:` 会连带停 RotorHazard（serve.mjs:484）。后端不另发明一条路径，
  也不预先判断 id 存不存在——桥会用 `unknown endpoint <ep>` 自己回答，那个理由比我们猜的可靠。
- **只读扇出 + 写命令单写者锁**：桥本身对同一端点只放一把钥匙（第二个人直接 `busy` 关掉，
  serve.mjs:382）。后端把这一位挪到自己这边来管：谁先**发命令**谁持有写权，后来者发命令
  就吃到同一个词 `busy`；只看不发的页永远不占写权。于是"一块板几块屏"不再需要有人一直让着。
- **文本帧不透传**：桥明写"1 text / 10 pong: unused"（serve.mjs:333）。后端替它收下就等于
  把桥丢掉的字节送到板上，语义变了。这里计数不静默（`text_frames_ignored`）。

上游客户端抽成 `factory.connect(url)`：测试交一条假的进来就能测扇出与写权，
不需要起任何临时服务。真实现用 `websockets`。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Mapping, Protocol
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import WebSocket, WebSocketDisconnect
from starlette.status import WS_1008_POLICY_VIOLATION, WS_1011_INTERNAL_ERROR
from websockets.exceptions import ConnectionClosed
from websockets.asyncio.client import connect as websocket_connect

from ..config import Settings
from ..database import Database
from ..models import Gateway
from .instance import effective_instance_role

#: 桥给页面用的唯一一条透传路径（`/node?id=` 是给中继节点本身连进来的，不是给页面的）。
BRIDGE_WS_PATH = "/serial"
#: 拒绝第二把钥匙的词与桥保持一致：页面不必为后端再学一套理由表。
BUSY = "busy"
#: 关闭理由的长度上限：Starlette 允许 123 字节，桥自己只截 100，我们跟桥一致。
REASON_LIMIT = 100


class UpstreamClosed(Exception):
    """上游连接结束。`code`/`reason` 是桥给的那对，原样往外传，不翻译。"""

    def __init__(self, code: int = 1000, reason: str = "") -> None:
        super().__init__(f"upstream closed: {code} {reason}")
        self.code = code
        self.reason = reason


class Refused(Exception):
    """连接可以建立，但这条端点归不了属（或写权不在你手上）。

    先 accept 再带理由关闭，是桥自己的做法（serve.mjs:370 的注释）：握手被拒只会让页面
    报一句笼统的"连不上桥"，而关闭理由能原样显示出来。
    """

    def __init__(self, reason: str, code: int = WS_1008_POLICY_VIOLATION) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class Upstream(Protocol):
    """桥的那一侧需要我们的全部三件事。"""

    async def send(self, payload: bytes) -> None: ...

    async def recv(self) -> bytes: ...

    async def close(self, code: int = 1000, reason: str = "") -> None: ...


class WebsocketsUpstream:
    """一条真的 `websockets` 客户端连接，收敛成上面那三个方法。"""

    def __init__(self, connection) -> None:
        self._connection = connection

    async def send(self, payload: bytes) -> None:
        await self._connection.send(payload)

    async def recv(self) -> bytes:
        try:
            frame = await self._connection.recv()
        except ConnectionClosed as closed:
            raise UpstreamClosed(closed.code, closed.reason or "") from None
        # 桥只发二进制帧；真收到文本帧就按字节继续透传，不替它决定算什么。
        return frame if isinstance(frame, bytes) else frame.encode()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._connection.close(code=code, reason=reason[:REASON_LIMIT])


class WebsocketFactory:
    """生产用的上游工厂。"""

    async def connect(self, url: str) -> Upstream:
        return WebsocketsUpstream(await websocket_connect(url))


@dataclass(frozen=True)
class Link:
    """一条已经定好归属的连接目标：哪台接入机、哪个端点、上游地址是什么。"""

    gateway_id: int
    ep: str
    url: str


def bridge_ws_url(gateway: Gateway, ep: str) -> str:
    """`base_url` 换成 ws/wss + 桥的 `/serial?ep=`；host 与端口原样沿用，不补默认值。"""
    parts = urlsplit(gateway.base_url)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, BRIDGE_WS_PATH, urlencode({"ep": ep}), ""))


def resolve_link(db: Database, settings: Settings, query: Mapping[str, str], ep: str) -> Link:
    """把 `?gateway=` 与 `{ep}` 落成一条确定的上游。说不清的那一条直接拒绝，不猜。

    `gateway` 必填与 REST 侧同一条理由：多接入机并跑时，同一个 `/dev/widscan58-2` 在两台
    机器上是两块板，"默认第一台"会安静地连错板。
    """
    raw = (query.get("gateway") or "").strip()
    if not raw:
        raise Refused("gateway_required")
    try:
        gateway_id = int(raw)
    except ValueError:
        raise Refused("gateway_not_found") from None
    with db.scope() as session:
        if effective_instance_role(session, settings.instance_role) == "aggregator":
            raise Refused("aggregator_role")
        gateway = session.get(Gateway, gateway_id)
        if gateway is None:
            raise Refused("gateway_not_found", WS_1008_POLICY_VIOLATION)
        if not gateway.base_url:
            raise Refused("no_base_url")
        return Link(gateway_id=gateway.gateway_id, ep=ep, url=bridge_ws_url(gateway, ep))


def _close_reason(reason: str) -> str:
    return (reason or "")[:REASON_LIMIT]


async def _close_quietly(websocket: WebSocket, code: int, reason: str) -> None:
    """关一条对客连接。已经没了就当成功——对面走了不是故障。"""
    try:
        await websocket.close(code=code, reason=_close_reason(reason))
    except (RuntimeError, WebSocketDisconnect):
        pass


async def refuse(websocket: WebSocket, refused: Refused) -> None:
    """router 唯一需要的外带动作：把 `Refused` 变成页面看得见的关闭理由。"""
    await _close_quietly(websocket, refused.code, refused.reason)


@dataclass
class _Session:
    """一个端点在后台的全部状态：一条上游、若干读者、至多一个写者。"""

    link: Link
    upstream: Upstream
    pump: asyncio.Task | None = None
    #: 页序号 → 连接。序号在一次会话内单调递增，写者按序号记，日志与状态里都指得清是谁。
    readers: dict[int, WebSocket] = field(default_factory=dict)
    writer: int | None = None
    next_page: int = 1
    fanned: int = 0
    text_frames_ignored: int = 0
    dropped_pages: int = 0

    def add_reader(self, websocket: WebSocket) -> int:
        page = self.next_page
        self.next_page += 1
        self.readers[page] = websocket
        return page

    def as_dict(self) -> dict:
        return {
            "gateway_id": self.link.gateway_id,
            "ep": self.link.ep,
            "url": self.link.url,
            "subscribers": len(self.readers),
            "writer": self.writer,
            "fanned": self.fanned,
            "text_frames_ignored": self.text_frames_ignored,
            "dropped_pages": self.dropped_pages,
        }


class BridgeHub:
    """`/ws/bridge` 的调度核心。一条端点一条上游，页面多少页都只是它的读者。

    三条不能含糊的口径：
    - **扇出不再开第二把钥匙**：两个读者必须共用一条上游，否则桥那边就报 `busy`，
      第二页永远连不上——那正是"后端反而不如直连"的事故。
    - **打开上游按端点串行、跨端点并行**：一把全局锁会让一台连不上的机器拖住全场；
      不加锁则两页同时首连会各开一条上游。用 per-key 锁（`dict.setdefault` 在单个循环里
      不会被打断），既串行又不互相牵连。
    - **任何一页的死活都不许停掉上游的读取**：读者发送失败就从名单里摘掉，泵继续跑。
    """

    def __init__(self, upstream_factory) -> None:
        self.factory = upstream_factory
        self._sessions: dict[tuple[int, str], _Session] = {}
        self._opening: dict[tuple[int, str], asyncio.Lock] = {}

    # --- 对客入口 ------------------------------------------------------------

    async def serve(self, websocket: WebSocket, link: Link) -> None:
        """接一页进来直到它离开。`Refused` 是唯一往外抛的异常，其余都在内部收口。"""
        key = (link.gateway_id, link.ep)
        session = await self._join(key, link)
        page = session.add_reader(websocket)
        try:
            await self._client_loop(websocket, session, page)
        finally:
            await self._leave(key, session, page)

    async def _join(self, key, link: Link) -> _Session:
        lock = self._opening.setdefault(key, asyncio.Lock())
        async with lock:
            session = self._sessions.get(key)
            if session is not None:
                return session
            try:
                upstream = await self.factory.connect(link.url)
            except Exception as exc:  # 连不上桥：说得出是哪个异常，别只说"失败"
                raise Refused(f"upstream_{type(exc).__name__}") from exc
            session = _Session(link=link, upstream=upstream)
            session.pump = asyncio.create_task(self._pump(key, session))
            self._sessions[key] = session
            return session

    async def _client_loop(self, websocket: WebSocket, session: _Session, page: int) -> None:
        while True:
            try:
                message = await websocket.receive()
            except (WebSocketDisconnect, RuntimeError):
                return  # 页面走了：客户端循环的正常终点，不是错误
            if message["type"] == "websocket.disconnect":
                return
            payload = message.get("bytes")
            if payload is None:
                session.text_frames_ignored += 1
                continue
            if session.writer is None:
                session.writer = page
            elif session.writer != page:
                raise Refused(BUSY)
            try:
                await session.upstream.send(payload)
            except UpstreamClosed as closed:
                raise Refused(closed.reason or "upstream_gone", closed.code or 1011) from None
            except Exception as exc:
                raise Refused(f"upstream_{type(exc).__name__}") from exc

    # --- 上游侧 --------------------------------------------------------------

    async def _pump(self, key, session: _Session) -> None:
        """读上游、扇出。这一段是"live 数据可以丢，读循环不能停"的对客版本。"""
        try:
            while True:
                await self._fan_out(session, await session.upstream.recv())
        except asyncio.CancelledError:
            raise  # 是我们最后一页走时主动收的摊，不必再通知任何人
        except UpstreamClosed as closed:
            await self._drop(key, session, closed.code, closed.reason)
        except Exception as exc:
            await self._drop(key, session, WS_1011_INTERNAL_ERROR, f"upstream_{type(exc).__name__}")

    async def _fan_out(self, session: _Session, payload: bytes) -> None:
        dead = []
        for page, reader in list(session.readers.items()):
            try:
                await reader.send_bytes(payload)
            except Exception:  # 发不出去就是这一页没了；别的页与上游都不受影响
                dead.append(page)
        for page in dead:
            if session.readers.pop(page, None) is not None:
                session.dropped_pages += 1
                if session.writer == page:
                    session.writer = None
        session.fanned += 1

    async def _leave(self, key, session: _Session, page: int) -> None:
        session.readers.pop(page, None)
        if session.writer == page:
            session.writer = None
        if session.readers:
            return
        # 最后一页走了就把端点交还给桥：桥靠这次断开释放串口并把 RotorHazard 起回来。
        # 关不回 1000 就等于把这块板永久占在一个已经关掉的标签页上。
        self._sessions.pop(key, None)
        if session.pump is not None:
            session.pump.cancel()
            await asyncio.gather(session.pump, return_exceptions=True)
        try:
            await session.upstream.close(1000, "no more pages")
        except Exception:
            pass  # 上游已经没了就是我们想要的结果，不是新故障

    async def _drop(self, key, session: _Session, code: int, reason: str) -> None:
        """上游先结束：把所有读者一起关掉，理由原样传出去（桥说了什么就是什么）。"""
        if self._sessions.get(key) is not session:
            return  # `_leave` 已经收过摊了
        self._sessions.pop(key, None)
        pages = list(session.readers.items())
        session.readers.clear()
        session.writer = None
        for _, reader in pages:
            await _close_quietly(reader, code or 1011, reason)
        try:
            await session.upstream.close(code or 1011, reason)
        except Exception:
            pass

    # --- 对外呈现 ------------------------------------------------------------

    def status(self) -> dict:
        """纯内存快照：谁在看哪块板、写权在谁手上。健康检查每 2 s 读一次，不开会话。"""
        return {
            "sessions": [session.as_dict() for session in self._sessions.values()],
            "upstreams": len(self._sessions),
        }
