"""Stable user-facing labels shared by backend presentation layers.

Keep domain labels here; request handlers and renderers must not invent variants.
The browser-side companion is ``web/static/js/core/copy.js`` and a contract test
checks the cross-language values that must remain byte-identical.
"""
from __future__ import annotations

from typing import Any


RAW_FASTQ_YES = "✅ 包含 FASTQ"
RAW_FASTQ_NO = "❌ 无 FASTQ"
RAW_FASTQ_GUESS = "⚪ 未确认"
RAW_FASTQ_UNKNOWN = "⚪ 未说明"

PROJECT_CONDITION_LABELS = {
    "include": "纳入条件",
    "exclude": "排除条件",
}
UNNAMED_DATASET = "（未命名）"
INTRO_FACT_LABELS = (
    "数据来源", "物种", "组织", "疾病", "技术与平台", "样本量", "发表时间", "原始数据",
)


def raw_fastq_status(value: Any, *, guessed_false: bool = False) -> str:
    """Render the catalogue-level FASTQ tri-state without overstating guesses."""
    if value is True:
        return RAW_FASTQ_YES
    if value is False:
        return RAW_FASTQ_GUESS if guessed_false else RAW_FASTQ_NO
    return RAW_FASTQ_UNKNOWN
