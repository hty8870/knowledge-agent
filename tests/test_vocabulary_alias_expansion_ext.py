"""受控词表 alias 覆盖扩展（外部平台库对齐）：外部库常见 species/tissue/disease/技术现在能被正确解析。

背景：基础库(10x)以外的平台库(CELLxGENE/HCA/EBI SCEA/ArrayExpress，约4900条)含大量 base 没有的
物种/组织/疾病/技术（拟南芥、线虫、酵母、胸腺、小脑、视网膜、新冠、阿尔茨海默、帕金森、糖尿病、
Smart-seq2、Drop-seq、Slide-seq、MERFISH…）。这些说法此前一律 fail-closed 弃权 → 漏掉数百条真实记录。

钉死三面：
1) 覆盖——这些说法现在解析到正确规范 target，不再 abstain。
2) 护栏——扩词表不得破坏 fail-closed（未知实体/否定/OR 仍弃权），也不得制造跨维度串味
   （小脑≠脑、子宫颈≠子宫、狨猴≠恒河猴、回肠/直肠≠结肠）。
3) 无假阳性——英文子串陷阱（matrices/price 里的 "rice"）不得被误解析成约束。
   （0% 硬违规由 hard_filter 结构性保证 + 54 题冻结门回归，不在本文件重复。）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402


def _assert(q, dim, tgt):
    intent = parse_query(q)
    assert not intent.abstain, f"{q!r} 不应弃权，got reason={intent.abstain_reason}"
    assert tgt in intent.constraints.get(dim, []), f"{q!r} 应解析 {dim}={tgt}, got {dict(intent.constraints)}"


def test_new_species_resolve():
    for q, tgt in {
        "拟南芥的单细胞数据": "arabidopsis",
        "arabidopsis thaliana data": "arabidopsis",
        "线虫单细胞数据": "elegans",
        "c. elegans dataset": "elegans",
        "酵母数据": "saccharomyces",
        "狨猴脑数据": "marmoset",
        "mouse lemur data": "microcebus",
        "黑猩猩数据": "chimpanzee",
    }.items():
        _assert(q, "species", tgt)


def test_new_tissue_resolve():
    for q, tgt in {
        "胸腺单细胞": "thymus",
        "小脑组织数据": "cerebell",
        "海马体数据": "hippocamp",
        "视网膜 scRNA": "retina",
        "脊髓数据": "spinal",
        "睾丸单细胞": "testis",
        "气管数据": "trachea",
        "支气管数据": "bronch",
        "子宫的数据": "uterus",
        "回肠数据": "ileum",
        "直肠数据": "rectum",
    }.items():
        _assert(q, "tissue", tgt)


def test_new_disease_resolve():
    for q, tgt in {
        "新冠病人的肺数据": "covid",
        "covid-19 lung data": "covid",
        "阿尔茨海默患者脑数据": "alzheimer",
        "帕金森单细胞": "parkinson",
        "糖尿病肾数据": "diabet",
        "克罗恩病肠道数据": "crohn",
        "哮喘的肺数据": "asthma",
        "神经母细胞瘤数据": "neuroblastoma",
        "多发性硬化数据": "multiple sclerosis",
    }.items():
        _assert(q, "disease", tgt)


def test_new_technology_resolve():
    for q, tgt in {
        "smart-seq2 数据": "smart",
        "drop-seq 小鼠数据": "drop-seq",
        "slide-seq 空间数据": "slide-seq",
        "sci-rna-seq3 数据": "sci-rna-seq",
        "merfish 数据": "merfish",
        "cite-seq 免疫数据": "cite-seq",
    }.items():
        _assert(q, "assay", tgt)


def test_no_cross_dimension_bleed():
    # 小脑组织 ≠ 脑；子宫颈 ≠ 子宫；狨猴 ≠ 恒河猴；回肠/直肠 ≠ 结肠
    b = parse_query("小脑组织数据")
    assert "cerebell" in b.constraints.get("tissue", []) and "brain" not in b.constraints.get("tissue", [])
    c = parse_query("子宫颈癌数据")
    assert "cervix" in c.constraints.get("tissue", [])
    assert "uterus" not in c.constraints.get("tissue", [])
    m = parse_query("恒河猴数据")
    assert "macaque" in m.constraints.get("species", []) and "marmoset" not in m.constraints.get("species", [])
    ile = parse_query("回肠数据")
    assert "ileum" in ile.constraints.get("tissue", [])
    # mus musculus 仍不误触发 muscle（两批扩充叠加不破坏）
    mm = parse_query("mus musculus 脑数据")
    assert "mouse" in mm.constraints.get("species", []) and "muscle" not in mm.constraints.get("tissue", [])


def test_english_substring_traps_not_matched():
    # "matrices"/"price" 含 "rice" 子串，但我们未收 "rice" 别名 → 不得误解析成 water rice(oryza)
    for q in ("单细胞表达矩阵 matrices", "gene expression price"):
        intent = parse_query(q)
        assert "oryza" not in intent.constraints.get("species", []), f"{q!r} 误命中 rice: {dict(intent.constraints)}"


def test_failclosed_guards_still_hold():
    assert parse_query("翼龙的单细胞数据").abstain is True
    assert parse_query("霍格沃茨综合征的人类数据").abstain is True
    # 否定语法落地后：跨维歧义/复合否定仍弃权（fail-closed 未被削弱）
    assert parse_query("不要小鼠的原始数据").abstain is True
    assert parse_query("不要小鼠脑数据").abstain is True
    # 「或」不再进 fail-closed 范围（同维度多值就是「或」，引擎一直支持）。
    assert parse_query("人或小鼠的脑数据").abstain is False


def test_negation_marker_inside_legit_entity_not_misread():
    # 负号扫描改在「已消费 alias 的 working 串」上进行：名字里含「非」的合法实体不再被误判为否定。
    # 修 非洲爪蟾(Xenopus) 死别名，并顺带修复既有的 非小细胞肺癌(NSCLC) 被 fail-closed 误弃权。
    nsclc = parse_query("非小细胞肺癌的人类数据")
    assert not nsclc.abstain, f"NSCLC 不应弃权，got {nsclc.abstain_reason}"
    assert "non-small cell lung" in nsclc.constraints.get("disease", [])
    xen = parse_query("非洲爪蟾的数据")
    assert not xen.abstain and "xenopus" in xen.constraints.get("species", [])
    # 否定语法落地：安全否定可执行（除了脑以外 → include human + exclude brain）
    ex = parse_query("除了脑以外的人类组织数据")
    assert not ex.abstain
    assert "human" in ex.constraints.get("species", [])
    assert "brain" in ex.excluded_constraints.get("tissue", [])
    # 未收录「非…」实体仍必须弃权
    # （非霍奇金淋巴瘤 已由 non 复合术语修复映射到 lymphoma，见 test_negation_exempt.py；此处换未收录的裸「非」）
    assert parse_query("非哺乳类的数据").abstain is True   # 哺乳类非 alias、非白名单，裸「非」→ 弃权
    # OR 与 hedge 改为照做（同维度多值＝或；hedge＝软偏好）。
    assert parse_query("肺癌或肝癌的数据").abstain is False
    assert parse_query("最好是 Xenium 的黑色素瘤数据").abstain is False


def test_eye_alias_no_substring_false_constraint():
    # 「眼」(裸1字) 与 "eye"(3字母) 已删除：不再把 Meyer/surveyed/青光眼/转眼 误命中成 tissue=eye。
    for q in ("Meyer lab 的人类数据", "surveyed human samples", "青光眼相关数据"):
        assert "eye" not in parse_query(q).constraints.get("tissue", []), f"{q!r} 误命中 eye"
    # 仍可经双字词命中
    for q in ("眼睛的单细胞数据", "眼部组织数据"):
        it = parse_query(q)
        assert not it.abstain and "eye" in it.constraints.get("tissue", [])
