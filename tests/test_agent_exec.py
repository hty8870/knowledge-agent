# -*- coding: utf-8 -*-
"""agent_exec（langgraph 执行侧编排，2026-08-03）的确定性门。**全离线**：

- fake chat_model 注入（tools 模式 / JSON 降级模式双通道）驱动全路由表——
  与 action_plan 的 llm_call 注入同纪律：注入即跳过 should_use_llm 闸，零网络；
- 钉死 plan 契约零变化（source="agent"、经同一套 build_plan_from_raw 护栏、trace 逐节点）；
- 钉死失败契约：LLM 吐垃圾 → repair 一次 → 再垃圾 → AgentPlanInvalid；
  依赖缺席 / env 关停 / 大模型未武装 → agent_available False / AgentUnavailable；
- 钉死 turn.route_turn 接线：agent 抛错（AgentError 或任何异常）→ 原样回退 plan_action；
  llm_call 注入 / 请求级 use_agent=False → 永不走 agent。
"""
import importlib.util
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：agent_exec 测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec, turn  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

#: 武装到「只要走真 LLM 就一定会发调用」的配置（注入 chat_model 时此配置只过闸、不触网）。
CFG = LLMConfig(enable_llm=True, api_key="sk-agent-test")


@pytest.fixture(autouse=True)
def _stub_loop_tools(monkeypatch):
    """本文件测的是**规划/护栏/流式编排**，不是工具本体：LOOP_TOOLS 换确定性替身
    （离线、结果紧凑、零写盘），审计落账改 noop（绝不写真实账本）。
    多步循环本体的门在 `tests/test_agent_exec_loop.py`。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.db_status": {
            "run": lambda slots, root: {"total_records": 0, "sources": [],
                                        "external_files": [], "recycle": [], "ledger": {}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True,
        },
        "curate.check_updates": {
            "run": lambda slots, root: {
                "checked_at": "2026-08-04T00:00:00+08:00",
                "sources": [{"source": "10x", "label": "10x Genomics", "mode": "online",
                             "local_count": 12, "online_recent": 12, "new_count": 0}],
                "hint_zh": "",
            },
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True,
        },
        "curate.search_online": {
            "run": lambda slots, root: {"source_label": "ArrayExpress", "query": "x",
                                        "species": "", "sample_titles": [],
                                        "record_count": 0, "filename": "upload_x.json",
                                        "warnings": []},
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False,
        },
    })
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)


class _FakeToolsModel:
    """tools 模式替身：bind_tools 记录工具表并返回自身；invoke 依次弹出预置 AIMessage。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.bound_tools = None
        self.tool_choice = None
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.bound_tools = tools
        self.tool_choice = tool_choice
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


class _FakeJsonModel:
    """JSON 降级模式替身：bind_tools 抛错（模拟 provider 不支持 tool-calling），
    invoke 弹预置 content 的 AIMessage。"""

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


def _plan(utterance, model, *, has_results=False, result_total=0):
    return agent_exec.plan_with_agent(
        utterance,
        has_results=has_results, result_total=result_total,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )


# ---------------------------------------------------------------- 工具表由 VERB_SPECS 程序生成

def test_tool_table_is_generated_from_verb_specs():
    """bind_tools 收到的工具表必须与封闭动词表一一对应（单一真源，手抄必漂移）。"""
    fake = _FakeToolsModel(_tool_call("none", quoted="", confidence="high"))
    plan, _ = _plan("今天天气怎么样", fake)
    assert plan["verb"] == "none"
    names = {t["function"]["name"] for t in fake.bound_tools}
    # 2026-08-17 nl1 转正：understand 首步面 = 全部 EXEC + none（general 套件安全地板）——
    # ROUTE 投影（search.new/refine.conditions/lookup.identifier）退役、route.request 不进首步面。
    assert names == ({s.verb.replace(".", "_") for s in AP.VERB_SPECS if s.kind == AP.EXEC}
                     | {"none"})
    assert fake.tool_choice == "required", "必须要求恰好一次 tool_call"


# ---------------------------------------------------------------- tools 模式路由表

def test_check_updates_routes_with_source_slot():
    plan, trace = _plan("检查10x是否有更新", _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x",
                   confidence="high", reason="查来源更新"),
    ))
    assert plan["verb"] == "curate.check_updates"
    assert plan["kind"] == AP.EXEC
    assert plan["slots"]["source"] == "10x"
    assert plan["source"] == "agent"
    assert plan["llm_status"] == "ok"
    assert plan["trace"] is trace
    # check_updates 已进 LOOP_TOOLS（2026-08-04）：图内真跑（此处是替身）→ execute/decide 进 trace
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "execute", "decide", "narrate"]


def test_search_online_keeps_its_own_semantics():
    """「联网搜…新数据」仍是 search_online（检查更新语义剥出后，两者各管各的）。"""
    plan, _ = _plan("联网搜一下ArrayExpress有没有新的人类肺单细胞数据", _FakeToolsModel(
        _tool_call("curate.search_online", quoted="联网搜一下", source="ArrayExpress",
                   keywords="人类肺单细胞", species="人类", confidence="high", reason="在线找新数据"),
    ))
    assert plan["verb"] == "curate.search_online"
    assert plan["slots"]["source"] == "ArrayExpress"
    assert plan["slots"]["keywords"] == "人类肺单细胞"


def test_remove_carries_quoted_and_target():
    plan, _ = _plan("删掉我上传的X", _FakeToolsModel(
        _tool_call("curate.remove", quoted="删掉我上传的X", target="X",
                   confidence="high", reason="删除上传文件"),
    ))
    assert plan["verb"] == "curate.remove"
    assert plan["quoted"] == "删掉我上传的X"
    assert plan["slots"]["target"] == "X"
    assert plan["cancelled"] is False


def test_pack_download_with_limit():
    plan, _ = _plan("下载top3", _FakeToolsModel(
        _tool_call("pack.download", quoted="下载top3", limit=3, confidence="high", reason="要文件"),
    ), has_results=True, result_total=10)
    assert plan["verb"] == "pack.download"
    assert plan["slots"]["limit"] == 3
    assert plan["slot_sources"]["limit"] == "said"


def test_negated_action_is_cancelled_by_the_mechanical_gate():
    """LLM 没标 cancelled 时极性门机械补标（门与 LLM 自报取或，安全侧以门为准）。"""
    plan, _ = _plan("不要打包了", _FakeToolsModel(
        _tool_call("pack.download", quoted="打包", confidence="high", reason="用户要打包"),
    ), has_results=True, result_total=5)
    assert plan["verb"] == "pack.download"
    assert plan["cancelled"] is True
    assert "不" in plan["reason_zh"]


def test_search_sentence_routes_with_rank(monkeypatch):
    """2026-08-17 nl1 转正：agent 环内 search.new 投影退役——检索诉求在环内由 rank
    承接（search.new 本体保留给保底 plan_action 面，见 turn.py ROUTE 分支）。"""
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "rank", {  # 本文件 LOOP_TOOLS 是替身字典
        "run": lambda slots, root: {"query": str((slots or {}).get("query") or ""), "total": 1},
        "label_zh": "检索数据集", "card_kind": "rank", "readonly": True,
    })
    plan, _ = _plan("帮我找人类肺单细胞数据", _FakeToolsModel(
        _tool_call("rank", quoted="帮我找人类肺单细胞数据",
                   query="人类肺单细胞数据", confidence="high", reason="新检索"),
    ))
    assert plan["verb"] == "rank"
    assert plan["slots"]["query"] == "人类肺单细胞数据"
    assert plan["source"] == "agent"


def test_uncertainty_attribution_stays_llm_flavored():
    """source 改成 agent 后，「这几项是大模型读出来的」的归因不许变成「按关键词猜的」。"""
    plan, _ = _plan("检查10x是否有更新", _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x",
                   confidence="high", reason="查更新"),
    ))
    assert plan["uncertainty_zh"] == AP.UNCERTAINTY_ZH


def test_trace_entries_carry_the_full_contract():
    _, trace = _plan("检查10x是否有更新", _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x",
                   confidence="high", reason="查更新"),
    ))
    assert trace, "trace 不能为空"
    for entry in trace:
        expected = {"node", "label_zh", "detail", "ok", "ms"}
        if entry["node"] == "route_consensus":
            expected = expected | {"route_votes"}  # M1：共识全部原始投票随 trace 留痕
        assert set(entry) == expected
        assert isinstance(entry["ms"], int) and entry["ms"] >= 0
    assert [t["label_zh"] for t in trace] == [
        "分流共识", "理解意图", "合规检查", "执行工具 · 检查来源更新", "判断下一步", "生成说明",
    ]


# ---------------------------------------------------------------- JSON 降级模式

def test_json_fallback_mode_when_tool_calling_unsupported():
    """provider 不支持 tool-calling（bind_tools 抛错）→ 图内降级 JSON-in-prompt，路由不变。"""
    fake = _FakeJsonModel(json.dumps(
        {"verb": "pack.download", "quoted": "下载top3", "limit": 3, "confidence": "high"},
        ensure_ascii=False,
    ))
    plan, trace = _plan("下载top3", fake, has_results=True, result_total=10)
    assert plan["verb"] == "pack.download"
    assert plan["slots"]["limit"] == 3
    assert len(fake.invocations) == 1, "降级后一次即解析成功，不该多调"
    assert "换一种问法" in trace[1]["detail"]  # [0] 是常驻环首 route_consensus（nl1）


# ---------------------------------------------------------------- repair / AgentPlanInvalid

def test_garbage_then_garbage_raises_agent_plan_invalid():
    """LLM 吐垃圾 → repair 反馈一次 → 再垃圾 → AgentPlanInvalid（恰好调了两次）。"""
    fake = _FakeJsonModel("这不是 JSON", "依然不是")
    with pytest.raises(agent_exec.AgentPlanInvalid) as exc_info:
        _plan("检查10x是否有更新", fake)
    assert exc_info.value.violations, "违规清单要随异常带出（日志/调试用）"
    assert len(fake.invocations) == 2, "understand 一次 + repair 一次，不许多调"


def test_repair_recovers_from_a_guardrail_violation():
    """第一次 quoted 不是逐字子串 → violations 喂回 → 第二次修好 → 正常出 plan。"""
    fake = _FakeJsonModel(
        json.dumps({"verb": "curate.check_updates", "quoted": "检查更新一下",
                    "source": "10x", "confidence": "high"}, ensure_ascii=False),
        json.dumps({"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
                    "source": "10x", "confidence": "high"}, ensure_ascii=False),
    )
    plan, trace = _plan("检查10x是否有更新", fake)
    assert plan["verb"] == "curate.check_updates"
    assert plan["quoted"] == "检查10x是否有更新"
    # understand 一次 + repair 一次 + decide 一次 + narrate 一次（2026-08-04 多步循环新增后两次）
    assert len(fake.invocations) == 4
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "repair", "validate", "execute", "decide", "narrate",
    ]
    assert trace[2]["ok"] is False and "逐字" in trace[2]["detail"]  # 索引随环首 +1（nl1）
    assert trace[3]["label_zh"] == "让大模型改一版"


# ---------------------------------------------------------------- 可用性闸

def test_agent_available_false_when_langgraph_missing(monkeypatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
    assert agent_exec.agent_available() is False


def test_agent_available_false_when_env_forces_off(monkeypatch):
    monkeypatch.setenv("BIODATA_AGENT_EXEC", "off")
    assert agent_exec.agent_available() is False


def test_plan_with_agent_unavailable_without_llm_armament():
    """未注入 chat_model 且大模型关/无 key/mock → AgentUnavailable（与单次分类同一把闸）。"""
    with pytest.raises(agent_exec.AgentUnavailable):
        agent_exec.plan_with_agent(
            "检查10x是否有更新", has_results=False, result_total=0,
            config=LLMConfig(), retrieval=None, current_query="", current_filters=None,
        )
    with pytest.raises(agent_exec.AgentUnavailable):
        agent_exec.plan_with_agent(
            "检查10x是否有更新", has_results=False, result_total=0,
            config=LLMConfig(enable_llm=True, mock_llm=True, api_key="sk-x"),
            retrieval=None, current_query="", current_filters=None,
        )


def test_agent_errors_share_a_common_base():
    assert issubclass(agent_exec.AgentUnavailable, agent_exec.AgentError)
    assert issubclass(agent_exec.AgentPlanInvalid, agent_exec.AgentError)


# ---------------------------------------------------------------- turn.route_turn 接线

def _armed_config():
    return LLMConfig(enable_llm=True, api_key="sk-route-test")


def test_route_turn_falls_back_to_plan_action_when_agent_raises(monkeypatch):
    """agent 抛 AgentError → 原样回退 plan_action 保底（行为与不装扩展时逐位一致）。"""
    calls: list = []

    def boom(*args, **kwargs):
        calls.append(1)
        raise agent_exec.AgentUnavailable("依赖缺席")

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent", boom)
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "curate.check_updates", "quoted": "检查10x是否有更新", "source": "10x",
         "confidence": "high"}, ensure_ascii=False,
    ))
    out = turn.route_turn("检查10x是否有更新", config=_armed_config())
    assert calls, "agent 应先被尝试"
    assert out["route"] == "tool"
    assert out["plan"]["verb"] == "curate.check_updates"
    assert out["plan"]["source"] == "llm", "agent 失败 → 保底单次分类路径"
    assert out["via"] == "llm"


def test_route_turn_falls_back_on_any_exception_not_just_agent_error(monkeypatch):
    """任何异常（不只是 AgentError）都回退——agent 路径绝不成为新的单点。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)

    def boom(*args, **kwargs):
        raise ValueError("图编排内部错误")

    monkeypatch.setattr(agent_exec, "plan_with_agent", boom)
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: json.dumps(
        {"verb": "none", "quoted": "", "confidence": "high", "reason": "闲聊"}, ensure_ascii=False,
    ))
    out = turn.route_turn("今天天气怎么样", config=_armed_config())
    assert out["route"] == "none"
    assert out["plan"]["source"] == "llm"


def test_route_turn_never_uses_agent_when_llm_call_injected(monkeypatch):
    """llm_call 注入 = 测试隔离：永不走 agent（与 plan_action 同纪律）。"""
    called: list = []
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: called.append(1))
    out = turn.route_turn(
        "帮我打包前5条", has_results=True, result_total=5,
        llm_call=lambda p: json.dumps(
            {"verb": "pack.download", "quoted": "打包前5条", "limit": 5,
             "confidence": "high"}, ensure_ascii=False,
        ),
    )
    assert called == []
    assert out["plan"]["source"] == "llm"


def test_route_turn_use_agent_false_skips_the_agent(monkeypatch):
    """请求级关掉「AI 执行」（/api/utterance 的 agent:false）→ 规则直达：langgraph agent 与
    LLM 分流器**双双不启动**（2026-08-03 起 agent 标志 = 分流器总闸，
    不再是「跳过 langgraph、照走单次分类」）。"""
    called: list = []
    llm_called: list = []
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: called.append(1))
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: llm_called.append(prompt))
    out = turn.route_turn("今天天气怎么样", config=_armed_config(), use_agent=False)
    assert called == [] and llm_called == [], "C 关：分流器永不启动（不拼装提示词、不发调用）"
    assert out["route"] == "search" and out["via"] == "rule_direct"
    assert out["plan"] is None


def test_route_turn_marks_via_agent_when_agent_planned(monkeypatch):
    """agent 出的 plan：EXEC 分支 via 随 plan.source 自动是 agent；检索分支 via 显式标 agent。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)

    def fake_plan(utterance, **kwargs):
        plan = AP.build_plan_from_raw(
            {"verb": "search.new", "quoted": utterance,
             "effective_query": "人类肺数据", "confidence": "high"},
            utterance, has_results=False, result_total=0,
        )
        plan["source"] = "agent"
        plan["trace"] = [{"node": "understand", "label_zh": "理解意图",
                          "detail": "x", "ok": True, "ms": 1}]
        return plan, plan["trace"]

    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_plan)
    out = turn.route_turn("帮我找人类肺数据", config=_armed_config())
    assert out["route"] == "search"
    assert out["via"] == "agent"
    assert out["query"] == "人类肺数据"


# ---------------------------------------------------------------- 点名源一致性护栏

def test_named_source_mismatch_is_repaired_to_the_named_source():
    """真机病灶回放：「检查10x」首轮被填 source=ArrayExpress → 机械校验记 violation
    （点名的是 10x Genomics）→ repair 修正为规范名 → 正常出 plan。"""
    fake = _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x Genomics",
                   confidence="high", reason="改成用户点名的来源"),
    )
    plan, trace = _plan("检查10x是否有更新", fake)
    assert plan["verb"] == "curate.check_updates"
    assert plan["slots"]["source"] == "10x Genomics"
    # understand 一次 + repair 一次 + decide **两次**（2026-08-07 换装：decide 的 tools 通道
    # 异常时跌 JSON 兜底再问一次——本用例 fake 剧本耗尽触发 IndexError，等价于 provider
    # 全断的罕见路径）+ narrate 一次（2026-08-04 多步循环新增后两次）
    assert len(fake.invocations) == 5
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "repair", "validate", "execute", "decide", "narrate",
    ]
    assert trace[2]["ok"] is False  # validate：索引随环首 +1（nl1）
    # violation 要同时点名「用户说的是谁」和「你填的是谁」——repair 靠这句话自修
    assert "10x Genomics" in trace[2]["detail"] and "ArrayExpress" in trace[2]["detail"]


def test_named_source_mismatch_surviving_repair_raises_agent_plan_invalid():
    """点名源不一致且 repair 仍填错 → AgentPlanInvalid（回退保底由调用方 turn 负责）。"""
    fake = _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="ENCODE",
                   confidence="high", reason="还是填错"),
    )
    with pytest.raises(agent_exec.AgentPlanInvalid) as exc_info:
        _plan("检查10x是否有更新", fake)
    assert any("10x Genomics" in v for v in exc_info.value.violations)
    assert len(fake.invocations) == 2


def test_named_source_alias_fill_is_accepted():
    """填**别名**也算对（「10x」是 10x Genomics 的登记别名）——护栏比对的是同一来源，不是同一字符串。"""
    plan, _ = _plan("检查10x是否有更新", _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x",
                   confidence="high", reason="查更新"),
    ))
    assert plan["verb"] == "curate.check_updates"
    assert plan["slots"]["source"] == "10x"


def test_unnamed_source_verb_is_not_policed():
    """原话没点名来源时机械校验不越界（source 填不填由槽位描述约束，不归代码护栏管）。"""
    plan, _ = _plan("检查一下有没有更新", _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查一下有没有更新",
                   confidence="high", reason="查全部来源更新"),
    ))
    assert plan["verb"] == "curate.check_updates"
    assert "source" not in plan["slots"]


# ---------------------------------------------------------------- 流式（plan_with_agent_events）

def _plan_events(utterance, model, on_event):
    return agent_exec.plan_with_agent_events(
        utterance,
        has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model, on_event=on_event,
    )


def _check_updates_model():
    return _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x",
                   confidence="high", reason="查更新"),
    )


def test_events_arrive_in_node_order_with_labels():
    """on_event 的 step 事件序 = 节点执行序，条目与 trace 元素同形（含 label_zh）。
（2026-08-16）起同一条回调通道多了 tool_start 即时帧：understand/narrate
    节点档（verb="node"）与 execute 工具档都**先于**对应 step 落帧，label_zh 逐字一致
    （前端 pending 行按 label 匹配改行）。"""
    events: list = []
    plan, trace = _plan_events("检查10x是否有更新", _check_updates_model(),
                               lambda kind, entry: events.append((kind, entry)))
    assert [(kind, entry["label_zh"]) for kind, entry in events] == [
        ("tool_start", "分流共识"), ("step", "分流共识"),  # 常驻环首（nl1）
        ("tool_start", "理解意图"), ("step", "理解意图"),
        ("step", "合规检查"),
        ("tool_start", "执行工具 · 检查来源更新"), ("step", "执行工具 · 检查来源更新"),
        ("step", "判断下一步"),
        ("tool_start", "生成说明"), ("step", "生成说明"),
    ]
    starts = [entry for kind, entry in events if kind == "tool_start"]
    assert [s["verb"] for s in starts] == ["node", "node", "curate.check_updates", "node"]
    entries = [entry for kind, entry in events if kind == "step"]
    assert [e["node"] for e in entries] == ["route_consensus", "understand", "validate", "execute", "decide", "narrate"]
    for entry in entries:
        expected = {"node", "label_zh", "detail", "ok", "ms"}
        if entry["node"] == "route_consensus":
            expected = expected | {"route_votes"}  # M1：共识全部原始投票随 trace 留痕
        assert set(entry) == expected
    # 事件条目就是 trace 的元素（同一批 dict，不另造一份）
    assert entries == trace


def test_events_stream_matches_plain_invoke_shape():
    """流式收集路径与薄封装**同形**：同一剧本跑两遍（ms 是时钟值不可比，剔除后逐位相等）。"""
    events: list = []
    streamed_plan, streamed_trace = _plan_events(
        "检查10x是否有更新", _check_updates_model(),
        lambda kind, entry: events.append((kind, entry)))
    plain_plan, plain_trace = _plan("检查10x是否有更新", _check_updates_model())

    def strip_ms(tr):
        return [{k: v for k, v in e.items() if k != "ms"} for e in tr]

    def strip_plan(plan):
        # steps[i]["ms"] 也是时钟值（execute 节点写入），与 trace 的 ms 同样必须剥掉——
        # 否则空载时两次调用同为 0ms 侥幸相等，负载下差 ≥1ms 间歇红（flaky 坐实后修）。
        out = {k: v for k, v in plan.items() if k != "trace"}
        if out.get("steps"):
            out["steps"] = [{k: v for k, v in s.items() if k != "ms"} for s in out["steps"]]
        return out

    assert strip_plan(streamed_plan) == strip_plan(plain_plan)
    assert strip_ms(streamed_trace) == strip_ms(plain_trace)
    assert streamed_plan["trace"] is streamed_trace


def test_events_cover_the_repair_loop_in_order():
    """repair 回路的事件序同样 = 节点序：understand → validate(败) → repair → validate → narrate。"""
    fake = _FakeToolsModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _tool_call("curate.check_updates", quoted="检查10x是否有更新", source="10x Genomics",
                   confidence="high", reason="改成用户点名的来源"),
    )
    events: list = []
    plan, _ = _plan_events("检查10x是否有更新", fake,
                           lambda kind, entry: events.append((kind, entry)))
    assert plan["slots"]["source"] == "10x Genomics"
    #（2026-08-16）起事件流里混有 tool_start 即时帧——节点序钉只看 step 帧。
    assert [e["node"] for kind, e in events if kind == "step"] == [
        "route_consensus", "understand", "validate", "repair", "validate", "execute", "decide", "narrate",
    ]
    assert [e["ok"] for kind, e in events if kind == "step"] == [True, True, False, True, True, True, True, True]
