from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

from dataset_recommender.retrieval import model_worker as worker


def _ready(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer_config.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"weights")


def test_ready_requires_config_tokenizer_and_nonempty_weight(tmp_path):
    root = tmp_path / "model"
    root.mkdir()
    assert not worker.model_files_ready(root)
    (root / "config.json").write_text("{}", encoding="utf-8")
    (root / "tokenizer.json").write_text("{}", encoding="utf-8")
    (root / "model.safetensors").write_bytes(b"")
    assert not worker.model_files_ready(root)
    (root / "model.safetensors").write_bytes(b"ok")
    assert worker.model_files_ready(root)


def test_pair_validation_is_bounded():
    assert worker._valid_pairs([["q", "doc"]]) == [("q", "doc")]
    assert worker._valid_pairs([]) is None
    assert worker._valid_pairs([["q"]]) is None
    assert worker._valid_pairs([["q" * (worker.MAX_QUERY_CHARS + 1), "doc"]]) is None
    assert worker._valid_pairs([["q", "d"]] * (worker.MAX_PAIRS + 1)) is None


def test_download_prefers_modelscope_and_validates_real_files(monkeypatch, tmp_path):
    source = tmp_path / "cache"
    _ready(source)
    fake = ModuleType("modelscope")
    fake.snapshot_download = lambda model_id, ignore_file_pattern: str(source)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", fake)
    target = tmp_path / "target"
    assert worker.download_model(target)
    assert worker.model_files_ready(target)


def test_download_falls_back_to_huggingface(monkeypatch, tmp_path):
    bad = ModuleType("modelscope")
    bad.snapshot_download = lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("offline"))  # type: ignore[attr-defined]
    hf = ModuleType("huggingface_hub")

    def download(*, repo_id, local_dir, ignore_patterns):
        _ready(Path(local_dir))

    hf.snapshot_download = download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", bad)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf)
    assert worker.download_model(tmp_path / "target")


# ---------- --embed 模式：嵌入请求边界 / 模型 id 路由 ----------

def test_texts_validation_is_bounded():
    assert worker._valid_texts(["q", "doc"]) == ["q", "doc"]
    assert worker._valid_texts([]) is None
    assert worker._valid_texts([1]) is None
    assert worker._valid_texts(["x" * (worker.MAX_EMBED_TEXT_CHARS + 1)]) is None
    assert worker._valid_texts(["ok"] * (worker.MAX_TEXTS + 1)) is None


def test_download_embed_uses_embed_model_id(monkeypatch, tmp_path):
    source = tmp_path / "cache"
    _ready(source)
    seen = {}
    fake = ModuleType("modelscope")

    def snap(model_id, ignore_file_pattern):
        seen["id"] = model_id
        return str(source)

    fake.snapshot_download = snap  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "modelscope", fake)
    assert worker.download_model(tmp_path / "target", model_id=worker.EMBED_MODEL_ID)
    assert seen["id"] == worker.EMBED_MODEL_ID


def test_main_embed_flag_routes_to_embed_model(monkeypatch, tmp_path):
    seen = {}

    def fake_download(target, model_id=worker.MODEL_ID):
        seen["id"] = model_id
        return True

    monkeypatch.setattr(worker, "download_model", fake_download)
    assert worker.main(["--download", str(tmp_path / "m"), "--embed"]) == 0
    assert seen["id"] == worker.EMBED_MODEL_ID
    assert worker.main(["--download", str(tmp_path / "m2")]) == 0
    assert seen["id"] == worker.MODEL_ID
