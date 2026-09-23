# -*- coding: utf-8 -*-
"""trace 包：agent 执行侧的可追溯性侧车（波1）。

仅追加事件记录（每 session+turn 一份 JSONL）+ 按 turn 导出 + 文件级快照回退。
"""
from __future__ import annotations

from .recorder import (
    TracePayloadError,
    TraceRecorder,
    active_recorder,
    bind_recorder,
    current_recorder,
    emit_event,
    recorder_for_turn,
    trace_enabled,
    trace_root,
)
from .snapshot import SnapshotError, SnapshotStore, snapshot_store

__all__ = [
    "TracePayloadError",
    "TraceRecorder",
    "SnapshotError",
    "SnapshotStore",
    "active_recorder",
    "bind_recorder",
    "current_recorder",
    "emit_event",
    "recorder_for_turn",
    "snapshot_store",
    "trace_enabled",
    "trace_root",
]
