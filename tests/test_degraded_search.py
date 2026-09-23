# -*- coding: utf-8 -*-
"""未收录词自动降级（2026-09-10 产品哲学修正）专项。

新哲学：**不替用户弃权**——用户是有判断力的科研人员，拿到「可能的错误结果」远
大于什么结果都不给。unresolved_term（句子里有系统未收录的词）不再弃权：
编排层自动忽略未收录词、拿剩余可解析条件继续检索，并把「忽略了哪些词、实际在
筛什么条件」如实标注（resolution_status="degraded" + degraded_search payload）。

保留的硬闸：**忽略之后一个条件都不剩时不降级**——那已经不是检索，是把整个库倒
出来。解析层与检索层原语不变（解析器仍如实报弃权），因此冻结评测（走
parse_query + retriever.retrieve、不经编排层）结构性不受影响。
"""
import pytest

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import known_source_values
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import DatasetRetriever
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, strip_terms

CAT = get_settings().keyword_mapping
S = get_settings()
ALL_SOURCES = known_source_values(S.data_dir, S.project_root)


@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


# ---------- 解析层：行为不变，仍如实报弃权（编排层才做降级） ----------
def test_parser_still_abstains_on_unresolved_terms():
    it = parse_query("人类膀胱造瘘的单细胞数据", CAT)
    assert it.abstain and it.abstain_reason == "unresolved_term"
    assert it.unresolved_terms == ["造瘘"], it.unresolved_terms
    assert "造瘘" in it.abstain_detail


@pytest.mark.parametrize("query", ["人类肺组织的单细胞数据", "不需要fastq", "最好是 Xenium 的黑色素瘤数据"])
def test_unresolved_terms_empty_on_other_states(query):
    """只有 unresolved_term 弃权才有值；可执行 / 澄清 / 其它弃权理由恒空。"""
    assert parse_query(query, CAT).unresolved_terms == []


# ---------- strip_terms：大小写不敏感、长词先挖 ----------
def test_strip_terms_is_case_insensitive():
    assert strip_terms("Human XYZZY lung", ["xyzzy"]).split() == ["Human", "lung"]
    assert strip_terms("人类肺数据", []) == "人类肺数据"


# ---------- 核心：弃权变降级，结果与标注一起给 ----------
def test_unresolved_term_auto_degrades_with_honest_notice(wf):
    res = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "degraded"
    assert res.retrieved_data and res.result_total > 0
    deg = res.degraded_search
    assert deg is not None and deg["ignored_terms"] == ["造瘘"]
    labels = {f["label"] for f in deg["active_filters"]}
    # 用户必须能看到「忽略之后实际在筛什么」，否则无法判断这批结果值不值。
    assert {"物种", "组织"} <= labels, deg["active_filters"]


@pytest.mark.parametrize("query,ignored", [
    ("翼龙的单细胞数据", "翼龙"),
    ("霍格沃茨综合征的人类数据", "霍格沃茨综合征"),
])
def test_adversarial_queries_now_return_degraded_results(wf, query, ignored):
    """曾经的 adv01/adv02：不再零返回，而是降级结果 + 标注。"""
    res = wf.run_with_meta(query=query, use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "degraded"
    assert res.retrieved_data and res.result_total > 0
    assert res.degraded_search and res.degraded_search["ignored_terms"] == [ignored]


def test_gongkai_now_parses_directly_without_degrade(wf):
    """「公开」雷区句（2026-09-11 OOD 批次起）：公开/开放是元可用性词（本目录全公开），
    已进 FILLER_GRAMMAR——不再触发降级，直接正常检索出结果。"""
    res = wf.run_with_meta(query="肺癌的公开数据集", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "results"
    assert res.retrieved_data and res.result_total > 0
    assert res.degraded_search is None


def test_degraded_results_are_actually_filtered_by_remaining_conditions(wf):
    """降级的代价必须真实可见：结果满足剩余条件（物种=人类），不按被忽略的词筛。"""
    res = wf.run_with_meta(query="霍格沃茨综合征的人类数据", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "degraded"
    for row in res.retrieved_data:
        assert "human" in str(row.get("species", "")).lower(), row


# ---------- 硬闸后不再沉默：向量语义兜底 + 诚实标注（2026-09-12 用户裁定） ----------
def test_vector_fallback_after_hard_gate(wf):
    """忽略之后一个条件都不剩 → 不降级（倒库闸不动），但也不再沉默：
    全库向量语义召回兜底 + 「未经条件核验」标注。"""
    res = wf.run_with_meta(query="霍格沃茨综合征的数据", use_llm=False, sources=ALL_SOURCES)
    if res.resolution_status == "abstained":
        pytest.skip("本地嵌入模型不可用，兜底回退为诚实弃权（设计内行为）")
    assert res.resolution_status == "vector_fallback"
    assert res.retrieved_data and res.vector_fallback
    assert res.vector_fallback["reason"] == "unresolved_term"
    assert "未经条件核验" in res.vector_fallback["note"]
    assert "未经条件核验" in res.answer.split("\n")[0]  # 纯文本消费方同样可见
    assert res.degraded_search is None


def test_vector_fallback_not_for_identifier(wf):
    """裸标识符维持诚实反查通道，不走向量兜底。"""
    res = wf.run_with_meta(query="10.1038/s41597-025-99999-x", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "abstained"
    assert res.vector_fallback is None


def test_normal_results_have_no_vector_fallback(wf):
    res = wf.run_with_meta(query="人类肺组织的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert res.resolution_status == "results" and res.vector_fallback is None


# ---------- 其余状态不受波及 ----------
def test_normal_results_have_no_degraded_notice(wf):
    ok = wf.run_with_meta(query="人类肺组织的单细胞数据", use_llm=False, sources=ALL_SOURCES)
    assert ok.resolution_status == "results" and ok.degraded_search is None


def test_no_match_still_uses_relaxation_options(wf):
    """词都认识、只是库里没有这个组合 → 空交集，照旧归引导式放宽管，不降级。"""
    empty = wf.run_with_meta(query="斑马鱼的乳腺癌数据", use_llm=False, sources=ALL_SOURCES)
    assert empty.resolution_status == "no_match" and empty.degraded_search is None
    assert empty.relaxation_options, "空交集仍应给出引导式放宽项"


def test_non_unresolved_abstain_is_not_degraded(wf):
    """其它弃权理由（软性排除表达不了等）不触发自动降级（degraded_search 恒 None）；
    2026-09-12 起也不再沉默——走向量语义兜底 + 「未经条件核验」标注
    （标识符直达/纯动作句除外，见 test_vector_fallback_not_for_identifier）。"""
    intent = parse_query("优先不要小鼠的肺数据", CAT)
    assert intent.abstain and intent.abstain_reason != "unresolved_term"
    res = wf.run_with_meta(query="优先不要小鼠的肺数据", use_llm=False, sources=ALL_SOURCES)
    assert res.degraded_search is None
    if res.resolution_status == "abstained":
        pytest.skip("本地嵌入模型不可用，兜底回退为诚实弃权（设计内行为）")
    assert res.resolution_status == "vector_fallback"
    assert res.vector_fallback and res.vector_fallback["reason"] == intent.abstain_reason


# ---------- 用户设置必须继承到降级那次检索（同一 _prepare_context 入口） ----------
def test_degraded_run_honours_explicit_date_range(wf):
    res = wf.run_with_meta(query="翼龙的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           date_from="2020-01-01", date_to="2021-12-31")
    assert res.resolution_status == "degraded"
    labels = {f["label"] for f in res.degraded_search["active_filters"]}
    assert "发表时间" in labels, f"降级检索漏掉了用户设的时间范围：{labels}"
    # 与「用户自己拿降级句 + 同一时间窗重搜」逐位同源
    truth = wf.run_with_meta(query=res.degraded_search["query"], use_llm=False,
                             sources=ALL_SOURCES, date_from="2020-01-01", date_to="2021-12-31")
    assert res.result_total == truth.result_total


def test_degraded_run_honours_facet_filters(wf):
    facets = [{"dim": "source", "value": "ArrayExpress"}]
    res = wf.run_with_meta(query="翼龙的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           facet_filters=facets)
    truth = wf.run_with_meta(query="的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                             facet_filters=facets)
    assert res.resolution_status == "degraded"
    assert res.result_total == truth.result_total


def test_degraded_run_does_not_resurrect_suppressed_constraints(wf):
    """用户刚在「已命中」里删掉的条件，不该在降级后的「实际在筛的条件」里复活。"""
    res = wf.run_with_meta(query="人类膀胱造瘘的单细胞数据", use_llm=False, sources=ALL_SOURCES,
                           suppressed_constraints=["tissue"])
    if res.resolution_status == "abstained":
        return          # 删掉组织后没条件了 → 硬闸生效，也是正确行为
    labels = {f["label"] for f in (res.degraded_search or {}).get("active_filters", [])}
    assert "组织" not in labels, f"被用户删掉的条件在降级检索里复活了：{labels}"


# ---------- 结构性隔离：冻结评测不经编排层 ----------
def test_frozen_eval_path_never_sees_degraded_results():
    """官方评测走 parse_query + retriever.retrieve、不碰 workflow：同一句话在那一层
    仍然如实零返回——弃权是检索层原语，自动降级是编排层决定，两层互不假装。"""
    it = parse_query("翼龙的单细胞数据", CAT)
    assert it.abstain is True and it.parse_status == "abstained"
    assert it.constraints == {} and it.has_raw_data_required is None
    records = []
    assert DatasetRetriever(top_k=5).retrieve(records, it, top_k=5) == []
