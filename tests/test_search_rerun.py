# -*- coding: utf-8 -*-
"""search.rerun 专项门。**全离线**：

- 机械闸分档直调 `_loop_search_rerun`：真实 database/base 规则检索（与
  探测事实："human blood"/"mouse brain" 有结果且结果集不同；
  "小鼠 胰腺癌" 恒零命中 no_match），adopted / 改空也采纳（设计决定：
  条件变更重检的空结果就是诚实答案，空结果集照常上屏）/ rewrite_no_change /
  空 query 槽 bad_param / 无基准五档钉死，出口顺手过 `SearchRerunResult` 形状闸；
- 裁决层两道机械闸直调 `_adjudicate_decide_obj`：换词重检预算闸（MAX_SEARCH_RERUN=1）
  与 rescue 面收敛闸（rescue 回合只许 search.rerun）；
- rescue 收敛面图级：`_FakeToolsModel` 脚本驱动（与 test_agent_exec 同 seam，
  chat_model 注入跳过 should_use_llm 闸）——adopted 全链路与 validate rescue 闸
  （首步提表外动词 → repair 再提 → AgentPlanInvalid）；
- `/api/agent/search-rescue` 端点三态：llm_unavailable fail-open / adopted / none 如实放弃
  （webapp.load_llm_config 与 agent_exec._build_chat_model 双注入缝，零网络）。
审计落账一律 noop（绝不写真实账本）。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：agent_exec 测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.agent import agent_exec, agent_schemas  # noqa: E402
from dataset_recommender.app import webapp  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

#: 武装到「只要走真 LLM 就一定会发调用」的配置（注入 chat_model 时此配置只过闸、不触网）。
CFG = LLMConfig(enable_llm=True, api_key="sk-agent-test")


@pytest.fixture(autouse=True)
def _no_audit_ledger(monkeypatch):
    """本文件的图级/端点用例会真跑 search.rerun——每次执行照常记账的联网账本改 noop，
    绝不写真实账本（与 test_agent_exec 的纪律一致）。"""
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)


class _FakeToolsModel:
    """tools 模式替身：bind_tools 记录工具表并返回自身；invoke 依次弹出预置 AIMessage。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.bound_history = []  # 逐次 bind 的工具表（understand/decide 各自的面分开钉）
        self.tool_choice = None
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.bound_history.append(tools)
        self.tool_choice = tool_choice
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(
        content="",
        tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}],
    )


def _rescue_plan(utterance, model):
    """rescue 入口的图级驱动：route_scope="rescue" + current_query=原句。"""
    return agent_exec.plan_with_agent_events(
        utterance,
        has_results=False, result_total=0,
        config=CFG, retrieval=None,
        current_query=utterance, current_filters=None,
        chat_model=model, route_scope="rescue", search_sources=None,
    )


# ---------------------------------------------------------------- 择优闸三态（直调工具本体）

def test_loop_search_rerun_adopted():
    """改写切换了硬过滤（human → mouse）→ 采纳：载荷 = /api/recommend 同形 dict，
    audit 九键自构（mode="rerank"），n_before/n_after 实算不采信自述。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": False})
    agent_schemas.SearchRerunResult.model_validate(result)  # 形状闸顺手过一遍
    assert result["adopted"] is True and result["reason"] == "adopted"
    assert result["query"] == "mouse brain"
    assert isinstance(result["n_before"], int) and result["n_before"] > 0
    assert isinstance(result["n_after"], int) and result["n_after"] > 0
    assert result["replace_screen"] is False
    payload = result["payload"]
    assert isinstance(payload, dict) and payload["ok"] is True
    # n_after 是择优闸口径（prep 候选数），payload.result_total 是 /api/recommend 终态口径
    # （含回退放宽等后处理）——两个口径各自如实，不钉互等。
    assert payload["result_total"] > 0
    assert payload["results"], "采纳档载荷必须带结果行"
    audit = payload["audit"]
    assert set(audit) == {"triggered", "verdict", "rewritten_query", "used", "reason",
                          "mode", "n_before", "n_after", "was_no_result"}
    assert audit["mode"] == "rerank" and audit["used"] is True
    assert audit["rewritten_query"] == "mouse brain"
    # 钉字记录（本行刻意改口径）：audit 的 n_before/n_after 从择优闸
    # 截断口径改为**屏口径**（= 步骤结果的 n_before_total/n_after_total，n_after_total 与
    # payload.result_total 单源）——audit 喂的是整屏替换后的横幅，与结果区同屏不许打架。
    assert audit["n_before"] == result["n_before_total"]
    assert audit["n_after"] == result["n_after_total"] == payload["result_total"]


def test_loop_search_rerun_keeps_explicit_date_scope_2099():
    """ 失败钉：屏上 human blood + 2099 起始日是真 0 条，重检不得丢日期救出数据。

    旧实现只向基准/改写管线传 query+sources，故把真实 0 条屏重算成 261 条并采纳
    mouse brain 的 93 条；结构化条件必须与 /api/recommend 同径进入三次检索。
     改空也采纳后本钉升级：2099 窗口下重检同样真 0 条——采纳的是**带日期
    条件的空结果集**（payload 如实 0 条、date_from 逐位保留），不是丢条件救出的数据。
    """
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True,
         "search_date_from": "2099-01-01", "search_date_to": "",
         "search_facet_filters": [], "search_suppressed_constraints": [],
         "search_lenient_dims": []})
    agent_schemas.SearchRerunResult.model_validate(result)
    assert result["adopted"] is True and result["reason"] == "adopted"
    assert result["n_after"] == 0
    assert result["n_before_total"] == result["n_after_total"] == 0
    payload = result["payload"]
    assert payload is not None and payload["result_total"] == 0
    assert payload["results"] == []
    assert payload["interpretation"]["intent"]["date_from"] == "2099-01-01"
    assert payload["audit"]["n_before"] == payload["audit"]["n_after"] == 0


def test_loop_search_rerun_audit_uses_same_date_scope_as_screen():
    """ 屏口径钉：2026 起 human blood=0、mouse brain=1，audit 必须如实记 0→1。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True,
         "search_date_from": "2026-01-01", "search_date_to": "",
         "search_facet_filters": [], "search_suppressed_constraints": [],
         "search_lenient_dims": []})
    assert result["adopted"] is True
    assert result["n_before_total"] == 0
    assert result["n_after_total"] == result["payload"]["result_total"] == 1
    audit = result["payload"]["audit"]
    assert audit["n_before"] == 0 and audit["n_after"] == 1
    assert audit["was_no_result"] is True
    assert result["payload"]["interpretation"]["intent"]["date_from"] == "2026-01-01"


def test_loop_search_rerun_totals_respect_active_facets():
    """ 分面钉：未截断 totals 也必须带 facet_filters，不能只让 top-k 候选带。"""
    facets = [{"dim": "platform", "value": "visium"}]
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True,
         "search_facet_filters": facets})
    assert result["adopted"] is True
    assert result["n_before_total"] == 0
    assert result["n_after_total"] == result["payload"]["result_total"] == 42
    assert result["payload"]["applied_facets"] == facets
    assert result["payload"]["audit"]["was_no_result"] is True


def test_loop_search_rerun_rejects_payload_that_loses_structured_scope(monkeypatch):
    """ fail-closed 钉：卡片 payload 丢日期时拒绝换屏，不能只把计数算对。"""
    from dataset_recommender.app import recommend_rows

    real = recommend_rows.recommend_payload

    def broken(meta):
        payload = real(meta)
        interpretation = dict(payload.get("interpretation") or {})
        interpretation["intent"] = dict(interpretation.get("intent") or {}, date_from="")
        payload["interpretation"] = interpretation
        return payload

    monkeypatch.setattr(recommend_rows, "recommend_payload", broken)
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True,
         "search_date_from": "2026-01-01", "search_date_to": ""})
    assert result["adopted"] is False
    assert result["reason"] == "structured_context_lost_kept_original"
    assert result["payload"] is None
    assert "筛选条件" in result["disclosure_zh"]


def test_loop_search_rerun_empty_rewrite_adopted():
    """设计决定：条件变更重检命中 0 条 → **采纳**——
    空结果集照常上屏（payload 非 None、result_total==0、results 空），原结果被覆盖；
    绝不否决后「保持不变」。披露句如实说 0 条。"""
    result = agent_exec._loop_search_rerun(
        {"query": "小鼠 胰腺癌"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": False})
    agent_schemas.SearchRerunResult.model_validate(result)
    assert result["adopted"] is True and result["reason"] == "adopted"
    assert result["n_after"] == 0 and result["n_after_total"] == 0
    assert isinstance(result["n_before"], int) and result["n_before"] > 0
    payload = result["payload"]
    assert payload is not None and payload["ok"] is True
    assert payload["result_total"] == 0 and payload["results"] == []
    assert payload["audit"]["n_after"] == 0
    assert payload["audit"]["n_before"] == result["n_before_total"]
    assert "找到 0 条" in result["disclosure_zh"]


def test_loop_search_rerun_no_change_kept_original():
    """改写与基准同一句 → 硬过滤同集（`_same_hard_filter`）→ 拒，不重跑第三次检索。"""
    result = agent_exec._loop_search_rerun(
        {"query": "human blood"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True})
    agent_schemas.SearchRerunResult.model_validate(result)
    assert result["adopted"] is False
    assert result["reason"] == "rewrite_no_change_kept_original"
    assert result["payload"] is None
    assert result["n_before"] == result["n_after"]
    assert result["replace_screen"] is True  # ctx 如实透传（rescue 入口恒 True）


def test_loop_search_rerun_empty_query_slot_bad_param():
    """query 槽空 → `_SearchRerunParamError`（bad_param，与 search_online 缺槽位同纪律）。"""
    for slots in ({}, {"query": ""}, {"query": "   "}):
        with pytest.raises(agent_exec._SearchRerunParamError) as exc_info:
            agent_exec._loop_search_rerun(slots, None, {"current_query": "human blood"})
        assert exc_info.value.code == "bad_param"
        assert "query" in exc_info.value.hint


def test_loop_search_rerun_no_baseline_adopts_nonempty_rewrite():
    """无基准（链内空现场，current_query=""）→ n_before 如实记 None、同集闸跳过，
    改写非空即采纳。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "", "search_sources": None, "replace_screen": False})
    agent_schemas.SearchRerunResult.model_validate(result)
    assert result["n_before"] is None
    assert result["adopted"] is True and result["payload"] is not None
    assert result["payload"]["audit"]["n_before"] is None
    assert result["payload"]["audit"]["was_no_result"] is False  # None == 0 不成立


# ---------------------------------------------------------------- 裁决层两道机械闸（直调）

def _state(**kw):
    base = {"utterance": "human blood", "steps": [], "dead_ends": [],
            "route_scope": "general"}
    base.update(kw)
    return base


def test_adjudicate_rerun_budget_gate():
    """换词重检预算闸：search.rerun 已用满 MAX_SEARCH_RERUN=1 次后再提议 → 机械拒，
    按 done 收尾、note 如实点名（提议过即消耗预算，不论成败）。"""
    state = _state(steps=[{"verb": "search.rerun", "ok": True}])
    raw, note, declined, violation = agent_exec._adjudicate_decide_obj(
        {"verb": "search.rerun", "quoted": "human blood", "query": "mouse brain"}, state)
    assert raw is None and violation == ""
    assert "换词重检" in note and "1 次" in note
    assert "预算已用完" in declined


def test_adjudicate_rescue_gate_rejects_other_verbs():
    """rescue 面收敛闸：检索救回回合提议 search.rerun 以外的动词（含只读的 db_status）
    → 机械拒，按 done 收尾、note 如实点名。"""
    raw, note, declined, violation = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.db_status", "quoted": "human blood"},
        _state(route_scope="rescue"))
    assert raw is None and violation == ""
    assert "rescue" in note and "工具面" in note


def test_adjudicate_rescue_gate_allows_search_rerun():
    """对偶钉：rescue 回合提议 search.rerun 本身 → 两道闸都放行（预算未用、面内动词）。"""
    raw, note, declined, violation = agent_exec._adjudicate_decide_obj(
        {"verb": "search.rerun", "quoted": "human blood", "query": "mouse brain"},
        _state(route_scope="rescue"))
    assert raw is not None and raw["verb"] == "search.rerun"
    assert violation == "" and declined == ""


def test_decide_tool_specs_rescue_shape():
    """rescue 档 decide 工具面从真表滤出：恰好 search_rerun + finish（面收敛到改写或放弃）。"""
    names = [t["function"]["name"]
             for t in agent_exec._DECIDE_TOOL_SPECS_BY_SUITE["rescue"]]
    assert names == ["search_rerun", "finish"]
    assert agent_exec._DECIDE_TOOL_NAME_TO_VERB_BY_SUITE["rescue"] == {
        "search_rerun": "search.rerun"}


# ---------------------------------------------------------------- rescue 收敛面（图级）

def test_rescue_graph_adopted_path():
    """rescue 回合全链路：understand 提 search.rerun（面内）→ execute 真跑择优闸采纳
    → decide 收尾 finish → narrate 汇报。steps 实录 adopted=True、replace_screen=True。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换一组查询词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),  # narrate 汇报（无数字、无否认语素）
    )
    plan, trace = _rescue_plan("human blood", fake)
    assert plan["source"] == "agent"
    rerun = [s for s in plan["steps"] if s.get("verb") == "search.rerun"]
    assert len(rerun) == 1 and rerun[0]["ok"] is True
    agent_schemas.SearchRerunResult.model_validate(rerun[0]["result"])
    assert rerun[0]["result"]["adopted"] is True
    assert rerun[0]["result"]["replace_screen"] is True  # rescue 入口恒替换整屏
    assert rerun[0]["result"]["query"] == "mouse brain"
    # understand 的工具面被机械收窄：第一次 bind（understand）只有 search_rerun + none
    understand_names = {t["function"]["name"] for t in fake.bound_history[0]}
    assert understand_names == {"search_rerun", "none"}


def test_rescue_graph_validate_gate_rejects_other_verb():
    """validate 的 rescue 闸（提示不是围栏的机械兜底）：首步提表外动词 → violation
    → repair 再提表外动词 → 再违规 → AgentPlanInvalid（端点据此 fail-open）。
    内容 JSON 通道给药（tool_calls 通道的 name_to_verb 已收窄，表外名字映不回 verb）。"""
    bad = json.dumps({"verb": "curate.check_updates", "quoted": "human blood",
                      "confidence": "high", "reason": "想检查更新"}, ensure_ascii=False)
    fake = _FakeToolsModel(AIMessage(content=bad), AIMessage(content=bad))
    with pytest.raises(agent_exec.AgentPlanInvalid) as exc_info:
        _rescue_plan("human blood", fake)
    assert "rescue" in str(exc_info.value) and "不在本路线面内" in str(exc_info.value)


# ---------------------------------------------------------------- /api/agent/search-rescue 端点三态

def _patch_armed_server(monkeypatch, fake):
    """端点 adopted/none 路径的双注入缝：服务端配置武装（过 should_use_llm 闸）+
    chat_model 换替身（零网络）。load_llm_config 在端点里被调两次（server 对照 +
    锁内实载），同钉一个补丁。"""
    monkeypatch.setattr(
        webapp, "load_llm_config",
        lambda *a, **k: LLMConfig(provider="zhipuai", enable_llm=True, api_key="sk-test"))
    monkeypatch.setattr(agent_exec, "_build_chat_model", lambda config: fake)


def test_endpoint_llm_unavailable_fail_open():
    """大模型未武装 → 200 fail-open：attempted=False、reason=llm_unavailable、不带载荷。"""
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue", json={"query": "human blood"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["attempted"] is False
    assert data["reason"] == "llm_unavailable"
    assert data["adopted"] is False and data["payload"] is None
    assert data["agent"] == {"available": True, "used": False}
    assert data["query"] == "human blood" and data["rewrite"] == ""


def test_endpoint_adopted(monkeypatch):
    """采纳路径：脚本模型提 search.rerun → 机械择优闸采纳 → 200 + /api/recommend 同形
    payload（调用方整屏替换结果），rewrite/n_before/n_after 如实。
     钉字：additive 的日期/分面/抑制/宽容字段必须进同一检索现场并稳定回显。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换一组查询词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    _patch_armed_server(monkeypatch, fake)
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue",
        json={"query": "human blood", "provider": "zhipuai",
              "use_llm": True, "api_key": "sk-test",
              "date_from": "2026-01-01", "date_to": None,
              "facet_filters": [], "suppressed_constraints": [], "lenient_dims": []})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["attempted"] is True
    assert data["reason"] == "adopted" and data["adopted"] is True
    assert data["rewrite"] == "mouse brain"
    assert isinstance(data["n_before"], int) and data["n_after"] > 0
    assert data["payload"]["ok"] is True and data["payload"]["result_total"] == 1
    assert data["payload"]["interpretation"]["intent"]["date_from"] == "2026-01-01"
    assert data["payload"]["applied_facets"] == []
    assert data["payload"]["applied_suppressed"] == []
    assert data["payload"]["applied_lenient"] == []
    assert data["payload"]["audit"]["mode"] == "rerank"
    assert data["agent"] == {"available": True, "used": True}
    assert data["report_zh"] and data["trace"]


def test_endpoint_rejects_invalid_rescue_date_before_llm():
    """ 契约钉：rescue 日期与 /api/utterance 同径校验，非法值不能静默丢弃。"""
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue",
        json={"query": "human blood", "date_from": "2026-13-40"})
    assert r.status_code == 400
    assert "date_from" in r.json()["detail"]


def test_endpoint_none_no_rewrite(monkeypatch):
    """LLM 选 none 如实放弃 → 200：reason=no_rewrite、attempted=True、adopted=False、
    不带载荷，report_zh 落端点默认句（当前结果未变）。"""
    fake = _FakeToolsModel(
        _tool_call("none", quoted="", confidence="high", reason="没有更合适的改写"),
    )
    _patch_armed_server(monkeypatch, fake)
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue",
        json={"query": "human blood", "provider": "zhipuai",
              "use_llm": True, "api_key": "sk-test"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True and data["attempted"] is True
    assert data["reason"] == "no_rewrite" and data["adopted"] is False
    assert data["payload"] is None and data["rewrite"] == ""
    assert data["agent"] == {"available": True, "used": True}


# ---------------------------------------------------------------- rescue2 放宽

def test_rescue_block_zh_with_unresolved_terms_relaxes():
    """放宽段（有未收录词）：逐字点名每个未收录词 + 允许丢弃/映射收录近义词 +
    已收录条件必须全保留 + 不许新增 + 机械约束尾——五要素齐全。"""
    block = agent_exec._rescue_block_zh(["神经", "壶腹"])
    assert "「神经」" in block and "「壶腹」" in block
    assert "没有收录" in block and "丢弃" in block and "近义词" in block
    assert "已收录的条件必须全部保留" in block
    assert "不许新增用户没表达的条件" in block
    assert "机械拒绝" in block  # 面收敛尾不变


def test_rescue_block_zh_without_unresolved_terms_stays_strict():
    """无未收录词（条件全收录/无投影）：不放逐字清单，明确「只允许等价换说法」——
    守卫例的提示词保持严格（放弃指路 none 也在）。"""
    for empty in (None, [], ["", "  "]):
        block = agent_exec._rescue_block_zh(empty)
        assert "全部已收录" in block and "等价换说法" in block
        assert "没有可丢弃的未收录词" in block
        assert "丢弃这些词" not in block  # 无清单档绝不开丢词口
        assert "机械拒绝" in block


def test_loop_search_rerun_dropped_terms_mechanical():
    """dropped_terms 机械比对：ctx 带未收录词「神经」，改写「mouse brain」里它消失
    → dropped_terms==["神经"]；disclosure_zh 点名丢弃词+改写词+实算条数。
    未采纳档（同集拒）dropped_terms 恒 []。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "小鼠神经胶质瘤", "search_sources": None,
         "replace_screen": True, "unresolved_terms": ["神经"]})
    agent_schemas.SearchRerunResult.model_validate(result)
    assert result["adopted"] is True
    assert result["dropped_terms"] == ["神经"]
    disc = result["disclosure_zh"]
    assert "「神经」" in disc and "没有收录" in disc
    # 披露句条数 = payload 终态口径（未截断命中总数），不是择优闸 top-k 截断的 n_after
    assert f"「mouse brain」" in disc and f"{result['payload']['result_total']} 条" in disc
    rejected = agent_exec._loop_search_rerun(
        {"query": "human blood"}, None,
        {"current_query": "human blood", "search_sources": None,
         "replace_screen": True, "unresolved_terms": ["神经"]})
    agent_schemas.SearchRerunResult.model_validate(rejected)
    assert rejected["adopted"] is False and rejected["dropped_terms"] == []
    # 无 unresolved_terms（旧 ctx 形状）→ 旧行为逐位不变：dropped=[]、披露句无丢弃段
    legacy = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": False})
    assert legacy["dropped_terms"] == [] and "没有收录" not in legacy["disclosure_zh"]


def test_rescue_graph_prompt_carries_unresolved_terms():
    """提示词注入链（图级）：state.retrieval 的 unresolved_terms 进 understand 双壳
    prompt——fake 模型实录的 HumanMessage 含逐字「神经」与「没有收录」放宽段。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="小鼠神经胶质瘤", query="mouse brain",
                   confidence="high", reason="丢弃未收录词后重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    plan, _trace = agent_exec.plan_with_agent_events(
        "小鼠神经胶质瘤",
        has_results=False, result_total=0, config=CFG,
        retrieval={"status": "abstained", "total": 0, "top_titles": [],
                   "abstain_reason": "unresolved_term", "unresolved_terms": ["神经"],
                   "note": ""},
        current_query="小鼠神经胶质瘤", current_filters=None,
        chat_model=fake, route_scope="rescue", search_sources=None,
    )
    assert plan["source"] == "agent"
    prompt_text = str(fake.invocations[0][-1].content)
    assert "「神经」" in prompt_text and "没有收录" in prompt_text
    rerun = [s for s in plan["steps"] if s.get("verb") == "search.rerun"]
    assert rerun and rerun[0]["result"]["dropped_terms"] == ["神经"]


def test_loop_graph_search_rerun_gets_unresolved_from_state_retrieval():
    """r2p：**非 rescue 环内** search.rerun 采纳时，unresolved_terms 与
    rescue 入口同函数同口径——execute dispatch 统一从 state.retrieval 现取（不是 rescue
    端点注入独占）：采纳步 result 带 dropped_terms 机械比对与 disclosure_zh 确定性披露句。
    对偶：无检索投影（retrieval=None）→ ctx 缺省恒 []，旧行为不变（dropped 恒 []、
    披露句无丢弃段）。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="小鼠神经胶质瘤", query="mouse brain",
                   confidence="high", reason="丢弃未收录词后重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    plan, _trace = agent_exec.plan_with_agent_events(
        "小鼠神经胶质瘤",
        has_results=False, result_total=0, config=CFG,
        retrieval={"status": "abstained", "total": 0, "top_titles": [],
                   "abstain_reason": "unresolved_term", "unresolved_terms": ["神经"],
                   "note": ""},
        current_query="小鼠神经胶质瘤", current_filters=None,
        chat_model=fake, route_scope="", search_sources=None,
    )
    assert plan["source"] == "agent"
    rerun = [s for s in plan["steps"] if s.get("verb") == "search.rerun"]
    assert rerun and rerun[0]["ok"] is True
    r = rerun[0]["result"]
    assert r["adopted"] is True
    assert r["dropped_terms"] == ["神经"]
    assert "「神经」" in r["disclosure_zh"] and "没有收录" in r["disclosure_zh"]

    fake2 = _FakeToolsModel(
        _tool_call("search.rerun", quoted="小鼠神经胶质瘤", query="mouse brain",
                   confidence="high", reason="换词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="已换查询词重新检索。"),
    )
    plan2, _t2 = agent_exec.plan_with_agent_events(
        "小鼠神经胶质瘤",
        has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="小鼠神经胶质瘤", current_filters=None,
        chat_model=fake2, route_scope="", search_sources=None,
    )
    rerun2 = [s for s in plan2["steps"] if s.get("verb") == "search.rerun"]
    assert rerun2 and rerun2[0]["result"]["dropped_terms"] == []
    assert "没有收录" not in rerun2[0]["result"]["disclosure_zh"]


def test_endpoint_adopted_disclosure_and_dropped_terms(monkeypatch):
    """端点披露链：采纳档 report_zh = 确定性披露句（非 LLM narrate）、dropped_terms
    结构化随响应下发；fail-open 档 dropped_terms 恒 []（形状稳定）。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="human blood", query="mouse brain",
                   confidence="high", reason="换一组查询词重检"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换词重检：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="这句是 LLM 的汇报，不该上屏。"),
    )
    _patch_armed_server(monkeypatch, fake)
    r = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue",
        json={"query": "human blood", "provider": "zhipuai",
              "use_llm": True, "api_key": "sk-test"})
    assert r.status_code == 200
    data = r.json()
    assert data["adopted"] is True
    assert data["dropped_terms"] == []  # human blood 无未收录词
    assert data["report_zh"].startswith("已按「mouse brain」重查，找到 ")
    assert data["report_zh"].endswith(" 条，结果区已更新。")
    assert "LLM 的汇报" not in data["report_zh"]  # 确定性披露覆盖 narrate
    fail_open = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/agent/search-rescue", json={"query": "human blood"})
    assert fail_open.json()["dropped_terms"] == []


# ---------------------------------------------------------------- 择优闸与屏口径一致（口径核对）

def test_adopted_payload_audit_counts_match_screen_totals():
    """口径核对：rescue 采纳档整屏替换后，audit 横幅（shell.js renderAuditBanner 读
    payload.audit 的 n_before→n_after）与结果区「库中共 N 条匹配」、sys 披露句
    （disclosure_zh，rescue2 已对齐 result_total）**同屏**——截断口径（top-k=10）
    与屏口径同屏打架（实测 human blood→mouse brain：横幅 10 → 10 条 vs 屏上 93 条）。
    步骤结果的 n_before/n_after 保留择优闸口径（步骤卡注释明示），additive 的
    n_before_total/n_after_total 与 audit 计数必须与屏同源。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": True})
    agent_schemas.SearchRerunResult.model_validate(result)
    payload = result["payload"]
    assert payload["result_total"] > 10, "测试前提：命中超过 top-k 截断，口径差才显形"
    # 择优闸口径原样保留（截断），屏口径 additive 上新键。
    assert result["n_after"] <= 10 < result["n_after_total"]
    assert result["n_after_total"] == payload["result_total"]
    assert result["n_before"] <= 10 < result["n_before_total"]
    # audit 横幅计数与屏同源（结果区头部/披露句同数）。
    audit = payload["audit"]
    assert audit["n_after"] == payload["result_total"]
    assert audit["n_before"] == result["n_before_total"]
    assert audit["was_no_result"] is False


def test_search_rerun_user_sentences_use_screen_totals():
    """采纳档的用户可见句子（execute trace 摘要句 + 确定性兜底汇报）
    不许拿截断口径「原来 10 条 → 10 条」去对照屏上的「库中共 261/93 条」。"""
    result = agent_exec._loop_search_rerun(
        {"query": "mouse brain"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": False})
    total = result["n_after_total"]
    base_total = result["n_before_total"]
    fact = agent_exec._execute_detail_zh({"card_kind": "search_rerun"}, result)
    assert f"原来 {base_total} 条" in fact and f" → {total} 条" in fact
    assert "→ 10 条" not in fact
    report = agent_exec._steps_report_fallback_zh([{
        "verb": "search.rerun", "verb_zh": "检索新查询", "ok": True,
        "card_kind": "search_rerun", "result": result}])
    assert f"原来 {base_total} 条" in report and f" → {total} 条" in report
    assert "→ 10 条" not in report
    # 旧形状步骤记录（无 totals 键）回退择优闸口径，不炸。
    legacy = {"adopted": True, "query": "x", "n_before": 3, "n_after": 7}
    assert "原来 3 条 → 7 条" in agent_exec._execute_detail_zh(
        {"card_kind": "search_rerun"}, legacy)


# ---------------------------------------------------------------- 改空也采纳（「换成猪的」投诉）

def test_loop_graph_condition_change_empty_adopted():
    """图级还原投诉现场：屏上 36 条（human blood）→ 用户「换成小鼠胰腺癌的」→
    search.rerun 真跑（恒零命中库）→ **采纳空结果集**（旧结果被覆盖），不再
    「结果不如当前就保持不变」。steps 实录 adopted=True、payload 0 条。"""
    fake = _FakeToolsModel(
        _tool_call("search.rerun", quoted="换成小鼠胰腺癌的", query="小鼠 胰腺癌",
                   confidence="high", reason="换条件重查"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "换条件重查：已做（第1步）"}, "id": "t2"}]),
        AIMessage(content="按「小鼠 胰腺癌」重新检索：没有匹配到数据集，结果区已更新。"),
    )
    plan, _trace = agent_exec.plan_with_agent_events(
        "换成小鼠胰腺癌的",
        has_results=True, result_total=36, config=CFG, retrieval=None,
        current_query="human blood", current_filters=None,
        chat_model=fake, route_scope="", search_sources=None,
    )
    assert plan["source"] == "agent"
    rerun = [s for s in plan["steps"] if s.get("verb") == "search.rerun"]
    assert len(rerun) == 1 and rerun[0]["ok"] is True
    agent_schemas.SearchRerunResult.model_validate(rerun[0]["result"])
    r = rerun[0]["result"]
    assert r["adopted"] is True and r["reason"] == "adopted"
    assert r["n_after"] == 0 and r["n_after_total"] == 0
    assert r["payload"]["result_total"] == 0 and r["payload"]["results"] == []


def test_route_turn_adopted_empty_rerun_replaces_screen(monkeypatch):
    """路由级覆盖钉：0 命中的采纳步照常产出上屏批——route_turn 的 result_payload
    是空结果集 payload（不是 None），active 批 = 该重搜批，旧结果整屏被覆盖。"""
    from dataset_recommender.agent import turn as _turn

    empty_payload = {"ok": True, "result_total": 0, "results": []}
    plan = {
        "kind": agent_exec._ap.EXEC, "verb": "search.rerun", "source": "agent",
        "llm_status": "ok",
        "steps": [{"verb": "search.rerun", "ok": True,
                   "result": {"adopted": True, "query": "小鼠 胰腺癌",
                              "replace_screen": False, "payload": empty_payload}}],
    }
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(
        agent_exec, "plan_with_agent_events", lambda *a, **k: (dict(plan), []))
    out = _turn.route_turn(
        "换成小鼠胰腺癌的", config=CFG, has_results=True, result_total=36,
        current_query="human blood", on_event=lambda k, e: None)
    assert out["route"] == "tool"
    assert out["result_payload"] is empty_payload, "0 命中采纳批必须上屏（覆盖旧结果）"
    assert out["result_batches"][-1]["kind"] == "search_rerun"
    assert out["result_batches"][-1]["payload"]["result_total"] == 0
    assert out["active_batch"] == out["result_batches"][-1]["batch_id"]


def test_steps_report_prompt_guides_user_facing_wording():
    """去八股化改版回归钉：narrate 系统 prompt 必须载
    「关键词/检索方式/命中数/结果区是否更新」的引导式汇报指引与工程黑话禁令
    （重检/择优/采纳/救回/闸/批次 不得出现在给用户的文本里），并钉死反自相矛盾
    原则（「结果已更新」与「保持不变」绝不同段）——不再授逐字句式模板（旧版
    「必须说清三件事」清单体 + 「按新条件没有匹配到数据集」逐字句式诱导模型
    照模板造句，还能接出自相矛盾的后半句）。"""
    prompt = agent_exec._STEPS_REPORT_RULES_ZH
    assert "用了哪些关键词" in prompt and "命中多少条" in prompt
    assert "结果区是否更新" in prompt
    for jargon in ("重检", "择优", "采纳", "救回", "闸", "批次"):
        assert jargon in prompt, f"黑话禁令必须逐一点名「{jargon}」"
    assert "绝不出现内部工程术语" in prompt
    # 0 命中采纳：照实直说是新条件的真实答案（原则式指引，非逐字句式）。
    assert "照实直说" in prompt
    # 反自相矛盾钉：「结果已更新」与「保持不变」绝不出现在同一段汇报里。
    assert "绝不出现在同一段汇报里" in prompt


def test_steps_report_feeds_tool_execution_facts():
    """回归钉：narrate 的输入必须带工具执行记录——_steps_report_with_llm 发给
    chat_model 的 prompt 里，steps 是逐步紧凑投影（verb/ok/裁决态/命中数）；0 命中
    采纳步的 query、adopted、result_total=0 逐字在场（模型拿到的是结构化事实，
    不是只有一句用户原话）。"""
    fake = _FakeToolsModel(AIMessage(content="按「猪」重新检索：没有匹配到数据集。"))
    step = {"verb": "search.rerun", "verb_zh": "换条件重查", "ok": True,
            "card_kind": "search_rerun",
            "result": {"adopted": True, "reason": "adopted", "query": "猪",
                       "replace_screen": True,
                       "payload": {"ok": True, "result_total": 0, "results": []}}}
    out = agent_exec._steps_report_with_llm(fake, "改成猪", [step])
    assert out == "按「猪」重新检索：没有匹配到数据集。"
    assert len(fake.invocations) == 1
    sent = fake.invocations[0][0].content
    payload = json.loads(sent.split("----- 原话与步骤结果（JSON）-----", 1)[1])
    assert payload["utterance"] == "改成猪"
    proj = payload["steps"][0]
    assert proj["verb"] == "search.rerun" and proj["ok"] is True
    assert proj["result"]["adopted"] is True
    assert proj["result"]["query"] == "猪"
    assert proj["result"]["result_total"] == 0


def test_static_fallback_wording_no_jargon():
    """静态回执去黑话钉：采纳/同集/条件丢失三档的确定性文案都不含工程黑话，
    且条件丢失档说的是「没有执行」（不是「结果不好」）。"""
    adopted = agent_exec._loop_search_rerun(
        {"query": "小鼠 胰腺癌"}, None,
        {"current_query": "human blood", "search_sources": None, "replace_screen": False})
    fact = agent_exec._execute_detail_zh({"card_kind": "search_rerun"}, adopted)
    assert "0 条" in fact and "结果已更新" in fact
    for jargon in ("重检", "择优", "采纳", "救回", "闸", "批次"):
        assert jargon not in fact
    rejected = {"adopted": False, "reason": "structured_context_lost_kept_original",
                "query": "x", "disclosure_zh": ""}
    fact2 = agent_exec._execute_detail_zh({"card_kind": "search_rerun"}, rejected)
    assert "没有执行" in fact2
    for jargon in ("重检", "择优", "采纳", "救回", "闸", "批次"):
        assert jargon not in fact2
