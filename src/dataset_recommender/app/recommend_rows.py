# -*- coding: utf-8 -*-
"""卡片行投影与 /api/recommend 响应载荷的真源。

三个函数原本（16 日上午）放在 `app/workflow.py`，当天下午被冻结路径隔离闸
（tests/test_provenance.py::test_frozen_path_never_reaches_manuscript_modules 与
tests/test_summary_genre.py 的传递 import 闭包钉）拦下：`rows_from_retrieved` 必须调
`content.introduction.build_dataset_introduction`（卡片行与 /api/recommend 逐位同形
的代价），而 introduction → summary_genre → provenance 是稿件产物链——workflow 是
冻结 767 评测路径的入口之一，它的闭包不许碰这条链（本文件头部注释与
workflow.py:`_raw_status_is_guess` 的既有注释同一口径）。

故把「需要 introduction 的卡片投影」整体收进本模块：webapp（/api/recommend 族）与
agent（search.rerun 采纳档）共用这一个真源，workflow 保持冻结闭包干净。依赖方向
单向：本模块 → content.introduction；workflow 不 import 本模块。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..corpus.corpus import raw_data_false_is_guess
from ..content.labels import raw_fastq_status
from ..retrieval.units import format_sample_size

if TYPE_CHECKING:  # 仅类型标注：运行时不在本模块载 workflow（保持依赖方向单向）
    from .workflow import WorkflowResult


def rows_from_retrieved(payload: "list[dict[str, object]]") -> "list[dict[str, object]]":
    """把检索器的结构化候选转成前端卡片字段（含真实 reason/score/matched_fields）。

     自 webapp._rows_from_retrieved 迁入（先落 workflow、再随冻结闭包隔离
    迁到本模块）：search.rerun 工具采纳改写后要产出与 /api/recommend 同形的卡片行，
    web 层与 agent 层共用这一个真源（webapp 保留同名别名）。"""
    from ..content.introduction import build_dataset_introduction

    rows: list[dict[str, object]] = []
    for item in payload:
        row: dict[str, object] = {
                "dataset_name": item.get("dataset_name", ""),
                "species": item.get("species", ""),
                "tissue": item.get("tissue", ""),
                "disease": item.get("disease", ""),
                "chemistry": item.get("chemistry", ""),
                "platform": item.get("platform_family", ""),
                "assay": item.get("assay", ""),
                "sample_size": format_sample_size(
                    str(item.get("count") or "").strip(), str(item.get("unit") or "").strip()),
                "gene_count": item.get("gene_count", ""),
                "raw_data_status": _raw_status_text(item),
                "published_date": item.get("published_date", ""),
                "source": item.get("source", "") or "10x Genomics",
                "url": item.get("url", ""),
                # 阶段二：真实文件下载直链（查不到回退页面 url），供前端「下载数据」按钮。
                "download_url": item.get("download_url", "") or item.get("url", ""),
                # 国内可达性启发（workflow 序列化已算好，透传给卡片；非实测速度）。
                "reachability": item.get("reachability"),
                # 「查看全部文件」入口：uid 供 /api/files 按需拉全部直链，n_files 决定是否显示入口。
                "dataset_uid": item.get("dataset_uid", ""),
                "n_files": item.get("n_files", 0),
                "reason": item.get("reason", ""),
                "matched_fields": item.get("matched_fields", []),
                "score": item.get("score"),
                "description": item.get("description", ""),
                "preservation_method": item.get("preservation_method", ""),
                "analysis_software": item.get("analysis_software", ""),
                "software_version": item.get("software_version", ""),
            }
        row["introduction"] = build_dataset_introduction(row)
        rows.append(row)
    return rows


def _raw_status_text(item: "dict[str, object]") -> str:
    """payload dict 版的原始数据状态文案（与 webapp 原 _raw_status_text 逐字同约）。"""
    return raw_fastq_status(
        item.get("has_raw_data"), guessed_false=raw_data_false_is_guess(item))


def recommend_payload(meta: "WorkflowResult", *, provider: str = "") -> dict:
    """WorkflowResult → /api/recommend 同形响应 dict（search.rerun 采纳档的载荷真源）。

    键集与 webapp /api/recommend 响应的核心子集逐位对齐：前端「替换结果屏」只需这些键。
    warnings 口径同 /api/recommend：fallback_reason 非空即留痕一条。"""
    warnings: list[str] = []
    if meta.fallback_reason:
        warnings.append(meta.fallback_reason)
    return {
        "ok": True,
        "markdown": meta.answer,
        "pipeline": meta.pipeline,
        "llm_attempted": meta.llm_attempted,
        "llm_succeeded": meta.llm_succeeded,
        "llm_response_used": meta.llm_response_used,
        "provider": meta.llm_provider or provider,
        "llm_mode": meta.llm_mode,
        "fallback": meta.fallback,
        "fallback_reason": meta.fallback_reason,
        "results": rows_from_retrieved(meta.retrieved_data),
        "result_total": meta.result_total,
        "facets": meta.facets,
        "resolution_status": meta.resolution_status,
        "clarification": meta.clarification,
        "coverage_caveats": meta.coverage_caveats,
        "unused_query_terms": meta.unused_query_terms,
        "or_handling": meta.or_handling or {},
        "query_constraints": meta.active_filters,
        "interpretation": meta.interpretation,
        "search_trace": meta.search_trace,
        "warnings": warnings,
    }
