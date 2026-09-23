# -*- coding: utf-8 -*-
"""数据库状态汇报（corpus_status）。

「汇报数据库的当前状态」类指令的**只读状态工具**——通用化 agent 的第一个非更新检查用例
（langgraph execute 节点的 LOOP_TOOLS 注册表首项，取代 READ_TOOLS/observe；
同一份能力也供 `/api/curate/status` 端点给未装 langchain 扩展时的前端 runner 直取）。

刻意守住的边界：

1. **只读、离线、不抛**：不联网（在线比对是 `corpus_curation.check_updates` 的职责）、
   不写盘、任何单点失败都降级为该部分的如实空缺，绝不掀翻整份汇报。
2. **数据单一真源**：各源条数/快照日期复用 `corpus_curation` 的 `CHECK_UPDATE_SOURCES`
   注册表与 `_snapshot_local_info`（同包私有复用，与 `rerank._first_json_object` 同例）；
   外部库/回收站复用 `list_curations`；账本读 `curate_net_ledger.jsonl` 原文件。
   本模块**不复制**任何一份口径。
3. **汇报措辞不归这里**：本模块只产结构化事实；「组织成简明中文汇报」由
   agent 的 narrate / 前端 `/api/act/summary` 的 LLM 完成（本模块的输出就是它们的素材）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..app.runtime_paths import get_app_paths, instance_data_dir_for, resource_file_for
from .corpus_curation import (
    CHECK_UPDATE_SOURCES,
    NET_LEDGER_NAME,
    USERDATA_DIR_NAME,
    _snapshot_local_info,
    list_curations,
)

#: 账本摘要的读取窗口（行）与回显条数——状态汇报只需要「近期」体感，不扛全量。
_LEDGER_TAIL_LINES = 50
_LEDGER_RECENT_SHOW = 3


def _default_project_root() -> Path:
    """agent/trace 的默认根真源：runtime_paths 的实例数据根——source/portable = 项目根
    （历史逐字节一致）；frozen = data_root（%LOCALAPPDATA%/BioDataAgent）。webapp 显式传
    PROJECT_ROOT；agent 图内调用走这个默认（与 corpus_curation 各默认同径）。"""
    return get_app_paths().data_root


def _sources_status(root: Path) -> list[dict[str, Any]]:
    """各源条数/快照日期：注册表逐源读本地快照（离线；读不到就 0 条 + None 日期）。

    单源统计失败必须带 error 字段——失败与「真空快照」不同形，
    不许把「库坏了」呈现成「库里没东西」。"""
    out: list[dict[str, Any]] = []
    for key, spec in CHECK_UPDATE_SOURCES.items():
        entry: dict[str, Any] = {
            "source": key,
            "label": str(spec.get("label") or key),
        }
        try:
            # 官方快照是随包静态资源（只读）→ frozen 布局实例根从 resource 层读。
            count, snap = _snapshot_local_info(resource_file_for(root, str(spec["file"])))
        except Exception as exc:  # 单源失败不掀翻整份汇报（边界 1），但失败要可见
            count, snap = 0, None
            entry["error"] = type(exc).__name__
        entry["local_count"] = int(count)
        entry["snapshot_date"] = snap
        out.append(entry)
    return out


def _ledger_summary(root: Path) -> dict[str, Any]:
    """近期审计摘要：账本尾部窗口的总条数、按 endpoint 的计数、最近几条的紧凑回显。

    账本只记联网（`_fetch_logged`）——「近期没有条目」本身就是如实信息（最近没联过网）。
    """
    path = instance_data_dir_for(root, USERDATA_DIR_NAME) / NET_LEDGER_NAME
    out: dict[str, Any] = {"entries": 0, "by_endpoint": {}, "recent": []}
    if not path.is_file():
        return out
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return out
    entries: list[dict] = []
    for ln in lines[-_LEDGER_TAIL_LINES:]:
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            entries.append(obj)
    by_endpoint: dict[str, int] = {}
    for e in entries:
        key = str(e.get("endpoint") or "unknown")
        by_endpoint[key] = by_endpoint.get(key, 0) + 1
    out["entries"] = len(entries)
    out["by_endpoint"] = by_endpoint
    out["recent"] = [
        {
            "ts": str(e.get("ts") or ""),
            "endpoint": str(e.get("endpoint") or ""),
            "query": str(e.get("query") or "")[:60],
            "records": int(e.get("records") or 0),
            "error": str(e.get("error") or "") or None,
        }
        for e in entries[-_LEDGER_RECENT_SHOW:]
    ]
    return out


def db_status(*, project_root: Path | None = None) -> dict[str, Any]:
    """数据库当前状态（结构化事实，只读离线）：

    - `sources`：各源条数/快照日期（本地快照口径）；
    - `external_files` / `recycle`：外部库与回收站清单（紧凑投影：文件名/条数/时间）；
    - `ledger`：近期联网审计摘要；
    - `total_records`：各源条数合计（不含外部库 upload_*——那是用户自己的数据，分开报）。
    """
    root = Path(project_root) if project_root else _default_project_root()
    sources = _sources_status(root)
    curations_error: str | None = None
    try:
        cur = list_curations(project_root=root)
        external_files = [
            {
                "filename": str(f.get("filename") or ""),
                "record_count": f.get("record_count"),
                "curatable": bool(f.get("curatable")),
                "modified_at": str(f.get("modified_at") or ""),
            }
            for f in (cur.get("files") or [])
        ]
        recycle = [
            {
                "original_filename": str(r.get("original_filename") or r.get("recycle_name") or ""),
                "record_count": r.get("record_count"),
                "moved_at": str(r.get("moved_at") or ""),
            }
            for r in (cur.get("recycle") or [])
        ]
    except Exception as exc:  # 清单失败不掀翻整份汇报（边界 1），但失败要可见
        external_files, recycle = [], []
        curations_error = type(exc).__name__
    out: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sources": sources,
        "total_records": sum(int(s["local_count"]) for s in sources),
        "external_files": external_files,
        "recycle": recycle,
        "ledger": _ledger_summary(root),
    }
    if curations_error:
        # 清单失败与「真空清单」不同形——带 error 字段如实区分。
        out["curations_error"] = curations_error
    return out


__all__ = ["db_status"]
