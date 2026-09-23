# -*- coding: utf-8 -*-
"""「复用数据出处」清单 —— 一次把 N 个复用的公开数据集整理成投稿材料。确定性、离线、只读。

## 这是什么，不是什么

**是**：你在论文里复用了 N 个公开数据集，本模块把它们整理成
  1. 一段可放进 Data Availability 小节的**英文段落**（按来源汇总，指向补充表）
  2. 一张**补充表**（每个数据集一行：编号 / 链接 / 原始数据状态）
  3. 一份**待办清单**（本工具无法提供、必须你自己核实的事）

**不是** Data Availability Statement 的主句。主句讲的是**你自己产出**的数据去向
（"The data generated in this study have been deposited in GEO under GSExxxxx"）。
那句我们帮不上，也**永远不该**帮——它需要吃进用户的未发表工作。
这不是外部规则，是产品定义：本工具只管**别人已公开的数据**。

这条边界原本还会以一段中文（`BOUNDARY_ZH`）置顶呈现给用户。 按产品要求删除：
它是在**用户没问的时候辩解一件不会发生的事**——面板标题、英文段落、导出 Markdown 的
`## Reused public datasets` 已经把范围写死在「复用的公开数据」上，那段话只是把它又说了一遍。
边界本身没有松动：它是**结构性的**（keys-only 入参，见下），从来不靠这段文案兜着。

## 为什么是 keys-only 入参

调用方只传 `dataset_uid` 列表，服务端自己解析记录。**不接受**调用方传入数据集内容——
一旦开了「你把数据集描述贴进来」的口子，产品就有了吃进未发表工作的路径。
入参是键、不是内容，这条红线就是结构性的，而不是靠我记得。

## 诚实约束（本模块存在的全部理由）

判定一律走 `provenance`（「我们凭什么这么说」的单一真源）：
  - 没有真 accession 就**不写** accession（全库只有 2170/5667 = 38.3% 有）
  - 「没查过」（ArrayExpress 1786 条）**绝不**写成「没有原始数据」，改列进 gaps
  - 查不到的 uid 出**显式墓碑**（`unresolved`），绝不静默丢弃——静默少一行，
    用户会以为那个数据集已经写进去了
"""
from __future__ import annotations

from .labels import UNNAMED_DATASET

from collections import Counter, OrderedDict
from datetime import date
from typing import Any

from ..corpus import provenance
from .introduction import meaningful_metadata_text

#: **面向用户的运行期字符串一律不带 markdown 强调**（`**…**`）：所有消费方
#: （前端 escapeHtml / MCP 文本）都按纯文本呈现，集成验证过字面星号会原样显示成
#: 「本清单只覆盖你**复用的公开数据**」。中文强调用本仓库既有的「」约定。
#: （那句话本身已于 删除，见模块头；这条排版约束对其余所有运行期字符串仍然有效。）

#: 补充表的英文表头（导出 Markdown 用；顺序即列序）。
_TABLE_HEADERS_EN = ("Dataset", "Source", "Identifier", "Access URL", "Raw sequencing data")

_MAX_UIDS = 200


class ReusePackError(ValueError):
    """入参非法。带 `code` 供 HTTP / MCP 层映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sanitize_uids(raw: Any) -> list[str]:
    """入参归一：去空白、去重（保序）、限量。非法 → `ReusePackError`。

    保序是有意的：用户勾选的顺序就是他心里的顺序，产物里不该重排。
    """
    if raw is None or isinstance(raw, (str, bytes, dict)):
        raise ReusePackError("bad_param", "uids 必须是字符串数组（dataset_uid 列表）。")
    try:
        items = list(raw)
    except TypeError:
        raise ReusePackError("bad_param", "uids 必须是字符串数组（dataset_uid 列表）。") from None
    out: list[str] = []
    seen: set[str] = set()
    for x in items:
        if not isinstance(x, str):
            raise ReusePackError("bad_param", f"uids 的每一项必须是字符串，收到 {type(x).__name__}。")
        u = x.strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    if not out:
        raise ReusePackError("empty_input", "uids 为空；请传至少一个 dataset_uid。")
    if len(out) > _MAX_UIDS:
        raise ReusePackError("too_many", f"一次最多 {_MAX_UIDS} 个数据集，收到 {len(out)} 个。")
    return out


def _identifier(item: dict[str, Any]) -> dict[str, Any]:
    """这条记录在稿件里**该怎么被指认**。没有就是没有，不编。"""
    source = meaningful_metadata_text(item.get("source"))
    accession = provenance.public_accession(item.get("dataset_uid"), source)
    if accession:
        return {"kind": "accession", "value": accession, "label_en": "Accession"}
    pid = provenance.platform_id(item.get("dataset_uid"), source)
    if pid:
        # 「Dataset ID」而非「Accession」：上游（CELLxGENE/HCA）不用 accession 这套编号法。
        return {"kind": "platform_id", "value": pid, "label_en": "Dataset ID"}
    return {"kind": None, "value": None, "label_en": None}


def _row(item: dict[str, Any]) -> dict[str, Any]:
    source = meaningful_metadata_text(item.get("source")) or "未说明"
    ident = _identifier(item)
    raw = provenance.raw_data_provenance(item)
    url = meaningful_metadata_text(item.get("url")) or meaningful_metadata_text(item.get("download_url"))

    if raw["state"] == provenance.LISTED:
        raw_en = f"Listed in {raw['scope_en']}"
        raw_zh = "来源清单中列出"
    elif raw["state"] == provenance.NOT_LISTED:
        as_of = f" (as captured on {raw['as_of']})" if raw.get("as_of") else ""
        raw_en = f"Not listed in {raw['scope_en']}{as_of}"
        raw_zh = f"未列于{raw['scope']}（不等于不存在）"
    else:
        # 「没查过」在补充表里也必须诚实标出——留白会被读成「没有」。
        raw_en = "Not determined"
        raw_zh = "本工具未核验"

    return {
        "dataset_uid": str(item.get("dataset_uid") or ""),
        "dataset_name": meaningful_metadata_text(item.get("dataset_name")) or UNNAMED_DATASET,
        "source": source,
        "identifier": ident["value"],
        "identifier_kind": ident["kind"],
        "identifier_label_en": ident["label_en"],
        "url": url,
        "collection_doi": provenance.collection_doi(item),
        "raw_state": raw["state"],
        "raw_basis": raw["basis"],
        "raw_note_en": raw_en,
        "raw_note_zh": raw_zh,
        # 文件级台账只覆盖 10x 的 767 条 → 其余一律 None（「不知道」），绝不写 0（「没有」）。
        "n_files": _n_files_or_none(item),
    }


def _n_files_or_none(item: dict[str, Any]) -> int | None:
    source = meaningful_metadata_text(item.get("source"))
    if not source or provenance.SOURCE_10X.lower() not in source.lower():
        return None
    try:
        return max(0, int(item.get("n_files") or 0))
    except (TypeError, ValueError):
        return 0


def _source_breakdown(rows: list[dict[str, Any]]) -> "OrderedDict[str, int]":
    counts: "OrderedDict[str, int]" = OrderedDict()
    for r in rows:
        counts[r["source"]] = counts.get(r["source"], 0) + 1
    return counts


def _join_en(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _paragraph(rows: list[dict[str, Any]]) -> str:
    """投稿段落：只陈述**我们真知道**的事，其余留给补充表和 gaps。

    刻意**不**在段落里写「本工具未核验」之类的话——那是我们的局限，不是数据集的属性，
    不该出现在别人的论文里。它属于 `gaps`（给用户看的中文待办）。
    """
    n = len(rows)
    noun = "dataset" if n == 1 else "datasets"
    breakdown = _source_breakdown(rows)
    src_parts = [
        f"{provenance.english_source_name(s)} (n={c})" if len(breakdown) > 1 else provenance.english_source_name(s)
        for s, c in breakdown.items()
    ]
    sentence = (
        f"This study reuses {n} publicly available {noun} obtained from {_join_en(src_parts)}. "
        "Identifiers and access URLs for all reused datasets are listed in the accompanying table."
    )

    # 原始数据：只有在**确实核验过**的子集上才发声；没查过的（not_checked）一个字不提——
    # 它们只出现在补充表的 "Not determined" 与 gaps 里。
    listed = [r for r in rows if r["raw_state"] == provenance.LISTED]
    not_listed = [r for r in rows if r["raw_state"] == provenance.NOT_LISTED]
    if listed:
        sentence += (
            f" Raw sequencing data (FASTQ) are listed in the corresponding source repositories"
            f" for {_subject_en(len(listed), n)}."
        )
    if not_listed:
        sentence += (
            f" No FASTQ files are listed in the corresponding source repositories"
            f" for {_subject_en(len(not_listed), n)};"
            " raw sequencing reads may be deposited in other archives."
        )
    return sentence


def _subject_en(k: int, n: int) -> str:
    """「N 个里的 k 个」的英文主语。k==n 时说 "all N"，别写成别扭的 "5 of these 5"。"""
    if n == 1:
        return "this dataset"
    if k == n:
        return f"all {n} datasets"
    return f"{k} of these datasets"


def _gaps(rows: list[dict[str, Any]]) -> list[str]:
    """本工具**无法提供**、必须用户自己核实的事。中文，给用户看，不进稿件。"""
    gaps: list[str] = []

    unchecked = [r for r in rows if r["raw_state"] == provenance.NOT_CHECKED]
    if unchecked:
        srcs = ", ".join(sorted({r["source"] for r in unchecked}))
        gaps.append(
            f"{len(unchecked)} 个数据集的原始数据可及性「本工具未核验」（{srcs}）："
            "目录里的「无 FASTQ」是抓取时的保守占位、不是来源的声明。"
            "若你的方法学涉及原始 reads，请到来源页面或 ENA/GEO/SRA 核实。"
        )

    no_ident = [r for r in rows if not r["identifier"]]
    if no_ident:
        # 括号里原本还有一句「本工具的内部编号不是 accession，不会替你写进稿件」——
        # 按产品要求删掉：清单里那一列本来就写着「来源未登记编号」，
        # 而且从头到尾没有哪一处把内部编号当 accession 输出过，这句是在辩解一件没发生的事。
        gaps.append(
            f"{len(no_ident)} 个数据集的来源未登记可引用编号：请在稿件中用来源页 URL 指认，并注明访问日期。"
        )

    no_url = [r for r in rows if not r["url"]]
    if no_url:
        gaps.append(f"{len(no_url)} 个数据集没有公开访问链接：投稿前必须到来源确认访问方式。")

    unknown_files = [r for r in rows if r["n_files"] is None]
    if unknown_files:
        gaps.append(
            f"{len(unknown_files)} 个数据集的文件级清单「本工具未核验」"
            "（文件级清单目前只覆盖 10x Genomics 的 767 条）：如需在稿件里点名具体文件，请到来源页面核对。"
        )

    dois = [r for r in rows if r["collection_doi"]]
    if dois:
        gaps.append(
            f"{len(dois)} 个数据集带合集（collection）级 DOI：它指向「产出这批数据的论文」、"
            "可能涵盖多个数据集，不是这一个数据集的引用。按期刊要求决定是否引用该文献。"
        )
    return gaps


def _retrieval_provenance(snapshot: dict[str, Any] | None, retrieval_date: str | None) -> dict[str, Any] | None:
    """复现出处：本清单基于哪份语料、哪天检索的。让重跑可对比、可复现。

    `snapshot` 是 `corpus.corpus_snapshot` 的确定性描述（内容指纹 + 条数 + 来源分布）；
    `retrieval_date` 是检索时刻（ISO 日期）。二者缺一则返回 None（不硬造）。
    `note_en` 是可放进论文 Methods 的复现句；`note_zh` 是给用户看的复现提示。
    """
    if not snapshot or not snapshot.get("snapshot_id"):
        return None
    sid = snapshot["snapshot_id"]
    n = snapshot.get("n_records", 0)
    sources = snapshot.get("sources") or {}
    src_zh = "、".join(f"{s}（{c}）" for s, c in sources.items()) or "未知"
    note_en = (
        f"Datasets were retrieved from a BioData Agent catalog snapshot (id {sid}, {n} records)"
        + (f" on {retrieval_date}." if retrieval_date else ".")
    )
    date_zh = f"{retrieval_date} 的" if retrieval_date else ""
    return {
        "date": retrieval_date,
        "snapshot_id": sid,
        "n_corpus_records": n,
        "sources": dict(sources),
        "note_en": note_en,
        "note_zh": (
            f"本清单基于{date_zh}检索，语料快照 {sid}（{n} 条，来源：{src_zh}）。"
            "换库或增删数据后快照 ID 会变；重跑若结果不同，可用该 ID 与本次对比。"
        ),
    }


def build_reuse_pack(
    items: list[dict[str, Any]],
    unresolved: list[str] | None = None,
    *,
    snapshot: dict[str, Any] | None = None,
    retrieval_date: str | None = None,
) -> dict[str, Any]:
    """N 个已解析的数据集 item → 复用出处清单。纯函数（Web 与 MCP 共用单一真源）。

    `unresolved` 是查不到的 uid，**必须**原样带出：静默少一行，用户会以为它已经写进去了。
    `snapshot`/`retrieval_date` 填入可复现出处 `pack["retrieval"]`；不传则为 None（向后兼容）。
    """
    rows = [_row(it) for it in items]
    tombstones = [str(u) for u in (unresolved or [])]
    pack = {
        "n_datasets": len(rows),
        "paragraph": _paragraph(rows) if rows else "",
        "table": rows,
        "sources": [{"source": s, "count": c} for s, c in _source_breakdown(rows).items()],
        "raw_state_counts": dict(Counter(r["raw_state"] for r in rows)),
        "gaps": _gaps(rows),
        "unresolved": tombstones,
        "retrieval": _retrieval_provenance(snapshot, retrieval_date),
    }
    if tombstones:
        pack["gaps"].insert(
            0,
            f"{len(tombstones)} 个数据集在当前库中找不到，「未纳入本清单」："
            + "、".join(tombstones[:5])
            + ("…" if len(tombstones) > 5 else "")
            + "。它们可能来自已卸载的外部库，请自行核实后手工补写。",
        )
    return pack


def build_pack_for_uids(uids: list[str], records: Any, today: str | None = None) -> dict[str, Any]:
    """uid 列表 + 语料 → 复用出处清单。**Web `/api/reuse-pack` 与 MCP 共用的唯一入口。**

    刚刚（`item_view`）才因为「两份手抄投影」漏掉半个 bug，这里从第一天就只留一个入口。
    查不到的 uid 进 `unresolved` 墓碑，不静默丢。
    `today` = 检索日期（ISO），默认 `date.today()`；连同**确定性语料快照**（内容指纹）
    一起填入 `pack["retrieval"]`，让清单可复现。`records` 是被检索的语料，快照即由它算。
    """
    from .item_view import build_item
    from ..corpus.corpus import corpus_snapshot

    records = list(records)
    index: dict[str, Any] = {}
    for record in records:
        raw = record.raw if isinstance(record.raw, dict) else {}
        uid = str(raw.get("dataset_uid") or "")
        if uid and uid not in index:
            index[uid] = record

    items: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for uid in uids:
        record = index.get(uid)
        if record is None:
            unresolved.append(uid)
        else:
            items.append(build_item(record, include_introduction=True))
    snapshot = corpus_snapshot(records)
    retrieval_date = today or date.today().isoformat()
    return build_reuse_pack(items, unresolved, snapshot=snapshot, retrieval_date=retrieval_date)


def to_markdown(pack: dict[str, Any]) -> str:
    """清单 → Markdown（补充表 + 段落）。用户实际要粘走的东西。

    导出走**纯文本返回**、由前端 Blob 落盘；服务端不写盘——新增写盘目录就要多维护一份
    路径清单，手抄清单迟早漂移（fail-open 事故的同型根因）。
    """
    lines: list[str] = ["## Reused public datasets", ""]
    retrieval = pack.get("retrieval")
    if retrieval:
        # 复现出处：note_en 是可放进 Methods 的复现句（可见）；note_zh 是给用户的提示（注释，不随稿件走）。
        lines += [f"> {retrieval['note_en']}", ""]
        lines.append(f"<!-- 复现信息（不随稿件提交）：{retrieval['note_zh']} -->")
        lines.append("")
    if pack.get("paragraph"):
        lines += [pack["paragraph"], ""]
    rows = pack.get("table") or []
    if rows:
        lines.append("| " + " | ".join(_TABLE_HEADERS_EN) + " |")
        lines.append("|" + "|".join(["---"] * len(_TABLE_HEADERS_EN)) + "|")
        for r in rows:
            ident = f"{r['identifier_label_en']}: {r['identifier']}" if r["identifier"] else "—"
            lines.append(
                "| "
                + " | ".join(
                    _md_cell(x)
                    for x in (
                        r["dataset_name"],
                        provenance.english_source_name(r["source"]),
                        ident,
                        r["url"] or "—",
                        r["raw_note_en"],
                    )
                )
                + " |"
            )
        lines.append("")
    if pack.get("gaps"):
        lines += ["<!-- 以下为本工具给你的待办，**不要**随稿件提交 -->", ""]
        lines += [f"<!-- - {g} -->" for g in pack["gaps"]]
        lines.append("")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    """Markdown 表格单元格：`|` 会截断整行 → 必须转义（数据集名里真的有 `|`）。"""
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------- 引文导出
#
# **核心诚实点（provenance.py 早已警告的坑）**：`collection_doi` 指向「产出这批数据的**论文**」、
# 可能涵盖多个数据集，**不是这一个数据集的引用**。所以导出时：
#   - 引用的**主条目是数据集本身**（RIS `TY - DATA` / BibTeX `@misc`），用 accession + URL + 来源库；
#   - **绝不**把数据集写成 `@article`，也**绝不**把 collection_doi 当成数据集自己的 DOI 挂在条目上——
#     那会让审稿人以为这个数据集有独立 DOI。DOI 只作为「关联论文 DOI」放进 note，由用户按期刊决定是否引。
# 这与 reuse_pack 的 gaps 口径一致：数据集不是论文，别把两者混成一条引用。


def _cite_key(row: dict[str, Any], index: int) -> str:
    """BibTeX 引用键：优先 accession，退回 uid，再退回序号。只留字母数字，保唯一（附序号）。"""
    base = row.get("identifier") or row.get("dataset_uid") or f"dataset{index + 1}"
    safe = "".join(ch for ch in str(base) if ch.isalnum())
    return f"{safe or 'dataset'}{index + 1}"


def _assoc_doi_note(row: dict[str, Any]) -> str:
    doi = row.get("collection_doi")
    if not doi:
        return ""
    return f"Associated publication DOI: {doi} (this DOI identifies the paper, not the dataset; cite per journal requirements)."


def to_ris(pack: dict[str, Any]) -> str:
    """复用清单 → RIS。每个数据集一条 `TY - DATA`（数据集，不是论文）。"""
    out: list[str] = []
    for r in pack.get("table") or []:
        rec = ["TY  - DATA", f"T1  - {r.get('dataset_name') or '(untitled)'}"]
        src = provenance.english_source_name(r.get("source") or "")
        if src:
            rec.append(f"DB  - {src}")
        if r.get("identifier"):
            rec.append(f"AN  - {r['identifier']}")
        if r.get("url"):
            rec.append(f"UR  - {r['url']}")
        note = _assoc_doi_note(r)
        if note:
            rec.append(f"N1  - {note}")
        rec.append("ER  - ")
        out.append("\n".join(rec))
    return "\n\n".join(out) + ("\n" if out else "")


def _bib_escape(value: Any) -> str:
    return str(value if value is not None else "").replace("{", "(").replace("}", ")").replace("\\", "/")


def to_bibtex(pack: dict[str, Any]) -> str:
    """复用清单 → BibTeX。每个数据集一条 `@misc`（数据集，不是 `@article`）。"""
    out: list[str] = []
    for i, r in enumerate(pack.get("table") or []):
        fields = [f"  title = {{{_bib_escape(r.get('dataset_name') or '(untitled)')}}}"]
        src = provenance.english_source_name(r.get("source") or "")
        ident = r.get("identifier")
        howpub = ", ".join(x for x in (src, ident) if x)
        if howpub:
            fields.append(f"  howpublished = {{{_bib_escape(howpub)}}}")
        if r.get("url"):
            fields.append(f"  url = {{{_bib_escape(r['url'])}}}")
        note = _assoc_doi_note(r)
        note_full = ("Reused public dataset. " + note).strip()
        fields.append(f"  note = {{{_bib_escape(note_full)}}}")
        out.append("@misc{" + _cite_key(r, i) + ",\n" + ",\n".join(fields) + "\n}")
    return "\n\n".join(out) + ("\n" if out else "")
