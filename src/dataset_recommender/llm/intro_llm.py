# -*- coding: utf-8 -*-
""" · LLM 中文介绍层（**默认随 key（已决策）· fail-open · 只在单数据集介绍端点**）。

在确定性介绍（`introduction.build_dataset_introduction`）之上，**可选**叠加一段 LLM 生成的中文导读。
它 additive、绝不替换确定性证据（facts / caveat 恒在），出任何岔子都无缝回退到确定性版。

## 为什么这样设计（把这一层的红线钉死在代码结构里）

- **默认随 key（产品方 2026-07-19 已决策：「如果填了apikey就默认开启，否则默认关闭」）**：
  `ENABLE_LLM` 未显式设置时，当前 provider 有真实（非 placeholder）API key 即默认开、否则默认关；
  显式 true/false 恒优先。判定单一真源在 `llm_client.resolve_enable_llm`（`config.get_settings` 同源
  复用），本层只消费 `load_llm_config` 的结果。关时零成本直返、不构造 prompt、不联网。
- **必须短路 mock**：`llm_client.call_mock_llm` **忽略 prompt**、直吐 curator markdown 表
  （见其实现）。若让介绍层走 `call_llm` 的 mock 分支，mock 测试会「荒谬通过」——产出根本不是导读。
  故 `should_use_llm` 里 `mock` 一律判否，只调**真** provider（调用核心复用 act_summary_llm
  `_summarize_with_prompt`：同一闸、同一通道、同一份 fail-open 纪律）。
- **只译可译的体裁**：`summary_genre` 已把「有真散文可译」（prose/title）与「机器拼装」（scaffold/
  factor_list/absent）分开。只有 `TRANSLATABLE` 才喂 LLM；CXG 的 title 明确是**关联论文标题、不是摘要**，
  prompt 里说清，绝不让 LLM 据标题编数据集实验细节。
- **fail-open**：provider 失败 / 无 key / 空返回 → 确定性介绍原样保留，`llm_status` 记原因，永不崩。
- **接地防编造**：prompt 只给已有元数据 + 来源叙述，铁律禁止补写未标注字段、禁止对研究下开创性/独占性
  断言（受众不变量：数据集目录无从判断学界地位）、截断不补全。
- **术语落表（硬校验 #2 满分化）**：`build_intro_prompt` 把本条记录 species/tissue/disease 字段实际命中的
  受控词表规范词（`vocabulary.CATALOG` 自带的中文规范写法，非本层新造译名）随 prompt 给出，铁律要求译文
  必须使用规范词、不得自造译名；未命中的维度/条目零注入（无关词表内容不进 prompt）。

## 诚实说明（本层的验证边界）

结构 / 护栏 / 回退 / mock 短路 全部有**确定性**测试（monkeypatch 掉真 provider，无网络即可证明）。
但「真 LLM 产出的中文质量」只能靠真 provider + 网络复验——那不在 `quality_gate`（清空密钥、指向空
LLM env、设网络 tripwire）能覆盖的范围内。本层 fail-open；无 key 的部署按 P1 默认关、不受影响。
"""
from __future__ import annotations

import re
from typing import Any

from ..content.introduction import build_dataset_introduction, meaningful_metadata_text
from . import prompts
from .act_summary_llm import _summarize_with_prompt
from .llm_client import (
    LLMConfig,
    should_use_llm as _should_use_llm,
)
from ..content import summary_genre
from ..domain.registry import domain_catalog as _domain_catalog


# 铁律写进 user prompt 而非 system slot 的理由：见 llm_client._call_chat_completions 的 system 引用处注释。
_RULES_ZH = (
    "你是单细胞数据集目录的中文导读助手。任务：把下面给出的**公开元数据**改写成一段简洁、准确的"
    "中文导读，帮中文研究者快速判断该数据集是否与其研究相关。\n"
    f"{prompts.ANTI_FABRICATION_HEADER_ZH}\n"
    "1. 只使用给出的元数据。绝不补充、推断或编造任何未给出的事实、数字、结论或方法细节。\n"
    "2. 元数据里标为「未说明/未标注」的字段，不要替它填值，也不要暗示它有值。\n"
    "3. 不要对该研究下任何『开创性 / 独占性 / 学界领先』判断——这是公开数据集目录、不是文献库，"
    "无从判断一项工作在同行中是否独一无二或空前。\n"
    "4. 若标注这是「关联论文标题」，它只是论文标题、不是数据集摘要；写导读时要说清这一点，"
    "不要把标题当成对数据集内容的描述、不要据标题编出实验细节。\n"
    "5. 若标注文本已被截断，不要补全被截断的句子，可提示原文在此处被截断。\n"
    "6. 输出 2–4 句纯中文，不加标题/项目符号/Markdown；客观、不夸张。\n"
    "7. 术语必须落到受控词表规范词：若下方给出「术语规范」表，凡表中列出的术语，译文必须使用对应"
    "规范词，不得自造译名（例如 Homo sapiens 只能译作「人类」，不得译成「智人」或「人源」）；"
    "表未覆盖的术语保持朴素直译或保留原文，不得编造听起来更专业的名字。技术与平台名是规范技术名"
    "（英文品牌/缩写），保持原文写法，不要自造中文译名。"
)


def should_use_llm(config: LLMConfig, genre: str) -> "tuple[bool, str]":
    """判定是否对该体裁调用**真** LLM。返回 (是否, 原因短标签)。

    mock 一律判否（它忽略 prompt、吐 curator 表，绝不用于介绍）；enable 关、无 key、不可译 都判否。
    判定实现单一真源在 llm_client.should_use_llm，体裁可译性档经 post 钩子叠加。
    """
    return _should_use_llm(
        config,
        post=lambda _c: None if genre in summary_genre.TRANSLATABLE else (False, "not_translatable"),
    )


# ---------------------------------------------------------------- 硬校验 #2 满分化：受控词表随 prompt 接地
# 只读消费 `vocabulary.CATALOG`（公共常量；workflow 构造 rerank 审核提示时同径只读）。
# 规范词取条目的**首个中文 alias**——那是词表自带的规范写法（「人类」「肺」「肺癌」），不是本层新造译名；
# 无中文 alias 时回退 display（如 MERFISH 这类技术名本就该保持英文）。
#
# 只接地 species / tissue / disease 三个维：它们有真中文规范词、且自由散文里确实需要翻译（拉丁学名→通用名
# 是 #2 点名的风险面）。平台/assay 是 Chromium / Smart-seq 这类英文规范技术名，LLM 本应保持原文，若落表反而
# 会把 Visium 错压成「空间转录组」这类通用词——故技术名由铁律 7 统一要求保持原文，不进映射表。

_CJK_RE = re.compile(r"[一-鿿]")

_GROUND_DIMS: "tuple[tuple[str, str, str], ...]" = (
    ("species", "species", "物种"),
    ("tissue", "tissue", "组织"),
    ("disease", "disease", "疾病"),
)


def _canonical_zh(entry: dict[str, Any]) -> str:
    """词表条目的规范词：首个含汉字的 alias；无中文 alias → display（技术名保持英文）。"""
    for alias in entry.get("aliases") or []:
        alias = str(alias).strip()
        if alias and _CJK_RE.search(alias):
            return alias
    return str(entry.get("display") or "").strip()


def _ascii_boundary_ok(low: str, start: int, end: int, term: str) -> bool:
    """ASCII 术语要求词边界（前后非字母数字），防「foreskin」误中「skin」、「Meyer」误中「eye」。

    CJK 术语无词界概念，直接放行；「non-human primate」里「human」前是连字符 → 仍算边界，不误杀。
    """
    if term and term[0].isascii() and term[0].isalnum():
        if start > 0 and low[start - 1].isascii() and low[start - 1].isalnum():
            return False
    if term and term[-1].isascii() and term[-1].isalnum():
        if end < len(low) and low[end].isascii() and low[end].isalnum():
            return False
    return True


def _ground_terms_for_value(value: str, dim: str) -> "list[str]":
    """记录某维字段值实际命中的词表规范词（按原文出现顺序、去重）。

    匹配复用 CATALOG 的「子串 + 长优先消费」语义（alias ∪ target，小写，ASCII 词加词边界）：短匹配落入
    已消费的长匹配 span 则丢弃——「lung cancer」命中「肺癌」后不再重复命中 generic「癌」。
    """
    text = (value or "").strip()
    if not text:
        return []
    low = text.lower()
    matches: "list[tuple[int, int, str]]" = []  # (start, end, canonical)
    for entry in _domain_catalog().get(dim) or []:
        if not isinstance(entry, dict):
            continue
        canonical = _canonical_zh(entry)
        if not canonical:
            continue
        terms = [str(t).strip().lower() for t in (entry.get("aliases") or [])]
        terms += [str(t).strip().lower() for t in (entry.get("targets") or [])]
        best: "tuple[int, int] | None" = None
        for term in terms:
            if not term:
                continue
            pos = low.find(term)
            while pos >= 0:
                end = pos + len(term)
                if _ascii_boundary_ok(low, pos, end, term):
                    if best is None or len(term) > best[1] - best[0]:
                        best = (pos, end)
                    break  # 该 term 已取到首个合法出现，去看下一个 term
                pos = low.find(term, pos + 1)
        if best is not None:
            matches.append((best[0], best[1], canonical))
    matches.sort(key=lambda m: (-(m[1] - m[0]), m[0]))  # 长优先消费
    taken: "list[tuple[int, int]]" = []
    accepted: "list[tuple[int, str]]" = []
    for start, end, canonical in matches:
        if any(start >= ts and end <= te for ts, te in taken):
            continue
        taken.append((start, end))
        accepted.append((start, canonical))
    out: "list[str]" = []
    for _, canonical in sorted(accepted):
        if canonical not in out:
            out.append(canonical)
    return out


def _vocab_grounding_lines(item: dict[str, Any]) -> "list[str]":
    """「术语规范」块的内容行；三个维都无命中 → 空列表（整块不进 prompt，无关词表内容零注入）。"""
    lines: "list[str]" = []
    for field, dim, label in _GROUND_DIMS:
        value = meaningful_metadata_text(item.get(field))
        terms = _ground_terms_for_value(value, dim)
        if terms:
            shown = "、".join(f"「{t}」" for t in terms)
            lines.append(f"{label}：原文「{value}」→ 规范词{shown}")
    return lines


def build_intro_prompt(intro: dict[str, Any], item: dict[str, Any], genre: str) -> str:
    """据确定性介绍 + 记录字段构造**接地**的 user prompt（纯函数、可确定性测试）。

    只放已有元数据 + 来源叙述；体裁决定叙述如何框定（prose=忠实改写 / title=明说是论文标题）。
    """
    def fact(label: str) -> str:
        for f in intro.get("facts") or []:
            if f.get("label") == label:
                return str(f.get("value") or "")
        return ""

    lines = [
        _RULES_ZH,
        "",
        "----- 元数据 -----",
        f"数据集名称：{meaningful_metadata_text(item.get('dataset_name')) or fact('数据集名称') or '未说明'}",
        f"数据来源：{fact('数据来源') or '未说明'}",
        f"物种：{fact('物种') or '未说明'}",
        f"组织：{fact('组织') or '未标注'}",
        f"疾病：{fact('疾病') or '未标注'}",
        f"技术与平台：{fact('技术与平台') or '未说明'}",
        f"样本量：{fact('样本量') or '未说明'}",
    ]
    grounding = _vocab_grounding_lines(item)
    if grounding:
        lines.append("")
        lines.append("----- 术语规范（本条记录命中的受控词表规范词，译文必须照用） -----")
        lines.extend(grounding)
    residual = str(intro.get("summary") or "").strip()
    if genre == summary_genre.GENRE_TITLE:
        lines.append(
            "该来源只提供了**关联论文的标题**（不是数据集摘要）：\n" + residual + "\n"
            "请把它作为论文标题介绍，并明确说明这是关联论文标题、非数据集摘要；不要据标题编造实验内容。"
        )
    else:  # GENRE_PROSE（唯一另一个 TRANSLATABLE）
        block = "来源提供的研究描述（请忠实改写为中文，不要增删事实）：\n" + residual
        if intro.get("truncated"):
            block += "\n（注意：以上文字在抓取时已被截断，不是完整原文，不要补全被截断的句子。）"
        lines.append(block)
    lines.append("请据以上元数据写 2–4 句中文导读。")
    return "\n".join(lines)


def enrich_introduction_with_llm(
    item: dict[str, Any],
    *,
    intro: dict[str, Any] | None = None,
    config: LLMConfig | None = None,
) -> dict[str, Any]:
    """在确定性介绍上 additive 叠加 `llm_summary`（可选）。永不抛、永不替换确定性字段。

    返回的 dict = 确定性介绍 + `llm_summary`(str|None) + `llm_summary_source`("llm"|None) +
    `llm_status`(原因短标签) + `llm_model`(成功时)。
    """
    result = dict(intro) if intro is not None else build_dataset_introduction(item)
    result["llm_summary"] = None
    result["llm_summary_source"] = None

    genre = str(result.get("genre") or "")
    # 调用核心（载 config / 闸 / provider 通道 / fail-open）单一实现在
    # act_summary_llm._summarize_with_prompt；本层只提供体裁附加闸与 prompt 构造器，
    # 并把通用返回键映射回本层命名。
    out = _summarize_with_prompt(
        result,
        config=config,
        build_prompt=lambda _f: build_intro_prompt(result, item, genre),
        gate=lambda cfg: should_use_llm(cfg, genre),
    )
    result["llm_summary"] = out["summary_zh"]
    result["llm_summary_source"] = out["summary_source"]
    result["llm_status"] = out["llm_status"]
    if out["llm_model"] is not None:  # llm_model 仅成功时存在（additive 钉）
        result["llm_model"] = out["llm_model"]
    return result
