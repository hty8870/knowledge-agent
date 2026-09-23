# -*- coding: utf-8 -*-
"""「并发分流与确定性 RAG 策略」· turn/agent_exec 级门（设计约定）。**全离线**：

- marker 分层起跑：marker 命中不起 flight（零 RAG）直接进图；无标记起飞；早退
  （编号快速道 / AI 执行关）不起飞；llm_call 注入路径不变（agent_path=False）。
- verdict-gated 发射：understand 入口恰好一次；action 永不发射；保底分支「非 EXEC 才
  发射 / EXEC 抑制」；非流式不发；emitted 回填 preliminary_sent（b 档）。
- 标记误伤 lany 补起（marker 假阳性从此有早首屏，设计约定有意改进）。
- tool 路线 retrieval=None + retrieval_note（breaking 契约，设计约定）。
- 杠杆②（设计 v3.2 设计约定/设计约定）：图起跑前同步关键词快速计数段注入 route_extra_zh——
  有标记复合行 / 无标记关键词段行、按 status 如实写（命中/零命中/弃权/error）、
  fail-open（fixed 摘要异常不成为新故障源）、诚实不变量（补偿行绝不含结果集标题）、
  marker 句仍零 RAG 起跑。
- agent_exec 层：route_extra_zh 只在有标记时出现；route_consensus 节点内零发射；
  understand 局部 resolved 汇合（本次 prompt 即见检索段、rescue 分支也用 resolved）；
  真图确定性帧序 tool_start(共识)→step(共识)→tool_start(understand)→preliminary→…。
"""
import json
import threading

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, turn
from dataset_recommender.llm.llm_client import LLMConfig

_SP = {
    "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None, "lenient_dims": None,
    "date_from": "", "date_to": "", "polish": True,
}
CFG = LLMConfig(enable_llm=True, api_key="sk-fake-turn")
_SEARCH_PLAN = {
    "kind": "query", "verb": "search.new", "source": "agent",
    "llm_status": "ok", "effective_query": "", "steps": [],
}
_EXEC_PLAN = {
    "kind": AP.EXEC, "verb": "pack.download", "source": "agent",
    "llm_status": "ok", "steps": [],
}


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    """每测试干净准入信号量（不串槽）；rule_match_summary 默认替换为确定性验证
    （**按真函数契约填 meta_out**——flight.meta 是发射闸与载荷的数据源）；
    默认清掉回退开关环境变量（既有用例钉 v3.1 并发路径，off 用例自己 setenv）。"""
    monkeypatch.setattr(turn, "_RAG_SEMAPHORE", threading.Semaphore(turn._RAG_MAX_CONCURRENT))
    monkeypatch.setattr(turn, "rule_match_summary", _fake_rule_match_summary)
    yield


class _FakeMeta:
    """最小 WorkflowResult 替身：recommend_payload 需要的字段齐备（发射载荷数据源）。"""
    resolution_status = "results"
    result_total = 5
    answer = "规则生成说明"
    pipeline = "rule-based"
    llm_attempted = False
    llm_succeeded = False
    llm_response_used = False
    llm_provider = ""
    llm_mode = "disabled"
    fallback = "rule-based formatting"
    fallback_reason = ""
    retrieved_data = []
    facets = []
    clarification = None
    coverage_caveats = []
    unused_query_terms = []
    or_handling = None
    active_filters = []
    interpretation = ""
    search_trace = []
    audit = None


def _fake_rule_match_summary(*a, **k):
    holder = k.get("meta_out")
    if holder is not None:
        holder.append(_FakeMeta())
    return {"status": "results", "total": 5, "top_titles": ["样本"],
            "abstain_reason": "", "unresolved_terms": [], "note": ""}


def _stub_agent(monkeypatch, plan, *, verdict="search", call_provider=True):
    """agent 图替身：模拟 understand 入口在 route_scope≠action 时调 provider；
    verdict="action" 时不调（action 永不发射）；call_provider=False 时模拟
    route_consensus 只回调 verdict hook（验证节点内零发射）。"""
    def _fake_events(*a, **k):
        hook = k.get("on_route_verdict")
        if hook is not None and not call_provider:
            hook(verdict)
        if call_provider and verdict != "action":
            provider = k.get("retrieval_provider")
            if provider is not None:
                provider()
        return (dict(plan), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", _fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: (dict(plan), []))
    return _fake_events


def _spy_flight(monkeypatch) -> list:
    made: list = []
    real = turn._RagFlight

    def spy(*a, **k):
        made.append(1)
        return real(*a, **k)

    monkeypatch.setattr(turn, "_RagFlight", spy)
    return made


# ---------------------------------------------------------------- marker 分层起跑

def test_marker_never_launches_flight(monkeypatch):
    """marker 命中 → 不起 flight 直接进图（纯执行零 RAG）；tool 路线 retrieval=None +
    note="skipped_action_marker"（breaking 契约）。"""
    _stub_agent(monkeypatch, _EXEC_PLAN, verdict="action")
    made = _spy_flight(monkeypatch)
    events: list = []
    out = turn.route_turn(
        "human blood 帮我打包", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert made == [], "marker 命中 → 零 RAG 起跑"
    assert out["route"] == "tool"
    assert out["retrieval"] is None
    assert out["retrieval_note"] == "skipped_action_marker"
    assert [k for k, _ in events] == []


def test_no_marker_launches_flight(monkeypatch):
    """无标记 → flight 起跑（准入信号量承接）∥ 图起跑。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    made = _spy_flight(monkeypatch)
    out = turn.route_turn("human blood", config=CFG, search_params=_SP)
    assert len(made) == 1, "无标记必须起飞一个 flight"
    assert out["retrieval"] is not None and out["retrieval"]["status"] == "results"


def test_early_exits_never_launch_flight(monkeypatch):
    """早退（编号快速道 / AI 执行关）不起飞。"""
    made = _spy_flight(monkeypatch)
    turn.route_turn("GSE123456", config=CFG)
    assert made == []
    turn.route_turn("人类肺癌数据", config=CFG, use_agent=False)
    assert made == []


def test_llm_call_injection_path_unchanged(monkeypatch):
    """llm_call 注入 = 测试隔离：永不走 agent（agent_path=False）；保底分支对无 marker
    句就地起 flight 供 plan_action 上下文（=今天同步时序，正确性保底）。"""
    called: list = []
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: called.append(1))
    made = _spy_flight(monkeypatch)
    # marker 句：零 RAG 起跑（与 agent 图路径同纪律）。
    out = turn.route_turn(
        "帮我打包前5条", has_results=True, result_total=5,
        llm_call=lambda p: json.dumps(
            {"verb": "pack.download", "quoted": "打包前5条", "limit": 5,
             "confidence": "high"}, ensure_ascii=False))
    assert called == [] and out["plan"]["source"] == "llm"
    assert made == [], "marker 句 + llm_call 注入也不起 flight"
    # 无 marker 句：保底就地起并 join（plan_action 拿到规则概览上下文）。
    made.clear()
    out2 = turn.route_turn(
        "人类肺癌数据", config=CFG,
        llm_call=lambda p: json.dumps(
            {"verb": "search.new", "quoted": "人类肺癌数据",
             "effective_query": "人类肺癌数据", "confidence": "high"},
            ensure_ascii=False))
    assert out2["route"] == "search" and out2["plan"]["source"] == "llm"
    assert len(made) == 1, "保底分支就地起 flight 供分类上下文"


# ---------------------------------------------------------------- verdict-gated 发射

def test_understand_emit_exactly_once_and_backfills(monkeypatch):
    """understand 入口发射恰好一次（主路径唯一发射点）；emitted 回填 preliminary_sent
    → b 档（preliminary_final）可判定（关键核查②）。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)),
        search_params={**_SP, "polish": False})
    assert [k for k, _ in events].count("preliminary") == 1
    assert out["route"] == "search" and out["via"] == "agent"
    assert out["result_batches"][-1]["kind"] == "preliminary"
    assert out["preliminary_final"] is True, "发射回填 preliminary_sent → b 档成立"
    # （设计约定）：每批必须带规范化检索范围指纹（契约级身份键），
    # 前端据它判「是否同一次 scope」——缺失 = 前端无从判同/去重。
    for _b in out["result_batches"]:
        assert "scope_fingerprint" in _b and _b["scope_fingerprint"], "批次缺 scope_fingerprint"


def test_batch_scope_fingerprint_is_stable_and_order_insensitive():
    """（设计约定）：scope 指纹是稳定哈希——sources/facet 顺序不影响结果（项序不参与 scope），
    换检索句或换条件即变。供前端判同/去重的唯一键必须确定性。"""
    f = turn._batch_scope_fingerprint
    base = {"facet_filters": [{"dim": "species", "value": "human"}],
            "suppressed_constraints": ["exclude:species"]}
    a = f("human lung", ["GEO", "10x Genomics"], base)
    b = f("human lung", ["10x Genomics", "GEO"], base)   # sources 顺序颠倒
    c = f("human lung", ["GEO", "10x Genomics"],
          {"facet_filters": [{"value": "human", "dim": "species"}],  # facet 键序不同（dict 值仍相等）
           "suppressed_constraints": ["exclude:species"]})
    assert a == b and b == c and len(a) == 64, "指纹应稳定且对 sources/facet 键序不敏感"
    assert a != f("human lung cancer", ["GEO", "10x Genomics"], base), "换检索句指纹必变"
    assert a != f("human lung", ["GEO", "10x Genomics"], {"facet_filters": [], "suppressed_constraints": ["exclude:species"]}), "换条件指纹必变"


def test_action_never_emits(monkeypatch):
    """action 路线永不发射（无 marker 也如此——verdict hook 置 abandoned 后不汇合）。"""
    _stub_agent(monkeypatch, _EXEC_PLAN, verdict="action")
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert [k for k, _ in events] == []
    assert out["route"] == "tool"
    assert out["retrieval"] is None
    assert out["retrieval_note"] == "discarded_action_route"


def test_lazy_restart_on_marker_mistake(monkeypatch):
    """标记误伤被翻案为 search/general → lazy 补起（设计约定有意改进：marker 假阳性
    从此有早首屏，且发射时路线已被共识证实非 action）。"""
    made: list = []
    real = turn._RagFlight

    def spy(*a, **k):
        made.append(1)
        return real(*a, **k)

    monkeypatch.setattr(turn, "_RagFlight", spy)

    def fake_events(*a, **k):
        hook = k.get("on_route_verdict")
        if hook is not None:
            hook("search")  # 共识翻案：marker 误伤
        provider = k.get("retrieval_provider")
        if provider is not None:
            provider()  # understand 入口：join + 发射
        return (dict(_SEARCH_PLAN), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: (dict(_SEARCH_PLAN), []))
    events: list = []
    out = turn.route_turn(
        "human blood 帮我打包", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert len(made) == 1, "lazy 补起恰好一个 flight（marker 分支本不起）"
    assert [k for k, _ in events].count("preliminary") == 1
    assert out["route"] == "search"
    assert out["retrieval"] is not None, "lazy 补起后 understand 汇合 retrieval"


def test_non_stream_no_preliminary(monkeypatch):
    """非流式同构：on_event 不在场 → 不发射（闸含 on_event）；retrieval 照常汇合。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    out = turn.route_turn("human blood", config=CFG, search_params=_SP)
    assert out["route"] == "search"
    assert out["retrieval"] is not None
    assert "result_batches" not in out
    assert out["preliminary_final"] is False


def test_fallback_emit_only_non_exec(monkeypatch):
    """保底分支：plan_action 判出非 EXEC（search/general 向）∧
    闸过 ∧ 未发射 → 发射。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)

    def boom(*a, **k):
        raise agent_exec.AgentUnavailable("测试注入：agent 图失败")

    monkeypatch.setattr(agent_exec, "plan_with_agent_events", boom)
    monkeypatch.setattr(agent_exec, "plan_with_agent", boom)
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "search.new", "quoted": "human blood",
         "effective_query": "human blood", "confidence": "high"},
        ensure_ascii=False))
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert [k for k, _ in events].count("preliminary") == 1
    assert out["route"] == "search"
    assert out["plan"].get("agent_fallback") is True


def test_fallback_suppresses_exec_and_none(monkeypatch):
    """保底分支 EXEC/none 一律抑制。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)

    def boom(*a, **k):
        raise agent_exec.AgentUnavailable("测试注入")

    monkeypatch.setattr(agent_exec, "plan_with_agent_events", boom)
    monkeypatch.setattr(agent_exec, "plan_with_agent", boom)
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "curate.check_updates", "quoted": "检查10x是否有更新", "source": "10x",
         "confidence": "high"}, ensure_ascii=False))
    events: list = []
    out = turn.route_turn(
        "检查10x是否有更新", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert [k for k, _ in events] == [], "EXEC 一律抑制（保底分支不发射）"
    assert out["route"] == "tool"


def test_route_consensus_node_zero_emission(monkeypatch):
    """route_consensus 节点内零 preliminary 发射：verdict hook 只标记，
    understand 入口才是主路径唯一发射点。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN, call_provider=False)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert [k for k, _ in events] == [], "只回调 verdict hook 不调 provider → 零发射"
    assert out["route"] == "search"
    assert out["retrieval"] is not None, "turn 层 return 装配仍汇合 retrieval"


# ---------------------------------------------------------------- agent_exec 层

def test_route_extra_zh_only_with_markers(monkeypatch):
    """route_extra_zh 只在有标记分支出现（缺省空串 = 今天逐位不变）。"""
    from types import SimpleNamespace
    prompts: list = []

    def spy(model, prompt, usage_sink=None, **_kwargs):
        prompts.append(prompt)
        return "search", [{"ok": True, "route": "search"}, {"ok": True, "route": "search"}]

    monkeypatch.setattr(agent_exec, "_run_route_consensus", spy)
    base = {"utterance": "下载top5", "has_results": False,
            "result_total": 0, "current_query": "", "current_filters": []}
    # 有标记：机械标记事实行拼进上下文尾部。
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None,
        on_route_verdict=None, route_extra_zh="规则动作标记：命中「下载」。"))
    agent_exec.route_consensus(dict(base, retrieval=None), runtime=runtime)
    assert "规则动作标记：命中「下载」" in prompts[0]
    # 无标记：route_extra_zh 缺省空串，上下文与今天逐位一致。
    runtime2 = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None,
        on_route_verdict=None, route_extra_zh=""))
    agent_exec.route_consensus(dict(base, retrieval={"status": "results", "total": 5}),
                               runtime=runtime2)
    assert "规则动作标记" not in prompts[1]
    assert "规则匹配命中 5 条" in prompts[1]


def test_route_consensus_calls_verdict_hook_but_emits_nothing(monkeypatch):
    """verdict hook 在 route_consensus 内被回调（只标记）；节点自身不发任何事件。"""
    from types import SimpleNamespace
    calls: list = []

    def spy(model, prompt, usage_sink=None, **_kwargs):
        return "action", [{"ok": True, "route": "action"}, {"ok": True, "route": "action"}]

    monkeypatch.setattr(agent_exec, "_run_route_consensus", spy)
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None,
        on_route_verdict=lambda route: calls.append(route),
        route_extra_zh=""))
    state = {"utterance": "下载top5", "has_results": False,
             "result_total": 0, "current_query": "", "current_filters": [],
             "retrieval": None}
    out = agent_exec.route_consensus(state, runtime=runtime)
    assert calls == ["action"], "verdict hook 被回调（标记 abandoned）"
    assert out["route_scope"] == "action"


def test_understand_local_resolved_visible_in_prompt(monkeypatch):
    """understand 局部 resolved 汇合（关键核查①）：provider 在场时本次 prompt 即见
    检索摘要（return 增量只惠及下游是不够的）。"""
    pytest.importorskip("langgraph", reason="langchain 扩展未安装：图级用例跳过")
    from langchain_core.messages import AIMessage

    def provider():
        return {"status": "results", "total": 5, "top_titles": ["样本甲"],
                "abstain_reason": "", "unresolved_terms": [], "note": ""}

    fake = _FakeToolsModel(
        # conftest 全局 stub route_consensus → general（不 invoke）；剧本从 understand 开始。
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish", "args": {"completion_report": "已重检"}, "id": "t"}]),
        AIMessage(content="已换词重检。"),
    )
    events: list = []
    plan, trace = agent_exec.plan_with_agent_events(
        "human blood", has_results=True, result_total=10,
        config=CFG, retrieval=None,
        current_query="human blood", current_filters=None,
        chat_model=fake, on_event=lambda k, e: events.append((k, e)),
        retrieval_provider=provider, on_route_verdict=None, route_extra_zh="")
    assert plan["source"] == "agent"
    # understand 的 prompt（第 1 次 invoke）必须含检索摘要。
    understand_msgs = fake.invocations[0]
    prompt_text = " ".join(str(getattr(m, "content", "") or "") for m in understand_msgs)
    assert "命中 5 条" in prompt_text, "本次 understand prompt 必须即见局部 resolved 摘要"
    assert "样本甲" in prompt_text
    # 帧序确定性（裁定）：tool_start(共识) → step(共识) → tool_start(understand)
    # → preliminary? → step(understand) → …
    kinds = [k for k, _ in events]
    assert kinds[:3] == ["tool_start", "step", "tool_start"]
    assert events[2][1]["label_zh"] == "理解意图"
    # 帧序断言：preliminary 在 tool_start(understand) 之后（如有）——本测试 provider
    # 的摘要无 meta（has_hits False）→ 不发射，故无 preliminary 帧，只验前 3 帧。


def test_understand_rescue_block_uses_resolved(monkeypatch):
    """rescue 分支的未收录词清单取自局部 resolved（关键核查①——不再读 state 旧值）。"""
    pytest.importorskip("langgraph", reason="langchain 扩展未安装：图级用例跳过")
    from types import SimpleNamespace
    from langchain_core.messages import AIMessage

    def provider():
        return {"status": "results", "total": 0, "top_titles": [],
                "abstain_reason": "", "unresolved_terms": ["神经"], "note": ""}

    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="去掉神经", query="小鼠胶质瘤",
                   confidence="high", reason="丢弃未收录词重检"),
    )
    ctx = agent_exec._AgentContext(
        chat_model=fake, retrieval_provider=provider,
        on_route_verdict=None, route_extra_zh="")
    runtime = SimpleNamespace(context=ctx)
    state = {"utterance": "去掉神经", "route_scope": "rescue",
             "has_results": True, "result_total": 0,
             "retrieval": None,
             "current_query": "小鼠神经胶质瘤", "current_filters": None,
             "raw": {}, "steps": []}
    out = agent_exec.understand(state, runtime=runtime)
    prompt_text = " ".join(str(getattr(m, "content", "") or "")
                           for m in fake.invocations[0])
    assert "「神经」在库里**没有收录**" in prompt_text, \
        "rescue 限制段必须用 provider 的局部 resolved（未收录词「神经」逐字进 prompt）"
    assert out["retrieval"] is not None and out["retrieval"]["unresolved_terms"] == ["神经"], \
        "return 增量带局部 resolved 供下游（repair/execute 经 state 读到）"


# ---------------------------------------------------------------- 杠杆②：共识信号补偿 v2（设计 v3.2 设计约定/设计约定）

def test_consensus_extra_zh_shapes(monkeypatch):
    """杠杆② route_extra_zh 内容：逐字复刻今天串行路径共识的检索概览段文案
    （前缀 + `_route_retrieval_zh`），有/无标记分支同构（fixture 验证命中 5 条）。"""
    expected = "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配命中 5 条。"
    assert turn._consensus_extra_zh("下载前 5 条数据") == expected
    assert turn._consensus_extra_zh("human blood") == expected


def test_keyword_count_status_shapes(monkeypatch):
    """杠杆② 按 status 如实写（经 `agent_exec._route_retrieval_zh` 同口径）：
    命中/零命中/弃权/error 四态文案，只含 status/total 与弃权原因，绝不含结果集标题。"""
    monkeypatch.setattr(turn, "rule_match_summary",
                        lambda *a, **k: {"status": "results", "total": 12, "top_titles": [],
                                         "abstain_reason": "", "unresolved_terms": [], "note": ""})
    assert turn._consensus_extra_zh("x") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配命中 12 条。"
    monkeypatch.setattr(turn, "rule_match_summary",
                        lambda *a, **k: {"status": "results", "total": 0, "top_titles": [],
                                         "abstain_reason": "", "unresolved_terms": [], "note": ""})
    assert turn._consensus_extra_zh("x") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配**零命中**（库中没有同时满足所有条件的记录）。"
    monkeypatch.setattr(turn, "rule_match_summary",
                        lambda *a, **k: {"status": "abstained", "total": 0, "top_titles": [],
                                         "abstain_reason": "unresolved_term",
                                         "unresolved_terms": ["plapdi"], "note": ""})
    assert turn._consensus_extra_zh("x") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：" \
        "规则匹配**整句弃权**（unresolved_term；未收录词：「plapdi」）。"
    monkeypatch.setattr(turn, "rule_match_summary",
                        lambda *a, **k: {"status": "error", "total": 0, "top_titles": [],
                                         "abstain_reason": "", "unresolved_terms": [], "note": "boom"})
    assert turn._consensus_extra_zh("x") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配这次没能跑（boom）。"


def test_keyword_count_line_never_contains_result_titles(monkeypatch):
    """诚实不变量（test_scoped_routing.py:236 同族）：补偿行只报 status/total，
    结果集标题绝不许进分流上下文。"""
    monkeypatch.setattr(turn, "rule_match_summary",
                        lambda *a, **k: {"status": "results", "total": 5,
                                         "top_titles": ["SENTINEL_TITLE_肺癌甲"],
                                         "abstain_reason": "", "unresolved_terms": [], "note": ""})
    line = turn._consensus_extra_zh("human blood")
    assert "SENTINEL_TITLE" not in line
    assert line == "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配命中 5 条。"


def test_keyword_count_fail_open_never_raises(monkeypatch):
    """杠杆② fail-open：fixed 摘要**抛异常**时补偿段如实降级（note=keyword_count_error），
    绝不成为新故障源。"""
    def boom(*a, **k):
        raise RuntimeError("keyword count 段故障注入")

    monkeypatch.setattr(turn, "rule_match_summary", boom)
    assert turn._consensus_extra_zh("human blood") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配这次没能跑（keyword_count_error）。"
    assert turn._consensus_extra_zh("下载top5") == \
        "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配这次没能跑（keyword_count_error）。"


def test_keyword_count_fail_open_in_turn(monkeypatch):
    """杠杆② fail-open 全链路：fixed 摘要故障时图照常跑、route_extra_zh 降级透传、
    分流结果完整（flight 自身 fail-open 形状照常汇合）。"""
    def boom(*a, **k):
        raise RuntimeError("keyword count 段故障注入")

    monkeypatch.setattr(turn, "rule_match_summary", boom)
    saw: list = []

    def fake_events(*a, **k):
        saw.append(k.get("route_extra_zh", ""))
        return (dict(_SEARCH_PLAN), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_events)
    out = turn.route_turn("human blood", config=CFG, search_params=_SP)
    assert saw == ["**这句话**过规则匹配（关键词检索第一段）的结果："
                   "规则匹配这次没能跑（keyword_count_error）。"]
    assert out["route"] == "search"
    assert out["retrieval"] is not None and out["retrieval"]["status"] == "error", \
        "flight 自身 fail-open：摘要异常形状照常汇合"


def test_route_extra_zh_new_content_flows_to_graph(monkeypatch):
    """杠杆② 端到端：turn 并发路径把修正版 route_extra_zh 透传给图——有/无标记分支
    同构（逐字复刻今天共识检索段文案；不再拼机械标记行，盲跑裁定其有 action 偏置）。"""
    expected = "**这句话**过规则匹配（关键词检索第一段）的结果：规则匹配命中 5 条。"
    saw: list = []

    def fake_events(*a, **k):
        saw.append(k.get("route_extra_zh", ""))
        return (dict(_SEARCH_PLAN), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_events)
    turn.route_turn("human blood", config=CFG, search_params=_SP)
    assert saw == [expected]
    saw.clear()
    turn.route_turn("human blood 帮我打包", config=CFG, search_params=_SP)
    assert saw == [expected]
    assert "规则动作标记" not in saw[0], "机械标记行已移除（盲跑裁定偏置源）"


def test_marker_sentence_still_no_flight_with_compensation(monkeypatch):
    """杠杆② 不破坏 marker 分层起跑：有标记句补了检索段文案但**仍不起 flight**
    （320s 收益不变）；tool 路线 retrieval=None + note=skipped_action_marker。"""
    _stub_agent(monkeypatch, _EXEC_PLAN, verdict="action")
    made = _spy_flight(monkeypatch)
    events: list = []
    out = turn.route_turn(
        "human blood 帮我打包", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=_SP)
    assert made == [], "有标记句即使补检索段也不起 flight（零 RAG 直接进图）"
    assert out["route"] == "tool"
    assert out["retrieval"] is None
    assert out["retrieval_note"] == "skipped_action_marker"
    assert [k for k, _ in events] == []


# ---------------------------------------------------------------- 事件流确定性帧序（真图）

def test_graph_preliminary_frame_after_understand_tool_start(monkeypatch):
    """真图确定性帧序（设计约定）：发射在 understand 入口——preliminary 恒在
    tool_start(understand) 之后；route_consensus 内零发射（hook 只标记）。"""
    pytest.importorskip("langgraph", reason="langchain 扩展未安装：图级用例跳过")
    from langchain_core.messages import AIMessage

    class _ProviderFlight:
        """发射闸验证：done=True、has_hits=True、¬abandoned、¬emitted → 发射。"""

        def __init__(self):
            self.emitted = False
            self.abandoned = False
            self.lock = threading.Lock()
            self.payload = {"ok": True, "result_total": 3, "results": []}

        def done(self):
            return True

        def join(self):
            return {"status": "results", "total": 3, "top_titles": ["x"],
                    "abstain_reason": "", "unresolved_terms": [], "note": ""}

        def ensure_payload(self):
            return self.payload

        @property
        def has_hits(self):
            return True

    flight = _ProviderFlight()
    holder = {"preliminary_sent": False, "prelim_payload": None}
    events: list = []

    def provider():
        turn._emit_preliminary(flight, agent_path=True,
                               on_event=lambda k, e: events.append((k, e)),
                               state=holder)
        return flight.join()

    fake = _FakeToolsModel(
        # conftest 全局 stub route_consensus → general（不 invoke）；剧本从 understand 开始。
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish", "args": {"completion_report": "已重检"}, "id": "t"}]),
        AIMessage(content="已换词重检。"),
    )
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)
    agent_exec.plan_with_agent_events(
        "human blood", has_results=True, result_total=10,
        config=CFG, retrieval=None,
        current_query="human blood", current_filters=None,
        chat_model=fake, on_event=lambda k, e: events.append((k, e)),
        retrieval_provider=provider, on_route_verdict=None, route_extra_zh="")
    kinds = [k for k, _ in events]
    # tool_start(共识) → step(共识) → tool_start(understand) → preliminary → step(understand)
    assert kinds[:4] == ["tool_start", "step", "tool_start", "preliminary"]
    assert events[2][1] == {"verb": "node", "label_zh": "理解意图", "detail": ""}
    assert holder["preliminary_sent"] is True, "发射 state 已回填（preliminary_sent）"
    assert holder["prelim_payload"] is not None


class _FakeToolsModel:
    """tools 模式替身：bind_tools 记录并返回自身；invoke 依次弹出预置 AIMessage。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
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
