# -*- coding: utf-8 -*-
"""BioData telemetry HTTP/PostgreSQL load probe.

Runs bounded 10/50/100-way ingest waves and emits machine-readable latency/status evidence.
It never prints the ingest token or payload text. The receiver remains the system under test: this
client uses only stdlib HTTP and therefore can run inside the production receiver container.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_LEVELS = (10, 50, 100)


@dataclass(frozen=True)
class Attempt:
    status: int
    latency_ms: float
    ok: bool
    duplicate: bool = False
    error: str = ""


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, math.ceil((pct / 100) * len(ordered)) - 1))
    return round(ordered[rank], 3)


def make_payload(run_id: str, index: int, *, pad_bytes: int = 0) -> bytes:
    event_id = f"load-u-{run_id}-{index}"
    obj = {
        "schema": "biodata-telemetry/1",
        "contract_version": 2,
        "packet_id": f"load-pkt-{run_id}-{index}",
        "install_id": f"load-{run_id}",
        # 每请求独立 profile，避免 profile 日配额/限流掩盖数据库并发能力；IP 限流仍真实生效。
        "client_id": f"client-load-{run_id}",
        "profile_id": f"profile-load-{run_id}-{index}",
        "exported_at": "2026-08-25T00:00:00Z",
        "prompt_version": None,
        "experiment_id": None,
        "experiment_arm": None,
        "propensity": None,
        "training_consent": False,
        "app": {"cache_generation": "load-probe", "ua": "telemetry_pg_load/1", "lang": "zh-CN"},
        "usage_events": [{
            "event_id": event_id, "t": index + 1, "k": "load_probe", "contract_version": 2,
            "prompt_version": None, "experiment_id": None, "experiment_arm": None, "propensity": None,
            "pad": "x" * max(0, int(pad_bytes)),
        }],
        "benchfb_records": [],
    }
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def post_once(endpoint: str, token: str, payload: bytes, timeout: float) -> Attempt:
    request = Request(endpoint, data=payload, method="POST", headers={
        "Content-Type": "application/json", "X-Ingest-Token": token,
    })
    started = time.perf_counter()
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            body = json.loads(response.read())
        latency = (time.perf_counter() - started) * 1000
        return Attempt(status=status, latency_ms=latency,
                       ok=status == 200 and body.get("ok") is True,
                       duplicate=body.get("duplicate") is True)
    except HTTPError as exc:
        return Attempt(status=int(exc.code), latency_ms=(time.perf_counter() - started) * 1000,
                       ok=False, error=f"http_{exc.code}")
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        return Attempt(status=0, latency_ms=(time.perf_counter() - started) * 1000,
                       ok=False, error=type(exc).__name__)


def run_level(*, concurrency: int, requests: int, sender: Callable[[int], Attempt]) -> dict:
    started = time.perf_counter()
    attempts: list[Attempt] = []
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="telemetry-load") as pool:
        futures = [pool.submit(sender, i) for i in range(requests)]
        for future in as_completed(futures):
            try:
                attempts.append(future.result())
            except Exception as exc:  # probe itself must report worker failures, not lose them
                attempts.append(Attempt(status=0, latency_ms=0, ok=False, error=type(exc).__name__))
    elapsed = max(time.perf_counter() - started, 1e-9)
    latencies = [row.latency_ms for row in attempts]
    statuses = Counter(str(row.status) for row in attempts)
    errors = Counter(row.error for row in attempts if row.error)
    return {
        "concurrency": concurrency,
        "requests": requests,
        "ok": sum(row.ok for row in attempts),
        "failed": sum(not row.ok for row in attempts),
        "duplicates": sum(row.duplicate for row in attempts),
        "elapsed_seconds": round(elapsed, 6),
        "throughput_rps": round(len(attempts) / elapsed, 3),
        "latency_ms": {
            "min": round(min(latencies), 3) if latencies else None,
            "mean": round(statistics.fmean(latencies), 3) if latencies else None,
            "p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95),
            "p99": _percentile(latencies, 99), "max": round(max(latencies), 3) if latencies else None,
        },
        "statuses": dict(sorted(statuses.items())),
        "errors": dict(sorted(errors.items())),
    }


def _levels(text: str) -> tuple[int, ...]:
    levels = tuple(int(piece.strip()) for piece in text.split(",") if piece.strip())
    if not levels or any(level < 1 or level > 500 for level in levels):
        raise argparse.ArgumentTypeError("levels must be comma-separated integers in [1,500]")
    return levels


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run bounded telemetry ingest concurrency waves")
    parser.add_argument("--endpoint", default=os.environ.get("BIODATA_TELEMETRY_ENDPOINT", "http://127.0.0.1:8471/v1/ingest"))
    parser.add_argument("--token", default=os.environ.get("BIODATA_INGEST_TOKEN", ""))
    parser.add_argument("--levels", type=_levels, default=DEFAULT_LEVELS)
    parser.add_argument("--requests-per-level", type=int, default=100)
    parser.add_argument("--pad-bytes", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--allow-non-200", action="store_true", help="report failures but return exit 0")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.token:
        parser.error("--token or BIODATA_INGEST_TOKEN is required")
    if args.requests_per_level < 1 or args.requests_per_level > 10000:
        parser.error("--requests-per-level must be in [1,10000]")
    if args.pad_bytes < 0 or args.pad_bytes > 1_500_000:
        parser.error("--pad-bytes must be in [0,1500000]")

    run_id = uuid.uuid4().hex[:16]
    waves = []
    index_base = 0
    for level in args.levels:
        def sender(index: int, *, base=index_base) -> Attempt:
            payload = make_payload(run_id, base + index, pad_bytes=args.pad_bytes)
            return post_once(args.endpoint, args.token, payload, args.timeout)
        waves.append(run_level(concurrency=level, requests=args.requests_per_level, sender=sender))
        index_base += args.requests_per_level
    report = {
        "schema": "biodata-telemetry-load/1",
        "run_id": run_id,
        "endpoint": args.endpoint,
        "levels": list(args.levels),
        "requests_per_level": args.requests_per_level,
        "pad_bytes": args.pad_bytes,
        "waves": waves,
        "all_ok": all(wave["failed"] == 0 for wave in waves),
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["all_ok"] or args.allow_non_200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
