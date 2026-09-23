# -*- coding: utf-8 -*-
"""查询复杂度分类器（strategy.py）专项：分档正确性 + 能力感知 + 确定性 + workflow 集成等价。

核心不变量：
- strategy="fixed"（默认）→ WorkflowResult.strategy is None，行为与不传逐位一致（冻结门另由评测脚本守，
  评测直调 retriever.retrieve、根本不经过 workflow）。
- strategy="auto" 且无可用后端 → 分类器分档但全 off → 与 fixed 结果**逐位一致**（安全降级）。
- 分类器纯函数：给定同输入恒定；绝不选不可用/未就绪的后端。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.strategy import (  # noqa: E402
    BROAD_MIN, PRECISE_MAX, StrategyDecision, classify_strategy, count_survivors,
)
from dataset_recommender.retrieval.query_parser import PS_ABSTAINED, PS_EXECUTABLE, QueryIntent, parse_query  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.corpus.corpus import load_normalized_corpus  # noqa: E402
from dataset_recommender.retrieval.retriever import passes_hard_filter  # noqa: E402
from dataset_recommender.retrieval.vector_recall import recall_backend_available, recall_backend_ready  # noqa: E402
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow  # noqa: E402


def _exec(constraints=None, free_text_terms=None, query="q"):
    """构造一个可执行 intent（parse_status=executable）用于分档单测（存活集大小由 n_survivors 注入）。"""
    return QueryIntent(original_query=query, constraints=constraints or {"species": ["human"]},
                       free_text_terms=free_text_terms or [], parse_status=PS_EXECUTABLE)


def _abstain():
    return QueryIntent(original_query="q", parse_status=PS_ABSTAINED, abstain=True)


# ---------- 分档：注入 n_survivors，避开语料依赖，纯测银带逻辑 ----------
def test_abstain_intent_all_off():
    d = classify_strategy(_abstain(), [], recall_available=True, llm_available=True, n_survivors=999)
    assert d.tier == "abstain" and d.recall_backend == "off" and d.rerank_backend == "off"


def test_empty_survivors_all_off():
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=True, n_survivors=0)
    assert d.tier == "empty" and d.recall_backend == "off" and d.rerank_backend == "off"


def test_precise_stays_off_even_with_capabilities():
    """紧查询（存活集≤cutoff）即便后端全可用也保持 off——不让非确定性重排碰已近最优的确定性序。"""
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=True, n_survivors=PRECISE_MAX)
    assert d.tier == "precise" and d.recall_backend == "off" and d.rerank_backend == "off"


def test_broad_with_recall_picks_cross_encoder():
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=True,
                          preferred_recall="cross_encoder", n_survivors=BROAD_MIN)
    assert d.tier == "broad" and d.recall_backend == "cross_encoder" and d.rerank_backend == "off"


def test_broad_without_recall_but_llm_degrades_to_llm_rerank():
    d = classify_strategy(_exec(free_text_terms=["immune"]), [], recall_available=False, llm_available=True, n_survivors=500)
    assert d.tier == "broad" and d.recall_backend == "off" and d.rerank_backend == "llm"


def test_complex_with_both_capabilities_uses_local_then_llm():
    d = classify_strategy(
        _exec(free_text_terms=["immune", "response"]), [],
        recall_available=True, llm_available=True, n_survivors=500,
    )
    assert d.tier == "complex"
    assert d.recall_backend == "cross_encoder" and d.rerank_backend == "llm"


def test_broad_without_semantic_preference_does_not_spend_llm():
    d = classify_strategy(_exec(), [], recall_available=False, llm_available=True, n_survivors=500)
    assert d.tier == "broad" and d.recall_backend == "off" and d.rerank_backend == "off"


def test_broad_without_any_backend_stays_off():
    d = classify_strategy(_exec(), [], recall_available=False, llm_available=False, n_survivors=500)
    assert d.tier == "broad" and d.recall_backend == "off" and d.rerank_backend == "off"


def test_medium_with_recall_picks_cross_encoder_and_semantic_llm_fallback():
    mid = (PRECISE_MAX + BROAD_MIN) // 2
    # 有本地后端 → cross_encoder
    d1 = classify_strategy(_exec(), [], recall_available=True, llm_available=True, n_survivors=mid)
    assert d1.tier == "medium" and d1.recall_backend == "cross_encoder"
    # 无本地后端、只有已授权 LLM，且有自由语义 → 用 LLM；没有自由语义仍保持规则序。
    d2 = classify_strategy(_exec(free_text_terms=["immune"]), [], recall_available=False, llm_available=True, n_survivors=mid)
    assert d2.tier == "medium" and d2.recall_backend == "off" and d2.rerank_backend == "llm"
    d3 = classify_strategy(_exec(), [], recall_available=False, llm_available=True, n_survivors=mid)
    assert d3.rerank_backend == "off"


def test_top_k_raises_precise_cutoff():
    """top_k 很大时「全返」区间也变大：15 存活集 + top_k=20 → 仍算 precise（都会被返回，排序无意义）。"""
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=True, n_survivors=15, top_k=20)
    assert d.tier == "precise" and d.recall_backend == "off"


def test_dense_preferred_honored():
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=False,
                          preferred_recall="dense", n_survivors=100)
    assert d.recall_backend == "dense"


def test_deterministic_pure_function():
    """纯函数：同输入多次调用结果恒定（可复现，适合进分级评测/单测）。"""
    args = dict(recall_available=True, llm_available=True, preferred_recall="cross_encoder", n_survivors=200)
    a = classify_strategy(_exec(), [], **args)
    b = classify_strategy(_exec(), [], **args)
    assert a == b
    assert isinstance(a, StrategyDecision)


def test_signals_exposed_for_observability():
    d = classify_strategy(_exec({"species": ["human"], "modality": ["single-cell"]}, ["immune"]), [],
                          recall_available=True, llm_available=False, n_survivors=300)
    s = d.signals
    assert s["n_survivors"] == 300 and s["n_pos_constraints"] == 2
    assert s["recall_available"] is True and s["llm_available"] is False
    assert s["n_semantic_terms"] == 1 and s["candidate_pressure"] == 30.0
    # 30.0 = 300/10：effective_top_k 缺省随产品默认 5→10。


# ---------- count_survivors 与 retriever 同源 ----------
def test_count_survivors_matches_hard_filter():
    s = get_settings()
    base = load_normalized_corpus(s.data_dir, s.project_root)
    intent = parse_query("人类肺癌的单细胞数据", s.keyword_mapping)
    expected = sum(1 for r in base if passes_hard_filter(r, intent))
    assert count_survivors(base, intent) == expected


def test_count_survivors_abstain_is_zero():
    s = get_settings()
    base = load_normalized_corpus(s.data_dir, s.project_root)
    assert count_survivors(base, _abstain()) == 0


# ---------- 能力探测 helper ----------
def test_recall_backend_helpers():
    assert recall_backend_ready("off") is True          # off 无需模型
    assert recall_backend_available("off") is False     # off 无从加载语义模型
    assert recall_backend_available("bogus") is False
    # cross_encoder：结果取决于本机是否装模型；只断言类型 + 与「不加载」契约一致（不抛错）。
    assert isinstance(recall_backend_available("cross_encoder"), bool)


def test_recall_backend_available_counts_api_gate(monkeypatch):
    """本地模型不可加载、但 API 闸就绪（env 启用 + key 在位）→ cross_encoder 计为可执行。
    钉住「available = 本地可加载 OR API 就绪」的网页形态语义（服务器无本地模型，全靠此闸）。"""
    from dataset_recommender.retrieval import vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_local_available", lambda backend: False)
    monkeypatch.setenv("BIODATA_RERANK_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    assert vr.recall_backend_available("cross_encoder") is True


def test_recall_backend_available_off_without_local_or_api(monkeypatch):
    """本地不可加载且 API 闸未就绪（env 未启用/无 key）→ 两种语义后端均不可执行。"""
    from dataset_recommender.retrieval import vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_local_available", lambda backend: False)
    for name in ("BIODATA_RERANK_API", "BIODATA_EMBED_API", "BIODATA_EMBED_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    assert vr.recall_backend_available("cross_encoder") is False
    assert vr.recall_backend_available("dense") is False


# ---------- workflow 集成：fixed 逐位不变 + auto 安全降级 ----------
def test_workflow_fixed_default_has_no_strategy():
    wf = DatasetRecommendationWorkflow()
    r = wf.run_with_meta(query="单细胞数据", use_llm=False)
    assert r.strategy is None


def test_workflow_auto_without_capabilities_equals_fixed():
    """auto 且 recall/llm 均不可用 → 分类器全 off → 结果与 fixed **逐位一致**（安全降级证明）。"""
    wf = DatasetRecommendationWorkflow()
    q = "单细胞数据"
    r_fixed = wf.run_with_meta(query=q, use_llm=False)
    r_auto = wf.run_with_meta(query=q, use_llm=False, strategy="auto",
                              recall_available=False, llm_available=False)
    names_fixed = [c["dataset_name"] for c in r_fixed.retrieved_data]
    names_auto = [c["dataset_name"] for c in r_auto.retrieved_data]
    assert names_auto == names_fixed              # 逐位一致
    assert r_auto.strategy is not None            # 但决策被如实记录
    assert r_auto.strategy["recall_backend"] == "off"
    assert r_auto.strategy["tier"] == "broad"     # 单细胞数据 存活集大


def test_workflow_auto_with_api_ready_selects_cross_encoder(monkeypatch):
    """auto + 本地模型不可用但 API 闸就绪 → 分类器仍选 cross_encoder（网页形态接通证明）；
    API 打分失败 fail-closed → 结果与 fixed 逐位一致（安全降级不破）。
    与本机有无装本地模型无关：API 分支在 recall_rerank 里先于本地路径短路。"""
    from dataset_recommender.retrieval import vector_recall as vr
    from dataset_recommender.retrieval import recall_api
    monkeypatch.setattr(vr, "recall_backend_local_available", lambda backend: False)
    monkeypatch.setenv("BIODATA_RERANK_API", "zhipu")
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "dummy")
    monkeypatch.setattr(recall_api, "api_rerank_scores", lambda q, texts: None)  # 防真调 API
    wf = DatasetRecommendationWorkflow()
    q = "单细胞数据"
    r_fixed = wf.run_with_meta(query=q, use_llm=False)
    recall_avail = vr.recall_backend_available("cross_encoder")  # Web/CLI 的显式传参方式
    assert recall_avail is True
    r_auto = wf.run_with_meta(query=q, use_llm=False, strategy="auto",
                              recall_available=recall_avail, llm_available=False)
    names_fixed = [c["dataset_name"] for c in r_fixed.retrieved_data]
    names_auto = [c["dataset_name"] for c in r_auto.retrieved_data]
    assert r_auto.strategy is not None
    assert r_auto.strategy["recall_backend"] == "cross_encoder"
    assert names_auto == names_fixed              # fail-closed → 逐位回退规则序


def test_workflow_auto_precise_query_stays_off():
    wf = DatasetRecommendationWorkflow()
    r = wf.run_with_meta(query="Atera 平台数据", use_llm=False, strategy="auto",
                         recall_available=True, llm_available=True)
    # 紧查询：即便能力全可用，分类器仍 off（不引非确定性重排）。
    assert r.strategy is not None and r.strategy["tier"] == "precise"
    assert r.strategy["recall_backend"] == "off" and r.strategy["rerank_backend"] == "off"


# ---------- auto 偏好解析（resolve_auto_recall_preference，2026-09-04 vector 进 auto）----------
def test_resolve_auto_recall_preference_prefers_vector(monkeypatch):
    """vector 可执行 → 偏好 vector（每查询只补嵌 1 条查询，成本低于 cross_encoder 全池打分）。"""
    from dataset_recommender.retrieval import vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_available", lambda b: b == "vector")
    assert vr.resolve_auto_recall_preference() == ("vector", True)


def test_resolve_auto_recall_preference_falls_back_to_cross_encoder(monkeypatch):
    """vector 不可用、cross_encoder 可用 → 回退 cross_encoder（保留 Batch 1 网页形态通路）。"""
    from dataset_recommender.retrieval import vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_available", lambda b: b == "cross_encoder")
    assert vr.resolve_auto_recall_preference() == ("cross_encoder", True)


def test_resolve_auto_recall_preference_none_available(monkeypatch):
    """两者皆无 → ("cross_encoder", False)：调用方按「无语义后端」走确定性词面序。"""
    from dataset_recommender.retrieval import vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_available", lambda b: False)
    assert vr.resolve_auto_recall_preference() == ("cross_encoder", False)


def test_broad_with_vector_preferred_picks_vector():
    d = classify_strategy(_exec(), [], recall_available=True, llm_available=False,
                          preferred_recall="vector", n_survivors=BROAD_MIN)
    assert d.tier == "broad" and d.recall_backend == "vector" and d.rerank_backend == "off"


def test_complex_with_vector_and_llm_uses_vector_then_llm():
    d = classify_strategy(_exec(free_text_terms=["immune", "tumor"]), [],
                          recall_available=True, llm_available=True,
                          preferred_recall="vector", n_survivors=500)
    assert d.tier == "complex" and d.recall_backend == "vector" and d.rerank_backend == "llm"


def test_vector_available_counts_api_gate(monkeypatch):
    """vector：本地不可加载但 embed API 就绪 → 可执行；双闸皆无 → 不可执行（与 dense 同闸）。"""
    from dataset_recommender.retrieval import vector_recall as vr
    from dataset_recommender.retrieval import recall_api
    monkeypatch.setattr(vr, "recall_backend_local_available", lambda backend: False)
    monkeypatch.setattr(recall_api, "embed_api_ready", lambda: True)
    assert vr.recall_backend_available("vector") is True
    monkeypatch.setattr(recall_api, "embed_api_ready", lambda: False)
    assert vr.recall_backend_available("vector") is False


def test_workflow_auto_with_vector_available_selects_vector(monkeypatch):
    """auto + preferred_recall=vector 且可执行 → 分类器选 vector；索引不可用 fail-closed
    → 结果与 fixed 逐位一致（安全降级不破）。"""
    from dataset_recommender.retrieval import recall_api, vector_index
    monkeypatch.setattr(recall_api, "api_embed_enabled", lambda: False)
    monkeypatch.setattr(vector_index, "index_dense_vectors", lambda q, items: None)
    wf = DatasetRecommendationWorkflow()
    q = "单细胞数据"
    r_fixed = wf.run_with_meta(query=q, use_llm=False)
    r_auto = wf.run_with_meta(query=q, use_llm=False, strategy="auto",
                              recall_available=True, llm_available=False,
                              preferred_recall="vector")
    names_fixed = [c["dataset_name"] for c in r_fixed.retrieved_data]
    names_auto = [c["dataset_name"] for c in r_auto.retrieved_data]
    assert r_auto.strategy is not None
    assert r_auto.strategy["recall_backend"] == "vector"
    assert names_auto == names_fixed              # fail-closed → 逐位回退规则序
