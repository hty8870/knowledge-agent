# -*- coding: utf-8 -*-
"""冻结 telemetry benchmark 的纯离线、无泄漏和可复现契约测试。"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("build_telemetry_benchmark", ROOT / "scripts/build_telemetry_benchmark.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


freeze = _load_module()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _candidate(record_id: str, *, install: str, query: str, day: int, topk: list[str] | None = None,
               consent: bool = True, rating: dict | None = None, **extra) -> dict:
    row = {
        "record_id": record_id,
        "packet_id": "pkt-" + record_id,
        "install_id": install,
        "ts": f"2026-01-{day:02d}T12:00:00Z",
        "query": query,
        "system_topk_uids": topk or ["d1", "d2"],
        "rating": rating if rating is not None else {"completion": "done", "useful_uids": ["d1"]},
        "training_consent": consent,
        "policy": "bpol1:snap=" + record_id,
        "experiment_arm": "control",
        "propensity": None,
        "prompt_version": "pv1",
        "route": "search",
    }
    row.update(extra)
    return row


def _all_split_rows(directory: Path) -> dict[str, list[dict]]:
    return {split: _rows(directory / f"{split}.jsonl") for split in freeze.SPLITS}


def test_training_freeze_dedupes_requires_consent_and_keeps_leakage_groups_together(tmp_path):
    source = tmp_path / "candidates.jsonl"
    rows = [
        # Same install links different days; token-order-equivalent query links a different install.
        _candidate("a1", install="install-a", query="lung cancer", day=1),
        _candidate("a2", install="install-a", query="heart atlas", day=2),
        _candidate("b1", install="install-b", query="cancer lung", day=3),
        # Same UTC day links otherwise independent records.
        _candidate("c1", install="install-c", query="kidney atlas", day=4),
        _candidate("d1", install="install-d", query="mouse brain", day=4),
        _candidate("e1", install="install-e", query="spatial transcriptomics", day=5),
        _candidate("f1", install="install-f", query="immune cells", day=6),
        _candidate("g1", install="install-g", query="organoid", day=7),
        _candidate("h1", install="install-h", query="metabolism", day=8),
        _candidate("i1", install="install-i", query="proteomics", day=9),
        # Same normalized query + top-k is a duplicate even if it came from another user.
        _candidate("dup", install="install-dup", query="  SPATIAL   TRANSCRIPTOMICS ", day=10),
        _candidate("no-consent", install="install-x", query="excluded consent", day=11, consent=False),
        _candidate("no-label", install="install-y", query="excluded label", day=12, rating={}),
    ]
    _write_jsonl(source, rows)
    target = freeze.build_benchmark(source, tmp_path / "frozen", run_id="training-a", purpose="training",
                                    created_at="2026-02-01T00:00:00Z", time_bucket_days=1,
                                    ratios=(0.6, 0.2, 0.2))
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    split_rows = _all_split_rows(target)
    flattened = [row for values in split_rows.values() for row in values]
    assert len(flattened) == 10
    assert manifest["exclusions"] == {
        "duplicate_query_topk": 1, "no_human_label": 1, "no_training_consent": 1,
    }
    assert all(row["training_consent"] is True for row in flattened)
    good = next(row for row in flattened if row["record_id"] == "a1")
    assert good["graded_relevance"] == [{"uid": "d1", "grade": 2}, {"uid": "d2", "grade": 0}]
    assert all(key in good for key in ("policy", "experiment_arm", "propensity", "prompt_version", "route"))

    # A user / normalized token cluster / UTC bucket must occur in at most one split.
    for field in ("install_id", "semantic_cluster", "time_bucket_utc"):
        seen: dict[str, str] = {}
        for split, values in split_rows.items():
            for row in values:
                assert seen.setdefault(str(row[field]), split) == split
    by_record = {row["record_id"]: split for split, values in split_rows.items() for row in values}
    assert by_record["a1"] == by_record["a2"] == by_record["b1"]
    assert by_record["c1"] == by_record["d1"]
    assert set(manifest["files"]) == {"train.jsonl", "validation.jsonl", "test.jsonl"}
    for name, metadata in manifest["files"].items():
        assert metadata["sha256"] == hashlib.sha256((target / name).read_bytes()).hexdigest()


def test_freeze_is_deterministic_and_refuses_existing_target(tmp_path):
    source = tmp_path / "candidates.jsonl"
    _write_jsonl(source, [_candidate(f"r{i}", install=f"i{i}", query=f"query {i}", day=i + 1) for i in range(12)])
    args = dict(purpose="training", created_at="2026-02-01T00:00:00Z", time_bucket_days=1, ratios=(0.8, 0.1, 0.1))
    first = freeze.build_benchmark(source, tmp_path / "one", run_id="same-content", **args)
    second = freeze.build_benchmark(source, tmp_path / "two", run_id="same-content", **args)
    for split in freeze.SPLITS:
        assert (first / f"{split}.jsonl").read_bytes() == (second / f"{split}.jsonl").read_bytes()
    assert freeze.main(["--input", str(source), "--out-root", str(tmp_path / "one"), "--run-id", "same-content",
                        "--created-at", "2026-02-01T00:00:00Z"]) == 2


def test_evaluation_can_use_non_training_consent_but_training_cannot(tmp_path):
    source = tmp_path / "candidates.jsonl"
    _write_jsonl(source, [_candidate("eval-only", install="i", query="q", day=1, consent=False)])
    train = freeze.build_benchmark(source, tmp_path / "out", run_id="train", purpose="training",
                                   created_at="2026-02-01T00:00:00Z", time_bucket_days=1, ratios=(1, 0, 0))
    evaluation = freeze.build_benchmark(source, tmp_path / "out", run_id="evaluation", purpose="evaluation",
                                        created_at="2026-02-01T00:00:00Z", time_bucket_days=1, ratios=(1, 0, 0))
    assert sum(len(values) for values in _all_split_rows(train).values()) == 0
    eval_rows = [row for values in _all_split_rows(evaluation).values() for row in values]
    assert [row["record_id"] for row in eval_rows] == ["eval-only"]
    manifest = json.loads((evaluation / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["authorization"]["evaluation_may_include_without_training_consent"] is True
    assert eval_rows[0]["training_consent"] is False


def test_explicit_grades_bad_json_base_rejection_and_small_data_are_fail_closed(tmp_path):
    source = tmp_path / "candidates.jsonl"
    _write_jsonl(source, [_candidate("graded", install="i", query="q", day=1,
                                     relevance_grades={"d1": 5, "d2": 1})])
    target = freeze.build_benchmark(source, tmp_path / "small", run_id="one", purpose="training",
                                    created_at="2026-02-01T00:00:00Z", time_bucket_days=1, ratios=(0.8, 0.1, 0.1))
    only = [row for values in _all_split_rows(target).values() for row in values]
    assert only[0]["graded_relevance"] == [{"uid": "d1", "grade": 5}, {"uid": "d2", "grade": 1}]

    malformed = tmp_path / "broken.jsonl"
    malformed.write_text('{"ok":true}\n{not json}\n', encoding="utf-8")
    assert freeze.main(["--input", str(malformed), "--out-root", str(tmp_path / "safe"), "--run-id", "bad"]) == 2
    assert not (tmp_path / "safe" / "bad").exists()
    assert freeze.main(["--input", str(source), "--out-root", str(tmp_path / "base"), "--run-id", "blocked"]) == 2
    assert not (tmp_path / "base").exists()
