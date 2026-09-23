# -*- coding: utf-8 -*-
"""元数据反标富化：从记录的 dataset_name + description 文本里识别词表已知术语，回填**空**的结构化维度字段。

用途：
  1. 离线富化官方快照——SCP/GEO 的 species/tissue/disease/chemistry 官方端点不供（全 null →
     约束式 NL 检索在硬过滤阶段整源灭掉，2,092 条只能编号直达）；
  2. 联网适配器（`corpus_curation.search_online`）入库前的同一口径反标（如 HuBMAP 端点不供
     species/disease、SCP 端点不供物种，与官方快照缺口同形）。

隔离红线适配：`corpus_curation` 不得 import query_parser（tests/test_curation_isolation.py AST 门），
故词边界匹配在此自实现一份，规则与 `query_parser._alias_occurrences` **逐行同源**（ASCII 端词边界 +
右侧容忍一个复数 s）。**改任何一边必须双同步**，两侧测试互锁（tests/test_corpus_enrich.py 里有
与 query_parser 行为一致性的对照用例）。

诚实纪律：
  - 只填**缺失值**字段（`normalizer.is_missing_value` 判定），绝不覆盖已有真值；
  - 反标所得值集**不穷尽**（只标识别出的术语）→ 填了 tissue/disease 的记录须把
    `metadata_provenance.complete` 置 False（retriever「值集不完整」第三态据此生效，
    与 SCEA 抽样先例同口径）；species/chemistry 不受影响（非 _PARTIAL_CAPABLE_DIMS）；
  - 命中明细随返回值给调用方写 provenance/报告，不静默。
"""
from __future__ import annotations

from typing import Any

from ..retrieval.normalizer import is_missing_value
from ..domain.registry import domain_catalog as _domain_catalog

__all__ = [
    "BACKFILL_METHOD",
    "BACKFILL_DIMS",
    "DIM_TO_FIELD",
    "detect_terms",
    "backfill_record",
]

#: 反标方法标识（写进 metadata_provenance.backfill.method，供审计/复跑核对）。
BACKFILL_METHOD = "offline description alias backfill v1"

#: 参与反标的维度 → 记录顶层字段。assay 维落 chemistry 字段（与 CELLxGENE ingest 口径一致：
#: assay 文本即 chemistry；硬约束侧的联合文本访问器在 domains/biodata/dimensions 的 assay 描述符）。
BACKFILL_DIMS: tuple[str, ...] = ("species", "tissue", "disease", "assay")
DIM_TO_FIELD: dict[str, str] = {
    "species": "species",
    "tissue": "tissue",
    "disease": "disease",
    "assay": "chemistry",
}

_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _alias_occurrences(text: str, alias: str) -> list[tuple[int, int]]:
    """与 query_parser._alias_occurrences 逐行同源（隔离门禁止 import query_parser，故复制）。

    规则：alias 首/尾字符是 ASCII 字母数字时，该侧不允许紧邻另一个 ASCII 字母数字；中文两端不判
    边界；右侧容忍一个复数尾巴 s。返回 [start, end) 列表。两边改动必须双同步。"""
    if not alias:
        return []
    left_guard = alias[0] in _ASCII_ALNUM
    right_guard = alias[-1] in _ASCII_ALNUM
    out: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while True:
        i = text.find(alias, start)
        if i < 0:
            break
        start = i + 1
        if left_guard and i > 0 and text[i - 1] in _ASCII_ALNUM:
            continue
        end = i + len(alias)
        if right_guard and end < n and text[end] in _ASCII_ALNUM:
            if text[end] == "s" and (end + 1 >= n or text[end + 1] not in _ASCII_ALNUM):
                end += 1
            else:
                continue
        out.append((i, end))
    return out


def _iter_entries(catalog: dict[str, list[dict[str, Any]]], dim: str):
    for entry in catalog.get(dim) or []:
        if not isinstance(entry, dict):
            continue
        aliases = entry.get("aliases") or entry.get("keywords") or []
        display = str(entry.get("display") or "").strip()
        targets = [str(t) for t in (entry.get("targets") or [])]
        if not display or not targets:
            continue
        yield entry, [str(a) for a in aliases if str(a).strip()], display, targets


def detect_terms(
    text: str,
    *,
    dims: tuple[str, ...] = BACKFILL_DIMS,
    catalog: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """在 text（任意大小写，内部小写化）里识别词表术语。

    返回 {dim: [{"display", "targets", "alias"}…]}（命中顺序、按 display 去重）。不做「消费」——
    反标要的是**标全**（同一文本可同时命中 generic 与 specific 术语，两者都标上对检索只有好处）。
    """
    cat = catalog or _domain_catalog()
    lowered = (text or "").lower()
    found: dict[str, list[dict[str, Any]]] = {}
    for dim in dims:
        seen: set[str] = set()
        for _entry, aliases, display, targets in _iter_entries(cat, dim):
            if display in seen:
                continue
            hit_alias = next((a for a in aliases if _alias_occurrences(lowered, a.lower())), None)
            if hit_alias is None:
                continue
            seen.add(display)
            found.setdefault(dim, []).append(
                {"display": display, "targets": targets, "alias": hit_alias}
            )
    return found


def _field_value(display: str, targets: list[str]) -> str:
    """反标字段值：display 须能被该条目的至少一个 target 子串命中（检索硬过滤按 target 子串匹配），
    否则把首个 target 附在括号里兜底（如 display「Non-human Primate」与 target「macaque」的情形）。"""
    disp = display.lower()
    if any(t.lower() in disp for t in targets):
        return display
    return f"{display} ({targets[0]})"


def backfill_record(
    record: dict[str, Any],
    *,
    dims: tuple[str, ...] = BACKFILL_DIMS,
    text_keys: tuple[str, ...] = ("dataset_name", "description"),
    catalog: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """对一条记录（raw dict，就地修改）做反标，返回审计报告。

    只对 `is_missing_value` 判定为缺失的字段落笔；已标注字段一律跳过（不覆盖真值）。
    填了 tissue/disease 时把 `metadata_provenance.complete` 置 False（值集不穷尽的诚实声明）；
    任何落笔都会在 `metadata_provenance.backfill` 留明细（method/dims/命中 display）。
    """
    text = " ".join(str(record.get(k) or "") for k in text_keys)
    found = detect_terms(text, dims=dims, catalog=catalog)
    report: dict[str, Any] = {"filled": {}, "skipped_present": []}
    if not found:
        return report

    for dim, hits in found.items():
        field = DIM_TO_FIELD[dim]
        if not is_missing_value(record.get(field)):
            report["skipped_present"].append(dim)
            continue
        value = ", ".join(_field_value(h["display"], h["targets"]) for h in hits)
        record[field] = value
        report["filled"][dim] = [h["display"] for h in hits]

    if report["filled"]:
        prov = record.get("metadata_provenance")
        if not isinstance(prov, dict):
            prov = {}
            record["metadata_provenance"] = prov
        # tissue/disease 是 retriever 的「值集可不穷尽」维：反标只标识别出的 → 必须声明不穷尽，
        # 否则「没标 X」会被误读成「不是 X」（SCEA 抽样先例同口径）。
        if "tissue" in report["filled"] or "disease" in report["filled"]:
            prov["complete"] = False
        prov["backfill"] = {
            "method": BACKFILL_METHOD,
            "dims": sorted(report["filled"].keys()),
            "displays": {dim: report["filled"][dim] for dim in sorted(report["filled"])},
        }
    return report
