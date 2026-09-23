# -*- coding: utf-8 -*-
from fastapi.testclient import TestClient
from types import SimpleNamespace

from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.search_request import resolve_search_request
from dataset_recommender.retrieval.vector_recall import recall_rerank
from dataset_recommender.retrieval.rerank import rerank_candidates
from dataset_recommender.app.webapp import app
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, _build_search_trace


def _steps(trace):
    return {x["id"]: x for x in trace["steps"]}


def test_workflow_auto_source_and_time_share_one_interpretation():
    r = DatasetRecommendationWorkflow().run_with_meta(
        query="CELLxGENE Discover 2020年以后的人类肝脏数据",
        auto_parse_sources=True,
        use_llm=False,
        strategy="auto",
        recall_available=False,
        llm_available=False,
    )
    assert r.interpretation["effective_sources"] == ["CELLxGENE Discover"]
    intent = r.interpretation["intent"]
    assert intent["date_from"] == "2020-01-01"
    assert "human" in intent["constraints"].get("species", [])
    assert "liver" in intent["constraints"].get("tissue", [])
    assert r.resolution_status == "results" and r.retrieved_data
    steps = _steps(r.search_trace)
    assert steps["source_parse"]["status"] == "used"
    assert "CELLxGENE" in steps["source_parse"]["detail"]
    assert steps["constraint_parse"]["status"] == "used"
    assert steps["llm_polish"]["status"] == "skipped"
    assert isinstance(r.search_trace["total_duration_ms"], int)


def test_web_response_exposes_interpretation_and_actual_trace():
    data = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/recommend",
        json={
            "query": "Human Cell Atlas 近三年的人类数据",
            "sources": ["10x Genomics", "Human Cell Atlas"],
            "auto_parse_sources": True,
            "use_llm": False,
        },
    ).json()
    assert data["ok"] is True
    assert data["interpretation"]["effective_sources"] == ["Human Cell Atlas"]
    assert data["interpretation"]["intent"]["date_from"]
    assert _steps(data["search_trace"])["source_parse"]["status"] == "used"


def test_interpret_preview_uses_shared_source_and_time_parser():
    data = TestClient(app, base_url="http://127.0.0.1").post(
        "/api/interpret",
        json={
            "query": "CELLxGENE 2020年以来的人类肝脏数据",
            "sources": ["10x Genomics", "CELLxGENE Discover", "Human Cell Atlas"],
            "auto_parse_sources": True,
        },
    ).json()
    assert data["ok"] is True
    interpretation = data["interpretation"]
    assert interpretation["detected_sources"] == ["CELLxGENE Discover"]
    assert interpretation["effective_sources"] == ["CELLxGENE Discover"]
    assert interpretation["intent"]["date_from"] == "2020-01-01"


def test_source_trace_distinguishes_auto_pool_from_manual_selection():
    execution = {
        "recall": {"backend": "off", "status": "skipped", "reason": "disabled", "duration_ms": 0},
        "rerank": {"backend": "off", "status": "skipped", "reason": "disabled", "duration_ms": 0},
    }
    pool = ["10x Genomics", "CELLxGENE Discover"]
    automatic = resolve_search_request("人类肺癌数据", pool, pool, auto_parse_sources=True)
    manual = resolve_search_request("人类肺癌数据", pool, pool, auto_parse_sources=False)
    auto_trace = _build_search_trace(automatic, parse_query(automatic.parsed_query), execution, None, 10)
    manual_trace = _build_search_trace(manual, parse_query(manual.parsed_query), execution, None, 10)
    assert "自动识别已开启" in _steps(auto_trace)["source_parse"]["detail"]
    assert "手动选择" not in _steps(auto_trace)["source_parse"]["detail"]
    assert "手动选择" in _steps(manual_trace)["source_parse"]["detail"]


def test_search_trace_carries_machine_readable_ranking_snapshot_additively():
    execution = {
        "recall": {}, "rerank": {},
        "ranking_snapshot": {"schema": "biodata-ranking-snapshot/1", "ordered_uids": ["u1"]},
    }
    resolution = resolve_search_request("人类肺癌数据", [], [], auto_parse_sources=False)
    trace = _build_search_trace(resolution, parse_query(resolution.parsed_query), execution, None, 1)
    assert trace["ranking_snapshot"]["ordered_uids"] == ["u1"]


def test_local_recall_trace_distinguishes_used_from_fallback():
    # 空候选只验证状态形状；注入打分器的真实排序由现有 vector_recall 专项覆盖。
    trace = {}
    assert recall_rerank("q", [], backend="cross_encoder", cross_scorer=lambda _p: [], trace=trace) == []
    assert trace["status"] == "skipped" and trace["reason"] == "no_candidates"
    assert isinstance(trace["duration_ms"], int)


def test_llm_rerank_trace_records_success_and_invalid_output():
    from dataset_recommender.retrieval.normalizer import DatasetRecord
    from dataset_recommender.retrieval.retriever import RetrievedCandidate

    def cand(name):
        rec = DatasetRecord(
            dataset_name=name, species="human", tissue="lung", disease="cancer",
            chemistry="gex", count="1", unit="cell", has_raw_data=True,
            url="https://example.test", source_file="test.json", description=name,
            raw={}, family_id=name,
        )
        return RetrievedCandidate(rec, 0.0, [], [], "")

    items = [cand("A"), cand("B")]
    ok_trace = {}
    out = rerank_candidates("q", items, backend="llm", llm_call=lambda _p: "[2,1]", trace=ok_trace)
    assert [x.record.dataset_name for x in out] == ["B", "A"]
    assert ok_trace["status"] == "used" and isinstance(ok_trace["duration_ms"], int)

    bad_trace = {}
    out2 = rerank_candidates("q", items, backend="llm", llm_call=lambda _p: "not-json", trace=bad_trace)
    assert out2 == items and bad_trace["status"] == "fallback"
    assert isinstance(bad_trace["duration_ms"], int)


def test_strategy_reason_and_step_copy_follow_actual_fallback_not_plan():
    known = ["10x Genomics"]
    resolution = resolve_search_request("人类肺癌数据", None, known, auto_parse_sources=True)
    intent = parse_query(resolution.parsed_query)
    decision = SimpleNamespace(
        tier="broad",
        recall_backend="cross_encoder",
        rerank_backend="off",
        signals={"n_survivors": 120},
    )
    trace = _build_search_trace(
        resolution,
        intent,
        {
            "recall": {
                "backend": "cross_encoder",
                "status": "fallback",
                "reason": "model_or_dependency_unavailable",
                "duration_ms": 1234,
            },
            "rerank": {"backend": "off", "status": "skipped", "reason": "disabled", "duration_ms": 0},
        },
        decision,
        120,
    )
    local = _steps(trace)["local_semantic"]
    assert local["status"] == "fallback"
    assert "本地模型或运行依赖不可用" in local["detail"]
    assert "耗时 1.2 秒" in local["detail"]
    assert "发生回退，最终采用规则排序" in trace["strategy_reason"]


def test_manual_strategy_reason_also_reports_actual_fallback():
    resolution = resolve_search_request("人类肺癌数据", None, ["10x Genomics"], auto_parse_sources=True)
    intent = parse_query(resolution.parsed_query)
    trace = _build_search_trace(
        resolution,
        intent,
        {
            "recall": {
                "backend": "cross_encoder",
                "status": "fallback",
                "reason": "model_or_dependency_unavailable",
                "duration_ms": 80,
            },
            "rerank": {"backend": "off", "status": "skipped", "reason": "disabled", "duration_ms": 0},
        },
        None,
        25,
    )
    assert trace["strategy_reason"] == "使用手动选择的排序设置；语义排序发生回退，最终采用规则排序。"
