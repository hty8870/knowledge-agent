# -*- coding: utf-8 -*-
"""load_smoke（加载抽样冒烟）+ 台账 load 维（additive "l" 槽）确定性测试。

全程禁网、不真装 scanpy：下载走 download_executor 的 opener 接缝（假响应/假异常），
加载走 run_smoke 的 readers 接缝（假 reader，绝不 import scanpy）；台账写到 tmp 目录。
覆盖：scanpy 缺失 fail-closed、platform 分层抽样确定性、类型分发（h5 真加载 / 其它
skipped_unsupported）、loaded / load_failed / download_failed 分级、台账 "l" 槽写入 +
inspection 读取派生 + patrol 重测不抹掉已有 "l" 值。
"""
from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_plan as DP  # noqa: E402
from dataset_recommender.corpus import inspection as I  # noqa: E402
import load_smoke as LS  # noqa: E402
import patrol_links as P  # noqa: E402
import record_provision_results as REC  # noqa: E402


# ---------------------------------------------------------------- 假 HTTP / 假 reader

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


DATA = b"fake-10x-h5-matrix-bytes" * 100


def _opener_ok(content=DATA, status=200):
    def open_(url, timeout):
        return FakeResp(content, status)
    return open_


def _opener_http_error(code):
    def open_(url, timeout):
        raise urllib.error.HTTPError(url, code, "nope", None, None)
    return open_


def _fake_adata(n_obs=100, n_vars=2000):
    return SimpleNamespace(shape=(n_obs, n_vars), n_obs=n_obs, n_vars=n_vars)


# ---------------------------------------------------------------- 假清单记录（uid → primary 文件）

def _rec(uid, filename, url, content=DATA, platform="10x Genomics Chromium"):
    return {
        "url": f"https://www.10xgenomics.com/datasets/{uid.lower()}",
        "platform": platform,
        "primary_download_url": url, "primary_bytes": len(content),
        "primary_filename": filename, "primary_title": "Filtered feature barcode matrix (HDF5)",
        "files": [{"filename": filename, "download_url": url, "bytes": len(content),
                   "md5sum": _md5(content), "category": "outputs", "pipeline": ""}],
        "n_files": 1,
    }


URL_A = "https://cf.10xgenomics.com/x/a_filtered_feature_bc_matrix.h5"
URL_B = "https://cf.10xgenomics.com/x/b_analysis.tar.gz"
URL_C = "https://cf.10xgenomics.com/x/c_filtered_feature_bc_matrix.h5"


@pytest.fixture
def fake_downloads(monkeypatch):
    """三个假数据集：A/C primary 是 .h5，B 是 .tar.gz（类型分发用）。"""
    registry = {
        "10x:A": _rec("10x:A", "a_filtered_feature_bc_matrix.h5", URL_A),
        "10x:B": _rec("10x:B", "b_analysis.tar.gz", URL_B),
        "10x:C": _rec("10x:C", "c_filtered_feature_bc_matrix.h5", URL_C),
    }
    monkeypatch.setattr(DE.downloads, "get", lambda uid: registry.get(uid))
    monkeypatch.setattr(DP.downloads, "get", lambda uid: registry.get(uid))
    monkeypatch.setattr(DP.downloads, "files_for",
                        lambda uid: registry[uid]["files"] if uid in registry else [])
    monkeypatch.setattr(DP.downloads, "primary_url",
                        lambda uid: registry[uid]["primary_download_url"] if uid in registry else None)
    monkeypatch.setattr(DP.downloads, "fastq_url", lambda uid: None)
    monkeypatch.setattr(DP.downloads, "is_available", lambda: True)
    return registry


# ---------------------------------------------------------------- scanpy 可选依赖闸（fail-closed）

def test_missing_scanpy_fail_closed(monkeypatch, capsys, tmp_path):
    """sys.modules[name]=None 会让 import 抛 ImportError——等价于 scanpy 未安装。"""
    monkeypatch.setitem(sys.modules, "scanpy", None)
    assert LS.require_scanpy() is None
    rc = LS.main(["--dest", str(tmp_path / "dest")])
    assert rc == 2
    err = capsys.readouterr().err
    assert "requirements-loadsmoke.txt" in err and "scanpy" in err
    assert not (tmp_path / "dest").exists()     # fail-closed：一个文件都没下


# ---------------------------------------------------------------- 分层抽样

def _fake_by_uid():
    """三个平台、规模悬殊的假清单（含一个 primary 非 .h5 的，应被 eligibility 排除）。"""
    by = {}
    for i in range(30):
        by[f"chromium-{i:02d}"] = _rec(f"chromium-{i:02d}", "m.h5",
                                       f"https://cf.10xgenomics.com/c/{i}.h5")
    for i in range(10):
        by[f"visium-{i:02d}"] = _rec(f"visium-{i:02d}", "m.h5",
                                     f"https://cf.10xgenomics.com/v/{i}.h5",
                                     platform="10x Genomics Visium")
    for i in range(5):
        by[f"xenium-{i:02d}"] = _rec(f"xenium-{i:02d}", "m.h5",
                                     f"https://cf.10xgenomics.com/x/{i}.h5",
                                     platform="10x Genomics Xenium")
    by["not-h5"] = _rec("not-h5", "m.tar.gz", "https://cf.10xgenomics.com/z/m.tar.gz")
    return by


def test_eligibility_only_primary_h5():
    elig = LS.eligible_uids(_fake_by_uid())
    assert len(elig) == 45 and "not-h5" not in elig
    assert elig["visium-00"] == "10x Genomics Visium"


def test_stratified_sample_deterministic_same_seed():
    by = _fake_by_uid()
    a = LS.stratified_sample(by, sample=20, seed=42)
    b = LS.stratified_sample(by, sample=20, seed=42)
    assert a == b and len(a) == 20 and len(set(a)) == 20


def test_stratified_sample_every_platform_represented():
    by = _fake_by_uid()
    picked = LS.stratified_sample(by, sample=20, seed=42)
    plats = {p.split("-")[0] for p in picked}
    assert plats == {"chromium", "visium", "xenium"}     # 小层不被大层挤没
    # 比例分配：大层名额严格多于小层
    assert sum(p.startswith("chromium") for p in picked) > sum(p.startswith("xenium") for p in picked)


def test_stratified_sample_caps_at_eligible():
    by = _fake_by_uid()
    picked = LS.stratified_sample(by, sample=999, seed=1)
    assert len(picked) == 45


def test_allocate_edge_total_below_strata():
    assert sum(LS._allocate([10, 5, 1], 2)) == 2
    assert LS._allocate([10, 5, 1], 2)[0] == 1           # 名额保最大的层


def test_real_manifest_sample_shape():
    """真清单（在仓、离线可读）：默认参数抽 60 个，全部 primary .h5，跨多个 platform。"""
    by = LS.load_links()
    assert len(by) > 0
    picked = LS.stratified_sample(by)
    assert len(picked) == LS.DEFAULT_SAMPLE == 60
    assert all((by[u].get("primary_filename") or "").endswith(".h5") for u in picked)
    assert len({by[u].get("platform") for u in picked}) >= 2


# ---------------------------------------------------------------- 类型分发与分级

def test_dispatch_h5_loaded_and_tar_skipped(tmp_path, fake_downloads):
    report = LS.run_smoke(["10x:A", "10x:B"], str(tmp_path / "out"),
                          readers={".h5": lambda p: _fake_adata()},
                          opener=_opener_ok(), sleep=lambda s: None)
    a, b = report["results"]
    assert a["load_status"] == LS.LOADED
    assert a["shape"] == [100, 2000] and a["n_obs"] == 100 and a["n_vars"] == 2000
    assert b["load_status"] == LS.SKIPPED_UNSUPPORTED and "诚实降级" in b["note"]
    assert report["schema"] == LS.SCHEMA
    assert report["counts"][LS.LOADED] == 1 and report["counts"][LS.SKIPPED_UNSUPPORTED] == 1


def test_load_failed_records_reason(tmp_path, fake_downloads):
    def boom(path):
        raise ValueError("not a 10x h5 file")
    report = LS.run_smoke(["10x:A"], str(tmp_path / "out"),
                          readers={".h5": boom}, opener=_opener_ok(), sleep=lambda s: None)
    r = report["results"][0]
    assert r["load_status"] == LS.LOAD_FAILED
    assert "ValueError" in r["error"] and "not a 10x h5" in r["error"]
    assert r["download_status"] == DE.STATUS_OK      # 下载核对是通过的，失败发生在加载


def test_download_failed_grade(tmp_path, fake_downloads):
    report = LS.run_smoke(["10x:A"], str(tmp_path / "out"),
                          readers={".h5": lambda p: _fake_adata()},
                          opener=_opener_http_error(404), sleep=lambda s: None)
    r = report["results"][0]
    assert r["load_status"] == LS.DOWNLOAD_FAILED
    assert r["download_status"] == DE.STATUS_UNREACHABLE and r["http_status"] == 404


def test_corrupt_download_not_loaded(tmp_path, fake_downloads):
    """md5 对不上（.corrupt 留证据）的文件不下加载结论：skipped_unsupported + 注明原因。"""
    def opener_bad_md5(url, timeout):
        return FakeResp(b"tampered-content", 200)
    report = LS.run_smoke(["10x:A"], str(tmp_path / "out"),
                          readers={".h5": lambda p: _fake_adata()},
                          opener=opener_bad_md5, sleep=lambda s: None)
    r = report["results"][0]
    assert r["download_status"] == DE.STATUS_MD5_MISMATCH
    assert r["load_status"] == LS.SKIPPED_UNSUPPORTED and "corrupt" in r["note"]


# ---------------------------------------------------------------- 台账 "l" 槽：写入 → 读取派生 → patrol 保留

def _run_and_record(tmp_path, fake_downloads, *, iso="2026-08-01T00:00:00Z"):
    """真冒烟（假网+假 reader：A  loaded、C load_failed）→ 报告落盘 → 回写 tmp 台账。"""
    def reader(path):
        if "c_filtered" in str(path):
            raise ValueError("boom")
        return _fake_adata()
    report = LS.run_smoke(["10x:A", "10x:C"], str(tmp_path / "out"),
                          readers={".h5": reader}, opener=_opener_ok(), sleep=lambda s: None)
    report_path = tmp_path / "loadsmoke.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")

    ledger = tmp_path / "ledger"
    fake_manifest = {uid: [(P.norm(f["download_url"]), f["bytes"]) for f in rec["files"]]
                     for uid, rec in fake_downloads.items()}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    rc = REC.record(str(report_path), out_dir=str(ledger), snapshot_iso=iso)
    monkeypatch.undo()
    assert rc == 0
    return report, ledger


def test_record_writes_load_slot(tmp_path, fake_downloads):
    _, ledger = _run_and_record(tmp_path, fake_downloads)
    snaps = list((ledger / "snapshots").glob("loadsmoke-*.jsonl"))
    assert len(snaps) == 1
    lines = snaps[0].read_text(encoding="utf-8").strip().split("\n")
    meta = json.loads(lines[0])["_meta"]
    assert meta["schema"] == P.SCHEMA and meta["source"] == "loadsmoke"

    cur = json.loads((ledger / "current.json").read_text(encoding="utf-8"))
    vec_a = cur["by_uid"]["10x:A"]["f"][P.norm(URL_A)]
    vec_c = cur["by_uid"]["10x:C"]["f"][P.norm(URL_C)]
    assert vec_a["l"] == "loaded" and vec_c["l"] == "failed"
    # 下载证据同帧落盘：reach/size/integrity 与 provision 同口径
    assert vec_a["r"] == "ok" and vec_a["s"] == "match" and vec_a["i"] == "verified"
    assert cur["by_uid"]["10x:A"]["np"] == 0 and cur["by_uid"]["10x:C"]["np"] == 1
    assert cur["totals"]["load_loaded"] == 1 and cur["totals"]["load_failed"] == 1
    assert cur["totals"]["problem"] == 1


def test_record_read_side_derives_load(tmp_path, fake_downloads, monkeypatch):
    _, ledger = _run_and_record(tmp_path, fake_downloads)
    monkeypatch.setattr(I, "_DATA_PATH", str(ledger / "current.json"))
    I._load.cache_clear()
    try:
        ok = I.status_for("10x:A", URL_A)
        assert ok["load"] == "loaded" and ok["problem"] is False
        bad = I.status_for("10x:C", URL_C)
        assert bad["load"] == "failed" and bad["problem"] is True
        assert "加载" in bad["problem_reason"] and "2026-08-01" in bad["problem_reason"]
        summ = I.dataset_summary("10x:C")
        assert summ["n_problem"] == 1 and summ["provisioning_ok"] is False
    finally:
        I._load.cache_clear()


def test_patrol_reprobe_preserves_load_slot(tmp_path, fake_downloads):
    """patrol 只探存活/大小（results 不带 load）→ 不抹掉已实测的 "l"（与 "i" 同理）。"""
    _, ledger = _run_and_record(tmp_path, fake_downloads)
    prior = json.loads((ledger / "current.json").read_text(encoding="utf-8"))
    manifest = {uid: [(P.norm(f["download_url"]), f["bytes"]) for f in rec["files"]]
                for uid, rec in fake_downloads.items()}
    patrol_results = {}
    for uid, files in manifest.items():
        for url, b in files:
            patrol_results[f"{uid}\t{url}"] = {"reach": "ok", "http": 200, "srv": b, "size": "match"}
    current2, _, _, tally = P.rebuild_current(manifest, prior, patrol_results,
                                              "2026-08-02T00:00:00Z", source="patrol")
    vec_a = current2["by_uid"]["10x:A"]["f"][P.norm(URL_A)]
    vec_c = current2["by_uid"]["10x:C"]["f"][P.norm(URL_C)]
    assert vec_a["l"] == "loaded" and vec_c["l"] == "failed"     # 保留
    assert vec_a["v"] == "2026-08-02"                            # 存活维照常刷新
    assert tally["problem"] == 1                                 # load failed 仍是 problem


def test_skipped_unsupported_leaves_no_load_slot(tmp_path, fake_downloads):
    """skipped_unsupported / download_failed 没产生加载结论 → 向量不留 "l" 键（= 未实测）。"""
    report = LS.run_smoke(["10x:B"], str(tmp_path / "out"),
                          readers={".h5": lambda p: _fake_adata()},
                          opener=_opener_ok(), sleep=lambda s: None)
    assert report["results"][0]["load_status"] == LS.SKIPPED_UNSUPPORTED
    report_path = tmp_path / "loadsmoke.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    fake_manifest = {uid: [(P.norm(f["download_url"]), f["bytes"]) for f in rec["files"]]
                     for uid, rec in fake_downloads.items()}
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(P, "load_manifest", lambda: fake_manifest)
    rc = REC.record(str(report_path), out_dir=str(tmp_path / "ledger"),
                    snapshot_iso="2026-08-01T00:00:00Z")
    monkeypatch.undo()
    assert rc == 0
    cur = json.loads((tmp_path / "ledger" / "current.json").read_text(encoding="utf-8"))
    vec_b = cur["by_uid"]["10x:B"]["f"][P.norm(URL_B)]
    assert "l" not in vec_b and vec_b["r"] == "ok"               # 下载证据落、load 维不留键


def test_record_rejects_unknown_schema(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "something-else/v9", "results": []}), encoding="utf-8")
    assert REC.record(str(bad), out_dir=str(tmp_path / "l")) == 2
