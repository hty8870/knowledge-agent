# -*- coding: utf-8 -*-
"""trace.export：按 turn 导出完整性/撕裂尾容忍/损坏行如实列出/CLI。"""
from __future__ import annotations

import json

import pytest

from dataset_recommender.agent.trace.export import export_turn, list_turns, main
from dataset_recommender.agent.trace.recorder import TraceRecorder, trace_root


def _make_turn(root, session, turn, events):
    rec = TraceRecorder(root, session, turn, enabled=True)
    for kind, payload in events:
        rec.emit(kind, payload)
    return rec


def test_export_turn_complete_chain(tmp_path):
    _make_turn(tmp_path, "s1", "t1", [
        ("route_decision", {"route": "tool"}),
        ("llm_call", {"node": "understand", "ms": 100}),
        ("tool_call", {"verb": "curate.db_status", "ok": True}),
        ("finish_reason", {"kind": "completed"}),
    ])
    out = export_turn(tmp_path, "t1", session_id="s1")
    assert out["session_id"] == "s1" and out["turn_id"] == "t1"
    assert out["event_count"] == 4
    assert out["kinds"] == {"route_decision": 1, "llm_call": 1, "tool_call": 1, "finish_reason": 1}
    assert out["seq_gaps"] == [] and out["bad_tail_lines"] == 0 and out["corrupt"] == []
    assert [e["kind"] for e in out["events"]] == [
        "route_decision", "llm_call", "tool_call", "finish_reason"]
    assert [e["seq"] for e in out["events"]] == [0, 1, 2, 3]


def test_export_finds_turn_without_session(tmp_path):
    _make_turn(tmp_path, "alice", "t9", [("finish_reason", {"kind": "completed"})])
    out = export_turn(tmp_path, "t9")
    assert out["session_id"] == "alice"


def test_export_torn_tail_tolerated_and_counted(tmp_path):
    rec = _make_turn(tmp_path, "s1", "t1", [("a", {}), ("b", {})])
    with rec.path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq": 2, "ts": "x", "kind": "c", "payl')   # 崩溃截断的半行
    out = export_turn(tmp_path, "t1", session_id="s1")
    assert out["bad_tail_lines"] == 1
    assert out["event_count"] == 2                            # 已提交前缀完整
    assert out["corrupt"] == []


def test_export_corrupt_middle_line_listed_not_hidden(tmp_path):
    rec = _make_turn(tmp_path, "s1", "t1", [("a", {})])
    with rec.path.open("a", encoding="utf-8") as fh:
        fh.write("not-json-at-all\n")
        fh.write(json.dumps({"seq": 2, "ts": "t", "kind": "c", "payload": {}}) + "\n")
    out = export_turn(tmp_path, "t1", session_id="s1")
    assert out["bad_tail_lines"] == 0
    assert out["corrupt"] and out["corrupt"][0]["line"] == 2  # 中间损坏如实列行号
    assert out["event_count"] == 2


def test_export_seq_gap_detected(tmp_path):
    rec = _make_turn(tmp_path, "s1", "t1", [("a", {})])
    with rec.path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"seq": 7, "ts": "t", "kind": "b", "payload": {}}) + "\n")
    out = export_turn(tmp_path, "t1", session_id="s1")
    assert out["seq_gaps"] == [1]                             # 断档机械检出


def test_export_unknown_kind_counted_not_rejected(tmp_path):
    rec = _make_turn(tmp_path, "s1", "t1", [("future_kind", {"x": 1})])
    out = export_turn(tmp_path, "t1", session_id="s1")
    assert out["unknown_kinds"] == ["future_kind"]            # 向前兼容：不拒读
    assert out["kinds"] == {"future_kind": 1}


def test_export_missing_turn_is_honest_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        export_turn(tmp_path, "nope", session_id="s1")


def test_list_turns_newest_first(tmp_path):
    _make_turn(tmp_path, "s1", "t1", [("a", {})])
    _make_turn(tmp_path, "s1", "t2", [("a", {}), ("b", {})])
    _make_turn(tmp_path, "s2", "t3", [("a", {})])
    rows = list_turns(tmp_path)
    assert {(r["session_id"], r["turn_id"]) for r in rows} == {
        ("s1", "t1"), ("s1", "t2"), ("s2", "t3")}
    by_id = {r["turn_id"]: r for r in rows}
    assert by_id["t2"]["event_count"] == 2
    rows_s1 = list_turns(tmp_path, session_id="s1")
    assert {r["turn_id"] for r in rows_s1} == {"t1", "t2"}


def test_cli_export_to_file_and_missing_turn(tmp_path, capsys):
    _make_turn(tmp_path, "s1", "t1", [("finish_reason", {"kind": "completed"})])
    out_file = tmp_path / "out.json"
    rc = main(["--root", str(tmp_path), "--session", "s1", "--turn", "t1",
               "--out", str(out_file)])
    assert rc == 0
    payload = json.loads(out_file.read_text(encoding="utf-8"))
    assert payload["turn_id"] == "t1" and payload["event_count"] == 1
    rc = main(["--root", str(tmp_path), "--session", "s1", "--turn", "ghost"])
    assert rc == 2                                            # 缺 turn：非零退出 + stderr
    assert "导出失败" in capsys.readouterr().err


def test_cli_list(tmp_path, capsys):
    _make_turn(tmp_path, "s1", "t1", [("a", {})])
    rc = main(["--root", str(tmp_path), "--list"])
    assert rc == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["turn_id"] == "t1"
