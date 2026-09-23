"""Deterministic dataset introductions shared by Web and MCP surfaces.

The builder only reformats fields already present on a candidate.  Missing values
stay explicit and source descriptions are reduced to plain text; no model call or
domain-knowledge inference is involved.
"""
from __future__ import annotations

import html
import re
from typing import Any

from ..retrieval.normalizer import MISSING_VALUE_TOKENS
from ..retrieval.units import format_sample_size, sample_size_note
from ..corpus.corpus import raw_data_false_is_guess
from .labels import INTRO_FACT_LABELS, raw_fastq_status


# 缺失哨兵的单一真源在 normalizer（见其 MISSING_VALUE_TOKENS 的注释：同一概念曾有三套定义，
# 其中一套让诚实降级层在冻结 base 的 disease 维上 100% 失效）。此处保留旧名以免大改本模块。
_MISSING_TEXT = MISSING_VALUE_TOKENS


def plain_metadata_text(value: object, limit: int = 1200) -> str:
    """Return display-safe plain text for a source metadata value."""
    text = html.unescape(str(value or ""))
    # 只剥"看起来像 HTML 标签"的片段：< 或 </ 后必须紧跟字母，标签体不跨越 < >。
    # 这样裸比较符（科研描述里的 FDR<0.01 and log2FC>1）不会被误当标签吞掉，长标签也不再漏网。
    text = re.sub(r"</?[a-zA-Z][^<>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def meaningful_metadata_text(value: object, limit: int = 1200) -> str:
    """Return metadata text, treating common missing-value sentinels as empty."""
    text = plain_metadata_text(value, limit=limit)
    return "" if text.lower() in _MISSING_TEXT else text


def _sample_size(item: dict[str, Any]) -> str:
    raw = item.get("sample_size")
    # workflow._serialize_retrieved_data 把 sample_size 序列化成结构化 dict
    # {count,unit,display,unit_explanation}（供前端结构化消费）；MCP 富化会把该原始候选直接喂进本
    # builder。这里取其已算好的 display，避免 str(dict) 泄成 Python 字典 repr——否则 MCP 每条候选的
    # 「样本量」都会变成 "{'count': ..., 'unit': ...}"，与 Web 口径分叉，破坏 T7「Web/MCP 同源」。
    if isinstance(raw, dict):
        shown = meaningful_metadata_text(raw.get("display"))
    else:
        shown = meaningful_metadata_text(raw)
    if not shown:
        shown = format_sample_size(
            str(item.get("count") or "").strip(),
            str(item.get("unit") or "").strip(),
        )
    return "未说明" if shown.startswith("未说明 ") else shown


def _count_unit(item: dict[str, Any]) -> tuple[str, str]:
    """取样本量的原始 count / unit（语义提醒用）。与 `_sample_size` 同源：
    workflow 序列化后 sample_size 是 {count,unit,display} dict；MCP/其它路径可能直接带 count/unit。"""
    raw = item.get("sample_size")
    if isinstance(raw, dict):
        count = str(raw.get("count") or "").strip()
        unit = str(raw.get("unit") or "").strip()
        if count or unit:
            return count, unit
    return str(item.get("count") or "").strip(), str(item.get("unit") or "").strip()


def _raw_status(item: dict[str, Any]) -> str:
    shown = meaningful_metadata_text(item.get("raw_data_status"))
    if shown:
        return shown
    return raw_fastq_status(
        item.get("has_raw_data"), guessed_false=raw_data_false_is_guess(item))


def build_dataset_introduction(item: dict[str, Any]) -> dict[str, Any]:
    """Build a structured, evidence-bounded introduction from candidate fields."""
    source = meaningful_metadata_text(item.get("source")) or "未说明"
    species = meaningful_metadata_text(item.get("species"))
    tissue = meaningful_metadata_text(item.get("tissue"))
    disease = meaningful_metadata_text(item.get("disease"))
    platform = meaningful_metadata_text(item.get("platform")) or meaningful_metadata_text(item.get("platform_family"))
    assay = meaningful_metadata_text(item.get("assay"))
    chemistry = meaningful_metadata_text(item.get("chemistry"))
    description = meaningful_metadata_text(item.get("description"))
    sample_size = _sample_size(item)
    published = meaningful_metadata_text(item.get("published_date"))
    raw_status = _raw_status(item)
    preservation = meaningful_metadata_text(item.get("preservation_method"))
    software = meaningful_metadata_text(item.get("analysis_software"))
    software_version = meaningful_metadata_text(item.get("software_version"))
    if software and software_version:
        software = f"{software} {software_version}"

    technology = " / ".join(v for v in (platform, assay, chemistry) if v)
    gene_count = meaningful_metadata_text(item.get("gene_count"))
    values = (source, species or "未说明", tissue or "未说明",
              disease or "未说明", technology or "未说明",
              sample_size or "未说明", published or "未说明", raw_status)
    facts = [{"label": label, "value": value}
             for label, value in zip(INTRO_FACT_LABELS, values)]
    # 检测基因数（10x 平台信息补充旁挂表）：使用者最关注的两个指标之一，紧随样本量。
    # 仅在有补充时追加——无补充的记录保持原样（不加「未说明」行，避免整页噪音）。
    if gene_count:
        facts.append({"label": "检测基因数", "value": gene_count})
    if preservation:
        facts.append({"label": "保存方式", "value": preservation})
    if software:
        facts.append({"label": "分析软件", "value": software})
    try:
        n_files = max(0, int(item.get("n_files") or 0))
    except (TypeError, ValueError):
        n_files = 0
    if n_files:
        facts.append({"label": "可用文件", "value": f"{n_files} 个"})

    # 10x 平台信息补充的次要指标（测序深度/转录本中位数/空间分辨率/供体数等，按 uid 旁挂 join）：
    # 原样转述手工整理表里的官方页面数值，排在内置字段之后，作为补充说明；无补充 → 一行不加。
    from ..corpus.sample_supplement import count_note as _supp_count_note, extra_facts as _supp_extra_facts

    _supp_uid = str(item.get("dataset_uid") or "").strip()
    for _fact in _supp_extra_facts(_supp_uid):
        facts.append({"label": str(_fact["label"]), "value": str(_fact["value"])})

    # 体裁路由：`if description:` 把五种**根本不是同一种东西**的字符串
    # 一律当摘要展示。实测：SCEA 384 条的「摘要」是 `因子: organism part, sampling site…`
    # （字段名清单），base 767 条是索引器自拼的模板，CELLxGENE 2192 条是**论文标题**。
    # 判定按来源（= 哪个 ingest 写的，可读事实），不按字符串形状猜——详见 summary_genre。
    from . import summary_genre   # 局部 import：introduction 是 summary_genre 的依赖，避免环

    verdict = summary_genre.classify(item)
    genre = verdict["genre"]
    residual = verdict["residual"]

    if genre in (summary_genre.GENRE_PROSE, summary_genre.GENRE_UNKNOWN) and residual:
        # UNKNOWN（用户上传等未知来源）走同一分支：我们不知道它的体裁，但它的描述是
        # 用户/来源自己写的真文本 —— 原样展示，别丢。只是不拿去做「忠实翻译」。
        summary = residual
        summary_source = "source_metadata"
        source_label = "来源元数据"
        caveat = "介绍来自数据源公开元数据；未提供的字段保持“未说明”，不会根据常识补写。"
    elif genre == summary_genre.GENRE_TITLE:
        # 关键诚实点：这是**关联论文的标题**，不是这个数据集的摘要。不说清楚，
        # 用户会把一句标题当成「这个数据集讲了什么」。
        summary = residual
        summary_source = "source_title"
        source_label = "关联论文标题"
        caveat = "该来源只提供了关联论文的标题（不是数据集摘要）；实验细节请到来源页面或该论文核实。"
    else:
        pieces: list[str] = []
        if species:
            pieces.append(f"物种为 {species}")
        if tissue:
            pieces.append(f"组织为 {tissue}")
        if disease:
            pieces.append(f"疾病信息为 {disease}")
        if technology:
            pieces.append(f"采用 {technology}")
        if sample_size and sample_size != "未说明":
            pieces.append(f"样本量为 {sample_size}")
        summary = (
            "该数据集的公开字段显示：" + "；".join(pieces) + "。"
            if pieces
            else "当前来源只提供了数据集名称和有限索引信息，请到来源页面核对完整实验背景。"
        )
        summary_source = "field_summary"
        source_label = "字段合成"
        if genre == summary_genre.GENRE_FACTOR_LIST:
            caveat = "该来源只提供了实验因子清单、没有文字摘要；以上介绍只组合已有字段，不推断缺失信息。"
        else:
            caveat = "原始来源未提供完整摘要；以上介绍只组合已有字段，不推断缺失信息。"

    if verdict["truncated"] and summary_source != "field_summary":
        # 1688 条来源文本被截断（其中 313 条是**中部**截断，endswith 抓不到）。
        # 不说，用户会把半句话当成完整摘要。
        caveat += "来源提供的文字在抓取时已被截断，不是完整原文。"

    _ss_count, _ss_unit = _count_unit(item)
    sample_size_caveats = sample_size_note(_ss_count, _ss_unit, n_files)
    # 补充表的样本量口径说明（如「样本量为 12 张切片的合计」）——与样本量提醒同列展示，先于定论。
    _supp_note = _supp_count_note(_supp_uid)
    if _supp_note and _ss_count:
        sample_size_caveats = [_supp_note + "。"] + sample_size_caveats

    return {
        "summary": summary,
        "summary_source": summary_source,
        "source_label": source_label,
        "genre": genre,
        "truncated": verdict["truncated"],
        "facts": facts,
        "caveat": caveat,
        # 样本量语义提醒（确定性、单一真源 units.sample_size_note）：
        # 细胞数≠生物学重复 / 文件数≠样本数 / 单位不明不横向比较 / 仅元数据不判统计功效。
        "sample_size_caveats": sample_size_caveats,
    }
