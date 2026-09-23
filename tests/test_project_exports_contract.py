# -*- coding: utf-8 -*-
"""课题导出中心（project_exports_core.js / project_exports.js / 后端 export_pack.py）
的结构契约门。

与 artifacts/projects/usage/benchfb 契约同一套思路：**三门测不出真行为**（web_smoke 静态查
字符串、node --check 只验语法、import 图只验 import 边），所以这里静态钉死结构不变量，
真行为由 `tests/js/project_exports_core_spec.mjs` 在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **纯逻辑核心零 DOM / 零网络 / 零 localStorage / 零 `#` import**：project_exports_core.js
   只做确定性计算（diff/台账条目/折叠推导/文案）；出网与 DOM 全在 project_exports.js 壳层。
2. **自接线不进 boot**：project_exports.js 经 MutationObserver 发现 projects.js 的
   ENG-P5-MOUNT 挂点渲染导出区；不进 boot.js 的 import 表（同 feedback.js 哲学——
   新增 `#` 键会牵动两页 importmap，dataset.html 归 F2 并行包，本包不碰）。
3. **importmap 只在本页 + package.json**：parity 门只查**被使用**的 specifier——project_exports.js
   只相对 import 纯逻辑核、不引入新的 `#` 静态 import，故不红。
4. **挂点契约**：projects.js 的 ENG-P5-MOUNT 区域内必须有 `data-p5-mount-export`（带课题 id）
   与默认隐藏的 `data-p5-mount-section`；无 P5 时整段不渲染。
5. **埋点与后端同源**：`export_downloaded` 在 USAGE_KINDS；端点路径常量与 webapp.py
   `@app.post("/api/artifacts/export-pack")` 逐字一致；导出类型枚举与后端 export_pack.EXPORT_KINDS 一致。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "core" / "project_exports_core.js"
UI = ROOT / "web" / "static" / "js" / "panel" / "project_exports.js"
PROJECTS = ROOT / "web" / "static" / "js" / "core" / "projects.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET = ROOT / "web" / "static" / "dataset.html"
PKG = ROOT / "package.json"
BOOT = ROOT / "web" / "static" / "js" / "core" / "boot.js"
USAGE = ROOT / "web" / "static" / "js" / "core" / "usage_core.js"
WEBAPP = ROOT / "src" / "dataset_recommender" / "app" / "webapp.py"
BACKEND = ROOT / "src" / "dataset_recommender" / "content" / "export_pack.py"
SPEC = ROOT / "tests" / "js" / "project_exports_core_spec.mjs"

FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
FORBIDDEN_DOM_TOKENS = ("document.", "window.", "getElementById", "localStorage")


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _strip_js_comments(text: str) -> str:
    """断言只看真代码（注释里当然会出现「零网络」「不进静态图」这类说明词）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 职责边界

def test_core_layer_cannot_talk_to_the_network() -> None:
    """纯逻辑核零出网：网络只在 UI 壳层（fetch 导出端点）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"project_exports_core.js 出现出网原语：{hits}"


def test_core_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage：界面与台账存储（IndexedDB）都在壳层/数据层。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"project_exports_core.js 出现 DOM/localStorage 访问：{hits}"


def test_core_layer_is_self_contained() -> None:
    """纯逻辑核完全自包含（零 `#` import、零相对 import）——不进 import 图、不进环。"""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r'from\s*"', text) is None, "project_exports_core.js 出现了 import（要求自包含）"


def test_shell_uses_mutation_observer_and_self_wiring() -> None:
    """壳层经 MutationObserver 发现挂点渲染（不自建轮询）；自接线不进 boot。"""
    ui = UI.read_text(encoding="utf-8")
    assert "MutationObserver" in ui, "project_exports.js 缺 MutationObserver 挂点发现"
    assert "DOMContentLoaded" in ui or "initProjectExports" in ui, "自接线缺失"
    boot = BOOT.read_text(encoding="utf-8")
    assert "project_exports" not in boot, "boot.js 出现了 project_exports import（本包自接线，不进 boot）"


def test_shell_imports_core_relatively_and_shared_keys_only() -> None:
    """壳层只相对 import 纯逻辑核；静态 `#` import 只用两页已有的共享键（不新增 specifier）。

    2026-08-30 dl-browser-queue：`#downloads` 入列——导出包 blob 走统一下载引擎
    （单通道原则，AGENTS.md §2），这是登记过的共享键，不是私自新增 specifier。"""
    ui = UI.read_text(encoding="utf-8")
    assert 'from "../core/project_exports_core.js"' in ui, "壳层未相对 import 纯逻辑核"
    used = re.findall(r'from\s*"(#[A-Za-z_0-9]+)"', ui)
    assert set(used) <= {"#core", "#artifacts", "#usage_core", "#usage_log", "#downloads"}, f"壳层用了新 # 键：{used}"
    assert re.search(r'from\s*"\./usage_upload\.js"', ui) is None, "壳层静态 import 了 usage_upload（应动态）"


# ---------------------------------------------------------------- 挂点契约

def test_projects_js_mount_region_contract() -> None:
    """projects.js 的 ENG-P5-MOUNT 区域内：挂点带课题 id、区域默认整段隐藏（无 P5 不渲染）。"""
    ui = PROJECTS.read_text(encoding="utf-8")
    assert "ENG-P5-MOUNT" in ui
    assert "data-p5-mount-export" in ui, "ENG-P5-MOUNT 区域缺 data-p5-mount-export 挂点"
    assert "data-prj-id=" in ui, "挂点缺课题 id（P5 读库需要）"
    assert 'data-p5-mount-section hidden' in ui, "导出区默认整段隐藏缺失（无 P5 时不渲染）"


def test_importmap_registration_stays_on_index_page_only() -> None:
    """importmap 登记只在本页 + package.json（dataset.html 归 F2 并行包，不碰）。"""
    pkg = PKG.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    for key, rel in (('"#project_exports_core"', "/static/js/core/project_exports_core.js"),
                     ('"#project_exports"', "/static/js/panel/project_exports.js")):
        assert key in pkg, f"package.json 缺 {key}"
        assert key in index, f"index.html importmap 缺 {key}"
        assert rel + "?v=" in index, f"index.html 缺 {key} 的 script 标签（{rel}）"
    ds = DATASET.read_text(encoding="utf-8")
    assert '"#project_exports_core"' not in ds and '"#project_exports"' not in ds, (
        "dataset.html 出现本包键——该页归另一并行改动，本包不得碰")


def test_boot_stays_last_in_load_order() -> None:
    """新 script 标签加在 boot 之前（web_smoke 的加载序契约：core 第一、boot 最后）。"""
    scripts = re.findall(r'<script type="module" src="/static/js/([^"?]+)\.js', INDEX.read_text(encoding="utf-8"))
    assert scripts[0] == "core/core" and scripts[-1] == "core/boot", f"加载序契约破坏：{scripts[0]} … {scripts[-1]}"


# ---------------------------------------------------------------- 埋点与后端同源

def test_usage_kind_registered() -> None:
    """埋点（计数型无文本）：export_downloaded 必须在 USAGE_KINDS。"""
    usage = USAGE.read_text(encoding="utf-8")
    assert 'export_downloaded: "export_downloaded"' in usage, "USAGE_KINDS 缺 export_downloaded"


def test_shell_telemetry_only_counting_kind() -> None:
    """壳层埋点：调 usageLog 且用 USAGE_KINDS.export_downloaded（无文本载荷）。"""
    code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "usageLog(" in code, "project_exports.js 未埋点（设计 §10 export_downloaded）"
    assert "USAGE_KINDS.export_downloaded" in code, "埋点未走 USAGE_KINDS.export_downloaded"


def test_endpoint_and_kinds_match_backend() -> None:
    """前端端点路径/类型枚举与后端 webapp.py / export_pack.py 逐字一致（不自己造口径）。"""
    core = CORE.read_text(encoding="utf-8")
    assert 'EXPORT_API_PATH = "/api/artifacts/export-pack"' in core
    webapp = WEBAPP.read_text(encoding="utf-8")
    assert '@app.post("/api/artifacts/export-pack")' in webapp, "webapp.py 缺导出端点"
    assert "X-Biodata-Export-Meta" in webapp and "X-Biodata-Export-Meta" in core, "台账 meta 响应头契约断裂"
    kinds = re.search(r'EXPORT_KINDS = \[(.*?)\]', core, re.S)
    assert kinds, "前端 EXPORT_KINDS 枚举缺失"
    front_kinds = re.findall(r'"([a-z_]+)"', kinds.group(1))
    backend = BACKEND.read_text(encoding="utf-8")
    bk = re.search(r'EXPORT_KINDS = \((.*?)\)', backend, re.S)
    assert bk, "后端 EXPORT_KINDS 枚举缺失"
    backend_kinds = re.findall(r'"([a-z_]+)"', bk.group(1))
    assert front_kinds == backend_kinds, f"前后端导出类型枚举不一致：{front_kinds} vs {backend_kinds}"


# ---------------------------------------------------------------- node 规格驱动

def test_core_spec_passes_in_node() -> None:
    """纯逻辑核心真行为：node 直跑 project_exports_core_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    r = subprocess.run([node, str(SPEC)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"project_exports_core_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"
