"""代理指定接入机的 serve.mjs：端点表 + 板卡归属切换（契约 §4 的 `bridge` 行）。

两条硬口径：
- **只转发不改语义**。字段原样带出（因此**故意不套 `response_model`**：套了就会把
  我们不认识的字段静默丢掉，而"对端说了什么"是排查的第一手证据），
  `/api/mode` 的状态码与响应体也必须是桥自己给的那个——把 409 吞成 200 等于
  绕过"这块板正在计时"这条硬约束。
- **多网关后必须带 `gateway` 参数**，否则不知道该切哪台。

端点表拉不通一律 502，原因词表沿用 `dialer.classify`（`dns`/`refused`/`timeout`/
`http_status`/`unexpected_response`/`empty_endpoints`）：空表回 200 加空数组，在 UI
上和"桥没连上"长得一模一样，必须分开说。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..deps import get_session, require_writable_session
from ..models import Gateway
from ..services.dialer import ENDPOINTS_PATH, MODE_PATH, TRANSPORT_DETAILS, bridge_url, classify
from ..services.hierarchy import get_or_404
from ..services.ingest import advertise

router = APIRouter(tags=["bridge"])


def _gateway(session: Session, gateway: int) -> Gateway:
    return get_or_404(session, Gateway, gateway, "gateway")


def _unreachable(gateway_id: int, reason: str, detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_502_BAD_GATEWAY,
        {"reason": reason, "detail": detail, "gateway_id": gateway_id},
    )


@router.get("/api/endpoints")
def list_endpoints(
    request: Request,
    gateway: int = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    """这台接入机当前的端点表，外加 `medium` / `stops_rh` 两列我们的分类。"""
    row = _gateway(session, gateway)
    result = request.app.state.transport.fetch(
        bridge_url(row, ENDPOINTS_PATH), request.app.state.settings.probe_timeout_s
    )
    ok, reason, detail, endpoints = classify(result)
    if not ok:
        raise _unreachable(row.gateway_id, reason, detail)
    return {"gateway_id": row.gateway_id, "endpoints": advertise(endpoints, row.gateway_id)}


@router.post("/api/board/mode")
def switch_board_mode(
    request: Request,
    body: dict[str, Any],
    gateway: int = Query(...),
    session: Session = Depends(require_writable_session),
) -> Response:
    """`{mode: "scan" | "rh"}` 原样转给那台机器的 `/api/mode`，状态码一并转回来。

    这是"写"：它会动到物理板子，所以汇聚实例上 405（S4 权限裁定）。`GET /api/endpoints`
    不走这条——读端点表正是汇聚实例该干的事。
    """
    row = _gateway(session, gateway)
    result = request.app.state.transport.post(
        bridge_url(row, MODE_PATH), body, request.app.state.settings.probe_timeout_s
    )
    if result.error:
        raise _unreachable(
            row.gateway_id, result.error, TRANSPORT_DETAILS.get(result.error, result.error)
        )
    if result.payload is None:
        # 连上了但回的不是 JSON（中间挂了反向代理最常见）：
        # 不能编一个空对象交回去，那会被读成"桥同意了"。
        return JSONResponse(
            status_code=result.status_code or status.HTTP_502_BAD_GATEWAY,
            content={
                "reason": "unexpected_response",
                "detail": f"桥回了 HTTP {result.status_code} 但不是 JSON",
                "gateway_id": row.gateway_id,
            },
        )
    return JSONResponse(
        status_code=result.status_code or status.HTTP_502_BAD_GATEWAY,
        content=result.payload,
    )
