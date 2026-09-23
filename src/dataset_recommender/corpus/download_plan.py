# -*- coding: utf-8 -*-
"""下载计划：把一批数据集变成「哪些文件能下、怎么核对、哪些根本下不了」。

纯函数、离线、只读、**不写盘、不联网**。检索器 / 编排 / 冻结评测从不 import 本模块。

## 四档，以及为什么必须是四档

用户最想知道的其实只有一件事：**这个数据集我到底能不能用脚本拉下来、拉下来算不算数。**
一档「能不能下载」回答不了它，所以分成四档：

- `checksum_verifiable` 来源给了文件清单，且本次选中的每个文件都带 md5 → 脚本能下、能逐字节核对。
- `size_only`           有直链、有文件大小，但没有 md5 → 只能比对字节数。
- `direct_unsized`      有一个不同于页面地址的链接，但既没有文件名、也没有大小和校验和 →
                        本工具没核验过它是不是一个文件，**不为它生成下载命令**。
- `page_only`           只有数据集页面地址 → 不生成下载命令。这**不表示它不能下载**，
                        只表示本工具没有这个来源的文件级清单。

## 两条踩过的坑，写进判据里

1. **绝不用「有没有 download_url」判定**。`item_view` 的取值链带 `or record.url` 兜底，
   于是每一条的 `download_url` 都非空——按这个判，全库都会被说成「可下载」。
   正确判据是 `download_url` 与页面 `url` **不同**。
2. **md5 覆盖率要用 all() 而不是 any()**。一个数据集 100 个文件里只有 1 个带 md5，
   按 any() 会被整体说成「可核对校验和」，脚本跑完还会打印「全部通过」。

## 入参形状必须 fail-closed

不同调用方喂进来的 item 形状不同（检索序列化的 payload 里没有 `filesize`，
而 `item_view.build_item` 有）。少一个键就静默降档，会让同一批数据集在两个入口落到不同的档，
用户看到的两处数字互相矛盾。所以缺键直接报错，绝不猜。
"""
from __future__ import annotations

from ..content.labels import UNNAMED_DATASET

import re
from typing import Any, Sequence
from urllib.parse import unquote, urlsplit

from . import downloads, inspection, provenance, reachability

TIER_CHECKSUM = "checksum_verifiable"
TIER_SIZE = "size_only"
TIER_DIRECT = "direct_unsized"
TIER_PAGE = "page_only"
TIERS = (TIER_CHECKSUM, TIER_SIZE, TIER_DIRECT, TIER_PAGE)

#: 四档的中文说明。面板、README、文件清单、脚本注释共用这一份，不许各写各的。
_TIER_ZH = {
    TIER_CHECKSUM: "可下载并核对校验和：来源给出了文件清单，且本包选中的每一个文件都带 md5，"
                   "脚本可以下载并与来源声明的 md5 比对。",
    TIER_SIZE: "可下载，只能核对文件大小：来源给了文件直链和文件大小，没有给 md5，脚本只能比对字节数。",
    TIER_DIRECT: "来源给了一个与页面地址不同的链接，但没有文件名、大小，也没有校验和；"
                 "本工具没有核验过它是不是一个文件，也没有为它生成下载命令。",
    TIER_PAGE: "只有数据集页面地址：本工具没有该来源的文件级清单，没有为它生成下载命令。"
               "这不表示它不能下载，只表示本工具没有核验过。",
}
_TIER_LABEL = {
    TIER_CHECKSUM: "可下载并核对校验和",
    TIER_SIZE: "只能核对大小",
    TIER_DIRECT: "只有一个未核验的链接",
    TIER_PAGE: "只有页面地址",
}

#: 巡检发现的两类问题，措辞必须分开——「链接失效」和「大小对不上」是两回事，
#: 混成一句「有问题」用户不知道该不该重试。
_FLAG_ZH = {
    # 措辞必须与脚本真实行为一致：这一行**照样进 manifest**，只是脚本默认跳过它，
    # 加 --include-flagged 就会真的下。写成「不生成下载命令」用户会以为它根本不在包里。
    "dead": "最近一次巡检（{date}）这个直链没有返回文件，脚本默认跳过；"
            "来源可能已经换了地址，请先到数据集页面确认，确实要试请加 --include-flagged。",
    "size_mismatch": "最近一次巡检（{date}）来源报告的大小与清单不一致，脚本默认跳过；"
                     "确实要下载请加 --include-flagged，下载后请自行确认内容。",
}

REQUIRED_ITEM_KEYS = ("dataset_uid", "url", "download_url", "filesize", "source")

SCOPE_PRIMARY = "primary"
SCOPE_ALL = "all"
SCOPES = (SCOPE_PRIMARY, SCOPE_ALL)

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_DOS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class DownloadPlanError(ValueError):
    """入参非法。带 `code` 供 HTTP / MCP 映射成稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def tier_text(tier: str) -> str:
    return _TIER_ZH.get(tier, "")


def tier_label(tier: str) -> str:
    return _TIER_LABEL.get(tier, tier)


def assert_item_shape(item: Any) -> dict:
    """喂进来的东西必须是同一种形状。少一个键就报错，**绝不静默降档**。"""
    if not isinstance(item, dict):
        raise DownloadPlanError("bad_item_shape", "每条数据集必须是一个对象。")
    missing = [key for key in REQUIRED_ITEM_KEYS if key not in item]
    if missing:
        raise DownloadPlanError(
            "bad_item_shape",
            "数据集缺少判定下载方式所需的内容：" + "、".join(missing)
            + "。不同入口喂不同形状会让同一批数据集落到不同的档，所以这里不猜。",
        )
    return item


def safe_name(name: str, taken: "set[str] | None" = None) -> str:
    """把来源给的文件名变成在 Windows / macOS / Linux 上都能落地的名字。

    实证：10x 有一批文件名里带冒号（`jurkat:293t_50:50_…`）。在 NTFS 上直接写会被当成
    备用数据流——文件看起来存在、大小是 0、md5 永远对不上，而且没有任何报错。
    """
    text = unquote(str(name or "")).strip().replace("\n", " ")
    text = _UNSAFE.sub("_", text)
    text = re.sub(r"_{2,}", "_", text).strip(" .")
    if not text:
        text = "file"
    stem, dot, ext = text.rpartition(".")
    if dot and (stem.upper() in _DOS_RESERVED):
        text = stem + "_" + dot + ext
    elif text.upper() in _DOS_RESERVED:
        text = text + "_"
    if len(text) > 150:
        stem, dot, ext = text.rpartition(".")
        keep = 150 - (len(ext) + 1 if dot else 0)
        text = (stem[:keep] + dot + ext) if dot else text[:150]
    if taken is None:
        return text
    candidate, index = text, 2
    while candidate.lower() in taken:
        stem, dot, ext = text.rpartition(".")
        candidate = (f"{stem}~{index}.{ext}" if dot else f"{text}~{index}")
        index += 1
    taken.add(candidate.lower())
    return candidate


def safe_uid(uid: str) -> str:
    """数据集编号当目录名用。全部外部来源的编号都带 `cxg:` / `ae:` 这样的冒号前缀。"""
    return safe_name(str(uid or "dataset").replace(":", "_"))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _files_of(uid: str) -> list[dict]:
    return [f for f in downloads.files_for(uid) if isinstance(f, dict) and f.get("download_url")]


def select_files(item: dict, files: Sequence[dict], scope: str) -> list[dict]:
    """本次要下哪些文件。

    默认只取**每个数据集一个代表性主文件**。这不是省事：全部 15119 个文件合计 56.70 TB，
    其中原始测序数据就占 31.17 TB。默认把这些一起塞给用户，等于给他一个会跑一整周的脚本。
    """
    if not files:
        return []
    if scope == SCOPE_ALL:
        return list(files)
    primary = downloads.primary_url(item.get("dataset_uid")) or ""
    for candidate in files:
        if candidate.get("download_url") == primary:
            return [candidate]
    return [files[0]]


def classify_download_tier(item: dict, files: "Sequence[dict] | None" = None,
                           scope: str = SCOPE_PRIMARY) -> dict:
    """这个数据集属于哪一档，以及**凭什么这么说**（判据要能被人复核）。"""
    assert_item_shape(item)
    uid = _text(item.get("dataset_uid"))
    known = list(files) if files is not None else _files_of(uid)
    chosen = select_files(item, known, scope)
    if chosen:
        with_md5 = sum(1 for f in chosen if _text(f.get("md5sum")))
        if with_md5 == len(chosen):
            return {"tier": TIER_CHECKSUM,
                    "evidence": f"来源清单里有 {len(known)} 个文件，本次选中 {len(chosen)} 个，都带 md5"}
        return {"tier": TIER_SIZE,
                "evidence": f"来源清单里有 {len(known)} 个文件，本次选中 {len(chosen)} 个，"
                            f"其中 {with_md5} 个带 md5"}

    download_url, page_url = _text(item.get("download_url")), _text(item.get("url"))
    if not download_url or download_url == page_url:
        return {"tier": TIER_PAGE, "evidence": "只有数据集页面地址，没有文件级清单"}
    size = provenance.size_bytes_or_none(item)
    if size is not None:
        return {"tier": TIER_SIZE, "evidence": f"有与页面不同的直链，来源声明大小 {size} 字节，没有 md5"}
    return {"tier": TIER_DIRECT, "evidence": "有与页面不同的直链，但既没有文件名、也没有大小和校验和"}


def _synth_row(item: dict) -> "dict | None":
    """来源没有文件级清单、但给了一个带大小的直链时，合成一行下载计划。"""
    download_url = _text(item.get("download_url"))
    size = provenance.size_bytes_or_none(item)
    if not download_url or size is None:
        return None
    base = urlsplit(download_url).path.rsplit("/", 1)[-1]
    derived = not bool(base)
    filename = base or (safe_uid(_text(item.get("dataset_uid"))) + ".bin")
    return {"filename": filename, "filename_derived": derived, "download_url": download_url,
            "bytes": size, "md5sum": None, "category": "", "pipeline": ""}


def build_plan(items: Sequence[dict], *, scope: str = SCOPE_PRIMARY) -> dict:
    """一批数据集 → 完整的下载计划。**每个数据集都会出现在 items 里**，包括下不了的那些。

    下不了的也留一行（`rows_planned=0`）并同时进 `manual`，这样「结果清单 / 下载计划 /
    FAIR / 引文」四份产物的数据集集合可以逐一相等地机械核对——少一条就红，而不是靠人看。
    """
    if scope not in SCOPES:
        raise DownloadPlanError("bad_param", f"scope 只能是 {'/'.join(SCOPES)}。")
    plan_items: list[dict] = []
    rows: list[dict] = []
    manual: list[dict] = []
    taken_dirs: set[str] = set()
    ledger_source_in_scope = ledger_matched = 0

    for raw_item in items:
        item = assert_item_shape(raw_item)
        uid = _text(item.get("dataset_uid"))
        source = _text(item.get("source"))
        known = _files_of(uid)
        has_ledger_source = provenance.SOURCE_10X.lower() in source.lower()
        if has_ledger_source:
            ledger_source_in_scope += 1
            if known:
                ledger_matched += 1

        verdict = classify_download_tier(item, known, scope)
        tier = verdict["tier"]
        chosen = select_files(item, known, scope)
        if not chosen and tier == TIER_SIZE:
            synthetic = _synth_row(item)
            chosen = [synthetic] if synthetic else []

        dataset_dir = safe_name(safe_uid(uid), taken_dirs)
        taken_names: set[str] = set()
        planned: list[dict] = []
        problem_files: list[dict] = []
        bytes_selected = 0
        for entry in chosen:
            url = _text(entry.get("download_url"))
            if not url:
                continue
            status = inspection.status_for(uid, url) or {}
            flag_kind = None
            if status.get("reachable") not in (None, "ok"):
                flag_kind = "dead"
            elif status.get("size") == "mismatch":
                flag_kind = "size_mismatch"
            filename = _text(entry.get("filename")) or urlsplit(url).path.rsplit("/", 1)[-1] or "file.bin"
            size_bytes = entry.get("bytes")
            try:
                size_bytes = int(size_bytes) if size_bytes is not None else None
            except (TypeError, ValueError):
                size_bytes = None
            row = {
                "dataset_uid": uid,
                "safe_uid": dataset_dir,
                "source": source,
                "tier": tier,
                "filename": filename,
                "filename_derived": bool(entry.get("filename_derived")),
                "safe_name": safe_name(filename, taken_names),
                "download_url": url,
                "netloc": (urlsplit(url).hostname or "").lower(),
                "bytes": size_bytes,
                "md5sum": _text(entry.get("md5sum")) or None,
                "verify": "md5" if _text(entry.get("md5sum")) else ("size" if size_bytes else "none"),
                "category": _text(entry.get("category")),
                "pipeline": _text(entry.get("pipeline")),
                "flag_kind": flag_kind,
                "flag_reason_zh": (_FLAG_ZH[flag_kind].format(date=status.get("last_verified") or "未记录")
                                   if flag_kind else ""),
                "last_verified": status.get("last_verified") or "",
            }
            planned.append(row)
            if size_bytes:
                bytes_selected += size_bytes
            if flag_kind:
                problem_files.append({"filename": filename, "flag_kind": flag_kind,
                                      "reason_zh": row["flag_reason_zh"],
                                      "last_verified": row["last_verified"]})

        rows.extend(planned)
        ledger = downloads.get(uid) or {}
        n_files_total = int(ledger.get("n_files") or 0) if ledger else len(known)
        fastq_url = downloads.fastq_url(uid)
        n_fastq_excluded = sum(
            1 for f in known
            if f.get("download_url") == fastq_url or "fastq" in _text(f.get("filename")).lower()
        ) if scope == SCOPE_PRIMARY else 0
        bytes_excluded = max(0, sum(int(f.get("bytes") or 0) for f in known) - bytes_selected)

        plan_items.append({
            "dataset_uid": uid,
            "safe_uid": dataset_dir,
            "dataset_name": _text(item.get("dataset_name")) or UNNAMED_DATASET,
            "source": source or "未说明",
            "page_url": _text(item.get("url")),
            "tier": tier,
            "tier_evidence": verdict["evidence"],
            "n_files_total": n_files_total,
            "n_files_selected": len(planned),
            "n_fastq_files_excluded": n_fastq_excluded,
            "bytes_selected": bytes_selected,
            "bytes_excluded_lower_bound": bytes_excluded,
            "n_files_with_md5": sum(1 for r in planned if r["md5sum"]),
            "rows_planned": len(planned),
            "problem_files": problem_files,
            "reachability": reachability.classify(_text(item.get("download_url")) or _text(item.get("url"))),
        })
        if tier in (TIER_PAGE, TIER_DIRECT) or not planned:
            manual.append({
                "dataset_uid": uid,
                "dataset_name": plan_items[-1]["dataset_name"],
                "source": plan_items[-1]["source"],
                "page_url": plan_items[-1]["page_url"],
                "tier": tier,
                "why_zh": tier_text(tier),
            })

    tiers = []
    for tier in TIERS:
        mine = [it for it in plan_items if it["tier"] == tier]
        tiers.append({
            "tier": tier,
            "label_zh": tier_label(tier),
            "count": len(mine),
            "rows": sum(it["rows_planned"] for it in mine),
            "explain_zh": tier_text(tier),
        })

    snapshot = inspection.snapshot_info() or {}
    return {
        "scope": scope,
        "items": plan_items,
        "rows": rows,
        "manual": manual,
        "tiers": tiers,
        "allowed_hosts": sorted({r["netloc"] for r in rows if r["netloc"]}),
        "estimate": plan_totals(plan_items, rows),
        "ledger": _ledger_state(ledger_source_in_scope, ledger_matched),
        "inspection": {
            "ready": inspection.is_available(),
            "snapshot_id": snapshot.get("snapshot_id", ""),
            "snapshot_date": (snapshot.get("snapshot_date") or "")[:10],
            "scope_zh": "巡检只覆盖 10x Genomics 的文件清单，不覆盖其它来源。",
        },
    }


def plan_totals(plan_items: Sequence[dict], rows: Sequence[dict]) -> dict:
    # 「另有几个文件没列入」必须**逐个数据集**取 max(0, 总数-选中)再求和，不能整包相减：
    # 来源没有文件级清单、但给了带大小的直链时（`_synth_row`），这一条的 n_files_total 是 0
    # 而选中是 1，整包相减会被它抵掉一个——每混进一条这样的数据集，就少报一个被排除的文件。
    n_excluded = sum(max(0, int(it.get("n_files_total") or 0) - int(it.get("n_files_selected") or 0))
                     for it in plan_items)
    return {
        "n_datasets_selected": len(plan_items),
        "n_files_selected": len(rows),
        "bytes_selected_lower_bound": sum(int(r.get("bytes") or 0) for r in rows),
        "n_files_total_in_source": sum(int(it.get("n_files_total") or 0) for it in plan_items),
        "n_files_excluded": n_excluded,
        "n_fastq_files_excluded": sum(int(it.get("n_fastq_files_excluded") or 0) for it in plan_items),
        "bytes_excluded_lower_bound": sum(int(it.get("bytes_excluded_lower_bound") or 0) for it in plan_items),
        "n_page_only_datasets": sum(1 for it in plan_items if it["tier"] == TIER_PAGE),
        "n_files_without_md5": sum(1 for r in rows if not r.get("md5sum")),
        # 「没有 md5」里还要再分一层：有大小的脚本能比字节数，两样都没有的**下完谁也核不了**。
        # 混成一句「只能比对字节数」，等于替后一种情况许了一个脚本兑现不了的承诺。
        "n_files_unverifiable": sum(1 for r in rows if r.get("verify") == "none"),
        "n_flagged_files": {
            "dead": sum(1 for r in rows if r.get("flag_kind") == "dead"),
            "size_mismatch": sum(1 for r in rows if r.get("flag_kind") == "size_mismatch"),
        },
    }


def _ledger_state(in_scope: int, matched: int) -> dict:
    """本机这份文件清单到底有没有覆盖到这一批。二值的「加载成功了吗」不够用：
    加载成功但只查到一半，和完全没加载，对用户的意义完全不同。"""
    ready = bool(downloads.is_available())
    if not ready:
        kind, sentence = "empty", ("本机的文件清单没有加载成功，本次一律按只有页面地址处理；"
                                   "这不表示这些数据集没有直链。")
    elif in_scope and matched < in_scope:
        kind = "partial"
        sentence = (f"本机的文件清单里只查到了本包 {in_scope} 条 10x Genomics 数据集中的 {matched} 条，"
                    "另外几条按只有页面地址处理；这不表示它们没有直链，而是本机这份清单不完整。")
    else:
        kind, sentence = "none", ""
    return {"ready": ready and (matched == in_scope), "n_ledger_source_in_scope": in_scope,
            "n_matched": matched, "degraded_kind": kind, "degrade_sentence_zh": sentence}
