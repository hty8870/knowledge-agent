"""可选向量召回层单测：注入 mock 嵌入器（不碰真实模型/依赖），钉死安全守卫、融合排序、优雅降级。

重点：off 字节等价；dense 输出恒为输入的排列（不加不丢）；模型缺失/异常/畸形输出一律回退原序。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval import vector_recall  # noqa: E402


def _rec(name: str) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={},
    )


def _cands(n: int) -> "list[RetrievedCandidate]":
    # score = n-i 递减 → D0 词面分最高（原序 D0,D1,...）
    return [RetrievedCandidate(record=_rec(f"D{i}"), score=float(n - i)) for i in range(n)]


def _names(cands):
    return [c.record.dataset_name for c in cands]


def _is_permutation(out, inp):
    return sorted(id(c) for c in out) == sorted(id(c) for c in inp)


# 2 维 mock 向量：query=[1,0]；cosine 排序应为 D1 > D2 > D0。
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


# ---------- off / 非法 backend / 空输入：字节等价 passthrough ----------
def test_off_is_identity():
    c = _cands(5)
    out = vector_recall.recall_rerank("QRY", c, backend="off", embedder=_mock_embedder)
    assert out == c and _names(out) == ["D0", "D1", "D2", "D3", "D4"]


def test_off_truncates_top_k():
    c = _cands(5)
    out = vector_recall.recall_rerank("QRY", c, backend="off", top_k=3, embedder=_mock_embedder)
    assert _names(out) == ["D0", "D1", "D2"]


def test_unknown_backend_safe_noop():
    c = _cands(4)
    out = vector_recall.recall_rerank("QRY", c, backend="does-not-exist", embedder=_mock_embedder)
    assert _names(out) == _names(c)


def test_empty_candidates():
    assert vector_recall.recall_rerank("QRY", [], backend="dense", embedder=_mock_embedder) == []


# ---------- dense：融合排序 + 排列守卫 ----------
def test_dense_pure_semantic_reorders():
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, embedder=_mock_embedder)
    assert _names(out) == ["D1", "D2", "D0"]      # 纯稠密：cosine 高→低
    assert _is_permutation(out, c)


def test_dense_alpha_zero_is_lexical_order():
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=0.0, embedder=_mock_embedder)
    assert _names(out) == ["D0", "D1", "D2"]      # 纯词面：原确定性序不变
    assert _is_permutation(out, c)


def test_dense_alpha_clamped_above_one():
    c = _cands(3)
    # alpha=9 应被夹到 1 → 与纯稠密同序
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=9.0, embedder=_mock_embedder)
    assert _names(out) == ["D1", "D2", "D0"]


def test_dense_truncates_top_k():
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0, top_k=2, embedder=_mock_embedder)
    assert _names(out) == ["D1", "D2"]
    assert set(_names(out)).issubset({"D0", "D1", "D2"})


def test_dense_is_always_permutation():
    c = _cands(6)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=0.5, embedder=_mock_embedder)
    assert _is_permutation(out, c)
    assert set(_names(out)) == {f"D{i}" for i in range(6)}


# ---------- 优雅降级：任何不可用/异常/畸形 → 回退原序 ----------
def test_dense_model_unavailable_falls_back(tmp_path):
    # embedder=None + 指向不存在的模型目录 → load_embedder 返回 None → 回退原序（不下载、不报错）
    c = _cands(4)
    missing = tmp_path / "no_such_model_dir"
    out = vector_recall.recall_rerank("QRY", c, backend="dense", embedder=None, model_dir=str(missing))
    assert _names(out) == _names(c)


def test_dense_embedder_exception_falls_back():
    c = _cands(4)

    def boom(_texts):
        raise RuntimeError("embed backend down")

    out = vector_recall.recall_rerank("QRY", c, backend="dense", embedder=boom)
    assert _names(out) == _names(c)              # 异常不外泄，回退原序


def test_dense_malformed_length_falls_back():
    c = _cands(4)
    # 返回长度与 (query + N) 不符 → 回退，绝不错位打分
    out = vector_recall.recall_rerank("QRY", c, backend="dense", embedder=lambda ts: [[1.0, 0.0]])
    assert _names(out) == _names(c)


def test_dense_empty_vectors_falls_back():
    c = _cands(4)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", embedder=lambda ts: [])
    assert _names(out) == _names(c)


# ---------- cross_encoder 后端：注入 mock 打分器 ----------
def _mock_cross(pairs):
    """按文档里的 D{i} 标记打分：D1>D2>D0，用来验证重排。"""
    scoremap = {"D1": 3.0, "D2": 2.0, "D0": 1.0}
    out = []
    for _q, doc in pairs:
        s = 0.0
        for k, v in scoremap.items():
            if k in doc:
                s = v
                break
        out.append(s)
    return out


def test_cross_reorders_by_score():
    c = _cands(3)
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", cross_scorer=_mock_cross)
    assert _names(out) == ["D1", "D2", "D0"]     # 按打分高→低
    assert _is_permutation(out, c)


def test_cross_truncates_top_k():
    c = _cands(5)
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", cross_scorer=_mock_cross, top_k=2)
    assert len(out) == 2 and set(_names(out)).issubset({f"D{i}" for i in range(5)})
    assert _names(out)[0] == "D1"                # 最高分置顶后截断


def test_cross_model_unavailable_falls_back(tmp_path):
    c = _cands(4)
    missing = tmp_path / "no_such_cross_dir"
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", cross_scorer=None, model_dir=str(missing))
    assert _names(out) == _names(c)              # 缺模型 → 回退原序（不下载不报错）


def test_cross_scorer_exception_falls_back():
    c = _cands(4)

    def boom(_pairs):
        raise RuntimeError("reranker down")

    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", cross_scorer=boom)
    assert _names(out) == _names(c)


def test_cross_malformed_length_falls_back():
    c = _cands(4)
    out = vector_recall.recall_rerank("q", c, backend="cross_encoder", cross_scorer=lambda ps: [1.0])
    assert _names(out) == _names(c)              # 分数长度不符 → 回退，绝不错位


# ---------- 双语 query 扩展 ----------
class _FakeIntent:
    def __init__(self, dm, raw=None):
        self.display_map = dm
        self.has_raw_data_required = raw


def test_expand_query_bilingual():
    it = _FakeIntent({"species": ["Human"], "disease": ["Breast Cancer"]}, raw=True)
    q = vector_recall.expand_query_bilingual("人类乳腺癌", it)
    assert "人类乳腺癌" in q and "Human" in q and "Breast Cancer" in q and "FASTQ" in q


def test_expand_query_no_terms_is_identity():
    assert vector_recall.expand_query_bilingual("随便", _FakeIntent({})) == "随便"


# ---------- 纯函数小件 ----------
def test_cosine_basics():
    assert vector_recall._cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert vector_recall._cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert vector_recall._cosine([1.0, 0.0], []) == 0.0        # 退化/长度不符 → 0，不抛
    assert vector_recall._cosine([0.0, 0.0], [1.0, 1.0]) == 0.0  # 零向量 → 0


def test_minmax_norm():
    assert vector_recall._minmax_norm([1.0, 3.0, 2.0]) == [0.0, 1.0, 0.5]
    assert vector_recall._minmax_norm([5.0, 5.0]) == [0.0, 0.0]   # 全相等 → 全 0
    assert vector_recall._minmax_norm([]) == []


# ---------- 非有限打分（NaN/Inf）守卫：绝不静默乱序，一律回退原序 ----------
def test_cross_encoder_nan_scores_falls_back():
    # NaN 会让 sorted 静默产出乱序（真最高分不顶首）而非报错——必须显式挡掉、回退输入序。
    cands = _cands(4)  # 原序 D0..D3
    nan_scorer = lambda pairs: [float("nan"), 5.0, float("nan"), 1.0]  # noqa: E731
    out = vector_recall.recall_rerank("q", cands, backend="cross_encoder", cross_scorer=nan_scorer, intent=None)
    assert _names(out) == ["D0", "D1", "D2", "D3"]      # 回退输入序
    assert _is_permutation(out, cands)


def test_cross_encoder_inf_scores_falls_back():
    cands = _cands(3)
    inf_scorer = lambda pairs: [float("inf"), 1.0, 2.0]  # noqa: E731
    out = vector_recall.recall_rerank("q", cands, backend="cross_encoder", cross_scorer=inf_scorer, intent=None)
    assert _names(out) == ["D0", "D1", "D2"]            # 回退输入序（不让 Inf 顶首）
    assert _is_permutation(out, cands)


def test_cross_encoder_finite_scores_still_sort():
    # 有限分仍正常按分降序（守卫不误伤正常路径）。
    cands = _cands(4)
    finite = lambda pairs: [0.1, 9.0, 0.2, 3.0]  # noqa: E731
    out = vector_recall.recall_rerank("q", cands, backend="cross_encoder", cross_scorer=finite, intent=None)
    assert _names(out) == ["D1", "D3", "D2", "D0"]


# ---------- RRF 名次融合（fusion="rrf"）----------
def test_rrf_fused_order_deterministic():
    # 词面序 D0>D1>D2（分 3/2/1），稠密序 D1>D2>D0（mock 余弦）→ RRF：D1 双路都靠前拿第一，
    # D0 词面第一+稠密垫底（1/60+1/62）仍胜过 D2 的双中游（1/62+1/61）→ D1, D0, D2。
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="rrf", embedder=_mock_embedder)
    assert _names(out) == ["D1", "D0", "D2"]
    assert _is_permutation(out, c)


def test_rrf_is_scale_invariant():
    # RRF 的核心性质：只吃名次、不看分值尺度——词面分放大 1000 倍，融合序逐位不变
    # （linear 融合在 min-max 下恰好也不变，但 z-score/原始分场景会漂；这里钉死 RRF 的尺度无关）。
    c1 = _cands(3)
    out1 = vector_recall.recall_rerank("QRY", c1, backend="dense", fusion="rrf", embedder=_mock_embedder)
    c2 = _cands(3)
    for cand in c2:
        cand.score *= 1000.0
    out2 = vector_recall.recall_rerank("QRY", c2, backend="dense", fusion="rrf", embedder=_mock_embedder)
    assert _names(out1) == _names(out2)


def test_default_fusion_is_explicit_ltr_byte_equivalent(monkeypatch):
    # 默认参数（不传 fusion）必须与显式 ltr 逐位一致——ltr 是 dense/vector 的缺省融合。
    # 伪 scorer 钉住「工件可用」情形，不依赖本机 models/ltr 与 torch。
    # 特征名镜像 models/ltr/ltr_spec.json 的 feature_order 契约。
    names = (
        "lex_norm", "vec_norm", "has_vec", "inv_pos",
        "matched_species", "matched_tissue", "matched_disease",
        "matched_has_raw_data", "raw_required",
    )

    def scorer(batch):
        return [float(dict(zip(names, row))["inv_pos"]) for row in batch]

    scorer.ltr_feature_order = list(names)
    monkeypatch.setattr(vector_recall, "load_ltr_scorer", lambda *a, **k: scorer)
    intent = SimpleNamespace(constraints={}, preferred_constraints={}, has_raw_data_required=False)
    out_default = vector_recall.recall_rerank("QRY", _cands(4), backend="dense", alpha=0.5,
                                              embedder=_mock_embedder, intent=intent)
    out_ltr = vector_recall.recall_rerank("QRY", _cands(4), backend="dense", alpha=0.5,
                                          fusion="ltr", embedder=_mock_embedder, intent=intent)
    assert _names(out_default) == _names(out_ltr)


def test_default_fusion_without_artifact_is_linear_byte_equivalent(monkeypatch):
    # 工件不可用（load_ltr_scorer → None）时默认融合 fail-soft 回退 linear，
    # 与显式 linear 逐位一致——无工件环境与历史默认行为字节等价（冻结纪律）。
    monkeypatch.setattr(vector_recall, "load_ltr_scorer", lambda *a, **k: None)
    intent = SimpleNamespace(constraints={}, preferred_constraints={}, has_raw_data_required=False)
    out_default = vector_recall.recall_rerank("QRY", _cands(4), backend="dense", alpha=0.5,
                                              embedder=_mock_embedder, intent=intent)
    out_linear = vector_recall.recall_rerank("QRY", _cands(4), backend="dense", alpha=0.5,
                                             fusion="linear", embedder=_mock_embedder, intent=intent)
    assert _names(out_default) == _names(out_linear)


def test_rrf_unknown_fusion_falls_back_to_linear():
    # 非法 fusion 值不当 ltr/rrf 处理（安全默认 = 历史 linear 行为），也不许炸。
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="bogus", embedder=_mock_embedder)
    out_linear = vector_recall.recall_rerank("QRY", _cands(3), backend="dense",
                                             fusion="linear", embedder=_mock_embedder)
    assert _names(out) == _names(out_linear)


def test_rrf_nan_dense_scores_falls_back():
    # NaN 稠密分 → 回退原序（与 linear 路同一「畸形输出→回退」合同）。
    def nan_embedder(texts):
        vecs = _mock_embedder(texts)
        vecs[1] = [float("nan"), 1.0]
        return vecs
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="dense", fusion="rrf", embedder=nan_embedder)
    assert _names(out) == ["D0", "D1", "D2"]
    assert _is_permutation(out, c)


def test_rrf_trace_records_fusion():
    c = _cands(3)
    trace: dict = {}
    vector_recall.recall_rerank("QRY", c, backend="dense", fusion="rrf", embedder=_mock_embedder, trace=trace)
    assert trace.get("fusion") == "rrf"
    assert trace.get("status") == "used"


def test_rrf_fused_order_helper():
    # 助手本身：已知两路分 → 已知名次 → 已知融合序；并列按原下标稳定。
    order = vector_recall._rrf_fused_order([3.0, 2.0, 1.0], [0.1, 0.9, 0.5])
    # 词面名次 0/1/2；稠密名次 2/0/1 → 融合分 i0: 1/60+1/62, i1: 1/61+1/60, i2: 1/62+1/61
    assert order == [1, 0, 2]
    # 全同分 → 全并列 → 原序
    assert vector_recall._rrf_fused_order([1.0, 1.0], [2.0, 2.0]) == [0, 1]


# ---------- 失败不缓存：首次加载失败 → 故障消除后同进程即可恢复 ----------
class _FakeSentenceTransformer:
    def __init__(self, _path):
        pass

    def encode(self, texts, **_kw):
        return [[1.0, 0.0] for _ in texts]


class _FakeCrossEncoder:
    def __init__(self, _path, **_kw):
        pass

    def predict(self, pairs, **_kw):
        return [1.0 for _ in pairs]


def _fake_st_module(**attrs):
    import types
    mod = types.ModuleType("sentence_transformers")
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_embedder_failure_not_cached_and_recovers(tmp_path, monkeypatch):
    # 首次：模型目录不存在 → None，且**不写入负缓存**（旧行为会把 None 永久缓存，须重启才恢复）。
    vector_recall._EMBEDDER_CACHE.clear()
    model_dir = tmp_path / "emb_model"
    assert vector_recall.load_embedder(model_dir=model_dir) is None
    assert str(model_dir) not in vector_recall._EMBEDDER_CACHE
    # 故障消除（目录补齐 + 依赖可用）→ 同进程重试即成功，无需重启。
    model_dir.mkdir()
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        _fake_st_module(SentenceTransformer=_FakeSentenceTransformer))
    emb = vector_recall.load_embedder(model_dir=model_dir)
    assert emb is not None
    # 成功路径正缓存语义不变：键入缓存，且依赖被摘除后仍从缓存命中（不重新加载）。
    assert vector_recall._EMBEDDER_CACHE[str(model_dir)] is emb
    monkeypatch.delitem(sys.modules, "sentence_transformers")
    assert vector_recall.load_embedder(model_dir=model_dir) is emb
    vector_recall._EMBEDDER_CACHE.clear()


def test_cross_encoder_failure_not_cached_and_recovers(tmp_path, monkeypatch):
    vector_recall._CROSS_CACHE.clear()
    model_dir = tmp_path / "ce_model"
    assert vector_recall.load_cross_encoder(model_dir=model_dir) is None
    assert "ce::" + str(model_dir) not in vector_recall._CROSS_CACHE
    model_dir.mkdir()
    monkeypatch.setitem(sys.modules, "sentence_transformers",
                        _fake_st_module(CrossEncoder=_FakeCrossEncoder))
    scorer = vector_recall.load_cross_encoder(model_dir=model_dir)
    assert scorer is not None
    assert vector_recall._CROSS_CACHE["ce::" + str(model_dir)] is scorer
    monkeypatch.delitem(sys.modules, "sentence_transformers")
    assert vector_recall.load_cross_encoder(model_dir=model_dir) is scorer
    vector_recall._CROSS_CACHE.clear()


# ---------- P-1 文档向量缓存（默认路径， 审计）----------
# _cands(n) 造的候选文本（含 dataset_name 与 description=f"desc of {name}"）互不相同
# → 缓存键天然不同，直接复用。假 embedder 须对同一文本返回同一向量：按文本内容确定性
# 生成（ord 求和取模），勿用内置 hash()——PYTHONHASHSEED 每进程随机，跨运行不可复现。
def _recording_embedder(calls):
    """记录每次调用批次的假编码器：含 "QRY" 的查询 → [1,0]；文档 → [Σord(t) mod 97, 1]。"""

    def enc(batch):
        calls.append(list(batch))
        return [[1.0, 0.0] if "QRY" in t else [float(sum(map(ord, t)) % 97), 1.0] for t in batch]

    return enc


def test_dense_default_path_encodes_docs_once(tmp_path, monkeypatch):
    # 默认路径（embedder=None）：第一次 enc 两个批次（[query] + 缺失文档批），第二次
    # **只 [query]**——文档向量全部命中缓存不再重编码；两次输出完全一致且仍是输入的排列。
    calls: "list[list[str]]" = []
    tmp_model_dir = tmp_path / "emb"   # 不必真实存在：load_embedder 命中 _EMBEDDER_CACHE 即不查目录
    monkeypatch.setitem(vector_recall._EMBEDDER_CACHE, str(tmp_model_dir), _recording_embedder(calls))
    c1, c2 = _cands(3), _cands(3)
    try:
        out1 = vector_recall.recall_rerank("QRY", c1, backend="dense", alpha=1.0,
                                           embedder=None, model_dir=str(tmp_model_dir))
        assert len(calls) == 2 and calls[0] == ["QRY"] and len(calls[1]) == 3  # 冷缓存：查询批 + 文档批各一次
        calls.clear()
        out2 = vector_recall.recall_rerank("QRY", c2, backend="dense", alpha=1.0,
                                           embedder=None, model_dir=str(tmp_model_dir))
        assert calls == [["QRY"]]       # 第二次只现编查询——文档全命中缓存
        assert _names(out1) == _names(out2)
        assert _is_permutation(out2, c2)
    finally:
        vector_recall._DOC_VECTOR_CACHE.clear()   # 勿让缓存跨测试泄漏（monkeypatch 会回收 embedder 键）


def test_dense_injected_embedder_bypasses_cache():
    # 注入路径（embedder 显式传入）：逐位保持旧行为——每次都是**单批** enc([query, *texts])，
    # 不过文档缓存（单测/评测隔离：不同测试对同一文本的 mock 向量互不串味）。
    calls: "list[list[str]]" = []
    enc = _recording_embedder(calls)
    c1, c2 = _cands(3), _cands(3)
    texts = [vector_recall._candidate_text(c) for c in c1]
    try:
        out1 = vector_recall.recall_rerank("QRY", c1, backend="dense", alpha=1.0, embedder=enc)
        out2 = vector_recall.recall_rerank("QRY", c2, backend="dense", alpha=1.0, embedder=enc)
        assert calls == [["QRY", *texts], ["QRY", *texts]]   # 两次均为单批、含全部文本
        assert _is_permutation(out1, c1) and _is_permutation(out2, c2)
        assert vector_recall._DOC_VECTOR_CACHE == {}          # 注入路径绝不写入文档缓存
    finally:
        vector_recall._DOC_VECTOR_CACHE.clear()


def test_dense_cache_returns_identical_object(tmp_path, monkeypatch):
    # 字节等价红线：缓存存/取的都是**当初编码出的同一个 list 对象**（is 同一），绝不重算
    # ——融合数学与 _cosine 一行不动，linear 融合输出逐位不变由此钉死。
    # 注意：_recording_embedder 的 calls 只记**文本批次**；要拿到编码器**返回的向量对象**本体，
    # 本测试另存 returns（每批返回的 list[list[float]]，下标与 calls 对齐）。
    calls: "list[list[str]]" = []
    returns: "list[list[list[float]]]" = []

    def _enc(batch):
        calls.append(list(batch))
        out = [[1.0, 0.0] if "QRY" in t else [float(sum(map(ord, t)) % 97), 1.0] for t in batch]
        returns.append(out)
        return out

    tmp_model_dir = tmp_path / "emb"
    monkeypatch.setitem(vector_recall._EMBEDDER_CACHE, str(tmp_model_dir), _enc)
    c = _cands(3)
    d0_text = vector_recall._candidate_text(c[0])   # 第一个候选（D0）的打分文本 = 缓存键的文本侧
    try:
        vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0,
                                    embedder=None, model_dir=str(tmp_model_dir))
        first_doc_vecs = returns[1]         # 第一次调用：批0=[查询]、批1=文档批 → 批1 是文档向量对象
        key = (str(tmp_model_dir), d0_text)
        assert vector_recall._DOC_VECTOR_CACHE[key] is first_doc_vecs[0]   # 入缓存即原对象本体
        vector_recall.recall_rerank("QRY", _cands(3), backend="dense", alpha=1.0,
                                    embedder=None, model_dir=str(tmp_model_dir))
        assert vector_recall._DOC_VECTOR_CACHE[key] is first_doc_vecs[0]   # 命中后未被重算/替换
    finally:
        vector_recall._DOC_VECTOR_CACHE.clear()


def test_dense_cache_evicts_bounded(tmp_path, monkeypatch):
    # 有界：上限临时调到 2，编码 3 个互不相同的候选文本 → 缓存至多 2 条（最旧被逐出），
    # 且功能不崩（输出仍是输入的排列）。
    calls: "list[list[str]]" = []
    tmp_model_dir = tmp_path / "emb"
    monkeypatch.setitem(vector_recall._EMBEDDER_CACHE, str(tmp_model_dir), _recording_embedder(calls))
    monkeypatch.setattr(vector_recall, "_DOC_VECTOR_CACHE_MAX", 2)
    c = _cands(3)                          # D0/D1/D2 → 三段互不相同的候选文本
    try:
        out = vector_recall.recall_rerank("QRY", c, backend="dense", alpha=1.0,
                                          embedder=None, model_dir=str(tmp_model_dir))
        assert _is_permutation(out, c)
        assert len(vector_recall._DOC_VECTOR_CACHE) <= 2
    finally:
        vector_recall._DOC_VECTOR_CACHE.clear()


def test_cross_encoder_uses_isolated_runtime_when_main_process_has_no_heavy_dependency(tmp_path, monkeypatch):
    fake_sentence = ModuleType("sentence_transformers")  # 无 CrossEncoder → from ... import 失败
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake_sentence)
    from dataset_recommender.retrieval import model_runtime

    scorer = lambda pairs: [float(i) for i, _ in enumerate(pairs)]
    monkeypatch.setattr(model_runtime, "external_runtime_ready", lambda: True)
    monkeypatch.setattr(model_runtime, "external_cross_scorer", lambda: scorer)
    target = tmp_path / "model"
    target.mkdir()
    key = "ce::" + str(target)
    vector_recall._CROSS_CACHE.pop(key, None)
    try:
        loaded = vector_recall.load_cross_encoder(target)
        assert loaded is scorer
        assert vector_recall._CROSS_CACHE[key] is scorer
    finally:
        vector_recall._CROSS_CACHE.pop(key, None)


# ---------- vector 后端：语料级向量召回（2026-09-04 进 auto，缺省 RRF）----------
def test_vector_backend_registered():
    assert "vector" in vector_recall.RECALL_BACKENDS


def test_vector_ready_is_false_for_mcp():
    # MCP v1 不开放 vector：索引首用构建是重 IO，piped-stdio 下不预热 → ready 恒 False。
    assert vector_recall.recall_backend_ready("vector") is False


def test_vector_injected_reorders_and_is_permutation():
    c = _cands(3)
    out = vector_recall.recall_rerank("QRY", c, backend="vector", embedder=_mock_embedder)
    assert _is_permutation(out, c)
    # 缺省 ltr 在无 intent 时 fail-soft 到 rrf：与 dense+rrf 同一融合算法、同输入 → 同名次。
    out_dense_rrf = vector_recall.recall_rerank("QRY", _cands(3), backend="dense",
                                                fusion="rrf", embedder=_mock_embedder)
    assert _names(out) == _names(out_dense_rrf)


def test_vector_default_fusion_fails_soft_to_rrf():
    # vector 缺省 ltr；无 intent（约束解析缺席）→ ltr 不可用，fail-soft 回退 rrf 并留痕。
    trace: dict = {}
    vector_recall.recall_rerank("QRY", _cands(3), backend="vector",
                                embedder=_mock_embedder, trace=trace)
    assert trace.get("fusion") == "rrf"
    assert trace.get("ltr_status") == "fallback"
    assert trace.get("status") == "used"


def test_vector_explicit_linear_matches_dense():
    out_v = vector_recall.recall_rerank("QRY", _cands(4), backend="vector",
                                        fusion="linear", embedder=_mock_embedder)
    out_d = vector_recall.recall_rerank("QRY", _cands(4), backend="dense",
                                        fusion="linear", embedder=_mock_embedder)
    assert _names(out_v) == _names(out_d)


def test_vector_malformed_vectors_fall_back():
    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="vector",
                                      embedder=lambda ts: [[1.0]], trace=trace)
    assert _names(out) == ["D0", "D1", "D2"]
    assert trace.get("status") == "fallback" and trace.get("reason") == "invalid_vectors"


def test_vector_api_path_used(monkeypatch):
    from dataset_recommender.retrieval import recall_api
    monkeypatch.setattr(recall_api, "api_embed_enabled", lambda: True)
    vecs = _mock_embedder(["QRY", "D0", "D1", "D2"])
    monkeypatch.setattr(recall_api, "api_dense_vectors", lambda q, items: vecs)
    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="vector", trace=trace)
    assert trace.get("status") == "used"
    assert _is_permutation(out, c)


def test_vector_api_unavailable_falls_back(monkeypatch):
    from dataset_recommender.retrieval import recall_api
    monkeypatch.setattr(recall_api, "api_embed_enabled", lambda: True)
    monkeypatch.setattr(recall_api, "api_dense_vectors", lambda q, items: None)
    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="vector", trace=trace)
    assert _names(out) == ["D0", "D1", "D2"]
    assert trace.get("status") == "fallback" and trace.get("reason") == "api_unavailable"


def test_vector_index_path_used(monkeypatch):
    from dataset_recommender.retrieval import recall_api, vector_index
    monkeypatch.setattr(recall_api, "api_embed_enabled", lambda: False)
    vecs = _mock_embedder(["QRY", "D0", "D1", "D2"])
    monkeypatch.setattr(vector_index, "index_dense_vectors", lambda q, items: vecs)
    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="vector", trace=trace)
    assert trace.get("status") == "used"
    assert _is_permutation(out, c)


def test_vector_index_unavailable_falls_back(monkeypatch):
    from dataset_recommender.retrieval import recall_api, vector_index
    monkeypatch.setattr(recall_api, "api_embed_enabled", lambda: False)
    monkeypatch.setattr(vector_index, "index_dense_vectors", lambda q, items: None)
    c = _cands(3)
    trace: dict = {}
    out = vector_recall.recall_rerank("QRY", c, backend="vector", trace=trace)
    assert _names(out) == ["D0", "D1", "D2"]
    assert trace.get("status") == "fallback" and trace.get("reason") == "index_unavailable"
