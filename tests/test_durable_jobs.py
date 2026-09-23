# -*- coding: utf-8 -*-
"""通用 SQLite 持久 job 镜像的门（durable_jobs）。

全部用例只用 tmp_path 下的临时 DB 文件，绝不触碰真实 .userdata；覆盖：running/终态
往返、started_at 保留、重启对账（running → failed）、连续两轮 job 的最新性、坏 JSON
降级不抛、默认路径与 env 覆盖的路径守卫。
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import durable_jobs as D  # noqa: E402


@pytest.fixture()
def db(tmp_path):
    return tmp_path / "jobs.sqlite"


def test_record_running_then_load(db):
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    snap = D.load("corpus_sync", db_path=db)
    assert snap == {
        "status": "running",
        "started_at": "2026-09-08T01:02:03+00:00",
        "finished_at": None,
        "result": None,
        "error": None,
    }


def test_record_terminal_done_roundtrip_keeps_started_at(db):
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    result = {"imported_total": 3, "sources": ["geo"], "detail": {"nested": [1, 2]}}
    D.record_terminal(
        "corpus_sync", "done", "2026-09-08T01:05:00+00:00", result, None, db_path=db)
    snap = D.load("corpus_sync", db_path=db)
    assert snap["status"] == "done"
    assert snap["finished_at"] == "2026-09-08T01:05:00+00:00"
    assert snap["result"] == result          # JSON 完整往返（含嵌套）
    assert snap["error"] is None
    assert snap["started_at"] == "2026-09-08T01:02:03+00:00"  # 终态不抹掉起始时刻


def test_record_terminal_failed(db):
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    D.record_terminal(
        "corpus_sync", "failed", "2026-09-08T01:03:00+00:00", None,
        "语料向量重建失败（退出码 1）", db_path=db)
    snap = D.load("corpus_sync", db_path=db)
    assert snap["status"] == "failed"
    assert snap["error"] == "语料向量重建失败（退出码 1）"
    assert snap["result"] is None


def test_load_missing_db_and_missing_row(db):
    assert D.load("corpus_sync", db_path=db) is None          # 文件都还没建
    D.record_running("other_kind", "2026-09-08T01:02:03+00:00", db_path=db)
    assert D.load("corpus_sync", db_path=db) is None          # 库在但没有这个 kind


def test_reconcile_interrupted_flips_running(db):
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    assert D.reconcile_interrupted("corpus_sync", "进程重启中断，任务已停止", db_path=db) is True
    snap = D.load("corpus_sync", db_path=db)
    assert snap["status"] == "failed"
    assert snap["error"] == "进程重启中断，任务已停止"
    assert snap["finished_at"]                              # 补上终态时刻
    assert snap["started_at"] == "2026-09-08T01:02:03+00:00"


def test_reconcile_interrupted_ignores_terminal_and_missing(db):
    assert D.reconcile_interrupted("corpus_sync", "重启中断", db_path=db) is False  # 空库
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    D.record_terminal("corpus_sync", "done", "2026-09-08T01:05:00+00:00", {}, None, db_path=db)
    assert D.reconcile_interrupted("corpus_sync", "重启中断", db_path=db) is False
    assert D.load("corpus_sync", db_path=db)["status"] == "done"   # 终态不被改写


def test_second_job_replaces_first(db):
    D.record_running("corpus_sync", "2026-09-08T01:00:00+00:00", db_path=db)
    D.record_terminal("corpus_sync", "done", "2026-09-08T01:01:00+00:00", {"imported_total": 1}, None, db_path=db)
    D.record_running("corpus_sync", "2026-09-08T02:00:00+00:00", db_path=db)
    snap = D.load("corpus_sync", db_path=db)
    assert snap["status"] == "running"
    assert snap["started_at"] == "2026-09-08T02:00:00+00:00"   # 新一轮起始时刻
    assert snap["finished_at"] is None
    assert snap["result"] is None and snap["error"] is None    # 上一轮残留被清空
    D.record_terminal("corpus_sync", "done", "2026-09-08T02:01:00+00:00", {"imported_total": 2}, None, db_path=db)
    snap = D.load("corpus_sync", db_path=db)
    assert snap["result"] == {"imported_total": 2}
    assert snap["started_at"] == "2026-09-08T02:00:00+00:00"


def test_load_tolerates_corrupt_result_json(db):
    D.record_running("corpus_sync", "2026-09-08T01:02:03+00:00", db_path=db)
    D.record_terminal("corpus_sync", "done", "2026-09-08T01:05:00+00:00", {"ok": True}, None, db_path=db)
    conn = sqlite3.connect(str(db))                            # 直接写坏 JSON（绕过公共接口）
    try:
        conn.execute("UPDATE jobs SET result_json = ? WHERE kind = ?", ("{not json", "corpus_sync"))
        conn.commit()
    finally:
        conn.close()                                           # 显式关连接，不留 Windows 文件锁
    snap = D.load("corpus_sync", db_path=db)                   # 不抛
    assert snap is not None
    assert snap["result"] is None                              # 只退化成结果读不出来
    assert snap["status"] == "done"                            # 其余字段仍如实呈现


def test_default_db_path_env_override(monkeypatch, tmp_path):
    target = tmp_path / "custom" / "jobs.sqlite"
    monkeypatch.setenv(D.JOBS_DB_ENV, str(target))
    assert D.default_db_path(tmp_path) == target.resolve()


def test_default_db_path_guard_rejects_database_dir(monkeypatch):
    monkeypatch.setenv(D.JOBS_DB_ENV, str(ROOT / "database" / "jobs.sqlite"))
    with pytest.raises(D.JobStoreError) as exc:
        D.default_db_path(ROOT)
    assert exc.value.code == "bad_store_path"


def test_default_db_path_falls_back_to_userdata(monkeypatch, tmp_path):
    monkeypatch.delenv(D.JOBS_DB_ENV, raising=False)
    monkeypatch.delenv("BIODATA_DATA_ROOT", raising=False)
    monkeypatch.delenv("BIODATA_RESOURCE_ROOT", raising=False)
    assert D.default_db_path(tmp_path) == tmp_path / ".userdata" / "jobs.sqlite"
