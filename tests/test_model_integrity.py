# -*- coding: utf-8 -*-
"""模型完整性（风险 9）：READY manifest 记录关键文件大小，启动时核对。"""
from __future__ import annotations

import json
from pathlib import Path

from dataset_recommender.app.runtime_paths import AppPaths
from dataset_recommender.retrieval import model_runtime as mr
from dataset_recommender.retrieval import model_worker as worker


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


def _runtime_and_worker(paths: AppPaths) -> None:
    python = mr.runtime_python(paths)
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"python")
    script = mr.worker_script(paths)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_bytes(b"worker")


def _write_manifest(paths: AppPaths, **overrides) -> dict:
    target = mr.model_dir(paths)
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    (target / "tokenizer.json").write_text("{}", encoding="utf-8")
    (target / "model.safetensors").write_bytes(b"weights")
    manifest = {
        "schema": mr.READY_SCHEMA,
        "model_id": worker.MODEL_ID,
        "python": "3.12.13",
        "lock_sha256": "0" * 64,
        "installed_at": "2026-08-21T00:00:00+00:00",
        "runtime_bytes": 6,
        "model_bytes": 11,
        "model_files": {
            "config.json": 2,
            "tokenizer.json": 2,
            "model.safetensors": 7,
        },
    }
    manifest.update(overrides)
    mr.ready_manifest_path(paths).write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return manifest


def test_intact_manifest_and_files_are_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    assert mr.external_runtime_ready(paths)


def test_empty_weights_make_runtime_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    (mr.model_dir(paths) / "model.safetensors").write_bytes(b"")
    assert not mr.external_runtime_ready(paths)


def test_missing_tokenizer_makes_runtime_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    (mr.model_dir(paths) / "tokenizer.json").unlink()
    assert not mr.external_runtime_ready(paths)


def test_bad_manifest_model_id_makes_runtime_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths, model_id="someone-else/model")
    assert not mr.external_runtime_ready(paths)


def test_corrupt_manifest_makes_runtime_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    mr.ready_manifest_path(paths).write_text("{not json", encoding="utf-8")
    assert not mr.external_runtime_ready(paths)


def test_manifest_file_size_mismatch_makes_runtime_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    # 权重被替换成不同内容（非空但大小与 manifest 不符）→ 完整性核对必须拒绝。
    (mr.model_dir(paths) / "model.safetensors").write_bytes(b"different-weight")
    assert not mr.external_runtime_ready(paths)


# ---------- 嵌入侧就绪闸（external_embed_ready）：与重排侧同族、互不拖累 ----------

def _embed_ready(paths: AppPaths) -> dict:
    target = mr.embed_model_dir(paths)
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text("{}", encoding="utf-8")
    (target / "tokenizer.json").write_text("{}", encoding="utf-8")
    (target / "model.safetensors").write_bytes(b"weights")
    return {"config.json": 2, "tokenizer.json": 2, "model.safetensors": 7}


def test_embed_ready_with_dual_model_manifest(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths, embed_files=_embed_ready(paths))
    assert mr.external_embed_ready(paths)


def test_embed_not_ready_when_manifest_lacks_embed_files(tmp_path):
    """双模型同批安装之前的旧 manifest 没有 embed_files → 嵌入侧不就绪，重排侧不受影响。"""
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths)
    _embed_ready(paths)  # 文件在、manifest 无清单 → fail-closed
    assert not mr.external_embed_ready(paths)
    assert mr.external_runtime_ready(paths)


def test_embed_size_mismatch_makes_embed_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths, embed_files=_embed_ready(paths))
    (mr.embed_model_dir(paths) / "model.safetensors").write_bytes(b"different-weight")
    assert not mr.external_embed_ready(paths)


def test_embed_missing_dir_makes_embed_not_ready(tmp_path):
    paths = _paths(tmp_path)
    _runtime_and_worker(paths)
    _write_manifest(paths, embed_files={"config.json": 2, "tokenizer.json": 2, "model.safetensors": 7})
    assert not mr.external_embed_ready(paths)  # 清单在、实物缺 → 不就绪
