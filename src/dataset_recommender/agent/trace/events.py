# -*- coding: utf-8 -*-
"""trace 事件词表与载荷构造。

六类受控 kind：route_decision / llm_call / tool_call /
batch_emission / state_snapshot / finish_reason。载荷构造函数全是**纯函数**——
集成点每处一行 `emit_xxx(...)`，构造与落盘（recorder.emit_event）分离可单测。
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable

from .recorder import _warn_once, active_recorder

__all__ = [
    "KIND_ROUTE_DECISION", "KIND_LLM_CALL", "KIND_TOOL_CALL",
    "KIND_BATCH_EMISSION", "KIND_STATE_SNAPSHOT", "KIND_FINISH_REASON",
    "EVENT_KINDS",
    "digest_text",
    "recorder_active",
    "route_decision_payload", "llm_call_payload", "tool_call_payload",
    "batch_emission_payload", "state_snapshot_payload", "finish_reason_payload",
    "emit_route_decision", "emit_llm_call", "emit_tool_call",
    "emit_batch_emission", "emit_state_snapshot", "emit_finish_reason",
    "emit_understand_vote", "emit_route_consensus_votes",
]

KIND_ROUTE_DECISION = "route_decision"
KIND_LLM_CALL = "llm_call"
KIND_TOOL_CALL = "tool_call"
KIND_BATCH_EMISSION = "batch_emission"
KIND_STATE_SNAPSHOT = "state_snapshot"
KIND_FINISH_REASON = "finish_reason"

#: 受控词表（导出器按它校验「未知 kind 如实计数，不拒读」——向前兼容留口）。
EVENT_KINDS: frozenset[str] = frozenset({
    KIND_ROUTE_DECISION, KIND_LLM_CALL, KIND_TOOL_CALL,
    KIND_BATCH_EMISSION, KIND_STATE_SNAPSHOT, KIND_FINISH_REASON,
})


def digest_text(text: Any) -> dict[str, Any]:
    """prompt/response 的留痕口径：sha256 + 字符数 + 首 80 字预览——能回答「这次调用
    是什么」，不落全文（体积与敏感面；全文 opt-in 为预留能力）。"""
    s = str(text or "")
    return {"sha256": hashlib.sha256(s.encode("utf-8")).hexdigest(),
            "chars": len(s), "head": s[:80]}


def route_decision_payload(*, route: str, via: str, plan: dict | None = None,
                           retrieval: dict | None = None, needs_agent: bool = False,
                           votes: dict | None = None, preliminary: str = "",
                           retrieval_note: str = "") -> dict[str, Any]:
    """分流裁决（错误分析第一现场）：route/via + plan 关键键 + 规则概览 +
    understand 原始投票（votes：verb/confidence/reason/slots/mode，图外路径恒 None）。
    （additive）：`preliminary`（"emitted"/"suppressed_action"/
    "skipped_marker"/""——verdict-gated 发射的处置留痕）与 `retrieval_note`
    （tool 路线的 "skipped_action_marker"/"discarded_action_route"/""）。"""
    plan = plan or {}
    retrieval = retrieval or {}
    return {
        "route": str(route or ""),
        "via": str(via or ""),
        "plan_verb": str(plan.get("verb") or ""),
        "plan_source": str(plan.get("source") or ""),
        "llm_status": str(plan.get("llm_status") or ""),
        "confidence": str(plan.get("confidence") or ""),
        "agent_fallback": bool(plan.get("agent_fallback")),
        "needs_agent": bool(needs_agent),
        "retrieval_status": str(retrieval.get("status") or ""),
        "retrieval_total": int(retrieval.get("total") or 0),
        "preliminary": str(preliminary or ""),
        "retrieval_note": str(retrieval_note or ""),
        "votes": votes,
    }


def llm_call_payload(*, node: str, model: str, prompt: Any, response: Any,
                     ms: int, channel: str, fallback_reason: str = "",
                     usage: dict | None = None) -> dict[str, Any]:
    """单次 LLM 调用：节点/模型/双 digest/延迟/通道档位（required/auto/json_fallback）/
    兜底原因/缓存用量（agent_exec._usage_record 同形 dict，缺省 None）。"""
    return {
        "node": str(node or ""),
        "model": str(model or ""),
        "prompt": digest_text(prompt),
        "response": digest_text(response),
        "ms": int(ms or 0),
        "channel": str(channel or ""),
        "fallback_reason": str(fallback_reason or ""),
        "usage": usage,
    }


def tool_call_payload(*, verb: str, slots: dict | None, ok: bool,
                      error_code: str | None = None, ms: int = 0,
                      card_kind: str = "", readonly: bool = True,
                      budgets: dict | None = None) -> dict[str, Any]:
    """环内工具步的机器可读副本（step 实录的持久化超集）：verb/slots/ok/错误码 +
    预算计数现场（steps/write_steps/write_records/search_rerun，集成侧现算）。"""
    return {
        "verb": str(verb or ""),
        "slots": dict(slots or {}),
        "ok": bool(ok),
        "error_code": (str(error_code) if error_code else None),
        "ms": int(ms or 0),
        "card_kind": str(card_kind or ""),
        "readonly": bool(readonly),
        "budgets": dict(budgets or {}),
    }


def batch_emission_payload(*, n_calls: int, adopted: int, dropped: int,
                           note: str = "", n_placeholder: int = 0) -> dict[str, Any]:
    """decide 多调用同批消费（批 additive `n_placeholder` = 采纳的
    **占位接地**续步数——依赖占位批量计划的机械信号，验证/复盘据此统计占位使用率）：
    模型一次给了几个/同批采纳几个/回炉几个。"""
    return {"n_calls": int(n_calls or 0), "adopted": int(adopted or 0),
            "dropped": int(dropped or 0), "note": str(note or ""),
            "n_placeholder": int(n_placeholder or 0)}


def state_snapshot_payload(*, snapshot_id: str, verb: str,
                           created: list, modified: list, deleted: list,
                           preimage_missing: list | None = None) -> dict[str, Any]:
    """mutating 步的 before/after 引用：快照 id + 三清单（各 {name,sha256,size}）+
    无 preimage 不可回退清单（fail-closed 口径，rollback 拒动）。"""
    return {
        "snapshot_id": str(snapshot_id or ""),
        "verb": str(verb or ""),
        "created": list(created or []),
        "modified": list(modified or []),
        "deleted": list(deleted or []),
        "preimage_missing": list(preimage_missing or []),
    }


def finish_reason_payload(*, kind: str, steps: int = 0, repairs: int = 0,
                          finish_vetoes: int = 0, reask_write_count: int = 0,
                          declined: str = "", truncated: bool = False,
                          truncated_settled: bool = False) -> dict[str, Any]:
    """turn 收尾：completed/truncated/truncated_settled/agent_fallback/plan_invalid/
    unavailable + 核销/自修/重问写步计数。"""
    return {
        "kind": str(kind or ""),
        "steps": int(steps or 0),
        "repairs": int(repairs or 0),
        "finish_vetoes": int(finish_vetoes or 0),
        "reask_write_count": int(reask_write_count or 0),
        "declined": str(declined or ""),
        "truncated": bool(truncated),
        "truncated_settled": bool(truncated_settled),
    }


# ---- emit 薄封装（无 recorder 在场时经 emit_event 统一静默）-----------------------------


def recorder_active() -> bool:
    """挂钩点的统一短路：recorder 绑了且 AGENT_TRACE 启用才 True——
    OFF/未绑时调用方连载荷都不构造（零开销、零落盘，行为逐位不变）。"""
    return active_recorder() is not None


def _safe_emit(kind: str, build: Callable[[], dict]) -> bool:
    """emit **全路径** fail-soft（纪律）：载荷构造异常与落盘异常一样
    warn-once + 丢弃——观测设施自身故障绝不掀翻主流程。"""
    rec = active_recorder()
    if rec is None:
        return False
    try:
        payload = build()
    except Exception as exc:
        _warn_once(f"payload::{kind}::{type(exc).__name__}",
                   f"trace 载荷构造失败（{kind}，仅记异常类型），本事件丢弃。")
        return False
    return rec.emit(kind, payload)


def emit_route_decision(result: dict, *, votes: dict | None = None) -> bool:
    """从 route_turn 的返回 dict 直接构造（集成点一行：return 前调它）。
    votes 缺省时并入本 turn 暂存的原始投票（understand / route_consensus）。
    additive 字段取自 result 的 `_preliminary_trace`（内部键，响应层不透传）与
    `retrieval_note`（tool 路线透传键）。"""
    if votes is None:
        rec = active_recorder()
        votes = dict(rec.votes) if (rec is not None and rec.votes) else None
    return _safe_emit(KIND_ROUTE_DECISION, lambda: route_decision_payload(
        route=str(result.get("route") or ""), via=str(result.get("via") or ""),
        plan=result.get("plan"), retrieval=result.get("retrieval"),
        needs_agent=bool(result.get("needs_agent")), votes=votes,
        preliminary=str(result.get("_preliminary_trace") or ""),
        retrieval_note=str(result.get("retrieval_note") or "")))


def emit_llm_call(**kwargs: Any) -> bool:
    return _safe_emit(KIND_LLM_CALL, lambda: llm_call_payload(**kwargs))


def emit_tool_call(**kwargs: Any) -> bool:
    return _safe_emit(KIND_TOOL_CALL, lambda: tool_call_payload(**kwargs))


def emit_batch_emission(**kwargs: Any) -> bool:
    return _safe_emit(KIND_BATCH_EMISSION, lambda: batch_emission_payload(**kwargs))


def emit_state_snapshot(**kwargs: Any) -> bool:
    return _safe_emit(KIND_STATE_SNAPSHOT, lambda: state_snapshot_payload(**kwargs))


def emit_finish_reason(**kwargs: Any) -> bool:
    return _safe_emit(KIND_FINISH_REASON, lambda: finish_reason_payload(**kwargs))


# ---- 原始投票暂存（不是独立事件——并入 route_decision 的 votes 字段）-----------
# 注意：暂存挂在 recorder 对象上（recorder.votes），不是 contextvar——langgraph 节点
# 跑在复制的 context 里，contextvar 写入出不了节点；recorder 对象跨 context 共享。


def emit_understand_vote(raw: Any, mode: str) -> bool:
    """understand 的原始投票（verb/confidence/reason/slots/mode）stash 进本 turn 暂存袋。"""
    rec = active_recorder()
    if rec is None:
        return False
    raw = raw if isinstance(raw, dict) else {}
    rec.votes["understand"] = {
        "verb": str(raw.get("verb") or ""),
        "confidence": str(raw.get("confidence") or ""),
        "reason": str(raw.get("reason") or ""),
        "slots": {k: v for k, v in raw.items()
                  if k not in ("verb", "confidence", "reason")},
        "mode": str(mode or ""),
    }
    return True


def emit_route_consensus_votes(route: str, votes: Any) -> bool:
    """route_consensus 的**全部原始投票**（温度/原文/解析结果逐票留痕——
    分流判错的第一现场，一票不丢）stash 进本 turn 暂存袋。"""
    rec = active_recorder()
    if rec is None:
        return False
    rec.votes["route_consensus"] = {
        "route": str(route or ""),
        "votes": [dict(v) for v in (votes or []) if isinstance(v, dict)],
    }
    return True
