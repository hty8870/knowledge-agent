"""可选「API 向量召回」数据源（recall_api）mock 单测：钉死方案A 四路 fail-closed 回退 + 成功路。

全部用 monkeypatch，绝不真联网。覆盖点：
- dense 成功路：语料向量文件命中部分条目、文件外条目走查询侧 API 补嵌、按 API 向量重排；
- dense 回退：API 返回 None / 限流 / 向量文件版本不匹配 → 回退原序 + trace 留痕；
- rerank 成功路 / 畸形响应回退；
- env 全 off → api_embed_enabled/api_rerank_enabled 为 False、_post 零调用、走本地模型缺失回退；
- usage 落账只记 ts/kind/model/total_tokens，绝不落查询明文。
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.app import runtime_paths  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval import recall_api, vector_recall  # noqa: E402

#: 与 recall_api._embed_dims() 默认 1024 对齐；所有 mock 向量都用该维度，避免维度校验打回。
_DIMS = 1024

#: recall_api 全部环境变量（清空用，保证测试隔离不受 ambient env 影响）。
_RECALL_ENVS = [
    "BIODATA_EMBED_API", "BIODATA_RERANK_API", "BIODATA_EMBED_MODEL",
    "BIODATA_EMBED_DIMENSIONS", "BIODATA_EMBED_VECTOR_FILE", "BIODATA_EMBED_API_KEY",
    "BIODATA_EMBED_BASE_URL", "BIODATA_RERANK_MODEL", "BIODATA_EMBED_QPM",
    "BIODATA_RERANK_QPM",
]


def _clear_recall_env(monkeypatch) -> None:
    for name in _RECALL_ENVS:
        monkeypatch.delenv(name, raising=False)


def _rec(name: str, uid: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={"dataset_uid": uid},
    )


def _cands(n: int) -> "list[RetrievedCandidate]":
    # score 递减 → 词面原序 D0,D1,...；uid 与 dataset_name 对应，供向量文件按 uid 写条目。
    return [RetrievedCandidate(record=_rec(f"D{i}", f"uid{i}"), score=float(n - i)) for i in range(n)]


def _names(cands) -> list[str]:
    return [c.record.dataset_name for c in cands]


#: 2 维有效方向 + 其余补 0 的 1024 维向量：query=[1,0]；余弦序 D1(0.98) > D2(0.71) > D0(0.20)。
_VECS = {
    "QRY": [1.0, 0.0],
    "D0": [0.2, 1.0],
    "D1": [1.0, 0.2],
    "D2": [0.7, 0.7],
}


def _vec(x: float, y: float) -> list[float]:
    v = [0.0] * _DIMS
    v[0], v[1] = x, y
    return v


def _embed_vec_for(text: str) -> list[float]:
    for key, (x, y) in _VECS.items():
        if key in text:
            return _vec(x, y)
    return [0.0] * _DIMS


def _write_vectors(tmp_path: Path, *, model: str = "embedding-3", dims: int = _DIMS,
                   entries: dict | None = None) -> Path:
    entries = entries or {}
    payload = {
        "meta": {"model": model, "dimensions": dims,
                 "created_at": "2026-08-25T00:00:00Z", "count": len(entries)},
        "vectors": entries,
    }
    path = tmp_path / "corpus_vectors.json.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


# ---------- dense 成功路：文件命中 + 文件外 API 补嵌 + 按 API 向量重排 ----------
def test_dense_api_success_reorders_and_embeds_missing(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_EMBED_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")

    c = _cands(3)
    text0 = vector_recall._candidate_text(c[0])
    # 只把 uid0 写进文件（含正确 text sha + 向量），uid1/uid2 不在文件 → 应走 API 补嵌。
    # uid 取值口径 = record.raw["dataset_uid"]（DatasetRecord 是 slots dataclass，无
    # dataset_uid 属性；审查修正后文件命中分支真实可达）。
    entries = {"uid0": {"h": recall_api.candidate_text_sha(text0), "v": _vec(0.2, 1.0)}}
    vfile = _write_vectors(tmp_path, entries=entries)
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(vfile))

    calls: list[str] = []

    def _post(path, payload, timeout_s):
        calls.append(path)
        texts = payload["input"]
        data = [{"index": i, "embedding": _embed_vec_for(t)} for i, t in enumerate(texts)]
        return {"data": data, "usage": {"total_tokens": sum(len(t) for t in texts)}}

    monkeypatch.setattr(recall_api, "_post", _post)

    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, trace=trace)

    assert trace["status"] == "used"
    assert _names(out) == ["D1", "D2", "D0"]            # 按 API 向量余弦重排
    # 文件未覆盖（或版本外）的条目走查询侧 API 补嵌 → /embeddings 被调用
    assert "/embeddings" in calls


# ---------- dense 回退：API 返回 None ----------
def test_dense_api_unavailable_falls_back(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_EMBED_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    vfile = _write_vectors(tmp_path, entries={"uid0": {"h": "x" * 16, "v": _vec(0.0, 0.0)}})
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(vfile))
    monkeypatch.setattr(recall_api, "_post", lambda *a, **k: None)

    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, trace=trace)

    assert _names(out) == ["D0", "D1", "D2"]            # 回退原序
    assert trace["status"] == "fallback"
    assert trace["reason"] == "api_unavailable"


# ---------- dense 回退：进程级限流 ----------
def test_dense_api_rate_limited_falls_back(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_EMBED_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    vfile = _write_vectors(tmp_path, entries={"uid0": {"h": "x" * 16, "v": _vec(0.0, 0.0)}})
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(vfile))

    posted: list[object] = []
    monkeypatch.setattr(recall_api, "_post", lambda *a, **k: posted.append(a) or None)
    monkeypatch.setattr(recall_api._EMBED_QPM, "allow", lambda: False)

    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, trace=trace)

    assert _names(out) == ["D0", "D1", "D2"]
    assert trace["status"] == "fallback"
    assert trace["reason"] == "api_unavailable"
    assert posted == []                                 # 限流在 _post 之前拦截


# ---------- dense 回退：向量文件版本不匹配 ----------
def test_dense_vector_version_mismatch_falls_back(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_EMBED_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    vfile = _write_vectors(tmp_path, model="other-model", entries={"uid0": {"h": "x" * 16, "v": _vec(0.0, 0.0)}})
    monkeypatch.setenv("BIODATA_EMBED_VECTOR_FILE", str(vfile))

    posted: list[object] = []
    monkeypatch.setattr(recall_api, "_post", lambda *a, **k: posted.append(a) or None)

    assert recall_api._load_vectors() is None           # meta model 不符 → 拒启用

    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, trace=trace)

    assert _names(out) == ["D0", "D1", "D2"]
    assert trace["status"] == "fallback"
    assert trace["reason"] == "api_unavailable"
    assert posted == []                                 # 文件拒启用后不再打 API


# ---------- rerank 成功路 ----------
def test_rerank_api_success_reorders(monkeypatch):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_RERANK_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")

    def _post(path, payload, timeout_s):
        assert path == "/rerank"
        scoremap = {"D1": 3.0, "D2": 2.0, "D0": 1.0}
        results = []
        for i, doc in enumerate(payload["documents"]):
            s = 0.0
            for k, v in scoremap.items():
                if k in doc:
                    s = v
                    break
            results.append({"index": i, "relevance_score": s})
        return {"results": results, "usage": {"total_tokens": 1}}

    monkeypatch.setattr(recall_api, "_post", _post)

    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", trace=trace)

    assert trace["status"] == "used"
    assert _names(out) == ["D1", "D2", "D0"]            # 按 rerank 分降序


# ---------- rerank 畸形响应回退 ----------
def test_rerank_api_malformed_falls_back(monkeypatch):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_RERANK_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    # 只覆盖 1/3 个候选 → api_rerank_scores 检出部分缺分 → 回退
    monkeypatch.setattr(recall_api, "_post",
                        lambda *a, **k: {"results": [{"index": 0, "relevance_score": 1.0}], "usage": {}})

    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", trace=trace)

    assert _names(out) == ["D0", "D1", "D2"]
    assert trace["status"] == "fallback"
    assert trace["reason"] == "api_unavailable"


# ---------- env off 零影响：不启用、零 _post 调用、走本地模型缺失回退 ----------
def test_env_off_zero_effect(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)

    assert recall_api.api_embed_enabled() is False
    assert recall_api.api_rerank_enabled() is False

    posted: list[object] = []
    monkeypatch.setattr(recall_api, "_post", lambda *a, **k: posted.append(a) or None)

    c = _cands(3)
    missing = tmp_path / "no_such_model"
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense",
                                      embedder=None, model_dir=str(missing), trace=trace)

    assert _names(out) == ["D0", "D1", "D2"]
    assert trace["status"] == "fallback"
    assert trace["reason"] == "model_or_dependency_unavailable"
    assert posted == []                                 # env off 时绝不碰 _post


# ---------- usage 落账：只记 ts/kind/model/total_tokens，绝不落查询明文 ----------
def test_usage_log_excludes_query_plaintext(monkeypatch, tmp_path):
    recall_api.reset_caches_for_test()
    _clear_recall_env(monkeypatch)
    monkeypatch.setenv("BIODATA_RERANK_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    # 把 usage 落账重定向到 per-test 临时目录，不污染真实 .userdata/
    monkeypatch.setattr(runtime_paths, "get_app_paths",
                        lambda: SimpleNamespace(userdata_dir=tmp_path))

    query = "绝密查询明文Q12345XYZ"

    def _post(path, payload, timeout_s):
        docs = payload["documents"]
        return {
            "results": [{"index": i, "relevance_score": float(len(docs) - i)} for i in range(len(docs))],
            "usage": {"total_tokens": 42},
        }

    monkeypatch.setattr(recall_api, "_post", _post)

    c = _cands(2)
    vector_recall.recall_rerank(query, c, backend="cross_encoder")

    log = tmp_path / "embed_usage.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").splitlines()
    assert lines
    for line in lines:
        obj = json.loads(line)
        assert set(obj.keys()) == {"ts", "kind", "model", "total_tokens"}
        assert query not in line                          # 查询明文绝不落账
