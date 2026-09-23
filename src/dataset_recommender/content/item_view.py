# -*- coding: utf-8 -*-
"""归一化记录 → 展示层 item 的**单一真源**。确定性、离线、只读。

## 为什么这个模块必须存在

`webapp._web_item_from_record` 与 `mcp_server._browse_item` 曾是**两份手抄的平行投影**。
MCP 那份的 docstring 白纸黑字写着「webapp._web_item_from_record 的 MCP 侧同口径投影」——
但它们**并不同口径**，而且没有任何测试能发现这一点。

2026-07-17 实测到的后果：修 `_web_item_from_record` 漏传 `modality` 时，Web 的 `/api/fair`
拿回了正确措辞（single-cell 3456 / spatial 894），而 **MCP 的 `assess_dataset_fair` 仍然
5667/5667 印泛泛的 "dataset"** —— 同一个 bug 修了一半。`collection_doi` 同理：Web 能印
DOI 句，MCP 永远印不出。

这正是审计反复抓到的那个模式（`.gitignore` / `.deliveryignore` 两份手抄从未对账 →
交付把密钥打进 ZIP）：**两份手抄的东西，迟早会漂移，而且漂移时没人知道**。
所以这里不是「顺手重构」，是把「Web 与 MCP 口径一致」从**叮嘱**变成**结构**：
只有一处 `build_item`，想漂移都没地方漂。

## 边界

只重排已有字段、不做领域推断、不联网、不写盘。`include_introduction=True` 时才现算介绍
（大列表不携带，避免 N 次拼装）。list 级展示辅助（来源交错 `interleave_by_source`、
分面计数投影 `facet_list`）同住本模块，同样 Web 与 MCP 共用一份。本模块被
`webapp` / `mcp_server` 消费；`retriever` / `workflow` / `evaluate_recommendation` **从不 import 它**。
"""
from __future__ import annotations

import re
from typing import Any

from ..corpus import reachability
from ..corpus.corpus import source_of
from ..corpus.downloads import file_count, primary_url
from .introduction import build_dataset_introduction

#: 发表年份的**单一真源**正则。此前 webapp、mcp_server、前端 browse.js 各有一份；
#: 前端那份已在 A2 收口（改读后端下发的 published_year），这里收口后端两份。
_YEAR_RE = re.compile(r"^(19|20)\d{2}")


def published_year(published_date: str) -> int | None:
    """把 published_date 解析成发表年份（int）或 None。

    年份分面/时间线过滤的单一真源：前端 `browse.js` 不再独立跑一份正则，直接读本字段（A2）。
    """
    match = _YEAR_RE.match((published_date or "").strip())
    return int(match.group(0)) if match else None


def build_item(record: Any, include_introduction: bool = False) -> dict[str, Any]:
    """归一化记录 → 展示层 item。**Web 与 MCP 共用这一个函数，不许再各抄一份。**

    `include_introduction=True` 才附带描述类字段并现算介绍——大列表不重复携带。
    """
    raw = record.raw if isinstance(record.raw, dict) else {}
    uid = raw.get("dataset_uid")
    try:
        raw_n_files = max(0, int(raw.get("n_files") or 0))
    except (TypeError, ValueError):
        raw_n_files = 0
    published_date = str(raw.get("published_date") or "").strip()
    item: dict[str, Any] = {
        "dataset_name": record.dataset_name,
        "species": record.species,
        "tissue": record.tissue,
        "disease": record.disease,
        "platform": (record.platform_family or "").strip(),
        "assay": record.assay,
        "chemistry": record.chemistry,
        "has_raw_data": record.has_raw_data,
        "count": record.count,
        "unit": record.unit,
        # 检测基因数（10x 平台信息补充旁挂表；additive 展示字段，无补充时为 ""）。
        # getattr 容错：归一化记录之外的轻量替身（测试命名空间等）可能没有这个属性。
        "gene_count": getattr(record, "gene_count", ""),
        "source": str(raw.get("source") or "").strip() or "10x Genomics",
        "url": record.url,
        "download_url": primary_url(uid) or str(raw.get("download_url") or "") or record.url,
        "dataset_uid": uid or "",
        "n_files": file_count(uid) or raw_n_files,
        # N11 国内可达性启发（按下载 host 推断、非实测速度；见 reachability 模块的诚实边界）。
        "reachability": reachability.classify(primary_url(uid) or str(raw.get("download_url") or "") or record.url),
        "family_id": record.family_id,
        "published_date": published_date,
        # modality 是领域包声明的一等维度、由 domains/biodata/vocabulary.derive_modality 单一真源算好、
        # retriever 正常消费——**唯独展示层漏传**，于是 fair._tech_phrase 的 spatial/single-cell
        # 两条分支从上线至今一次没执行过：全库 5667/5667 的投稿声明都退化成泛泛的 "dataset"，
        # 一个单细胞数据集目录从没说过 "single-cell"。典型的「生产者有、消费者要、中间层丢了」。
        "modality": record.modality,
        # collection_doi / filesize 供 provenance 判定；二者都**不能**直接渲染：
        # collection_doi 是 collection 级、指向论文（见 provenance.collection_doi 的粒度警告）；
        # filesize 在 2702 条上硬编码 0（把「未知」编码成「0 字节」）→ 必须过 provenance.size_bytes_or_none。
        "collection_doi": str(raw.get("collection_doi") or "").strip(),
        "filesize": raw.get("filesize"),
        # A2 单一真源：后端解析好 published_year(int|null) 随记录下发；前端**过滤与柱计数都消费
        # 该字段**，不再各自维护一份年份正则（producer 改了 consumer 没改 → 柱计数与过滤静默背离）。
        "published_year": published_year(published_date),
    }
    if include_introduction:
        item.update(
            {
                "description": record.description,
                "preservation_method": raw.get("preservation_method", ""),
                "analysis_software": raw.get("analysis_software", ""),
                "software_version": raw.get("analysis_software_version", "") or raw.get("software_version", ""),
            }
        )
        item["introduction"] = build_dataset_introduction(item)
    return item


def facet_list(counter: dict[str, int]) -> list[dict[str, Any]]:
    # 高频在前、同频按名字，供前端筛选下拉
    ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"value": value, "count": count} for value, count in ordered]


def interleave_by_source(records: list[Any]) -> list[Any]:
    """按来源轮转交错，让浏览页首屏就同时看到各库（「并列」）。各库内部保持原顺序。"""
    from collections import OrderedDict

    buckets: "OrderedDict[str, list[Any]]" = OrderedDict()
    for r in records:
        buckets.setdefault(source_of(r), []).append(r)
    lists = [iter(v) for v in buckets.values()]
    out: list[Any] = []
    exhausted = 0
    while exhausted < len(lists):
        exhausted = 0
        for it in lists:
            nxt = next(it, None)
            if nxt is None:
                exhausted += 1
            else:
                out.append(nxt)
    return out
