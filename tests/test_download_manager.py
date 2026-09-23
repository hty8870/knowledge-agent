# -*- coding: utf-8 -*-
"""服务端真下载管理器（download_manager）+ `/api/download/*` 端点契约测试。

全程禁网，与 test_download_executor 同一纪律：`opener` 是唯一网络接缝，测试注入假响应 /
假异常；集成连通性由巡逻脚本 patrol_links 与人工 sanity 覆盖，本文件不触网。

覆盖：
- plan 分档（manager 级纯函数 + webapp 级真实语料）：10x 台账 supported / CELLxGENE supported /
  SCP 直链 supported / GEO·10x-synced unsupported+reason / 语料查不到 unsupported；
- start→status→done 全流程（假文件流、逐数据集子文件夹、README/manifest 落盘、md5 核验）；
- 取消：chunk 间取消 → cancelled + .part 保留（可续传语义）；
- 磁盘不足拒绝（monkeypatch disk_usage）；并发第二 job → 409；坏入参 → 400；乱序 status → 404；
- 白名单不变量：计划里每一行的主机都在 allowed_hosts 内（download_one 再逐行复核）。
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
import time
import urllib.error
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_manager as DM  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


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


class BlockingResp(FakeResp):
    """read() 阻塞在 gate 上——测试用它把下载线程钉在「第一块」处。
    `entered`（可选）在第一次 read() 进入时 set，测试据此确认线程确实已进 read，
    避免「取消发生在首块写入前」的竞态。"""

    def __init__(self, data: bytes, gate: threading.Event, entered: "threading.Event | None" = None,
                 status: int = 200):
        super().__init__(data, status)
        self._gate = gate
        self._entered = entered

    def read(self, n=-1):
        if self._entered is not None:
            self._entered.set()
        assert self._gate.wait(timeout=15), "gate 等待超时（测试线程应 set）"
        return super().read(n)


def _opener(data: bytes, status: int = 200):
    def open_(url, timeout):
        return FakeResp(data, status)
    return open_


def _opener_map(by_url: dict):
    """按 URL 分发不同内容的 opener：多文件流测试用。"""

    def open_(url, timeout):
        return FakeResp(by_url[url])
    return open_


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


# ---------------------------------------------------------------- 假记录

def _record(uid, *, url, download_url, filesize=None, source="CELLxGENE Discover", name=None):
    return {"dataset_uid": uid, "url": url, "download_url": download_url,
            "filesize": filesize, "source": source, "dataset_name": name or uid}


def _records(*records) -> dict:
    return {r["dataset_uid"]: r for r in records}


H5AD_URL = "https://datasets.cellxgene.cziscience.com/731e83e8-93ff-4018-84fd-61e7258d6d55.h5ad"
CXG_PAGE = "https://cellxgene.cziscience.com/e/24921392-22ed-479a-9144-7d40adf148ae.cxg/"

#: 真实语料样例 uid（只读 database/ 与 data/，零网络）
UID_10X = "multiome-gemx-5k-mouse-kidney"          # 台账覆盖 → checksum_verifiable
UID_CXG = "cxg:24921392-22ed-479a-9144-7d40adf148ae"  # CELLxGENE h5ad 直链
UID_SCP = "scp:SCP1257"                            # SCP 直链 + filesize
UID_GEO = "geo:GSE3642"                            # 只有页面地址
UID_SYNCED_10X = "10x:flexv2-4plex-rat-liver-kidney-intest-testis"  # base 有、台账无


def _client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _wait_state(job_id: str, wanted=("done", "cancelled", "error"), timeout: float = 10.0) -> dict:
    """等任务到达 wanted 状态**且线程已退出**（finished_at 落盘）。

    cancel_job 会立即把 state 置为 cancelled——单看 state 会在 worker 真正收尾前
    （还在写 .part / 更新文件状态）就返回，导致断言抓到半成品。finished_at 由线程
    finally 写，是「线程真结束」的可靠信号。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        st = DM.get_status(job_id)
        assert st is not None, f"job {job_id} 不在注册表"
        if st["state"] in wanted and st["finished_at"]:
            return st
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} 在 {timeout}s 内没有到达 {wanted}，当前 {DM.get_status(job_id)}")


@pytest.fixture(autouse=True)
def _cleanup_jobs():
    """每个测试结束把可能遗留的运行中任务取消/等它收尾，避免跨测试串扰。"""
    yield
    for job in list(DM._JOBS.values()):
        if job.get("state") == DM.STATE_RUNNING or job.get("cancel_requested"):
            DM.cancel_job(job["job_id"])
            _wait_state(job["job_id"], timeout=8)
    DM.bind_activity_callback(None)


@pytest.fixture(autouse=True)
def _no_real_downloads_pollution():
    """回归钉：本文件任何测试都**不得在真实 ~/Downloads 新建 BioData数据-* 目录**。

     批留下 8 个空目录（BioData数据-2026*）的根源：start_job 先 `base.mkdir` 再磁盘预检，
    507（disk_space_insufficient）路径会在真实下载目录留下空目录。 已把 start_job 改成
    「先预检、后建目录」（预检用父目录做、同卷等价），这里再把「测试不污染真实下载目录」
    钉成回归断言——将来任何测试忘记传 out_dir / 又走到真实目录创建路径，立即变红。
    """
    home = Path.home() / "Downloads"
    before = set(p.name for p in home.glob("BioData数据-*")) if home.is_dir() else set()
    yield
    after = set(p.name for p in home.glob("BioData数据-*")) if home.is_dir() else set()
    assert after <= before, f"测试在真实下载目录新建了目录：{sorted(after - before)}（必须改用 tmp_path / out_dir）"


# ---------------------------------------------------------------- plan：manager 级纯函数

def test_plan_size_only_synth_row_supported():
    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=123456)
    plan = DM.build_download_plan(["cxg:a"], records=_records(rec))
    assert len(plan["items"]) == 1 and not plan["unsupported"]
    it = plan["items"][0]
    assert it["tier"] == "size_only"
    assert it["bytes"] == 123456
    assert it["files"] == [{"filename": "731e83e8-93ff-4018-84fd-61e7258d6d55.h5ad",
                            "url": H5AD_URL, "bytes": 123456}]
    assert plan["total_bytes"] == 123456
    assert plan["allowed_hosts"] == ["datasets.cellxgene.cziscience.com"]
    assert len(plan["rows"]) == 1 and plan["rows"][0]["verify"] == "size"


def test_plan_page_only_unsupported_with_reason():
    rec = _record("geo:x", url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=X",
                  download_url="", source="NCBI GEO", name="GEO 数据集")
    plan = DM.build_download_plan(["geo:x"], records=_records(rec))
    assert not plan["items"] and plan["total_bytes"] == 0
    assert plan["unsupported"][0]["dataset_uid"] == "geo:x"
    assert "任务包" in plan["unsupported"][0]["reason"]


def test_plan_direct_unsized_unsupported():
    """有与页面不同的直链但没有大小 → direct_unsized，不生成下载行。"""
    rec = _record("src:y", url="https://example.org/dataset", download_url="https://example.org/raw-file",
                  filesize=None, source="示例源")
    plan = DM.build_download_plan(["src:y"], records=_records(rec))
    assert not plan["items"] and not plan["rows"]
    assert plan["unsupported"][0]["reason"]


def test_plan_unknown_uid_unsupported():
    plan = DM.build_download_plan(["ghost:uid"], records=_records())
    assert not plan["items"]
    assert plan["unsupported"][0]["reason"] == "本机语料中没有这个数据集编号，无法提供下载信息。"


def test_plan_dedupes_and_skips_blank():
    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=10)
    plan = DM.build_download_plan(["cxg:a", "cxg:a", "", "  "], records=_records(rec))
    assert len(plan["items"]) == 1


def test_plan_row_hosts_always_inside_allowed_hosts():
    """白名单不变量：计划里每一行下载 URL 的主机都 ∈ allowed_hosts（download_one 再逐行复核）。
    语料查不到的编号不进计划，页面地址兜底不会变成下载行。"""
    cxg = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=123456)
    geo = _record("geo:x", url="https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=X",
                  download_url="", source="NCBI GEO")
    plan = DM.build_download_plan(["cxg:a", "geo:x", "ghost"], records=_records(cxg, geo))
    from urllib.parse import urlsplit
    hosts = {h.lower() for h in plan["allowed_hosts"]}
    for row in plan["rows"]:
        assert urlsplit(row["download_url"]).hostname.lower() in hosts
    assert not any(r["dataset_uid"] in ("geo:x", "ghost") for r in plan["rows"])
    assert len(plan["unsupported"]) == 2


# ---------------------------------------------------------------- webapp 端点：plan 分档（真实语料）

def test_plan_endpoint_10x_ledger_checksum_supported():
    r = _client().post("/api/download/plan", json={"uids": [UID_10X]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and len(body["items"]) == 1 and not body["unsupported"]
    it = body["items"][0]
    assert it["tier"] == "checksum_verifiable"
    assert it["files"] and all(f["bytes"] > 0 for f in it["files"])
    assert body["total_bytes"] == sum(f["bytes"] for f in it["files"])


def test_plan_endpoint_cellxgene_size_only_supported():
    r = _client().post("/api/download/plan", json={"uids": [UID_CXG]})
    assert r.status_code == 200
    body = r.json()
    it = body["items"][0]
    assert it["tier"] == "size_only"
    assert it["files"][0]["filename"].endswith(".h5ad")
    assert body["total_bytes"] > 0


def test_plan_endpoint_scp_direct_link_supported():
    r = _client().post("/api/download/plan", json={"uids": [UID_SCP]})
    assert r.status_code == 200
    body = r.json()
    assert body["items"] and body["items"][0]["tier"] == "size_only"


def test_plan_endpoint_geo_and_uncovered_10x_unsupported():
    r = _client().post("/api/download/plan", json={"uids": [UID_GEO, UID_SYNCED_10X]})
    assert r.status_code == 200
    body = r.json()
    assert not body["items"]
    assert {u["dataset_uid"] for u in body["unsupported"]} == {UID_GEO, UID_SYNCED_10X}
    for u in body["unsupported"]:
        assert u["reason"] and "任务包" in u["reason"]


def test_plan_endpoint_mixed_supported_and_unsupported():
    r = _client().post("/api/download/plan", json={"uids": [UID_10X, UID_GEO]})
    body = r.json()
    assert {it["dataset_uid"] for it in body["items"]} == {UID_10X}
    assert {u["dataset_uid"] for u in body["unsupported"]} == {UID_GEO}


def test_plan_endpoint_bad_params_400():
    c = _client()
    for payload in ({"uids": []}, {"uids": "abc"}, {"uids": [123]}, {"uids": ["  "]},
                    {}, {"uids": None}):
        r = c.post("/api/download/plan", json=payload)
        assert r.status_code == 400, f"{payload!r} 应 400，实际 {r.status_code}"


# ---------------------------------------------------------------- start→status→done 全流程

def test_start_done_flow_with_dataset_subfolders(tmp_path):
    cxg = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL,
                  filesize=len(b"A" * 3000), name="测试数据集甲")
    scp = _record("scp:b", url="https://singlecell.broadinstitute.org/studies/b",
                  download_url="https://singlecell.broadinstitute.org/download/b.h5ad",
                  filesize=len(b"B" * 5000), source="Broad Single Cell Portal", name="测试数据集乙")
    uids = ["cxg:a", "scp:b"]
    job = DM.start_job(uids, records=_records(cxg, scp), out_dir=str(tmp_path),
                       opener=_opener_map({H5AD_URL: b"A" * 3000,
                                           "https://singlecell.broadinstitute.org/download/b.h5ad": b"B" * 5000}))
    st = _wait_state(job["job_id"])
    assert st["state"] == "done" and st["dir"] == str(tmp_path)
    assert st["total_bytes"] == 8000 and st["done_bytes"] == 8000

    subdirs = sorted(p.name for p in tmp_path.iterdir() if p.is_dir())
    assert len(subdirs) == 2
    assert any(s.startswith("cxg_a__测试数据集甲") for s in subdirs), subdirs
    assert any(s.startswith("scp_b__测试数据集乙") for s in subdirs), subdirs
    # 每个数据集一个子文件夹，文件直接落在里面
    for sub in subdirs:
        files = list((tmp_path / sub).iterdir())
        assert len(files) == 1
    (tmp_path / subdirs[0] / "731e83e8-93ff-4018-84fd-61e7258d6d55.h5ad").read_bytes() == b"A" * 3000

    for f in st["files"]:
        assert f["status"] == "size_ok" and f["done_bytes"] == f["bytes"]

    readme = (tmp_path / "README.txt").read_text(encoding="utf-8")
    assert "测试数据集甲" in readme and "size_ok" in readme and "校验结果含义" in readme
    manifest = (tmp_path / "manifest.tsv").read_text(encoding="utf-8")
    lines = manifest.strip().splitlines()
    assert lines[0].startswith("dataset_uid\t")
    assert len(lines) == 3  # 表头 + 2 个文件
    assert all("\tsize_ok\t" in line for line in lines[1:])


def test_start_md5_verified_flow(monkeypatch, tmp_path):
    """checksum_verifiable 档（10x 台账宇宙）：假台账 + 假文件流，断言 md5 核验 ok。"""
    uid = "10x:fake"
    content = b"single-cell-matrix-bytes" * 100
    file_url = "https://cf.10xgenomics.com/x/fake.h5"
    ledger = {"dataset_uid": uid, "url": f"https://www.10xgenomics.com/datasets/{uid}",
              "files": [{"filename": "fake.h5", "download_url": file_url,
                         "bytes": len(content), "md5sum": _md5(content)}]}
    monkeypatch.setattr(DM.downloads, "get", lambda u: ledger)
    monkeypatch.setattr(DM.downloads, "files_for", lambda u: ledger["files"])
    monkeypatch.setattr(DM.downloads, "primary_url", lambda u: file_url)
    rec = _record(uid, url=ledger["url"], download_url=file_url, source="10x Genomics")
    job = DM.start_job([uid], records=_records(rec), out_dir=str(tmp_path), opener=_opener(content))
    st = _wait_state(job["job_id"])
    assert st["state"] == "done"
    assert st["files"][0]["status"] == "ok"
    assert st["files"][0]["md5_actual"] == _md5(content)
    manifest = (tmp_path / "manifest.tsv").read_text(encoding="utf-8")
    assert "\tok\t" in manifest and _md5(content) in manifest
    assert (tmp_path / "README.txt").read_text(encoding="utf-8").count("md5 一致 1")


def test_cancel_mid_download_keeps_part(tmp_path):
    entered = threading.Event()
    gate = threading.Event()
    content = b"Z" * (3 * 1024 * 1024)  # 3 MiB → 3 个 1 MiB 块

    def open_(url, timeout):
        return BlockingResp(content, gate, entered)

    rec = _record("cxg:c", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(content))
    job = DM.start_job(["cxg:c"], records=_records(rec), out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10), "下载线程没有进入 read()"
    DM.cancel_job(job["job_id"])
    gate.set()
    st = _wait_state(job["job_id"], wanted=("cancelled",))
    assert st["state"] == "cancelled"
    assert st["files"][0]["status"] == "cancelled"
    assert st["done_bytes"] == 1024 * 1024  # 只写了第一块
    parts = list(tmp_path.glob("**/*.part"))
    assert len(parts) == 1 and parts[0].read_bytes() == content[:1024 * 1024]


def test_download_activity_callback_tracks_real_worker_lifetime(tmp_path):
    """壳层关窗提示必须由真实 worker 边沿驱动，不是只在测试手动置位。"""
    entered = threading.Event()
    gate = threading.Event()
    content = b"A" * (2 * 1024 * 1024)
    events: list[bool] = []
    DM.bind_activity_callback(events.append)
    assert events == [False]

    def open_(url, timeout):
        return BlockingResp(content, gate, entered)

    rec = _record("cxg:active", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(content))
    job = DM.start_job(
        ["cxg:active"], records=_records(rec), out_dir=str(tmp_path), opener=open_
    )
    assert entered.wait(timeout=10)
    assert events[-1] is True
    DM.cancel_job(job["job_id"])
    gate.set()
    _wait_state(job["job_id"], wanted=("cancelled",))
    assert events[-1] is False


def test_cancel_between_files_keeps_done_and_marks_rest_pending(tmp_path):
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL,
                filesize=len(b"a"), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")
    c = _record("cxg:c", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d57"),
                filesize=len(b"c"), name="丙")
    calls = []
    gate = threading.Event()

    def open_(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return FakeResp(b"a")          # 第一个文件正常完成 → size_ok
        return BlockingResp(b"b", gate)    # 第二个文件钉在 read（第三个从未开始）

    job = DM.start_job(["cxg:a", "cxg:b", "cxg:c"], records=_records(a, b, c),
                       out_dir=str(tmp_path), opener=open_)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        st = DM.get_status(job["job_id"])
        if st and st["files"] and st["files"][1]["status"] == "downloading":
            break
        time.sleep(0.01)
    DM.cancel_job(job["job_id"])
    gate.set()
    st = _wait_state(job["job_id"], wanted=("cancelled",))
    assert st["files"][0]["status"] == "size_ok"
    assert st["files"][1]["status"] == "cancelled"
    assert st["files"][2]["status"] == "pending"


# ---------------------------------------------------------------- 磁盘 / 并发 / 取消 / 404

def test_run_job_passes_defaults_when_no_injection(tmp_path, monkeypatch):
    """集成回归钉（sanity 抓到）：管理器不注入 opener/sleep 时，必须让 download_one
    落到它自己的默认值（_open_stream / time.sleep）——显式传 None 会把默认参数覆盖成
    "TypeError: 'NoneType' object is not callable"，而测试全注入 opener，只有集成才现形。"""
    captured = {}

    def fake_download_one(row, out_root, allowed, **kwargs):
        captured.update(kwargs)
        return DE.FileResult(dataset_uid=row["dataset_uid"], safe_uid=row["safe_uid"],
                             filename=row["filename"], safe_name=row["safe_name"],
                             url=row["download_url"], status=DE.STATUS_SIZE_OK,
                             bytes_downloaded=10, expected_bytes=10)

    monkeypatch.setattr(DM.DE, "download_one", fake_download_one)
    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=10)
    job = DM.start_job(["cxg:a"], records=_records(rec), out_dir=str(tmp_path))
    st = _wait_state(job["job_id"])
    assert st["state"] == "done" and st["files"][0]["status"] == "size_ok"
    assert "opener" not in captured and "sleep" not in captured
    assert captured["subdir"].startswith("cxg_a__")


def test_disk_space_insufficient_rejected(monkeypatch, tmp_path):
    from types import SimpleNamespace
    monkeypatch.setattr(DM.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=1000, total=10 ** 12, used=10 ** 12))
    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=10 ** 9)
    with pytest.raises(DM.DownloadManagerError) as e:
        DM.start_job(["cxg:a"], records=_records(rec), out_dir=str(tmp_path))
    assert e.value.code == "disk_space_insufficient"


def test_second_job_conflict_409(tmp_path):
    gate = threading.Event()
    content = b"Q" * (2 * 1024 * 1024)

    def open_(url, timeout):
        return BlockingResp(content, gate)

    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(content))
    job1 = DM.start_job(["cxg:a"], records=_records(rec), out_dir=str(tmp_path), opener=open_)
    try:
        with pytest.raises(DM.DownloadManagerError) as e:
            DM.start_job(["cxg:a"], records=_records(rec), out_dir=str(tmp_path), opener=open_)
        assert e.value.code == "job_conflict"
    finally:
        DM.cancel_job(job1["job_id"])
        gate.set()
    _wait_state(job1["job_id"], wanted=("cancelled",))


def test_status_unknown_job_404():
    r = _client().get("/api/download/status", params={"job": "dl-ghost"})
    assert r.status_code == 404


def test_cancel_endpoint_ok_and_404_and_400(tmp_path):
    gate = threading.Event()
    content = b"W" * (2 * 1024 * 1024)

    def open_(url, timeout):
        return BlockingResp(content, gate)

    rec = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(content))
    job = DM.start_job(["cxg:a"], records=_records(rec), out_dir=str(tmp_path), opener=open_)
    try:
        r = _client().post("/api/download/cancel", json={"job_id": job["job_id"]})
        assert r.status_code == 200
        assert r.json()["state"] == "cancelled"
        r = _client().post("/api/download/cancel", json={"job_id": "dl-ghost"})
        assert r.status_code == 404
        r = _client().post("/api/download/cancel", json={"job_id": ""})
        assert r.status_code == 400
        r = _client().post("/api/download/cancel", json={})
        assert r.status_code == 400
    finally:
        gate.set()
    _wait_state(job["job_id"], wanted=("cancelled",))


def test_start_no_downloadable_400():
    r = _client().post("/api/download/start", json={"uids": [UID_GEO]})
    assert r.status_code == 400
    body = r.json()
    assert body["code"] == "no_downloadable" and "任务包" in body["message_zh"]


def test_start_bad_params_400():
    c = _client()
    for payload in ({"uids": []}, {"uids": "abc"}, {"uids": [1]}, {}, {"uids": None}):
        r = c.post("/api/download/start", json=payload)
        assert r.status_code == 400, f"{payload!r} 应 400，实际 {r.status_code}"


def test_start_endpoint_disk_insufficient_507(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(DM.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=1000, total=10 ** 12, used=10 ** 12))
    # 真实语料里挑一个 filesize 足够大的 CELLxGENE 数据集（只读，零网络；磁盘预检先拒绝）
    r = _client().post("/api/download/start", json={"uids": [UID_CXG]})
    assert r.status_code == 507
    assert r.json()["code"] == "disk_space_insufficient"


# ---------------------------------------------------------------- 在途增删（/api/download/update）

def test_update_add_appends_new_dataset_to_running_job(tmp_path):
    gate = threading.Event()
    entered = threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")

    def open_(url, timeout):
        if url == H5AD_URL:
            return BlockingResp(b"a", gate, entered)   # 首文件钉在 read → job 保持 running
        return FakeResp(b"b")                          # 追加的 b 正常完成

    job = DM.start_job(["cxg:a"], records=_records(a, b), out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10), "下载线程没有进入 read()"
    r = DM.update_job(add=["cxg:b"], records=_records(a, b))
    assert r["job_id"] == job["job_id"]
    assert [x["dataset_uid"] for x in r["added"] if x["status"] == "added"] == ["cxg:b"]
    gate.set()
    st = _wait_state(job["job_id"])
    assert st["state"] == "done"
    assert {f["dataset_uid"] for f in st["files"]} == {"cxg:a", "cxg:b"}
    assert all(f["status"] == "size_ok" for f in st["files"])


def test_update_add_dedupes_and_reports_unsupported(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    geo = _record("geo:x", url="https://www.ncbi.nlm.nih.gov/geo/x", download_url="",
                  source="NCBI GEO", name="下不了的数据集")

    def open_(url, timeout):
        return BlockingResp(b"a", gate, entered)

    job = DM.start_job(["cxg:a"], records=_records(a, geo), out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10)
    r = DM.update_job(add=["cxg:a", "geo:x"], records=_records(a, geo))
    assert [x["dataset_uid"] for x in r["added"] if x["status"] == "already_in_queue"] == ["cxg:a"]
    assert [x["dataset_uid"] for x in r["added_unsupported"]] == ["geo:x"]
    gate.set()
    _wait_state(job["job_id"])


def test_update_remove_pending_dataset_is_skipped(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")
    c = _record("cxg:c", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d57"),
                filesize=len(b"c"), name="丙")
    calls = []

    def open_(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return BlockingResp(b"a", gate, entered)
        return FakeResp(b"b")

    job = DM.start_job(["cxg:a", "cxg:b", "cxg:c"], records=_records(a, b, c),
                       out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10)
    r = DM.update_job(remove=["cxg:c"])
    assert any(x["dataset_uid"] == "cxg:c" and x["outcome"] == "skipped" for x in r["removed"])
    gate.set()
    st = _wait_state(job["job_id"])
    by = {f["dataset_uid"]: f for f in st["files"]}
    assert by["cxg:a"]["status"] == "size_ok" and by["cxg:b"]["status"] == "size_ok"
    assert by["cxg:c"]["status"] == "skipped"


def test_update_preflight_failure_leaves_job_untouched(monkeypatch, tmp_path):
    """remove+add 同批且磁盘预检失败 → 任务零变更（remove 不先生效）：预检必须先于一切变更。"""
    from types import SimpleNamespace
    gate, entered = threading.Event(), threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    c = _record("cxg:c", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d57"),
                filesize=len(b"c"), name="丙")
    big = _record("cxg:big", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d58"),
                  filesize=10 ** 9, name="大件")

    def open_(url, timeout):
        return BlockingResp(b"a", gate, entered)

    job = DM.start_job(["cxg:a", "cxg:c"], records=_records(a, c, big),
                       out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10)
    monkeypatch.setattr(DM.shutil, "disk_usage",
                        lambda p: SimpleNamespace(free=1000, total=10 ** 12, used=10 ** 12))
    with pytest.raises(DM.DownloadManagerError) as e:
        DM.update_job(remove=["cxg:c"], add=["cxg:big"], records=_records(a, c, big))
    assert e.value.code == "disk_space_insufficient"
    st = DM.get_status(job["job_id"])
    assert all(f["status"] == "pending" for f in st["files"] if f["dataset_uid"] == "cxg:c")
    assert all(f["dataset_uid"] != "cxg:big" for f in st["files"])
    gate.set()
    _wait_state(job["job_id"])


def test_update_remove_downloading_aborts_cleans_part_and_continues(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    content = b"Z" * (3 * 1024 * 1024)   # 3 MiB → 3 块；首块后钉住
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(content), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")
    calls = []

    def open_(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return BlockingResp(content, gate, entered)
        return FakeResp(b"b")

    job = DM.start_job(["cxg:a", "cxg:b"], records=_records(a, b), out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10)
    r = DM.update_job(remove=["cxg:a"])
    assert any(x["dataset_uid"] == "cxg:a" and x["outcome"] == "aborted" for x in r["removed"])
    gate.set()
    st = _wait_state(job["job_id"])
    assert st["state"] == "done", "移除一条正在下载的数据集不应把整个任务取消"
    by = {f["dataset_uid"]: f for f in st["files"]}
    assert by["cxg:a"]["status"] == "skipped"
    assert by["cxg:b"]["status"] == "size_ok"
    # 未完成部分被清理：只剩 b 的子目录，无 .part 残留
    subdirs = [p.name for p in tmp_path.iterdir() if p.is_dir()]
    assert len(subdirs) == 1, subdirs
    assert not list(tmp_path.glob("**/*.part"))


def test_update_remove_completed_dataset_is_rejected(tmp_path):
    gate, entered = threading.Event(), threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")
    calls = []

    def open_(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            return FakeResp(b"a")                    # a 立即完成 → 文件已落盘
        return BlockingResp(b"b", gate, entered)     # b 钉住 → job 仍 running

    job = DM.start_job(["cxg:a", "cxg:b"], records=_records(a, b), out_dir=str(tmp_path), opener=open_)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        st = DM.get_status(job["job_id"])
        if st["files"][1]["status"] == "downloading":
            break
        time.sleep(0.01)
    r = DM.update_job(remove=["cxg:a"])
    assert any(x["dataset_uid"] == "cxg:a" for x in r["rejected"])
    assert not any(x["dataset_uid"] == "cxg:a" for x in r["removed"])
    assert "已在磁盘" in next(x["reason"] for x in r["rejected"])
    gate.set()
    _wait_state(job["job_id"])


def test_update_no_running_job_409():
    # 无运行任务：manager 级抛 job_not_running；端点级 409
    with pytest.raises(DM.DownloadManagerError) as e:
        DM.update_job(add=["cxg:a"])
    assert e.value.code == "job_not_running"
    r = _client().post("/api/download/update", json={"remove": ["cxg:a"]})
    assert r.status_code == 409 and r.json()["code"] == "job_not_running"


def test_update_bad_params_400():
    c = _client()
    for payload in ({"add": []}, {"remove": []}, {}, {"add": None, "remove": None},
                    {"add": [1]}, {"remove": "abc"}):
        r = c.post("/api/download/update", json=payload)
        assert r.status_code == 400, f"{payload!r} 应 400，实际 {r.status_code}"
    # manager 级：两个都空 → bad_param
    with pytest.raises(DM.DownloadManagerError) as e:
        DM.update_job()
    assert e.value.code == "bad_param"


def test_update_endpoint_end_to_end_add_and_remove(tmp_path, monkeypatch):
    """端点级全链路：真实 /api/download/update 对 running job 做 add + remove。
    monkeypatch build_download_plan 以注入假记录（端点内部读真实语料，这里换成假记录）——
    否则假 uid 在真实语料里不存在，会被报成 unsupported。"""
    orig_build = DM.build_download_plan
    gate, entered = threading.Event(), threading.Event()
    a = _record("cxg:a", url=CXG_PAGE, download_url=H5AD_URL, filesize=len(b"a"), name="甲")
    b = _record("cxg:b", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d56"),
                filesize=len(b"b"), name="乙")
    c = _record("cxg:c", url=CXG_PAGE, download_url=H5AD_URL.replace("6d55", "6d57"),
                filesize=len(b"c"), name="丙")
    monkeypatch.setattr(DM, "build_download_plan",
                        lambda uids, records=None: orig_build(uids, records=_records(a, b, c)))

    def open_(url, timeout):
        if url == H5AD_URL:
            return BlockingResp(b"a", gate, entered)
        return FakeResp(b"b")

    job = DM.start_job(["cxg:a", "cxg:b"], records=_records(a, b, c), out_dir=str(tmp_path), opener=open_)
    assert entered.wait(timeout=10)
    r = _client().post("/api/download/update", json={"add": ["cxg:c"], "remove": ["cxg:b"]})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"]
    assert any(x["dataset_uid"] == "cxg:c" and x["status"] == "added" for x in body["added"])
    assert any(x["dataset_uid"] == "cxg:b" and x["outcome"] == "skipped" for x in body["removed"])
    assert body["snapshot"]["state"] == "running"
    gate.set()
    st = _wait_state(job["job_id"])
    assert st["state"] == "done"
    by = {f["dataset_uid"]: f for f in st["files"]}
    assert by["cxg:a"]["status"] == "size_ok"
    assert by["cxg:b"]["status"] == "skipped"
    assert by["cxg:c"]["status"] == "size_ok"
