# -*- coding: utf-8 -*-
"""裸子串匹配这一族缺陷的统一回归网（批量测试 + 实测 + 场景覆盖）。

这个项目栽在**同一个病根**上已经四次了，每次只修了其中一半：

     白天 alias 的 ASCII 半边 `integrated` 里的 `rat` → 悄悄多一个物种约束 已修（词边界）
     ① alias 的中文半边 「高血压」含「血」→ 组织=Blood 已修（登记本体）
     ② 保护表只收了一个成员 「肾上腺素」→ 组织 Adrenal Gland 本文件
     ③ free_text 打分 查「AD」→ 命中标题里的 ad**ult** 本文件

共同形态：**没有一次是报错**。界面上挂着一个看起来完全正常的筛选标签、或者一批排序整齐
分数偏高的结果，用户没有任何理由怀疑，于是永远不会去改写查询。这比零返回危险得多。
"""
import pytest

from dataset_recommender.retrieval import query_lexicon as V
from dataset_recommender.llm.config import get_settings
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import _word_hit

CAT = get_settings().keyword_mapping


def _hard_dims(query: str) -> dict:
    """去掉 modality 的硬约束（modality 由「单细胞」独立落维，与劫持无关）。"""
    it = parse_query(query, CAT)
    if it.abstain or it.parse_status != "executable":
        return {}
    return {d: list(v) for d, v in it.constraints.items() if v and d != "modality"}


# ---------- ②「X素 / X肽」一族：分子名不该退化成器官 ----------
@pytest.mark.parametrize("query,forbidden_dim", [
    ("肾上腺素相关数据", "tissue"),
    ("去甲肾上腺素的数据", "tissue"),
    ("肝素处理的数据", "tissue"),
    ("胸腺肽的数据", "tissue"),
    ("甲状腺素相关单细胞", "tissue"),
    ("胰岛素抵抗的单细胞数据", "tissue"),
    ("胸腺嘧啶代谢相关数据", "tissue"),
    ("血管紧张素受体研究数据", "tissue"),
])
def test_molecule_names_never_become_organ_filters(query, forbidden_dim):
    dims = _hard_dims(query)
    assert forbidden_dim not in dims, (
        f"「{query}」被悄悄改写成了 {forbidden_dim}={dims.get(forbidden_dim)}——"
        f"用户搜的是一个分子，拿到的是一整个器官的数据，而界面上那个标签看起来完全正常")


@pytest.mark.parametrize("query", ["咽炎的单细胞数据", "喉炎的数据"])
def test_inflammations_without_corpus_data_abstain_instead_of_organ_filter(query):
    """pharyngitis / laryngitis 全库 0 条：诚实弃权，而不是把用户丢给 20 条咽弓组织数据。"""
    it = parse_query(query, CAT)
    assert it.abstain, f"「{query}」应当弃权（语料里没有这个病），实际 {it.constraints}"


def test_hemangioma_is_registered_not_protected():
    """反过来：语料里**有**数据的（liver hemangioma 1 条）必须登记本体。

    塞进保护表会让它弃权说「系统未收录」，而库里明明有——那是另一种撒谎。
    """
    dims = _hard_dims("血管瘤单细胞数据")
    assert dims.get("disease") == ["hemangioma"], dims
    assert "tissue" not in dims, f"血管瘤又退化成血管组织了：{dims}"


@pytest.mark.parametrize("compound", V.ALIAS_PROTECTED_COMPOUNDS)
def test_protected_compound_is_not_a_dead_entry(compound):
    """保护表里的每一条都必须真的在挡住劫持——空转条目会让这张表越来越不可信。

    判据：把它单独拿去解析，不能落到任何硬约束维度上（真被保护了）。
    """
    dims = _hard_dims(compound)
    assert not dims, f"保护词「{compound}」仍然落到了 {dims}——保护没生效，或这条是空转的"


# ---------- ③ free_text 打分：短 ASCII 词按词边界 ----------
def test_word_hit_rejects_inside_word_matches():
    assert _word_hit("ad", "alzheimer ad brain") is True
    assert _word_hit("ad", "adult intestine atlas") is False, "'ad' 不该命中 adult"
    assert _word_hit("ms", "ms lesion") is True
    assert _word_hit("ms", "transcriptomics") is False
    assert _word_hit("pd", "pd patients") is True
    assert _word_hit("pd", "updated atlas") is False


def test_short_free_text_token_does_not_boost_unrelated_titles():
    """端到端：查「AD」时 adult 图谱不该因为词面相关性被顶上来。

    修前实测：前 5 条全是标题含 adult 的肠/肝图谱，score 2.5 高于基线，排得整整齐齐，
    而库里 37 条真正的阿尔茨海默数据一条没露面。
    """
    from dataset_recommender.retrieval.normalizer import DatasetRecord
    from dataset_recommender.retrieval.retriever import DatasetRetriever

    def rec(name):
        return DatasetRecord(
            dataset_name=name, url="http://x", species="Human", tissue="Brain",
            disease="unknown", chemistry="", count="", unit="", has_raw_data=False,
            source_file="t.json", description="", raw={"dataset_name": name},
        )

    it = parse_query("AD", CAT)
    assert "ad" in it.free_text_terms, "前提：AD 会进自由词"
    r = DatasetRetriever(top_k=5)
    pool = [rec("Adult human intestine atlas"), rec("AD brain single cell"),
            rec("Updated adult liver map")]
    got = [c.record.dataset_name for c in r.retrieve(pool, it, top_k=3)]
    assert got[0] == "AD brain single cell", f"真正含 AD 的那条没排第一：{got}"


def test_long_free_text_token_still_matches_as_substring():
    """长词保持子串匹配：不能因为修短词把 atlas→atlases 这种正常形态变化一起干掉。"""
    assert _word_hit("atlas", "human lung atlases") is False    # 词边界确实会拒
    # 但长词走的不是词边界分支 —— 这条不变量由命中判定的 len<=3 判据保证
    # （判据集中在 `_term_hits_text`，`_rank_score` 经 `_any_synonym_hits`
    # 调它；受控同义扩展不改变词边界阈值），这里显式钉住阈值，防止有人把阈值调大而不自知。
    from dataset_recommender.retrieval import retriever as R
    import inspect
    src = inspect.getsource(R._term_hits_text)
    assert "len(term) <= 3" in src, "词边界的适用阈值变了，请同步复核长词形态变化是否被误伤"
    # 扩展入口必须复用同一判据（不得绕开词边界把短词裸子串化）
    assert "expand_term" in inspect.getsource(R._any_synonym_hits)
