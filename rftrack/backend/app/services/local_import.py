"""原 `web/` SPA 的 localStorage 一次性导入（方案 §7 S4、§3 `local_settings_import` 表）。

三条纪律决定这里的形态：

1. **只写全局层**。浏览器里那份是"这个用户平时看什么"（主题、语言、历史窗口），不是
   "这个场地怎么采集"。场地/布局/实例三层由后端自己的界面管，导入不许替它们做主。
2. **认不出来就原样列出来，不许猜映射**。`unmapped` 把每一条没落点的 legacy 键连同原值
   一起带回去。猜一个映射（`dbMin` → `threshold_db` 看着挺像）会静默改写用户的告警阈值，
   那比"这几个键今天没有落点"严重得多。
3. **留痕可回退**。`payload_json` 存整份原始 entries，`changes_json` 存每个落点的新旧值，
   revert 照着 `previous` 逐项写回，只把 `reverted_at` 标上——**行不删**，删了就没有对照，
   下一次导入也无从比较。

legacy 键名与字段名来自原代码，不是设想出来的：`web/src/main.ts:50` 的
`widscan58.theme`、`web/src/i18n.ts:8` 的 `widscan58.lang`、`web/src/model/app.ts:13` 的
`UiSettings`（`historyMetric` / `historySeconds` / `showAverage` 三个字段有注册表落点，
其余 14 个字段今天没有，一律进 `unmapped`）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import AppConfig, LocalSettingsImport, utcnow
from .config_layer import (
    COLUMN_FIELDS,
    JSON_FIELDS,
    Change,
    spec_for,
    stage_overrides,
)

#: 顶层 localStorage 键 → 注册表键（值本身就是设置）。
LEGACY_TOP_LEVEL = {
    "widscan58.theme": "theme",
    "widscan58.lang": "lang",
}

#: `widscan58.settings` 里的字段 → 注册表键。
LEGACY_SETTINGS = {
    "historyMetric": "history_metric",
    "historySeconds": "time_range_s",
    "showAverage": "show_average",
}

#: 单次导入的原始 JSON 上限。留痕会整份进库，不设上限等于让一个页面向导撑爆 SD 卡。
MAX_PAYLOAD_CHARS = 64 * 1024


def _decoded(value: Any) -> Any:
    """localStorage 只会存字符串，所以"值是 JSON 文本"是常态；解不开就原样当标量。"""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError:
        return value


def flatten(entries: dict[str, Any]) -> tuple[list[tuple[str, str, Any]], list[dict]]:
    """(有落点的 (来源标签, 注册表键, 值), 没落点的清单)。

    排序是为了让留痕可 diff：同一份 entries 无论键序如何，导出的 `changes`/`unmapped`
    都得一模一样。
    """
    mapped: list[tuple[str, str, Any]] = []
    unmapped: list[dict] = []
    for name, raw in sorted(entries.items()):
        if name in LEGACY_TOP_LEVEL:
            mapped.append((name, LEGACY_TOP_LEVEL[name], _decoded(raw)))
            continue
        if name != "widscan58.settings":
            unmapped.append(
                {"source": name, "reason": "no_registered_key", "value": _decoded(raw)}
            )
            continue
        payload = _decoded(raw)
        if not isinstance(payload, dict):
            # 认不出结构就把整包列出来，不逐字段猜——猜出来的字段落点比不导更糟。
            unmapped.append({"source": name, "reason": "not_an_object", "value": None})
            continue
        for field, value in sorted(payload.items()):
            key = LEGACY_SETTINGS.get(field)
            if key is None:
                unmapped.append(
                    {
                        "source": f"{name}.{field}",
                        "reason": "no_registered_key",
                        "value": value,
                    }
                )
            else:
                mapped.append((f"{name}.{field}", key, value))
    return mapped, unmapped


def plan_import(
    settings: Settings, config: AppConfig, entries: dict[str, Any]
) -> dict[str, Any]:
    """算出这次导入要改什么，**不落库**。

    校验复用 `stage_overrides`：legacy 值一样要过注册表（老的 `historySeconds` 若是 0，
    今天照样是 `out_of_range`），不许因为"它是从浏览器来的"就免检。
    """
    mapped, unmapped = flatten(entries)
    updates: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for label, key, value in mapped:
        updates[key] = value
        sources.setdefault(key, label)
    changes, errors = stage_overrides(settings, "global", config, updates)
    rejected = [
        {**error, "source": sources.get(error["key"])} for error in errors
    ]
    return {
        "changes": changes,
        "applied": [change.as_dict() for change in changes],
        "rejected": rejected,
        "unmapped": unmapped,
    }


def payload_too_large(entries: dict[str, Any]) -> bool:
    return len(json.dumps(entries, ensure_ascii=False, default=str)) > MAX_PAYLOAD_CHARS


def record_import(
    session: Session, settings: Settings, config: AppConfig, entries: dict[str, Any], source: str
) -> tuple[LocalSettingsImport, dict[str, Any]]:
    """应用 + 留痕，一次做完。返回 (留痕行, 这次的计划)。"""
    result = plan_import(settings, config, entries)
    for change in result["changes"]:
        change.apply(config)
    row = LocalSettingsImport(
        source=source,
        payload_json=json.dumps(entries, ensure_ascii=False, default=str),
        changes_json=json.dumps(
            {"applied": result["applied"], "rejected": result["rejected"]},
            ensure_ascii=False,
        ),
        unmapped_json=json.dumps(result["unmapped"], ensure_ascii=False),
    )
    session.add(row)
    session.flush()
    return row, result


def _restored(change: dict) -> Change | None:
    """把留痕里的一条还原成"设回旧值"的可执行改动。"""
    key, layer, previous = change["key"], change["layer"], change.get("previous")
    if spec_for(key) is None:
        return None  # 这个键已从注册表里去掉：落点没了，只能跳过并如实列出来
    column = COLUMN_FIELDS.get(layer, {}).get(key)
    json_field = None if column else JSON_FIELDS.get(layer)
    if not column and not json_field:
        return None
    return Change(
        key=key, layer=layer, new=previous, previous=change.get("value"),
        column=column, json_field=json_field,
    )


def revert_import(
    session: Session, settings: Settings, config: AppConfig, row: LocalSettingsImport
) -> dict[str, list]:
    """按留痕把全局层写回"这次导入之前"。

    不做事务式回滚链：还原的是这次导入触及的那几个键，所以**多次导入要按时间倒序撤销**，
    先撤早的那一次会把后一次的值一起抹掉——这是留痕设计的固有语义，写在这里而不是藏在代码里。
    """
    applied = json.loads(row.changes_json or "{}").get("applied", [])
    restored, skipped = [], []
    for entry in applied:
        change = _restored(entry)
        if change is None:
            skipped.append({**entry, "reason": "no_landing"})
            continue
        change.apply(config)
        restored.append(change.as_dict())
    row.reverted_at = utcnow()
    session.flush()
    return {"restored": restored, "skipped": skipped}


def summary(row: LocalSettingsImport) -> dict:
    """列表视图：只回计数与来源，原始 payload 留给详情接口。"""
    applied, rejected, unmapped = _counts(row)
    return {
        "import_id": row.import_id,
        "imported_at": row.imported_at,
        "source": row.source,
        "reverted_at": row.reverted_at,
        "applied_count": applied,
        "rejected_count": rejected,
        "unmapped_count": unmapped,
    }


def detail(row: LocalSettingsImport) -> dict:
    stored = json.loads(row.changes_json or "{}")
    return {
        **summary(row),
        "payload": json.loads(row.payload_json or "null"),
        "applied": stored.get("applied", []),
        "rejected": stored.get("rejected", []),
        "unmapped": json.loads(row.unmapped_json or "[]"),
    }


def _counts(row: LocalSettingsImport) -> tuple[int, int, int]:
    stored = json.loads(row.changes_json or "{}")
    return (
        len(stored.get("applied", [])),
        len(stored.get("rejected", [])),
        len(json.loads(row.unmapped_json or "[]")),
    )


def get_row(session: Session, import_id: int) -> LocalSettingsImport | None:
    return session.get(LocalSettingsImport, import_id)


def list_rows(session: Session) -> list[LocalSettingsImport]:
    return list(
        session.scalars(select(LocalSettingsImport).order_by(LocalSettingsImport.import_id))
    )
