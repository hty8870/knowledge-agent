# -*- coding: utf-8 -*-
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "telemetry_pg_load.py"
SPEC = importlib.util.spec_from_file_location("telemetry_pg_load", SCRIPT)
load = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = load
SPEC.loader.exec_module(load)


def test_payload_is_v2_unique_and_never_contains_token():
    one = json.loads(load.make_payload("run", 1, pad_bytes=10))
    two = json.loads(load.make_payload("run", 2, pad_bytes=10))
    assert one["contract_version"] == 2 and one["training_consent"] is False
    assert one["packet_id"] != two["packet_id"] and one["profile_id"] != two["profile_id"]
    assert one["usage_events"][0]["pad"] == "x" * 10
    assert "token" not in json.dumps(one).lower()


def test_run_level_reports_percentiles_statuses_and_worker_failures():
    def sender(index):
        if index == 4:
            raise RuntimeError("boom")
        return load.Attempt(status=200 if index < 3 else 429, latency_ms=float(index + 1), ok=index < 3)

    report = load.run_level(concurrency=3, requests=5, sender=sender)
    assert report["requests"] == 5 and report["ok"] == 3 and report["failed"] == 2
    assert report["statuses"] == {"0": 1, "200": 3, "429": 1}
    assert report["errors"] == {"RuntimeError": 1}
    assert report["latency_ms"]["p95"] == 4.0
    assert report["throughput_rps"] > 0


def test_default_levels_are_exactly_requested_scale_points():
    assert load.DEFAULT_LEVELS == (10, 50, 100)
    assert load._levels("10,50,100") == (10, 50, 100)


def test_cli_default_runs_enough_requests_to_saturate_highest_wave(monkeypatch):
    captured = []
    monkeypatch.setattr(load, "post_once", lambda *args: load.Attempt(200, 1.0, True))
    monkeypatch.setattr(load, "run_level", lambda **kwargs: captured.append(kwargs) or {"failed": 0})
    assert load.main(["--token", "not-printed", "--levels", "10,100"]) == 0
    assert [row["requests"] for row in captured] == [100, 100]
