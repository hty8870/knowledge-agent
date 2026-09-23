# -*- coding: utf-8 -*-
"""modality 维（单细胞语义受控重基线）：单细胞=解离式模态，不再等同 platform=chromium。
设计经 3 视角验证定稿；base 单细胞⟺chromium → 冻结门逐位不变，多源修召回（已授权）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.domains.biodata.vocabulary import derive_modality  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import load_normalized_corpus, load_full_corpus
from dataset_recommender.retrieval.retriever import passes_hard_filter


# ---------- derive_modality 单元（单一真源）----------
def test_derive_modality_base_is_platform_family_only():
    """base 记录全有 platform_family：chromium→single-cell；visium/xenium/atera→spatial（确定、冻结门根据）。"""
    assert derive_modality("chromium", "anything") == "single-cell"
    assert derive_modality("visium", "") == "spatial"
    assert derive_modality("xenium", "") == "spatial"
    assert derive_modality("atera", "") == "spatial"   # Atera 是 10x 原位平台（Xenium 同族），非解离式单细胞


def test_derive_modality_external_positive_before_bulk():
    """外部（platform_family 空）扫 chemistry：单细胞正信号先于 bulk 排除——
    'rna-seq of coding rna from single cells' 必须判 single-cell，而 'rna-seq of coding rna' 判 bulk(排除)。"""
    assert derive_modality("", "rna-seq of coding rna from single cells") == "single-cell"
    assert derive_modality("", "rna-seq of coding rna") == ""        # bulk → 排除
    assert derive_modality("", "smart-seq2") == "single-cell"
    assert derive_modality("", "sci-rna-seq3") == "single-cell"
    assert derive_modality("", "drop-seq") == "single-cell"
    assert derive_modality("", "single nucleus rna sequencing") == "single-cell"
    assert derive_modality("", "slide-seqv2") == "spatial"
    assert derive_modality("", "merfish") == "spatial"


def test_derive_modality_bulk_and_unknown_fail_closed():
    """bulk/array/ChIP/methylation → 排除；无信号 → fail-closed（不臆断单细胞，与 has_raw_data==None 一致）。"""
    for bulk in ("rna-seq of non coding rna", "transcription profiling by array",
                 "chip-seq", "dna-seq", "methylation profiling by high throughput sequencing"):
        assert derive_modality("", bulk) == "", bulk
    assert derive_modality("", "") == ""
    assert derive_modality("", "other") == ""
    assert derive_modality("", "baseline") == ""


# ---------- 解析：单细胞/scRNA-seq → modality（不再 platform）----------
def test_single_cell_query_maps_to_modality_not_platform():
    i = parse_query("人类肺癌的单细胞数据", None)
    assert i.constraints.get("modality") == ["single-cell"]
    assert "chromium" not in i.constraints.get("platform", [])   # 不再硬映射平台


def test_scrna_seq_recognized_not_abstained():
    """验证反馈：scRNA-seq 此前 unresolved_term 直接弃权 → 现识别为 modality:single-cell。"""
    for q in ("人类肺癌 scRNA-seq 数据", "human lung cancer scRNA-seq data", "单核测序 人类数据"):
        i = parse_query(q, None)
        assert i.parse_status == "executable", q
        assert i.constraints.get("modality") == ["single-cell"], q


def test_chromium_platform_query_still_platform():
    """显式按平台查『chromium』仍走 platform 维（保留平台维查询能力）。"""
    i = parse_query("chromium 平台数据", None)
    assert i.constraints.get("platform") == ["chromium"]
    assert "modality" not in i.constraints


# ---------- base 分类分布（冻结门根据）----------
def test_base_single_cell_equals_chromium_count():
    s = get_settings()
    base = load_normalized_corpus(s.data_dir, s.project_root)
    sc = [r for r in base if r.modality == "single-cell"]
    chromium = [r for r in base if (r.platform_family or "").lower() == "chromium"]
    # 受控重基线 774→784：晋升 10 条里 1 条 flexv2（chromium→single-cell），582→583
    assert len(sc) == len(chromium) == 583
    # base 无未分类（modality=""）记录 → 派生在 base 上确定
    assert all(r.modality in ("single-cell", "spatial") for r in base)


# ---------- 多源召回修复 ----------
def test_multisource_single_cell_recall_and_precision():
    """人类肺癌单细胞（全源）应召回外部非 10x 单细胞（>旧 chromium 硬映射 23），且结果全 single-cell（排除 spatial/bulk）。"""
    s = get_settings()
    full = load_full_corpus(s.data_dir, s.project_root)
    i = parse_query("人类肺癌的单细胞数据", None)
    hits = [r for r in full if passes_hard_filter(r, i)]
    assert len(hits) >= 24, "多源单细胞召回应超过旧 chromium 硬映射的 23"
    assert all(r.modality == "single-cell" for r in hits), "结果不得含 spatial/bulk 记录"
    # 召回应含非 10x 外部源（修复点）
    sources = {str((r.raw or {}).get("source", "")) for r in hits}
    assert any("10x" not in src for src in sources if src), "应召回到非 10x 外部单细胞"
