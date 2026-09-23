# -*- coding: utf-8 -*-
"""阶段二真实下载链接 · 主线接入模块（零第三方依赖，优雅降级）。

按 `dataset_uid`（首选）或数据集页面 `url` 查该数据集的**真实文件下载直链**——替换掉表格里
原本的数据集页面链接。数据来自阶段二产出（767 数据集、15119 直链、逐条 Range-GET 校验 200/206、
join 覆盖率 100%），已随包落地到 `data/download_links.by_uid.json`，**运行时不联网**。

优雅降级（与推荐器"永不崩"一致）：数据文件缺失/损坏 → 所有查询返回 None/空 → 调用方回退页面 url，
不报错、不阻塞。用 `BIODATA_DOWNLOAD_LINKS` 环境变量可覆盖数据文件位置。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import fs_utils

_DEFAULT = str(Path(__file__).resolve().parents[1] / "data" / "download_links.by_uid.json")
_DATA_PATH = os.environ.get("BIODATA_DOWNLOAD_LINKS", _DEFAULT)

def _data_path() -> str:
    """数据文件路径：每次加载现读环境变量，未设置回落 import 期快照 `_DATA_PATH`（可 monkeypatch）。"""
    return os.environ.get("BIODATA_DOWNLOAD_LINKS") or _DATA_PATH


_load = fs_utils.make_sidecar_loader(
    data_path=_data_path,
    shape_gate=fs_utils.by_uid_shape("台账"),
    missing=({}, {}),
)


def get(uid_or_url: "str | None") -> "dict | None":
    """按 dataset_uid 或页面 url 取整条记录；查不到返回 None。"""
    if not uid_or_url:
        return None
    by_uid, by_url = _load()
    return by_uid.get(uid_or_url) or by_url.get(uid_or_url)


def primary_url(uid_or_url: "str | None") -> "str | None":
    """代表性主文件的真实下载直链（分析师最先要的那份）；查不到返回 None。"""
    r = get(uid_or_url)
    return r.get("primary_download_url") if r else None


def fastq_url(uid_or_url: "str | None") -> "str | None":
    """真实 FASTQ 原始 reads 直链（已排除 VDJ contig 输出）；无则 None。"""
    r = get(uid_or_url)
    return r.get("fastq_download_url") if r else None


def has_fastq(uid_or_url: "str | None") -> "bool | None":
    """是否存在真实 FASTQ reads（实测更正信号）；查不到返回 None。
    注意：与索引 has_raw_data 在 99 条上不一致（多为 Xenium 天然无 FASTQ）——采用前先量影响。"""
    r = get(uid_or_url)
    return r.get("has_raw_data_actual") if r else None


def files_for(uid_or_url: "str | None") -> list:
    """该数据集全部文件直链列表（可能为空 list）。"""
    r = get(uid_or_url)
    return r.get("files", []) if r else []


def all_real_urls(uid_or_url: "str | None") -> "set[str]":
    """该数据集所有真实下载直链集合。反捏造校验器白名单化时并入，避免真实直链被误判 unknown。"""
    return {f["download_url"] for f in files_for(uid_or_url) if isinstance(f, dict) and f.get("download_url")}


def file_count(uid_or_url: "str | None") -> int:
    """该数据集真实文件直链条数（0 表示无/降级——调用方据此决定是否显示「查看全部文件」入口）。"""
    return sum(1 for f in files_for(uid_or_url) if isinstance(f, dict) and f.get("download_url"))


def download_cell(uid_or_url: "str | None", fallback: str = "-") -> str:
    """Markdown 表格「下载链接」列：有真实直链则 [点击下载](url)，否则 fallback。"""
    u = primary_url(uid_or_url)
    return f"[点击下载]({u})" if u else fallback


def is_available() -> bool:
    """数据文件是否成功加载（供诊断/测试）。"""
    by_uid, _ = _load()
    return len(by_uid) > 0
