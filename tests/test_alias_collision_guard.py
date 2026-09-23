# -*- coding: utf-8 -*-
"""中文别名子串劫持的回归网。

**病根**：受控词表的别名匹配是裸子串。ASCII 那一半在 白天已按词边界收紧
（`integrated` 里的 `rat` 不再变成物种约束），中文这一半没有——中文本来不分词，没法照搬。
于是短别名会被**更长的、语义不同的**词包含：

    「高血压」含「血」 → 组织 = Blood
    「骨髓瘤」含「骨髓」 → 组织 = Bone Marrow
    「肺炎 / 肾炎 / 肝炎 / 胃炎 / 肾病」→ 各自退化成对应器官
    「单核细胞」含「单核」 → 模态 = 单细胞
    「皮质醇」含「皮质」 → 组织 = Cortex

这一类比零返回危险得多：界面上会挂一个看起来完全正常的「组织：Blood」标签，
用户没有任何理由怀疑，也就永远不会去改写查询。

**两道防线**（本文件分别钉死）：
  1. **登记本体**——把这些概念本身收进词表。alias 消费是全局长度降序，3 字的「高血压」
     天然压过 1 字的「血」，病根自动消失（和「肺癌」压过「肺」是同一个机制）。
     这是首选，因为它同时把「有数据却查不到」一起修了。
  2. **保护复合词**（`V.ALIAS_PROTECTED_COMPOUNDS`）——概念本身没有可筛维度、无法登记时
     （单核细胞是细胞类型、皮质醇是分子），在 alias 消费**之前**整体屏蔽，
     然后按描述词如实回显「未作为筛选维度」，不静默丢弃。

本文件是**行为**测试，不是成员表快照：断言的是「这句话解析成什么」，
所以后续任何人改词表、只要把某个词重新劫持回去，这里就会红。
"""
import pytest

from dataset_recommender.retrieval import query_lexicon as V
from dataset_recommender.domains.biodata import vocabulary as _BV
from dataset_recommender.llm.config import get_settings
from dataset_recommender.retrieval.query_parser import parse_query

CAT = get_settings().keyword_mapping


def _dims(query: str) -> dict:
    """去掉 modality 后的硬约束维度 → display 名（modality 由「单细胞」独立落维，与劫持无关）。"""
    intent = parse_query(query, CAT)
    if intent.abstain or intent.parse_status != "executable":
        return {"__abstain__": intent.abstain_reason or intent.parse_status}
    return {d: list(intent.display_map.get(d, [])) for d, v in intent.constraints.items()
            if v and d != "modality"}


# ---------- 防线 1：曾被劫持的词，现在必须落在**正确**维度 ----------
# 每条都是实测抓到过的真实错筛，不是假想。
HIJACK_CASES = [
    # (查询词, 必须出现的维度, 必须出现的 display, 绝不能出现的维度)
    ("高血压",   "disease", "Hypertension",            "tissue"),
    ("血栓",     "disease", "Thrombosis",              "tissue"),
    ("贫血",     "disease", "Anemia",                  "tissue"),
    ("出血",     "disease", "Hemorrhage",              "tissue"),
    ("骨髓瘤",   "disease", "Myeloma",                 "tissue"),
    ("脑梗",     "disease", "Stroke",                  "tissue"),
    ("肝炎",     "disease", "Hepatitis",               "tissue"),
    ("肝硬化",   "disease", "Cirrhosis",               "tissue"),
    ("脂肪肝",   "disease", "Fatty Liver Disease",     "tissue"),
    ("肺炎",     "disease", "Pneumonia",               "tissue"),
    ("肺气肿",   "disease", "Emphysema",               "tissue"),
    ("胃炎",     "disease", "Gastritis",               "tissue"),
    ("肾炎",     "disease", "Nephropathy/Nephritis",   "tissue"),
    ("肾病",     "disease", "Nephropathy/Nephritis",   "tissue"),
    ("心肌炎",   "disease", "Myocarditis",             "tissue"),
    ("心肌病",   "disease", "Cardiomyopathy",          "tissue"),
    ("骨关节炎", "disease", "Osteoarthritis",          "tissue"),
    # 反向：这些是**组织**，别被登记成疾病
    ("血管",     "tissue",  "Blood Vessel",            "disease"),
    ("肾上腺",   "tissue",  "Adrenal Gland",           "disease"),
    ("子宫内膜", "tissue",  "Endometrium",             "disease"),
    ("软骨",     "tissue",  "Cartilage",               "disease"),
]


@pytest.mark.parametrize("term,dim,display,forbidden_dim", HIJACK_CASES)
def test_previously_hijacked_terms_land_on_right_dimension(term, dim, display, forbidden_dim):
    got = _dims(term + "的单细胞数据")
    assert "__abstain__" not in got, f"「{term}」不该弃权：{got}"
    assert display in got.get(dim, []), f"「{term}」应落 {dim}={display}，实得 {got}"
    assert forbidden_dim not in got, (
        f"「{term}」不该同时落 {forbidden_dim} —— 这正是短别名子串劫持的样子：{got}")


def test_endometrium_tissue_and_cancer_are_separate_concepts():
    """「子宫内膜」曾被整个登记成 Endometrial Cancer —— 维度归属反了。
    组织和癌症是两个概念，分别要能查到，且组织查询不得被塞进疾病约束。"""
    tissue = _dims("子宫内膜的单细胞数据")
    cancer = _dims("子宫内膜癌的单细胞数据")
    assert tissue == {"tissue": ["Endometrium"]}, tissue
    assert "Endometrial Cancer" in cancer.get("disease", []), cancer


# ---------- 防线 2：保护复合词 ----------
PROTECTED_CASES = [
    ("单核细胞", "modality"),   # 含「单核」→ 曾被当成单核测序
    ("胸腺嘧啶", "tissue"),     # 含「胸腺」→ 曾被当成组织 Thymus
    ("血管紧张素", "tissue"),   # 含「血管」「血」→ 曾被当成组织
    ("胰岛素",   "tissue"),     # 含「胰」→ 曾被当成组织 Pancreas
]


@pytest.mark.parametrize("compound,leaked_dim", PROTECTED_CASES)
def test_protected_compounds_do_not_leak_a_constraint(compound, leaked_dim):
    """保护的**唯一**职责：不让短别名劫持出一个错误的硬约束。

     修正：此前这条还顺带断言 `parse_status == "executable"`，
    等于把「屏蔽」和「当作已知词放行」绑成了一件事。真实后果见
    `test_unknown_protected_compound_still_abstains`：「胰岛素」变成了返回全库 5665 条。
    现在只钉「不许漏出错误约束」这一条，放不放行由下面两条分类测试各自负责。
    """
    intent = parse_query("人类" + compound + "数据", CAT)
    assert leaked_dim not in intent.constraints or not intent.constraints.get(leaked_dim), (
        f"「{compound}」把 {leaked_dim} 约束漏出来了：{intent.constraints}")


# 保护词分两类，判据取自 FILLER_DOMAIN 的成员资格（见 query_parser._protected_echoable）：
_ECHOABLE = [c for c in V.ALIAS_PROTECTED_COMPOUNDS if c in set(V.FILLER_DOMAIN)]
_UNKNOWN = [c for c in V.ALIAS_PROTECTED_COMPOUNDS if c not in set(V.FILLER_DOMAIN)]


@pytest.mark.parametrize("compound", _ECHOABLE)
def test_known_but_dimensionless_compound_is_echoed_not_silently_dropped(compound):
    """「单核细胞」这种**有名有姓、系统只是没有这个维度**的：放行 + 回显「未作为筛选维度」。"""
    intent = parse_query("人类" + compound + "数据", CAT)
    assert intent.parse_status == "executable", (compound, intent.abstain_reason)
    assert compound in intent.unused_query_terms, (compound, intent.unused_query_terms)
    assert intent.constraints.get("species") == ["human"], intent.constraints


@pytest.mark.parametrize("compound", _UNKNOWN)
def test_unknown_protected_compound_still_abstains(compound):
    """「胰岛素 / 肾上腺素 / 咽炎」这种**系统压根不认识**的：必须照常弃权，并引述这个词本身。

    这是回归验证抓到的 major：屏蔽发生在 alias 消费**之前**，于是残差弃权门
    也看不见这个词了 —— 「胰岛素」从「诚实弃权」变成「返回全库 5665 条、零个筛选芯片、
    首屏全是 CRISPRi K562 和 PBMC」。同一词类的「皮质醇 / 多巴胺」（不在保护表里）却照常弃权，
    两套互斥口径同时存在。屏蔽只该挡劫持，不该顺手把「我不认识这个词」一起抹掉。
    """
    intent = parse_query("人类" + compound + "数据", CAT)
    assert intent.abstain and intent.abstain_reason == "unresolved_term", (
        f"「{compound}」没有弃权：status={intent.parse_status} constraints={intent.constraints}")
    assert compound in intent.unresolved_terms, (compound, intent.unresolved_terms)
    assert compound not in intent.unused_query_terms, (
        f"「{compound}」同时被说成「未收录」和「无对应维度」——两句话不可能都对")


def test_protected_list_has_no_dead_entries():
    """表里每一条都必须**确有劫持**才留着：把它从保护里拿掉就会漏出约束。
    这条防的是预防性堆砌——没有实际作用的条目会让人误以为有保护。"""
    cn_aliases = [str(a) for entries in _BV.CATALOG.values() for e in entries
                  for a in e.get("aliases", []) if all("一" <= ch <= "鿿" for ch in str(a))]
    dead = [c for c in V.ALIAS_PROTECTED_COMPOUNDS
            if not any(a in c and a != c for a in cn_aliases)]
    assert not dead, f"这些保护项没有任何中文别名会命中、屏蔽是空转的，应删除：{dead}"


# ---------- 覆盖：本轮登记的概念确实能查到（不是登记了个寂寞）----------
NEW_CONCEPTS = [
    ("膀胱",       "tissue",  "Bladder"),
    ("耳蜗",       "tissue",  "Cochlea"),
    ("下丘脑",     "tissue",  "Hypothalamus"),
    ("杏仁核",     "tissue",  "Amygdala"),
    ("纹状体",     "tissue",  "Striatum"),
    ("口腔",       "tissue",  "Oral Cavity"),
    ("大脑皮层",   "tissue",  "Cortex"),
    ("纤维化",     "disease", "Fibrosis"),
    ("多囊肾",     "disease", "Polycystic Kidney Disease"),
    ("巨细胞病毒", "disease", "Cytomegalovirus Infection"),
    ("干燥综合征", "disease", "Sjogren Syndrome"),
    ("肌营养不良", "disease", "Muscular Dystrophy"),
]


@pytest.mark.parametrize("term,dim,display", NEW_CONCEPTS)
def test_new_concepts_are_executable(term, dim, display):
    got = _dims(term + "的单细胞数据")
    assert "__abstain__" not in got, f"「{term}」仍在弃权：{got}"
    assert display in got.get(dim, []), got


def test_muscular_dystrophy_no_longer_reads_as_negation():
    """「肌营养不良」里的「不良」曾让整句报 unsupported_negation（「不」被当排除操作符）。
    登记成实体后 _entity_spans 会保护 span 内的否定字。"""
    intent = parse_query("人类肌营养不良的单细胞数据", CAT)
    assert intent.parse_status == "executable"
    assert intent.abstain_reason != "unsupported_negation"
    assert not any(intent.excluded_constraints.get(d) for d in intent.excluded_constraints)


# ---------- 语法词：和「来自」同类的动词/虚词不得再触发整句弃权 ----------
FUNCTION_WORD_QUERIES = [
    "人类心脏心衰的单细胞数据，2022 年之后发表",
    "人类前列腺癌的空间数据，要原始 FASTQ",
    "人类卵巢癌的 Xenium 空间数据，要原始文件",
    "人类肝癌的单细胞 RNA 和 ATAC 联合数据",
]


@pytest.mark.parametrize("query", FUNCTION_WORD_QUERIES)
def test_function_words_do_not_abstain(query):
    intent = parse_query(query, CAT)
    assert intent.parse_status == "executable", (query, intent.abstain_reason, intent.abstain_detail)


def test_raw_requirement_survives_the_function_word_fix():
    """「要原始文件」必须仍然筛 FASTQ。把「原始」丢进 filler 会让它变成**静默不筛**——
    用户明确要了原始数据却没筛，比整句弃权更糟。"""
    for q in ("人类卵巢癌的数据，要原始文件", "人类前列腺癌的空间数据，要原始 FASTQ",
              "人类肺数据，要原始序列"):
        intent = parse_query(q, CAT)
        assert intent.has_raw_data_required is True, (q, intent.has_raw_data_required)
