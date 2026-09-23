# -*- coding: utf-8 -*-
"""安装版可选本地语义模型：独立 venv + 固定模型的可恢复在线安装。

安全边界：
- URL/包名/模型 id 不接受调用方输入；依赖来自随包 SHA-256 lock，uv 只用 wheels；
- 重依赖住 data_root/model-runtime/venv，不进入 FastAPI 主进程；
- 单飞跨进程锁；失败/取消不写 READY，不影响规则排序；
- 对前端只回状态、阶段、普通说明和体积，不回本机路径/原始下载错误。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .runtime_paths import AppPaths, get_app_paths
from ..retrieval.model_runtime import (
    READY_SCHEMA,
    embed_model_dir,
    external_embed_ready,
    external_runtime_ready,
    model_dir,
    read_ready_manifest,
    ready_manifest_path,
    runtime_python,
    runtime_root,
    worker_script,
)
from ..retrieval.model_worker import EMBED_MODEL_ID, MODEL_ID, model_file_manifest, model_files_ready

STATUS_SCHEMA = "biodata-model-install-status/v1"
PYTHON_VERSION = "3.12.13"
#: 磁盘预检保守阈值：双模型下载约 2.7 GB、装完约 5.5 GB，预留 7 GiB 余量 fail-fast。
MIN_FREE_BYTES = 7 * 1024 ** 3
_STATE_LOCK = threading.Lock()
# 修复：status.json 的写侧专用锁。此前 `_write_status` 的「写 .tmp + os.replace」没有
# 任何串行化——取消（cancel_model_install）与安装线程（取消事件的 except 处理器）会**并发**写
# 同一个 `status.json.tmp` 再各自 os.replace：Windows 上另一线程正持有打开句柄时 replace/truncate
# 会抛 PermissionError（文件共享冲突），在活动下载中取消偶发 500 的根因之一。本锁把「写临时文件 +
# 原子替换」整段串行化；`_STATE_LOCK` 已承担进程/线程状态职责且 start_model_install 在其内
# 调用 _write_status，不能复用（会重入死锁），故单独一把写锁。
_STATUS_LOCK = threading.Lock()
# 残余根因兜底：锁内仍可能偶发 `os.replace` WinError 5（杀软/索引器短暂持有新 tmp 文件句柄），
# 重试写+替换（幂等、最终一致）；最后一次仍失败才上抛（status 落盘失败是真实故障，不静默吞）。
_STATUS_WRITE_RETRIES = 5
_ACTIVE_PROCESS: "subprocess.Popen | None" = None
_JOB_THREAD: "threading.Thread | None" = None
_CANCEL = threading.Event()


class ModelInstallError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def status_path(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "status.json"


def install_log_path(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "install.log"


def model_lock_path(paths: "AppPaths | None" = None) -> Path:
    resolved = paths or get_app_paths()
    return resolved.resource_root / "packaging" / "requirements" / "model-win-x64.lock"


def uv_path(paths: "AppPaths | None" = None) -> "Path | None":
    resolved = paths or get_app_paths()
    if resolved.runtime_mode == "frozen":
        candidate = resolved.resource_root / "tools" / "uv.exe"
        return candidate if candidate.is_file() else None
    found = shutil.which("uv")
    return Path(found) if found else None


def _dir_bytes(root: Path) -> int:
    total = 0
    try:
        for path in root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
    except OSError:
        return total
    return total


def _rotate_install_log(log_path: Path) -> None:
    """安装开始前把上一份 install.log 轮转成 install.log.1，只保留最近一份，避免无界增长。

    只触碰 install.log 与其 .1 副本，绝不删除其它日志文件。"""
    if not log_path.is_file():
        return
    rotated = log_path.with_name(log_path.name + ".1")
    try:
        rotated.unlink()
    except OSError:
        pass
    try:
        os.replace(log_path, rotated)
    except OSError:
        pass


def _ensure_free_space(paths: AppPaths) -> None:
    """在 data_root 所在卷按保守阈值 fail-fast，避免 5 GB 安装中途耗尽磁盘。"""
    probe = paths.data_root
    while not probe.exists():
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return  # 无法探测时不阻断安装，真实失败照常降级
    if free < MIN_FREE_BYTES:
        raise ModelInstallError("disk_space", "磁盘剩余空间不足，无法安装本地模型；基础检索不受影响。")


def _public_status(value: dict) -> dict:
    return {
        "schema": STATUS_SCHEMA,
        "state": str(value.get("state") or "idle"),
        "stage": str(value.get("stage") or "idle"),
        "message": str(value.get("message") or "尚未安装本地精准重排模型。")[:240],
        "runtime_bytes": max(0, int(value.get("runtime_bytes") or 0)),
        "model_bytes": max(0, int(value.get("model_bytes") or 0)),
        "can_cancel": bool(value.get("can_cancel")),
    }


def _write_status(
    paths: AppPaths, *, state: str, stage: str, message: str, can_cancel: bool = False,
    runtime_bytes: "int | None" = None, model_bytes: "int | None" = None,
) -> dict:
    root = runtime_root(paths)
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema": STATUS_SCHEMA,
        "state": state,
        "stage": stage,
        "message": message,
        "can_cancel": can_cancel,
        "runtime_bytes": _dir_bytes(root / "venv") if runtime_bytes is None else runtime_bytes,
        "model_bytes": _dir_bytes(model_dir(paths)) if model_bytes is None else model_bytes,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    target = status_path(paths)
    temporary = target.with_suffix(".tmp")
    # 串行化「写 .tmp + os.replace」：取消线程与安装线程并发写同一临时文件会触发 Windows
    # 文件共享冲突（PermissionError → 500）；_dir_bytes 计算放在锁外（纯读、可并行），
    # 只把写+替换这段临界区锁住，避免长时间持锁。
    # 锁外仍可能偶发 `os.replace` WinError 5（拒绝访问）：Windows 杀软/索引器会在新 tmp 文件
    # 写完后短暂持有句柄扫描，MoveFileExW 即失败——这是实测 500 的残余根因，用重试兜底
    # （写+替换整体重试，最后一次仍失败才上抛）。
    with _STATUS_LOCK:
        for attempt in range(_STATUS_WRITE_RETRIES):
            try:
                temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
                os.replace(temporary, target)
                break
            except OSError:
                if attempt == _STATUS_WRITE_RETRIES - 1:
                    raise
                time.sleep(0.02 * (attempt + 1))
    return _public_status(value)


def model_install_status(paths: "AppPaths | None" = None) -> dict:
    resolved = paths or get_app_paths()
    manifest = read_ready_manifest(resolved)
    if manifest and external_runtime_ready(resolved) and external_embed_ready(resolved):
        runtime_bytes = manifest.get("runtime_bytes")
        model_bytes = manifest.get("model_bytes")
        # READY 查询直接读安装完成时写入的缓存字节；缺失/损坏时才回退一次扫描，
        # 绝不让 5 GB venv 目录遍历成为每次页面加载的成本。
        if not isinstance(runtime_bytes, int) or not isinstance(model_bytes, int):
            runtime_bytes = _dir_bytes(runtime_root(resolved) / "venv")
            model_bytes = _dir_bytes(model_dir(resolved))
        return _public_status({
            "state": "ready", "stage": "ready", "message": "本地精准重排已经就绪。",
            "runtime_bytes": runtime_bytes, "model_bytes": model_bytes, "can_cancel": False,
        })
    try:
        value = json.loads(status_path(resolved).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    if not isinstance(value, dict) or value.get("schema") != STATUS_SCHEMA:
        value = {}
    # 走到这里说明双模型实物不齐（上面 ready 分支已 return），旧版写入的 ready 属陈旧状态，作废回 idle 以恢复安装入口。
    if value.get("state") == "ready":
        value = {}
    return _public_status(value)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        os.kill(pid, 0)
        return True
    except Exception:
        return False


@contextlib.contextmanager
def _install_lock(paths: AppPaths) -> Iterator[None]:
    root = runtime_root(paths)
    root.mkdir(parents=True, exist_ok=True)
    lock = root / "install.lock"
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            break
        except FileExistsError:
            try:
                owner = int(lock.read_text(encoding="ascii").strip())
            except (OSError, ValueError):
                owner = 0
            if _pid_alive(owner):
                raise ModelInstallError("busy", "本地模型正在安装，请等待当前任务完成。")
            try:
                lock.unlink()
            except OSError as exc:
                raise ModelInstallError("busy", "本地模型安装锁暂时不可用。") from exc
    else:
        raise ModelInstallError("busy", "本地模型安装锁暂时不可用。")
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _run_command(command: "list[str]", *, env: dict[str, str], log, cancel: threading.Event) -> None:
    global _ACTIVE_PROCESS
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    with _STATE_LOCK:
        _ACTIVE_PROCESS = subprocess.Popen(
            command, stdout=log, stderr=subprocess.STDOUT, env=env,
            creationflags=flags,
        )
        process = _ACTIVE_PROCESS
    try:
        while process.poll() is None:
            if cancel.wait(0.25):
                try:
                    process.terminate()
                except OSError:
                    pass  # 进程恰好在 poll 与 terminate 之间自然退出：仍按取消处理，不误报 error
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                raise ModelInstallError("cancelled", "已取消本地模型安装。")
        if cancel.is_set():
            raise ModelInstallError("cancelled", "已取消本地模型安装。")
        if process.returncode != 0:
            raise ModelInstallError("command_failed", f"安装步骤失败（exit={process.returncode}）。")
    finally:
        with _STATE_LOCK:
            if _ACTIVE_PROCESS is process:
                _ACTIVE_PROCESS = None


def _ready_manifest(paths: AppPaths, lock: Path, *, runtime_bytes: int, model_bytes: int) -> dict:
    return {
        "schema": READY_SCHEMA,
        "model_id": MODEL_ID,
        "python": PYTHON_VERSION,
        "lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "runtime_bytes": runtime_bytes,
        "model_bytes": model_bytes,
        "model_files": model_file_manifest(model_dir(paths)),
        "embed_model_id": EMBED_MODEL_ID,
        "embed_files": model_file_manifest(embed_model_dir(paths)),
    }


def install_local_model(paths: "AppPaths | None" = None, *, cancel: "threading.Event | None" = None) -> dict:
    resolved = paths or get_app_paths()
    cancel_event = cancel or threading.Event()
    if external_runtime_ready(resolved) and external_embed_ready(resolved):
        return model_install_status(resolved)
    uv = uv_path(resolved)
    lock = model_lock_path(resolved)
    worker = worker_script(resolved)
    if uv is None or not uv.is_file():
        raise ModelInstallError("uv_missing", "安装组件缺少 uv，基础程序仍可正常使用。")
    if not lock.is_file() or not worker.is_file():
        raise ModelInstallError("bundle_incomplete", "安装组件文件不完整，基础程序仍可正常使用。")

    root = runtime_root(resolved)
    root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "UV_PYTHON_INSTALL_DIR": str(root / "python"),
        "UV_CACHE_DIR": str(root / "cache"),
        "UV_LINK_MODE": "copy",
        "UV_NO_CONFIG": "1",
        "PYTHONUTF8": "1",
    })
    log_path = install_log_path(resolved)
    _rotate_install_log(log_path)
    with _install_lock(resolved), open(log_path, "wb", buffering=0) as log:
        try:
            _ensure_free_space(resolved)
            _write_status(resolved, state="running", stage="python", message="正在准备独立的本地模型运行环境…", can_cancel=True)
            if not runtime_python(resolved).is_file():
                _run_command([
                    str(uv), "venv", "--no-project", "--managed-python",
                    "--python", PYTHON_VERSION, str(root / "venv"),
                ], env=env, log=log, cancel=cancel_event)

            _write_status(resolved, state="running", stage="dependencies", message="正在下载并校验本地模型运行组件…", can_cancel=True)
            _run_command([
                str(uv), "pip", "sync", str(lock),
                "--python", str(runtime_python(resolved)),
                "--require-hashes", "--strict", "--only-binary", ":all:",
                "--torch-backend", "cpu",
            ], env=env, log=log, cancel=cancel_event)

            _write_status(resolved, state="running", stage="model", message="正在下载并核验约 2.2 GB 重排模型权重…", can_cancel=True)
            _run_command([
                str(runtime_python(resolved)), str(worker), "--download", str(model_dir(resolved)),
            ], env=env, log=log, cancel=cancel_event)
            if not model_files_ready(model_dir(resolved)):
                raise ModelInstallError("model_incomplete", "模型下载完成但文件不完整。")

            _write_status(resolved, state="running", stage="model", message="正在下载并核验约 0.5 GB 嵌入模型权重…", can_cancel=True)
            _run_command([
                str(runtime_python(resolved)), str(worker), "--download", str(embed_model_dir(resolved)), "--embed",
            ], env=env, log=log, cancel=cancel_event)
            if not model_files_ready(embed_model_dir(resolved)):
                raise ModelInstallError("model_incomplete", "模型下载完成但文件不完整。")

            # 安装完成时一次性计算字节数，写进 READY manifest 与 status 缓存；
            # 之后 READY 状态查询直接读缓存，不再每次遍历 5 GB 目录。
            runtime_bytes = _dir_bytes(root / "venv")
            model_bytes = _dir_bytes(model_dir(resolved)) + _dir_bytes(embed_model_dir(resolved))
            manifest = ready_manifest_path(resolved)
            temporary = manifest.with_suffix(".tmp")
            temporary.write_text(json.dumps(
                _ready_manifest(resolved, lock, runtime_bytes=runtime_bytes, model_bytes=model_bytes),
                ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(temporary, manifest)
            return _write_status(
                resolved, state="ready", stage="ready", message="本地精准重排已经就绪。",
                runtime_bytes=runtime_bytes, model_bytes=model_bytes,
            )
        except ModelInstallError as exc:
            state = "cancelled" if exc.code == "cancelled" else "error"
            _write_status(resolved, state=state, stage="stopped", message=exc.message)
            raise
        except Exception as exc:  # noqa: BLE001
            _write_status(resolved, state="error", stage="stopped", message="本地模型安装失败，可稍后重试；基础检索不受影响。")
            raise ModelInstallError("unexpected", f"本地模型安装失败（{type(exc).__name__}）。") from exc


def start_model_install(paths: "AppPaths | None" = None) -> dict:
    global _JOB_THREAD, _CANCEL
    resolved = paths or get_app_paths()
    with _STATE_LOCK:
        if _JOB_THREAD is not None and _JOB_THREAD.is_alive():
            return model_install_status(resolved)
        starting = _write_status(resolved, state="running", stage="starting", message="正在启动本地模型安装…", can_cancel=True)
        _CANCEL = threading.Event()

        def run() -> None:
            try:
                install_local_model(resolved, cancel=_CANCEL)
            except ModelInstallError:
                pass

        _JOB_THREAD = threading.Thread(target=run, name="biodata-model-install", daemon=True)
        _JOB_THREAD.start()
    return starting


def cancel_model_install(paths: "AppPaths | None" = None) -> dict:
    resolved = paths or get_app_paths()
    _CANCEL.set()
    with _STATE_LOCK:
        process = _ACTIVE_PROCESS
    if process is not None and process.poll() is None:
        try:
            process.terminate()
        except OSError:
            pass
    return _write_status(resolved, state="cancelled", stage="stopping", message="正在取消本地模型安装…")


def cli_install_local_model(paths: "AppPaths | None" = None) -> int:
    try:
        install_local_model(paths)
        return 0
    except ModelInstallError:
        return 1
