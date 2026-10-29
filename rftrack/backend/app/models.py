"""SQLAlchemy 模型（单文件，技能基线）。

层级：site 场地 → relay 中继（物理位置）→ gateway 接入机 → node 节点。
节点上不存 site_id：场地与位置一律经 gateway → relay → site 推导，避免三处双写不一致。
例外是 S6 那两张只追加的观测表——它们按"每一次查询都不 join"设计，冗余列由采集层写一次。
时间一律 aware UTC，禁止 naive/aware 混用。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime | None) -> datetime | None:
    """把"从库里读回来的 naive"或"别的时区"统一成 aware UTC。

    这个函数存在的理由是一条**实测事实**（`tests/test_retention.py` 钉住它）：SQLite 方言的
    `DATETIME(timezone=True)` 写进去的是**不带偏移的字符串**，而且它不做换算——
    `20:00+08:00` 落成 `'… 20:00:00'`，读回来是 naive 的 20:00。也就是说：

    - 读侧拿到的 naive 值**就是 UTC 墙上时间**，必须重新贴上 UTC 才能和 `utcnow()` 比；
    - 写侧传一个非 UTC 的 aware 时间进去，会被当成它自己的墙上时间存错 8 小时。

    所以任何跨这条边界的时刻都先过这里，而不是在各个 service 里各写一遍 `replace`。
    naive 一律按 UTC 解释（我们的列只由 `utcnow()` 写过）；认不出来的类型不猜，抛出去。
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"期望 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


SITE_STATUSES = ("active", "archived")
GATEWAY_KINDS = ("usb-bridge", "mcu-relay", "local-dev")
#: mcu-relay 是方式③（F2）预留，第一版只建枚举不实现。
DIAL_STATES = ("online", "unreachable", "dialing")
NODE_STATUSES = ("active", "lost", "disabled")
SITE_ORIGINS = ("local", "aggregated")
#: 插件的作用域档位。与方案 §3 里 `alert_rule.scope`（S8）同一套词，但**不含 `node`**：
#: 规则锁到单块板子会退化成"每块板一条规则"，而这三支规则的判断对象本来就是跨节点的。
PLUGIN_SCOPES = ("global", "site", "relay", "gateway")
#: `stopped → starting → running → failing → stopped`（方案 §2 的状态机表）。
PLUGIN_STATUSES = ("stopped", "starting", "running", "failing")
PLUGIN_LOG_LEVELS = ("info", "warning", "error")


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


# --- S6：采样摘要与信号事件 ---------------------------------------------------

#: 来源词表说的是**这串字节从哪儿来的**，不是数据新不新鲜。
#: `real` = 此刻接在桥上的板子发出来的；`mock` = 解析了但来源是回放/注入（含本仓库的合成
#: 录制）。第三个词 `demo` 属于页面自己的演示数，**不进这张表**——写进来就等于把演示数存成观测量。
#: AI 文本另立 `llm_note`，不进这里（草案 §5 与 llm-integration.md 的同一口径）。
EVENT_SOURCES = ("real", "mock")

#: 事件 `kind` 的词表**刻意不加 CHECK 约束**：S7 的三个插件与 S8 的告警都会往这里加条目，
#: 每认一种就要改一次表结构，而那正是"迁移债"。认识的 kinds 只影响渲染与文档，
#: 不认识的原样存——把对端/算法报的词改成我们猜的近义词，等于擦掉排查线索。
CAPTURED_EVENT_KINDS = ("node_offline", "node_online")

#: 摘要行的两个派生量。`ema_*` 可空不是"忘了写"：`LEVELS` 帧的 RSSI 模式确实不测 EMA
#: （`frame_parser.parse_levels` 回 `rssiEma: None`），那一格必须是 NULL，不能是 0。
SUMMARY_SERIES = ("rssi", "ema")


class SignalEvent(Base):
    """一条**观测事实**。写它的必须是采集层本身，不是推理——推理结果属 S7/S8 的表。

    `site_id` 与 `gateway_id` 是**故意的冗余列**（草案 §5 / 方案 §3）：按场地过滤事件流、
    按场地分级保留、按场地归档这三件事都要 `WHERE site_id=…`，穿三表 join 去推导场地
    在 Pi 的 SD 卡上不叫查询，叫自残。写入时由采集层从 `gateway → relay → site` 解析一次，
    推导链与 `routers/nodes.py` 用的是同一条。
    """

    __tablename__ = "signal_event"
    __table_args__ = (
        # 一条索引服务两件事：事件流按 `(site_id, ts)` 取最近 N 条，保留任务按
        # `(site_id, ts < 界限)` 逐场地删。第二件事走同一个索引的前缀，不必再建一条——
        # 每个索引都是每次写的额外页写，这是 R11 上直接可见的成本。
        Index("ix_event_site_ts", "site_id", "ts"),
    )

    event_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.site_id", ondelete="RESTRICT"), nullable=False
    )
    gateway_id: Mapped[int] = mapped_column(
        ForeignKey("gateway.gateway_id", ondelete="RESTRICT"), nullable=False
    )
    #: 可空是给"没有具体节点也值得记一条"留的位置（整台接入机判死）。
    #: S6 的采集层每条都带 node，所以这里不会真的为空——但约束不该挡住 S7 的事实。
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("node.node_id", ondelete="RESTRICT")
    )
    slot: Mapped[int | None] = mapped_column(Integer)
    #: 与 `channel_plan_item.freq_khz` 同一个量纲（kHz 整数），页面上不出现第二种写法。
    freq_khz: Mapped[int | None] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    #: 事件说的那一个量：`rssi` / `ema` / `snr`。`kind` 说发生了什么，`metric` 说从哪个量看出来的。
    metric: Mapped[str | None] = mapped_column(String(16))
    #: 触发这条读数的那个值，单位随 `metric` 走（dBFS）。没有数值的事件（状态跃迁）为 NULL，
    #: 不写 0——0 dBFS 是"满量程"，是一个很强的假话。
    value: Mapped[float | None] = mapped_column(Float)
    #: 读数的来路质量标记（如 `saturated`、`partial`）。S6 不写它：目前没有可断言的来源，
    #: 空着比造一个"看起来正常"强。
    quality: Mapped[str | None] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="real")
    #: 事件的全部上下文（帧里的原始字段、断流秒数、回放文件名）。禁止只留一句人话结论：
    #: 规范 §10.5 要的是证据链，S8 的复核要点开它。
    payload_json: Mapped[str | None] = mapped_column(Text)


class SampleSummary(Base):
    """采样**摘要**：唯一入库的采样形态（原始 RSSI 序列只进内存环形缓冲，不落库）。

    主键就是草案里那句"唯一键 `(node_id, slot, ts_bucket)`"——**用复合主键而不是自增 id
    加一条 UNIQUE**：同一格同一桶重写必须落在同一行上（聚合是幂等的，重放一遍不该长出新行），
    而在 SD 卡上多一列 id、多一个索引页就是每次写多一份磨损（R11）。

    `bucket_s` 记的是**写这行时生效的桶宽**。它不进主键：桶宽是全局键，改一次就会让
    "1 秒的旧行"和"5 秒的新行"混在同一张表里，那时读侧必须说得清存的是哪几种粒度
    （`routers/samples.py` 如实回 `bucket_widths`），而不是假装全库一种。
    """

    __tablename__ = "sample_summary"
    __table_args__ = (
        PrimaryKeyConstraint("node_id", "slot", "ts_bucket"),
        # 方案 §3 的判据原文：`site_id` 过滤不得走全表扫。这一条同时是趋势取数的路径
        # （`WHERE site_id=? AND node_id=? AND slot=? AND ts_bucket BETWEEN ? AND ?`
        # 四个约束全在索引列上），所以不再另建 `(site_id, ts_bucket)`——理由同 `SignalEvent`。
        Index("ix_summary_site_node_slot_bucket", "site_id", "node_id", "slot", "ts_bucket"),
    )

    node_id: Mapped[int] = mapped_column(
        ForeignKey("node.node_id", ondelete="RESTRICT"), primary_key=True
    )
    #: 频道槽位（0 基），与 `Settings.slots_per_node`、`config_layer` 的 `slots` 键同一口径。
    slot: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 桶起点的 unix 秒（UTC）：`floor(ts / bucket_s) * bucket_s`。存整数而不是 datetime，
    # 是因为桶聚合要做整除对齐，而 SQLite 的 datetime 做不了。
    ts_bucket: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.site_id", ondelete="RESTRICT"), nullable=False
    )
    gateway_id: Mapped[int] = mapped_column(
        ForeignKey("gateway.gateway_id", ondelete="RESTRICT"), nullable=False
    )
    bucket_s: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    #: 这一格里有多少个点：`mean` 是逐点平均还是逐桶平均，全靠这个数说话。
    sample_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 其中**带 EMA 的**点数。它必须单独存：`LEVELS` 的 RSSI 模式一条 EMA 都不带，
    #: 只有一个 `sample_count` 时 `ema_mean` 的分母就成了"所有点"，那是个算错了但看不出错的数。
    ema_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rssi_mean: Mapped[float | None] = mapped_column(Float)
    rssi_min: Mapped[float | None] = mapped_column(Float)
    rssi_max: Mapped[float | None] = mapped_column(Float)
    #: 桶内**最后一个**读数：趋势图连线到"现在"靠它，不是靠 `ts_bucket` 的右边界。
    rssi_last: Mapped[float | None] = mapped_column(Float)
    ema_mean: Mapped[float | None] = mapped_column(Float)
    ema_min: Mapped[float | None] = mapped_column(Float)
    ema_max: Mapped[float | None] = mapped_column(Float)
    #: 桶内最后一个**带 EMA 的**读数（不是"最后一个点"那一格的 EMA——那一条可能压根没测）。
    ema_last: Mapped[float | None] = mapped_column(Float)
    #: 读数来源，与 `SignalEvent.source` 同一套词：摘要行也必须能说出"这是回放出来的"。
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="real")
    #: 桶内最后一个点的**观测时刻**（不是桶边界）。取数侧靠它回答"这行有多新"。
    last_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PluginRegistry(Base):
    """一支**内置**插件的登记行。这不是"装了什么第三方插件"——方案 §11 明写不做动态代码加载。

    行由 `services/registry.py` 在启动时按代码里的注册表同步：代码里有而库里没有的插进来
    （`enabled=0`），库里没有对应实现的原样留着并记一条 log。**用户能改的只有五样**：
    `enabled` / `scope` / `scope_id` / `queue_limit` / `config_json`；`name`/`version`/`summary`
    由代码那一份覆写，因为页面上"插件叫什么、第几版"必须是可追责的、不是可编辑的。
    """

    __tablename__ = "plugin_registry"

    plugin_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    #: 整套 S7 的回滚判据就是"全部 `enabled=0` 即等效不存在"，所以默认必须是 0。
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="global")
    #: `scope` 不是 `global` 时它指向哪一行，由 `scope` 决定是 site/relay/gateway 哪张表——
    #: 所以这里**不能建外键**（多态指向），校验只能落在服务层：`scope_id` 必须真实存在，
    #: 且必须落在 `scope` 那一档上（拿 relay 的 id 当 site 用要直接 422，不许静默按全局跑）。
    scope_id: Mapped[int | None] = mapped_column(Integer)
    #: NULL = 跟随全局键 `plugin_queue_limit`；写数字 = 这一支插件自己的上限。
    queue_limit: Mapped[int | None] = mapped_column(Integer)
    config_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PluginState(Base):
    """一支插件**当前**的运行态：一行一支，主键就是插件 id（不是自增 id——状态是幂等 upsert 的）。

    计数器全部 `nullable=False` 且默认 0：`/api/health` 的 `problems` 与插件页都直接读这些数，
    而"读不到"和"是 0"是两件事。留 NULL 会诱导读侧写 `or 0` 之类的兜底，那正好把"这一列根本没被
    写过"这种真故障掩掉（同 `SignalEvent.value` 不写 0 的理由）。
    """

    __tablename__ = "plugin_state"

    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_registry.plugin_id", ondelete="RESTRICT"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stopped")
    #: 最近一次真正进入 `running` 的时刻。停用的插件保留旧值不清空——"上次跑到什么时候"
    #: 是排查的一部分，清了它就只剩"现在是 stopped"这一句废话。
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_tick_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: 退避重启的下一个可尝试时刻。supervisor 每轮先看它，没到点就不重启，
    #: 这样"崩溃—重启"的频率由这一列控制，而不是靠 sleep 常数散在各处。
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    restarts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    produced: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: 各网关队列丢的最旧批次数之和（明细在 `stats_json`）。
    dropped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    #: 每 (插件, 网关) 一条队列的 `{depth, peak, dropped, limit}` 快照，形状与
    #: `GatewayQueue.stats()` 同构；"A 场地打满不吃 B 场地配额"这条判据就看这里。
    stats_json: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class PluginLog(Base):
    """插件的状态跃迁与错误流水。**只记事件，不记逐帧日志**——每帧一条的话，这支插件本身就是
    新的写放大源（R11 在 SD 卡上的老问题），而那正是它要防的事。
    """

    __tablename__ = "plugin_log"
    __table_args__ = (Index("ix_plugin_log_plugin_ts", "plugin_id", "ts"),)

    log_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_registry.plugin_id", ondelete="RESTRICT"), nullable=False
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    level: Mapped[str] = mapped_column(String(8), nullable=False, default="info")
    #: 机器可分支的事件码（`enabled`/`started`/`crashed`/`restarting`/`backoff_exhausted`…）。
    #: `message` 给人看，`event` 给断言与页面图标用——只靠 message Substring 匹配的测试会随
    #: 措辞改动一起失效，所以两者都要有。
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str | None] = mapped_column(Text)
    detail_json: Mapped[str | None] = mapped_column(Text)


class PluginFinding(Base):
    """插件的**推导**结果，与 `signal_event`（观测事实）**分表**。

    分表不是洁癖：`SignalEvent` 的 docstring 写着"写它的必须是采集层本身，不是推理"，而任务书
    §10.5 要"人工复核与算法推测分开记录"。混在一张表里，S8 的复核页就得靠一个字段区分"这块板
    安静了"和"这条像是干扰"——那正是它最容易看错的地方。

    `evidence_json` 与 `signal_event.payload_json` 同一条纪律：禁止只留一句人话结论，规范 §10.5
    的证据链要能点开看（关联型判定的证据是**每个越限节点自己的读数**——节点、峰值、槽位、时刻，
    见 `interference-correlator`；它不引用别的判定的 id，因为插件只看得到交进来的那一批点）。
    """

    __tablename__ = "plugin_finding"
    __table_args__ = (
        # 一条索引同时服务"按场地取最近 N 条判定"与保留任务"按场地删旧行"，
        # 理由与 `SignalEvent` 的 `ix_event_site_ts` 完全一样（每个索引都是每次写的额外页写）。
        Index("ix_finding_site_ts", "site_id", "ts"),
    )

    finding_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    #: 判定**成立**的时刻，取触发它的最后一帧的观测时刻，而不是写库时刻——回放一批历史数据时
    # 这两个值能差一小时，而页面上的时间轴必须落在观测那一侧。
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: 冗余到底：与事件表同一个理由，按场地过滤与按场地分级保留都不许穿 join。
    site_id: Mapped[int] = mapped_column(
        ForeignKey("site.site_id", ondelete="RESTRICT"), nullable=False
    )
    gateway_id: Mapped[int | None] = mapped_column(
        ForeignKey("gateway.gateway_id", ondelete="RESTRICT")
    )
    node_id: Mapped[int | None] = mapped_column(
        ForeignKey("node.node_id", ondelete="RESTRICT")
    )
    slot: Mapped[int | None] = mapped_column(Integer)
    plugin_id: Mapped[str] = mapped_column(
        ForeignKey("plugin_registry.plugin_id", ondelete="RESTRICT"), nullable=False
    )
    #: `rule:<plugin_id>@<version>`：`docs/architecture/llm-integration.md` §4 给 LLM 产物定的
    # producer 形状，规则产物沿用同一套——页面上"这是谁说的"永远只有一个问法。
    producer: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    metric: Mapped[str | None] = mapped_column(String(16))
    value: Mapped[float | None] = mapped_column(Float)
    #: 判定当时生效的那条阈值（生效值，不是内置默认）。没有它，"为什么这条会响"就只能猜。
    threshold: Mapped[float | None] = mapped_column(Float)
    #: 来路质量与观测同源：回放/模拟数据推出的判定必须带着 `mock`，否则一条推论会冒充实时。
    source: Mapped[str] = mapped_column(String(8), nullable=False, default="real")
    evidence_json: Mapped[str | None] = mapped_column(Text)
