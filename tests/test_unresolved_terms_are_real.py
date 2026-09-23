# -*- coding: utf-8 -*-
"""弃权时引述给用户的词，必须是用户**真的打过**的（查询电池回归）。

**病根**：`_residual_salient` 判残差时，把 filler 从一段汉字里直接 `replace(f, "")` **抠掉**，
于是被抠词左右的残字紧挨在一起，拼出一个原句里**根本不存在**的词；这个词随后被
`unresolved_terms` 和弃权文案原样引述回去。实测三例（全库 5665 条）：

    白细胞介素相关单细胞数据            → 「查询里有系统未收录的词：『白介素』」
    10x Genomics 的人类外周血单个核细胞数据 → 「…『单核』」（顺带把 PBMC 这种最常见的说法打成弃权）
    我需要一些多组学的人类数据集用来做整合分析  → 「…『来整合』」

第一条是**诚实性缺陷**：本项目的全部价值建立在「宁可说查不到，也不说假话」上，
而这里是直接把一句用户没说过的话塞进他嘴里，还拿它当弃权理由。

**修法**：filler 换成分隔符再按空白切片。残差只会减少或持平，不会凭空多出弃权。

本文件钉的是**不变量**（引述的词必须是原句的连续子串），不是具体几个例子——
以后任何人再引入「拼词」的写法，这里都会红。
"""
import re

import pytest

from dataset_recommender.retrieval import query_lexicon as V
from dataset_recommender.llm.config import get_settings
from dataset_recommender.retrieval.query_parser import (
    _residual_salient,
    parse_query,
)

CAT = get_settings().keyword_mapping

# 覆盖各档：能查到的 / 该弃权的 / 执行类说法 / 长句口语 / 中英混排。
# 不变量对**每一句**都成立，与这句话最终是不是弃权无关。
CORPUS = [
    "白细胞介素相关单细胞数据",
    "10x Genomics 的人类外周血单个核细胞数据",
    "我需要一些多组学的人类数据集用来做整合分析",
    "给我拉取一下人类肝脏的单细胞数据",
    "巨细胞病毒感染的单细胞数据",
    "特发性肺纤维化的单细胞转录组",
    "胰岛素抵抗的单细胞数据",
    "胸腺嘧啶代谢相关数据",
    "血管紧张素受体研究数据",
    "脑膜瘤单细胞测序",
    "翼龙的单细胞数据",
    "霍格沃茨综合征的人类数据",
    "人类造瘘相关单细胞数据",
    "我想找人类肺癌的免疫细胞单细胞转录组数据，最好有原始数据",
    "有没有人类胰腺 beta 细胞的 scRNA-seq 数据",
    "帮我看看有没有小鼠心脏发育的单细胞数据集",
    "人类肺数据，帮我打包前20条",
    "人类肺癌数据，生成下载脚本",
    "单核细胞的单细胞数据",
    "人类肺数据 <script>alert(1)</script>",
    "找一些做空间转录组的人类肿瘤数据",
    "2022 年之后发表的心衰单细胞数据",
    "人类肺数据，不要癌症的",
    "人类肺数据，优先 10x",
    "integrated human lung atlas",
]


def _norm(s: str) -> str:
    """比对用的归一：只留字母数字汉字。弃权词取自 lower 后的工作串，原句可能有大小写/标点差异。"""
    return re.sub(r"[^0-9a-z一-鿿]", "", (s or "").lower())


@pytest.mark.parametrize("query", CORPUS)
def test_unresolved_terms_are_substrings_of_the_query(query):
    """**核心不变量**：每一个被引述的「未收录的词」都必须是原句的连续子串。

    不是「拼出来的」、不是「归一化后碰巧像」——就是用户敲进去的那几个字连在一起。
    """
    intent = parse_query(query, CAT)
    hay = _norm(query)
    for term in intent.unresolved_terms:
        assert _norm(term) in hay, (
            f"弃权文案引述了原句里没有的词：{term!r}\n"
            f"  原句：{query!r}\n"
            f"  这就是「白细胞介素 → 白介素」那一类：把用户没说过的话塞进他嘴里当弃权理由。"
        )


@pytest.mark.parametrize("query", CORPUS)
def test_abstain_detail_quotes_only_real_words(query):
    """弃权**文案**里用「」引起来的词同样必须来自原句（文案和结构化字段两条路都不能撒谎）。"""
    intent = parse_query(query, CAT)
    if intent.abstain_reason != "unresolved_term":
        pytest.skip("只约束未收录词弃权的文案")
    hay = _norm(query)
    quoted = re.findall(r"「([^」]+)」", intent.abstain_detail or "")
    assert quoted, "未收录词弃权必须把卡住的词引出来，否则用户不知道改哪个字"
    for word in quoted:
        assert _norm(word) in hay, f"弃权文案引述了原句里没有的词：{word!r}（原句 {query!r}）"


def test_residual_pieces_never_span_a_removed_filler():
    """直接钉住修法本身：filler 必须换成分隔符，不能删掉。

    构造一个「实义字 + filler + 实义字」的工作串——删掉 filler 会拼出「甲乙」，
    换成分隔符则两边各自不足 2 字、双双落空。
    """
    filler = sorted((f for f in V.FILLER_TOKENS if re.fullmatch(r"[一-鿿]+", f)),
                    key=len, reverse=True)
    probe = "甲" + filler[0] + "乙"
    out = _residual_salient(probe)
    assert "甲乙" not in out, (
        f"「{probe}」抠掉 filler「{filler[0]}」后拼出了「甲乙」——这正是幻影词的成因"
    )


def test_specific_phantoms_are_gone():
    """三个实测幻影词各钉一条：光有不变量测试，回归时看不出是哪一句坏了。"""
    for query, phantom in (
        ("白细胞介素相关单细胞数据", "白介素"),
        ("10x Genomics 的人类外周血单个核细胞数据", "单核"),
        ("我需要一些多组学的人类数据集用来做整合分析", "来整合"),
    ):
        intent = parse_query(query, CAT)
        assert phantom not in intent.unresolved_terms, f"{query!r} 又拼出了幻影词 {phantom!r}"
        assert phantom not in (intent.abstain_detail or ""), \
            f"{query!r} 的弃权文案里又出现了幻影词 {phantom!r}"


def test_unresolved_terms_are_deduplicated():
    """同一个词出现两次，不该在弃权文案/降级建议里说两遍。

    实测：`人类肺数据 <script>alert(1)</script>` 的 ignored_terms 是 ['script','alert','script']。
    """
    intent = parse_query("人类肺数据 <script>alert(1)</script>", CAT)
    terms = list(intent.unresolved_terms)
    assert terms == list(dict.fromkeys(terms)), f"未收录词有重复：{terms}"


def test_abstain_names_the_protected_compound_it_could_not_resolve():
    """弃权态必须说清楚「胰岛素」这几个字也没参与筛选，不能只提「抵抗」。

    修前：`unused_query_terms` 和 `unresolved_terms` 里都没有「胰岛素」——屏幕上只说
    「未收录的词：『抵抗』」，用户会以为胰岛素还在生效，降级建议的 `ignored_terms` 同样只有「抵抗」。
    修后它进 `unresolved_terms`（系统不认识这个词），而不是 `unused_query_terms`
    （有这个词、只是没有对应维度）——后者对分子名是**假话**。
    """
    intent = parse_query("胰岛素抵抗的单细胞数据", CAT)
    assert intent.abstain, "这句仍应弃权（系统不认识胰岛素，也没有胰岛素抵抗这个维度）"
    assert "胰岛素" in intent.unresolved_terms, \
        f"弃权时没点名被屏蔽的复合词：unresolved={intent.unresolved_terms}"


_ECHOABLE_PROTECTED = [c for c in V.ALIAS_PROTECTED_COMPOUNDS if c in set(V.FILLER_DOMAIN)]


@pytest.mark.parametrize("compound", _ECHOABLE_PROTECTED)
def test_echoable_protected_compound_is_echoed_when_abstaining(compound):
    """「单核细胞」这类**有名有姓、只是没有对应维度**的保护词，弃权态也要回显。

    （系统压根不认识的那一类不在此列——它们由残差门点名，见 test_alias_collision_guard
    的 `test_unknown_protected_compound_still_abstains`。同一个词不能既「未收录」又「无维度」。）
    """
    intent = parse_query(f"{compound}的翼龙数据", CAT)
    assert intent.abstain, "含翼龙必然弃权"
    assert compound in intent.unused_query_terms, \
        f"「{compound}」被整体屏蔽却没在弃权态回显：{intent.unused_query_terms}"


# ---- 新收词条不能是空转的（与 test_alias_collision_guard 的死条目检查同一个思路）----

@pytest.mark.parametrize("query,expect_results", [
    ("给我拉取一下人类肝脏的单细胞数据", True),     # 「一下」
    ("巨细胞病毒感染的单细胞数据", True),           # 「感染」
    ("我需要一些多组学的人类数据集用来做整合分析", True),  # 「整合分析」
    ("10x Genomics 的人类外周血单个核细胞数据", True),  # 「genomics」+ 幻影词修复
    ("特发性肺纤维化的单细胞转录组", True),          # 「特发性」
])
def test_new_filler_entries_actually_unblock_queries(query, expect_results):
    """这五句在修之前**每一句都整句弃权**，连里面能查到的东西都查不到。"""
    intent = parse_query(query, CAT)
    assert (not intent.abstain) == expect_results, \
        f"{query!r} 期望 {'可执行' if expect_results else '弃权'}，实际 {intent.abstain_reason or 'executable'}"


def test_idiopathic_is_echoed_not_silently_dropped():
    """「特发性」进的是 FILLER_DOMAIN 不是 GRAMMAR：疾病只落到 Pulmonary Fibrosis
    （各种成因混在一起），把 idiopathic 这个限定悄悄丢掉还不吭声，就是静默丢词。"""
    intent = parse_query("特发性肺纤维化的单细胞转录组", CAT)
    assert not intent.abstain
    assert "特发性" in intent.unused_query_terms, \
        f"「特发性」被丢掉却没回显：{intent.unused_query_terms}"


def test_infection_is_not_echoed_because_it_is_already_a_matched_value():
    """反过来，「感染」进的是 GRAMMAR：disease 已经落成 Cytomegalovirus **Infection**，
    再回显「『感染』未作为筛选维度」就是撒谎——正是诚实层要消灭的那种谎的镜像。"""
    intent = parse_query("巨细胞病毒感染的单细胞数据", CAT)
    assert not intent.abstain
    assert intent.constraints.get("disease"), "巨细胞病毒应当落到 disease 维度"
    assert "感染" not in intent.unused_query_terms, \
        "「感染」是已落维度那个值的尾巴，不该被报成「未作为筛选维度」"
