from __future__ import annotations

import json
import sys

from fastapi.testclient import TestClient

from dataset_recommender.app import webapp


client = TestClient(webapp.app, base_url="http://127.0.0.1")


def test_local_model_status_contract(monkeypatch):
    monkeypatch.setattr(webapp, "model_install_status", lambda _paths: {
        "schema": "biodata-model-install-status/v1", "state": "idle", "stage": "idle",
        "message": "尚未安装", "runtime_bytes": 0, "model_bytes": 0, "can_cancel": False,
    })
    response = client.get("/api/local-model/status")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["state"] == "idle"
    assert set(body) == {"ok", "schema", "state", "stage", "message", "runtime_bytes", "model_bytes", "can_cancel"}


def test_local_model_install_and_cancel_are_additive_post_actions(monkeypatch):
    monkeypatch.setattr(webapp, "start_model_install", lambda _paths: {
        "schema": "biodata-model-install-status/v1", "state": "running", "stage": "starting",
        "message": "正在启动", "runtime_bytes": 0, "model_bytes": 0, "can_cancel": True,
    })
    monkeypatch.setattr(webapp, "cancel_model_install", lambda _paths: {
        "schema": "biodata-model-install-status/v1", "state": "cancelled", "stage": "stopping",
        "message": "正在取消", "runtime_bytes": 0, "model_bytes": 0, "can_cancel": False,
    })
    start = client.post("/api/local-model/install")
    cancel = client.post("/api/local-model/cancel")
    assert start.status_code == 200 and start.json()["state"] == "running"
    assert cancel.status_code == 200 and cancel.json()["state"] == "cancelled"


def test_local_model_install_rejects_evil_origin_before_work(monkeypatch):
    """风险 10：外部网页的跨源 POST 不能诱导 localhost 触发 5 GB 下载。"""
    triggered = []

    def forbidden_start(_paths):
        triggered.append(True)
        raise AssertionError("跨源请求不应触发安装")

    monkeypatch.setattr(webapp, "start_model_install", forbidden_start)
    response = client.post("/api/local-model/install", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403
    assert triggered == []


# ── 回归：本地模型取消偶发 500 的竞态修复 ──────────────────────────────
# 根因：cancel_model_install（取消线程）与安装线程的取消 except 处理器**并发**调用
# `_write_status`，同一 `status.json.tmp` 上「写文本 + os.replace」在 Windows 会互踩
# （文件共享冲突 PermissionError → 500）。修复：`_write_status` 的写+替换整段加
# `_STATUS_LOCK` 串行化。下面两条直接压并发写，锁在则必然全过。

def _tmp_paths(tmp_path):
    import dataclasses

    from dataset_recommender.app.runtime_paths import get_app_paths

    return dataclasses.replace(
        get_app_paths(), data_root=tmp_path, model_root=tmp_path / "models"
    )


def test_concurrent_status_writes_never_fail(tmp_path):
    """并发压 `_write_status`（模拟取消 vs 安装线程同时写 status）：锁在则零异常、文件合法。"""
    import threading

    from dataset_recommender.app import model_installer as mi

    paths = _tmp_paths(tmp_path)
    states = ["running", "cancelled", "error", "running", "cancelled"]
    errors: list[BaseException] = []
    barrier = threading.Barrier(16)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            for _ in range(30):
                mi._write_status(paths, state=states[i % len(states)], stage="stopped", message="x")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"并发 _write_status 出现异常（竞态未修复）：{errors[:3]}"
    status_file = mi.status_path(paths)
    assert status_file.is_file()
    value = json.loads(status_file.read_text(encoding="utf-8"))
    assert value.get("schema") == mi.STATUS_SCHEMA
    # 修复前 Windows 上偶发 PermissionError；本测试用 16 线程 × 30 次把命中率抬高，
    # 修复后由锁保证串行，必然全过（无锁时会随机红，属本测试想抓住的回归）。
    assert not (tmp_path / "model-runtime" / "status.json.tmp").exists()


def test_cancel_model_install_idempotent_without_job(tmp_path):
    """无活动任务时取消也必须 200 级返回（幂等），绝不 500；落盘状态为 cancelled。"""
    from dataset_recommender.app import model_installer as mi

    paths = _tmp_paths(tmp_path)
    result = mi.cancel_model_install(paths)
    assert result["state"] == "cancelled"
    assert mi.status_path(paths).is_file()
