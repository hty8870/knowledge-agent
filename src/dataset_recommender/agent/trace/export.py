# -*- coding: utf-8 -*-
"""trace 导出器：按 turn 导出单 JSON（完整事件链）+ CLI（可追溯性波1）。

读取纪律（借鉴 DSH 撕裂尾容忍）：无法解析的行**不炸**——末尾坏行按
撕裂尾计数（崩溃截断现场），中间坏行按损坏如实列出；seq 连续性机械校验，断档如实报。
绝不假装读到了一份完整日志。

CLI：
  PYTHONPATH=src python -m dataset_recommender.agent.trace.export --turn <turn_id> [--session <id>] [--root .] [--out out.json]
  PYTHONPATH=src python -m dataset_recommender.agent.trace.export --list [--session <id>]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .events import EVENT_KINDS
from .recorder import default_project_root, trace_root

__all__ = ["read_turn_file", "export_turn", "list_turns", "main"]


def read_turn_file(path: Path) -> dict[str, Any]:
    """读一份 turn JSONL：返回 {events, bad_tail_lines, corrupt}。
    corrupt 元素 {line, error}（行号 1 起）；bad_tail_lines 是末尾无法解析的行数。"""
    events: list[dict] = []
    corrupt: list[dict] = []
    bad_tail = 0
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError as exc:
            # 只有**最后一行**坏才算撕裂尾（崩溃时写了一半）；中间坏行是损坏，分开记。
            if i == len(lines) - 1:
                bad_tail += 1
            else:
                corrupt.append({"line": i + 1, "error": str(exc)[:120]})
    return {"events": events, "bad_tail_lines": bad_tail, "corrupt": corrupt}


def _seq_gaps(events: list[dict]) -> list[int]:
    """seq 断档清单（空 = 从头连续；DSH 连续契约的读取侧校验）。"""
    gaps: list[int] = []
    for expect, ev in enumerate(events):
        seq = ev.get("seq")
        if isinstance(seq, int) and seq != expect:
            gaps.append(expect)
    return gaps


def _find_turn_file(root: Path, turn_id: str, session_id: str | None) -> Path:
    base = trace_root(root)
    if session_id:
        path = base / session_id / f"{turn_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"turn 日志不存在：{path}")
        return path
    matches = sorted(base.glob(f"*/{turn_id}.jsonl")) if base.is_dir() else []
    if not matches:
        raise FileNotFoundError(f"找不到 turn {turn_id} 的日志（{base} 下无匹配）")
    if len(matches) > 1:
        raise FileNotFoundError(
            f"turn {turn_id} 命中 {len(matches)} 份日志，请用 --session 限定："
            + "、".join(str(m.parent.name) for m in matches))
    return matches[0]


def export_turn(project_root: Path, turn_id: str, *,
                session_id: str | None = None) -> dict[str, Any]:
    """按 turn 导出单 JSON：header + 全事件（顺序=落盘序）+ 按 kind 计数 + 健康指标。"""
    path = _find_turn_file(Path(project_root), turn_id, session_id)
    parsed = read_turn_file(path)
    events = parsed["events"]
    kinds: dict[str, int] = {}
    unknown_kinds: list[str] = []
    for ev in events:
        kind = str(ev.get("kind") or "")
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind not in EVENT_KINDS and kind not in unknown_kinds:
            unknown_kinds.append(kind)
    return {
        "session_id": path.parent.name,
        "turn_id": path.stem,
        "source_file": str(path),
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event_count": len(events),
        "kinds": kinds,
        "unknown_kinds": unknown_kinds,   # 词表外 kind 如实列出（向前兼容留口，不拒读）
        "seq_gaps": _seq_gaps(events),
        "bad_tail_lines": parsed["bad_tail_lines"],
        "corrupt": parsed["corrupt"],
        "events": events,
    }


def list_turns(project_root: Path, *, session_id: str | None = None) -> list[dict[str, Any]]:
    """turn 清单（新→旧）：session/turn/事件数/首末事件时间/健康标记。"""
    base = trace_root(Path(project_root))
    if session_id:
        files = sorted((base / session_id).glob("*.jsonl")) if (base / session_id).is_dir() else []
    else:
        files = sorted(base.glob("*/*.jsonl")) if base.is_dir() else []
    out: list[dict[str, Any]] = []
    for path in files:
        parsed = read_turn_file(path)
        events = parsed["events"]
        out.append({
            "session_id": path.parent.name,
            "turn_id": path.stem,
            "event_count": len(events),
            "first_ts": events[0].get("ts") if events else None,
            "last_ts": events[-1].get("ts") if events else None,
            "seq_gaps": len(_seq_gaps(events)),
            "bad_tail_lines": parsed["bad_tail_lines"],
            "corrupt": len(parsed["corrupt"]),
        })
    out.sort(key=lambda r: str(r.get("first_ts") or ""), reverse=True)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dataset_recommender.agent.trace.export",
        description="按 turn 导出 trace 事件链为单 JSON（错误定位用）。")
    parser.add_argument("--root", default=None, help="项目根（缺省自动解析）")
    parser.add_argument("--session", default=None, help="session id（缺省全局搜 turn）")
    parser.add_argument("--turn", default=None, help="要导出的 turn id")
    parser.add_argument("--out", default=None, help="输出文件（缺省打印到 stdout）")
    parser.add_argument("--list", action="store_true", help="列出全部 turn（新→旧）")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else default_project_root()
    try:
        if args.list:
            payload: Any = list_turns(root, session_id=args.session)
        else:
            if not args.turn:
                parser.error("--turn 必填（或用 --list 列出全部）")
            payload = export_turn(root, args.turn, session_id=args.session)
    except (FileNotFoundError, ValueError) as exc:
        print(f"导出失败：{exc}", file=sys.stderr)
        return 2
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"已导出：{args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
