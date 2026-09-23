"""10x 平台信息补充（sample_supplement）单测：真实账本查询、只补缺不覆盖、优雅降级、字段流转。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.corpus import sample_supplement  # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_dataset_record  # noqa: E402
from dataset_recommender.content.introduction import build_dataset_introduction  # noqa: E402

# 账本里稳定存在的两条（生成器 验证）：
#  - base 缺失样本量的 Visium HD（卵巢，使用者截图同款）
#  - 多切片 Xenium（12 张切片合计）
_VISIUM_UID = "visium-hd-three-prime-ovarian-cancer-fresh-frozen"
_XENIUM_UID = "xenium-ffpe-human-breast-biomarkers"


def test_data_available():
    assert sample_supplement.is_available() is True


def test_lookup_by_uid_and_url():
    by_uid = sample_supplement.get(_VISIUM_UID)
    assert by_uid and by_uid.get("count") and by_uid.get("gene_count")
    by_url = sample_supplement.get(by_uid["url"])
    assert by_url is by_uid or by_url == by_uid


def test_missing_key_and_none_are_safe():
    assert sample_supplement.get("no-such-dataset-xyz") is None
    assert sample_supplement.get(None) is None
    assert sample_supplement.count_fill("no-such-dataset-xyz") == ""
    assert sample_supplement.gene_count(None) == ""
    assert sample_supplement.extra_facts("no-such-dataset-xyz") == []


def test_extra_facts_shape():
    facts = sample_supplement.extra_facts(_XENIUM_UID)
    assert facts and all(set(f) >= {"label", "value"} for f in facts)


def test_xenium_multi_section_count_note():
    assert sample_supplement.count_note(_XENIUM_UID).startswith("样本量为 12 张切片的合计")


def test_normalizer_fills_count_only_when_missing():
    # base 缺 total_records → 旁挂表回填；unit 保持 base 原值
    rec = normalize_dataset_record(
        {"dataset_uid": _VISIUM_UID, "dataset_name": "X", "unit": "spots", "total_records": None},
        "test.json",
    )
    assert rec.count == sample_supplement.count_fill(_VISIUM_UID)
    assert rec.count != ""
    assert rec.unit == "Spots"
    assert rec.gene_count == sample_supplement.gene_count(_VISIUM_UID)
    # base 已有值 → 绝不覆盖
    rec2 = normalize_dataset_record(
        {"dataset_uid": _VISIUM_UID, "dataset_name": "X", "unit": "spots", "total_records": 5756},
        "test.json",
    )
    assert rec2.count == "5756"


def test_unsupplemented_record_unchanged():
    rec = normalize_dataset_record(
        {"dataset_uid": "no-such-dataset-xyz", "dataset_name": "X", "unit": "cells"},
        "test.json",
    )
    assert rec.count == "" and rec.gene_count == ""


def test_supplement_beats_title_shorthand_guess():
    # 「Xenium Prime 5K」的 5K 是产品名：标题缩写兜底会误读成 5000 Cells，
    # 旁挂表的官方页数值必须压过这个猜测（但仍不覆盖 base 显式值）。
    uid = "xenium-comparison-fresh-frozen-human-ovarian-cancer"
    rec = normalize_dataset_record(
        {"dataset_uid": uid,
         "dataset_name": "Cross-Platform Comparison: FF Human Ovarian Cancer with Xenium Prime 5K",
         "unit": "cells", "total_records": None},
        "test.json",
    )
    assert rec.count == sample_supplement.count_fill(uid)
    assert rec.count != "5000"
    # 无补充的记录仍走标题兜底（Chromium 的 7.5k 类标题是真实规模，行为不变）
    rec2 = normalize_dataset_record(
        {"dataset_uid": "no-such-dataset-xyz", "dataset_name": "7.5k cells, pilot", "unit": "cells"},
        "test.json",
    )
    assert rec2.count == "7500"


def test_introduction_facts_include_gene_count_and_extras():
    item = {
        "dataset_uid": _VISIUM_UID,
        "dataset_name": "X",
        "source": "10x Genomics",
        "gene_count": sample_supplement.gene_count(_VISIUM_UID),
        "count": sample_supplement.count_fill(_VISIUM_UID),
        "unit": "Spots",
    }
    intro = build_dataset_introduction(item)
    labels = [f["label"] for f in intro["facts"]]
    assert "检测基因数" in labels
    assert labels.index("检测基因数") > labels.index("样本量")  # 紧随样本量之后
    # 无补充的数据集不加「检测基因数」行（避免整页噪音）
    intro2 = build_dataset_introduction({"dataset_name": "Y", "source": "10x Genomics"})
    assert "检测基因数" not in [f["label"] for f in intro2["facts"]]


def test_introduction_count_note_in_sample_size_caveats():
    item = {
        "dataset_uid": _XENIUM_UID,
        "dataset_name": "X",
        "source": "10x Genomics",
        "count": sample_supplement.count_fill(_XENIUM_UID),
        "unit": "Cells",
    }
    intro = build_dataset_introduction(item)
    assert any("12 张切片" in c for c in intro["sample_size_caveats"])


def test_missing_data_file_degrades_gracefully(monkeypatch):
    sample_supplement._load.cache_clear()
    monkeypatch.setattr(sample_supplement, "_DATA_PATH", str(Path("/nonexistent") / "xyz.json"))
    try:
        assert sample_supplement.is_available() is False
        assert sample_supplement.get(_VISIUM_UID) is None
        assert sample_supplement.count_fill(_VISIUM_UID) == ""
        assert sample_supplement.extra_facts(_VISIUM_UID) == []
        # 降级路径下归一化绝不崩：结果等同无补充
        rec = normalize_dataset_record(
            {"dataset_uid": _VISIUM_UID, "dataset_name": "X", "unit": "spots"},
            "test.json",
        )
        assert rec.count == "" and rec.gene_count == ""
    finally:
        sample_supplement._load.cache_clear()  # 恢复：后续测试重新加载真实账本


# ---- 环境变量运行时生效 ----

def test_env_override_takes_effect_at_runtime(tmp_path, monkeypatch):
    """BIODATA_SAMPLE_SUPPLEMENT 运行时变更必须生效——路径每次加载现读、含进缓存键，
    不再 import 期固化。"""
    import json as _json

    p = tmp_path / "alt_supplement.json"
    p.write_text(_json.dumps({
        "uid-y": {"url": "https://example.org/ds", "count": "12345"},
    }), encoding="utf-8")
    monkeypatch.setenv("BIODATA_SAMPLE_SUPPLEMENT", str(p))
    # 不清缓存：路径变了 → 缓存键自然不同 → 直接读到新文件
    assert sample_supplement.count_fill("uid-y") == "12345"


# ---- D6：读盘失败不再负向缓存 ----

def test_corrupt_file_not_cached_and_recovers(tmp_path, monkeypatch):
    """D6：坏 JSON → _load() 降级空表但不入缓存；同进程修复后无需 cache_clear 即恢复。"""
    import json as _json

    p = tmp_path / "supplement.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("BIODATA_SAMPLE_SUPPLEMENT", str(p))
    sample_supplement._load.cache_clear()
    try:
        assert sample_supplement._load() == ({}, {})   # 降级：不抛
        p.write_text(_json.dumps({"uid-z": {"url": "https://example.org/z", "count": "7"}}),
                     encoding="utf-8")
        assert sample_supplement.count_fill("uid-z") == "7"   # 同路径、不清缓存即恢复
    finally:
        sample_supplement._load.cache_clear()
