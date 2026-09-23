"""发表时间范围硬过滤（「时间维度」）。

约束：date_from/date_to 默认空串 → 官方评测/CLI 不传时整段 no-op，确定性门不受影响。
"""
from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.data_loader import load_raw_records
from dataset_recommender.retrieval.normalizer import normalize_records
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import DatasetRetriever


def _setup():
    s = get_settings()
    return normalize_records(load_raw_records(s.data_dir)), s


def _dates(cands):
    return [str(c.record.raw.get("published_date", "")) for c in cands]


def test_date_range_confines_results_to_window():
    recs, s = _setup()
    r = DatasetRetriever(top_k=10)
    intent = parse_query("人类", s.keyword_mapping)
    intent.date_from, intent.date_to = "2020-01-01", "2021-12-31"
    cands = r.retrieve(recs, intent, top_k=10)
    assert cands, "库内 2020–2021 应有人类数据"
    assert all("2020-01-01" <= d <= "2021-12-31" for d in _dates(cands))


def test_date_from_only_is_lower_bound():
    recs, s = _setup()
    r = DatasetRetriever(top_k=10)
    intent = parse_query("人类", s.keyword_mapping)
    intent.date_from = "2025-01-01"
    cands = r.retrieve(recs, intent, top_k=10)
    assert cands
    assert all(d >= "2025-01-01" for d in _dates(cands))


def test_nl_relative_dates_parsed_from_query():
    """中文/英文相对日期从查询里解析进 intent（此前会因残片「年以后」误弃权、date_from 为空）。"""
    i = parse_query("2022年以后的乳腺癌数据")
    assert i.date_from == "2022-01-01" and i.date_to == ""
    assert not i.abstain  # 关键：不再误弃权
    assert "breast cancer" in i.constraints.get("disease", []) or "invasive ductal carcinoma" in i.constraints.get("disease", [])

    r = parse_query("2020到2023年的人类数据")
    assert r.date_from == "2020-01-01" and r.date_to == "2023-12-31"

    b = parse_query("2023年以前的小鼠脑数据")
    assert b.date_to == "2022-12-31" and not b.abstain

    e = parse_query("human lung data since 2021")
    assert e.date_from == "2021-01-01"


def test_nl_date_survives_mcp_path_no_explicit_params():
    """workflow 未传 date 参数时（MCP/CLI 路径）应保留 NL 解析出的时间范围，而非清空。"""
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow
    wf = DatasetRecommendationWorkflow()
    res = wf.run_with_meta(query="2024年以后的人类数据", top_k=8, use_llm=False)
    # 命中的都应 >= 2024（若库里有 2024+ 的人类数据）
    dates = [str((c.get("published_date") or "")) for c in res.retrieved_data if c.get("published_date")]
    assert dates, "库内应有 2024+ 人类数据"
    assert all(d >= "2024-01-01" for d in dates)


def test_empty_date_range_is_noop():
    """不设时间范围 → 与不带该字段逐位一致（保护确定性 / 官方评测路径）。"""
    recs, s = _setup()
    r = DatasetRetriever(top_k=8)
    base = [c.record.dataset_name for c in r.retrieve(recs, parse_query("人类乳腺癌", s.keyword_mapping), top_k=8)]
    i1 = parse_query("人类乳腺癌", s.keyword_mapping)
    i1.date_from, i1.date_to = "", ""
    same = [c.record.dataset_name for c in r.retrieve(recs, i1, top_k=8)]
    assert base == same
