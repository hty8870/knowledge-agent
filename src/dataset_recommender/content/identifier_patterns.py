# -*- coding: utf-8 -*-
"""标识符**形态识别**（纯词表/正则，零本包依赖的叶子模块）。

**为什么单独成模块**：`identifiers` 的反查（lookup）要装配卡片（item_view → introduction →
summary_genre → 稿件产物模块），因此 `identifiers` 永远进不了冻结 784 评测路径的 import 闭包；
而检索解析层（query_parser，在冻结路径上）也需要「这句话是不是一个标识符」的判定来做
裸标识符 fail-closed（普查 ：裸贴 DOI 曾被词面解析拆成数字残片 → 全库冒充结果）。
把**形态识别**沉到本叶子模块，两条路径共用同一份正则真源，闭包隔离不被破坏。

只读、确定性、离线；不 import 任何本包模块（结构性保证，测试门会守）。
"""
from __future__ import annotations

import re
from typing import Any

#: 本目录索引的 11 个来源（人读，用于 fail-closed 文案）。 接入 HuBMAP / Broad Single Cell Portal / NCBI GEO；
#: Zenodo 首批入库（通用仓储人工甄别切片，第 10 源）；
#: refine.bio 首批入库（GEO/SRA/AE 统一加工镜像的单细胞/空间切片，第 11 源）。
INDEXED_SOURCES_ZH = (
    "10x Genomics、CELLxGENE Discover、Human Cell Atlas、EBI SCEA、ArrayExpress、ENCODE、"
    "HuBMAP、Broad Single Cell Portal、NCBI GEO、Zenodo、refine.bio"
)

# —— 标识符类型识别（顺序即优先级：先判专属格式，再判宽泛 DOI）——
# 我们**索引**的：ArrayExpress/SCEA accession（E-XXXX-N）、ENCODE 实验 accession（ENCSR…）、
# CELLxGENE/HCA 的 UUID、collection DOI、GEO Series（GSE…，随 geo.json 收录）、
# HuBMAP 编号（HBM###.XXXX.###）、Single Cell Portal 编号（SCP###）。
_RE_AE_ACC = re.compile(r"\bE-[A-Z]{4}-\d+\b", re.IGNORECASE)
_RE_ENCSR = re.compile(r"\bENCSR[0-9A-Z]{6}\b", re.IGNORECASE)
_RE_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.IGNORECASE)
_RE_DOI = re.compile(r"\b10\.\d{4,9}/[^\s\"'<>]+\b")
_RE_GSE = re.compile(r"\bGSE\d{3,}\b", re.IGNORECASE)
_RE_HBM = re.compile(r"\bHBM\d{3,}(?:\.[A-Z0-9]+){2}\b", re.IGNORECASE)
_RE_SCP_ACC = re.compile(r"\bSCP\d+\b", re.IGNORECASE)
# 我们**不索引**的：GEO Sample（GSM，Sample 级不入库）、SRA / ENA / BioProject / BioSample。
_RE_GSM = re.compile(r"\bGSM\d{3,}\b", re.IGNORECASE)
_RE_SRA = re.compile(r"\b(SR[APRSXZ]\d{3,}|PRJ[EDN][A-Z]\d+|SAM[EDN][A-Z]?\d+|ER[RPXS]\d{3,}|DRR\d{3,})\b", re.IGNORECASE)

#: 裸贴 DOI 时最常一起复制上的解析器/协议前缀（https://doi.org/…、doi:…）。
#: classify 只认 DOI 本体；整句判等（query_parser 的裸标识符闸）若不先剥前缀，
#: 「https://doi.org/10.xxxx/yyy」会被词面解析拆成 https/doi/org 一堆「未收录词」，
#: 指路文案反过来教用户把 DOI 拆掉。
#: 前缀族与 identifiers._norm_doi 同形；identifiers 进不了冻结闭包（见模块 docstring），
#: 故正则沉在本叶子模块，两条路径共用这一份真源。
_RE_DOI_PREFIX = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi\.org/|doi:\s*)", re.IGNORECASE)


def strip_doi_prefix(text: str) -> str:
    """剥掉整句开头的一个 DOI 解析器前缀（只剥一次、只剥开头）。非 DOI 前缀输入原样返回。"""
    return _RE_DOI_PREFIX.sub("", (text or "").strip(), count=1)


_EXTERNAL_POINTERS = {
    "geo_sample": ("GEO Sample 编号", "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc="),
    "sra": ("SRA / ENA / BioProject 编号", "https://www.ncbi.nlm.nih.gov/sra/?term="),
}


def classify(text: str) -> dict[str, Any] | None:
    """识别 text 是否是一个（我们关心的）标识符。返回 {kind,value,indexed} 或 None（不是标识符）。

    `indexed`＝这类标识符是否落在本目录的 11 个来源内。GEO Sample(GSM)/SRA→False（结构性不索引）；
    GEO Series(GSE) 自 geo.json 随包收录起转为 True。
    """
    t = (text or "").strip()
    if not t:
        return None
    for kind, rx, indexed in (
        ("arrayexpress_accession", _RE_AE_ACC, True),
        ("geo", _RE_GSE, True),
        ("geo_sample", _RE_GSM, False),
        ("sra", _RE_SRA, False),
        ("hubmap_accession", _RE_HBM, True),
        ("scp_accession", _RE_SCP_ACC, True),
        ("cellxgene_uuid", _RE_UUID, True),
        ("encode_accession", _RE_ENCSR, True),
        ("doi", _RE_DOI, True),
    ):
        m = rx.search(t)
        if m:
            return {"kind": kind, "value": m.group(0), "indexed": indexed}
    return None
