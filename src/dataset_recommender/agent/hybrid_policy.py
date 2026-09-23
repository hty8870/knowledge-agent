"""Deterministic mixed-intent routing policy.

This module is deliberately free of LangGraph and model clients.  It owns the
mechanical clause splitter, action/search detectors and the capability ledger
projected into Agent state.  ``agent_exec`` only consumes the two public
functions and re-exports selected constants for compatibility tests.
"""
from __future__ import annotations

import re
from typing import Any

from . import action_plan as _ap
from ..retrieval import query_lexicon as _vocab


HYBRID_CURATE_NOUN_GATE = frozenset({"上传", "导入"})
HYBRID_ACTION_VERBS = tuple(
    marker for marker in _vocab.ACTION_VERBS if marker not in ("下载脚本", "下载链接"))
HYBRID_ACTION_VERB_TAIL_EXCL = {"下载": ("链接", "脚本")}

HYBRID_ACTION_FAMILIES: tuple[tuple[str, re.Pattern, tuple[str, ...], str], ...] = (
    ("action.check_updates",
     re.compile(r"(?:检查|核查|清查|盘点).{0,8}?更新|有没有更新|是否有更新|有更新吗"),
     ("curate.check_updates", "curate.sync_updates"), "检查库更新"),
    ("action.import",
     re.compile(r"入库(?!的)|进库(?!的)|纳入(?!的)|收录(?!的)|同步(?!化)|更新一下|更新下"),
     ("curate.sync_updates", "curate.search_online"), "同步/入库新数据"),
    ("action.search_online", re.compile(r"联网搜|在线搜|上网搜|网上搜"),
     ("curate.search_online",), "联网检索外部源"),
    ("action.db_status", re.compile(r"数据库状态|库的状态|库容|库.{0,4}?有?(?:多少|几)条"),
     ("curate.db_status",), "清点库容"),
)
HYBRID_ACTION_RES_EXTRA = tuple(re.compile(pattern) for pattern in (r"安装",))
HYBRID_ACTION_RES = tuple(pattern for _, pattern, _, _ in HYBRID_ACTION_FAMILIES) + HYBRID_ACTION_RES_EXTRA
HYBRID_SEARCH_VERB_RE = re.compile(
    r"找(?!回)|推荐|(?<!联网|在线|上网|网上)(?:搜索|检索)|"
    r"(?<!联网|在线|上网|网上)搜(?!来|回|索)")
HYBRID_IMPORT_CHAIN_RE = re.compile(r"入库|进库|纳入|收录")
HYBRID_HAS_DATA_RE = re.compile(r"有没有(.{0,12}?)数据(?!库)")
HYBRID_NEW_ONLY_RE = re.compile(r"最?新(?:的)?")
HYBRID_CLAUSE_SPLIT_RE = re.compile(r"然后|顺便|接着|并且|再|[，。；！？,.;!?]")
HYBRID_LEXICON_VERSION = "v3-2026-08-31"


def split_clauses(text: str) -> list[str]:
    return [part.strip() for part in HYBRID_CLAUSE_SPLIT_RE.split(str(text or "")) if part.strip()]


def action_hit(text: str) -> bool:
    low = text.lower()
    for marker in _vocab.CURATE_OP_MARKERS:
        if marker in low:
            if marker in HYBRID_CURATE_NOUN_GATE and _ap._action_verb_noun_usage(low, marker):
                continue
            return True
    for marker in HYBRID_ACTION_VERBS:
        if marker in low and not _ap._action_verb_noun_usage(low, marker):
            at = low.find(marker)
            if any(low[at + len(marker):].startswith(tail)
                   for tail in HYBRID_ACTION_VERB_TAIL_EXCL.get(marker, ())):
                continue
            return True
    return any(pattern.search(text) for pattern in HYBRID_ACTION_RES)


def search_hit(text: str) -> bool:
    if HYBRID_IMPORT_CHAIN_RE.search(text):
        return False
    if HYBRID_SEARCH_VERB_RE.search(text):
        return True
    match = HYBRID_HAS_DATA_RE.search(text)
    if match:
        gap = match.group(1).strip()
        if gap and "更新" not in gap and not HYBRID_NEW_ONLY_RE.fullmatch(gap):
            return True
    return False


def hybrid_intent_gate(text: str) -> bool:
    clauses = split_clauses(text)
    action_indexes = {i for i, clause in enumerate(clauses) if action_hit(clause)}
    search_indexes = {i for i, clause in enumerate(clauses) if search_hit(clause)}
    return bool(action_indexes and search_indexes
                and any(i != j for i in action_indexes for j in search_indexes))


def required_capabilities(text: str) -> list[dict[str, Any]]:
    capabilities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for clause in split_clauses(text):
        if action_hit(clause):
            family = next(((cap, verbs, label) for cap, pattern, verbs, label
                           in HYBRID_ACTION_FAMILIES if pattern.search(clause)), None)
            if family is None:
                family = ("action.generic", (), f"完成「{clause[:12]}」的操作")
            if family[0] not in seen:
                seen.add(family[0])
                capabilities.append({"capability": family[0], "verbs": list(family[1]),
                                     "label_zh": family[2], "anchor": clause[:24]})
        if search_hit(clause) and "search" not in seen:
            seen.add("search")
            capabilities.append({"capability": "search",
                                 "verbs": ["rank", "rerank", "search.rerun"],
                                 "label_zh": "在本地库检索数据", "anchor": clause[:24]})
    return capabilities
