# -*- coding: utf-8 -*-
"""corpus_enrich 反标富化的契约测试：词边界与 query_parser 同源互锁、只填缺失值、
provenance 诚实声明（complete=False 只在 tissue/disease 落笔时置位）。"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.retrieval import query_parser  # noqa: E402
from dataset_recommender.corpus.corpus_enrich import (  # noqa: E402
    _alias_occurrences,
    backfill_record,
    detect_terms,
)


def test_alias_occurrences_matches_query_parser_semantics() -> None:
    """两边实现必须逐行同源：抽样用例逐位对照 query_parser._alias_occurrences。"""
    cases = [
        ("integrated human lung atlas", "rat"),
        ("rats and mice", "rat"),
        ("human pbmc data", "pbmc"),
        ("海马体组织", "海马"),
        ("snuc-seq profile", "snuc-seq"),
        ("scratex", "rat"),
    ]
    for text, alias in cases:
        assert _alias_occurrences(text, alias) == query_parser._alias_occurrences(text, alias), (
            f"corpus_enrich._alias_occurrences 与 query_parser 分叉：{text!r} / {alias!r} —— "
            "两处实现必须双同步（见 corpus_enrich.py 模块 docstring）。"
        )


def test_detect_terms_word_boundary_no_false_rat() -> None:
    found = detect_terms("integrated multiome atlas of lung")
    species_hits = [h["display"] for h in found.get("species", [])]
    assert "Rat" not in species_hits  # integrated 里的 rat 不算
    assert any(h["display"] == "Lung" for h in found.get("tissue", []))


def test_detect_terms_cjk_and_english() -> None:
    found = detect_terms("Single nucleus RNA-seq of mouse hippocampus（小鼠海马体）")
    species = [h["display"] for h in found.get("species", [])]
    tissue = [h["display"] for h in found.get("tissue", [])]
    assert "Mouse" in species
    assert any("hippocamp" in " ".join(h["targets"]) for h in found.get("tissue", [])), tissue


def test_backfill_fills_only_missing_and_marks_provenance() -> None:
    rec = {
        "dataset_name": "Single nucleus RNA-seq of mouse hippocampus",
        "description": "sNuc-Seq profile",
        "species": None,
        "tissue": None,
        "disease": None,
        "chemistry": None,
    }
    rep = backfill_record(rec)
    assert "Mouse" in (rec["species"] or "")
    assert "hippocamp" in (rec["tissue"] or "").lower()
    prov = rec["metadata_provenance"]
    assert prov["complete"] is False  # tissue 落笔 → 值集不穷尽声明
    assert prov["backfill"]["method"].startswith("offline description alias backfill")
    assert set(rep["filled"]) >= {"species", "tissue"}


def test_backfill_never_overwrites_present_values() -> None:
    rec = {
        "dataset_name": "mouse lung atlas",
        "description": "",
        "species": "Human",  # 已有真值：即便文本说 mouse 也不覆盖
        "tissue": "unknown",  # 缺失哨兵 → 可填
    }
    rep = backfill_record(rec)
    assert rec["species"] == "Human"
    assert "species" in rep["skipped_present"]
    assert "lung" in (rec["tissue"] or "").lower()


def test_backfill_no_hit_is_silent_noop() -> None:
    rec = {"dataset_name": "zzz qqq", "description": "", "species": None}
    rep = backfill_record(rec)
    assert rep == {"filled": {}, "skipped_present": []}
    assert "metadata_provenance" not in rec


def test_backfill_species_only_does_not_flip_complete() -> None:
    """只填 species（非 tissue/disease）时不得把 complete 置 False——complete 只服务 tissue/disease 两维。"""
    rec = {"dataset_name": "zebrafish something", "description": "", "species": None, "tissue": None}
    backfill_record(rec)
    prov = rec.get("metadata_provenance") or {}
    assert "Zebrafish" in (rec["species"] or "")
    assert prov.get("complete") is not False
    assert "backfill" in prov


def test_field_value_target_contained_fallback() -> None:
    """display 不含任何 target 子串时，字段值要兜上 target（否则硬过滤子串匹配永远打不中）。"""
    from dataset_recommender.corpus.corpus_enrich import _field_value

    assert _field_value("Mouse", ["mouse", "musculus"]) == "Mouse"
    assert _field_value("Non-human Primate", ["macaque", "primate"]) == "Non-human Primate"
    assert _field_value("Odd Name", ["xyzzy"]) == "Odd Name (xyzzy)"
