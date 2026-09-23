# -*- coding: utf-8 -*-
"""实验室资产台账（单机、只读、纯校验和）。

把 `verify_local_download`（单数据集单文件核验）放大到**一整个本地目录树**：扫描用户实验室里已下载
的文件，按**校验和**（md5，最强）/ 文件名+大小（回退）与本目录的**文件级清单**（10x 基础语料 15119 个
文件，每个都带来源声明的 md5sum/bytes/filename）比对，回答：
  - 我本地这堆文件里，哪些属于本目录已知的数据集？分别核验通过没有？
  - 哪些本地文件与清单**大小/名字一致但 md5 对不上**（＝本地副本与来源声明不符，可能损坏/换版）？
  - 哪些本地文件本目录不认识？

## 红线（钉在实现里）

- **只算校验和、绝不读数据内容**：`file_md5` 只按块流式哈希字节（md5 本就是来源清单里已有的校验和），
  不解析、不解压、不看任何生物内容——不越元数据红线。
- **只读**：不改任何本地文件、不写数据目录、不联网。扫描结果由调用方（CLI/MCP）决定是否落地成报告。
- **诚实边界**：文件级 md5 清单**只覆盖 10x 基础语料（15119 文件）**。外部库（ArrayExpress/HCA/
  CELLxGENE/ENCODE 等）本目录只存**数据集页面 url、无单文件 md5**——那些数据集的本地文件**无法 md5 核验**，
  只能按文件名提示，报告里显式说明，绝不假装核验过。
- **md5sum 是来源声明值**：本工具把你本地文件的**实算 md5** 与之比对；比对不上＝你的本地副本与来源
  声明不一致（这是真核验，不是自证）。

单机 CLI 先行：不建 lab server、不建 web 端点（浏览器无法扫本地 FS，服务端扫任意
路径是另一个安全面、明确推迟）。surfaces = `scripts/scan_lab_assets.py`（CLI）+ MCP `verify_local_assets`。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from ..corpus import downloads

_HASH_CHUNK = 1024 * 1024          # 1 MiB 流式哈希块
MAX_SCAN_FILES = 200_000           # 防跑飞：文件数硬上限
DEFAULT_MAX_HASH_BYTES = 8 * 1024 * 1024 * 1024  # 单文件 >8 GiB 默认跳过 md5、只按名+大小比对（诚实标注）


def build_manifest_index(load=None) -> dict[str, Any]:
    """从本目录文件级清单（`download_links` 的 15119 个文件）建反查索引。

    返回 {by_md5, by_name, by_name_size, n_files, n_datasets}。每个值是文件条目列表，条目带
    dataset_uid / filename / bytes / md5sum / download_url / title。
    """
    by_uid, _ = (load or downloads._load)()
    by_md5: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}
    by_name_size: dict[tuple[str, int], list[dict]] = {}
    total = 0
    for uid, rec in by_uid.items():
        if not isinstance(rec, dict):
            continue
        for f in rec.get("files") or []:
            if not isinstance(f, dict):
                continue
            entry = {
                "dataset_uid": uid,
                "filename": str(f.get("filename") or "").strip(),
                "bytes": int(f.get("bytes") or 0),
                "md5sum": str(f.get("md5sum") or "").strip().lower(),
                "download_url": str(f.get("download_url") or ""),
                "title": str(f.get("title") or ""),
            }
            total += 1
            if entry["md5sum"]:
                by_md5.setdefault(entry["md5sum"], []).append(entry)
            if entry["filename"]:
                by_name.setdefault(entry["filename"], []).append(entry)
                if entry["bytes"]:
                    by_name_size.setdefault((entry["filename"], entry["bytes"]), []).append(entry)
    return {"by_md5": by_md5, "by_name": by_name, "by_name_size": by_name_size,
            "n_files": total, "n_datasets": len(by_uid)}


def file_md5(path: "str | os.PathLike[str]", chunk: int = _HASH_CHUNK) -> "str | None":
    """流式计算本地文件 md5（小写 hex）。**只哈希字节、不解析内容**。读失败返回 None（不崩）。"""
    h = hashlib.md5()
    try:
        with open(path, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
    except OSError:
        return None
    return h.hexdigest()


def match_local_file(md5: "str | None", size: int, name: str, index: dict[str, Any]) -> dict[str, Any]:
    """把一个本地文件（md5 可为 None＝未哈希）按清单归类。md5 命中最权威。

    status ∈ verified | md5_mismatch | name_size_match | name_only | unmatched。
    """
    md5 = (md5 or "").strip().lower()
    if md5 and md5 in index["by_md5"]:
        e = index["by_md5"][md5][0]
        return {"status": "verified", "dataset_uid": e["dataset_uid"],
                "filename": e["filename"], "match": "md5"}
    key = (name, int(size or 0))
    if key in index["by_name_size"]:
        e = index["by_name_size"][key][0]
        # 名+大小命中一条**有 md5** 的清单，但我们（有实算 md5 且）没命中 by_md5 → 内容不符。
        if md5 and e["md5sum"]:
            return {"status": "md5_mismatch", "dataset_uid": e["dataset_uid"],
                    "filename": e["filename"], "match": "name+size", "expected_md5": e["md5sum"]}
        return {"status": "name_size_match", "dataset_uid": e["dataset_uid"],
                "filename": e["filename"], "match": "name+size"}
    if name in index["by_name"]:
        e = index["by_name"][name][0]
        return {"status": "name_only", "dataset_uid": e["dataset_uid"],
                "filename": name, "match": "name"}
    return {"status": "unmatched", "dataset_uid": None, "filename": name, "match": None}


def scan_directory(
    root: "str | os.PathLike[str]",
    index: dict[str, Any],
    *,
    max_files: int = MAX_SCAN_FILES,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
    follow_symlinks: bool = False,
) -> dict[str, Any]:
    """递归扫描 root，对每个文件算 md5（超 `max_hash_bytes` 的巨文件跳 md5、只按名+大小比对）并归类。

    只读：仅为算校验和而 open 文件、绝不解析内容；不改任何文件。返回
    {results, truncated, n_scanned, n_unhashed, root_exists}。
    """
    root = Path(root)
    results: list[dict] = []
    n = 0
    n_unhashed = 0
    n_symlink = 0
    n_unreadable = 0
    truncated = False
    if not root.exists() or not root.is_dir():
        return {"results": [], "truncated": False, "n_scanned": 0, "n_unhashed": 0,
                "n_symlink": 0, "n_unreadable": 0, "root_exists": False}
    for dirpath, _dirs, files in os.walk(root, followlinks=follow_symlinks):
        for fn in files:
            if n >= max_files:
                truncated = True
                break
            fp = Path(dirpath) / fn
            # 跳过文件符号链接：`follow_symlinks=False` 只挡**目录**递归，不挡逐文件 symlink——
            # 那会 stat+open 跟到 root **外**的目标、把外部内容哈希进来（安全）。
            try:
                if fp.is_symlink():
                    n_symlink += 1
                    continue
            except OSError:
                n_unreadable += 1
                continue
            try:
                size = fp.stat().st_size
            except OSError:
                n_unreadable += 1   # stat 失败（权限/断链/路径过长）→ 如实计数，不静默丢弃
                continue
            md5: "str | None"
            if size > max_hash_bytes:
                md5 = None
                n_unhashed += 1
            else:
                md5 = file_md5(fp)
                if md5 is None:
                    # 非巨文件却算不出 md5（被占用/权限）= 读失败 → 如实计数，**不**静默降级成弱匹配
                    # （诚实：否则 n_unhashed 少算、读失败无处浮现）。
                    n_unreadable += 1
                    continue
            m = match_local_file(md5, size, fn, index)
            m["path"] = str(fp)
            m["size"] = size
            m["md5"] = md5
            m["hashed"] = md5 is not None
            results.append(m)
            n += 1
        if truncated:
            break
    return {"results": results, "truncated": truncated, "n_scanned": n,
            "n_unhashed": n_unhashed, "n_symlink": n_symlink, "n_unreadable": n_unreadable,
            "root_exists": True}


def build_ledger_report(scan: dict[str, Any], index: dict[str, Any], load=None) -> dict[str, Any]:
    """把逐文件扫描结果汇总成**按数据集**的台账：本地拥有哪些已知数据集、各核验多少、缺多少，
    以及本目录不认识的本地文件。"""
    results = scan.get("results") or []
    by_uid, _ = (load or downloads._load)()

    datasets: dict[str, dict] = {}
    unmatched: list[dict] = []
    n_verified = n_mismatch = n_weak = 0
    for r in results:
        uid = r.get("dataset_uid")
        st = r.get("status")
        if st == "verified":
            n_verified += 1
        elif st == "md5_mismatch":
            n_mismatch += 1
        elif st in ("name_size_match", "name_only"):
            n_weak += 1
        if not uid:
            unmatched.append({"path": r["path"], "size": r["size"]})
            continue
        d = datasets.setdefault(uid, {
            "dataset_uid": uid, "verified": 0, "md5_mismatch": 0, "weak_match": 0, "files": [],
        })
        if st == "verified":
            d["verified"] += 1
        elif st == "md5_mismatch":
            d["md5_mismatch"] += 1
        else:
            d["weak_match"] += 1
        d["files"].append({"path": r["path"], "status": st, "filename": r.get("filename"),
                           "hashed": r.get("hashed", False)})

    out_datasets = []
    for uid, d in datasets.items():
        rec = by_uid.get(uid) if isinstance(by_uid.get(uid), dict) else {}
        d["catalog_files"] = len(rec.get("files") or [])
        d["local_matched"] = d["verified"] + d["md5_mismatch"] + d["weak_match"]
        d["primary_title"] = str(rec.get("primary_title") or "")
        out_datasets.append(d)
    out_datasets.sort(key=lambda x: (-x["verified"], -x["local_matched"], x["dataset_uid"]))

    return {
        "root_exists": scan.get("root_exists", False),
        "n_scanned": scan.get("n_scanned", 0),
        "n_unhashed": scan.get("n_unhashed", 0),
        "n_symlink_skipped": scan.get("n_symlink", 0),
        "n_unreadable": scan.get("n_unreadable", 0),
        "truncated": scan.get("truncated", False),
        "n_verified": n_verified,
        "n_md5_mismatch": n_mismatch,
        "n_weak_match": n_weak,
        "n_unmatched": len(unmatched),
        "n_datasets_present": len(out_datasets),
        "datasets": out_datasets,
        "unmatched": unmatched[:200],
        "manifest": {"n_files": index.get("n_files", 0), "n_datasets": index.get("n_datasets", 0)},
        "caveat": (
            "md5 校验只对 10x 基础语料的文件级清单可用（本目录 15119 个文件各带来源声明的 md5sum）；"
            "外部库（ArrayExpress/HCA/CELLxGENE/ENCODE 等）本目录只存数据集页面 url、无单文件清单 → 那些数据集的"
            "本地文件无法 md5 核验，只能按文件名提示。md5sum 是来源声明值，本工具用你本地文件的实算 md5 "
            "与之比对：verified=一致；md5_mismatch=名字大小对上但内容不符（可能损坏/换版）。"
            "**weak_match（name_only / name_size_match）= 只按文件名（+大小）匹配、md5 未确认**，且当一个"
            "文件名横跨清单里多个数据集（如 barcodes.tsv.gz / web_summary.html 这类通用名）时，**归属到其中"
            "任意一个**——所以 weak_match 撑起的「本地拥有该数据集」并不可靠、可能张冠李戴，只作线索、勿当结论。"
            "巨文件（默认 >8 GiB）跳过 md5、只按名+大小比对（hashed=false），不冒充已校验；"
            "符号链接文件跳过（不跟到目录外哈希）、读失败/权限受限文件如实计入 n_unreadable、不静默丢。"
        ),
    }
