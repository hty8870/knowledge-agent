# -*- coding: utf-8 -*-
"""数据集页一键同步（sync_button_core.js / sync_button.js）结构契约门。

与 projects/artifacts 契约同一套思路：**三门测不出真行为**（web_smoke 静态查字符串、
node --check 只验语法、import 图只验 import 边），所以这里静态钉死结构不变量，
真行为由 `tests/js/sync_button_core_spec.mjs` 在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **纯逻辑核心零 DOM / 零网络 / 零 localStorage / 零 #import**：sync_button_core.js 只做
   文案与结构推导（设计约定三态口径都在它身上），任何出网/DOM/存储原语即红。
2. **设计约定文案逐字在场**：副文案「检查官方源更新并导入（仅入外部库，可一键撤销）」、
   sync_busy「另一个同步任务进行中，请稍候」——上屏语是行为承诺，不许漂。
3. **「更新 Y」红线**：sync 没有「更新既有记录」语义（设计约定）——壳层结果文案
   不得出现「更新 Y」，摘要必须经 syncReceiptText 构造。
4. **埋点**：`sync_button_used` 必须在 usage_core USAGE_KINDS（usageLog 依赖该键，不登记
   打点静默丢失）+ 壳层调 usageLog(USAGE_KINDS.sync_button_used, {added,skipped,failed})。
5. **三端点走 API 真源**：sync-updates / sync-status / recall 必须经 core.js API 常量
   （端点真源单一化，不散裸串）。
6. ** 已退役**：「并检查 N 个追踪的更新」全体批量
   联动随全体按钮一并删除（用户：点一下太耗费资源；批量诉求由登录后语料代哨兵自动刷新
   承接）——壳层不得再出现 SYNC_P4_HOOK/ds-sync-p4 痕迹；网页形态异步 job 轮询
   （API.curateSyncJobStatus）必须在场。
7. **「上次同步」不落 per-profile localStorage**（设计约定：实例级事实）——壳层零 localStorage。
8. **登记契约**：两页 importmap + package.json 同键含 #sync_button_core/#sync_button；
   dataset.html 有 #dsSync 挂点骨架与两个 script 标签；index.html 正文零改动（只 importmap 键）。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "search" / "sync_button_core.js"
UI = ROOT / "web" / "static" / "js" / "search" / "sync_button.js"
DATASET = ROOT / "web" / "static" / "dataset.html"
INDEX = ROOT / "web" / "static" / "index.html"
SPEC = ROOT / "tests" / "js" / "sync_button_core_spec.mjs"

# 与 projects/artifacts/遥测同一套出网原语（sync_button_core 一个都不能有）。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
# DOM / localStorage：纯逻辑核职责边界（UI 壳才许碰 DOM；「上次同步」是实例级事实，壳也不许存 localStorage）。
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
    """断言只看真代码（注释里当然会出现「零网络」「localStorage 归 UI」这类说明词）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 职责边界

def test_core_layer_cannot_talk_to_the_network() -> None:
    """纯逻辑核零出网（同步回执/文案推导不碰网络，请求在壳层）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"sync_button_core.js 出现出网原语：{hits}"


def test_core_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage——「上次同步」是实例级事实，任何一层都不得落 per-profile 存储（设计约定）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"sync_button_core.js 出现 DOM/localStorage 访问：{hits}"


def test_core_has_no_hash_imports() -> None:
    """纯逻辑核零 #import——不进 import 图、不进环；node 规格可裸相对 import。"""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r'from\s*"#', text) is None, "sync_button_core.js 出现了 # import（应零依赖纯模块）"


def test_shell_never_stores_sync_status_in_localstorage() -> None:
    """设计约定硬约束：实例级事实不得存 per-profile localStorage（验证#6 裁决）。"""
    code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "localStorage" not in code, "sync_button.js 出现 localStorage——「上次同步」不得落 per-profile 存储"


# ---------------------------------------------------------------- 设计约定文案

def test_design_copy_present_verbatim() -> None:
    """副文案与 busy 文案逐字在场（设计约定；上屏语是行为承诺，漂一个字的成本是用户误解）。"""
    text = CORE.read_text(encoding="utf-8")
    assert 'SYNC_SUB_COPY = "检查官方源更新并导入（仅入外部库，可一键撤销）"' in text, "副文案缺失/漂移"
    assert 'SYNC_BUSY_COPY = "另一个同步任务进行中，请稍候"' in text, "sync_busy 文案缺失/漂移"
    assert "SYNC_RUNNING_COPY" in text, "进行中进度文案常量缺失"


def test_no_update_y_semantics_anywhere() -> None:
    """设计约定红线：sync 无「更新既有记录」语义，结果文案绝不写「更新 Y」。"""
    core_code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    ui_code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "更新 Y" not in core_code and "更新 Y" not in ui_code, "出现「更新 Y」——sync 无更新既有记录语义"
    # 摘要必须经纯核 syncReceiptText 构造（单一真源），壳层不许自拼一份
    assert "syncReceiptText" in core_code and "syncReceiptText" in ui_code, "结果摘要构造（syncReceiptText）缺失"


# ---------------------------------------------------------------- 埋点（设计约定）

def test_usage_kind_registered() -> None:
    """sync_button_used 必须在 USAGE_KINDS（usageLog 依赖该键，不登记打点静默丢失）。"""
    usage = (ROOT / "web" / "static" / "js" / "core" / "usage_core.js").read_text(encoding="utf-8")
    assert 'sync_button_used: "sync_button_used"' in usage, "USAGE_KINDS 缺 sync_button_used"


def test_usage_log_call_shape() -> None:
    """壳层打点：usageLog(USAGE_KINDS.sync_button_used, {added,skipped,failed})（计数型无文本）。"""
    ui = UI.read_text(encoding="utf-8")
    assert "usageLog(USAGE_KINDS.sync_button_used" in ui, "sync_button_used 打点缺失"
    assert "{ added: s.added, skipped: s.skipped, failed: s.failed }" in ui, "打点三计数载荷缺失"


# ---------------------------------------------------------------- 端点真源

def test_endpoints_via_api_map() -> None:
    """三端点必须经 core.js API 真源（curateSyncStatus/curateRecall 为新登记键；sync-updates 既有）。"""
    core = (ROOT / "web" / "static" / "js" / "core" / "core.js").read_text(encoding="utf-8")
    for key in ("curateSyncStatus: \"/api/curate/sync-status\"", "curateRecall: \"/api/curate/recall\""):
        assert key in core, f"core.js API 缺 {key}"
    ui = UI.read_text(encoding="utf-8")
    for ref in ("API.curateSyncUpdates", "API.curateSyncStatus", "API.curateRecall"):
        assert ref in ui, f"sync_button.js 未经 API 真源调 {ref}"


# ---------------------------------------------------------------- 批量联动退役 + 异步 job

def test_legacy_sync_mount_retired_and_async_job_poll_present() -> None:
    """ 联动已随全体批量按钮退役（SYNC_P4_HOOK/ds-sync-p4 零痕迹）；
    网页形态异步 sync-updates 响应（{async:true, job}）的轮询路径必须在场。"""
    ui = UI.read_text(encoding="utf-8")
    assert "SYNC_P4_HOOK" not in ui, "批量联动钩子残留（应已退役）"
    assert "ds-sync-p4" not in ui, "批量联动按钮残留（应已退役）"
    core = (ROOT / "web" / "static" / "js" / "core" / "core.js").read_text(encoding="utf-8")
    assert 'curateSyncJobStatus: "/api/curate/sync-updates/status"' in core, "core.js API 缺 curateSyncJobStatus"
    assert "API.curateSyncJobStatus" in ui, "sync_button.js 缺异步 job 轮询"
    assert "j.async === true" in ui, "sync_button.js 未识别异步响应形状"


# ---------------------------------------------------------------- 登记契约

def test_importmap_and_package_registration() -> None:
    """两页 importmap + package.json 同键登记 #sync_button_core/#sync_button（import 图 parity 门）。"""
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["imports"]
    for spec in ("#sync_button_core", "#sync_button"):
        assert spec in pkg, f"package.json imports 缺 {spec}"
    for page in (INDEX, DATASET):
        html = page.read_text(encoding="utf-8")
        m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
        assert m, f"{page.name} 缺 importmap"
        keys = json.loads(m.group(1))["imports"]
        for spec in ("#sync_button_core", "#sync_button"):
            assert spec in keys, f"{page.name} importmap 缺 {spec}"


def test_dataset_page_skeleton_and_scripts() -> None:
    """dataset.html：#dsSync 挂点骨架 + 两个 script 标签（壳 + 纯核，与首页同款双保险加载）。"""
    html = DATASET.read_text(encoding="utf-8")
    assert 'id="dsSync"' in html, "dataset.html 缺 #dsSync 挂点"
    assert '/static/js/search/sync_button.js?v=' in html, "dataset.html 缺 sync_button.js script 标签"
    assert '/static/js/search/sync_button_core.js?v=' in html, "dataset.html 缺 sync_button_core.js script 标签"


def test_index_html_has_no_feature_changes() -> None:
    """index.html 只允许 importmap 键登记（同步特性只属 dataset.html）：正文不得出现同步按钮特性痕迹。"""
    html = INDEX.read_text(encoding="utf-8")
    assert "dsSync" not in html, "index.html 出现 #dsSync——同步按钮只属 dataset.html，本页不碰"
    assert 'script type="module" src="/static/js/search/sync_button' not in html, "index.html 不应加载同步按钮模块"


# ---------------------------------------------------------------- node 规格驱动

def test_sync_button_core_spec_passes_in_node() -> None:
    """纯逻辑核心真行为：node 直跑 sync_button_core_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    r = subprocess.run([node, str(SPEC)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"sync_button_core_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"
