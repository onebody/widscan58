"""配置的层级、生效值与来源链（规范 §4.1/§4.2 + 方案 §4 契约）。

一句话概括这个文件存在的理由：**"哪个值最终生效"这件事只能有一份算法**。路由、前端、
将来的 retention 清理与插件都从这里取值，谁也不许自己再走一遍优先级。

五条纪律决定了下面的形态：

1. **覆写 = 键存在，继承 = 键缺失**。不给每个键再配一个 `is_explicit` 标记——那是第二份
   可能与值本身不同步的真相。取消覆写走显式 `null`（"这层我不设了"），它和"这次请求没提
   这个键"是两件事，所以写入侧一律用 `exclude_unset`。
2. **取值域来自既有事实，不新造**：`theme`/`lang`/`history_metric` 的候选值抄自原 `web/`
   SPA（`main.ts` 的 dark|light、`i18n.ts` 的 en|zh、`views/history.ts` 的 ema|rssi），
   `threshold_db` 的量纲是协议自己的 **dBFS**（`web/src/proto/frames.ts:126` 的 int16/100），
   `time_range_s` 的默认 30 s 是原 `historySeconds`，`slots` 的上界是
   `Settings.slots_per_node`（今天 8，40+ 频道属固件 M3）。`units` 只有一个合法值 `dbfs`：
   把它做成可选项等于暗示存在一套换算，而它不存在。
3. **下级不得绕过硬件、资源与权限约束**（规范 §4.2 后半句）：`locked=True` 的键任何层级
   都拒写；`slots_per_node`、`refresh_ms_floor`、环形缓冲容量是**边界**而不是建议。
4. **要重启采集的键必须说明影响并要求确认**（规范 §4.2）：`requires_restart=True` 的键由
   路由侧带 `confirm`，这里只负责把它标出来。
5. **坏值不炸整个工作台**（规范 §4.4）：库里存坏的 JSON 一律"这一层视为未设置 + 把问题记
   进来源链回显"，让下一层生效，而不是 500。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AppConfig, LayoutWidget, Site, WorkbenchLayout, utcnow

#: 从低到高。`default` 是 `config.py` 里的平台默认值：它只被继承，永不被写。
LAYERS = ("default", "global", "site", "layout", "instance")

#: 键名 → 该层的**列**落点。没在表里的键走那一层的 JSON 覆写列（`JSON_FIELDS`）。
COLUMN_FIELDS: dict[str, dict[str, str]] = {
    "global": {
        "theme": "theme",
        "lang": "lang",
        "tz": "tz",
        "units": "units",
        "refresh_ms": "default_refresh_ms",
        "alert_defaults": "alert_defaults_json",
        "retention_events_days": "retention_events_days",
        "retention_summary_days": "retention_summary_days",
        "summary_bucket_s": "summary_bucket_s",
        "rawiq_persist": "rawiq_persist",
        "current_site_id": "current_site_id",
        # 只有读侧：见 KeySpec.locked。
        "instance_role": "instance_role",
    },
    # 场地有三个键本来就有专用列（§3 表结构），其余走 site.settings_json。
    "site": {
        "tz": "timezone",
        "retention_events_days": "retention_events_days",
        "retention_summary_days": "retention_summary_days",
    },
}

#: 每一层的"其它键"存进哪个 Text 列。global 也有：`app_config` 的列只装得上平台级的几个
#: 硬键，而"工作台默认历史窗口"这类仍然该有个全局默认。
JSON_FIELDS: dict[str, str] = {"global": "settings_json",
                               "site": "settings_json", "layout": "defaults_json",
                               "instance": "config_json"}


@dataclass(frozen=True)
class KeySpec:
    """一个可配置键的全部事实：谁能设、合法取值长什么样、默认从哪来。"""

    name: str
    kind: str  # choice | int | float | bool | text | slots | object
    #: 允许覆写的层（`default` 不在里面，它是兜底而不是可写层）。
    layers: tuple[str, ...]
    choices: tuple[str, ...] | None = None
    #: 数值边界。`*_from_settings` 指向 `Settings` 的字段名——边界跟着部署走，
    #: 不写死在代码里（同一份代码在 Pi 上和工作站上的合理上限本就不同）。
    minimum: int | float | None = None
    maximum: int | float | None = None
    minimum_from_settings: str | None = None
    maximum_from_settings: str | None = None
    #: 平台默认值来源：`Settings` 的字段名，或下面的字面量（None = 这层没有默认值）。
    default_from_settings: str | None = None
    default: Any = None
    locked: bool = False
    requires_restart: bool = False
    unit: str | None = None
    #: 今天只有生效值、还没有执行者的键。`None` 才是"这个值真的在管事"，所以每清掉一个
    #: 都得指着一段能跑的代码（`retention.purge_site`）或一张表，不能指着"阶段做完了"。
    pending_stage: str | None = None


def _bounds(spec: KeySpec, settings: Settings) -> tuple[Any, Any]:
    low = spec.minimum
    high = spec.maximum
    if spec.minimum_from_settings is not None:
        low = getattr(settings, spec.minimum_from_settings)
    if spec.maximum_from_settings is not None:
        high = getattr(settings, spec.maximum_from_settings)
    return low, high


# --- 键注册表 ---------------------------------------------------------------

GLOBAL_ONLY = ("global",)
SITABLE = ("global", "site")
WIDGETABLE = ("global", "site", "layout", "instance")

KEYS: tuple[KeySpec, ...] = (
    # 外观与语言：原 SPA 有这两个键，值域照抄，不扩。
    KeySpec("theme", "choice", WIDGETABLE, choices=("dark", "light"), default="dark"),
    KeySpec("lang", "choice", GLOBAL_ONLY, choices=("en", "zh"), default="en"),
    KeySpec("tz", "text", SITABLE, default="UTC"),
    KeySpec("units", "choice", GLOBAL_ONLY, choices=("dbfs",), default="dbfs", unit="dBFS"),
    # 采集节奏与显示窗口：刷新周期有下限，时间范围受环形缓冲容量约束。
    KeySpec(
        "refresh_ms",
        "int",
        WIDGETABLE,
        minimum_from_settings="refresh_ms_floor",
        maximum=600_000,
        default_from_settings="refresh_ms_floor",
        unit="ms",
    ),
    KeySpec(
        "time_range_s", "int", WIDGETABLE, minimum=1, maximum=86_400, default=30, unit="s"
    ),
    KeySpec(
        "threshold_db", "float", WIDGETABLE, minimum=-128, maximum=0, default=-85, unit="dBFS"
    ),
    # `slots` 的判定只有一处：`_validate_slots`（0 基、去重、非空、上界取 `slots_per_node`）。
    # 这里这两头仍然填，因为 `_bounds()` 的产物会进 `GET /api/settings/schema`——写给页面与
    # 任何按 schema 生成控件的工具看的。`minimum` 一度写成 1：那是把**显示**（1 基的"第 1 档"）
    # 当成了**存储**（0 基下标），而第 0 档完全合法（`models.py` 的 `slot` 列注释、
    # `routers/samples.py:45` 的闸、固件 `planner.c` 填的 plan 下标三处都是 0 基）。
    KeySpec(
        "slots", "slots", WIDGETABLE, minimum=0, maximum_from_settings="slots_per_node", default=None
    ),
    KeySpec("history_metric", "choice", WIDGETABLE, choices=("ema", "rssi"), default="ema"),
    KeySpec("show_average", "bool", WIDGETABLE, default=True),
    # 保留天数：S6 起了执行者（`retention.purge_site` 读的就是这里的生效值，两条 DELETE 都带
    # `site_id`），所以这两个键的 `pending_stage` 清掉了。摘要桶宽与 RAW IQ 落库还没有——见下。
    KeySpec(
        "retention_events_days",
        "int",
        SITABLE,
        minimum=0,
        maximum=3650,
        default_from_settings="retention_events_days",
        unit="d",
    ),
    KeySpec(
        "retention_summary_days",
        "int",
        SITABLE,
        minimum=0,
        maximum=3650,
        default_from_settings="retention_summary_days",
        unit="d",
    ),
    KeySpec(
        "summary_bucket_s",
        "int",
        GLOBAL_ONLY,
        minimum_from_settings="summary_bucket_s",
        maximum=3600,
        default_from_settings="summary_bucket_s",
        unit="s",
        # 聚合器已经在按桶写行（S6 `SummaryAggregator`），缺的是让**这一层的覆写**决定桶宽：
        # 读写两侧现在都取 `Settings.summary_bucket_s`（`routers/samples.py` 的 docstring 记着
        # 为什么必须同源）。桶宽一改就是 SD 磨损档位，与它的实测一起属 S9（R11）。
        pending_stage="S9",
    ),
    KeySpec(
        "rawiq_persist",
        "bool",
        GLOBAL_ONLY,
        default_from_settings="rawiq_persist",
        # 采集层一律不落 RAW IQ（R11 的对策之一），库里也没有存它的表——这个键今天只有生效值。
        # 要它有执行者，先得有 RAW IQ 的存储与磨损实测，同属 S9。
        pending_stage="S9",
    ),
    KeySpec("alert_defaults", "object", SITABLE, default={}),
    # 本实例服务哪个场地：改它就是换拨号对象，属于"要重启/重连"那一类。
    KeySpec(
        "current_site_id", "int", GLOBAL_ONLY, minimum=1, default=None, requires_restart=True
    ),
    # 权限位：实例角色不是"设置"，是部署事实。任何层级想覆写它都拒（§4.4 第十条）。
    KeySpec("instance_role", "choice", (), choices=("site", "aggregator"), locked=True),
)

BY_NAME: dict[str, KeySpec] = {spec.name: spec for spec in KEYS}
LOCKED_NAMES = frozenset(spec.name for spec in KEYS if spec.locked)


def spec_for(key: str) -> KeySpec | None:
    return BY_NAME.get(key)


# --- 校验 -------------------------------------------------------------------


def validate(settings: Settings, key: str, value: Any) -> str | None:
    """返回 None 表示合法，否则回一个稳定的 reason 词（供 422/409 与测试断言）。"""
    spec = BY_NAME.get(key)
    if spec is None:
        return "unknown_key"
    if spec.locked:
        return "protected_key"
    if spec.kind == "choice":
        if not isinstance(value, str):
            return "wrong_type"
        return None if value in (spec.choices or ()) else "not_a_choice"
    if spec.kind == "text":
        if not isinstance(value, str):
            return "wrong_type"
        return None if value.strip() and len(value) <= 64 else "out_of_range"
    if spec.kind == "bool":
        return None if isinstance(value, bool) else "wrong_type"
    if spec.kind in ("int", "float"):
        # bool 是 int 的子类，但 `true` 当刷新周期用一定是错的，这里不惯着。
        numbers = int if spec.kind == "int" else (int, float)
        if isinstance(value, bool) or not isinstance(value, numbers):
            return "wrong_type"
        low, high = _bounds(spec, settings)
        # 边界可以只有一头：`current_site_id` 只有下限，上限取决于库里有几个场地，
        # 那不是注册表能写的数。`None` 是"这一头不封顶"，不是"和 0 比"。
        if low is not None and value < low:
            return "out_of_range"
        if high is not None and value > high:
            return "out_of_range"
        return None
    if spec.kind == "slots":
        return _validate_slots(settings, value)
    if spec.kind == "object":
        return None if isinstance(value, dict) else "wrong_type"
    return "unknown_kind"  # 注册表里加了没人认识的 kind：宁可报错也不当合法放过


def _validate_slots(settings: Settings, value: Any) -> str | None:
    if not isinstance(value, list):
        return "wrong_type"
    if not all(isinstance(s, int) and not isinstance(s, bool) for s in value):
        return "wrong_type"
    if len(set(value)) != len(value):
        return "duplicate_slot"
    if not value:
        return "empty_slot_list"
    high = settings.slots_per_node
    # 超出频道数就是"想要还不支持的频道"（40+ 属固件 M3）：拒，不截断也不假装能跑。
    if any(s < 0 or s >= high for s in value):
        return "slot_out_of_range"
    return None


def capacity_error(settings: Settings, refresh_ms: int, time_range_s: int) -> str | None:
    """跨键的资源约束：要的点数装不进环形缓冲，就是"画一段并不存在的历史"。"""
    if refresh_ms <= 0:
        return "out_of_range"
    if (time_range_s * 1000) // refresh_ms > settings.ring_depth_per_slot:
        return "ring_capacity_exceeded"
    return None


def capacity_check(settings: Settings, effective: dict[str, dict]) -> dict | None:
    """对一份"生效值"算容量硬闸，回 None 表示装得下。

    判据只在这里写一遍：全局/场地层（`routers/settings.py`）与布局/实例层
    （`services/layouts.py`）必须用同一个口径，否则"局部设不了超限的组合"这句话就是假的
    ——规范 §4.4 第九条要的正是后者。回的是可以直接进 409 的 detail，包括算出来的点数：
    拒绝一个组合却不说明它要多少个点，运营者只能自己猜。
    """
    refresh = (effective.get("refresh_ms") or {}).get("value")
    span = (effective.get("time_range_s") or {}).get("value")
    if refresh is None or span is None:
        return None
    reason = capacity_error(settings, refresh, span)
    if reason is None:
        return None
    return {
        "reason": reason,
        "refresh_ms": refresh,
        "time_range_s": span,
        "points": (span * 1000) // refresh,
        "ring_depth_per_slot": settings.ring_depth_per_slot,
    }


# --- 读数 -------------------------------------------------------------------


@dataclass
class LayerRead:
    """一层对某个键的读数。`set=False` 就是"这层没设"，与"设了个 null"同义。"""

    layer: str
    value: Any = None
    set: bool = False
    declaration_error: str | None = None


def load_json(raw: str | None) -> tuple[dict | None, str | None]:
    """(对象, 问题)。坏 JSON 不抛异常——那一层当"未设置"，问题记进链里回显。"""
    if raw is None:
        return None, None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None, "malformed_json"
    if not isinstance(parsed, dict):
        return None, "not_an_object"
    return parsed, None


def _json_read(layer: str, raw: str | None, key: str) -> LayerRead:
    read = LayerRead(layer)
    parsed, problem = load_json(raw)
    if problem:
        read.declaration_error = problem
        return read
    if parsed and key in parsed and parsed[key] is not None:
        read.value = parsed[key]
        read.set = True
    return read


def _column_read(layer: str, obj: Any, key: str) -> LayerRead:
    field_name = COLUMN_FIELDS[layer].get(key)
    read = LayerRead(layer)
    if obj is None or field_name is None:
        return read
    value = getattr(obj, field_name, None)
    if value is None:
        return read
    if field_name.endswith("_json"):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            read.declaration_error = "malformed_json"
            return read
    read.value = value
    read.set = True
    return read


def platform_default(settings: Settings, spec: KeySpec) -> Any:
    if spec.default_from_settings is not None:
        return getattr(settings, spec.default_from_settings)
    return spec.default


# --- 生效值与来源链 ---------------------------------------------------------


@dataclass
class Resolved:
    """一个键的生效值 + 来源链。`source` 是最后真正设了它的那一层名。"""

    key: str
    value: Any
    source: str
    #: 生效值的单位（dBFS/ms/s…）。没有单位的键不回空串，回 None。
    unit: str | None
    chain: list[dict] = field(default_factory=list)
    #: 各层被拒的覆写与声明问题：看得见"这层确实设了个东西但没生效"，才谈得上回滚。
    rejected: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "value": self.value,
            "source": self.source,
            "unit": self.unit,
            "chain": self.chain,
            "rejected": self.rejected,
        }


def resolve(settings: Settings, spec: KeySpec, layers: list[LayerRead]) -> Resolved:
    """把各层读数按优先级折成一个生效值。

    `layers` 必须已按 `LAYERS` 从低到高排好；长度不必等于 5——调用方只关心自己那几层
    （场地的设置就没有 layout/instance 层），少掉的层就不出现在来源链里。
    """
    value = platform_default(settings, spec)
    source = "default"
    chain: list[dict] = []
    rejected: list[dict] = []
    for read in layers:
        entry: dict = {"layer": read.layer}
        if read.declaration_error:
            entry["declaration_error"] = read.declaration_error
            rejected.append({"layer": read.layer, "reason": read.declaration_error})
        if read.layer == "default":
            # 平台默认值不校验：它是这张表自己写的，校验它只会把"我们没有默认值"
            # （slots/current_site_id 的 None）误报成一个配置错误。
            entry["value"] = read.value
        elif read.set:
            problem = validate(settings, spec.name, read.value)
            if problem:
                entry.update(set_value=read.value, rejected=problem)
                rejected.append({"layer": read.layer, "value": read.value, "reason": problem})
            else:
                value = read.value
                source = read.layer
                entry["value"] = value
        chain.append(entry)
    return Resolved(
        key=spec.name, value=value, source=source, unit=spec.unit, chain=chain, rejected=rejected
    )


def read_app_config(session: Session) -> AppConfig | None:
    try:
        return session.scalar(select(AppConfig).where(AppConfig.id == 1))
    except Exception:  # 未迁移的库连表都没有：全局层就是"没设"，不炸。
        session.rollback()
        return None


def ensure_app_config(session: Session) -> AppConfig:
    """全局层的唯一落点，缺行就建（约定只用 `id=1`）。

    读侧允许"没有那一行"（等价于全是 NULL），写侧不行：不先有行就没地方存版本。
    表还不存在时这里会抛 `OperationalError`——那是"库没迁移就敢来写配置"，如实炸，
    不许退化成"写成功了但其实没写"。
    """
    config = session.get(AppConfig, 1)
    if config is None:
        # 0 而不是列默认值 1：客户端在"还没有这一行"时读到的是 version 0，第一次写就该
        # 变成 1，中间不许凭空多一次跳号。
        config = AppConfig(id=1, version=0)
        session.add(config)
        session.flush()
    return config


def touch(session: Session, config: AppConfig) -> AppConfig:
    """写一次加一版。`updated_by` 留给 S11：今天没有身份，不编一个名字进去。"""
    config.version = (config.version or 0) + 1
    config.updated_at = utcnow()
    session.flush()
    return config


def read_layers(
    settings: Settings,
    spec: KeySpec,
    *,
    session: Session | None = None,
    site: Site | None = None,
    layout: WorkbenchLayout | None = None,
    widget: LayoutWidget | None = None,
) -> list[LayerRead]:
    default_value = platform_default(settings, spec)
    reads = [LayerRead("default", value=default_value, set=default_value is not None)]
    if session is not None and "global" in spec.layers:
        config = read_app_config(session)
        if spec.name in COLUMN_FIELDS["global"]:
            reads.append(_column_read("global", config, spec.name))
        else:
            raw = getattr(config, JSON_FIELDS["global"], None) if config else None
            reads.append(_json_read("global", raw, spec.name))
    if "site" in spec.layers and site is not None:
        if spec.name in COLUMN_FIELDS["site"]:
            reads.append(_column_read("site", site, spec.name))
        else:
            reads.append(_json_read("site", site.settings_json, spec.name))
    if "layout" in spec.layers and layout is not None:
        reads.append(_json_read("layout", layout.defaults_json, spec.name))
    if "instance" in spec.layers and widget is not None:
        reads.append(_json_read("instance", widget.config_json, spec.name))
    return reads


def effective_for_site(
    session: Session, settings: Settings, site: Site | None
) -> dict[str, dict]:
    """场地这一份设置的生效值与来源链（判据：默认 < 全局 < 场地）。

    `site=None` 就是"只看默认 + 全局"——`GET /api/settings/global` 用的正是这条，
    少一层不等于换算法：链里少出现的层是"这层不参与"，不是"这层设了空"。
    """
    out: dict[str, dict] = {}
    for spec in KEYS:
        if spec.locked or not {"global", "site"} & set(spec.layers):
            continue
        out[spec.name] = resolve(
            settings, spec, read_layers(settings, spec, session=session, site=site)
        ).as_dict()
    return out


def effective_for_layout(
    session: Session, settings: Settings, *, site: Site | None, layout: WorkbenchLayout
) -> dict[str, dict]:
    """布局这一层的生效值（不含实例层）：容量硬闸要在"还没有具体格子"时也能算。

    布局的 `defaults` 会落到这个布局的每一个格子上，所以超限的组合在这里就得拦住，
    不能等到某个组件被点开才报——那时候半套配置已经进库了。
    """
    out: dict[str, dict] = {}
    for spec in KEYS:
        if spec.locked or "layout" not in spec.layers:
            continue
        out[spec.name] = resolve(
            settings,
            spec,
            read_layers(settings, spec, session=session, site=site, layout=layout),
        ).as_dict()
    return out


def effective_for_widget(
    session: Session,
    settings: Settings,
    *,
    site: Site | None,
    layout: WorkbenchLayout | None,
    widget: LayoutWidget | None,
) -> dict[str, dict]:
    """组件实例的生效值：五层全走一遍，回显来源链。"""
    out: dict[str, dict] = {}
    for spec in KEYS:
        if spec.locked or "instance" not in spec.layers:
            continue
        out[spec.name] = resolve(
            settings,
            spec,
            read_layers(
                settings, spec, session=session, site=site, layout=layout, widget=widget
            ),
        ).as_dict()
    return out


# --- 写入 -------------------------------------------------------------------


@dataclass
class Change:
    """一次"把某层的某个键设成新值（或取消）"的可执行改动。

    `previous` 必须留着：导入的 revert 与"改坏了要回退"都靠它，而不是靠猜。
    """

    key: str
    layer: str
    new: Any  # None = 取消这一层的覆写
    previous: Any
    column: str | None = None
    json_field: str | None = None

    def apply(self, target: Any) -> None:
        if self.column is not None:
            setattr(target, self.column, _dump_if_json(self.column, self.new))
            return
        field_name = self.json_field
        assert field_name is not None
        raw = getattr(target, field_name, None)
        data, _problem = load_json(raw)
        data = dict(data or {})
        if self.new is None:
            data.pop(self.key, None)
        else:
            data[self.key] = self.new
        setattr(target, field_name, json.dumps(data, ensure_ascii=False) if data else None)

    def as_dict(self) -> dict:
        return {
            "key": self.key,
            "layer": self.layer,
            "value": self.new,
            "previous": self.previous,
        }


def _dump_if_json(column: str, value: Any) -> Any:
    if column.endswith("_json") and isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def stage_overrides(
    settings: Settings,
    layer: str,
    target: Any,
    updates: dict[str, Any],
) -> tuple[list[Change], list[dict]]:
    """把"这层要设/取消的这些键"折成可执行改动 + 被拒清单。

    拒的是注册表说了算的三件事：不认识这个键、这个键不许写、这一层没有资格设它。
    值本身合不合法交给 `validate`，路由侧再决定翻成 422 还是 409。
    """
    changes: list[Change] = []
    errors: list[dict] = []
    for key, value in updates.items():
        spec = BY_NAME.get(key)
        if spec is None:
            errors.append({"key": key, "reason": "unknown_key"})
            continue
        if spec.locked:
            errors.append({"key": key, "reason": "protected_key"})
            continue
        if layer not in spec.layers:
            errors.append({"key": key, "reason": "layer_not_allowed"})
            continue
        column = COLUMN_FIELDS.get(layer, {}).get(key)
        json_field = None if column else JSON_FIELDS.get(layer)
        if not column and not json_field:
            # 这一层既没有列也没有 JSON 落点：想在这儿设这个键，就是注册表与表结构不一致。
            errors.append({"key": key, "reason": "no_storage_for_layer"})
            continue
        if value is not None:
            problem = validate(settings, key, value)
            if problem:
                errors.append({"key": key, "reason": problem})
                continue
        previous = (
            getattr(target, column, None)
            if column
            else (load_json(getattr(target, json_field, None))[0] or {}).get(key)
        )
        changes.append(
            Change(
                key=key,
                layer=layer,
                new=value,
                previous=previous,
                column=column,
                json_field=json_field,
            )
        )
    return changes, errors


def schema_document(settings: Settings) -> list[dict]:
    """给前端渲染用的声明：键、可写层级、值域、边界、单位、要不要确认、哪层还没执行者。

    这份声明的存在理由和规范 §2.3 同一条：**能力由声明渲染，未声明即不支持**。前端不该
    自己抄一遍候选值，否则固件频道数一变，UI 就先于数据说谎。
    """
    document: list[dict] = []
    for spec in KEYS:
        low, high = _bounds(spec, settings)
        document.append(
            {
                "key": spec.name,
                "kind": spec.kind,
                "layers": list(spec.layers),
                "choices": list(spec.choices) if spec.choices else None,
                "minimum": low,
                "maximum": high,
                "default": platform_default(settings, spec),
                "unit": spec.unit,
                "locked": spec.locked,
                "requires_restart": spec.requires_restart,
                "pending_stage": spec.pending_stage,
            }
        )
    return document
