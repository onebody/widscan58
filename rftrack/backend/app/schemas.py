"""Pydantic 入/出参。S2 只覆盖层级三级与节点读取，S4 加设置系统与布局。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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


# --- S4：设置系统与布局 -------------------------------------------------------


class GlobalSettingsUpdate(BaseModel):
    """`version` 必填：乐观并发不许客户端"忘了带版本"就把别人的改动盖掉。

    没带过版本的第一个客户端要先 GET 一次，拿到的 `version` 是 0（库里还没有那一行）。
    """

    version: int = Field(ge=0)
    settings: dict[str, Any]
    #: 需要重启采集的键必须逐个列在这里，否则 409 `confirm_required`。
    confirm: list[str] = Field(default_factory=list)


class SiteSettingsUpdate(BaseModel):
    """场地层写入。契约里只有全局层带 `version`，场地层的并发覆盖由来源链回显兜。"""

    settings: dict[str, Any]


class LocalImportCreate(BaseModel):
    """原 `web/` SPA 的 localStorage 一次性导入。

    `entries` 就是浏览器里那份原样导出：键 → 值，值通常是 JSON 字符串。后端不要求前端
    先翻译——翻译规则（哪条 legacy 键落到哪个注册表键）只许有一份，放在服务里。
    """

    source: str = Field(default="localStorage", min_length=1, max_length=64)
    entries: dict[str, Any]


class LayoutCreate(BaseModel):
    site_id: int
    name: str = Field(min_length=1, max_length=120)
    relay_id: int | None = None


class LayoutClone(BaseModel):
    """另存为的新名字；不给就是"原名 + 副本"，撞名了回 409 而不是自动编号。"""

    name: str | None = Field(default=None, min_length=1, max_length=120)


class LayoutUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    relay_id: int | None = None
    #: 布局级覆写：与场地/实例同一套键注册表，`null` = 取消这一层的覆写。
    defaults: dict[str, Any] | None = None


class LayoutOut(ORMModel):
    layout_id: int
    site_id: int
    relay_id: int | None
    name: str
    version: int
    is_active: bool
    #: 解析后的布局层覆写。库里存坏了就是 None + `declaration_error`，不 500 也不猜。
    defaults: dict[str, Any] | None = None
    declaration_error: str | None = None
    #: 结构版本比这份代码认识的还高：降级渲染，原因写清楚，不静默改用户的布局。
    degraded: bool = False
    degrade_reason: str | None = None
    widget_count: int = 0
    created_at: datetime
    updated_at: datetime


class WidgetUpdate(BaseModel):
    """`PUT /api/layouts/{id}/widgets/{instance_id}` 的**替换**语义。

    `config` 是这一层的完整期望内容：没出现的键就是"这层不设了"，回落到上层。
    位置与 `sort` 可选，缺省保持原样（拖格子不该顺带把配置重写一遍）。
    """

    plugin_id: str | None = Field(default=None, min_length=1, max_length=120)
    pos_x: int | None = Field(default=None, ge=0)
    pos_y: int | None = Field(default=None, ge=0)
    pos_w: int | None = Field(default=None, ge=1)
    pos_h: int | None = Field(default=None, ge=1)
    sort: int | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class WidgetOut(BaseModel):
    instance_id: str
    layout_id: int
    plugin_id: str
    pos_x: int
    pos_y: int
    pos_w: int
    pos_h: int
    sort: int
    config: dict[str, Any] = Field(default_factory=dict)
    declaration_error: str | None = None
    #: 生效值 + 来源链（默认 < 全局 < 场地 < 布局 < 实例）。
    effective: dict[str, Any] = Field(default_factory=dict)
