from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("ranking_interleave", ROOT / "scripts" / "ranking_interleave.py")
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_interleave_is_deterministic_and_preserves_source_orders():
    source = {"query_id": "q-7", "control_uids": ["a", "b", "c"], "candidate_uids": ["b", "d", "e"]}
    one = MODULE.interleave_record(source, seed="trial-2026")
    two = MODULE.interleave_record(source, seed="trial-2026")
    assert one == two
    assert one["control_uids"] == source["control_uids"]
    assert one["candidate_uids"] == source["candidate_uids"]
    assert len(one["interleaved_uids"]) == len(set(one["interleaved_uids"]))
    assert set(one["interleaved_uids"]) == {"a", "b", "c", "d", "e"}
    assert set(one["ownership"].values()) <= {"control", "candidate"}


def test_first_drafter_is_fair_across_many_query_ids():
    arms = [MODULE.interleave_record({"query_id": f"q-{i}", "control_uids": ["c"], "candidate_uids": ["d"]}, seed="s")["first_arm"] for i in range(1000)]
    imbalance = abs(arms.count("control") - arms.count("candidate"))
    assert imbalance < 100


@pytest.mark.parametrize("field", ["control_uids", "candidate_uids"])
def test_rejects_repeated_uid_inside_a_ranked_list(field):
    row = {"query_id": "q", "control_uids": ["a"], "candidate_uids": ["b"]}
    row[field] = ["same", "same"]
    with pytest.raises(ValueError, match="unique"):
        MODULE.interleave_record(row, seed="s")


def test_click_credit_deduplicates_and_reports_unknown_clicks():
    assignment = MODULE.interleave_record({"query_id": "q", "control_uids": ["a", "b"], "candidate_uids": ["c", "d"]}, seed="s")
    known_control = next(uid for uid, arm in assignment["ownership"].items() if arm == "control")
    known_candidate = next(uid for uid, arm in assignment["ownership"].items() if arm == "candidate")
    credit = MODULE.credit_record(assignment, [known_control, known_control, "absent", known_candidate, "absent"])
    assert credit["click_credit"] == {"control": 1, "candidate": 1}
    assert credit["unknown_clicked_uids"] == ["absent"]
    assert credit["credited_uids"] == [known_control, known_candidate]


def test_jsonl_run_writes_hashed_manifest_and_refuses_overwrite(tmp_path):
    source = tmp_path / "in.jsonl"
    source.write_text(json.dumps({"query_id": "q", "control_uids": ["a"], "candidate_uids": ["b"]}) + "\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    manifest = MODULE.run_interleave(source, output, seed="s")
    assert manifest["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert manifest["output"]["sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert manifest["output"]["rows"] == 1
    assert json.loads((tmp_path / "out.jsonl.manifest.json").read_text(encoding="utf-8"))["seed"] == "s"
    with pytest.raises(FileExistsError, match="overwrite"):
        MODULE.run_interleave(source, output, seed="s")


def test_bad_json_is_rejected_without_creating_output(tmp_path):
    source = tmp_path / "bad.jsonl"
    source.write_text("{not valid}\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    with pytest.raises(ValueError, match="invalid JSON"):
        MODULE.run_interleave(source, output, seed="s")
    assert not output.exists()
