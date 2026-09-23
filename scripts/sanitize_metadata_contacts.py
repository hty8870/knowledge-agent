# -*- coding: utf-8 -*-
"""Remove contact-email literals from redistributable metadata snapshots.

Default mode is read-only and exits 1 when contacts remain.  --apply performs a
same-directory atomic replacement while preserving every other byte and line.
No matched address is ever printed.
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "database"
EMAIL_RE = re.compile(r"(?i)(?<![A-Z0-9._%+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![A-Z0-9.-])")
REPLACEMENT = "[contact removed]"


def snapshot_paths(root: Path = ROOT) -> list[Path]:
    candidates = [
        *(root / "database" / "base").glob("*.json"),
        *(root / "database" / "external").glob("*.json"),
    ]
    return sorted(path for path in candidates if path.is_file())


def scan(paths: list[Path], root: Path = ROOT) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for path in paths:
        text = path.read_text(encoding="utf-8-sig")
        for line_no, line in enumerate(text.splitlines(), 1):
            if EMAIL_RE.search(line):
                hits.append((path.relative_to(root).as_posix(), line_no))
    return hits


def redact_file(path: Path) -> int:
    raw = path.read_text(encoding="utf-8-sig")
    updated, count = EMAIL_RE.subn(REPLACEMENT, raw)
    if not count:
        return 0
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    try:
        tmp.write_text(updated, encoding="utf-8", newline="")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return count


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="sanitize redistributed metadata contacts")
    parser.add_argument("--apply", action="store_true", help="atomically replace contact emails")
    args = parser.parse_args(argv)

    paths = snapshot_paths()
    before = scan(paths)
    if not args.apply:
        if before:
            print(f"metadata contact scan failed: {len(before)} locations")
            for rel, line_no in before[:40]:
                print(f"  {rel}:{line_no}")
            return 1
        print(f"metadata contact scan passed: {len(paths)} snapshots")
        return 0

    replaced = sum(redact_file(path) for path in paths)
    after = scan(paths)
    if after:
        print(f"metadata contact sanitization incomplete: {len(after)} locations")
        return 1
    print(f"metadata contacts removed: {replaced} values across {len(paths)} snapshots")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
