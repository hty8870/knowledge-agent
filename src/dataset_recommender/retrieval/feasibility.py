# -*- coding: utf-8 -*-
"""可行性概览（N12）：一个研究问题 → 有多少可复用数据、够不够、缺口在哪。确定性、离线、只读。

聚合**通过硬过滤的全部候选**（不是 top-k 排序结果），给出：候选数、总细胞量估计（**显式标注下限**）、
物种/平台/年份/来源分布、可下载率、缺口提示。

**诚实约束（N14 审计的直接结论）**：ArrayExpress 的 `count` 覆盖率 0%（源库不提供研究级细胞数，见
`docs/AE_INGEST_COVERAGE_AUDIT_2026-07-18.md`），且全库 31% 语料无细胞数。所以「总细胞量」**必然是下限**、
会系统性少算——报告必须**显式说明**有多少数据集（含全部 AE）没有细胞数，绝不把下限当成总量。
不被检索/排序/评测路径调用。
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .normalizer import is_missing_value
from .units import _CELL_COUNT_UNITS
from ..content.item_view import published_year


def _field(item: dict[str, Any], key: str) -> str:
    v = str(item.get(key) or "").strip()
    return "" if is_missing_value(v) else v


def _count_unit(item: dict[str, Any]) -> tuple[str, str]:
    raw = item.get("sample_size")
    if isinstance(raw, dict):
        c = str(raw.get("count") or "").strip()
        u = str(raw.get("unit") or "").strip()
        if c or u:
            return c, u
    return str(item.get("count") or "").strip(), str(item.get("unit") or "").strip()


def _as_int(text: str) -> int | None:
    t = "".join(ch for ch in str(text or "") if ch.isdigit())
    return int(t) if t else None


def build_report(survivors: list[dict[str, Any]], result_total: int, truncated: bool = False) -> dict[str, Any]:
    """通过硬过滤的候选（序列化 dict）→ 可行性概览。`result_total` 是未截断的命中总数；
    `truncated` 表示 survivors 是否只是命中集的一个前缀（top_k 截断）。"""
    n = len(survivors)
    total_cells = 0
    counted = 0
    no_count_sources: Counter = Counter()
    species_dist: Counter = Counter()
    platform_dist: Counter = Counter()
    year_dist: Counter = Counter()
    source_dist: Counter = Counter()
    downloadable = 0
    fastq = 0

    for s in survivors:
        src = _field(s, "source") or "未知来源"
        source_dist[src] += 1
        c, u = _count_unit(s)
        cell_n = _as_int(c) if u.strip().lower() in _CELL_COUNT_UNITS else None
        if cell_n is not None:
            total_cells += cell_n
            counted += 1
        else:
            no_count_sources[src] += 1
        for tok in [t.strip() for t in _field(s, "species").split(",") if t.strip()]:
            species_dist[tok] += 1
        plat = _field(s, "platform") or _field(s, "platform_family")
        if plat:
            platform_dist[plat] += 1
        yr = s.get("published_year")
        if yr is None:
            yr = published_year(_field(s, "published_date"))
        if yr:
            year_dist[str(yr)] += 1
        if _field(s, "download_url") or _field(s, "url"):
            downloadable += 1
        if s.get("has_raw_data") is True or "FASTQ" in _field(s, "raw_data_status"):
            fastq += 1

    no_count = n - counted
    gaps: list[str] = []
    if no_count:
        by = "、".join(f"{src}（{c}）" for src, c in no_count_sources.most_common(5))
        gaps.append(
            f"{no_count}/{n} 个数据集没有细胞数（{by}）——总细胞量估计是**下限**、会系统性少算。"
            "ArrayExpress 源库不提供研究级细胞数（这是来源本身不提供，不是漏抓）。"
        )
    if truncated:
        gaps.append(f"命中 {result_total} 条、本报告只统计了前 {n} 条；分布与总量是样本、非全量。")
    if n and downloadable < n:
        gaps.append(f"{n - downloadable}/{n} 个数据集没有公开访问链接，投稿前需到来源确认可获取性。")

    return {
        "candidate_count": result_total,
        "aggregated_count": n,
        "truncated": bool(truncated),
        "total_cells_lower_bound": total_cells,
        "datasets_with_cell_count": counted,
        "datasets_without_cell_count": no_count,
        "species": [{"value": k, "count": v} for k, v in species_dist.most_common(12)],
        "platforms": [{"value": k, "count": v} for k, v in platform_dist.most_common(12)],
        "years": [{"value": k, "count": v} for k, v in sorted(year_dist.items())],
        "sources": [{"value": k, "count": v} for k, v in source_dist.most_common()],
        "downloadable_count": downloadable,
        "fastq_count": fastq,
        "gaps": gaps,
        "caveat": (
            "本概览只统计**本次检索所选来源范围内**、通过硬过滤的候选；总细胞量是**下限**（多个来源无细胞数、"
            "尤其 ArrayExpress——这是来源本身不提供，不是漏抓）。仅凭元数据无法判断统计功效，够不够需结合原始数据与实验设计评估。"
        ),
    }
