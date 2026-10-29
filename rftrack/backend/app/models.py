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
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
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
    # --- S4：保留策略的全局覆写。NULL = 用 `config.py` 的内置默认（那一层才是平台默认值）。
    # 这四列必须追加在末尾：`create_all` 按声明顺序、`add_column` 按迁移顺序，两边都得落到
    # 同一个列序上，否则"两条建库路径结构相同"那条对拍会因为顺序而不是语义而失败。
    retention_events_days: Mapped[int | None] = mapped_column(Integer)
    retention_summary_days: Mapped[int | None] = mapped_column(Integer)
    summary_bucket_s: Mapped[int | None] = mapped_column(Integer)
    rawiq_persist: Mapped[bool | None] = mapped_column(Boolean)
    #: 本实例服务哪个场地（§5"只拨本场地"）。NULL = 退回 `RFTRACK_SITE_ID` 环境变量，
    #: 再退回"库里只有一个场地"那条推断。
    current_site_id: Mapped[int | None] = mapped_column(Integer)
    #: 全局层的 JSON 兜底：上面那些列之外、但仍然可以全局设默认的键（工作台的历史窗口、
    #: 阈值、频道槽位…）落在这里。理由不是"方便"——localStorage 导入要往全局层写
    #: `historySeconds`/`historyMetric`/`showAverage`，没有这一列它们就无处可去，
    #: 只能被记成 `unmapped`，而那等于"你的旧设置我们导不进来"。
    settings_json: Mapped[str | None] = mapped_column(Text)


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
    #: 场地级覆写：保留天数与告警默认值之外的所有"场地这一层想设的键"。
    #: 用 JSON 而不是每键一列——一列一次迁移，而"场地能不能覆写某键"是注册表里的事。
    #: 追加在末尾的理由同 `AppConfig`：两条建库路径的列序必须一致。
    settings_json: Mapped[str | None] = mapped_column(Text)

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


# --- S4：布局与实例局部配置 ---------------------------------------------------

#: 布局的 `is_active` 是唯一真源，不并存第二个 status 列：§1 三态里的 `active` 就是
#: `is_active=1`，"不要了"走 DELETE；`archived` 分组等 S5 的布局管理器需要时再加可空列。
LAYOUT_VERSION = 1


class WorkbenchLayout(Base):
    """一个场地里的工作台布局；`is_active` 的唯一性**按场地成立**。

    部分唯一索引（`WHERE is_active = 1`）就是"A 场地切布局不许动 B 场地的当前布局"
    这条判据的数据库落点——它比应用层检查可靠：两个请求同时抢"当前布局"时，
    库里只会接受一个。
    """

    __tablename__ = "workbench_layout"
    __table_args__ = (
        UniqueConstraint("site_id", "name", name="uq_layout_site_name"),
        Index(
            "uq_layout_active_site",
            "site_id",
            unique=True,
            sqlite_where=text("is_active = 1"),
        ),
    )

    layout_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 必填：布局属于场地，多场地定案后"全局布局"这个概念就不存在了。
    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.site_id", ondelete="RESTRICT"), nullable=False
    )
    #: 可空 = 不绑位置；绑了就是"这个位置的默认工作台"。
    relay_id: Mapped[int | None] = mapped_column(
        ForeignKey("relay.relay_id", ondelete="RESTRICT")
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    #: 布局结构版本：比代码认识的还高时降级并报原因，不 500、也不静默改用户的布局。
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=LAYOUT_VERSION)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: 布局级默认值（优先级里"布局"那一格的整份覆写）。
    defaults_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    site: Mapped[Site] = relationship()
    widgets: Mapped[list["LayoutWidget"]] = relationship(
        back_populates="layout", cascade="all, delete-orphan"
    )


class LayoutWidget(Base):
    """组件实例的局部配置——"改 A 的阈值不影响 B"靠的就是这张表按实例分行。"""

    __tablename__ = "layout_widget"

    #: 实例 id **在所属布局内**唯一（与 `layout_id` 组成复合主键，声明顺序即主键顺序）。
    #: 不选"全局唯一实例 id"：那样"另存为"就拷不走组件——两份布局里出现同一个主键，
    #: 要么撞库要么得给每个格子重发一个 id，而重发出来的 id 又要在响应里逐个回显给前端。
    #: 按布局作用域之后，"B 布局里没有一个叫 A 的实例"照样是 PUT 404，不是悄悄新建。
    instance_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    layout_id: Mapped[int] = mapped_column(
        ForeignKey("workbench_layout.layout_id", ondelete="CASCADE"), primary_key=True
    )
    plugin_id: Mapped[str] = mapped_column(String(120), nullable=False)
    pos_x: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pos_y: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pos_w: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    pos_h: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    #: 只存"这个实例明确设了的键"；键缺失 = 继承上层。不另存 is_explicit 标记，
    #: 那会是第二份可能与本值不同步的真相。
    config_json: Mapped[str | None] = mapped_column(Text)
    sort: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    layout: Mapped[WorkbenchLayout] = relationship(back_populates="widgets")


class LocalSettingsImport(Base):
    """原 `web/` SPA 的 localStorage 一次性导入留痕。

    留痕不是日志装饰：`payload_json` 存原始键值、`changes_json` 存每个落点的新旧值，
    回滚要照着它把 `app_config` 改回去；`unmapped_json` 记录"没认出来的键"——
    认不出来就原样列出来，不许猜一个映射把它塞进某一层。
    """

    __tablename__ = "local_settings_import"

    import_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    #: 来源标识（浏览器/文件/采集记录），回滚报告里要点名是哪一次。
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="localStorage")
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    changes_json: Mapped[str | None] = mapped_column(Text)
    unmapped_json: Mapped[str | None] = mapped_column(Text)
    #: 回滚只记时刻，不删行：留痕被改没了，下一次就无从对照。
    reverted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
