from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import make_delivery as delivery  # noqa: E402
import sanitize_metadata_contacts as contacts  # noqa: E402


def _snapshots(root: Path = ROOT) -> set[str]:
    """tracked 快照清单（git ls-files）：权利清单只管**随仓库分发**的快照。
    filesystem glob 会把本机运行产物（gitignored 的 database/external/upload_*.json
    用户上传落盘）也算进来——那些文件从不进仓库、从不出门，不该要求权利条目，
    否则任何真用过本产品的机器都过不了门。"""
    import subprocess

    out = subprocess.run(
        ["git", "ls-files", "database/base", "database/external"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip().endswith(".json")}


def test_every_snapshot_has_a_rights_manifest_entry() -> None:
    manifest = (ROOT / "database" / "SOURCES.yml").read_text(encoding="utf-8")
    declared = set(re.findall(r"(?m)^\s+snapshot:\s+([^\s#]+)\s*$", manifest))
    assert declared == _snapshots()
    assert "project code, not third-party records" in manifest
    assert "contact_data_policy:" in manifest


def test_live_metadata_contains_no_contact_email() -> None:
    assert contacts.scan(contacts.snapshot_paths()) == []


def test_contact_redaction_is_atomic_and_does_not_log_values(tmp_path: Path) -> None:
    database = tmp_path / "database" / "external"
    database.mkdir(parents=True)
    snapshot = database / "sample.json"
    snapshot.write_text('[{"description":"contact person@example.invalid"}]\n', encoding="utf-8")

    assert contacts.scan([snapshot], root=tmp_path) == [("database/external/sample.json", 1)]
    assert contacts.redact_file(snapshot) == 1
    assert contacts.scan([snapshot], root=tmp_path) == []
    assert "[contact removed]" in snapshot.read_text(encoding="utf-8")


def test_delivery_gate_reports_only_contact_location(tmp_path: Path) -> None:
    database = tmp_path / "database" / "base"
    database.mkdir(parents=True)
    snapshot = database / "sample.json"
    snapshot.write_text('[{"description":"person@example.invalid"}]\n', encoding="utf-8")

    assert delivery.scan_metadata_contacts([snapshot], root=tmp_path) == [
        {"path": "database/base/sample.json", "line": 1}
    ]
