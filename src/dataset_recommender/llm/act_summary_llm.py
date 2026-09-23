# -*- coding: utf-8 -*-
"""p10 · LLM 执行结果总结层（**默认随 key（已决策）· fail-open · 端点在 `/api/act/summary`**）。

（2026-08-30 p11 起：调用核心 `_summarize_with_prompt` 同时被 `search_reply_llm`（`/api/search/reply`
检索回执层）复用——同一闸口/通道/fail-open 纪律只有这一份实现，防止两档口径漂移。）

在执行层确定性事实句（前端 act_core 的 ACT_LEAD/actWhatHappened，数字全部取自真实返回值）之上，
**可选**叠加一段 LLM 生成的自然中文总结。它 additive、绝不替换事实句（LLM 缺席/失败时前端原样
保留事实句），出任何岔子都无缝回退。

## 为什么这样设计（把这一层的红线钉死在代码结构里）

- **默认随 key（产品方 2026-07-19 已决策：「如果填了apikey就默认开启，否则默认关闭」）**：
  判定单一真源在 `llm_client.resolve_enable_llm`，本层只消费 `load_llm_config` 的结果。
  关时零成本直返、不构造 prompt、不联网。
- **必须短路 mock**：`llm_client.call_mock_llm` **忽略 prompt**、直吐 curator markdown 表。
  若让总结层走 mock 分支，mock 测试会「荒谬通过」——产出根本不是执行总结。
  故 `should_use_llm` 里 `mock` 一律判否，只调**真** provider（`call_openai_compatible`/`call_zhipuai`）。
- **只总结，不执行**：本层输入是执行**已经完成**后的事实（done/gap/policy 行由调用方从真实
  返回值构造），本层不检索、不落盘、不产交付物；它唯一的作用是把这些事实说成 1–3 句自然中文。
- **fail-open**：provider 失败 / 无 key / 空返回 → `summary_zh=None`，`llm_status` 记原因，永不崩。
- **接地防编造**：prompt 只给调用方上报的事实行，铁律禁止新增任何事实/数字/文件名/动作；
  数字与文件名必须原文照用；`ok=False` 时绝不能说「已」完成任何事——与 act_core 的诚实不变量
  （失败支无「已」字）同口径，本层只复述、绝不粉饰失败。

## 诚实说明（本层的验证边界）

结构 / 护栏 / 回退 / mock 短路 全部有**确定性**测试（monkeypatch 掉真 provider，无网络即可证明）。
但「真 LLM 产出的中文质量」只能靠真 provider + 网络复验——那不在 `quality_gate`（清空密钥、指向空
LLM env、设网络 tripwire）能覆盖的范围内。本层 fail-open；无 key 的部署按 P1 默认关、不受影响。
"""
from __future__ import annotations

from typing import Any

from . import prompts
from .llm_client import (
    LLMConfig,
    _sanitize_provider_error,
    call_llm,
    load_llm_config,
    should_use_llm as _should_use_llm,
)


# 铁律写进 user prompt 而非 system slot 的理由：见 llm_client._call_chat_completions 的 system 引用处注释。
_RULES_ZH = (
    "你是 BioData Agent 的执行结果解说助手。任务：把下面「事实」区块里给出的执行结果，说成一段"
    "简洁、准确的自然中文总结，让用户快速知道这次动作到底做成了什么、没做成什么。\n"
    f"{prompts.ANTI_FABRICATION_HEADER_ZH}\n"
    f"1. {prompts.EXEC_FACTS_ONLY_RULE_ZH}\n"
    f"2. {prompts.EXEC_VERBATIM_NUMBERS_RULE_ZH}\n"
    "3. 若「事实」标明本次动作没有成功（ok=False），绝不能说「已」完成任何事；要明说**没有完成**，"
    "并照事实给出原因。\n"
    "4. 输出 1–3 句自然中文，口语化但准确；不加标题、列表或 Markdown。\n"
    f"5. {prompts.NO_PARROT_RULE_ZH}"
)


#: 一句话模式：执行收尾用**一句**话原位替换总结泡正文。
#: 与 _RULES_ZH 同一套接地铁律（只用事实、数字原文照用、ok=False 直说没做成不粉饰），
#: 只是把篇幅收紧到「一句、≤35 字」——两档并存，非 brief 路径逐位不变。
_BRIEF_RULES_ZH = (
    "你是 BioData Agent 的执行结果解说助手。任务：把下面「事实」区块里给出的执行结果，说成"
    "**一句**自然中文，让用户一眼知道这次动作到底做成了什么、没做成什么。\n"
    f"{prompts.ANTI_FABRICATION_HEADER_ZH}\n"
    "1. 只输出**一句**中文，不超过 35 个字（含标点）；不加标题、列表或 Markdown。\n"
    f"2. {prompts.EXEC_FACTS_ONLY_RULE_ZH}\n"
    f"3. {prompts.EXEC_VERBATIM_NUMBERS_RULE_ZH}\n"
    "4. 若「事实」标明本次动作没有成功（ok=False），直说**没有完成**并照事实给出原因，"
    "绝不粉饰、绝不能说「已」完成任何事。\n"
    f"5. {prompts.NO_PARROT_RULE_ZH}"
)


def should_use_llm(config: LLMConfig) -> "tuple[bool, str]":
    """判定是否调用**真** LLM。返回 (是否, 原因短标签)。

    mock 一律判否（它忽略 prompt、吐 curator 表，绝不用于总结）；enable 关、无 key 都判否。
    判定实现单一真源在 llm_client.should_use_llm，本函数是保名的薄封装。
    """
    return _should_use_llm(config)


def _fact_lines(facts: dict[str, Any]) -> "list[str]":
    """把 facts  dict 摊成「事实」区块的逐行文本（纯函数；空块写「（无）」而不是省略——
    区块缺席会让 LLM 以为「没提 = 可以自由发挥」，写明「（无）」是接地的一部分）。"""
    lines = [
        prompts.fact_line_utterance_zh(facts),
        f"动作：{str(facts.get('verb_zh') or '').strip() or '（未提供）'}",
        f"结果：{'成功' if facts.get('ok') else '没有成功（ok=False）'}",
    ]
    for key, label in (("done_lines", "做到的"), ("gap_lines", "没做到的"), ("policy_lines", "口径")):
        items = [str(x).strip() for x in (facts.get(key) or []) if str(x).strip()]
        if items:
            lines.append(f"{label}：")
            lines.extend(f"- {x}" for x in items)
        else:
            lines.append(f"{label}：（无）")
    return lines


def build_act_summary_prompt(facts: dict[str, Any]) -> str:
    """据执行事实构造**接地**的 user prompt（纯函数、可确定性测试）。

    只放调用方上报的事实行；铁律禁止新增事实、要求数字/文件名原文照用、ok=False 禁说「已」。
    """
    return "\n".join([
        _RULES_ZH,
        "",
        prompts.FACTS_BLOCK_HEADING_ZH,
        *_fact_lines(facts),
        "请据以上事实写 1–3 句中文总结。",
    ])


def build_act_brief_prompt(facts: dict[str, Any]) -> str:
    """一句话模式的接地 user prompt（纯函数；与长总结共用 `_fact_lines` 事实行真源——
    两档看到的事实必须一字不差，差别只在篇幅铁律）。"""
    return "\n".join([
        _BRIEF_RULES_ZH,
        "",
        prompts.FACTS_BLOCK_HEADING_ZH,
        *_fact_lines(facts),
        "请据以上事实写一句中文总结（不超过 35 字）。",
    ])


def _summarize_with_prompt(
    facts: dict[str, Any],
    *,
    config: LLMConfig | None,
    build_prompt: "Any",
    gate: "Any" = None,
) -> dict[str, Any]:
    """act 长/一句话、search_reply、intro 四处**共用的调用核心**：同一把 should_use_llm 闸、
    同一条真 provider 通道、同一份 fail-open 纪律——各档唯一的差别是 prompt 构造器与闸口附加档，
    各自走一遍就是抄两份（闸口口径一旦漂移，就会出现「一处说没接上一处却在调」的双重口径）。
    gate 缺省用本模块的 should_use_llm；intro 经它叠加体裁可译性档。

    返回 {"summary_zh": str|None, "summary_source": "llm"|None,
          "llm_status": 原因短标签, "llm_model": str|None(仅成功)}。
    """
    result: dict[str, Any] = {
        "summary_zh": None,
        "summary_source": None,
        "llm_status": "",
        "llm_model": None,
    }

    # 载 config（含 .env）。**刻意不做 `os.getenv("ENABLE_LLM")` 快路径**：.env 里的 ENABLE_LLM 只有
    # `load_llm_config`→`load_env_candidates` 才会灌进 `os.environ`，抢在它之前读 os.getenv 会在**新进程**
    # 里把「.env 已开」误判成「关」（intro_llm 已踩过的真机 bug，同教训同写法）。本端点是单次阻塞调用、
    # 不在热循环，每请求载一次 config 可接受。
    try:
        cfg = config or load_llm_config()
    except Exception as exc:  # 配置加载异常也 fail-open
        # 经脱敏层（防万一异常文本里带凭据；defense-in-depth，与 llm.error 同口径）。
        result["llm_status"] = f"config_error:{_sanitize_provider_error(exc)[:80]}"
        return result

    ok, reason = (gate or should_use_llm)(cfg)
    result["llm_status"] = reason
    if not ok:
        return result

    prompt = build_prompt(facts)
    try:
        llm = call_llm(prompt, cfg)
    except Exception as exc:  # provider 层任何异常 → 回退（脱敏，defense-in-depth；显式传 key 确保不泄漏）
        result["llm_status"] = f"error:{_sanitize_provider_error(exc, cfg.api_key)[:80]}"
        return result

    if llm.succeeded and llm.text and llm.text.strip():
        result["summary_zh"] = llm.text.strip()
        result["summary_source"] = "llm"
        result["llm_status"] = "ok"
        result["llm_model"] = llm.model
    else:
        # fail-open：调用方的事实句原样保留，只记原因（脱敏由 llm_client 负责）。
        result["llm_status"] = f"failed:{(llm.error or 'empty')[:80]}"
    return result


def summarize_action_with_llm(
    facts: dict[str, Any],
    *,
    config: LLMConfig | None = None,
) -> dict[str, Any]:
    """据执行事实生成可选的 LLM 中文总结（1–3 句档）。永不抛、永不编造事实。

    返回 {"summary_zh": str|None, "summary_source": "llm"|None,
          "llm_status": 原因短标签, "llm_model": str|None(仅成功)}。
    """
    return _summarize_with_prompt(facts, config=config, build_prompt=build_act_summary_prompt)


def summarize_brief_with_llm(
    facts: dict[str, Any],
    *,
    config: LLMConfig | None = None,
) -> "str | None":
    """一句话档：执行收尾用**一句 ≤35 字**原位替换总结泡正文。

    与长总结档共用 `_summarize_with_prompt`（同一闸、同一通道、同一份 fail-open 纪律），
    只换 `_BRIEF_RULES_ZH` 这份收紧篇幅的 prompt。成功回总结句本身；关/无 key/mock/
    provider 失败或空回一律 **None**——LLM 不在场时调用方只留首条事实句折叠其余，
    绝不伪造简洁。
    """
    return _summarize_with_prompt(facts, config=config, build_prompt=build_act_brief_prompt)["summary_zh"]
