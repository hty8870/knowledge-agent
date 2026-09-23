# -*- coding: utf-8 -*-
"""标识符精确反查 + 诚实 fail-closed。

**受众形态**：研究者脑子里是**论文 / DOI / 数据集编号**，不是我们的内部主键。所以入口保持
「贴一个标识符」的形状，底层做类型识别 + 精确解析。

**诚实边界（本模块存在的理由）**：我们索引 11 个来源（10x Genomics / CELLxGENE Discover /
Human Cell Atlas / EBI SCEA / ArrayExpress / ENCODE / HuBMAP / Broad Single Cell Portal / NCBI GEO /
Zenodo / refine.bio；HuBMAP/SCP/GEO 三者 接入，GEO 侧为 GSE 级试点切片，Zenodo 为
人工甄别首批，refine.bio 为 单细胞/空间镜像切片）。scRNA-seq 的**原始数据多在 SRA/ENA**，
GEO Sample（GSM）也不入库，而我们**不索引 GSM/SRA**。贴一个 SRA 号进来，与其静默返回 0
（让用户以为「搜过了、没有」），不如**如实说**：「这类编号不在收录范围、去来源库直接查」
——省掉用户「自己搜一遍才断定我们没有」。命中率被结构性锁死（SRA=0），文案绝不暗示能反查任意论文。

只读、确定性、离线。不被检索/排序/评测路径调用（api_recommend 只在**识别到标识符**时附加只读字段）。

**形态识别（classify 与正则词表）的单一真源在叶子模块 `identifier_patterns`**（零本包依赖）：
本模块从这里再导出，调用方无需改名。这样检索解析层（query_parser，冻结路径上）也能
import 形态判定，而不会把本模块的反查装配链（item_view → introduction → 稿件产物模块）
拖进冻结闭包（普查 修复时的结构性约束）。
"""
from __future__ import annotations

import re
from typing import Any, Callable

# 再导出：形态识别真源在 identifier_patterns（见模块 docstring）。
from .identifier_patterns import (  # noqa: F401
    INDEXED_SOURCES_ZH,
    _EXTERNAL_POINTERS,
    classify,
)


def _norm_doi(text: str) -> str:
    """DOI 比较键：去常见存储前缀 + casefold。**只用于判等**，展示一律用原文。

    子串匹配（`v in doi`）会让截断/手滑的残片（如 `10.1101/2021`）也拿到自信「直达」
    （验证）；等值匹配则照常容忍存储侧带 `https://doi.org/`、`doi:` 前缀。
    """
    d = (text or "").strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi.org/", "doi:"):
        if d.startswith(prefix):
            d = d[len(prefix):]
            break
    return d


def _uid_contains(uid: str, v: str) -> bool:
    """uid 里的**词边界子串**匹配：`scp1` 命中 `scp:scp1`，但**不**命中 `scp:scp10`/`scp:scp101`。

    裸 `v in uid` 会把数字尾缀编号的前缀当成命中——「SCP1」曾反查出 241 条候选
    （scp10/scp101/… 全被 `in` 吞进， 三源接入集成冒烟抓获；AE 的 E-MTAB-1/E-MTAB-11
    同源潜伏）。编号必须边界闭合，与 query_parser._alias_occurrences 同一个道理。"""
    if not v:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(v) + r"(?![a-z0-9])", uid) is not None


def _find_records(kind: str, value: str, records: Any) -> list:
    """在语料里精确定位，返回**全部**命中（可能多条）——调用方负责消歧，绝不静默任取第一条。

    doi 用「去前缀 + casefold 后等值」匹配 collection_doi / url 里嵌入的 doi.org 链接；
    accession/uuid 匹配 dataset_uid 的**词边界子串**（uid 里常带 `ae:` 之类前缀，等值会漏；
    边界要求见 `_uid_contains`），或 public_accession 等值（HuBMAP 的 uid 装平台 UUID，
    HBM 编号只在 public_accession 上）。
    """
    v = _norm_doi(value) if kind == "doi" else value.strip().casefold()
    hits = []
    for record in records:
        raw = record.raw if isinstance(getattr(record, "raw", None), dict) else (record if isinstance(record, dict) else {})
        uid = str(raw.get("dataset_uid") or "").casefold()
        pub = str(raw.get("public_accession") or "").casefold()
        alt = str(raw.get("alternate_accession") or "").casefold()
        if kind == "doi":
            if v and (v == _norm_doi(str(raw.get("collection_doi") or ""))
                      or v == _norm_doi(str(raw.get("url") or ""))):
                hits.append(record)
        elif v and (_uid_contains(uid, v) or (pub and v == pub) or (alt and v == alt)):
            # uid 词边界子串（uid 常带 `ae:` 前缀）；public_accession 等值——HuBMAP 的 uid 装的是
            # 平台 UUID，HBM 编号只在 public_accession 上（三源接入补）；
            # alternate_accession 等值——refine.bio 镜像条目的副号（主号 SRP 时的 GSE，
            # 第 11 源补；只认显式字段，不扫描述文本防误命中）。
            hits.append(record)
    return hits


def _candidate_projection(record: Any) -> dict[str, str]:
    """候选投影（与 corpus.locate_record 的 409 消歧同口径）：只含公开元数据，供如实列候选。"""
    raw = record.raw if isinstance(getattr(record, "raw", None), dict) else (record if isinstance(record, dict) else {})
    return {
        "dataset_uid": str(raw.get("dataset_uid") or ""),
        "dataset_name": str(getattr(record, "dataset_name", "") or ""),
        "source": str(raw.get("source") or ""),
        "url": str(getattr(record, "url", "") or ""),
    }


def lookup(text: str, load_records: Callable[[], Any]) -> dict[str, Any] | None:
    """标识符反查。`load_records` 是**惰性**取语料的零参函数——只有识别到「本目录应含」的标识符
    才真正装载语料（GEO/SRA 直接 fail-closed、不装载）。返回渲染用 dict，或 None（不是标识符）。
    """
    hit = classify(text)
    if hit is None:
        return None
    kind, value, indexed = hit["kind"], hit["value"], hit["indexed"]

    if not indexed:
        # 显式分支：未知类型如实报错，而不是被「else 一律当 SRA」静默误指路。
        pointer = _EXTERNAL_POINTERS.get(kind)
        if pointer is None:
            raise ValueError(f"未登记的外部标识符类型: {kind!r}")
        label, base = pointer
        return {
            "is_identifier": True,
            "kind": kind,
            "value": value,
            "indexed": False,
            "match": None,
            "external_url": base + value,
            "message": (
                f"{value} 是{label}，不在本目录收录范围内。本目录索引的 11 个来源是 {INDEXED_SOURCES_ZH}——"
                "GEO 侧只收录 Series（GSE）级条目，GSM/SRA/ENA 等 Sample 与原始 reads 编号不索引。"
                f"请到来源库直接查：{base}{value}。"
                "（如实告知，省去你先在本目录搜一遍才确定我们没有。）"
            ),
        }

    from .item_view import build_item

    hits = _find_records(kind, value, load_records())
    if len(hits) > 1:
        # 共享标识符（一篇论文挂多个数据集是单细胞领域常态，真实语料 246 组共享 DOI）：
        # 与 locate_record 的 409 消歧同口径——如实列出全部候选，绝不静默任取第一条
        # 还用单数「已直达」对其余 N−1 条零披露（验证）。
        candidates = [_candidate_projection(r) for r in hits]
        uids = "、".join(c["dataset_uid"] for c in candidates[:8])
        if len(candidates) > 8:
            uids += f" 等 {len(candidates)} 条"
        why = "（同一篇论文挂了多个数据集）" if kind == "doi" else ""
        return {
            "is_identifier": True,
            "kind": kind,
            "value": value,
            "indexed": True,
            "match": None,
            "candidates": candidates,
            "external_url": None,
            "message": (
                f"{value} 对应本目录 **{len(candidates)} 条**数据集{why}：{uids}。"
                "请用具体数据集编号查看对应条目——不替你任选一条。"
            ),
        }
    if hits:
        record = hits[0]
        item = build_item(record, include_introduction=True)
        return {
            "is_identifier": True,
            "kind": kind,
            "value": value,
            "indexed": True,
            "match": item,
            "external_url": None,
            "message": f"已在本目录直达该{_kind_zh(kind)}对应的数据集。",
        }
    return {
        "is_identifier": True,
        "kind": kind,
        "value": value,
        "indexed": True,
        "match": None,
        "external_url": None,
        "message": (
            f"{value} 看起来是{_kind_zh(kind)}，但未匹配本目录任何数据集——"
            "可能它属于我们尚未收录的数据集，或编号有出入。请核对编号，或到来源库确认。"
        ),
    }


def _kind_zh(kind: str) -> str:
    return {
        "arrayexpress_accession": " ArrayExpress/SCEA 编号",
        "cellxgene_uuid": " CELLxGENE/HCA 数据集 UUID",
        "encode_accession": " ENCODE 实验编号",
        "hubmap_accession": " HuBMAP 编号",
        "scp_accession": " Single Cell Portal 编号",
        "doi": "关联论文 DOI",
        "geo": " GEO Series 编号",
        "geo_sample": " GEO Sample 编号",
        "sra": " SRA 编号",
    }.get(kind, "标识符")
