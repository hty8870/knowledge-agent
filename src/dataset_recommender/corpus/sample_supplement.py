# -*- coding: utf-8 -*-
"""10x 平台信息补充（样本量 / 检测基因数 / 次要指标）· by-uid 旁挂账本（零第三方依赖，优雅降级）。

数据来自 2026-08 手工整理的《Visium-10x.xlsx》《Xenium-10x.xlsx》（10x 官方数据集页数值），
由 `scripts/build_sample_supplement.py` 生成到 `data/sample_supplement.by_uid.json`，
按 `dataset_uid`（首选）或数据集页面 `url` join，**运行时不联网**。

定位与 `downloads.py` 一致：只补缺、不覆盖——冻结 base 已有 `total_records` 的记录一律不动，
只有 base 缺失（卡片显示「未说明 Spots/Cells」）时才用本表回填；`gene_count` 是 base 没有的新字段。

优雅降级（与推荐器「永不崩」一致）：数据文件缺失/损坏 → 所有查询返回 None/空 → 调用方
按无补充处理，不报错、不阻塞。用 `BIODATA_SAMPLE_SUPPLEMENT` 环境变量可覆盖数据文件位置。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import fs_utils

_DEFAULT = str(Path(__file__).resolve().parents[1] / "data" / "sample_supplement.by_uid.json")
_DATA_PATH = os.environ.get("BIODATA_SAMPLE_SUPPLEMENT", _DEFAULT)

def _data_path() -> str:
    """数据文件路径：每次加载现读环境变量，未设置回落 import 期快照 `_DATA_PATH`（可 monkeypatch）。"""
    return os.environ.get("BIODATA_SAMPLE_SUPPLEMENT") or _DATA_PATH


_load = fs_utils.make_sidecar_loader(
    data_path=_data_path,
    shape_gate=fs_utils.by_uid_shape("补充表"),
    missing=({}, {}),
)


def get(uid_or_url: "str | None") -> "dict | None":
    """按 dataset_uid 或页面 url 取整条补充记录；查不到返回 None。"""
    if not uid_or_url:
        return None
    by_uid, by_url = _load()
    return by_uid.get(uid_or_url) or by_url.get(uid_or_url)


def count_fill(uid_or_url: "str | None") -> str:
    """用于回填的样本量数字（纯数字字符串）；无补充返回 ''。调用方只在自身 count 缺失时使用。"""
    r = get(uid_or_url)
    return str(r.get("count") or "") if r else ""


def gene_count(uid_or_url: "str | None") -> str:
    """检测基因数（纯数字字符串）；无补充返回 ''。"""
    r = get(uid_or_url)
    return str(r.get("gene_count") or "") if r else ""


def count_note(uid_or_url: "str | None") -> str:
    """样本量口径说明（如多切片合计）；无则 ''。"""
    r = get(uid_or_url)
    return str(r.get("count_note") or "") if r else ""


def extra_facts(uid_or_url: "str | None") -> list:
    """次要补充事实行 [{"label","value"}]（测序深度/转录本中位数/空间分辨率/供体数等）；无则 []。"""
    r = get(uid_or_url)
    facts = r.get("extra_facts") if r else None
    if not isinstance(facts, list):
        return []
    return [f for f in facts if isinstance(f, dict) and f.get("label") and f.get("value")]


def is_available() -> bool:
    """数据文件是否成功加载（供诊断/测试）。"""
    by_uid, _ = _load()
    return len(by_uid) > 0
