# -*- coding: utf-8 -*-
"""p11 · LLM 检索回执层（2026-08-30，**fail-open · 只在 `/api/search/reply` 端点**）。

产品原则（用户 2026-08-30 重申的原始要求）：**系统回复气泡 = LLM 给出的 final answer**——
可以灵活概述这次检索的过程/结果，也可以灵活给出帮助/建议/提示/提醒；绝不允许程序拼接的
死板模板冒充 LLM 回复。检索落地时前端先上一条确定性事实句（数字全部取自真实响应），
随后异步请本层把它改写成 1–2 句自然中文，成功则原位替换并挂「AI 总结」标；
LLM 缺席/失败时事实句原样保留（且不挂标——归因诚实），出任何岔子都无缝回退。

## 与 act_summary_llm 的关系

闸口（mock 短路 / enable / 无 key）、真 provider 通道、fail-open 纪律全部复用
`act_summary_llm._summarize_with_prompt`——那是同一份纪律的唯一实现，抄第二份就会漂移
（两档口径一旦分叉，会出现「执行总结说没接上、检索回执却在调」的双重口径）。本模块只
定义自己的事实行与 prompt 铁律。

## 接地防编造（与执行总结同口径）

prompt 只给调用方上报的事实行（命中数/展示数/命中关键词/解析状态/可建议动作白名单），
铁律禁止新增任何事实/数字；数字与关键词必须原文照用；0 命中/弃权/需澄清直说，
出路（放宽方式）只在事实标明存在时才许提。**建议白名单是硬约束**：只能从
「可建议动作」清单挑一条，清单为（无）时绝不允许发明建议——否则 LLM 会建议用户去做
前端根本没有入口的动作。

## 诚实说明（本层的验证边界）

结构 / 护栏 / 回退 / mock 短路 全部有**确定性**测试（monkeypatch 掉真 provider，无网络即可证明）。
「真 LLM 产出的中文质量」只能靠真 provider + 网络复验，不在 quality_gate 覆盖范围内。
"""
from __future__ import annotations

from typing import Any

from . import prompts
from .act_summary_llm import _summarize_with_prompt, should_use_llm  # noqa: F401  (should_use_llm 供端点回报闸口原因)
from .llm_client import LLMConfig


# 铁律写进 user prompt 而非 system slot 的理由：见 llm_client._call_chat_completions 的 system 引用处注释。
_SEARCH_REPLY_RULES_ZH = (
    "你是 BioData Agent 的检索结果解说助手。任务：把下面「事实」区块里给出的这次检索的结果，"
    "说成 1–2 句自然中文回复，让用户一眼知道查到了什么、接下来可以怎么办。\n"
    f"{prompts.ANTI_FABRICATION_HEADER_ZH}\n"
    "1. 只使用下面「事实」区块的行。绝不新增任何事实、数字、关键词或数据集名字——区块里没写的，"
    "一概不许出现。\n"
    "2. 数字（命中条数、展示条数）与命中关键词必须原文照用，不得改写、约算或四舍五入。\n"
    "3. 命中 0 条时直说没有找到，绝不谎称有结果；「有放宽方式」为「是」时才可以提结果区给出的"
    "放宽方式，为「否」时不许提。解析状态为「弃权/需澄清」时照事实说明原因，不许说成正常检索完成。\n"
    "4. 下一步建议只能从「可建议动作」清单里**原样挑一条**说；清单为（无）时绝对不许给任何建议。\n"
    "5. 输出 1–2 句自然中文，不超过 60 个字（含标点）；口语化但准确；不加标题、列表或 Markdown。\n"
    f"6. {prompts.NO_PARROT_RULE_ZH}"
)


def _fact_lines(facts: dict[str, Any]) -> "list[str]":
    """把 facts dict 摊成「事实」区块的逐行文本（纯函数；空块写「（无）」而不是省略——
    区块缺席会让 LLM 以为「没提 = 可以自由发挥」，写明「（无）」是接地的一部分）。"""
    total = int(facts.get("total") or 0)
    shown = int(facts.get("shown") or 0)
    keywords = [str(x).strip() for x in (facts.get("hit_keywords") or []) if str(x).strip()]
    suggest = [str(x).strip() for x in (facts.get("can_suggest") or []) if str(x).strip()]
    status_label = {
        "": "正常完成",
        "ok": "正常完成",
        "abstained": "弃权（这次没有做检索，结果区说明了原因）",
        "clarification_required": "需澄清（这句话有两种理解，结果区给了可以直接点的选项）",
    }.get(str(facts.get("resolution_status") or ""), "正常完成")
    lines = [
        prompts.fact_line_utterance_zh(facts),
        f"实际检索词：{str(facts.get('query') or '').strip() or '（未提供）'}",
        f"前置说明：{str(facts.get('note') or '').strip() or '（无）'}",
        f"命中条数：{total}",
        f"结果区展示条数：{shown}",
        f"命中关键词：{'、'.join(keywords) if keywords else '（无）'}",
        f"解析状态：{status_label}",
        f"有放宽方式：{'是' if facts.get('has_relax') else '否'}",
        "可建议动作：",
    ]
    if suggest:
        lines.extend(f"- {x}" for x in suggest)
    else:
        lines[-1] = "可建议动作：（无）"
    return lines


def build_search_reply_prompt(facts: dict[str, Any]) -> str:
    """据检索事实构造**接地**的 user prompt（纯函数、可确定性测试）。

    只放调用方上报的事实行；铁律禁止新增事实、要求数字/关键词原文照用、
    建议只能白名单原样挑一条（清单为空则禁止建议）。
    """
    return "\n".join([
        _SEARCH_REPLY_RULES_ZH,
        "",
        prompts.FACTS_BLOCK_HEADING_ZH,
        *_fact_lines(facts),
        "请据以上事实写 1–2 句中文回复（不超过 60 字）。",
    ])


def search_reply_with_llm(
    facts: dict[str, Any],
    *,
    config: LLMConfig | None = None,
) -> dict[str, Any]:
    """据检索事实生成可选的 LLM 中文回执（1–2 句档）。永不抛、永不编造事实。

    返回 {"reply_zh": str|None, "reply_source": "llm"|None,
          "llm_status": 原因短标签, "llm_model": str|None(仅成功)}。
    关/无 key/mock/provider 失败或空回一律 reply_zh=None——前端原样保留确定性事实句。
    """
    out = _summarize_with_prompt(facts, config=config, build_prompt=build_search_reply_prompt)
    return {
        "reply_zh": out["summary_zh"],
        "reply_source": "llm" if out["summary_zh"] else None,
        "llm_status": out["llm_status"],
        "llm_model": out["llm_model"],
    }
