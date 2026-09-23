# -*- coding: utf-8 -*-
""" 下一步行动 · suggested_recipe 后端契约门（设计约定）。

钉四件事：
1. **allowlist 单一真源**：`action_plan.SUGGESTED_RECIPES` 每项只允许缩小到既有已验证能力
   （动词 ⊆ VERB_SPECS 封闭词表）；前端 `ladder_core.js` 的 LADDER_RECIPES id 必须是其后端
   子集、且每项 verb 落在后端 recipe 的动词集内（前端只是文案/模板镜像，不许发明能力）。
2. **合法 hint 缩小路由**：suggested_recipe 在允许集内 → 动词选择面收窄（plan_action
   allowed_verbs 机械闸；agent 路径产出的 plan 在 turn 层机械收窄，降 none 附如实注记）。
3. **非法 hint 忽略回普通路由并如实记录**：不在表 → 行为与不传逐位一致 + 响应 recipe_note。
4. **不绕安全闸**：入幂等指纹（同 req_id 不同 recipe → 409）；「AI 执行」关 → 忽略；
   极性门（否定句 cancelled=True）不受收窄影响。

零网络：LLM 一律靠 llm_call 注入（plan_action / route_turn 同纪律）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import turn
from dataset_recommender.app import webapp

client = TestClient(webapp.app, base_url="http://127.0.0.1")


def _llm(payload):
    """把一份 dict 当成 LLM 的返回。"""
    return lambda _prompt: json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------- 1. allowlist 单一真源

def test_every_recipe_verb_is_an_existing_capability():
    """SUGGESTED_RECIPES 每项动词必须是封闭词表 VERB_SPECS 内的既有能力（不新造）。"""
    assert AP.SUGGESTED_RECIPES, "allowlist 不许为空"
    for recipe, verbs in AP.SUGGESTED_RECIPES.items():
        assert verbs, f"{recipe} 必须有至少一个动词"
        for v in verbs:
            assert v in AP.VERB_BY_NAME, f"recipe {recipe} 引用了不存在的动词 {v}"


def test_frontend_recipe_ids_are_subset_of_backend_single_source():
    """前端 LADDER_RECIPES 是后端 SUGGESTED_RECIPES 的镜像子集（id 与动词都不得越界）。"""
    import re
    from pathlib import Path
    core_js = (Path(__file__).resolve().parents[1]
               / "web" / "static" / "js" / "search" / "ladder_core.js").read_text(encoding="utf-8")
    ids = set(re.findall(r"^\s{4}([a-z_]+):\s*\{", core_js, re.M))
    # 只挑 LADDER_RECIPES 块内的 id（块以注释开头，直接对全体小写 id 断言子集关系更稳：
    # 前端镜像必须 ⊆ 后端，任何前端新 id 都会在此红）。
    assert ids, "前端镜像表解析为空"
    backend = set(AP.SUGGESTED_RECIPES.keys())
    extra = ids - backend
    assert not extra, f"前端 LADDER_RECIPES 出现后端 allowlist 没有的 id：{sorted(extra)}"
    # 每项前端 verb 必须落在后端同 id recipe 的动词集内。
    for recipe_id in ids:
        m = re.search(rf"{recipe_id}:\s*\{{[^}}]*?verb:\s*\"([a-z.]+)\"", core_js, re.S)
        assert m, f"前端 {recipe_id} 缺 verb 声明"
        assert m.group(1) in AP.SUGGESTED_RECIPES[recipe_id], (
            f"前端 {recipe_id} 的 verb {m.group(1)} 不在后端 recipe 动词集")


def test_resolve_suggested_recipe_allowlist():
    """合法 → 动词集；空/未知 → None（调用方按「未提供」处理）。"""
    assert AP.resolve_suggested_recipe("manifest") == frozenset({"pack.download"})
    assert AP.resolve_suggested_recipe("compare_datasets") == frozenset({"compare.datasets"})
    assert AP.resolve_suggested_recipe(None) is None
    assert AP.resolve_suggested_recipe("") is None
    assert AP.resolve_suggested_recipe("   ") is None
    assert AP.resolve_suggested_recipe("核验前10条") is None, "不在表 = 忽略（不新造 recipe）"
    assert AP.resolve_suggested_recipe("manifest; DROP TABLE") is None, "非法串不进城"
    assert AP.resolve_suggested_recipe(" manifest ") == frozenset({"pack.download"}), "容忍首尾空白"


# ---------------------------------------------------------------- 2. 合法 hint 缩小路由

def test_plan_action_keeps_verb_inside_allowed_recipe():
    """合法 recipe 内动词照常执行（收窄不误伤）。"""
    plan = AP.plan_action(
        "把当前这批结果打包成下载清单",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "打包成下载清单", "limit": 10, "confidence": "high"}),
        allowed_verbs=AP.resolve_suggested_recipe("manifest"),
    )
    assert plan["verb"] == "pack.download", plan


def test_plan_action_rejects_verb_outside_allowed_recipe():
    """recipe 收窄面外动词 → 进 rejected 降 none（与未知 verb 同一「不做，但要说」渠道）。"""
    plan = AP.plan_action(
        "对比当前结果的前两条数据集",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "对比", "limit": 10, "confidence": "high"}),
        allowed_verbs=AP.resolve_suggested_recipe("compare_datasets"),
    )
    assert plan["verb"] == "none", plan
    assert "pack.download" in plan["rejected"], plan


def test_plan_action_agent_only_recipe_falls_back_to_none():
    """环内专属动词（compare.datasets 等）不在保底通道全表：合法 recipe 但交集为空 →
    天然 none + 如实 rejected（agent 关闭时前端本就会隐藏该 chip，这里只是不扩权）。"""
    plan = AP.plan_action(
        "对比当前结果的前两条数据集",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "compare.datasets", "confidence": "high"}),
        allowed_verbs=AP.resolve_suggested_recipe("compare_datasets"),
    )
    assert plan["verb"] == "none", plan
    assert "compare.datasets" in plan["rejected"], plan


def test_turn_legal_hint_routes_to_recipe_verb():
    """route_turn 层：合法 hint + LLM 判出 recipe 动词 → tool 路由、动词保留。"""
    out = turn.route_turn(
        "把当前这批结果打包成下载清单",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "打包成下载清单", "limit": 10, "confidence": "high"}),
        suggested_recipe="manifest",
    )
    assert out["route"] == "tool", out
    assert out["plan"]["verb"] == "pack.download", out["plan"]


def test_turn_legal_hint_narrows_out_of_set_verb():
    """route_turn 层：合法 hint 但 LLM 判到收窄面外动词 → 机械收窄为 none + recipe_note。"""
    out = turn.route_turn(
        "对比当前结果的前两条数据集",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "对比", "limit": 10, "confidence": "high"}),
        suggested_recipe="compare_datasets",
    )
    # 保底通道交集为空 → plan_action 已拒为 none；recipe_note 只在「被收窄」路径出现。
    assert out["plan"]["verb"] == "none", out["plan"]
    assert "pack.download" in out["plan"]["rejected"], out["plan"]
    # 机械收窄证据（_recipe_narrow_plan 只对 EXEC 生效；此处已被 plan_action 拒掉，
    # recipe_note 由「非法忽略」之外的分支生成——合法但未命中时无注记，行为如实）。
    assert out.get("recipe_note") is None or "allowlist" not in out["recipe_note"]


def test_recipe_narrow_plan_mechanical_gate():
    """`_recipe_narrow_plan` 机械闸（agent 路径产出的 plan 在 turn 层的收窄落点）：
    EXEC 动词不在 recipe 允许集 → 降 none + 如实注记；在集内 / 非 EXEC → 一字不动。"""
    # EXEC 且动词在集内：原样放行（含 cancelled 安全闸不碰）。
    kept = turn._recipe_narrow_plan(
        {"kind": "exec", "verb": "feasibility.run", "cancelled": False},
        frozenset({"feasibility.run"}))
    assert kept["verb"] == "feasibility.run" and kept["kind"] == "exec"
    # EXEC 且动词在集外：降 none + recipe_narrowed 证据。
    narrowed = turn._recipe_narrow_plan(
        {"kind": "exec", "verb": "pack.download", "cancelled": True},
        frozenset({"feasibility.run"}))
    assert narrowed["verb"] == "none", narrowed
    assert narrowed.get("recipe_narrowed") == "pack.download", narrowed
    assert narrowed.get("cancelled") is not True, "降 none 时取消标记随 none 语义复位（不执行即安全）"
    # 非 EXEC（search/none）不动。
    search_plan = {"kind": "route", "verb": "search.new", "effective_query": "x"}
    assert turn._recipe_narrow_plan(search_plan, frozenset({"feasibility.run"})) is search_plan
    # 无 recipe / 无 plan：不动。
    assert turn._recipe_narrow_plan(None, frozenset({"feasibility.run"})) is None
    assert turn._recipe_narrow_plan({"kind": "exec", "verb": "pack.download"}, None)["verb"] == "pack.download"


def test_turn_legal_hint_agent_plan_post_hoc_narrow():
    """route_turn 层：合法 hint + LLM 判到收窄面外动词 → plan_action 闸已拒为 none
    （保底通道交集为空时自然 none）；recipe_note 恒不出现「allowlist 忽略」口径。"""
    out = turn.route_turn(
        "对比当前结果的前两条数据集",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "对比", "limit": 10, "confidence": "high"}),
        suggested_recipe="feasibility",
    )
    assert out["plan"]["verb"] == "none", out["plan"]
    assert "pack.download" in out["plan"]["rejected"], out["plan"]
    assert not out.get("recipe_note") or "allowlist" not in out["recipe_note"], out.get("recipe_note")


# ---------------------------------------------------------------- 3. 非法 hint 忽略 + 如实记录

def test_illegal_hint_ignored_and_recorded():
    """不在 allowlist → 忽略回普通路由 + recipe_note 如实记录；路由结果与不传一致。"""
    base = turn.route_turn(
        "人类肺癌数据",
        llm_call=_llm({"verb": "search.new", "quoted": "人类肺癌数据",
                       "effective_query": "人类肺癌数据", "confidence": "high"}),
    )
    hinted = turn.route_turn(
        "人类肺癌数据",
        llm_call=_llm({"verb": "search.new", "quoted": "人类肺癌数据",
                       "effective_query": "人类肺癌数据", "confidence": "high"}),
        suggested_recipe="核验前10条",
    )
    assert hinted["route"] == base["route"] == "search", hinted
    assert hinted["query"] == base["query"]
    assert "recipe_note" in hinted, "非法 hint 必须如实记录"
    assert "不在 allowlist" in hinted["recipe_note"], hinted["recipe_note"]
    # 普通请求（无 hint）不带 recipe_note。
    assert "recipe_note" not in base


def test_hint_does_not_bypass_agent_off_gate():
    """「AI 执行」关：suggested_recipe 被忽略（规则直达），连 plan 都不进。"""
    out = turn.route_turn(
        "把当前这批结果打包成下载清单",
        has_results=True, result_total=10,
        use_agent=False,
        suggested_recipe="manifest",
    )
    assert out["route"] == "none" or out["route"] == "search", out  # 规则兜底照旧
    assert out.get("recipe_note") is None, "agent 关时不携带 recipe，无注记"


def test_hint_does_not_bypass_cancelled_polarity_gate():
    """否定句（「别打包了」）极性门不受收窄影响：allowed 内动词仍 cancelled=True。"""
    plan = AP.plan_action(
        "先看清单，别打包了",
        has_results=True, result_total=10,
        llm_call=_llm({"verb": "pack.download", "quoted": "打包", "limit": 10, "confidence": "high"}),
        allowed_verbs=AP.resolve_suggested_recipe("manifest"),
    )
    assert plan["verb"] == "pack.download", plan
    assert plan["cancelled"] is True, "否定取消态不能被 recipe 收窄消掉（安全闸不绕）"


def test_hint_does_not_bypass_requires_results_gate():
    """屏上无结果时 requires_results 动词照样 blocked（不因 hint 放行）。"""
    plan = AP.plan_action(
        "打包下载清单",
        has_results=False, result_total=0,
        llm_call=_llm({"verb": "pack.download", "quoted": "打包", "limit": 10, "confidence": "high"}),
        allowed_verbs=AP.resolve_suggested_recipe("manifest"),
    )
    assert plan["verb"] == "pack.download", plan
    assert plan.get("blocked_reason"), "无结果屏的打包必须 blocked（不因 hint 扩权）"


# ---------------------------------------------------------------- 4. 幂等指纹 + 端点透传

def test_fingerprint_differs_by_suggested_recipe():
    """suggested_recipe 进幂等指纹：同 req_id 不同 recipe = 另一次请求（撞 409）。"""
    def fp(**overrides):
        fields = {"utterance": "对比前两条", "has_results": False, "result_total": 0,
                  "query": "", "current_filters": None, "sources": None,
                  "provider": "mock", "use_llm": False, "mock_llm": False,
                  "api_key": None, "base_url": "", "model": None, "agent": True,
                  "top_k": None, "rerank": "", "recall": "", "strategy": "",
                  "facet_filters": None, "suppressed_constraints": None,
                  "lenient_dims": None, "date_from": "", "date_to": "", "polish": True,
                  "req_id": None, "stream": False}
        fields.update(overrides)
        payload = webapp.UtteranceRequest(**fields)
        return webapp._utterance_request_fp(payload.utterance, payload, "mock", False, False)

    fp_a = fp(suggested_recipe="compare_datasets")
    fp_b = fp(suggested_recipe="fair_check")
    assert fp_a != fp_b, "同 req_id 但 recipe 不同 = 另一次请求，指纹必须分开"
    assert fp(suggested_recipe="compare_datasets") == fp_a, "同 recipe 恒同指纹（幂等性不变）"
    assert fp() == fp(suggested_recipe=""), "缺省与空串同指纹"


def test_endpoint_same_req_id_different_recipe_409():
    """端点级：同 req_id 同句、suggested_recipe 不同 → 撞指纹 409（不当成同一次重发）。"""
    webapp._UTT_IDEM.clear()
    try:
        body = {"utterance": "对比前两条", "req_id": "p6-1", "suggested_recipe": "compare_datasets"}
        r1 = client.post("/api/utterance", json=body)
        assert r1.status_code == 200, r1.text
        r2 = client.post("/api/utterance", json=dict(body, suggested_recipe="fair_check"))
        assert r2.status_code == 409, r2.text
        # 同 recipe 原样重发 → 幂等缓存体（不二次执行路由）。
        r3 = client.post("/api/utterance", json=body)
        assert r3.status_code == 200
    finally:
        webapp._UTT_IDEM.clear()


def test_endpoint_ignored_recipe_records_note():
    """端点级：非法 recipe → 200 正常路由 + 响应 recipe_note 如实记录（不报错）。"""
    webapp._UTT_IDEM.clear()
    try:
        r = client.post("/api/utterance", json={
            "utterance": "人类肺癌数据", "suggested_recipe": "不存在的东西"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert "recipe_note" in body and "不在 allowlist" in body["recipe_note"], body
    finally:
        webapp._UTT_IDEM.clear()


def test_endpoint_field_defaults_none():
    payload = webapp.UtteranceRequest(utterance="对比前两条")
    assert payload.suggested_recipe is None, "缺省 = None（普通手打不携带）"
    payload2 = webapp.UtteranceRequest(utterance="对比前两条", suggested_recipe="compare_datasets")
    assert payload2.suggested_recipe == "compare_datasets"
