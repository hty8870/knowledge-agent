"""隔离模型运行时客户端单测：不真起子进程（fake _roundtrip），钉死两个子类的协议整形、
截断防御、畸形响应 fail-closed，以及共享基座的服务命令路由。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import model_runtime as mr  # noqa: E402


def _fake_roundtrip(seen: dict, payload: dict):
    def roundtrip(self, request):
        seen.update(request)
        return {**payload, "id": request["id"], "ok": True}
    return roundtrip


# ---------- ExternalEmbedder ----------

def test_embedder_request_shape_and_12000_truncation(monkeypatch):
    seen = {}
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip(seen, {"vectors": [[0.1, 0.2], [0.3, 0.4]]}))
    out = mr.ExternalEmbedder()(["短", "x" * 13000])
    assert seen["texts"][0] == "短"
    assert len(seen["texts"][1]) == 12000  # 与 recall_api / vector_index 同款截断
    assert out == [[0.1, 0.2], [0.3, 0.4]]


def test_embedder_vector_count_mismatch_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"vectors": []}))
    with pytest.raises(RuntimeError):
        mr.ExternalEmbedder()(["a", "b"])


def test_embedder_non_list_vectors_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"vectors": "broken"}))
    with pytest.raises(RuntimeError):
        mr.ExternalEmbedder()(["a"])


# ---------- ExternalCrossScorer（重构后行为逐位不变） ----------

def test_cross_scorer_request_shape(monkeypatch):
    seen = {}
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip(seen, {"scores": [0.5]}))
    assert mr.ExternalCrossScorer()([("q", "d")]) == [0.5]
    assert seen["pairs"] == [["q", "d"]]


def test_cross_scorer_non_list_scores_raises(monkeypatch):
    monkeypatch.setattr(mr._WorkerProcess, "_roundtrip",
                        _fake_roundtrip({}, {"scores": "broken"}))
    with pytest.raises(RuntimeError):
        mr.ExternalCrossScorer()([("q", "d")])


# ---------- 服务命令路由（--embed 只挂在嵌入侧） ----------

def test_serve_commands(monkeypatch):
    monkeypatch.setattr(mr, "runtime_python", lambda paths=None: Path("py"))
    monkeypatch.setattr(mr, "worker_script", lambda paths=None: Path("worker.py"))
    monkeypatch.setattr(mr, "model_dir", lambda paths=None: Path("cross"))
    monkeypatch.setattr(mr, "embed_model_dir", lambda paths=None: Path("embed"))
    cross_cmd = mr.ExternalCrossScorer()._serve_command()
    embed_cmd = mr.ExternalEmbedder()._serve_command()
    assert cross_cmd[1:] == ["worker.py", "--serve", "cross"]
    assert embed_cmd[1:] == ["worker.py", "--serve", "embed", "--embed"]


def test_ready_gates_route_to_respective_checks(monkeypatch):
    monkeypatch.setattr(mr, "external_runtime_ready", lambda paths=None: True)
    monkeypatch.setattr(mr, "external_embed_ready", lambda paths=None: False)
    assert mr.ExternalCrossScorer()._ready() is True
    assert mr.ExternalEmbedder()._ready() is False
