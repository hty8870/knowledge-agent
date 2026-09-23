# -*- coding: utf-8 -*-
"""回退措辞的诚实性门：「未启用」和「没能完成」不是同一件事，永远不许混。

事故原文（2026-07-26 复盘）：后端从第一天起就分得清两种回退——
`status="skipped"` = 这一层没启用，`status="fallback"` = **试过但没成**。
`shell.js` 的开发者面板也一直守着这条（轮的注释：「尝试过但失败才是真故障」）。
唯独 `results.js` 那句**用户每次检索都会读到**的摘要，把两者一律写成「本次未启用，已改用基础方式」。

后果不是措辞难看，是**看不出坏了**：provider 连着几天返 400 的那段时间，界面上写的是
「AI 重排本次未启用」——读起来像系统自己决定不用这一层。连写这段代码的人自己都判断错了
它坏了多久。

所以这里同时钉三件事：
1. 措辞只有**一个产地**（`workflow._fallback_note`）；
2. 每个 reason 都被明确归档到「没启用」或「开了但没成」，新增 reason 漏归档 → 红；
3. 前端**不许自己写死**这句话，且拿不到后端那半句时退到「没能完成」（宁重不轻）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from dataset_recommender.llm.llm_client import LLMConfig
from dataset_recommender.retrieval.normalizer import DatasetRecord
from dataset_recommender.retrieval.rerank import rerank_candidates
from dataset_recommender.retrieval.retriever import RetrievedCandidate
from dataset_recommender.app.workflow import (
    _FALLBACK_MEANS_NOT_ENABLED,
    _TRACE_REASON_LABELS,
    _build_search_trace,
    _fallback_note,
    _update_trace_step,
)

ROOT = Path(__file__).resolve().parents[1]
NOT_ENABLED = "未启用"
FAILED = "没能完成"


# ---------------------------------------------------------------- 后端：措辞分档

def test_the_two_wordings_are_mutually_exclusive_for_every_known_reason():
    """每个已知 reason 恰好落进一档，且两档的字面前缀互斥。

    「互斥」是这条门的全部意义：只要有一个 reason 能同时读成两种意思，
    用户就无从判断到底是没开还是坏了。
    """
    assert _TRACE_REASON_LABELS, "原因标签表不该是空的"
    for reason in _TRACE_REASON_LABELS:
        note = _fallback_note({"reason": reason})
        enabled_side = note.startswith(NOT_ENABLED + "：")
        failed_side = note.startswith(FAILED + "：")
        assert enabled_side != failed_side, f"{reason} 的措辞既不是未启用也不是没能完成：{note}"
        assert enabled_side == (reason in _FALLBACK_MEANS_NOT_ENABLED), (
            f"{reason} 归档与措辞不一致：{note}"
        )
        # 归到「没能完成」的那一档，整句话里不许出现「未启用」——否则用户照样会读成「我没开」。
        if failed_side:
            assert NOT_ENABLED not in note, f"故障档的措辞里出现了「未启用」：{note}"


def test_an_unknown_reason_is_reported_as_a_failure_not_as_a_choice():
    """没登记过的 reason 一律按故障说。

    方向是刻意选的：把「其实只是没开」说成「没能完成」，代价是用户多看一眼诊断；
    反过来把故障说成「未启用」，代价是**永远发现不了**。宁重不轻。
    """
    note = _fallback_note({"reason": "brand_new_reason_nobody_registered"})
    assert note.startswith(FAILED)
    assert NOT_ENABLED not in note
    assert _fallback_note({}).startswith(FAILED)          # 连 reason 都没有时同理


def test_a_retired_provider_model_is_reported_as_a_failure():
    """这条门就是那次事故本身：模型名被服务端拒 → rerank 记 `llm_call_failed` → 措辞必须是故障。"""
    assert _fallback_note({"reason": "llm_call_failed"}) == f"{FAILED}：AI 接口调用失败或返回为空"
    assert _fallback_note({"reason": "llm_not_configured"}) == f"{NOT_ENABLED}：服务端还没有配置可用的 AI 接口"
    # 本地语义模型没装是「真的没启用」——《使用说明书》10.4 就是这么向客户承诺的，别改成故障。
    assert _fallback_note({"reason": "model_or_dependency_unavailable"}).startswith(NOT_ENABLED)


# ---------------------------------------------------------------- rerank：谁该记「没配」，谁该记「调用失败」

def _cand(name: str) -> RetrievedCandidate:
    rec = DatasetRecord(
        dataset_name=name, species="human", tissue="lung", disease="cancer",
        chemistry="gex", count="1", unit="cell", has_raw_data=True,
        url="https://example.test", source_file="test.json", description=name,
        raw={}, family_id=name,
    )
    return RetrievedCandidate(rec, 0.0, [], [], "")


def test_rerank_separates_no_key_from_a_call_that_really_happened_and_failed():
    items = [_cand("A"), _cand("B")]

    # ① 没 key：这一次**根本没发请求** → 未启用
    no_key = {}
    rerank_candidates("q", items, backend="llm", config=LLMConfig(), trace=no_key)
    assert no_key["status"] == "fallback" and no_key["reason"] == "llm_not_configured"
    assert _fallback_note(no_key).startswith(NOT_ENABLED)

    # ② 有 key、真去调了、provider 回空 → 故障
    with_key = LLMConfig(api_key="sk-not-a-real-key", enable_llm=True)
    failed = {}
    rerank_candidates("q", items, backend="llm", config=with_key,
                      llm_call=lambda _p: None, trace=failed)
    assert failed["status"] == "fallback" and failed["reason"] == "llm_call_failed"
    assert _fallback_note(failed).startswith(FAILED)

    # ③ 注入了 llm_call 但配置里没 key（测试常见形态）：调用方自带 provider，仍算「真调过」
    injected = {}
    rerank_candidates("q", items, backend="llm", config=LLMConfig(),
                      llm_call=lambda _p: "", trace=injected)
    assert injected["reason"] == "llm_call_failed"

    # ④ provider 抛异常也算真调过（异常在 rerank 内被吞成 order_text=None）
    boom = {}
    def _boom(_p):
        raise RuntimeError("HTTP 400")
    rerank_candidates("q", items, backend="llm", config=with_key, llm_call=_boom, trace=boom)
    assert boom["reason"] == "llm_call_failed"

    # ⑤ 格式坏（真答了、但排序解析不出来）走既有 invalid_order，同样是故障档
    bad = {}
    rerank_candidates("q", items, backend="llm", config=with_key,
                      llm_call=lambda _p: "not-json", trace=bad)
    assert bad["reason"] == "invalid_order"
    assert _fallback_note(bad).startswith(FAILED)


# ---------------------------------------------------------------- trace：note 只挂在 fallback 上

@dataclass
class _FakeResolution:
    automatic_requested: bool = False
    automatic_skipped_reason: str | None = None
    source_mode: str = "explicit"
    detected_sources: tuple = ()
    parsed_query: str = "人类肺癌数据"


def _trace(recall_status: str, recall_reason: str, rerank_status: str, rerank_reason: str) -> dict:
    from dataset_recommender.retrieval.query_parser import parse_query
    from dataset_recommender.retrieval.search_request import resolve_search_request

    resolution = resolve_search_request("人类肺癌数据", None, ["10x Genomics"], auto_parse_sources=False)
    intent = parse_query(resolution.parsed_query)
    execution = {
        "recall": {"backend": "cross_encoder", "status": recall_status, "reason": recall_reason, "duration_ms": 5},
        "rerank": {"backend": "llm", "status": rerank_status, "reason": rerank_reason, "duration_ms": 5},
    }
    return _build_search_trace(resolution, intent, execution, None, 12)


def _steps(trace: dict) -> dict:
    return {s["id"]: s for s in trace["steps"]}


def test_only_fallback_steps_carry_a_note_and_it_matches_their_reason():
    trace = _trace("fallback", "model_or_dependency_unavailable", "fallback", "llm_call_failed")
    steps = _steps(trace)
    assert steps["local_semantic"]["fallback_note"].startswith(NOT_ENABLED)
    assert steps["llm_rerank"]["fallback_note"].startswith(FAILED)
    # used / skipped 的步骤**不许**带 note——带了就会在界面上多出一句与状态矛盾的话
    for sid in ("constraint_parse", "rule_rank", "final_guard", "llm_polish"):
        assert "fallback_note" not in steps[sid], sid

    clean = _trace("used", "completed", "skipped", "disabled")
    clean_steps = _steps(clean)
    assert "fallback_note" not in clean_steps["local_semantic"]
    assert "fallback_note" not in clean_steps["llm_rerank"]


def test_updating_a_step_away_from_fallback_clears_the_stale_note():
    """同一份 trace 会被就地改写（llm_polish 那几支）。留着上一次的 note 就等于留一句矛盾的话。"""
    trace = {"steps": [{"id": "llm_polish", "label": "AI 说明润色", "status": "pending", "detail": ""}]}
    _update_trace_step(trace, "llm_polish", "fallback", "d", fallback_note=f"{FAILED}：X")
    assert trace["steps"][0]["fallback_note"] == f"{FAILED}：X"
    _update_trace_step(trace, "llm_polish", "used", "d2")
    assert "fallback_note" not in trace["steps"][0]


def test_polish_fallback_paths_speak_the_right_half_sentence():
    """润色三支回退：没发出请求=未启用；调用失败/没过反捏造校验=没能完成。"""
    assert _fallback_note({"reason": "llm_not_configured"}).startswith(NOT_ENABLED)
    assert _fallback_note({"reason": "llm_call_failed"}).startswith(FAILED)
    assert _fallback_note({"reason": "invalid_llm_answer"}) == f"{FAILED}：AI 说明没通过反捏造校验"

    src = (ROOT / "src" / "dataset_recommender" / "app" / "workflow.py").read_text(encoding="utf-8")
    # 三支必须各自传 note；漏传的那一支在界面上会退成「没能完成」这句泛化话（不算撒谎，但丢了原因）
    assert src.count('_update_trace_step(\n                search_trace, "llm_polish", "fallback"') == 3


# ---------------------------------------------------------------- 前端：真行为（node 里跑真函数）

def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _notes_in_node(steps: list[dict]) -> list[str]:
    """把 results.js 整个源码塞进一个 Function 体里，调真的 fallbackLayerNotes。

    刻意跑真函数而不是查字符串：这句话怎么拼出来的，只有真跑一遍才算数——
    上一轮就吃过「静态门全绿、真行为撒谎」的亏。

    C2 起 results.js 是 ES Module：先剥掉 import 行 / export 行首前缀 / window 绞杀桥
    （桥引用浏览器才有的 window），剩余函数体与原经典形态等价。起 fallbackLayerNotes
    调 escapeHtml（import 自 #core，会被剥掉）——从 core.js 源码抠出**真** escapeHtml 前置
    注入；手抄替身会与真实现漂移，门就废了。
    """
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过前端真行为门（full 质量门的语法检查环节必有 node）。")
    script = (
        "const escSrc = require('fs').readFileSync('web/static/js/core/core.js', 'utf-8')\n"
        "  .match(/export function escapeHtml\\([\\s\\S]*?\\n\\}/)[0].replace('export ', '');\n"
        "const src = escSrc + '\\n' + require('fs').readFileSync('web/static/js/search/results.js', 'utf-8')\n"
        "  .replace(/^import[^\\n]*\\n/gm, '')\n"
        "  .replace(/^Object\\.(?:assign|defineProperty)\\(window[^\\n]*\\n/gm, '')\n"
        "  .replace(/^export /gm, '');\n"
        "const steps = JSON.parse(require('fs').readFileSync(0, 'utf-8'));\n"
        "const wanted = {local_semantic: '本地精准重排', llm_rerank: 'AI 重排', llm_polish: 'AI 说明润色'};\n"
        "const fn = new Function('steps', 'wanted', src + '\\nreturn fallbackLayerNotes(steps, wanted);');\n"
        "console.log(JSON.stringify(fn(steps, wanted)));\n"
    )
    proc = subprocess.run(
        [node, "-e", script], cwd=str(ROOT),
        input=json.dumps(steps, ensure_ascii=False),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"node 执行失败：\n{proc.stdout}\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_the_frontend_repeats_the_backend_wording_and_invents_none_of_its_own():
    notes = _notes_in_node([
        {"id": "llm_rerank", "status": "fallback",
         "fallback_note": _fallback_note({"reason": "llm_call_failed"})},
        {"id": "local_semantic", "status": "fallback",
         "fallback_note": _fallback_note({"reason": "model_or_dependency_unavailable"})},
    ])
    assert notes == [
        f"AI 重排（{FAILED}：AI 接口调用失败或返回为空）",
        f"本地精准重排（{NOT_ENABLED}：本地模型或运行依赖不可用）",
    ]


def test_a_failed_layer_is_never_rendered_as_not_enabled():
    """本轮修的就是这一条：故障那一档，界面上不许出现「未启用」四个字。"""
    for reason in _TRACE_REASON_LABELS:
        if reason in _FALLBACK_MEANS_NOT_ENABLED:
            continue
        note = _notes_in_node([
            {"id": "llm_rerank", "status": "fallback", "fallback_note": _fallback_note({"reason": reason})},
        ])[0]
        assert FAILED in note and NOT_ENABLED not in note, f"{reason} → {note}"


def test_an_old_backend_without_the_field_degrades_to_failed_never_to_not_enabled():
    """新前端遇上没有 `fallback_note` 的旧后端：说不清原因可以，说成「未启用」不行。"""
    notes = _notes_in_node([
        {"id": "llm_rerank", "status": "fallback"},
        {"id": "llm_polish", "status": "fallback", "fallback_note": ""},
    ])
    assert notes == [f"AI 重排{FAILED}", f"AI 说明润色{FAILED}"]


def test_non_fallback_steps_and_unknown_ids_produce_no_note():
    assert _notes_in_node([
        {"id": "llm_rerank", "status": "used", "fallback_note": "陈旧的话"},
        {"id": "llm_rerank", "status": "skipped"},
        {"id": "final_guard", "status": "fallback", "fallback_note": "去重与终检不进摘要"},
        None,
    ]) == []


def test_backend_note_is_html_escaped_before_innerhtml_assembly():
    """：fallback_note 是后端任意字符串——provider 原始
    报错可含任意字符，而本函数返回值经 innerHTML 上屏（results.js renderResultSummary），
    不转义就是 XSS 面。钉：恶意串进、转义串出，< > & " ' 一个不留；包裹层（全角括号）原样。"""
    payload = "<img src=x onerror=\"alert('xss')\"> & <b>加粗</b>"
    notes = _notes_in_node([
        {"id": "llm_rerank", "status": "fallback", "fallback_note": payload},
    ])
    assert len(notes) == 1
    assert "<" not in notes[0] and ">" not in notes[0]
    assert "&lt;img" in notes[0] and "&amp;" in notes[0] and "&#39;" in notes[0]
    assert notes[0].startswith("AI 重排（") and notes[0].endswith("）")


# ---------------------------------------------------------------- C3：密钥无效（401/403）与临时故障分两句

def test_auth_failure_gets_its_own_sentence_not_the_transient_one():
    """2026-08-04 C3：401/403=密钥无效/无权——重试永不自愈，用户该去改设置；
    它与「临时故障（超时/5xx/空回）」必须是两句不同的话，否则密钥坏了的人对着「稍后再试」干等。"""
    from dataset_recommender.llm.llm_client import is_auth_error

    # 判据单测：错误串产地（llm_client f"LLM HTTPError {code}: …"）的各形态
    assert is_auth_error("LLM HTTPError 401: unauthorized")
    assert is_auth_error("LLM HTTPError 403: forbidden")
    assert not is_auth_error("LLM HTTPError 500: internal")
    assert not is_auth_error("LLM HTTPError 429: rate limited")
    assert not is_auth_error("LLM URL error: timed out")
    assert not is_auth_error("LLM response content is empty or invalid")
    assert not is_auth_error(None) and not is_auth_error("")


def test_auth_error_judges_only_leading_code_not_echoed_body():
    """E-05（2026-08-15 触发点审计）：错误串 = f"LLM HTTPError {真实code}: {服务商正文}"——
    正文若回显上游 "HTTPError 401" 字样（网关透传/嵌套 JSON 错误描述），不得把 502/429
    误判成密钥无效（误分类会把临时故障的用户引导去改密钥）。判据已收窄为串首匹配。"""
    from dataset_recommender.llm.llm_client import is_auth_error

    assert not is_auth_error("LLM HTTPError 502: upstream error: HTTPError 401: invalid token")
    assert not is_auth_error('LLM HTTPError 429: {"error": "HTTPError 401 from gateway"}')
    # 串首是真 401/403 的干净串仍判 True（收窄不退化正常路径）
    assert is_auth_error("LLM HTTPError 401: unauthorized")
    assert is_auth_error("LLM HTTPError 403: forbidden")

    # 措辞：新的 auth reason 落故障档、字面带 401/403 与「密钥」，且与临时那句不同
    auth_note = _fallback_note({"reason": "llm_auth_failed"})
    transient_note = _fallback_note({"reason": "llm_call_failed"})
    assert auth_note != transient_note
    assert auth_note.startswith(FAILED) and NOT_ENABLED not in auth_note
    assert "401/403" in auth_note and "密钥" in auth_note
    # 注册表互斥门（上方 test_the_two_wordings…）自动覆盖新 reason——这里钉的是它真的登记了
    assert "llm_auth_failed" in _TRACE_REASON_LABELS


def test_rerank_marks_401_as_auth_failure_and_5xx_as_transient(monkeypatch):
    """rerank 真链路：provider 错误串经 _default_llm_call_with_error 带回，401 → llm_auth_failed，
    503 → llm_call_failed。monkeypatch 打在 call_llm 上（错误判据真跑，不替身）。"""
    import dataset_recommender.retrieval.rerank as R
    from dataset_recommender.llm.llm_client import LLMResult

    items = [_cand("A"), _cand("B")]
    cfg = LLMConfig(api_key="sk-not-a-real-key", enable_llm=True)

    def _result(error):
        return LLMResult(text=None, attempted=True, succeeded=False, response_used=False,
                         provider="openai-compatible", model="m", error=error)

    monkeypatch.setattr(R, "call_llm", lambda _p, _c: _result("LLM HTTPError 401: invalid api key"))
    auth = {}
    R.rerank_candidates("q", items, backend="llm", config=cfg, trace=auth)
    assert auth["reason"] == "llm_auth_failed", auth
    assert _fallback_note(auth).startswith(FAILED) and "401/403" in _fallback_note(auth)

    monkeypatch.setattr(R, "call_llm", lambda _p, _c: _result("LLM HTTPError 503: overloaded"))
    busy = {}
    R.rerank_candidates("q", items, backend="llm", config=cfg, trace=busy)
    assert busy["reason"] == "llm_call_failed", busy

    # 无 key 档不受新分档影响：仍是「未启用」
    no_key = {}
    R.rerank_candidates("q", items, backend="llm", config=LLMConfig(), trace=no_key)
    assert no_key["reason"] == "llm_not_configured"


def test_polish_fallback_also_splits_auth_from_transient():
    """润色链路与重排同一分档：workflow.py 的失败分支必须过 is_auth_error 判据（静态钉产地）。"""
    src = (ROOT / "src" / "dataset_recommender" / "app" / "workflow.py").read_text(encoding="utf-8")
    assert "is_auth_error(llm_result.error)" in src, "润色失败分支没有按 401/403 分档"
    assert "from ..llm.llm_client import" in src and "is_auth_error" in src


def test_summary_layers_are_highlighted_and_polish_note_stays_short():
    """sum1（2026-08-16 用户）：摘要句的方法层关键词（规则排序/本地精准重排/AI 重排）行内高光，
    润色附注精简成短句。钉四件事：
    1. results.js 两个分支（有无结果）的层名都包 <mark class="sum-layer">——只高一边等于
       「没结果时告诉用户走了哪几层」这条信息没了高光；
    2. 润色附注是新短句「推荐说明由 AI 润色。」，旧长句（「不改变结果与顺序」）不再出现；
    3. 高光样式落在 app.css 的 .rs-text .sum-layer（行内荧光笔，窄栏不破行全靠它）。"""
    src = (ROOT / "web" / "static" / "js" / "search" / "results.js").read_text(encoding="utf-8")
    assert src.count('<mark class="sum-layer">') >= 2, "摘要句层名高光只覆盖了一个分支"
    assert "推荐说明由 AI 润色。" in src
    assert "不改变结果与顺序" not in src, "旧的润色长句还在"
    css = (ROOT / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert ".rs-text .sum-layer" in css, "高光样式 .rs-text .sum-layer 缺失"
