# -*- coding: utf-8 -*-
"""corpus-sync job 双后端的 webapp 接线门（2026-09-08 Redis/MQ 批）。

与 test_rq_jobs.py 的分工：那边测 rq_jobs 模块自身；本文件钉 **webapp 侧接线**——
默认线程路径的持久镜像写穿透/重启对账/idle 回落，以及 env=rq 时的委托与 503 翻译。
全部 fakeredis / mock：零真实网络、零真实 Redis、零子进程。

覆盖：
  1. 默认路径终态写穿透：job 到 done 后，持久镜像（tmp SQLite）load 出同形状终态。
  2. 重启对账：镜像停在 running（上一进程被打断）→ 首次触碰状态如实翻 failed 且含「中断」，
     且对账幂等（不重复改写 finished_at）。
  3. idle 回落：本进程从未跑过（内存 idle）而镜像有 done 终态 → snapshot 返回镜像。
  4. RQ 委托：env BIODATA_JOB_BACKEND=rq → start/snapshot 委托 rq_jobs，内存 job 与
     持久镜像都不被默认路径触碰；JobBackendError → 端点 503 + X-Error-Code 透传。
  5. 跨进程失效旗标：RQ 终态 + needs_web_invalidate="1" → snapshot 后 web 进程内
     invalidate_external_cache 与 recall_api.invalidate_vectors 各执行一次、旗标清零
     （再次 snapshot 不重复失效）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import fakeredis
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import durable_jobs  # noqa: E402
from dataset_recommender.app import redis_store  # noqa: E402
from dataset_recommender.app import rq_jobs  # noqa: E402
from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402
from dataset_recommender.retrieval import recall_api  # noqa: E402

# 测试专用假管理令牌（非真实秘密；真实部署令牌绝不进仓库）。
TEST_ADMIN_TOKEN = "test-admin-token-not-a-real-secret"
_KIND = webapp._CORPUS_SYNC_JOB_KIND


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与 test_corpus_sync_job.py 同款隔离：tmp 账号/会话/job 镜像库 + guard off +
    默认线程路径（显式删 BIODATA_JOB_BACKEND）；每测试前后复位 job 状态机与对账旗标。"""
    A._reset_state_for_tests()
    webapp._rate_buckets.clear()
    _reset_job()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("BIODATA_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("BIODATA_JOB_BACKEND", raising=False)
    with TestClient(webapp.app, base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)) as c:
        yield c
    _reset_job()
    webapp._rate_buckets.clear()
    A._reset_state_for_tests()


@pytest.fixture()
def rq_env(tmp_path, monkeypatch):
    """env=rq + fakeredis 双通道（与 test_rq_jobs.py 同套路：两个解码模式的 client
    挂同一 FakeServer，注入 redis_store 两个 builder 单例接缝）。"""
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "rq")
    monkeypatch.setenv("BIODATA_JOBS_DB", str(tmp_path / "jobs.sqlite"))
    server = fakeredis.FakeServer()
    fake = fakeredis.FakeStrictRedis(server=server, decode_responses=True)
    fake_bytes = fakeredis.FakeStrictRedis(server=server, decode_responses=False)
    monkeypatch.setattr(redis_store, "_build_client", lambda: fake)
    monkeypatch.setattr(redis_store, "_build_rq_client", lambda: fake_bytes)
    redis_store._reset_for_tests()
    yield fake
    redis_store._reset_for_tests()


def _reset_job() -> None:
    with webapp._CORPUS_SYNC_JOB_LOCK:
        webapp._CORPUS_SYNC_JOB.update(
            status="idle", started_at=None, finished_at=None, result=None, error=None)
    webapp._CORPUS_SYNC_RECONCILED = False


def _wait_job(timeout_s: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = webapp._corpus_sync_job_snapshot()
        if snap["status"] in ("done", "failed"):
            return snap
        time.sleep(0.02)
    raise AssertionError("corpus_sync_job 未在时限内到达终态")


def _wait_mirror(db: Path, timeout_s: float = 10.0) -> dict:
    """轮询持久镜像到终态。终态落账顺序是内存快照在前、镜像写穿透在后（best-effort），
    只等内存会在镜像写完成前返回——测写穿透必须等镜像本身。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        row = durable_jobs.load(_KIND, db_path=db)
        if row is not None and row["status"] in ("done", "failed"):
            return row
        time.sleep(0.02)
    raise AssertionError("持久镜像未在时限内到达终态")


def _fake_sync_result(imported: int = 0) -> dict:
    return {"checked_at": "2026-09-08T00:00:00Z", "sources": [],
            "imported_total": imported, "skipped_existing": 0,
            "operation_id": "sync_test", "created_files": [], "failed_sources": []}


# ---------------------------------------------------------------- 1. 默认路径终态写穿透

def test_default_path_terminal_write_through(client, tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result())
    webapp._corpus_sync_job_start(None)
    assert _wait_job()["status"] == "done"
    mirrored = _wait_mirror(tmp_path / "jobs.sqlite")
    assert mirrored["status"] == "done"
    assert mirrored["result"]["operation_id"] == "sync_test"   # result 以 JSON 如实落库
    assert mirrored["started_at"] and mirrored["finished_at"]  # running→done 两跳都留痕
    assert mirrored["error"] is None


def test_default_path_failed_write_through(client, tmp_path, monkeypatch):
    def boom(sources, *, project_root):
        raise cc.CurateError("sync_busy", "另一个「同步数据集」正在运行")

    monkeypatch.setattr(cc, "sync_updates", boom)
    webapp._corpus_sync_job_start(None)
    assert _wait_job()["status"] == "failed"
    mirrored = _wait_mirror(tmp_path / "jobs.sqlite")
    assert mirrored["status"] == "failed"
    assert "同步" in (mirrored["error"] or "")                  # hint 同样进镜像


# ---------------------------------------------------------------- 2. 重启对账

def test_restart_reconcile_marks_interrupted_failed(client, tmp_path):
    db = tmp_path / "jobs.sqlite"
    durable_jobs.record_running(_KIND, "2026-09-08T00:00:00+00:00", db_path=db)
    # 模拟进程重启：内存 idle（fixture 已复位），镜像却停在 running → 首次触碰如实翻 failed
    snap = webapp._corpus_sync_job_snapshot()
    assert snap["status"] == "failed"
    assert "中断" in (snap["error"] or "")
    assert snap["finished_at"]
    again = durable_jobs.load(_KIND, db_path=db)
    assert again["status"] == "failed"
    assert again["finished_at"] == snap["finished_at"]         # 对账幂等，不重复改写


def test_reconcile_noop_when_mirror_not_running(client, tmp_path):
    db = tmp_path / "jobs.sqlite"
    durable_jobs.record_terminal(_KIND, "done", "2026-09-08T01:00:00+00:00",
                                 _fake_sync_result(), None, db_path=db)
    snap = webapp._corpus_sync_job_snapshot()
    assert snap["status"] == "done"                            # 终态镜像原样呈现，不被误翻


# ---------------------------------------------------------------- 3. idle 回落到镜像

def test_idle_snapshot_falls_back_to_mirror(client, tmp_path):
    db = tmp_path / "jobs.sqlite"
    durable_jobs.record_terminal(_KIND, "done", "2026-09-08T01:00:00+00:00",
                                 {"operation_id": "sync_prev", "imported_total": 2}, None,
                                 db_path=db)
    snap = webapp._corpus_sync_job_snapshot()                  # 本进程从未跑过 → 回落镜像
    assert snap["status"] == "done"
    assert snap["result"]["operation_id"] == "sync_prev"
    assert snap["finished_at"] == "2026-09-08T01:00:00+00:00"


def test_memory_snapshot_wins_after_this_process_ran(client, tmp_path, monkeypatch):
    """本进程跑过之后的内存快照优先于镜像（镜像只是重启对账的兜底，不反压运行期事实）。"""
    db = tmp_path / "jobs.sqlite"
    durable_jobs.record_terminal(_KIND, "failed", "2026-09-08T01:00:00+00:00",
                                 None, "上一轮的历史错误", db_path=db)
    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result())
    webapp._corpus_sync_job_start(None)
    snap = _wait_job()
    assert snap["status"] == "done"                            # 内存 done，不被镜像的 failed 覆盖
    assert snap["error"] is None


# ---------------------------------------------------------------- 4. RQ 委托与 503 翻译

def test_start_delegates_to_rq_backend(client, rq_env, monkeypatch, tmp_path):
    calls = []
    sentinel = {"status": "running", "started_at": "2026-09-08T02:00:00+00:00",
                "finished_at": None, "result": None, "error": None}
    monkeypatch.setattr(rq_jobs, "start", lambda sources: calls.append(sources) or dict(sentinel))
    out = webapp._corpus_sync_job_start(["10x"])
    assert calls == [["10x"]]
    assert out["status"] == "running"
    with webapp._CORPUS_SYNC_JOB_LOCK:
        assert webapp._CORPUS_SYNC_JOB["status"] == "idle"     # 默认路径内存状态机不被触碰
    assert durable_jobs.load(_KIND, db_path=tmp_path / "jobs.sqlite") is None  # 不双写镜像


def test_snapshot_delegates_to_rq_backend(client, rq_env, monkeypatch):
    sentinel = {"status": "running", "started_at": "2026-09-08T02:00:00+00:00",
                "finished_at": None, "result": None, "error": None}
    calls = []
    monkeypatch.setattr(rq_jobs, "snapshot", lambda: calls.append(1) or dict(sentinel))
    out = webapp._corpus_sync_job_snapshot()
    assert calls == [1] and out["status"] == "running"


def test_rq_start_error_maps_endpoint_503(client, rq_env, monkeypatch):
    monkeypatch.setenv("BIODATA_ADMIN_TOKEN", TEST_ADMIN_TOKEN)

    def boom(sources):
        raise rq_jobs.JobBackendError("job_store_unavailable", "任务状态存储暂时不可用，请稍后重试。")

    monkeypatch.setattr(rq_jobs, "start", boom)
    r = client.post("/api/admin/corpus-sync", headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    assert r.status_code == 503                                # 如实报错，不静默回落内存路径
    assert r.headers.get("x-error-code") == "job_store_unavailable"
    assert "不可用" in r.json()["detail"]


def test_rq_snapshot_error_maps_endpoint_503(client, rq_env, monkeypatch):
    def boom():
        raise rq_jobs.JobBackendError("job_store_unavailable", "任务状态存储暂时不可用，请稍后重试。")

    monkeypatch.setattr(rq_jobs, "snapshot", boom)
    r = client.get("/api/curate/sync-updates/status")          # guard off 即可达
    assert r.status_code == 503
    assert r.headers.get("x-error-code") == "job_store_unavailable"


def test_bad_backend_value_fails_closed(client, monkeypatch):
    """开关笔误（非 rq 的非空值）→ backend_active 抛 bad_config，启动路径直接报错。"""
    monkeypatch.setenv("BIODATA_JOB_BACKEND", "rabbit")
    with pytest.raises(rq_jobs.JobBackendError) as ei:
        webapp._corpus_sync_job_start(None)
    assert ei.value.code == "bad_config"


# ---------------------------------------------------------------- 5. 跨进程失效旗标

def test_snapshot_consumes_web_invalidate_flag_once(client, rq_env, monkeypatch):
    rq_jobs._write_hash({
        "status": "done",
        "started_at": "2026-09-08T02:00:00+00:00",
        "finished_at": "2026-09-08T02:01:00+00:00",
        "result": "",
        "error": "",
        rq_jobs._FLAG_FIELD: "1",
    })
    calls = []
    monkeypatch.setattr(webapp, "invalidate_external_cache", lambda: calls.append("ext"))
    monkeypatch.setattr(recall_api, "invalidate_vectors", lambda: calls.append("vec"))
    snap = webapp._corpus_sync_job_snapshot()
    assert snap["status"] == "done"
    assert calls == ["ext", "vec"]                             # web 进程内各执行一次
    again = webapp._corpus_sync_job_snapshot()
    assert again["status"] == "done"
    assert calls == ["ext", "vec"]                             # 旗标已清，不重复失效


def test_snapshot_running_status_does_not_consume_flag(client, rq_env, monkeypatch):
    rq_jobs._write_hash({
        "status": "running",
        "started_at": "2026-09-08T02:00:00+00:00",
        rq_jobs._FLAG_FIELD: "0",
    })
    calls = []
    monkeypatch.setattr(webapp, "invalidate_external_cache", lambda: calls.append("ext"))
    snap = webapp._corpus_sync_job_snapshot()
    assert snap["status"] == "running"
    assert calls == []                                         # 未到终态不做失效


def test_snapshot_direct_call_503_raises_http_exception(client, rq_env, monkeypatch):
    """函数级消费点（非 HTTP 层）拿到的是 HTTPException(503)——cron/测试直调也一致。"""
    def boom():
        raise rq_jobs.JobBackendError("job_store_unavailable", "任务状态存储暂时不可用，请稍后重试。")

    monkeypatch.setattr(rq_jobs, "snapshot", boom)
    with pytest.raises(HTTPException) as ei:
        webapp._corpus_sync_job_snapshot()
    assert ei.value.status_code == 503
