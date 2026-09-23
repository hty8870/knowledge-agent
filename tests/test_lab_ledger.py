# -*- coding: utf-8 -*-
"""实验室资产台账（lab_ledger）确定性测试。

用**合成清单** + tmp 目录里真实写的文件（真算 md5）验证：md5 命中=verified、名+大小对上但内容不符=
md5_mismatch、只名字对上=name_only、都对不上=unmatched；巨文件跳 md5、目录不存在优雅降级；
只读（不改本地文件）；报告按数据集汇总 + caveat 说明外部库无法核验。
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.content import lab_ledger


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _fake_manifest(files_by_uid: dict) -> dict:
    """files_by_uid: {uid: [(filename, bytes_content_or_size, md5 or None), ...]}"""
    by_uid = {}
    for uid, files in files_by_uid.items():
        fl = []
        for fn, content, md5 in files:
            size = len(content) if isinstance(content, (bytes, bytearray)) else int(content)
            fl.append({"filename": fn, "bytes": size, "md5sum": md5 or "",
                       "download_url": f"https://x/{fn}", "title": f"file {fn}"})
        by_uid[uid] = {"files": fl, "primary_title": f"Dataset {uid}"}
    return by_uid


def _load(by_uid):
    return lambda: (by_uid, {})


# ---------------------------------------------------------------- index + md5

def test_build_index_groups_by_md5_name_size():
    by_uid = _fake_manifest({
        "10x:A": [("a.bam", 100, "aaa"), ("b.h5", 200, "bbb")],
        "10x:B": [("c.csv", 50, "ccc")],
    })
    idx = lab_ledger.build_manifest_index(load=_load(by_uid))
    assert idx["n_files"] == 3 and idx["n_datasets"] == 2
    assert "aaa" in idx["by_md5"] and idx["by_md5"]["aaa"][0]["dataset_uid"] == "10x:A"
    assert ("c.csv", 50) in idx["by_name_size"]
    assert "b.h5" in idx["by_name"]


def test_file_md5_matches_hashlib(tmp_path):
    f = tmp_path / "x.bin"
    data = b"hello single cell world" * 1000
    f.write_bytes(data)
    assert lab_ledger.file_md5(f) == _md5(data)


def test_file_md5_unreadable_returns_none(tmp_path):
    assert lab_ledger.file_md5(tmp_path / "nope.bin") is None


# ---------------------------------------------------------------- match

def test_match_verified_by_md5():
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("a.bam", 10, "themd5")]})))
    m = lab_ledger.match_local_file("themd5", 10, "a.bam", idx)
    assert m["status"] == "verified" and m["dataset_uid"] == "10x:A" and m["match"] == "md5"


def test_match_md5_mismatch_when_name_size_hit_but_md5_differs():
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("a.bam", 10, "expected")]})))
    m = lab_ledger.match_local_file("different", 10, "a.bam", idx)
    assert m["status"] == "md5_mismatch"
    assert m["expected_md5"] == "expected"


def test_match_name_only_when_size_differs():
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("a.bam", 10, "m")]})))
    m = lab_ledger.match_local_file("whatever", 999, "a.bam", idx)
    assert m["status"] == "name_only"


def test_match_unmatched():
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("a.bam", 10, "m")]})))
    m = lab_ledger.match_local_file("x", 1, "totally_unknown.txt", idx)
    assert m["status"] == "unmatched" and m["dataset_uid"] is None


# ---------------------------------------------------------------- scan + report

def _write(p: Path, data: bytes) -> bytes:
    p.write_bytes(data)
    return data


def test_scan_and_report_end_to_end(tmp_path):
    # good1 在根、good2 在**子目录**（真验证 os.walk 递归——旧版 `if False` 把它平铺进根、递归从未被测）
    good1 = _write(tmp_path / "good1.bam", b"AAAA" * 500)
    (tmp_path / "sub").mkdir()
    good2 = _write(tmp_path / "sub" / "good2.h5", b"BBBB" * 300)
    corrupt = _write(tmp_path / "corrupt.csv", b"XXXX" * 10)  # 名+大小进清单、但 md5 用别的
    _write(tmp_path / "stranger.txt", b"nobody knows me")

    by_uid = _fake_manifest({
        "10x:A": [("good1.bam", len(good1), _md5(good1)), ("good2.h5", len(good2), _md5(good2)),
                  ("missing.bam", 12345, "notlocal")],
        "10x:B": [("corrupt.csv", len(corrupt), "declared_md5_that_wont_match")],
    })
    idx = lab_ledger.build_manifest_index(load=_load(by_uid))
    scan = lab_ledger.scan_directory(tmp_path, idx)
    assert scan["root_exists"] and scan["n_scanned"] == 4
    # 子目录里的 good2 被真扫到并 md5 核验（证明递归而非平铺）
    assert any(r["status"] == "verified" and r["filename"] == "good2.h5" for r in scan["results"])

    report = lab_ledger.build_ledger_report(scan, idx, load=_load(by_uid))
    assert report["n_verified"] == 2
    assert report["n_md5_mismatch"] == 1
    assert report["n_unmatched"] == 1
    ds = {d["dataset_uid"]: d for d in report["datasets"]}
    assert ds["10x:A"]["verified"] == 2
    assert ds["10x:A"]["catalog_files"] == 3          # 清单有 3 个，本地 2 个 → 缺 1
    assert ds["10x:B"]["md5_mismatch"] == 1
    assert report["caveat"] and "无法 md5 核验" in report["caveat"]
    assert len(report["unmatched"]) == 1


def test_scan_missing_dir_graceful():
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("a", 1, "m")]})))
    scan = lab_ledger.scan_directory(Path("/no/such/dir/really/nope"), idx)
    assert scan["root_exists"] is False and scan["n_scanned"] == 0
    report = lab_ledger.build_ledger_report(scan, idx, load=_load({}))
    assert report["n_scanned"] == 0 and report["root_exists"] is False


def test_huge_file_skips_md5_but_still_name_size_matches(tmp_path):
    data = b"C" * 4096
    f = _write(tmp_path / "big.bam", data)
    by_uid = _fake_manifest({"10x:A": [("big.bam", len(data), _md5(data))]})
    idx = lab_ledger.build_manifest_index(load=_load(by_uid))
    # max_hash_bytes 小于文件 → 跳 md5
    scan = lab_ledger.scan_directory(tmp_path, idx, max_hash_bytes=10)
    assert scan["n_unhashed"] == 1
    r = scan["results"][0]
    assert r["hashed"] is False and r["md5"] is None
    # 未算 md5 → 退化到名+大小命中（诚实：name_size_match，不冒充 verified）
    assert r["status"] == "name_size_match"


def test_report_discloses_weak_match_and_failure_counts(tmp_path):
    """weak_match（纯名+大小命中、md5 未确认）计入 n_weak_match，且 caveat 披露其「通用名可能张冠李戴」；
    scan 返回 n_symlink_skipped / n_unreadable 计数键。"""
    data = b"E" * 256
    f = _write(tmp_path / "barcodes.tsv.gz", data)
    # 清单里同名文件带 md5，但本地实算 md5 与之不同大小 → name_only（弱匹配，不冒充 verified）
    by_uid = _fake_manifest({"10x:A": [("barcodes.tsv.gz", len(data) + 99, "some_other_md5")]})
    idx = lab_ledger.build_manifest_index(load=_load(by_uid))
    scan = lab_ledger.scan_directory(tmp_path, idx)
    assert "n_symlink" in scan and "n_unreadable" in scan
    report = lab_ledger.build_ledger_report(scan, idx, load=_load(by_uid))
    assert report["n_weak_match"] == 1
    assert "n_symlink_skipped" in report and "n_unreadable" in report
    assert "weak_match" in report["caveat"] and "张冠李戴" in report["caveat"]


def test_scan_is_readonly(tmp_path):
    """扫描不改任何本地文件（内容 + mtime 不变）。"""
    f = tmp_path / "x.bin"
    data = b"D" * 2048
    f.write_bytes(data)
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    idx = lab_ledger.build_manifest_index(load=_load(_fake_manifest({"10x:A": [("x.bin", 2048, _md5(data))]})))
    lab_ledger.scan_directory(tmp_path, idx)
    after = (f.read_bytes(), f.stat().st_mtime_ns)
    assert before == after
