"""全部环境变量在此收口：本地开发用默认值，部署用 RFTRACK_* 覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "rftrack.sqlite3"
#: 工作台是同源静态目录（不起 CORS、也不给页面第二个入口）。
#: 相对 `backend/` 定位而不是相对 cwd：uvicorn 从哪儿起都不影响页面能不能打开。
DEFAULT_WORKBENCH_DIR = BACKEND_DIR.parent / "frontend" / "workbench"

#: 汇聚实例只做只读拉取，不接受本地拨号（见 docs/rf-track/ 落地方案 §5）。
INSTANCE_ROLES = ("site", "aggregator")


def _optional_int(value: str | None) -> int | None:
    """空串与未设置是同一件事："没有指定当前场地"，不得变成 0。"""
    return int(value) if value not in (None, "") else None


def _optional_path(value: str | None, default: Path) -> Path | None:
    """路径位上「未设置」与「设成空串」是**两种不同**的意图，不像 `_optional_int`。

    未设置 → 用仓库里的默认目录；空串 → 明确不要这个目录（只跑 API）。把它折叠成
    "都算默认"就等于让 `RFTRACK_WORKBENCH_DIR=` 变成一个什么都没发生的配置。
    """
    if value is None:
        return default
    return Path(value) if value else None


#: 布尔开关的取值只认这几个写法；其余一律报错，不"看起来像真就当真"。
BOOL_TRUE = ("1", "true", "yes", "on")
BOOL_FALSE = ("0", "false", "no", "off")


def _bool(value: str, name: str) -> bool:
    lowered = value.strip().lower()
    if lowered in BOOL_TRUE:
        return True
    if lowered in BOOL_FALSE:
        return False
    raise ValueError(f"{name}={value!r} 只能是 {BOOL_TRUE + BOOL_FALSE} 之一")


@dataclass(frozen=True)
class Settings:
    db_path: Path = DEFAULT_DB_PATH
    bind: str = "127.0.0.1"
    port: int = 9000
    #: 数据库里没有 app_config 行、或该列为 NULL 时的兜底值。
    instance_role: str = "site"
    #: 单次 probe 的等待预算；场地内的桥就在同一段局域网，3 s 不返回就是不通。
    probe_timeout_s: float = 3.0
    #: 连续失败到几次才落下 unreachable（§1：一次超时不判死）。
    dial_fails_to_unreachable: int = 2
    #: 一台接入机最多落地多少个节点。**这是"总并发上有硬闸"的配置位，不是实测上限**：
    #: 超限时同步直接 409 并点名是哪些端点，绝不允许静默丢掉新接入的板
    #: （否则运营者只会看到"加了机器怎么没数据"）。默认 8 = 一台带 USB hub 的桥的合理规模，
    #: 真实上限待 S9 在 Pi 上压测后再调。
    max_nodes_per_gateway: int = 8
    #: 一台接入机最多同时挂几台——`store` 的点数预算按它算，dialer 的并发闸也按它卡。
    max_gateways: int = 8
    #: 每接入机一条有界队列的容量（帧数）。打满就丢最旧，绝不 backpressure 到读循环。
    #: 256 帧在 ②a 的档位下是几秒的量：宁可丢历史也要保"现在"，因为 live 视图只读最新。
    queue_limit: int = 256
    #: 环形缓冲每 (网关, 端点, 槽位) 保留的点数。300 点在 8 频道跳频档位下是几十秒到
    #: 一两分钟——趋势图要的是"刚才那一段"，不是全量历史（全量走 003 的桶摘要）。
    ring_depth_per_slot: int = 300
    #: 一块板的频道槽位数：本阶段按现有 8 频道方案算（40+ 频道属固件 M3，不在 S3）。
    slots_per_node: int = 8
    #: 同时处于"正在拨"状态的接入机数。局域网里几十台桥同时握手会把 serve.mjs 的
    #: HTTP 面压出假超时，所以并发要小于机器数——真值待 S9 压测。
    dial_concurrency: int = 4
    #: 后台循环的节奏：健康的接入机每隔这么多秒重枚举一次，规划任务也按它扫一遍
    #: 该拨谁（新登记的机器因此不用重启进程就会被接上）。5 s 是"板子插回 USB 后几秒内
    #: 网页就该看见它"的体感要求，不是实测值。
    dial_poll_interval_s: float = 5.0
    #: 后台拨号循环的总开关。默认开：S3 交付的就是"没人点也会自己更新节点表"。
    #: 关掉只用于测试与"先登记配置、稍后再开始采集"的场合，关了健康检查会照实说没启用。
    dialer_enabled: bool = True
    #: 退避：1 s 起、每次翻倍、封顶 30 s（拔了重插的体感上限）。
    dial_backoff_base_s: float = 1.0
    dial_backoff_max_s: float = 30.0
    #: 本场地实例只拨这个场地的接入机（§5："配置里出现远端场地地址直接报错，不静默接受"）。
    #: None = 库里只有一个场地时不必指定；S4 起 `app_config.current_site_id` 优先于它，
    #: 环境变量留作部署时的强制覆盖位。
    current_site_id: int | None = None
    # --- S4：保留与摘要的平台默认值（方案 §3"全部可 env 覆盖，默认值即落地值"）-----------
    #: 这三条是 SD 磨损 R11 的对策位，`app_config`/`site` 的覆写为空就落到这里。
    retention_events_days: int = 7
    retention_summary_days: int = 30
    retention_log_rows: int = 5000
    #: 桶宽必须 ≥1 s：比这更细就是把 SD 写穿（§2.4 原文），所以它是校验而不是偏好。
    summary_bucket_s: int = 1
    #: 原始 IQ 默认不入库——Pi 只有这一张卡。
    rawiq_persist: bool = False
    #: 组件刷新的下限（毫秒）。这是 UI/渲染预算不是实测值：低于它，一屏 24 个组件
    #: 就把主线程变成刷屏，而帧本来就只按桥的节拍来（默认 200 = 5 fps）。
    refresh_ms_floor: int = 200
    #: 工作台静态目录。S5 起由后端同源挂在 `/workbench/`；`RFTRACK_WORKBENCH_DIR=` 
    #: （空串）= 这台实例只提供 API，页面另说——那时 `/api/health` 的 `workbench` 一节
    #: 如实回 `mounted=false`，不让运营者对着 404 猜是哪一步没做。
    workbench_dir: Path | None = DEFAULT_WORKBENCH_DIR

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        if self.workbench_dir is not None:
            object.__setattr__(self, "workbench_dir", Path(self.workbench_dir))
        if self.instance_role not in INSTANCE_ROLES:
            raise ValueError(
                f"instance_role 只能是 {INSTANCE_ROLES} 之一，收到 {self.instance_role!r}"
            )
        # 下面三条都是"配错会让现象极难倒推回配置"的取值，所以在构造时就拦住。
        if self.dial_concurrency < 1:
            raise ValueError("dial_concurrency 至少 1：0 会让拨号循环永久卡在并发闸上")
        if self.dial_poll_interval_s <= 0 or self.dial_backoff_base_s <= 0:
            raise ValueError("轮询与退避的秒数必须大于 0：否则循环会把自己钉在 CPU 上")
        if self.dial_backoff_max_s < self.dial_backoff_base_s:
            raise ValueError("退避封顶不能小于起始值")
        if self.dial_fails_to_unreachable < 1:
            raise ValueError("dial_fails_to_unreachable 至少 1：0 等于每轮都判死")
        # 保留策略配成负数不是"更激进地清理"，是"永远清理不动"或"把库删空"，两种都别要。
        if self.retention_events_days < 0 or self.retention_summary_days < 0:
            raise ValueError("保留天数不能为负：0 已经表示「只留当下」，负数没有意义")
        if self.summary_bucket_s < 1:
            raise ValueError("summary_bucket_s 至少 1 s：桶更细会直接把 SD 卡写穿（R11）")
        if self.refresh_ms_floor < 1:
            raise ValueError("refresh_ms_floor 至少 1 ms；0 会让刷新变成紧循环")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        source = os.environ if env is None else env
        return cls(
            db_path=Path(source.get("RFTRACK_DB_PATH", DEFAULT_DB_PATH)),
            bind=source.get("RFTRACK_BIND", "127.0.0.1"),
            port=int(source.get("RFTRACK_PORT", "9000")),
            instance_role=source.get("RFTRACK_INSTANCE_ROLE", "site"),
            probe_timeout_s=float(source.get("RFTRACK_PROBE_TIMEOUT_S", "3.0")),
            dial_fails_to_unreachable=int(
                source.get("RFTRACK_DIAL_FAILS_TO_UNREACHABLE", "2")
            ),
            max_nodes_per_gateway=int(source.get("RFTRACK_MAX_NODES_PER_GATEWAY", "8")),
            max_gateways=int(source.get("RFTRACK_MAX_GATEWAYS", "8")),
            queue_limit=int(source.get("RFTRACK_QUEUE_LIMIT", "256")),
            ring_depth_per_slot=int(source.get("RFTRACK_RING_DEPTH_PER_SLOT", "300")),
            slots_per_node=int(source.get("RFTRACK_SLOTS_PER_NODE", "8")),
            dial_concurrency=int(source.get("RFTRACK_DIAL_CONCURRENCY", "4")),
            dial_poll_interval_s=float(source.get("RFTRACK_DIAL_POLL_INTERVAL_S", "5.0")),
            dialer_enabled=_bool(source.get("RFTRACK_DIALER_ENABLED", "1"), "RFTRACK_DIALER_ENABLED"),
            dial_backoff_base_s=float(source.get("RFTRACK_DIAL_BACKOFF_BASE_S", "1.0")),
            dial_backoff_max_s=float(source.get("RFTRACK_DIAL_BACKOFF_MAX_S", "30.0")),
            current_site_id=_optional_int(source.get("RFTRACK_SITE_ID")),
            retention_events_days=int(source.get("RFTRACK_RETENTION_EVENTS_DAYS", "7")),
            retention_summary_days=int(source.get("RFTRACK_RETENTION_SUMMARY_DAYS", "30")),
            retention_log_rows=int(source.get("RFTRACK_RETENTION_LOG_ROWS", "5000")),
            summary_bucket_s=int(source.get("RFTRACK_SUMMARY_BUCKET_S", "1")),
            rawiq_persist=_bool(
                source.get("RFTRACK_RAWIQ_PERSIST", "0"), "RFTRACK_RAWIQ_PERSIST"
            ),
            refresh_ms_floor=int(source.get("RFTRACK_REFRESH_MS_FLOOR", "200")),
            workbench_dir=_optional_path(
                source.get("RFTRACK_WORKBENCH_DIR"), DEFAULT_WORKBENCH_DIR
            ),
        )

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"
