from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataset_recommender.app.runtime_paths import AppPaths
from dataset_recommender.app import model_installer as mi
from dataset_recommender.retrieval import model_runtime as mr


def _paths(tmp_path: Path) -> AppPaths:
    resource = tmp_path / "resource"
    data = tmp_path / "data"
    return AppPaths(
        install_root=tmp_path / "app", resource_root=resource, data_root=data,
        config_root=data / "config", shipped_base_dir=resource / "database/base",
        shipped_external_dir=resource / "database/external", user_external_dir=data / "database/external",
        userdata_dir=data / ".userdata", model_root=data / "models", log_root=data / "logs",
        trace_root=data / "database/trace", export_root=data / "exports", run_root=data / "run",
        runtime_mode="frozen",
    )


def _bundle(paths: AppPaths) -> None:
    uv = paths.resource_root / "tools" / "uv.exe"
    worker = paths.resource_root / "tools" / "model_worker.py"
    lock = paths.resource_root / "packaging" / "requirements" / "model-win-x64.lock"
    for path, content in ((uv, b"uv"), (worker, b"worker"), (lock, b"demo==1 --hash=sha256:" + b"0" * 64)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _model_ready(paths: AppPaths) -> None:
    # 安装器同批下双模型：重排 + 嵌入两个目录都按完整文件就绪（fake_run 对两条
    # --download 命令都会调到本助手）。
    for target in (mr.model_dir(paths), mr.embed_model_dir(paths)):
        target.mkdir(parents=True, exist_ok=True)
        (target / "config.json").write_text("{}", encoding="utf-8")
        (target / "tokenizer.json").write_text("{}", encoding="utf-8")
        (target / "model.safetensors").write_bytes(b"weights")


def test_install_creates_isolated_runtime_and_ready_manifest(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _bundle(paths)
    commands: list[list[str]] = []

    def fake_run(command, *, env, log, cancel):
        commands.append(command)
        assert env["UV_PYTHON_INSTALL_DIR"].startswith(str(mi.runtime_root(paths)))
        if "venv" in command:
            py = mi.runtime_python(paths)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"python")
        if "--download" in command:
            _model_ready(paths)

    monkeypatch.setattr(mi, "_run_command", fake_run)
    result = mi.install_local_model(paths)
    assert result["state"] == "ready"
    assert [cmd[1] for cmd in commands] == ["venv", "pip", str(mi.worker_script(paths)), str(mi.worker_script(paths))]
    download_cmds = [cmd for cmd in commands if "--download" in cmd]
    assert "--embed" in download_cmds[1] and "--embed" not in download_cmds[0]
    assert "--managed-python" in commands[0] and mi.PYTHON_VERSION in commands[0]
    assert "--require-hashes" in commands[1] and "--torch-backend" in commands[1]
    manifest = json.loads(mi.ready_manifest_path(paths).read_text(encoding="utf-8"))
    assert manifest["schema"] == mr.READY_SCHEMA and manifest["model_id"] == mi.MODEL_ID
    assert manifest["embed_model_id"] == mi.EMBED_MODEL_ID and manifest["embed_files"]
    assert mr.external_runtime_ready(paths)
    assert mr.external_embed_ready(paths)


def test_install_failure_never_writes_ready_and_public_status_has_no_paths(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _bundle(paths)

    def fail(*_args, **_kwargs):
        raise mi.ModelInstallError("command_failed", "安装步骤失败（exit=9）。")

    monkeypatch.setattr(mi, "_run_command", fail)
    with pytest.raises(mi.ModelInstallError):
        mi.install_local_model(paths)
    assert not mi.ready_manifest_path(paths).exists()
    status = mi.model_install_status(paths)
    assert status["state"] == "error"
    raw = json.dumps(status, ensure_ascii=False)
    assert str(tmp_path) not in raw and "exit=9" in raw


def test_cancel_terminates_active_process_and_keeps_base_usable(monkeypatch, tmp_path):
    paths = _paths(tmp_path)

    class Process:
        def __init__(self):
            self.terminated = False

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

    process = Process()
    monkeypatch.setattr(mi, "_ACTIVE_PROCESS", process)
    result = mi.cancel_model_install(paths)
    assert process.terminated
    assert result["state"] == "cancelled"
    assert not mi.ready_manifest_path(paths).exists()


def test_cross_process_lock_rejects_live_owner_and_recovers_stale_owner(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    lock = mi.runtime_root(paths) / "install.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("123", encoding="ascii")
    monkeypatch.setattr(mi, "_pid_alive", lambda pid: pid == 123)
    with pytest.raises(mi.ModelInstallError, match="正在安装"):
        with mi._install_lock(paths):
            pass
    monkeypatch.setattr(mi, "_pid_alive", lambda _pid: False)
    with mi._install_lock(paths):
        assert lock.exists()
    assert not lock.exists()


def test_start_is_single_flight(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    blocker = threading.Event()

    def install(*_args, **_kwargs):
        blocker.wait(2)
        return {"state": "ready"}

    monkeypatch.setattr(mi, "install_local_model", install)
    monkeypatch.setattr(mi, "_JOB_THREAD", None)
    first = mi.start_model_install(paths)
    thread = mi._JOB_THREAD
    second = mi.start_model_install(paths)
    blocker.set()
    assert thread is mi._JOB_THREAD
    assert first["state"] == "running" and second["state"] == "running"
    thread.join(2)


def test_pip_sync_pins_only_binary_without_no_build(monkeypatch, tmp_path):
    """风险 0：uv 0.11.x 拒绝同时传 --no-build 与 --only-binary，钉死该参数组合。"""
    paths = _paths(tmp_path)
    _bundle(paths)
    commands: list[list[str]] = []

    def fake_run(command, *, env, log, cancel):
        commands.append(command)
        if "venv" in command:
            py = mi.runtime_python(paths)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"python")
        if "--download" in command:
            _model_ready(paths)

    monkeypatch.setattr(mi, "_run_command", fake_run)
    mi.install_local_model(paths)
    pip = commands[1]
    assert pip[:2] == [str(mi.uv_path(paths)), "pip"]
    assert "--no-build" not in pip
    assert "--only-binary" in pip and pip[pip.index("--only-binary") + 1] == ":all:"
    assert "--require-hashes" in pip and "--strict" in pip


def test_ready_status_reads_cached_bytes_without_rescanning(monkeypatch, tmp_path):
    """风险 1：READY 状态查询必须读缓存字节，绝不重新遍历 5 GB 目录。"""
    paths = _paths(tmp_path)
    _bundle(paths)

    def fake_run(command, *, env, log, cancel):
        if "venv" in command:
            py = mi.runtime_python(paths)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"python")
        if "--download" in command:
            _model_ready(paths)

    monkeypatch.setattr(mi, "_run_command", fake_run)
    mi.install_local_model(paths)

    def forbid_scan(_root):
        raise AssertionError("READY 状态查询不应重新扫描目录")

    monkeypatch.setattr(mi, "_dir_bytes", forbid_scan)
    status = mi.model_install_status(paths)
    assert status["state"] == "ready"
    assert status["runtime_bytes"] > 0
    assert status["model_bytes"] > 0


def test_install_log_rotation_keeps_only_previous_copy(tmp_path):
    """风险 2：install.log 只保留最近一份（install.log + install.log.1），不碰其它日志。"""
    paths = _paths(tmp_path)
    log = mi.install_log_path(paths)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("first run\n", encoding="utf-8")
    sibling = log.parent / "user-other.log"
    sibling.write_text("keep me", encoding="utf-8")

    mi._rotate_install_log(log)
    assert not log.exists()
    assert log.with_name("install.log.1").read_text(encoding="utf-8") == "first run\n"
    assert sibling.read_text(encoding="utf-8") == "keep me"

    log.write_text("second run\n", encoding="utf-8")
    mi._rotate_install_log(log)
    assert log.with_name("install.log.1").read_text(encoding="utf-8") == "second run\n"
    assert sibling.read_text(encoding="utf-8") == "keep me"


def test_install_rotates_previous_log_and_writes_fresh(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _bundle(paths)
    log = mi.install_log_path(paths)
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_bytes(b"previous run\n")

    def fake_run(command, *, env, log, cancel):
        if "venv" in command:
            py = mi.runtime_python(paths)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"python")
        if "--download" in command:
            _model_ready(paths)
        log.write(b"new run\n")

    monkeypatch.setattr(mi, "_run_command", fake_run)
    mi.install_local_model(paths)
    assert log.with_name("install.log.1").read_bytes() == b"previous run\n"
    new_content = log.read_bytes()
    assert b"previous run" not in new_content
    assert b"new run" in new_content


def test_disk_precheck_fails_fast_without_leaking_paths(monkeypatch, tmp_path):
    """风险 3：空间不足 fail-fast，错误只回普通中文、不暴露绝对路径、不写 READY。"""
    paths = _paths(tmp_path)
    _bundle(paths)
    monkeypatch.setattr(
        mi.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=0, used=0, free=mi.MIN_FREE_BYTES - 1),
    )
    with pytest.raises(mi.ModelInstallError) as exc:
        mi.install_local_model(paths)
    assert exc.value.code == "disk_space"
    assert str(tmp_path) not in exc.value.message
    assert not mi.ready_manifest_path(paths).exists()
    status = mi.model_install_status(paths)
    assert status["state"] == "error"
    assert str(tmp_path) not in json.dumps(status, ensure_ascii=False)


def test_disk_precheck_allows_when_enough_free_space(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    _bundle(paths)
    monkeypatch.setattr(
        mi.shutil, "disk_usage",
        lambda _p: SimpleNamespace(total=10 ** 12, used=0, free=mi.MIN_FREE_BYTES + 1),
    )

    def fake_run(command, *, env, log, cancel):
        if "venv" in command:
            py = mi.runtime_python(paths)
            py.parent.mkdir(parents=True, exist_ok=True)
            py.write_bytes(b"python")
        if "--download" in command:
            _model_ready(paths)

    monkeypatch.setattr(mi, "_run_command", fake_run)
    result = mi.install_local_model(paths)
    assert result["state"] == "ready"


def test_cancel_with_no_active_task_returns_cancelled_without_crash(monkeypatch, tmp_path):
    """风险 5：无活动任务时 cancel 也必须诚实返回 cancelled、不崩溃、不写 READY。"""
    paths = _paths(tmp_path)
    monkeypatch.setattr(mi, "_ACTIVE_PROCESS", None)
    result = mi.cancel_model_install(paths)
    assert result["state"] == "cancelled"
    assert not mi.ready_manifest_path(paths).exists()


def test_run_command_cancel_at_natural_exit_boundary_reports_cancelled(monkeypatch):
    """风险 5：进程已自然退出但取消已置位，仍报 cancelled，不误报 error。"""

    class ExitedProcess:
        def poll(self):
            return 0

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    cancel = threading.Event()
    cancel.set()
    monkeypatch.setattr(mi.subprocess, "Popen", lambda *a, **k: ExitedProcess())
    monkeypatch.setattr(mi, "_ACTIVE_PROCESS", None)
    with pytest.raises(mi.ModelInstallError) as exc:
        mi._run_command(["uv", "pip"], env={}, log=None, cancel=cancel)
    assert exc.value.code == "cancelled"
    assert mi._ACTIVE_PROCESS is None


def test_run_command_cancel_terminate_race_stays_cancelled(monkeypatch):
    """风险 5：poll 与 terminate 之间进程自然退出的竞态，仍报 cancelled 而非 error。"""

    class CancelNow:
        def wait(self, timeout):
            return True

        def is_set(self):
            return True

    class Process:
        def poll(self):
            return None

        def terminate(self):
            raise OSError("already gone")

        def wait(self, timeout=None):
            return 1

        def kill(self):
            pass

    monkeypatch.setattr(mi.subprocess, "Popen", lambda *a, **k: Process())
    monkeypatch.setattr(mi, "_ACTIVE_PROCESS", None)
    with pytest.raises(mi.ModelInstallError) as exc:
        mi._run_command(["uv"], env={}, log=None, cancel=CancelNow())
    assert exc.value.code == "cancelled"


def test_worker_thread_can_restart_after_finishing(monkeypatch, tmp_path):
    """风险 5：worker thread 结束后立刻重启能启动新线程，而非被死线程挡住。"""
    paths = _paths(tmp_path)
    results: list[str] = []

    def install(*_args, **_kwargs):
        results.append("ran")
        return {"state": "ready"}

    monkeypatch.setattr(mi, "install_local_model", install)
    monkeypatch.setattr(mi, "_JOB_THREAD", None)
    mi.start_model_install(paths)
    thread1 = mi._JOB_THREAD
    thread1.join(2)
    assert not thread1.is_alive()

    mi.start_model_install(paths)
    thread2 = mi._JOB_THREAD
    assert thread2 is not thread1
    thread2.join(2)
    assert results == ["ran", "ran"]


def test_install_lock_is_mutually_exclusive_across_threads(monkeypatch, tmp_path):
    """风险 5：两个安装者竞争单飞锁，后到者拿到 busy，锁在退出后释放。"""
    paths = _paths(tmp_path)
    monkeypatch.setattr(mi, "_pid_alive", lambda _pid: True)
    entered = threading.Event()
    release = threading.Event()
    outcomes: list[str] = []

    def holder():
        with mi._install_lock(paths):
            entered.set()
            release.wait(2)

    def contender():
        entered.wait(2)
        try:
            with mi._install_lock(paths):
                outcomes.append("acquired")
        except mi.ModelInstallError as exc:
            outcomes.append(exc.code)

    t1 = threading.Thread(target=holder)
    t2 = threading.Thread(target=contender)
    t1.start()
    t2.start()
    t2.join(3)
    release.set()
    t1.join(3)
    assert outcomes == ["busy"]


def test_legacy_cross_only_install_gets_embed_top_up(monkeypatch, tmp_path):
    """双模型同批安装之前的旧世界：venv + 重排模型就绪、无嵌入模型、manifest 无 embed_files。
    就绪判定必须显示未就绪（而非"已就绪"且无补装入口），重跑安装器补齐嵌入后双模型转就绪。"""
    paths = _paths(tmp_path)
    _bundle(paths)
    py = mi.runtime_python(paths)
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_bytes(b"python")
    cross = mr.model_dir(paths)
    cross.mkdir(parents=True, exist_ok=True)
    (cross / "config.json").write_text("{}", encoding="utf-8")
    (cross / "tokenizer.json").write_text("{}", encoding="utf-8")
    (cross / "model.safetensors").write_bytes(b"weights")
    legacy_manifest = {
        "schema": mr.READY_SCHEMA,
        "model_id": mi.MODEL_ID,
        "installed_at": "2026-01-01T00:00:00+00:00",
        "runtime_bytes": 1,
        "model_bytes": 11,
        "model_files": {"config.json": 2, "tokenizer.json": 2, "model.safetensors": 7},
    }
    manifest_path = mi.ready_manifest_path(paths)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(legacy_manifest), encoding="utf-8")
    assert mr.external_runtime_ready(paths)
    assert not mr.external_embed_ready(paths)
    assert mi.model_install_status(paths)["state"] != "ready"

    def fake_run(command, *, env, log, cancel):
        if "--download" in command and "--embed" in command:
            _model_ready(paths)

    monkeypatch.setattr(mi, "_run_command", fake_run)
    result = mi.install_local_model(paths)
    assert result["state"] == "ready"
    manifest = json.loads(mi.ready_manifest_path(paths).read_text(encoding="utf-8"))
    assert manifest["embed_files"]
    assert mr.external_runtime_ready(paths)
    assert mr.external_embed_ready(paths)


def test_stale_ready_status_json_not_shown_when_models_incomplete(tmp_path):
    """status.json 的 ready 只是安装完成时写入的缓存；实物不齐（无 manifest/无模型文件）
    时不得据此显示"已就绪"（否则前端没有补装入口），应回退到 idle。"""
    paths = _paths(tmp_path)
    mi._write_status(paths, state="ready", stage="ready", message="本地精准重排已经就绪。")
    assert mi.model_install_status(paths)["state"] == "idle"
