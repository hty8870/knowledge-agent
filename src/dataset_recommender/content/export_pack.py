# -*- coding: utf-8 -*-
"""追踪研究材料包生成（导出中心）。

## 这是什么，不是什么

**是**：把追踪的**当前状态快照**（候选 uid+状态+理由+核验时间 + check_condition + provenance，
前端组装、本模块归一）生成一份研究材料 ZIP——manifest（稳定标识符+来源 URL+最后核验时间）、
纳入/排除表（含理由）、下载任务包（复用 `task_pack`）、三格式引文（RIS/BibTeX 复用 `reuse_pack`
+ GB/T 7714-2015 新 formatter）、检索与核验溯源、「数据发现与筛选方法」草稿、recipe.json（可重跑）。

**不是**：追踪的第二个真源。研究材料是追踪当前状态的**派生文件**；
本模块不保存任何状态、不写盘、不联网、不调 LLM。ZIP 由内存拼出、服务端零写盘
（`task_pack.files_to_zip_bytes` 同一范式——多一个写盘目录就多一对必须手工对账的忽略清单）。

## 诚实约束（本模块存在的全部理由，与 reuse_pack/provenance 同一套红线）

- **方法草稿只从真实溯源生成**：只渲染 `provenance` / `search_trace` 里**在场**的字段；
  样本数、访问日期、数据库范围一概不编——没有就不写，并在草稿里如实声明「未记录」。
- **引文不编造字段**：GB/T 7714-2015 按 [DS/OL]（数据集/联机网络）著录，主要责任者与出版地
  在本目录数据中普遍缺失——**如实留位不编造**：条目只含在场字段，缺什么在文件末尾的
  「未填字段说明」逐条列出（同 reuse_pack 的 gaps 哲学：留白会被读成「没有」，所以另列清单）。
- **查不到的 uid 出显式墓碑**：候选在本地语料里找不到 → 清单照列（它是追踪候选的一部分）、
  名称如实标「当前库中找不到」，下载/引文不含它（与 reuse_pack 的 unresolved 语义一致）。
- **一切数字有据可查**：纳入/排除计数来自快照候选状态（前端零 LLM diff 时也用它）；
  目录版本来自语料快照（corpus_snapshot 单一真源），不是调用方自述。

## 入参边界

调用方（前端）只传追踪快照的**键与状态**（uid+status+reason+verified_at + check_condition +
provenance），**不传数据集元数据全文**——数据集内容由本模块从本地语料解析（reuse-pack 的
keys-only 哲学：一旦开了「把数据集描述贴进来」的口子，产品就有了吃进未发表工作的路径）。
"""
from __future__ import annotations

from .labels import PROJECT_CONDITION_LABELS, UNNAMED_DATASET

import json
from typing import Any, Sequence

from . import item_view, reuse_pack
from .task_pack import files_to_zip_bytes  # 复用 task_pack 的内存拼包（服务端零写盘同一范式）
from ..corpus.corpus import corpus_snapshot
from ..corpus import provenance as PROV

SCHEMA = "biodata-export-pack/v1"

#: 导出类型。四种按钮对应四个导出动作；前三种是 ZIP 的**单项轻量导出**（内容如实标注
#: 在 `render_files` 的 KIND_FILES 里），`full` = 全部研究材料。
EXPORT_KINDS = ("download_list", "citations", "screening_record", "full")
EXPORT_KIND_ZH = {
    "download_list": "下载清单",
    "citations": "引文",
    "screening_record": "筛选记录",
    "full": "全部研究材料",
}

#: 候选/导出 uid 上限（与 reuse_pack._MAX_UIDS 同档；超出即 400，不做静默截断）。
_MAX_CANDIDATES = 200

#: 候选状态枚举（与前端 artifacts.js PROJECT_STATUS 逐字同源——同一枚举值的单一写法）。
CANDIDATE_STATUSES = ("候选", "待核验", "已核验", "已排除")

#: 下载任务包子目录（task_pack 的扁平文件名与导出清单的 manifest 同名，必须分目录避撞）。
DOWNLOAD_DIR = "download"


class ExportPackError(ValueError):
    """入参非法。带 `code` 供 HTTP 层映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 入参归一

def _text(value: Any) -> str:
    return str(value or "").strip()


def _str_list(value: Any, *, limit: int | None = None, name: str = "列表") -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise ExportPackError("bad_param", f"{name} 必须是字符串数组。")
    out: list[str] = []
    for x in value:
        if not isinstance(x, str):
            raise ExportPackError("bad_param", f"{name} 的每一项必须是字符串。")
        t = x.strip()
        if t and t not in out:
            out.append(t)
    if limit is not None and len(out) > limit:
        raise ExportPackError("too_many", f"{name}一次最多 {limit} 项，收到 {len(out)} 项。")
    return out


def sanitize_kind(raw: Any) -> str:
    kind = _text(raw)
    if kind not in EXPORT_KINDS:
        raise ExportPackError(
            "bad_param",
            f"导出类型必须是 {'/'.join(EXPORT_KINDS)} 之一，收到「{kind or '空'}」。",
        )
    return kind


def _norm_candidates(raw: Any) -> list[dict]:
    """候选快照归一：uid 必填、状态必须是四种之一（未知状态不猜、显式 400）、
    理由/核验时间如实保留（空=未填，绝不补写）。"""
    if raw is None:
        return []
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise ExportPackError("bad_param", "candidates 必须是数组。")
    out: list[dict] = []
    seen: set[str] = set()
    for c in raw:
        if not isinstance(c, dict):
            raise ExportPackError("bad_param", "candidates 的每一项必须是对象。")
        uid = _text(c.get("uid"))
        if not uid:
            continue
        status = _text(c.get("status"))
        if status not in CANDIDATE_STATUSES:
            raise ExportPackError(
                "bad_param",
                f"候选「{uid}」状态非法：{status or '空'}。"
                f"合法状态：{'/'.join(CANDIDATE_STATUSES)}。",
            )
        if uid in seen:
            continue
        seen.add(uid)
        out.append({
            "uid": uid,
            "status": status,
            "reason": _text(c.get("reason")),
            "verified_at": _text(c.get("verified_at")),
            "added_at": _text(c.get("added_at")),
        })
    if len(out) > _MAX_CANDIDATES:
        raise ExportPackError("too_many", f"候选一次最多 {_MAX_CANDIDATES} 个，收到 {len(out)} 个。")
    return out


def _norm_spec(raw: Any) -> dict:
    """check_condition.spec 归整（与 /api/watch/check 入参同构；空对象 = 未记录）。"""
    s = raw if isinstance(raw, dict) else {}
    return {
        "spec_version": _text(s.get("spec_version")) or "v1",
        "query": _text(s.get("query")),
        "sources": _str_list(s.get("sources"), name="spec.sources"),
        "facet_filters": [
            {"dim": _text(f.get("dim")), "value": _text(f.get("value"))}
            for f in (s.get("facet_filters") or [])
            if isinstance(f, dict) and _text(f.get("dim")) and _text(f.get("value"))
        ],
        "suppressed_constraints": _str_list(s.get("suppressed_constraints"), name="spec.suppressed"),
        "lenient_dims": _str_list(s.get("lenient_dims"), name="spec.lenient"),
        "date_from": _text(s.get("date_from")),
        "date_to": _text(s.get("date_to")),
    }


def _norm_provenance(raw: Any) -> dict:
    """provenance 归整（全字段；字段名与前端 artifacts.js `_normProvenance` 对齐）。
    只保留在场字段——方法草稿/溯源渲染按「在场才写、缺省如实留空」走。"""
    p = raw if isinstance(raw, dict) else {}
    filters = p.get("filters") if isinstance(p.get("filters"), dict) else {}
    result = p.get("result") if isinstance(p.get("result"), dict) else {}
    retrieval_params = p.get("retrieval_params")
    if retrieval_params is not None and not isinstance(retrieval_params, dict):
        retrieval_params = None
    return {
        "query": _text(p.get("query")),
        "retrieval_params": dict(retrieval_params) if isinstance(retrieval_params, dict) else {},
        "search_trace": p.get("search_trace") if isinstance(p.get("search_trace"), dict) else {},
        "filters": {
            "active": _str_list(filters.get("active"), name="filters.active"),
            "suppressed": _str_list(filters.get("suppressed"), name="filters.suppressed"),
            "lenient": _str_list(filters.get("lenient"), name="filters.lenient"),
        },
        "corpus_digest": _text(p.get("corpus_digest")),
        "retrieved_at": _text(p.get("retrieved_at")),
        "policy_id": _text(p.get("policy_id")),
        "trace_turn_id": _text(p.get("trace_turn_id")),
        "result": {
            "uids": _str_list(result.get("uids"), limit=_MAX_CANDIDATES, name="result.uids"),
            "truncated": result.get("truncated") is True,
        },
    }


def _norm_check_condition(raw: Any) -> dict:
    cc = raw if isinstance(raw, dict) else {}
    baseline = cc.get("baseline") if isinstance(cc.get("baseline"), dict) else {}
    return {
        "display_query": _text(cc.get("display_query")),
        "spec": _norm_spec(cc.get("spec")),
        "baseline": {
            "uids": _str_list(baseline.get("uids"), limit=_MAX_CANDIDATES, name="baseline.uids"),
            "result_total": baseline.get("result_total") if isinstance(baseline.get("result_total"), (int, float)) else 0,
            "truncated": baseline.get("truncated") is True,
            "generated_at": _text(baseline.get("generated_at")),
        },
        "last_checked_at": _text(cc.get("last_checked_at")),
    }


def sanitize_snapshot(raw: Any) -> dict:
    """追踪当前状态快照归一。**所有字段都可缺省**（追踪可以没有检查条件/溯源）——
    导出包对缺字段的处理是「如实不渲染」，不是拒绝导出。"""
    if not isinstance(raw, dict):
        raise ExportPackError("bad_param", "project 必须是对象（追踪状态快照）。")
    project_id = _text(raw.get("project_id"))
    return {
        "project_id": project_id,
        "name": _text(raw.get("name")) or (project_id or "未命名追踪"),
        "goal": _text(raw.get("goal")),
        "include_conditions": _str_list(raw.get("include_conditions"), limit=8, name=PROJECT_CONDITION_LABELS["include"]),
        "exclude_conditions": _str_list(raw.get("exclude_conditions"), limit=8, name=PROJECT_CONDITION_LABELS["exclude"]),
        "candidates": _norm_candidates(raw.get("candidates")),
        "check_condition": _norm_check_condition(raw.get("check_condition")),
        "provenance": _norm_provenance(raw.get("provenance")),
    }


# ---------------------------------------------------------------- 语料解析

def _resolve_candidates(snapshot: dict, records: Sequence[Any]) -> tuple[list[dict], list[str], dict]:
    """候选 uid → 展示层 item（本地语料解析，服务端单一真源）。查不到 → unresolved 墓碑。
    `by_uid` 供 manifest 按 uid 回查。"""
    index: dict[str, Any] = {}
    for record in records:
        raw = record.raw if isinstance(record.raw, dict) else {}
        uid = str(raw.get("dataset_uid") or "")
        if uid and uid not in index:
            index[uid] = record
    items: list[dict] = []
    unresolved: list[str] = []
    for c in snapshot["candidates"]:
        record = index.get(c["uid"])
        if record is None:
            unresolved.append(c["uid"])
            continue
        item = item_view.build_item(record, include_introduction=True)
        # 把追踪侧的核验状态/理由/时间并进 item（manifest 与候选表用），不污染 item_view 契约字段。
        item["_candidate"] = {
            "status": c["status"], "reason": c["reason"],
            "verified_at": c["verified_at"], "added_at": c["added_at"],
        }
        items.append(item)
    return items, unresolved, index


def _stable_identifier(item: dict) -> str:
    """该数据集在稿件里该怎么被指认（与 reuse_pack 同源：accession / platform_id / 留空）。"""
    uid = str(item.get("dataset_uid") or "")
    source = item.get("source") or ""
    acc = PROV.public_accession(uid, source)
    if acc:
        return acc
    return PROV.platform_id(uid, source) or ""


# ---------------------------------------------------------------- GB/T 7714-2015

def gb7714_entry(item: dict, export_date: str) -> str:
    """一条数据集的 GB/T 7714-2015 引文（文献类型标志 [DS/OL] = 数据集/联机网络）。

    著录骨架：`主要责任者. 题名[DS/OL]. 出版地: 出版者, 出版年[引用日期]. 获取和访问路径.`

    **字段缺失如实留位不编造**：本目录不记录「主要责任者」（个人/机构作者）与「出版地」，
    一律不写（题名可作首项，这是国标允许的省略）；出版者 = 来源库（最接近事实的著录主体）；
    出版年 = 来源登记的发表年（没有则省略）；引用日期 = 导出日期（真实，不是编的访问日期）。
    缺了哪些字段由 `gb7714_gaps` 在文件末尾逐条列出，供投稿前人工补全。"""
    title = str(item.get("dataset_name") or "").strip() or "（未命名数据集）"
    source = str(item.get("source") or "").strip()
    year = item.get("published_year")
    year_text = str(year) if isinstance(year, int) or (year and str(year).strip().isdigit()) else ""
    url = str(item.get("url") or "").strip()

    head = f"{title}[DS/OL]."
    pub_bits = [x for x in (source, year_text) if x]
    tail = ""
    if pub_bits:
        tail += ", ".join(pub_bits)
    tail += f"[{export_date}]"
    if url:
        tail += ". 获取和访问路径: " + url
    return f"{head} {tail}."


def gb7714_gaps(items: list[dict]) -> list[str]:
    """GB/T 引文里「没填」的字段清单（中文，给用户看，不混进引文条目）。"""
    gaps: list[str] = []
    for it in items:
        name = str(it.get("dataset_name") or "").strip() or str(it.get("dataset_uid") or "")
        missing = []
        if not (isinstance(it.get("published_year"), int) or it.get("published_year")):
            missing.append("出版年")
        if not str(it.get("url") or "").strip():
            missing.append("访问路径")
        if missing:
            gaps.append(f"{name}：未填{'、'.join(missing)}。")
    if items:
        gaps.insert(
            0,
            "本目录不记录「主要责任者」（个人/机构作者）与「出版地」，引文均未填写这两项；"
            "投稿前请按数据集的真实责任主体与来源补全。",
        )
    return gaps


# ---------------------------------------------------------------- 溯源与方法草稿

def _trace_steps(prov: dict) -> list[dict]:
    trace = prov.get("search_trace") or {}
    steps = trace.get("steps")
    return [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []


def _provenance_md(prov: dict) -> str:
    """检索与核验溯源：只写在场字段（「检索与核验溯源（来自 provenance）」）。"""
    lines = ["# 检索与核验溯源", "", "> 本文件来自追踪保存的检索溯源（provenance），只列系统真实记录的内容。", ""]
    if prov.get("query"):
        lines += ["## 检索语句", "", prov["query"], ""]
    rp = prov.get("retrieval_params") or {}
    if rp:
        lines += ["## 检索参数（确定性请求参数）", "", "```json", json.dumps(rp, ensure_ascii=False, indent=1), "```", ""]
    filters = prov.get("filters") or {}
    if any(filters.get(k) for k in ("active", "suppressed", "lenient")):
        lines += ["## 生效的筛选", ""]
        if filters.get("active"):
            lines.append("- 生效分面：" + "；".join(filters["active"]))
        if filters.get("suppressed"):
            lines.append("- 忽略条件：" + "；".join(filters["suppressed"]))
        if filters.get("lenient"):
            lines.append("- 宽放维度：" + "；".join(filters["lenient"]))
        lines.append("")
    steps = _trace_steps(prov)
    if steps:
        lines += ["## 检索执行轨迹（search_trace）", ""]
        for s in steps:
            label = str(s.get("label") or s.get("id") or "步骤")
            detail = str(s.get("detail") or "")
            status = str(s.get("status") or "")
            lines.append(f"- **{label}**（{status}）：{detail}" if detail else f"- **{label}**（{status}）")
        lines.append("")
    for label, key in (("语料快照 digest", "corpus_digest"), ("检索日期", "retrieved_at"),
                       ("遥测策略串", "policy_id"), ("关联对话轮", "trace_turn_id")):
        if prov.get(key):
            lines.append(f"- {label}：{prov[key]}")
    result = prov.get("result") or {}
    if result.get("uids"):
        lines.append(f"- 结果：{len(result['uids'])} 个数据集"
                     + ("（命中超过上限，已截断）" if result.get("truncated") else ""))
    return "\n".join(lines).rstrip() + "\n"


def _method_draft(prov: dict, snapshot: dict, export_date: str) -> str:
    """「数据发现与筛选方法」**草稿**：只从真实 provenance/search_trace 生成。

    标题与正文都标注「草稿——请核对后使用」；样本数、访问日期、数据库范围一概不编——
    溯源里没有就不写，并如实声明「未记录」。候选核验计数来自追踪当前状态（真实）。"""
    rp = prov.get("retrieval_params") or {}
    lines = [
        "# 数据发现与筛选方法（草稿）", "",
        "> **草稿——请核对后使用。** 本文件由系统按追踪保存的检索溯源自动生成，不是正式方法学陈述。"
        "系统只复述真实运行记录，不编造样本数、访问日期或数据库范围；未记录的内容如实标注「未记录」，"
        "投稿或归档前请逐条核对并补全。", "",
        "## 1. 检索语句", "",
        prov.get("query") or "未记录。", "",
        "## 2. 检索与筛选参数（来自保存的检索溯源）", "",
    ]
    spec = (snapshot.get("check_condition") or {}).get("spec") or {}
    params: list[str] = []
    sources = rp.get("sources") or spec.get("sources") or []
    if sources:
        params.append("- 数据来源：" + "、".join(sources))
    facets = spec.get("facet_filters") or rp.get("facet_filters") or []
    if facets:
        params.append("- 分面筛选：" + "；".join(
            f"{f.get('dim')}={f.get('value')}" for f in facets if isinstance(f, dict)))
    suppressed = spec.get("suppressed_constraints") or []
    if suppressed:
        params.append("- 忽略条件：" + "、".join(suppressed))
    lenient = spec.get("lenient_dims") or []
    if lenient:
        params.append("- 宽放维度：" + "、".join(lenient))
    date_from, date_to = spec.get("date_from") or "", spec.get("date_to") or ""
    if date_from or date_to:
        params.append(f"- 发表时间范围：{date_from or '不限'} 至 {date_to or '不限'}")
    strategy = "、".join(x for x in (rp.get("strategy"), rp.get("recall"), rp.get("rerank")) if x)
    if strategy:
        params.append(f"- 排序策略：{strategy}")
    params.append(f"- 检索日期：{prov.get('retrieved_at') or '未记录'}")
    lines += params + [""]
    steps = _trace_steps(prov)
    if steps:
        lines += ["## 3. 检索执行轨迹（来自真实运行溯源）", ""]
        for s in steps:
            label = str(s.get("label") or s.get("id") or "步骤")
            detail = str(s.get("detail") or "")
            status = str(s.get("status") or "")
            lines.append(f"- {label}（{status}）：{detail}" if detail else f"- {label}（{status}）")
        lines.append("")
    else:
        lines += ["## 3. 检索执行轨迹", "", "未记录。", ""]
    counts = {"候选": 0, "待核验": 0, "已核验": 0, "已排除": 0}
    for c in snapshot["candidates"]:
        st = c.get("status")
        if st in counts:
            counts[st] += 1
    lines += ["## 4. 候选核验情况（来自追踪当前状态）", "",
              f"- 候选共 {len(snapshot['candidates'])} 个：待核验 {counts['待核验']} · 已核验 {counts['已核验']} · 已排除 {counts['已排除']}。",
              "",
              "## 5. 未记录事项（系统没有、需人工核对补充）", "",
              "- 样本数：系统未记录样本量数字，如需在方法中报告，请从来源页面核对后补写。",
              "- 数据库检索范围：以上数据来源为追踪保存时的检索范围；后续检索如有增删，请如实更新。",
              f"- 本草稿生成于 {export_date}；检索日期见第 2 节（若为「未记录」请补写）。", "",
    ]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 组装

def build_export_pack(snapshot: dict, records: Sequence[Any], today: str | None = None) -> dict:
    """追踪状态快照 + 本地语料 → 研究材料包（纯函数，一次算、处处渲染）。

    - 下载任务包复用 `task_pack.build_task_pack`（四件套口径一致的那套），但只取下载相关
      文件（file-list/manifest/脚本/todo）——引文与溯源在导出包顶层另有自己的文件。
    - 三格式引文：RIS/BibTeX 走 `reuse_pack`（TY-DATA / @misc，数据集不是论文）；
      GB/T 7714 走本模块新 formatter。
    - 目录版本（corpus.snapshot_id）来自 `corpus_snapshot` 单一真源。
    """
    from datetime import date
    from . import task_pack

    export_date = today or date.today().isoformat()
    prov = snapshot.get("provenance") or {}
    items, unresolved, _ = _resolve_candidates(snapshot, records)
    corpus = corpus_snapshot(records, with_content=True)

    # 下载任务包：查询/检索参数只从真实溯源取（没有就如实空——task_pack 接受空 query）。
    # 用 task_pack.render_files（公开 API）取下载相关子集——不伸手进私有渲染函数，
    # 且 00-START-HERE/README/todo 与 task_pack 其它文件口径天然一致。
    retrieval_params = dict(prov.get("retrieval_params") or {})
    tp = task_pack.build_task_pack(
        query=str(prov.get("query") or ""),
        items=items,
        records=records,
        unresolved=unresolved,
        scope="primary",
        retrieval_params=retrieval_params,
        honesty={},
        today=export_date,
        membership=None,
    )
    _TP_DOWNLOAD_SUBSET = ("00-START-HERE.txt", "README.md", "file-list.md", "manifest.tsv",
                           "manifest.json", "download.sh", "download.ps1", "todo.md")
    download_files = [dict(f, path=f"{DOWNLOAD_DIR}/{f['path']}")
                      for f in task_pack.render_files(tp)
                      if f["path"] in _TP_DOWNLOAD_SUBSET]

    # 引文（RIS/BibTeX 复用 reuse_pack；GB/T 7714 新 formatter）。
    reuse = reuse_pack.build_reuse_pack(items, unresolved, snapshot=corpus, retrieval_date=export_date)

    manifest_rows = []
    for it in items:
        cand = it.get("_candidate") or {}
        manifest_rows.append({
            "uid": str(it.get("dataset_uid") or ""),
            "name": str(it.get("dataset_name") or UNNAMED_DATASET),
            "identifier": _stable_identifier(it),
            "source": str(it.get("source") or "未说明"),
            "url": str(it.get("url") or ""),
            "status": cand.get("status") or "待核验",
            "reason": cand.get("reason") or "",
            "verified_at": cand.get("verified_at") or "",
        })
    for uid in unresolved:
        manifest_rows.append({
            "uid": uid, "name": "（当前库中找不到）", "identifier": "", "source": "",
            "url": "", "status": "", "reason": "", "verified_at": "",
        })

    pack = {
        "schema": SCHEMA,
        "exported_at": export_date,
        "snapshot": snapshot,
        "items": items,
        "unresolved": unresolved,
        "manifest_rows": manifest_rows,
        "corpus": {
            "snapshot_id": corpus.get("snapshot_id", ""),
            "content_digest": corpus.get("content_digest", ""),
            "n_records": corpus.get("n_records", 0),
            "sources": corpus.get("sources", {}),
        },
        "citations": {
            "ris": reuse_pack.to_ris(reuse),
            "bibtex": reuse_pack.to_bibtex(reuse),
            "gb7714": _render_gb7714(items, export_date),
            "gaps": reuse.get("gaps", []),
        },
        "download": {
            "files": download_files,
            "todo": tp["todo"],
        },
    }
    return pack


def _render_gb7714(items: list[dict], export_date: str) -> str:
    lines = [
        "# 数据集引文（GB/T 7714-2015）", "",
        "> 著录格式：文献类型标志 [DS/OL]（数据集/联机网络）。",
        "> 主要责任者与出版地在本目录中普遍未记录，条目如实省略（不编造）；缺哪些字段见文末"
        "「未填字段说明」——投稿前请按数据集的真实责任主体与来源补全。", "",
    ]
    for i, it in enumerate(items, start=1):
        lines.append(f"[{i}] {gb7714_entry(it, export_date)}")
        lines.append("")
    gaps = gb7714_gaps(items)
    if gaps:
        lines += ["## 未填字段说明", ""]
        lines += [f"- {g}" for g in gaps]
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 产物渲染

def _plain(path: str, text: str, *, bom: bool = False, executable: bool = False) -> dict:
    return {"path": path, "text": text, "newline": "\n", "bom": bom, "executable": executable}


def _render_readme(pack: dict, kind: str) -> str:
    snapshot = pack["snapshot"]
    prov = snapshot.get("provenance") or {}
    name = snapshot.get("name") or "未命名追踪"
    rows = pack["manifest_rows"]
    counts = {"候选": 0, "待核验": 0, "已核验": 0, "已排除": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    kind_zh = EXPORT_KIND_ZH.get(kind, kind)
    lines = [
        "# 追踪研究材料 · " + name, "",
        f"这是追踪「{name}」的{kind_zh}导出（生成于 {pack['exported_at']}）。",
        "**研究材料是追踪当前状态的派生文件，不是第二真源**——导出内容随追踪状态变化。", "",
        "## 本包里有什么", "",
    ]
    file_explain = {
        "manifest.md": "数据集清单：稳定标识符 + 来源 URL + 最后核验时间",
        "manifest.tsv": "同上，制表符分隔（机器可读）",
        "candidates.md": "纳入/排除表：候选状态 + 理由 + 核验时间",
        "candidates.tsv": "同上，制表符分隔",
        "citations.ris": "数据集引文（RIS TY-DATA）",
        "citations.bib": "数据集引文（BibTeX @misc）",
        "citations-gb7714.txt": "数据集引文（GB/T 7714-2015 [DS/OL]）",
        "provenance.md": "检索与核验溯源（来自保存的检索记录）",
        "method-draft.md": "「数据发现与筛选方法」草稿——请核对后使用",
        "recipe.json": "可重跑配方（检索 spec + 溯源关键参数 + 候选快照）",
        "download/": "下载任务包（00-START-HERE / file-list / manifest / download 脚本 / todo）",
    }
    for path, explain in file_explain.items():
        listed = any(f == path or f.startswith(path) for f in pack["included_files"])
        if listed:
            lines.append(f"- `{path}`：{explain}")
    lines += [
        "",
        "## 追踪状态（导出时的快照）", "",
        f"- 候选共 {len(rows)} 个：待核验 {counts['待核验']} · 已核验 {counts['已核验']} · 已排除 {counts['已排除']}。",
    ]
    if snapshot.get("include_conditions"):
        lines.append(f"- {PROJECT_CONDITION_LABELS['include']}：" + "；".join(snapshot["include_conditions"]))
    if snapshot.get("exclude_conditions"):
        lines.append(f"- {PROJECT_CONDITION_LABELS['exclude']}：" + "；".join(snapshot["exclude_conditions"]))
    if prov.get("retrieved_at"):
        lines.append(f"- 检索日期：{prov['retrieved_at']}")
    lines += ["", "## 需要你自己确认的事", ""]
    lines += list(pack["download"]["todo"])
    lines.append("- 本工具只处理公开目录的元数据；方法草稿请核对后使用。")
    return "\n".join(lines) + "\n"


def _render_manifest_md(rows: list[dict]) -> str:
    lines = ["# 数据集清单", "",
             "| 数据集 | 稳定标识符 | 来源 | URL | 状态 | 最后核验时间 | 理由 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
            _cell(r["name"]), _cell(r["identifier"] or "—"), _cell(r["source"] or "—"),
            _cell(r["url"] or "—"), _cell(r["status"] or "—"), _cell(r["verified_at"] or "—"),
            _cell(r["reason"] or "—")))
    return "\n".join(lines) + "\n"


def _render_manifest_tsv(rows: list[dict]) -> str:
    out = ["#uid\tdataset_name\tstable_identifier\tsource\turl\tstatus\tverified_at\treason"]
    for r in rows:
        out.append("\t".join(_cell(r[k]) for k in ("uid", "name", "identifier", "source", "url",
                                                   "status", "verified_at", "reason")))
    return "\n".join(out) + "\n"


def _render_candidates_md(snapshot: dict, rows: list[dict]) -> str:
    counts = {"候选": 0, "待核验": 0, "已核验": 0, "已排除": 0}
    for r in rows:
        if r["status"] in counts:
            counts[r["status"]] += 1
    groups = {
        "已核验（纳入）": [r for r in rows if r["status"] == "已核验"],
        "已排除（排除）": [r for r in rows if r["status"] == "已排除"],
        "待核验 / 候选（未裁决）": [r for r in rows if r["status"] in ("待核验", "候选")],
    }
    lines = ["# 纳入/排除表", "",
             f"候选共 {len(rows)} 个：已核验 {counts['已核验']} · 已排除 {counts['已排除']} · "
             f"待核验 {counts['待核验']} · 候选 {counts['候选']}。", "",
             "> 「已核验/已排除」是用户终态（带核验时间与理由）；「待核验/候选」尚未裁决，"
             "未做纳入/排除结论。", ""]
    for title, group in groups.items():
        lines += [f"## {title}（{len(group)}）", ""]
        if not group:
            lines.append("（无）")
            lines.append("")
            continue
        lines.append("| 数据集 | 编号 | 理由 | 核验时间 |")
        lines.append("|---|---|---|---|")
        for r in group:
            lines.append("| {} | {} | {} | {} |".format(
                _cell(r["name"]), _cell(r["uid"]), _cell(r["reason"] or "—"), _cell(r["verified_at"] or "—")))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_candidates_tsv(snapshot: dict, rows: list[dict]) -> str:
    out = ["#uid\tdataset_name\tstatus\tdecision\treason\tverified_at"]
    decision = {"已核验": "纳入", "已排除": "排除", "待核验": "未裁决", "候选": "未裁决"}
    for r in rows:
        out.append("\t".join(_cell(r[k]) for k in ("uid", "name", "status"))
                   + "\t" + _cell(decision.get(r["status"], "未裁决"))
                   + "\t" + _cell(r["reason"])
                   + "\t" + _cell(r["verified_at"]))
    return "\n".join(out) + "\n"


def _render_recipe_json(pack: dict) -> str:
    snapshot = pack["snapshot"]
    prov = snapshot.get("provenance") or {}
    recipe = {
        "schema": SCHEMA,
        "exported_at": pack["exported_at"],
        "project": {
            "project_id": snapshot.get("project_id") or "",
            "name": snapshot.get("name") or "",
            "goal": snapshot.get("goal") or "",
            "include_conditions": snapshot.get("include_conditions") or [],
            "exclude_conditions": snapshot.get("exclude_conditions") or [],
        },
        "candidates": snapshot.get("candidates") or [],
        "check_spec": (snapshot.get("check_condition") or {}).get("spec") or {},
        "provenance": {
            "query": prov.get("query") or "",
            "retrieval_params": prov.get("retrieval_params") or {},
            "search_trace": prov.get("search_trace") or {},
            "filters": prov.get("filters") or {},
            "corpus_digest": prov.get("corpus_digest") or "",
            "retrieved_at": prov.get("retrieved_at") or "",
            "policy_id": prov.get("policy_id") or "",
            "result": prov.get("result") or {},
        },
        "corpus": pack["corpus"],
        "unresolved": pack["unresolved"],
    }
    return json.dumps(recipe, ensure_ascii=False, indent=1) + "\n"


def _cell(value: Any) -> str:
    """Markdown 表格 / TSV 单元格：竖线与换行会破坏结构，必须清洗。"""
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ").replace("\r", " ")


#: 每种导出类型的文件集（顺序即 ZIP 内顺序）。前三种是单项轻量导出，内容如实标注在
#: `_render_readme` 的文件说明里；`full` = 全部研究材料。
_KIND_FILES = {
    "download_list": ("README.md", "manifest.md", "manifest.tsv", "download/"),
    "citations": ("README.md", "manifest.md", "manifest.tsv",
                  "citations.ris", "citations.bib", "citations-gb7714.txt"),
    "screening_record": ("README.md", "manifest.md", "manifest.tsv", "candidates.md", "candidates.tsv",
                         "provenance.md", "method-draft.md", "recipe.json"),
    "full": ("README.md", "manifest.md", "manifest.tsv", "candidates.md", "candidates.tsv",
             "citations.ris", "citations.bib", "citations-gb7714.txt",
             "download/", "provenance.md", "method-draft.md", "recipe.json"),
}


def render_files(pack: dict, kind: str) -> list[dict]:
    """包内文件清单。`kind` 决定子集；`full` 全量。顺序与 `_KIND_FILES` 一致（测试断言用）。"""
    snapshot = pack["snapshot"]
    prov = snapshot.get("provenance") or {}
    selected = _KIND_FILES.get(kind, _KIND_FILES["full"])
    # 先算「这个 kind 会含哪些文件」（README 的「本包里有什么」要如实按它列）。
    included_files: list[str] = []
    for token in selected:
        if token == "download/":
            # download 文件在 build_export_pack 已带 "download/" 前缀。
            included_files += [f["path"] for f in pack["download"]["files"]]
        else:
            included_files.append(token)
    pack["included_files"] = included_files

    manifest = _render_manifest_md(pack["manifest_rows"])
    manifest_tsv = _render_manifest_tsv(pack["manifest_rows"])
    readme = _render_readme(pack, kind)
    candidates_md = _render_candidates_md(snapshot, pack["manifest_rows"])
    candidates_tsv = _render_candidates_tsv(snapshot, pack["manifest_rows"])

    by_path: dict[str, dict] = {
        "README.md": _plain("README.md", readme),
        "manifest.md": _plain("manifest.md", manifest),
        "manifest.tsv": _plain("manifest.tsv", manifest_tsv),
        "candidates.md": _plain("candidates.md", candidates_md),
        "candidates.tsv": _plain("candidates.tsv", candidates_tsv),
        "citations.ris": _plain("citations.ris", pack["citations"]["ris"]),
        "citations.bib": _plain("citations.bib", pack["citations"]["bibtex"]),
        "citations-gb7714.txt": _plain("citations-gb7714.txt", pack["citations"]["gb7714"]),
        "download/": None,
        "provenance.md": _plain("provenance.md", _provenance_md(prov)),
        "method-draft.md": _plain("method-draft.md", _method_draft(prov, snapshot, pack["exported_at"])),
        "recipe.json": _plain("recipe.json", _render_recipe_json(pack)),
    }
    for f in pack["download"]["files"]:
        by_path[f["path"]] = f

    files: list[dict] = []
    for token in selected:
        if token == "download/":
            # 保持 download_files 的原始顺序（task_pack 契约顺序：manifest 先、脚本后）。
            files.extend(by_path[f["path"]] for f in pack["download"]["files"])
            continue
        entry = by_path[token]
        if entry is not None:
            files.append(entry)
    pack["included_files"] = [f["path"] for f in files]
    return files
