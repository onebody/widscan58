"""拨号器：对 serve.mjs 的 HTTP 探测 + 失败原因分类。

S2 只做"人工点一次的实测"（`POST /api/gateways/{id}/probe`）；定时轮询、退避与
静默判定属于 S6 的 dialer 循环，但**连续失败才落 unreachable** 这条规则现在就生效，
因为 §1 写明"不得因为一次超时就把整台机器判死"。

传输层被抽成 `transport.fetch(url, timeout_s)`：测试直接交进想要的事实，
不需要起一个假 HTTP 服务。
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from time import perf_counter
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Gateway, utcnow
from .hierarchy import conflict

#: serve.mjs 现在只公布这两个 HTTP 面；探测取前者，不发明新端点。
ENDPOINTS_PATH = "/api/endpoints"

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

    url = gateway.base_url.rstrip("/") + ENDPOINTS_PATH
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
