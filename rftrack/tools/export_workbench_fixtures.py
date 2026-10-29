#!/usr/bin/env python3
"""造一份**真后端状态**的工作台夹具，给浏览器那一轮冒烟用（`frontend/e2e/workbench.mjs`）。

为什么不是"页面里塞点假数据"：S5 的出口判据要的是"坏布局降级不崩""未注册组件保住位置"
这些路径。它们只有在库里真有一条 `version` 超前的布局、真有一行认不出的 `plugin_id` 时才
会被走到——手工在页面上点不出这两种库，而拿假 JSON 喂页面，测的又是页面自己在骗自己。

三条纪律：

1. **一切正常状态都走 HTTP**（`/api/sites`、`/api/relays`、`/api/gateways`、
   `/api/layouts/**`），和页面走的是同一条路；只有"库里本来就该是坏的"那三处才动 SQL——
   半路拷坏的库没法用 API 造出来。
2. **节点行由 `ingest.reconcile` 生成**：serve.mjs 的 `/api/endpoints` 是节点唯一的事实
   来源，软件侧不许另起一条写入路径。能力与 `fw_version` 用 SQL 补，因为
   `PUT /api/nodes/{id}`（契约 §4 里那行显式声明）要到 S6 才实现，今天没有 API 可走。
3. **导出即自检**：末尾用 HTTP 回读那三条降级路径，确认服务端真按
   `layout_version_too_new` / `malformed_json` / 认不出的 `plugin_id` 回答。
   夹具坏在半路比没有夹具更糟——它会让人测出一条不存在的结论。

用法：

    python3 rftrack/tools/export_workbench_fixtures.py            # 默认 /tmp/rftrack-e2e
    python3 rftrack/tools/export_workbench_fixtures.py --out-dir /path/to/scratch
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlencode

TOOLS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TOOLS_DIR.parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import migrate  # noqa: E402
from app import APP_VERSION  # noqa: E402
from app.config import Settings  # noqa: E402
from app.database import Database  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Gateway, Node  # noqa: E402
from app.services.ingest import reconcile  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

DEFAULT_OUT = Path("/tmp/rftrack-e2e")
DB_NAME = "workbench-fixtures.sqlite3"
MANIFEST_NAME = "manifest.json"
#: 只许清掉我们自己产出的那几样；目录里有别的文件就停手（别把别人的目录当临时盘）。
OWN_FILES = {DB_NAME, MANIFEST_NAME, f"{DB_NAME}-wal", f"{DB_NAME}-shm"}

#: 组件的 `instance_id` 在这里给死：冒烟脚本按同一批 id 找格子。让服务端随机生成的话，
#: 每次导出的夹具都要重算一遍"点哪个"，那条 e2e 就成了跟着夹具变的活物。
GRID = [
    ("w-matrix", "channel-matrix", 0, 0, 6, 4, {"slots": [0, 1, 2, 3, 4, 5, 6, 7]}),
    ("w-detail", "channel-detail", 6, 0, 3, 4, {"threshold_db": -90}),
    ("w-trend", "rssi-trend", 6, 4, 6, 4, {"time_range_s": 60}),
    ("w-events", "signal-events", 0, 4, 6, 4, {}),
    ("w-nodes", "node-status", 9, 0, 3, 4, {}),
]

#: 未注册那一格用的 id：故意挑一个"新版本前端才认识"的名字，而不是拼错的现有名字——
#: 前者是规范 §3.3 说的那种格子（别的实例带过来的布局），后者只是笔误。
UNKNOWN_PLUGIN = "from-a-newer-build"


def endpoint(label: str, path: str, kind: str = "serial", online: bool = True) -> dict:
    """serve.mjs `/api/endpoints` 的一行，形状照 `tests/test_ingest_nodes.py` 的常量。"""
    row = {"id": f"{kind}:{path or label}", "kind": kind, "label": label, "online": online, "busy": False}
    if path:
        row["path"] = path
    if kind == "uart":
        row["scannable"] = True
    return row


def fresh_scratch(out_dir: Path) -> None:
    if out_dir.exists():
        foreign = sorted(p.name for p in out_dir.iterdir() if p.name not in OWN_FILES)
        if foreign:
            raise SystemExit(f"{out_dir} 里有不属于本夹具的文件：{foreign}；换一个 --out-dir")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)


def put_row(client, layout_id, instance_id, plugin_id, x, y, w, h, config) -> dict:
    body = {"plugin_id": plugin_id, "pos_x": x, "pos_y": y, "pos_w": w, "pos_h": h, "config": config}
    response = client.put(f"/api/layouts/{layout_id}/widgets/{instance_id}", json=body)
    assert response.status_code == 200, (instance_id, response.status_code, response.text)
    return response.json()


def build(out_dir: Path) -> dict:
    db_path = out_dir / DB_NAME
    migrated = migrate.run_migrations(db_path)
    if not migrated.ok:
        raise SystemExit(f"迁移失败：{migrated.error}")

    settings = Settings(db_path=db_path, dialer_enabled=False)
    app = create_app(settings)
    db = Database(db_path)
    manifest: dict = {"sites": [], "layouts": [], "degraded": {}}

    with TestClient(app) as client:
        # --- 层级：两个场地，各两个位置、每位置一台接入机 ------------------------
        for site_index, (site_name, slug) in enumerate(
            (("A 号赛段", "track-a"), ("B 号赛段", "track-b"))
        ):
            site = client.post("/api/sites", json={"name": site_name, "slug": slug}).json()
            entry = {
                "site_id": site["site_id"],
                "name": site_name,
                "relays": [],
                "gateways": [],
                "nodes": [],
                "widgets": [],
                "layouts": {},
            }
            for relay_name in ("起点塔", "看台"):
                relay = client.post(
                    "/api/relays", json={"site_id": site["site_id"], "name": relay_name}
                ).json()
                entry["relays"].append(relay["relay_id"])
                gateway = client.post(
                    "/api/gateways",
                    json={
                        "relay_id": relay["relay_id"],
                        "name": f"pi-{site_index + 1}-{relay_name}",
                        "kind": "usb-bridge",
                    },
                ).json()
                entry["gateways"].append(gateway["gateway_id"])
                with db.scope() as session:
                    result = reconcile(
                        session,
                        session.get(Gateway, gateway["gateway_id"]),
                        [
                            endpoint(
                                f"widscan58-{site_index}-{relay['relay_id']}",
                                f"/dev/widscan58-{site_index}-{relay['relay_id']}",
                            ),
                            # ②a 的 UART 出口：介质决定它会不会停这台接入机的 RH（R21）。
                            endpoint(
                                f"uart-{site_index}-{relay['relay_id']}",
                                f"/dev/serial{relay['relay_id']}",
                                kind="uart",
                            ),
                            # 掉线那块：`online=False` → `offline`，页面要显示"没接上"这一态。
                            endpoint(f"idle-{site_index}-{relay['relay_id']}", "", kind="remote", online=False),
                        ],
                        settings,
                    )
                    session.commit()
                assert len(result.seen) == 3, result
                with db.scope() as session:
                    entry["nodes"].extend(
                        session.scalars(
                            select(Node.node_id).where(Node.gateway_id == gateway["gateway_id"])
                        )
                    )
            manifest["sites"].append(entry)

        # --- 布局：每场地两份（激活那份 + 用来验"切场地各留自己状态"那份）------
        for site_index, entry in enumerate(manifest["sites"]):
            primary = client.post(
                "/api/layouts", json={"site_id": entry["site_id"], "name": "值班视图"}
            ).json()
            for instance_id, plugin_id, x, y, w, h, config in GRID:
                widget = put_row(client, primary["layout_id"], instance_id, plugin_id, x, y, w, h, config)
                entry["widgets"].append({"instance_id": instance_id, "plugin_id": widget["plugin_id"]})
            client.post(f"/api/layouts/{primary['layout_id']}/activate")
            entry["layouts"]["primary"] = primary["layout_id"]
            manifest["layouts"].append({"layout_id": primary["layout_id"], "site_id": entry["site_id"], "kind": "primary"})

            secondary = client.post(
                "/api/layouts", json={"site_id": entry["site_id"], "name": "备用视图"}
            ).json()
            # 两个场地的备用视图**故意用不同的实例号**：冒烟要在 B 里断"A 的格子没串过来"，
            # 两边同名的话那条判据就永远为真，等于没断。
            secondary_instance = f"w-secondary-{site_index}"
            entry["secondary_instance"] = secondary_instance
            put_row(client, secondary["layout_id"], secondary_instance, "channel-matrix", 0, 0, 6, 4, {"slots": [0, 1]})
            entry["layouts"]["secondary"] = secondary["layout_id"]
            manifest["layouts"].append({"layout_id": secondary["layout_id"], "site_id": entry["site_id"], "kind": "secondary"})

        # --- 三条"库里就是坏的"：只能用 SQL 造，API 到不了这个状态 --------------
        site_a, site_b = manifest["sites"]
        primary_a = site_a["layouts"]["primary"]
        broken_b = site_b["layouts"]["secondary"]
        with sqlite3.connect(db_path) as conn:
            # ①未注册的 `plugin_id`，留在 A 场地激活布局里：页面一进来就该撞到那一格。
            conn.execute(
                "UPDATE layout_widget SET plugin_id = ? WHERE layout_id = ? AND instance_id = ?",
                (UNKNOWN_PLUGIN, primary_a, "w-nodes"),
            )
            # ②+③同一份布局挑两种坏法：`defaults_json` 半截（一次没写完的拷贝），
            #   且结构版本超前（老代码只读不改）。它属 B 场地，正好也验"坏的不跨场地"。
            conn.execute(
                "UPDATE workbench_layout SET defaults_json = ?, version = ? WHERE layout_id = ?",
                ('{"threshold_db": -90', 99, broken_b),
            )
            # 版本超前的那份**还得是当前布局**——真实场景就是"新版本工作台把它设成了激活项，
            # 老版本打开这个场地第一眼就撞上它"。不当激活项的话，页面永远走不到降级渲染那条路：
            # `POST /activate` 对 version>LAYOUT_VERSION 回 409，从好布局切过去是切不动的。
            conn.execute(
                "UPDATE workbench_layout SET is_active = (layout_id = ?) WHERE site_id = ?",
                (broken_b, site_b["site_id"]),
            )
            site_b["expected_active"] = broken_b
            # 能力与固件版本：`PUT /api/nodes/{id}` 属 S6，今天没有 API 可走（见模块注释）。
            conn.execute(
                "UPDATE node SET capabilities_json = ?, fw_version = ?, protocol_version = ?"
                " WHERE transport_kind = 'uart-pl011'",
                (
                    json.dumps(
                        {
                            "levels": {"supported": True},
                            "spectrum": {"supported": True, "max_bins": 512},
                            "rawiq": {"supported": False},
                        }
                    ),
                    "0.6-dev",
                    6,
                ),
            )
            conn.commit()

        site_a["widgets"] = [
            {**row, "plugin_id": UNKNOWN_PLUGIN} if row["instance_id"] == "w-nodes" else row
            for row in site_a["widgets"]
        ]
        manifest["degraded"] = {
            "unknown_plugin": {"layout_id": primary_a, "instance_id": "w-nodes", "plugin_id": UNKNOWN_PLUGIN},
            "broken_layout": {
                "layout_id": broken_b,
                "site_id": site_b["site_id"],
                "version": 99,
                "expect_declaration_error": "malformed_json",
                "expect_degrade_reason": "layout_version_too_new",
            },
        }

        check(client, manifest)
        db.dispose()

    manifest["app_version"] = APP_VERSION
    manifest["db_path"] = str(db_path)
    return manifest


def check(client, manifest: dict) -> None:
    """回读三条降级路径。服务端不认就说明夹具没造对——那时要让脚本报错，而不是产出。"""
    degraded = manifest["degraded"]
    widgets = client.get(f"/api/layouts/{degraded['unknown_plugin']['layout_id']}/widgets").json()
    by_id = {row["instance_id"]: row for row in widgets}
    assert by_id["w-nodes"]["plugin_id"] == UNKNOWN_PLUGIN, by_id.get("w-nodes")

    broken = degraded["broken_layout"]
    layout = client.get(f"/api/layouts/{broken['layout_id']}").json()
    assert layout["declaration_error"] == "malformed_json", layout
    assert layout["defaults"] is None, layout
    assert layout["degraded"] is True and layout["degrade_reason"] == "layout_version_too_new", layout

    # 版本超前只锁写：读还得读得动，否则页面连"为什么不能改"都说不出来。
    refused = client.patch(f"/api/layouts/{broken['layout_id']}", json={"name": "改名试试"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["reason"] == "layout_version_too_new", refused.text

    # "A 的格子没串到 B"那条冒烟判据，前提是两份备用视图的实例号不同；同名就等于没断。
    assert len({entry["secondary_instance"] for entry in manifest["sites"]}) == len(manifest["sites"]), [
        entry["secondary_instance"] for entry in manifest["sites"]
    ]

    # 状态栏与 `site-tree` 要显示的那几样，逐场地核一遍数量与身份。
    for entry in manifest["sites"]:
        nodes = client.get(f"/api/nodes?{urlencode({'site': entry['site_id']})}").json()
        assert len(nodes) == len(entry["nodes"]), (entry["site_id"], len(nodes), entry["nodes"])
        entry["node_count"] = len(nodes)
        entry["declared_nodes"] = [
            node["node_id"] for node in nodes if node["capabilities"]["spectrum"]["supported"]
        ]
        entry["stops_rh_nodes"] = [node["node_id"] for node in nodes if node["stops_rh"]]
        assert entry["declared_nodes"], "没有一块板声明能力，那 node-status 只会全是不支持"
        assert entry["stops_rh_nodes"], "没有 uart-pl011 那块板，R21 那条介质属性就没人显示"
        listed = client.get(f"/api/layouts?{urlencode({'site': entry['site_id']})}").json()
        assert {row["layout_id"] for row in listed} == {
            row["layout_id"] for row in manifest["layouts"] if row["site_id"] == entry["site_id"]
        }, listed
        assert [row["layout_id"] for row in listed if row["is_active"]] == [
            entry.get("expected_active", entry["layouts"]["primary"])
        ], (entry["site_id"], listed)
        # B 场地那份激活的是版本超前的布局：列表里它必须带着降级标记，页面才说得清为什么只读。
        if "expected_active" in entry:
            broken = next(row for row in listed if row["layout_id"] == entry["expected_active"])
            assert broken["degraded"] is True and broken["degrade_reason"] == "layout_version_too_new", broken

    # `?site=` 缺参的形状（S4 申报④补的那一条）：这一轮顺手把它钉住。
    missing = client.get("/api/layouts")
    assert missing.status_code == 422, missing.text
    assert missing.json()["detail"]["reason"] == "site_required", missing.text


def main() -> int:
    parser = argparse.ArgumentParser(description="导出工作台浏览器冒烟用的夹具库与清单")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    out_dir = args.out_dir.resolve()
    fresh_scratch(out_dir)
    manifest = build(out_dir)
    (out_dir / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"夹具库：{manifest['db_path']}")
    print(f"清单：  {out_dir / MANIFEST_NAME}（app {manifest['app_version']}）")
    for entry in manifest["sites"]:
        print(
            f"  {entry['site_id']} {entry['name']}：{len(entry['relays'])} 位置 / "
            f"{len(entry['gateways'])} 接入机 / {entry['node_count']} 节点"
            f"（{len(entry['stops_rh_nodes'])} 块会停 RH、{len(entry['declared_nodes'])} 块声明了频谱）"
        )
    print(
        f"  降级：未注册组件 {manifest['degraded']['unknown_plugin']['instance_id']}"
        f"（布局 {manifest['degraded']['unknown_plugin']['layout_id']}）；"
        f"坏 defaults + 版本超前布局 {manifest['degraded']['broken_layout']['layout_id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
