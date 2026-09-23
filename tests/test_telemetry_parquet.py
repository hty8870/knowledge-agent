# -*- coding: utf-8 -*-
"""Contract tests for the optional, lossless telemetry Parquet materializer."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location("telemetry_parquet", ROOT / "scripts/telemetry_parquet.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


materialize = _load_module()


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")


def test_roundtrip_is_lossless_streamed_hashed_and_uses_fixed_schema(tmp_path):
    one = tmp_path / "one.jsonl"
    two = tmp_path / "two.jsonl"
    rows_one = [
        {"record_type": "benchmark_candidate", "install_id": "i1", "tid": "t1", "ts": 1710000000,
         "policy": "bpol1:snap=s", "training_consent": True, "propensity": "0.25",
         "nested": {"z": [2, 1], "a": "保留"}},
        {"record_type": "interaction", "install_id": "i2", "event_id": "e2", "contract_version": 2,
         "experiment_arm": "control", "unknown_new_field": ["kept"]},
    ]
    rows_two = [{"record_type": "mcp", "call_id": "c1", "route": "action", "prompt_version": "pv1"}]
    _write(one, rows_one)
    _write(two, rows_two)

    out = materialize.convert([one, two], tmp_path / "parquet-out", batch_size=1)
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema"] == materialize.MANIFEST_SCHEMA
    assert manifest["batch_size"] == 1
    assert len(manifest["files"]) == 2
    assert {field["name"] for field in manifest["parquet_schema"]} >= {"record_type", "row_json", "tid", "policy"}
    first = pq.read_table(out / "one.parquet").to_pylist()
    assert len(first) == 2
    assert first[0]["propensity"] == pytest.approx(0.25)
    assert json.loads(first[0]["row_json"]) == rows_one[0]
    assert json.loads(first[1]["row_json"]) == rows_one[1]
    assert materialize._fixed_schema() == pq.read_schema(out / "one.parquet")
    for entry in manifest["files"]:
        source = Path(entry["input"]["path"])
        product = out / entry["output"]["path"]
        assert entry["input"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
        assert entry["output"]["sha256"] == hashlib.sha256(product.read_bytes()).hexdigest()


def test_refuses_existing_base_and_input_sibling_outputs_and_cleans_up_broken_json(tmp_path, capsys):
    source = tmp_path / "source.jsonl"
    source.write_text('{"api_token":"do-not-print-this","record_type":"ok"}\n{bad json}\n', encoding="utf-8")
    assert materialize.main(["--input", str(source), "--out", str(tmp_path / "broken-out")]) == 2
    assert not (tmp_path / "broken-out").exists()
    assert "do-not-print-this" not in capsys.readouterr().err

    valid = tmp_path / "valid.jsonl"
    _write(valid, [{"record_type": "ok"}])
    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(materialize.ParquetError, match="已存在"):
        materialize.convert([valid], existing)
    with pytest.raises(materialize.ParquetError, match="database/base"):
        materialize.convert([valid], tmp_path / "database" / "base" / "forbidden")
    # A fresh sibling is safe and is published atomically only after conversion.
    sibling = materialize.convert([valid], tmp_path / "safe-output")
    assert (sibling / "valid.parquet").is_file()
    assert not list(tmp_path.glob(".safe-output.tmp-*"))


def test_missing_pyarrow_fails_closed_with_install_guidance(monkeypatch, tmp_path):
    source = tmp_path / "source.jsonl"
    _write(source, [{"record_type": "ok"}])
    monkeypatch.setattr(materialize, "pa", None)
    monkeypatch.setattr(materialize, "pq", None)
    with pytest.raises(materialize.ParquetError, match="requirements-analytics.lock"):
        materialize.convert([source], tmp_path.parent / "separate-output")


def test_analytics_dependency_is_hash_locked_for_windows_and_linux():
    lock = (ROOT / "requirements" / "requirements-analytics.lock").read_text(encoding="utf-8")
    assert "pyarrow==25.0.1" in lock
    assert "--require-hashes" in lock
    hashes = [line for line in lock.splitlines() if "--hash=sha256:" in line]
    assert len(hashes) == 2
    assert any("5389cdf79447ed1515c9e31620e6e1e2302249564d603f2ad727d4f6d313e4c3" in line for line in hashes)
    assert any("8858d7bfc22e3f51529aeaa4077225029724623e4595dc9eff8c793935c34140" in line for line in hashes)
