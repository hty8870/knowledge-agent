"""维度描述符：骨架引擎与领域数据之间的最小契约。

骨架（解析器/检索器/编排）不认识任何具体维度名，只遍历领域包注册的
`DimensionDescriptor` 序列：每个描述符声明一个检索维度的
名字、中文标签、从规范化记录取该维文本的访问器与匹配语义。

biodata 的 6 个维度描述符（`domains/biodata/dimensions.py`）逐项复刻旧
`retriever._dim_field_text` 写死 if 链的行为；新领域包（课程库、文献库……）
只声明自己的描述符即可挂上同一条管线，骨架零改动。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

MATCH_SUBSTRING = "substring"
MATCH_EXACT = "exact"
_MATCH_MODES = (MATCH_SUBSTRING, MATCH_EXACT)


@dataclass(frozen=True)
class DimensionDescriptor:
    """一个检索维度的完整声明。

    - `name`：维度标识（查询/意图/响应里的 key，如 "species"）。
    - `label_zh`：维度中文标签（分面/条件板/回显共用）。
    - `get_text(record)`：硬约束判定的字段文本访问器（duck-typed，不依赖具体记录类）。
    - `match`：匹配语义——`substring`（targets 任一出现于文本）或 `exact`（文本小写后
      精确等值于 targets 之一）。
    - `partial_capable`：该维取值集合**可能不完整**（如抽样回填）的记录是否需按
      `metadata_provenance.complete` 三态处理（诚实降级用；biodata 仅 tissue/disease）。
    - `facet_text(record)`：分面分组用的原始取值访问器；缺省与 `get_text` 相同再 strip。
      （biodata 的 assay 维：硬约束用 assay+chemistry 联合文本、分面只用 assay 字段。）
    - `casefold`：分面键是否按小写归并（自由文本维度消除 Blood/blood 重复桶）。
    """

    name: str
    label_zh: str
    get_text: Callable[[Any], str]
    match: str = MATCH_SUBSTRING
    partial_capable: bool = False
    facet_text: Callable[[Any], str] | None = None
    casefold: bool = False

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("DimensionDescriptor.name 不能为空")
        if self.match not in _MATCH_MODES:
            raise ValueError(f"未知匹配语义: {self.match!r}（合法值 {_MATCH_MODES}）")
        if not callable(self.get_text):
            raise TypeError("get_text 必须是可调用访问器")

    def text_of(self, record: Any) -> str:
        return self.get_text(record) or ""

    def facet_of(self, record: Any) -> str:
        f = self.facet_text or self.get_text
        return (f(record) or "").strip()
