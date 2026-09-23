# -*- coding: utf-8 -*-
"""一句话任务包：一次检索 → 结果清单 + 下载脚本 + FAIR 自检 + 引文，**四件套口径一致**。

纯函数、离线、只读、不写盘、不调用大模型。检索器 / 编排 / 冻结评测从不 import 本模块。

## 这个模块存在的全部理由：口径一致

用户要的四样东西各自都已经有了（结果来自检索、下载来自文件清单、FAIR 来自 fair.py、
引文来自 reuse_pack.py）。把它们凑到一起最容易出的错不是缺功能，而是**互相矛盾**：
结果清单里 10 条、下载脚本里 8 条、引文里 9 条，各自都"没说错"，合起来就是一份没法用的材料。

所以这里只做一件事：**一次算，四处扇出**。同一批数据集编号、同一份目录快照、同一个检索日期，
出口再逐一比对三处 uid 集合，不等就报内部错误而不是发出去。

## plan_token：先看清单再产包

预览时把「候选池 + 各档 + 文件行指纹 + 目录快照 + 内容指纹 + 检索参数 + 诚实层指纹」
压成一个 token。产包时用同样的参数重跑一遍再算一次，对不上就明确告诉用户哪里变了、
**一个字节都不产出**，而不是默默发一份与预览不符的包。

**锁的是候选池，不是勾选。** 用户勾掉两条是完全正常的操作，
如果把 selected_uids 也压进 token，他每勾一下都会撞 409、重新预览又把勾掉的加回来——死循环。
"""
from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import date
from typing import Any, Sequence

from ..corpus import download_plan as DP
from ..corpus import download_script as DS
from . import item_view, reuse_pack
from ..retrieval import fair
from ..corpus import provenance
from ..corpus.corpus import corpus_snapshot

SCHEMA = "biodata-task-pack/v1"
SPEC_SCHEMA = "biodata-task-pack-spec/v2"

DEFAULT_PREVIEW_ITEMS = 10
ALLOWED_LIMITS = (10, 20, 50)
MAX_PACK_ITEMS = 50
MAX_ZIP_BYTES = 8 * 1024 * 1024

#: 包里固定这些文件，顺序固定。测试直接对这个常量断言，而不是抄实现里的 namelist。
PACK_FILES = (
    "00-START-HERE.txt", "README.md", "datasets.md", "file-list.md",
    "manifest.tsv", "manifest.json", "download.sh", "download.ps1",
    "todo.md", "fair_report.md", "citations.ris", "citations.bib",
    "reuse_provenance.md", "provenance.json",
)


class TaskPackError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def sanitize_uids_for_pack(raw: Any) -> list[str]:
    if raw is None or isinstance(raw, (str, bytes, dict)):
        raise TaskPackError("bad_param", "数据集编号必须是字符串数组。")
    out, seen = [], set()
    for value in list(raw):
        if not isinstance(value, str):
            raise TaskPackError("bad_param", f"数据集编号必须是字符串，收到 {type(value).__name__}。")
        text = value.strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    if not out:
        raise TaskPackError("empty_input", "没有选中任何数据集。")
    if len(out) > MAX_PACK_ITEMS:
        raise TaskPackError("too_many", f"一次最多 {MAX_PACK_ITEMS} 个数据集，收到 {len(out)} 个。")
    return out


#: 候选池只有三档，但**进包的可以是候选池的任意子集**（`plan_spec` 刻意不含 `selected_uids`，
#: 产包时服务端按 `selected_uids` 重建）。所以「打包前 5 条」不需要把档位改成 5：取 10 条的池、
#: 只勾前 5 条即可，且这 5 条就是确定性排序前 5 条。前端 `tpPoolFor()` 就是干这个的。
#: 结论：**这里不放宽成 1..50**——放宽会把 HTTP/MCP 契约、四处文案和一道测试全拖下水，
#: 换不到任何用户能感知的差别。
_LIMITS_ZH = "、".join(str(x) for x in ALLOWED_LIMITS)


def sanitize_limit(raw: Any) -> int:
    if raw is None:
        return DEFAULT_PREVIEW_ITEMS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        # 别把 Python 元组 repr 直接甩给用户看：`(10, 20, 50)` 不是中文。
        raise TaskPackError("bad_param", f"候选池条数只能是 {_LIMITS_ZH} 之一。") from None
    if value not in ALLOWED_LIMITS:
        raise TaskPackError("bad_param", f"候选池条数只能是 {_LIMITS_ZH} 之一。")
    return value


def sanitize_scope(raw: Any) -> str:
    scope = str(raw or DP.SCOPE_PRIMARY).strip() or DP.SCOPE_PRIMARY
    if scope not in DP.SCOPES:
        raise TaskPackError("bad_param", f"scope 只能是 {'/'.join(DP.SCOPES)}。")
    return scope


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _digest(payload: Any, length: int = 16) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()[:length]


def rows_digest(plan: dict) -> str:
    """文件行的指纹：同一批数据集、但来源换了文件（改版、补 md5）时也能发现。"""
    lines = sorted(
        f"{r['dataset_uid']}\t{r['safe_name']}\t{r['bytes'] or '-'}\t{r['md5sum'] or '-'}"
        for r in plan.get("rows", [])
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:16]


def plan_spec(*, candidate_uids: Sequence[str], plan: dict, snapshot: dict,
              retrieval_date: str, scope: str, retrieval_params: dict,
              honesty: dict) -> dict:
    """token 的原像。**刻意不含 selected_uids**——见模块头「锁的是候选池，不是勾选」。"""
    return {
        "schema": SPEC_SCHEMA,
        "candidate_uids": list(candidate_uids),
        "tiers": {it["dataset_uid"]: it["tier"] for it in plan.get("items", [])},
        "rows_digest": rows_digest(plan),
        "snapshot_id": snapshot.get("snapshot_id", ""),
        "content_digest": snapshot.get("content_digest", ""),
        "retrieval_date": retrieval_date,
        "scope": scope,
        "retrieval_params": retrieval_params,
        "honesty_digest": _digest(honesty),
    }


def plan_token(spec: dict) -> str:
    return _digest(spec)


def resolve_membership(uids: Sequence[str], records: Sequence[Any],
                       result_uids: "set[str] | None" = None) -> dict:
    """这些编号在不在库里、在不在这次结果里。**三类分开，措辞各不相同，绝不猜用户做过什么。**"""
    index: dict[str, Any] = {}
    for record in records:
        raw = record.raw if isinstance(getattr(record, "raw", None), dict) else {}
        uid = str(raw.get("dataset_uid") or "")
        if uid and uid not in index:
            index[uid] = record
    resolved, unresolved, membership = [], [], {}
    for uid in uids:
        record = index.get(uid)
        if record is None:
            unresolved.append(uid)
            membership[uid] = "not_in_catalog"
            continue
        resolved.append(record)
        if result_uids is None:
            membership[uid] = "unverified"
        else:
            membership[uid] = "in_result_set" if uid in result_uids else "out_of_result_set"
    return {"records": resolved, "unresolved": unresolved, "membership": membership}


def build_task_pack(*, query: str, items: Sequence[dict], records: Sequence[Any],
                    unresolved: Sequence[str] = (), scope: str = DP.SCOPE_PRIMARY,
                    retrieval_params: "dict | None" = None, honesty: "dict | None" = None,
                    today: "str | None" = None, membership: "dict | None" = None) -> dict:
    """一次算、四处扇出。**这是唯一的编排点**，别处不许再拼一遍。"""
    scope = sanitize_scope(scope)
    items = [DP.assert_item_shape(dict(it)) for it in items]
    records = list(records)
    snapshot = corpus_snapshot(records, with_content=True)
    retrieval_date = today or date.today().isoformat()
    honesty = dict(honesty or {})
    params = dict(retrieval_params or {})

    plan = DP.build_plan(items, scope=scope)
    fair_reports = fair.build_fair_reports(items)
    citation = reuse_pack.build_reuse_pack(items, list(unresolved),
                                           snapshot=snapshot, retrieval_date=retrieval_date)

    spec = plan_spec(candidate_uids=[it["dataset_uid"] for it in items], plan=plan,
                     snapshot=snapshot, retrieval_date=retrieval_date, scope=scope,
                     retrieval_params=params, honesty=honesty)
    token = plan_token(spec)

    pack = {
        "schema": SCHEMA,
        "plan_token": token,
        "query": query,
        "scope": scope,
        "retrieval": {
            "date": retrieval_date,
            "snapshot_id": snapshot.get("snapshot_id", ""),
            "content_digest": snapshot.get("content_digest", ""),
            "n_corpus_records": snapshot.get("n_records", 0),
            "sources": snapshot.get("sources", {}),
            "snapshot_meaning_zh": "数据集目录快照编号：只标识编号集合，不覆盖字段内容。",
            "content_meaning_zh": "内容指纹：覆盖本包用到的字段取值，来源补录字段会让它改变。",
        },
        "retrieval_params": params,
        "membership": dict(membership or {}),
        "items": items,
        "plan": plan,
        "fair_reports": fair_reports,
        "citation": {
            "pack": citation,
            "markdown": reuse_pack.to_markdown(citation),
            "ris": reuse_pack.to_ris(citation),
            "bibtex": reuse_pack.to_bibtex(citation),
        },
        "honesty": honesty,
        "unresolved": list(unresolved),
    }
    pack["todo"] = build_todo(pack)
    _assert_single_scope(pack)
    return pack


def _assert_single_scope(pack: dict) -> None:
    """四件套的数据集集合必须逐一相等。不等就报内部错误，绝不把自相矛盾的材料发出去。"""
    items = {it["dataset_uid"] for it in pack["items"]}
    planned = {it["dataset_uid"] for it in pack["plan"]["items"]}
    reported = {r.get("dataset_uid") for r in pack["fair_reports"]}
    cited = {r.get("dataset_uid") for r in pack["citation"]["pack"].get("table", [])}
    if not (items == planned == reported == cited):
        raise TaskPackError(
            "internal_scope_mismatch",
            "四份产物覆盖的数据集不一致："
            f"清单 {len(items)} / 下载计划 {len(planned)} / FAIR {len(reported)} / 引文 {len(cited)}。",
        )


def build_todo(pack: dict) -> list[str]:
    """本工具帮不上、必须用户自己确认的事。**先说坏消息。**"""
    plan = pack["plan"]
    todo: list[str] = [DS.primary_only_sentence(plan)]
    manual = plan.get("manual", [])
    if manual:
        todo.append(f"{len(manual)} 个数据集脚本下不了，只能到数据集页面自己取："
                    + "、".join(m["dataset_name"] for m in manual[:5])
                    + ("…" if len(manual) > 5 else "") + "。这不表示它们不能下载。")
    without_md5 = plan["estimate"].get("n_files_without_md5", 0)
    unverifiable = plan["estimate"].get("n_files_unverifiable", 0)
    if without_md5 - unverifiable > 0:
        todo.append(f"{without_md5 - unverifiable} 个文件来源没有给校验和，脚本下完只能比对字节数，"
                    "字节数对得上也不代表内容一定正确。")
    if unverifiable:
        todo.append(f"{unverifiable} 个文件来源既没给校验和也没给大小，脚本下完无法做任何核验，"
                    "报告里会如实记成「未能核对」（unverified）。")
    flagged = plan["estimate"].get("n_flagged_files", {})
    if flagged.get("dead"):
        todo.append(f"{flagged['dead']} 个文件在最近一次巡检里没能取到，脚本默认跳过；"
                    "请到数据集页面确认地址是否变了。")
    if flagged.get("size_mismatch"):
        todo.append(f"{flagged['size_mismatch']} 个文件来源报告的大小与清单不一致，脚本默认跳过。")
    if plan["ledger"].get("degrade_sentence_zh"):
        todo.append(plan["ledger"]["degrade_sentence_zh"])
    todo.extend(pack["citation"]["pack"].get("gaps", []))
    caveats = pack.get("honesty", {}).get("coverage_caveats") or []
    for caveat in caveats:
        todo.append(f"还有 {caveat.get('count')} 条数据可能也符合你的要求，"
                    f"但它们没有填写「{caveat.get('label')}」这一项，本次没有收进来。"
                    "想看它们：回到检索结果页，在结果摘要那段文字里点「也纳入」。")
    unused = pack.get("honesty", {}).get("unused_query_terms") or []
    if unused:
        todo.append("这几个词没有作为筛选条件用上：" + "、".join(unused)
                    + "。系统里没有对应的可筛选项。")
    if pack.get("unresolved"):
        todo.append(f"{len(pack['unresolved'])} 个数据集在当前库中找不到，没有收进本包。")
    todo.append("本工具只处理公开目录的元数据，不接收你未发表的实验信息；"
                "投稿前请把引文与你自己产出数据的声明合并。")
    return todo


# ---------------------------------------------------------------- 产物渲染

def _render_readme(pack: dict) -> str:
    plan, est = pack["plan"], pack["plan"]["estimate"]
    ledger_source = plan["ledger"].get("n_ledger_source_in_scope", 0)
    lines = [
        "# 这一包是什么", "",
        f"你这次检索里被收录的 {est['n_datasets_selected']} 个公开数据集的清单、下载脚本、"
        "复用就绪度自检和引文。**包里没有任何数据文件。**", "",
        "先做什么：请看同目录的 00-START-HERE.txt。", "",
        DS.primary_only_sentence(plan), "",
        f"文件级清单只覆盖 10x Genomics 的数据集；本包 {est['n_datasets_selected']} 条里有 "
        f"{ledger_source} 条属于它。其余来源的文件级可及性本工具未核验。", "",
        "本工具只处理公开目录的元数据，不接收你未发表的实验信息。", "",
        "## 这一包按什么档位分类", "",
    ]
    for tier in plan["tiers"]:
        note = ""
        if tier["count"] > 0 and tier["rows"] == 0 and tier["tier"] in (DP.TIER_CHECKSUM, DP.TIER_SIZE):
            note = "（这一档有数据集，但本包没有为它们生成下载命令）"
        lines.append(f"- **{tier['label_zh']} {tier['count']} 个**（下载命令 {tier['rows']} 条）{note}：{tier['explain_zh']}")
    lines += ["", "## 需要你自己确认的事", ""]
    lines += [f"- {x}" for x in pack["todo"]]
    lines += ["", "## 供复现（不需要理解）", "",
              f"- 检索语句：{pack['query']}",
              f"- 检索日期：{pack['retrieval']['date']}",
              f"- 数据集目录快照编号：{pack['retrieval']['snapshot_id']}"
              f"（{pack['retrieval']['snapshot_meaning_zh']}）",
              f"- 内容指纹：{pack['retrieval']['content_digest']}"
              f"（{pack['retrieval']['content_meaning_zh']}）",
              f"- 本包编号：{pack['plan_token']}"]
    return "\n".join(lines) + "\n"


def _render_datasets_md(pack: dict) -> str:
    lines = ["# 收录的数据集", "",
             f"检索语句：{pack['query']}　·　检索日期：{pack['retrieval']['date']}", "",
             "| 数据集 | 来源 | 物种 | 组织 | 下载方式 | 页面 |", "|---|---|---|---|---|---|"]
    tiers = {it["dataset_uid"]: it["tier"] for it in pack["plan"]["items"]}
    for item in pack["items"]:
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            DS._cell(item.get("dataset_name")), DS._cell(item.get("source")),
            DS._cell(item.get("species") or "未标注"), DS._cell(item.get("tissue") or "未标注"),
            DP.tier_label(tiers.get(item["dataset_uid"], "")),
            DS._cell(item.get("url") or "—")))
    return "\n".join(lines) + "\n"


def _render_fair_md(pack: dict) -> str:
    lines = ["# 复用就绪度自检", "",
             "逐个数据集看：要复用它，元数据够不够、缺什么。**这是对元数据的检查，不是对数据质量的评价。**", ""]
    by_uid = {it["dataset_uid"]: it for it in pack["items"]}
    for report in pack["fair_reports"]:
        item = by_uid.get(report.get("dataset_uid"), {})
        lines += [f"## {item.get('dataset_name') or report.get('dataset_uid')}", ""]
        for check in report.get("checks", []):
            lines.append(f"- [{check.get('status')}] {check.get('label')}：{check.get('evidence')}")
            if check.get("action"):
                lines.append(f"  - 要补的话：{check['action']}")
        statement = report.get("data_availability", {}).get("statement_en")
        if statement:
            lines += ["", "可放进投稿材料的英文句：", "", f"> {statement}", ""]
    return "\n".join(lines) + "\n"


def _render_todo_md(pack: dict) -> str:
    lines = ["# 需要你自己确认的事", "",
             "下面每一条都是本工具**帮不上**的部分。不列出来，它们就会变成你以为已经处理好的事。", ""]
    lines += [f"- {x}" for x in pack["todo"]]
    return "\n".join(lines) + "\n"


def _render_provenance_json(pack: dict) -> str:
    return json.dumps({
        "schema": SCHEMA,
        "plan_token": pack["plan_token"],
        "query": pack["query"],
        "retrieval": pack["retrieval"],
        "retrieval_params": pack["retrieval_params"],
        "membership": pack["membership"],
        "scope": pack["scope"],
        "tiers": pack["plan"]["tiers"],
        "estimate": pack["plan"]["estimate"],
        "ledger": pack["plan"]["ledger"],
        "inspection": pack["plan"]["inspection"],
        "honesty": pack["honesty"],
        "unresolved": pack["unresolved"],
    }, ensure_ascii=False, indent=1) + "\n"


def render_files(pack: dict) -> list[dict]:
    """包里的全部 14 个文件。顺序与 `PACK_FILES` 一致，缺一即测试红。"""
    plan = pack["plan"]
    meta = {"plan_token": pack["plan_token"], "snapshot_id": pack["retrieval"]["snapshot_id"],
            "content_digest": pack["retrieval"]["content_digest"],
            "retrieval_date": pack["retrieval"]["date"]}
    byname = {f["path"]: f for f in DS.artifact_files(plan, meta)}
    plain = lambda path, text: {"path": path, "text": text, "newline": "\n", "bom": False}
    files = [
        plain("00-START-HERE.txt", DS.render_start_here(plan)),
        plain("README.md", _render_readme(pack)),
        plain("datasets.md", _render_datasets_md(pack)),
        byname["file-list.md"],
        byname["manifest.tsv"],
        byname["manifest.json"],
        byname["download.sh"],
        byname["download.ps1"],
        plain("todo.md", _render_todo_md(pack)),
        plain("fair_report.md", _render_fair_md(pack)),
        plain("citations.ris", pack["citation"]["ris"]),
        plain("citations.bib", pack["citation"]["bibtex"]),
        plain("reuse_provenance.md", pack["citation"]["markdown"]),
        plain("provenance.json", _render_provenance_json(pack)),
    ]
    names = tuple(f["path"] for f in files)
    if names != PACK_FILES:
        raise TaskPackError("internal_scope_mismatch",
                            f"包内文件与约定不符：{names} != {PACK_FILES}")
    return files


def files_to_zip_bytes(files: Sequence[dict]) -> bytes:
    """内存里打包。服务端零写盘——多一个写盘目录就多一对必须手工对账的忽略清单。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for entry in files:
            text = entry["text"].replace("\r\n", "\n")
            if entry.get("newline") == "\r\n":
                text = text.replace("\n", "\r\n")
            data = text.encode("utf-8")
            if entry.get("bom"):
                data = b"\xef\xbb\xbf" + data
            info = zipfile.ZipInfo(entry["path"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # .sh 要可执行，否则 macOS/Linux 用户还得先 chmod。
            info.external_attr = (0o100755 if entry.get("executable") else 0o100644) << 16
            archive.writestr(info, data)
    payload = buffer.getvalue()
    if len(payload) > MAX_ZIP_BYTES:
        raise TaskPackError("too_large",
                            f"打出来的包有 {len(payload) // 1024} KB，超过上限。请少选几个数据集再试。")
    return payload
