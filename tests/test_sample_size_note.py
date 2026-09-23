"""样本量语义提醒（确定性、单一真源）：细胞数≠生物学重复 / 文件数≠样本数 /
单位不明不横向比较 / 仅元数据不判统计功效。三处（卡片介绍 / /api/introduction / MCP）
同源 —— 都走 `introduction.build_dataset_introduction` → `units.sample_size_note`。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.retrieval.units import sample_size_note  # noqa: E402
from dataset_recommender.content.introduction import build_dataset_introduction  # noqa: E402


def _joined(count: str, unit: str, n_files: int = 0) -> str:
    return " || ".join(sample_size_note(count, unit, n_files))


def test_cell_count_unit_flags_replicate_confusion():
    notes = sample_size_note("10000", "cells")
    assert any("生物学重复数" in n for n in notes)
    # 且不误报「单位不明」（cells 是已知单位）
    assert not any("未标注单位" in n for n in notes)


def test_spots_and_nuclei_also_flagged():
    assert any("生物学重复数" in n for n in sample_size_note("500", "spots"))
    assert any("生物学重复数" in n for n in sample_size_note("500", "nuclei"))


def test_count_without_unit_warns_no_cross_comparison():
    notes = sample_size_note("12345", "")
    assert any("未标注单位" in n and "横向比较" in n for n in notes)
    # 无单位时不谎称是细胞数
    assert not any("生物学重复数" in n for n in notes)


def test_biological_sample_unit_not_flagged_as_cell_count():
    """unit=samples 已经是生物样本，不应触发「细胞数≠生物学重复」。"""
    notes = sample_size_note("12", "samples")
    assert not any("生物学重复数" in n for n in notes)
    assert not any("未标注单位" in n for n in notes)
    # 但仍提醒统计功效
    assert any("统计功效" in n for n in notes)


def test_multiple_files_flag_file_vs_sample():
    assert any("文件数量" in n for n in sample_size_note("", "", n_files=5))
    # 单文件不提醒
    assert not any("文件数量" in n for n in sample_size_note("100", "cells", n_files=1))


def test_statistical_power_always_when_any_size_shown():
    assert any("统计功效" in n for n in sample_size_note("100", "cells"))
    assert any("统计功效" in n for n in sample_size_note("", "spots"))
    assert any("统计功效" in n for n in sample_size_note("", "", n_files=3))


def test_empty_when_nothing_to_say():
    assert sample_size_note("", "") == []
    assert sample_size_note("", "", 0) == []


def test_order_is_stable():
    # 顺序：细胞数 → 单位不明 → 文件数 → 统计功效（互斥项不会同现）
    notes = sample_size_note("999", "cells", n_files=4)
    assert notes[0].startswith("样本量是测序捕获")
    assert notes[-1].startswith("仅凭元数据")


def test_introduction_surfaces_sample_size_caveats():
    """三处同源的证据：共享 builder 返回该字段。"""
    intro = build_dataset_introduction(
        {"dataset_name": "D", "source": "cellxgene", "species": "Human",
         "count": "26000", "unit": "cells", "n_files": 3}
    )
    assert isinstance(intro["sample_size_caveats"], list)
    assert any("生物学重复数" in n for n in intro["sample_size_caveats"])
    assert any("文件数量" in n for n in intro["sample_size_caveats"])


def test_introduction_no_caveats_when_no_size():
    intro = build_dataset_introduction(
        {"dataset_name": "D", "source": "base", "species": "Human"}
    )
    assert intro["sample_size_caveats"] == []


def test_introduction_reads_structured_sample_size_dict():
    """workflow 序列化后 sample_size 是 {count,unit,display}——builder 要能取到 count/unit。"""
    intro = build_dataset_introduction(
        {"dataset_name": "D", "source": "hca",
         "sample_size": {"count": "8000", "unit": "cells", "display": "8000 cells"}}
    )
    assert any("生物学重复数" in n for n in intro["sample_size_caveats"])
