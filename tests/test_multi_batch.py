# -*- coding: utf-8 -*-
"""多批检索结果后端专项门。**全离线**：

 批次机制常驻——轮内累积批次（preliminary 在前、环内上屏批
在后），各补 batch_id（b1…轮内稳定）/seq/created_at（ISO8601）/turn_id（同轮同 id）；
active_batch 默认最后一批；legacy result_payload 镜像最后一个环内上屏批，无环内批时
镜像 active 批（仅 preliminary 批也镜像——「legacy 镜像 active batch」）；
preliminary_final 的 b 档判定用独立哨兵 loop_payload，
不再拿 result_payload is None 充当。原环境开关与 OFF 逐位一致负向钉
随代码一并摘除。

- 溯源：各批 query_raw 恒 = 本轮用户原话；
  query_effective = 该批真实生效的检索表述（preliminary = 本轮原话——pre-loop
  管线跑的就是它；rank = rank.query；rerank = rewritten_query）。
- kind 规则：preliminary / rank / rerank 原样透传环内 batch；search.rerun 采纳档
  按 ctx.replace_screen 分 search_rerun（链内重搜）与 rescue（救回端点链下）。
- webapp._utterance_response_body 条件透传：有批次才加两键（流式 final 与非流式
  共用本真源，天然同步）。
"""
from __future__ import annotations

from datetime import datetime

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, turn
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig

#: 与 test_prelim_backend 同口径的收敛检索参数。
SP = {
    "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None, "lenient_dims": None,
    "date_from": "", "date_to": "", "polish": True,
}

#: 武装到「只要走真 LLM 就一定会发调用」的配置（注入替身时只过闸、不触网）。
CFG = LLMConfig(enable_llm=True, api_key="sk-mb-test")

_SEARCH_PLAN = {
    "kind": "query", "verb": "search.new", "source": "agent",
    "llm_status": "ok", "effective_query": "", "steps": [],
}


def _stub_agent(monkeypatch, plan, *, verdict: str = "search"):
    """agent 图替身（与 test_prelim_backend 同 seam）：preliminary 发射点在
    understand 入口——替身模拟 understand 在 route_scope≠action 时调 provider
    （join + 发射）；verdict="action" 时不调（action 永不发射）。"""
    def _fake_events(*a, **k):
        if verdict != "action":
            provider = k.get("retrieval_provider")
            if provider is not None:
                provider()
        return (dict(plan), [])

    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", _fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent",
                        lambda *a, **k: (dict(plan), []))


def _exec_plan(steps):
    return {
        "kind": AP.EXEC, "verb": "search.rerun", "source": "agent",
        "llm_status": "ok", "steps": steps,
    }


# ---------------------------------------------------------------- 批次组卷

def test_preliminary_only_batch(monkeypatch):
    """只发 preliminary（无环内批）：单批 kind=preliminary，id/seq/时间戳/轮 id 齐，
    active=b1；legacy result_payload 镜像 active 批（= preliminary 载荷）。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events].count("preliminary") == 1
    batches = out["result_batches"]
    assert len(batches) == 1
    b = batches[0]
    assert b["batch_id"] == "b1" and b["seq"] == 1
    assert b["kind"] == "preliminary"
    assert b["query_raw"] == "human blood"
    assert b["label"] and len(b["label"]) <= 20
    assert b["payload"]["ok"] is True and b["payload"]["result_total"] > 0
    # created_at 是合法 ISO8601；turn_id 非空。
    datetime.fromisoformat(b["created_at"])
    assert b["turn_id"]
    assert out["active_batch"] == "b1"
    assert out["result_payload"] is b["payload"]  # legacy 镜像 active 批（中5）


def test_preliminary_batch_provenance(monkeypatch):
    """对话态（旧 current_query 在场）：preliminary 批的 label/query_raw/
    query_effective 一律记**本轮原话**——pre-loop 管线实际跑的就是它；记旧查询是
    张冠李戴。"""
    _stub_agent(monkeypatch, _SEARCH_PLAN)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG, current_query="小鼠脑旧查询",
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    assert [k for k, _ in events].count("preliminary") == 1
    b = out["result_batches"][0]
    assert b["kind"] == "preliminary"
    assert b["query_raw"] == "human blood"
    assert b["query_effective"] == "human blood"
    assert b["label"] == "human blood"


def test_preliminary_plus_rerun_adopted(monkeypatch):
    """preliminary + 环内 search.rerun 采纳：两批累积顺序 preliminary→search_rerun，
    active=最后一批；legacy 镜像采纳 payload；同轮 turn_id 一致。"""
    adopted = {"ok": True, "result_total": 7, "results": [{"dataset_name": "x"}]}
    plan = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "mouse brain",
                    "replace_screen": False, "payload": adopted}},
    ])
    _stub_agent(monkeypatch, plan)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG, current_query="旧的现场查询",
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    batches = out["result_batches"]
    assert [b["kind"] for b in batches] == ["preliminary", "search_rerun"]
    assert [b["batch_id"] for b in batches] == ["b1", "b2"]
    assert [b["seq"] for b in batches] == [1, 2]
    rb = batches[1]
    assert rb["label"] == "mouse brain"
    assert rb["query_effective"] == "mouse brain"
    assert rb["query_raw"] == "human blood"  # 本轮用户原话（契约——不是旧现场查询）
    assert rb["payload"] is adopted
    assert out["active_batch"] == "b2"
    assert out["result_payload"] is adopted  # legacy 镜像最后一个环内上屏批
    assert batches[0]["turn_id"] == batches[1]["turn_id"]


def test_search_rerun_batch_carries_disclosure_zh(monkeypatch):
    """r2p：采纳档批 additive 带 disclosure_zh（供 final a 档换屏的
    sys 留痕优先用披露句）；未采纳/无披露句的批不带该键——形状与旧版逐位一致。"""
    adopted = {"ok": True, "result_total": 7, "results": [{"dataset_name": "x"}]}
    plan = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "mouse brain",
                    "replace_screen": False, "payload": adopted,
                    "dropped_terms": ["神经"],
                    "disclosure_zh": "「神经」在库里没有收录，已按「mouse brain」重查，找到 7 条，结果区已更新。"}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn("小鼠神经胶质瘤", config=CFG, search_params=SP)
    batches = [b for b in out["result_batches"] if b["kind"] == "search_rerun"]
    assert len(batches) == 1
    assert batches[0]["disclosure_zh"] == (
        "「神经」在库里没有收录，已按「mouse brain」重查，找到 7 条，结果区已更新。")
    # 无披露句 → 批不带该键（additive 缺省即旧形状，前端无披露句时走既有通用句）。
    plan2 = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "mouse brain",
                    "replace_screen": False, "payload": adopted}},
    ])
    _stub_agent(monkeypatch, plan2)
    out2 = turn.route_turn("human blood", config=CFG, search_params=SP)
    b2 = [b for b in out2["result_batches"] if b["kind"] == "search_rerun"][0]
    assert "disclosure_zh" not in b2


def test_rescue_kind_when_replace_screen(monkeypatch):
    """rescue 端点链下（replace_screen=True）的采纳重搜 → kind=rescue。"""
    adopted = {"ok": True, "result_total": 3, "results": []}
    plan = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "human lung",
                    "replace_screen": True, "payload": adopted}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn("human blood", config=CFG, search_params=SP)
    kinds = [b["kind"] for b in out["result_batches"]]
    assert "rescue" in kinds and "search_rerun" not in kinds


def test_rerun_not_adopted_no_batch(monkeypatch):
    """search.rerun 未采纳（无 payload）→ 不产批；无 preliminary 时两键均不出现。"""
    plan = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": False, "reason": "rewrite_empty_kept_original",
                    "payload": None}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn("human blood", config=CFG, search_params=SP)
    assert "result_batches" not in out and "active_batch" not in out
    assert out["result_payload"] is None


def test_rank_rerank_batches_passthrough(monkeypatch):
    """rank/rerank/results.show 上屏批次的 kind/label/query_raw/query_effective 由环内
    工具生成（rerank label=rewritten_query；results.show 的 kind=**试跑来源动词**——
    2026-09-13 rerank 处置批：show 只是出口，批次内容来源是试跑的 rank/rerank），
    turn 原样透传不另造口径；legacy 取最后一批。"""
    rank_payload = {"ok": True, "result_total": 5, "results": []}
    rerank_payload = {"ok": True, "result_total": 4, "results": []}
    show_payload = {"ok": True, "result_total": 6, "results": []}
    plan = _exec_plan([
        {"verb": "rank", "ok": True,
         "result": {"total": 5, "displayed": True,
                    "batch": {"kind": "rank", "label": "human blood",
                              "query_raw": "human blood",
                              "query_effective": "human blood",
                              "payload": rank_payload}}},
        {"verb": "rerank", "ok": True,
         "result": {"total": 4, "displayed": True,
                    "batch": {"kind": "rerank", "label": "Homo sapiens blood RNA-seq",
                              "query_raw": "human blood",
                              "query_effective": "Homo sapiens blood RNA-seq",
                              "payload": rerank_payload}}},
        {"verb": "results.show", "ok": True,
         "result": {"shown": True, "total": 6, "displayed": True,
                    "batch": {"kind": "rank", "label": "human blood RNA",
                              "query_raw": "human blood",
                              "query_effective": "human blood RNA",
                              "payload": show_payload}}},
    ])
    _stub_agent(monkeypatch, plan)
    out = turn.route_turn("human blood", config=CFG, search_params=SP)
    batches = out["result_batches"]
    assert [b["kind"] for b in batches] == ["rank", "rerank", "rank"]
    assert batches[1]["label"] == "Homo sapiens blood RNA-seq"[:20]  # label ≤20 字截断
    assert batches[1]["query_raw"] == "human blood"
    assert batches[1]["query_effective"] == "Homo sapiens blood RNA-seq"
    assert batches[2]["query_effective"] == "human blood RNA"
    assert out["active_batch"] == "b3"
    assert out["result_payload"] is show_payload


def test_accumulation_order_preliminary_first(monkeypatch):
    """preliminary 恒在环内批次之前（时间序即批次序），多环内批按 steps 实录序。"""
    p1 = {"ok": True, "result_total": 1, "results": []}
    p2 = {"ok": True, "result_total": 2, "results": []}
    plan = _exec_plan([
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "q1", "replace_screen": False,
                    "payload": p1}},
        {"verb": "search.rerun", "ok": True,
         "result": {"adopted": True, "query": "q2", "replace_screen": False,
                    "payload": p2}},
    ])
    _stub_agent(monkeypatch, plan)
    events: list = []
    out = turn.route_turn(
        "human blood", config=CFG,
        on_event=lambda k, e: events.append((k, e)), search_params=SP)
    batches = out["result_batches"]
    assert [b["kind"] for b in batches] == [
        "preliminary", "search_rerun", "search_rerun"]
    assert [b["batch_id"] for b in batches] == ["b1", "b2", "b3"]
    assert out["active_batch"] == "b3"
    assert out["result_payload"] is p2  # 多个采纳步取最后一个
    assert len({b["turn_id"] for b in batches}) == 1


# ---------------------------------------------------------------- webapp 响应体透传

def test_response_body_passthrough_when_batches_present():
    """_utterance_response_body：route_turn 产了批次才加两键，原样透传不加工。"""
    batches = [{"batch_id": "b1", "seq": 1, "kind": "preliminary", "label": "x",
                "query_raw": "x", "query_effective": "x", "payload": {"ok": True},
                "created_at": "2026-08-17T00:00:00+00:00", "turn_id": "t"}]
    result = {
        "route": "search", "query": "x", "plan": None, "echo_zh": "",
        "retrieval": None, "via": "agent", "needs_agent": False, "suggestions": [],
        "result_payload": None, "preliminary_final": False,
        "result_batches": batches, "active_batch": "b1",
    }
    body = webapp._utterance_response_body(result)
    assert body["result_batches"] is batches
    assert body["active_batch"] == "b1"


def test_response_body_no_keys_without_batches():
    """对偶钉：无批次 → 两键均不出现，键集与现状逐位一致。"""
    result = {
        "route": "search", "query": "x", "plan": None, "echo_zh": "",
        "retrieval": None, "via": "rule_direct", "needs_agent": False,
        "suggestions": [], "result_payload": None, "preliminary_final": False,
    }
    body = webapp._utterance_response_body(result)
    assert "result_batches" not in body and "active_batch" not in body
    assert set(body) == {
        "ok", "route", "query", "plan", "echo_zh", "retrieval", "via",
        "needs_agent", "suggestions", "agent", "result_payload", "preliminary_final",
    }
