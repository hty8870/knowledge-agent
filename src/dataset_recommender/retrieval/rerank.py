"""
可选 LLM 重排层（默认关，零权重下载，复用现有 LLM 通道）。

设计约束（与 Codex + 内部评审收敛一致）：
- 只对**已过硬过滤的存活集**重排：输入恒为 survivors 的子集，输出是其**排列/截断**，
  绝不引入集合外记录 → 0% 硬违规由 retriever 的终检(passes_hard_filter)结构性保证，
  本层的 ID 交集守卫是额外冗余（defense-in-depth）。
- 后端 `off`：原样返回（与未启用时**字节等价**）。
- 后端 `llm`：编号候选 → LLM 返回编号排列 → 交集存活集 + 补回遗漏 + 截 top_k；
  任何异常/空/解析失败 → 回退传入顺序（永不报错、永不违规）。

本模块不下载任何模型，不新增运行时依赖；LLM 调用复用 llm_client 的现成通道。
查询改写、零命中救回与动作理解统一由 Agent 工具环负责；本模块只排序，不生成第二条判断通道。
"""
from __future__ import annotations

import contextvars
import json
import re
import time
from dataclasses import asdict
from typing import Callable, Sequence

from ..llm.llm_client import LLMConfig, call_llm, is_auth_error
from .retriever import RetrievedCandidate
from .vector_recall import _WARNED, _warn_once_prefixed

RERANK_BACKENDS = ("off", "llm")
DEFAULT_RERANK_TOP_N = 12


def _candidate_line(idx: int, cand: RetrievedCandidate) -> str:
    r = cand.record
    desc = (r.description or "").strip().replace("\n", " ")
    if len(desc) > 120:
        desc = desc[:120] + "…"
    fields = [
        f"物种:{r.species or '-'}",
        f"组织:{r.tissue or '-'}",
        f"疾病:{r.disease or '-'}",
        f"平台:{r.platform_family or '-'}",
        f"实验:{r.assay or '-'}",
    ]
    return f"{idx}. {r.dataset_name or '(未命名)'} | " + " | ".join(fields) + (f" | {desc}" if desc else "")


def build_rerank_prompt(
    query: str,
    candidates: Sequence[RetrievedCandidate],
) -> str:
    """构造唯一的纯重排 prompt；查询改写由 Agent 的 ``search.rerun`` 工具负责。"""
    lines = [_candidate_line(i + 1, c) for i, c in enumerate(candidates)]
    n = len(candidates)
    return (
        "你是一个检索结果重排器。下面是针对用户查询检索到的候选数据集"
        "（它们已经通过了全部硬性筛选条件，都合规）。\n"
        "请**仅根据与查询的语义相关性**，把它们从最相关到最不相关重新排序。\n\n"
        f"用户查询：{query}\n\n"
        "候选（每行前是编号）：\n" + "\n".join(lines) + "\n\n"
        f"要求：只输出一个 JSON 整数数组，包含 1 到 {n} 的**全部**编号、不重复、不新增，"
        "按相关性从高到低排列，例如 [3,1,2,...]。除这个数组外不要输出任何其它文字。"
    )


def _sanitize_order(values, n: int) -> list[int]:
    """把一串（可能混杂）编号规整成 **0-based 索引**排列：转 int、1-based→0-based、去重、限 [0,n)、保序。"""
    order: list[int] = []
    seen: set[int] = set()
    for token in values:
        try:
            one_based = int(token)
        except (ValueError, TypeError, OverflowError):
            # OverflowError：JSON 里的 1e400 / Infinity 被 json.loads 解析成 float('inf')，
            # int(inf) 抛 OverflowError（非 ValueError/TypeError 子类）——不接住会穿透 fail-open 链。
            continue
        zero_based = one_based - 1
        if 0 <= zero_based < n and zero_based not in seen:
            seen.add(zero_based)
            order.append(zero_based)
    return order


def parse_order(text: str, n: int) -> list[int]:
    """从 LLM 文本抽出编号排列，返回 **0-based 索引**（去重、限定在 [0,n) 内、按出现顺序）。

    对 LLM 的输出格式极其宽容：抽取所有整数，1-based → 0-based，丢弃越界/重复。
    """
    return _sanitize_order(re.findall(r"\d+", text or ""), n)


def _first_json_object(text: str) -> str | None:
    """Return the first outer JSON-object slice from a noisy model response."""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    return text[start : end + 1]


# ==================== 解析失败的错误回灌====================
# BioChatter ResponderWithRetries 的本落化：LLM 输出机械结构无效时，把失败原因回灌重问
# **一次**（上限硬顶），仍败照旧走既有 fail-open——「闸住之后给一次自修机会」，
# 与本仓 repair（violations 回灌）/ decide（非法重问）同一哲学的检索侧落地。
# 纪律（审核）：
#   · 共享层只是 transport 外壳；每条路径自带 typed validator 判 valid/partial/invalid，互不共用；
#   · 只重试「非空输出且机械结构无效」；空输出 / 异常 / 鉴权错误绝不重试（原语义逐位不变）；
#   · partial（可用但走形：缺号排列、rewrite-only、markers-only）**不重试**——
#     既有的交集守卫 / 宽容解析本就是为它们准备的；
#   · 「10x」这类字母粘连文本不算数字（standalone 判定）——畸形应答不许被当成候选 10。
# 可观测：重试结局落 `_LAST_PARSE_RETRY`（channel → "recovered" / "failed"）；
# `rerank_candidates` 把它拷进 trace 的**附加字段** parse_retry——不覆盖既有 reason。
# ContextVar 而不是模块级 dict（2026-08-15 触发点，与下方 _LAST_LLM_ERROR 同型）：
# `/api/recommend` 是 sync def 走 anyio 线程池，模块级 dict 让两路并发请求互踩
# （A 写 "recovered" → B 覆写 "failed" → A pop 到 B 的结局，trace 张冠李戴）；
# ContextVar 每请求各拿一份默认值，重试状态出不了本请求。
# 读写必须「拷贝-改写-set」：ContextVar 的 default dict 对象本身跨上下文共享，
# 原地 mutate 就等于又变回模块级共享。
_LAST_PARSE_RETRY: contextvars.ContextVar[dict] = contextvars.ContextVar(
    "rerank_last_parse_retry", default={}
)


def _parse_retry_put(channel: str, outcome: str) -> None:
    _LAST_PARSE_RETRY.set({**_LAST_PARSE_RETRY.get(), channel: outcome})


def _parse_retry_take(channel: str) -> "str | None":
    """读出并清除**本上下文**里该渠道的重试结局（无 → None）。"""
    outcomes = dict(_LAST_PARSE_RETRY.get())
    outcome = outcomes.pop(channel, None)
    _LAST_PARSE_RETRY.set(outcomes)
    return outcome


def _standalone_numbers(text: str) -> list[str]:
    """standalone 数字（两侧都不粘连单词字符）：「3, 1, 2」命中；「10x」「GSE123」不命中。"""
    return re.findall(r"(?<![\w])\d+(?![\w])", text or "")


def _maybe_retry_parse(first_text, prompt: str, *, caller, validate, contract_zh: str, channel: str):
    """非空输出经 validate 判 invalid → 形状契约回灌重问一次；返回最终应采用的那段文本。

    任何不重试的情形（首答非 invalid / caller 抛异常 / 二答为空或非字符串 / 二答仍 invalid）
    都原样返回首答——下游宽容解析链行为与历史逐位一致。
    """
    if not isinstance(first_text, str) or not first_text.strip():
        return first_text
    if validate(first_text) != "invalid":
        _parse_retry_take(channel)
        return first_text
    feedback = (
        prompt + "\n\n（系统回执：你上一次的输出机械结构无效，无法解析。"
        + contract_zh + "除这个 JSON 外不要输出任何其它文字。）"
    )
    try:
        second = caller(feedback)
    except Exception:
        second = None
    if isinstance(second, str) and second.strip() and validate(second) != "invalid":
        _parse_retry_put(channel, "recovered")
        return second
    _parse_retry_put(channel, "failed")
    return first_text


def _grade_order_text(text: str, n: int) -> str:
    """纯重排输出分级：JSON 数组且 1..n 全覆盖 → valid；有 standalone 数字（宽容链可消费）
    → partial；啥都提不出 → invalid。"""
    if not _standalone_numbers(text):
        return "invalid"
    try:
        arr = json.loads((text or "").strip())
        if isinstance(arr, list):
            got = {int(x) for x in arr if isinstance(x, (int, float)) and not isinstance(x, bool)}
            if len(got) == n:
                return "valid"
    except Exception:
        pass
    return "partial"


def apply_order(
    order: list[int], candidates: Sequence[RetrievedCandidate]
) -> list[RetrievedCandidate]:
    """按解析出的顺序重排；**补回任何被 LLM 遗漏的候选**（按原顺序追加）。

    输出严格是输入 candidates 的一个排列（长度不变、元素不变），保证不丢存活集成员、
    不引入集合外成员。截断由调用方决定。
    """
    picked = [candidates[i] for i in order]
    included = set(order)
    for i, cand in enumerate(candidates):
        if i not in included:
            picked.append(cand)
    return picked


def _default_llm_call_with_error(prompt: str, config: LLMConfig | None) -> "tuple[str | None, str | None]":
    """同 _default_llm_call 的真身，但把 provider 错误串一并带回（成功时错误为 None）。

    错误串的用途只有一个：回退归因把「密钥无效（401/403）」与「临时故障（超时/5xx/空回）」
    分开（2026-08-04 C3）——前者重试不自愈，用户该去改设置；混成一句「调用失败」时，
    密钥坏了的人会对着「稍后再试」干等。
    """
    if config is None or not getattr(config, "api_key", None):
        return None, None
    cfg = LLMConfig(**asdict(config))
    cfg.temperature = 0.0          # 求确定性（注意：DeepSeek 服务端仍不完全可复现）
    cfg.enable_llm = True
    cfg.mock_llm = False
    result = call_llm(prompt, cfg)
    return (result.text if (result.succeeded and result.text) else None), result.error


# 最近一次**真实**默认通道调用的 provider 错误串（无 → None）。只服务回退归因分档；
# 私有、调用前清、随调用写——测试用替身换掉 _default_llm_call 时没有错误面（保持 None），
# 归因自动落回临时故障档，与既有钉死行为逐位一致。
# ContextVar 而不是模块级槽（2026-08-04 对抗评审 docs-arch）：`/api/recommend` 是 sync def
# 走 anyio 线程池，两路并发检索共享一个模块全局会互踩——A 清槽 → B 清槽 → A 写 → B 写 → A 读，
# A 的 401 可能被读成 B 的超时，「密钥无效」误标成「临时故障」（用户被指路去白等）。
# ContextVar 随 anyio 的上下文快照走，每个请求各拿一份，互不串；同一线程内清→写→读是同步连续
# 执行、无 await 让出，也不可能自踩。
_LAST_LLM_ERROR: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "rerank_last_llm_error", default=None
)


def _default_llm_call(prompt: str, config: LLMConfig | None) -> str | None:
    """用现有 provider 通道跑一次原始 chat；失败/无 key → None（触发回退）。

    本名字是**补丁缝**（audit/降级测试的 monkeypatch 都打在这里）——rerank 的默认通道
    必须经它调用，替身才生效；错误串走 _LAST_LLM_ERROR 旁路，不改动本函数的返回契约。"""
    text, error = _default_llm_call_with_error(prompt, config)
    _LAST_LLM_ERROR.set(error)
    return text


def _default_llm_call_capture_error(prompt: str, config: LLMConfig | None) -> "tuple[str | None, str | None]":
    """默认通道的调用入口（rerank 专用）：经补丁缝 _default_llm_call 取文本，
    同时把真身写下的错误串一并读出。调用前清槽，缝上替身时错误恒 None。"""
    _LAST_LLM_ERROR.set(None)
    text = _default_llm_call(prompt, config)
    return text, _LAST_LLM_ERROR.get()


# 运行期异常留痕（与 vector_recall._warn_once 同款纪律）：宽 except 兜底绝不打断请求，
# 但异常本体必须至少留一行 stderr——否则「重排静默失效」事后无从归因（2026-08-15 。
# _WARNED 集合与判定已收编进 vector_recall._warn_once_prefixed（三模块共享同一集合），
# 此处只留带 [rerank] 前缀的薄 wrapper；模块级名字 _WARNED 经 import 保留（测试靠它复位）。
def _warn_once(key: str, message: str) -> None:
    """同一原因只在 stderr 提示一次；绝不抛异常、绝不打断请求。"""
    _warn_once_prefixed("[rerank]", key, message)


def rerank_candidates(
    query: str,
    candidates: Sequence[RetrievedCandidate],
    backend: str = "off",
    top_k: int | None = None,
    config: LLMConfig | None = None,
    llm_call: Callable[[str], str | None] | None = None,
    *,
    trace: dict | None = None,
) -> list[RetrievedCandidate]:
    """唯一的可选重排通道。

    backend="off"（默认）→ 原样返回（截断到 top_k，如未传则不截）。
    backend="llm"       → LLM 编号排列 → 交集守卫 → 补回遗漏 → 截 top_k；任何问题回退原序。

    llm_call 可注入（便于单测）：签名 (prompt)->str|None。默认用 config 走真实通道。
    """
    items = list(candidates)
    started_at = time.perf_counter()

    def _duration_ms() -> int:
        return max(0, int(round((time.perf_counter() - started_at) * 1000)))

    def _mark(status: str, reason: str) -> None:
        if trace is not None:
            trace.update({"status": status, "reason": reason, "duration_ms": _duration_ms()})

    if trace is not None:
        trace.update({
            "backend": backend, "status": "skipped", "reason": "disabled",
            "candidate_count": len(items), "duration_ms": 0,
        })
    if not items or backend == "off" or backend not in RERANK_BACKENDS:
        if trace is not None and not items:
            trace.update({"status": "skipped", "reason": "no_candidates", "duration_ms": _duration_ms()})
        return items[:top_k] if top_k is not None else items

    # backend == "llm"
    order_text: str | None = None
    llm_error: str | None = None   # provider 错误串：仅默认通道带回（注入的 llm_call 没有错误面）
    try:
        prompt = build_rerank_prompt(query, items)
        if llm_call is not None:
            order_text = llm_call(prompt)
        else:
            # 默认通道经补丁缝 _default_llm_call（测试替身可注入），
            # 错误串走 _LAST_LLM_ERROR 旁路带出（替身没有错误面 → None → 临时故障档）。
            order_text, llm_error = _default_llm_call_capture_error(prompt, config)
    except Exception as exc:
        order_text = None
        _warn_once(f"rerank_exc::{type(exc).__name__}", f"LLM 重排主调用异常，回退原序：{exc!r}")

    if not order_text:
        # 回退原序（fail-open）。
        # **「没配」与「配了但没成」必须分成两个 reason**：前者对用户是「未启用」（如实，不必当故障报），
        # 后者是真故障（模型名被服务端拒、超时、限流、返回空…）。把两者合成一个 reason 的代价实测过：
        # provider 连着几天返 400，用户看到的摘要却写着「AI 重排本次未启用」——谁都看不出它坏了。
        # 判据是「这一次到底有没有真去调」：注入了 llm_call 就是调用方自带 provider（视为已调用）；
        # 否则看 config 里有没有真 key（load_llm_config 已把 placeholder 脱敏成 None）。
        # 真故障再分两档（2026-08-04 C3）：401/403=密钥无效/无权（llm_auth_failed，重试不自愈，
        # 指路去改设置）；超时/5xx/空回=临时故障（llm_call_failed，稍后重试即可）。
        attempted = llm_call is not None or bool(config is not None and getattr(config, "api_key", None))
        if not attempted:
            _mark("fallback", "llm_not_configured")
        else:
            _mark("fallback", "llm_auth_failed" if is_auth_error(llm_error) else "llm_call_failed")
        return items[:top_k] if top_k is not None else items

    # B3：非空但机械结构无效的输出 → 错误回灌重问一次；
    # 仍败则拿着首答原文走下方既有宽容解析/回退，行为与历史一致。
    _retry_caller = llm_call if llm_call is not None else (lambda p: _default_llm_call(p, config))
    order_text = _maybe_retry_parse(
        order_text, prompt, caller=_retry_caller,
        validate=lambda t: _grade_order_text(t, len(items)),
        contract_zh=(f"只输出一个 JSON 整数数组，包含 1 到 {len(items)} 的全部编号、"
                     "不重复、不新增，按相关性从高到低排列。"),
        channel="rerank")
    _parse_retry = _parse_retry_take("rerank")
    if _parse_retry and trace is not None:
        trace["parse_retry"] = _parse_retry   # 附加字段：不覆盖既有 status/reason

    order = parse_order(order_text, len(items))

    if not order:
        _mark("fallback", "invalid_order")
        return items[:top_k] if top_k is not None else items

    reordered = apply_order(order, items)
    _mark("used", "completed")
    return reordered[:top_k] if top_k is not None else reordered
