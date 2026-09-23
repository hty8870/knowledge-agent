# -*- coding: utf-8 -*-
"""dream 记忆：基于历史对话整理出用户记忆（手动「整理记忆」按钮触发，LLM 护栏输出）。

定位与诚实语义（设计决定：手动触发 + 可以发给已配置的 LLM）：
- 素材是**前端从本机历史快照组织**的对话记录（query + chat），服务端零存储、无后台任务。
- LLM 配置随请求带来（与 /api/utterance 同契约：请求级 key 优先、绝不持久化）；
  无 key → `no_key`，前端如实提示去设置配置，**不静默退化成编造**。
- 封闭输出护栏（对齐 action_plan 哲学）：只接受 JSON 数组 `[{"text","summary","evidence"}]`；
  解析失败 / 结构不符 / 超界 → 该条丢弃，全丢 = 空清单（前端如实说「这次没整理出」），
  绝不允许模型随口一段散文变成「记忆」。
- 产出**一律是 AI 生成物**：返回 `generated: true`，前端徽标「AI 整理」，预览勾选后才写入，
  绝不冒充用户手写偏好。

2026-08-08 B6 调研六候选批——两道新机械闸（epub「持久记忆是第四致命要素」+「升级需两条
非失败轨迹支持」+ Mem0 对账思想；审核 修订：词法过滤只当纵深防御，不当安全门）：
1. **出处核验（证据门槛，真正的安全门）**：每条候选必须附 1~2 段逐字摘自对话原文的
   evidence；服务端归一化核验——每段 span 真实落在**单条消息**里（跨消息拼接的伪
   evidence 不算，E-03），且全部 span 合计覆盖 **≥2 段不同对话**（对话按内容 hash
   去重，防同一段重复提交凑数；k="sys" 系统消息不算证据面）。核验不过 → 丢弃记
   `dropped.evidence`。
2. **注入审查（纵深防御）**：候选 text 命中指令形态（「忽略/你现在是/系统提示/指令/
   prompt/system/ignore/不要返回」等元词与行为封禁词）→ 丢弃记 `dropped.injection`。
   它只是兜底——真正的防线是出处核验 + 封闭输出闸 + 人工预览勾选。
安全门语义不变：任何一道不过都**丢弃**（宁缺毋滥），不降级、不编造。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Callable

from .llm_client import (
    LLMConfig,
    _sanitize_provider_error,
    call_llm,
    load_llm_config,
    should_use_llm as _should_use_llm,
)

DREAM_MAX_ITEMS = 8            # 一次整理最多产出的记忆条数（宁缺毋滥）
DREAM_TEXT_MAX = 120           # 单条记忆正文上限（字）
DREAM_SUMMARY_MAX = 240        # 单条依据摘要上限（字）
DREAM_EVIDENCE_MAX = 120       # 单段 evidence 上限（字）
DREAM_EVIDENCE_SPANS = 2       # 每条最多证据段数
DREAM_MIN_SPAN_CHARS = 4       # 归一后短于它的片段不算有效证据（防「数据」这类随处可命中）
DREAM_MAX_CONVERSATIONS = 12   # 一次最多带入的对话段数
DREAM_CHAT_PER_CONV_MAX = 40   # 每段对话最多带入的消息条数（与前端 CB_LOG_MAX 对齐）
DREAM_CONV_CHAR_MAX = 1500     # 每段对话拼进 prompt 的字符预算

_PROMPT_HEAD = """你是一个「研究记忆整理员」。下面是一位生物医学数据检索用户的历史对话记录
（每段：当时的检索需求 + 对话过程）。请从中提炼**值得长期记住**的信息，写成记忆条目。

只提炼这几类：
1. 研究偏好：反复出现或被明确强调的方向（物种/组织/疾病/平台/数据类型偏好）；
2. 工作习惯：反复出现的操作意图（例如总要 FASTQ 原始数据、总要打包下载、关注某类来源）；
3. 明确说过的约束：排除什么、只要什么、时间范围等。

规则：
- 只写对话里**确实出现**的信息，不许推测、不许编造没有说过的偏好；
- 一次性、随口的话不写（只出现一次且无强调的不写）；
- 每条 text 是可直接回填检索框的一句需求或偏好陈述（不超过 60 字）；
- 每条 summary 用一句话写明依据（哪段对话、出现了几次）；
- 每条必须附 evidence：1~2 段**逐字摘自对话原文**的小片段（每段不超过 60 字）。
  系统会逐字核验：这些片段必须真的出现在对话里、且加起来至少覆盖**两段不同对话**；
  核验不过的条目会被直接丢弃——所以**照抄原文、不要改写、不要翻译**；
- 最多 8 条；没有值得写的就返回空数组。

输出**只有** JSON 数组，不要任何其它文字：
[{"text": "…", "summary": "…", "evidence": ["原文片段", "…"]}, …]

历史对话：
"""


class DreamError(Exception):
    """机器码 + 人读提示；接口层翻成 4xx/503。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _clip_conversations(conversations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """服务端入参收口（纵深防御：前端截断过一遍，这里只认白名单字段再截一遍）。"""
    out: list[dict[str, Any]] = []
    for conv in (conversations or [])[:DREAM_MAX_CONVERSATIONS]:
        if not isinstance(conv, dict):
            continue
        query = str(conv.get("query") or "").strip()[:300]
        chat: list[dict[str, str]] = []
        for msg in (conv.get("chat") or [])[:DREAM_CHAT_PER_CONV_MAX]:
            if not isinstance(msg, dict):
                continue
            text = str(msg.get("t") or "").strip()[:300]
            note = str(msg.get("n") or "").strip()[:300]
            kind = str(msg.get("k") or "")
            if text:
                chat.append({"k": kind, "t": text, "n": note})
        if query or chat:
            out.append({"query": query, "chat": chat})
    return out


def build_dream_prompt(conversations: list[dict[str, Any]]) -> str:
    """程序生成 prompt（action_plan 模式：模型只见到整理好的素材，不见原始自由文本指令位）。"""
    parts = [_PROMPT_HEAD]
    for i, conv in enumerate(_clip_conversations(conversations), 1):
        lines = [f"—— 第 {i} 段 ——"]
        if conv["query"]:
            lines.append(f"检索需求：{conv['query']}")
        budget = DREAM_CONV_CHAR_MAX - sum(len(x) for x in lines)
        for msg in conv["chat"]:
            who = {"say": "用户", "refine": "细化", "action": "用户(执行)", "sys": "系统"}.get(msg["k"], "用户")
            line = f"{who}：{msg['t']}" + (f"（{msg['n']}）" if msg["n"] else "")
            if budget - len(line) < 0:
                lines.append("……（本段后文从略）")
                break
            lines.append(line)
            budget -= len(line)
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def parse_dream_output(raw: str | None) -> list[dict[str, Any]]:
    """封闭输出解析：只认 JSON 数组 [{text, summary, evidence}]；坏条丢弃、全坏为空（绝不编造）。
    evidence 容忍模型给单字符串（收进列表）；每段截 DREAM_EVIDENCE_MAX、每条至多
    DREAM_EVIDENCE_SPANS 段、空段丢弃——**没有 evidence 的条目在这里不丢**（解析≠核验），
    由 `_evidence_coverage` 统一裁决（那样丢弃原因才可计数上报）。"""
    if not raw:
        return []
    text = str(raw).strip()
    fence = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end <= start:
            return []
        text = text[start:end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        t = str(item.get("text") or "").strip()[:DREAM_TEXT_MAX]
        s = str(item.get("summary") or "").strip()[:DREAM_SUMMARY_MAX]
        raw_ev = item.get("evidence")
        spans_in = raw_ev if isinstance(raw_ev, list) else ([raw_ev] if raw_ev else [])
        spans: list[str] = []
        for span in spans_in:
            span_text = str(span or "").strip()[:DREAM_EVIDENCE_MAX]
            if span_text and span_text not in spans:
                spans.append(span_text)
            if len(spans) >= DREAM_EVIDENCE_SPANS:
                break
        key = _norm(t)
        if not t or key in seen:
            continue
        seen.add(key)
        out.append({"text": t, "summary": s, "evidence": spans})
        if len(out) >= DREAM_MAX_ITEMS:
            break
    return out


def _default_llm_call(prompt: str, config: LLMConfig) -> str | None:
    result = call_llm(prompt, config)
    return result.text if result.succeeded else None


# ==================== B6 两道机械闸 ====================

#: 注入形态词表（纵深防御，不是安全门）：元词（系统/提示词/指令/ignore 等）+
#: 行为封禁（不要/不许 返回|显示|回答）。刻意**不收**「不要 mouse」这类排除偏好——
#: 那是合法记忆；只打「对系统行为下指令」的形态。
_INJECTION_RE = re.compile(
    r"(忽略|你现在是|你是(一个|名)?(AI|助手|模型)|系统提示|提示词|指令|jailbreak|override"
    r"|prompt|system|ignore|不要(返回|显示|回答)|不许(返回|显示|回答))",
    re.IGNORECASE)


def _looks_injection(text: str) -> bool:
    return bool(_INJECTION_RE.search(text or ""))


def _conversation_evidence_pools(conversations: list[dict[str, Any]]) -> list[tuple[int, str]]:
    """可引用原文面：**逐条消息一个 pool 单元**（query 一条、每条消息的 t/n 各一条，归一化
    文本），返回 (对话序号, 单条消息文本) 对——证据必须逐字摘自**单条消息**，「消息 A 尾 +
    消息 B 头」跨消息拼接的伪 evidence 不得通过（E-03：此前整段 join 成一个串，拼接可绕过）。
    对话仍按**内容 hash 去重**——同一段对话重复提交只算一段（防「复制同一段凑两段」绕过
    升级门槛）；k="sys" 的系统消息不算证据面。"""
    pools: list[tuple[int, str]] = []
    seen: set[str] = set()
    for conv_index, conv in enumerate(conversations):
        parts = [str(conv.get("query") or "")]
        for msg in conv.get("chat") or []:
            if not isinstance(msg, dict) or str(msg.get("k") or "") == "sys":
                continue
            parts.append(str(msg.get("t") or ""))
            parts.append(str(msg.get("n") or ""))
        units = [n for n in (_norm(p) for p in parts) if n]
        full = " ".join(units)
        if not full:
            continue
        digest = hashlib.sha256(full.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        pools.extend((conv_index, unit) for unit in units)
    return pools


def _evidence_coverage(spans: list[str], pools: list[tuple[int, str]]) -> int:
    """全部 span 合计覆盖的不同对话段数：每段 span 归一后**逐字**落在哪几段对话的**单条
    消息**里；短于 DREAM_MIN_SPAN_CHARS 的片段不算（防「数据」这类随处可命中的伪证据）。"""
    covered: set[int] = set()
    for span in spans:
        needle = _norm(span)
        if len(needle) < DREAM_MIN_SPAN_CHARS:
            continue
        for conv_index, unit in pools:
            if needle in unit:
                covered.add(conv_index)
    return len(covered)


def _apply_gates(memories: list[dict[str, Any]],
                 conversations: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """两道闸（注入审查 → 出处核验），被拦条目计数上报；任何一道不过都丢弃，不降级。"""
    pools = _conversation_evidence_pools(conversations)
    kept: list[dict[str, Any]] = []
    dropped = {"injection": 0, "evidence": 0}
    for mem in memories:
        if _looks_injection(mem["text"]) or _looks_injection(mem["summary"]):
            dropped["injection"] += 1
            continue
        if _evidence_coverage(mem.get("evidence") or [], pools) < 2:
            dropped["evidence"] += 1
            continue
        kept.append(mem)
    return kept, dropped


def dream_from_conversations(
    conversations: list[dict[str, Any]],
    *,
    config: LLMConfig | None = None,
    llm_call: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:
    """对话快照 → 记忆候选清单。`llm_call` 可注入（签名 `(prompt) -> str | None`），
    确定性测试据此完全避开网络（与 action_plan 同契约）。"""
    convs = _clip_conversations(conversations)
    if not convs:
        raise DreamError("empty_input", "还没有可以整理的历史对话。")
    if llm_call is None:
        try:
            cfg = config or load_llm_config()
        except Exception as exc:  # 配置加载异常也兜底（E-04：LLM_TIMEOUT 等非法数值不再炸成 500）
            # 经脱敏层（防万一异常文本里带凭据；与 intro/act 层 config_error 同款写法）。
            detail = _sanitize_provider_error(exc)[:80]
            raise DreamError("config_error", f"AI 配置读取失败（{detail}）——请检查环境变量配置后重试。") from exc
        # mock 短路纪律与 intro/act 层同款（E-02）：call_mock_llm 忽略 prompt、空 records 必败，
        # 放它走到底只会误报「没能连上 AI」。dream 不查 enable_llm（只认 key），故 require_enabled=False；
        # 该口径下否定原因只剩 mock_not_used / no_key 两档，下面逐字映射回 DreamError。
        ok, reason = _should_use_llm(cfg, require_enabled=False)
        if not ok:
            if reason == "mock_not_used":
                raise DreamError("mock_not_used", "当前是 mock 演示模式，不会真的调用 AI——请关闭 MOCK_LLM 并配好密钥后再整理记忆。")
            raise DreamError("no_key", "还没有配置 AI——先到「设置 → AI / API 配置」填好密钥，再来整理记忆。")
        llm_call = lambda prompt: _default_llm_call(prompt, cfg)  # noqa: E731
    raw = llm_call(build_dream_prompt(convs))
    if raw is None:
        raise DreamError("llm_failed", "这次整理没能连上 AI，请稍后再试。")
    memories = parse_dream_output(raw)
    memories, dropped = _apply_gates(memories, convs)
    return {"ok": True, "generated": True, "memories": memories, "count": len(memories),
            "dropped": dropped}
