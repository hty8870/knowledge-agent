# -*- coding: utf-8 -*-
"""ask.relax 后端专项门（2026-09-14 YOLO 批）。**全离线**：

- 机械闸三态：只在屏面 degenerate（`_screen_baseline`：无经条件核验的结果）放行；
  no_query / screen_not_degenerate / parse_error / abstain / no_options 五档如实拒绝
  （ok 恒 True——拒绝是数据不是故障，同 rerank rejected 哲学）。
- 载荷形状：选项由 `retriever.relaxation_options` 确定性预演（零 LLM）；每选项
  suppress = 既有 suppressed_constraints + 本选项翻译清单（`_relax_to_suppressed`）
  合并去重（前端整组替换语义，服务端合并好不丢既有忽略）；digest options（回模型）
  不带 suppress，card payload（透传前端）带；卡层不带 preview（2026-09-14 用户评审：
  示例名塞进按钮信息效率太低，预览仍留在 relaxation_options 检索事实里）。
- `_is_search_settle_step`：ask.relax 真发起（asked=True）核销检索半（finish 闸
  不误杀「等用户作答」），被闸拒绝（asked=False）不算。
- 预算闸：MAX_ASK_RELAX=1，`_adjudicate_decide_obj` 用满后再提议机械拒绝并点名。
- turn.py 组卷透传：真发起 → 响应带 ask_relax 键（**有批次时也不被吞**——
  extra.update 回归钉）；拒绝档/未发起 → 键不出现，响应与现状逐位一致。
- webapp._utterance_response_body 条件透传：有载荷才加键。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import turn
from dataset_recommender.agent.agent_schemas import AskRelaxResult
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig
from dataset_recommender.retrieval.query_parser import parse_query, relax_key_to_suppressed

#: 与 test_multi_batch 同口径的收敛检索参数 / 武装配置（注入替身时只过闸、不触网）。
SP = {
    "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None, "lenient_dims": None,
    "date_from": "", "date_to": "", "polish": True,
}
CFG = LLMConfig(enable_llm=True, api_key="sk-ar-test")

#: 条件互相打架的典型坏查询（物种撞车 + 未来年份）；解析得出 species/disease 等约束。
BAD_QUERY = "human lung adenocarcinoma FASTQ"


def _option(key="dim:disease", kind="drop", label="疾病", count=12,
            names=("DS-X", "DS-Y", "DS-Z")):
    return {"key": key, "kind": kind, "label": label, "count": count,
            "candidates": [SimpleNamespace(record=SimpleNamespace(dataset_name=n))
                           for n in names]}


def _stub_flow(monkeypatch, *, options=None, abstain=False, broken=False):
    """管线替身：`_prepare_context` 返真实解析 intent（abstain/broken 档除外），
    `retriever.relaxation_options` 返固定假选项——只换 `wf.DatasetRecommendationWorkflow`
    类符号，模块级 sanitize_* 真源保持真实。"""
    import dataset_recommender.app.workflow as wf

    class _Flow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                # suppress 由单锚点真源现算（与 retriever 真实现同路径），桩只给
                # key/kind/label/count/candidates——解析器行为变化时本测试自动跟随。
                out = []
                for o in (options or []):
                    d = dict(o)
                    d.setdefault("suppress", relax_key_to_suppressed(intent, d["key"]))
                    out.append(d)
                return out

        def _prepare_context(self, query, **kwargs):
            if broken:
                raise RuntimeError("boom")
            if abstain:
                return (SimpleNamespace(abstain=True), [], [],
                        None, None, None, None, None)
            return (parse_query(query, None), [], [],
                    None, None, None, None, None)

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _Flow)


def _degenerate_ctx(**over):
    """屏面 degenerate 的环内现场：最近一次 rank 试跑零命中（health=degenerate）。"""
    ctx = {"steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "degenerate", "total": 0,
                                 "query": BAD_QUERY}}],
           "search_sources": None, "has_results": False, "result_total": 0,
           "current_query": BAD_QUERY}
    ctx.update(over)
    return ctx


# ---------------------------------------------------------------- 机械闸三态与拒绝档

def test_gate_rejects_without_query():
    """环内无任何检索事实且现场无当前检索句 → no_query 如实拒绝（不进管线）。"""
    out = AX._loop_ask_relax({"reason": "条件打架"}, None, {})
    assert out["asked"] is False and out["refuse_reason"] == "no_query"
    assert out["ask_relax"] is None and out["options"] == []
    AskRelaxResult.model_validate(out)


def test_gate_rejects_when_screen_not_degenerate():
    """屏面健康（rank healthy）或未经本环核验（unknown：有结果但环内无事实）
    → screen_not_degenerate 如实拒绝——问用户没有意义的不打扰。"""
    healthy = _degenerate_ctx()
    # 2026-09-18 专家化批刻意更新：`_screen_baseline` 只认真实上屏批次——
    # rank 步补 displayed=True+batch 才代表「屏面上是它」（纯试跑不再冒充屏面）。
    healthy["steps"] = [{"verb": "rank", "ok": True,
                         "result": {"health": "healthy", "total": 7, "query": "q",
                                    "displayed": True, "batch": {"kind": "rank"}}}]
    out = AX._loop_ask_relax({}, None, healthy)
    assert out["asked"] is False and out["refuse_reason"] == "screen_not_degenerate"
    unknown = {"has_results": True, "result_total": 3, "current_query": "旧查询",
               "steps": []}
    out2 = AX._loop_ask_relax({}, None, unknown)
    assert out2["asked"] is False and out2["refuse_reason"] == "screen_not_degenerate"
    AskRelaxResult.model_validate(out2)


def test_gate_screen_baseline_ignores_rejected_trial():
    """（2026-09-18 专家化批，WP1 回归钉·ask.relax 侧）：上屏健康批次之后紧跟被拒的
    degenerate 试跑时，基线仍须取上屏批次 → screen_not_degenerate 如实拒绝。
    旧口径会把被拒试跑误当屏面（degenerate）而错误放行弹卡——试跑只是情报。"""
    ctx = _degenerate_ctx()
    ctx["steps"] = [
        {"verb": "rank", "ok": True,
         "result": {"health": "healthy", "total": 7, "query": "q",
                    "displayed": True, "batch": {"kind": "rank"}}},
        {"verb": "rerank", "ok": True,
         "result": {"health": "degenerate", "total": 0, "query": "worse",
                    "rejected": True, "reject_reason": "trial_degenerate",
                    "displayed": False, "batch": None}},
    ]
    out = AX._loop_ask_relax({}, None, ctx)
    assert out["asked"] is False and out["refuse_reason"] == "screen_not_degenerate"
    AskRelaxResult.model_validate(out)


def test_gate_rejects_on_parse_error(monkeypatch):
    """条件预演异常 → parse_error 如实拒绝（弹卡是增量便利，失败不炸环）。"""
    _stub_flow(monkeypatch, options=[_option()], broken=True)
    out = AX._loop_ask_relax({}, None, _degenerate_ctx())
    assert out["asked"] is False and out["refuse_reason"] == "parse_error"
    assert "rerank" in out["note_zh"]


def test_gate_rejects_on_abstain(monkeypatch):
    """解析弃权 → abstain 如实拒绝（没有可放松的维度；提示改用 rerank 改写查询本身）。"""
    _stub_flow(monkeypatch, options=[_option()], abstain=True)
    out = AX._loop_ask_relax({}, None, _degenerate_ctx())
    assert out["asked"] is False and out["refuse_reason"] == "abstain"
    assert "rerank" in out["note_zh"]


def test_gate_rejects_when_no_options(monkeypatch):
    """各维试算均零新增（或无可放松约束）→ no_options 如实拒绝。"""
    _stub_flow(monkeypatch, options=[])
    out = AX._loop_ask_relax({}, None, _degenerate_ctx())
    assert out["asked"] is False and out["refuse_reason"] == "no_options"
    AskRelaxResult.model_validate(out)


# ---------------------------------------------------------------- 放行档：载荷形状

def test_asked_payload_shape_and_suppress_merge(monkeypatch):
    """degenerate 放行：载荷三键齐；suppress=既有忽略+本选项翻译合并去重（整组替换
    语义不丢既有）；卡层不带 preview（2026-09-14 评审退役）；digest 不带 suppress；
    note 告知环已暂停等作答、作答后自动重跑回环（2026-09-14 ask 暂停/重启批改口径，
    旧档「提示调 finish」随暂停语义退役）。"""
    _stub_flow(monkeypatch, options=[
        _option("dim:disease", "drop", "疾病", 12),
        _option("only:tissue", "only", "组织", 30, names=("DS-A",)),
    ])
    ctx = _degenerate_ctx(search_suppressed_constraints=["include:disease"])
    out = AX._loop_ask_relax({"reason": "物种与年份互相打架"}, None, ctx)
    assert out["asked"] is True and out["query"] == BAD_QUERY
    assert out["reason"] == "物种与年份互相打架"
    assert "环已暂停等作答" in out["note_zh"]
    # digest（回模型）：只有 key/kind/label/count。
    assert out["options"] == [
        {"key": "dim:disease", "kind": "drop", "label": "疾病", "count": 12},
        {"key": "only:tissue", "kind": "only", "label": "组织", "count": 30},
    ]
    payload = out["ask_relax"]
    assert payload["query"] == BAD_QUERY and payload["reason"] == "物种与年份互相打架"
    card = payload["options"]
    assert len(card) == 2
    # drop 档：relaxed=["include:disease"]，与既有 ["include:disease"] 合并去重 → 单份。
    assert card[0]["suppress"] == ["include:disease"]
    assert "preview" not in card[0]  # 卡层 preview 已退役（2026-09-14 评审）
    # only 档：删其余全部（species/disease 正向 + FASTQ），与既有合并保序去重。
    assert card[1]["suppress"] == [
        "include:disease", "include:species", "raw:required"]
    assert "preview" not in card[1]
    AskRelaxResult.model_validate(out)


def test_asked_without_prior_suppressed(monkeypatch):
    """无既有忽略时 suppress 就是本选项翻译清单（合并逻辑不退化）。"""
    _stub_flow(monkeypatch, options=[_option("dim:disease", "drop", "疾病", 5)])
    out = AX._loop_ask_relax({}, None, _degenerate_ctx())
    assert out["asked"] is True
    assert out["ask_relax"]["options"][0]["suppress"] == ["include:disease"]


def test_relax_to_suppressed_delegates_to_single_anchor():
    """`_relax_to_suppressed` 委托解析层单锚点 `relax_key_to_suppressed`：合法 key
    逐位等价、保序去重；非法/与 intent 不符的 key 抛 `_RerankRelaxParamError`
    （如实拒绝，不静默忽略）——环内入参契约与网页放松卡同词表。"""
    intent = parse_query(BAD_QUERY, None)
    keys = ["dim:disease", "raw", "only:tissue"]
    merged: list[str] = []
    for k in keys:
        for s in relax_key_to_suppressed(intent, k):
            if s not in merged:
                merged.append(s)
    assert AX._relax_to_suppressed(intent, keys) == merged
    with pytest.raises(AX._RerankRelaxParamError):
        AX._relax_to_suppressed(intent, ["dim:atlantis"])
    with pytest.raises(AX._RerankRelaxParamError):
        AX._relax_to_suppressed(intent, ["only:platform"])  # 不在本查询约束里


# ---------------------------------------------------------------- 检索半核销证据步

def test_is_search_settle_step():
    """asked=True 的 ask.relax 核销「找数据」半（等用户作答不算「没做」）；
    被拒（asked=False）/ 非 ok / 无 result 不算；rank 三元组回归不破。"""
    assert AX._is_search_settle_step({"ok": True, "verb": "rank"}) is True
    assert AX._is_search_settle_step(
        {"ok": True, "verb": "ask.relax", "result": {"asked": True}}) is True
    assert AX._is_search_settle_step(
        {"ok": True, "verb": "ask.relax", "result": {"asked": False}}) is False
    assert AX._is_search_settle_step(
        {"ok": False, "verb": "ask.relax", "result": {"asked": True}}) is False
    assert AX._is_search_settle_step({"ok": True, "verb": "ask.relax"}) is False
    assert AX._is_search_settle_step({"ok": True, "verb": "finish"}) is False


# ---------------------------------------------------------------- 预算机械闸

def test_ask_relax_budget_gate():
    """MAX_ASK_RELAX=1：提议过即消耗，用满后再提议 → 机械拒绝、按 done 收尾、
    如实点名「询问放宽条件」「预算已用完」；未用满放行。"""
    assert AX.MAX_ASK_RELAX == 1
    state = {"utterance": "找找人类肺癌的数据",
             "steps": [{"verb": "ask.relax", "ok": True}]}
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "ask.relax", "quoted": "找找人类肺癌的数据"}, state)
    assert nxt is None and violation == ""
    assert "询问放宽条件" in refused and "预算已用完" in refused
    nxt2, *_ = AX._adjudicate_decide_obj(
        {"verb": "ask.relax", "quoted": "找找人类肺癌的数据"},
        {"utterance": "找找人类肺癌的数据", "steps": []})
    assert nxt2 is not None and nxt2["verb"] == "ask.relax"


# ---------------------------------------------------------------- 登记钉

def test_ask_relax_registered():
    """LOOP_TOOLS / LOOP_RESULT_MODELS / search 套件面登记齐；本地工具不进联网清单。"""
    assert AX._LOOP_RESULT_MODELS["ask.relax"] is AskRelaxResult
    tool = AX.LOOP_TOOLS["ask.relax"]
    assert tool["readonly"] is True and tool["needs_context"] is True
    assert tool["card_kind"] == "ask_relax" and tool["run"] is AX._loop_ask_relax
    assert "ask.relax" in AX._SUITE_LOOP_VERBS["search"]
    assert "ask.relax" not in AX._NETWORK_LOOP_TOOLS
    assert AP.VERB_BY_NAME["ask.relax"].zh == "询问放宽条件"


# ---------------------------------------------------------------- turn.py 组卷透传

def _exec_plan(steps):
    return {"kind": AP.EXEC, "verb": "search.rerun", "source": "agent",
            "llm_status": "ok", "steps": steps}


def _stub_agent(monkeypatch, plan):
    monkeypatch.setattr(AX, "agent_available", lambda: True)
    monkeypatch.setattr(AX, "plan_with_agent_events",
                        lambda *a, **k: (dict(plan), []))
    monkeypatch.setattr(AX, "plan_with_agent",
                        lambda *a, **k: (dict(plan), []))


def _payload():
    return {"query": BAD_QUERY, "reason": "条件互相打架",
            "options": [{"key": "dim:species", "kind": "drop", "label": "物种",
                         "count": 9,
                         "suppress": ["include:species"]}]}


def test_turn_passthrough_asked_with_batches(monkeypatch):
    """真发起的 ask.relax 步 → 响应带 ask_relax 键（原样透传）；**同响应有批次时
    也不被吞**（extra.update 回归钉——重赋值曾静默吃掉该键）。"""
    plan = _exec_plan([
        {"verb": "rank", "ok": True,
         "result": {"total": 0, "health": "degenerate", "query": BAD_QUERY,
                    "batch": {"kind": "rank", "label": BAD_QUERY,
                              "query_raw": BAD_QUERY, "query_effective": BAD_QUERY,
                              "payload": {"ok": True, "result_total": 0,
                                          "results": []}}}},
        {"verb": "ask.relax", "ok": True,
         "result": {"asked": True, "query": BAD_QUERY, "ask_relax": _payload()}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn(BAD_QUERY, config=CFG, search_params=SP)
    assert out["ask_relax"] == _payload()
    assert out["result_batches"]  # 批次仍在（同响应共存）


def test_turn_passthrough_refused_absent(monkeypatch):
    """对偶钉：拒绝档（asked=False）/ 无 ask.relax 步 → 键不出现，响应与现状一致。"""
    plan = _exec_plan([
        {"verb": "ask.relax", "ok": True,
         "result": {"asked": False, "refuse_reason": "screen_not_degenerate",
                    "ask_relax": None}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn(BAD_QUERY, config=CFG, search_params=SP)
    assert "ask_relax" not in out
    plan2 = _exec_plan([
        {"verb": "rank", "ok": True,
         "result": {"total": 0, "health": "degenerate", "query": BAD_QUERY}},
    ])
    _stub_agent(monkeypatch, plan2)
    out2 = turn.route_turn(BAD_QUERY, config=CFG, search_params=SP)
    assert "ask_relax" not in out2


# ---------------------------------------------------------------- webapp 响应体透传

def _body_result(**over):
    result = {"route": "search", "query": "x", "plan": None, "echo_zh": "",
              "retrieval": None, "via": "agent", "needs_agent": False,
              "suggestions": [], "result_payload": None, "preliminary_final": False}
    result.update(over)
    return result


def test_response_body_passthrough_when_ask_relax_present():
    body = webapp._utterance_response_body(_body_result(ask_relax=_payload()))
    assert body["ask_relax"] == _payload()


def test_response_body_no_key_without_ask_relax():
    body = webapp._utterance_response_body(_body_result())
    assert "ask_relax" not in body
    body2 = webapp._utterance_response_body(_body_result(ask_relax=None))
    assert "ask_relax" not in body2


# ---------------------------------------------------- relax_live 情报注入（2026-09-14 二批）
#
# 诊断（repro_loop.py 实录）：环真跑 rank/rerank/search.rerun 全 degenerate，但
# ① rerank 的 probe_zh 不进 decide 投影；② 屏面告警只给通用对策清单——模型看不到
# 「放松哪维有救」的机械事实，盲选后 finish。修复：degenerate 试跑结果内嵌
# relax_live（零 LLM 预演 digest），告警据此点名活选项或如实标注不适用。

def _alert_steps(marker):
    r = {"health": "degenerate", "total": 0, "query": BAD_QUERY,
         "resolution_status": "no_match"}
    if marker is not None:
        r["relax_live"] = marker
    return [{"verb": "rank", "ok": True, "result": r}]


def test_screen_alert_names_live_options():
    """relax_live 非空 → 告警点名活选项（key/label/count）+ ask.relax 是对因对策。"""
    block = AX._screen_alert_block_zh(_alert_steps([
        {"key": "dim:disease", "label": "Pancreatic Cancer", "count": 2}]))
    assert "dim:disease" in block and "Pancreatic Cancer" in block
    assert "2 条" in block and "对因对策" in block and "ask.relax" in block


def test_screen_alert_empty_relax_live_marks_unfit():
    """relax_live 空清单 → 如实标注 ask.relax 不适用，并从可选对策清单摘掉
    （防模型空撞 no_options 机械闸白烧一步）。"""
    block = AX._screen_alert_block_zh(_alert_steps([]))
    assert "ask.relax 不适用" in block
    assert "弹卡问用户放松哪个" not in block


def test_screen_alert_without_relax_live_keeps_generic():
    """无 relax_live 键（旧步/弃权/预演异常档）→ 通用对策清单保留 ask.relax，不妄断。"""
    block = AX._screen_alert_block_zh(_alert_steps(None))
    assert "ask.relax" in block and "不适用" not in block


def test_step_projection_carries_relax_live():
    """rank/rerank 的 decide 投影带 relax_live（模型看到的与告警同源）；缺键不补。"""
    step = {"verb": "rank", "ok": True, "card_kind": "rank",
            "result": {"query": "q", "total": 0, "health": "degenerate",
                       "resolution_status": "no_match",
                       "relax_live": [{"key": "dim:disease", "label": "X", "count": 2}]}}
    proj = AX._step_projection(step)
    assert proj["result"]["relax_live"] == [
        {"key": "dim:disease", "label": "X", "count": 2}]
    step2 = {"verb": "rank", "ok": True, "card_kind": "rank",
             "result": {"query": "q", "total": 3, "health": "healthy"}}
    assert "relax_live" not in AX._step_projection(step2)["result"]
