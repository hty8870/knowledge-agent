# -*- coding: utf-8 -*-
"""trace.recorder：信封/seq 连续性/入口序列化校验/fail-soft/flag/路径消毒/contextvars。"""
from __future__ import annotations

import json
import time

import pytest

from dataset_recommender.agent.trace import (
    TracePayloadError,
    TraceRecorder,
    bind_recorder,
    current_recorder,
    emit_event,
    recorder_for_turn,
    trace_enabled,
    trace_root,
)


def _read_events(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_emit_writes_envelope_with_continuous_seq(tmp_path):
    rec = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    assert rec.emit("route_decision", {"route": "search"}) is True
    assert rec.emit("finish_reason", {"kind": "completed"}) is True
    events = _read_events(rec.path)
    assert [e["seq"] for e in events] == [0, 1]
    assert events[0]["kind"] == "route_decision"
    assert events[0]["payload"] == {"route": "search"}
    assert events[1]["payload"]["kind"] == "completed"
    assert all(e["ts"] for e in events)
    # 落盘位置：database/trace/<session>/<turn>.jsonl
    assert rec.path == trace_root(tmp_path) / "s1" / "t1.jsonl"


def test_reopen_same_turn_continues_seq(tmp_path):
    rec1 = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    rec1.emit("a", {})
    rec1.emit("b", {})
    rec2 = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    rec2.emit("c", {})
    events = _read_events(rec2.path)
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert [e["kind"] for e in events] == ["a", "b", "c"]


def test_append_rejects_unserializable_payload(tmp_path):
    rec = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    with pytest.raises(TracePayloadError):
        rec.append("x", {"bad": float("nan")})          # NaN 是伪合法 JSON，拒
    with pytest.raises(TracePayloadError):
        rec.append("x", {"bad": float("inf")})
    with pytest.raises(TracePayloadError):
        rec.append("x", {"bad": {1, 2, 3}})             # set 不可序列化
    with pytest.raises(TracePayloadError):
        rec.append("x", {"bad": object()})
    with pytest.raises(TracePayloadError):
        rec.append("x", ["not", "a", "dict"])           # payload 必须是 dict
    with pytest.raises(TracePayloadError):
        rec.append("", {})                              # kind 不能为空
    # 拒绝是原子的：一行都没落，seq 没前进
    assert not rec.path.exists() or _read_events(rec.path) == []
    rec.append("ok", {"fine": True})
    assert [e["seq"] for e in _read_events(rec.path)] == [0]


def test_emit_fail_soft_never_raises(tmp_path):
    rec = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    assert rec.emit("x", {"bad": {1, 2}}) is False      # 坏载荷 → 丢弃，不抛
    assert rec.dropped == 1
    assert rec.emit("x", {"good": 1}) is True           # 后续事件照常
    assert [e["seq"] for e in _read_events(rec.path)] == [0]


def test_flag_off_disables_recording(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_TRACE", "off")
    assert trace_enabled() is False
    rec = TraceRecorder(tmp_path, "s1", "t1")           # enabled 缺省读 env
    assert rec.enabled is False
    assert rec.emit("x", {"a": 1}) is False
    assert not rec.path.exists()
    monkeypatch.setenv("AGENT_TRACE", "on")
    assert trace_enabled() is True
    monkeypatch.delenv("AGENT_TRACE")
    assert trace_enabled() is True                      # 缺省 ON


def test_session_id_sanitized_against_traversal(tmp_path):
    rec = TraceRecorder(tmp_path, "../../evil", "t1", enabled=True)
    assert trace_root(tmp_path) in rec.path.parents     # 逃不出 trace 根
    assert ".." not in rec.path.parent.name
    rec.emit("x", {})
    assert rec.path.is_file()


def test_recorder_for_turn_mints_uuid_and_binds(tmp_path):
    rec = recorder_for_turn(tmp_path, "alice")
    assert rec.session_id == "alice"
    assert len(rec.turn_id) == 32                       # uuid4 hex
    assert current_recorder() is None
    with bind_recorder(rec):
        assert current_recorder() is rec
        assert emit_event("route_decision", {"route": "tool"}) is True
    assert current_recorder() is None
    assert emit_event("x", {}) is False                 # 解绑后静默不记
    assert [e["kind"] for e in _read_events(rec.path)] == ["route_decision"]


def test_emit_event_without_recorder_is_silent_noop():
    assert emit_event("x", {"a": 1}) is False           # 无 recorder 在场：一行集成点的静默口


def test_emit_throughput_smoke(tmp_path):
    """开销钉（默认 ON 的论证：千次级 emit 必须秒级内完成）。"""
    rec = TraceRecorder(tmp_path, "s1", "t1", enabled=True)
    t0 = time.monotonic()
    for i in range(500):
        rec.emit("tool_call", {"verb": "curate.db_status", "i": i, "ok": True})
    elapsed = time.monotonic() - t0
    assert elapsed < 5.0
    events = _read_events(rec.path)
    assert [e["seq"] for e in events] == list(range(500))
