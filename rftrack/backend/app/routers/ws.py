"""`WS /ws/bridge/{ep}`：契约 §4 的"字节透传"行，router 只负责 accept、参数与角色。

调度、扇出、写权全在 `services/ws_bridge.py`；这里唯一值得写下来的决定是
**先 accept 再判断**。理由抄自桥自己（serve.mjs:370 的注释）：握手阶段就被拒，页面只能
报一句笼统的"连不上桥"，而 accept 之后再关闭，`reason` 会原样显示在页面上。运营者区分
"没配 gateway"和"这台机器没地址"靠的就是那句话。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket

from ..services.ws_bridge import Refused, refuse, resolve_link

router = APIRouter(tags=["bridge"])


@router.websocket("/ws/bridge/{ep:path}")
async def bridge_socket(websocket: WebSocket, ep: str) -> None:
    """把一页接到它自己那台接入机的一个端点上，直到它离开。"""
    await websocket.accept()
    app = websocket.app
    try:
        # 解析要读一次库（gateway 行 + 实例角色）。放线程里，慢库不许卡住别的连接。
        link = await asyncio.to_thread(
            resolve_link, app.state.db, app.state.settings, dict(websocket.query_params), ep
        )
        await app.state.bridge.serve(websocket, link)
    except Refused as refused:
        await refuse(websocket, refused)
