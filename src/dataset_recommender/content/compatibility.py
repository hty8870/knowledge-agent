# -*- coding: utf-8 -*-
"""元数据兼容分组。给一个种子数据集，找**同物种 + 兼容 chemistry/platform** 的其它数据集。

**诚实边界（本模块的全部约束）**：这是**元数据分面**，**不是**「可整合」推荐。同物种 + 同 chemistry
是可整合的**必要非充分**条件——批次效应、注释体系、细胞类型定义、参考基因组版本、质控口径都可能让
实际整合不可行。所以本模块只回「元数据上兼容」并**始终附带这条 caveat**，绝不说「这些可以合并分析」。
（自审 6：越界成「可整合」= 把必要条件当充分条件，正是本项目在杀的病。）

只读、确定性、离线。不被检索/排序/评测路径调用。
"""
from __future__ import annotations

from typing import Any

from ..retrieval.normalizer import is_missing_value
from .item_view import build_item

CAVEAT_ZH = (
    "「元数据兼容」只表示物种一致、且 chemistry 或平台相同——这是可整合的**必要非充分**条件。"
    "实际能否整合还取决于批次效应、注释体系、细胞类型定义、参考基因组版本与质控口径，本工具不做这些判断。"
)


def _tokens(value: Any) -> set[str]:
    """多值字段（可能逗号分隔）→ 归一小写 token 集合；缺失哨兵 → 空集。"""
    text = str(value or "").strip()
    if not text or is_missing_value(text):
        return set()
    parts = [p.strip().lower() for chunk in text.split(",") for p in [chunk] if p.strip()]
    return {p for p in parts if p and not is_missing_value(p)}


def _one(value: Any) -> str:
    text = str(value or "").strip()
    return "" if (not text or is_missing_value(text)) else text.lower()


def find_compatible(seed_uid: str, records: Any, limit: int = 20) -> dict[str, Any] | None:
    """种子 uid + 语料 → 元数据兼容同伴。种子查不到 → None。

    兼容判据：**共享 ≥1 物种** 且（**chemistry 相同** 或 **platform 家族相同**）。
    """
    records = list(records)
    seed = None
    for r in records:
        raw = r.raw if isinstance(r.raw, dict) else {}
        if str(raw.get("dataset_uid") or "") == seed_uid:
            seed = r
            break
    if seed is None:
        return None

    seed_species = _tokens(seed.species)
    seed_chem = _one(seed.chemistry)
    seed_platform = _one(seed.platform_family)

    companions: list[dict[str, Any]] = []
    for r in records:
        raw = r.raw if isinstance(r.raw, dict) else {}
        if str(raw.get("dataset_uid") or "") == seed_uid:
            continue
        if not (seed_species and (_tokens(r.species) & seed_species)):
            continue
        chem = _one(r.chemistry)
        plat = _one(r.platform_family)
        same_chem = bool(seed_chem) and chem == seed_chem
        same_plat = bool(seed_platform) and plat == seed_platform
        if not (same_chem or same_plat):
            continue
        basis = []
        if same_chem:
            basis.append(f"chemistry={r.chemistry}")
        if same_plat:
            basis.append(f"platform={r.platform_family}")
        item = build_item(r, include_introduction=False)
        item["_compat_basis"] = "、".join(basis)
        companions.append(item)

    return {
        "seed": build_item(seed, include_introduction=False),
        "criteria": {
            "species": sorted(seed_species),
            "chemistry": seed.chemistry if seed_chem else "",
            "platform_family": seed.platform_family if seed_platform else "",
        },
        "compatible": companions[:limit],
        "total": len(companions),
        "caveat": CAVEAT_ZH,
    }
