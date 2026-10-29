"""工作台静态资产：挂得上、而且**装齐了**。

为什么这类断言放在后端而不是只放 `node --test`：`node --test` 钉的是逻辑（栅格、租约、
文案、生效值），它不碰 `index.html` 与 CSS。而 S5 这一阶段最容易出的三种事故恰好全在
这条路上——页面 404、少拷一个组件文件、CSS 变量改了名而绘图代码还在读旧名。
它们都发生在「部署」这一步，所以由部署的这一侧（后端）来把关。

`health.workbench` 那几条同样在这里钉：**工作台是可选件**，「这台实例只发 API」是合法
部署形态，把它报成 `degraded` 会让运营者开始忽略 `degraded`（规范 §1 的代价）。
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

WORKBENCH = Path(__file__).resolve().parents[2] / "frontend" / "workbench"

#: `index.html` 里那条 SRI 与 `vendor/` 副本必须是同一份字节：CDN 与本地副本二选一的
#: 部署形态下，「两个来源其实是两个版本」是没人能靠肉眼看出来的故障。
VENDOR_SHA256 = "c459ba7cc8db65c982589fa5d64c7ff478877e8e5b0fd75683207cec6a4e89e8"

INDEX_HTML = (WORKBENCH / "index.html").read_text(encoding="utf-8")
MODULES = sorted(WORKBENCH.rglob("*.js"))


def local_page_refs(html: str) -> list[str]:
    """页面里所有 `./` 开头的本地引用：`<link>`/`<script>` 与动态 `import()`。"""
    return (
        re.findall(r'''(?:href|src)\s*=\s*"(\./[^"]+)''', html)
        + re.findall(r'''import\("(\./[^"]+)''', html)
    )


def module_imports(source: str) -> list[str]:
    """模块里的相对导入：`from "./x.js"` 与 `import("./x.js")`，带 query 的剥掉。"""
    return [
        specifier.split("?")[0]
        for pattern in (r'''from\s+"(\.[./][^"]*)''', r'''import\(\s*"(\.[./][^"]*)''')
        for specifier in re.findall(pattern, source)
    ]


# --- 资产完整性 ---------------------------------------------------------------


def test_every_local_asset_the_page_references_exists():
    assert WORKBENCH.is_dir(), "工作台目录不在：发布包少拷了 frontend/"
    refs = local_page_refs(INDEX_HTML)
    assert refs, "index.html 一个本地引用都没有，那这页是空的"
    for ref in refs:
        assert (WORKBENCH / ref.split("?")[0]).is_file(), f"页面引用了 {ref}，文件不在"


def test_page_references_are_all_relative():
    """写死 `/app.js` 的页面只能活在一个挂载点下；反代到子路径就白屏（方案 §6）。"""
    absolute = re.findall(r'''(?:href|src)\s*=\s*"/(?!/)''', INDEX_HTML)
    assert absolute == [], f"出现了根绝对路径引用：{absolute}"


def test_the_module_graph_has_no_broken_imports():
    """少拷一个组件文件时，这条要在部署侧失败，而不是等页面渲染到那一格才崩。"""
    missing = [
        f"{module.relative_to(WORKBENCH)} → {specifier}"
        for module in MODULES
        for specifier in module_imports(module.read_text(encoding="utf-8"))
        if not (module.parent / specifier).is_file()
    ]
    assert missing == [], f"依赖图断裂：{missing}"


def test_the_vendored_vue_copy_is_the_bytes_the_page_commits_to():
    blob = (WORKBENCH / "vendor" / "vue.global.prod.js").read_bytes()
    assert hashlib.sha256(blob).hexdigest() == VENDOR_SHA256
    sri = "sha384-" + base64.b64encode(hashlib.sha384(blob).digest()).decode()
    assert re.findall(r'const SRI = "([^"]+)"', INDEX_HTML) == [sri]


def test_no_module_imports_vue_directly():
    """无构建形态下 Vue 是注入的全局量；模块顶层 `import "vue"` 等于把它钉死在浏览器里，
    Node 侧那批断言（`test/components.test.mjs`）会当场跑不起来。"""
    offenders = []
    for module in MODULES:
        for line in module.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith(("*", "//", "/*")):  # 注释里那句"不许这么写"不算违规
                continue
            if re.match(r'^import\s+(\{[^}]*\}\s+from\s+)?"vue', stripped):
                offenders.append(module.name)
    assert offenders == [], f"这些模块直接引了 vue：{offenders}"


def test_css_variables_that_the_chart_reads_are_declared():
    """`chart.js` 从样式表取色，取不到就退成硬编码的 `#333`——改名不会报错，只会让图在
    换主题时安静退回默认色。这种「看不见的降级」只能靠跨文件断言钉住。"""
    css = (WORKBENCH / "styles.css").read_text(encoding="utf-8")
    for name in ("--grid", "--muted", "--s1", "--warning"):
        assert re.search(rf"{re.escape(name)}\s*:", css), f"styles.css 里没有 {name}"


# --- 挂载与健康检查 -----------------------------------------------------------


def test_health_reports_the_mount_honestly_and_stays_ok(client):
    health = client.get("/api/health").json()
    assert health["workbench"] == {
        "mounted": True,
        "reason": None,
        "dir": str(WORKBENCH),
        "index": True,
    }
    assert health["status"] == "ok", health["problems"]


def test_the_page_and_its_assets_are_served_from_the_same_origin(client):
    response = client.get("/workbench/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert 'id="app"' in response.text
    # 样式表与入口模块也要从同一个 origin 取到，否则页面只是「半个 200」。
    for path in ("/workbench/styles.css", "/workbench/app.js", "/workbench/vendor/vue.global.prod.js"):
        assert client.get(path).status_code == 200, path


def _client(db_path, workbench_dir):
    app = create_app(Settings(db_path=db_path, dialer_enabled=False, workbench_dir=workbench_dir))
    return TestClient(app, raise_server_exceptions=False)


def test_an_api_only_instance_is_a_valid_deployment_not_a_problem(db_path):
    with _client(db_path, None) as client:
        health = client.get("/api/health").json()
        assert health["workbench"]["mounted"] is False
        assert health["workbench"]["reason"] == "disabled"
        assert health["status"] == "ok", "只发 API 是合法形态，不许报 degraded"
        assert client.get("/workbench/").status_code == 404


def test_a_directory_without_index_html_names_the_missing_piece(db_path):
    """半路拷过来的发布包最像这个样子：目录有了，`index.html` 还在路上。"""
    with _client(db_path, WORKBENCH.parent / "no-such-workbench") as client:
        health = client.get("/api/health").json()
        assert health["workbench"] == {
            "mounted": False,
            "reason": "no_index_html",
            "dir": str(WORKBENCH.parent / "no-such-workbench"),
            "index": False,
        }
        assert health["status"] == "ok"
