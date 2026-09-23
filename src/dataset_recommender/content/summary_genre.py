# -*- coding: utf-8 -*-
"""描述字段的**体裁判定** —— 「这段 description 到底是什么东西」。纯函数、确定性、离线。

## 为什么需要它

`introduction.build_dataset_introduction` 的逻辑是 `if description: summary = description`。
但「description」在五个源里**根本不是同一种东西**（全库验证）：

| 源 | description 实际是什么 | 剥脚手架后残余 |
|---|---|---|
| ArrayExpress 1786 | `编号 · assay · ` + **真散文摘要** | 中位 401 字符 |
| Human Cell Atlas 532 | **真散文摘要**（其中 313 条中部截断） | 中位 470 |
| CELLxGENE 2198 | `<论文标题> · DOI: x` —— **是标题，不是摘要** | 中位 80 |
| EBI SCEA 384 | `编号 · Baseline · 因子: organism part, …` —— **因子清单** | 中位 41 |
| 10x base 767 | ingest 自拼模板（`…contains X material, from Y tissue.`） | 0 |

把这五种东西一律当「摘要」直接显示，就是介绍页的实况：SCEA 用户看到的「摘要」是
`E-ANND-1 · Baseline · 因子: organism part, sampling site, …`。**那不是摘要，
只是恰好落在 description 字段里的字符串。**

## 判定依据是**来源**，不是字符串形状（这是本模块的设计核心）

第一版我按字符串形状写正则猜体裁，实测当场打脸：base 的 ingest 模板有 618 条被判成
`prose`（残余里漏出 `preservation method: Fresh Frozen. Analysis 2.0…`），因为我手写的
模板正则只覆盖了我看过的那两个样例。这是我在本语料上第三次犯同一个方法论错误
（前两次：体裁普查数错、`_tech_phrase` 抽样全是 10x）。

**正解**：description 长什么样，取决于**哪个 ingest 脚本写的**——而那些脚本就在
`scripts/ingest_*.py`，是可读的事实，不是需要猜的模式。所以按 `source` 判定，
字符串形状**只**用来剥前后缀与检测截断。这与 `provenance.py`「按源 + 读 ingest 原文」
是同一个范式：**能读到事实就别猜**。

来源→体裁的依据（逐条读 ingest 脚本得出，非推测）：
  - `ingest_arrayexpress.py`：description = `{accession} · {assay} · {study description}`
    → 剥前缀后是来源自己的研究描述 = 真散文。
  - `ingest_hca.py`：description = DCP 的 project description 原文 = 真散文。
  - `ingest_cellxgene.py`：description = `{collection name} · DOI: {doi}`
    → collection name 是**论文标题**，不是数据集摘要。
  - `ingest_ebi_scea.py`：description = `{accession} · {kind} · 因子: {factor list}`
    → 因子名清单，不是自然语言。
  - `corpus_curation._zenodo_to_record`：description = 官方 metadata.description 剥 HTML → 真散文。
  - `corpus_curation._refinebio_to_record`：description = 上游实验描述原文 → 真散文。
  - base（10x）：description 由索引器按字段拼装 → 没有任何来源叙述。
  - 其它（用户上传、缺 source 字段的记录）：**未知** → 保守处理，不假定是散文。

## 截断检测的坑（我踩过）

**必须用字面 `…` 字符全串搜索，不能用 `endswith`**：实测 HCA 有 **313 条中部截断**
（`endswith("…")` 对它们 100% 失明），AE+HCA 合计 1688 条被截断。把中部截断的半句话
喂给「忠实翻译」，产出就是半句中文，而用户看不出它被截断过。
"""
from __future__ import annotations

import re
from typing import Any

from ..corpus import provenance
from .introduction import meaningful_metadata_text

# ---------------------------------------------------------------- 体裁常量

#: 真散文摘要（AE 剥前缀后 / HCA 原文）。值得忠实翻译。
GENRE_PROSE = "prose"
#: 只有论文标题（CELLxGENE 的 `<title> · DOI:`）。标题可译，但**它不是摘要**。
GENRE_TITLE = "title"
#: 因子/字段名清单（SCEA）。不是自然语言，翻译无意义。
GENRE_FACTOR_LIST = "factor_list"
#: ingest 自拼的模板（base）。没有任何来源叙述。
GENRE_SCAFFOLD = "scaffold"
#: 压根没有 description。
GENRE_ABSENT = "absent"
#: 来源不在已知的五个 ingest 之内（用户上传）→ 不假定体裁，保守处理。
GENRE_UNKNOWN = "unknown"

#: 有「可翻译的自然语言」的体裁。其余只能据结构化字段重写。
TRANSLATABLE = (GENRE_PROSE, GENRE_TITLE)

# ---------------------------------------------------------------- 脚手架剥离
#
# 只剥**已读过 ingest 原文、确知存在**的前后缀；不做防御性猜测。

#: `E-MTAB-11814 · RNA-seq of coding RNA from single cells · ` —— AE 前缀（实测 1784/1786）。
_ACCESSION_PREFIX = re.compile(r"^\s*(?:E-\w+-\w+|GSE\d+)\s*·\s*[^·]{0,90}·\s*")
#: ` · DOI: 10.1038/...` —— CELLxGENE 后缀（实测 2155/2198）。
_DOI_SUFFIX = re.compile(r"\s*·\s*DOI:\s*\S+\s*$")

#: 剥完之后短于此 → 认为来源没给出可用的自然语言。
_MIN_USABLE_CHARS = 12


def is_truncated(description: str) -> bool:
    """来源文本是否被截断（含**中部**截断）。

    用字面 `…` 全串搜索，**不用 `endswith`**：HCA 实测 313 条是中部截断，
    `endswith("…")` 对它们完全失明。截断必须传给 LLM 层，否则「忠实翻译」会把半句话
    译成半句中文，而用户看不出来。
    """
    return "…" in meaningful_metadata_text(description)


def _genre_for_source(source: str) -> str:
    """来源 → 体裁。依据是各 ingest 脚本/采集管线**写死的拼装格式**（读过原文，非推测）。"""
    src = meaningful_metadata_text(source)
    if not src:
        # 无 source 的记录此前静默默认按 10x（→ scaffold）处理，
        # 真实描述被当 ingest 模板整段丢弃（展示与翻译双失明）。未知来源就该是
        # UNKNOWN：文本原样保留供展示，但不进「忠实翻译」。
        return GENRE_UNKNOWN
    if src == provenance.SOURCE_ARRAYEXPRESS or src == provenance.SOURCE_HCA:
        return GENRE_PROSE
    # GEO 的 description 是官方 ESummary 的 submitter summary（逐条对照缓存原文核验）；
    # SCP 的是 study 自带 full_description 剥 HTML——两者都是来源真实叙述 = 真散文。
    # HuBMAP 的 description 体裁未经同等级核验，保守留在 UNKNOWN。
    if src in (provenance.SOURCE_GEO, provenance.SOURCE_SCP):
        return GENRE_PROSE
    # Zenodo 的 description 是官方 metadata.description 剥 HTML
    # （corpus_curation._zenodo_to_record）；refine.bio 的是上游实验描述原文
    # （corpus_curation._refinebio_to_record）——两者都是来源真实叙述 = 真散文。
    if src in (provenance.SOURCE_ZENODO, provenance.SOURCE_REFINEBIO):
        return GENRE_PROSE
    if src == provenance.SOURCE_CELLXGENE:
        return GENRE_TITLE
    if src == provenance.SOURCE_SCEA:
        return GENRE_FACTOR_LIST
    if src == provenance.SOURCE_10X:
        return GENRE_SCAFFOLD
    return GENRE_UNKNOWN


def strip_scaffolding(description: str, source: str) -> str:
    """剥掉**该来源的 ingest 已知会拼上去的**前后缀，返回来源真正提供的叙述。

    体裁不是整串的属性：AE 是「模板前缀 + 真散文」，剥掉前缀才看得见那 401 字符的摘要。
    只对已知格式的源动手；未知源原样返回（不猜）。
    """
    text = meaningful_metadata_text(description)
    if not text:
        return ""
    # 不再把缺 source 静默默认成 10x——认不出的来源一律不剥（不猜）。
    src = meaningful_metadata_text(source)
    if src in (provenance.SOURCE_ARRAYEXPRESS, provenance.SOURCE_SCEA):
        text = _ACCESSION_PREFIX.sub("", text)
    elif src == provenance.SOURCE_CELLXGENE:
        text = _DOI_SUFFIX.sub("", text)
    # 只剥分隔符类脚手架残渣，**不剥句号** —— 句号是散文的一部分，
    # 剥掉会把 "…prostate cancer." 变成 "…prostate cancer"（测试当场抓到）。
    return text.strip(" ,;·　")


def classify(item: dict[str, Any]) -> dict[str, Any]:
    """判定这条记录的 description 体裁。

    返回 `{genre, residual, truncated, translatable}`：
      - `genre`：GENRE_* 之一，**由 source 决定**（见模块头「判定依据是来源」）
      - `residual`：来源真正提供的叙述。**LLM 层只该拿这个，不拿原串** ——
        `scaffold` / `factor_list` 档恒为空串，结构上不可能把 ingest 模板喂进「翻译」
      - `truncated`：来源文本是否被截断（含中部截断）
      - `translatable`：是否有值得翻译的自然语言
    """
    description = meaningful_metadata_text(item.get("description"))
    if not description:
        return _verdict(GENRE_ABSENT, "", False)

    truncated = is_truncated(description)
    genre = _genre_for_source(str(item.get("source") or ""))

    # scaffold / factor_list：我们**知道**来源没提供叙述 —— base 的 description 是索引器
    # 自己拼的，SCEA 的是字段名清单。残余置空，结构上杜绝把机器拼装喂进「忠实翻译」。
    if genre in (GENRE_FACTOR_LIST, GENRE_SCAFFOLD):
        return _verdict(genre, "", truncated)

    if genre == GENRE_UNKNOWN:
        # 「不知道它是什么」≠「它什么都不是」。用户上传的记录可能带真实描述，
        # 丢掉它就是丢掉真信息——比现状更糟。原样保留供**展示**，
        # 但 translatable=False：形状未知，不能拿去做「忠实翻译」。
        return _verdict(genre, description, truncated)

    residual = strip_scaffolding(description, str(item.get("source") or ""))
    if len(residual) < _MIN_USABLE_CHARS:
        # 该源按契约该有叙述，但这一条实际没有（如 AE 的 `· TBC`）→ 降级，不硬凑。
        return _verdict(GENRE_SCAFFOLD, "", truncated)
    return _verdict(genre, residual, truncated)


def _verdict(genre: str, residual: str, truncated: bool) -> dict[str, Any]:
    """`residual` 是**可展示**的来源叙述；`translatable` 是**可否喂 LLM 翻译**。

    两者是不同的问题，第一版把它们混成一个字段，结果是：未知来源（用户上传）的真实描述
    被整段丢弃，展示成「该数据集的公开字段显示：物种为 Human。」—— 丢真信息，比现状更糟。
    """
    return {
        "genre": genre,
        "residual": residual,
        "truncated": truncated,
        "translatable": genre in TRANSLATABLE and bool(residual),
    }
