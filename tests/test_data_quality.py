# -*- coding: utf-8 -*-
"""数据内容一致性检查（试用反馈驱动）：字段 vs 描述文本不一致检测。

关键：字段常多值（species='Mouse, Human'、tissue='Brain, Lung'）——描述提到字段里**已有**的另一值
不得误报冲突（否则混样/多组织数据集会刷屏假阳）。内联 caveat 只查物种（高精度，正是 验证 报告场景），
组织留给全库审计做人工 triage。record_caveats/field_description_conflict 均只读、不改任何数据。"""
from types import SimpleNamespace

from dataset_recommender.domains.biodata import vocabulary
from dataset_recommender.corpus.data_quality import field_description_conflict, record_caveats

CAT = vocabulary.CATALOG


def _rec(species="", tissue="", description=""):
    return SimpleNamespace(species=species, tissue=tissue, description=description)


def test_species_field_description_conflict_detected():
    """标注 Mouse、描述文本出现 Human → 物种冲突（验证 报告的 Mouse/Human-Kidney 场景）。"""
    r = _rec(species="Mouse", tissue="Kidney", description="Single-nucleus RNA-seq of human kidney cortex.")
    c = field_description_conflict(r, "species", CAT)
    assert c is not None
    assert c["own"] == "Mouse" and "Human" in c["description_mentions"]


def test_consistent_species_no_conflict():
    r = _rec(species="Mouse", tissue="Brain", description="Mouse brain single-cell atlas.")
    assert field_description_conflict(r, "species", CAT) is None


def test_multivalue_field_no_false_positive():
    """字段多值『Mouse, Human』+ 描述提到 Mouse（字段里已有）→ 不算冲突（混样数据集）。"""
    r = _rec(species="Mouse, Human", description="1:1 mixture of human HEK293T and mouse NIH3T3 cells.")
    assert field_description_conflict(r, "species", CAT) is None


def test_empty_field_yields_no_judgement():
    assert field_description_conflict(_rec(species="", description="human lung"), "species", CAT) is None
    assert field_description_conflict(_rec(species="Mouse", description=""), "species", CAT) is None


def test_ascii_alias_word_boundary_no_substring_false_hit():
    """『rat』词边界匹配：不得命中 generation/strategy 里的子串（避免噪声假阳）。"""
    r = _rec(species="Human", description="a generation strategy for integration of datasets")
    assert field_description_conflict(r, "species", CAT) is None


def test_record_caveats_species_only_inline():
    """内联 caveat 只报**物种**冲突；组织层级噪声（Brain vs Hippocampus）不进内联。"""
    sp = _rec(species="Mouse", tissue="Kidney", description="human kidney snRNA-seq")
    cav = record_caveats(sp, CAT)
    assert cav and any("物种" in c for c in cav)
    # 纯组织不一致（描述提到别的组织）不进内联 caveat
    ti = _rec(species="Mouse", tissue="Brain", description="mouse hippocampus and cortex")
    assert record_caveats(ti, CAT) == []


def test_record_caveats_clean_record_empty():
    assert record_caveats(_rec(species="Human", tissue="Lung", description="human lung scRNA-seq"), CAT) == []
