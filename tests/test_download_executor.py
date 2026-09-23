# -*- coding: utf-8 -*-
"""下载执行器（download_executor）+ provision 回写（record_provision_results）确定性测试。

全程禁网：HTTP 出口只有一个接缝 `opener`，测试注入假响应/假异常；台账写到 tmp 目录。
覆盖：校验分级全档（ok/size_ok/md5_mismatch/size_mismatch/unverified/unreachable/
rejected/skipped_flagged）、白名单与 https 双闸、.part 清理、.corrupt 留证据、
目录 fail-closed、指数退避重试、台账帧追加 + current 重建 + 内容寻址往返一致。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_plan as DP  # noqa: E402
from dataset_recommender.corpus import inspection as I  # noqa: E402
import patrol_links as P  # noqa: E402
import record_provision_results as REC  # noqa: E402


# ---------------------------------------------------------------- 假 HTTP

class FakeResp:
    """模拟 urllib 响应：.status / .read(n) / 上下文管理。"""

    def __init__(self, data: bytes, status: int = 200):
        self._data, self.status, self._pos = data, status, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:] if n is None or n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def _row(content: bytes, *, md5=True, size=True, flag=None, url="https://cf.10xgenomics.com/x/f.h5"):
    return {
        "dataset_uid": "10x:A", "safe_uid": "10x_A", "source": "10x Genomics",
        "tier": DP.TIER_CHECKSUM, "filename": "f.h5", "filename_derived": False,
        "safe_name": "f.h5", "download_url": url, "netloc": "cf.10xgenomics.com",
        "bytes": len(content) if size else None, "md5sum": _md5(content) if md5 else None,
        "verify": "md5" if md5 else ("size" if size else "none"),
        "category": "", "pipeline": "", "flag_kind": flag,
        "flag_reason_zh": "", "last_verified": "",
    }


DATA = b"single-cell-matrix-bytes" * 100
ALLOWED = ["cf.10xgenomics.com"]


def _opener_ok(content=DATA, status=200):
    calls = []

    def open_(url, timeout):
        calls.append(url)
        return FakeResp(content, status)
    open_.calls = calls
    return open_


def _opener_flaky(fail_times, exc_factory, then=DATA):
    calls = []

    def open_(url, timeout):
        calls.append(url)
        if len(calls) <= fail_times:
            raise exc_factory()
        return FakeResp(then, 200)
    open_.calls = calls
    return open_


# ---------------------------------------------------------------- 目录 fail-closed

def test_out_dir_rejects_relative():
    with pytest.raises(DE.ProvisionError) as e:
        DE.resolve_out_dir("some/relative/dir")
    assert e.value.code == "bad_out_dir"


def test_out_dir_rejects_empty():
    with pytest.raises(DE.ProvisionError):
        DE.resolve_out_dir("")


def test_out_dir_rejects_frozen_base():
    base = ROOT / "database" / "base"
    with pytest.raises(DE.ProvisionError) as e:
        DE.resolve_out_dir(str(base / "subdir"))
    assert e.value.code == "protected_out_dir"


def test_out_dir_rejects_external_metadata_store():
    """实证：external 只许 upload_*.json 元数据，
    显式把下载物落进去此前竟能通过——整个 database/ 都在保护区内。"""
    with pytest.raises(DE.ProvisionError) as e:
        DE.resolve_out_dir(str(ROOT / "database" / "external"))
    assert e.value.code == "protected_out_dir"


def test_out_dir_rejects_database_root_and_research():
    """研究流水线上移为顶层 research/，
    受保护区随之等价扩展（整个 research/ 都不许落下载物）。"""
    for sub in ("database", "research"):
        with pytest.raises(DE.ProvisionError) as e:
            DE.resolve_out_dir(str(ROOT / sub))
        assert e.value.code == "protected_out_dir", sub


def test_out_dir_rejects_inrepo_data():
    with pytest.raises(DE.ProvisionError) as e:
        DE.resolve_out_dir(str(ROOT / "src" / "dataset_recommender" / "data" / "x"))
    assert e.value.code == "protected_out_dir"


def test_out_dir_creates_abs(tmp_path):
    d = DE.resolve_out_dir(str(tmp_path / "new" / "nested"))
    assert d.is_dir() and d.is_absolute()


def test_out_dir_rejects_real_write_side_when_frozen(tmp_path, monkeypatch):
    """frozen 下 `_repo_root()` 会指向只读快照 `_MEIPASS`，历史推导会把真实写侧
    data_root 下的 external 漏出保护名单——必须改经 runtime_paths 的真实路径。
    dest 指向 user_external_dir / data_root / shipped_external_dir 都应被拒。"""
    from dataset_recommender.app.runtime_paths import get_app_paths, reset_app_paths_cache

    meipass = tmp_path / "bundle"
    local = tmp_path / "localappdata"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    reset_app_paths_cache()
    try:
        paths = get_app_paths()
        assert paths.runtime_mode == "frozen"
        # 真实写侧 user_external_dir（data_root/database/external）必须被拒
        with pytest.raises(DE.ProvisionError) as e:
            DE.resolve_out_dir(str(paths.user_external_dir))
        assert e.value.code == "protected_out_dir"
        # data_root 整体也在保护区内（含 run/.userdata/trace 等子路径）
        with pytest.raises(DE.ProvisionError) as e:
            DE.resolve_out_dir(str(paths.data_root / "run" / "downloads"))
        assert e.value.code == "protected_out_dir"
        # 读侧 shipped_external_dir（resource_root/database/external）同样被拒
        with pytest.raises(DE.ProvisionError) as e:
            DE.resolve_out_dir(str(paths.shipped_external_dir))
        assert e.value.code == "protected_out_dir"
        # 双根分离下普通用户目录（非保护区）仍可正常创建
        ok = tmp_path / "user-downloads"
        assert DE.resolve_out_dir(str(ok)) == ok.resolve()
    finally:
        reset_app_paths_cache()


# ---------------------------------------------------------------- 白名单 / https

def test_url_policy_rejects_http():
    assert DE.url_policy_error("http://cf.10xgenomics.com/x", ALLOWED)


def test_url_policy_rejects_unknown_host():
    assert DE.url_policy_error("https://evil.example.com/x", ALLOWED)


def test_url_policy_allows_whitelisted_https():
    assert DE.url_policy_error("https://cf.10xgenomics.com/x", ALLOWED) is None


def test_rejected_row_never_touches_network(tmp_path):
    row = _row(DATA, url="https://evil.example.com/x/f.h5")
    opener = _opener_ok()
    r = DE.download_one(row, tmp_path, ALLOWED, opener=opener, sleep=lambda s: None)
    assert r.status == DE.STATUS_REJECTED and opener.calls == []


# ---------------------------------------------------------------- 校验分级

def test_ok_md5_verified(tmp_path):
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=_opener_ok(), sleep=lambda s: None)
    assert r.status == DE.STATUS_OK
    assert r.md5_actual == _md5(DATA) and r.bytes_downloaded == len(DATA)
    assert (tmp_path / "10x_A" / "f.h5").read_bytes() == DATA
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()
    assert r.attempts == 1 and r.http_status == 200


def test_size_ok_without_md5(tmp_path):
    r = DE.download_one(_row(DATA, md5=False), tmp_path, ALLOWED, opener=_opener_ok(), sleep=lambda s: None)
    assert r.status == DE.STATUS_SIZE_OK


def test_md5_mismatch_keeps_corrupt_evidence(tmp_path):
    # 等长篡改：声明 2300 字节、服务器也实发 2300 字节但内容不同 → md5 对不上 → .corrupt。
    # （「声明 10 字节、实发 2300 字节」会被硬上限拦截为 unreachable——那是新的
    # 预期行为；md5_mismatch 钉改用等长内容验证，与 test_download_manager 同口径。）
    claimed, served = b"a" * 2300, b"b" * 2300
    r = DE.download_one(_row(claimed), tmp_path, ALLOWED,
                        opener=_opener_ok(served), sleep=lambda s: None)
    assert r.status == DE.STATUS_MD5_MISMATCH
    assert (tmp_path / "10x_A" / "f.h5.corrupt").exists()
    assert not (tmp_path / "10x_A" / "f.h5").exists()
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()


def test_size_mismatch_when_bytes_differ(tmp_path):
    row = _row(DATA, md5=False)
    row["bytes"] = len(DATA) + 7
    r = DE.download_one(row, tmp_path, ALLOWED, opener=_opener_ok(), sleep=lambda s: None)
    assert r.status == DE.STATUS_SIZE_MISMATCH
    assert (tmp_path / "10x_A" / "f.h5.corrupt").exists()


def test_unverified_when_nothing_to_check(tmp_path):
    r = DE.download_one(_row(DATA, md5=False, size=False), tmp_path, ALLOWED,
                        opener=_opener_ok(), sleep=lambda s: None)
    assert r.status == DE.STATUS_UNVERIFIED
    assert (tmp_path / "10x_A" / "f.h5").exists()   # 下完了是事实，核不动也是事实


def test_unreachable_after_retries_cleans_part(tmp_path):
    sleeps = []

    def boom():
        return urllib.error.URLError("simulated network down")
    opener = _opener_flaky(99, boom)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        max_attempts=3, backoff=1.0, sleep=sleeps.append)
    assert r.status == DE.STATUS_UNREACHABLE
    assert r.attempts == 3 and len(opener.calls) == 3
    assert sleeps == [1.0, 2.0]                       # 指数退避：1*2^0, 1*2^1
    assert not (tmp_path / "10x_A" / "f.h5.part").exists()
    assert r.error == "URLError" and r.http_status is None


def test_http_error_is_definitive_no_retry(tmp_path):
    def dead():
        return urllib.error.HTTPError("https://x", 404, "not found", None, None)
    opener = _opener_flaky(99, dead)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener, sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE
    assert r.attempts == 1 and r.http_status == 404    # 4xx 是确定答案，不重试（与 patrol 同口径）


def test_retry_recovers_and_verifies(tmp_path):
    def flaky():
        return urllib.error.URLError("transient")
    opener = _opener_flaky(2, flaky)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        max_attempts=3, sleep=lambda s: None)
    assert r.status == DE.STATUS_OK and r.attempts == 3


def test_skipped_flagged_default_and_override(tmp_path):
    row = _row(DATA, flag="dead")
    opener = _opener_ok()
    r1 = DE.download_one(row, tmp_path, ALLOWED, opener=opener, sleep=lambda s: None)
    assert r1.status == DE.STATUS_SKIPPED_FLAGGED and opener.calls == []
    r2 = DE.download_one(row, tmp_path, ALLOWED, opener=opener,
                         include_flagged=True, sleep=lambda s: None)
    assert r2.status == DE.STATUS_OK and r2.flag_kind == "dead"


# ---------------------------------------------------------------- provision 端到端（假清单+假计划）

def _fake_manifest_record():
    return {
        "url": "https://www.10xgenomics.com/datasets/a",
        "primary_download_url": "https://cf.10xgenomics.com/x/f.h5",
        "primary_bytes": len(DATA), "primary_title": "Fake A",
        "files": [
            {"filename": "f.h5", "download_url": "https://cf.10xgenomics.com/x/f.h5",
             "bytes": len(DATA), "md5sum": _md5(DATA), "category": "outputs", "pipeline": ""},
            {"filename": "g.h5", "download_url": "https://cf.10xgenomics.com/x/g.h5",
             "bytes": len(DATA), "md5sum": _md5(DATA), "category": "outputs", "pipeline": ""},
        ],
        "n_files": 2,
    }


@pytest.fixture
def fake_downloads(monkeypatch):
    rec = _fake_manifest_record()
    monkeypatch.setattr(DE.downloads, "get", lambda uid: rec if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "files_for", lambda uid: rec["files"] if uid == "10x:A" else [])
    monkeypatch.setattr(DP.downloads, "get", lambda uid: rec if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "primary_url",
                        lambda uid: rec["primary_download_url"] if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "fastq_url", lambda uid: None)
    monkeypatch.setattr(DP.downloads, "is_available", lambda: True)
    # 不 patch inspection：虚构 uid/url 本就不在真台账里，status_for 自然返回 None（无旗标）；
    # 若 patch 共享模块的 status_for，会把同测试后段对读取侧的断言也一起换掉。
    return rec


def test_provision_end_to_end_all_scope(tmp_path, fake_downloads):
    report = DE.provision(["10x:A"], str(tmp_path / "out"), scope=DP.SCOPE_ALL,
                          workers=2, opener=_opener_ok(), sleep=lambda s: None)
    assert len(report.results) == 2
    assert all(r.status == DE.STATUS_OK for r in report.results)
    d = report.to_dict()
    assert d["schema"] == "biodata-provision/v0" and d["counts"]["ok"] == 2
    assert "核对通过" in report.summary_zh()
    # 并发下两个文件同名 safe_name？不同 filename → 各自落盘
    assert (tmp_path / "out" / "10x_A" / "f.h5").exists()
    assert (tmp_path / "out" / "10x_A" / "g.h5").exists()


def test_provision_primary_scope_picks_one(tmp_path, fake_downloads):
    report = DE.provision(["10x:A"], str(tmp_path / "out"),
                          opener=_opener_ok(), sleep=lambda s: None)
    assert len(report.results) == 1 and report.results[0].filename == "f.h5"


def test_unknown_uid_fail_closed(tmp_path, fake_downloads):
    with pytest.raises(DE.ProvisionError) as e:
        DE.provision(["10x:NOPE"], str(tmp_path))
    assert e.value.code == "unknown_uid"


def test_only_files_subset_and_fail_closed(tmp_path, fake_downloads):
    report = DE.provision(["10x:A"], str(tmp_path / "out"), scope=DP.SCOPE_ALL,
                          only_files=["g.h5"], opener=_opener_ok(), sleep=lambda s: None)
    assert [r.filename for r in report.results] == ["g.h5"]
    with pytest.raises(DE.ProvisionError):
        DE.provision(["10x:A"], str(tmp_path / "o2"), scope=DP.SCOPE_ALL,
                     only_files=["no-such-file.bin"], opener=_opener_ok(), sleep=lambda s: None)


# ---------------------------------------------------------------- 台账回写：帧追加 + current 重建 + 往返一致

def _run_and_record(tmp_path, fake_downloads, *, iso="2026-08-01T00:00:00Z"):
    """真执行（假网）→ 报告落盘 → 回写 tmp 台账目录。返回 (report, ledger_dir)。"""
    out = tmp_path / "out"
    report = DE.provision(["10x:A"], str(out), scope=DP.SCOPE_ALL,
                          opener=_opener_ok(), sleep=lambda s: None)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")

    ledger = tmp_path / "ledger"
    fake_manifest = {"10x:A": [(P.norm(f["download_url"]), f["bytes"])
                               for f in _fake_manifest_record()["files"]]}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    rc = REC.record(str(report_path), out_dir=str(ledger), snapshot_iso=iso)
    monkeypatch.undo()
    assert rc == 0
    return report, ledger


def test_record_appends_frame_and_rebuilds_current(tmp_path, fake_downloads, monkeypatch):
    report, ledger = _run_and_record(tmp_path, fake_downloads)
    snaps = list((ledger / "snapshots").glob("provision-*.jsonl"))
    assert len(snaps) == 1
    lines = snaps[0].read_text(encoding="utf-8").strip().split("\n")
    meta = json.loads(lines[0])["_meta"]
    assert meta["schema"] == P.SCHEMA and meta["source"] == "provision"
    rows = [json.loads(x) for x in lines[1:]]
    assert all(r.get("i") == "verified" for r in rows)          # integrity 维首次落盘
    assert all(set(("k", "r", "h", "s", "srv", "i")) >= set(r) for r in rows)

    cur = json.loads((ledger / "current.json").read_text(encoding="utf-8"))
    vecs = cur["by_uid"]["10x:A"]["f"]
    assert all(v["i"] == "verified" and v["r"] == "ok" and v["s"] == "match"
               for v in vecs.values())
    assert cur["by_uid"]["10x:A"]["np"] == 0
    assert cur["totals"].get("integrity_verified") == 2

    # 读取侧立刻能看到 integrity 维（point inspection at this current.json）
    monkeypatch.setattr(I, "_DATA_PATH", str(ledger / "current.json"))
    I._load.cache_clear()
    try:
        st = I.status_for("10x:A", "https://cf.10xgenomics.com/x/f.h5")
        assert st["integrity"] == "verified" and st["problem"] is False
    finally:
        I._load.cache_clear()


def test_record_content_addressed_rerun_same_id(tmp_path, fake_downloads):
    """同一份报告、同一时刻重跑 → 同一个 snapshot_id（可核验、可去重）。"""
    _, ledger = _run_and_record(tmp_path, fake_downloads)
    snap_id_1 = json.loads((ledger / "current.json").read_text(encoding="utf-8"))["snapshot_id"]
    report_path = tmp_path / "report.json"
    fake_manifest = {"10x:A": [(P.norm(f["download_url"]), f["bytes"])
                               for f in _fake_manifest_record()["files"]]}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    rc = REC.record(str(report_path), out_dir=str(ledger), snapshot_iso="2026-08-01T00:00:00Z")
    monkeypatch.undo()
    assert rc == 0
    snap_id_2 = json.loads((ledger / "current.json").read_text(encoding="utf-8"))["snapshot_id"]
    assert snap_id_1 == snap_id_2
    assert len(list((ledger / "snapshots").glob("provision-*.jsonl"))) == 1   # 同 id 覆盖，不堆帧


def test_record_md5_mismatch_becomes_problem(tmp_path, fake_downloads, monkeypatch):
    # 服务器给的内容与清单 md5 不符（等长，避免 size 维抢 reason 优先级）→ integrity=mismatch → 派生 problem
    report = DE.provision(["10x:A"], str(tmp_path / "out"), scope=DP.SCOPE_ALL,
                          opener=_opener_ok(content=b"tampered" + b"x" * (len(DATA) - 8)),
                          sleep=lambda s: None)
    assert all(r.status == DE.STATUS_MD5_MISMATCH for r in report.results)
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
    fake_manifest = {"10x:A": [(P.norm(f["download_url"]), f["bytes"])
                               for f in _fake_manifest_record()["files"]]}
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    ledger = tmp_path / "ledger"
    assert REC.record(str(report_path), out_dir=str(ledger)) == 0

    monkeypatch.setattr(I, "_DATA_PATH", str(ledger / "current.json"))
    I._load.cache_clear()
    try:
        st = I.status_for("10x:A", "https://cf.10xgenomics.com/x/f.h5")
        assert st["integrity"] == "mismatch" and st["problem"] is True
        assert "md5" in st["problem_reason"]
        summ = I.dataset_summary("10x:A")
        assert summ["n_problem"] == 2 and summ["provisioning_ok"] is False
        # 钉不变量：摘要计数 == 逐文件重算计数（与 test_inspection.py 同一条）
        fs = I.file_status("10x:A")
        assert sum(1 for s in fs.values() if s["problem"]) == summ["n_problem"]
    finally:
        I._load.cache_clear()


def test_record_skipped_and_unreachable_produce_no_or_soft_evidence(tmp_path, fake_downloads, monkeypatch):
    # skipped_flagged：无新证据 → 不落帧；unreachable(网络错)：reach=unknown，不死、不覆盖 integrity
    rep = {"schema": "biodata-provision/v0", "results": [
        {"dataset_uid": "10x:A", "url": "https://cf.10xgenomics.com/x/f.h5",
         "status": "skipped_flagged", "http_status": None, "bytes_downloaded": None},
        {"dataset_uid": "10x:A", "url": "https://cf.10xgenomics.com/x/g.h5",
         "status": "unreachable", "http_status": None, "bytes_downloaded": None},
    ]}
    fake_manifest = {"10x:A": [(P.norm(f["download_url"]), f["bytes"])
                               for f in _fake_manifest_record()["files"]]}
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    results, n_no_evidence = REC.results_from_report(rep, {
        f"10x:A\t{P.norm('https://cf.10xgenomics.com/x/f.h5')}": len(DATA),
        f"10x:A\t{P.norm('https://cf.10xgenomics.com/x/g.h5')}": len(DATA),
    })
    assert n_no_evidence == 1 and len(results) == 1
    only = next(iter(results.values()))
    assert only["reach"] == "unknown" and "integrity" not in only


def test_record_rejects_foreign_report(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "something-else", "results": []}), encoding="utf-8")
    assert REC.record(str(bad), out_dir=str(tmp_path / "l")) == 2


def test_record_empty_evidence_writes_nothing(tmp_path, fake_downloads, monkeypatch):
    rep = {"schema": "biodata-provision/v0", "results": [
        {"dataset_uid": "10x:A", "url": "https://cf.10xgenomics.com/x/f.h5",
         "status": "skipped_flagged", "http_status": None, "bytes_downloaded": None},
    ]}
    p = tmp_path / "r.json"
    p.write_text(json.dumps(rep), encoding="utf-8")
    monkeypatch.setattr(P, "load_manifest", lambda: {"10x:A": []})
    ledger = tmp_path / "ledger"
    assert REC.record(str(p), out_dir=str(ledger)) == 0
    assert not (ledger / "current.json").exists()


# ---------------------------------------------------------------- patrol 兼容：无 integrity 的帧格式逐位不变

def test_rebuild_current_without_integrity_keeps_legacy_shape():
    manifest = {"10x:A": [("https://cf.10xgenomics.com/x/f.h5", 100)]}
    results = {"10x:A\thttps://cf.10xgenomics.com/x/f.h5":
               {"reach": "ok", "http": 206, "srv": 100, "size": "match"}}
    current, snap_rows, snap_id, tally = P.rebuild_current(
        manifest, {}, results, "2026-08-01T00:00:00Z", source="patrol")
    assert snap_rows == [{"k": "10x:A\thttps://cf.10xgenomics.com/x/f.h5",
                          "r": "ok", "h": 206, "s": "match", "srv": 100}]
    vec = current["by_uid"]["10x:A"]["f"]["https://cf.10xgenomics.com/x/f.h5"]
    assert "i" not in vec and "integrity_verified" not in tally


def test_rebuild_current_preserves_prior_integrity_on_patrol_reprobe():
    """patrol 重测存活（不带 integrity）不得抹掉 provision 已落盘的 i 结论。"""
    url = "https://cf.10xgenomics.com/x/f.h5"
    manifest = {"10x:A": [(url, 100)]}
    prior = {"by_uid": {"10x:A": {"lv": "2026-08-01", "nf": 1, "np": 0,
                                  "f": {url: {"r": "ok", "h": 206, "s": "match",
                                              "srv": 100, "v": "2026-08-01", "i": "verified"}}}}}
    results = {f"10x:A\t{url}": {"reach": "ok", "http": 206, "srv": 100, "size": "match"}}
    current, snap_rows, _, tally = P.rebuild_current(
        manifest, prior, results, "2026-08-02T00:00:00Z", source="patrol")
    vec = current["by_uid"]["10x:A"]["f"][url]
    assert vec["i"] == "verified" and snap_rows[0]["i"] == "verified"
    assert tally["integrity_verified"] == 1


# ---------------------------------------------------------------- 429/503 退避重试

def test_http_429_is_retried_with_backoff(tmp_path):
    """429 限流不是确定答案——按指数退避重试（与 corpus_net/corpus_curation 同一网络纪律）。"""
    def throttled():
        return urllib.error.HTTPError("https://x", 429, "too many requests", None, None)
    sleeps = []
    opener = _opener_flaky(2, throttled)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        max_attempts=3, backoff=1.0, sleep=sleeps.append)
    assert r.status == DE.STATUS_OK and r.attempts == 3
    assert sleeps == [1.0, 2.0], "429 必须走与其他瞬时错误相同的指数退避"


def test_http_503_persistent_gives_unreachable_after_retries(tmp_path):
    """503 持续失败 → 重试耗尽后如实 unreachable（attempts 记全）。"""
    def unavailable():
        return urllib.error.HTTPError("https://x", 503, "service unavailable", None, None)
    opener = _opener_flaky(99, unavailable)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        max_attempts=3, backoff=1.0, sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE
    assert r.attempts == 3 and r.http_status == 503
    assert r.error == "HTTPError(503)"


def test_http_404_still_definitive_no_retry(tmp_path):
    """回归钉：404 等其余 4xx 仍是确定答案，不重试（既有纪律不变）。"""
    def dead():
        return urllib.error.HTTPError("https://x", 404, "not found", None, None)
    opener = _opener_flaky(99, dead)
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        max_attempts=3, sleep=lambda s: None)
    assert r.status == DE.STATUS_UNREACHABLE
    assert r.attempts == 1 and r.http_status == 404


# ---------------------------------------------------------------- additive 参数（subdir / progress_cb / cancel）

def test_subdir_overrides_safe_uid_folder(tmp_path):
    """subdir 非 None 时文件落在 out_root/<subdir>/，而不是 out_root/<safe_uid>/；
    saved_as 如实反映。不给 subdir 时行为与旧版逐字节一致（既有测试覆盖）。"""
    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, subdir="10x_A__某标题",
                        opener=_opener_ok(), sleep=lambda s: None)
    assert r.status == DE.STATUS_OK
    assert (tmp_path / "10x_A__某标题" / "f.h5").read_bytes() == DATA
    assert not (tmp_path / "10x_A" / "f.h5").exists()
    assert r.saved_as == "10x_A__某标题/f.h5"


def test_progress_cb_receives_per_chunk_deltas(tmp_path):
    """progress_cb 每写完一块收到本块字节数；累计 == 文件总长。"""
    received = []

    def cb(delta):
        received.append(delta)

    r = DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=_opener_ok(),
                        sleep=lambda s: None, progress_cb=cb)
    assert r.status == DE.STATUS_OK
    assert sum(received) == len(DATA) and received
    assert all(d > 0 for d in received)


def test_cancel_event_pre_set_raises_and_keeps_empty_part(tmp_path):
    """cancel_event 在调用前已置位 → 不读网络内容直接 DownloadCancelled；.part 保留（可续传语义）。"""
    ev = threading.Event()
    ev.set()
    opener = _opener_ok()
    with pytest.raises(DE.DownloadCancelled):
        DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=opener,
                        sleep=lambda s: None, cancel_event=ev)
    part = tmp_path / "10x_A" / "f.h5.part"
    assert part.exists(), "取消必须保留 .part（续传语义），不能像失败一样清掉"


def test_cancel_mid_stream_raises_and_keeps_partial_part(tmp_path):
    """进度回调里取消（首块已落盘）→ DownloadCancelled；.part 保留首块字节。"""
    content = b"Z" * (3 * 1024 * 1024)  # 3 MiB → 3 个 1 MiB 块
    ev = threading.Event()

    def cb(delta):
        ev.set()

    with pytest.raises(DE.DownloadCancelled):
        DE.download_one(_row(content), tmp_path, ALLOWED, opener=_opener_ok(content),
                        sleep=lambda s: None, cancel_event=ev, progress_cb=cb)
    part = tmp_path / "10x_A" / "f.h5.part"
    assert part.exists() and part.stat().st_size == 1024 * 1024
    assert part.read_bytes() == content[:1024 * 1024]
    assert not (tmp_path / "10x_A" / "f.h5").exists()
    assert not (tmp_path / "10x_A" / "f.h5.corrupt").exists()


def test_cancel_download_cancelled_is_not_retried(tmp_path):
    """DownloadCancelled 是 BaseException：不进入 except Exception 重试分支，
    总尝试次数恒为 1（取消不是网络错误，重试无意义）。"""
    ev = threading.Event()
    ev.set()
    sleeps = []

    def cb(delta):
        ev.set()

    with pytest.raises(DE.DownloadCancelled):
        DE.download_one(_row(DATA), tmp_path, ALLOWED, opener=_opener_ok(),
                        max_attempts=3, backoff=1.0, sleep=sleeps.append,
                        cancel_event=ev, progress_cb=cb)
    assert sleeps == [], "取消不能触发退避重试"
