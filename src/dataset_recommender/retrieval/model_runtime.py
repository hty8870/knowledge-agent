# -*- coding: utf-8 -*-
"""隔离本地模型运行时的路径与 JSONL 客户端。

在线安装的 torch/transformers 全部住 data_root/model-runtime/venv，主 FastAPI 进程
不修改 sys.path。打分通过该 venv 的常驻 worker 进行，依赖冲突或 worker 崩溃只会让
vector_recall 回退规则顺序。
"""
from __future__ import annotations

import atexit
import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Sequence

from ..app.runtime_paths import AppPaths, get_app_paths
from .model_worker import MODEL_ID, model_files_ready
from .vector_recall import DEFAULT_CROSS_ENCODER_MODEL, DEFAULT_EMBEDDING_MODEL

READY_SCHEMA = "biodata-model-runtime/v1"
STARTUP_TIMEOUT_S = 300.0
SCORE_TIMEOUT_S = 180.0


def runtime_root(paths: "AppPaths | None" = None) -> Path:
    return (paths or get_app_paths()).data_root / "model-runtime"


def runtime_python(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "venv" / "Scripts" / "python.exe"


def ready_manifest_path(paths: "AppPaths | None" = None) -> Path:
    return runtime_root(paths) / "READY.json"


def worker_script(paths: "AppPaths | None" = None) -> Path:
    resolved = paths or get_app_paths()
    if resolved.runtime_mode == "frozen":
        return resolved.resource_root / "tools" / "model_worker.py"
    return Path(__file__).resolve().with_name("model_worker.py")


def model_dir(paths: "AppPaths | None" = None) -> Path:
    return (paths or get_app_paths()).model_root / "cross_encoders" / DEFAULT_CROSS_ENCODER_MODEL


def embed_model_dir(paths: "AppPaths | None" = None) -> Path:
    return (paths or get_app_paths()).model_root / "embeddings" / DEFAULT_EMBEDDING_MODEL


def read_ready_manifest(paths: "AppPaths | None" = None) -> dict:
    path = ready_manifest_path(paths)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(value, dict) or value.get("schema") != READY_SCHEMA or value.get("model_id") != MODEL_ID:
        return {}
    return value


def _manifest_files_intact(manifest: dict, paths: "AppPaths | None" = None) -> bool:
    """核对 READY manifest 记录的关键文件大小与磁盘实际一致。

    旧 manifest 若无 ``model_files`` 尺寸清单，退化为仅存在性检查（model_files_ready 已覆盖）。
    清单必须覆盖配置、分词器与真权重，缺项即视为不完整（防 manifest 被篡改/写坏）。"""
    files = manifest.get("model_files")
    if not isinstance(files, dict) or not files:
        return True
    return _files_intact(files, model_dir(paths))


def _files_intact(files, target: Path) -> bool:
    """尺寸清单完整性核对（两个模型目录共用的同一机制）：清单必须覆盖配置、分词器与
    真权重，缺项/大小不符即视为不完整（防 manifest 被篡改/写坏）。"""
    if not isinstance(files, dict) or not files:
        return False
    for relative, expected in files.items():
        path = target / relative
        if not path.is_file():
            return False
        try:
            if path.stat().st_size != int(expected):
                return False
        except (OSError, ValueError, TypeError):
            return False
    if "config.json" not in files:
        return False
    if not any(name in files for name in ("tokenizer.json", "tokenizer_config.json")):
        return False
    if not any(name.endswith((".safetensors", ".bin")) for name in files):
        return False
    return True


def _embed_manifest_files_intact(manifest: dict, paths: "AppPaths | None" = None) -> bool:
    """嵌入模型的同款完整性核对。双模型同批安装之前的旧 manifest 没有 ``embed_files``
    → 嵌入侧一律不就绪（fail-closed）；重跑安装器补齐后自动转就绪。"""
    return _files_intact(manifest.get("embed_files"), embed_model_dir(paths))


def external_embed_ready(paths: "AppPaths | None" = None) -> bool:
    """嵌入侧就绪闸（与 external_runtime_ready 同族）：运行时要件 + 嵌入模型文件与
    manifest 完整性。嵌入侧不就绪不影响重排侧就绪判定。"""
    resolved = paths or get_app_paths()
    manifest = read_ready_manifest(resolved)
    if not manifest:
        return False
    if not (runtime_python(resolved).is_file() and worker_script(resolved).is_file()):
        return False
    if not model_files_ready(embed_model_dir(resolved)):
        return False
    return _embed_manifest_files_intact(manifest, resolved)


def external_runtime_ready(paths: "AppPaths | None" = None) -> bool:
    resolved = paths or get_app_paths()
    manifest = read_ready_manifest(resolved)
    if not manifest:
        return False
    if not (runtime_python(resolved).is_file() and worker_script(resolved).is_file()):
        return False
    if not model_files_ready(model_dir(resolved)):
        return False
    return _manifest_files_intact(manifest, resolved)


class _WorkerProcess:
    """常驻 worker 子进程共享基座：启动/读写/关闭一套实现，子类只声明就绪闸与服务命令。"""

    def __init__(self, paths: "AppPaths | None" = None) -> None:
        self.paths = paths or get_app_paths()
        self.process: "subprocess.Popen[str] | None" = None
        self.responses: "queue.Queue[dict]" = queue.Queue()
        self.stderr_lines: "queue.Queue[str]" = queue.Queue(maxsize=40)
        self.lock = threading.Lock()

    def _reader(self, stream) -> None:
        for line in stream:
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                self.responses.put(payload)

    def _stderr_reader(self, stream) -> None:
        for line in stream:
            text = str(line).strip()[:500]
            if not text:
                continue
            if self.stderr_lines.full():
                try:
                    self.stderr_lines.get_nowait()
                except queue.Empty:
                    pass
            self.stderr_lines.put(text)

    def _ready(self) -> bool:
        raise NotImplementedError

    def _serve_command(self) -> "list[str]":
        raise NotImplementedError

    def _start(self) -> None:
        if self.process is not None and self.process.poll() is None:
            return
        if not self._ready():
            raise RuntimeError("model_runtime_not_ready")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            self._serve_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=flags,
        )
        assert self.process.stdout is not None and self.process.stderr is not None
        threading.Thread(target=self._reader, args=(self.process.stdout,), daemon=True).start()
        threading.Thread(target=self._stderr_reader, args=(self.process.stderr,), daemon=True).start()
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("model_worker_start_failed")
            try:
                payload = self.responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if payload.get("ready") is True:
                return
        self.close()
        raise TimeoutError("model_worker_start_timeout")

    def _roundtrip(self, request: dict) -> dict:
        with self.lock:
            self._start()
            assert self.process is not None and self.process.stdin is not None
            request_id = str(request.get("id") or "")
            self.process.stdin.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
            deadline = time.monotonic() + SCORE_TIMEOUT_S
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("model_worker_exited")
                try:
                    payload = self.responses.get(timeout=min(0.25, max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    continue
                if payload.get("id") != request_id:
                    continue
                if payload.get("ok") is not True:
                    raise RuntimeError("model_worker_bad_response")
                return payload
            self.close()
            raise TimeoutError("model_worker_score_timeout")

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
            except Exception:
                pass


class ExternalCrossScorer(_WorkerProcess):
    """cross_encoder 打分客户端：request ``{"pairs"}`` → response ``{"scores"}``。"""

    def _ready(self) -> bool:
        return external_runtime_ready(self.paths)

    def _serve_command(self) -> "list[str]":
        return [str(runtime_python(self.paths)), str(worker_script(self.paths)),
                "--serve", str(model_dir(self.paths))]

    def __call__(self, pairs: "Sequence[tuple[str, str]]") -> "list[float]":
        request = {"id": uuid.uuid4().hex, "pairs": [[str(a), str(b)] for a, b in pairs]}
        payload = self._roundtrip(request)
        scores = payload.get("scores")
        if not isinstance(scores, list):
            raise RuntimeError("model_worker_bad_response")
        return [float(value) for value in scores]


class ExternalEmbedder(_WorkerProcess):
    """dense 嵌入客户端（``--embed`` worker）：request ``{"texts"}`` → response ``{"vectors"}``。

    文本截断与 vector_index.MAX_TEXT_CHARS / recall_api 的 12000 防御逐字同源。"""

    MAX_TEXT_CHARS = 12000

    def _ready(self) -> bool:
        return external_embed_ready(self.paths)

    def _serve_command(self) -> "list[str]":
        return [str(runtime_python(self.paths)), str(worker_script(self.paths)),
                "--serve", str(embed_model_dir(self.paths)), "--embed"]

    def __call__(self, texts: "Sequence[str]") -> "list[list[float]]":
        batch = [str(t)[: self.MAX_TEXT_CHARS] for t in texts]
        payload = self._roundtrip({"id": uuid.uuid4().hex, "texts": batch})
        vectors = payload.get("vectors")
        if not isinstance(vectors, list) or len(vectors) != len(batch):
            raise RuntimeError("model_worker_bad_response")
        return [[float(x) for x in vec] for vec in vectors]


_WORKERS: "list[_WorkerProcess]" = []


def external_cross_scorer(paths: "AppPaths | None" = None) -> ExternalCrossScorer:
    scorer = ExternalCrossScorer(paths)
    _WORKERS.append(scorer)
    return scorer


def external_embedder(paths: "AppPaths | None" = None) -> ExternalEmbedder:
    embedder = ExternalEmbedder(paths)
    _WORKERS.append(embedder)
    return embedder


@atexit.register
def _close_workers() -> None:
    for worker in list(_WORKERS):
        worker.close()
