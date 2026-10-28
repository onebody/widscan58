"""节点能力渲染：只把**声明过**的能力当能力。

口径来自 `docs/architecture/topology-and-access-methods.md` §5 与落地方案 §5.2：
- 没声明 → `unsupported`，软件侧不得用插值、复制上一帧或降采样"补"出来；
- 有界能力（UART 上的 spectrum 被固件钳到 512 bins、转速被节流）必须带上限，
  只说"支持"不说上限，等同于把有界渲染成无界——同样是伪造；
- 认不出的声明原样呈现，不静默丢掉（静默丢会让"对端说了什么"无法排查）。
"""

from __future__ import annotations

import json

#: 本系统认识的能力名。不在这里的都不许影响渲染。
KNOWN_CAPABILITIES = ("levels", "spectrum", "rawiq")

#: 必须带上限才算声明支持的能力 → 允许的限字段。
BOUNDED_CAPABILITIES = {"spectrum": ("max_bins", "paced_rotation_hz")}

#: 介质决定"接入它会不会停掉这台接入机的 RotorHazard 计时"，不由数据行说了算（R21）。
MEDIUMS_THAT_STOP_RH = ("uart-pl011",)


def _supported(value) -> bool:
    if isinstance(value, dict):
        return bool(value.get("supported"))
    return value is True


def _limits(name: str, value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in BOUNDED_CAPABILITIES.get(name, ())
        if isinstance(value.get(key), (int, float)) and value[key]
    }


def render_capabilities(raw: str | None) -> tuple[dict, str | None]:
    """返回 (渲染结果, 声明本身的问题)。"""
    undeclared = {
        name: {"supported": False, "reason": "undeclared"} for name in KNOWN_CAPABILITIES
    }
    if raw in (None, ""):
        return undeclared, None
    try:
        declared = json.loads(raw)
    except ValueError:
        return undeclared, "malformed_json"
    if not isinstance(declared, dict):
        return undeclared, "not_an_object"

    rendered: dict = {}
    for name in KNOWN_CAPABILITIES:
        if name not in declared:
            rendered[name] = {"supported": False, "reason": "undeclared"}
            continue
        value = declared[name]
        if not _supported(value):
            rendered[name] = {"supported": False, "reason": "declared_unsupported"}
            continue
        limits = _limits(name, value)
        if name in BOUNDED_CAPABILITIES and not limits:
            rendered[name] = {"supported": False, "reason": "missing_limits"}
            continue
        rendered[name] = {"supported": True, **limits}

    for name, value in declared.items():
        if name not in rendered:
            rendered[name] = {"supported": None, "reason": "unknown_capability", "declared": value}
    return rendered, None


def stops_rotorhazard(transport_kind: str, declared_flag: bool) -> bool:
    """②a 一定会停计时，所以这一位不允许被数据行写成 False。"""
    return declared_flag or transport_kind in MEDIUMS_THAT_STOP_RH
