# -*- coding: utf-8 -*-
"""语料定期更新 + 追踪自动刷新编排的后端门。

覆盖（全部 mock 真实网络与子进程，套路沿用 test_webapp_guard_hardening.py /
test_curate_sync_updates.py）：
  1. patch_package.unbound_patch_scope：绑定态进入 → 强制未绑定；退出恢复原绑定。
  2. recall_api.invalidate_vectors：清向量缓存与失败标记（生产失效口）。
  3. build_corpus_vectors --include-uploads：默认口径剔 upload_* 不变，加 flag 才纳入。
  4. corpus_sync_job 状态机：单飞（running 中附着不新建）、done/failed 路径、
     imported>0 触发向量重建、=0 不触发、sync 显式无补丁作用域。
  5. admin 端点双闸：缺 env → 403「未启用」、token 错 → 403、非 loopback → 403、
     正确 → 202 附着；guard on 无会话也可凭 token+loopback 通过（开放但自认证）。
  6. guard on 下 /api/curate/sync-updates 异步入参响应形状 {ok, job, async:true} +
     GET status 轮询；guard off 阻塞行为逐字节不变（同步返回 result、无 async 键）。
  7. /api/health 的 corpus.gen：在 → 12 位 hex；语料代计算异常 → null 不 500。
"""
from __future__ import annotations

import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402
from dataset_recommender.corpus import patch_package  # noqa: E402
from dataset_recommender.retrieval import recall_api  # noqa: E402

# 测试专用假管理令牌（非真实秘密；真实部署令牌绝不进仓库）。
TEST_ADMIN_TOKEN = "test-admin-token-not-a-real-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与 test_webapp_guard_hardening.py 同款：tmp 账号/会话文件 + 默认 guard off；
    每个测试前后复位 job 状态机（进程内单例，测试间必须隔离）。"""
    A._reset_state_for_tests()
    webapp._rate_buckets.clear()
    _reset_job()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_JOBS_DB", str(tmp_path / "jobs.sqlite"))  # 持久镜像落 tmp，测试密闭
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("BIODATA_ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("BIODATA_JOB_BACKEND", raising=False)             # 本文件只测默认线程路径
    with TestClient(webapp.app, base_url="http://127.0.0.1",
                    client=("127.0.0.1", 50000)) as c:   # 对端回环：admin 端点 loopback 闸依赖它
        yield c
    _reset_job()
    webapp._rate_buckets.clear()
    A._reset_state_for_tests()


def _reset_job() -> None:
    with webapp._CORPUS_SYNC_JOB_LOCK:
        webapp._CORPUS_SYNC_JOB.update(
            status="idle", started_at=None, finished_at=None, result=None, error=None)
    webapp._CORPUS_SYNC_RECONCILED = False   # 每测试重新允许一次镜像对账


def _wait_job(timeout_s: float = 10.0) -> dict:
    """轮询 job 到终态（done/failed）；超时则断言失败。"""
    import time
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        snap = webapp._corpus_sync_job_snapshot()
        if snap["status"] in ("done", "failed"):
            return snap
        time.sleep(0.02)
    raise AssertionError("corpus_sync_job 未在时限内到达终态")


def _fake_sync_result(imported: int = 0) -> dict:
    return {"checked_at": "2026-08-26T00:00:00Z", "sources": [],
            "imported_total": imported, "skipped_existing": 0,
            "operation_id": "sync_test", "created_files": [], "failed_sources": []}


# ---------------------------------------------------------------- 1. unbound_patch_scope

def test_unbound_patch_scope_clears_and_restores() -> None:
    assert patch_package.current_patch_scope() is None
    with patch_package.bind_patch_scope("abc123de"):
        assert patch_package.current_patch_scope() == "abc123de"
        with patch_package.unbound_patch_scope():
            assert patch_package.current_patch_scope() is None   # 强制未绑定（共享写层口径）
        assert patch_package.current_patch_scope() == "abc123de"  # 退出恢复原绑定
    assert patch_package.current_patch_scope() is None


# ---------------------------------------------------------------- 2. recall_api.invalidate_vectors

def test_invalidate_vectors_clears_cache_and_failure_flag() -> None:
    recall_api.reset_caches_for_test()
    recall_api._VECTOR_CACHE = {"uid:x": {"h": "h", "v": [0.0]}}
    recall_api._VECTOR_LOAD_FAILED = True
    recall_api.invalidate_vectors()
    assert recall_api._VECTOR_CACHE is None
    assert recall_api._VECTOR_LOAD_FAILED is False
    recall_api.reset_caches_for_test()


# ---------------------------------------------------------------- 3. --include-uploads 口径

def _fake_record(source_file: str, uid: str):
    return types.SimpleNamespace(source_file=source_file, raw={"dataset_uid": uid}, dataset_name=uid)


def _run_build_dry_run(monkeypatch, capsys, argv: list[str]) -> int:
    import build_corpus_vectors as bcv
    monkeypatch.setattr(bcv, "load_full_corpus", lambda *a, **k: [
        _fake_record("10x-Visium.json", "uid-official"),
        _fake_record("upload_curate_sync_10x.json", "uid-upload"),
    ])
    # 候选文本模板与本测试无关（钉的是 upload_* 过滤口径），打桩避免拖入生产模板依赖。
    monkeypatch.setattr(bcv, "_candidate_text", lambda ns: "文本")
    monkeypatch.setattr(sys, "argv", ["build_corpus_vectors.py", "--dry-run"] + argv)
    rc = bcv.main()
    return rc


def test_build_vectors_default_excludes_uploads(monkeypatch, capsys):
    assert _run_build_dry_run(monkeypatch, capsys, []) == 0
    out = capsys.readouterr().out
    assert "待嵌入语料 1 条" in out          # 只有官方快照，upload_* 被剔
    assert "uid-upload" not in out


def test_build_vectors_include_uploads(monkeypatch, capsys):
    assert _run_build_dry_run(monkeypatch, capsys, ["--include-uploads"]) == 0
    out = capsys.readouterr().out
    assert "待嵌入语料 2 条" in out          # upload_* 纳入（服务器运营重建口径）
    assert "uid-upload" in out


# ---------------------------------------------------------------- 4. job 状态机 / 单飞 / 向量重建编排

def test_job_singleflight_attaches_when_running(client, monkeypatch):
    """running 中重复触发不新建（单飞吸收并发，不抛 sync_busy）：sync 只跑一次。"""
    gate = threading.Event()
    calls = []

    def fake_sync(sources, *, project_root):
        calls.append(sources)
        gate.wait(5)
        return _fake_sync_result()

    monkeypatch.setattr(cc, "sync_updates", fake_sync)
    first = webapp._corpus_sync_job_start(["10x"])
    assert first["status"] == "running"
    second = webapp._corpus_sync_job_start(["GEO"])     # 附着：不新建、不抛
    assert second["status"] == "running"
    gate.set()
    final = _wait_job()
    assert final["status"] == "done"
    assert len(calls) == 1 and calls[0] == ["10x"]       # 只有一次执行，且是第一次的入参
    assert final["result"]["imported_total"] == 0


def test_job_failed_path_records_hint(client, monkeypatch):
    def boom(sources, *, project_root):
        raise cc.CurateError("sync_busy", "另一个「同步数据集」正在运行")

    monkeypatch.setattr(cc, "sync_updates", boom)
    webapp._corpus_sync_job_start(None)
    final = _wait_job()
    assert final["status"] == "failed"
    assert "同步" in (final["error"] or "")              # CurateError.hint 如实进 error
    assert final["finished_at"]


def test_job_triggers_vector_rebuild_only_when_imported(client, monkeypatch):
    calls = []
    monkeypatch.setattr(webapp, "_corpus_sync_rebuild_vectors", lambda: calls.append(1) or None)

    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result(imported=3))
    webapp._corpus_sync_job_start(None)
    assert _wait_job()["status"] == "done"
    assert calls == [1]                                  # imported>0 → 触发向量重建

    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result(imported=0))
    webapp._corpus_sync_job_start(None)
    assert _wait_job()["status"] == "done"
    assert calls == [1]                                  # imported=0 → 不再触发


def test_job_runs_sync_explicitly_unbound(client, monkeypatch):
    """job 线程里的 sync 必须在无补丁作用域下跑（共享写层 upload_* 口径）。"""
    seen = []

    def fake_sync(sources, *, project_root):
        seen.append(patch_package.current_patch_scope())
        return _fake_sync_result()

    monkeypatch.setattr(cc, "sync_updates", fake_sync)
    with patch_package.bind_patch_scope("abc123de"):     # 调用方有绑定也不许泄进 job
        webapp._corpus_sync_job_start(None)
    assert _wait_job()["status"] == "done"
    assert seen == [None]


def test_vector_rebuild_atomic_replace_and_invalidate(tmp_path, monkeypatch):
    """重建成功（退出码 0/2）：tmp → os.replace 原子替换目标 → recall_api 失效口被调。"""
    target = tmp_path / "vectors.json.gz"
    target.write_bytes(b"old")
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(target))
    invalidated = []
    monkeypatch.setattr(recall_api, "invalidate_vectors", lambda: invalidated.append(1))

    class _Proc:
        returncode = 2                                   # 留缺口也算可用（运行期补嵌兜住）
        stdout = "ok-ish"
        stderr = ""

    def fake_run(cmd, **kw):
        out = Path(cmd[cmd.index("--out") + 1])
        out.write_bytes(b"new")
        return _Proc()

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", fake_run)
    err = webapp._corpus_sync_rebuild_vectors()
    assert err is None
    assert target.read_bytes() == b"new"                 # 原子替换落目标
    assert invalidated == [1]


def test_vector_rebuild_failure_keeps_target(tmp_path, monkeypatch):
    """重建失败（退出码 1）：目标文件不动、不调用失效口，错误摘要只留 tail。"""
    target = tmp_path / "vectors.json.gz"
    target.write_bytes(b"old")
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(target))
    invalidated = []
    monkeypatch.setattr(recall_api, "invalidate_vectors", lambda: invalidated.append(1))

    class _Proc:
        returncode = 1
        stdout = "x" * 3000
        stderr = ""

    import subprocess as _sp
    monkeypatch.setattr(_sp, "run", lambda cmd, **kw: _Proc())
    err = webapp._corpus_sync_rebuild_vectors()
    assert err and "退出码 1" in err and len(err) < 2000  # 只留 tail
    assert target.read_bytes() == b"old"
    assert invalidated == []


def test_vector_rebuild_skipped_when_env_missing(monkeypatch):
    monkeypatch.delenv("BIODATA_EMBED_VECTOR_FILE", raising=False)
    assert webapp._corpus_sync_rebuild_vectors() is None   # 未配置 → 跳过不算失败


# ---------------------------------------------------------------- 5. admin 端点双闸

def _admin_env(monkeypatch):
    monkeypatch.setenv("BIODATA_ADMIN_TOKEN", TEST_ADMIN_TOKEN)


def test_admin_sync_403_when_token_env_missing(client):
    r = client.post("/api/admin/corpus-sync")
    assert r.status_code == 403
    assert "未启用" in r.json()["detail"]                  # fail-closed：未配置即未启用


def test_admin_sync_403_on_wrong_token(client, monkeypatch):
    _admin_env(monkeypatch)
    assert client.post("/api/admin/corpus-sync").status_code == 403               # 缺头
    assert client.post("/api/admin/corpus-sync",
                       headers={"X-Admin-Token": "wrong"}).status_code == 403     # 错 token
    r = client.get("/api/admin/corpus-sync/status", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403


def test_admin_sync_403_on_non_loopback(client, monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.setattr(webapp, "_admin_client_host", lambda request: "10.0.0.5")
    r = client.post("/api/admin/corpus-sync", headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    assert r.status_code == 403
    assert TEST_ADMIN_TOKEN not in r.text                  # token 绝不进响应


def test_admin_sync_202_attaches_and_status(client, monkeypatch):
    _admin_env(monkeypatch)
    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result())
    r = client.post("/api/admin/corpus-sync", headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True and body["job"]["status"] in ("running", "done")
    assert _wait_job()["status"] == "done"                 # 立即返回不阻塞，后台跑到终态
    s = client.get("/api/admin/corpus-sync/status", headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    assert s.status_code == 200 and s.json()["job"]["status"] == "done"


def test_admin_sync_open_but_self_authed_under_guard(client, monkeypatch):
    """guard on + 无会话：路径在开放集合（不被中间件 401），但双闸仍然自足。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "test-invite-not-a-real-secret")
    _admin_env(monkeypatch)
    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result())
    assert client.post("/api/admin/corpus-sync").status_code == 403   # 无 token → 双闸拦
    r = client.post("/api/admin/corpus-sync", headers={"X-Admin-Token": TEST_ADMIN_TOKEN})
    assert r.status_code == 202                                        # token+loopback → 过


# ---------------------------------------------------------------- 6. 用户触发路径（guard on 异步 / guard off 逐字节不变）

def _register(client, username="alice"):
    return client.post("/api/account/register", json={
        "username": username, "password": "password12",
        "invite_code": "test-invite-not-a-real-secret"})


def test_guard_on_sync_updates_async_shape(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "test-invite-not-a-real-secret")
    monkeypatch.setattr(cc, "sync_updates", lambda s, *, project_root: _fake_sync_result())
    assert _register(client).status_code == 200

    r = client.post("/api/curate/sync-updates", json={"sources": ["10x"]})
    assert r.status_code == 202
    body = r.json()
    assert body["ok"] is True and body["async"] is True
    assert body["job"]["status"] in ("running", "done")
    assert "result" not in body                            # 异步响应不带阻塞结果键
    s = client.get("/api/curate/sync-updates/status")
    assert s.status_code == 200 and s.json()["ok"] is True
    assert s.json()["job"]["status"] in ("running", "done", "failed")
    assert _wait_job()["status"] == "done"


def test_guard_on_sync_updates_status_requires_login(client, monkeypatch):
    """status 端点无 token 闸但走中间件登录闸：guard on 匿名 → 401。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "test-invite-not-a-real-secret")
    assert client.get("/api/curate/sync-updates/status").status_code == 401


def test_guard_off_sync_updates_blocking_unchanged(client, monkeypatch):
    """本机形态钉死：请求内阻塞、同步返回 {ok, result}、无 async 键、无 job 键。"""
    seen = []
    monkeypatch.setattr(cc, "sync_updates",
                        lambda s, *, project_root: seen.append(s) or _fake_sync_result())
    r = client.post("/api/curate/sync-updates", json={"sources": None})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["result"]["imported_total"] == 0
    assert "async" not in body and "job" not in body
    assert seen == [None]
    assert webapp._corpus_sync_job_snapshot()["status"] == "idle"   # 本机路径不起 job


# ---------------------------------------------------------------- 7. /api/health 的 corpus.gen

def test_health_corpus_gen_present(client):
    body = client.get("/api/health").json()
    gen = body["corpus"]["gen"]
    assert isinstance(gen, str) and len(gen) == 12
    int(gen, 16)                                           # 纯 hex


def test_health_corpus_gen_null_on_failure(client, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("corpus broken")

    monkeypatch.setattr(webapp, "corpus_cache_generation", boom)
    body = client.get("/api/health").json()
    assert body["corpus"]["gen"] is None                   # 算不出 → null，绝不 500
    assert body["ok"] is True
