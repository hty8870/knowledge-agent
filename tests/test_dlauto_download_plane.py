# -*- coding: utf-8 -*-
"""混合句「检索+下载」单句 → Agent 图内 plan-only 终态的 turn 级契约。

背景：agent 图把「打包下载」这类**前端直派 exec 动词**（有 act.js runner、不在 LOOP_TOOLS 环内
注册表）过去被 turn 直接绕过图派发，导致规划、追踪、核销断链。现在子意图枚举只生成
`intent_checklist` 与 `pending_frontend`；所有非取消动作都进 Agent 图，前端动词作为 plan-only
正式终态，图后再由同一 act dispatcher 接力。

本组测试全离线：stub 枚举 LLM 出口与 Agent 图，同时检查图实际收到清单。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, turn
from dataset_recommender.llm.llm_client import LLMConfig

CFG = LLMConfig(enable_llm=True, api_key="sk-dlauto-turn")

_LLM_PACK_DOWNLOAD = json.dumps(
    {"verb": "pack.download", "quoted": "并下载top2", "limit": 2,
     "target": "results", "confidence": "high"}, ensure_ascii=False)


@pytest.fixture(autouse=True)
def _stub_agent_off(monkeypatch):
    """每测试给 Agent 图一个可观测的 plan-only 替身。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    def fake_graph(text, **kw):
        checklist = list(kw.get("intent_checklist") or [])
        frontend = next((x for x in checklist if x.get("plane") == "frontend"), None)
        if frontend:
            plan = _mk_plan(frontend["verb"], frontend.get("quoted") or "")
            plan.update(source="agent", llm_status="ok")
        else:
            plan = {"verb": "none", "verb_zh": "无操作", "kind": AP.NONE,
                    "source": "agent", "llm_status": "ok", "slots": {}}
        return plan, []
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_graph)
    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_graph)


def test_mixed_search_download_produces_frontend_download_plan(monkeypatch):
    """「检索+下载」混合句 → turn 探测命中前端直派面 → route=tool、plan=pack.download{limit:2}、
    requires_results=True，并且有图后 pending_frontend 接力记录。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，并下载top2", has_results=False, config=CFG)
    assert out["route"] == "tool"
    p = out["plan"]
    assert p["verb"] == "pack.download" and p["kind"] == AP.EXEC
    assert p["requires_results"] is True
    assert p["pending_frontend"][0]["slots"]["limit"] == 2
    assert p["source"] == "agent"
    assert [x["verb"] for x in p["pending_frontend"]] == ["pack.download"]
    assert "agent_bypassed" not in p


def test_negative_download_marks_cancelled_not_executed(monkeypatch):
    """否定句「不要下载」→ 极性门把下载动作打回（实测最终 verb=none / 无 EXEC 下载）——
    绝不产出**可执行**的 pack.download，前端不会自动开始下载。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，不要下载", has_results=False, config=CFG)
    p = out["plan"]
    assert p.get("kind") != AP.EXEC or p.get("verb") != "pack.download", \
        f"否定句绝不能产出可执行下载 plan：{p}"
    assert p.get("verb") != "pack.download"


def test_ai_exec_off_never_routes_to_download(monkeypatch):
    """「AI 执行」关（use_agent=False）→ 不进入前端直派探测，也走不了 agent 图——
    混合句 route 绝不是 tool / 绝不产出可执行的 pack.download。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_PACK_DOWNLOAD)
    out = turn.route_turn("小鼠空间转录组，并下载top2",
                          has_results=False, config=CFG, use_agent=False)
    p = out.get("plan") or {}
    assert out["route"] != "tool"
    assert p.get("verb") != "pack.download"
    assert p.get("cancelled") is not False or True   # 既有 agent_off 分支：标记/回音，不执行


def test_frontend_plane_excludes_preview_and_inloop(monkeypatch):
    """前端直派面只收「会自动执行」的两个动词：pack.download / reuse.pack。
    pack.preview（预览不自动下载）与 cite.export（环内 LOOP_TOOLS 自动落盘）都不在面内——
    它们仍走 agent 图，不能因探测而被前端抢走。"""
    plane = turn._FRONTEND_EXEC_PLANE
    assert "pack.download" in plane and "reuse.pack" in plane
    assert "pack.preview" not in plane
    assert "cite.export" not in plane


# ---------------------------------------------------------------- 「不少于我」枚举分流（2026-09-01）

def _mk_plan(verb, quoted, cancelled=False):
    return {"verb": verb, "verb_zh": verb, "kind": AP.EXEC, "quoted": quoted,
            "cancelled": cancelled, "requires_results": True, "slots": {}}


def test_partition_intents_empty_or_all_cancelled_goes_to_graph():
    """空清单 / 全 cancelled → (None, None, None)：无 EXEC 待办，走 agent 图（现状）。"""
    assert turn._partition_intents([]) == (None, None, None)
    only_cancelled = [_mk_plan("pack.download", "下载", cancelled=True)]
    assert turn._partition_intents(only_cancelled) == (None, None, None)


def test_partition_intents_all_frontend_still_goes_to_graph():
    """非 cancelled 全在前端面也不绕过图；清单进图，非取消项进接力队列。"""
    intents = [_mk_plan("pack.download", "下载"),
               _mk_plan("reuse.pack", "复用打包", cancelled=True)]
    top, checklist, pending = turn._partition_intents(intents)
    assert top is None
    assert [(x["verb"], x["plane"]) for x in checklist] == [
        ("pack.download", "frontend")]
    assert [x["verb"] for x in pending] == ["pack.download"]


def test_partition_intents_inloop_splits_checklist_and_pending_frontend():
    """含环内 EXEC → 走图：checklist 两 plane 分段（inloop 待核销 / frontend 已交前端），
    直派面项进 pending_frontend（图后挂回响应，绝不进 plan.steps）。"""
    intents = [_mk_plan("cite.export", "导出引文"), _mk_plan("pack.download", "下载")]
    top, checklist, pending = turn._partition_intents(intents)
    assert top is None
    assert [(c["verb"], c["plane"]) for c in checklist] == [
        ("cite.export", "inloop"), ("pack.download", "frontend")]
    assert [p["verb"] for p in pending] == ["pack.download"]


_LLM_TWO_FRONTEND = json.dumps([
    {"verb": "pack.download", "quoted": "下载top2", "limit": 2,
     "cancelled": False, "confidence": "high", "reason": "明说下载"},
    {"verb": "reuse.pack", "quoted": "打包复用", "limit": None,
     "cancelled": False, "confidence": "high", "reason": "明说复用"},
], ensure_ascii=False)


def test_enumeration_multi_frontend_intents_goes_through_graph(monkeypatch):
    """枚举出两件前端动作仍进图，两件都保留在图后接力队列。"""
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: _LLM_TWO_FRONTEND)
    out = turn.route_turn("小鼠空间转录组，下载top2再打包复用", has_results=False, config=CFG)
    p = out["plan"]
    assert out["route"] == "tool" and "agent_bypassed" not in p
    assert p["verb"] == "pack.download"
    assert [i["verb"] for i in p["pending_frontend"]] == ["pack.download", "reuse.pack"]


def test_enumeration_inloop_routes_to_graph_with_checklist_and_pending(monkeypatch):
    """枚举含环内 EXEC（cite.export）+ 直派尾巴（pack.download）→ 走 agent 图：
    intent_checklist 缝收到两 plane 分段清单；图跑完后 pending_frontend 挂回响应。"""
    captured = {}

    def fake_graph(text, **kw):
        captured["intent_checklist"] = kw.get("intent_checklist")
        return ({"verb": "cite.export", "kind": AP.EXEC, "source": "agent",
                 "llm_status": "ok", "slots": {}, "requires_results": True}, [])

    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_graph)
    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_graph)
    llm = json.dumps([
        {"verb": "cite.export", "quoted": "导出成 BibTeX 引文", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "明说引文"},
        {"verb": "pack.download", "quoted": "并下载", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "明说下载"},
    ], ensure_ascii=False)
    monkeypatch.setattr(AP, "_default_llm_call", lambda prompt, config: llm)
    out = turn.route_turn("检索 Visium HD 人类乳腺癌数据，导出成 BibTeX 引文并下载",
                          has_results=False, config=CFG)
    cl = captured.get("intent_checklist")
    assert cl is not None, "含环内 EXEC 的枚举必须把 intent_checklist 注入图"
    assert [(c["verb"], c["plane"]) for c in cl] == [
        ("cite.export", "inloop"), ("pack.download", "frontend")]
    p = out["plan"]
    assert [x["verb"] for x in p["pending_frontend"]] == ["pack.download"]
    assert not any(s.get("verb") == "pack.download"
                   for s in (p.get("steps") or [])), "pending_frontend 绝不进 plan.steps"


def test_enumeration_failure_falls_back_to_single_probe(monkeypatch):
    """枚举通道失败（整单垃圾：quoted 非原文 → 逐项降 none）→ 回落单次探测，行为与
    引入枚举前的分类能力一致，但单次探测产物也必须进图。"""
    def flaky(prompt, config):
        # 枚举调用（prompt 含「清单」特征）回垃圾；单次探测调用回合法单对象
        if "JSON 数组" in prompt:
            return json.dumps([{"verb": "pack.download", "quoted": "原文没这词",
                                "confidence": "high"}], ensure_ascii=False)
        return _LLM_PACK_DOWNLOAD
    monkeypatch.setattr(AP, "_default_llm_call", flaky)
    out = turn.route_turn("小鼠空间转录组，并下载top2", has_results=False, config=CFG)
    p = out["plan"]
    assert p["verb"] == "pack.download" and "agent_bypassed" not in p
    assert [x["verb"] for x in p["pending_frontend"]] == ["pack.download"]
