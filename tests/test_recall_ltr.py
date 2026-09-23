# -*- coding: utf-8 -*-
"""LTR 融合（`fusion="ltr"`）专项单测：回退字节等价、排列保证、intent/畸形分守卫、确定性、正缓存纪律。

隔离保证：全部用注入的 fake embedder / fake scorer，不依赖真实模型工件、不联网、确定性。
真工件冒烟单列为可选测试，缺 `models/ltr/ltr_spec.json` 即 skip。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval import vector_recall  # noqa: E402


#: 训练侧契约的特征顺序（models/ltr/ltr_spec.json 的 feature_order，逐位一致）。
LTR_FEATURE_ORDER = (
    "lex_norm",
    "vec_norm",
    "has_vec",
    "inv_pos",
    "matched_species",
    "matched_tissue",
    "matched_disease",
    "matched_has_raw_data",
    "raw_required",
)


def _rec(name: str) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={},
    )


def _cands(n: int) -> "list[RetrievedCandidate]":
    # score = n-i 递减 → D0 词面分最高（原序 D0,D1,...）
    return [RetrievedCandidate(record=_rec(f"D{i}"), score=float(n - i)) for i in range(n)]


def _names(cands) -> list[str]:
    return [c.record.dataset_name for c in cands]


def _is_permutation(out, inp) -> bool:
    return sorted(id(c) for c in out) == sorted(id(c) for c in inp)


# 2 维 mock 向量：query=[1,0]；cosine 排序应为 D1 > D2 > D0（与既有 vector_recall 单测同构）。
_VECS = {"QRY": [1.0, 0.0], "D0": [0.2, 1.0], "D1": [1.0, 0.2], "D2": [0.7, 0.7]}


def _mock_embedder(texts):
    out = []
    for t in texts:
        vec = [0.0, 0.0]
        for key, v in _VECS.items():
            if key in t:
                vec = v
                break
        out.append(vec)
    return out


class _FakeIntent:
    """最小 intent：只带 `_ltr_fused_order` 读的三个属性（constraints/preferred/raw）。"""

    def __init__(self, constraints=None, preferred=None, raw=False):
        self.constraints = constraints if constraints is not None else {"species": ["human"]}
        self.preferred_constraints = preferred if preferred is not None else {"tissue": ["breast"]}
        self.has_raw_data_required = raw


def _make_scorer(weights: dict):
    """确定性伪 scorer：按自身 feature_order 把行值映射回特征名，再做加权和。"""
    names = list(LTR_FEATURE_ORDER)

    def scorer(batch):
        out = []
        for row in batch:
            feats = dict(zip(names, row))
            out.append(sum(w * feats.get(name, 0.0) for name, w in weights.items()))
        return out

    scorer.ltr_feature_order = names
    return scorer


def _linear_names(n: int = 4) -> list[str]:
    return _names(vector_recall.recall_rerank(
        "QRY", _cands(n), backend="dense", fusion="linear", embedder=_mock_embedder,
    ))


def _patch_scorer(monkeypatch, scorer) -> None:
    """把模块级 load_ltr_scorer 换成返回固定 scorer（`_ltr_fused_order` 无参调用它）。"""
    monkeypatch.setattr(vector_recall, "load_ltr_scorer", lambda *a, **k: scorer)


# ---------- 注册与回退字节等价 ----------
def test_ltr_registered_in_fusions():
    assert "ltr" in vector_recall.RECALL_FUSIONS


def test_ltr_scorer_unavailable_matches_linear_and_traces_fallback(monkeypatch):
    # 模型不可用（load_ltr_scorer → None）→ 回退 dense 默认融合，且与 fusion="linear" 逐位同序。
    _patch_scorer(monkeypatch, None)
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    assert _names(out) == _linear_names(4)
    assert _is_permutation(out, c)
    assert trace["fusion"] == "linear"
    assert trace["ltr_status"] == "fallback"
    assert trace["status"] == "used"


def test_ltr_fallback_uses_rrf_for_vector_backend(monkeypatch):
    # vector 后端的默认融合是 rrf——ltr 不可用时回退目标随之改变（不是一律 linear）。
    _patch_scorer(monkeypatch, None)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", _cands(3), backend="vector", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    ref = vector_recall.recall_rerank("QRY", _cands(3), backend="vector", fusion="rrf",
                                      embedder=_mock_embedder)
    assert _names(out) == _names(ref)
    assert trace["fusion"] == "rrf"
    assert trace["ltr_status"] == "fallback"


# ---------- 成功路径：排列保证 + 确定性 ----------
def test_ltr_success_is_permutation_and_traced(monkeypatch):
    # 伪分只吃 inv_pos 的负值 → 越靠后分越高 → 恰好逆序，证明走的是 scorer 而非原序。
    _patch_scorer(monkeypatch, _make_scorer({"inv_pos": -1.0}))
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    assert _is_permutation(out, c)
    assert _names(out) == ["D3", "D2", "D1", "D0"]
    assert trace["fusion"] == "ltr"
    assert trace["status"] == "used"
    assert "ltr_status" not in trace


def test_ltr_output_is_deterministic(monkeypatch):
    _patch_scorer(monkeypatch, _make_scorer({"lex_norm": 1.0, "vec_norm": 2.0, "inv_pos": -0.5}))
    c = _cands(5)
    out1 = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                       embedder=_mock_embedder, intent=_FakeIntent())
    out2 = vector_recall.recall_rerank("QRY", _cands(5), backend="dense", fusion="ltr",
                                       embedder=_mock_embedder, intent=_FakeIntent())
    assert _names(out1) == _names(out2)
    assert _is_permutation(out1, c)


# ---------- 守卫：intent / 畸形打分 ----------
def test_ltr_intent_none_falls_back_to_linear(monkeypatch):
    # 无解析约束时特征处于训练分布外：必须在 load/打分**之前**短路回退。
    calls: list = []

    def scorer(batch):
        calls.append(batch)
        return [0.0] * len(batch)

    scorer.ltr_feature_order = list(LTR_FEATURE_ORDER)
    _patch_scorer(monkeypatch, scorer)
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=None, trace=trace)
    assert _names(out) == _linear_names(4)
    assert trace["fusion"] == "linear"
    assert trace["ltr_status"] == "fallback"
    assert calls == []


def test_ltr_nan_scores_fall_back(monkeypatch):
    base = _make_scorer({"lex_norm": 1.0})

    def nan_scorer(batch):
        out = base(batch)
        if out:
            out[0] = float("nan")
        return out

    nan_scorer.ltr_feature_order = list(LTR_FEATURE_ORDER)
    _patch_scorer(monkeypatch, nan_scorer)
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    assert _names(out) == _linear_names(4)
    assert _is_permutation(out, c)
    assert trace["fusion"] == "linear"
    assert trace["ltr_status"] == "fallback"


def test_ltr_length_mismatch_falls_back(monkeypatch):
    def short_scorer(batch):
        return [1.0]  # 长度与存活集不符 → 绝不错位打分

    short_scorer.ltr_feature_order = list(LTR_FEATURE_ORDER)
    _patch_scorer(monkeypatch, short_scorer)
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    assert _names(out) == _linear_names(4)
    assert _is_permutation(out, c)
    assert trace["ltr_status"] == "fallback"


def test_ltr_scorer_exception_falls_back(monkeypatch):
    # 打分器抛异常由 recall_rerank 的外层兜底：回退输入序、留痕 runtime_error；
    # 且 trace 必须如实反映回退后的融合（外层兜底补齐 fusion/ltr_status 改写）。
    def boom(batch):
        raise RuntimeError("scorer exploded")

    boom.ltr_feature_order = list(LTR_FEATURE_ORDER)
    _patch_scorer(monkeypatch, boom)
    c = _cands(4)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                      embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
    assert _names(out) == _names(c)
    assert _is_permutation(out, c)
    assert trace["status"] == "fallback"
    assert trace["reason"] == "runtime_error"
    assert trace["fusion"] == "linear"
    assert trace["ltr_status"] == "fallback"


# ---------- 缓存纪律：失败不入正缓存，每次真实重试 ----------
def test_ltr_scorer_missing_dir_not_cached_and_retries(tmp_path, monkeypatch):
    vector_recall._LTR_SCORER_CACHE.clear()
    missing = tmp_path / "no_such_ltr_dir"
    checks: list[str] = []
    real_is_file = Path.is_file

    def spy(self):
        checks.append(str(self))
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", spy)
    try:
        assert vector_recall.load_ltr_scorer(missing) is None
        assert str(missing) not in vector_recall._LTR_SCORER_CACHE
        assert vector_recall.load_ltr_scorer(missing) is None
        assert str(missing) not in vector_recall._LTR_SCORER_CACHE
        assert len(checks) == 2  # 两次调用都真的回查了文件系统 → 无负缓存
    finally:
        vector_recall._LTR_SCORER_CACHE.clear()


# ---------- 可选：真工件冒烟（缺工件即 skip） ----------
def test_real_artifact_smoke():
    pytest.importorskip("torch")
    repo_root = Path(__file__).resolve().parents[1]
    if not (repo_root / "models" / "ltr" / "ltr_spec.json").is_file():
        pytest.skip("models/ltr/ltr_spec.json not present")
    key = str(vector_recall.default_ltr_dir())
    try:
        scorer = vector_recall.load_ltr_scorer()
        assert scorer is not None
        assert list(scorer.ltr_feature_order) == list(LTR_FEATURE_ORDER)
        assert vector_recall.load_ltr_scorer() is scorer  # 成功路径正缓存：同一对象
        c = _cands(5)
        trace: dict = {}
        out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="ltr",
                                          embedder=_mock_embedder, intent=_FakeIntent(), trace=trace)
        assert _is_permutation(out, c)
        assert trace["fusion"] == "ltr"
        assert trace["status"] == "used"
    finally:
        vector_recall._LTR_SCORER_CACHE.pop(key, None)
