# -*- coding: utf-8 -*-
"""两个数据集的确定性字段对比 + LLM 措辞层（compare.datasets）。

## 分层（数字与事实只有一处真源）

- **确定性 diff**（`diff_items`）：纯函数、零 LLM、零网络。复用 `item_view.build_item`
  的归一化 item + `normalizer.is_missing_value` 的缺失判定，逐字段产出结构化差异
  （same / different / only_a / only_b / both_missing）。**这是事实层**。
- **LLM 措辞层**（`build_prompt` + 措辞调用本身在 agent_exec 的
  `_render_compare_with_llm`）：独立上下文 + 独立系统提示词（`prompts/compare.md`，
  文件即真源），把 diff JSON 翻译成一段中文对比。**只负责措辞**：机械健全性检查
  （非空、≤800 字符、阿拉伯数字必须能在 diff 事实文本里找到出处——数字交叉核验，
  与 agent_exec 汇报后检同一哲学）任一不过 → 退回确定性拼接
  （`render_deterministic`），`wording_source` 如实标注。

## 诚实边界

- 只对比**元数据字段**，不评价哪个数据集更好、不推断批次效应/实际可整合性。
- 缺失字段如实记「未知 / 未标注」（only_a / only_b / both_missing），**绝不**把
  「我们不知道」说成「它没有」。
- 降级路径（找不到数据集 / 只有一条可比 / 无结果 / 字段全同）产出诚实的句子，
  不假装对比成功、不编造差异。

本模块自身不 import langchain 系（措辞调用的模型注入由 agent_exec 负责，与 rerank
改写同纪律）；只被 `agent_exec` 消费，检索器/编排/冻结评测从不 import 它。
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..retrieval.normalizer import is_missing_value

#: 参与对比的字段（key, 展示名）——顺序即结论里的呈现顺序。key 与
#: `item_view.build_item` 的 item 键同名（sample_size 为 count+unit 合成键）。
DIFF_FIELDS: tuple[tuple[str, str], ...] = (
    ("dataset_name", "数据集名称"),
    ("source", "来源"),
    ("species", "物种"),
    ("tissue", "组织"),
    ("disease", "疾病"),
    ("platform", "平台"),
    ("assay", "技术(assay)"),
    ("chemistry", "chemistry"),
    ("modality", "技术模态"),
    ("sample_size", "样本量"),
    ("published_date", "发表时间"),
    ("n_files", "文件数"),
)

#: 多值字段（逗号分隔）：按**集合**比较——顺序差异不算差异（compatibility._tokens 同思路）。
_MULTI_VALUE_FIELDS: frozenset[str] = frozenset({"species", "tissue", "disease"})

#: LLM 措辞的字数上限（机械健全性检查之一；措辞层只是翻译，不该长篇大论）。
MAX_WORDING_CHARS = 800


def _tokens(text: str) -> set[str]:
    """逗号分隔文本 → 归一小写 token 集合（compatibility._tokens 同思路，只做比较不做展示）。"""
    return {p.strip().lower() for p in str(text or "").split(",") if p.strip()}


def field_value(item: dict[str, Any], key: str) -> str:
    """item → 该字段的比较值（缺失/未知哨兵 → ""；sample_size 由 count+unit 合成）。"""
    if key == "sample_size":
        count = str(item.get("count") or "").strip()
        unit = str(item.get("unit") or "").strip()
        if count and not is_missing_value(count) and unit and not is_missing_value(unit):
            return f"{count} {unit}".strip()
        return ""
    value = str(item.get(key) or "").strip()
    if not value or is_missing_value(value):
        return ""
    return value


def diff_items(item_a: dict[str, Any], item_b: dict[str, Any]) -> dict[str, Any]:
    """两个 item → 结构化字段差异。**纯函数（零 LLM 零网络），独立单测的对象。**

    返回 {fields: [{field, label_zh, a, b, status}], n_same, n_diff, n_unknown, identical}：
      - status ∈ same / different / only_a / only_b / both_missing；
      - n_diff 计 different + only_a + only_b；n_same 计 same；both_missing 另计 n_unknown；
      - identical = n_diff == 0（字段全同是**如实结论**，不是失败）。"""
    fields: list[dict[str, str]] = []
    n_same = n_diff = n_unknown = 0
    for key, label_zh in DIFF_FIELDS:
        a = field_value(item_a, key)
        b = field_value(item_b, key)
        if key in _MULTI_VALUE_FIELDS:
            equal = _tokens(a) == _tokens(b)
        else:
            equal = a == b
        if not a and not b:
            status, n_unknown = "both_missing", n_unknown + 1
        elif not b:
            # b 缺失 → 只有 a 有
            status, n_diff = "only_a", n_diff + 1
        elif not a:
            # a 缺失 → 只有 b 有
            status, n_diff = "only_b", n_diff + 1
        elif equal:
            status, n_same = "same", n_same + 1
        else:
            status, n_diff = "different", n_diff + 1
        fields.append({
            "field": key, "label_zh": label_zh,
            "a": a, "b": b, "status": status,
        })
    return {
        "fields": fields, "n_same": n_same, "n_diff": n_diff,
        "n_unknown": n_unknown, "identical": n_diff == 0,
    }


def _missing_zh(value: str) -> str:
    return value if value else "（缺失/未标注）"


def render_deterministic(diff: dict[str, Any], name_a: str, name_b: str) -> str:
    """确定性对比结论（LLM 缺席/措辞不过机械检查时的兜底——与 LLM 措辞同一批事实）。"""
    name_a = str(name_a or "数据集A")
    name_b = str(name_b or "数据集B")
    if diff["identical"]:
        return (f"「{name_a}」与「{name_b}」在可比字段上完全相同"
                f"（{diff['n_same']} 个字段一致），未发现差异。")
    diffs = [f for f in diff["fields"] if f["status"] in ("different", "only_a", "only_b")]
    shown = "；".join(
        f"{f['label_zh']}：{_missing_zh(f['a'])} vs {_missing_zh(f['b'])}"
        for f in diffs[:4])
    tail = f"等 {len(diffs)} 项" if len(diffs) > 4 else ""
    return (f"「{name_a}」与「{name_b}」对比：{diff['n_same']} 个字段一致、"
            f"{diff['n_diff']} 个字段不同（{shown}{tail}）。")


def _fact_corpus(diff: dict[str, Any], name_a: str, name_b: str) -> str:
    """措辞里阿拉伯数字的**允许出处**（diff 事实文本 + 两个数据集名），机械交叉核验用。"""
    parts = [str(name_a or ""), str(name_b or "")]
    for f in diff.get("fields") or []:
        parts.append(str(f.get("a") or ""))
        parts.append(str(f.get("b") or ""))
    return " ".join(parts)


_DIGIT_RUN_RE = re.compile(r"\d+")


def introduces_foreign_numbers(text: str, diff: dict[str, Any], name_a: str, name_b: str) -> bool:
    """LLM 措辞里是否有 diff 事实文本中**找不到出处**的阿拉伯数字？

    与 narrate 的数字交叉核验同一哲学（汇报数字必须有出处）：LLM 只负责措辞，
    任何数字都必须是确定性 diff 里已有的（日期「」里的 2021/3/15、样本量
    12000 里的 1/2/0 都以子串形式存在于事实文本——按**数字串子串**判出处，宽容合法改写
    「约 1.2 万」里的 1/2，拦截凭空捏造的 9999）。"""
    corpus = _fact_corpus(diff, name_a, name_b)
    return any(run not in corpus for run in _DIGIT_RUN_RE.findall(str(text or "")))


def build_prompt(diff: dict[str, Any], name_a: str, name_b: str) -> str:
    """措辞层的人类消息素材：diff 事实 JSON + 两个数据集名（系统提示词在
    `prompts/compare.md`，agent_exec 侧懒加载、缺文件退回内置最小版）。"""
    return json.dumps({
        "dataset_a": str(name_a or ""),
        "dataset_b": str(name_b or ""),
        "same_fields": diff["n_same"],
        "different_fields": diff["n_diff"],
        "identical": diff["identical"],
        "fields": [
            {"label_zh": f["label_zh"], "a": f["a"], "b": f["b"], "status": f["status"]}
            for f in diff["fields"]
        ],
    }, ensure_ascii=False)
