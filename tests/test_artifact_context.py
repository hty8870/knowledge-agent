# -*- coding: utf-8 -*-
"""`/api/utterance` 课题上下文卡后端字段。**全离线**。

- 字段：可选 `artifact_context`（max_length=2000，超限 422）；缺省 None → 旧行为逐位不变。
- 透传：端点 → turn.route_turn → agent prompt（understand/route_consensus 结构化块，
  标注「用户附加上下文（仅供参考）」）；**不进** identifier 快速道 / 检索 query /
  action_plan 解析（utterance 独立字段，既有 500 字上限不受其影响）。
- 幂等：artifact_context 进 `_utterance_request_fp`——同 req_id 但上下文卡不同 → 409。
- 本地演示/无 AI 模式：字段被安全忽略，不报错、不进任何 prompt。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, turn
from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app
from dataset_recommender.llm.llm_client import LLMConfig

client = TestClient(app, base_url="http://127.0.0.1")

#: 假 route_turn 的固定返回体（喂 `_utterance_response_body` 所需的最小键集）。
FAKE_RESULT = {
    "route": "none",
    "query": "",
    "plan": {"verb": "none", "source": "rule"},
    "echo_zh": "假回声",
    "retrieval": None,
    "via": "rule",
}

CARD = "目标：人类肺单细胞；纳入：人类、肺；排除：小鼠；候选：GSE123456(已核验)"
CFG = LLMConfig(enable_llm=True, api_key="sk-artifact-context")


@pytest.fixture(autouse=True)
def _clean_registry():
    """幂等注册表是模块级进程态，用例间必须清空。"""
    webapp._UTT_IDEM.clear()
    yield
    webapp._UTT_IDEM.clear()


@pytest.fixture
def capturing_route_turn(monkeypatch):
    """记录 kwargs 的假 route_turn：返回固定结果，调用参数可查。"""
    calls: list[dict] = []

    def fake(text, **kwargs):
        calls.append({"text": text, "kwargs": kwargs})
        return dict(FAKE_RESULT)

    monkeypatch.setattr(turn, "route_turn", fake)
    return calls


# ---------------------------------------------------------------- 请求模型字段

def test_field_defaults_none_and_accepts_value():
    payload = webapp.UtteranceRequest(utterance="帮我找人类肺数据")
    assert payload.artifact_context is None, "缺省 = None，旧请求不带字段行为不变"
    payload2 = webapp.UtteranceRequest(utterance="x", artifact_context=CARD)
    assert payload2.artifact_context == CARD


def test_over_limit_422_and_boundary_accepted():
    # 超 2000 Unicode 字符 → 422（前端已截断，后端再兜一道硬闸）。
    over = "卡" * 2001
    r = client.post("/api/utterance", json={
        "utterance": "帮我找人类肺数据", "artifact_context": over})
    assert r.status_code == 422, "超限必须 422，不能静默吞成长文本"
    # 恰好 2000 字符 → 校验通过（后续路由被假实现接管，只验契约门）。
    boundary = "卡" * 2000
    r2 = client.post("/api/utterance", json={
        "utterance": "帮我找人类肺数据", "artifact_context": boundary})
    assert r2.status_code == 200, "恰好 2000 字符必须放行"


# ---------------------------------------------------------------- 端点 → route_turn 透传

def test_field_flows_to_route_turn_non_streaming(capturing_route_turn):
    r = client.post("/api/utterance", json={
        "utterance": "帮我找人类肺数据", "artifact_context": CARD})
    assert r.status_code == 200
    assert capturing_route_turn[0]["kwargs"]["artifact_context"] == CARD
    # 独立字段：不进用户原话（route_turn 收到的 text 仍是纯 utterance）。
    assert capturing_route_turn[0]["text"] == "帮我找人类肺数据"


def test_field_flows_to_route_turn_streaming(capturing_route_turn):
    r = client.post("/api/utterance", json={
        "utterance": "帮我找人类肺数据", "artifact_context": CARD, "stream": True})
    assert r.status_code == 200
    assert capturing_route_turn[0]["kwargs"]["artifact_context"] == CARD
    assert capturing_route_turn[0]["text"] == "帮我找人类肺数据"


def test_missing_field_passes_empty_string(capturing_route_turn):
    """缺省兼容：不带字段的旧请求逐位不变——route_turn 收到空串、不报错。"""
    r = client.post("/api/utterance", json={"utterance": "帮我找人类肺数据"})
    assert r.status_code == 200
    assert capturing_route_turn[0]["kwargs"]["artifact_context"] == ""


# ---------------------------------------------------------------- 幂等指纹

def _fp(text="帮我找人类肺数据", **overrides):
    fields = {"utterance": text, "has_results": False, "result_total": 0,
              "query": "", "current_filters": None, "sources": None,
              "provider": "mock", "use_llm": False, "mock_llm": False,
              "api_key": None, "base_url": "", "model": None, "agent": True,
              "top_k": None, "rerank": "", "recall": "", "strategy": "",
              "facet_filters": None, "suppressed_constraints": None,
              "lenient_dims": None, "date_from": "", "date_to": "", "polish": True,
              "req_id": None, "stream": False}
    fields.update(overrides)
    payload = webapp.UtteranceRequest(**fields)
    return webapp._utterance_request_fp(text, payload, "mock", False, False)


def test_fingerprint_differs_by_artifact_context():
    fp_a = _fp(artifact_context=CARD)
    fp_b = _fp(artifact_context=CARD + "（追加一句）")
    assert fp_a != fp_b, "同 req_id 但上下文卡不同 = 另一次请求，指纹必须分开"
    assert _fp(artifact_context=CARD) == fp_a, "同卡必须恒同指纹（幂等性不变）"


def test_fingerprint_missing_and_empty_identical():
    """缺省与空串同指纹：不带字段的旧客户端与带空串的新客户端同号重发互不撞 409。"""
    assert _fp() == _fp(artifact_context=""), "缺省 None 与空串必须同指纹"


def test_same_req_id_different_card_409(capturing_route_turn):
    """端点级：同 req_id、同 utterance，上下文卡不同 → 撞指纹 409（不当成同一次重发）。"""
    body = {"utterance": "帮我找人类肺数据", "req_id": "b4-1", "artifact_context": CARD}
    r1 = client.post("/api/utterance", json=body)
    assert r1.status_code == 200
    body2 = dict(body, artifact_context=CARD + "（变了）")
    r2 = client.post("/api/utterance", json=body2)
    assert r2.status_code == 409
    # 同卡原样重发 → 幂等缓存体（不二次执行路由）。
    r3 = client.post("/api/utterance", json=body)
    assert r3.status_code == 200
    assert len(capturing_route_turn) == 1, "同号同卡重发不重跑路由"


# ---------------------------------------------------------------- 不进路由/检索/证据（负向断言）

def test_identifier_fast_path_ignores_artifact_context():
    """编号快速道只看原话：带上下文卡也不能把编号句变成别的。"""
    out = turn.route_turn("GSE123456", config=CFG, artifact_context=CARD)
    assert out["route"] == "search"
    assert out["query"] == "GSE123456", "检索句必须是原话，上下文卡不得混入"
    assert CARD not in out["query"]
    assert out["via"] == "identifier"


def test_agent_off_mode_ignores_artifact_context(monkeypatch):
    """「AI 执行」关（本地演示/无 AI 模式）：字段被安全忽略，不报错、不进任何产物。"""
    called: list = []
    monkeypatch.setattr(agent_exec, "plan_with_agent", lambda *a, **k: called.append(1))
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: None)
    out = turn.route_turn("帮我找人类肺数据", config=CFG, use_agent=False,
                          artifact_context=CARD)
    assert called == [], "C 关：分流器永不启动"
    assert out["route"] == "search" and out["via"] == "rule_direct"
    assert out["query"] == "帮我找人类肺数据"
    assert CARD not in out["query"] and CARD not in str(out["echo_zh"] or "")


def _stub_flight(monkeypatch):
    """保底分支的无标记句会就地起 `_RagFlight`（真实检索线程）——测试替身换 noop，
    让「保底/检索句」用例只验证上下文卡隔离，不付真实语料开销。"""
    class _NoopFlight:
        def __init__(self, *a, **k):
            pass

        def start(self):
            pass

        def join(self):
            return None

    monkeypatch.setattr(turn, "_RagFlight", _NoopFlight)


def test_action_plan_fallback_never_sees_artifact_context(monkeypatch):
    """保底单次分类（agent 缺席 → plan_action）：签名没有 artifact_context——
    上下文卡绝不进入 action_plan 的 utterance 解析。"""
    _stub_flight(monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(agent_exec, "agent_available", lambda: False)
    monkeypatch.setattr(AP, "plan_action", lambda text, **kw: seen.append(
        {"text": text, "kw": kw}) or AP._blank_plan(source="llm"))
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "search.new", "quoted": "帮我找人类肺数据",
         "effective_query": "人类肺数据", "confidence": "high"}, ensure_ascii=False,
    ))
    out = turn.route_turn("帮我找人类肺数据", config=CFG, artifact_context=CARD)
    assert seen, "plan_action 保底应被调用"
    assert "artifact_context" not in seen[0]["kw"], "保底解析不得接收上下文卡"
    assert seen[0]["text"] == "帮我找人类肺数据", "解析对象必须是纯用户原话"
    assert CARD not in out["query"]


def test_route_search_query_never_contains_card(monkeypatch):
    """检索句（route=search 的 query）不得混入上下文卡——它要拿去 /api/recommend。"""
    _stub_flight(monkeypatch)
    seen: list[dict] = []
    monkeypatch.setattr(agent_exec, "agent_available", lambda: False)
    monkeypatch.setattr(AP, "plan_action", lambda text, **kw: seen.append(text) or AP._blank_plan(
        source="llm", verb_zh="检索", kind="query"))
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "search.new", "quoted": "帮我找人类肺数据",
         "effective_query": "人类肺数据", "confidence": "high"}, ensure_ascii=False,
    ))
    out = turn.route_turn("帮我找人类肺数据", config=CFG, artifact_context=CARD)
    assert CARD not in out["query"], "检索 query 绝不含上下文卡"


# ---------------------------------------------------------------- agent prompt 透传（组装处断言）

class _FakeToolsModel:
    """tools 模式替身（同 test_agent_exec.py）：bind_tools 记录工具表；invoke 弹预置答案。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.bound_tools = tools
        self.tool_choice = tool_choice
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


class _FakeJsonModel:
    """JSON 降级替身（同 test_agent_exec.py）：bind_tools 抛错，invoke 弹 content。"""

    def __init__(self, *contents):
        self.contents = list(contents)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        raise RuntimeError("provider does not support tool calling")

    def invoke(self, messages):
        self.invocations.append(messages)
        return AIMessage(content=self.contents.pop(0))


def _tool_call(verb, **args):
    return AIMessage(
        content="",
        tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}],
    )


def _plan(utterance, model, *, artifact_context=""):
    return agent_exec.plan_with_agent(
        utterance,
        has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model, artifact_context=artifact_context,
    )


def test_understand_prompt_carries_card_not_in_user_sentence():
    """understand 的 prompt：卡作结构化块（标注「仅供参考」）出现在「用户这一句」之前，
    用户原话本身逐字不变——卡绝不拼进用户原话。"""
    fake = _FakeToolsModel(_tool_call("none", quoted="", confidence="high"))
    plan, _ = _plan("今天天气怎么样", fake, artifact_context=CARD)
    assert plan["verb"] == "none"
    # 首帧 messages 是 understand 的 [SystemMessage, HumanMessage(context)]。
    understand_text = " ".join(
        str(getattr(m, "content", "") or "") for m in fake.invocations[0])
    assert "用户附加上下文" in understand_text, "卡必须进 agent prompt"
    assert "仅供参考" in understand_text, "必须标注「仅供参考」"
    assert CARD in understand_text
    # 用户原话尾段逐字不变：卡内容不出现在「用户这一句」段内。
    tail = understand_text.split("----- 用户这一句 -----")[1]
    assert tail.strip() == "今天天气怎么样"
    assert CARD not in tail, "卡不得混进用户这一句"


def test_understand_prompt_without_card_unchanged():
    """缺省不带卡：prompt 与旧版逐位一致（无卡块）。"""
    fake = _FakeToolsModel(_tool_call("none", quoted="", confidence="high"))
    plan, _ = _plan("今天天气怎么样", fake)
    assert plan["verb"] == "none"
    understand_text = " ".join(
        str(getattr(m, "content", "") or "") for m in fake.invocations[0])
    assert "用户附加上下文" not in understand_text


def test_json_fallback_prompt_carries_card():
    """工具通道失败 → JSON 兜底重问：同口径携带卡（只发 json_prompt，独立补块）。"""
    fake = _FakeJsonModel(json.dumps(
        {"verb": "none", "quoted": "", "confidence": "high", "reason": "闲聊"},
        ensure_ascii=False,
    ))
    plan, _ = _plan("今天天气怎么样", fake, artifact_context=CARD)
    assert plan["verb"] == "none"
    fallback_text = " ".join(
        str(getattr(m, "content", "") or "") for m in fake.invocations[-1])
    assert "用户附加上下文" in fallback_text and CARD in fallback_text


def test_route_consensus_prompt_carries_card(monkeypatch):
    """route_consensus 的 prompt 同口径携带卡（插在「用户原话」之前）。"""
    from types import SimpleNamespace

    prompts: list = []
    monkeypatch.setattr(agent_exec, "_run_route_consensus",
                        lambda model, prompt, usage_sink=None, **_kwargs: (
                            prompts.append(prompt) or ("general", [])))
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None,
        on_route_verdict=None, route_extra_zh="", artifact_context=CARD))
    state = {"utterance": "今天天气怎么样", "has_results": False,
             "result_total": 0, "current_query": "", "current_filters": [],
             "retrieval": None, "artifact_context": CARD}
    out = agent_exec.route_consensus(state, runtime=runtime)
    assert out["route_scope"] == "general"
    assert "用户附加上下文" in prompts[0] and CARD in prompts[0]
    tail = prompts[0].split("----- 用户原话 -----")[1]
    assert tail.strip() == "今天天气怎么样", "原话尾段逐字不变"


def test_route_consensus_without_card_unchanged(monkeypatch):
    """缺省不带卡：route_consensus 的 prompt 无卡块。"""
    from types import SimpleNamespace

    prompts: list = []
    monkeypatch.setattr(agent_exec, "_run_route_consensus",
                        lambda model, prompt, usage_sink=None, **_kwargs: (
                            prompts.append(prompt) or ("general", [])))
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None,
        on_route_verdict=None, route_extra_zh="", artifact_context=""))
    state = {"utterance": "今天天气怎么样", "has_results": False,
             "result_total": 0, "current_query": "", "current_filters": [],
             "retrieval": None}
    agent_exec.route_consensus(state, runtime=runtime)
    assert "用户附加上下文" not in prompts[0]
