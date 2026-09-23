#!/usr/bin/env python3
"""Deterministic, offline team-draft interleaving for ranking experiments.

The tool deliberately has no network or database dependency.  It turns a
JSONL file containing paired ranked lists into an immutable JSONL assignment
plus a small manifest, so an experiment can be reproduced from its input hash.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


ALGORITHM = "team-draft-interleaving"
VERSION = "ranking-interleave/v1"
MAX_LIST_LENGTH = 200
MAX_INTERLEAVED_LENGTH = 400


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_ranked_uids(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(uid, str) and uid for uid in value):
        raise ValueError(f"{field} must be a list of non-empty UID strings")
    if len(value) > MAX_LIST_LENGTH:
        raise ValueError(f"{field} exceeds MAX_LIST_LENGTH={MAX_LIST_LENGTH}")
    if len(set(value)) != len(value):
        raise ValueError(f"{field} must contain unique UIDs")
    return list(value)


def _first_arm(query_id: str, seed: str) -> str:
    """Choose the first drafter reproducibly and close to 50/50 over queries."""
    bit = hashlib.sha256((seed + "\x00" + query_id).encode("utf-8")).digest()[0] & 1
    return "control" if bit == 0 else "candidate"


def interleave_record(record: dict[str, Any], *, seed: str) -> dict[str, Any]:
    """Return one validated, deterministic team-draft assignment."""
    if not isinstance(record, dict):
        raise ValueError("each JSONL value must be an object")
    query_id = record.get("query_id")
    if not isinstance(query_id, str) or not query_id:
        raise ValueError("query_id must be a non-empty string")
    control = _require_ranked_uids(record.get("control_uids"), "control_uids")
    candidate = _require_ranked_uids(record.get("candidate_uids"), "candidate_uids")

    ranked = {"control": control, "candidate": candidate}
    indices = {"control": 0, "candidate": 0}
    ownership: dict[str, str] = {}
    interleaved: list[str] = []
    first = _first_arm(query_id, seed)
    other = "candidate" if first == "control" else "control"
    turn = first

    while len(interleaved) < MAX_INTERLEAVED_LENGTH:
        # Pick this arm's next not-yet-present document.  If it is exhausted,
        # the other arm gets the next chance; stop only when both are empty.
        uid: str | None = None
        while indices[turn] < len(ranked[turn]):
            possible = ranked[turn][indices[turn]]
            indices[turn] += 1
            if possible not in ownership:
                uid = possible
                break
        if uid is not None:
            interleaved.append(uid)
            ownership[uid] = turn
            turn = other if turn == first else first
            continue

        alternate = other if turn == first else first
        if indices[alternate] >= len(ranked[alternate]):
            break
        turn = alternate

    return {
        "query_id": query_id,
        "control_uids": control,
        "candidate_uids": candidate,
        "interleaved_uids": interleaved,
        "ownership": ownership,
        "algorithm": ALGORITHM,
        "version": VERSION,
        "seed": seed,
        "first_arm": first,
    }


def credit_record(record: dict[str, Any], clicked_uids: Iterable[str] | None = None) -> dict[str, Any]:
    """Attribute each distinct known click exactly once to its drafting arm."""
    ownership = record.get("ownership")
    if not isinstance(ownership, dict) or not all(arm in {"control", "candidate"} for arm in ownership.values()):
        raise ValueError("ownership must map UIDs to control/candidate")
    clicks = record.get("clicked_uids") if clicked_uids is None else list(clicked_uids)
    if not isinstance(clicks, list) or not all(isinstance(uid, str) for uid in clicks):
        raise ValueError("clicked_uids must be a list of UID strings")
    seen: set[str] = set()
    credited: list[str] = []
    unknown: list[str] = []
    scores = {"control": 0, "candidate": 0}
    for uid in clicks:
        if uid in seen:
            continue
        seen.add(uid)
        arm = ownership.get(uid)
        if arm is None:
            unknown.append(uid)
            continue
        scores[arm] += 1
        credited.append(uid)
    return {
        "query_id": record.get("query_id"),
        "algorithm": record.get("algorithm", ALGORITHM),
        "version": record.get("version", VERSION),
        "click_credit": scores,
        "credited_uids": credited,
        "unknown_clicked_uids": unknown,
    }


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_no, raw in enumerate(handle, 1):
            text = raw.strip()
            if not text:
                continue
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSON: {exc.msg}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_no}: each JSONL value must be an object")
            yield value


def _ensure_new(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing output: {path}")
    if not path.parent.exists() or not path.parent.is_dir():
        raise ValueError(f"output directory does not exist: {path.parent}")


def _atomic_write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    _ensure_new(path)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    count = 0
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(_canonical_json(row) + "\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {path}")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return count


def run_interleave(input_path: Path, output_path: Path, *, seed: str, manifest_path: Path | None = None) -> dict[str, Any]:
    if not input_path.is_file():
        raise ValueError(f"input JSONL does not exist: {input_path}")
    if not isinstance(seed, str) or not seed:
        raise ValueError("seed must be a non-empty string")
    manifest_path = manifest_path or output_path.with_name(output_path.name + ".manifest.json")
    _ensure_new(output_path)
    _ensure_new(manifest_path)
    rows = (interleave_record(row, seed=seed) for row in _read_jsonl(input_path))
    count = _atomic_write_jsonl(output_path, rows)
    manifest = {
        "algorithm": ALGORITHM,
        "version": VERSION,
        "seed": seed,
        "input": {"path": str(input_path), "sha256": _sha256_file(input_path)},
        "output": {"path": str(output_path), "sha256": _sha256_file(output_path), "rows": count},
    }
    try:
        _atomic_write_jsonl(manifest_path, [manifest])
    except BaseException:
        # The assignment is still valid and immutable; do not delete it on a
        # manifest failure, which would make a successful experiment vanish.
        raise
    return manifest


def run_credit(input_path: Path, output_path: Path, *, manifest_path: Path | None = None) -> dict[str, Any]:
    if not input_path.is_file():
        raise ValueError(f"input JSONL does not exist: {input_path}")
    manifest_path = manifest_path or output_path.with_name(output_path.name + ".manifest.json")
    _ensure_new(output_path)
    _ensure_new(manifest_path)
    count = _atomic_write_jsonl(output_path, (credit_record(row) for row in _read_jsonl(input_path)))
    manifest = {
        "algorithm": ALGORITHM,
        "version": VERSION,
        "kind": "click-credit",
        "input": {"path": str(input_path), "sha256": _sha256_file(input_path)},
        "output": {"path": str(output_path), "sha256": _sha256_file(output_path), "rows": count},
    }
    _atomic_write_jsonl(manifest_path, [manifest])
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("interleave", "credit"):
        child = sub.add_parser(command)
        child.add_argument("--input", type=Path, required=True)
        child.add_argument("--output", type=Path, required=True)
        child.add_argument("--manifest", type=Path)
        if command == "interleave":
            child.add_argument("--seed", required=True)
    args = parser.parse_args(argv)
    try:
        manifest = (run_interleave(args.input, args.output, seed=args.seed, manifest_path=args.manifest)
                    if args.command == "interleave"
                    else run_credit(args.input, args.output, manifest_path=args.manifest))
    except (ValueError, FileExistsError, OSError) as exc:
        parser.error(str(exc))
    print(_canonical_json(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
