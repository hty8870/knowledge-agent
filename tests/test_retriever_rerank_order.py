"""回归：LLM 重排前先做 family 去重成"多样候选池"（先去重后重排）。

新顺序：规则过滤 → 向量召回(全集) → family 去重成池 → LLM 重排(多样池) → 截断 top_k → 终检。
钉死两点：
1) 喂给 LLM 的候选池是**家族互异**的（不是同一数据集的多个切片稀释掉有限窗口）；旧顺序会喂进 A,A,A。
2) rerank 关（默认 + 官方评测门）时**绝不**调用重排，输出与旧行为一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval import rerank as rerank_mod  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.query_parser import QueryIntent  # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever  # noqa: E402


def _rec(name: str, family: str, count: str = "0") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="scRNA", count=count, unit="Cells", has_raw_data=True, url="",
        source_file="x.json", description="d", raw={}, family_id=family,
    )


def _records() -> list[DatasetRecord]:
    # family A 有 3 个切片，且样本量最大 → 规则序里 A 的三条排最前（构造"若不去重则 top-3 全是 A"）。
    # B/C/D/E 各 1 条 → 共 5 个不同家族。
    return [
        _rec("A sec1", "fam-a", count="100000"),
        _rec("A sec2", "fam-a", count="100000"),
        _rec("A sec3", "fam-a", count="100000"),
        _rec("B", "fam-b"), _rec("C", "fam-c"), _rec("D", "fam-d"), _rec("E", "fam-e"),
    ]


def test_rerank_pool_is_family_distinct(monkeypatch):
    captured: dict = {}

    def spy(query, candidates, backend, top_k=None, config=None, **kw):
        captured["pool"] = list(candidates)
        return list(candidates)  # 原样返回，只观察喂进来的池

    monkeypatch.setattr(rerank_mod, "rerank_candidates", spy)

    retriever = DatasetRetriever(top_k=3)
    intent = QueryIntent(original_query="任意")  # 空约束 → 全部过硬过滤
    out = retriever.retrieve(_records(), intent, top_k=3, rerank_backend="llm", rerank_top_n=3)

    pool = captured.get("pool")
    assert pool is not None, "开启 LLM 重排时应调用重排"
    fams = [c.record.family_id for c in pool]
    # 关键：池必须家族互异（旧顺序会是 [fam-a, fam-a, fam-a]）
    assert len(fams) == len(set(fams)) == 3, f"池应为 3 个不同家族，实得 {fams}"
    assert fams.count("fam-a") == 1, "最高分家族 A 只以代表进池，不占满窗口"
    # 最终输出同样家族互异、≤ top_k
    out_fams = [c.record.family_id for c in out]
    assert len(out_fams) == len(set(out_fams)) and len(out) <= 3


def test_rerank_off_never_calls_rerank(monkeypatch):
    called = {"n": 0}

    def spy(*a, **k):
        called["n"] += 1
        return list(k.get("candidates") or (a[1] if len(a) > 1 else []))

    monkeypatch.setattr(rerank_mod, "rerank_candidates", spy)

    retriever = DatasetRetriever(top_k=3)
    intent = QueryIntent(original_query="任意")
    out = retriever.retrieve(_records(), intent, top_k=3)  # 不传 rerank_backend → off
    assert called["n"] == 0, "rerank 关（默认/评测门）时绝不应调用重排"
    out_fams = [c.record.family_id for c in out]
    assert len(out_fams) == len(set(out_fams)) and len(out) <= 3  # 输出仍家族互异
