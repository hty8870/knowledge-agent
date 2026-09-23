"""语料级向量索引单测：全隔离（注入假语料/确定性假嵌入器/临时索引路径），不碰真实模型与语料。

钉死：首用构建→落盘→roundtrip；按条目文本指纹增量重建（未变条目零重嵌、漂移条目单独重嵌、
消失 uid 剔除）；坏档/版本不符整档重建；查询侧命中用存向量、未命中现场补嵌**仅内存不持久化**；
任何畸形（短输出/错维度/嵌入器不可用）→ None（调用方回退规则序）。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import vector_index as vi  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402


def _rec(name: str, uid: str = "", desc: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=desc or f"desc of {name}",
        raw={"dataset_uid": uid} if uid else {},
    )


def _cand(rec: DatasetRecord, score: float = 1.0) -> RetrievedCandidate:
    return RetrievedCandidate(record=rec, score=score)


class _StubEmbedder:
    """确定性假嵌入器：sha256(text) 循环填 384 维；seen 记录全部嵌入文本（断增量用）。"""

    def __init__(self, dims: int = vi.EMBED_DIMENSIONS, truncate_at: "int | None" = None):
        self.dims = dims
        self.truncate_at = truncate_at  # 模拟畸形：只回前 N 条向量
        self.seen: "list[str]" = []

    def __call__(self, texts):
        texts = list(texts)
        self.seen.extend(texts)
        if self.truncate_at is not None:
            texts = texts[: self.truncate_at]
        out = []
        for t in texts:
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            out.append([digest[i % len(digest)] / 255.0 for i in range(self.dims)])
        return out


@pytest.fixture()
def idx_file(tmp_path, monkeypatch):
    """索引落盘重定向到临时目录；用例前后清进程内缓存（防跨用例串扰）。"""
    path = tmp_path / "vector_index" / "corpus_vectors.test.json.gz"
    monkeypatch.setattr(vi, "index_path", lambda paths=None: path)
    vi.reset_caches_for_test()
    yield path
    vi.reset_caches_for_test()


def _read_store(path: Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)["vectors"]


# ---------- ensure_index：首用构建 + 落盘 roundtrip ----------

def test_ensure_index_builds_and_persists(idx_file):
    recs = [_rec("A", "uid-a"), _rec("B", "uid-b"), _rec("C", "uid-c")]
    enc = _StubEmbedder()
    store = vi.ensure_index(recs, embedder=enc)
    assert store is not None and set(store) == {"uid-a", "uid-b", "uid-c"}
    for entry in store.values():
        assert len(entry["v"]) == vi.EMBED_DIMENSIONS and entry["h"]
    assert idx_file.exists()
    assert set(_read_store(idx_file)) == {"uid-a", "uid-b", "uid-c"}
    assert len(enc.seen) == 3  # 语料一次性整批嵌入


def test_ensure_index_second_run_zero_reembed(idx_file):
    recs = [_rec("A", "uid-a"), _rec("B", "uid-b")]
    vi.ensure_index(recs, embedder=_StubEmbedder())
    enc2 = _StubEmbedder()
    store = vi.ensure_index(recs, embedder=enc2)
    assert set(store) == {"uid-a", "uid-b"}
    assert enc2.seen == []  # 全部命中索引，零重嵌


def test_ensure_index_only_stale_reembedded(idx_file):
    vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b", desc="old desc")],
                    embedder=_StubEmbedder())
    enc2 = _StubEmbedder()
    recs_v2 = [_rec("A", "uid-a"), _rec("B", "uid-b", desc="new desc"), _rec("C", "uid-c")]
    store = vi.ensure_index(recs_v2, embedder=enc2)
    assert set(store) == {"uid-a", "uid-b", "uid-c"}
    assert len(enc2.seen) == 2  # 只有文本漂移的 B 与新增的 C 重嵌
    assert sum(("new desc" in t) or ("desc of C" in t) for t in enc2.seen) == 2


def test_ensure_index_drops_removed_uid(idx_file):
    vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b")], embedder=_StubEmbedder())
    store = vi.ensure_index([_rec("A", "uid-a")], embedder=_StubEmbedder())
    assert set(store) == {"uid-a"}
    assert set(_read_store(idx_file)) == {"uid-a"}  # 消失 uid 同步剔出落盘档


def test_corrupt_index_triggers_full_rebuild(idx_file):
    idx_file.parent.mkdir(parents=True, exist_ok=True)
    idx_file.write_bytes(b"not a gzip")
    enc = _StubEmbedder()
    store = vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b")], embedder=enc)
    assert set(store) == {"uid-a", "uid-b"}
    assert len(enc.seen) == 2  # 坏档 → 整档重建


def test_model_dims_mismatch_triggers_full_rebuild(idx_file):
    idx_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": {"model": "other-model", "dimensions": 2, "created_at": "x", "count": 1},
               "vectors": {"uid-a": {"h": "zzz", "v": [0.1, 0.2]}}}
    with gzip.open(idx_file, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh)
    enc = _StubEmbedder()
    store = vi.ensure_index([_rec("A", "uid-a")], embedder=enc)
    assert set(store) == {"uid-a"} and len(enc.seen) == 1
    with gzip.open(idx_file, "rt", encoding="utf-8") as fh:
        meta = json.load(fh)["meta"]
    assert meta["model"] == vi.DEFAULT_EMBEDDING_MODEL
    assert meta["dimensions"] == vi.EMBED_DIMENSIONS


def test_records_without_uid_skipped(idx_file):
    recs = [_rec("A", "uid-a"), _rec("NoUid")]  # raw={} → 无 uid，不进索引
    store = vi.ensure_index(recs, embedder=_StubEmbedder())
    assert set(store) == {"uid-a"}


# ---------- ensure_index：畸形嵌入输出 → None ----------

def test_build_short_output_returns_none(idx_file):
    bad = _StubEmbedder(truncate_at=1)  # 请求 2 条只回 1 条
    assert vi.ensure_index([_rec("A", "uid-a"), _rec("B", "uid-b")], embedder=bad) is None


def test_build_bad_dims_returns_none(idx_file):
    bad = _StubEmbedder(dims=8)
    assert vi.ensure_index([_rec("A", "uid-a")], embedder=bad) is None


# ---------- index_dense_vectors：命中用存向量、未命中内存补嵌 ----------

def test_query_hits_store_and_patch_is_memory_only(idx_file, monkeypatch):
    rec_a, rec_b = _rec("A", "uid-a"), _rec("B", "uid-b")
    vi.ensure_index([rec_a], embedder=_StubEmbedder())  # 索引只覆盖 A
    monkeypatch.setattr(vi, "_load_corpus", lambda paths=None: [rec_a])
    enc2 = _StubEmbedder()
    out = vi.index_dense_vectors("查询Q", [_cand(rec_a), _cand(rec_b)], embedder=enc2)
    assert out is not None and len(out) == 3
    assert all(len(v) == vi.EMBED_DIMENSIONS for v in out)
    assert enc2.seen == ["查询Q", vi._record_text(rec_b)]  # A 命中存向量，只补嵌查询与 B
    assert set(_read_store(idx_file)) == {"uid-a"}  # B 仅内存补嵌，不持久化


def test_query_all_hit_zero_embed_of_docs(idx_file, monkeypatch):
    rec_a = _rec("A", "uid-a")
    vi.ensure_index([rec_a], embedder=_StubEmbedder())
    monkeypatch.setattr(vi, "_load_corpus", lambda paths=None: [rec_a])
    enc2 = _StubEmbedder()
    out = vi.index_dense_vectors("查询Q", [_cand(rec_a)], embedder=enc2)
    assert out is not None and len(out) == 2
    assert enc2.seen == ["查询Q"]  # 文档全命中，只嵌查询
    assert out[1] == _read_store(idx_file)["uid-a"]["v"]  # 文档向量逐位取自索引


def test_query_short_embedder_output_returns_none(idx_file, monkeypatch):
    rec_a = _rec("A", "uid-a")
    vi.ensure_index([rec_a], embedder=_StubEmbedder())
    monkeypatch.setattr(vi, "_load_corpus", lambda paths=None: [rec_a])
    bad = _StubEmbedder(truncate_at=0)  # 返回空列表
    assert vi.index_dense_vectors("Q", [_cand(rec_a)], embedder=bad) is None


def test_query_bad_dims_returns_none(idx_file, monkeypatch):
    rec_a = _rec("A", "uid-a")
    vi.ensure_index([rec_a], embedder=_StubEmbedder())
    monkeypatch.setattr(vi, "_load_corpus", lambda paths=None: [rec_a])
    bad = _StubEmbedder(dims=8)
    assert vi.index_dense_vectors("Q", [_cand(rec_a)], embedder=bad) is None


def test_embedder_unavailable_returns_none(idx_file, monkeypatch):
    monkeypatch.setattr(vi, "_local_embedder", lambda: None)
    assert vi.ensure_index([_rec("A", "uid-a")]) is None
    assert vi.index_dense_vectors("Q", [_cand(_rec("A", "uid-a"))]) is None


def test_ensure_index_batches_stale_embeds_within_worker_limit(idx_file):
    """整档重建必须分批（≤ model_worker.MAX_TEXTS）：frozen 形态外部 worker 超批即拒，
    不分批则首建永远失败、vector 召回恒回退规则序。"""
    sizes: "list[int]" = []

    class _BatchedStub(_StubEmbedder):
        def __call__(self, texts):
            texts = list(texts)
            sizes.append(len(texts))
            assert len(texts) <= vi.MAX_TEXTS, f"单批 {len(texts)} 超过 worker 上限"
            return super().__call__(texts)

    records = [_rec(f"d{i}", uid=f"u{i}") for i in range(vi.MAX_TEXTS + 60)]
    store = vi.ensure_index(records, embedder=_BatchedStub())
    assert store is not None and len(store) == vi.MAX_TEXTS + 60
    assert sizes == [vi.MAX_TEXTS, 60]  # 分批喂入且顺序拼接


def test_local_embedder_cached_across_calls(monkeypatch):
    """frozen 形态本地嵌入器必须进程内单例：external_embedder() 每次新建常驻模型子进程
    （持有到进程退出），不缓存会在连续 vector 检索间累积子进程耗尽内存。"""
    import dataset_recommender.retrieval.model_runtime as mr

    created: "list[object]" = []

    class _Enc:
        def __call__(self, texts):  # pragma: no cover - 本测试不触嵌入
            return []

    def fake_factory(*_args, **_kw):
        created.append(_Enc())
        return created[-1]

    vi._EMBEDDER_CACHE.clear()
    monkeypatch.setattr(vi, "load_embedder", lambda: None)
    monkeypatch.setattr(mr, "external_embed_ready", lambda *a, **k: True)
    monkeypatch.setattr(mr, "external_embedder", fake_factory)
    try:
        e1 = vi._local_embedder()
        e2 = vi._local_embedder()
        assert e1 is e2 and len(created) == 1
    finally:
        vi._EMBEDDER_CACHE.clear()
