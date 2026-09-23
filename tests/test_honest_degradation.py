# -*- coding: utf-8 -*-
"""诚实降级层专项：缺元数据的记录不再静默判负——coverage_caveats 显式计数 + lenient_dims 精确纳入。

隔离保证（同 relaxation_options / facet_filters 范式）：coverage_caveats 只读、不被 retrieve/评测调用；
lenient_dims 默认空 → constraint_satisfied/passes_hard_filter 默认分支逐位不变。冻结 767 门另由
scripts/evaluate_recommendation.py 结构性守护，这里补「默认 no-op」「只纳空不纳已知不同」两条行为断言。
"""
from dataclasses import replace

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.data_loader import RawRecord, load_raw_records
from dataset_recommender.retrieval.normalizer import normalize_records
from dataset_recommender.retrieval.query_parser import QueryIntent, parse_query
from dataset_recommender.retrieval.retriever import (
    DatasetRetriever,
    constraint_satisfied,
    passes_hard_filter,
    _dim_field_present,
)
from dataset_recommender.app.workflow import sanitize_lenient_dims


def _mk(name, **fields):
    raw = RawRecord(source_file="synthetic.json", record={"dataset_name": name, "url": "http://x/" + name, **fields})
    return normalize_records([raw])[0]


def _intent(**kw):
    return QueryIntent(original_query="q", **kw)


def _load_base():
    s = get_settings()
    return normalize_records(load_raw_records(s.data_dir)), s


# ---------- constraint_satisfied: lenient 只纳「空」、不纳「已知不同」 ----------
def test_lenient_admits_empty_but_not_known_different():
    empty = _mk("e", species="human")                       # tissue 空
    lung = _mk("l", species="human", tissue="Lung")
    brain = _mk("b", species="human", tissue="Brain")
    terms = ["lung"]
    # 严格（默认）：空→False，lung→True，brain→False
    assert constraint_satisfied(empty, "tissue", terms) is False
    assert constraint_satisfied(lung, "tissue", terms) is True
    assert constraint_satisfied(brain, "tissue", terms) is False
    # 宽容：空→True（无法核验，纳入），lung→True，brain→**仍 False**（已知不同，真负不放行）
    assert constraint_satisfied(empty, "tissue", terms, lenient=True) is True
    assert constraint_satisfied(lung, "tissue", terms, lenient=True) is True
    assert constraint_satisfied(brain, "tissue", terms, lenient=True) is False


def test_lenient_false_is_byte_identical_to_no_arg():
    recs = [_mk(str(i), species="human", tissue=t) for i, t in enumerate(["", "Lung", "Brain", "lung tumor"])]
    for r in recs:
        for dim, terms in (("tissue", ["lung"]), ("species", ["human"]), ("disease", ["cancer"])):
            assert constraint_satisfied(r, dim, terms) == constraint_satisfied(r, dim, terms, lenient=False)


def test_passes_hard_filter_lenient_only_through_intent():
    empty = _mk("e", species="human")                       # tissue 空
    brain = _mk("b", species="human", tissue="Brain")
    strict = _intent(constraints={"species": ["human"], "tissue": ["lung"]})
    lenient = replace(strict, lenient_dims={"tissue"})
    assert passes_hard_filter(empty, strict) is False and passes_hard_filter(brain, strict) is False
    assert passes_hard_filter(empty, lenient) is True       # 空 tissue 纳入
    assert passes_hard_filter(brain, lenient) is False      # 已知不同 tissue 仍排除
    # 宽容只作用于被点名的维度：species 仍严格
    mouse_empty_tissue = _mk("m", species="mouse")
    assert passes_hard_filter(mouse_empty_tissue, lenient) is False


# ---------- coverage_caveats：数「满足其它 + 该维空」，区分不匹配与不知道 ----------
def test_coverage_caveats_counts_unknown_only_and_labels_by_dimension():
    recs = [
        _mk("e1", species="human", source="EBI"),           # tissue 空 → 计
        _mk("e2", species="human", source="EBI"),           # tissue 空 → 计
        _mk("e3", species="human", source="ArrayExpress"),  # tissue 空 → 计（另一源）
        _mk("lung", species="human", tissue="Lung"),        # 命中 → 不计
        _mk("brain", species="human", tissue="Brain"),      # 已知不同 → 不计（真负）
        _mk("mouse", species="mouse"),                      # 物种不符 → 被别的约束排除，不计
    ]
    rt = DatasetRetriever(top_k=5)
    intent = _intent(constraints={"species": ["human"], "tissue": ["lung"]})
    cav = rt.coverage_caveats(recs, intent)
    tissue = next(c for c in cav if c["dim"] == "tissue")
    assert tissue["count"] == 3                             # 只数三条空 tissue 的 human
    assert tissue["label"] == "组织"                        # 维度名，不是查询值「Lung」
    by = {s["source"]: s["count"] for s in tissue["by_source"]}
    assert by == {"EBI": 2, "ArrayExpress": 1}
    # 没有 species caveat（species 全部已标注）
    assert not any(c["dim"] == "species" for c in cav)


def test_coverage_caveats_respects_active_facet_filters():
    """COV-1 修复：激活分面时 caveat 与存活集同口径——只数分面内的未标注记录，
    不把用户已过滤掉的来源算进「另有 N 条」；且 caveat 计数 == 分面内点「也纳入」真正新增数。"""
    from dataset_recommender.retrieval.retriever import record_passes_facets
    recs = [
        _mk("e1", species="human", source="EBI"),
        _mk("e2", species="human", source="EBI"),
        _mk("e3", species="human", source="ArrayExpress"),
        _mk("lung", species="human", tissue="Lung"),
    ]
    rt = DatasetRetriever(top_k=5)
    intent = _intent(constraints={"species": ["human"], "tissue": ["lung"]})
    # 无分面：3 条空 tissue（EBI 2 + ArrayExpress 1）
    assert next(c for c in rt.coverage_caveats(recs, intent) if c["dim"] == "tissue")["count"] == 3
    # 激活 source=EBI：caveat 只数 EBI 的 2 条（ArrayExpress 已被分面排除，不再误报）
    ff = [{"dim": "source", "value": "EBI"}]
    ebi = next(c for c in rt.coverage_caveats(recs, intent, facet_filters=ff) if c["dim"] == "tissue")
    assert ebi["count"] == 2 and {s["source"]: s["count"] for s in ebi["by_source"]} == {"EBI": 2}
    # 自洽：分面内 caveat 计数 == 点「也纳入」新增数
    strict = [r for r in recs if passes_hard_filter(r, intent) and record_passes_facets(r, ff)]
    lenient = [r for r in recs if passes_hard_filter(r, replace(intent, lenient_dims={"tissue"})) and record_passes_facets(r, ff)]
    assert len(lenient) - len(strict) == ebi["count"]


def test_coverage_caveats_skips_already_lenient_dim():
    recs = [_mk("e", species="human"), _mk("l", species="human", tissue="Lung")]
    intent = _intent(constraints={"species": ["human"], "tissue": ["lung"]}, lenient_dims={"tissue"})
    # tissue 已宽容 → 缺口已消解、不再报 caveat
    assert not any(c["dim"] == "tissue" for c in DatasetRetriever().coverage_caveats(recs, intent))


def test_coverage_caveats_empty_when_no_constraints_or_abstain():
    recs = [_mk("e", species="human")]
    assert DatasetRetriever().coverage_caveats(recs, _intent()) == []
    assert DatasetRetriever().coverage_caveats(recs, _intent(abstain=True, constraints={"tissue": ["lung"]})) == []


# ---------- sanitize 白名单 ----------
def test_sanitize_lenient_dims_whitelist():
    assert sanitize_lenient_dims(["tissue", "disease", "bogus", "", 123, "species"]) == {"tissue", "disease", "species"}
    assert sanitize_lenient_dims(None) == set()
    assert sanitize_lenient_dims("tissue") == set()   # 非序列 → 空（安全默认）


# ---------- 结构性隔离 + 自洽（base 语料） ----------
def test_default_lenient_empty_is_noop_on_base():
    records, settings = _load_base()
    rt = DatasetRetriever(top_k=8)
    q = parse_query("人类肺组织数据", settings.keyword_mapping)
    a = [c.record.dataset_name for c in rt.retrieve(records, q, top_k=8)]
    b = [c.record.dataset_name for c in rt.retrieve(records, replace(q, lenient_dims=set()), top_k=8)]
    assert a == b   # lenient_dims 默认空 → retrieve 输出逐位一致


def test_caveat_count_equals_lenient_added_on_base():
    records, settings = _load_base()
    rt = DatasetRetriever(top_k=5)
    q = parse_query("人类肺组织数据", settings.keyword_mapping)
    cav = {c["dim"]: c["count"] for c in rt.coverage_caveats(records, q)}
    if cav.get("tissue"):   # base 767 有部分空 tissue 的 human
        strict = [r for r in records if passes_hard_filter(r, q)]
        lenient = [r for r in records if passes_hard_filter(r, replace(q, lenient_dims={"tissue"}))]
        added = [r for r in lenient if r not in strict]
        assert len(added) == cav["tissue"]
        assert all(not _dim_field_present(r, "tissue") for r in added)   # 新增全是空 tissue


# ---------- 缺失哨兵：「unknown」不是取值，是「没有取值」的另一种写法 ----------
def test_missing_sentinels_are_not_treated_as_verified():
    """`disease="unknown"` 不得被诚实层当成「已核验的真取值」。

     全盘审计：判空曾是裸 `.strip()` → 冻结 base 767 条里 **298 条（38.9%）**
    disease 写着 "unknown"/"n/a" 的记录被当成「已核验」→ caveat 恒空、lenient 恒无效，
    而同一界面的卡片对同一条记录显示「疾病：未说明」（introduction 认哨兵、retriever 不认）。
    """
    for token in ("unknown", "UNKNOWN", " n/a ", "N/A", "none", "null", "-", "未说明", "未知", ""):
        r = _mk("s" + token.strip(), species="human", disease=token)
        assert _dim_field_present(r, "disease") is False, f"哨兵 {token!r} 被误判为已核验"
    # 真实取值不受影响——尤其不能子串误伤：unknown syndrome 是一个真实病名
    for real in ("lung adenocarcinoma", "COVID-19", "normal", "unknown syndrome"):
        r = _mk("r" + real[:4], species="human", disease=real)
        assert _dim_field_present(r, "disease") is True, f"真实取值 {real!r} 被误判为未标注"


def test_base_disease_sentinels_enter_caveat_and_are_recoverable_by_lenient():
    """端到端（真·冻结语料）：搜疾病时，那 298 条未标注疾病的记录必须被报出来、且捞得回。

    修复前实测：caveat = []（层完全是死的），点「也纳入未标注疾病的数据」新增 0 条。
    这条断言不写死 298（它是 base 的现状、非政策），只钉「必须报 > 0 且 caveat == lenient 新增」。
    """
    records, settings = _load_base()
    rt = DatasetRetriever(top_k=5)
    q = parse_query("肿瘤相关的单细胞数据", settings.keyword_mapping)
    if q.abstain or not q.constraints.get("disease"):
        import pytest
        pytest.skip("词表未把该查询解析出 disease 约束 → 该断言不适用")

    cav = {c["dim"]: c["count"] for c in rt.coverage_caveats(records, q)}
    assert cav.get("disease"), (
        "base 有数百条 disease='unknown' 的记录，搜疾病时诚实层必须报「无法核验」——"
        "报空即回归到「把哨兵当已核验」的静默判负"
    )
    strict = [r for r in records if passes_hard_filter(r, q)]
    lenient = [r for r in records if passes_hard_filter(r, replace(q, lenient_dims={"disease"}))]
    added = [r for r in lenient if r not in strict]
    assert len(added) == cav["disease"], "caveat 计数必须等于点「也纳入」后真正新增数"


def test_sentinel_records_are_not_offered_as_facet_chips():
    """同一套哨兵口径也管分面：`disease=unknown` 不该变成一枚可点的「疾病: unknown」芯片。

    （此前分面自有一套 skip 集，与 introduction 的那套互不相同——收敛后三处同源。）
    """
    from dataset_recommender.retrieval.retriever import facet_value
    assert facet_value("disease", _mk("u", disease="unknown")) == ""
    assert facet_value("disease", _mk("n", disease="未说明")) == ""
    assert facet_value("disease", _mk("c", disease="COVID-19")) == "covid-19"


# ---------- workflow 接通 ----------
def test_workflow_run_with_meta_exposes_caveats_and_threads_lenient():
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow
    wf = DatasetRecommendationWorkflow()
    m = wf.run_with_meta(query="人类肺组织数据", use_llm=False)
    assert isinstance(m.coverage_caveats, list)   # 字段存在、结构正确
    # 宽容 tissue → 命中总数不减（纳入未标注的）、且该维 caveat 消失
    strict_total = m.result_total
    m2 = wf.run_with_meta(query="人类肺组织数据", use_llm=False, lenient_dims=["tissue"])
    assert m2.result_total >= strict_total
    assert not any(c["dim"] == "tissue" for c in m2.coverage_caveats)
