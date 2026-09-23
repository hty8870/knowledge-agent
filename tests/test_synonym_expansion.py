"""受控同义词表（retrieval/synonyms.py）产品层扩展测试。

背景（同义词扩展批）：评测侧 must_match 嵌套列表早有
同义类判定但只影响评分；产品检索层的自由词打分只认 query 原词，标题只写 carcinoma
的数据集在搜 cancer 时拿不到标题命中分。本文件锁三件事：

1. 词表加载与结构门（合法形状、坏形状启动即抛、双向词目合法）；
2. 扩展命中（搜 cancer 命中 carcinoma 条目，反向亦然——打分与排名两个层面）；
3. 无关词不被扩展（未登记词零行为变化，误扩展零容忍）。

维护规范见 synonyms.py 模块 docstring：新增词目必须回到这里补测试。
"""
import pytest

from dataset_recommender.retrieval.normalizer import DatasetRecord
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import (
    DatasetRetriever,
    _any_synonym_hits,
    _term_hits_text,
)
from dataset_recommender.retrieval.synonyms import (
    CONTROLLED_SYNONYMS,
    SYNONYM_TABLE,
    _load,
    expand_term,
    is_registered,
)


def _mk_record(name: str, disease: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="", disease=disease,
        chemistry="", count="", unit="", has_raw_data=None, url="",
        source_file="syn_test.json", description=f"description of {name}", raw={},
    )


# ---------- 1. 词表加载与结构门 ----------

def test_table_loads_and_covers_both_directions():
    # 首条词目 cancer↔carcinoma 双向：两个键映射到同一个组
    assert SYNONYM_TABLE["cancer"] == SYNONYM_TABLE["carcinoma"]
    assert set(SYNONYM_TABLE["cancer"]) == {"cancer", "carcinoma"}
    # 导出的受控表与加载产物同源（没有运行时偷偷加料）
    assert set(SYNONYM_TABLE) == {k.lower() for k in CONTROLLED_SYNONYMS}


def test_expand_term_registered_and_unregistered():
    assert expand_term("cancer") == ("cancer", "carcinoma")
    assert expand_term("CANCER") == ("cancer", "carcinoma")   # 大小写不敏感
    assert expand_term("carcinoma") == ("cancer", "carcinoma")
    # 未登记词：单元素组，原样返回
    for w in ("fibrosis", "pbmc", "xenium", "blood", "adenoma"):
        assert expand_term(w) == (w,)
    # 空串原样（调用方对空串判否）
    assert expand_term("") == ("",)
    assert is_registered("cancer") and is_registered("carcinoma")
    assert not is_registered("tumor") and not is_registered("")


def test_table_validator_rejects_bad_shapes():
    # 键不在自身组内
    with pytest.raises(ValueError):
        _load({"a": ("x", "y")})
    # 组内重复
    with pytest.raises(ValueError):
        _load({"a": ("a", "a")})
    # 组内空串
    with pytest.raises(ValueError):
        _load({"a": ("a", "")})
    # 同一成员映射到两个不同组（行为不可预期，禁止）
    with pytest.raises(ValueError):
        _load({"a": ("a", "b"), "b": ("b", "c")})
    # 双向词目（组内成员一致）是合法形态
    ok = _load({"a": ("a", "b"), "b": ("a", "b")})
    assert ok == {"a": ("a", "b"), "b": ("a", "b")}


# ---------- 2. 扩展命中（双向） ----------

def test_hit_judgement_bidirectional():
    # 搜 cancer，标题/描述只写 carcinoma → 命中
    assert _any_synonym_hits("cancer", "3k human squamous cell lung carcinoma dtcs")
    # 搜 carcinoma，文本只写 cancer → 命中
    assert _any_synonym_hits("carcinoma", "human breast cancer flex data")
    # 原词命中不受影响
    assert _any_synonym_hits("cancer", "ovarian cancer")
    assert _any_synonym_hits("carcinoma", "renal cell carcinoma")


def test_rank_score_lifts_carcinoma_only_title_for_cancer_query():
    """搜 cancer 时，标题只写 carcinoma 的记录拿到与写 cancer 的记录同等的标题分。"""
    ret = DatasetRetriever(top_k=5)
    carc = _mk_record("Human Renal Cell Carcinoma Xenium Data", disease="renal cell carcinoma")
    canc = _mk_record("Human Kidney Cancer Chromium Data", disease="kidney cancer")
    plain = _mk_record("Human PBMC Atlas", disease="")
    intent_c = parse_query("cancer")
    intent_k = parse_query("carcinoma")
    s_carc_c = ret._rank_score(carc, intent_c)
    s_canc_c = ret._rank_score(canc, intent_c)
    s_plain_c = ret._rank_score(plain, intent_c)
    # 标题同义命中 ≈ 标题原词命中（同为 +1.0，其余项相同构造 → 分数相同）
    assert s_carc_c == s_canc_c
    # 无关记录不吃扩展分
    assert s_plain_c < s_carc_c
    # 反向：搜 carcinoma 时写 cancer 的标题同样吃满
    assert ret._rank_score(canc, intent_k) == ret._rank_score(carc, intent_k)


def test_retrieve_ranks_carcinoma_entry_under_cancer_query():
    """端到端：检索结果里 carcinoma-only 条目在 cancer 查询下进入结果集。

    注：cancer 同时是硬约束（disease 维 targets 双形），无关记录被硬过滤淘汰是
    正确行为——本测的重点是 carcinoma-only 条目**不被淘汰且排进前列**，无关
    记录若因约束被淘汰则顺带断言它不在结果里。
    """
    records = [
        _mk_record("Human PBMC Atlas Bulk Data"),                       # 无关（被硬约束淘汰）
        _mk_record("Human Renal Cell Carcinoma Tissue Data",
                   disease="renal cell carcinoma"),                     # 只写 carcinoma
        _mk_record("Human Breast Cancer Flex Data", disease="breast cancer"),
    ]
    ret = DatasetRetriever(top_k=3)
    cands = ret.retrieve(records, parse_query("cancer"), top_k=3)
    names = [c.record.dataset_name for c in cands]
    assert "Human Renal Cell Carcinoma Tissue Data" in names
    # 与标题写 cancer 的记录同分（标题命中 +1.0 同源），并列按名字稳定排序
    assert "Human Breast Cancer Flex Data" in names


def test_retrieve_ranks_cancer_entry_under_carcinoma_query():
    """端到端反向：检索 carcinoma 时只写 cancer 的条目进入结果集。"""
    records = [
        _mk_record("Human Liver Atlas Data"),
        _mk_record("Human Breast Cancer Flex Data", disease="breast cancer"),
        _mk_record("Human Ovarian Carcinoma Visium Data", disease="ovarian carcinoma"),
    ]
    ret = DatasetRetriever(top_k=3)
    cands = ret.retrieve(records, parse_query("carcinoma"), top_k=3)
    names = [c.record.dataset_name for c in cands]
    assert "Human Breast Cancer Flex Data" in names
    assert "Human Liver Atlas Data" not in names


# ---------- 3. 无关词不被扩展（误扩展零容忍） ----------

def test_unrelated_terms_never_expand():
    # tumor/tumour/neoplasm 刻意不收（外延更宽，见 synonyms.py 词目 1 注释）：
    # 搜 cancer 不得命中只写 tumor 的文本
    assert not _any_synonym_hits("cancer", "benign tumor tissue bank")
    assert not _any_synonym_hits("carcinoma", "tumor microenvironment atlas")
    # 反方向也不得把 tumor 查询放宽到 cancer 文本
    assert expand_term("tumor") == ("tumor",)
    assert not _any_synonym_hits("tumor", "breast cancer flex")


def test_free_text_hit_unchanged_for_unregistered_words():
    """未登记词的命中判定与扩展前逐字等价（短词词边界 / 长词子串）。"""
    # 短 ASCII 词边界（历史行为：'ad' 不命中 'adult'）
    assert not _term_hits_text("ad", "adult tissue")
    assert _term_hits_text("ad", "the ad molecule")
    # 长词子串（'atlas' 命中 'atlases'）
    assert _term_hits_text("atlas", "human atlases of cells")
    # 长词不做词边界（'cancer' 命中 'cancerous'，扩展前即如此）
    assert _term_hits_text("cancer", "cancerous lesion")


def test_full_corpus_cancer_vs_carcinoma_top10_overlap():
    """真库冒烟：冻结 base 语料上，搜 cancer 与搜 carcinoma 的 Top1 必须同属癌症类

    （disease 字段含 cancer/carcinoma 之一），且 carcinoma-only 标题记录在 cancer
    查询的 top10 里与 cancer 标题记录同分并列——这正是本任务要修的缺口
    （扩展前它们拿不到标题 +1.0，被挤出并列梯队）。
    """
    from pathlib import Path

    from dataset_recommender.corpus.data_loader import load_raw_records
    from dataset_recommender.retrieval.normalizer import normalize_records

    records = normalize_records(load_raw_records(Path("database/base")))
    ret = DatasetRetriever(top_k=10)
    for q in ("cancer", "carcinoma"):
        cands = ret.retrieve(records, parse_query(q), top_k=10)
        assert cands, f"Q={q} 无结果"
        top1_d = cands[0].record.disease.lower()
        assert "cancer" in top1_d or "carcinoma" in top1_d
    # 扩展的核心收益（对照检索调研）：搜 cancer 时 carcinoma-only 标题与 cancer
    # 标题的记录分数并列（同为 3.0 梯队），不再系统性低 1.0 分。
    cands_c = ret.retrieve(records, parse_query("cancer"), top_k=10)
    scores = [c.score for c in cands_c]
    assert scores[0] == scores[-1], f"top10 应为同分并列梯队（实际 {scores}）"
    has_carc_title = any(
        "carcinoma" in c.record.dataset_name.lower()
        and "cancer" not in c.record.dataset_name.lower()
        for c in cands_c
    )
    has_canc_title = any("cancer" in c.record.dataset_name.lower() for c in cands_c)
    assert has_canc_title, "top10 应有 cancer 标题记录"
    # carcinoma-only 标题记录与 cancer 标题记录同分 → 两形在并列梯队里公平竞争，
    # family cap / 稳定排序决定谁最终入列（不再由词形写法决定）。
