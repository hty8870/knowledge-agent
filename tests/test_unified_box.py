# -*- coding: utf-8 -*-
"""统一对话窗口（微信式输入行）的前端接线门 + **真行为**门（后者在 node 里跑真函数）。

钉的是这条链路：首屏 #queryInput / 结果态侧栏 #chatInput（微信式、发送即清空）→ ubSubmit 问 `/api/utterance`
（turn pipeline：「AI 执行」闸 → 规则检索直达 / LLM 分流，唯一路由脑）→ 按 route 三档分发：
search→runRecommend 既有流（effective_query 改写如实回显；对话窗来的 keepConv 保对话、
sayPushed 不重复推泡）；tool→经共享规范链派发返回的 EXEC plan 与 pending_frontend（不再二次调 /api/action/plan；
curate.* 不需结果、直派——起全自动化：runner 链式 plan（零写盘）→ apply
回传 confirm_token 直接执行，审计账本 + 回收站可回退兜底）；
none→如实回音（needs_agent 档渲染成降级气泡、带「去开启 AI 执行」指路按钮），
**绝不退回 runRecommend 静默全库检索**。

三门都不执行 JS（web_smoke 只静态查字符串、node --check 只验语法），所以「cancelled 不触发
任何面板」必须用 node 真行为门钉住：cancelled 的 plan 若被派发，runner 在 node 里会当场
ReferenceError（DOM/网络全不存在），返回 reason_zh 本身才证明执行层没有碰它。
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
    """只留代码。**注释里写了什么不算数**（与 test_act_frontend.py 同款去注释器）。"""
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", out)


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
    """与 test_act_frontend.py 同款的 node 真行为门：脚本落临时文件再跑（WinError 206 教训）。"""
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过统一框真行为门（full 质量门的语法检查环节必有 node）。")
    # 后缀必须显式给：quality_gate 把临时目录指进仓库（outputs/），而根 package.json
    # 是 "type": "module"——落在仓库树里的 .js 会被 node 当 ESM，require 当场 undefined。
    # act.js 本身是 ESM：它的门用 .mjs（import 真模块）；其余嵌板脚本维持 .cjs。
    fd, script_path = tempfile.mkstemp(suffix=suffix, prefix="biodata_ub_gate_")
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


#: 在 node 里 import 真 act.js 所需的最小宿主桩（act.js 是 ESM，不能再文本拼接进 CJS）。
#: act 的 import 链拉进 core/task_pack/board/search/results 等——core 在模块顶层读
#: window.matchMedia、早期模块末尾的 window 桥接写 window 并被它们的裸引用经全局作用域读取
#: （浏览器里 window 就是全局对象）。所以桩 window = globalThis 本物：桥上挂的名字才成为真全局。
#: localStorage 供 readJSON 兜底。桩完再动态 import（静态 import 会提升、先于赋值执行）。
_ACT_ESM_PRELUDE = (
    "globalThis.window = globalThis;\n"
    "const _els = new Map();\n"
    "function _el(id) {\n"
    "    if (!_els.has(id)) {\n"
    "        _els.set(id, {\n"
    "            id, value: \"\", checked: false, hidden: false, disabled: false,\n"
    "            innerHTML: \"\", textContent: \"\", style: {}, dataset: {},\n"
    "            classList: { add() {}, remove() {}, toggle() {}, contains: () => false },\n"
    "            addEventListener() {}, setAttribute() {}, scrollIntoView() {},\n"
    "        });\n"
    "    }\n"
    "    return _els.get(id);\n"
    "}\n"
    "globalThis.document = {\n"
    "    getElementById: (id) => _el(id),\n"
    "    querySelector: () => null,\n"
    "    body: { classList: { add() {}, remove() {}, toggle() {}, contains: () => false } },\n"
    "};\n"
    "const _store = new Map();\n"
    "globalThis.localStorage = {\n"
    "    getItem: (k) => (_store.has(k) ? _store.get(k) : null),\n"
    "    setItem: (k, v) => { _store.set(k, String(v)); },\n"
    "    removeItem: (k) => { _store.delete(k); },\n"
    "};\n"
    f'const ns = await import("{(STATIC / "js" / "act" / "act.js").as_uri()}");\n'
)


BOARD = _read("panel/board.js")
ACT = _read("act/act.js")
CORE = _read("core/core.js")
INTERACTIONS = _read("core/interactions.js")
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 端点集中声明

def test_utterance_endpoint_lives_in_the_core_api_dict():
    """端点地址集中声明在 core.js 的 API 常量里；board.js 不许再手写一遍。"""
    assert "utterance:" in CORE and '"/api/utterance"' in CORE
    assert BOARD.count('"/api/utterance"') == 0, "board.js 里不该再手写一遍端点地址"
    assert "API.utterance" in BOARD
    # turn pipeline 起前端不再直接调 /api/action/plan——一切规划都过 /api/utterance，
    # 常量表/任何 JS 里都不许留第二规划入口（留了就是二次规划回潮的邀请）。
    assert "actionPlan:" not in CORE
    assert "API.actionPlan" not in BOARD and "API.actionPlan" not in ACT


# ---------------------------------------------------------------- 提交链路与五档分发

def test_hand_typed_submits_go_through_the_unified_router():
    """回车与检索按钮这两条「用户亲手提交」路径都走 ubSubmit；userSubmit 旧通道随之退役。"""
    assert INTERACTIONS.count("ubSubmit()") == 2, "回车 / 检索按钮两条路径都要接 ubSubmit"
    assert "userSubmit" not in INTERACTIONS, "统一框不再有绕过路由的 userSubmit 提交"
    assert re.search(r"\basync function\s+ubSubmit\b", BOARD)
    # 手写 IME/Shift 守卫照搬既有：中文输入法组词中的回车绝不提交
    assert "isComposing" in INTERACTIONS and "shiftKey" in INTERACTIONS


def test_ubsubmit_has_its_own_inflight_gate_and_seq():
    """四类并发代号纪律：_ubSeq/_ubBusy 独立存在，在途闸不绑死 submitBtn。"""
    assert re.search(r"\blet\s+_ubSeq\s*=\s*0", BOARD)
    assert re.search(r"\blet\s+_ubBusy\s*=\s*false", BOARD)
    assert re.search(r"\blet\s+_recSeq\b", _read("search/search.js"))
    assert re.search(r"\blet\s+_cbSeq\b", BOARD)
    # _curateSeq 退役；管护并发由「_actBusy 串行闸 + runner 链式直推」单闸接管
    # （问卷单例随全自动化一并退役——不再有第二份确认可并发）。
    assert re.search(r"\blet\s+_actBusy\b", ACT)
    assert not (STATIC / "js" / "survey.js").exists(), "问卷已随全自动化退役"
    body = re.search(r"async function ubSubmit\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body and "_ubBusy" in body.group(1) and "_ubSeq" in body.group(1)


def test_route_body_is_self_contained_from_the_frame_stack():
    """body 自包含：utterance + 分流现场（has_results / 当前查询 / 当前条件 / 来源池）自 _cbStack 帧与配置取。"""
    body = re.search(r"function ubRouteBody\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubRouteBody"
    code = body.group(1)
    assert "cbFrameData(" in code, "有无结果/当前条件的真源是 _cbStack 帧"
    assert "cbFrameQuery()" in code, "refine 改写要带上产生当前结果的那句话"
    assert "has_results" in code and "result_total" in code and "current_filters" in code


def test_all_three_routes_are_dispatched():
    body = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubDispatch"
    code = body.group(1)
    for route in ('"search"', '"tool"'):
        assert f"route === {route}" in code, f"ubDispatch 缺 {route} 档"
    assert "echo_zh" in code, "none 档必须如实回音 echo_zh"
    # search → runRecommend 既有流；tool → 专用分发；none → 如实回音（needs_agent 档为降级气泡）
    assert "runRecommend" in code
    assert "ubDispatchAction(" in code
    # search 档不再带 userSubmit：路由已判定无执行诉求，actAfterSearch 再判一次是白调
    assert "userSubmit" not in BOARD


def test_chat_search_keeps_the_conversation_and_echoes_rewrites():
    """对话窗来的 search 档：**对话记录保留**（keepConv）+ 原话 say 不重复推（sayPushed）
    + LLM 改写的检索句如实回显——用户报的「聊天记录丢了」正是旧路在 chat 里 runRecommend
    默认 cbLogClear 清掉的。"""
    body = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubDispatch"
    code = _strip_comments(body.group(1))
    search = code[code.index('route === "search"'):code.index('route === "tool"')]
    assert "keepConv: true" in search, "chat 来源的检索必须保对话"
    assert "sayPushed: true" in search, "原话 say 已在 ubSubmit 上屏，不许重复推泡"
    # 改写回显的 sys 留痕随落地逻辑收进共享应用函数 _applyBatchDecision。
    apply_fn = re.search(r"function _applyBatchDecision\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert apply_fn, "找不到 _applyBatchDecision"
    apply_code = _strip_comments(apply_fn.group(1))
    assert 'cbLogPush("sys"' in apply_code, "effective_query 改写必须如实回显（经 _applyBatchDecision 落地留痕）"


def test_zero_result_frame_does_not_block_a_search_route():
    """零结果帧不许把检索指令吞掉（同型坑）：

    「去掉小鼠」正是逃出零结果死局的那一步——turn pipeline 里它由 LLM 判成
    refine.conditions 并给出 effective_query，ubDispatch 的 search 档**无条件**拿去
    runRecommend（闸不在帧里有没有结果，改写句拿去重搜就是逃生门）。"""
    body = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubDispatch"
    code = _strip_comments(body.group(1))
    search = code[code.index('route === "search"'):code.index('route === "tool"')]
    assert "runRecommend" in search
    assert "(data.results || []).length" not in search, \
        "search 档不得按结果条数设闸——零结果帧也要能重搜（去掉一个条件正是逃出死局的路）"


def test_route_kind_none_echoes_back_never_silent_full_search():
    """verb=none（没听懂）必须如实回音，**绝不退回 runRecommend 静默全库检索**
    （零检索信号的歧义句被静默搜全库、连一句回音都没有，正是这次修掉的缺陷）。
    turn pipeline 起 none 由 ubDispatch 的兜底分支处理（tool 档只接 EXEC plan）；
    起 needs_agent 档原样透传（降级气泡由 cbLogPush/cbRenderHistory 承接）。"""
    body = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubDispatch"
    code = _strip_comments(body.group(1))
    # none 分支 = 两档之后的兜底：sys 回音（needs_agent 透传）+ 收表，不许有 runRecommend
    tail = code[code.index('route === "tool"'):]
    tail_after_dispatch = tail[tail.index("return;"):]
    assert "runRecommend" not in tail_after_dispatch, "none 分支绝不许静默全库检索"
    assert 'cbLogPush("sys"' in tail_after_dispatch and "needs_agent" in tail_after_dispatch
    assert "resetSubmitButton()" in tail_after_dispatch
    # tool 档本身只接 EXEC：词表漂移时如实回音，不瞎做
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub and 'plan.kind !== "exec"' in ub.group(1)


def test_search_first_dispatch_reuses_the_plan_without_a_second_plan_call():
    """action + 屏上没结果 → 先按原话检索，落地后派发**这份已拿到的** plan（actPlan 档）。"""
    body = re.search(r"function actAfterSearch\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "找不到 actAfterSearch"
    assert "actPlan" in body.group(1) and "actDispatchPlanChain(stashed, said)" in body.group(1)
    # 派发时结果闸按屏上真实状态复算，不照抄规划时的 blocked_reason（检索落地后旧态已失效）
    dispatch = re.search(r"async function actDispatchPlan\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert dispatch, "找不到 actDispatchPlan"
    assert "blocked_reason" not in dispatch.group(1), "结果闸必须复算（requires_results + 实时 hasResults）"
    assert "requires_results" in dispatch.group(1)


def test_canonical_dispatch_chain_deduplicates_root_from_pending_frontend():
    """P1 回归：顶层动作与 pending_frontend 同 verb+slots 时只派一次，异参尾巴保留。"""
    payload = {
        "verb": "pack.download", "kind": "exec", "slots": {"limit": 2, "uids": ["u1", "u2"]},
        "pending_frontend": [
            {"verb": "pack.download", "kind": "exec", "slots": {"uids": ["u1", "u2"], "limit": 2}},
            {"verb": "reuse.pack", "kind": "exec", "slots": {"format": "full"}},
            {"verb": "pack.download", "kind": "exec", "slots": {"limit": 3, "uids": ["u1", "u2"]}},
            {"verb": "reuse.pack", "kind": "exec", "cancelled": True, "slots": {"format": "full"}},
        ],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => ({ verb: x.verb, slots: x.slots }))));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == [
        {"verb": "pack.download", "slots": {"limit": 2, "uids": ["u1", "u2"]}},
        {"verb": "reuse.pack", "slots": {"format": "full"}},
        {"verb": "pack.download", "slots": {"limit": 3, "uids": ["u1", "u2"]}},
    ]


def test_canonical_dispatch_chain_mixed_turn_steps_do_not_mask_exec_verb():
    """混合轮（检索+执行）形态钉：顶层 verb=pack.download、steps 只记环内 rank/rerank、
    pending_frontend 与顶层同动作 → 规范清单输出单个 pack.download。
    steps 是「后端已在图内执行」的记录，绝不能把它当派发清单（双泡 bug 的根因形态）。"""
    payload = {
        "verb": "pack.download", "kind": "exec", "slots": {"limit": 5},
        "steps": [{"verb": "rank"}, {"verb": "rerank"}],
        "pending_frontend": [
            {"verb": "pack.download", "kind": "exec", "slots": {"limit": 5}},
        ],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => ({ verb: x.verb, slots: x.slots }))));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == [{"verb": "pack.download", "slots": {"limit": 5}}]


def test_canonical_dispatch_chain_real_mixed_turn_filters_graph_done_rank():
    """真实混合轮形态钉（2026-09-03 真机 route_turn 核实）：route=tool 收尾时顶层 plan
    就是环内已执行的 rank（steps 记 ok:true），真身动作排在 pending_frontend。
    规范清单必须只输出 [pack.download]——rank 若被派发，会经「图内已执行渲染通道」
    再产一颗 actFinish 泡，与 pack.download 的总结泡构成双泡（用户截图投诉的真根因）。"""
    payload = {
        "verb": "rank", "kind": "exec",
        "steps": [{"verb": "rank", "ok": True}],
        "pending_frontend": [
            {"verb": "pack.download", "kind": "exec", "slots": {"limit": 5, "target": "results"}},
        ],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => ({ verb: x.verb, slots: x.slots }))));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == [{"verb": "pack.download", "slots": {"limit": 5, "target": "results"}}]


def test_canonical_dispatch_chain_pure_search_turn_dispatches_nothing():
    """纯检索轮（route=tool、plan=rank 且图内已执行、无 pending）→ 规范清单为空：
    检索回执由 board 批次落地通道负责（唯一气泡），act 层不派发、无 actFinish。"""
    payload = {
        "verb": "rank", "kind": "exec",
        "steps": [{"verb": "rank", "ok": True}, {"verb": "rerank", "ok": True}],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => x.verb)));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == []


def test_canonical_dispatch_chain_pause_turn_returns_head_for_its_receipt():
    """ask 暂停回合形态钉（2026-09-14 真机核实）：route=tool、无 result_payload、顶层 plan 是
    环内已跑完的 rank、环内还跑了 ask.relax（非检索步）、带确定性 report_zh、无 pending。
    批次回执通道没跑过（node 里 cbExecReceiptCovered 恒初始 false）→ 规范清单必须交回头部，
    让「图内已执行渲染通道」收进度泡 + 落 report_zh；否则屏上永远停在「规划中…」、汇报丢失。
    对照上面的纯检索钉（同形态但无 report_zh → 仍为空）：只有真有汇报要落地的回合才回本通道。
    回执已被结果批覆盖的回合（cbExecReceiptCovered=true）仍滤除头部——双泡防线不变。"""
    payload = {
        "verb": "rank", "kind": "exec",
        "report_zh": "三条件同时满足为 0 条，条件过严。选完我会按你的选择自动重跑检索并继续。",
        "report_source": "deterministic",
        "steps": [{"verb": "rank", "ok": True, "readonly": True, "card_kind": "rank"},
                  {"verb": "ask.relax", "ok": True, "readonly": True, "card_kind": "ask_relax"}],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => x.verb)));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == ["rank"]


def test_canonical_dispatch_chain_keeps_graph_done_non_retrieval_verb():
    """守护钉：图内已执行的**非检索**动作（compare 等）不被过滤——它的卡片与总结
    只能靠 actDispatchPlan 的「图内已执行渲染通道」上屏，过滤了就是整个总结消失。"""
    payload = {
        "verb": "compare", "kind": "exec",
        "steps": [{"verb": "compare", "ok": True, "card_kind": "compare"}],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => x.verb)));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == ["compare"]


def test_canonical_dispatch_chain_keeps_failed_retrieval_step():
    """守护钉：图内检索**失败**（ok 非 true）不过滤——失败步要走渲染通道如实汇报，
    只有 ok:true 的成功检索才视作「已落地、回执归批次通道」。"""
    payload = {
        "verb": "rank", "kind": "exec",
        "steps": [{"verb": "rank", "ok": False}],
        "pending_frontend": [
            {"verb": "pack.download", "kind": "exec", "slots": {"limit": 5}},
        ],
    }
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + 'const p = JSON.parse(readFileSync(0, "utf-8"));\n'
        + "const out = ns.actCanonicalDispatchPlans(p);\n"
        + "console.log(JSON.stringify(out.map((x) => x.verb)));\n"
    )
    out = _run_node(script, payload, suffix=".mjs")
    assert out == ["rank", "pack.download"]


def test_act_after_search_dispatches_pending_frontend_through_shared_chain():
    """P1 回归：检索落地后把 stashed 的顶层动作与 pending_frontend 一并交给共享链。"""
    after = _strip_comments(
        re.search(r"function actAfterSearch\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S).group(1))
    assert "actDispatchPlanChain(stashed, said)" in after
    assert "stashed.intents" not in after and "stashed.pending_frontend" not in after, (
        "actAfterSearch 不得再自行拼清单；共享链才是 pending_frontend 的唯一消费机制")
    board = _strip_comments(
        re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S).group(1))
    assert board.count("actDispatchPlanChain(plan, said)") == 1
    assert "const _tails" not in board and "actDispatchPlanList(" not in board


# ---------------------------------------------------------------- cancelled：只回音，不打开任何面板

def test_cancelled_short_circuits_before_the_runner_table():
    """取消态的检查必须**早于**派发表查表与 busy 闸——结构上不可能碰到 runner。"""
    body = re.search(r"async function actDispatchPlan\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "找不到 actDispatchPlan"
    code = _strip_comments(body.group(1))
    i_cancel = code.index("plan.cancelled")
    assert "reason_zh" in code, "取消态必须把后端 reason_zh 原样交回"
    assert i_cancel < code.index("ACT_RUNNERS["), "cancelled 检查必须早于派发表查表"
    # 统一框的 action 分发也要有取消档：只在对话流里回音
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub and "plan.cancelled" in ub.group(1) and "reason_zh" in ub.group(1)
    assert 'cbLogPush("sys"' in BOARD, "取消/路由回音必须渲染进对话记录（sys 气泡）"


def test_a_cancelled_plan_never_reaches_a_panel_or_receipt():
    """node 真行为门：cancelled 的 plan 过 actDispatchPlan 只能拿回 reason_zh。

    在 node 里 import 真 act.js（ESM）跑真函数。不变量不变：若执行层碰了 runner /
    actPush / 面板，返回值就不再是 reason_zh 本身——runner 走出去的任何一步
    （预览/产包/打开面板）都会改变产出或当场抛错，断言立刻红。
    逐个 EXEC 动词过一遍（词表新加动词时一并覆盖）。"""
    payloads = [
        {"verb": verb, "kind": "exec", "cancelled": True, "reason_zh": "你说的是「别」做这一步，所以这次没有执行。"}
        for verb in AP.EXEC_VERBS
    ]
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        + "Promise.all(_in.map((p) => ns.actDispatchPlan(p, '别做了'))).then((out) => console.log(JSON.stringify(out)));\n"
    )
    out = _run_node(script, payloads, suffix=".mjs")
    for verb, note in zip(AP.EXEC_VERBS, out):
        assert note == "你说的是「别」做这一步，所以这次没有执行。", (verb, note)
        assert "已" not in note


def test_kind_route_plans_are_not_executed_either():
    """kind=route 的 plan（none / search.new / refine.conditions / lookup.identifier）交回调用方指路。"""
    payloads = [{"verb": v, "kind": "route", "cancelled": False} for v in AP.ROUTE_VERBS]
    script = (
        _ACT_ESM_PRELUDE
        + 'import { readFileSync } from "node:fs";\n'
        + "const _in = JSON.parse(readFileSync(0, \"utf-8\"));\n"
        + "Promise.all(_in.map((p) => ns.actDispatchPlan(p, '随便一句'))).then((out) => console.log(JSON.stringify(out)));\n"
    )
    out = _run_node(script, payloads, suffix=".mjs")
    assert out == [False] * len(payloads), out


# ---------------------------------------------------------------- 对话流呈现（sys 回音 + 降级气泡）

def test_sys_echo_is_a_first_class_log_kind():
    """sys 进 cbLogPush / cbRenderHistory / cbRestoreConversation 三处——漏一处就会在
    历史回看时被降级成「细化」消息（kind 归并的静默失真）。"""
    assert re.search(r'kind === "say" \|\| kind === "sys"', BOARD), "cbLogPush 没有收 sys"
    assert "cbh-sys" in BOARD and "cbh-sys" in CSS, "sys 气泡的渲染与样式要在两端同时在"
    restore = re.search(r"function cbRestoreConversation\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert restore and '"sys"' in restore.group(1), "历史回看时 sys 不许被归并成 refine"


def test_needs_agent_echo_renders_the_degradation_bubble():
    """降级气泡：「AI 执行」关 + 规则检出操作指令 → sys 回音带
    needsAgent 标记，渲染成 accent 浅底气泡 + 「去开启 AI 执行」指路按钮（点击打开设置）。"""
    assert "needsAgent" in BOARD, "cbLogPush 必须收 needsAgent 选项"
    assert "cbh-agent-bubble" in BOARD and "cbh-agent-bubble" in CSS, "降级气泡的渲染与样式两端同时在"
    assert "cbh-agent-cta" in BOARD and "cbh-agent-cta" in CSS
    click = re.search(r"function cbHistoryClick\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert click and "data-cbh-settings" in click.group(1) and "openSettings()" in click.group(1)


# ---------------------------------------------------------------- 开关语义三态（node 真行为门）

#: 与 _ACT_ESM_PRELUDE 同一套 DOM/localStorage 桩，改 import shell.js（门控逻辑属主）。
_SHELL_ESM_PRELUDE = (
    _ACT_ESM_PRELUDE.rsplit("const ns = await import", 1)[0]
    + f'const ns = await import("{(STATIC / "js" / "core" / "shell.js").as_uri()}");\n'
)


def test_llm_gate_three_states_in_node():
    """开关语义统一（根因修复）——真跑 shell.js 的 llmCapable/aiGateChange：
    ① 未配 key（mock 或无任何 key）→ 禁点：点到开也弹回；
    ② 已配 key（会话 key 或服务端 key）→ 必可开关；
    ③ 服务端 key 只在「接入方式与接口地址同服务端一致」时适用（自定义地址后不再适用）。
    """
    script = (
        _SHELL_ESM_PRELUDE
        + "const $ = (id) => document.getElementById(id);\n"
        + "const out = {};\n"
        + '$("cfgProvider").value = "mock";\n'
        + "out.mockCapable = ns.llmCapable();\n"
        + '$("cfgProvider").value = "deepseek"; $("cfgApiKey").value = "sk-session";\n'
        + "out.sessionKeyCapable = ns.llmCapable();\n"
        + '$("cfgApiKey").value = "";\n'
        + "globalThis.fetch = async () => ({ json: async () => ({ ok: true,"
        + ' llm_server: { key_detected: true, provider: "openai-compatible", base_url: "https://api.deepseek.com" },'
        + " extensions: { agent: true } }) });\n"
        + "await ns.syncAgentAvailability();\n"
        + "out.serverKeyCapable = ns.llmCapable();\n"
        + '$("cfgBaseUrl").value = "https://other.example/v1";\n'
        + "out.customBaseCapable = ns.llmCapable();\n"
        + '$("cfgBaseUrl").value = ""; $("cfgProvider").value = "mock";\n'
        + 'const box = $("cfgAgentExec");\n'
        + "box.checked = true; ns.aiGateChange(box);\n"
        + "out.revertedWhenUncapable = box.checked === false;\n"
        + '$("cfgProvider").value = "deepseek"; $("cfgApiKey").value = "sk-session";\n'
        + "box.checked = true; ns.aiGateChange(box);\n"
        + "out.keptWhenCapable = box.checked === true;\n"
        + "console.log(JSON.stringify(out));\n"
    )
    out = _run_node(script, None, suffix=".mjs")
    assert out == {
        "mockCapable": False,            # 本地演示不含大模型
        "sessionKeyCapable": True,       # 会话 key → 必可开
        "serverKeyCapable": True,        # 服务端 key（同接入方式同地址）→ 必可开
        "customBaseCapable": False,      # 自定义地址后服务端 key 不再适用（前后端同约）
        "revertedWhenUncapable": True,   # 未配 key 禁点：点到开也弹回
        "keptWhenCapable": True,         # 已配 key 必可开关
    }, out


# ---------------------------------------------------------------- cbInput 退役与挂点存在性

def test_the_second_input_box_is_retired_from_the_dom():
    for gone in ('id="cbInput"', 'id="cbSubmitBtn"', 'id="cbComposer"'):
        assert gone not in HTML, f"index.html 还留着已退役的 {gone}"
    assert 'id="cbInput"' not in BOARD and '$("cbInput")' not in BOARD
    assert 'id="cbSubmitBtn"' not in BOARD and '$("cbSubmitBtn")' not in BOARD
    assert "cbComposerGrow" not in BOARD, "退役对话框的自增高也该一起删"
    # 退役后 bind 不再引用已删除节点（没了就是没了，靠本门钉住不许回潮）
    init = re.search(r"function initCondBoard\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert init and "cbInput" not in init.group(1) and "cbSubmitBtn" not in init.group(1)


def test_wechat_composer_is_the_results_state_input():
    """布局定稿：结果态主区**没有输入框**（hero console 只在桌面+侧栏开着时隐藏，
    侧栏收起/移动端保留）；唯一输入入口是侧栏工作卡最下方的微信式输入行（发送即清空、默认为空）。"""
    # 输入行 DOM 三件套：容器 / 文本框 / 发送键
    for token in ('id="chatComposer"', 'id="chatInput"', 'id="chatSendBtn"'):
        assert token in HTML, f"index.html 缺少 {token}"
    # hero console 的隐藏必须带双重守卫（min-width + :not(.side-closed)）：移动端/收起侧栏时它是唯一入口
    assert re.search(r"@media \(min-width: 781px\)", CSS)
    assert re.search(r"body:not\(\.side-closed\).*has-results.*\.console", CSS), \
        "结果态隐藏主 console 的规则缺席或丢了守卫"
    # 输入行接进同一条 ubSubmit 路由（来源参数 "chat"），发送即清空
    assert INTERACTIONS.count('ubSubmit("chat")') == 2, "回车 / 发送键两条路径都要接 ubSubmit(\"chat\")"
    body = re.search(r"async function ubSubmit\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubSubmit"
    code = body.group(1)
    assert 'source === "chat"' in code and 'box.value = ""' in code, \
        "chat 来源必须：读 #chatInput、发送即清空（微信式默认为空）"
    assert '"queryInput"' in code, "说过的话要写进 #queryInput——它是「当前检索句」的唯一状态真源"


def test_the_log_lives_in_the_sidebar_never_in_main_results():
    """桌面 + 侧栏开着时主区**永不再有对话界面**——#cbHistory 静态家就是
    侧栏 #sideBoardScroll；仅侧栏收起/移动端回退 hero（那时主 console 仍可见，对话跟着输入框走）。"""
    assert 'id="cbHistory"' in HTML
    scroll = re.search(r'<div class="sw-board-scroll" id="sideBoardScroll"[^>]*>(.*?)</div>\s*</div>\s*<!--', HTML, re.S)
    assert scroll and 'id="cbHistory"' in scroll.group(1), "#cbHistory 的静态家必须是 #sideBoardScroll"
    assert "cbh-main" in CSS, "hero 回退态（侧栏收起/移动端）要有自己的滚动样式"
    assert re.search(r"\bfunction\s+placeChatLog\b", BOARD), "对话记录落位函数不存在"
    pl = re.search(r"function placeChatLog\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert pl and '$("sideBoardScroll")' in pl.group(1), "侧栏开着的唯一家是 #sideBoardScroll"
    sw = re.search(r"function swApplyMode\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert sw and "placeChatLog()" in sw.group(1), "落位必须挂在 swApplyMode（与条件板/步进条同触发点）"


def test_sidebar_stays_put_during_a_search_and_animates_in_afterwards():
    """检索**进行中**侧栏工作卡不许在落地页提前弹出——只有 say 不够格，
    须 condBoard / has-results / sys 回音三者之一；落地后整卡带入场动画（reduced-motion 关闭）。"""
    avail = re.search(r"function swAvailable\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert avail, "找不到 swAvailable"
    code = avail.group(1)
    assert "has-results" in code and 'kind === "sys"' in code, \
        "对话记录页签够格条件必须是 condBoard/has-results/sys 三者，只有 say 不许弹出"
    assert re.search(r"\.side-work:not\(\[hidden\]\)\s*\{[^}]*animation:\s*swIn", CSS), "入场动画缺席"
    assert re.search(r"@media \(prefers-reduced-motion: reduce\)[^{]*\{[^}]*\.side-work:not\(\[hidden\]\)[^}]*animation:\s*none", CSS), \
        "reduced-motion 下动画必须关闭"


def test_research_fix_chip_retired_but_keep_conv_survives():
    """keepConv 机制仍在（runRecommend 的 keepConv 档：分面/抑制照清，
    cbLogClear 不调、对话记录保留）；但回执 chip「按原话重新检索」已退役（agent 能力已足够）
    ——act.js 不再有 research 分支，接线方只剩 board 路径。"""
    assert 'what === "research"' not in ACT, "research 分支应随 chip 一并退役"
    assert 'data-act-fix="research"' not in ACT, "research chip 应已删除"
    body = re.search(r"async function runRecommend\([^)]*\)\s*\{(.*?)\n\}", _read("search/search.js"), re.S)
    assert body and "opts.keepConv" in body.group(1) and "cbLogClear" in body.group(1), \
        "runRecommend 必须有 keepConv 档（跳过 cbLogClear、仍 push say）"
    assert "keepConv" in BOARD, "keepConv 的现存接线方在 board 路径（对话窗/主框来的检索指令保对话）"


def test_send_button_is_a_paper_plane():
    """图3 保真：发送键是纸飞机（不是上箭头）。"""
    assert "m22 2-7 20-4-9-9-4z" in HTML, "纸飞机图标缺席（图3 保真）"


def test_undo_tree_and_revert_survive_the_retirement():
    """页签保留为历史/帧列表：帧栈、查看历史回复、分支/回退全都要在（撤销/重做按钮退役，
    游标移动只有「查看历史回复」气泡按钮一个入口；回栈顶用「回到最新」）。"""
    for sym in ("cbPushCurrent", "cbReplay", "cbViewFrame", "cbRevertToFrame", "cbToLatest"):
        assert re.search(r"\bfunction\s+" + sym + r"\b", BOARD), f"{sym} 不存在"
    for gone in ("cbUndoBtn", "cbRedoBtn", "cbSteps", "placeSteps"):
        assert gone not in BOARD and gone not in HTML, f"撤销/重做残留：{gone}"
    for token in ('id="scopePop"', 'id="scopeChip"', 'id="cbForkBar"', 'id="cbTopBtn"', 'id="cbBranchBtn"', 'id="cbRevertBtn"'):
        assert token in HTML, f"index.html 缺少 {token}"


# ---------------------------------------------------------------- curate.restore 接线

def test_curate_restore_has_a_named_runner_and_direct_flow():
    """restore 在派发表里指向一个**真实存在**的 runner；全自动化直推
    （list 回收站 → slots.target 子串定位（多命中/零命中如实说）→ plan 预览 → apply 回传
    confirm_token；同名冲突照实拒绝，不给注定失败的执行）。"""
    table = re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S)
    assert table, "找不到 ACT_RUNNERS"
    m = re.search(r'"curate\.restore":\s*(\w+)', table.group(1))
    assert m, "curate.restore 不在派发表里"
    assert m.group(1) == "actRunCurateRestore"
    body = re.search(r"async function actRunCurateRestore\([^)]*\)\s*\{(.*?)\n\}", ACT, re.S)
    assert body, "actRunCurateRestore 不存在"
    code = _strip_comments(body.group(1))
    assert "surveyRun(" not in code, " 全自动化：restore 不再开问卷"
    assert 'action: "restore"' in code and "confirm_token" in code, "restore 的 apply 没有回传 confirm_token"
    assert "will_conflict" in code and "为避免覆盖" in code, "同名冲突时必须照实拒绝"
    assert "actMatchFile" in code, "对象定位必须与 remove 同一条子串匹配真源"


def test_act_match_file_zero_hits_falls_through_to_the_listing_branch():
    """target 非空但零命中时 actMatchFile 曾返回
    { many: [] }——空数组是真值，remove/restore 的 if (m.many) 截胡，报出自相矛盾的
    「对上了 0 个文件——说具体一点」，真正有用的「没有名字含「x」的文件。现有：…」分支（!m.one）
    在此输入下不可达。钉两头：源码层 many 只许在非空时返回；行为层在 node 里真跑 actMatchFile
    验四种分支（无 target / 唯一命中 / 多命中 / 零命中）。"""
    fn = re.search(r"function actMatchFile\(target, files, keyOf\) \{.*?\n\}", ACT, re.S)
    assert fn, "找不到 actMatchFile"
    code = _strip_comments(fn.group(0))
    assert "if (hits.length) return { many: hits };" in code, (
        "零命中不许返回 { many: [] }——空数组真值会截胡消费方的 if (m.many) 多命中分支"
    )
    assert ACT.count("现有：") >= 2, "remove/restore 的「现有：…」清单分支必须在（零命中的落点）"
    script = (
        "const src = JSON.parse(require(\"fs\").readFileSync(0, \"utf-8\"));\n"
        "eval(src);\n"
        "const files = [{ filename: \"a.json\" }, { filename: \"ab.json\" }];\n"
        "const keyOf = (f) => f.filename;\n"
        "const out = {\n"
        "    noTarget: actMatchFile(\"\", files, keyOf),\n"
        "    one: actMatchFile(\"a.j\", files, keyOf),\n"
        "    many: actMatchFile(\"a\", files, keyOf),\n"
        "    zero: actMatchFile(\"zzz\", files, keyOf),\n"
        "};\n"
        "console.log(JSON.stringify(out));\n"
    )
    out = _run_node(script, code)
    assert out["noTarget"] == {"none": True}
    assert out["one"] == {"one": {"filename": "a.json"}}
    assert [f["filename"] for f in out["many"]["many"]] == ["a.json", "ab.json"]
    zero = out["zero"]
    assert not zero.get("many") and not zero.get("one"), (
        f"零命中必须落到消费方的 !m.one 分支（如实列出现有清单），实得：{zero}"
    )


def test_curate_verbs_dispatch_directly_without_a_search_detour():
    """直派门：curate.*（requires_results=false，作用对象是本地语料库）**直派**——
    不许走「先按原话检索」绕行（管护句在关键词阶段被毙掉、永远到不了执行层，正是这次修掉的缺陷）；
    起**全自动化**：派发后 runner 链式 plan（零写盘预览）→ apply（回传
    confirm_token）直接执行，审计落账 + 删除走回收站可回退——不再停在确认面板等人点。"""
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub, "找不到 ubDispatchAction"
    code = _strip_comments(ub.group(1))
    assert '"curate.")' in code or '"curate."' in code, "curate 直派分支不存在"
    # 「先检索后派发」的闸门必须带 requires_results：不需要结果的动词不许被拿去先搜一遍。
    # 枚举清单在场时任一执行项 requires_results 同样触发——
    # 条件仍是 requires_results 专属（curate.* 恒 false、清单项同为护栏产物，语义不变）。
    gate = re.search(
        r"if \(!hasResults && \(plan\.requires_results \|\|.*?"
        r"_activeIntents\.some\(function \(p\) \{ return !!p\.requires_results; \}\)\)\)",
        code, re.S)
    assert gate, "先检索闸门必须是 requires_results 专属（含枚举清单的逐项判据）"
    # curate 绕过 actEnabled 的判据要在；「结果区露脸」分支已退役（行动流与总结长在对话流，
    # 结果区不再为执行面板提前露头）——这里反向钉死它不得回来。
    assert "isCurate" in code and "actEnabled()" in code
    assert "enterResultsLayout" not in code, "直派不该再为面板露结果区（行动流长在对话流里）"
    assert "resultsWrap" not in code


def test_dispatch_realigns_the_live_response_with_the_frame_before_acting():
    """统一框特有的坑（board.js 顶部记过的那一个）：在唯一输入框里打字，onQueryInput 就把
    LAST_RECOMMEND_DATA 置成 null（那是给解释预览的失效信号）——而屏上渲染的还是当前帧那批结果。
    派发层（actDispatchPlan 的结果闸 / actResultItems）与任务包都读 LAST_RECOMMEND_DATA，
    不对齐回当前帧，「打包这批」会被误判成「屏上还没有检索结果」——旧继续对话框没这个坑，
    正因为打字发生在另一个框里。tool 档在派发前必须从 _cbStack 帧把它对齐回来。"""
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub, "找不到 ubDispatchAction"
    code = ub.group(1)
    assert "cbFrameData(" in code, "派发前必须读 _cbStack 帧（屏幕真源），不是会被按键清空的全局"
    i_frame = code.index("cbFrameData(")
    # LAST_RECOMMEND_DATA 归 search.js 所有（ESM 可变 let），board 的对齐写必经 setter（语义不变）
    i_align = code.index("setLastRecommendData(data)")
    i_dispatch = code.index("actDispatchPlanChain(")
    assert i_frame < i_align < i_dispatch, "对齐必须发生在派发之前"


def test_restore_graduated_from_the_frontend_exemption_list():
    """豁免机制本身保留（常量仍是 EXEC 子集），但 restore 已经接线毕业、清单当前为空。"""
    assert "curate.restore" not in AP.FRONTEND_UNWIRED_EXEC_VERBS
    assert set(AP.FRONTEND_UNWIRED_EXEC_VERBS) <= set(AP.EXEC_VERBS)
    wired = set(re.findall(r'"([a-z]+\.[a-z_]+)"',
                           re.search(r"const ACT_RUNNERS = \{(.*?)\};", ACT, re.S).group(1)))
    assert wired == set(AP.EXEC_VERBS) - set(AP.FRONTEND_UNWIRED_EXEC_VERBS)


# ---------------------------------------------------------------- chat-in-main 主框发检索句不清对话

def test_chat_in_main_search_keeps_the_conversation_via_waschat():
    """根因：ubDispatch search 档 keepConv 此前只看 fromChat——chat-in-main（无结果、对话在主区）
    时从**主框**发检索句，前段工具对话被 cbLogClear 清掉。
    修法钉三件事：① ubSubmit 在 cbLogPush **之前**捕获 wasChat=cbChatInMain()（之后恒真，分不清
    hero 首句与主区续聊）；② wasChat 串进 ubDispatch；③ search 档 `fromChat || wasChat` 时
    runRecommend({keepConv:true, sayPushed:true})——hero 首句（wasChat=false）走原 sayText 路径不变。"""
    sub = re.search(r"async function ubSubmit\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert sub, "找不到 ubSubmit"
    body = _strip_comments(sub.group(1))
    i_capture = body.index("cbChatInMain()")
    i_saypush = body.index('cbLogPush("say", text)')
    assert "wasChat" in body and i_capture < i_saypush, \
        "wasChat=cbChatInMain() 必须在 say 上屏之前捕获（之后 cbChatInMain 恒真）"
    assert "ubDispatch(text, reply, fromChat, wasChat)" in body, "wasChat 必须串进 ubDispatch"
    disp = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert disp, "找不到 ubDispatch"
    code = _strip_comments(disp.group(1))
    search = code[code.index('route === "search"'):code.index('route === "tool"')]
    assert "fromChat || wasChat" in search, "search 档 keepConv 判定必须包含 wasChat"
    assert "keepConv: true" in search and "sayPushed: true" in search
    # hero 首句路径（else 分支）维持 sayText 原样
    assert "sayText: text" in search, "hero 首句仍走 sayText 路径"


# ---------------------------------------------------------------- B：在途草稿不被落地回显覆盖

def test_inflight_draft_survives_all_three_refill_paths():
    """B + owner 新指①（行为改订）：发送即清空贯穿**整个在途窗口**——
    search 档 / requires_results 档不再在路由落地时回写输入框（chat-in-main 下那是「原话又回来了」），
    检索句经 `opts.queryOverride` 显式交给 runRecommend；输入框由 runRecommend 在**结果落地时**
    经 `_ubLandingFill` 回填成「当前检索句」。三条回填路径统一过同一草稿守卫：
    框为空或框里仍是被发送的那句（trim 相等）才回写，否则保留草稿。
    ① search 档落地回填 q；② requires_results 档落地回填 said；③ catch 回退档即时回填 text。"""
    helper = re.search(r"function ubFillQuery\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert helper, "找不到 ubFillQuery 回填守卫"
    code = _strip_comments(helper.group(1))
    # 守卫语义钉两头：当前框内容与被发送句都 trim 后参与比较（旧断言
    # `"trim()" in code and "!=" in code or "!==" in code` 是近同义反复——
    # 任何带 !== 的 JS 都能蒙混，什么也没钉住）。
    assert 'String(input.value || "").trim()' in code, "框里当前内容必须 trim 后参与比较"
    assert 'String(sentText || "").trim()' in code, "被发送句必须 trim 后参与比较"
    assert re.search(r"if\s*\(cur && cur !==.*?\)\s*return;", code), "框里是别人的草稿 → 直接 return 保留"
    whole = _strip_comments(BOARD)
    # catch 回退档维持即时回填（框里的原话就是 fail-open 的退路）
    assert 'ubFillQuery($("queryInput"), text, text)' in whole, "catch 回退档回填未过守卫"
    # search 档 / requires_results 档：检索句走 queryOverride + sentText，不在分发时碰框
    assert "queryOverride: q" in whole and "sentText: text" in whole, "search 档必须显式递句（queryOverride+sentText）"
    assert "queryOverride: said" in whole and "sentText: said" in whole, "requires_results 档必须显式递句"
    # 落地回填在 search.js，守卫同口径（双 trim + 草稿保留 return）
    search_src = _strip_comments(_read("search/search.js"))
    fill = re.search(r"function _ubLandingFill\([^)]*\)\s*\{(.*?)\n\}", search_src, re.S)
    assert fill, "search.js 缺 _ubLandingFill 落地回填"
    fcode = fill.group(1)
    assert 'String(box.value || "").trim()' in fcode and 'String(sentText || "").trim()' in fcode
    assert re.search(r"if\s*\(cur && cur !==.*?\)\s*return;", fcode), "落地回填必须保留在途草稿"
    # sr1（检索工具化）：两个落地点的回填收口进共享入口 landRecommendResult——
    # 本钉从「两处各一份」改成「共享入口一份 + 两落地点都走它」，防的是回填语义两边漂移。
    assert search_src.count("_ubLandingFill(query, opts.sentText)") == 1, "落地回填只在共享入口一份"
    assert search_src.count("landRecommendResult(cached") == 1
    assert search_src.count("landRecommendResult(data, query, { noScroll") == 1, "缓存命中 + 真请求两个落地点都要走共享入口"
    assert "opts.queryOverride ||" in search_src, "runRecommend 必须优先取 queryOverride（框不是在途取数口）"
    # 三处旧裸写不许残留
    assert 'input.value = q;' not in whole and 'input.value = said;' not in whole
    assert '_qi.value = text;' not in whole


def test_failopen_fallback_alerts_user_before_literal_search():
    """/api/utterance 非 503/409 失败（含后端 500、
    网络异常、JSON 解析失败）曾**静默** fail-open——原话被当检索词直接 runRecommend()，
    「删掉某文件」这类操作句无任何提示地被曲解成字面检索，用户与排障都零痕迹。
    fail-open 本身保留（设计：不比统一前的主框更坏），但检索前必须亮 isError sys 泡
    如实告知「没走通、已按原话检索」+ console.warn 留痕。"""
    body = re.search(r"async function ubSubmit\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 ubSubmit"
    code = _strip_comments(body.group(1))
    _, _, catch_tail = code.partition("catch (err)")
    assert "err.busy" in catch_tail, "503/409 忙态分支不许被 fail-open 提示波及"
    i_push = catch_tail.index('cbLogPush("sys", "没走通 AI 分流')
    i_run = catch_tail.index("runRecommend()")
    assert i_push < i_run, "fail-open 字面检索前必须先如实告知用户（isError sys 泡）"
    notice = catch_tail[i_push:i_run]
    assert "{ isError: true }" in notice, "fail-open 提示必须按错误泡渲染（isError）"
    assert "err.message" in notice and "已按原话直接检索" in notice, (
        "提示必须带上失败原因，并说清「已按原话检索」——操作没执行要可感知"
    )
    assert "console.warn(" in catch_tail[:i_run], "fail-open 必须 console.warn 留痕（排障入口）"


# ---------------------------------------------------------------- mock/无 key 时 AI 执行引导不死路

def test_llm_absent_echo_points_to_settings_when_not_capable():
    """重试话术：LLM 缺席的规则兜底回音里「可以再发一次试试」在 mock/无 key 下永不成功——
    llmCapable() 为假时 ubDispatch none 档必须换成如实指路（去设置配置 API）。"""
    disp = re.search(r"function ubDispatch\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert disp, "找不到 ubDispatch"
    code = _strip_comments(disp.group(1))
    tail = code[code.index('route === "tool"'):]
    assert "llmCapable()" in tail, "none 档必须按 API 可用性换话术"
    assert "AI / API 配置" in tail, "无 key 话术必须如实指路到 API 配置"
    assert "needs_agent" in tail, "needs_agent 降级气泡档不许被换话术（它有自己的指路）"


def test_needs_agent_cta_label_and_reveal_follow_capability():
    """降级气泡 CTA：llmCapable 假 → 文案「去配置 API」（点击 openSettings + 展开 apiConfig +
    scrollIntoView）；真 → 维持「去开启 AI 执行」，但滚到该开关并短暂高亮（直达兑现）。"""
    assert "去配置 API" in BOARD and "去开启 AI 执行" in BOARD
    cta = re.search(r"cbh-agent-cta[^>]*>(.*?)</button>", BOARD, re.S)
    assert cta and "llmCapable()" in cta.group(0), "CTA 文案必须随 API 可用性切换"
    click = re.search(r"function cbHistoryClick\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert click, "找不到 cbHistoryClick"
    code = _strip_comments(click.group(1))
    assert "openSettings()" in code
    assert 'revealSetting("nodeAgentExec", false)' in code, "有 key → 直达 AI 执行开关"
    assert 'revealSetting("apiConfig", true)' in code, "无 key → 展开 API 配置并滚到"
    # shell.js 的 revealSetting 真身：展开 details + scrollIntoView + 短暂高亮类
    shell_src = _read("core/shell.js")
    rev = re.search(r"export function revealSetting\([^)]*\)\s*\{(.*?)\n\}", shell_src, re.S)
    assert rev, "shell.js 缺 revealSetting"
    rcode = _strip_comments(rev.group(1))
    assert "scrollIntoView" in rcode and "setting-flash" in rcode and "DETAILS" in rcode
    assert ".setting-flash" in CSS, "短暂高亮样式缺席"


# ---------------------------------------------------------------- 纯工具对话丢弃前归档「仅对话」历史行

def test_chat_only_conversation_is_archived_before_discard():
    """纯工具对话（无检索帧）从不走 pushHist——丢弃前写一条仅对话历史行。
    钉链路：board.cbArchiveChatOnly（有对话 ∧ 无检索帧 ∧ 非 hero 首句那句 say）→ core.pushHistChatOnly
    （chatOnly:true 标记 + convId/chat 同形 + 同 convId 去重）；两个丢弃点接线：
    search.js runRecommend 清对话前、board.cbClear（账户切换 archive:false 防跨账户泄漏）。"""
    arch = re.search(r"export function cbArchiveChatOnly\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert arch, "board.js 缺 cbArchiveChatOnly"
    acode = _strip_comments(arch.group(1))
    assert "_cbStack.length" in acode, "有检索帧的对话已被 pushHist 归档，不许重复归档"
    assert "pushHistChatOnly()" in acode
    assert 'kind === "say"' in acode, "hero 首句刚上屏的那句 say 不许误归档"
    core_src = _read("core/core.js")
    writer = re.search(r"export function pushHistChatOnly\([^)]*\)\s*\{(.*?)\n\}", core_src, re.S)
    assert writer, "core.js 缺 pushHistChatOnly"
    wcode = _strip_comments(writer.group(1))
    assert "chatOnly: true" in wcode, "仅对话行必须带可识别标记"
    assert "_histConvId" in wcode and "_histLogForHistory" in wcode, \
        "convId/chat 必须经 board 注册的钩子取运行期真源（与 pushHist 普通行同形同源）"
    assert "setHistHooks({ convId: cbConvId, logForHistory: cbLogForHistory })" in BOARD, \
        "board.js 必须在 initCondBoard 注册历史打标钩子（core→board 反向边已切断，走注册反转）"
    assert "nsKey(LS.hist)" in wcode, "必须写当前账户命名空间键"
    search_src = _read("search/search.js")
    # 钉「归档紧邻清对话之前」这个次序，不过拟合归档调用的实参表达式（实参取
    # 用户原话 sayText、改写时回退 query——比错对象会产幽灵「仅对话」行，见 search.js 注释）。
    s_clear = re.search(r"cbArchiveChatOnly\([^;]*\);\s*cbLogClear\(\)", search_src)
    assert s_clear, "runRecommend 清对话前必须先归档"
    cb_clear = re.search(r"export function cbClear\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert cb_clear and "cbArchiveChatOnly" in cb_clear.group(1), "cbClear 丢弃前必须先归档"
    acc = _read("panel/accounts.js")
    assert "cbClear({ archive: false })" in acc, "账户切换不归档（命名空间已是新账户，防泄漏）"


# ---------------------------------------------------------------- 动作句的 query 真源与回注目标（疑点回归）

def test_action_sentence_never_becomes_the_query_source():
    """有结果后在对话窗只说「打包前5条」→ 打包预览 0 命中假失败（屏上有结果）。
    根因：发送即清空后，fromChat 把动作原话写进主框 / hero 主框被清空，而任务包/引文/可行性
    都读框取 query。修法钉死：ubDispatchAction 在直派/指路**之前**把框恢复成当前帧检索句
    （cbFrameQuery），且只在「框空或框里仍是那句原话」时才回写（B 草稿守卫同口径）。"""
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub, "找不到 ubDispatchAction"
    code = _strip_comments(ub.group(1))
    assert "cbFrameQuery()" in code, "直派/指路前必须取当前帧检索句作真源"
    assert re.search(r"_qbox\.value = _frameQ", code), "框必须恢复成帧检索句"
    assert 'String(said || "").trim()' in code, "回写必须带「仍是那句原话」守卫（草稿不冲掉）"
    # 恢复必须发生在两条执行路径之前（指路 cbRouteAsFirstBox / 直派 actDispatchPlan 都读框）
    i_restore = code.index("cbFrameQuery()")
    assert i_restore < code.index("cbRouteAsFirstBox("), "指路档之前必须先恢复框"
    assert i_restore < code.index("actDispatchPlanChain("), "直派档之前必须先恢复框"


def test_search_then_dispatch_keeps_the_say_and_marks_the_right_bubble():
    """「先检索后派发」（「人类肺癌数据，打包前5条」）三件事——
    ① 原话 say 不许消失：hero 新时间线会被 runRecommend 清场，必须给 sayText 重推
       （keepConv 的对话窗/chat-in-main 路径才用 sayPushed 防双泡）；
    ② keepConv 判据与 search 档一致（fromChat || wasChat，同型）；
    ③ 执行注记必须按 kind==='say' 定位原话——actFinish 的总结 sys 先落地，
       按「最后一条」找会把回执泡标成 action（明细折叠区不上屏、sr 前缀错成「你要求：」）。"""
    ub = re.search(r"function ubDispatchAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert ub, "找不到 ubDispatchAction"
    code = _strip_comments(ub.group(1))
    assert "keepConv: !!(fromChat || wasChat)" in code, "keepConv 必须含 wasChat（chat-in-main 同待遇）"
    assert 'sayText: said' in code, "新时间线档必须给 sayText 重推原话（否则 say 被清场吃掉）"
    assert 'sayPushed: true' in code, "keepConv 档必须 sayPushed（say 已在屏，防双泡）"
    # ubDispatchAction 要能拿到 wasChat：ubDispatch 调用处必须传（第 5 参 searchData 是
    # 混合轮检索事实，可空——纯派发路径不灌注）
    assert "ubDispatchAction(text, reply.plan || null, fromChat, wasChat, searchData)" in _strip_comments(BOARD)
    mark = re.search(r"export function cbMarkLastSayAsAction\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert mark, "找不到 cbMarkLastSayAsAction"
    mcode = _strip_comments(mark.group(1))
    assert 'kind === "say"' in mcode, "回注目标必须按 kind==='say' 找（总结 sys 已先落地，末尾不是原话）"
    assert "_cbLog[_cbLog.length - 1]" not in mcode, "不许再按「最后一条」取回注目标"
