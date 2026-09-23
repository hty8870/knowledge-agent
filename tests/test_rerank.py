"""可选 LLM 重排层单测：重点钉死 ID 交集守卫（幻觉/掉/重复/空/异常都不越界不报错）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval import rerank  # noqa: E402


def _rec(name: str) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={},
    )


def _cands(n: int) -> list[RetrievedCandidate]:
    return [RetrievedCandidate(record=_rec(f"D{i}"), score=float(n - i)) for i in range(n)]


def _names(cands):
    return [c.record.dataset_name for c in cands]


def _is_permutation(out, inp):
    return sorted(id(c) for c in out) == sorted(id(c) for c in inp)


# ---------- off backend ----------
def test_off_is_identity():
    c = _cands(5)
    out = rerank.rerank_candidates("q", c, backend="off")
    assert out == c and _names(out) == ["D0", "D1", "D2", "D3", "D4"]


def test_off_truncates_top_k():
    c = _cands(5)
    out = rerank.rerank_candidates("q", c, backend="off", top_k=3)
    assert _names(out) == ["D0", "D1", "D2"]


def test_unknown_backend_is_safe_noop():
    c = _cands(4)
    out = rerank.rerank_candidates("q", c, backend="does-not-exist")
    assert _names(out) == _names(c)


def test_empty_candidates():
    assert rerank.rerank_candidates("q", [], backend="llm") == []


# ---------- parse_order ----------
def test_parse_order_basic():
    assert rerank.parse_order("[3,1,2]", 3) == [2, 0, 1]


def test_parse_order_dedup_and_bounds():
    # 1-based; 9 越界丢弃；重复 1 只留一次
    assert rerank.parse_order("3 1 1 9 2", 3) == [2, 0, 1]


def test_parse_order_tolerates_prose():
    assert rerank.parse_order("我认为顺序是 2, 然后 1, 最后 3。", 3) == [1, 0, 2]


def test_parse_order_empty():
    assert rerank.parse_order("no numbers here", 3) == []


# ---------- apply_order: 永远是输入的排列，绝不丢/加成员 ----------
def test_apply_order_appends_omitted():
    c = _cands(5)
    out = rerank.apply_order([2, 0], c)  # LLM 只给了 2 个
    assert _is_permutation(out, c)
    assert _names(out)[:2] == ["D2", "D0"]
    assert set(_names(out)) == {f"D{i}" for i in range(5)}  # 其余补回


# ---------- llm backend with injected caller ----------
def test_llm_valid_permutation_reorders():
    c = _cands(3)
    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=lambda p: "[3,1,2]")
    assert _names(out) == ["D2", "D0", "D1"]
    assert _is_permutation(out, c)


def test_llm_hallucinated_ids_never_violate():
    c = _cands(3)
    # 返回集合外编号 (7,8) + 掉 1 个 (只给 2)
    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=lambda p: "[7, 2, 8]")
    assert _is_permutation(out, c)          # 幻觉 ID 被丢，无集合外记录
    assert _names(out)[0] == "D1"           # 唯一有效编号 2 -> D1 置顶
    assert set(_names(out)) == {"D0", "D1", "D2"}


def test_llm_duplicate_ids_dedup():
    c = _cands(3)
    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=lambda p: "[2,2,2,1]")
    assert _is_permutation(out, c)
    assert _names(out)[:2] == ["D1", "D0"]


def test_llm_empty_response_falls_back():
    c = _cands(4)
    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=lambda p: "")
    assert _names(out) == _names(c)         # 回退原序


def test_llm_none_response_falls_back():
    c = _cands(4)
    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=lambda p: None)
    assert _names(out) == _names(c)


def test_llm_caller_exception_falls_back():
    c = _cands(4)

    def boom(_p):
        raise RuntimeError("network down")

    out = rerank.rerank_candidates("q", c, backend="llm", llm_call=boom)
    assert _names(out) == _names(c)         # 异常不外泄，回退原序


def test_llm_truncates_top_k():
    c = _cands(5)
    out = rerank.rerank_candidates("q", c, backend="llm", top_k=2, llm_call=lambda p: "[5,4,3,2,1]")
    assert _names(out) == ["D4", "D3"]
    assert set(_names(out)).issubset({f"D{i}" for i in range(5)})


def test_no_key_default_caller_falls_back():
    # 不注入 llm_call、config=None（无 key）→ _default_llm_call 返回 None → 回退原序
    c = _cands(3)
    out = rerank.rerank_candidates("q", c, backend="llm", config=None)
    assert _names(out) == _names(c)


# ---------------------------------------------------------------------------
