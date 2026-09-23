# -*- coding: utf-8 -*-
"""ask 暂停/重启环专项门（2026-09-14 ask 暂停/重启批）。**全离线**：

- 暂停侧：ask.relax 真发起（asked=True）= 环暂停——本批剩余续步不再执行
  （execute trace 如实交代）、`_route_after_execute` 跳过 decide 直去 narrate、
  narrate 走确定性说明（弹卡理由 + 等作答事实句，零 LLM 调用）。
- 重启侧：用户作答经 ask_answer 新回合回环——route_consensus 免投票直定 search、
  understand 零 LLM 强置首步 rank（mode="forced"）、decide prompt 注入作答事实段
  （`_ask_answer_resume_block_zh`）。
- fail-soft：agent/LLM 不在场时 ask_answer 回退纯检索路由（via="ask_answer_fallback"，
  等同旧 cbCommit 零 LLM 重搜）。
- webapp：`_sanitize_ask_answer` 的 kind 闸（非 "ask.relax" → 400 bad_ask_answer）、
  合法载荷收编（定长截断 + suppress 过单一真源）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import turn
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig

CFG = LLMConfig(enable_llm=True, api_key="sk-pause-test")

#: 条件互相打架的典型坏查询（与 test_ask_relax 同口径）。
BAD_QUERY = "human lung adenocarcinoma FASTQ"

#: 合法作答载荷（webapp 收编后的形状：kind/choice_key/label/message/suppress）。
ANSWER = {"kind": "ask.relax", "choice_key": "dim:disease",
          "label": "不按「疾病」筛", "message": "条件互相打架",
          "suppress": ["include:disease"]}


def _asked_step(**over):
    """一条真发起的 ask.relax 实录步（ok ∧ asked=True）。"""
    step = {"verb": "ask.relax", "ok": True,
            "result": {"asked": True, "query": BAD_QUERY,
                       "ask_relax": {"query": BAD_QUERY, "reason": "条件互相打架",
                                     "options": []}}}
    step.update(over)
    return step


def _runtime(**ctx_kw):
    """节点直调用替身：节点只读 `runtime.context`（与 test_agent_decide_routing 同径）。"""
    return SimpleNamespace(context=SimpleNamespace(**ctx_kw))


# ---------------------------------------------------------------- _ask_relax_pending

def test_pending_detects_asked_step():
    """存在 ok ∧ asked=True 的 ask.relax 步 → 返回该步（环正停在等作答的卡上）。"""
    step = _asked_step()
    assert AX._ask_relax_pending([step]) is step


def test_pending_returns_latest():
    """多张卡时取最近一次（预算闸下现实中至多一张，取最新是防御语义）。"""
    older, newer = _asked_step(), _asked_step()
    assert AX._ask_relax_pending([older, newer]) is newer


def test_pending_ignores_refused_failed_and_other_verbs():
    """被拒（asked=False）/ 失败步 / 非 ask.relax 动词 → 不算暂停。"""
    assert AX._ask_relax_pending([]) is None
    assert AX._ask_relax_pending([_asked_step(result={"asked": False})]) is None
    assert AX._ask_relax_pending([_asked_step(ok=False)]) is None
    assert AX._ask_relax_pending([{"verb": "rank", "ok": True,
                                   "result": {"asked": True}}]) is None


# ---------------------------------------------------------------- 暂停侧：execute 续步截断

def _degenerate_state(**over):
    """屏面 degenerate 的图状态：既有 steps 含一条零命中 rank（ask.relax 机械闸放行档）。"""
    state = {"utterance": BAD_QUERY, "current_query": BAD_QUERY,
             "has_results": False, "result_total": 0,
             "steps": [{"verb": "rank", "ok": True,
                        "result": {"health": "degenerate", "total": 0,
                                   "query": BAD_QUERY}}],
             "plan": {"verb": "ask.relax", "verb_zh": "询问放宽条件",
                      "slots": {"reason": "条件互相打架"}},
             "loop_plan": None, "loop_batch": [], "route_scope": "search"}
    state.update(over)
    return state


@pytest.fixture
def _stub_ask_flow(monkeypatch, tmp_path):
    """管线替身（与 test_ask_relax 同径）：relaxation_options 返固定假选项；
    审计账本重定向 tmp——不碰真实库。"""
    import dataset_recommender.app.workflow as wf

    class _Flow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                return [{"key": "dim:disease", "kind": "drop", "label": "疾病",
                         "count": 12, "suppress": ["include:disease"],
                         "candidates": []}]

        def _prepare_context(self, query, **kwargs):
            from dataset_recommender.retrieval.query_parser import parse_query
            return (parse_query(query, None), [], [],
                    None, None, None, None, None)

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _Flow)
    monkeypatch.setattr(AX, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(AX, "_audit_loop_tool", lambda *a, **k: None)


def test_execute_pauses_after_ask_card(_stub_ask_flow):
    """主步弹出等作答卡 → 本批续步全部跳过：extras 清零、trace 留「环已暂停等作答」、
    续步不执行不记步。"""
    ran = []

    def _probe(slots, root):
        ran.append(slots)
        return {"done": True}

    tools = dict(AX.LOOP_TOOLS)
    tools["probe.step"] = {"run": _probe, "label_zh": "探针", "card_kind": "probe",
                           "readonly": True}
    state = _degenerate_state(loop_batch=[{"verb": "probe.step", "slots": {}},
                                          {"verb": "probe.step", "slots": {}}])
    import dataset_recommender.agent.agent_exec as ax_mod
    ax_mod.LOOP_TOOLS = tools  # 本测试进程内临时注册探针动词
    try:
        out = AX.execute(state, runtime=_runtime(chat_model=None, on_progress=None))
    finally:
        ax_mod.LOOP_TOOLS = {k: v for k, v in tools.items() if k != "probe.step"}
    assert ran == [], "暂停后本批续步不许再执行"
    assert any("环已暂停等作答" in str(t.get("label_zh") or "")
               for t in out.get("trace") or [])
    assert len(out["steps"]) == 1 and out["steps"][0]["verb"] == "ask.relax"
    assert out["loop_batch"] is None  # 批已清账


def test_execute_mid_batch_pause_breaks(_stub_ask_flow):
    """续步弹出卡（主步是普通只读步）→ 其后续步不再执行（同主步口径）。"""
    ran = []
    tools = dict(AX.LOOP_TOOLS)
    tools["probe.step"] = {"run": lambda slots, root: ran.append(slots) or {"done": True},
                           "label_zh": "探针", "card_kind": "probe", "readonly": True}
    state = _degenerate_state(
        plan={"verb": "probe.step", "verb_zh": "探针", "slots": {}},
        loop_batch=[{"verb": "ask.relax", "slots": {"reason": "条件互相打架"}},
                    {"verb": "probe.step", "slots": {}}])
    import dataset_recommender.agent.agent_exec as ax_mod
    ax_mod.LOOP_TOOLS = tools
    try:
        out = AX.execute(state, runtime=_runtime(chat_model=None, on_progress=None))
    finally:
        ax_mod.LOOP_TOOLS = {k: v for k, v in tools.items() if k != "probe.step"}
    assert len(ran) == 1, "主步探针执行一次；ask.relax 之后的续步探针不许再跑"
    verbs = [s["verb"] for s in out["steps"]]
    assert verbs == ["probe.step", "ask.relax"]
    assert any("环已暂停等作答" in str(t.get("label_zh") or "")
               for t in out.get("trace") or [])


def test_route_after_execute_goes_narrate_when_pending():
    """环暂停时跳过 decide（不再发起下一次 LLM 调用）直去 narrate。"""
    assert AX._route_after_execute({"steps": [_asked_step()], "last_ran": True}) == "narrate"
    # 对偶钉：无暂停卡时保持现状（真跑了工具 → decide）。
    assert AX._route_after_execute({"steps": [], "last_ran": True}) == "decide"


def test_narrate_deterministic_when_pending():
    """暂停档 narrate：确定性说明 = 弹卡理由 + 等作答事实句；零 LLM 调用。"""
    def _boom(*a, **k):
        raise AssertionError("暂停档 narrate 不许发起 LLM 调用")

    state = {"utterance": BAD_QUERY,
             "plan": {"verb": "ask.relax", "verb_zh": "询问放宽条件"},
             "steps": [_asked_step()]}
    out = AX.narrate(state, runtime=_runtime(chat_model=_boom, on_progress=None))
    plan = out["plan"]
    assert plan["report_source"] == "deterministic"
    assert "条件互相打架" in plan["report_zh"]
    assert "选完我会按你的选择自动重跑检索并继续" in plan["report_zh"]
    assert "环已暂停等作答" in out["trace"][0]["detail"]


def test_narrate_pending_without_reason_still_honest():
    """弹卡理由缺席（可空）→ 说明只剩等作答事实句，不伪造理由。"""
    state = {"utterance": BAD_QUERY,
             "plan": {"verb": "ask.relax", "verb_zh": "询问放宽条件"},
             "steps": [_asked_step(result={"asked": True, "query": BAD_QUERY,
                                           "ask_relax": {"query": BAD_QUERY,
                                                         "reason": "", "options": []}})]}
    out = AX.narrate(state, runtime=_runtime(chat_model=None, on_progress=None))
    assert out["plan"]["report_zh"] == "选完我会按你的选择自动重跑检索并继续。"


# ---------------------------------------------------------------- 重启侧：route_consensus / understand / decide

def test_route_consensus_skips_votes_on_ask_answer():
    """ask_answer 在场 → 免投票直定 search 套件（路由是机械事实）；verdict hook 同调。"""
    verdicts = []
    ctx = dict(chat_model=None, decide_model=None, on_progress=None,
               on_route_verdict=verdicts.append)
    out = AX.route_consensus({"utterance": BAD_QUERY, "ask_answer": ANSWER},
                             runtime=_runtime(**ctx))
    assert out["route_scope"] == "search"
    assert "不发起分流投票" in out["trace"][0]["detail"]
    assert verdicts == ["search"]


def test_understand_forces_rank_on_ask_answer():
    """ask_answer 在场 → 零 LLM 强置首步 rank（quoted=原话自身，过 EXEC quoted 闸）；
    用户所选 label 进 trace 如实交代。"""
    def _boom(*a, **k):
        raise AssertionError("作答续跑的 understand 不许发起 LLM 调用")

    out = AX.understand({"utterance": BAD_QUERY, "ask_answer": ANSWER,
                         "retrieval": None},
                        runtime=_runtime(chat_model=_boom, decide_model=None,
                                         on_progress=None))
    assert out["mode"] == "forced"
    assert out["raw"] == {"verb": "rank", "query": BAD_QUERY, "quoted": BAD_QUERY}
    assert "不按「疾病」筛" in out["trace"][0]["detail"]
    assert "零 LLM 强置" in out["trace"][0]["detail"]


def test_resume_block_content():
    """作答事实段：dict → 含所选 label 与裁决指引；非 dict → 恒空段（普通回合逐位不变）。"""
    block = AX._ask_answer_resume_block_zh(ANSWER)
    assert "用户已在放松卡上作答" in block
    assert "不按「疾病」筛" in block
    assert "自动上屏" in block
    assert AX._ask_answer_resume_block_zh(None) == ""
    assert AX._ask_answer_resume_block_zh("not a dict") == ""


def test_decide_prompt_carries_resume_block():
    """decide prompt 注入作答事实段（与预算段同槽位）——注入缝 = chat_model。"""
    class _Fake:
        def __init__(self):
            self.invocations = []

        def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
            return self

        def invoke(self, messages):
            self.invocations.append(messages)
            return SimpleNamespace(content='{"done": true}')

    fake = _Fake()
    ctx = AX._AgentContext(chat_model=fake, model_name="chat-x")
    AX.decide({"utterance": BAD_QUERY, "ask_answer": ANSWER,
               "steps": [{"verb": "rank", "ok": True,
                          "result": {"health": "healthy", "total": 9,
                                     "query": BAD_QUERY}}],
               "finish_vetoes": 0, "reask_writes": []},
              runtime=SimpleNamespace(context=ctx))
    assert len(fake.invocations) == 1
    prompt = "".join(str(getattr(m, "content", "") or "")
                     for m in fake.invocations[0])
    assert "用户已在放松卡上作答" in prompt
    assert "不按「疾病」筛" in prompt


# ---------------------------------------------------------------- fail-soft：turn 层

def test_turn_ask_answer_fallback_when_agent_unavailable(monkeypatch):
    """agent/LLM 不在场 + ask_answer → 纯检索路由（via=ask_answer_fallback）：
    等同旧 cbCommit 行为，前端随之走 /api/recommend；retrieval 恒 None。"""
    monkeypatch.setattr(AX, "agent_available", lambda: False)
    out = turn.route_turn(BAD_QUERY, config=CFG, ask_answer=ANSWER)
    assert out["route"] == "search"
    assert out["via"] == "ask_answer_fallback"
    assert out["query"] == BAD_QUERY
    assert out["retrieval"] is None and out["result_payload"] is None


def test_turn_ask_answer_non_dict_ignored(monkeypatch):
    """ask_answer 非 dict（脏入参）→ 按未携带处理，走正常分流（不误触 fail-soft）。"""
    monkeypatch.setattr(AX, "agent_available", lambda: False)
    out = turn.route_turn(BAD_QUERY, config=CFG, ask_answer="junk")
    assert out["route"] == "search"
    assert out["via"] != "ask_answer_fallback"  # 不误触作答 fail-soft


# ---------------------------------------------------------------- webapp 入参闸

def test_sanitize_ask_answer_rejects_bad_kind():
    """kind 非 "ask.relax" → 400 bad_ask_answer（与端点其它入参校验同径）。"""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as ei:
        webapp._sanitize_ask_answer({"kind": "magic"})
    assert ei.value.status_code == 400
    assert ei.value.headers.get("X-Error-Code") == "bad_ask_answer"
    with pytest.raises(HTTPException):
        webapp._sanitize_ask_answer({"no_kind": True})


def test_sanitize_ask_answer_accepts_and_truncates():
    """合法载荷收编：kind 恒 ask.relax；label/message/choice_key 定长截断；
    suppress 过 `_sanitize_suppressed` 单一真源；None → None。"""
    assert webapp._sanitize_ask_answer(None) is None
    out = webapp._sanitize_ask_answer(
        {"kind": "ask.relax", "choice_key": "dim:disease",
         "label": "不按「疾病」筛", "message": "条件互相打架",
         "suppress": ["include:disease"]})
    assert out == ANSWER
    long = webapp._sanitize_ask_answer(
        {"kind": "ask.relax", "choice_key": "k" * 100,
         "label": "L" * 300, "message": "M" * 600, "suppress": []})
    assert len(long["choice_key"]) == 64
    assert len(long["label"]) == 200 and len(long["message"]) == 500


def test_utterance_endpoint_rejects_bad_ask_answer():
    """端点级：ask_answer.kind 非法 → 400（X-Error-Code: bad_ask_answer），不进分流。"""
    from fastapi.testclient import TestClient

    client = TestClient(webapp.app, base_url="http://127.0.0.1")
    res = client.post("/api/utterance",
                      json={"utterance": BAD_QUERY, "agent": False,
                            "provider": "mock", "use_llm": False,
                            "ask_answer": {"kind": "magic"}})
    assert res.status_code == 400
    assert res.headers.get("X-Error-Code") == "bad_ask_answer"
