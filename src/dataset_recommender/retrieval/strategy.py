# -*- coding: utf-8 -*-
"""查询排序难度分类器（确定性、纯函数）——按候选压力与语义信息量自动选后端。

动机（量化验证 标定）：recall/rerank 只**重排存活集**（不改命中集合）。
- 存活集**小**（紧约束，如「Xenium 的人类肺癌」→ 数条）：确定性词面序近乎全返，语义重排无杠杆，
  且 LLM 重排非确定性 → 只会添噪/添险。**质量优先 = 别动**。
- 存活集**大**（宽查询，如「单细胞数据」→ 582 条、「人类的数据」→ 数千条）：中文宽查询 `free_text_terms`
  为空 → `retriever._rank_score` 退化成「完整度+新鲜度+样本量」的**通用序**、与查询语义无关 → 语义层杠杆最大。

因此本分类器先看候选相对 top_k 的压力，再看规则约束之外是否还有足够自由语义。优先用**确定性**的
本地 cross_encoder；只有语义信息丰富、候选压力高且调用方明确允许 LLM 时，才在本地全池排序之后
叠加有限候选池的 LLM 重排。分类器本身无 I/O、无副作用、给定同输入恒定 → 可单测、可复现。

**opt-in**：只有调用方显式传 `strategy="auto"` 才生效；默认 "fixed" = 调用方给什么后端就用什么后端
（现行行为逐位不变）。官方评测直调 `retriever.retrieve`、根本不经过本模块 → 冻结门结构性不受影响。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .normalizer import DatasetRecord
from .query_parser import PS_EXECUTABLE, QueryIntent
from .retriever import passes_hard_filter
from ..domain.registry import domain_dimensions

STRATEGY_MODES = ("fixed", "auto")

# 分档阈值（存活集条数）。precise 上界与 top_k 取大（top_k 很大时「全返」区间也变大）。
PRECISE_MAX = 8
BROAD_MIN = 30


@dataclass(frozen=True)
class StrategyDecision:
    """一次分类结果（只读投影，供 workflow 挂进 WorkflowResult / MCP meta / 前端回显）。"""
    tier: str            # abstain | empty | precise | medium | broad | complex
    recall_backend: str  # off | cross_encoder | dense | vector
    rerank_backend: str  # off | llm
    reason: str          # 人类可读：为什么这么选
    signals: dict        # 可观测信号：n_survivors / n_pos_constraints / parse_status / 能力旗标

    def as_dict(self) -> dict:
        return {
            "mode": "auto",
            "tier": self.tier,
            "recall_backend": self.recall_backend,
            "rerank_backend": self.rerank_backend,
            "reason": self.reason,
            "signals": dict(self.signals),
        }


def count_survivors(records: Sequence[DatasetRecord], intent: QueryIntent) -> int:
    """存活集大小 = 过硬过滤的记录数。非可执行/弃权 → 0（与 retriever 短路一致）。一次线性扫描，廉价。"""
    if intent.parse_status != PS_EXECUTABLE or intent.abstain:
        return 0
    return sum(1 for r in records if passes_hard_filter(r, intent))


def classify_strategy(
    intent: QueryIntent,
    records: Sequence[DatasetRecord],
    *,
    recall_available: bool = False,
    llm_available: bool = False,
    preferred_recall: str = "cross_encoder",
    top_k: int | None = None,
    n_survivors: int | None = None,
    prefer_hybrid: bool = True,
) -> StrategyDecision:
    """确定性策略分类。纯函数：无 I/O、无副作用、给定同输入恒定。

    参数：
      recall_available   —— 语义重排后端是否可执行，调用方按环境语义传：MCP 传「已预热」
                            （recall_backend_ready）、Web/CLI 传「可执行」（recall_backend_available——
                            本地可加载或 API 数据源就绪）。
      llm_available      —— 是否同时具备可用 LLM 配置与调用方联网授权。MCP 默认传 False，只有显式
                            auto_allow_llm 才可传 True，避免 strategy=auto 意外联网。
      preferred_recall   —— 首选语义重排后端（默认 cross_encoder；可选 dense/vector）。
      top_k / n_survivors—— top_k 参与 precise 上界；n_survivors 可外部注入（避免重复扫描）。
    """
    n_pos = sum(1 for d in domain_dimensions() if intent.constraints.get(d))
    semantic_terms = sorted({str(t).strip().lower() for t in intent.free_text_terms if str(t).strip()})
    if n_survivors is None:
        n_survivors = count_survivors(records, intent)
    precise_cutoff = max(PRECISE_MAX, top_k or 0)

    effective_top_k = max(1, top_k or 10)
    candidate_pressure = round(n_survivors / effective_top_k, 3)
    signals = {
        "n_survivors": n_survivors,
        "n_pos_constraints": n_pos,
        "n_semantic_terms": len(semantic_terms),
        "semantic_terms": semantic_terms,
        "candidate_pressure": candidate_pressure,
        "parse_status": intent.parse_status,
        "recall_available": bool(recall_available),
        "llm_available": bool(llm_available),
        "preferred_recall": preferred_recall,
        "precise_cutoff": precise_cutoff,
        "broad_min": BROAD_MIN,
        "prefer_hybrid": bool(prefer_hybrid),
    }

    def off(tier: str, reason: str) -> StrategyDecision:
        return StrategyDecision(tier, "off", "off", reason, signals)

    # 非可执行（弃权/澄清）→ 无候选可排。
    if intent.parse_status != PS_EXECUTABLE or intent.abstain:
        return off("abstain", "查询非可执行（fail-closed 弃权 / 需澄清），无候选可重排 → 全 off。")
    if n_survivors == 0:
        return off("empty", "存活集为空（0 结果）→ 无候选可重排，全 off。")
    if n_survivors <= precise_cutoff:
        # 刻意维持现状的取舍：precise 档「全返即不重排」对**非确定性**后端（LLM）
        # 无疑——存活集小、语义重排既无杠杆又添非确定性风险。但对**确定性** cross_encoder，理论上仍可能
        # 改善这几条的 Top-1 顺序（当自由语义词被约束吃光时，词面序退化成通用完整度/新鲜度序、与查询语义
        # 无关）。是否放开须先在独立标注集上验证不损 Top-1；官方评测门直调 retriever.retrieve、
        # 结构性绕开本分类器，**无法**覆盖该路径，故在产出该证据前维持 off/off。
        # 放开时应仅对确定性后端、且分档改用已算好的 candidate_pressure（而非绝对条数）。
        return off(
            "precise",
            f"存活集小（{n_survivors} ≤ {precise_cutoff}）：确定性词面序近乎全返，"
            "语义重排无杠杆；不引非确定性重排以免添噪/添险。",
        )

    # “复杂”必须同时有排序杠杆和语义信息；只有候选很多、但原句没有自由偏好时，LLM 没有额外
    # 依据可用，不应为了“宽”而浪费调用。两项以上自由语义 + 宽候选池才进入双层路径。
    is_complex = n_survivors >= BROAD_MIN and len(semantic_terms) >= 2
    tier = "complex" if is_complex else ("broad" if n_survivors >= BROAD_MIN else "medium")

    # 最复杂：确定性语义重排先排全存活集，LLM 再处理去重后的有限池。两层都只重排存活集，
    # 不扩大候选集合；任一后端失败仍由各自 fail-open 合同回退。
    if is_complex and prefer_hybrid and recall_available and llm_available \
            and preferred_recall in ("cross_encoder", "dense", "vector"):
        return StrategyDecision(
            tier, preferred_recall, "llm",
            f"候选压力高（{n_survivors}/{effective_top_k}）且包含 {len(semantic_terms)} 个自由语义词："
            f"先用 {preferred_recall} 语义重排排全池，再用 LLM 精排有限候选池。",
            signals,
        )

    # 其次：有可用语义后端时用确定性语义重排（确定性、可复现）。
    if recall_available and preferred_recall in ("cross_encoder", "dense", "vector"):
        return StrategyDecision(
            tier, preferred_recall, "off",
            f"存活集较大（{n_survivors}），确定性词面序信号弱 → 叠加 {preferred_recall} "
            "语义重排（确定性、可复现）。",
            signals,
        )
    # 语义重排后端不可用 + 有明确自由语义 + 已授权 LLM → 用 LLM 单层重排。纯结构化宽查询不调用 LLM。
    if llm_available and semantic_terms and tier in ("medium", "broad", "complex"):
        return StrategyDecision(
            tier, "off", "llm",
            f"存在 {len(semantic_terms)} 个自由语义词且语义重排后端不可用 → 使用已授权的 LLM 重排"
            "（需联网、非确定性）。",
            signals,
        )
    return off(
        tier,
        f"存活集较大（{n_survivors}）但无可用且获准的语义后端，或原句没有额外排序偏好 → 保持确定性词面序。",
    )
