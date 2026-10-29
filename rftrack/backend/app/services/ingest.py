"""端点表 → 节点行：serve.mjs 的 `/api/endpoints` 是本系统唯一的节点事实来源。

写入路径只有这里（读侧是 `routers/nodes.py`），所以三条硬口径在本文件收口：

- **身份键 `(gateway_id, transport_addr)`**（唯一约束 `uq_node_gateway_addr`）。
  显示名一律不参与去重：两台接入机各报一块 `/dev/widscan58-1` 就是两块板，
  按名字合并会让一个物理位置的板子"消失"在另一个位置的列表里。
- **只落对端声明过的东西**。`/api/endpoints` 到今天不报固件版本、不报协议版本、
  不报任何数据能力（见 §7 S2 偏差①），所以 `fw_version`/`protocol_version`/
  `capabilities_json` 三列必须留 NULL，由读侧渲染成 `unsupported`。显式声明走
  `PUT /api/nodes/{id}`（契约 §4），不在这里代填。
- **消失只转 `lost`，不删行**。删了就无法回答"那块板昨天还在不在"，而那是判
  "接触不良"还是"真被拔走"的唯一证据。

`transport_kind` 的三种叫法与 `models.Node` 的注释对齐：
`usb-native`（①USB 原生）、`uart-pl011`（②a，已裁定形态）、`node-ws`（③，F2 后置）。
对端如果出现我们不认识的 `kind`，**原样存它自己的说法**——重命名成我们猜的类别，
等于把"对端说了什么"这条排查线索擦掉。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Gateway, Node, utcnow
from .capabilities import stops_rotorhazard
from .hierarchy import conflict

#: serve.mjs 的 `kind` → 本系统的介质名。认不出的 kind 原样落库（见模块注释）。
TRANSPORT_BY_KIND = {
    "serial": "usb-native",
    "uart": "uart-pl011",
    "remote": "node-ws",
}

#: 节点只有三个态：列出来了且接得上 / 列出来了但接不上 / 这轮没再列出来。
NODE_ACTIVE = "active"
NODE_OFFLINE = "offline"
NODE_LOST = "lost"


@dataclass(frozen=True)
class SyncResult:
    """一轮重枚举的增量。`seen` 是本轮在场的全部地址，供上层与健康检查对齐口径。"""

    gateway_id: int
    created: tuple[str, ...] = ()
    updated: tuple[str, ...] = ()
    lost: tuple[str, ...] = ()
    seen: tuple[str, ...] = ()
    #: 同一份端点表里地址重复的条目：不新建行、也不静默吞掉。
    duplicates: tuple[str, ...] = ()

    @property
    def changed(self) -> int:
        return len(self.created) + len(self.updated) + len(self.lost)


def endpoint_addr(endpoint: dict) -> str:
    """串口端点用 `path`；③ 的远端节点没有 path，只能落在 `id` 上。

    两者都没有就是没法寻址——宁可直接拒绝，也不让 `transport_addr` 落成空串：
    空串在唯一约束下会把这台接入机的所有匿名端点悄悄并成一行。
    """
    addr = endpoint.get("path") or endpoint.get("id")
    if not addr:
        raise conflict("endpoint_unidentified", endpoint=endpoint)
    return addr


def transport_medium(endpoint: dict) -> str:
    """桥的 `kind` → 我们的介质名；认不出的原样保留它自己的说法。"""
    kind = endpoint.get("kind")
    return TRANSPORT_BY_KIND.get(kind, kind or "unknown")


def _declared_facts(endpoint: dict, addr: str) -> tuple[str, str, bool]:
    """端点声明里能决定节点身份的那几项：显示名、介质、接入它会不会停 RH。"""
    kind = transport_medium(endpoint)
    return endpoint.get("label") or addr, kind, stops_rotorhazard(kind, False)


def reconcile(
    session: Session, gateway: Gateway, endpoints: list[dict], settings: Settings
) -> SyncResult:
    """把一份端点表对账进 node 表；同一份表重复调用必须得到同样的行集。

    超限（`max_nodes_per_gateway`）在动任何一行之前就 409，并点名是哪些端点：
    半落地状态比拒绝更难排查，静默丢弃则会让用户以为"加了机器怎么没数据"。
    """
    if len(endpoints) > settings.max_nodes_per_gateway:
        raise conflict(
            "max_nodes_per_gateway",
            gateway_id=gateway.gateway_id,
            limit=settings.max_nodes_per_gateway,
            endpoints=[ep.get("label") or ep.get("id") for ep in endpoints],
        )

    known = {
        row.transport_addr: row
        for row in session.execute(
            select(Node).where(Node.gateway_id == gateway.gateway_id)
        ).scalars()
    }
    created: list[str] = []
    updated: list[str] = []
    duplicates: list[str] = []
    seen: set[str] = set()

    for endpoint in endpoints:
        addr = endpoint_addr(endpoint)
        name, kind, stops_rh = _declared_facts(endpoint, addr)
        active = bool(endpoint.get("online"))
        status = NODE_ACTIVE if active else NODE_OFFLINE
        row = known.get(addr)
        if row is None:
            row = Node(
                gateway_id=gateway.gateway_id,
                display_name=name,
                transport_kind=kind,
                transport_addr=addr,
                status=status,
                stops_rh=stops_rh,
                last_seen=utcnow() if active else None,
            )
            session.add(row)
            # 本轮新建的行也要进 known：对端如果把这块板报了两遍（symlink 指向同一个
            # 设备是这么表现的），第二次必须走"已存在"分支，否则唯一约束在 flush 时炸。
            known[addr] = row
            created.append(addr)
        else:
            # `last_seen` 是流水账，不计入"声明变了"：否则每轮重枚举都报一次增量。
            facts = (row.display_name, row.transport_kind, row.stops_rh, row.status)
            if facts != (name, kind, stops_rh, status):
                updated.append(addr)
            row.display_name = name
            row.transport_kind = kind
            row.stops_rh = stops_rh
            row.status = status
            if active:
                row.last_seen = utcnow()
        if addr in seen:
            duplicates.append(addr)
        seen.add(addr)

    lost = [addr for addr in known if addr not in seen]
    for addr in lost:
        known[addr].status = NODE_LOST

    session.flush()
    return SyncResult(
        gateway_id=gateway.gateway_id,
        created=tuple(created),
        updated=tuple(updated),
        lost=tuple(lost),
        seen=tuple(sorted(seen)),
        duplicates=tuple(duplicates),
    )


def advertise(endpoints: list[dict], gateway_id: int) -> list[dict]:
    """把桥的端点表渲染成对外的样子：**桥的字段一个都不删**，只往上加我们的分类。

    加的两列是运营者必须先看到的：`medium`（②a 还是 ①还是③）与 `stops_rh`
    （接入它会不会停掉这台接入机的 RotorHazard 计时，R21）。后者由介质决定，
    桥说过与否都不改结论——写成 UI 文案就成了"提示"，写进数据才是约束。
    """
    out = []
    for endpoint in endpoints:
        kind = transport_medium(endpoint)
        out.append(
            {**endpoint, "gateway_id": gateway_id, "medium": kind, "stops_rh": stops_rotorhazard(kind, False)}
        )
    return out


def mark_gateway_lost(session: Session, gateway: Gateway) -> int:
    """接入机判死 → 其下节点连带 `lost`（行全部保留）。

    这条必须在"拨号连续失败到阈值"之后才调（§1：一次超时不判死），
    所以调用方是 dialer 的退避逻辑，不是每一次抓取失败。
    """
    rows = session.execute(
        select(Node).where(Node.gateway_id == gateway.gateway_id)
    ).scalars()
    changed = 0
    for row in rows:
        if row.status != NODE_LOST:
            row.status = NODE_LOST
            changed += 1
    session.flush()
    return changed
