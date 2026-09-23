# -*- coding: utf-8 -*-
"""「初步结果先行 + 信息流升级」后端专项门。**全离线**：

- 机械闸（设计约定，全与）逐项击破：正例（"human blood"，261 条真实命中）只发一次
  preliminary 且载荷与 /api/recommend 同形；on_event 缺席 / 非 agent 路径（LLM 未武装）
  / 零命中（"小鼠 胰腺癌"）/ 规则动作标记（"human blood 帮我打包"——检索照出 261 条、
  唯独 markers 非空，精准击破第 3 条）/ clarify（"不需要fastq的人类肺数据"）各击破一条；
  monkeypatch 替身不填 meta_out（既有 seam 形态）→ meta=None fail-closed 不发。
- rule_match_summary 升级：search_params 全管线与旧轻量概览的**投影逐键同形**
  （top_titles 恒切前 3、total 未截断——与 top_k 无关），meta_out 接住 WorkflowResult。
- final additive 键三档：result_payload None（无批）/ 非空（环内 search.rerun 采纳，
  从 plan.steps 实录扫出；**或镜像 active 批**——批次机制常驻，
  仅 preliminary 批时 legacy result_payload 镜像该批，b 档判定改用独立哨兵
  loop_payload）；preliminary_final 显式判定「润色不会跑 = ¬(LLM 武装 ∧
  polish 子开关)」（收尾：ubRouteBody 第 10 参 polish 落地，b 档解锁——
  polish=false 且其余条件满足 → True；polish=true/缺省 + 武装 → 润色会跑 → False）。
- agent 图级（langgraph + _FakeToolsModel 脚本驱动，与 test_search_rerun 同 seam）：
  tool_start（node/工具）**先于**对应 step 落帧，且 label_zh 逐字一致（前端按 label
  匹配 pending 行）；rescue/无回调路径 on_progress=None 自然静默。
- /api/utterance 端点：10 个新检索参数与 /api/recommend 同口径收敛（垃圾值安全默认、
  非法日期 400、倒挂 400）；非流式响应体 additive 两键缺省保守、既有键集零漂移；
  流式 on_event kind 透传（preliminary/tool_start/step 不再一律打 "step"）；
  幂等指纹覆盖新参数（同号不同 recall/polish → 409）。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec, turn  # noqa: E402
from dataset_recommender.app import webapp  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

#: 收敛后的典型检索参数（与 webapp 端点产出同构；polish 缺省 true，recommend 同口径）。
SP = {
    "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None, "lenient_dims": None,
    "date_from": "", "date_to": "", "polish": True,
}

#: 武装到「只要走真 LLM 就一定会发调用」的配置（注入替身时只过闸、不触网）。
CFG = LLMConfig(enable_llm=True, api_key="sk-test-fixture")

_SEARCH_PLAN = {
    "kind": "query", "verb": "search.new", "source": "agent",
    "llm_status": "ok", "effective_query": "", "steps": [],
}
_EXEC_PLAN = {
    "kind": AP.EXEC, "verb": "pack.download", "source": "agent",
    "llm_status": "ok", "steps": [],
}


def _stub_agent(monkeypatch, plan, *, verdict: str = "search"):
    """agent 图替身：agent_available 恒真 + plan_with_agent_events 回脚本 plan。

    （并发分流 v3.1）重钉：preliminary 发射点从图前移进 understand 节点入口——
    替身模拟 understand 节点在 route_scope≠action 时调 retrieval_provider（join +
    发射，r3 关键核查②回填）；verdict="action" 时模拟 action 路线**不调** provider
    （action 永不发射）。collector 里出现 preliminary 必为 provider 发射所发。"""
    def _fake_events(*a, **k):
        if verdict != "action":
            provider = k.get("retrieval_provider")
            if provider is not None:
                provider()  # 模拟 understand 入口：join/deferred 补跑 + 闸过则发射
        return (dict(plan), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", _fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: (dict(plan), []))


# ---------------------------------------------------------------- 机械闸（设计约定全与逐项击破）

def test_gate_positive_emits_preliminary_exactly_once(monkeypatch):
    """正例：agent 图路径 + 流式回调 + 真实命中 + 无动作标记 → preliminary 恰发一次，
    载荷与 /api/recommend 同形（recommend_payload 真源）；final 两键保守默认。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    prelim = [e for k, e in events if k == "preliminary"]
    assert len(prelim) == 1, "preliminary 最多一次（单调用点）"
    payload = prelim[0]
    assert payload["ok"] is True
    assert payload["result_total"] > 0 and payload["results"]
    assert payload["resolution_status"] == "results"
    assert out["route"] == "search" and out["via"] == "agent"
    # 批次机制常驻：仅 preliminary 批时 legacy result_payload **镜像
    # active 批**（过渡期回退兼容）——不再是 None；批次两键随批出现。
    assert out["result_payload"] is not None and out["result_payload"]["ok"] is True
    assert out["result_batches"][-1]["kind"] == "preliminary"
    assert out["active_batch"] == out["result_batches"][-1]["batch_id"]
    # polish=true（SP 缺省）+ LLM 武装 → 润色会跑 → 必须重检，b 档不成立。
    assert out["preliminary_final"] is False


def test_preliminary_final_true_when_polish_off(monkeypatch):
    """b 档解锁（收尾，ubRouteBody 第 10 参 polish）：polish=false 显式关闭
    → 润色恒不会跑（与 LLM 状态无关，与 /api/recommend 的 use_llm∧polish 同口径）；
    发过 preliminary ∧ 无环内采纳 ∧ 无改写 ∧ rerank=off → preliminary_final=True。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)),
        search_params={**SP, "polish": False})
    assert [k for k, _ in events].count("preliminary") == 1
    assert out["route"] == "search" and out["via"] == "agent"
    # result_payload 镜像 preliminary 批（非 None）——b 档判定看的是
    # 独立哨兵 loop_payload（无环内上屏），不是这个镜像键。
    assert out["result_payload"] is not None
    assert out["preliminary_final"] is True


def test_preliminary_final_false_when_polish_on_or_default(monkeypatch):
    """对偶钉：polish=true 与**缺省**（缺省 true，与 recommend 同口径）两种形态 +
    LLM 武装 → 润色会跑 → 恒 False（宁可重检不跳检）。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    sp_default = {k: v for k, v in SP.items() if k != "polish"}  # 缺省不传
    for sp in ({**SP, "polish": True}, sp_default):
        events: list = []
        out = turn.route_turn(
            "human blood", config=CFG,
            on_event=lambda k, e: events.append((k, e)), search_params=sp)
        assert [k for k, _ in events].count("preliminary") == 1
        assert out["preliminary_final"] is False


def test_gate_breaks_without_on_event(monkeypatch):
    """击破条件 1（非流式无回调）：on_event=None → 走 plan_with_agent 薄封装，无 preliminary。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    out = turn.route_turn("human blood", config=CFG, search_params=SP)
    assert out["route"] == "search"
    assert out["preliminary_final"] is False


def test_gate_breaks_when_llm_not_armed(monkeypatch):
    """击破条件 1'（非 agent 图路径）：LLM 未武装 → 保底路径，on_event 在场也不发。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=LLMConfig(),  # enable_llm=False → should_use_llm 不过
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events if k == "preliminary"] == []
    assert out["route"] == "search" and out["via"] != "agent"
    assert out["preliminary_final"] is False


def test_gate_breaks_on_zero_hit(monkeypatch):
    """击破条件 2（零命中）：「小鼠 胰腺癌」恒 no_match → 不发（救回链负责，互不越界）。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    turn.route_turn(
        "小鼠 胰腺癌", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events if k == "preliminary"] == []


def test_gate_breaks_on_action_markers(monkeypatch):
    """击破条件 3（规则动作标记）：「human blood 帮我打包」检索照出 261 条（打包已收进
    FILLER_GRAMMAR 不炸检索）——唯独 markers 非空，精准击破第 3 条，不发 preliminary。
    marker 命中 → 不起 flight 直接进图（纯执行不打印）；action 路线 never 发射。"""
    _stub_agent(monkeypatch, _EXEC_PLAN, verdict="action")
    events: list = []
    out = turn.route_turn(
        "human blood 帮我打包", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events if k == "preliminary"] == []
    assert out["route"] == "tool"  # 动作句由工具环承接，互不越界
    assert out["result_payload"] is None and out["preliminary_final"] is False
    # breaking：tool 路线 retrieval 由摘要 dict 变 None + additive retrieval_note。
    assert out["retrieval"] is None
    assert out["retrieval_note"] == "skipped_action_marker"  # marker 分支未起 flight


def test_gate_breaks_on_clarification(monkeypatch):
    """击破条件 4（clarify 投影）：status=clarification_required → 被 results 条件天然覆盖。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    turn.route_turn(
        "不需要fastq的人类肺数据", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events if k == "preliminary"] == []


def test_gate_fail_closed_when_meta_missing(monkeypatch):
    """既有 seam 形态（test_turn_fallback_audit 的 lambda *a, **k 替身）不填 meta_out
    → meta=None → fail-closed 不发 preliminary。"""
    monkeypatch.setattr(turn, "rule_match_summary", lambda *a, **k: {
        "status": "results", "total": 5, "top_titles": ["x"],
        "abstain_reason": "", "unresolved_terms": [], "note": "",
    })
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events if k == "preliminary"] == []
    assert out["route"] == "search"


# ---------------------------------------------------------------- rule_match_summary 升级

def test_rule_match_summary_projection_identical_with_search_params():
    """升级前后投影逐键同形：top_titles 恒切前 3、total 是未截断总数——与 top_k 无关；
    meta_out 接住同一次运行的 WorkflowResult（preliminary 载荷与闸判定的数据源）。"""
    legacy = turn.rule_match_summary("human blood")
    holder: list = []
    upgraded = turn.rule_match_summary("human blood", search_params=SP, meta_out=holder)
    assert upgraded == legacy
    assert upgraded["status"] == "results" and upgraded["total"] > 0
    assert len(upgraded["top_titles"]) <= 3
    assert len(holder) == 1
    assert int(holder[0].result_total) == upgraded["total"]


def test_rule_match_summary_honors_search_params_filters():
    """真实参数真的进管线：date 窗口收窄到不可能区间（from==to 的合法单日）与宽口相比，
    total 不增；projections 自身不炸（绝不抛异常契约）。"""
    wide = turn.rule_match_summary("human blood", search_params=SP)
    narrow = turn.rule_match_summary("human blood", search_params={
        **SP, "date_from": "1990-01-01", "date_to": "1990-01-01"})
    assert narrow["status"] in ("results", "no_match")
    assert narrow["total"] <= wide["total"]


# ---------------------------------------------------------------- final additive 键三档

def test_result_payload_scanned_from_adopted_rerun_step(monkeypatch):
    """a 档：环内 search.rerun 采纳 → route_turn 从 plan.steps 实录扫出 payload 挂 final；
    preliminary 照发（闸独立），preliminary_final 恒 False（有采纳必重检语义不适用）。"""
    adopted_payload = {"ok": True, "result_total": 7, "results": [{"dataset_name": "x"}]}
    plan = {
        "kind": AP.EXEC, "verb": "search.rerun", "source": "agent", "llm_status": "ok",
        "steps": [
            {"verb": "search.rerun", "ok": True,
             "result": {"adopted": False, "reason": "rewrite_no_change_kept_original",
                        "payload": None}},
            {"verb": "search.rerun", "ok": True,
             "result": {"adopted": True, "payload": adopted_payload}},
        ],
    }
    _stub_agent(monkeypatch, plan)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert out["route"] == "tool"
    assert out["result_payload"] is adopted_payload, "多个采纳/未采纳步取最后一个采纳者"
    assert out["preliminary_final"] is False


def test_result_payload_none_when_rerun_not_adopted(monkeypatch):
    """c 档对偶：search.rerun 跑了但未采纳（改空/同集）→ result_payload None。"""
    plan = {
        "kind": AP.EXEC, "verb": "search.rerun", "source": "agent", "llm_status": "ok",
        "steps": [{"verb": "search.rerun", "ok": True,
                   "result": {"adopted": False, "reason": "rewrite_empty_kept_original",
                              "payload": None}}],
    }
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn("human blood", config=CFG, search_params=SP)
    assert out["result_payload"] is None
    assert out["preliminary_final"] is False


# ---------------------------------------------------------------- 图级：tool_start 先于 step（langgraph）

class _FakeToolsModel:
    """tools 模式替身：bind_tools 记录工具表并返回自身；invoke 依次弹出预置 AIMessage。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.bound_history = []
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.bound_history.append(tools)
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


def _tool_call(verb, **args):
    from langchain_core.messages import AIMessage
    return AIMessage(
        content="",
        tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}],
    )


def test_graph_tool_start_precedes_matching_step(monkeypatch):
    """图级全链：understand 提 search.rerun → execute 真跑（真实离线检索）→ decide 收尾
    → narrate。on_event 事件序钉死：node/tool 的 tool_start 都**先于**对应 step 落帧，
    且 label_zh 逐字一致（前端 pending 行按 label 匹配改行）。"""
    pytest.importorskip("langgraph", reason="langchain 扩展未安装：图级用例跳过")
    from langchain_core.messages import AIMessage
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换一组查询词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    events: list = []
    plan, trace = agent_exec.plan_with_agent_events(
        "human blood", has_results=True, result_total=10,
        config=CFG, retrieval=None,
        current_query="human blood", current_filters=None,
        chat_model=fake, on_event=lambda k, e: events.append((k, e)))
    assert plan["source"] == "agent"
    assert any(s.get("verb") == "search.rerun" and s.get("ok") for s in plan["steps"])

    kinds = [k for k, _ in events]
    assert kinds[0] == "tool_start", "分流共识的 node_start 是全链第一帧（常驻环首）"
    assert events[0][1] == {"verb": "node", "label_zh": "分流共识", "detail": ""}
    # 理解意图紧随其后（环首 tool_start→step 之后才是旧链第一帧）。
    assert events[1][0] == "step" and events[1][1]["label_zh"] == "分流共识"
    assert events[2] == ("tool_start", {"verb": "node", "label_zh": "理解意图", "detail": ""})

    # 工具 tool_start 先于对应 step，label 逐字一致。
    ts_idx = next(i for i, (k, e) in enumerate(events)
                  if k == "tool_start" and e["verb"] == "search.rerun")
    st_idx = next(i for i, (k, e) in enumerate(events)
                  if k == "step" and e["label_zh"] == "执行工具 · 检索新查询")
    assert ts_idx < st_idx
    assert events[ts_idx][1]["label_zh"] == events[st_idx][1]["label_zh"]

    # narrate 的 node_start 先于 narrate step。
    ns_idx = next(i for i, (k, e) in enumerate(events)
                  if k == "tool_start" and e["label_zh"] == "生成说明")
    nm_idx = next(i for i, (k, e) in enumerate(events)
                  if k == "step" and e["label_zh"] == "生成说明")
    assert ns_idx < nm_idx
    assert events[ns_idx][1]["verb"] == "node"


def test_graph_silent_without_on_event(monkeypatch):
    """无回调路径（rescue/非流式）：on_progress=None 自然静默，行为与旧版逐位一致。"""
    pytest.importorskip("langgraph", reason="langchain 扩展未安装：图级用例跳过")
    from langchain_core.messages import AIMessage
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换一组查询词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    plan, trace = agent_exec.plan_with_agent_events(
        "human blood", has_results=True, result_total=10,
        config=CFG, retrieval=None,
        current_query="human blood", current_filters=None,
        chat_model=fake)  # on_event 缺省
    assert plan["source"] == "agent"
    assert any(s.get("verb") == "search.rerun" and s.get("ok") for s in plan["steps"])


# ---------------------------------------------------------------- /api/utterance 端点

def _fake_route_result(**over):
    base = {
        "route": "search", "query": "human blood", "plan": None, "echo_zh": "",
        "retrieval": None, "via": "rule_direct", "needs_agent": False, "suggestions": [],
    }
    base.update(over)
    return base


def _capture_route_turn(monkeypatch, sink):
    """端点 route_turn 替身：捕获 search_params 等 kwargs，回最小合法结果。"""
    def fake(text, **kwargs):
        sink.append((text, kwargs))
        return _fake_route_result()
    monkeypatch.setattr(turn, "route_turn", fake)


def test_endpoint_search_params_converged_like_recommend(monkeypatch):
    """10 参收敛与 /api/recommend 同口径：垃圾值安全默认（rerank/recall/strategy）、
    合法值原样透传、分面/忽略/放宽走白名单 sanitizer（缺省 → 空 list）、
    polish 子开关原样透传（第 10 参，preliminary_final 判定用）。"""
    sink: list = []
    _capture_route_turn(monkeypatch, sink)
    r = TestClient(app, base_url="http://127.0.0.1").post("/api/utterance", json={
        "utterance": "human blood",
        "top_k": 20, "rerank": "garbage", "recall": "dense", "strategy": "AUTO",
        "date_from": "2020-01-01", "date_to": "2021-01-01", "polish": False,
    })
    assert r.status_code == 200
    assert len(sink) == 1
    sp = sink[0][1]["search_params"]
    assert sp == {
        "top_k": 20, "rerank": "off", "recall": "dense", "strategy": "auto",
        "facet_filters": [], "suppressed_constraints": [], "lenient_dims": [],
        "date_from": "2020-01-01", "date_to": "2021-01-01", "polish": False,
    }


def test_endpoint_invalid_date_400():
    """非法日期与倒挂窗口 → 400，文案与 /api/recommend 同口径（不静默吞条件）。"""
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/utterance", json={
        "utterance": "human blood", "date_from": "2020-13-45"})
    assert r.status_code == 400 and "date_from" in r.json()["detail"]
    r = client.post("/api/utterance", json={
        "utterance": "human blood", "date_from": "2022-01-01", "date_to": "2021-01-01"})
    assert r.status_code == 400 and "颠倒" in r.json()["detail"]


def test_endpoint_non_stream_body_additive_only(monkeypatch):
    """非流式零影响：既有键集零漂移，additive 键缺省保守（None / False）。

     再加 `policy_id`（route=="search" 且组装成功时非 None；本用例检索句走
    search 路线故键恒在；非 search 路线或组装失败时键不出现）。
    """
    sink: list = []
    _capture_route_turn(monkeypatch, sink)
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/utterance", json={"utterance": "human blood"})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "ok", "route", "query", "plan", "echo_zh", "retrieval", "via",
        "needs_agent", "suggestions", "agent",
        "result_payload", "preliminary_final",  # 新增 additive 两键，仅此两键
        "policy_id", "policy_id_str",  # 结构体 + 稳定紧凑串（route=="search" 时）
    }
    assert body["result_payload"] is None and body["preliminary_final"] is False
    assert body["policy_id"]["schema"] == "biodata-policy-id/1"
    assert body["policy_id_str"].startswith("bpol1:")
    # 缺省请求也带收敛后的 search_params（缺省=现状行为：全安全默认，polish 缺省 true）。
    assert sink[0][1]["search_params"] == {
        "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
        "facet_filters": [], "suppressed_constraints": [], "lenient_dims": [],
        "date_from": "", "date_to": "", "polish": True,
    }


def _sse_frames(text: str):
    frames = []
    for chunk in text.split("\n\n"):
        chunk = chunk.strip()
        if chunk.startswith("data: "):
            frames.append(json.loads(chunk[len("data: "):]))
    return frames


def test_endpoint_stream_kind_passthrough(monkeypatch):
    """流式 kind 透传 + 确定性帧序（v3.1，r3 裁定）：tool_start(共识) →
    step(共识) → tool_start(understand) → preliminary → step(understand) → final——
    preliminary 不再保证首帧（verdict-gated，understand 入口发射），恒在
    tool_start(understand) 之后、final 之前；final 体 additive 键原样携带。"""
    def fake(text, **kwargs):
        on_event = kwargs.get("on_event")
        on_event("tool_start", {"verb": "node", "label_zh": "分流共识", "detail": ""})
        on_event("step", {"node": "route_consensus", "label_zh": "分流共识",
                          "detail": "x", "ok": True, "ms": 1})
        on_event("tool_start", {"verb": "node", "label_zh": "理解意图", "detail": ""})
        on_event("preliminary", {"ok": True, "result_total": 3, "results": []})
        on_event("step", {"node": "understand", "label_zh": "理解意图",
                          "detail": "x", "ok": True, "ms": 1})
        return _fake_route_result(
            via="agent", result_payload={"ok": True, "result_total": 9},
            preliminary_final=False)
    monkeypatch.setattr(turn, "route_turn", fake)
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/utterance", json={"utterance": "human blood", "stream": True})
    assert r.status_code == 200
    frames = _sse_frames(r.text)
    assert [f["event"] for f in frames] == [
        "tool_start", "step", "tool_start", "preliminary", "step", "final"]
    assert frames[0]["data"] == {"verb": "node", "label_zh": "分流共识", "detail": ""}
    assert frames[3]["data"]["result_total"] == 3
    assert frames[2]["data"] == {"verb": "node", "label_zh": "理解意图", "detail": ""}
    final = frames[-1]["data"]
    assert final["result_payload"] == {"ok": True, "result_total": 9}
    assert final["preliminary_final"] is False


def test_endpoint_idem_fingerprint_covers_search_params(monkeypatch):
    """幂等指纹覆盖新参数：同 req_id 同一句但 recall/polish 不同 → 409（撞号，不是重发）。"""
    sink: list = []
    _capture_route_turn(monkeypatch, sink)
    client = TestClient(app, base_url="http://127.0.0.1")
    req = {"utterance": "human blood", "req_id": "fp-001", "recall": "off"}
    r1 = client.post("/api/utterance", json=req)
    assert r1.status_code == 200
    r2 = client.post("/api/utterance", json={**req, "recall": "dense"})
    assert r2.status_code == 409
    r3 = client.post("/api/utterance", json=req)
    assert r3.status_code == 200, "同号同参 = 合法重发，回缓存体"
    r4 = client.post("/api/utterance", json={**req, "polish": False})
    assert r4.status_code == 409, "polish 进指纹：同号不同 polish 也是另一次请求"
