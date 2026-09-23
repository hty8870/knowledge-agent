# -*- coding: utf-8 -*-
"""诚实层第三态：「已标注但值集不完整」不得被当成「已核验」。

背景（验证证伪的设计缺陷）：SCEA 的 design 文件逐细胞、最大 453MB，只能按 Range 抽样
（中位覆盖 2.5%、最差 0.1%）。把抽样得来的 tissue 填进字段后，`_dim_field_present` 原先只看「非空」，
于是记录从「不知道」**静默升格**成「已知」：

    查询 species=human, tissue=alveolus（肺泡）
      修复前(字段全空)：caveat 显式报出 160 条「无法核验」，lenient 可捞回
      仅补字段    ：SCEA 命中 0、caveat 只报 7 条、lenient 也捞不回 E-ANND-1（人类肺图谱！）

即：搜「肺」修好了，搜「肺泡」修坏了——静默排除换了个规模继续存在，且连信号都没了。

语义契约（本文件钉死）：
- 抽样值**命中** → 可信证据，正常命中（走 `_field_contains`，不经 `_dim_field_present`）。
- 抽样值**不命中** → **不构成否证** → 仍算「无法核验」→ 进 caveat、可被 lenient 纳入。
- 没有 provenance 的记录（冻结 base/10x + cellxgene/hca/arrayexpress）→ 视作完整 → 判定逐位不变。
"""
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest  # noqa: E402

from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_dataset_record  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import (  # noqa: E402
    DatasetRetriever,
    _dim_field_present,
    _dim_value_set_complete,
    constraint_satisfied,
    passes_hard_filter,
)


def _rec(uid, tissue, prov=None, **extra):
    raw = {
        "dataset_name": f"ds-{uid}", "species": "Human", "tissue": tissue, "disease": "",
        "chemistry": "10xv3", "count": "1000", "unit": "Cells", "has_raw_data": False,
        "url": "https://example.org/" + uid, "download_url": "https://example.org/" + uid,
        "description": "d", "source": "EBI Single Cell Expression Atlas", "dataset_uid": uid,
    }
    raw.update(extra)
    if prov is not None:
        raw["metadata_provenance"] = prov
    return normalize_dataset_record(raw, "ebi_scea.json")


SAMPLED = {"origin": "declared", "complete": False, "total_bytes": 453559278,
           "sampled_bytes": 458752, "rows_parsed": 577}
FULL = {"origin": "declared", "complete": True, "total_bytes": 72482,
        "sampled_bytes": 72482, "rows_parsed": 118}


# --------------------------------------------------- _dim_value_set_complete


def test_no_provenance_is_treated_as_complete():
    """冻结 base/10x 与其余三库没有 provenance → 必须沿用旧语义（视作完整），否则全库被降级成
    「无法核验」、caveat 会爆炸。"""
    r = _rec("x", "lung")
    assert _dim_value_set_complete(r, "tissue") is True
    assert _dim_field_present(r, "tissue") is True


def test_only_explicit_false_counts_as_incomplete():
    """provenance 形状漂移（缺键 / None / 非 dict）一律保守视作完整——宁可少报 caveat，
    也不要因为一次 schema 变更把全库判成不可核验。"""
    for prov in ({"origin": "declared"}, {"complete": None}, {"complete": "no"}, "not-a-dict", None):
        r = _rec("x", "lung", prov=prov) if prov is not None else _rec("x", "lung")
        assert _dim_value_set_complete(r, "tissue") is True, f"prov={prov!r}"
    assert _dim_value_set_complete(_rec("x", "lung", prov=SAMPLED), "tissue") is False


def test_completeness_only_applies_to_sampled_dims():
    """species/platform/assay/modality 不由抽样得来 → 不受 provenance 影响。"""
    r = _rec("x", "lung", prov=SAMPLED)
    for dim in ("species", "platform", "assay", "modality"):
        assert _dim_value_set_complete(r, dim) is True


# ------------------------------------------------------- _dim_field_present


def test_sampled_record_with_value_is_not_counted_as_verified():
    """核心：抽样记录**有值**，但值集不全 → 仍算「无法核验」。"""
    r = _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED)
    assert r.tissue, "前置：字段确实非空"
    assert _dim_field_present(r, "tissue") is False, "有值但取自 0.1% 抽样 → 不能算已核验"


def test_full_read_record_with_value_is_verified():
    r = _rec("ebi:E-CURD-10", "kidney", prov=FULL)
    assert _dim_field_present(r, "tissue") is True


def test_empty_field_is_unverified_regardless_of_completeness():
    assert _dim_field_present(_rec("x", "", prov=FULL), "tissue") is False
    assert _dim_field_present(_rec("x", "", prov=SAMPLED), "tissue") is False


# ------------------------------------------------------ constraint_satisfied


def test_sampled_value_that_matches_is_trusted_in_strict_mode():
    """抽样值**命中**＝可信证据：搜「肺」照常命中 E-ANND-1（标志案例不得回退）。"""
    r = _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED)
    assert constraint_satisfied(r, "tissue", ["lung"], lenient=False) is True


def test_sampled_value_that_misses_is_excluded_strictly_but_lenient_recovers_it():
    """抽样值**不命中**不构成否证：strict 仍排除（保 0% 违规），但 lenient 必须能捞回。"""
    r = _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED)
    assert constraint_satisfied(r, "tissue", ["alveolus"], lenient=False) is False
    assert constraint_satisfied(r, "tissue", ["alveolus"], lenient=True) is True, (
        "0.1% 抽样没看到肺泡 ≠ 该图谱没有肺泡 → lenient 必须放行")


def test_complete_record_with_different_value_stays_excluded_even_under_lenient():
    """「精确纳入」不得退化成「整维放宽」：值集完整且已知不同 → lenient 也不放行。"""
    r = _rec("ebi:E-CURD-10", "kidney", prov=FULL)
    assert constraint_satisfied(r, "tissue", ["lung"], lenient=True) is False


# ---------------------------------------------------------- coverage_caveats


def test_caveat_reports_sampled_records_whose_sampled_values_miss_the_query():
    """端到端：肺图谱的抽样取值里**没有**被查的那个组织 → 必须仍被 caveat 报成「无法核验」。

    ## 这条测试自己的历史（比它测的东西更值得记）

    上一版用查询「人类肺泡的单细胞数据」，作者（我）以为它会解析成 `tissue=alveolus`。
    **实际上词表把「肺泡」映射成 `tissue=['lung']`。** 于是 `E-ANND-1` 的 "lower lobe of left lung"
    **命中**了该查询、本就在结果列表里，而这条测试却断言它必须出现在「另有 N 条无法核验」里 ——
    **它把双算 bug 断言成了正确行为**。当时的 skip 守卫只检查「词表有没有收录」，没检查「解析出来
    的约束是不是真的 alveolus」，所以前提是假的、断言照样绿。

    教训：**端到端测试必须断言自己的前提成立**，否则它测的是另一件事，而你不会知道。
    现在改用一个抽样取值里确实没有的组织（胰腺），并显式断言前提（该记录确实不命中）。
    """
    intent = parse_query("人类胰腺的单细胞数据", get_settings().keyword_mapping)
    if intent.abstain or not intent.constraints.get("tissue"):
        pytest.skip("词表未收录胰腺 → 该查询不可执行，本断言不适用")

    sampled_atlas = _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED)
    known_kidney = _rec("ebi:E-CURD-10", "kidney", prov=FULL)
    records = [sampled_atlas, known_kidney]

    # 前提断言：两条都不命中该查询（否则本测试测的不是它自称要测的东西）
    assert passes_hard_filter(sampled_atlas, intent) is False, "前提：抽样图谱不命中「胰腺」"
    assert passes_hard_filter(known_kidney, intent) is False, "前提：整读的肾也不命中「胰腺」"

    caveats = DatasetRetriever().coverage_caveats(records, intent)
    tissue_caveat = [c for c in caveats if c["dim"] == "tissue"]
    assert tissue_caveat, "抽样记录必须进 caveat（否则用户收不到任何信号）"
    assert tissue_caveat[0]["count"] == 1, (
        "只报抽样那条（0.1% 取样没看到胰腺 ≠ 该图谱没有胰腺）；"
        "整读确认是肾的那条是**真负**、不该计入"
    )


# ------------------------------------ 第三态的「caveat == lenient 新增」不变量（回归网）
#
# 全盘审计指出的**测试盲区**（比它抓到的 bug 更值钱）：钉这条不变量的既有测试
# 全部**结构上跑不到第三态** ——
#   - test_honest_degradation.py::test_caveat_count_equals_lenient_added_on_base 用冻结 base，
#     而 base 记录**没有 metadata_provenance** → `_dim_value_set_complete` 恒 True →
#     `_dim_field_present` 退化成「非空」→ 「有值但值集不全」这个分支根本不存在；
#   - 同文件的 facet 版不变量用 `_mk()` 合成记录，同样不带 provenance；
#   - 本文件上面那条是唯一同时带 SAMPLED provenance 和 coverage_caveats 的测试，但它用「肺泡」
#     ——抽样值**不命中**的那一支，恰好绕开双算；且它只断言 count==1，从不断言 count == lenient 新增。
# 于是：有 bug 的版本和修好的版本**都能通过全部用例**。集成 over-count 14~18 条，测试全绿。


def _lenient_added(records, intent, dim):
    strict = [r for r in records if passes_hard_filter(r, intent)]
    lenient = [r for r in records if passes_hard_filter(r, replace(intent, lenient_dims={dim}))]
    return len(lenient) - len(strict)


def test_sampled_record_whose_value_matches_is_not_double_counted():
    """**核心**：抽样值已命中、记录已经在结果列表里 → 不得再被算进「另有 N 条无法核验」。

    集成症状：勾 EBI 搜「人类肺组织」→ 列出 17 条，横幅却说「另有 92 条…未标注组织」，点「也纳入」
    实增 78。差额 14 条全在上方列表里，且 tissue 明明写着 "lower lobe of left lung" ——
    横幅在对一条 Top1 可见、组织标得清清楚楚的记录说「未标注组织」。
    （被双算的正是 E-ANND-1：真实回归里那条「标志案例 Top5 第 1」。）
    """
    hit = _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED)   # 抽样值命中 lung
    miss = _rec("ebi:E-OTHER-1", "pancreas", prov=SAMPLED)                          # 抽样值不命中
    blank = _rec("ebi:E-BLANK-1", "", prov=SAMPLED)                                 # 真·未标注
    records = [hit, miss, blank]
    intent = parse_query("肺组织", get_settings().keyword_mapping)
    if intent.abstain or intent.constraints.get("tissue") != ["lung"]:
        pytest.skip("词表把「肺组织」解析成了别的约束 → 本断言的前提不成立")

    assert passes_hard_filter(hit, intent) is True, "前置：抽样值命中的记录本就在结果里"
    caveat = next(c for c in DatasetRetriever().coverage_caveats(records, intent) if c["dim"] == "tissue")
    assert caveat["count"] == 2, (
        f"只该数 miss + blank 两条（都不在结果里）；实得 {caveat['count']} —— "
        "已命中的 hit 被重复计进「另有 N 条」"
    )


def test_caveat_count_equals_lenient_added_with_sampled_provenance():
    """把 `caveat 计数 == lenient 新增数` 这条不变量**在带 SAMPLED provenance 的记录集上**再跑一遍。

    这是唯一能让第三态进入回归网的改法——既有的同名不变量测试跑在无 provenance 的语料上，
    对本文件钉的整套语义**一个字都没测到**。
    """
    records = [
        _rec("ebi:E-ANND-1", "lower lobe of left lung, trachea", prov=SAMPLED),  # 命中 → 在结果里
        _rec("ebi:E-S2", "pancreas", prov=SAMPLED),                              # 抽样不命中 → 可 lenient 捞回
        _rec("ebi:E-S3", "spleen", prov=SAMPLED),                                # 抽样不命中 → 可 lenient 捞回
        _rec("ebi:E-B1", "", prov=SAMPLED),                                      # 未标注 → 可捞回
        _rec("ebi:E-F1", "kidney", prov=FULL),                                   # 整读已知不同 → 真负、捞不回
    ]
    intent = parse_query("肺组织", get_settings().keyword_mapping)
    if intent.abstain or intent.constraints.get("tissue") != ["lung"]:
        pytest.skip("词表把「肺组织」解析成了别的约束 → 本断言的前提不成立")

    caveat = next(c for c in DatasetRetriever().coverage_caveats(records, intent) if c["dim"] == "tissue")
    added = _lenient_added(records, intent, "tissue")
    assert caveat["count"] == added, (
        f"诚实层数字必须与现实一致：caveat 报 {caveat['count']}、点「也纳入」实增 {added}"
    )
    assert added == 3, "E-S2 / E-S3 / E-B1 三条应被捞回；整读已知是肾的 E-F1 是真负、不该捞回"


# ------------------------------------ 排序加成按可信度打折
#
# `_rank_score` 的完整性 tie-breaker `+0.15 * filled` 曾把 SCEA 抽样得来的 tissue/disease 也计入
# 「字段填得全」。实测（改前）：SCEA `complete=False` 198/384，其中 190 条 tissue/disease 有值 →
# 每条多拿 +0.15~0.30，「抽样碰巧看到」压过「源库真没标注」。修复：filled 计数时
# `_dim_value_set_complete(record, dim) is False` 的维不计。base/cellxgene/hca/ae 无 provenance
# → 恒完整 → 计数逐位不变（冻结 767 结构性不受影响）。


def test_rank_score_discounts_sampled_dims():
    """抽样维不计入完整性 tie-breaker：complete=False 且 tissue/disease 有值 → 每维少得 +0.15。"""
    ret = DatasetRetriever()
    intent = parse_query("肺组织", get_settings().keyword_mapping)
    sampled = _rec("ebi:E-ANND-1", "lower lobe of left lung", prov=SAMPLED, disease="normal")
    full = _rec("ebi:E-ANND-1", "lower lobe of left lung", prov=FULL, disease="normal")
    noprov = _rec("ebi:E-ANND-1", "lower lobe of left lung", disease="normal")
    s_sampled = ret._rank_score(sampled, intent)
    s_full = ret._rank_score(full, intent)
    s_noprov = ret._rank_score(noprov, intent)
    assert s_full - s_sampled == pytest.approx(0.30), (
        "tissue + disease 两个抽样维 → 恰好少 2×0.15")
    assert s_noprov == s_full, "无 provenance 视作完整 → 与整读同分（冻结 767 逐位不变的根据）"


def test_rank_score_discount_only_applies_to_sampled_dim_not_species_or_chemistry():
    """打折只作用于抽样可及维（tissue/disease）；species/chemistry 不受 provenance 影响。"""
    ret = DatasetRetriever()
    intent = parse_query("肺组织", get_settings().keyword_mapping)
    # tissue 空、disease 空、仅 species+chemistry 有值 → 抽不抽样同分
    sampled_blank = _rec("ebi:E-B1", "", prov=SAMPLED)
    full_blank = _rec("ebi:E-B1", "", prov=FULL)
    assert ret._rank_score(sampled_blank, intent) == ret._rank_score(full_blank, intent)


def test_rank_score_local_missing_rule_preserved():
    """完整性 tie-breaker 刻意保留局部二元判空（只认 ""/"unknown"，不收敛到 is_missing_value）——
    冻结评测热路径行为冻结；本测试钉住 disease="unknown" 不计入 filled。"""
    ret = DatasetRetriever()
    intent = parse_query("肺组织", get_settings().keyword_mapping)
    a = ret._rank_score(_rec("x", "lung", disease="unknown"), intent)
    b = ret._rank_score(_rec("x", "lung", disease=""), intent)
    assert a == b
