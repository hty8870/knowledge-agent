# -*- coding: utf-8 -*-
"""一句话执行层前端的接线门 + **真行为**门（后者在 node 里跑真函数）。

三门都不执行 JS：`web_smoke_test.py` 只做静态字符串检查、`node --check` 只验语法。
所以这一层最要命的两个不变量——**失败回执写不出「已」字**、**低置信度不会被跳过**——
必须分别用真行为门和位置断言钉住，否则以后有人「顺手统一一下文案」就把它们改没了，
而全部三门照绿。
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from dataset_recommender.agent import action_plan as AP

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"


def _read(name: str) -> str:
    return (STATIC / "js" / name).read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """只留代码。**注释里写了什么不算数**——本文件第一版就被自己那句
    「刻意不写 typeof … 守卫」的注释判成违规，正是这条的存在理由。"""
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", out)


ACT_CORE = _read("act/act_core.js")
ACT = _read("act/act.js")
BOARD = _read("panel/board.js")
SEARCH = _read("search/search.js")
RESULTS = _read("search/results.js")
SHELL = _read("core/shell.js")
CORE = _read("core/core.js")
BOOT = _read("core/boot.js")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")


def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_node(script: str, payload: object, suffix: str = ".cjs") -> object:
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过执行层真行为门（full 质量门的语法检查环节必有 node）。")
    # act.js 长大之后 `node -e <script>` 会撞 Windows 命令行长度上限（WinError 206，
    # 2026-08-01 curate.* 接线后实测触发）——脚本落临时文件再跑。门的语义不变：
    # 仍是加载真 act_core.js、在 node 里跑真函数。
    fd, script_path = tempfile.mkstemp(suffix=suffix, prefix="biodata_act_gate_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(script)
        proc = subprocess.run(
            [node, script_path], cwd=str(ROOT),
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def _act_in_node(expr: str, payload: object) -> object:
    """在 node 里加载执行层的纯函数（act_core.js 纯核），跑一段表达式。

    act_core.js 已是 ES Module（Phase C · C1）——不能再文本拼接进 CJS 脚本。
    改为临时 .mjs：经 file:// URL import 纯核命名空间、挂上 globalThis 后跑表达式。
    传入表达式只引用 act_core 的符号（act.js 不需要进 node）；act_core 顶层不碰 DOM，无需桩。"""
    script = (
        f'import * as ns from "{(STATIC / "js" / "act" / "act_core.js").as_uri()}";\n'
        'import { readFileSync } from "node:fs";\n'
        "Object.assign(globalThis, ns);\n"
        "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        "console.log(JSON.stringify(" + expr + "));\n"
    )
    return _run_node(script, payload, suffix=".mjs")


def _function_source(src: str, name: str) -> str:
    """从真实浏览器源码截出一个具名函数，供 Node 行为钉直接执行。"""
    m = re.search(rf"(?:export\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{", src)
    assert m, f"找不到函数 {name}"
    start = m.start()
    brace = src.find("{", m.start())
    depth = 0
    for i in range(brace, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return re.sub(r"^export\s+", "", src[start:i + 1])
    raise AssertionError(f"函数 {name} 大括号未闭合")


def _browser_helper_in_node(src: str, name: str, expr: str, payload: object) -> object:
    """2026-08-18：执行 results/act 中的纯 helper，避免静态字符串钉再次误绿。"""
    script = (
        'const { readFileSync } = require("node:fs");\n'
        + _function_source(src, name) + "\n"
        + 'const _in = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "console.log(JSON.stringify(" + expr + "));\n"
    )
    return _run_node(script, payload)


def _card_in_node(src: str, name: str, expr: str, payload: object, deps: "list[str] | None" = None) -> object:
    """：在 node 里跑 act.js 的四工具卡片构造函数（真行为）。

    act.js 的卡片构造函数引用从 #core / #act_core 导入的 `escapeHtml` / `tpBytes`
    （与 act_core 顶层不同，act.js 顶层依赖浏览器）——node 里给**行为与真实现一致**的
    纯函数桩（escapeHtml 逐字符同 core.js；tpBytes 从 act_core.js 抽取真函数、不再手桩——
    曾因手桩掩盖 act.js 漏 import tpBytes 的 ReferenceError，import 缺失由结构门
    单独钉死）；API 只供 citationsDownload 常量，卡片只读它。deps：被测函数引用的其它
    act.js 具名函数（如 actLoopStepCardHtml 分发的四个构造函数），一并抽取进 node 作用域。
    断言渲染出的 HTML 结构/措辞。"""
    stubs = (
        'function escapeHtml(v) { return String(v ?? "").replaceAll("&", "&amp;")'
        '.replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll(String.fromCharCode(34), "&quot;")'
        '.replaceAll(String.fromCharCode(39), "&#39;"); }\n'
        + _function_source(_read("act/act_core.js"), "tpBytes") + "\n"
        'const API = { citationsDownload: "/api/citations/download" };\n'
    )
    deps_src = "".join(_function_source(src, d) + "\n" for d in (deps or []))
    # arxRow 是 act.js 卡片行骨架的文件级共享助手：卡片函数普遍引用，随真源码一并抽取。
    if "function arxRow(" in src:
        deps_src += _function_source(src, "arxRow") + "\n"
    script = (
        'const { readFileSync } = require("node:fs");\n'
        + stubs
        + deps_src
        + _function_source(src, name) + "\n"
        + 'const _in = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "console.log(JSON.stringify(" + expr + "));\n"
    )
    return _run_node(script, payload)


# ---------------------------------------------------------------- 真行为：回执写不出假话

def test_the_failure_template_structurally_cannot_say_it_was_done():
    """`ok=false` 的模板里**取不到「已」字**。

    这不是「拼字符串时小心别写上」，而是失败那一支根本没有那个字面量：`ACT_LEAD.fail`
    是一个可以被直接调用、直接断言的函数。逐个动词名都过一遍——万一以后有人往词表里
    加一个自带「已」字的中文名（「已归档清单」这种），本条当场红。
    """
    zh_names = [s.zh for s in AP.VERB_SPECS]
    out = _act_in_node("_in.map((zh) => [ACT_LEAD.fail(zh), actLeadZh(false, zh), actLeadZh(true, zh)])", zh_names)
    for zh, (fail_tpl, lead_false, lead_true) in zip(zh_names, out):
        assert "已" not in fail_tpl, f"失败模板里出现了「已」：{fail_tpl}"
        assert "已" not in lead_false, f"ok=false 的抬头里出现了「已」：{lead_false}"
        assert lead_true.startswith("已"), f"成功抬头应当以「已」开头：{lead_true}"
        assert zh in fail_tpl and zh in lead_true


def test_the_summary_factual_line_comes_from_the_same_template():
    """对话流里那条执行总结的事实句与回执**必须同源**（p10 起总结长在对话流，不再设结果区回执卡）。

    第一版 `board.js` 自己写死了一句「已按你说的执行，回执在结果区」，只要执行层返回了
    truthy 就贴上去 —— 于是「上一步还在跑」「屏上没结果」「产包失败」三档全都：
    回执写着「这一步没有完成」，聊天里写着「已按你说的执行」。同一件事，两句相反的话。
    p10 的落法：事实句只由 act_core 的 actWhatHappened 构造（成功支带「已」、失败支结构上
    取不到「已」），board.js 不许自己造文案（它拿到的 true 只表示「执行层已全程呈现」，
    挂的是空注记）。LLM 总结只是对这句事实句的改写层（act.js actFetchLlmSummary），
    不改写时事实句原样留存。"""
    code = _strip_comments(ACT)
    assert "actWhatHappened(plan, outcome)" in code, "act.js 的事实句没有走 act_core 的 actWhatHappened"
    assert "actFetchLlmSummary(" in code, "LLM 总结改写层不在（总结应可由 /api/act/summary 改写）"
    bcode = _strip_comments(BOARD)
    assert "已按你说的执行" not in bcode, "board.js 又在自己写死一句「已…」注记"
    assert 'cbMarkMessageAsAction(pending, "")' in bcode, "行动流全程呈现时调用方只标 action、不挂文案"
    assert "actDispatchPlanChain(plan, said)" in bcode, "统一路由的 tool 档必须经共享规范链派发"


def test_busy_and_blocked_paths_never_claim_the_step_was_done():
    """两条「什么都没做」的分支：上一步还在跑 / 屏上没结果。"""
    code = _strip_comments(ACT_CORE)
    busy = re.search(r'const ACT_BUSY_NOTE = "([^"]+)"', code)
    assert busy, "找不到 ACT_BUSY_NOTE"
    assert "已" not in busy.group(1), f"忙碌时的注记里出现了「已」：{busy.group(1)}"
    body = re.search(r"async function actDispatchPlan\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "找不到 actDispatchPlan"
    dispatch = _strip_comments(body.group(1))
    # p10 返回值契约：true=行动流全程呈现（调用方只标 action、不挂文案）；字符串=以注记挂上
    # （取消 reason / 忙碌）；false=不属执行。true 之所以安全：文案从来不由调用方造。
    assert "ACT_BUSY_NOTE" in dispatch, "忙碌档要如实回忙碌注记"
    assert "plan.reason_zh" in dispatch, "取消档要原样回后端 reason_zh"
    assert "arxFail()" in dispatch, "屏上没结果的 blocked 档要在行动流里如实亮失败"
    blocked = re.search(r"const blocked = \{ ok: false, error: \"([^\"]+)\" \}", dispatch)
    assert blocked and "已" not in blocked.group(1), "blocked 的事实句里出现了「已」"


def test_a_failed_blob_download_is_not_reported_as_done():
    """`downloadTextBlob` 内部 try/catch 只 toast 一句「下载失败」。不看返回值 →
    浏览器根本没存下文件时回执照样写「已导出引文：… 22.0 KB」。"""
    reuse = _read("act/reuse_pack.js")
    fn = re.search(r"function downloadTextBlob\([^)]*\)\s*\{(.*?)\n\}", reuse, re.S)
    assert fn, "找不到 downloadTextBlob"
    assert "return true" in fn.group(1) and "return false" in fn.group(1), (
        "downloadTextBlob 必须回报是否真的存下来了"
    )
    for runner in ("actRunCiteExport", "actRunReusePack"):
        body = re.search(r"async function " + runner + r"\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
        assert body, runner
        code = _strip_comments(body.group(1))
        # 返回值消费纪律钉在共用锚点上：autoDownloadReuseTrio 返回 {md, ris, bib} 布尔，
        # 两个 runner 都必须取回并用 !saved 判败（锚点内部转发 downloadTextBlob 的真伪）。
        assert "= autoDownloadReuseTrio(" in code and "if (!saved)" in code, (
            f"{runner} 没有消费 autoDownloadReuseTrio 的返回值"
        )


def test_a_slot_is_never_reported_twice_in_the_same_receipt():
    """后端对 clamped / dropped 是**两处都填**的（一条 delta + 一个 slot_source）。
    照单全收 → 同一栏里同一件事说两遍，读起来像两处偏离。本仓库在 coverage_caveats 上
    栽过一次同形的「双算」。"""
    plans = [
        # 钳位：deltas 已经逐字说过 limit，slot_sources 不再重复
        {"deltas": [{"slot": "limit", "said": "80", "used": "50", "why_zh": "一次最多 50 条。"}],
         "slot_sources": {"limit": "clamped", "target": "default"}},
        # 没有 delta 的 guessed：必须照说不误
        {"deltas": [], "slot_sources": {"limit": "guessed"}},
    ]
    out = _act_in_node("_in.map((p) => actSlotSourceNotes(p))", plans)
    assert out[0] == [], f"limit 已在 deltas 里说过，不该再说一遍：{out[0]}"
    assert len(out[1]) == 1


def test_the_files_runner_does_not_put_words_in_the_users_mouth():
    """「你没有点名是哪一条」是一句本工具**无从核实**的断言：用户完全可能说了
    「前5条的文件」，而这一步照样只处理第 1 条。口径行只说本工具做了什么。"""
    body = re.search(r"async function actRunFilesShow\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body
    code = _strip_comments(body.group(1))
    assert "你没有点名" not in code
    assert "第 1 条" in code, "只处理第 1 条这件事必须写进口径行"


def test_second_order_gaps_report_the_difference_between_promised_and_actual():
    """`said == used` 但 `used ≠ 实际产物`：`candidate_uids = ordered_uids[:limit]` 里
    limit 只是**上限**。说「打包前50条」而只有 12 条时没有任何 delta，
    回执却绝不能写「已打包 50 条」。"""
    cases = [
        [50, 12, 12],   # 承诺 50、实际 12 → 必须说
        [5, 5, 5],      # 完全兑现 → 不说
        [0, 10, 10],    # 没说条数 → 不说
        [20, 20, 3],    # 条数对上了，但只有 3 条生成了下载命令 → 必须说
    ]
    out = _act_in_node("_in.map((c) => actSecondOrderGaps(c[0], c[1], c[2]))", cases)
    assert out[0] and "50" in out[0][0] and "12" in out[0][0]
    assert out[1] == []
    assert out[2] == []
    assert out[3] and "3" in out[3][0]


def test_non_default_slot_sources_are_always_spoken_out_loud():
    """五态里 `clamped` / `guessed` / `dropped` **必须**出现在回执里；`said` / `default` 折叠。"""
    plans = [
        {"slot_sources": {"limit": "said", "target": "default"}},
        {"slot_sources": {"limit": "clamped"}},
        {"slot_sources": {"limit": "guessed"}},
        {"slot_sources": {"limit": "dropped"}},
    ]
    out = _act_in_node("_in.map((p) => actSlotSourceNotes(p))", plans)
    assert out[0] == [], "said / default 不该刷屏"
    assert len(out[1]) == 1 and len(out[2]) == 1 and len(out[3]) == 1
    # 三态各说各的话，不能三句一样（那等于没说清哪里偏了）
    assert len({out[1][0], out[2][0], out[3][0]}) == 3


def test_every_frontend_source_state_has_a_sentence():
    """后端会填哪几种 `slot_sources`，前端就得有对应的话可说——少一种就是静默偏离。"""
    backend_states = {"clamped", "guessed", "dropped"}
    for state in backend_states:
        assert f'{state}:' in ACT_CORE or f'"{state}"' in ACT_CORE or f"{state}:" in ACT_CORE, state
    # 反向：前端的表不许多出后端根本不会给的状态
    table = re.search(r"const ACT_SOURCE_ZH = \{(.*?)\};", ACT_CORE, re.S)
    assert table, "找不到 ACT_SOURCE_ZH"
    assert set(re.findall(r"(\w+):", table.group(1))) == backend_states


# ---------------------------------------------------------------- confidence 不是确认门

def test_the_dispatcher_never_consults_confidence():
    """堵死旧哲学的回插口。

    评审抓到 `confidence` 是个悬空字段——没人消费的 `"low"`，后人最自然的动作就是加一句
    「是这个意思吗？」，于是「低置信度转确认」这个旧哲学最体面的马甲会从空白处长出来。
    派发函数里**一次都不许出现** confidence：它只能影响回执排版（在 actReceiptFrom 里）。
    """
    body = re.search(r"async function actDispatchPlan\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "找不到 actDispatchPlan 的函数体"
    assert "confidence" not in body.group(1), (
        "派发函数里出现了 confidence —— 低置信度必须执行同一个 verb、同一套参数"
    )
    for runner in ("actRunPackDownload", "actRunPackPreview", "actRunCiteExport",
                   "actRunReusePack", "actRunFeasibility", "actRunFilesShow"):
        run_body = re.search(r"(?:async )?function " + runner + r"\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
        assert run_body, runner
        assert "confidence" not in run_body.group(1), f"{runner} 里不该看 confidence"


# ---------------------------------------------------------------- 词表 ↔ 派发表

def test_every_exec_verb_has_a_runner_and_no_runner_is_invented():
    """封闭词表与派发表必须一一对应：多一个 = 前端能做后端没授权的事；少一个 = 静默不响应。

    例外机制只有 `FRONTEND_UNWIRED_EXEC_VERBS`（后端模块级常量，不在这里手抄）——只准放
    「有独立执行入口、前端暂未接线」的动词。curate.* 六动词已全部毕业（前四个 2026-08-01 、
    restore 同日随统一对话窗口接线、check_updates 2026-08-03 随批接线）。
    清单现只含 search.rerun（2026-08-16 检索工具化）：环内专属动词，**永久豁免**——
    前端只渲染步骤卡、刻意不写 runner（runner 直打 /api/recommend 会绕过后端机械择优闸）。"""
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table, "找不到 ACT_RUNNERS"
    wired = set(re.findall(r'"([a-z]+\.[a-z_]+)"', table.group(1)))   # search_online 带下划线，字符类必须含 _
    expected = set(AP.EXEC_VERBS) - set(AP.FRONTEND_UNWIRED_EXEC_VERBS)
    assert wired == expected, (
        f"派发表与后端封闭词表不一致：多 {wired - expected}，少 {expected - wired}"
    )


# ---------------------------------------------------------------- 接线

def test_act_is_actually_wired_to_the_screen():
    assert re.search(r"\bfunction\s+initAct\b", ACT)
    assert "initAct()" in BOOT, "boot.js 没有初始化执行层（纠错按钮会点了没反应）"
    assert 'id="cbHistory"' in HTML, "index.html 缺少对话流挂点 #cbHistory（行动流与总结都长在这里）"
    assert '$("cbHistory")' in ACT, "纠错 chips 的点击委托必须挂在对话流上（p10 起 chips 长在总结泡里）"
    assert "arxOnChange(cbRenderHistory)" in ACT, "行动流的重画必须收口到对话流"
    assert "survey" not in BOOT, "问卷弹窗已随执行侧全自动化退役（boot 不该再初始化它）"
    assert ACT.count('"/api/action/plan"') == 0, "act.js 里不该再手写一遍端点地址"
    # 统一路由（turn pipeline）后，前端不再直接调 /api/action/plan——一切规划都过 /api/utterance；
    # 该端点仅为外部/MCP 兼容保留，API 常量表里也不该再留它（留了就是第二入口的邀请）。
    assert "actionPlan:" not in CORE, "前端已不调 /api/action/plan，常量表不该留第二入口"
    for name in ("act/act.js", "panel/board.js", "search/search.js", "core/interactions.js"):
        assert "API.actionPlan" not in _read(name), f"{name} 还在直接调 /api/action/plan"
    assert "actSummary:" in CORE, "LLM 执行总结端点应集中声明在 core.js 的 API 常量里"
    assert ACT.count('"/api/act/summary"') == 0
    assert "API.actSummary" in ACT


def test_board_and_search_both_route_through_the_execution_layer():
    """统一路由（turn pipeline）下两个落地点：对话窗 tool 档直接派发、
    「先检索后派发」经 actAfterSearch 的 actPlan 档——只接一个就又变成
    「同一个人在同一个页面上换个输入框说同样的话就不 work」。"""
    assert "actDispatchPlanChain(" in BOARD, "对话窗 tool 档没有接共享执行链"
    assert "actAfterSearch(" in SEARCH, "主检索路径没有接执行层"
    assert re.search(r"\bfunction\s+actAfterSearch\b", ACT)
    assert re.search(r"\bfunction\s+actDispatchPlan\b", ACT)
    # 主检索路径两个落地点（缓存命中 + 真请求）都要有，否则「相同查询沿用上次结果」那次不执行
    assert SEARCH.count("actAfterSearch(") == 2, "runRecommend 的两个成功落地点都要接"


# ---------------------------------------------------------------- 零命中救回（sr1，检索工具化 Phase 2）

def test_landing_goes_through_one_shared_entry():
    """：runRecommend 两个落地点与救回换屏**同走 landRecommendResult**——
    条件板推帧/回填三件套只许出现在共享函数体内，不许第二调用方各抄一份
    （抄出去 = 「回到上一步」帧语义两边漂移的起点）。"""
    assert re.search(r"export function landRecommendResult\(data, query, opts\)", SEARCH)
    assert SEARCH.count("renderCondBoard(") == 1, "renderCondBoard 只许在共享落地函数里调"
    assert SEARCH.count("cbPushCurrent(") == 1, "cbPushCurrent 只许在共享落地函数里推帧"
    assert SEARCH.count("landRecommendResult(cached") == 1, "缓存落地点没走共享入口"
    assert SEARCH.count("landRecommendResult(data, query, { noScroll") == 1, "真请求落地点没走共享入口"
    # 救回换屏路径已退役（救回不再发 /api/agent/search-rescue），共享入口只剩缓存 + 真请求
    # 两处调用。推帧仍在（preliminary 先行帧与 final a 档换屏按住进度泡不蜕变）。
    #（2026-08-16 初步结果先行）：推帧加第三参 popts.keepProgress（preliminary 先行帧
    # 与 final a 档换屏按住进度泡不蜕变）——钉字刻意更新为三参形态。
    land = re.search(r"export function landRecommendResult\([^)]*\)\s*\{(.*?)\n\}", SEARCH, re.S)
    assert land and "cbPushCurrent(data, query, { keepProgress:" in land.group(1), (
        "共享入口推帧必须带 keepProgress 透传（preliminary/final a 档按住进度泡）")


def test_auto_rescue_is_retired_from_search():
    """：自动救回链退役——不再有 maybeSearchRescue / handleSearchRescue、不再自动调
    /api/agent/search-rescue、不再自动产「没有命中，我试着换个说法再查一次…」sys 气泡。
    救回选项改由 board.js 选择条呈现（零命中 pill「点击处理」）。"""
    code = _strip_comments(SEARCH)
    assert "maybeSearchRescue" not in code, "自动救回链应已退役"
    assert "handleSearchRescue" not in code
    assert "_rescueSeq" not in code and "_rescuedKeys" not in code
    assert "searchRescue" not in code, "前端不再自动调 /api/agent/search-rescue"
    assert "没有命中，我试着换个说法再查一次" not in code


def test_relax_card_auto_popup_is_wired_in_board():
    """：零命中自动弹卡接线钉（2026-09-14 收编）——零命中 pill 渲染「点击处理」+
    视觉区分类（ft-pill--zero）+ data-ft-zero 不变；最新活跃 pill 零命中 →
    maybeAutoRelaxCard 兜底弹 ask.relax 选择卡（环内优先不抢 + _relaxDismissed 按批记忆）；
    作答走 cbChoose askrelax 分支 → cbCommit suppressed_constraints 零 LLM 重搜。"""
    assert "ft-pill--zero" in BOARD
    assert "点击处理" in BOARD
    assert "data-ft-zero" in BOARD
    for fn in ("maybeAutoRelaxCard", "_autoRelaxCardOpen", "askRelaxShow"):
        assert re.search(rf"function {fn}\(", BOARD), f"board.js 缺自动弹卡函数 {fn}"
    assert "_relaxDismissed" in BOARD, "自动卡 dismiss 须按 batchId 记忆（同批不再自动弹）"
    assert 'data-cb-choice="askrelax:' in BOARD, "选择卡选项没走 cbChoose askrelax 通道"
    assert "suppressed_constraints: (opt.suppress || []).slice()" in BOARD, \
        "作答执行没走 cbCommit suppressed_constraints 零 LLM 重搜"
    # legacy 单批零命中（纯 /api/recommend 路径，AI 关走这条）补网：包成单批视图，零命中 pill
    # 才存在——自动弹卡与 pill「点击处理」重开都锚在它上（e2e _relax1_check 抓到过无 pill 不弹卡）。
    wrap = re.search(r"data\.result_batches = \[\{ batch_id: _zid.*?\}\];", BOARD, re.S)
    assert wrap, "legacy 零命中未包装成单批视图（零命中 pill 无从存在，自动弹卡不触发）"
    assert 'data.active_batch = _zid' in BOARD
    assert '"fz" + frame.id' in BOARD, "批 id 须带帧号——dismiss 记忆按批 id 记，固定 id 会跨查询互相吞没"
    assert "const _zpayload = Object.assign({}, data);" in BOARD, \
        "payload 必须浅拷贝——直引 data 会让 LAST_RECOMMEND_DATA 自引用成环（JSON 序列化即炸）"
    assert "flowSetPills(_flowPillsFrom(data.result_batches, _zid, false))" in BOARD


def test_relax_card_derivation_lives_in_batch_select():
    """：零命中放松纯逻辑集中在 batch_select.js——isZeroHitBatch（payload.results 空数组）、
    deriveRelaxCard（relaxation_options → ask.relax 卡 payload，suppress 原样透传整组替换语义）、
    latestActiveBatchId（最后一个回执 entry 的活跃批）。board 从它取用，不各抄一份。
    「点击处理」只在零命中批是最新结果（最后回执 entry 的活跃批）时出现。"""
    BS = _read("core/batch_select.js")
    for fn in ("isZeroHitBatch", "deriveRelaxCard", "latestActiveBatchId"):
        assert re.search(rf"export function {fn}\(", BS), f"batch_select.js 缺导出 {fn}"
    # 渲染 & 点击都以「最后一个回执 entry 的活跃批」判定（数据-ft-zero-latest）
    assert "latestActiveBatchId(_cbLog)" in BOARD, "渲染侧没按最新活跃批判定「点击处理」"
    assert 'data-ft-zero-latest="1"' in BOARD


def test_honest_receipt_bubble_is_the_only_bubble_on_zerohit():
    """唯一气泡原则：零命中时诚实回执（disclosure_zh）保留为唯一气泡；救回选项不进对话流。"""
    apply_fn = _strip_comments(_fn_body(BOARD, "function _applyBatchDecision(text, reply, decision, opts)"))
    assert "_ba.disclosure_zh" in apply_fn, "换屏留痕没读活跃批随行的 disclosure_zh"
    assert "深入思考后找到了更匹配的结果，已更新。" in apply_fn, "无披露句时的回退通用句被删了"


def test_plansteps_wiring_is_kept_for_trace_summary():
    """：planSteps 透传不是救回专用的——它是信息流轨迹摘要「执行了 N 次检索」
    的数据源。ubDispatch → runRecommend → landRecommendResult 的 planSteps 链路仍须完整。"""
    land = re.search(r"export function landRecommendResult\([^)]*\)\s*\{(.*?)\n\}", SEARCH, re.S)
    assert land and "opts.planSteps" in land.group(1), "共享落地入口没消费 opts.planSteps"
    code = _strip_comments(SEARCH)
    assert code.count("planSteps: opts.planSteps") == 2, (
        f"runRecommend 两个落地点都要透传 planSteps，当前 {code.count('planSteps: opts.planSteps')} 处")
    sub = _strip_comments(_fn_body(BOARD, "function ubDispatch(text, reply, fromChat, wasChat)"))
    assert sub.count("planSteps: _planSteps") == 4, (
        f"ubDispatch search a/c 两档与 tool 档落地都要透传 planSteps，当前 {sub.count('planSteps: _planSteps')} 处")


def test_adopt_swap_sys_line_prefers_batch_disclosure():
    """2026-08-18 rescue2 披露句移植进环（评估迁移步骤 2 的前端消费侧）：换屏的 sys 留痕
    优先读活跃批随行的 disclosure_zh（确定性披露：哪些词没被理解/被丢弃），批上无该键时
    回退既有通用句——回退句不许删（无披露句的采纳批仍要有留痕）。披露读取
    与「已更新」词句一并收进共享应用函数 _applyBatchDecision（search a 档与 route=tool 档共用）。"""
    apply_fn = _strip_comments(_fn_body(BOARD, "function _applyBatchDecision(text, reply, decision, opts)"))
    assert "_ba.disclosure_zh" in apply_fn, "换屏留痕没读活跃批随行的 disclosure_zh"
    assert "深入思考后找到了更匹配的结果，已更新。" in apply_fn, "无披露句时的回退通用句被删了"
    # （设计约定）：两档共用 selectDisplayBatch——「严格更高级才自动换屏/去重/备选」统一真源。
    sub = _strip_comments(_fn_body(BOARD, "function ubDispatch(text, reply, fromChat, wasChat)"))
    assert sub.count("selectDisplayBatch(") == 2, f"search a 档与 route=tool 档都要调 selectDisplayBatch，当前 {sub.count('selectDisplayBatch(')} 处"


def test_search_rerun_step_card_is_summary_only_and_never_a_runner():
    """search_rerun 步骤卡：只出摘要三要素（改写词 + 采纳/拒绝 + n_before→n_after）
    动作链恒 replace_screen=false——卡片措辞绝不暗示换屏；前端**刻意无 runner**
    （runner 直打 /api/recommend 会绕过后端机械择优闸，豁免理由在 action_plan.py 常量注释）。"""
    assert 's.card_kind === "search_rerun"' in ACT
    assert re.search(r"function actSearchRerunCardHtml\(", ACT)
    card = re.search(r"function actSearchRerunCardHtml\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert card, "找不到 actSearchRerunCardHtml"
    body = card.group(1)
    for key in ("n_before", "n_after", "adopted", "reason"):
        assert key in body, f"步骤卡漏读 result.{key}"
    assert "不换屏" in body, "链内步骤卡必须写明不换屏（replace_screen 恒 false）"
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table and "search.rerun" not in table.group(1), "search.rerun 不许有前端 runner（择优闸在后端）"
    assert "searchRescue" not in ACT, "救回端点的唯一调用方是 search.js（act.js 不该有第二入口）"


# ---------------------------------------------------------------- 环内四工具专项卡
#
# 图内已执行通道（plan.steps 非空）里 compare/cite_export/compat_find/fair_check 四种 card_kind
# 渲染专项卡（HTML 随 actFinish 的 html 通道进 .cbh-sys-extra；entry.html 是重画真源）。契约点：
#   · actLoopStepCardHtml 必须有四种 card_kind 分发；构造函数读各自 result 的核心字段；
#   · 一卡一工具、默认核心信息、明细收 <details>；degraded=true 只上诚实降级句、不渲染空表格；
#   · cite 卡下载链到 core.js API 集中声明的 citationsDownload（act.js 不手写端点地址）；
#   · 历史重画随 entry.html 恢复（卡片在 html 里、不在 DOM 补丁里）。

FOUR_TOOL_RESULTS = {
    "compare": {
        "a": {"dataset_name": "A Lung", "dataset_uid": "uid-a"},
        "b": {"dataset_name": "B Lung", "dataset_uid": "uid-b"},
        "assumption_zh": "未指定对比对象，默认取当前结果的前两条进行对比。",
        "fields": [
            {"field": "species", "label_zh": "物种", "a": "Human", "b": "Human", "status": "same"},
            {"field": "chemistry", "label_zh": "化学", "a": "10x", "b": "Visium", "status": "different"},
        ],
        "n_same": 1, "n_diff": 1, "n_unknown": 0, "identical": False,
        "comparison_zh": "两个数据集在物种上相同，在化学上不同。",
        "wording_source": "deterministic", "degraded": False, "degrade_reason": "",
        "caveat_zh": "对比只覆盖本目录收录的元数据字段。",
    },
    "cite_export": {
        "n_datasets": 2, "uids": ["uid-a", "uid-b"],
        "files": [
            {"filename": "reused-public-datasets-20260819-101010.ris", "format": "ris", "bytes": 1234},
            {"filename": "reused-public-datasets-20260819-101010.bib", "format": "bibtex", "bytes": 567},
        ],
        "out_dir": r"C:\agent\.userdata\citations",
        "note_zh": "已导出 2 个数据集的引文，RIS 与 BibTeX 两种格式都已落盘。",
    },
    "compat_find": {
        "seed": {"dataset_name": "Seed Lung", "dataset_uid": "seed-1"},
        "criteria": {"species": ["Human"], "chemistry": "10x Chromium", "platform_family": "10x"},
        "total": 2,
        "compatible": [
            {"dataset_name": "C Lung", "dataset_uid": "uid-c", "_compat_basis": "chemistry=10x Chromium"},
            {"dataset_name": "D Lung", "dataset_uid": "uid-d"},
        ],
        "caveat": "元数据兼容是可整合的必要非充分条件，不代表可整合。",
        "note_zh": "已按「Seed Lung」的元数据找到 2 个兼容数据集。",
        "degraded": False, "degrade_reason": "",
    },
    "fair_check": {
        "dataset_name": "A Lung", "source": "10x",
        "fair": {
            "checks": [
                {"principle": "F", "id": "F1", "label": "持久标识符", "status": "pass", "evidence": "accession", "action": ""},
                {"principle": "F", "id": "F2", "label": "描述性元数据", "status": "partial", "evidence": "2/4", "action": "去核实"},
                {"principle": "A", "id": "A1", "label": "可获取链接", "status": "unknown", "evidence": "", "action": "去确认"},
            ],
            "summary": {"pass": 1, "partial": 1, "unknown": 1, "total": 3, "readiness_pct": 50,
                        "statement": "3 项复用就绪度检查：1 项充分、1 项部分、1 项未知"},
            "gaps": [],
        },
        "data_availability": {},
        "note_zh": "「A Lung」的 FAIR 复用就绪度：50%（1 项充分 / 1 项部分 / 1 项未知）——这是复用者视角的就绪度自检，不是官方 FAIR 认证。",
        "degraded": False, "degrade_reason": "",
    },
}


def test_four_tool_step_cards_are_wired_and_read_their_result_fields():
    """：actLoopStepCardHtml 必须有四种 card_kind 分发；四个构造函数存在且读各自 result 的
    核心字段；cite 卡下载链走 core.js API 集中声明的 citationsDownload（act.js 不手写端点）。"""
    for kind in ("compare", "cite_export", "compat_find", "fair_check"):
        assert f's.card_kind === "{kind}"' in ACT, f"actLoopStepCardHtml 缺 {kind} 分发"
    for name, keys in (
        ("actCompareCardHtml", ("comparison_zh", "n_same", "n_diff", "n_unknown", "fields", "caveat_zh", "degraded")),
        ("actCiteExportCardHtml", ("note_zh", "files", "uids", "tpBytes", "citationsDownload")),
        ("actCompatFindCardHtml", ("note_zh", "seed", "criteria", "compatible", "total", "caveat", "degraded")),
        ("actFairCheckCardHtml", ("readiness_pct", "pass", "partial", "unknown", "checks", "note_zh", "degraded")),
    ):
        body = re.search(r"function " + name + r"\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
        assert body, f"找不到 {name}"
        code = _strip_comments(body.group(1))
        for k in keys:
            assert k in code, f"{name} 漏读 result.{k}"
    assert '"/api/citations/download"' not in _strip_comments(ACT), (
        "端点地址只许在 core.js 的 API 集中声明（act.js 不手写）"
    )
    assert "citationsDownload:" in CORE and '"/api/citations/download"' in CORE


def test_compare_card_renders_counts_details_and_degrade_sentence():
    """真行为：compare 卡成功档含结论/计数条/字段明细/边界句；degraded 只上诚实降级句、
    不渲染计数条与空表格。"""
    out = _card_in_node(ACT, "actCompareCardHtml", "actCompareCardHtml(_in)", FOUR_TOOL_RESULTS["compare"])
    assert "数据集对比" in out and "↔" in out
    assert "两个数据集在物种上相同，在化学上不同。" in out
    assert "1 项相同" in out and "1 项不同" in out and "0 项未标注" in out
    assert '<details class="arx-card-details">' in out and "字段明细（2 项）" in out
    assert "物种" in out and "Human ↔ Human" in out
    assert "对比只覆盖本目录收录的元数据字段" in out
    degraded = dict(FOUR_TOOL_RESULTS["compare"], degraded=True,
                    comparison_zh="当前没有可对比的检索结果（也没有指定数据集编号/名称），无法对比。",
                    n_same=0, n_diff=0, n_unknown=0, fields=[], assumption_zh="", caveat_zh="")
    out2 = _card_in_node(ACT, "actCompareCardHtml", "actCompareCardHtml(_in)", degraded)
    assert "当前没有可对比的检索结果" in out2
    assert "项相同" not in out2 and "arx-cmp-bar" not in out2 and "字段明细" not in out2


def test_cite_export_card_has_download_links_and_uid_details():
    """真行为：cite.export 卡逐文件给「下载」链到 /api/citations/download?f=<裸文件名>、
    字节走 tpBytes；明细收 <details> 列 uids；无产物档只上诚实句。"""
    out = _card_in_node(ACT, "actCiteExportCardHtml", "actCiteExportCardHtml(_in)", FOUR_TOOL_RESULTS["cite_export"])
    assert "引文导出 · 2 个数据集" in out
    assert "已导出 2 个数据集的引文" in out
    assert out.count("/api/citations/download?f=") == 2, "每份文件一个下载链接"
    assert "reused-public-datasets-20260819-101010.ris" in out
    assert ">下载</a>" in out and "download" in out
    assert "1.2 KB" in out, "字节数必须走 tpBytes 格式化（1234 → 1.2 KB）"
    assert '<details class="arx-card-details">' in out and "导出对象（2 个编号）" in out
    assert "uid-a" in out and "uid-b" in out
    empty = {"n_datasets": 0, "uids": [], "files": [], "out_dir": "",
             "note_zh": "当前没有可导出的检索结果，没有生成引文文件。"}
    out2 = _card_in_node(ACT, "actCiteExportCardHtml", "actCiteExportCardHtml(_in)", empty)
    assert "当前没有可导出的检索结果" in out2
    assert ">下载</a>" not in out2 and "导出对象" not in out2


def test_compat_find_card_renders_seed_criteria_compatibles_and_caveat():
    """真行为：compat.find 卡含 note_zh/种子摘要/兼容判据/兼容列表（名称+uid+basis）/总数/caveat；
    降级档只上诚实降级句 + 恒带 caveat。"""
    out = _card_in_node(ACT, "actCompatFindCardHtml", "actCompatFindCardHtml(_in)", FOUR_TOOL_RESULTS["compat_find"])
    assert "元数据兼容 · 2 个兼容数据集" in out
    assert "已按「Seed Lung」的元数据找到 2 个兼容数据集" in out
    assert "种子数据集" in out and "Seed Lung" in out
    assert "兼容判据" in out and "Human" in out and "chemistry 10x Chromium" in out
    assert "C Lung" in out and "uid-c" in out and "chemistry=10x Chromium" in out
    assert "D Lung" in out and "uid-d" in out
    assert "元数据兼容是可整合的必要非充分条件" in out
    degraded = dict(FOUR_TOOL_RESULTS["compat_find"], degraded=True,
                    note_zh="「ghost」在当前库中找不到，没有可找兼容同伴的对象。",
                    seed={}, criteria={}, total=0, compatible=[])
    out2 = _card_in_node(ACT, "actCompatFindCardHtml", "actCompatFindCardHtml(_in)", degraded)
    assert "在当前库中找不到" in out2
    assert "0 个兼容数据集" not in out2 and "没有找到兼容数据集" not in out2
    assert "元数据兼容是可整合的必要非充分条件" in out2, "caveat 恒带（诚实边界不许丢）"


def test_fair_check_card_renders_readiness_counts_and_details():
    """真行为：fair.check 卡含 readiness_pct 醒目大字 + pass/partial/unknown 计数 + <details>
    逐项明细（id+label+状态+证据）+ 边界句；降级档只上诚实降级句。"""
    out = _card_in_node(ACT, "actFairCheckCardHtml", "actFairCheckCardHtml(_in)", FOUR_TOOL_RESULTS["fair_check"])
    assert "FAIR 自检 · A Lung" in out
    assert '<div class="arx-fair-pct">50<span class="arx-fair-pct-unit">%</span></div>' in out
    assert "1 项充分 · 1 项部分 · 1 项未知" in out
    assert '<details class="arx-card-details">' in out and "逐项明细（3 项）" in out
    assert "F1 持久标识符" in out and "充分" in out
    assert "F2 描述性元数据" in out and "部分" in out
    assert "A1 可获取链接" in out and "未知" in out
    assert "复用者视角的就绪度自检，不是官方 FAIR 认证" in out, "边界句不许丢（诚实层）"
    degraded = dict(FOUR_TOOL_RESULTS["fair_check"], degraded=True,
                    note_zh="「ghost」在当前库中找不到，无法做 FAIR 自检。", fair={})
    out2 = _card_in_node(ACT, "actFairCheckCardHtml", "actFairCheckCardHtml(_in)", degraded)
    assert "在当前库中找不到" in out2
    assert "arx-fair-pct" not in out2 and "逐项明细" not in out2 and "项充分" not in out2


def test_act_finish_passes_four_tool_cards_through_html_channel():
    """：actDispatchPlan 图内通道收集四工具卡（按执行顺序拼接）交给 actFinish 的 html 通道
    （.cbh-sys-extra，entry.html 是重画真源 → 历史重画随 html 一起恢复）；非四工具步不上卡
    （精简不回流）；actSummaryHtml 的精简契约保持不动。"""
    disp = _strip_comments(_fn_body(ACT, "export async function actDispatchPlan(plan, said)"))
    assert "FOUR_TOOL_CARD_KINDS" in disp and "actLoopStepCardHtml(s)" in disp
    assert "cardsHtml: cardsHtml" in disp, "图内通道必须把卡片交给 actFinish"
    finish = _strip_comments(_fn_body(ACT, "function actFinish(plan, outcome, said, opts)"))
    assert "opts.cardsHtml" in finish and 'cbLogPush("sys", factual' in finish
    assert "execSummary" not in finish, "执行摘要通道已退役（摘要归信息流压缩行）"
    assert "cbExecReceiptCovered" in finish and "planIsRetrievalOnly" in finish, (
        "唯一气泡规则：纯检索计划且批次回执已接管时 actFinish 必须抑制第二颗气泡"
        "（纯检索判定锚点：board_core.planIsRetrievalOnly）"
    )
    assert "detailLines:" not in finish, "明细折叠区不得回流"
    summ = _strip_comments(_fn_body(ACT, "function actSummaryHtml(opts)"))
    assert '<details class="arx-trace">' not in summ, "执行过程折叠区不得回流"


def test_act_js_imports_tp_bytes_from_act_core():
    """ 根因回归门：actCiteExportCardHtml 在 files 非空时调用 tpBytes（861 行），但 act.js
    曾漏掉从 #act_core 的 tpBytes import——浏览器里 ReferenceError，被 ubDispatchAction 的
    `.catch` 静默吞掉（pending=null 时 cbMarkMessageAsAction 是 no-op），组合轮与纯工具轮的
    cite 卡全部静默丢失（行动流留在屏上、无总结、无卡片）。node 桩（_card_in_node 曾手桩
    tpBytes）会掩盖这个缺失，所以这里钉死 import 行本身 + 不许本地重定义（单一真源在 act_core）。"""
    imports = re.search(r'import\s*\{[^}]*\}\s*from\s*"#act_core"', ACT)
    assert imports, "act.js 必须从 #act_core 导入"
    assert "tpBytes" in imports.group(0), "act.js 的 #act_core import 必须含 tpBytes（cite 卡在浏览器里的唯一来源）"
    assert re.search(r"(export\s+)?function\s+tpBytes\s*\(", _strip_comments(ACT)) is None, \
        "tpBytes 不许在 act.js 里重定义（格式化单一真源是 act_core.js）"


def test_combo_turn_step_sequence_dispatches_cite_card():
    """ 真行为：组合轮 plan.steps=[rank, cite_export] 经 actDispatchPlan 图内通道的卡片
    收集逻辑（actLoopStepCardHtml 按执行顺序分发）产出 cite 卡 + 每个文件一个下载链；
    rank 步非四工具不上专项卡（精简兜底句）。这是「检索 + 导出 BibTeX」组合轮真正派发
    时收集行为的真身——此前在浏览器里会因 tpBytes 缺失在 actCiteExportCardHtml 处抛错。"""
    rank_step = {"verb": "rank", "verb_zh": "检索数据集", "ok": True, "card_kind": "rank",
                 "readonly": True, "result": {"query": "x", "total": 114, "filters": []}}
    cite_step = {"verb": "cite.export", "verb_zh": "导出引文", "ok": True,
                 "card_kind": "cite_export", "readonly": False, "result": FOUR_TOOL_RESULTS["cite_export"]}
    out = _card_in_node(ACT, "actLoopStepCardHtml",
                        "_in.map(s => actLoopStepCardHtml(s)).join('')", [rank_step, cite_step],
                        deps=["actCompareCardHtml", "actCiteExportCardHtml",
                              "actCompatFindCardHtml", "actFairCheckCardHtml"])
    assert "引文导出 · 2 个数据集" in out, "cite 步必须产出专项卡（组合轮 cardsHtml 非空）"
    assert out.count("/api/citations/download?f=") == 2, "每个文件一个下载链"
    assert ">下载</a>" in out and "1.2 KB" in out, "字节数走 tpBytes（1234 → 1.2 KB）"
    assert "导出对象（2 个编号）" in out and "uid-a" in out and "uid-b" in out
    for other in ("数据集对比", "FAIR 自检", "元数据兼容"):
        assert other not in out, f"组合轮只出本轮的卡，不该混入 {other}"
    assert "这一步已经跑完，没有需要展示的内容" in out, "rank 步走精简兜底（不上专项卡）"


def test_rollback_step_receipt_uses_real_result_not_generic_write_claim():
    """2026-08-18 真行为钉：拒绝不谎称改库；成功按实际恢复/失败文件数播报。"""
    payload = {
        "refused": {"card_kind": "rollback", "ok": True, "readonly": False,
                    "result": {"rolled_back": False, "note_zh": "本轮没有可回滚的写操作。",
                               "recycled": [], "restored": [], "unrestorable": [], "errors": []}},
        "done": {"card_kind": "rollback", "ok": True, "readonly": False,
                 "result": {"rolled_back": True, "note_zh": "已回滚。",
                            "recycled": ["a.json", "b.json"], "restored": ["c.json"],
                            "unrestorable": [{"name": "d.json"}], "errors": []}},
    }
    out = _browser_helper_in_node(
        ACT, "actRollbackPolicyLine",
        "[actRollbackPolicyLine(_in.refused), actRollbackPolicyLine(_in.done)]", payload)
    assert out[0] == "本轮没有可回滚的写操作。"
    assert "改动了本地库" not in out[0]
    assert "3 个文件" in out[1] and "1 项未能恢复" in out[1]
    assert 's.card_kind === "rollback"' in ACT
    assert "这一步已经跑完，没有需要展示的内容。" in ACT, "上一批空回执措辞不得回退"


def test_batch_pill_texts_are_unique_for_every_collision_shape():
    """2026-08-18 真行为钉：跨 kind、同 kind、20 字截断撞名后，pill 文案仍两两唯一。"""
    batches = [
        {"kind": "preliminary", "label": "同一句", "payload": {}},
        {"kind": "rank", "label": "同一句", "payload": {}},
        {"kind": "rerank", "label": "同一句", "payload": {}},
        {"kind": "search_rerun", "label": "同一句", "payload": {}},
        {"kind": "rank", "label": "前二十字完全一样的查询标签", "payload": {}},
        {"kind": "rank", "label": "前二十字完全一样的查询标签", "payload": {}},
        {"kind": "rank", "label": "唯一标签", "payload": {}},
    ]
    texts = _browser_helper_in_node(RESULTS, "_batchPillTexts", "_batchPillTexts(_in)", batches)
    assert len(texts) == len(set(texts)), texts
    assert texts[:4] == ["初步·同一句", "新检索·同一句", "改写后重检·同一句", "换词重检·同一句"]
    assert texts[4] == "新检索·前二十字完全一样的查询标签"
    assert texts[5].endswith("·2")
    assert texts[6] == "唯一标签", "不撞名时必须一字不动"


def test_auto_execution_only_fires_on_a_hand_typed_submit():
    """自动执行只能由「用户亲手提交」触发。统一对话窗口后，两条亲手提交路径
    （回车 / 检索按钮）都走 ubSubmit → /api/utterance 路由；执行诉求只在两档落地：
    tool+有结果 直接派发、tool+没结果 先检索再经 actAfterSearch 的 actPlan 档派发。
    分面芯片、一键放宽、撤销/重做、从左侧历史重跑都会走 runRecommend——
    在那些路径上自动执行 = 用户点一下芯片就又下一个 zip。"""
    body = re.search(r"function actAfterSearch\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body and "actPlan" in body.group(1), (
        "actAfterSearch 必须只认 opts.actPlan（统一框先检索后派发）"
    )
    assert "userSubmit" not in body.group(1), "userSubmit 旧档已随统一路由退役"
    interactions = _read("core/interactions.js")
    assert "userSubmit" not in interactions, "统一框不再有绕过路由的 userSubmit 提交"
    assert interactions.count("ubSubmit()") == 2, "回车 / 检索按钮两条亲手提交路径都要走 ubSubmit"
    # 其余 runRecommend 调用点一律不得携带 userSubmit / actPlan
    for name in ("search/facets.js", "search/results.js", "search/browse.js", "panel/board.js"):
        assert "userSubmit" not in _read(name), f"{name} 里不该出现 userSubmit"


def test_the_route_llm_follows_api_capability_not_a_master_switch():
    """2026-08-03 设置三维度化：大模型总开关退役，`getConfig().use_llm` 由
    **API 可用性**（llmCapable 单一判据：已配 key 必可用；mock 演示恒 true）门控，
    AI 润色是独立开关 `cfgPolish`（getConfig 另发 `polish` 字段）。
    统一路由（ubRouteBody）读它——API 不可用时路由走规则兜底/降级气泡，如实回音，
    而不是假装听懂或静默调 LLM。"""
    body = re.search(r"function ubRouteBody\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubRouteBody"
    code = _strip_comments(body.group(1))
    assert "use_llm: cfg.use_llm" in code, "路由 LLM 必须跟 API 可用性走"
    assert 'id="cfgPolish"' in HTML and "polish:" in SHELL, "润色必须是独立开关"
    assert "llmCapable" in SHELL, "API 可用性判据必须存在（大模型总开关已退役）"
    assert 'id="cfgLlm"' not in HTML, "大模型总开关已退役（API 门控取代之）"


def test_the_merged_agent_exec_switch_is_a_first_class_setting():
    """「AI 执行」（维度，2026-08-03 合并旧「说了就直接做」+「Agent 规划执行」）：
    设置面板里的一等开关（与润色平级、不埋进 #rerankDetail），可持久化，
    act.js 的 actEnabled 与 getConfig 的 auto_act/agent 都读它。"""
    assert 'id="nodeAgentExec"' in HTML and 'id="cfgAgentExec"' in HTML
    detail = re.search(r'id="rerankDetail">(.*?)id="strategyDetail"', HTML, re.S)
    assert detail and "nodeAgentExec" not in detail.group(1), "#nodeAgentExec 不该埋在 #rerankDetail 里"
    assert 'id="nodeAutoAct"' not in HTML and 'id="cfgAutoAct"' not in HTML, "旧开关必须删干净"
    assert 'id="nodeAgent"' not in HTML and 'id="cfgAgent"' not in HTML, "旧开关必须删干净"
    shell = _strip_comments(SHELL)
    assert "agentExec" in shell, "开关必须能持久化（save / load 两处）"
    act = _strip_comments(ACT)
    assert "cfgAgentExec" in act, "actEnabled 必须读合并后的 AI 执行开关"


def test_no_typeof_function_guard_around_the_execution_layer():
    """`typeof x === "function"` 守卫会把「函数名打错」变成永久静默短路，
    本仓库栽过多次（最近一次是 `selectedSources()` 恒 undefined）。这里不许再来一次。"""
    code = _strip_comments(BOARD)
    assert 'typeof actMaybeAutoAct === "function"' not in code
    assert "typeof actEnabled" not in code


def test_receipt_text_carries_no_markdown_emphasis():
    """前端按纯文本转义呈现，写了星号就会原样显示成两个星号。"""
    for text in re.findall(r'"([^"\\\n]{4,})"', _strip_comments(ACT)):
        if any("一" <= ch <= "鿿" for ch in text):
            assert "**" not in text, f"面向用户的中文里有 markdown 强调：{text}"
            assert "`" not in text, f"面向用户的中文里有反引号：{text}"


def test_the_receipt_never_offers_an_undo_for_a_file_already_on_disk():
    """已经落到下载目录的文件，本工具删不掉。给一颗「撤销」按钮就是骗人。"""
    chips = re.search(r"function actFixChips\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert chips
    assert "撤销" not in _strip_comments(chips.group(1))
    assert "本工具删不掉" in ACT_CORE, "落盘动作必须明说这一点（act_core 的 onDisk 行）"


def test_undo_chip_is_offered_only_for_curate_write_verbs():
    """2026-08-04 用户：最近一次执行直接给「撤回这次执行」钮。
    只给 curate 系写动词（文件粒度互逆：import/search_online↔remove、remove↔restore）；
    落盘产物（pack/cite/reuse，见上门）与只读动词坚决不给。"""
    assert "actFixChips(plan, said, outcome)" in ACT, "chips 需要 outcome 才能解析撤回对象"
    assert 'data-act-fix="undo"' in ACT and "撤回这次执行" in ACT
    spec = re.search(r"function actUndoSpec\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert spec, "找不到 actUndoSpec（撤回对象解析）"
    body = spec.group(1)
    for v in ('verb === "curate.import"', 'verb === "curate.search_online"', 'verb === "curate.remove"', 'verb === "curate.restore"'):
        assert v in body, f"撤回逆映射缺 {v}"
    assert "outcome.cancelled" in body, "取消态不许给撤回钮（什么都没发生，没有可撤的）"
    # 「只留最近一次可撤」：新执行落地摘旧钮；成功后改 entry.html（重画真源）原位换「已撤回」注记
    assert "actStripUndoChip" in ACT and "_actUndoEntry" in ACT and "actUndoRun" in ACT
    # remove/restore 两个 runner 必须把撤回定位键透传进 artifact（recycle_name / restored_name）
    remove_fn = re.search(r"function actRunCurateRemove\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert remove_fn and "recycle_name" in remove_fn.group(1), "remove 的撤回键（recycle_name）没进 artifact"
    restore_fn = re.search(r"function actRunCurateRestore\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert restore_fn and "restored_name" in restore_fn.group(1), "restore 的撤回键（restored_name）没进 artifact"


def test_research_and_off_fix_chips_retired_pack1():
    """2026-08-16 pack1：「按原话重新检索」「以后别自动执行」两颗 chip 退役（agent 能力已足够）。
    断言**不存在**防回退；panel / undo / 婉拒候选 chip 保留；退役分支的清尾（runRecommend /
    syncAiGates 不再被 act.js 引用）一并钉住。"""
    code = _strip_comments(ACT)
    assert 'data-act-fix="research"' not in code, "research chip 应已删除"
    assert 'data-act-fix="off"' not in code, "off chip 应已删除"
    assert "按原话重新检索" not in code and "以后别自动执行" not in code
    assert 'what === "research"' not in code and 'what === "off"' not in code, "点击分支应随 chip 一并删除"
    assert "runRecommend" not in code, "research 退役后 act.js 不再用 runRecommend"
    assert "syncAiGates" not in code, "off 退役后 act.js 不再用 syncAiGates"
    # 保留面：panel（打开下载面板自己挑）/ undo（撤回这次执行）/ 婉拒候选（data-act-say）
    assert 'data-act-fix="panel"' in code and "打开下载面板自己挑" in code
    assert 'data-act-fix="undo"' in code
    assert "data-act-say" in code, "curate 失败的婉拒候选 chip 保留"


def test_task_pack_preview_no_longer_auto_opens_panel_pack1():
    """2026-08-16 pack1：预览不再自动打开面板——previewTaskPack / renderTaskPackPlan 体内
    没有 panel.hidden=false（显式开面板的调用方自己 unhide；pack.download 保持关闭，
    用户手动关面板后迟到的 fetch 也不再重开——vs3 在册 race 随本批消掉）。"""
    tp = _read("act/task_pack.js")
    prev = re.search(r"export async function previewTaskPack\([^)]*\)\s*\{(.*?)\n\}", tp, re.S)
    assert prev, "找不到 previewTaskPack"
    assert "panel.hidden = false" not in _strip_comments(prev.group(1)), "previewTaskPack 不得自动开面板"
    render = re.search(r"function renderTaskPackPlan\([^)]*\)\s*\{(.*?)\n\}", tp, re.S)
    assert render, "找不到 renderTaskPackPlan"
    assert "panel.hidden = false" not in _strip_comments(render.group(1)), "renderTaskPackPlan 不得接管显隐"
    # actRunPackPreview 按 verb 分流：pack.preview 履约开面板；pack.download 保持关闭、不 scroll
    run = re.search(r"async function actRunPackPreview\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert run, "找不到 actRunPackPreview"
    body = run.group(1)
    assert '"pack.download"' in body and "forDownload" in body, "pack.preview/download 必须分流"
    assert "清单面板已在结果区展开" in body, "pack.preview 的开面板口径保留"


def test_preview_and_build_return_values_are_actually_consumed():
    """P0 把 previewTaskPack / buildTaskPack 改成有返回值，就是为了这一刻。
    这里钉住「真的用了返回值」——不然回执又会退回「只能照成功渲染」。"""
    for symbol in ("pre.ok", "built.ok", "built.artifact", "out.ok"):
        assert symbol in ACT, f"act.js 没有消费 {symbol}"
    assert "outcome.artifact" in ACT_CORE


# ---------------------------------------------------------------- 管护（curate.*）接线
#
# 静态口径与 web_smoke 相同（不执行 JS）：派发表、端点集中声明、token 原样回传、
# 失败接管文案关键串；另配一条 node 真行为门钉「待确认回执写不出『已』字」。

CURATE_VERBS = ("curate.list", "curate.import", "curate.search_online", "curate.remove",
                "curate.restore", "curate.check_updates", "curate.db_status")


def test_curate_verbs_are_wired_to_named_runners():
    """六个管护动词必须在派发表里各有一个**真实存在**的 runner 函数
    （派发表闸只数动词名，runner 拼错名字它照样绿）。"""
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table, "找不到 ACT_RUNNERS"
    for verb in CURATE_VERBS:
        m = re.search(r'"' + re.escape(verb) + r'":\s*(\w+)', table.group(1))
        assert m, f"{verb} 不在派发表里"
        assert re.search(r"(?:async )?function " + m.group(1) + r"\(", ACT), f"runner {m.group(1)} 不存在"


def test_curate_endpoints_live_in_the_core_api_dict():
    """端点地址集中声明在 core.js 的 API 常量里；act.js 不许再手写一遍。"""
    assert "curatePlan:" in CORE and "curateApply:" in CORE
    assert '"/api/curate/plan"' in CORE and '"/api/curate/apply"' in CORE
    assert ACT.count('"/api/curate/plan"') == 0 and ACT.count('"/api/curate/apply"') == 0
    assert "API.curatePlan" in ACT and "API.curateApply" in ACT
    # check_updates（2026-08-03 只读无 token）同纪律：地址只在 core.js，act.js 只认常量。
    assert "curateCheckUpdates:" in CORE and '"/api/curate/check-updates"' in CORE
    assert ACT.count('"/api/curate/check-updates"') == 0
    assert "API.curateCheckUpdates" in ACT


def test_curate_apply_sends_back_the_plan_confirm_token():
    """两步端点的机械核心（全自动化后由 runner 链式直推，契约不变）：apply 必须
    **原样回传** plan 给的 confirm_token；search_online 还必须原样回传 plan_result
    （后端据此重算内容指纹，对不上零写入）。"""
    assert "confirm_token:" in ACT and "pr.confirm_token" in ACT, "apply 没有回传 plan 的 confirm_token"
    assert "plan_result: pr" in ACT, "search_online 的 apply 没有回传 plan_result"
    for action in ('action: "import"', 'action: "search_online"', 'action: "remove"', 'action: "restore"'):
        assert ACT.count(action) >= 2, f"{action} 的 plan/apply 至少各一次（执行必须走 apply 端点）"


def test_curate_apply_failure_says_nothing_was_changed():
    """apply 被拒（token_mismatch / duplicate_content / 400）时后端 fail-closed 零写入——
    失败总结必须照实带「本次没有任何改动」（p10：接管页退役，改由行动流失败条目 + 失败总结呈现）。"""
    fails = re.findall(r"if \(!applied\.ok\) return \{ ok: false, error: ([^}]+)\}", ACT)
    assert len(fails) >= 4, f"四个写动作的 apply 都该有失败出口，只找到 {len(fails)} 处"
    for f in fails:
        assert "本次没有任何改动" in f, f"apply 失败的总结没有照实说「本次没有任何改动」：{f}"
    # 失败句由 ACT_LEAD.fail 模板构造（结构上取不到「已」）——该真行为门见本文件 act_core 节


def test_curate_search_online_marks_the_network_fetch_before_it_happens():
    """联网是这步动作的特殊性质：行动流必须**在发请求之前**如实播报「联网查询 …」——
    不能等查完/入完库才说（全自动化：播报即行动流条目，无问卷）。"""
    runner = re.search(r"async function actRunCurateSearchOnline\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert runner, "找不到 actRunCurateSearchOnline"
    code = _strip_comments(runner.group(1))
    i_step = code.index('arxStep("联网查询 "')
    i_post = code.index("actCuratePost(API.curatePlan")
    assert i_step < i_post, "联网播报必须先于联网请求"
    assert "写入外部库" in code, "入库这一步也要如实播报"


def test_the_decision_survey_is_retired_by_full_automation():
    """2026-08-03 执行侧全自动化：问卷弹窗（survey.js）整建制拆除——
    文件、挂点、初始化、importmap、执行层引用全部清除，grep 零残留。"""
    assert not (STATIC / "js" / "survey.js").exists(), "survey.js 文件必须删除"
    assert 'id="surveyModal"' not in HTML, "index.html 不得再留问卷弹窗挂点"
    assert '"#survey"' not in HTML and "survey.js" not in HTML, "importmap/脚本标签不得再引 survey.js"
    assert "surveyRun" not in ACT and "#survey" not in ACT, "act.js 不得再引用问卷"
    assert "survey" not in BOOT, "boot.js 不得再初始化问卷"
    for name in ("panel/board.js", "core/interactions.js", "core/boot.js", "core/core.js", "act/act_run.js"):
        assert f'import {{ surveyRun }} from "#survey"' not in _read(name), f"{name} 还在 import 问卷"


def test_pending_receipt_branch_is_gone_with_two_step_confirm():
    """两步确认（预览面板→用户亲手点确认）随 2026-08-03 全自动化拆除：前端 runner 链式
    plan→apply 无人工停点，生产侧再无任何路径产出 outcome.pending——ACT_LEAD.pending
    模板与 actWhatHappened 的 pending 分支已成死代码，删除后不许回插
    （若未来恢复人工确认闸，连模板带门一起重写，别复活旧分支）。"""
    core = _strip_comments(ACT_CORE)
    assert "pending" not in core, "act_core 又出现已拆除的两步确认 pending 分支"
    assert _strip_comments(ACT).count("outcome.pending") == 0, "act.js 还有 pending 回执的生产消费"


def test_the_dead_chat_note_and_prefill_strip_patch_are_gone():
    """ 清理清单：actChatNote 已无生产调用方（p10 起执行全程由行动流 +
    总结 sys 呈现），删除后不许回插；ACT_PREFILL_STRIP 正则剥词补丁同期退役——
    「检查更新」语义已从 search_online 拆出成 curate.check_updates，预填改由规划侧槽位
    （plan.slots.keywords/species）与 /api/interpret 确定性解析供给。"""
    assert "actChatNote" not in _strip_comments(ACT_CORE), "actChatNote 死函数应已删除"
    assert "ACT_PREFILL_STRIP" not in _strip_comments(ACT), "正则剥词补丁应已删除"
    prefill = re.search(r"async function actCuratePrefill\(([^)]*)\)\s*\{(.*?)\n\}", ACT, re.S)
    assert prefill, "找不到 actCuratePrefill"
    assert "plan" in prefill.group(1).split(","), "预填必须能拿到 plan（slots 优先）"
    code = _strip_comments(prefill.group(2))
    assert code.index("slots.keywords") < code.index("API.interpret"), (
        "预填优先级必须是 plan.slots 在前、/api/interpret 在后"
    )





# ---------------------------------------------------------------- check_updates / cfgAgent / plan.trace

def test_curate_check_updates_is_a_readonly_surveyless_runner():
    """curate.check_updates（新动词，「检查来源更新」从 search_online 拆出）：
    只读、无问卷——与 curate.list 同形态（arxBegin → POST → 结果卡 → arxFinish）。
    端点集中声明在 core.js；runner 不开问卷、不碰 confirm_token、不调 apply；
    失败出口如实回报（行动流的 arxFail 由派发层统一走）。"""
    assert "curateCheckUpdates:" in CORE and '"/api/curate/check-updates"' in CORE, (
        "check-updates 端点应集中声明在 core.js 的 API 常量里"
    )
    assert ACT.count('"/api/curate/check-updates"') == 0, "act.js 不该再手写一遍端点地址"
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table, "找不到 ACT_RUNNERS"
    m = re.search(r'"curate\.check_updates":\s*(\w+)', table.group(1))
    assert m, "curate.check_updates 不在派发表里"
    body = re.search(r"async function " + m.group(1) + r"\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, f"runner {m.group(1)} 不存在"
    code = _strip_comments(body.group(1))
    assert "surveyRun" not in code and "confirm_token" not in code, "只读动词不该开问卷 / 碰 confirm_token"
    assert "API.curateCheckUpdates" in code
    assert "if (!got.ok) return { ok: false" in code, "网络/解析失败要如实回报"
    assert "sources: src ? [src] : null" in code, (
        "plan.slots.source 命中时查来源子集，否则 null 全查（后端契约）"
    )
    # snapshot 源不伪造在线比对：结果卡按 mode 分两支，快照支给官网核对入口
    card = re.search(r"function actCheckUpdatesCardHtml\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert card, "找不到 actCheckUpdatesCardHtml"
    ccode = _strip_comments(card.group(1))
    assert '"online"' in ccode and "site_url" in ccode, "online / snapshot 两种模式要分开如实呈现"


def test_curate_db_status_runner_uses_observation_then_endpoint_fallback():
    """curate.db_status（只读状态汇报）：runner 双通道同一真源——
    agent 图内 execute 已调过工具 → 直接用 plan.observation；保底规划 → POST /api/curate/status
    现取。汇报措辞：agent 路径 plan.report_zh 直接作总结正文（打 AI 总结标，不再二次调
    /api/act/summary）。"""
    assert "curateStatus:" in CORE and '"/api/curate/status"' in CORE, "status 端点集中声明在 core.js"
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table, "找不到 ACT_RUNNERS"
    m = re.search(r'"curate\.db_status":\s*(\w+)', table.group(1))
    assert m, "curate.db_status 不在派发表里"
    body = re.search(r"async function " + m.group(1) + r"\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, f"runner {m.group(1)} 不存在"
    code = _strip_comments(body.group(1))
    assert "plan.observation" in code, "agent 路径必须优先用图内 observation（不重复取）"
    assert "API.curateStatus" in code, "保底路径必须走 /api/curate/status 端点"
    assert "confirm_token" not in code and "API.curateApply" not in code, "只读动词不碰写盘通道"
    # 汇报呈现：report_zh 作正文 + AI 总结标；没有 report_zh 才走 /api/act/summary 改写
    finish = re.search(r"function actFinish\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert finish, "找不到 actFinish"
    fcode = _strip_comments(finish.group(1))
    assert "plan.report_zh" in fcode and "llmTag: true" in fcode
    assert "!outcome.cancelled && !plan.report_zh" in fcode, (
        "report_zh 已在场时不许再调 actFetchLlmSummary（图内汇报与端点改写不重复付）"
    )


def test_the_route_body_carries_the_agent_exec_switch():
    """AI 执行开关（cfgAgentExec，默认开；2026-08-03 合并旧 autoAct+agent）随
    /api/utterance 请求带给后端（契约 §2.4）：ubRouteBody 读 getConfig().agent；
    开关是设置面板一等项（#nodeAgentExec）、可持久化；
    启动后经 /api/health 探测（llm_server 门控 + extensions.agent 扩展可用性标注）。"""
    body = re.search(r"function ubRouteBody\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubRouteBody"
    code = _strip_comments(body.group(1))
    assert "agent: cfg.agent" in code, "ubRouteBody 必须带 agent 字段（读 getConfig().agent）"
    assert 'id="cfgAgentExec"' in HTML and 'id="nodeAgentExec"' in HTML
    shell = _strip_comments(SHELL)
    assert "agent:" in shell and "agentExec" in shell, "开关必须能持久化（getConfig / save / load 三处）"
    assert "syncAgentAvailability()" in BOOT, "启动后须探测 /api/health"
    assert "llm_server" in shell and "extensions" in shell, "health 快照：服务端 key 门控 + 扩展标注"
    # 门控语义（开关语义统一）：未配 key 禁点并指路；已配必可开关——不再有第二道闸锁开关
    assert "aiGateChange" in shell and "syncAiGates" in shell
    assert "API 未配置" in shell, "未配 key 点击必须弹「API 未配置」"
    interactions = _read("core/interactions.js")
    assert "aiGateChange" in interactions, "LLM 依赖开关必须统一过禁点闸"


def test_the_route_phase_renders_real_backend_trace_steps():
    """agent 路径的规划步骤（plan.trace）是后端 langgraph 各节点的真实记录——路由阶段照单
    渲染并标明「Agent 规划」，执行阶段仍走 runner 自有步骤；trace 为空时不伪造步骤。"""
    body = re.search(r"async function actDispatchPlan\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "找不到 actDispatchPlan"
    code = _strip_comments(body.group(1))
    assert "plan.trace" in code and "label_zh" in code, "路由阶段要用后端 trace 渲染"
    assert "Agent 规划" in code, "trace 开头一步要标明「Agent 规划」"
    assert code.index("plan.trace") < code.index("await runner(plan)"), (
        "trace（路由阶段）必须先于 runner（执行阶段）渲染"
    )


# ---------------------------------------------------------------- 流式规划+ 一句话收尾（§5.3）

PROGRESS = _read("core/progress.js")


def _fn_body(src: str, signature: str) -> str:
    """按函数签名取函数体（函数都以列 0 的 } 收尾——本仓库风格，正则可锚）。"""
    m = re.search(re.escape(signature) + r"\s*\{(.*?)\n\}", src, re.S)
    assert m, f"找不到 {signature}"
    return m.group(1)


def test_streaming_route_only_fires_when_agent_on_and_extension_available():
    """流式规划（claudecode 式）：`stream:true` 只在 AI 执行开且扩展可用时出现。
    （2026-08-04 产品方钦点改行为）：流式档**也起跑 startProgress**
    ——只挂 loading 静态视觉时没有在途旗标，进度泡不显示；数字里程表退役，loading 与
    行动流真实步骤并滚，一个报「还在干活」、一个报「干到哪步」，互不冒充。不确定态「规划中…」
    进度泡照旧。"""
    body = _strip_comments(_fn_body(BOARD, "export async function ubSubmit(source, opts)"))
    assert "stream: true" not in body, "stream:true 只能出现在 ubFetchStream 里（ubSubmit 不手拼）"
    assert "_cfg.agent && !agentExtMissing()" in body, (
        "流式档必须同时看 AI 执行开关与 health 扩展探测标记"
    )
    assert "ubFetchStream(text" in body, "流式档必须走 SSE 请求"
    # 全程**一条**恒速曲线：ubSubmit 只起跑一次 startProgress（流式/非流式同点），
    # runRecommend 接手被幂等守卫跳过；流式失败回退档**不重启**（重启会从 0 倒滚）。
    assert body.count("startProgress(") == 1, "startProgress 全程只许一处（幂等接手，不重启）"
    head, _, tail = body.partition("catch (streamErr)")
    assert head.count("startProgress(") == 1, "发送即起跑（流式档同点起跑，治孤 %）"
    assert "startProgress(" not in tail, "流式失败是同一次路由的继续，不许重启里程表（防 0 倒滚）"
    assert 'classList.remove("loading")' not in tail, "回退档不许摘 loading 再起跑（同一倒退坑）"
    assert "if (!reply)" in tail, "回退后必须落到既有非流式请求再发一次——不静默失败"
    # 数字百分比撤下——进度泡＝三点动画；流式档带不确定态文案，非流式档只有三点。
    assert 'cbProgressBegin(streamAgent ? "规划中…" : "")' in body
    assert "Math.floor(progressPct())" not in BOARD, "进度泡不许再回退到 progressPct() 百分比"
    assert "_cbProgLabel ?" in BOARD, "进度泡必须支持流式档不确定态文案（规划中…）"


def test_streaming_sse_parser_frames_across_chunks_and_fails_loud():
    """SSE 手动解析契约：空行分帧（帧可跨 chunk）、只消费 `data: ` 行、三档事件
    （step 实时上屏 / final 完整响应 / error 协议失败）；非 2xx、断流无 final 都必须抛错
    让调用方走回退——不许拿半截响应当答案。"""
    # 签名加 reqId（断流重发同号，服务端幂等占用）——钉字刻意更新。
    # 签名尾部再加 opts（suggestedRecipe 透传，流式/非流式同口径）——钉字刻意更新。
    body = _strip_comments(_fn_body(BOARD, "async function ubFetchStream(text, reqId, onStep, opts)"))
    assert "stream: true" in body, "请求体必须带 additive stream:true"
    assert "getReader()" in body and "TextDecoder" in body, "必须手动读流解码"
    assert r'"\n\n"' in body, "帧界是空行（跨 chunk 在缓冲里拼帧）"
    assert '"data: "' in body, "只消费 data: 行"
    for ev in ('"step"', '"final"', '"error"'):
        assert ev in body, f"SSE 事件 {ev} 未处理"
    assert "if (!res.ok || !res.body) throw" in body, "非 2xx 必须抛给调用方走回退"
    assert 'throw new Error("流式响应没有 final 帧")' in body, "收不到 final 是协议失败，不是空答案"


def test_streamed_steps_are_not_rendered_twice():
    """（用户重申三段结构）：流式期间过程展示**全部**归信息流工具行（flowPushEvent
    一工具一行、无 detail）——行动流（arx）不在流式期间开播（它把分流共识/理解意图等非工具
    节点连同 detail 搬上屏，是用户点名的冗余）；真实执行的 arx 由 actDispatchPlan 自开。
    _traceStreamed 去重标保留：流式回来的 plan 不许再把 plan.trace 渲染进行动流（步骤
    已随 SSE 进过工具行）。"""
    sub = _strip_comments(_fn_body(BOARD, "export async function ubSubmit(source, opts)"))
    assert 'flowPushEvent("step", step)' in sub and 'flowPushEvent("tool_start", step)' in sub, (
        "流式 step/tool_start 必须进信息流工具行"
    )
    assert "arxStep(" not in sub and "arxBegin(" not in sub, (
        "流式期间不许开行动流（过程展示归信息流工具行）"
    )
    assert "reply.plan._traceStreamed = true" in sub, "流式播过后 final 的 plan 必须打去重标"
    assert "replyFromStream && streamStepCount" in sub, (
        "去重标只许打在**流式回来的** plan 上——流式中途失败回退非流式后，"
        "重发的 plan.trace 一步都没播过，误打标会让规划步骤从记录里整体消失"
    )
    disp = _strip_comments(_fn_body(ACT, "export async function actDispatchPlan(plan, said)"))
    assert "plan._traceStreamed ? []" in disp, (
        "actDispatchPlan 必须跳过流式已播的 plan.trace（去重）"
    )


def test_brief_summary_replaces_body_and_exec_disclosure_is_lean():
    """一句话收尾+ 执行披露精简：/api/act/summary 带 brief:true
    拿到 summary_zh 后原位替换总结泡**正文**（「AI 总结」标保留）。
    起工具结果卡 / 「明细」/「执行过程」折叠条撤下；的 execSummary
    摘要句通道也退役——工具调用计数压缩句由 flow_trace.compressFlow 产出、渲染为回执
    气泡上方可展开的一行（entry.flow），本泡不再携带；LLM 缺席时正文回退为事实句一句话，
    不伪造简洁。"""
    body = _strip_comments(_fn_body(ACT, "function actFetchLlmSummary(plan, outcome, said, factual, entry, searchFacts)"))
    assert "brief: true" in body, "/api/act/summary 必须带 brief:true（一句话模式）"
    assert "cbUpdateEntry(entry, { text: String(d.summary_zh), llmTag: true })" in body, (
        "summary_zh 必须原位替换总结泡正文并保留「AI 总结」标"
    )
    # p11 混合轮：searchFacts（actFinish 一次性消费传入）非空时「前置检索」行前置进 done_lines，
    # LLM 的一句话把检索与执行两段合并写完（全轮单泡的事实来源）。
    assert "if (searchFacts && Number(searchFacts.total) > 0)" in body, (
        "p11：前置检索行必须有 total>0 守卫（0 命中的措辞由 act.js 检索回执侧负责，不进总结）"
    )
    assert 'doneLines.push(searchFactsReceiptText(searchFacts, "前置检索："))' in body, (
        "p11：searchFacts 非空时「前置检索」行必须经 board_core 锚点前置进 done_lines（检索+执行合并成一句话）"
    )
    assert "库中共 " in _read("panel/board_core.js"), (
        "措辞字面量的唯一真源在 board_core.searchFactsReceiptText（锚点丢失则措辞漂移）"
    )
    finish = _strip_comments(_fn_body(ACT, "function actFinish(plan, outcome, said, opts)"))
    assert 'cbLogPush("sys", factual' in finish, "LLM 缺席时正文回退为事实句一句话"
    assert "execSummary" not in finish, "执行摘要通道已退役（摘要归信息流压缩行）"
    assert "detailLines:" not in finish, "明细折叠区已撤"
    summ = _strip_comments(_fn_body(ACT, "function actSummaryHtml(opts)"))
    assert '<details class="arx-trace">' not in summ, "执行过程折叠区已撤"
    assert "cbh-ai" in BOARD and "AI 总结" in BOARD, "「AI 总结」小标的渲染必须在"
    flow = _read("core/flow_trace.js")
    assert "compressFlow" in flow and "执行了 " in flow, (
        "工具调用计数压缩句必须在纯核 flow_trace（可单测）"
    )


def test_loading_never_double_fires_with_streaming():
    """：loading 全程只挂一次——startProgress 的幂等守卫在（runRecommend 接手不重启）
    流式档与非流式档同走这一个起跑点（/ 后不再手工占 loading）
    且流式失败回退不得摘 loading 重起跑。数字里程表已退役，不存在「倒滚」。"""
    assert 'if (btn.classList.contains("loading")) return;' in PROGRESS, (
        "startProgress 的 loading 幂等守卫不在了——接手方会重复挂 loading"
    )
    sub = _strip_comments(_fn_body(BOARD, "export async function ubSubmit(source, opts)"))
    assert "if (streamAgent)" in sub, "流式分支必须还在（SSE 入口）"
    # 手工 loading 舞蹈已随孤 % 修复退役：流式分支里不许再出现绕过 startProgress 的裸挂类
    stream_seg = sub.split("if (streamAgent)", 1)[1]
    assert 'classList.add("loading")' not in stream_seg, (
        "流式档直接 startProgress（loading 与在途旗标统一起跑），不许再手工裸挂 loading"
    )


def test_loading_is_indeterminate_and_cache_hit_finishes():
    """：数字里程表退役——startProgress 只挂 loading 态与在途旗标（不再起 rAF 翻数）
    finishProgress 同步摘 loading（无补满尾巴）；缓存命中用**完成**语义收尾（finishProgress），
    不用取消语义 resetSubmitButton（结果秒出＝这次检索瞬间完成）。"""
    start = _strip_comments(_fn_body(PROGRESS, "export function startProgress(expectedMs)"))
    finish = _strip_comments(_fn_body(PROGRESS, "export function finishProgress()"))
    assert "requestAnimationFrame" not in start, "数字里程表退役，startProgress 不许再起 rAF 翻数"
    assert "_pctActive = true" in start, "startProgress 必须置在途旗标（board.js 据此挂三点动画）"
    assert 'classList.add("loading")' in start, "startProgress 必须挂 loading 静态态"
    assert 'classList.contains("loading")' in start, "loading 幂等守卫必须还在（接手方不重启）"
    assert "requestAnimationFrame" not in finish, "finishProgress 同步收尾，不许再补满翻数"
    assert "_pctActive = false" in finish, "finishProgress 必须清在途旗标"
    search = _strip_comments(_read("search/search.js"))
    cache_seg = search.split("if (cached)", 1)[1]
    assert "finishProgress()" in cache_seg, "缓存命中必须按完成语义收尾"
    assert 'classList.contains("loading")' in cache_seg, "缓存命中的收尾要先看有没有在途加载态"


# ---------------------------------------------------------------- 真实下载 UX 契约

def test_retired_server_download_endpoints_not_declared_in_core_api():
    """dl-browser-queue 后一切浏览器下载走统一下载引擎（core/downloads.js + /api/files），
    没有任何前端代码再调 /api/download/*（服务端代下状态机已退役，见 task_pack.js 文件头）。
    端点本身保留在后端供 MCP 消费方使用；core.js API 表只声明浏览器真调的端点，
    零消费的 download* 声明一律不许回流。"""
    core = _read("core/core.js")
    for key in ("downloadPlan", "downloadStart", "downloadStatus", "downloadCancel", "downloadUpdate"):
        assert f"{key}:" not in core, f"core.js API 出现零消费声明 {key}（/api/download/* 浏览器无人调）"


def test_task_pack_has_primary_browser_download_and_pack_fallback():
    """dl-browser-queue（2026-08-30 取代双按钮契约）：面板底部主按钮「下载勾选的数据集文件」
    （把勾选交给统一下载引擎 dlqEnqueueDatasets，浏览器直下主文件），次按钮「仍生成任务包」
    走既有 zip 链兜底；下载区是 downloads.js 的 dlqRender/dlqBind 薄适配。
    的 _dl 服务端代下状态机（plan/confirm/running/poll/update/cancel）已退役，遗物不得留。"""
    tp = _strip_comments(_read("act/task_pack.js"))
    assert 'id="tpDlEnqueueBtn"' in tp and "下载勾选的数据集文件" in tp, "主按钮「下载勾选的数据集文件」必须在"
    assert 'id="taskPackBuildBtn"' in tp and "仍生成任务包" in tp, "次按钮「仍生成任务包」兜底必须在"
    assert "dlqEnqueueDatasets" in tp, "主按钮必须把勾选交给统一下载引擎 dlqEnqueueDatasets"
    assert "dlqRender" in tp and "dlqBind" in tp, "下载区必须是 downloads.js 的 dlqRender/dlqBind 薄适配"
    for dead in ("_dl", "tpDownloadConfirm", "tpDownloadStart", "dlReset", "tpDlStartBtn"):
        assert dead not in tp, f"服务端代下时代遗物 {dead} 不许留在 task_pack.js（单通道原则）"


def test_pack_bytes_label_says_real_data_size_not_zip_size():
    """（B 文案）：按钮 label 的体积数字必须是「真实数据共约 X」——
    它是 preview 逐文件 bytes 求和（真实文件体积），不是 zip 体积。
    旧文案「约 X 下载量」被反复读成 zip 体积，前端按钮链路不得再现。"""
    tp = _strip_comments(_read("act/task_pack.js"))
    assert "真实数据共约" in tp, "按钮 label 必须写「真实数据共约 X」（真实文件体积）"
    assert "下载量" not in tp, "「约 X 下载量」已改「真实数据共约 X」——不许回潮"


def test_cache_hit_branch_wraps_finally_around_the_reset():
    """（小修）：缓存命中分支用 try/finally 包住「渲染 + 收尾」——
    landRecommendResult / 打点任何一步抛错（典型：新旧 JS 混合缓存的 ReferenceError，FRONTEND.md §4.3）
    也要复位按钮；否则 submitBtn/chatSendBtn 卡 loading、ubSubmit 在途闸（submitBtn.disabled）
    拦下所有后续输入。收尾仍按「有无在途加载态」分完成/取消两语义。"""
    search = _strip_comments(_read("search/search.js"))
    seg = search.split("if (cached)", 1)[1].split("return;", 1)[0]
    assert "try {" in seg and "} finally {" in seg, "缓存命中分支必须用 try/finally 包住渲染+收尾"
    finally_block = seg.split("} finally {", 1)[1]
    assert "finishProgress" in finally_block and "resetSubmitButton" in finally_block, (
        "finally 必须按有无在途加载态完成收尾（finishProgress / resetSubmitButton 两分支都在）"
    )


def test_pack_download_verb_goes_browser_direct_automatically():
    """dl-browser-queue（取代 dl-auto-1）：pack.download 动词入口（actRunPackDownload）preview 后
    把勾选交给 dlqEnqueueDatasets——浏览器直下、全形态一致（网页版护栏也放行：通道本身就是
    浏览器，不再服务端代下）；零直下文件自动落任务包兜底。旧确认闸措辞与服务端代下调用必须消失。"""
    body = _strip_comments(_fn_body(ACT, "async function actRunPackDownload(plan)"))
    assert "dlqEnqueueDatasets" in body, "动词入口必须把勾选交给统一下载引擎 dlqEnqueueDatasets"
    assert '"browser-download"' in body, "成功回执 artifact.mode 必须是 browser-download"
    assert "已开始下载" in body, "成功回执必须是「已开始下载 N 个数据文件…」"
    assert "Ctrl+J" in body, "回执必须如实指向浏览器下载管理（Ctrl+J）"
    assert "buildTaskPack()" in body, "零直下文件的诚实降级（任务包 zip 链）必须仍在"
    for dead in ("tpDownloadConfirm", "tpDownloadStart", "webGuardOn", "real-download",
                 "请在面板点", "要等你在面板点"):
        assert dead not in body, f"旧服务端代下/确认闸措辞 {dead} 必须消失"
    imp = _strip_comments(ACT)
    assert "dlqEnqueueDatasets" in imp and "dlqFireBlob" in imp, "act.js 必须从 #downloads import 引擎"
    assert "tpDownloadConfirm" not in imp and "tpDownloadStart" not in imp, "act.js 不许再 import 服务端代下"
    assert "webGuardOn" not in imp, "护栏分支退役后 act.js 不许再 import webGuardOn"


def test_popup_blocked_hint_single_anchor_and_wiring():
    """2026-09-09 用户点图：下载/新窗口被浏览器弹窗拦截时用户常无感知、也不知被拦意味着
    什么。提醒文案只在 COPY.common.popupBlocked 一处定义；core.js 的 openPopup 是程序化
    新标签页唯一出口，被拦（window.open 返回 null）统一 toast；downloads.js 发射 toast
    与面板页脚、act.js 下载回执共用同一锚点，不许各调用点另写变体。
    2026-09-11 补强：用户实测瞬时 toast（≤4s）根本没看到——所有携带拦截提醒的 toast
    一律 sticky（常驻到用户点一下才关），瞬时提醒对此类场景无效。"""
    copy_js = _read("core/copy.js")
    assert "popupBlocked:" in copy_js, "拦截提醒文案必须在 COPY.common 单一定义"
    core = _strip_comments(CORE)
    fn = core.split("export function openPopup(", 1)[1]
    assert "window.open(" in fn and "toast(COPY.common.popupBlocked, { sticky: true })" in fn, (
        "openPopup 必须在 window.open 被拦（返回 null）时 sticky toast 统一提醒")
    assert "export function toast(msg, opts)" in core and 'classList.toggle("sticky", sticky)' in core, (
        "core.js 的 toast 必须支持 sticky 选项（常驻到点击关闭）")
    dl = _strip_comments(_read("core/downloads.js"))
    assert dl.count("COPY.common.popupBlocked") >= 2, "发射 toast 与面板页脚都必须接同一提醒锚点"
    assert '+ COPY.common.popupBlocked, { sticky: true }' in dl, (
        "下载发射 toast 必须 sticky——瞬时 toast 用户看不到")
    assert 'q.status === "queued" || q.status === "fired"' in dl, (
        "点下载的成功判定必须含 fired：dlqEnqueue(auto:true) 同步泵出队首后单文件"
        "唯一条目已是 fired，只数 queued 会把首次成功下载误判进「没有重复下载」分支")
    assert 'window.open(item.url, "_blank")' in dl, (
        "_fire 跨域通道必须 _blank+null 检测：noopener 恒返 null、具名复用也返 null"
        "（2026-09-12 探针实测），唯 _blank 的 null 能如实区分「被拦」")
    assert "w.opener = null" in dl, "_fire 防 tabnabbing 靠手动置 opener，不用 noopener 特性"
    assert "sameOrigin" in dl, "_fire 同源通道原地静默下载（锚点+download 属性），不开标签"
    assert "if (!_fire(_queue[i]))" in dl, "_pump 必须在被拦（_fire 返 false）时断链留队列"
    assert ', "_blank", "noopener")' not in core, (
        "openPopup 不得再用 noopener 特性（恒返 null=误报拦截），opener 手动置 null")
    assert "COPY.common.popupBlocked" in _strip_comments(ACT), "act.js 下载回执必须带拦截提醒"
    for mod in ("search/browse.js", "panel/board.js", "core/interactions.js"):
        assert "window.open(" not in _strip_comments(_read(mod)), (
            f"{mod} 不许绕过 openPopup 裸调 window.open")


def test_reset_task_pack_no_longer_guards_server_download():
    """dl-browser-queue（反向钉，取代守卫钉）：_dl 服务端代下状态机已退役——浏览器下载
    队列状态收在 downloads.js，resetTaskPack 不触碰。旧的「下载进行中跳过重置」守卫
    （_dl.stage）与 dlReset 不得再出现：留着只会误导后来者以为还有第二条下载通道。"""
    tp = _strip_comments(_read("act/task_pack.js"))
    seg = tp.split("export function resetTaskPack()", 1)[1].split("export function syncTaskPackBar", 1)[0]
    assert "_dl.stage" not in seg, "_dl 守卫必须随状态机一起退役"
    assert "dlReset" not in seg, "dlReset 必须随状态机一起退役"


def test_pack_download_receipt_carries_dlq_panel_pill():
    """2026-08-30 任务2（用户定）：下载面板的打开开关 = 回执气泡内**每批一颗 pill**
    （与检索结果 pill 同通道 flowSetPills→entry.pills、同位气泡内文字下方）。
    act.js 的 pack.download 三条落地路径（新发射 / 全部已在队列未重复下载 / 零直下降级 zip）
    都必须挂 dlq pill；面板 chip 对 pack.download 退役（同一面板不留两个开关），
    pack.preview 不触发下载、chip 保留。board.js 负责渲染 data-dlq-pill 且点击开面板。"""
    body = _strip_comments(_fn_body(ACT, "async function actRunPackDownload(plan)"))
    assert body.count("flowSetPills") >= 3, "新发射/已在队列/降级 zip 三条路径都必须 flowSetPills 挂 dlq pill"
    assert "dlq: true" in body and "下载队列" in body, "pill 必须是 dlq 类、文案「下载队列」"
    imp = _strip_comments(ACT)
    assert "flowSetPills" in imp, "act.js 必须从 #board import flowSetPills"
    chips = _strip_comments(_fn_body(ACT, "function actFixChips(plan, said, outcome)"))
    assert 'verb === "pack.preview"' in chips, "面板 chip 只留给 pack.preview（它不触发下载）"
    assert 'verb === "pack.download"' not in chips, "pack.download 的面板 chip 必须退役（pill 取代，不留双开关）"
    board = _strip_comments(BOARD)
    assert 'data-dlq-pill="1"' in board and "ft-pill--dlq" in board, "board.js 必须渲染 dlq pill"
    click_seg = board.split('closest("[data-dlq-pill]")', 1)
    assert len(click_seg) == 2, "board.js 必须有 data-dlq-pill 的点击分支"
    branch = click_seg[1][:1000]
    assert "taskPackPanel" in branch and "previewTaskPack()" in branch and "scrollIntoView" in branch, (
        "dlq pill 点击 = 开下载面板（unhide + previewTaskPack 重渲复活队列区 + 滚进视野）"
    )
    assert "tpDlZone" in branch, "有队列时必须滚到 #tpDlZone 队列本身，而不是面板顶的勾选清单"
    css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
    assert ".ft-pill--dlq" in css, "app.css 必须有 dlq pill 的区分样式"


def test_cite_and_reuse_receipts_carry_dlq_pill():
    """2026-08-31（用户定「pill 与工具执行绑定」，泛下载一视同仁）：引文导出与投稿材料
    也是下载——actRunCiteExport / actRunReusePack / 环内 cite_export 自动下载三条路径
    都必须给回执气泡挂 dlq pill（与 pack.download 同通道 flowSetPills、同文案「下载队列」），
    点它开结果区下载面板。"""
    cite = _strip_comments(_fn_body(ACT, "async function actRunCiteExport(plan)"))
    assert "flowSetPills" in cite and "dlq: true" in cite, "actRunCiteExport 必须挂 dlq pill"
    reuse = _strip_comments(_fn_body(ACT, "async function actRunReusePack(plan)"))
    assert "flowSetPills" in reuse and "dlq: true" in reuse, "actRunReusePack 必须挂 dlq pill"
    fin = _strip_comments(_fn_body(ACT, "function actFinish(plan, outcome, said, opts)"))
    cite_dl = _strip_comments(_fn_body(ACT, "function actAutoDownloadCiteFiles(r)"))
    assert "return fired" in cite_dl, "actAutoDownloadCiteFiles 必须返回新发起数（供 pill 计数）"
    assert "_citeFired" in fin and "flowSetPills" in fin, (
        "actFinish 必须把环内 cite_export 的新发起下载挂成 dlq pill")


def test_flow_set_pills_merges_by_kind():
    """2026-08-31（用户定「pill 与工具执行绑定」）：flowSetPills 按族分治——入件里的
    下载 pill（dlq）追加不顶替（已持件的检索 pill 原样保留）；入件里的检索结果 pill
    同族顶替、下载 pill 不动，且检索 pill 恒排前。混合轮（先 rank 后 pack.download）
    因此两类 pill 同挂一颗回执气泡；空入件不清场（清场只归 flowReset）。"""
    body = _strip_comments(_fn_body(BOARD, "export function flowSetPills(pills)"))
    assert "inDlq" in body and "inResult" in body and "stagedDlq" in body, (
        "flowSetPills 必须分出「新 dlq / 新检索 pill / 已持件 dlq」三段")
    assert "concat(inDlq)" in body, "下载 pill 必须追加到已持件序列尾部"
    assert "inResult.concat(stagedDlq)" in body, "检索 pill 必须同族顶替且排在 dlq 之前"
    assert "if (!incoming.length) return" in body, "空入件不许清场（否则 dlq 持件会被随手抹掉）"


def test_action_hint_cleared_when_action_executed():
    """2026-08-31（用户指认自相矛盾）：「你提到了下载——检索本身不包含这一步」指路条
    只在该动作没被执行时才该挂着。同轮真执行成（pack.download/cite.export 等）时必须摘掉
    （results.js clearActionHint，actFinish 成功收尾时调）；失败/取消保留作手动退路。"""
    res = _strip_comments(RESULTS)
    assert "export function clearActionHint()" in res, "results.js 必须导出 clearActionHint"
    act = _strip_comments(ACT)
    imp = [ln for ln in act.splitlines() if "#results" in ln]
    assert any("clearActionHint" in ln for ln in imp), "act.js 必须从 #results import clearActionHint"
    fin = _strip_comments(_fn_body(ACT, "function actFinish(plan, outcome, said, opts)"))
    assert "actCoversActionHint(plan)" in fin and "clearActionHint()" in fin, (
        "actFinish 必须在动作执行成功时摘掉指路条")
    # 摘除必须只挂在成功守卫这一处：守卫写死「成功且非取消且动词覆盖」，别处不许再摘
    # （失败/取消时指路条保留，正是那时的手动退路）。整行压平空白后精确钉守卫条件。
    fin_flat = " ".join(fin.split())
    assert "outcome.ok && !outcome.cancelled && actCoversActionHint(plan)" in fin_flat, (
        "只在成功时摘——失败/取消保留指路条")
    assert fin.count("clearActionHint()") == 1, "指路条摘除在 actFinish 里必须只有成功守卫这一处调用"
    helper = _strip_comments(_fn_body(ACT, "function actCoversActionHint(plan)"))
    # 动词表是 helper 外的模块级常量，钉它必须取表体本身（此前钉整份 ACT 源码，
    # 表里删掉动词测试照绿）；表内四个动词一个不许漏（pack.preview 也在——预览即产包交付）。
    m = re.search(r"ACTION_HINT_COVERED_VERBS\s*=\s*\{([^}]*)\}", act)
    assert m, "act.js 必须有 ACTION_HINT_COVERED_VERBS 指路条核销动词表"
    verbs = m.group(1)
    for v in ('"pack.download"', '"pack.preview"', '"cite.export"', '"reuse.pack"'):
        assert v in verbs, f"指路条核销动词表必须含 {v}"
    assert "ACTION_HINT_COVERED_VERBS" in helper, "helper 必须查动词表，不许另写一份动词清单"
    assert "plan.steps" in helper, "环内 steps 里的 cite.export 也算执行成"


def test_action_hint_settled_survives_rerender():
    """指路条核销必须跨重渲存续：换批（switchBatch/_batchView 继承落地时的 action_markers）、
    分面重跑、历史回看都会重新走 renderActionHint，只摘 DOM 会把「检索本身不包含这一步」复活
    （明明执行了）。results.js 用时间线级核销态 _actionHintSettled 按住渲染口；新查询时间线
    （runRecommend 非 keep 分支）经 resetActionHint 复位，那句若也提到执行类说法则照常指路。"""
    res = _strip_comments(RESULTS)
    assert "export function resetActionHint()" in res, "results.js 必须导出 resetActionHint（新时间线复位口）"
    hint = _strip_comments(_fn_body(RESULTS, "function renderActionHint(data)"))
    assert "_actionHintSettled" in hint, (
        "renderActionHint 必须先判核销态——否则换批/历史回看会把已摘的指路条复活")
    clr = _strip_comments(_fn_body(RESULTS, "export function clearActionHint()"))
    assert "_actionHintSettled = true" in clr, "clearActionHint 必须同时置核销态，不能只摘 DOM"
    sea = _strip_comments(SEARCH)
    imp = [ln for ln in sea.splitlines() if "#results" in ln]
    assert any("resetActionHint" in ln for ln in imp), "search.js 必须从 #results import resetActionHint"
    branch = sea.split("if (!keep)", 1)
    assert len(branch) == 2 and "resetActionHint" in branch[1][:400], (
        "新查询时间线分支（!keep）必须复位指路条核销态——否则上一段对话执行过后，新查询的指路条不再出现")


# -------------------------------------- dl-browser-queue：统一下载队列引擎契约

def test_downloads_engine_module_contract():
    """core/downloads.js 是全站唯一浏览器下载通道（AGENTS.md §2 单通道原则锚点）。
    导出面必须齐全；fired 措辞只许「已交给浏览器」（绝不许暗示「已下载完成」）；
    面板脚注必须如实指向浏览器下载管理（Ctrl+J）与「是否允许下载多个文件」授权询问。"""
    dlc = _strip_comments(_read("core/downloads.js"))
    for fn in ("dlqEnqueue", "dlqEnqueueDatasets", "dlqFireBlob", "dlqRender", "dlqBind",
               "initDownloads", "dlqCancelItem", "dlqCancelQueued", "dlqRetryItem",
               "dlqClearFinished", "dlqSnapshot", "dlqResume"):
        assert f"export function {fn}" in dlc or f"export async function {fn}" in dlc, \
            f"downloads.js 必须导出 {fn}"
    assert "已交给浏览器" in dlc, "fired 措辞必须是「已交给浏览器」"
    assert "已下载完成" not in dlc, "页面拿不到在途进度，绝不许说「已下载完成」"
    assert "Ctrl+J" in dlc, "脚注必须指向浏览器下载管理（Ctrl+J）"
    assert "是否允许下载多个文件" in dlc, "脚注必须提示浏览器的多文件授权询问"


def test_downloads_engine_is_a_leaf_importer():
    """downloads.js 是汇点：只许 import core/act_core/usage_core/usage_log；绝不许反向 import
    task_pack/act/cards/reuse_pack（「为这部分生成任务包」靠 dlqBind 回调注入，不靠 import——
    这是它不进 import 环的原因，环门在 test_frontend_import_graph.py）。"""
    dl = _strip_comments(_read("core/downloads.js"))
    for bad in ('from "#task_pack"', 'from "#act"', 'from "#cards"', 'from "#reuse_pack"',
                'from "#board"', 'from "#search"'):
        assert bad not in dl, f"downloads.js 不许 import {bad}（用 dlqBind 回调解耦）"
    for ok in ('from "#core"', 'from "#act_core"', 'from "#usage_core"', 'from "#usage_log"'):
        assert ok in dl, f"downloads.js 应 import {ok}"


def test_downloads_registered_everywhere():
    """#downloads 必须在两页 importmap 与 package.json 登记；boot.js init() 必须调
    initDownloads()（document 级委托拦截 data-dlq 锚/动作），dataset 页 dsInit 同挂（本页无 boot）。"""
    assert '"#downloads": "/static/js/core/downloads.js' in HTML, "index.html importmap 缺 #downloads"
    dataset = (STATIC / "dataset.html").read_text(encoding="utf-8")
    assert '"#downloads": "/static/js/core/downloads.js' in dataset, "dataset.html importmap 缺 #downloads"
    pkg = (ROOT / "package.json").read_text(encoding="utf-8")
    assert '"#downloads": "./web/static/js/core/downloads.js"' in pkg, "package.json imports 缺 #downloads"
    assert "initDownloads();" in _strip_comments(BOOT), "boot.js init() 必须调 initDownloads()"
    dp = _strip_comments(_read("search/dataset_page.js"))
    assert "initDownloads();" in dp, "dsInit 必须挂 initDownloads()"


def test_download_anchors_carry_dlq_attrs():
    """卡片 CTA / 介绍 bare 行 / 文件弹窗 / 数据集页页头的「下载数据」直链必须带 data-dlq
    属性组（document 级委托拦截进队列）——裸 href+download 会绕过队列、下载面板里不留痕。"""
    cards = _strip_comments(_read("search/cards.js"))
    assert cards.count('data-dlq="data"') >= 3, "cards.js 至少三处 data-dlq（CTA/介绍行/文件弹窗）"
    dp = _strip_comments(_read("search/dataset_page.js"))
    assert 'data-dlq="data"' in dp, "dataset_page.js 页头下载按钮必须带 data-dlq"


def test_no_second_download_channel_besides_engine():
    """单通道守门员：downloadBlobAs 只许存在两处——core.js 定义 + downloads.js 包一层
    （dlqFireBlob）。其他任何文件 import/调用 downloadBlobAs = 开第二条下载通道，当场红。"""
    for rel in ("act/act.js", "act/task_pack.js", "act/reuse_pack.js",
                "core/benchfb.js", "panel/project_exports.js", "search/cards.js",
                "search/dataset_page.js"):
        src = _strip_comments(_read(rel))
        assert "downloadBlobAs" not in src, f"{rel} 不许再碰 downloadBlobAs（统一走 #downloads）"


# ---------------------------------------------------------------- 批次覆盖语义（设计约定）

def test_batch_rank_suffix_labels_the_sorting_layer() -> None:
    """（设计约定）：非活动备选批的排序层标注——规则=1 / 规则+本地精准重排=2 /
    规则+本地+AI 重排=3；未知/缺失 trace → 空串（不可比较，不标注）。"""
    a11 = {"payload": {"search_trace": {"steps": [
        {"id": "rule_rank", "status": "used"}, {"id": "local_semantic", "status": "used"}]}}}
    a21 = {"payload": {"search_trace": {"steps": [
        {"id": "rule_rank", "status": "used"}, {"id": "local_semantic", "status": "used"},
        {"id": "llm_rerank", "status": "used"}]}}}
    a31 = {"payload": {"search_trace": None}}
    a41 = {"payload": {"search_trace": {"steps": []}}}
    got = _browser_helper_in_node(RESULTS, "_batchRankSuffix",
                                  "_in.map((x) => _batchRankSuffix(x))", [a11, a21, a31, a41])
    assert got == ["规则+本地精准重排", "规则+本地+AI 重排", "", ""], got


def test_batch_select_spec_passes_in_node() -> None:
    """真行为：node 直跑 batch_select_spec.mjs（断言失败 → 非零退出）。"""
    node = _resolve_node()
    assert node, "未找到 node（BIODATA_NODE 或 PATH）"
    spec = ROOT / "tests" / "js" / "batch_select_spec.mjs"
    r = subprocess.run([node, str(spec)], cwd=ROOT, capture_output=True, text=True)
    assert r.returncode == 0, f"batch_select_spec.mjs 失败：\n{r.stdout}\n{r.stderr}"


def test_relax_card_wording_and_exec_single_channel() -> None:
    """ 契约钉（2026-09-14 收编）：「放松哪个条件」只剩 ask.relax 选择卡一个用户触点——
    渲染唯一（askRelaxShow：only→只按「X」搜 / drop→不按「X」筛）、执行唯一（cbChoose
    askrelax 分支：所选 opt.suppress 原样下发 cbCommit，前端零合并零改写）。钉死渲染措辞
    模板与执行透传，防第二触点/第二执行路径复活。"""
    show = re.search(r"function askRelaxShow\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert show, "找不到 askRelaxShow 的函数体"
    body = show.group(1)
    assert re.search(r'只按「"\s*\+\s*\(o\.label', body), "only 档措辞模板变了（只按「X」搜）"
    assert re.search(r'不按「"\s*\+\s*\(o\.label', body), "drop 档措辞模板变了（不按「X」筛）"
    # 2026-09-14 用户评审：选项不内嵌示例数据集名（信息效率），卡层不再消费 preview；
    # 卡面必须另起一行指出卡住的查询原句（一句话多句查询时不张冠李戴）。
    assert "o.preview" not in body, "选项不得再内嵌预览数据集名（卡层 preview 已退役）"
    assert "cb-relax-q" in body and "卡住的查询：「" in body, "卡面缺「卡住的查询」行"
    # 执行通道：suppress 清单原样透传（服务端已按整组替换语义合并好）
    choose = re.search(r"async function cbChoose\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert choose, "找不到 cbChoose 的函数体"
    assert "suppressed_constraints: (opt.suppress || []).slice()" in choose.group(1), \
        "作答执行必须把所选 suppress 原样下发（整组替换语义，前端零合并逻辑）"
    # 退役面：旧触点的函数/样式钩子不得在交付代码里复活
    for dead in ("deriveRescueOptions", "openRescueStrip", "closeRescueStrip",
                 "applyRelaxation", "buildRelaxBlock", "relaxEffectiveHits"):
        assert dead not in _strip_comments(BOARD), f"旧触点 {dead} 在 board.js 复活"
        assert dead not in _strip_comments(RESULTS), f"旧触点 {dead} 在 results.js 复活"


# ---------------------------------------------------------------- p11系统回复 = LLM final answer

def test_search_reply_endpoint_lives_in_core_api_dict():
    """检索回执 LLM 改写端点集中声明在 core.js 的 API 常量表；任何模块不手写端点字面量
    （与 act/summary 同纪律——第二入口就是漂移的起点）。"""
    assert 'searchReply: "/api/search/reply"' in CORE, "core.js API 常量表缺 searchReply"
    for name in ("panel/board.js", "act/act.js", "search/search.js", "core/interactions.js"):
        assert _read(name).count('"/api/search/reply"') == 0, f"{name} 手写了 /api/search/reply 端点地址"
    assert "API.searchReply" in BOARD, "board.js 的检索回执改写必须走 API 常量表"


def test_search_receipt_is_llm_rewritten_in_place_with_honest_fallback():
    """p11 核心不变量①：纯检索轮——cbProgressDone 把进度泡蜕变成**确定性事实句**并交出回执 entry，
    cbFetchSearchReply 随后异步请 LLM 改写、成功才原位替换+挂「AI 总结」标；fail-open 留事实句。
    事实句先行（LLM 再慢用户也立刻有真话），改写绝不另起一颗泡。"""
    assert "return cbLogPush(\"sys\", hint ? (hint + \"；\" + text) : text) || false;" in BOARD, (
        "cbProgressDone 必须返回回执 entry（LLM 改写的锚点），不能还是裸 true")
    assert re.search(r"export function cbFetchSearchReply\(entry, facts\)", BOARD), (
        "cbFetchSearchReply 不在（检索回执的 LLM 改写层缺失）")
    assert "cbUpdateEntry(entry, { text: String(d.reply_zh), llmTag: true })" in BOARD, (
        "LLM 成功时必须原位替换正文并挂「AI 总结」标（归因诚实）")
    fn = _strip_comments(_fn_body(BOARD, "export function cbFetchSearchReply(entry, facts)"))
    assert 'cfg.provider === "mock"' in fn.split("fetch(")[0], (
        "mock 必须结构性短路（后端同判否），省一次注定无果的往返")
    code = _strip_comments(BOARD)
    assert re.search(r"const _receiptEntry = cbProgressDone\(_doneText\);\s*\n\s*if \(_receiptEntry\) cbFetchSearchReply\(",
                     code), "cbPushCurrent 须在回执落地后立刻接 LLM 改写（同一处，不许分叉）"


def test_hybrid_turn_lands_exactly_one_bubble():
    """p11 核心不变量②：混合轮（「小鼠空间转录组，并下载top5」这类 先检索后派发）——
    cbPushCurrent 据 popts.actPending **抑制**检索模板回执（进度泡留给 actDispatchPlan 接管），
    检索事实经 act.js _actTurnSearchFacts 并进执行汇报那颗泡；全轮只有一颗系统气泡。"""
    assert "actPending: !!opts.actPending" in SEARCH, "共享落地入口必须把 actPending 透传进 cbPushCurrent"
    assert SEARCH.count("actPending: !!opts.actPlan") == 2, (
        "runRecommend 两个落地点（缓存命中 + 真请求）都要标 actPending——漏一个，缓存命中那次就退回双泡")
    code = _strip_comments(BOARD)
    assert re.search(r"if \(popts && popts\.actPending\)", code), (
        "cbPushCurrent 缺 actPending 抑制分支（混合轮会再次冒出检索模板泡）")
    # act.js 侧：stash → actFinish 一次性消费 → actFetchLlmSummary 的 done_lines 前置检索行
    assert "_actTurnSearchFacts" in ACT, "act.js 缺 _actTurnSearchFacts（前置检索事实的一次性stash）"
    assert re.search(r"const searchFacts = _actTurnSearchFacts;\s*\n\s*_actTurnSearchFacts = null;", _strip_comments(ACT)), (
        "actFinish 必须一次性消费 stash（取走即清，绝不泄漏到下一轮纯执行句）")
    assert "actFetchLlmSummary(plan, outcome, said, factual, entry, searchFacts)" in ACT, (
        "前置检索事实必须进 LLM 一句话总结的 done_lines（合并覆盖检索+执行两段）")
    assert "searchFactsReceiptText" in ACT and "库中共 " in _read("panel/board_core.js"), (
        "前置检索事实行的措辞锚点缺失（唯一真源：board_core.searchFactsReceiptText）")
    # 边界：执行没接住（取消/busy/未接住/AI 执行被中途关掉）时进度泡绝不悬空——actAfterSearch 补诚实收尾
    assert re.search(r"function actAfterSearch\(query, opts\) \{[\s\S]*?cbProgressDone\(", ACT), (
        "actAfterSearch 必须在派发没接住的边界用 cbProgressDone 收尾（泡不悬空）")
    assert "cbProgressDone" in ACT.split('from "#board"')[0], "act.js 必须从 board import cbProgressDone"


def test_search_reply_llm_layer_exists_and_reuses_the_single_gate():
    """后端：search_reply_llm 复用 act_summary_llm._summarize_with_prompt（闸口/通道/fail-open
    唯一实现，抄第二份必漂移）；端点挂在 webapp 并带同源闸。"""
    src = (ROOT / "src" / "dataset_recommender" / "llm" / "search_reply_llm.py").read_text(encoding="utf-8")
    assert "from .act_summary_llm import _summarize_with_prompt" in src, (
        "检索回执层必须复用执行总结层的调用核心（同一闸口同一 fail-open 纪律）")
    assert "原样挑一条" in src, "建议白名单硬约束必须写进 prompt 铁律"
    webapp_src = (ROOT / "src" / "dataset_recommender" / "app" / "webapp.py").read_text(encoding="utf-8")
    assert '@app.post("/api/search/reply")' in webapp_src
    assert "_require_same_origin(request)" in webapp_src.split('@app.post("/api/search/reply")')[1], (
        "检索回执端点必须过同源闸（与 act/summary 同口径）")


def test_all_search_receipt_sites_route_through_the_llm_rewrite():
    """p11 补网：「系统回复 = LLM final answer」不许只覆盖 cbPushCurrent 一条路径。
    检索回执的全部落地站点都必须接 cbFetchSearchReply 原位改写：
    ① cbPushCurrent 主路径（真 /api/recommend 落地）；② _applyBatchDecision 采纳留痕（_aNote，
    含 legacy 单批兜底用 _view 本身作事实源）；③ dedupe/alternate 如实回执（decision.sysText）；
    ④ ubDispatch b 档 preliminary_final 收尾；⑤ act.js 混合轮边界（AI 执行中途关/取消·busy/
    未接住/派发抛错，经 _receiptWithLlm 一处收口）。漏一处，那条路径的用户就继续吃模板句。"""
    code = _strip_comments(BOARD)
    assert code.count("cbFetchSearchReply(") == 5, (
        f"board.js 应有 1 定义 + 4 调用点（cbPushCurrent/_aNote/dedupe/b档），当前 {code.count('cbFetchSearchReply(')} 处")
    assert "export function cbSearchReplyFacts(data, utterance, query, note)" in BOARD, (
        "公共事实构造器缺失——各站点数字口径（result_total 优先）会各抄一份必漂移")
    assert re.search(r"const _ab = _batchById\(_view\.result_batches, decision\.activeBatchId\);\s*\n\s*if \(_e\)", code), (
        "_aNote 必须用屏上活跃批作事实源（legacy 单批回退 _view）")
    assert "decision.sysText" in code and re.search(
        r"if \(!_actWillReceipt && decision && decision\.sysText\) \{\s*\n\s*const _e = cbLogPush\("
        r"[\s\S]*?cbFetchSearchReply\(_e,", code), "dedupe/alternate 回执没接 LLM 改写"
    assert re.search(r"const _bEntry = cbProgressDone\(_bNote\);[\s\S]*?cbFetchSearchReply\(_bEntry,", code), (
        "b 档（preliminary_final）收尾回执没接 LLM 改写")
    act = _strip_comments(ACT)
    assert "cbFetchSearchReply" in act.split('from "#board"')[0] and "cbSearchReplyFacts" in act.split('from "#board"')[0], (
        "act.js 必须从 board import cbFetchSearchReply 与 cbSearchReplyFacts")
    assert re.search(r"const _receiptWithLlm = function \(entry, note\)", act), (
        "act.js 混合轮边界缺 _receiptWithLlm 收口（三处收尾会各写各的）")
    assert act.count("_receiptWithLlm(") == 3, (
        f"act.js 边界三出路（AI 执行中途关/取消·busy·未接住/派发抛错）各调一次 _receiptWithLlm，"
        f"当前 {act.count('_receiptWithLlm(')} 处")


def test_rewrite_disclosure_folds_into_the_single_receipt_bubble():
    """p11 补网：_aNote 的「我把这句按『X』检索。」不再单独成泡（一个动作两颗泡正是投诉形态），
    并入回执正文与事实包 note，由 LLM 合并说清；fail-open 的确定性句同样一字不少。
    通用回退句「深入思考后找到了更匹配的结果，已更新。」仍是缺省正文（既有锚钉的同源句）。"""
    code = _strip_comments(BOARD)
    assert 'if (rewritten) cbLogPush("sys", "我把这句按「"' not in code, (
        "改写披露不许再单独成泡（折叠进唯一回执）")
    assert re.search(r"const _note = \(rewritten \? \"我把这句按「\"[\s\S]*?_disc \|\| \"深入思考后找到了更匹配的结果，已更新。\"\)", code), (
        "改写披露与披露句/通用句必须合并成唯一回执正文")
    assert re.search(r"cbFetchSearchReply\(_e, cbSearchReplyFacts\(_ab \? _ab\.payload : _view, text,", code), (
        "_aNote 的 LLM 改写必须把合并后的 _note 与真实批次事实一起上报")


def test_act_source_token_re_matches_backend_alias_tables():
    """ACT_SOURCE_TOKEN_RE（act.js 兜底分词的来源名剔除表）与后端两张别名表同口径：
    后端的来源别名（corpus_net.SOURCE_ALIASES 的键 + 检索侧
    search_request.SOURCE_ALIASES 的 ASCII 别名）前端必须**全覆盖**——漏一个，来源名就会
    混进兜底关键词里再搜一遍。反向前端刻意多收 bare "encode"（剔除是保守方向，见 act.js 注释）。
    非 ASCII 别名（中文说法）不进本钉：free_text_terms 只产 ASCII 词。
    通用联网分发通道的别名（候选键仅 ddg 的一组：ddg/duckduckgo/web/generic/通用）不是
    数据来源名，不进剔除表——"web"/"generic" 是正常检索词，剔除会误伤。"""
    from dataset_recommender.corpus import corpus_net as CN
    from dataset_recommender.retrieval import search_request as SR

    m = re.search(r"ACT_SOURCE_TOKEN_RE = /\^(.*?)\$/i", ACT)
    assert m, "act.js 缺 ACT_SOURCE_TOKEN_RE"
    token_re = re.compile(r"^(?:" + m.group(1) + r")$", re.I)

    missing = [k for k, cands in CN.SOURCE_ALIASES.items()
               if cands != ("ddg",) and k.isascii() and not token_re.match(k)]
    assert not missing, f"前端来源剔除表漏了后端来源别名：{missing}"
    missing2 = [a for _src, aliases in SR.SOURCE_ALIASES for a in aliases
                if a.isascii() and not token_re.match(a)]
    assert not missing2, f"前端来源剔除表漏了检索侧 SOURCE_ALIASES 别名：{missing2}"


# ---------------------------------------------------------------- 去重批锚钉（2026-08-30 web8）
#
# 前端去重批（2026-08-30 web8）把一批平行实现/复制变体/文案多锚点收成了单真源。
# 静态检查与 node --check 都测不出「哪天又有人手抄一份回来」，这里给其中五个锚点各钉一道门：
# 单实现钉（消费方不得再自带）、字面量唯一出处钉、真行为钉、前后端逐字同源钉。

def test_copy_text_any_is_the_single_clipboard_implementation():
    """剪贴板写入能力基元只有一份：core.js 的 copyTextAny（clipboard API 优先、
    textarea + execCommand 兜底）。消费方只许 import 调用；在消费方源码里再出现
    execCommand / 兜底函数名即红线（那就是第二份实现又长回来了）。"""
    assert "export function copyTextAny" in CORE
    for name in ("core/interactions.js", "core/feedback.js", "search/cards.js", "act/reuse_pack.js"):
        src = _strip_comments(_read(name))
        assert "copyTextAny(" in src, f"{name} 应消费 core.copyTextAny"
        for forbidden in ("execCommand", "_copyTextLegacy", "_fallbackCopy"):
            assert forbidden not in src, f"{name} 又自带了剪贴板兜底（{forbidden}）"


def test_search_facts_receipt_wording_lives_only_in_board_core():
    """「库中共 N 条匹配，结果区展示前 M 条」落地事实句唯一真源是
    board_core.searchFactsReceiptText；board/act 只许调用、不再各留字面量。
    （results.js 结果摘要/放宽提示里的「库中共」是另一特性的句子，不在本钉范围。）"""
    assert "库中共 " in _read("panel/board_core.js")
    for name, src in (("board.js", BOARD), ("act.js", ACT)):
        body = _strip_comments(src)
        assert "库中共 " not in body, f"{name} 又自带了回执句字面量"
        assert "searchFactsReceiptText(" in body, f"{name} 应调用 searchFactsReceiptText"


def test_single_bubble_verdicts_live_only_in_board_core():
    """唯一气泡规则的两个判定件唯一真源都在 board_core：act.js 逐项抑制用
    planIsRetrievalOnly；board.js 批次决策用 plansNeedActReceipt（喂 act.js 规范派发清单
    actCanonicalDispatchPlans 的输出——plan.steps 是「后端已执行」记录，不得作判定输入）。
    两侧都不许再自带检索动词表或 _planVerbs 取词函数。"""
    src = _strip_comments(_read("panel/board_core.js"))
    assert "export function planIsRetrievalOnly" in src
    assert "export function plansNeedActReceipt" in src
    assert "PLAN_RETRIEVAL_VERBS" in src
    act = _strip_comments(_read("act/act.js"))
    assert "_RETRIEVAL_VERBS" not in act and "planIsRetrievalOnly(" in act
    board = _strip_comments(_read("panel/board.js"))
    assert "_RETRIEVAL_VERBS" not in board and "_planVerbs" not in board
    assert "plansNeedActReceipt(" in board, "board.js 应以 plansNeedActReceipt 判唯一气泡归属"
    assert "actCanonicalDispatchPlans(" in board, "board.js 判定输入必须是规范派发清单"


def test_plan_is_retrieval_only_behavior_in_node():
    """真行为门（node 里跑真 board_core）：全检索动词（含 steps 形态）→ true；
    混入非检索动词 → false；取不到任何 verb / 无 plan → 保守 false（不抑制任何回执）。"""
    plans = [
        {"verb": "rank"},
        {"steps": [{"verb": "rank"}, {"verb": "search.rerun"}]},
        {"verb": "curate.list"},
        {"steps": [{"verb": "rank"}, {"verb": "task_pack.build"}]},
        {"steps": []},
        None,
    ]
    script = (
        f'import * as ns from "{(STATIC / "js" / "panel" / "board_core.js").as_uri()}";\n'
        'import { readFileSync } from "node:fs";\n'
        "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        "console.log(JSON.stringify(_in.map((p) => ns.planIsRetrievalOnly(p))));\n"
    )
    out = _run_node(script, plans, suffix=".mjs")
    assert out == [True, True, False, False, False, False]


def test_plans_need_act_receipt_behavior_in_node():
    """真行为门（node 里跑真 board_core）：规范派发清单任一动作自身 verb 在检索族外 → true
    （混合形态：动作 steps 里记着 rank/rerank 但真身 pack.download，只看自身 verb）；
    全检索动词清单 → false；空清单 / None → false（没有执行，谈不上执行回执）。"""
    plans = [
        [{"verb": "pack.download", "kind": "exec", "steps": [{"verb": "rank"}, {"verb": "rerank"}]}],
        [{"verb": "rank"}, {"verb": "rerank"}],
        [{"verb": "cite.export"}],
        [],
        None,
    ]
    script = (
        f'import * as ns from "{(STATIC / "js" / "panel" / "board_core.js").as_uri()}";\n'
        'import { readFileSync } from "node:fs";\n'
        "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        "console.log(JSON.stringify(_in.map((p) => ns.plansNeedActReceipt(p))));\n"
    )
    out = _run_node(script, plans, suffix=".mjs")
    assert out == [True, False, True, False, False]


def test_plan_head_is_graph_done_retrieval_behavior_in_node():
    """真行为门（node 里跑真 board_core）：规范派发清单的头部过滤判据——
    检索族 verb 且同一 verb 在 plan.steps 以 ok:true 执行 → true（真实混合轮顶层 rank）；
    非检索动词 / 失败步 / steps 无此 verb / 无 steps / 空输入 → false（守护渲染通道与失败渲染）。"""
    cases = [
        [{"verb": "rank"}, {"steps": [{"verb": "rank", "ok": True}]}],
        [{"verb": "compare"}, {"steps": [{"verb": "compare", "ok": True}]}],
        [{"verb": "rank"}, {"steps": [{"verb": "rank", "ok": False}]}],
        [{"verb": "rank"}, {"steps": [{"verb": "rerank", "ok": True}]}],
        [{"verb": "rank"}, {}],
        [{"verb": "pack.download"}, {"steps": [{"verb": "rank", "ok": True}]}],
        [None, None],
    ]
    script = (
        f'import * as ns from "{(STATIC / "js" / "panel" / "board_core.js").as_uri()}";\n'
        'import { readFileSync } from "node:fs";\n'
        "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        "console.log(JSON.stringify(_in.map((c) => ns.planHeadIsGraphDoneRetrieval(c[0], c[1]))));\n"
    )
    out = _run_node(script, cases, suffix=".mjs")
    assert out == [True, False, False, False, False, False, False]



def test_content_disposition_filename_parsing_lives_only_in_downloads():
    """Content-Disposition 文件名解析唯一真源是 downloads.dlqFilenameFrom；
    task_pack / project_exports 只许 import 调用，不再各带一份 filename=" 正则。"""
    assert "export function dlqFilenameFrom" in _read("core/downloads.js")
    for name in ("act/task_pack.js", "panel/project_exports.js"):
        src = _strip_comments(_read(name))
        assert "dlqFilenameFrom(" in src, f"{name} 应调用 dlqFilenameFrom"
        assert 'filename="' not in src, f"{name} 又自带了文件名解析正则"


def test_plan_cancelled_fallback_wording_matches_backend_verbatim():
    """计划取消兜底句前后端逐字同源：后端 action_plan.py 极性门缺省 reason_zh 与前端
    board_core.PLAN_CANCELLED_FALLBACK_ZH 必须是同一句话（board_core 注释钉了
    「改一边必须同步另一边」）；act.js/board.js 只许消费常量、不留字面量。"""
    backend = (ROOT / "src" / "dataset_recommender" / "agent" / "action_plan.py").read_text(encoding="utf-8")
    m_be = re.search(r'reason_zh"\]\s*=.*?\bor\s+"(?P<s>[^"]+)"', backend)
    assert m_be, "action_plan.py 找不到 reason_zh 缺省句"
    m_fe = re.search(r'PLAN_CANCELLED_FALLBACK_ZH\s*=\s*"(?P<s>[^"]+)"', _read("panel/board_core.js"))
    assert m_fe, "board_core.js 缺 PLAN_CANCELLED_FALLBACK_ZH"
    assert m_fe.group("s") == m_be.group("s"), (
        f"前后端取消兜底句漂移：前端 {m_fe.group('s')!r} ≠ 后端 {m_be.group('s')!r}")
    for name in ("act/act.js", "panel/board.js"):
        body = _strip_comments(_read(name))
        assert "PLAN_CANCELLED_FALLBACK_ZH" in body, f"{name} 应消费该常量"
        assert m_fe.group("s") not in body, f"{name} 又留了兜底句字面量"


def test_flow_verb_label_matches_action_plan_zh():
    """flow_trace.FLOW_VERB_LABEL 对拍门：非流式合成行的展示名必须逐键等于
    action_plan.VERB_BY_NAME[verb].zh（动作中文名唯一真源），键集 = LOOP_TOOLS
    全部动词减 route.request（ROUTE 类不上工具行）——前端不手抄第二份名单，
    改 verb 中文后两处不齐即红。"""
    from dataset_recommender.agent.agent_exec import LOOP_TOOLS

    src = _read("core/flow_trace.js")
    m = re.search(r"FLOW_VERB_LABEL\s*=\s*\{(?P<body>.*?)\};", src, re.S)
    assert m, "flow_trace.js 缺 FLOW_VERB_LABEL"
    pairs = re.findall(r'"([^"]+)":\s*"([^"]+)"', m.group("body"))
    labels = dict(pairs)
    assert len(pairs) == len(labels), "FLOW_VERB_LABEL 出现重复键"

    expected = {v: AP.VERB_BY_NAME[v].zh for v in LOOP_TOOLS if v != "route.request"}
    assert set(labels) == set(expected), (
        f"键集漂移：多 {set(labels) - set(expected)} 少 {set(expected) - set(labels)}")
    for verb, zh in expected.items():
        assert labels[verb] == zh, (
            f"FLOW_VERB_LABEL[{verb}] = {labels[verb]!r} ≠ 唯一真源 {zh!r}")
