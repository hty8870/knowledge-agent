# -*- coding: utf-8 -*-
"""upload 晋升机制（scripts/promote_uploads.py）专项门。**全离线、tmp_path 夹具**：

- happy path：新记录晋升进 10x-synced.json（uid 排序）、原 upload 归档 + manifest、
  流水账 action="promote"、base 既有文件不动；
- 机械闸逐项：uid/url/name 三种重复各拒一则（locate_record 同序复用）、
  record_count 不符拒、包装缺键拒、记录缺身份键拒；
- 外源 upload 跳过并报告（不算失败、文件不动）；
- 二次运行幂等（空晋升，base 内容与外部目录零变化）；
- 合并到既有 10x-synced.json（内部去重 + uid 排序）；
- CLI main() 退出码（拒绝 → 非零）。
"""
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import promote_uploads as pu  # noqa: E402

BASE = pu.BASE_SOURCE  # "10x Genomics"


def _rec(uid, *, name=None, url=None, source=BASE):
    return {
        "dataset_uid": uid,
        "dataset_name": name or f"Dataset {uid}",
        "url": url or f"https://example.org/{uid}",
        "species": "Human", "tissue": "Blood", "source": source,
    }


def _wrapper(records, *, source=BASE, note="test sync"):
    return {"source": source, "note": note,
            "record_count": len(records), "records": records}


def _make_root(tmp_path, base_records=("_b1", "_b2")):
    """最小项目根：database/base/10x-Visium.json（策展产物）+ database/external/。"""
    base_dir = tmp_path / "database" / "base"
    ext_dir = tmp_path / "database" / "external"
    base_dir.mkdir(parents=True)
    ext_dir.mkdir(parents=True)
    (base_dir / "10x-Visium.json").write_text(
        json.dumps([_rec(u) for u in base_records], ensure_ascii=False, indent=2),
        encoding="utf-8")
    return tmp_path


def _put_upload(root, name, payload):
    (root / "database" / "external" / name).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _journal_lines(root):
    p = root / ".userdata" / "uploads_journal.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def _synced(root):
    p = root / "database" / "base" / "10x-synced.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def test_happy_path_promotes_journals_and_archives(tmp_path):
    root = _make_root(tmp_path)
    _put_upload(root, "upload_20260817_000000_000001_curate_sync_10x.json",
                _wrapper([_rec("n2"), _rec("n1")]))
    base_before = (root / "database" / "base" / "10x-Visium.json").read_bytes()

    report = pu.promote_uploads(root, log=lambda *_: None)

    assert report["ok"] is True and report["promoted_total"] == 2
    # 晋升文件：uid 排序、恰好两条新记录；策展产物逐位未动。
    synced = _synced(root)
    assert [r["dataset_uid"] for r in synced] == ["n1", "n2"]
    assert (root / "database" / "base" / "10x-Visium.json").read_bytes() == base_before
    # 原 upload 搬走归档（.promoted + manifest），外部目录不再有它。
    ext = root / "database" / "external"
    assert not list(ext.glob("upload_*.json"))
    arch = root / "research" / "promotions"
    promoted_blob = arch / "upload_20260817_000000_000001_curate_sync_10x.json.promoted"
    manifest = arch / "upload_20260817_000000_000001_curate_sync_10x.json.manifest.json"
    assert promoted_blob.exists() and manifest.exists()
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["promoted_count"] == 2 and m["skipped_count"] == 0
    assert m["uids"] == ["n2", "n1"]  # 晋升序（文件内顺序），写出另按 uid 排序
    assert m["sha256_source"] and m["sha256_base_after"]
    # 流水账：action="promote"、条数、指纹齐全（与 ingest 同一份账）。
    journal = _journal_lines(root)
    assert len(journal) == 1 and journal[0]["action"] == "promote"
    assert journal[0]["record_count"] == 2
    assert journal[0]["filename"] == "10x-synced.json"


def test_gate_rejects_uid_url_name_duplicates(tmp_path):
    """uid 精确 > url 精确 > name 精确三档重复各拒一则（复用 locate_record，整批语义）。"""
    cases = [
        _rec("_b1"),                                        # uid 撞 base
        _rec("new-url", url="https://example.org/_b1"),     # url 撞 base
        _rec("new-name", name="Dataset _b2"),               # name 撞 base
    ]
    for i, dup in enumerate(cases):
        root = _make_root(tmp_path / f"case{i}")
        _put_upload(root, "upload_20260817_000000_000002_curate_sync_10x.json",
                    _wrapper([dup, _rec(f"fine-{i}")]))
        report = pu.promote_uploads(root, log=lambda *_: None)
        assert report["ok"] is False, f"case {i} 应整批拒绝"
        assert len(report["rejected"]) == 1 and "重复" in report["rejected"][0]["reason"]
        assert _synced(root) is None, "整批不晋升：好记录也不许夹带入库"
        assert list((root / "database" / "external").glob("upload_*.json")), "原文件不动"
        assert _journal_lines(root) == [], "拒绝不写流水账"


def test_gate_rejects_record_count_mismatch(tmp_path):
    root = _make_root(tmp_path)
    payload = _wrapper([_rec("n1")])
    payload["record_count"] = 5
    _put_upload(root, "upload_20260817_000000_000003_curate_sync_10x.json", payload)
    report = pu.promote_uploads(root, log=lambda *_: None)
    assert report["ok"] is False
    assert "不符" in report["rejected"][0]["reason"]
    assert _synced(root) is None


def test_gate_rejects_wrapper_missing_keys(tmp_path):
    root = _make_root(tmp_path)
    payload = _wrapper([_rec("n1")])
    del payload["records"]
    _put_upload(root, "upload_20260817_000000_000004_curate_sync_10x.json", payload)
    report = pu.promote_uploads(root, log=lambda *_: None)
    assert report["ok"] is False and "缺键" in report["rejected"][0]["reason"]
    # 记录缺身份键同档
    root2 = _make_root(tmp_path / "nokey")
    bad = _rec("n1")
    del bad["url"]
    _put_upload(root2, "upload_20260817_000000_000005_curate_sync_10x.json", _wrapper([bad]))
    report2 = pu.promote_uploads(root2, log=lambda *_: None)
    assert report2["ok"] is False and "身份键" in report2["rejected"][0]["reason"]


def test_foreign_source_upload_skipped_not_failed(tmp_path):
    root = _make_root(tmp_path)
    _put_upload(root, "upload_20260817_000000_000006_cellxgene.json",
                _wrapper([_rec("x1", source="CELLxGENE Discover")],
                         source="CELLxGENE Discover"))
    report = pu.promote_uploads(root, log=lambda *_: None)
    assert report["ok"] is True, "外源跳过是报告不是失败"
    assert len(report["skipped"]) == 1 and "外源" in report["skipped"][0]["reason"]
    assert _synced(root) is None
    assert list((root / "database" / "external").glob("upload_*.json")), "外源文件原地不动"
    assert _journal_lines(root) == []


def test_second_run_is_idempotent(tmp_path):
    root = _make_root(tmp_path)
    _put_upload(root, "upload_20260817_000000_000007_curate_sync_10x.json",
                _wrapper([_rec("n1")]))
    first = pu.promote_uploads(root, log=lambda *_: None)
    assert first["ok"] is True and first["promoted_total"] == 1
    synced_bytes = (root / "database" / "base" / "10x-synced.json").read_bytes()

    second = pu.promote_uploads(root, log=lambda *_: None)
    assert second["ok"] is True and second["promoted_total"] == 0
    assert second["promoted"] == [] and second["rejected"] == [] and second["skipped"] == []
    assert (root / "database" / "base" / "10x-synced.json").read_bytes() == synced_bytes
    assert len(_journal_lines(root)) == 1, "空晋升不再写账"


def test_merges_into_existing_synced_file_sorted_deduped(tmp_path):
    root = _make_root(tmp_path)
    synced_path = root / "database" / "base" / "10x-synced.json"
    synced_path.write_text(json.dumps([_rec("s2"), _rec("s1")], indent=2), encoding="utf-8")
    _put_upload(root, "upload_20260817_000000_000008_curate_sync_10x.json",
                _wrapper([_rec("n1")]))
    report = pu.promote_uploads(root, log=lambda *_: None)
    assert report["ok"] is True and report["promoted_total"] == 1
    assert [r["dataset_uid"] for r in _synced(root)] == ["n1", "s1", "s2"]


def test_cli_exit_code_nonzero_on_rejection(tmp_path):
    root = _make_root(tmp_path)
    _put_upload(root, "upload_20260817_000000_000009_curate_sync_10x.json",
                _wrapper([_rec("_b1")]))
    assert pu.main(["--project-root", str(root)]) == 1
    root2 = _make_root(tmp_path / "ok")
    assert pu.main(["--project-root", str(root2)]) == 0, "无可晋升文件 = 幂等成功"
