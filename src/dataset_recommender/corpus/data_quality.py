# -*- coding: utf-8 -*-
"""数据内容一致性检查（只读、纯函数）——试用反馈驱动。

背景：发现层（尤其外部来源）存在**字段与描述文本不一致**（如某标注 Mouse 的肾数据集，
描述文本却出现 Human Kidney）与 **n_files=0**（外部记录无文件级清单）。这属数据清洗/来源质量，
**不是** MCP 稳定性；且 `database/base/` 是冻结 767、红线禁改。故本模块只做**只读检测**：

  * `record_caveats(record, catalog)` —— 逐记录人类可读 caveat 列表（供 MCP 候选内联露出）；
    **只查物种**（高精度、正是 验证 报告场景），绝不改动任何数据。
  * `field_description_conflict(record, dim, catalog)` —— 结构化的单维不一致（供审计脚本按物种+组织汇总成表）。

匹配语义复用受控词表 `vocabulary.CATALOG`：ASCII 别名用词边界（避免 rat 命中 generation），
CJK 别名用子串；记录字段归属取**所有**命中条目（集合——字段常多值如 `Mouse, Human`，
描述提到字段里已有的另一值不得误报冲突）。caveat 是**建议性**信号（可能标注有误，也可能跨物种/
多组织研究），措辞不武断下结论。
"""
from __future__ import annotations

import re
from typing import Any

from ..retrieval.normalizer import DatasetRecord

# 审计（全库人工 triage）查两维：物种 + 组织。疾病/平台/技术别名噪声大，暂不纳入。
DIMS_CHECKED = ("species", "tissue")
# 内联 caveat（MCP 逐候选露出）只查**物种**：物种冲突（Mouse vs Human）判据干净、
# 正是 验证 报告的场景；组织有层级/多组织 panel 噪声（Brain vs Hippocampus、Embryo），
# 逐条结果露出会变成噪声，故只留给审计报告做人工 triage，不进内联 caveat。
_CAVEAT_DIMS = ("species",)
_DIM_CN = {"species": "物种", "tissue": "组织"}


def _ascii_alias_re(alias: str) -> re.Pattern[str]:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


def _alias_hit(text_lower: str, alias: str) -> bool:
    a = (alias or "").lower().strip()
    if not a:
        return False
    if a.isascii():
        return bool(_ascii_alias_re(a).search(text_lower))
    return a in text_lower


def _mentioned(text_lower: str, entry: dict[str, Any]) -> bool:
    for a in list(entry.get("aliases", [])) + list(entry.get("targets", [])):
        if _alias_hit(text_lower, str(a)):
            return True
    return False


def _entries_for_field(field_text: str, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """记录某维字段命中的**所有**受控条目。字段常是多值（如 species='Mouse, Human'、
    tissue='Brain, Lung, Kidney'）——必须取**集合**，否则描述提到字段里已有的另一值会被误报冲突。"""
    fl = (field_text or "").lower()
    if not fl.strip():
        return []
    return [e for e in entries if _mentioned(fl, e)]


def field_description_conflict(
    record: DatasetRecord, dim: str, catalog: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """某维「标注字段 vs 描述文本」不一致 → 结构化结果；一致/无从判断 → None。
    冲突判据：描述文本提到了**字段里没有**的受控值（字段多值时逐个排除，避免混样/多组织误报）。"""
    entries = catalog.get(dim) or []
    own = _entries_for_field(getattr(record, dim, "") or "", entries)
    if not own:
        return None  # 字段本身无法归属受控条目 → 不臆测
    desc = (record.description or "").lower()
    if not desc.strip():
        return None
    own_ids = {id(e) for e in own}
    others = [e for e in entries if id(e) not in own_ids and _mentioned(desc, e)]
    if not others:
        return None
    return {
        "dim": dim,
        "field": getattr(record, dim, ""),
        "own": "、".join(e.get("display", "") for e in own),
        "description_mentions": [e.get("display", "") for e in others],
    }


def record_caveats(
    record: DatasetRecord,
    catalog: dict[str, list[dict[str, Any]]],
) -> list[str]:
    """逐记录的**内容一致性** caveat（字段 vs 描述文本），可能为空。建议性措辞，不武断断言错误。

    只查内容一致性，**不**含 n_files=0：后者对发现层外部记录是系统性现象（它们不进 10x 文件索引，
    但自带 download_url，仍可下载），逐条 candidate 报会变成噪声且误导。n_files=0 只在
    get_file_manifest（钻取单个数据集、可操作）与 evidence_scope 元信息处露出。"""
    caveats: list[str] = []
    for dim in _CAVEAT_DIMS:
        c = field_description_conflict(record, dim, catalog)
        if c:
            cn = _DIM_CN[dim]
            others = "、".join(m for m in c["description_mentions"] if m)
            caveats.append(
                f"描述文本疑似提到{cn}「{others}」，与标注{cn}「{c['own']}」不一致，"
                f"请核对（可能标注有误，或为跨{cn}/对照研究）。"
            )
    return caveats
