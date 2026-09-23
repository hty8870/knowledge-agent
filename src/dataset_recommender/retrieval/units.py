"""样本量单位释义 + 样本量格式化的**单一真源**。

历史上「样本量怎么显示」「单位怎么解释」这两段语义被复制在 workflow.py / llm_client.py /
webapp.py 三处，且 llm_client 的「仅 count」分支与规范不一致。这里把它们上移成结构化字段与
纯函数，供全项目复用；`format_sample_size` 是**全项目规范行为**，确定性冻结路径（workflow 的
markdown 表格）也走它。改样本量/单位文案，只改这一处。
"""

from __future__ import annotations

# unit（小写）→ 释义。逐字复制自旧 workflow._build_unit_explanation 的三句（含中文引号，勿改字）。
# dict 保序（cells→spots→nuclei）= 单位解释块的**规范输出顺序**，与历史逐位一致。
UNIT_EXPLANATIONS: dict[str, str] = {
    "cells": "“Cells” 指单细胞测序中捕获并测序的细胞数量。",
    "spots": "“Spots” 多用于空间转录组技术，表示组织切片上的空间检测位点，每个位点可能包含多个细胞。",
    "nuclei": "“Nuclei” 指单核测序或细胞核层面的计数单位，通常表示被捕获并测序的细胞核数量。",
}


def format_sample_size(count: str, unit: str) -> str:
    """样本量展示串（全项目规范）：
    count+unit → ``"{count} {unit}"``；仅 unit → ``"未说明 {unit}"``；
    仅 count → ``"{count}"``；都无 → ``"未说明"``。
    严格复刻旧 workflow._format_sample_size（不做 strip，交由调用方按需预处理）。
    """
    if count and unit:
        return f"{count} {unit}"
    if unit:
        return f"未说明 {unit}"
    if count:
        return f"{count}"
    return "未说明"


def explain_unit(unit: str) -> str | None:
    """小写去空白后查表返回单位释义；未知单位返回 None（绝不臆造）。"""
    if not unit:
        return None
    return UNIT_EXPLANATIONS.get(unit.strip().lower())


# 细胞计数类单位：这些数字是「测序规模」，最容易被误当成生物学重复数 n。
_CELL_COUNT_UNITS = {"cells", "nuclei", "spots"}


def sample_size_note(count: str, unit: str, n_files: int = 0) -> list[str]:
    """样本量语义提醒（确定性、单一真源）。

    只根据**已展示**的字段状态给出适用的诚实提醒，不臆造、不横向比较：
    - 细胞/位点/核数是测序捕获规模，**不等于**生物学重复数（n）；
    - 「可用文件」是文件数量，**不等于**样本数或生物学重复数；
    - 数字未标注单位时口径不明，**不宜**跨数据集横向比较；
    - 仅凭元数据**无法**判断统计功效。

    返回按上述顺序排列的适用提醒列表（无可提醒项 → 空列表）。生物学是本项目主场，
    这层是确定性的、零 IP、零外发——把「测序规模」与「统计功效」这对最常见的误读钉住。
    """
    u = (unit or "").strip().lower()
    c = (count or "").strip()
    try:
        nf = max(0, int(n_files or 0))
    except (TypeError, ValueError):
        nf = 0

    notes: list[str] = []
    if c and u in _CELL_COUNT_UNITS:
        notes.append("样本量是测序捕获的细胞/位点/核数（测序规模），不等于生物学重复数（n）。")
    if c and not u:
        notes.append("样本量数字未标注单位（是细胞数还是样本数？），含义不确定，不宜与其它数据集直接横向比较。")
    if nf > 1:
        notes.append("“可用文件”是文件数量，不代表样本数或生物学重复数。")
    if c or u or nf > 0:
        notes.append("仅凭元数据无法判断统计功效；是否够用需结合原始数据与实验设计评估。")
    return notes
