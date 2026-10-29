"""节点读数。S2 只有读：节点行由 serve.mjs 的枚举落地（S3 ingest）后才会有写入路径。

身份键是 `(gateway_id, transport_addr)`，显示名不参与去重；场地/位置一律
经 gateway → relay → site 推导，一条 SQL 取回，避免 N+1 也避免在 node 上双写。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..deps import get_session
from ..models import Gateway, Node, Relay
from ..schemas import NodeOut
from ..services.capabilities import render_capabilities, stops_rotorhazard
from ..services.hierarchy import not_found

router = APIRouter(prefix="/api/nodes", tags=["nodes"])


def _query():
    return (
        select(Node, Relay.relay_id, Relay.site_id)
        .join(Gateway, Node.gateway_id == Gateway.gateway_id)
        .join(Relay, Gateway.relay_id == Relay.relay_id)
        .order_by(Node.node_id)
    )


def as_out(node: Node, relay_id: int, site_id: int) -> NodeOut:
    capabilities, declaration_error = render_capabilities(node.capabilities_json)
    return NodeOut(
        node_id=node.node_id,
        gateway_id=node.gateway_id,
        relay_id=relay_id,
        site_id=site_id,
        display_name=node.display_name,
        transport={"kind": node.transport_kind, "addr": node.transport_addr},
        capabilities=capabilities,
        declaration_error=declaration_error,
        fw_version=node.fw_version,
        protocol_version=node.protocol_version,
        status=node.status,
        last_seen=node.last_seen,
        stops_rh=stops_rotorhazard(node.transport_kind, node.stops_rh),
    )


@router.get("", response_model=list[NodeOut])
def list_nodes(
    site: int | None = Query(default=None),
    gateway: int | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[NodeOut]:
    """范围参数用契约 §4 的短名（`?site=`/`?gateway=`），与 `/api/endpoints?gateway=` 同一套词。"""
    query = _query()
    if gateway is not None:
        query = query.where(Node.gateway_id == gateway)
    if site is not None:
        query = query.where(Relay.site_id == site)
    return [as_out(*row) for row in session.execute(query)]


@router.get("/{node_id}", response_model=NodeOut)
def read_node(node_id: int, session: Session = Depends(get_session)) -> NodeOut:
    row = session.execute(_query().where(Node.node_id == node_id)).first()
    if row is None:
        raise not_found("node", node_id)
    return as_out(*row)
