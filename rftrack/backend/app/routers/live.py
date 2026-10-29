"""`WS /ws/live?site=`：S6 的推送口，router 只做 accept、参数与扇出目标。

形状与 `routers/ws.py` 一致，两条理由也一致：**先 accept 再判断**，让 `reason` 原样落到页面上；
解析要读一次库（场地行在不在），所以放进线程，慢库不许卡住别的连接。

与桥的那条唯一的区别是这里没有"上游"：这一页订的是我们自己的运行态，场地 id 就是分桶的键，
`LiveHub` 之外的任何东西都不认识它。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket

from ..services.live import resolve_site
from ..services.ws_bridge import Refused, refuse

router = APIRouter(tags=["live"])


@router.websocket("/ws/live")
async def live_socket(websocket: WebSocket) -> None:
    """把一个场地推给这一页，直到它离开。"""
    await websocket.accept()
    app = websocket.app
    try:
        site_id = await asyncio.to_thread(
            resolve_site, app.state.db, dict(websocket.query_params)
        )
        await app.state.live.serve(websocket, site_id)
    except Refused as refused:
        await refuse(websocket, refused)
