# -*- coding: utf-8 -*-
"""非*/non-* 生物学复合术语误否定修复专项。

根因：`非`（NEGATION_GUARDS_CN 单字兜底）与 `non`（`non-`/`non ` 分隔时命中 _EN_NEG_RE）是否定 guard；
未被正向 alias 先消费的正向复合词（非人灵长类 / 非霍奇金淋巴瘤 / 非编码RNA）此前被误报 unsupported_negation。

修复两路（均结构性隔离冻结门；eval_queries.json 无任何 非/non 查询，且改动对不含豁免复合词的查询是 no-op）：
- 可映射维度 → CATALOG alias 正向消费（非人灵长类→species 分组、非霍奇金淋巴瘤→lymphoma）。
- 无维度可映射 → NEGATION_EXEMPT_COMPOUNDS 屏蔽词首否定形素 → 落 unresolved_term 诚实弃权（非误报的 unsupported_negation）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import passes_hard_filter  # noqa: E402
from dataset_recommender.corpus.corpus import load_normalized_corpus  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402

_S = get_settings()
_RECS = load_normalized_corpus(_S.data_dir, _S.project_root)

_NHP_SUBSTR = ("macaque", "rhesus", "mulatta", "marmoset", "callithrix",
               "microcebus", "chimpanzee", "troglodytes", "cynomolgus", "fascicularis", "primate")


def _p(q):
    return parse_query(q)


# ---------- Part B：非人灵长类（NHP）→ species 分组，可执行返回结果 ----------
def test_nhp_chinese_executable_species():
    i = _p("非人灵长类的单细胞数据")
    assert i.parse_status == "executable" and not i.abstain
    assert "macaque" in i.constraints.get("species", [])
    assert i.constraints.get("modality") == ["single-cell"]
    assert not i.excluded_constraints                      # 是正向约束、不是排除


def test_nhp_english_and_abbrev_executable():
    for q in ("non-human primate single cell", "nonhuman primate scRNA", "NHP 单细胞"):
        i = _p(q)
        assert i.parse_status == "executable", q
        assert "macaque" in i.constraints.get("species", []), q


def test_nhp_survivors_are_primate_and_nonempty():
    i = _p("非人灵长类的单细胞数据")
    surv = [r for r in _RECS if passes_hard_filter(r, i)]
    assert surv                                            # base 有 macaque 单细胞
    for r in surv:
        assert any(t in (r.species or "").lower() for t in _NHP_SUBSTR)
        assert "homo sapiens" not in (r.species or "").lower()   # 「非人」：绝不含人类


# ---------- Part B：非霍奇金淋巴瘤 → lymphoma（此前 test_negation_contract:110 钉住 unsupported_negation）----------
def test_non_hodgkin_maps_to_lymphoma():
    i = _p("非霍奇金淋巴瘤的人类数据")
    assert i.parse_status == "executable" and not i.abstain
    assert i.constraints.get("disease") == ["lymphoma"]
    assert i.constraints.get("species") == ["human"]
    assert [r for r in _RECS if passes_hard_filter(r, i)]        # 有人类淋巴瘤记录


def test_non_hodgkin_english_forms():
    for q in ("non-hodgkin lymphoma", "non-hodgkin lymphoma single cell"):
        i = _p(q)
        assert i.parse_status == "executable", q
        assert i.constraints.get("disease") == ["lymphoma"], q


def test_bare_nhl_abbrev_not_recognized():
    # 有意不收 3 字裸缩写 "nhl"（会子串误命中 NHLBI 机构名）；全称/中文形已覆盖。
    i = _p("nhl")
    assert "lymphoma" not in i.constraints.get("disease", [])
    # 反面守卫：NHLBI 不再被静默注入 disease=lymphoma
    assert "lymphoma" not in _p("nhlbi 肺纤维化数据").constraints.get("disease", [])


# ---------- Part A：非编码RNA 无维度可映射 → unresolved_term（诚实），不再是 unsupported_negation ----------
def test_noncoding_rna_downgrades_to_unresolved_not_negation():
    for q in ("非编码RNA的单细胞数据", "non-coding RNA single cell", "非编码基因组数据"):
        i = _p(q)
        assert i.abstain, q
        assert i.abstain_reason == "unresolved_term", f"{q}: got {i.abstain_reason}"


# ---------- 回归：已注册复合词（既有 alias 机制）不受影响 ----------
def test_registered_compounds_unchanged():
    nsclc = _p("非小细胞肺癌的单细胞")
    assert nsclc.parse_status == "executable"
    assert "non-small cell lung" in nsclc.constraints.get("disease", [])
    xen = _p("非洲爪蟾的数据")
    assert xen.parse_status == "executable" and "xenopus" in xen.constraints.get("species", [])


# ---------- 窄豁免：未收录的 非X（非白名单、非 alias）仍 fail-closed 弃权（证明没有整体停用「非」guard）----------
def test_whitelist_is_narrow_unknown_fei_still_abstains():
    for q in ("非哺乳类的数据", "非典型某某的数据"):
        i = _p(q)
        assert i.abstain, f"{q} 应仍弃权（白名单是窄的，不整体放行「非」）"


# ---------- 安全：真负向绝不因豁免被静默反向 ----------
def test_real_negation_excludes_nhp_not_includes():
    i = _p("不要非人灵长类的数据")
    assert i.parse_status == "executable"
    assert "macaque" in i.excluded_constraints.get("species", [])   # 排除 NHP
    assert not i.constraints.get("species")                          # 不是正向包含
    surv = [r for r in _RECS if passes_hard_filter(r, i)]
    for r in surv:
        assert not any(t in (r.species or "").lower() for t in _NHP_SUBSTR)


def test_real_negation_baseline_still_holds():
    # 与 test_negation_contract 呼应：豁免机制不放宽任何既有真负向语义
    assert _p("不排除小鼠的数据").abstain_reason == "nested_negation"
    # 「不要小鼠或大鼠」不再弃权（¬(A∨B)=¬A∧¬B，排除侧本来就是这个语义）。
    # 这里改成钉「照做且精确」——豁免机制同样不许把它退回弃权。
    _or = _p("不要小鼠或大鼠")
    assert not _or.abstain, _or.abstain_reason
    assert {"mouse", "rat"} <= set(_or.excluded_constraints.get("species") or [])
    m = _p("不要小鼠的人类数据")
    assert m.excluded_constraints.get("species") == ["mouse"] and m.constraints.get("species") == ["human"]
    # 「不要 + 无维度可映射词」：显式排除但定位不到 → 仍 unsupported_negation（区别于无排除词的 unresolved_term）
    assert _p("不要非编码RNA").abstain_reason == "unsupported_negation"


# ---------- 排除/剔除/去除 + 非X：不再被『除非』子串误判为 conditional_negation ----------
def test_exclude_verb_plus_fei_entity_executes_not_conditional():
    # 排除[非人灵长类] = 排除 NHP（此前 排除+非 拼出『除非』→ 误报 conditional_negation 过度弃权）
    i = _p("排除非人灵长类")
    assert i.parse_status == "executable", i.abstain_reason
    assert "macaque" in i.excluded_constraints.get("species", [])
    assert not i.constraints.get("species")
    # 排除[非霍奇金淋巴瘤] = 排除 lymphoma
    j = _p("排除非霍奇金淋巴瘤")
    assert j.parse_status == "executable", j.abstain_reason
    assert j.excluded_constraints.get("disease") == ["lymphoma"]
    # 剔除/去除 同族动词一致
    for q in ("剔除非人灵长类", "去除非人灵长类"):
        k = _p(q)
        assert k.parse_status == "executable", f"{q}: {k.abstain_reason}"
        assert "macaque" in k.excluded_constraints.get("species", []), q


def test_genuine_chufei_conditional_still_abstains():
    # 真正的条件连词『除非』仍须整句弃权（负向 lookbehind 不误伤句首/名词后的真『除非』）
    i = _p("除非有小鼠否则不要人类数据")
    assert i.abstain and i.abstain_reason == "conditional_negation"
