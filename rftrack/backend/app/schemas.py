"""Pydantic 入/出参。S2 只覆盖层级三级与节点读取。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .services.network import require_lan_base_url


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class SiteCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    timezone: str = "UTC"
    description: str | None = None


class SiteUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = None
    description: str | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")
    retention_events_days: int | None = Field(default=None, ge=0)
    retention_summary_days: int | None = Field(default=None, ge=0)


class SiteOut(ORMModel):
    site_id: int
    name: str
    slug: str
    timezone: str
    description: str | None
    status: str
    origin: str
    retention_events_days: int | None
    retention_summary_days: int | None
    last_sync_at: datetime | None
    lag_s: int | None
    created_at: datetime
    updated_at: datetime


class RelayCreate(BaseModel):
    site_id: int
    name: str = Field(min_length=1, max_length=120)
    note: str | None = None


class RelayUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    note: str | None = None
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class RelayOut(ORMModel):
    relay_id: int
    site_id: int
    name: str
    note: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class LanBaseUrlMixin(BaseModel):
    """`base_url` 只接受局域网地址——新增与修改走同一条规则。"""

    base_url: str | None = Field(default=None, max_length=255)

    @field_validator("base_url")
    @classmethod
    def _must_be_lan(cls, value: str | None) -> str | None:
        return require_lan_base_url(value)


class GatewayCreate(LanBaseUrlMixin):
    relay_id: int
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(default="usb-bridge", pattern="^(usb-bridge|mcu-relay|local-dev)$")
    serial_prefix: str | None = Field(default=None, max_length=64)


class GatewayUpdate(LanBaseUrlMixin):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: str | None = Field(default=None, pattern="^(usb-bridge|mcu-relay|local-dev)$")
    serial_prefix: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, pattern="^(active|archived)$")


class GatewayOut(ORMModel):
    gateway_id: int
    relay_id: int
    site_id: int | None = None
    relay_name: str | None = None
    name: str
    kind: str
    base_url: str | None
    serial_prefix: str | None
    status: str
    dial_state: str | None
    dial_reason: str | None
    dial_failures: int
    last_seen: datetime | None


class NodeOut(BaseModel):
    """节点读数：场地/位置一律推导，不在 node 上存。"""

    node_id: int
    gateway_id: int
    relay_id: int
    site_id: int
    display_name: str
    transport: dict
    capabilities: dict
    #: 声明自身的问题（malformed_json / not_an_object）；能力全部按 unsupported 渲染，不猜。
    declaration_error: str | None = None
    fw_version: str | None
    protocol_version: int | None
    status: str
    last_seen: datetime | None
    stops_rh: bool
