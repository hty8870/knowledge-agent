import pytest

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.data_loader import load_raw_records
from dataset_recommender.retrieval.normalizer import normalize_records
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import DatasetRetriever


def _load_normalized():
    settings = get_settings()
    return normalize_records(load_raw_records(settings.data_dir)), settings


def test_human_breast_query() -> None:
    records, settings = _load_normalized()
    retriever = DatasetRetriever(top_k=8)
    intent = parse_query("请帮我推荐经典的人类乳腺癌数据", settings.keyword_mapping)
    candidates = retriever.retrieve(records, intent, top_k=8)
    assert candidates
    assert any("human" in c.record.species.lower() for c in candidates)
    assert any(
        "breast" in f"{c.record.tissue} {c.record.disease} {c.record.dataset_name}".lower() for c in candidates
    )


def test_mouse_brain_spatial_query() -> None:
    records, settings = _load_normalized()
    retriever = DatasetRetriever(top_k=8)
    intent = parse_query("有没有小鼠脑组织空间转录组数据", settings.keyword_mapping)
    candidates = retriever.retrieve(records, intent, top_k=8)
    assert candidates
    assert any("mouse" in c.record.species.lower() for c in candidates)
    assert any("brain" in f"{c.record.tissue} {c.record.dataset_name}".lower() for c in candidates)
    assert any(
        any(token in f"{c.record.chemistry} {c.record.dataset_name}".lower() for token in ("visium", "spatial"))
        for c in candidates
    )


def test_fastq_required_query() -> None:
    records, settings = _load_normalized()
    retriever = DatasetRetriever(top_k=8)
    intent = parse_query("推荐有 FASTQ 的人类乳腺癌数据", settings.keyword_mapping)
    candidates = retriever.retrieve(records, intent, top_k=8)
    if candidates:
        assert all(candidate.record.has_raw_data is True for candidate in candidates)



# ---------- 缺来源记录不得被当成 "10x Genomics" ----------
def _synthetic_record(name: str, source: str | None) -> "object":
    from dataset_recommender.retrieval.normalizer import DatasetRecord

    raw = {} if source is None else {"source": source}
    return DatasetRecord(
        dataset_name=name, species="human", tissue="lung", disease="",
        chemistry="", count="", unit="", has_raw_data=None, url="",
        source_file="x.json", description=f"desc of {name}", raw=raw,
    )


def test_facet_source_missing_source_is_neutral_sentinel():
    from dataset_recommender.retrieval.retriever import _facet_source

    assert _facet_source(_synthetic_record("no-source", None)) == "未标注来源"
    assert _facet_source(_synthetic_record("blank", "  ")) == "未标注来源"
    assert _facet_source(_synthetic_record("tenx", "10x Genomics")) == "10x Genomics"


def test_sourceless_record_gets_no_preferred_source_boost():
    """「优先 10x」时无源记录此前白吃 +PREFERENCE_BOOST（把「不知道」当成「是 10x」），
    还连带污染分面计数与 caveat 分组。现在：无源记录不加权，真 10x 记录照加。"""
    from dataset_recommender.retrieval.query_parser import QueryIntent
    from dataset_recommender.retrieval.retriever import PREFERENCE_BOOST, DatasetRetriever

    intent = QueryIntent(original_query="q", preferred_sources=["10x Genomics"])
    r = DatasetRetriever()
    base = r._rank_score(_synthetic_record("plain", None), intent)
    boosted = r._rank_score(_synthetic_record("tenx", "10x Genomics"), intent)
    other = r._rank_score(_synthetic_record("cxg", "CELLxGENE Discover"), intent)
    assert boosted - base == pytest.approx(PREFERENCE_BOOST)
    assert other == base


def test_execution_trace_captures_complete_order_and_bounded_feature_snapshot():
    """训练现场不靠最终 top-k 反推：全 survivor UID 顺序保留，rich 特征有明确上限/哈希。"""
    from dataset_recommender.retrieval.query_parser import QueryIntent

    records = []
    for i in range(503):
        row = _synthetic_record(f"record-{i:03d}", "10x Genomics")
        row.raw["dataset_uid"] = f"uid-{i:03d}"
        row.family_id = f"family-{i:03d}"
        records.append(row)
    trace = {}
    selected = DatasetRetriever(top_k=10).retrieve(
        records, QueryIntent(original_query="record"), top_k=10, execution_trace=trace,
    )
    snapshot = trace["ranking_snapshot"]
    assert len(selected) == 10
    assert snapshot["candidate_count"] == 503 and len(snapshot["ordered_uids"]) == 503
    assert len(snapshot["features"]) == 500 and snapshot["features_truncated"] is True
    assert snapshot["ordered_uids"][0] == "uid-000" and len(snapshot["ordered_uids_sha256"]) == 64
    assert snapshot["parameters"]["weights"]["free_text_title"] == 1.0


def test_ranking_snapshot_records_post_recall_order_not_stale_rule_order():
    from dataset_recommender.retrieval.query_parser import QueryIntent

    records = []
    for i in range(4):
        row = _synthetic_record(f"record-{i}", "10x Genomics")
        row.raw["dataset_uid"] = f"uid-{i}"
        row.family_id = f"family-{i}"
        records.append(row)
    trace = {}
    selected = DatasetRetriever(top_k=4).retrieve(
        records, QueryIntent(original_query="record"), top_k=4,
        recall_backend="cross_encoder", cross_scorer=lambda pairs: list(range(len(pairs))),
        execution_trace=trace,
    )
    actual = [candidate.record.raw["dataset_uid"] for candidate in selected]
    assert trace["ranking_snapshot"]["ordered_uids"] == actual
    assert actual == ["uid-3", "uid-2", "uid-1", "uid-0"]
