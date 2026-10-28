"""全部环境变量在此收口：本地开发用默认值，部署用 RFTRACK_* 覆盖。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = BACKEND_DIR / "data" / "rftrack.sqlite3"

#: 汇聚实例只做只读拉取，不接受本地拨号（见 docs/rf-track/ 落地方案 §5）。
INSTANCE_ROLES = ("site", "aggregator")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        if self.instance_role not in INSTANCE_ROLES:
            raise ValueError(
                f"instance_role 只能是 {INSTANCE_ROLES} 之一，收到 {self.instance_role!r}"
            )

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
        )

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.db_path}"
