# -*- coding: utf-8 -*-
"""F1 课题 UI（projects_core.js / projects.js，engagement Wave 1B）的结构契约门。

设计：engagement 落地包（2026-08-22）设计文档 §2/§3（F1 课题）/§3.3（上下文卡）/
§3.4（coachmark）/§9（教程：coachmark 承担就地引导，不加步骤）。
与 artifacts/usage/benchfb 契约同一套思路：**三门测不出真行为**（web_smoke 静态查字符串、
node --check 只验语法、import 图只验 import 边），所以这里静态钉死结构不变量，
真行为由 `tests/js/projects_core_spec.mjs` 在 node 里逐条断言（本文件末尾驱动）。

钉死的结构不变量：

1. **纯逻辑核心零 DOM / 零网络 / 零 localStorage**：课题内容是用户研究内容（设计 §1.3 隐私
   红线）；projects_core.js 出现任何出网/DOM/存储原语即红（与 artifacts 同红线）。
2. **上下文卡硬 cap 常量在场**：≤2000 Unicode 字符 / 目标 ≤300 / 候选 ≤20（设计 §3.3）——
   超限「另有 N 项未注入」不静默截断（设计 §1.1）。
3. **P4/P5 挂点**：课题详情检查条件区 / 导出记录区的 `ENG-P4-MOUNT` / `ENG-P5-MOUNT` 注释标记
   必须存在于 projects.js 与 index.html（Wave 2 直接按标记挂载）。
4. **入口/骨架在场**：导航「档案」（#archiveNav，fx2 起课题并入档案浮窗 projects tab）、
   「存为课题」（#saveProjectBtn）、档案浮窗（#archiveWin，课题面板保留 #artifactsWinBody）、
   上下文卡挂点（#artifactCtx）、首页「继续课题」条（#homeProjects）
   ——缺一个用户可见功能就整体缺位。
5. **隐私口径**：上下文卡文案必须同时覆盖「会发往你配置的 AI 服务商」与「不出本机」
   （设计 §1.3 修订口径，评审①阻断1）。
6. **account 钩子走注册反转**：projects.js 不得被 accounts.js 直接 import（会成环——
   projects→interactions→browse→accounts→projects），必须经 setAccountChangedHook 注册。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "web" / "static" / "js" / "core" / "projects_core.js"
UI = ROOT / "web" / "static" / "js" / "core" / "projects.js"
INDEX = ROOT / "web" / "static" / "index.html"
SPEC = ROOT / "tests" / "js" / "projects_core_spec.mjs"

# 与 artifacts/遥测同一套出网原语（projects_core 一个都不能有）。
FORBIDDEN_NETWORK_TOKENS = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
# DOM / localStorage：纯逻辑核职责边界（UI 壳才许碰 DOM）。
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

def test_projects_core_layer_cannot_talk_to_the_network() -> None:
    """课题内容是用户研究内容，任何出网原语都是隐私事故（设计 §1.3 红线）。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_NETWORK_TOKENS if t in code]
    assert not hits, f"projects_core.js 出现出网原语：{hits}"


def test_projects_core_layer_has_no_dom_or_localstorage() -> None:
    """零 DOM、零 localStorage——纯逻辑核只做规格构造/序列化/文案，界面在 projects.js。"""
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    hits = [t for t in FORBIDDEN_DOM_TOKENS if t in code]
    assert not hits, f"projects_core.js 出现 DOM/localStorage 访问：{hits}"


def test_projects_core_has_no_hash_imports() -> None:
    """纯逻辑核只相对 import artifacts.js（同为零 DOM 纯模块）——不进 import 图、不进环。"""
    text = CORE.read_text(encoding="utf-8")
    assert re.search(r'from\s*"#', text) is None, "projects_core.js 出现了 # import（应相对 import artifacts.js）"


# ---------------------------------------------------------------- 上下文卡常量

def test_context_card_caps_are_present() -> None:
    """设计 §3.3 硬 cap 常量：2000 Unicode / 目标 300 / 候选 20（纳入排除与数据层同源 ≤8）。"""
    text = CORE.read_text(encoding="utf-8")
    assert "PROJECTS_CTX_MAX_CHARS = 2000" in text, "上下文卡 2000 字硬 cap 常量缺失"
    assert "PROJECTS_CTX_MAX_GOAL = 300" in text, "研究目标 ≤300 常量缺失"
    assert "PROJECTS_CTX_MAX_CANDIDATES = 20" in text, "候选 ≤20 常量缺失"
    # 「另有 N 项未注入」的 omitted 语义必须在场（不静默截断）
    assert "omitted" in text, "截断省略计数（omitted）缺失"


# ---------------------------------------------------------------- P4/P5 挂点

def test_p4_p5_mount_markers_present() -> None:
    """Wave 2 P4（更新检查）/P5（导出中心）按 ENG-P4-MOUNT / ENG-P5-MOUNT 注释标记挂载。
    P4 挂点只在 projects.js（详情检查条件区）；P5 挂点也只在 projects.js（导出记录区随详情动态渲染）。
    2026-08-26 corpus-sync 批：index.html 的 P4 静态骨架（浮窗头/首页条）随
    全体「检查 N 个追踪的更新」按钮一并退役（用户裁决：全体检查太耗费资源；
    批量刷新改由 corpus.gen 哨兵驱动的登录后自动编排承担），index.html 不应再有 P4 挂点。"""
    ui = UI.read_text(encoding="utf-8")
    index = INDEX.read_text(encoding="utf-8")
    assert "ENG-P4-MOUNT" in ui, "projects.js 缺 ENG-P4-MOUNT 注释标记"
    assert "ENG-P4-MOUNT" not in index, "index.html 不应再有 P4 静态挂点（全体更新按钮已退役）"
    assert "ENG-P5-MOUNT" in ui, "projects.js 缺 ENG-P5-MOUNT 注释标记"


def test_check_section_does_not_render_when_empty() -> None:
    """检查条件展示区「内容为空时不渲染」（设计 §3.1/§4.1：不伪造条件）；导出记录区同。"""
    ui = UI.read_text(encoding="utf-8")
    assert "let checkHtml = \"\"" in ui, "检查条件区空渲染守卫缺失（空时整段不渲染）"
    assert "hasCheck" in ui, "检查条件是否存在判定缺失"


# ---------------------------------------------------------------- 入口/骨架

def test_entry_points_and_skeleton_present() -> None:
    """我的库导航/存为追踪/浮窗/上下文 chip 挂点骨架必须在 index.html 里。
课题入口并入「我的库」浮窗（#libNav + #libWin 的追踪页签）。"""
    index = INDEX.read_text(encoding="utf-8")
    for token in ('id="libNav"', 'id="saveProjectBtn"', 'id="libWin"',
                  'id="artifactCtx"', 'id="libWinClose"', 'id="histNav"', 'id="histWin"'):
        assert token in index, f"index.html 缺 {token}"


def test_privacy_copy_covers_both_cases() -> None:
    """上下文卡隐私口径（设计 §1.3 修订）：远端模型明示会发往配置的 AI 服务商；本地模型「不出本机」。"""
    ui = UI.read_text(encoding="utf-8")
    assert "会发往你配置的 AI 服务商" in ui, "远端模型隐私明示缺失"
    assert "不出本机" in ui, "本地模型「不出本机」标注缺失"


def test_account_hook_uses_registered_inversion() -> None:
    """accounts.js 不得直接 import projects.js（会成环）——必须经 setAccountChangedHook 注册反转。"""
    accounts = (ROOT / "web" / "static" / "js" / "panel" / "accounts.js").read_text(encoding="utf-8")
    ui = UI.read_text(encoding="utf-8")
    assert 'from "#projects"' not in accounts, "accounts.js 直接 import projects.js——会成环，应走 setAccountChangedHook"
    assert "setAccountChangedHook" in accounts and "setAccountChangedHook" in ui, "账户切换钩子（注册反转）缺失"


# ---------------------------------------------------------------- 埋点 kind

def test_usage_kinds_registered() -> None:
    """埋点（计数型无文本）：project_created / project_resumed / context_card_used 必须在 USAGE_KINDS。"""
    usage = (ROOT / "web" / "static" / "js" / "core" / "usage_core.js").read_text(encoding="utf-8")
    for kind in ("project_created", "project_resumed", "context_card_used"):
        assert f'{kind}: "{kind}"' in usage, f"USAGE_KINDS 缺 {kind}"


# ---------------------------------------------------------------- node 规格驱动

def test_projects_core_spec_passes_in_node() -> None:
    """纯逻辑核心真行为：node 直跑 projects_core_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    r = subprocess.run([node, str(SPEC)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"projects_core_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"
