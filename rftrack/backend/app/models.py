"""SQLAlchemy 模型（单文件，技能基线）。

层级：site 场地 → relay 中继（物理位置）→ gateway 接入机 → node 节点。
节点上不存 site_id：场地与位置一律经 gateway → relay → site 推导，避免三处双写不一致。
时间一律 aware UTC，禁止 naive/aware 混用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


SITE_STATUSES = ("active", "archived")
GATEWAY_KINDS = ("usb-bridge", "mcu-relay", "local-dev")
#: mcu-relay 是方式③（F2）预留，第一版只建枚举不实现。
DIAL_STATES = ("online", "unreachable", "dialing")
NODE_STATUSES = ("active", "lost", "disabled")
SITE_ORIGINS = ("local", "aggregated")


class Base(DeclarativeBase):
    pass


class AppConfig(Base):
    """实例级全局设置的唯一真源，约定只用 id=1 这一行。"""

    __tablename__ = "app_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 乐观并发用，写一次加一。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: NULL / 缺行 → 按 Settings.instance_role 兜底（默认 site）。
    instance_role: Mapped[str | None] = mapped_column(String(16))
    theme: Mapped[str | None] = mapped_column(String(16))
    lang: Mapped[str | None] = mapped_column(String(8))
    tz: Mapped[str | None] = mapped_column(String(64))
    units: Mapped[str | None] = mapped_column(String(16))
    default_refresh_ms: Mapped[int | None] = mapped_column(Integer)
    alert_defaults_json: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(64))
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Site(Base):
    __tablename__ = "site"
    __table_args__ = (UniqueConstraint("slug", name="uq_site_slug"),)

    site_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: 为空 = 继承全局保留策略（按场地分级是 SD 磨损 R11 的对策）。
    retention_events_days: Mapped[int | None] = mapped_column(Integer)
    retention_summary_days: Mapped[int | None] = mapped_column(Integer)
    #: aggregated 只出现在汇聚实例；此时 last_sync_at/lag_s 必须有值。
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lag_s: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    relays: Mapped[list["Relay"]] = relationship(back_populates="site")


class Relay(Base):
    """物理位置（看台 / 起点塔 / 计时区…）：归属与管理单位，本身不承载连接。"""

    __tablename__ = "relay"
    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_relay_site_name"),)

    relay_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.site_id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    site: Mapped[Site] = relationship(back_populates="relays")
    gateways: Mapped[list["Gateway"]] = relationship(back_populates="relay")


class Gateway(Base):
    """一台跑 serve.mjs 的机器；一个位置可挂多台。"""

    __tablename__ = "gateway"
    __table_args__ = (UniqueConstraint("relay_id", "name", name="uq_gateway_relay_name"),)

    gateway_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    relay_id: Mapped[int] = mapped_column(
        ForeignKey("relay.relay_id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="usb-bridge")
    base_url: Mapped[str | None] = mapped_column(String(255))
    serial_prefix: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    #: 未拨过 = NULL，健康检查如实计入 never_probed。
    dial_state: Mapped[str | None] = mapped_column(String(16))
    #: probe 的失败原因分类（dns/refused/timeout/http_status/unexpected_response/empty_endpoints），成功时为 NULL。
    dial_reason: Mapped[str | None] = mapped_column(String(32))
    #: 连续失败次数：§1 "不得因为一次超时就把整台机器判死" 的落点。
    dial_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    relay: Mapped[Relay] = relationship(back_populates="gateways")
    nodes: Mapped[list["Node"]] = relationship(back_populates="gateway")


class Node(Base):
    """节点身份的全局唯一键是 (gateway_id, transport_addr)，禁止按显示名全局去重。"""

    __tablename__ = "node"
    __table_args__ = (
        UniqueConstraint("gateway_id", "transport_addr", name="uq_node_gateway_addr"),
    )

    node_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    gateway_id: Mapped[int] = mapped_column(
        ForeignKey("gateway.gateway_id", ondelete="RESTRICT"), nullable=False
    )
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: usb-native / uart-pl011（②a 已裁定形态）/ node-ws（③，F2 后置）。
    transport_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    transport_addr: Mapped[str] = mapped_column(String(191), nullable=False)
    #: 显式声明的能力；缺字段一律渲染 unsupported，不得由软件补造。
    capabilities_json: Mapped[str | None] = mapped_column(Text)
    fw_version: Mapped[str | None] = mapped_column(String(64))
    protocol_version: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: ②a 端点会停掉该接入机的 RotorHazard 计时，这是能力位而不是实现细节（R21）。
    stops_rh: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    gateway: Mapped[Gateway] = relationship(back_populates="nodes")


class ChannelPlan(Base):
    """频道方案服务端化；relay_id 为空表示全场共享。"""

    __tablename__ = "channel_plan"

    plan_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    relay_id: Mapped[int | None] = mapped_column(
        ForeignKey("relay.relay_id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    band: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    items: Mapped[list["ChannelPlanItem"]] = relationship(
        back_populates="plan", cascade="all, delete-orphan"
    )


class ChannelPlanItem(Base):
    __tablename__ = "channel_plan_item"
    __table_args__ = (UniqueConstraint("plan_id", "slot_index", name="uq_plan_slot"),)

    item_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plan_id: Mapped[int] = mapped_column(
        ForeignKey("channel_plan.plan_id", ondelete="CASCADE"), nullable=False
    )
    slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    #: 统一 kHz 整数，消掉 web 侧 MHz / 固件侧 kHz 的口径分裂。
    freq_khz: Mapped[int] = mapped_column(Integer, nullable=False)

    plan: Mapped[ChannelPlan] = relationship(back_populates="items")
