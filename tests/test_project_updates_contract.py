# -*- coding: utf-8 -*-
"""P4 课题更新检查（project_updates_core.js / project_updates.js，engagement Wave 2）结构契约门。

设计：engagement 落地包（2026-08-22）设计文档 §4.1/§4.2/§4.3/§4.4（F3 更新检查闭环）
/§10（埋点清单）/§7（F4 联动）。与 sync_button/projects/artifacts 契约同一套思路：
**三门测不出真行为**（web_smoke 静态查字符串、node --check 只验语法、import 图只验 import
边），所以这里静态钉死结构不变量，真行为由 `tests/js/project_updates_core_spec.mjs`
在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **纯逻辑核心零 DOM / 零网络 / 零 localStorage / 零 #import**：diff/文案推导是用户
   研究内容边界（设计 §1.3 隐私红线），任何出网/DOM/存储原语即红；相对 import
   artifacts.js（同为纯数据层）是唯一允许的依赖。
2. **设计 §4 文案逐字在场**：「结果超 200 条被截断，无法判定消失」/「本次检查无变化 ·
   刚检查过」/「检索规则已更新」/「重试生成基线」——上屏语是行为承诺，不许漂。
3. **绝不自动改纳入表**：待查看更新逐条处理走「纳入候选」（默认待核验进 candidates）/
   「忽略」，壳层不得出现直接写「已核验」的自动路径（设计 §3.1/§4.4 硬性）。
4. **埋点**：`watch_checked`/`delta_review_completed` 必须在 usage_core USAGE_KINDS
   （usageLog 依赖该键，不登记打点静默丢失）+ 壳层调 usageLog 形状在场（计数型无文本）。
5. **上游同步编排 + 语料代哨兵（2026-08-26 corpus-sync 批）**：全体「检查 N 个追踪的更新」
   按钮（浮窗头 + F2 联动钩子 `window.__engP4CheckProjects`）已撤——用户：点一下太耗费资源。
   批量诉求由「登录后语料代哨兵自动刷新」承接：纯核提供 watchSpecSources/watchSyncJobState/
   watchUpstreamText/watchGenChanged/watchAutoRefreshToast/watchCheckableProjects；壳层经
   API.curateSyncJobStatus 轮询 job、经 readJSON/writeJSON(nsKey("biodata_watch_gen")) 存哨兵
   （不经手写 localStorage——既有「壳层零 localStorage 字面量」口径不破）。
6. **挂点接线**：projects.js 在详情检查条件区调 p4DetailMount、行检查按钮走 runProjectCheck
   （ENG-P4-MOUNT 区域，探测式降级）、注册 setWatchesRefreshedHook 重渲列表徽标。
7. **登记契约**：两页 importmap + package.json 同键含 #project_updates_core/#project_updates；
   两页各有壳/纯核 script 标签。
8. **端点真源**：/api/watch/check 经 core.js API 常量；/api/curate/sync-status 同为
   API 真源（双时间戳「本地目录同步于」只从后端取，不落 per-profile localStorage）。
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "core" / "project_updates_core.js"
UI = ROOT / "web" / "static" / "js" / "core" / "project_updates.js"
PROJECTS = ROOT / "web" / "static" / "js" / "core" / "projects.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET = ROOT / "web" / "static" / "dataset.html"
SPEC = ROOT / "tests" / "js" / "project_updates_core_spec.mjs"

# 与 projects/artifacts/遥测同一套出网原语（纯逻辑核一个都不能有）。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
# DOM / localStorage：纯逻辑核职责边界（UI 壳才许碰 DOM；「本地目录同步」是实例级事实，壳层也不许存 localStorage）。
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
    """纯逻辑核零出网（diff/文案推导不碰网络，请求在壳层）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"project_updates_core.js 出现出网原语：{hits}"


def test_core_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage——纯逻辑核只做结构推导（设计 §1.3 隐私红线）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"project_updates_core.js 出现 DOM/localStorage 访问：{hits}"


def test_core_has_no_hash_imports() -> None:
    """纯逻辑核零 #import——只相对 import artifacts.js（同零 DOM 纯模块），不进 import 图、不进环。"""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r'from\s*"#', text) is None, "project_updates_core.js 出现了 # import（应相对 import artifacts.js）"


def test_shell_never_stores_sync_status_in_localstorage() -> None:
    """「本地目录同步于」是实例级事实（设计 §7，评审①#6）——壳层零 localStorage。"""
    code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "localStorage" not in code, "project_updates.js 出现 localStorage——双时间戳/检查状态不得落 per-profile 存储"


# ---------------------------------------------------------------- 设计 §4 文案

def test_design_copy_present_verbatim() -> None:
    """设计 §4 关键文案逐字在场（上屏语是行为承诺，漂一个字的成本是用户误解）。"""
    text = CORE.read_text(encoding="utf-8")
    assert 'WATCH_TRUNCATED_REMOVED_COPY = "结果超 200 条被截断，无法判定消失"' in text, "截断文案缺失/漂移"
    assert 'WATCH_NO_CHANGE_COPY = "本次检查无变化 · 刚检查过"' in text, "无变化文案缺失/漂移"
    assert 'WATCH_RULE_UPDATED_COPY = "检索规则已更新"' in text, "规则升级文案缺失/漂移"
    assert 'WATCH_RETRY_BASELINE_COPY = "重试生成基线"' in text, "重试基线文案缺失/漂移"
    assert 'WATCH_CHECK_COPY = "检查更新"' in text, "检查按钮文案缺失/漂移"
    assert 'WATCH_SPEC_VERSION = "v1"' in text, "spec 版本常量缺失（与后端 RECORD_FINGERPRINT_SCHEMA 同值）"
    assert 'WATCH_SYNC_TIMEOUT_COPY = "后台仍在同步，稍后再看"' in text, "同步超时文案缺失/漂移"
    assert 'WATCH_SYNC_BUSY_COPY = "另一个更新任务进行中，请稍候"' in text, "job 冲突文案缺失/漂移"


# ---------------------------------------------------------------- diff 语义红线（结构不变量；真行为在 node 规格）

def test_diff_material_change_gates_present() -> None:
    """material change 判定结构在场：新增/指纹/消失三通道 + 截断开关 + 规则升级单列。"""
    text = CORE.read_text(encoding="utf-8")
    for token in ("addedTrusted", "removedTrusted", "ruleUpdated", "fpChanged", "baselineTruncated"):
        assert token in text, f"diff 语义字段 {token} 缺失"


# ---------------------------------------------------------------- 绝不自动改纳入表

def test_no_auto_verify_path_in_shell() -> None:
    """「纳入候选」只以默认「待核验」进 candidates——壳层不得出现自动写「已核验」的路径
    （设计 §3.1/§4.4 硬性：任何自动流程不得直接改纳入表；终态由用户在候选区核验/排除）。"""
    code = _strip_js_comments(UI.read_text(encoding="utf-8"))
    assert "artifactsAddCandidate" in code, "纳入候选必须走 artifactsAddCandidate（默认待核验）"
    assert '"已核验"' not in code, "壳层出现直接写「已核验」的路径——违反绝不自动改纳入表"


# ---------------------------------------------------------------- 埋点（设计 §10）

def test_usage_kinds_registered() -> None:
    """watch_checked / delta_review_completed 必须在 USAGE_KINDS（usageLog 依赖该键，不登记打点静默丢失）。"""
    usage = (ROOT / "web" / "static" / "js" / "core" / "usage_core.js").read_text(encoding="utf-8")
    for kind in ("watch_checked", "delta_review_completed"):
        assert f'{kind}: "{kind}"' in usage, f"USAGE_KINDS 缺 {kind}"


def test_usage_log_call_shapes() -> None:
    """壳层打点形状在场（计数型无文本）：watch_checked{changed} / delta_review_completed{}。"""
    ui = UI.read_text(encoding="utf-8")
    assert "usageLog(USAGE_KINDS.watch_checked" in ui, "watch_checked 打点缺失"
    assert "changed: watchChangedFlag(diff)" in ui, "watch_checked{changed} 载荷缺失"
    assert "usageLog(USAGE_KINDS.delta_review_completed, {})" in ui, "delta_review_completed 打点缺失"


# ---------------------------------------------------------------- 全体批量按钮退役 + corpus-sync 编排（2026-08-26）

def test_batch_button_retired() -> None:
    """全体「检查 N 个追踪的更新」按钮已撤（用户：点一下太耗费资源）：壳层零批量面板/F2 钩子
    痕迹，index.html 零浮窗头挂点，纯核零批量函数。"""
    ui = UI.read_text(encoding="utf-8")
    for gone in ("p4RunBatch", "__engP4CheckProjects", "artifactsWinP4Mount", "检查 \" + n + \" 个追踪的更新"):
        assert gone not in ui, f"project_updates.js 残留已退役的批量入口：{gone}"
    index = INDEX.read_text(encoding="utf-8")
    assert "artifactsWinP4Mount" not in index, "index.html 残留浮窗头批量挂点"
    core = _strip_js_comments(CORE.read_text(encoding="utf-8"))   # 注释允许提及历史名（交代退役原因）
    for gone in ("watchBatchSlice", "watchBatchRestText", "watchSummaryText", "WATCH_BATCH_MAX", "watchCheckCount"):
        assert gone not in core, f"project_updates_core.js 残留无调用方的批量函数：{gone}"


def test_corpus_sync_orchestration_present() -> None:
    """corpus-sync 批结构在场：纯核六个新纯函数；壳层经 API 真源轮询 job + 哨兵读写走
    readJSON/writeJSON(nsKey(...))（不手写 localStorage）；注册 health/账户双钩子。"""
    core = CORE.read_text(encoding="utf-8")
    for fn in ("watchSpecSources", "watchSyncJobState", "watchUpstreamText",
               "watchGenChanged", "watchAutoRefreshToast", "watchCheckableProjects"):
        assert f"export function {fn}" in core, f"纯核缺 {fn}"
    ui = UI.read_text(encoding="utf-8")
    assert "API.curateSyncJobStatus" in ui, "壳层缺 job 状态轮询"
    assert 'nsKey(WATCH_GEN_LS)' in ui and '"biodata_watch_gen"' in ui, "语料代哨兵存储键缺失"
    assert "setHealthArrivedHook" in ui and "setAccountChangedHook" in ui, "health/账户钩子注册缺失"
    assert "webGuardOn" in ui, "guard 判定缺失（本机形态不自动刷）"


# ---------------------------------------------------------------- 挂点接线（ENG-P4-MOUNT）

def test_delta_entry_links_to_dataset_page() -> None:
    """设计 §4.4「待查看更新逐条可点开看数据集」：每条 uid 提供 /dataset?uid=… 链接
    （dataset_page.js 的独立标签页约定，uid 为主键）。"""
    ui = UI.read_text(encoding="utf-8")
    assert 'href = "/dataset?uid=" + encodeURIComponent(d.uid)' in ui, "待查看更新条目缺「查看数据集」链接"
    assert 'textContent = "查看数据集"' in ui, "「查看数据集」链接文案缺失"


def test_projects_mount_wiring_present() -> None:
    """projects.js 的 ENG-P4-MOUNT 区域接线：详情检查条件区调 p4DetailMount、行检查按钮走 runProjectCheck
    （探测式降级——typeof 守卫，P4 壳未加载不报错）、注册自动刷新完成钩子重渲列表徽标。"""
    projects = PROJECTS.read_text(encoding="utf-8")
    assert 'from "#project_updates"' in projects, "projects.js 未 import #project_updates"
    assert "typeof p4DetailMount === \"function\"" in projects, "详情挂载缺探测式降级守卫"
    assert "typeof runProjectCheck === \"function\"" in projects, "行检查缺探测式降级守卫"
    assert "typeof setWatchesRefreshedHook === \"function\"" in projects, "自动刷新完成钩子注册缺失"


# ---------------------------------------------------------------- 端点真源

def test_endpoints_via_api_map() -> None:
    """/api/watch/check 与 /api/curate/sync-status 必须经 core.js API 真源（不散裸串）。"""
    core = (ROOT / "web" / "static" / "js" / "core" / "core.js").read_text(encoding="utf-8")
    for key in ('watchCheck: "/api/watch/check"', 'curateSyncStatus: "/api/curate/sync-status"'):
        assert key in core, f"core.js API 缺 {key}"
    ui = UI.read_text(encoding="utf-8")
    for ref in ("API.watchCheck", "API.curateSyncStatus"):
        assert ref in ui, f"project_updates.js 未经 API 真源调 {ref}"


# ---------------------------------------------------------------- 登记契约

def test_importmap_and_package_registration() -> None:
    """两页 importmap + package.json 同键登记 #project_updates_core/#project_updates（import 图 parity 门）。"""
    pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["imports"]
    for spec in ("#project_updates_core", "#project_updates"):
        assert spec in pkg, f"package.json imports 缺 {spec}"
    for page in (INDEX, DATASET):
        html = page.read_text(encoding="utf-8")
        m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
        assert m, f"{page.name} 缺 importmap"
        keys = json.loads(m.group(1))["imports"]
        for spec in ("#project_updates_core", "#project_updates"):
            assert spec in keys, f"{page.name} importmap 缺 {spec}"


def test_both_pages_load_shell_scripts() -> None:
    """index.html 显式加载壳 + 纯核（防 import 边误删的静默失能，F2/F3 同款双保险）；
    dataset.html 不再加载（F2 联动随全体批量按钮退役，2026-08-26 corpus-sync 批）——
    importmap 键两页保留（parity 门），script 标签只属 index.html。"""
    html = INDEX.read_text(encoding="utf-8")
    assert '/static/js/core/project_updates.js?v=' in html, "index.html 缺 project_updates.js script 标签"
    assert '/static/js/core/project_updates_core.js?v=' in html, "index.html 缺 project_updates_core.js script 标签"
    dataset = DATASET.read_text(encoding="utf-8")
    assert 'src="/static/js/core/project_updates.js?v=' not in dataset, \
        "dataset.html 不应再加载 project_updates.js（F2 联动已退役；importmap 键保留）"


# ---------------------------------------------------------------- node 规格驱动

def test_project_updates_core_spec_passes_in_node() -> None:
    """纯逻辑核心真行为：node 直跑 project_updates_core_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    r = subprocess.run([node, str(SPEC)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"project_updates_core_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"
