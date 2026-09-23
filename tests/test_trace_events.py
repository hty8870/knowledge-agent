# -*- coding: utf-8 -*-
"""trace.events：六类载荷构造的形状钉（集成点只调这些纯函数，形状漂移在这里拦）。"""
from __future__ import annotations

import hashlib

from dataset_recommender.agent.trace import events as ev


def test_digest_text():
    d = ev.digest_text("你好世界")
    assert d["sha256"] == hashlib.sha256("你好世界".encode("utf-8")).hexdigest()
    assert d["chars"] == 4
    assert d["head"] == "你好世界"
    assert ev.digest_text(None)["chars"] == 0


def test_route_decision_payload_from_result_shape():
    result = {
        "route": "tool", "via": "agent", "needs_agent": False,
        "plan": {"verb": "curate.check_updates", "source": "agent",
                 "llm_status": "ok", "confidence": "high", "agent_fallback": True},
        "retrieval": {"status": "no_match", "total": 0},
    }
    p = ev.route_decision_payload(
        route=result["route"], via=result["via"], plan=result["plan"],
        retrieval=result["retrieval"], needs_agent=result["needs_agent"],
        votes={"verb": "curate.check_updates", "confidence": "high"})
    assert p["route"] == "tool" and p["via"] == "agent"
    assert p["plan_verb"] == "curate.check_updates"
    assert p["plan_source"] == "agent"
    assert p["agent_fallback"] is True                  # 跌过保底如实留痕
    assert p["retrieval_status"] == "no_match"
    assert p["votes"]["confidence"] == "high"
    # 缺省路径（规则直达，无 plan/retrieval/votes）：键在、值空，不 KeyError
    p2 = ev.route_decision_payload(route="search", via="rule_direct")
    assert p2["plan_verb"] == "" and p2["votes"] is None
    assert p2["retrieval_total"] == 0


def test_llm_call_payload():
    p = ev.llm_call_payload(
        node="decide", model="deepseek-chat", prompt="提示词", response="回答",
        ms=1234, channel="required", fallback_reason="",
        usage={"node": "decide", "input": 100, "cache_read": 80, "output": 5})
    assert p["node"] == "decide" and p["model"] == "deepseek-chat"
    assert p["prompt"]["sha256"] == hashlib.sha256("提示词".encode("utf-8")).hexdigest()
    assert p["response"]["chars"] == 2
    assert p["ms"] == 1234 and p["channel"] == "required"
    assert p["usage"]["cache_read"] == 80
    p2 = ev.llm_call_payload(node="narrate", model="", prompt="", response=None,
                             ms=0, channel="json_fallback", fallback_reason="Timeout")
    assert p2["fallback_reason"] == "Timeout" and p2["usage"] is None


def test_tool_call_payload():
    p = ev.tool_call_payload(
        verb="curate.search_online", slots={"keywords": "lung"}, ok=False,
        error_code="network_error", ms=55, card_kind="search_online", readonly=False,
        budgets={"steps": 3, "write_steps": 1, "write_records": 10, "search_rerun": 0})
    assert p["verb"] == "curate.search_online"
    assert p["ok"] is False and p["error_code"] == "network_error"
    assert p["readonly"] is False
    assert p["budgets"]["write_records"] == 10
    p2 = ev.tool_call_payload(verb="curate.db_status", slots=None, ok=True)
    assert p2["slots"] == {} and p2["error_code"] is None and p2["readonly"] is True


def test_batch_emission_payload():
    p = ev.batch_emission_payload(n_calls=4, adopted=2, dropped=1, note="其余回炉",
                                  n_placeholder=1)
    assert p == {"n_calls": 4, "adopted": 2, "dropped": 1, "note": "其余回炉",
                 "n_placeholder": 1}
    p2 = ev.batch_emission_payload(n_calls=2, adopted=1, dropped=0)
    assert p2["n_placeholder"] == 0 and p2["note"] == ""


def test_state_snapshot_payload():
    p = ev.state_snapshot_payload(
        snapshot_id="20260817_120000_abcd1234", verb="curate.search_online",
        created=[{"name": "upload_x.json", "sha256": "a", "size": 10}],
        modified=[], deleted=[], preimage_missing=None)
    assert p["created"][0]["name"] == "upload_x.json"
    assert p["preimage_missing"] == []
    assert p["modified"] == [] and p["deleted"] == []


def test_finish_reason_payload():
    p = ev.finish_reason_payload(kind="truncated", steps=8, repairs=1,
                                 finish_vetoes=2, reask_write_count=1,
                                 truncated=True, truncated_settled=True)
    assert p["kind"] == "truncated" and p["truncated_settled"] is True
    assert p["finish_vetoes"] == 2
    p2 = ev.finish_reason_payload(kind="completed")
    assert p2["steps"] == 0 and p2["declined"] == "" and p2["truncated"] is False


def test_event_kinds_vocabulary():
    assert ev.EVENT_KINDS == frozenset({
        "route_decision", "llm_call", "tool_call",
        "batch_emission", "state_snapshot", "finish_reason"})
