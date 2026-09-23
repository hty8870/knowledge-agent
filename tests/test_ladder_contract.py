# -*- coding: utf-8 -*-
""" 下一步行动（结果页阶梯 chips + 任务卡 + suggested_recipe + template_originated）
的前端静态契约门（设计约定；三门都不执行 JS，真行为由 node 规格断言）。

钉的结构不变量：
1. **纯核纪律**：ladder_core.js 零 DOM / 零网络 / 零存储 / 零 `#` import（node 可单测）。
2. **模块加载契约**：ladder_core/ladder/task_card 三键在本页 importmap + package.json 登记、
   三枚 script 标签在 boot 之前；本包模块间互引走**相对 import**（同 feedback.js 哲学——
   不新增 `#` 静态 import、不牵动 dataset.html，parity 门天然不红）。
3. **注册式反转**：results.js 导出 setLadderRenderHook 并在渲染点调用；ladder.js 经它注册，
   results **不** import ladder（无新 SCC 成员，import 图门已验证）。
4. **三类行为接线**：① 直接执行（raw_only → setFacetState + runRecommend 重跑）；② 确定性导出
   （动态 import("#export_center") 探测，无静态 #export_center import）；③ 任务卡（task_card.js
   弹窗骨架 + 「开始」才发送）。
5. **suggested_recipe 全链路**：ubRouteBody 带 suggested_recipe；ubSubmit(source, opts) 可传
   text/suggestedRecipe/templateOriginated；benchfbTurnBegin 落 template_originated 键。
6. **埋点（USAGE_KINDS 登记）**：ladder_shown / ladder_clicked / template_originated。
7. **遥测导出默认排除**：telemetry_export.py 与 benchfb_ingest.py 的候选生成点排除
   template_originated=true 的轮次；KNOWN_BENFB_KEYS 登记该键。
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"
CORE = JS_DIR / "search" / "ladder_core.js"
LADDER = JS_DIR / "search" / "ladder.js"
TASK_CARD = JS_DIR / "search" / "task_card.js"
RESULTS = JS_DIR / "search" / "results.js"
BENCHFB = JS_DIR / "core" / "benchfb.js"
BOARD = JS_DIR / "panel" / "board.js"
USAGE_CORE = JS_DIR / "core" / "usage_core.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET = ROOT / "web" / "static" / "dataset.html"
PKG = ROOT / "package.json"
SPEC = ROOT / "tests" / "js" / "ladder_core_spec.mjs"
TELEMETRY = ROOT / "scripts" / "telemetry_export.py"
INGEST = ROOT / "scripts" / "benchfb_ingest.py"

LADDER_KEYS = ("#ladder_core", "#ladder", "#task_card")
NEW_MODULES = ("ladder_core", "ladder", "task_card")


def _resolve_node() -> str | None:
    import os
    import shutil
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _importmap_keys(page: Path) -> dict:
    html = page.read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert m, f"{page.name} 缺 importmap"
    return __import__("json").loads(m.group(1))["imports"]


# ---------------------------------------------------------------- 1. 纯核纪律

def test_core_layer_is_pure() -> None:
    code = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    for token in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource",
                  "localStorage", "sessionStorage", "document.", "window."):
        assert token not in code, f"ladder_core 出现 DOM/网络/存储原语：{token}"
    assert 'from "#' not in code, "ladder_core 不许有 # import（纯核零依赖）"
    assert 'from "./' not in code and 'from "/' not in code, "ladder_core 不许有模块依赖"


def test_shells_do_not_leak_network_or_storage() -> None:
    for path in (LADDER, TASK_CARD):
        code = _strip_js_comments(path.read_text(encoding="utf-8"))
        for token in ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource",
                      "localStorage", "sessionStorage"):
            assert token not in code, f"{path.name} 出现出网/存储原语：{token}"


# ---------------------------------------------------------------- 2. 模块加载契约

def test_importmap_and_package_registered() -> None:
    index_map = _importmap_keys(INDEX)
    import json
    pkg = json.loads(PKG.read_text(encoding="utf-8"))["imports"]
    for key in LADDER_KEYS:
        assert key in index_map, f"index.html importmap 缺 {key}"
        assert key in pkg, f"package.json imports 缺 {key}"
    # 本包三键只在本页 + package.json 登记；dataset.html 无本包键（归另一并行改动，不碰）。
    dataset_map = _importmap_keys(DATASET)
    for key in LADDER_KEYS:
        assert key not in dataset_map, f"dataset.html 不应出现本包键 {key}（改动边界）"


def test_script_tags_present_before_boot() -> None:
    html = INDEX.read_text(encoding="utf-8")
    for mod in NEW_MODULES:
        assert f'src="/static/js/search/{mod}.js' in html, f"index.html 缺 script 标签 {mod}"
    boot_idx = html.index('src="/static/js/core/boot.js')
    for mod in NEW_MODULES:
        assert html.index(f'src="/static/js/search/{mod}.js') < boot_idx, f"{mod} 必须在 boot 之前"


def test_no_static_import_of_own_modules() -> None:
    """本包模块间互引走相对 import（同 feedback.js 哲学）——任何 `#ladder*`/`#task_card`
    静态 import 都会牵动 dataset.html importmap（跨改动边界），静态钉死。"""
    for path in (LADDER, TASK_CARD, CORE):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r'import\s*\{[^}]*\}\s*from\s*"(#[A-Za-z_0-9]+)"', text):
            assert m.group(1) not in LADDER_KEYS, f"{path.name} 不得静态 import 本包键 {m.group(1)}"
    assert 'import("#export_center")' in LADDER.read_text(encoding="utf-8"), "导出探测必须走动态 import"


# ---------------------------------------------------------------- 3. 注册式反转

def test_results_exposes_render_hook_without_importing_ladder() -> None:
    results = RESULTS.read_text(encoding="utf-8")
    assert "export function setLadderRenderHook" in results, "results.js 缺 setLadderRenderHook"
    assert "_ladderRenderHook(data)" in results, "results.js 渲染点未调用阶梯钩子"
    assert 'from "#ladder"' not in results and 'from "./ladder' not in results, \
        "results.js 不得 import ladder（注册式反转，防新 SCC 成员）"
    assert 'setLadderRenderHook' in LADDER.read_text(encoding="utf-8"), "ladder.js 未注册钩子"


# ---------------------------------------------------------------- 4. 三类行为接线

def test_task_card_skeleton_present() -> None:
    html = INDEX.read_text(encoding="utf-8")
    for node_id in ("taskCardModal", "taskCardTitle", "taskCardMeta", "taskCardText",
                    "taskCardStartBtn", "taskCardCancelBtn", "taskCardCloseBtn", "taskCardFail"):
        assert f'id="{node_id}"' in html, f"index.html 缺任务卡骨架 {node_id}"
    assert "开始" in html and "任务内容（可直接修改）" in html


def test_direct_action_wires_existing_facets() -> None:
    ladder = LADDER.read_text(encoding="utf-8")
    assert "setFacetState" in ladder, "直接执行必须走属主 setter"
    assert "runRecommend({ keepFacets: true })" in ladder, "套分面后必须带 keepFacets 重跑"
    assert "cbLogPush" in ladder, "分面动作须留对话痕迹"
    assert "RAW_ONLY_FACET" in ladder, "只看原始数据可用必须用既有分面取值"


def test_task_card_requires_explicit_start() -> None:
    card = TASK_CARD.read_text(encoding="utf-8")
    assert "taskCardOpen" in card and "export function taskCardOpen" in card
    assert 'onSubmit({ text:' in card, "开始后才把任务文本交回调用方"
    assert "usageLog(USAGE_KINDS.template_originated" in card, "template_originated 计数缺失"
    assert "ladderTemplateOriginated" in card, "origination 判定必须消费纯核"


# ---------------------------------------------------------------- 5. suggested_recipe 全链路

def test_board_supports_recipe_and_originated_opts() -> None:
    board = BOARD.read_text(encoding="utf-8")
    assert "export async function ubSubmit(source, opts)" in board, "ubSubmit 必须接受 opts"
    assert "opts.suggestedRecipe" in board, "ubRouteBody 缺 suggested_recipe 注入"
    assert "body.suggested_recipe" in board, "请求体缺 suggested_recipe 键"
    assert "templateOriginated: opts.templateOriginated" in board, "benchfbTurnBegin 未透传 origination"
    assert "opts.text" in board, "任务卡程序化提交缺 opts.text 通道"


def test_benchfb_round_records_template_originated() -> None:
    code = _strip_js_comments(BENCHFB.read_text(encoding="utf-8"))
    assert "template_originated" in code, "benchfb 轮次缺 template_originated 键"
    assert "opts.templateOriginated" in code, "benchfbTurnBegin 未读取 opts.templateOriginated"


# ---------------------------------------------------------------- 6. 埋点登记

def test_usage_kinds_registered() -> None:
    usage = USAGE_CORE.read_text(encoding="utf-8")
    for kind in ("ladder_shown", "ladder_clicked", "template_originated"):
        assert f'{kind}: "{kind}"' in usage, f"USAGE_KINDS 缺 {kind}"
    ladder = LADDER.read_text(encoding="utf-8")
    assert "usageLog(USAGE_KINDS.ladder_shown" in ladder, "ladder_shown 打点缺失"
    assert "usageLog(USAGE_KINDS.ladder_clicked" in ladder, "ladder_clicked 打点缺失"


# ---------------------------------------------------------------- 7. 遥测导出默认排除

def test_telemetry_export_registers_and_excludes() -> None:
    tel = TELEMETRY.read_text(encoding="utf-8")
    assert '"template_originated"' in tel, "KNOWN_BENFB_KEYS 未登记 template_originated"
    assert 'masked_rec.get("template_originated") is True' in tel, "候选生成未排除模板轮次"
    assert '"template_originated": rec.get("template_originated")' in tel, "候选未带该字段"
    ingest = INGEST.read_text(encoding="utf-8")
    assert 'r.get("template_originated") is not True' in ingest, "benchfb_ingest 候选未排除模板轮次"


# ---------------------------------------------------------------- node 规格驱动

def test_ladder_core_spec_passes_in_node() -> None:
    node = _resolve_node()
    if not node:
        import pytest
        pytest.skip("node 不可用")
    out = subprocess.run([node, str(SPEC)], capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, f"node 规格失败：\n{out.stdout}\n{out.stderr}"
    assert "LADDER_CORE_SPEC_OK" in out.stdout, out.stdout
