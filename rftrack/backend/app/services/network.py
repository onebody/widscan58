"""`base_url` 的局域网判定：整套系统本地化部署，接入机地址不允许指向公网或云。

用**显式白名单**而不是 `ipaddress` 的 `is_private`：后者把 100.64.0.0/10（运营商 CGNAT）
和 192.0.2.0/24（文档保留段）也算私有，那两类都不是场地里自己拉线的网络，
放过去就等于给"看起来像内网"的地址开了口子。
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

LAN_V4 = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
)
LAN_V6 = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),  # ULA
    ipaddress.ip_network("fe80::/10"),  # link-local
)
#: 只在本地解析器（mDNS / 内网 DNS）里成立的后缀；带点的其他域名一律按公网处理。
LOCAL_SUFFIXES = (".local", ".lan", ".home.arpa", ".home", ".internal", ".intra")

REASON = "base_url 必须是局域网地址（本系统不做云端部署，拒绝任何公网/云地址）"


def _in_networks(address, networks) -> bool:
    return any(address in network for network in networks)


def _why_rejected(value: str) -> str | None:
    """返回拒绝理由；None 表示这就是一个局域网地址。"""
    if not value.strip():
        return "不能是空串：还没接线就省略这个字段"
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return f"只允许 http/https，收到 {parts.scheme or '（缺 scheme）'!r}"
    if "@" in parts.netloc:
        return "地址里不要带用户名密码，它会漏进日志与归档"
    host = parts.hostname
    if not host:
        return "解析不出主机名"
    host = host.split("%")[0]  # IPv6 zone id 不参与判定
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host:
            return None  # 无点单标签名不可能在公网 DNS 上解析，只能是本地名
        if host.endswith(LOCAL_SUFFIXES):
            return None
        return f"{host} 是带点域名，只有 {LOCAL_SUFFIXES} 这些本地后缀算局域网"
    if address.version == 4 and _in_networks(address, LAN_V4):
        return None
    if address.version == 6 and _in_networks(address, LAN_V6):
        return None
    return f"{host} 不在局域网网段内"


def require_lan_base_url(value: str | None) -> str | None:
    """给 Pydantic 用：不合法就抛 ValueError，让接口回 422 并带上可解释的原因。"""
    if value is None:
        return None
    why = _why_rejected(value)
    if why:
        raise ValueError(f"{REASON}：{why}")
    return value
