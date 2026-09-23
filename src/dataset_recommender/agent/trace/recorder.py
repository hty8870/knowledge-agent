# -*- coding: utf-8 -*-
"""agent 执行侧的**仅追加事件记录器**（可追溯性波1）。

借鉴 DSH 强追踪性的最小子集：事件信封 {seq, ts, kind, payload}、seq 从头连续、
**追加处校验可 JSON 序列化**（坏事件在入口失败，不在消费时失败）、每 session+turn
一份 JSONL。不搬事件溯源/投影/崩溃合成 closers——我们要的是错误可定位 + 状态可回退，
不是字节级无损重放。

纪律（与 agent_exec 观测设施同款）：
- trace 自身故障**绝不掀翻主流程**——`emit` 全路径 fail-soft（stderr warn-once，
  事件丢弃、请求零感知）；需要硬校验的调用方用 `append`（坏载荷直接 TracePayloadError）。
- 落盘恒在 `database/trace/`（.gitignore 已追加）；`database/base/` 结构性不可达。
- 集成零签名变更：recorder 经 contextvars 传递（`bind_recorder` / `current_recorder`），
  既有函数的参数表/返回值/异常契约不动。
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterator

from ...app.runtime_paths import instance_data_dir_for

__all__ = [
    "TRACE_DIR_NAME",
    "TracePayloadError",
    "TraceRecorder",
    "active_recorder",
    "bind_recorder",
    "current_recorder",
    "default_project_root",
    "emit_event",
    "recorder_for_turn",
    "trace_enabled",
    "trace_root",
]

#: trace 运行时产物根（相对项目根的 database/ 之下；.gitignore 追加 database/trace/）。
TRACE_DIR_NAME = "trace"

#: AGENT_TRACE 的关闭取值（缺省/其余一律视为 ON）。
_OFF_VALUES = frozenset({"off", "0", "false", "no"})

#: session/turn id 进文件路径前的消毒（DSH 迁移坑 #1：id 是未校验字符串，进路径必须编码）。
#: 我们的 id 来源受控（账户 principal / uuid），白名单替换即可，不需要可逆编码。
_SEGMENT_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")

#: 每路径一把写锁（webapp 的 SSE worker 是多线程；同一路径的追加不交错）。
_PATH_LOCKS: dict[str, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()

#: 观测设施自身故障的 warn-once 集合（与 agent_exec._warn_once 同纪律）。
_WARNED: set[str] = set()


def _warn_once(key: str, message: str) -> None:
    """同一原因只在 stderr 提示一次；绝不抛异常、绝不打断请求。"""
    if key not in _WARNED:
        _WARNED.add(key)
        print(f"[agent.trace] {message}", file=sys.stderr)


def trace_enabled() -> bool:
    """AGENT_TRACE flag 判读（**读取于 recorder 构造时**，测试可 monkeypatch.setenv）。"""
    return os.environ.get("AGENT_TRACE", "on").strip().lower() not in _OFF_VALUES


def default_project_root() -> Path:
    """项目根的默认真源：复用 corpus_status（与 agent_exec._agent_project_root 同径）。"""
    from ...corpus.corpus_status import _default_project_root

    return _default_project_root()


def trace_root(project_root: Path) -> Path:
    """trace 运行时产物根（经 runtime_paths 单一真源：frozen 布局实例根 =
    data_root/database/trace；source/portable 与测试注入根 = project_root/database/trace，
    历史逐字节一致）。落盘恒在 trace 层，`database/base/` 结构性不可达。"""
    return instance_data_dir_for(Path(project_root), f"database/{TRACE_DIR_NAME}")


def _safe_segment(value: Any) -> str:
    """路径单段消毒：白名单字符原样保留，其余逐字替换为 _；`..` 序列折叠为 __
    （`.` 本身允许——turn id 可能是可读名，但父目录语义必须死掉）；空/纯点后兜底 "x"。
    不接受任何分隔符——../ 与绝对路径在替换后自然失效。"""
    seg = _SEGMENT_UNSAFE.sub("_", str(value or ""))
    while ".." in seg:
        seg = seg.replace("..", "__")
    seg = seg.strip(".")
    return seg or "x"


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


class TracePayloadError(ValueError):
    """事件载荷不是**严格**可 JSON 序列化的 dict（append 入口校验的拒绝信号）。"""


def _checked_line(seq: int, ts: str, kind: str, payload: Any) -> str:
    """信封拼装 + 入口校验（单趟）：payload 必须是 dict 且严格可序列化
    （allow_nan=False——NaN/Infinity 在 JSON 里是伪合法，落盘即毒）。"""
    if not isinstance(payload, dict):
        raise TracePayloadError(f"payload 必须是 dict，拿到 {type(payload).__name__}")
    if not kind or not str(kind).strip():
        raise TracePayloadError("kind 不能为空")
    try:
        return json.dumps(
            {"seq": int(seq), "ts": ts, "kind": str(kind), "payload": payload},
            ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TracePayloadError(f"payload 不可 JSON 序列化：{exc}") from exc


class TraceRecorder:
    """单 turn 的仅追加记录器。`emit` fail-soft；`append` 严格（测试/CLI 用）。

    seq 连续性：构造时若文件已存在则**从已有行数接续**（同一 turn 重开进程续写不重新
    编号——续写只发生在同进程崩溃后的手工场景，行数即真相）。"""

    def __init__(self, project_root: Path, session_id: str, turn_id: str,
                 *, enabled: bool | None = None) -> None:
        self.project_root = Path(project_root)
        self.session_id = str(session_id or "anonymous")
        self.turn_id = str(turn_id or "")
        self.enabled = trace_enabled() if enabled is None else bool(enabled)
        self.path = (trace_root(self.project_root)
                     / _safe_segment(self.session_id) / f"{_safe_segment(self.turn_id)}.jsonl")
        self._lock = _lock_for(self.path)
        self._dropped = 0
        self._next_seq = 0
        #: turn 级暂存（波2）：understand/route_consensus 的原始投票，
        #: route_turn 收尾发 route_decision 时并入 votes 字段。**挂在 recorder 对象上
        #: 而非 contextvar**——langgraph 节点跑在复制的 context 里，contextvar 的写入
        #: 出不了节点；recorder 对象本身跨 context 共享（每 turn 一个实例，无线程竞争：
        #: 投票 stash 恒发生在节点主线程）。
        self.votes: dict[str, Any] = {}
        if self.enabled and self.path.exists():
            try:
                with self.path.open(encoding="utf-8") as fh:
                    self._next_seq = sum(1 for line in fh if line.strip())
            except OSError:
                self._next_seq = 0

    @property
    def dropped(self) -> int:
        """被 fail-soft 丢弃的事件数（观测 trace 自身健康）。"""
        return self._dropped

    def append(self, kind: str, payload: dict) -> dict:
        """严格追加：坏载荷/写盘失败直接抛（TracePayloadError / OSError）。"""
        with self._lock:
            line = _checked_line(self._next_seq, _now_iso(), kind, payload)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
            self._next_seq += 1
        return {"seq": self._next_seq - 1, "kind": kind}

    def emit(self, kind: str, payload: Any) -> bool:
        """主流程调用口：flag 关/无 turn/坏载荷/写盘失败都**如实丢弃**（warn-once 留痕），
        返回是否真落盘。绝不抛异常——trace 是观测设施，不是执行的前提。"""
        if not self.enabled:
            return False
        try:
            self.append(kind, payload)
            return True
        except Exception as exc:
            self._dropped += 1
            _warn_once(f"emit::{type(exc).__name__}",
                       f"trace 事件落盘失败（{type(exc).__name__}），本 turn 可能缺事件。")
            return False


def _now_iso() -> str:
    """时间戳真源：复用 corpus_curation（与联网台账同一口径，两账可按 ts 互查）。"""
    from ...corpus.corpus_curation import _now_iso as _cc_now_iso

    return _cc_now_iso()


# ---- contextvars 传递（集成零签名变更：webapp 的 SSE worker 线程内天然隔离）-------------

_CURRENT: contextvars.ContextVar["TraceRecorder | None"] = contextvars.ContextVar(
    "biodata_agent_trace_recorder", default=None)


def current_recorder() -> "TraceRecorder | None":
    return _CURRENT.get()


def active_recorder() -> "TraceRecorder | None":
    """绑了**且启用**才返回 recorder——挂钩点的统一短路（OFF/未绑 = 零构造零落盘）。"""
    rec = _CURRENT.get()
    return rec if (rec is not None and rec.enabled) else None


@contextlib.contextmanager
def bind_recorder(recorder: "TraceRecorder | None") -> "Iterator[TraceRecorder | None]":
    """把 recorder 绑进当前 context（with 块内 current_recorder() 可得）。"""
    token = _CURRENT.set(recorder)
    try:
        yield recorder
    finally:
        _CURRENT.reset(token)


def emit_event(kind: str, payload: Any) -> bool:
    """无 recorder 在场时的统一静默口：集成点写成一行，不判 None。"""
    rec = current_recorder()
    return rec.emit(kind, payload) if rec is not None else False


def recorder_for_turn(project_root: Path, session_id: str,
                      turn_id: str | None = None) -> TraceRecorder:
    """turn 入口的构造口：turn_id 缺省铸 uuid4 hex（调用方回显给前端/用户报障用）。"""
    return TraceRecorder(project_root, session_id,
                         turn_id or uuid.uuid4().hex)
