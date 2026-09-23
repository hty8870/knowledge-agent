"""分面细化：有结果时按未固定维度分组，一键精确收窄（drill-down）。

钉死：
1) facets() 只在存活集上统计、只出「≥2 个不同取值」的未固定维度；弃权/命中<2 → 无分面。
2) **精确一致性**：点某分面值（返回的 count）== 加该过滤后 matched_survivors 的条数（点 N 剩 N）。
3) 大小写归并：Blood/blood 合成一个分面键（显示取最常见原始写法），不出现重复项。
4) 已被查询固定的维度、已被分面选中的维度都不再出现在分面面板里。
5) retrieve(facet_filters=…) 只收窄存活集、绝不引入违规（终检恒过）。
6) 确定性隔离：facet_filters=None（官方评测路径）→ retrieve/facets 逐位 no-op、结果与不传时全等。
7) /api/recommend 结构化返回 facets/result_total/applied_facets；非法维度被清洗丢弃。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.corpus.corpus import available_sources, load_full_corpus  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import (  # noqa: E402
    DatasetRetriever,
    facet_value,
    passes_hard_filter,
    record_passes_facets,
)
from dataset_recommender.app.webapp import app  # noqa: E402


def _corpus():
    s = get_settings()
    return load_full_corpus(s.data_dir, s.project_root)


def _all_sources():
    s = get_settings()
    return [x["value"] for x in available_sources(s.data_dir, s.project_root)]


def test_facets_groups_over_survivors():
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类的单细胞数据")   # 广查询 → 大量存活集、多维可细化
    fac = r.facets(recs, intent)
    assert fac["total"] > 100
    dims = {g["dim"] for g in fac["groups"]}
    # 至少来源/组织/发表年份等分类干净的维度应出现
    assert "source" in dims and "year" in dims
    for g in fac["groups"]:
        assert len(g["values"]) >= 2                 # 单值维度不出
        assert g["label"]
        assert all(v["count"] > 0 and v["display"] for v in g["values"])
        if g["dim"] == "year":
            # 发表年份按年份由新到旧排序（而非条数）
            years = [v["value"] for v in g["values"]]
            assert years == sorted(years, reverse=True)
        else:
            # 其余维度组内按条数降序
            counts = [v["count"] for v in g["values"]]
            assert counts == sorted(counts, reverse=True)


def test_facet_click_count_matches_narrowed_exactly():
    """精确一致性：分面值上写的 N，就是加该过滤后库里真实剩下的 N（计数与过滤同源）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类的单细胞数据")
    fac = r.facets(recs, intent)
    for g in fac["groups"]:
        for v in g["values"]:
            ff = [{"dim": g["dim"], "value": v["value"]}]
            narrowed = r.matched_survivors(recs, intent, ff)
            assert len(narrowed) == v["count"], f"{g['dim']}={v['value']} 计数不符"
            # 收窄集里每条在该维度上的键都等于所选值
            assert all(facet_value(g["dim"], rec) == v["value"] for rec in narrowed)


def test_casefold_merges_variants():
    """Blood 与 blood 归并为一个分面键（不出现重复项）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类的单细胞数据")
    fac = r.facets(recs, intent)
    tissue = next((g for g in fac["groups"] if g["dim"] == "tissue"), None)
    if tissue:   # 组织维度存在时
        keys = [v["value"] for v in tissue["values"]]
        assert keys == [k.lower() for k in keys], "分面键应为归一化小写"
        assert len(keys) == len(set(keys)), "不应有重复键"


def test_pinned_dims_not_faceted():
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类乳腺癌的单细胞数据")   # species + disease 被查询固定
    fac = r.facets(recs, intent)
    dims = {g["dim"] for g in fac["groups"]}
    assert "species" not in dims and "disease" not in dims


def test_selected_dim_collapses():
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类的单细胞数据")
    fac = r.facets(recs, intent)
    src = next((g for g in fac["groups"] if g["dim"] == "source"), None)
    assert src is not None
    ff = [{"dim": "source", "value": src["values"][0]["value"]}]
    fac2 = r.facets(recs, intent, ff)
    assert "source" not in {g["dim"] for g in fac2["groups"]}


def test_abstain_and_noresult_no_facets():
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    assert r.facets(recs, parse_query("不要小鼠的原始数据"))["groups"] == []   # 跨维歧义否定→弃权
    assert r.facets(recs, parse_query("翼龙的单细胞数据"))["groups"] == []       # 词表外→弃权
    # 可执行否定（不要小鼠的人类数据）反过来应有分面（存活集非空、可继续收窄）
    assert r.facets(recs, parse_query("不要小鼠的人类数据"))["groups"] != []


def test_retrieve_with_facets_no_violation():
    """加分面过滤后，retrieve 结果每条都真满足 query 硬约束 + 分面过滤（终检不变量）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类乳腺癌数据")
    ff = [{"dim": "source", "value": "CELLxGENE Discover"}]
    cands = r.retrieve(recs, intent, top_k=12, facet_filters=ff)
    assert cands
    for c in cands:
        assert passes_hard_filter(c.record, intent)
        assert record_passes_facets(c.record, ff)


def test_facet_filters_none_is_noop_for_retrieve():
    """确定性隔离：facet_filters=None 与不传 → retrieve 输出逐位相同（官方评测走此路径）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=8)
    intent = parse_query("人类乳腺癌数据")
    base = r.retrieve(recs, intent, top_k=8)
    none_ff = r.retrieve(recs, intent, top_k=8, facet_filters=None)
    empty_ff = r.retrieve(recs, intent, top_k=8, facet_filters=[])
    names = lambda cs: [c.record.dataset_name for c in cs]
    assert names(base) == names(none_ff) == names(empty_ff)


def test_or_within_dim_and_across_dims():
    """同维度多值 OR、跨维度 AND：验证过滤谓词语义。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=12)
    intent = parse_query("人类的单细胞数据")
    a = len(r.matched_survivors(recs, intent, [{"dim": "source", "value": "CELLxGENE Discover"}]))
    b = len(r.matched_survivors(recs, intent, [{"dim": "source", "value": "Human Cell Atlas"}]))
    ab = len(r.matched_survivors(recs, intent,
             [{"dim": "source", "value": "CELLxGENE Discover"}, {"dim": "source", "value": "Human Cell Atlas"}]))
    assert ab == a + b            # 同维度两值 OR（互斥来源）→ 相加
    # 跨维度 AND：来源 ∩ 年份 ≤ 各自
    cross = len(r.matched_survivors(recs, intent,
            [{"dim": "source", "value": "CELLxGENE Discover"}, {"dim": "year", "value": "2022"}]))
    assert cross <= a


def test_api_returns_facets_and_narrows():
    client = TestClient(app, base_url="http://127.0.0.1")
    all_sources = _all_sources()
    base = client.post("/api/recommend", json={
        "query": "人类的单细胞数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
    }).json()
    assert base["result_total"] > 100
    assert len(base["facets"]) >= 3
    src = next((g for g in base["facets"] if g["dim"] == "source"), None)
    assert src is not None
    pick = src["values"][0]
    narrowed = client.post("/api/recommend", json={
        "query": "人类的单细胞数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
        "facet_filters": [{"dim": "source", "value": pick["value"]}],
    }).json()
    assert narrowed["result_total"] == pick["count"]          # 精确一致
    assert narrowed["applied_facets"] == [{"dim": "source", "value": pick["value"]}]
    assert "source" not in {g["dim"] for g in narrowed["facets"]}   # 选中维度收起


def test_api_sanitizes_bad_facet_filters():
    client = TestClient(app, base_url="http://127.0.0.1")
    all_sources = _all_sources()
    resp = client.post("/api/recommend", json={
        "query": "人类的单细胞数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
        "facet_filters": [
            {"dim": "not_a_dim", "value": "x"},          # 非法维度 → 丢弃
            {"dim": "source", "value": ""},               # 空值 → 丢弃
            {"nonsense": 1},                              # 结构错 → 丢弃
        ],
    }).json()
    assert resp["applied_facets"] == []                       # 全部被清洗
    assert resp["result_total"] > 100                          # 等同无过滤


def test_api_no_facets_field_when_abstain():
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/api/recommend", json={
        "query": "不要小鼠的原始数据", "sources": _all_sources(),   # 跨维歧义否定→弃权
        "use_llm": False, "mock_llm": True, "top_k": 5,
    }).json()
    # 2026-09-12 起：弃权不再沉默——向量语义兜底（未经条件核验、语义序里恰好可能含 mouse，
    # 这正是「未核验」的含义，标注覆盖）；嵌入器不可用的环境回退旧弃权空态。
    if resp["resolution_status"] == "vector_fallback":
        assert resp["results"] and resp["vector_fallback"]
        assert "未经条件核验" in resp["vector_fallback"]["note"]
    else:
        assert resp["results"] == []
        assert resp["facets"] == []
        assert resp["resolution_status"] == "abstained"


def test_api_executable_negation_returns_results_no_mouse():
    """可执行否定经 API：include human + exclude mouse → 有结果、resolution_status=results、无 mouse 违规。"""
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/api/recommend", json={
        "query": "不要小鼠的人类数据", "sources": _all_sources(),
        "use_llm": False, "mock_llm": True, "top_k": 5,
    }).json()
    assert resp["resolution_status"] == "results"
    assert resp["results"]
    for r in resp["results"]:
        assert "mouse" not in (r.get("species", "") or "").lower(), r.get("dataset_name")


# ---- 验证修复回归 ----

def test_year_facet_key_stripped():
    """验证(low)：year 键先 strip 再切前 4 位——外部源 published_date 带前导空白也不出坏桶。"""
    from types import SimpleNamespace

    clean = SimpleNamespace(raw={"published_date": "2023-05-05"})
    spaced = SimpleNamespace(raw={"published_date": " 2023-05-05"})   # 前导空白
    assert facet_value("year", clean) == "2023"
    assert facet_value("year", spaced) == "2023"                       # 不是 " 202"


def test_sanitizer_casefolds_freetext_value():
    """验证(low)：清洗器把自由文本维度 value 归一小写，与分面键对齐。"""
    from dataset_recommender.app.webapp import _sanitize_facet_filters

    out = _sanitize_facet_filters([
        {"dim": "species", "value": "Homo Sapiens"},   # 原始大小写
        {"dim": "source", "value": "CELLxGENE Discover"},  # 分类维度保留原样
    ])
    assert {"dim": "species", "value": "homo sapiens"} in out
    assert {"dim": "source", "value": "CELLxGENE Discover"} in out


def test_api_accepts_mixed_case_casefold_value():
    """验证(low)：直连 API 用原始大小写物种值（如 'Homo sapiens'）也命中，不静默 0。"""
    client = TestClient(app, base_url="http://127.0.0.1")
    all_sources = _all_sources()
    base = {"query": "人类的单细胞数据", "sources": all_sources,
            "use_llm": False, "mock_llm": True, "top_k": 5}
    # 广查询不固定物种 → species 是可细化维度
    r0 = client.post("/api/recommend", json=base).json()
    sp = next((g for g in r0["facets"] if g["dim"] == "species"), None)
    if sp is None:
        return   # 该语料下物种未成分面则跳过
    key = sp["values"][0]["value"]        # 归一小写键
    lower = client.post("/api/recommend", json={**base, "facet_filters": [{"dim": "species", "value": key}]}).json()
    upper = client.post("/api/recommend", json={**base, "facet_filters": [{"dim": "species", "value": key.title()}]}).json()
    assert lower["result_total"] == upper["result_total"] == sp["values"][0]["count"] > 0
