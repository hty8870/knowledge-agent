# -*- coding: utf-8 -*-
"""对话式数据库管护（curate_datasets）的**单一真源**模块：清点 / 本地导入 / 联网搜索 / 回收站删除 / 恢复 / 检查来源更新。

设计蓝本：`agent/设计文档（2026-08-01 用户明确授权）。本模块是纯函数模块，
**plan 零副作用**（只读盘 + 联网查询，返回 preview + confirm_token）、**apply 显式执行**（回传 token，
重算比对不一致 → 一个字节不动）。MCP / Web / CLI 三入口共用本模块，不在各入口各写一份。

管护对象（v1，授权决策 3）：`database/external/` 的 `upload_*` 文件（用户上传 + 联网搜索入库记录）。
官方快照（arrayexpress/cellxgene/ebi_scea/encode/hca + 2026-08-06 接入的 hubmap/single_cell_portal/
geo.json）**不可**经本模块删改（`not_curatable`），仍走顶层 `research/`（原 workstream）人工流水线。`database/base/` 冻结基准结构性不可达：本模块只接受叶子文件名、
写入路径恒在 external 或 `.userdata/recycle/` 之下。

两步确认约定（复用 task_pack 指纹模式）：
  - preview 返回 `confirm_token = sha256(canonical_json(动作参数 + 内容指纹))[:16]`；
  - apply 用当前状态重算 token 比对：import/search 比对「内容指纹」防调包；remove/restore 还防
    plan→apply 之间文件被改（TOCTOU）。不一致 → `CurateError(token_mismatch)`，零写入。

内容去重口径（curate.import）：记录 canonical 形式 = 剔除 `source` 键（ingest 附加的标签，非内容本身）
后的 `json.dumps(sort_keys=True)`；逐条 sha256 → 排序拼接 → 再 sha256 得**文件内容指纹**；
与 external 各既有文件逐一比对，整集撞重 → preview 标 `duplicate`，apply 默认拒绝（`duplicate_content`），
`force=True` 可覆盖。

联网纪律（仅 curate.search_online；检索/排序/评测热路径保持离线）：
  - **唯一网络出口 `_fetch`**（urllib，GET/POST，超时、≤5 req/s 礼貌限速（GEO 按 NCBI 官方红线
    收紧到 ≤3 req/s；Zenodo 按官方 30 req/min 红线留余量收紧到 20/min）、429/503 与瞬时连接错误指数退避 ≤3 次、
    其余 4xx 不重试）；测试在 `_fetch` 接缝注入假响应，全模块测试禁网。
  - **源适配器注册表 `SOURCE_ADAPTERS`**：`arrayexpress`（BioStudies 关键词搜索 + 详情两段式富化，
    搜索与字段映射逻辑**移植**自 `scripts/ingest_arrayexpress.py`，共享助手移植自
    `scripts/ingest_cellxgene.py`）；`cellxgene`（全量拉取 + 本地关键词过滤，映射移植自
    `scripts/ingest_cellxgene.py`）；`hubmap`（POST Elasticsearch 查询，映射移植自 t06 mjs）；
    `single_cell_portal`（全量列表 + 逐条详情富化，映射移植自 t07/t34）；
    `hca`（Azul facet 物种过滤 + 分页拉取后本地关键词匹配，2026-08-08 接入）；
    `10x`（官网私有搜索 API，形状校验 fail-closed，2026-08-08 接入）；
    `geo`（NCBI E-utilities esearch→esummary 两段式，无 key 限速 ≤3 req/s，2026-08-07 接入）；
    `zenodo`（通用开放仓储公开 REST API，字段限定 Lucene 查询 + type=dataset，物种从文本抠取、
    组织/疾病槽位放弃如实标注，限速 30 req/min 留余量 20/min，2026-08-08 接入）。
    HuBMAP/SCP 端点不供 species 等字段 → 映射后调 `corpus_enrich.backfill_record` 反标回填
    （provenance 留痕）。
    无分页的全量端点（CELLxGENE / SCP 列表、HuBMAP 类型聚合）走进程内 TTL 缓存（300s）。
    未注册源 → `source_not_registered`。
  - **请求账本**：每次联网（含失败）追加 `.userdata/curate_net_ledger.jsonl`
    （ts/endpoint/query/HTTP 状态/条数，不记秘密）。
  - 候选**先审后入**：plan 只进内存（preview 随附 candidates 供 apply 回传），不落盘；apply 才经
    `uploads.ingest_dataset` 管线入库（复用其全部校验/落盘/缓存失效）。只存元数据 + 官方直链，不自托管。

删除设计（回收站式可逆删除，兑现推翻 action_plan.py:109 排除决策的前提）：
  - 粒度 = external 文件（v1 如实告知；记录级删除留 v2）。
  - **移动而非删除**：`.userdata/recycle/<timestamp>_<filename>` + 追加 `.userdata/recycle/manifest.jsonl`
    （原路径/移动时间/动作）+ `invalidate_external_cache()` 即时不可见；`curate.restore` 逆向移回。

错误契约：`CurateError(code, hint)`（仿 `UploadError`，机器码 + 中文人读提示）。
管护动作码表：bad_action / unknown_file / not_curatable / token_mismatch / duplicate_content /
source_not_registered / network_error / no_candidates / bad_param / too_large（条数或体积超上限）。
摄取类失败沿用 `UploadError` 码（bad_file / bad_encoding / invalid_json / no_records），按原码透传。

隔离红线：本模块**不得** import retriever / workflow / query_parser；它们与官方评测也**不得** import
本模块（`tests/test_curation_isolation.py` AST 机械门钉死）。
"""
from __future__ import annotations

import contextlib
import hashlib
import html
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from . import corpus_enrich
from . import corpus_net
from . import fs_utils
from .corpus_net import ORGANISM_COMMON
from .corpus import EXTERNAL_DIR_NAME, _cmp_key_cached, invalidate_external_cache
from .data_loader import extract_records
from .provenance import (
    SOURCE_10X,
    SOURCE_ARRAYEXPRESS,
    SOURCE_CELLXGENE,
    SOURCE_ENCODE,
    SOURCE_GEO,
    SOURCE_HCA,
    SOURCE_HUBMAP,
    SOURCE_SCP,
    SOURCE_SCEA,
)
from ..app.runtime_paths import instance_data_dir_for, resource_file_for
from ..retrieval.normalizer import MISSING_VALUE_TOKENS
from .uploads import (
    EXTERNAL_TOTAL_MAX_RECORDS,
    MAX_INGEST_RECORDS,
    UploadError,
    _ingest_warnings,
    _tag_records_for_ingest,
    decode_json_bytes,
    ingest_critical_section,
    ingest_dataset,
    new_upload_name,
    sanitize_upload_name,
)

__all__ = [
    "CurateError",
    "ACTIONS",
    "require_action",
    "SOURCE_ADAPTERS",
    "list_curations",
    "plan_import",
    "apply_import",
    "plan_search_online",
    "apply_search_online",
    "plan_remove",
    "apply_remove",
    "plan_restore",
    "apply_restore",
    "check_updates",
    "sync_updates",
    "sync_updates_critical_section",
    "sync_status",
    "recall_sync_operation",
    "make_confirm_token",
    "write_boundary_zh",
    "run_curate_action",
]

# ---- 路径约定（.userdata 已被 gitignore，回收站与账本都是本机运行产物）--------------------------------
USERDATA_DIR_NAME = ".userdata"
RECYCLE_DIR_NAME = "recycle"
RECYCLE_MANIFEST_NAME = "manifest.jsonl"
NET_LEDGER_NAME = "curate_net_ledger.jsonl"

#: v1 可管护文件名前缀（uploads.ingest_dataset 落盘命名空间：用户上传 + 联网搜索入库）。
CURATABLE_PREFIX = "upload_"

#: 单文件导入条数上限：实测 150 万条合法 JSON 入库后 /api/datasets
#: 单请求 37.8s、并发下线程池饿死。上传语义是「用户自己的数据集元数据」，20 万条已是极宽上限。
#: 真源迁入写入汇 `uploads.MAX_INGEST_RECORDS`（闸必须住在所有入口
#: 都要经过的地方），本常量只是兼容转发（plan_import 的早败门继续用它，测试可 monkeypatch）。
MAX_IMPORT_RECORDS = MAX_INGEST_RECORDS

#: 回收站文件名时间戳前缀（与 uploads 上传时间戳同格式：YYYYMMDD_HHMMSS_microseconds）。
_RECYCLE_STAMP_RE = re.compile(r"^[0-9]{8}_[0-9]{6}_[0-9]{6}_(?P<orig>.+)$")

#: 管护动作清单（接口层校验入口）。
ACTIONS = ("list", "import", "search_online", "remove", "restore")

#: 落盘包裹的溯源备注（审计用，不影响检索）。
CURATE_IMPORT_NOTE = "对话式管护 curate.import 入库（本地 JSON 导入）。"
CURATE_SEARCH_NOTE = "对话式管护 curate.search_online 入库（联网搜索官方源，只存元数据 + 官方直链）。"
CURATE_SYNC_NOTE = "对话式管护 curate.sync_updates 入库（在线比对发现疑似新增后自动同步，只存元数据 + 官方直链）。"


#: `CurateError.code` 的机器码全集（2026-08-06 schema 加固顺手项：从本模块**实际 raise 点**
#: 逐处收集——bad_action=未知动作；bad_param=入参非法；token_mismatch=确认指纹不符零写入；
#: invalid_json/no_records/too_large=载荷解析与上限；duplicate_content=内容整集撞重；
#: network_error=官方源请求失败；source_not_registered=源未注册；no_candidates=零候选；
#: unknown_file=external/回收站无此文件；not_curatable=非 upload_* 命名空间；
#: 增 sync_busy=另一个 sync_updates 正在跑（整任务文件锁被占
#: 立即失败不排队）；unknown_operation=按 operation_id 撤回时查无此操作）。
#: 纯类型标注：收窄 `__init__` 形参让 IDE/类型检查能抓「打错码」，运行时行为零变化。
CurateCode = Literal[
    "bad_action", "bad_param", "token_mismatch", "invalid_json", "no_records", "too_large",
    "duplicate_content", "network_error", "source_not_registered", "no_candidates",
    "unknown_file", "not_curatable", "sync_busy", "unknown_operation",
]


class CurateError(ValueError):
    """带机器码的管护失败。`code` 供调用方分类映射，`hint` 供人读定位；`str()` 为「code: hint」。

    继承 ValueError，与 UploadError 同构；接口层翻译：Web → HTTP 400/409；MCP → ToolError("code: hint")。
    所有码**fail-closed**：任何一步不确定就报错，绝不静默写入。"""

    def __init__(self, code: CurateCode, hint: str) -> None:
        self.code = code
        self.hint = hint
        super().__init__(f"{code}: {hint}")


def require_action(action: Any) -> str:
    """校验管护动作名；未知动作 → CurateError(bad_action)。供 MCP/Web/CLI 入口分发前调用。"""
    name = str(action or "").strip()
    if name not in ACTIONS:
        raise CurateError("bad_action", f"未知管护动作 {action!r}，可选：{'/'.join(ACTIONS)}。")
    return name


# ==============================================================================================
# confirm_token：单一生成/校验函数（sha256(canonical_json(动作参数 + 内容指纹))[:16]）
# ==============================================================================================

def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def make_confirm_token(preimage: dict) -> str:
    """confirm_token 的**唯一**生成/校验口径：canonical JSON → sha256 → 前 16 位十六进制。

    plan 用它生成、apply 用当前状态重算后比对；两侧只要走同一个 preimage 构造函数就恒一致。"""
    return hashlib.sha256(_canonical_json(preimage).encode("utf-8")).hexdigest()[:16]


def _canonical_record(record: dict) -> str:
    """内容去重的单条记录口径：剔除 `source`（ingest 附加的来源标签，非内容本身）后 canonical JSON。

    为什么剔除 source：同一份记录经不同入口入库会被打上不同来源标签，但**内容**是同一份——
    去重比的是内容，不是标签。"""
    payload = {k: v for k, v in record.items() if k != "source"}
    return _canonical_json(payload)


def _patch_scope() -> "str | None":
    """当前请求绑定的补丁账户（任务 3：基线+补丁包）。未绑定 → None（历史行为逐字节不变）。

    惰性 import：本模块被 MCP/CLI 直接使用时 patch 机制零介入。"""
    from .patch_package import current_patch_scope

    return current_patch_scope()


def records_content_digest(records: list[dict]) -> str:
    """一批记录的内容指纹（全 64 位十六进制）：逐条 canonical sha256 → 排序 → 拼接 → 再 sha256。

    排序使指纹与记录顺序无关：同一组记录换序仍判同。preview 展示用前 16 位，比对用全量。"""
    lines = sorted(
        hashlib.sha256(_canonical_record(r).encode("utf-8")).hexdigest()
        for r in records
        if isinstance(r, dict)
    )
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _check_token(expected: str, confirm_token: Any) -> None:
    """apply 统一闸：回传 token 与重算值不一致 → token_mismatch，调用方保证零写入。"""
    if str(confirm_token or "").strip() != expected:
        raise CurateError(
            "token_mismatch",
            "这份预览已经失效（内容在你确认前发生了变化）。"
            "本次没有写入任何内容，请重新预览一次再确认。",
        )


# ==============================================================================================
# 路径与文件小助手（W1：写盘侧统一经 runtime_paths 解析用户层——frozen 下 data_root 的
# external/.userdata；source/portable 与测试注入根保持根相对，历史逐字节一致）
# ==============================================================================================


def _external_dir(project_root: Path) -> Path:
    """external **写侧**目录（用户上传/管护目标；官方快照在 shipped 层、只读）。
    source/portable 下两层同目录；frozen 布局实例根 → data_root/database/external。"""
    return instance_data_dir_for(Path(project_root), EXTERNAL_DIR_NAME)


def _recycle_dir(project_root: Path) -> Path:
    return instance_data_dir_for(Path(project_root), USERDATA_DIR_NAME) / RECYCLE_DIR_NAME


def _recycle_manifest(project_root: Path) -> Path:
    return _recycle_dir(project_root) / RECYCLE_MANIFEST_NAME


def _net_ledger_path(project_root: Path) -> Path:
    return corpus_net._net_ledger_path(Path(project_root))


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _leaf_name(filename: Any) -> str:
    """只接受叶子文件名（不含任何路径分隔符）——`database/base/` 与其它目录因此**结构性不可达**。"""
    name = str(filename or "").strip()
    if not name or name in (".", "..") or Path(name).name != name:
        raise CurateError("bad_param", f"只接受外部库里的文件名（不含路径）：{filename!r}。")
    return name


def _load_file_records(path: Path) -> list[dict]:
    """读一个 external/recycle JSON 文件并抽取记录；解析失败 → CurateError(bad_param)（不静默跳过）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CurateError("bad_param", f"文件 {path.name} 无法解析为 JSON：{exc}") from exc
    return [r for r in extract_records(payload) if isinstance(r, dict)]


def _append_jsonl(path: Path, entry: dict) -> None:
    """Compatibility seam; the lock/write primitive lives in corpus_net."""
    corpus_net._append_jsonl(path, entry)


def _read_manifest(project_root: Path) -> list[dict]:
    """读回收站 manifest.jsonl（缺失 → 空；单行损坏跳过不连累其它行）。"""
    path = _recycle_manifest(project_root)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _file_info(path: Path, *, project_root: Path) -> dict:
    """external 库单文件清单项（list_curations 用）。解析失败的文件如实标注，不连累其它文件。"""
    stat = path.stat()
    info: dict[str, Any] = {
        "filename": path.name,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
        "is_upload": path.name.startswith(CURATABLE_PREFIX),
    }
    info["curatable"] = info["is_upload"]  # v1 管护对象 = upload_* 命名空间
    try:
        records = _load_file_records(path)
    except CurateError as exc:
        info.update({"record_count": None, "sources": {}, "parse_error": exc.hint})
        return info
    sources: dict[str, int] = {}
    for r in records:
        src = str(r.get("source") or "").strip() or "（未标注）"
        sources[src] = sources.get(src, 0) + 1
    info.update({"record_count": len(records), "sources": sources})
    return info


# ==============================================================================================
# curate.list：清点 external 库 + 回收站（纯只读）
# ==============================================================================================

def list_curations(*, project_root: Path) -> dict:
    """枚举 external 库全部文件（名/条数/来源/修改时间/是否 upload_*）与回收站内容。纯只读，零副作用。

    任务 3：绑定补丁作用域（登录账户）时改返回**本人补丁包视图**（adds/blocks/trash），
    共享 external 的内容不再冒充「你可管护的对象」（补丁形态下账户的写都落在补丁包）。"""
    scope = _patch_scope()
    if scope:
        return _list_patch_curations(Path(project_root), scope)
    root = Path(project_root)
    ext_dir = _external_dir(root)
    files = [
        _file_info(p, project_root=root)
        for p in sorted(ext_dir.glob("*.json"))
        if p.is_file()
    ] if ext_dir.is_dir() else []

    manifest = _read_manifest(root)
    last_remove: dict[str, dict] = {}
    for entry in manifest:
        if entry.get("action") == "remove" and entry.get("recycle_name"):
            last_remove[str(entry["recycle_name"])] = entry

    recycle: list[dict] = []
    rec_dir = _recycle_dir(root)
    if rec_dir.is_dir():
        for p in sorted(rec_dir.glob("*.json")):
            if not p.is_file():
                continue
            m = _RECYCLE_STAMP_RE.match(p.name)
            entry: dict[str, Any] = {
                "recycle_name": p.name,
                "original_filename": m.group("orig") if m else p.name,
                "size_bytes": p.stat().st_size,
            }
            if p.name in last_remove:
                entry["moved_at"] = last_remove[p.name].get("ts", "")
            try:
                entry["record_count"] = len(_load_file_records(p))
            except CurateError:
                entry["record_count"] = None
            recycle.append(entry)

    return {
        "action": "curate.list",
        "external_dir": EXTERNAL_DIR_NAME,
        "file_count": len(files),
        "files": files,
        "recycle_dir": f"{USERDATA_DIR_NAME}/{RECYCLE_DIR_NAME}",
        "recycle_count": len(recycle),
        "recycle": recycle,
    }


# ==============================================================================================
# curate.import：本地 JSON 导入（包装 uploads.ingest_dataset）+ 内容 hash 去重
# ==============================================================================================

def _parse_payload(payload_bytes: bytes, filename: Any) -> tuple[str, Any, list[dict]]:
    """解码 + 解析 + 抽取记录（与 ingest_dataset 同一组校验，UploadError 按原码透传为 CurateError）。"""
    try:
        cleaned = sanitize_upload_name(filename)
        text = decode_json_bytes(payload_bytes)
        payload: Any = json.loads(text)
    except UploadError as exc:
        raise CurateError(exc.code, exc.hint) from exc
    except json.JSONDecodeError as exc:
        raise CurateError("invalid_json", f"不是合法 JSON：{exc}") from exc
    records = [r for r in extract_records(payload) if isinstance(r, dict)]
    if not records:
        raise CurateError(
            "no_records",
            '未解析出任何数据集记录。文件应是记录数组 [ {…} ]，或对象 { "records": [ {…} ] }。',
        )
    # 2026-08-05：条数上限。实测 150 万条合法 JSON 能入库，随后 /api/datasets
    # 单请求 37.8s、并发下线程池饿死全站无响应——体量闸（64MB）挡不住「条数洪水」。
    # 本产品的上传语义是「用户自己的数据集元数据」，20 万条已是极宽上限。
    if len(records) > MAX_IMPORT_RECORDS:
        raise CurateError(
            "too_large",
            f"记录数 {len(records)} 超过单文件上限 {MAX_IMPORT_RECORDS} 条。"
            "本产品面向数据集元数据管护，超大规模目录请拆分文件后再导入。",
        )
    return cleaned, payload, records


def _probe_sources_and_warnings(records: list[dict], payload: Any, form_source: str) -> tuple[dict[str, int], list[str]]:
    """preview 的来源分布与可读校验提示。单一真源：打标与提示直接复用 ingest_dataset 的
    `_tag_records_for_ingest`/`_ingest_warnings`，preview 与落地同一口径（先审后入）。"""
    _fallback, source_counts, missing_name, unknown_species = _tag_records_for_ingest(
        payload, records, form_source
    )
    return source_counts, _ingest_warnings(missing_name, unknown_species)


def _find_duplicate_files(records: list[dict], project_root: Path) -> list[str]:
    """整集内容指纹与 external 各既有文件逐一比对，返回撞重的文件名列表（同序无关、剔除 source 口径）。"""
    matched, _total = _scan_external_files(records, project_root)
    return matched


#: external 全库累计条数上限（真源在写入汇
#: `uploads.EXTERNAL_TOTAL_MAX_RECORDS`，本模块顶层已同源导入——plan_import 早败门继续用，
#: 测试可 monkeypatch 本模块属性）。


def _scan_external_files(records: list[dict], project_root: Path) -> tuple[list[str], int]:
    """去重比对 + 累计计数**一趟扫**（两个闸共用同一次文件装载，不许把 external 全库
    逐文件解析两遍——大库下那是把 150 万条事故的代价付两次）。"""
    digest = records_content_digest(records)
    ext_dir = _external_dir(project_root)
    if not ext_dir.is_dir():
        return [], 0
    matched: list[str] = []
    total = 0
    for p in sorted(ext_dir.glob("*.json")):
        if not p.is_file():
            continue
        try:
            existing = _load_file_records(p)
        except CurateError:
            continue  # 坏文件不参与去重比对（loader 宽容装载同口径）
        total += len(existing)
        if records_content_digest(existing) == digest:
            matched.append(p.name)
    return matched, total


def _check_external_total_budget(new_records: int, existing_total: int) -> None:
    """全库累计闸：超顶即 too_large（如实报现状与出路，零写入）。"""
    if existing_total + new_records <= EXTERNAL_TOTAL_MAX_RECORDS:
        return
    raise CurateError(
        "too_large",
        f"外部库现有 {existing_total} 条，再导入 {new_records} 条会超过全库累计上限 "
        f"{EXTERNAL_TOTAL_MAX_RECORDS} 条——这个上限防的是全库加载拖垮检索"
        "（150 万条实测 /api/datasets 单请求 37.8s）。"
        "请先把不再用的文件移入回收站，或把目录拆分管理后再导入。",
    )


def _import_preimage(cleaned: str, source: Any, records: list[dict]) -> dict:
    """import token 原像：动作参数 + 内容指纹。**刻意不含 external 库状态**——
    plan→apply 之间库里增删其它文件不应使 token 失效（去重是 apply 时现查的独立闸）。"""
    return {
        "action": "curate.import",
        "filename": cleaned,
        "source": str(source or "").strip(),
        "record_count": len(records),
        "records_digest": records_content_digest(records),
    }


def _scan_patch_duplicates(records: list[dict], root: Path, account_id: str) -> tuple[list[str], int]:
    """补丁形态（任务 3）的去重比对 + 累计条数闸：整集指纹比对本人补丁 adds；
    预算按单账户补丁上限（不再适用实例级 external 总闸）。损坏 fail-closed（load_patch 抛）。"""
    from . import patch_package as pp

    patch = pp.load_patch(root, account_id)
    adds = patch["adds"]
    matched: list[str] = []
    if adds and records_content_digest(adds) == records_content_digest(records):
        matched = ["我的补丁包"]
    total = len(adds)
    if total + len(records) > pp.MAX_PATCH_ADD_RECORDS:
        raise CurateError(
            "too_large",
            f"补丁包现有 {total} 条，再导入 {len(records)} 条会超过单账户上限 "
            f"{pp.MAX_PATCH_ADD_RECORDS} 条——请先删除不再需要的补丁条目。",
        )
    return matched, total


def plan_import(payload_bytes: bytes, filename: Any, source: Any = None, *, project_root: Path) -> dict:
    """curate.import 第一步：解析 preview（零写盘）+ confirm_token。

    返回：条数/来源分布/warnings/内容指纹/去重结果（撞重文件列表）+ token。
    解析/校验失败沿用 UploadError 码（bad_file/bad_encoding/invalid_json/no_records）。
    绑定补丁作用域时去重与预算按本人补丁包口径（任务 3）。"""
    cleaned, payload, records = _parse_payload(payload_bytes, filename)
    source_counts, warnings = _probe_sources_and_warnings(records, payload, str(source or ""))
    scope = _patch_scope()
    if scope:
        matched, _patch_total = _scan_patch_duplicates(records, Path(project_root), scope)
    else:
        matched, external_total = _scan_external_files(records, Path(project_root))
        # 全库累计闸（K5/N8）：单文件 20 万条上限挡不住「连续导入多个 20 万」的累计洪水。
        _check_external_total_budget(len(records), external_total)
    digest = records_content_digest(records)
    token = make_confirm_token(_import_preimage(cleaned, source, records))
    return {
        "action": "curate.import",
        "dry_run": True,
        "filename": cleaned,
        "record_count": len(records),
        "sources": source_counts,
        "warnings": warnings,
        "records_digest": digest[:16],
        "duplicate": {
            "is_duplicate": bool(matched),
            "matched_files": matched,
            # force_param 是给 MCP/程序化调用方的结构化入口（人话 hint 里不塞 API 参数名——
            # 网页对话通道撞重时会自动带 force 重确认，用户根本不该看见参数名，copy 评审）。
            "force_param": "force",
            "hint": ("内容与既有文件整集重复；确认仍要入库的话，需在确认时声明「允许重复」。"
                     if matched else ""),
        },
        "confirm_token": token,
    }


def apply_import(
    payload_bytes: bytes,
    filename: Any,
    source: Any = None,
    *,
    confirm_token: Any,
    force: bool = False,
    project_root: Path,
) -> dict:
    """curate.import 第二步：token 比对 → 去重闸 → 经 uploads.ingest_dataset 入库。

    - token 不一致 → token_mismatch，**零写入**；
    - 内容撞重且非 force → duplicate_content，零写入；
    - 通过则复用 ingest_dataset 全部校验/落盘/缓存失效（只进 external，upload_ 前缀命名空间）。"""
    preview = plan_import(payload_bytes, filename, source, project_root=project_root)  # 重算指纹
    _check_token(preview["confirm_token"], confirm_token)
    if preview["duplicate"]["is_duplicate"] and not force:
        raise CurateError(
            "duplicate_content",
            f"内容与既有文件整集重复（{'、'.join(preview['duplicate']['matched_files'])}）。"
            "没有写入；确认仍要入库的话，请在确认时声明「允许重复」。",
        )
    safe_name = new_upload_name(preview["filename"])
    res = ingest_dataset(
        raw_bytes=payload_bytes,
        safe_name=safe_name,
        project_root=Path(project_root),
        form_source=str(source or ""),
        note=CURATE_IMPORT_NOTE,
    )
    return {
        "action": "curate.import",
        "dry_run": False,
        "filename": res.filename,
        "saved_to": res.saved_to,
        "record_count": res.record_count,
        "sources": res.sources,
        "warnings": res.warnings,
        "forced": bool(force and preview["duplicate"]["is_duplicate"]),
    }


# ==============================================================================================
# 联网：唯一出口 _fetch + 请求账本
# ==============================================================================================

_FETCH_TIMEOUT = 30
_FETCH_MAX_BYTES = corpus_net._MAX_RESPONSE_BYTES
_FETCH_RETRIES = corpus_net._RETRIES
_MIN_REQUEST_INTERVAL = corpus_net._DEFAULT_MIN_INTERVAL
_RETRYABLE_HTTP = corpus_net._RETRYABLE_HTTP


def _polite_wait(min_interval: float = _MIN_REQUEST_INTERVAL) -> None:
    """Compatibility seam backed by the shared per-host limiter."""
    corpus_net._polite_wait("curation-compat", min_interval)


def _fetch(
    url: str,
    *,
    timeout: int = _FETCH_TIMEOUT,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
    attempts: list[int] | None = None,
) -> tuple[Any, int]:
    """Thin curation adapter over corpus_net's shared HTTP+JSON kernel."""
    try:
        return corpus_net.fetch_json(
            url, timeout=timeout, min_interval=min_interval, headers=headers,
            method=method, body=body, attempts=attempts,
            # Preserve the long-standing monkeypatch seam in adapter tests.
            opener=urllib.request.urlopen,
        )
    except corpus_net._NetError as exc:
        raise CurateError("network_error", str(exc)) from exc


def _fetch_logged(
    url: str,
    *,
    project_root: Path,
    endpoint: str,
    query: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    min_interval: float = _MIN_REQUEST_INTERVAL,
) -> Any:
    """Thin record-enrichment adapter over the shared fetch+ledger kernel."""
    return corpus_net.fetch_json_logged(
        url, project_root=project_root, endpoint=endpoint, query=query,
        timeout=_FETCH_TIMEOUT, min_interval=min_interval, headers=headers,
        method=method, body=body,
        # Keep adapter tests and callers able to replace the neutral fetch seam;
        # the logging/counting/error algorithm itself remains single-source.
        fetcher=lambda tries: _fetch(
            url, timeout=_FETCH_TIMEOUT, method=method, body=body, headers=headers,
            min_interval=min_interval, attempts=tries),
        ledger_append=_append_jsonl, ledger_path=_net_ledger_path,
    )


# Only payload enrichment remains in this module; transport, parsing, retry,
# rate limiting and request-ledger semantics are owned by corpus_net.
_LIST_CACHE_TTL = 300.0
_LIST_CACHE: dict[str, tuple[float, Any]] = {}
_list_cache_lock = threading.Lock()


def _cached_fetch_logged(
    url: str,
    *,
    project_root: Path,
    endpoint: str,
    query: str,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """带 TTL 进程内缓存的 _fetch_logged：按 (method, url, body 指纹) 键缓存**成功**响应。

    只用于无分页的全量列表/聚合端点。锁只护字典读写：并发下同键最多多发一次请求
    （与限速纪律兼容的良性重复），不追求单飞。"""
    key = f"{method.upper()} {url} {hashlib.sha256(body).hexdigest()[:16] if body else '-'}"
    with _list_cache_lock:
        hit = _LIST_CACHE.get(key)
        if hit is not None and time.monotonic() - hit[0] < _LIST_CACHE_TTL:
            return hit[1]
    payload = _fetch_logged(
        url, project_root=project_root, endpoint=endpoint, query=query,
        method=method, body=body, headers=headers,
    )
    with _list_cache_lock:
        _LIST_CACHE[key] = (time.monotonic(), payload)
    return payload


# ==============================================================================================
# ArrayExpress 适配器：搜索与字段映射**移植**自 scripts/ingest_arrayexpress.py（勿重写），
# 共享清洗/物种助手移植自 scripts/ingest_cellxgene.py（src 模块不 import scripts，保持单一真源的可读复刻）。
# ==============================================================================================

AE_SEARCH_API = "https://www.ebi.ac.uk/biostudies/api/v1/arrayexpress/search"
AE_DETAIL_API = "https://www.ebi.ac.uk/biostudies/api/v1/studies"
AE_STUDY_TMPL = "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{accession}"
AE_SOURCE_LABEL = "ArrayExpress"

# 物种词表 ORGANISM_COMMON 的唯一真源在 corpus_net（两模块共用的叶子锚点），
# 本模块 import 复用：species 硬过滤与抽取正则正向查表。


def _labels(items: object) -> list[str]:
    """[{'label':...}] / [str] 混合结构 → 字符串列表。"""
    out: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("label"):
                out.append(str(it["label"]).strip())
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
    return out


def _clean_join(items: object, cap: int = 8) -> str:
    """label 列表 → 去重（拆 `||` 多标签）+ 截断的可读串。移植自 ingest_cellxgene._clean_join。"""
    seen: list[str] = []
    for raw in _labels(items):
        for part in str(raw).split("||"):
            part = part.strip().strip(",").strip()
            if part and part.lower() not in {s.lower() for s in seen}:
                seen.append(part)
    if len(seen) > cap:
        return ", ".join(seen[:cap]) + f" 等{len(seen)}项"
    return ", ".join(seen)


def _norm_token(v: str) -> str:
    """归一：小写 + 非字母数字转空格 + collapse。用于把 'N/A' / 'not applicable.' 等对齐到受控集合。"""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(v).strip().lower())).strip()


# NA 型占位/缺失标记 + 投稿备注 → 既非组织也非疾病，不写进语义字段（归一后比较）。
# 「未标注」词表的诚实层单一真源是 normalizer.MISSING_VALUE_TOKENS（其注释明令不要再造一套）；
# 本表 = 真源词表经同一 `_norm_token` 归一后的形态（"n/a"→"n a"、"-"→""），再叠加只在
# **管护摄取侧**出现的占位拼写（nan/undetermined/not applicable 等——不是已标注的取值，
# 但也不必进全产品的缺失判定）。新增缺失写法只许加在 normalizer，本表随之自动收录，
# 防两套词表各自漂移（对抗评审 docs-arch）。
_NONVALUE = {_norm_token(token) for token in MISSING_VALUE_TOKENS} | {
    "", "nan", "not applicable", "not available", "not collected", "not reported",
    "undetermined", "missing",
}


def _is_informative(v: str) -> bool:
    """是否是真实取值（非空、非 NA 占位、非投稿备注）。tissue/disease 共用。"""
    n = _norm_token(v)
    return bool(n) and n not in _NONVALUE and "see processed file" not in n


def _map_one_species(name: str) -> str:
    """单个学名 → 通用名。两级回退：① 精确查表；② 亚种三名法退化到「属+种」二名再查。都不中 → 保留原名。"""
    key = name.strip().lower()
    if key in ORGANISM_COMMON:
        return ORGANISM_COMMON[key]
    toks = key.split()
    if len(toks) > 2:
        binom = " ".join(toks[:2])
        if binom in ORGANISM_COMMON:
            return ORGANISM_COMMON[binom]
    return name.strip()


def map_species(organisms: list[str]) -> str:
    """学名列表 → 通用名（去重保序）；未知物种保留原名（保守，不误命中常见物种查询）。"""
    seen: list[str] = []
    for name in organisms:
        common = _map_one_species(name)
        if common and common not in seen:
            seen.append(common)
    return ", ".join(seen)


# ---- 移植自 ingest_arrayexpress.py：结构化字段名 / 健康态 / 平台家族分类 --------------------
_TISSUE_NAMES = {"organism part", "tissue"}
_DISEASE_NAMES = {"disease", "disease state", "clinical history", "clinical information"}
# 健康态标签（normal/healthy/control）＝ 源库**显式声明健康** → 统一写入规范值 "normal"
# （xdc1 口径：与 CELLxGENE / EBI SCEA 一致——「健康」是已知事实，不是「没标注」）。
_HEALTHY = {"normal", "healthy", "control"}


def _canonical_disease(v: str) -> str | None:
    """disease 原始取值 → 规范取值：非取值 → None；健康态 → "normal"；真实疾病 → 原文保留。"""
    if not _is_informative(v):
        return None
    n = _norm_token(v)
    return "normal" if n.split(" ")[0] in _HEALTHY else v


# 平台家族分类（N14）：只认无歧义关键词；按家族是否出现（二值）判定，恰好一个家族出现才给标签。
_PLATFORM_PATTERNS: "list[tuple[str, list[str]]]" = [
    ("Visium", [r"visium"]),
    ("Xenium", [r"xenium"]),
    ("Slide-seq", [r"slide-?seq"]),
    ("MERFISH", [r"merfish"]),
    ("seqFISH", [r"seqfish"]),
    ("Smart-seq", [r"smart-?seq"]),
    ("Drop-seq", [r"drop-?seq"]),
    ("inDrop", [r"in-?drop"]),
    ("CEL-seq", [r"cel-?seq"]),
    ("MARS-seq", [r"mars-?seq"]),
    ("Seq-Well", [r"seq-?well"]),
    ("sci-RNA-seq", [r"sci-rna-?seq"]),
    ("BD Rhapsody", [r"\brhapsody\b"]),
    ("Fluidigm", [r"fluidigm"]),
    ("STRT-seq", [r"\bstrt(?:-?seq)?\b"]),
    ("Quartz-seq", [r"quartz-?seq"]),
    ("Chromium", [r"chromium", r"cell\s?ranger",
                  r"10\s*x\s*genomics",
                  r"10\s*x[^a-z0-9]{0,4}(?:3['’]|5['’]|v[234]|single|scrna|gene\s*expression|gex)"]),
]
_PLATFORM_RE = [(fam, [re.compile(p, re.I) for p in pats]) for fam, pats in _PLATFORM_PATTERNS]

_PLATFORM_TEXT_NAMES = {
    "description", "study type", "software", "hardware", "title",
    "protocol", "library construction", "library construction protocol",
    "single cell isolation", "extract protocol", "nucleic acid sequencing protocol",
}

# publication DOI 匹配（字符类不含 `;`：防 "10.x;10.y" 被并成一个 token）。
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._()/:A-Za-z0-9]+")


def classify_platform(*texts: str) -> str:
    """合并自由文本 → 单细胞平台家族短标签。只用无歧义关键词；识别不出返回 ""（不猜）。

    按家族是否出现（二值）判定：恰好 1 个家族出现 → 该家族；0 或 2+ → 留空（测不准、不谎报）。"""
    combined = " ".join(t for t in texts if t)
    if not combined:
        return ""
    present = [fam for fam, res in _PLATFORM_RE if any(r.search(combined) for r in res)]
    return present[0] if len(present) == 1 else ""


def _collect_platform_text(section: object) -> str:
    """递归收集详情 section 里承载平台家族的自由文本（protocol Description / Software / Study type…）。"""
    bits: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if isinstance(a, dict):
                    name = str(a.get("name") or "").strip().lower()
                    val = str(a.get("value") or "").strip()
                    if val and name in _PLATFORM_TEXT_NAMES:
                        bits.append(val)
            if node.get("subsections") is not None:
                walk(node["subsections"])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    return " ".join(bits)


def extract_publication_doi(detail: object) -> str:
    """从 BioStudies 详情取 **publication DOI**（`DOI` 属性 + DOI 类型 link）。返回首个合法 DOI；无则 ""。"""
    if not isinstance(detail, dict):
        return ""
    section = detail.get("section")
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if isinstance(a, dict):
                    name = str(a.get("name") or "").strip().lower()
                    val = str(a.get("value") or "").strip()
                    if name == "doi" and val:
                        m = _DOI_RE.search(val)
                        if m:
                            found.append(m.group(0))
            url = node.get("url")
            if isinstance(url, str) and url:
                is_doi_link = any(
                    isinstance(x, dict)
                    and str(x.get("name") or "").strip().lower() == "type"
                    and str(x.get("value") or "").strip().lower() == "doi"
                    for x in (node.get("attributes") or [])
                )
                if is_doi_link:
                    m = _DOI_RE.search(url)
                    if m:
                        found.append(m.group(0))
            for key in ("subsections", "links"):
                if node.get(key) is not None:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    seen: list[str] = []
    for d in found:
        d = d.rstrip(".,;)")
        if d and d not in seen:
            seen.append(d)
    return seen[0] if seen else ""


def _attr(attrs: object, name: str) -> str:
    """attributes 列表（[{name, value}, ...]）里按 name 精确匹配取 value。"""
    if not isinstance(attrs, list):
        return ""
    for a in attrs:
        if isinstance(a, dict) and a.get("name") == name:
            return str(a.get("value") or "").strip()
    return ""


def _collect_characteristics(section: dict) -> tuple[list[str], list[str]]:
    """递归遍历 section.subsections，按 name（小写）收集 tissue / disease 取值。"""
    tissues: list[str] = []
    diseases: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if not isinstance(a, dict):
                    continue
                name = str(a.get("name") or "").strip().lower()
                val = str(a.get("value") or "").strip()
                if not val:
                    continue
                if name in _TISSUE_NAMES:
                    tissues.append(val)
                elif name in _DISEASE_NAMES:
                    diseases.append(val)
            sub = node.get("subsections")
            if sub is not None:
                walk(sub)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    return tissues, diseases


def _species_from_content(content: str) -> list[str]:
    """详情缺失时的回退：对搜索命中自由文本 content 做正则，匹配 ORGANISM_COMMON 的学名键。"""
    text_l = content.lower()
    found: list[str] = []
    for latin in ORGANISM_COMMON:
        if re.search(r"\b" + re.escape(latin) + r"\b", text_l):
            found.append(latin)
    return found


def _ae_to_record(hit: dict, detail: dict | None) -> dict | None:
    """一条 ArrayExpress 搜索命中（+ 可选详情）→ 本项目记录 schema。无标题/无 accession 则丢弃。

    移植自 ingest_arrayexpress.to_record；字段语义与诚实的局限（count/unit 留空、has_raw_data 保守 False）
    与原脚本逐位一致。"""
    title = str(hit.get("title") or "").strip()
    accession = str(hit.get("accession") or "").strip()
    if not title or not accession:
        return None
    content = str(hit.get("content") or "")
    release_date = str(hit.get("release_date") or "").strip()
    published_date = release_date if re.match(r"^\d{4}-\d{2}-\d{2}$", release_date) else ""

    organism_raw = ""
    study_type = ""
    description_detail = ""
    tissues: list[str] = []
    diseases: list[str] = []
    section = detail.get("section") if isinstance(detail, dict) else None
    if isinstance(section, dict):
        attrs = section.get("attributes") or []
        organism_raw = _attr(attrs, "Organism")
        study_type = _attr(attrs, "Study type")
        description_detail = _attr(attrs, "Description")
        tissues, diseases = _collect_characteristics(section)

    if organism_raw:
        species_names = [p.strip() for p in organism_raw.split(";") if p.strip()]
    else:
        species_names = _species_from_content(content)  # 详情缺失/无 Organism → 回退正则

    tissue = _clean_join([t for t in tissues if _is_informative(t)])
    disease = _clean_join([c for c in (_canonical_disease(d) for d in diseases) if c])

    desc = description_detail.replace("\n", " ").strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    desc_bits = [b for b in (accession, study_type, desc) if b]
    page_url = AE_STUDY_TMPL.format(accession=accession)

    platform = classify_platform(title, content, _collect_platform_text(section))
    collection_doi = extract_publication_doi(detail)

    return {
        "dataset_name": title,
        "species": map_species(species_names),
        "tissue": tissue,
        "disease": disease,
        "chemistry": study_type,
        "platform": platform,
        "count": "",
        "unit": "",
        "has_raw_data": False,
        "url": page_url,
        "download_url": page_url,
        "filesize": 0,
        "published_date": published_date,
        "description": " · ".join(desc_bits),
        "source": AE_SOURCE_LABEL,
        "collection_doi": collection_doi,
        "dataset_uid": f"ae:{accession}",
    }


def _search_arrayexpress(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """ArrayExpress（BioStudies）关键词搜索 + 详情两段式富化 → 候选记录（不落盘）。

    移植自 ingest_arrayexpress.main 的搜索段：`QUERY` 常量参数化为用户 query；`pageSize=limit`；
    逐条详情富化，详情失败优雅降级（species 回退 content 正则，tissue/disease 留空，warnings 如实告知）。
    species 过滤为**本地子串过滤**（与检索侧 species 子串匹配同口径）；联网只发原始 query。
    返回 (records, warnings)。"""
    q = urllib.parse.quote(query)
    url = f"{AE_SEARCH_API}?query={q}&pageSize={limit}"
    payload = _fetch_logged(url, project_root=project_root, endpoint=AE_SEARCH_API, query=query)
    hits = payload.get("hits") if isinstance(payload, dict) else None
    if not isinstance(hits, list):
        hits = []

    warnings: list[str] = []
    records: list[dict] = []
    seen: set[str] = set()
    n_detail_fail = 0
    for hit in hits[:limit]:
        if not isinstance(hit, dict):
            continue
        acc = str(hit.get("accession") or "").strip()
        if not acc or acc in seen:
            continue
        seen.add(acc)
        detail: dict | None = None
        try:
            detail = _fetch_logged(
                f"{AE_DETAIL_API}/{urllib.parse.quote(acc)}",
                project_root=project_root, endpoint=AE_DETAIL_API, query=acc,
            )
        except CurateError:
            n_detail_fail += 1  # 详情失败优雅降级（沿用 ingest 脚本口径），不中断整体搜索
        rec = _ae_to_record(hit, detail)
        if rec is not None:
            records.append(rec)
    if n_detail_fail:
        warnings.append(
            f"{n_detail_fail} 条详情拉取失败：species 回退为搜索文本正则匹配，tissue/disease 留空（诚实降级）。"
        )
    if species:
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    return records, warnings


# ==============================================================================================
# CELLxGENE Discover 适配器：全量拉取（TTL 缓存）+ 本地关键词过滤。
# 字段映射**移植**自 scripts/ingest_cellxgene.py 的 to_record/pick_asset/platform_hint（勿重写）。
# ==============================================================================================

CXG_DATASETS_API = "https://api.cellxgene.cziscience.com/curation/v1/datasets"
CXG_SOURCE_LABEL = "CELLxGENE Discover"
#: UA 沿用 ingest 脚本的 biodata-agent-ingest/1.0 风格（官方 curation API 的既有礼貌标识）。
_CXG_HEADERS = {"User-Agent": "biodata-agent-ingest/1.0"}


def _cxg_pick_asset(assets: object) -> tuple[str, int]:
    """选代表性下载资产：优先 H5AD，其次 RDS，再取首个有 url 的。返回 (url, filesize)。
    移植自 ingest_cellxgene.pick_asset。"""
    if not isinstance(assets, list):
        return "", 0
    valid = [a for a in assets if isinstance(a, dict) and a.get("url")]
    for ft in ("H5AD", "RDS"):
        for a in valid:
            if str(a.get("filetype", "")).upper() == ft:
                return str(a["url"]), int(a.get("filesize") or 0)
    if valid:
        return str(valid[0]["url"]), int(valid[0].get("filesize") or 0)
    return "", 0


def _cxg_platform_hint(assays: list[str]) -> str:
    """从 assay 归一出一个「平台家族」提示（10x 单细胞 → Chromium；空间/其他保留自身名）。
    移植自 ingest_cellxgene.platform_hint。"""
    text = " ".join(assays).lower()
    if "visium" in text:
        return "Visium"
    if "xenium" in text:
        return "Xenium"
    if "slide-seq" in text or "slideseq" in text:
        return "Slide-seq"
    if "merfish" in text:
        return "MERFISH"
    if "10x" in text:
        return "Chromium"
    return assays[0] if assays else ""


def _cxg_to_record(ds: dict) -> dict | None:
    """一条 CELLxGENE 数据集 → 本项目记录 schema。tombstone / 无有效下载资产 / 无标题且无集合名 → 丢弃。
    移植自 ingest_cellxgene.to_record（字段语义逐位一致）。"""
    if ds.get("tombstone"):
        return None
    dl_url, filesize = _cxg_pick_asset(ds.get("assets"))
    if not dl_url:
        return None
    assays = _labels(ds.get("assay"))
    organisms = _labels(ds.get("organism"))
    dataset_id = str(ds.get("dataset_id") or "").strip()
    title = str(ds.get("title") or "").strip()
    collection = str(ds.get("collection_name") or "").strip()
    doi = str(ds.get("collection_doi") or "").strip()
    published = str(ds.get("published_at") or "")[:10]
    cell_count = ds.get("cell_count")
    explorer = str(ds.get("explorer_url") or "").strip()
    if not (title or collection):
        return None
    desc_bits = [b for b in (collection, f"DOI: {doi}" if doi else "") if b]
    # dataset_uid 前缀 cxg: → 绝不与 10x by_uid 直链表键碰撞（查表 miss → has_raw_data 保留本记录值）。
    return {
        "dataset_name": title or collection,
        "species": map_species(organisms),
        "tissue": _clean_join(ds.get("tissue")),
        "disease": _clean_join(ds.get("disease")),
        "chemistry": ", ".join(dict.fromkeys(assays)),   # 精确 assay（前端「技术方案」）
        "platform": _cxg_platform_hint(assays),          # 平台家族提示（derive_platform_family/facet）
        "count": str(cell_count) if cell_count else "",
        "unit": "Cells",
        "has_raw_data": False,                            # H5AD/RDS 处理后矩阵，非 FASTQ 原始 reads
        "url": explorer or dl_url,                        # 人读页面（explorer）
        "download_url": dl_url,                           # 官方资产直链（H5AD/RDS）
        "filesize": filesize,
        "published_date": published,
        "description": " · ".join(desc_bits),
        "source": CXG_SOURCE_LABEL,
        "collection_doi": doi,
        "dataset_uid": f"cxg:{dataset_id}" if dataset_id else "",
    }


def _search_cellxgene(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """CELLxGENE 全量拉取（TTL 缓存 300s）→ 本地关键词过滤 → 候选记录（不落盘）。

    端点不收查询参数（无分页、单次全量，2026-08-06 实测 2,198+ 条）：关键词过滤在本地做——
    query 小写分词后对 title/collection_name 连接文本子串匹配，多词 AND。species 为本地子串过滤
    （与 AE 同口径，作用于映射后的通用名）。顺序：关键词过滤 → 映射/uid 去重 → species 过滤 →
    limit 截断。返回 (records, warnings)。"""
    payload = _cached_fetch_logged(
        CXG_DATASETS_API, project_root=project_root, endpoint=CXG_DATASETS_API,
        query=query, headers=_CXG_HEADERS,
    )
    datasets = payload if isinstance(payload, list) else []
    terms = [t for t in re.split(r"\s+", query.lower()) if t]
    records: list[dict] = []
    seen: set[str] = set()
    for ds in datasets:
        if not isinstance(ds, dict):
            continue
        text = f"{ds.get('title') or ''} {ds.get('collection_name') or ''}".lower()
        if not all(t in text for t in terms):
            continue
        rec = _cxg_to_record(ds)
        if rec is None:
            continue
        uid = rec["dataset_uid"]
        if uid:
            if uid in seen:
                continue
            seen.add(uid)
        records.append(rec)
    if species:
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    return records[:limit], []


# ==============================================================================================
# HuBMAP 适配器：POST Elasticsearch 查询（search.api v3）。
# 请求体构造与字段映射**移植**自 research/reports/t06-hubmap-formal-candidate/
# build-hubmap-formal.mjs（_source 白名单 / entity_type=Dataset & status=Published &
# data_access_level=public 公共边界 / dataset_type 动态 allowlist / normalized()）；
# 全文子句选型经 2026-08-06 真机探测（结论见 _hubmap_query_clause 注释）。
# ==============================================================================================

HUBMAP_SEARCH_API = "https://search.api.hubmapconsortium.org/v3/search"
HUBMAP_SOURCE_LABEL = "HuBMAP"
HUBMAP_STUDY_TMPL = "https://portal.hubmapconsortium.org/browse/dataset/{uuid}"
_HUBMAP_HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}

#: 候选映射实际读取的 _source 白名单（mjs 白名单的候选字段子集，不多要一个字段）。
_HUBMAP_SOURCE_FIELDS = [
    "uuid", "hubmap_id", "registered_doi", "data_access_level", "status",
    "entity_type", "dataset_type", "title", "metadata.description",
    "metadata.acquisition_instrument_model", "origin_samples.organ", "published_timestamp",
]

#: 公共边界（移植自 mjs searchBody 的 publicDatasetFilters）：逐条 term 精确过滤。
_HUBMAP_PUBLIC_FILTERS = [
    {"term": {"entity_type.keyword": "Dataset"}},
    {"term": {"status.keyword": "Published"}},
    {"term": {"data_access_level.keyword": "public"}},
]

#: dataset_type → 规范家族（移植自 mjs FAMILY_RULES，逐行同口径）：只认白名单家族基名 +
#: 字面「 [pipeline]」后缀；generic RNAseq 等不在列 → 排除（mjs 的 allowlist 口径）。
_HUBMAP_FAMILY_RULES: "list[tuple[str, list[re.Pattern]]]" = [
    ("Visium (no probes)", [re.compile(r"^Visium \(no probes\)(?: \[.*\])?$")]),
    ("Visium (with probes)", [re.compile(r"^Visium \(with probes\)(?: \[.*\])?$")]),
    ("Slideseq", [re.compile(r"^Slideseq(?: \[.*\])?$"), re.compile(r"^Slide-seq(?: \[.*\])?$")]),
    ("MERFISH", [re.compile(r"^MERFISH(?: \[.*\])?$")]),
    ("seqFISH", [re.compile(r"^seqFISH(?: \[.*\])?$")]),
    ("Xenium", [re.compile(r"^Xenium(?: \[.*\])?$")]),
    ("CosMx", [re.compile(r"^CosMx(?: Transcriptomics)?(?: \[.*\])?$")]),
    ("Stereo-seq", [re.compile(r"^Stereo-seq(?: \[.*\])?$")]),
    ("DBiT", [re.compile(r"^DBiT(?: \[.*\])?$")]),
    ("GeoMx (NGS)", [re.compile(r"^GeoMx \(NGS\)(?: \[.*\])?$")]),
    ("HiFi-Slide", [re.compile(r"^HiFi-Slide(?: \[.*\])?$")]),
    ("Pixel-seqV2", [re.compile(r"^Pixel-seqV2(?: \[.*\])?$")]),
    ("Molecular Cartography", [re.compile(r"^Molecular Cartography(?: \[.*\])?$")]),
    ("Resolve", [re.compile(r"^Resolve(?: \[.*\])?$")]),
    ("CODEX", [re.compile(r"^CODEX(?: \[.*\])?$")]),
    ("MIBI", [re.compile(r"^MIBI(?: \[.*\])?$")]),
    ("PhenoCycler", [re.compile(r"^PhenoCycler(?: \[.*\])?$")]),
    ("10X Multiome", [re.compile(r"^10X Multiome(?: \[.*\])?$")]),
    ("SNARE-seq2", [re.compile(r"^SNARE-seq2(?: \[.*\])?$")]),
    ("MUSIC", [re.compile(r"^MUSIC(?: \[.*\])?$")]),
]


def _hubmap_canonical_family(value: str) -> str | None:
    """dataset_type 原始取值 → 规范家族；不在 allowlist → None（移植自 mjs canonicalFamily）。"""
    for family, rules in _HUBMAP_FAMILY_RULES:
        if any(rule.search(value) for rule in rules):
            return family
    return None


def _hubmap_observed_types(project_root: Path) -> list[str]:
    """官方聚合先推导 allowlist 命中的**精确** dataset_type 取值，再供 terms 精确过滤
    （移植自 mjs 的两段式口径）。

    为什么必须动态推导：terms 是精确匹配，而线上取值普遍带「 [pipeline]」后缀——
    2026-08-06 真机实测 42 桶中「Visium (no probes)」基名 0 条、仅剩
    「Visium (no probes) [Salmon + Scanpy]」1 条，写死基名会整源漏光。聚合结果走 TTL 缓存
    （类型分布变化以月计，不必每次搜索都多问一次）。"""
    body = json.dumps({
        "_source": ["uuid"],
        "size": 0,
        "query": {"bool": {"filter": _HUBMAP_PUBLIC_FILTERS}},
        "aggs": {"dataset_types": {"terms": {"field": "dataset_type.keyword", "size": 1000,
                                             "missing": "__MISSING__"}}},
    }, ensure_ascii=False).encode("utf-8")
    payload = _cached_fetch_logged(
        HUBMAP_SEARCH_API, project_root=project_root, endpoint=HUBMAP_SEARCH_API,
        query="dataset_type 分布聚合（allowlist 精确取值推导）",
        method="POST", body=body, headers=_HUBMAP_HEADERS,
    )
    aggs = payload.get("aggregations") if isinstance(payload, dict) else None
    buckets = aggs.get("dataset_types", {}).get("buckets", []) if isinstance(aggs, dict) else []
    observed: list[str] = []
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        key = bucket.get("key")
        if isinstance(key, str) and _hubmap_canonical_family(key):
            observed.append(key)
    return observed


def _hubmap_query_clause(query: str) -> dict:
    """用户 query → ES 全文子句（2026-08-06 真机探测结论，POST ≤5 次礼貌预算内完成）：

    - `match` 已证实可用且真实过滤（title 上查 "lung"：公共全集 5,272 → 516 命中）；
    - `simple_query_string` 语法也被端点接受（200），但它解析 `+ " * ( )` 等操作符——
      用户自由文本里的标点可能直接 400；`match` 不解析查询语法，严格更安全，故选 match；
    - 三个 match(operator=and) 组 bool.should（minimum_should_match=1）：多词须在同一字段内
      全部出现、三个字段任一命中即可；title/description/metadata.description 三字段均经
      探测确认已映射（simple_query_string 引用不报 400）。"""
    clause = {"query": query, "operator": "and"}
    return {"bool": {"should": [
        {"match": {"title": clause}},
        {"match": {"description": clause}},
        {"match": {"metadata.description": clause}},
    ], "minimum_should_match": 1}}


def _hubmap_epoch_date(value: object) -> str | None:
    """epoch **毫秒** → UTC 日历日期（移植自 mjs epochDate：new Date(value).toISOString().slice(0,10)）。
    非数字/布尔/越界 → None（不猜）。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def _hubmap_to_record(source: dict) -> dict | None:
    """一条 HuBMAP _source → 本项目记录 schema（移植自 mjs normalized() 的候选字段子集）。

    无 uuid/title → 丢弃。species/disease 端点不供（mjs 同口径）→ None，由调用方
    `corpus_enrich.backfill_record` 反标；文件清单未查询 → has_raw_data/filesize/download_url
    留 None（不猜值）。"""
    uuid = str(source.get("uuid") or "").strip()
    title = str(source.get("title") or "").strip()
    if not uuid or not title:
        return None
    organs: list[str] = []
    samples = source.get("origin_samples")
    if isinstance(samples, list):
        for sample in samples:
            if isinstance(sample, dict):
                organ = str(sample.get("organ") or "").strip()
                if organ and organ not in organs:
                    organs.append(organ)
    metadata = source.get("metadata") if isinstance(source.get("metadata"), dict) else {}
    description = metadata.get("description")
    description = str(description).strip() if description else ""
    doi = str(source.get("registered_doi") or "").strip()
    dataset_type = str(source.get("dataset_type") or "")
    return {
        "dataset_uid": f"hubmap:{uuid}",
        "dataset_name": title,
        "source": HUBMAP_SOURCE_LABEL,
        "public_accession": str(source.get("hubmap_id") or "").strip() or None,
        "collection_doi": doi or None,
        "species": None,   # 端点不供 → backfill_record 反标
        "tissue": ", ".join(organs) if organs else None,   # HuBMAP organ code(s)，本地不展开（mjs 同口径）
        "disease": None,   # 端点不供 → backfill_record 反标
        "chemistry": _hubmap_canonical_family(dataset_type),
        "platform": str(metadata.get("acquisition_instrument_model") or "").strip() or None,
        "count": None,
        "unit": None,
        "has_raw_data": None,   # 文件清单未查询（不猜）
        "published_date": _hubmap_epoch_date(source.get("published_timestamp")),
        "url": HUBMAP_STUDY_TMPL.format(uuid=uuid),
        "download_url": None,
        "description": description or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": HUBMAP_SEARCH_API,
            "fields": {
                "species": {"origin": "not supplied by public Search response", "complete": False},
                "disease": {"origin": "not supplied by public Search response", "complete": False},
                "files": {"origin": "not queried; no file manifest downloaded", "complete": False},
            },
        },
        "source_dataset_type": dataset_type or None,
    }


def _search_hubmap(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """HuBMAP POST ES 查询 → 候选记录（不落盘）。

    两段式（移植自 mjs）：① 聚合推导 allowlist 精确 dataset_type 取值（TTL 缓存）；
    ② terms 过滤 + 全文子句查询。映射后逐条 backfill_record 反标 species/disease 等缺口；
    species 过滤为本地子串过滤（与 AE 同口径）。返回 (records, warnings)。

    分页补齐：species 本地过滤若发生在
    「size=limit 截取」之后，一页里匹配不足就如实少给——改成按页拉取、边滤边攒，凑足 limit
    或官方命中总量耗尽为止（ES from+size ≤10000 深分页窗口内）。不带 species 时第一页即够，
    行为与此前逐位一致（单请求、size=limit）。"""
    observed = _hubmap_observed_types(project_root)
    if not observed:
        # fail-closed：官方分布里一个 allowlist 家族都认不出（分布或词表已漂移）→ 零候选如实上报，
        # 绝不退化成无类型过滤的全库搜（会混入 generic RNAseq 等非白名单类型）。
        return [], ["HuBMAP 官方类型聚合没有返回任何 allowlist 命中的 dataset_type 取值，"
                    "本次无法构造搜索（未发起正文查询）。"]

    sp = species.lower()
    warnings: list[str] = []
    records: list[dict] = []
    seen: set[str] = set()
    n_backfill_species = 0
    page_from = 0
    while len(records) < limit:
        body = json.dumps({
            "_source": _HUBMAP_SOURCE_FIELDS,
            "size": limit,
            "from": page_from,
            "query": {"bool": {
                "filter": [*_HUBMAP_PUBLIC_FILTERS, {"terms": {"dataset_type.keyword": observed}}],
                "must": [_hubmap_query_clause(query)],
            }},
        }, ensure_ascii=False).encode("utf-8")
        payload = _fetch_logged(
            HUBMAP_SEARCH_API, project_root=project_root, endpoint=HUBMAP_SEARCH_API,
            query=query, method="POST", body=body, headers=_HUBMAP_HEADERS,
        )
        hits: list = []
        total: int | None = None
        if isinstance(payload, dict):
            hits_obj = payload.get("hits")
            if isinstance(hits_obj, dict):
                if isinstance(hits_obj.get("hits"), list):
                    hits = hits_obj["hits"]
                total_obj = hits_obj.get("total")
                if isinstance(total_obj, dict) and isinstance(total_obj.get("value"), int):
                    total = total_obj["value"]
        if not hits:
            break
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            src = hit.get("_source")
            if not isinstance(src, dict):
                continue
            rec = _hubmap_to_record(src)
            if rec is None or rec["dataset_uid"] in seen:
                continue
            seen.add(rec["dataset_uid"])
            report = corpus_enrich.backfill_record(rec)   # 端点不供的 species/disease 等 → 反标（留痕）
            if "species" in report["filled"]:
                n_backfill_species += 1
            if sp and sp not in str(rec.get("species") or "").lower():
                continue  # species 本地子串过滤（边滤边攒，见函数 docstring 的 R9 说明）
            records.append(rec)
            if len(records) >= limit:
                break
        page_from += limit
        if total is not None and page_from >= min(total, 10000):  # ES 深分页窗口
            break
    if n_backfill_species:
        warnings.append(
            f"{n_backfill_species} 条候选的 species 由标题/描述反标回填（HuBMAP 端点不供该字段，"
            "disease 同理），值集不穷尽（metadata_provenance 已留痕）。"
        )
    if records:
        warnings.append(
            "HuBMAP 候选未查询文件清单：has_raw_data / download_url / filesize 留空（诚实缺省，不猜值）；"
            "原始数据可及性请到数据集页面核实。"
        )
    return records, warnings


# ==============================================================================================
# Broad Single Cell Portal 适配器：全量列表（TTL 缓存）+ 本地过滤 + 逐条详情富化。
# 列表边界与骨架映射移植自 research/reports/t07-scp-formal-candidate/
# build_scp_formal_candidate.py；详情富化（description/count/文件三簇）移植自
# research/staging/single-cell-portal/t34/build_promotion.py。
# ==============================================================================================

SCP_LIST_API = "https://singlecell.broadinstitute.org/single_cell/api/v1/site/studies"
SCP_STUDY_TMPL = "https://singlecell.broadinstitute.org/single_cell/study/{accession}"
SCP_SOURCE_LABEL = "Broad Single Cell Portal"
_SCP_HEADERS = {"Accept": "application/json"}
_SCP_ACCESSION_RE = re.compile(r"^SCP[0-9]+$")
_SCP_FASTQ_RE = re.compile(r"\.(fastq|fq)(\.gz)?$", re.I)
#: 论文 DOI 匹配（移植自 t34 DOI_RE）：只用于**报告**，绝不冒充数据集 collection_doi。
_SCP_DOI_RE = re.compile(r"(?:doi\.org/|doi:\s*)(10\.\d{4,9}/\S+)", re.I)


def _scp_strip_html(value: object) -> str:
    """剥 HTML 标签 + 实体反转 + 空白折叠（移植自 t34 strip_html）；非字符串/剥空 → ""。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _scp_is_fastq(file: dict) -> bool:
    """Fastq 判定：file_type=="Fastq" 或文件名 .fastq/.fq(.gz)（移植自 t34 is_fastq_file）。"""
    if str(file.get("file_type") or "").strip().lower() == "fastq":
        return True
    return bool(_SCP_FASTQ_RE.search(str(file.get("name") or "")))


def _scp_to_record(row: dict) -> dict:
    """列表行 → 候选记录骨架（详情未富化）。调用方已按 t07 public_state 口径过滤
    （public is True + accession ^SCP[0-9]+$ + 标题非空）。

    species/tissue/disease/chemistry/published_date 端点不供（t07/t34 同口径）→ None，
    其中前四维由调用方 backfill_record 反标；文件三簇在详情核实前一律 None（不猜值）。"""
    accession = str(row.get("accession") or "").strip()
    return {
        "dataset_uid": f"scp:{accession}",
        "dataset_name": str(row.get("name") or "").strip(),
        "source": SCP_SOURCE_LABEL,
        "public_accession": accession,
        "collection_doi": None,   # 端点不供数据集 DOI；publications[].url 的论文 DOI 只报告不冒充
        "species": None,
        "tissue": None,
        "disease": None,
        "chemistry": None,
        "platform": None,
        "count": None,
        "unit": None,
        "has_raw_data": None,   # 详情文件清单未核实前不猜值
        "published_date": None,   # 端点不供
        "url": SCP_STUDY_TMPL.format(accession=accession),
        "download_url": None,
        "description": _scp_strip_html(row.get("description")) or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": SCP_LIST_API,
            "fields": {
                "species": {"origin": "not supplied by public API", "complete": False},
                "published_date": {"origin": "not supplied by public API", "complete": False},
            },
        },
    }


def _scp_apply_detail(record: dict, detail: dict) -> list[str]:
    """详情响应富化一条候选（就地，移植自 t34 的详情段），返回发现的论文 DOI 列表（只报告用）。

    study_files 非列表（如 "Unavailable (cannot load study workspace or bucket)" 占位串）→
    文件三簇保持 None 不猜值（t34 同口径：清单不可用 ≠ 没有 FASTQ）。"""
    desc = _scp_strip_html(detail.get("full_description")) or _scp_strip_html(detail.get("description"))
    if desc:
        record["description"] = desc
    cell_count = detail.get("cell_count")
    if isinstance(cell_count, int) and not isinstance(cell_count, bool) and cell_count > 0:
        record["count"] = cell_count
        record["unit"] = "cells"
    files_raw = detail.get("study_files")
    if isinstance(files_raw, list):
        files = [f for f in files_raw if isinstance(f, dict)]
        record["has_raw_data"] = any(_scp_is_fastq(f) for f in files)
        sizes = [f.get("upload_file_size") for f in files]
        if files and all(isinstance(s, int) and not isinstance(s, bool) and s >= 0 for s in sizes):
            record["filesize"] = sum(sizes)   # 任一未知 → 保持 None（不猜）
        downloadable = [f for f in files
                        if f.get("download_url") and isinstance(f.get("upload_file_size"), int)]
        if len(files) == 1 and len(downloadable) == 1:
            record["download_url"] = str(downloadable[0]["download_url"])   # 仅单文件且字节已知
    dois: list[str] = []
    pubs = detail.get("publications")
    if isinstance(pubs, list):
        for pub in pubs:
            if not isinstance(pub, dict):
                continue
            m = _SCP_DOI_RE.search(str(pub.get("url") or ""))
            if m:
                doi = m.group(1).rstrip(".").lower()
                if doi not in dois:
                    dois.append(doi)
    return dois


def _search_scp(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """SCP 全量列表（TTL 缓存 300s）→ 本地过滤 → 前 limit 条逐条详情富化 → 候选（不落盘）。

    列表边界：public is True + accession ^SCP[0-9]+$ + 标题非空（t07 public_state 同口径）；
    关键词本地过滤（name+description 小写子串，多词 AND）；详情失败优雅降级不中断
    （文件三簇/计数停留 None，warnings 如实告知）；species 过滤为本地子串过滤
    （SCP 的 species 只能来自 backfill 反标，故过滤在反标后做）。返回 (records, warnings)。"""
    payload = _cached_fetch_logged(
        SCP_LIST_API, project_root=project_root, endpoint=SCP_LIST_API,
        query=query, headers=_SCP_HEADERS,
    )
    rows = payload if isinstance(payload, list) else []
    terms = [t for t in re.split(r"\s+", query.lower()) if t]
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("public") is not True:
            continue
        accession = str(row.get("accession") or "")
        if not _SCP_ACCESSION_RE.fullmatch(accession):
            continue
        name = str(row.get("name") or "").strip()
        if not name or accession in seen:
            continue
        text = f"{name} {row.get('description') or ''}".lower()
        if not all(t in text for t in terms):
            continue
        seen.add(accession)
        candidates.append(row)

    warnings: list[str] = []
    records: list[dict] = []
    n_detail_fail = 0
    n_backfill_species = 0
    pub_doi_accessions = 0
    for row in candidates[:limit]:
        accession = str(row.get("accession") or "").strip()
        rec = _scp_to_record(row)
        detail: dict | None = None
        try:
            detail = _fetch_logged(
                f"{SCP_LIST_API}/{urllib.parse.quote(accession)}",
                project_root=project_root, endpoint=SCP_LIST_API, query=accession,
                headers=_SCP_HEADERS,
            )
        except CurateError:
            n_detail_fail += 1   # 详情失败优雅降级（沿用 AE/t34 口径），不中断整体搜索
        if isinstance(detail, dict):
            if _scp_apply_detail(rec, detail):
                pub_doi_accessions += 1
        report = corpus_enrich.backfill_record(rec)   # 端点不供的 species/tissue/disease/chemistry → 反标
        if "species" in report["filled"]:
            n_backfill_species += 1
        records.append(rec)
    if species:
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    if n_detail_fail:
        warnings.append(
            f"{n_detail_fail} 条详情拉取失败：description/count/文件信息停留在列表口径，"
            "has_raw_data 留空不猜值（诚实降级）。"
        )
    if n_backfill_species:
        warnings.append(
            f"{n_backfill_species} 条候选的 species 由标题/描述反标回填（SCP 端点不供该字段，"
            "tissue/disease/chemistry 同理；published_date 端点也不供、留空），值集不穷尽"
            "（metadata_provenance 已留痕）。"
        )
    if pub_doi_accessions:
        warnings.append(
            f"{pub_doi_accessions} 条候选的出版物字段含论文 DOI（非数据集 DOI）："
            "按纪律只报告、未写入 collection_doi。"
        )
    return records, warnings


# ==============================================================================================
# HCA（Human Cell Atlas）适配器：Azul `/index/projects`（免认证，2026-08-08 接入；
# 配方与实测证据见《数据源API调研-2026-08-08.md》§1）。
# Azul **无服务端全文检索**（filters 只做 facet 词条精确匹配）：search_online =
# genusSpecies facet 物种过滤（服务端）+ 跟随 pagination.next 键集分页拉取后**客户端**对
# 标题/描述/entryId 匹配关键词（全库仅 532 项、size≤75，全量 8 页代价可忽略）。
# ==============================================================================================

AZUL_PROJECTS_API = "https://service.azul.data.humancellatlas.org/index/projects"
HCA_SOURCE_LABEL = "Human Cell Atlas"
HCA_STUDY_TMPL = "https://data.humancellatlas.org/explore/projects/{project_id}"
_AZUL_PAGE_SIZE = 75    # 实测 size 上限（>75 → 400）
_AZUL_MAX_PAGES = 12    # 全库 532 项全量 8 页即可拉完；页数上限只是防分页游标失控的兜底

#: 通用名（小写）→ Azul genusSpecies facet 词表值（学名）：由 ORGANISM_COMMON 反推，
#: 同名取先见的规范二名（拼写变体如 homo sapien 排后自然不覆盖）；表内键是小写学名，
#: facet 精确匹配大小写敏感，首字母大写还原词表形态（mus musculus → Mus musculus）。
#: 词表外的物种不做服务端过滤，回退映射后本地子串过滤（与 AE 同口径）。
_SPECIES_TO_LATIN: dict[str, str] = {}
for _latin, _common in ORGANISM_COMMON.items():
    _SPECIES_TO_LATIN.setdefault(_common.lower(), _latin.capitalize())
del _latin, _common


def _azul_field_values(groups: object, field: str) -> list[str]:
    """Azul 聚合字段形态 [{field: [v, ...]}, ...] → 指定字段拍平成字符串列表（None/非串跳过）。"""
    out: list[str] = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict):
                values = group.get(field)
                if isinstance(values, list):
                    out.extend(str(v).strip() for v in values if isinstance(v, str) and v.strip())
    return out


def _azul_to_record(hit: dict) -> dict | None:
    """一条 Azul projects 命中 → 本项目记录 schema。无 entryId/标题 → 丢弃。

    字段口径（2026-08-08 实测响应形状）：标题/描述/估算细胞数取 projects[0]；物种取
    donorOrganisms[*].genusSpecies（学名 → 通用名）；组织取 samples[*].effectiveOrgan；
    疾病取 specimens/donorOrganisms 的 disease（健康态归一 normal）；日期取
    dates[0].aggregateSubmissionDate；文件簇取 fileTypeSummaries（format 含 fastq →
    has_raw_data=True，filesize=各档 totalSize 求和）。Azul 只供**论文** DOI
    （publications[].doi）、没有数据集级 DOI——按 SCP 同纪律不冒充，collection_doi 留 None；
    单文件直链端点不供，download_url 留 None（下载入口在项目页）。"""
    entry_id = str(hit.get("entryId") or "").strip()
    projects = hit.get("projects")
    proj = projects[0] if (isinstance(projects, list) and projects
                           and isinstance(projects[0], dict)) else {}
    title = str(proj.get("projectTitle") or "").strip()
    if not entry_id or not title:
        return None
    species = map_species(_azul_field_values(hit.get("donorOrganisms"), "genusSpecies"))
    organs = _azul_field_values(hit.get("samples"), "effectiveOrgan")
    if not organs:
        organs = _azul_field_values(hit.get("samples"), "organ")
    diseases_raw = (_azul_field_values(hit.get("specimens"), "disease")
                    + _azul_field_values(hit.get("donorOrganisms"), "disease"))
    disease = _clean_join([c for c in (_canonical_disease(d) for d in diseases_raw) if c])
    assays = _azul_field_values(hit.get("protocols"), "libraryConstructionApproach")

    has_raw: bool | None = None
    filesize: int | None = None
    fts = hit.get("fileTypeSummaries")
    if isinstance(fts, list) and fts:
        formats = [str(f.get("format") or "").lower() for f in fts if isinstance(f, dict)]
        has_raw = any(fmt.startswith("fastq") for fmt in formats)
        sizes = [f.get("totalSize") for f in fts if isinstance(f, dict)]
        if sizes and all(isinstance(s, (int, float)) and not isinstance(s, bool) and s >= 0 for s in sizes):
            filesize = int(sum(sizes))   # 项目全文件字节合计（任一未知 → 保持 None 不猜）

    cell_count = proj.get("estimatedCellCount")
    count = ""
    if isinstance(cell_count, (int, float)) and not isinstance(cell_count, bool) and cell_count > 0:
        count = str(int(cell_count))
    published = ""
    dates = hit.get("dates")
    if isinstance(dates, list) and dates and isinstance(dates[0], dict):
        published = str(dates[0].get("aggregateSubmissionDate") or "")[:10]
    desc = re.sub(r"\s+", " ", str(proj.get("projectDescription") or "")).strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"

    return {
        "dataset_uid": f"hca:{entry_id}",
        "dataset_name": title,
        "source": HCA_SOURCE_LABEL,
        "public_accession": entry_id,
        "collection_doi": None,   # Azul 只供论文 DOI（publications[].doi）：按纪律不冒充数据集 DOI
        "species": species or None,
        "tissue": _clean_join(organs) or None,
        "disease": disease or None,
        "chemistry": ", ".join(dict.fromkeys(assays)) or None,   # 精确建库方案
        "platform": _cxg_platform_hint(assays) or None,          # 平台家族提示（与 CELLxGENE 同口径）
        "count": count or None,
        "unit": "Cells" if count else None,
        "has_raw_data": has_raw,
        "published_date": published or None,
        "url": HCA_STUDY_TMPL.format(project_id=entry_id),
        "download_url": None,   # 端点不供单文件直链（不猜）；下载入口在项目页
        "description": desc or None,
        "filesize": filesize,
        "metadata_provenance": {
            "source_endpoint": AZUL_PROJECTS_API,
            "fields": {
                "collection_doi": {"origin": "only publication DOIs supplied; not a dataset DOI",
                                   "complete": False},
                "download_url": {"origin": "no per-file direct link supplied by index endpoint",
                                 "complete": False},
            },
        },
    }


def _search_hca(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """HCA（Azul）facet 过滤 + 分页拉取 + 客户端关键词匹配 → 候选记录（不落盘）。

    无服务端全文检索：query 小写分词后对 标题/描述/entryId 连接文本子串匹配（多词 AND）——
    entryId 进匹配域是为了让 sync_updates「按编号搜回」能命中（uuid 不是标题词）。
    物种在词表内 → 服务端 genusSpecies facet 精确过滤；词表外 → 映射后本地子串过滤
    （AE 同口径）。分页跟随 pagination.next（键集分页，size=75）。返回 (records, warnings)。"""
    latin = _SPECIES_TO_LATIN.get(species.lower()) if species else None
    url: str | None = f"{AZUL_PROJECTS_API}?size={_AZUL_PAGE_SIZE}"
    if latin:
        filters = json.dumps({"genusSpecies": {"is": [latin]}}, separators=(",", ":"))
        url += f"&filters={urllib.parse.quote(filters)}"
    terms = [t for t in re.split(r"\s+", query.lower()) if t]

    records: list[dict] = []
    seen: set[str] = set()
    pages = 0
    while url and len(records) < limit and pages < _AZUL_MAX_PAGES:
        payload = _fetch_logged(url, project_root=project_root, endpoint=AZUL_PROJECTS_API, query=query)
        pages += 1
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            # Azul 无公开 OpenAPI 文档（/openapi 实测要鉴权）：形状漂移 fail-closed 如实报错，不硬解析。
            raise CurateError(
                "network_error",
                "HCA（Azul）接口的响应形状变了（该服务无公开 API 文档，可能随版本静默变更）："
                "缺 hits 列表。本次没有拿回任何记录；可到 https://data.humancellatlas.org/ 人工核对。",
            )
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            projects = hit.get("projects")
            proj = projects[0] if (isinstance(projects, list) and projects
                                   and isinstance(projects[0], dict)) else {}
            text = " ".join([
                str(proj.get("projectTitle") or ""),
                str(proj.get("projectDescription") or ""),
                str(hit.get("entryId") or ""),
            ]).lower()
            if not all(t in text for t in terms):
                continue
            rec = _azul_to_record(hit)
            if rec is None or rec["dataset_uid"] in seen:
                continue
            seen.add(rec["dataset_uid"])
            records.append(rec)
            if len(records) >= limit:
                break
        pagination = payload.get("pagination") if isinstance(payload, dict) else None
        nxt = pagination.get("next") if isinstance(pagination, dict) else None
        # 只跟随同服务绝对 URL（防响应里混入奇怪链接被当成下一页）。
        url = nxt if (isinstance(nxt, str) and nxt.startswith(AZUL_PROJECTS_API)) else None
    if species and not latin:  # 词表外物种：本地子串过滤（作用于映射后的通用名）
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    return records[:limit], []


# ==============================================================================================
# 10x Genomics 适配器：官网前端私有搜索 API `/api/search?document=dataset`（免认证，
# 2026-08-08 接入；配方与实测证据见《数据源API调研-2026-08-08.md》§2）。
# **风险声明：私有接口、无官方契约**——10x 可随时改参数/改形状/加鉴权。故响应一律先过
# `_tenx_validate_payload` 形状校验：字段缺失/类型漂移 → fail-closed 如实报错（network_error
# + 写明漂移与人工核对入口），绝不拿畸形响应硬解析充数。
# ==============================================================================================

TENX_SEARCH_API = "https://www.10xgenomics.com/api/search"
TENX_SOURCE_LABEL = "10x Genomics"
_TENX_BASE = "https://www.10xgenomics.com"
_TENX_FULL_LIMIT = 1000  # 实测 limit 上限内（当前全库 786 条，slug 直查一次拉全）
_TENX_SLUG_RE = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)+")  # 「形似 slug」判定（sync 按编号搜回用；
#: 实测 slug 含下划线与大写：160k_DTC_Matched_PBMC_…，故字符集收 [-_]、比较前统一小写）
#: 物种 facet 服务端词表只钉 2026-08-08 实测过的两个显示值（facets 里还有学名/「Human, Mouse」
#: 等脏取值，不敢猜映射）；其余物种回退映射后本地子串过滤（与 AE 同口径）。
_TENX_SPECIES_TAG = {"human": "Human", "mouse": "Mouse"}


def _tenx_validate_payload(payload: Any) -> list[dict]:
    """10x 私有 API 响应形状闸：通过 → results 列表；任一处漂移 → CurateError 如实报错。

    为什么整单拒收而不是跳过坏条目：私有接口无契约，一条畸形即说明我们对形状的理解已过期，
    挑挑拣拣会把「契约漂移」伪装成「正常少几条」。"""
    if (isinstance(payload, dict) and isinstance(payload.get("meta"), dict)
            and isinstance(payload.get("results"), list)
            and isinstance(payload["meta"].get("count"), int)
            and not isinstance(payload["meta"]["count"], bool)
            and all(isinstance(r, dict) and isinstance(r.get("title"), str) and r["title"].strip()
                    and ((isinstance(r.get("slug"), str) and r["slug"].strip())
                         or (isinstance(r.get("path"), str) and r["path"].strip()))
                    for r in payload["results"])):
        return payload["results"]
    raise CurateError(
        "network_error",
        "10x 官网数据集接口的响应形状变了（它是官网前端私有接口、无官方契约，可能随时漂移）。"
        "本次没有拿回任何记录、没有入库；可到 https://www.10xgenomics.com/datasets 人工核对，"
        "或改用其它来源联网搜。",
    )


def _tenx_epoch_date(value: object) -> str:
    """publishedAt（Unix 秒，字符串）→ UTC 日历日期；非数字/越界 → ""（不猜）。"""
    try:
        return datetime.fromtimestamp(int(str(value).strip()), tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _tenx_to_record(item: dict) -> dict:
    """一条 10x 搜索结果 → 本项目记录 schema（调用方已过 `_tenx_validate_payload` 形状闸）。

    端点不供文件清单与数据集 DOI：has_raw_data/filesize/download_url/collection_doi 留 None
    （诚实缺省，不猜值；metadata_provenance 留痕）。species 取学名列表映射通用名，
    缺省时回退 speciesName。"""
    slug = str(item.get("slug") or "").strip()
    if not slug:
        slug = str(item.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
    path = str(item.get("path") or "").strip()
    page_url = _TENX_BASE + (path if path.startswith("/") else f"/datasets/{slug}")
    species_raw = [s for s in (item.get("species") or []) if isinstance(s, str)]
    species = map_species(species_raw)
    if not species and item.get("speciesName"):
        species = map_species([str(item["speciesName"])])
    diseases = []
    for d in (item.get("diseaseStateNames") or []):
        if not isinstance(d, str) or not d.strip():
            continue
        # 10x 词表的健康态写法是 "non-diseased"（2026-08-08 实测）→ 归一到规范 "normal"
        # （与 _HEALTHY 的 xdc1 口径一致：「健康」是已知事实，不是「没标注」）。
        diseases.append("normal" if _norm_token(d) in {"non diseased", "nondiseased"}
                        else (_canonical_disease(d) or ""))
    platforms = [p for p in (item.get("platformName") or []) if isinstance(p, str)]
    chemistries = [c for c in (item.get("chemistries") or []) if isinstance(c, str)]
    desc = re.sub(r"\s+", " ", str(item.get("body") or "")).strip()   # body 是 Markdown，折叠空白
    if len(desc) > 400:
        desc = desc[:400] + "…"
    return {
        "dataset_uid": f"10x:{slug}",
        "dataset_name": str(item["title"]).strip(),
        "source": TENX_SOURCE_LABEL,
        "public_accession": slug,
        "collection_doi": None,   # 端点不供数据集 DOI
        "species": species or None,
        "tissue": _clean_join(item.get("anatomicalEntities")) or None,
        "disease": _clean_join([d for d in diseases if d]) or None,
        "chemistry": ", ".join(dict.fromkeys(chemistries)) or None,
        "platform": ", ".join(dict.fromkeys(platforms)) or None,   # 10x 官方平台名（Visium/Chromium…）
        "count": None,          # 端点不供细胞数
        "unit": None,
        "has_raw_data": None,   # 端点不供文件清单（不猜值）
        "published_date": _tenx_epoch_date(item.get("publishedAt")) or None,
        "url": page_url,
        "download_url": None,   # 端点不供直链；下载入口在数据集页面
        "description": desc or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": TENX_SEARCH_API,
            "fields": {
                "files": {"origin": "not queried; private search API supplies no file manifest",
                          "complete": False},
                "collection_doi": {"origin": "not supplied by private search API", "complete": False},
            },
        },
    }


def _search_tenx(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """10x 官网私有搜索 API → 候选记录（不落盘）。

    两条路径：
    - **slug 直查**（query 形似 slug，sync_updates「按编号搜回」走这里）：slug 不是全文词，
      search= 打不中——拉全量清单（limit=1000 一次拉完）做 slug 精确匹配；匹配不到落回关键词流程
      （"single-cell" 这类带连字符的真关键词不能被 slug 模式吃掉）；
    - **关键词**：search= 全文 + 物种在实测词表内时 tag[species]= 服务端过滤，词表外回退
      映射后本地子串过滤（AE 同口径）。
    响应先过 `_tenx_validate_payload` 形状闸（私有接口无契约，漂移即如实失败）。返回 (records, warnings)。"""
    query_s = query.strip()
    if _TENX_SLUG_RE.fullmatch(query_s.lower()):
        url = (f"{TENX_SEARCH_API}?document=dataset&sort=publishedAt%20DESC"
               f"&limit={_TENX_FULL_LIMIT}&offset=0")
        payload = _fetch_logged(url, project_root=project_root, endpoint=TENX_SEARCH_API,
                                query=f"slug:{query_s}")
        results = _tenx_validate_payload(payload)
        probe = query_s.lower()
        hits = [r for r in results if str(r.get("slug") or "").strip().lower() == probe]
        if hits:
            return [_tenx_to_record(hits[0])], []

    params = ["document=dataset", f"search={urllib.parse.quote(query_s)}",
              f"limit={int(limit)}", "offset=0", "sort=publishedAt%20DESC"]
    tag = _TENX_SPECIES_TAG.get(species.lower()) if species else None
    if tag:
        params.append(f"tag%5Bspecies%5D={urllib.parse.quote(tag)}")
    payload = _fetch_logged(
        TENX_SEARCH_API + "?" + "&".join(params),
        project_root=project_root, endpoint=TENX_SEARCH_API, query=query_s,
    )
    results = _tenx_validate_payload(payload)
    records: list[dict] = []
    seen: set[str] = set()
    for item in results[:limit]:
        rec = _tenx_to_record(item)
        if rec["dataset_uid"] in seen:
            continue
        seen.add(rec["dataset_uid"])
        records.append(rec)
    if species and not tag:  # 词表外物种：本地子串过滤（作用于映射后的通用名）
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    warnings: list[str] = []
    if records:
        warnings.append(
            "10x 官网接口不提供文件清单与数据集 DOI：has_raw_data / download_url / filesize / "
            "collection_doi 留空（诚实缺省，不猜值）；原始数据可及性请到数据集页面核实。"
        )
    return records, warnings


# ==============================================================================================
# NCBI GEO 适配器：E-utilities esearch/esummary（免 key，2026-08-07 接入；
# 配方与实测证据见《数据源API调研-2026-08-08.md》§4）。
# 要点：esearch(db=gds) 用 "GSE"[Entry Type] 枚举只取 Series 级（与本地 geo.json 快照同口径）；
# 实验类型**不能**写 "Expression profiling by high throughput sequencing"[Entry Type]
# （实测被静默忽略），要过滤须走 esummary 的 gdstype 字段；retstart/retmax 分页；
# 无 key 官方红线 ≤3 req/s（_GEO_MIN_INTERVAL，比默认 ≤5 req/s 更严）。
# 礼貌声明：官方建议带 tool/email 参数——本仓库无对外联系邮箱，只带 tool 名，不编造 email。
# ==============================================================================================

GEO_ESEARCH_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
GEO_ESUMMARY_API = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
GEO_SOURCE_LABEL = "NCBI GEO"
GEO_STUDY_TMPL = "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={accession}"
_GEO_TOOL_PARAM = "tool=biodata_agent"
_GEO_MIN_INTERVAL = 0.34   # NCBI 无 key 官方红线 ≤3 req/s
_GEO_ACCESSION_RE = re.compile(r"GSE[0-9]+")


def _geo_esearch_ids(payload: Any) -> list[str]:
    """esearch JSON 形状闸：通过 → GDS UID 列表；缺 esearchresult.idlist 列表 →
    CurateError 如实报错（fail-closed，不硬解析）。"""
    result = payload.get("esearchresult") if isinstance(payload, dict) else None
    idlist = result.get("idlist") if isinstance(result, dict) else None
    if not isinstance(idlist, list):
        raise CurateError(
            "network_error",
            "NCBI E-utilities 的响应形状变了（esearch 缺 esearchresult.idlist）。"
            "本次没有拿回任何记录、没有入库；可到 https://www.ncbi.nlm.nih.gov/geo/ 人工核对。",
        )
    return [str(i).strip() for i in idlist if str(i).strip()]


def _geo_summary_docs(payload: Any) -> list[dict]:
    """esummary JSON 形状闸：通过 → 按 uids 顺序的文档列表；缺 result.uids 列表 →
    CurateError 如实报错（fail-closed，不硬解析）。"""
    result = payload.get("result") if isinstance(payload, dict) else None
    uids = result.get("uids") if isinstance(result, dict) else None
    if not isinstance(uids, list):
        raise CurateError(
            "network_error",
            "NCBI E-utilities 的响应形状变了（esummary 缺 result.uids）。"
            "本次没有拿回任何记录、没有入库；可到 https://www.ncbi.nlm.nih.gov/geo/ 人工核对。",
        )
    return [result[str(u)] for u in uids if isinstance(result.get(str(u)), dict)]


def _geo_to_record(doc: dict) -> dict | None:
    """一条 esummary 文档 → 本项目记录 schema。缺 accession/标题 → None（调用方跳过）。

    字段口径（调研 §4 实测响应形状）：物种 taxon（学名 → 通用名）；日期 pdat
    （"2026/08/04" → ISO）；platform 存 GEO 官方实验类型 gdstype；count=n_samples
    （单位 Samples）；download_url=ftplink（官方给的 Series FTP 目录，缺省不猜）。
    esummary 不供组织/疾病/建库方案/文件清单/数据集 DOI → tissue/disease/chemistry/
    has_raw_data/filesize/collection_doi 留 None（诚实缺省，不猜值；has_raw_data 未知≠无，
    与 geo.json 快照纪律一致；metadata_provenance 留痕）。"""
    acc = str(doc.get("accession") or "").strip()
    title = str(doc.get("title") or "").strip()
    if not acc or not title:
        return None
    species = map_species([str(doc.get("taxon") or "")])
    n_samples = doc.get("n_samples")
    count = ""
    if isinstance(n_samples, (int, float)) and not isinstance(n_samples, bool) and n_samples > 0:
        count = str(int(n_samples))
    ftplink = str(doc.get("ftplink") or "").strip()
    gdstype = str(doc.get("gdstype") or "").strip()
    desc = re.sub(r"\s+", " ", str(doc.get("summary") or "")).strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    return {
        "dataset_uid": f"geo:{acc}",
        "dataset_name": title,
        "source": GEO_SOURCE_LABEL,
        "public_accession": acc,
        "collection_doi": None,   # esummary 不供数据集 DOI
        "species": species or None,
        "tissue": None,           # esummary 不供（不猜值）
        "disease": None,          # esummary 不供（不猜值）
        "chemistry": None,        # 建库方案 esummary 不供；实验类型存 platform（gdstype）
        "platform": gdstype or None,
        "count": count or None,
        "unit": "Samples" if count else None,
        "has_raw_data": None,     # esummary 判不了原始数据可及性（未知非无，与快照纪律一致）
        "published_date": str(doc.get("pdat") or "").strip().replace("/", "-") or None,
        "url": GEO_STUDY_TMPL.format(accession=acc),
        "download_url": ftplink or None,   # 官方 ftplink（Series FTP 目录）
        "description": desc or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": GEO_ESUMMARY_API,
            "geo_uid": str(doc.get("uid") or ""),
            "source_taxon": str(doc.get("taxon") or "").strip(),
            "gdstype": gdstype,
            "fields": {
                "tissue_disease": {"origin": "not supplied by esummary", "complete": False},
                "has_raw_data": {"origin": "raw-data availability not derivable from esummary",
                                 "complete": False},
                "collection_doi": {"origin": "not supplied by esummary", "complete": False},
            },
        },
    }


def _search_geo(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """NCBI E-utilities 两段式（esearch → esummary 批量富化）→ 候选记录（不落盘）。

    term 组装：`(query) AND "GSE"[Entry Type]`（只取 Series 级）；物种在词表内 →
    `AND "学名"[Organism]` 服务端过滤，词表外 → 回退映射后本地子串过滤（AE 同口径）。
    query 形似 GSE 编号时走同一路径自然命中 accession 全文索引（sync_updates「按编号
    搜回」无需单独通道）。实验类型不进 term（Entry Type 短语实测被忽略）；gdstype
    存进 platform 字段供参考。限速 ≤3 req/s（NCBI 无 key 红线）。返回 (records, warnings)。"""
    query_s = query.strip()
    latin = _SPECIES_TO_LATIN.get(species.lower()) if species else None
    term_parts = [f"({query_s})"]
    if latin:
        term_parts.append(f'"{latin}"[Organism]')
    term_parts.append('"GSE"[Entry Type]')
    term = " AND ".join(term_parts)
    esearch_url = (f"{GEO_ESEARCH_API}?db=gds&term={urllib.parse.quote(term)}"
                   f"&retmax={int(limit)}&retmode=json&{_GEO_TOOL_PARAM}")
    payload = _fetch_logged(esearch_url, project_root=project_root, endpoint=GEO_ESEARCH_API,
                            query=query_s, min_interval=_GEO_MIN_INTERVAL)
    ids = _geo_esearch_ids(payload)
    if not ids:
        return [], []
    esummary_url = (f"{GEO_ESUMMARY_API}?db=gds&id={','.join(ids)}"
                    f"&retmode=json&{_GEO_TOOL_PARAM}")
    payload = _fetch_logged(esummary_url, project_root=project_root, endpoint=GEO_ESUMMARY_API,
                            query=query_s, min_interval=_GEO_MIN_INTERVAL)
    records: list[dict] = []
    seen: set[str] = set()
    for doc in _geo_summary_docs(payload):
        rec = _geo_to_record(doc)
        if rec is None or rec["dataset_uid"] in seen:
            continue
        seen.add(rec["dataset_uid"])
        records.append(rec)
    if species and not latin:  # 词表外物种：本地子串过滤（作用于映射后的通用名）
        sp = species.lower()
        records = [r for r in records if sp in str(r.get("species") or "").lower()]
    records = records[:limit]
    warnings: list[str] = []
    if records:
        warnings.append(
            "GEO esummary 不提供组织/疾病/文件清单与数据集 DOI：tissue / disease / "
            "has_raw_data / filesize / collection_doi 留空（诚实缺省，不猜值）；"
            "原始数据可及性请到 Series 页面核实。"
        )
    return records, warnings


# ==============================================================================================
# Zenodo 适配器：公开 REST API（2026-08-08 接入，第 10 源；配方与实测证据见
# 《调研-zenodo等新源-2026-08-08.md》）。
# 要点：Lucene 字段限定查询（metadata.title/description 短语 OR + type=dataset——裸自由词
# 实测噪声大）；官方限速 30 req/min（2025-11 公告，匿名/认证同口径），出口按 20/min 留余量；
# 响应是 legacy/InvenioRDM 混合形状，形状闸只钉两版共有核心字段（hits.hits、id、
# metadata.title），漂移 fail-closed。Zenodo 是**通用开放仓储**（生物数据集只占一部分）；
# 物种/组织/疾病无结构化字段——物种从 title+description 自由文本抠既有物种词表
# （抠不到留空不编），tissue/disease 槽位放弃（诚实缺省 + metadata_provenance 留痕）。
# ==============================================================================================

ZENODO_API = "https://zenodo.org/api/records"
ZENODO_SOURCE_LABEL = "Zenodo"
ZENODO_RECORD_TMPL = "https://zenodo.org/records/{record_id}"
_ZENODO_MIN_INTERVAL = 3.0   # 官方红线 30 req/min，出口按 20/min 留余量（爬虫封禁期礼貌）
_ZENODO_MAX_SIZE = 25        # 匿名 size 上限（>25 → 400）
_ZENODO_ID_RE = re.compile(r"\d{4,}")  # 「形似 record id」判定（sync_updates 按编号搜回用）

#: 物种抽取词表：学名（词边界、大小写不敏感）；真源是 ORGANISM_COMMON 的键。只从
#: title+description 抠——抠得到就标（经 map_species 归通用名）、抠不到留空（不编）。
_ZENODO_SPECIES_RES: "list[tuple[re.Pattern, str]]" = [
    (re.compile(r"\b" + re.escape(latin) + r"\b", re.I), latin)
    for latin in dict.fromkeys(ORGANISM_COMMON)
]


def _zenodo_species_from_text(*texts: str) -> str:
    """从自由文本抠物种（既有词表学名）→ 通用名；抠不到 → ""（诚实缺省）。"""
    combined = " ".join(t for t in texts if t)
    if not combined:
        return ""
    return map_species([latin for rx, latin in _ZENODO_SPECIES_RES if rx.search(combined)])


def _zenodo_lucene_quote(term: str) -> str:
    """Lucene 短语引号清洗：剥掉 term 内引号，防用户关键词把字段查询语法顶断。"""
    return re.sub(r"\s+", " ", term.replace('"', " ")).strip()


def _zenodo_core_ok(node: Any) -> bool:
    """单条 Zenodo 记录（命中或 id 直查）的核心形状：id(int) + metadata.title(str 非空)。
    legacy 与 InvenioRDM 两版共有；description/doi/publication_date 缺失容忍（诚实缺省）。"""
    return (isinstance(node, dict)
            and isinstance(node.get("id"), int) and not isinstance(node.get("id"), bool)
            and isinstance(node.get("metadata"), dict)
            and bool(str(node["metadata"].get("title") or "").strip()))


def _zenodo_validated_hits(payload: Any) -> list[dict]:
    """Zenodo 搜索响应形状闸：通过 → hits.hits 条目列表；漂移 → CurateError 如实报错
    （fail-closed：身份字段是公开契约核心，一条畸形即视为理解过期，不挑挑拣拣凑合用）。"""
    hits = payload.get("hits") if isinstance(payload, dict) else None
    hit_list = hits.get("hits") if isinstance(hits, dict) else None
    if not isinstance(hit_list, list) or not all(_zenodo_core_ok(h) for h in hit_list):
        raise CurateError(
            "network_error",
            "Zenodo 官方 API 的响应形状变了（缺 hits.hits 列表，或条目缺 id/标题）。"
            "本次没有拿回任何记录、没有入库；可到 https://zenodo.org/ 人工核对。",
        )
    return hit_list


def _zenodo_to_record(hit: dict) -> dict | None:
    """一条 Zenodo 记录（已过 `_zenodo_core_ok` 形状闸）→ 本项目记录 schema。

    字段口径（2026-08-08 实测响应形状，调研 §1）：accession/url 用数字 record id；
    collection_doi 取 conceptdoi（指向「所有版本的最新版」，比版本 doi 稳定），缺失回退
    doi / RDM pids.doi.identifier；description 是 HTML，剥标签后截 400；species 从
    title+description 抠既有物种词表（抠不到 None，不编）；resource_type 非 dataset 的
    混入条目 → None（type=dataset 是服务端过滤，混入即跳过）。
    Zenodo 无组织/疾病/建库方案/文件清单结构化字段 → tissue/disease/chemistry/platform/
    has_raw_data/filesize/download_url 留 None（诚实缺省，不猜值；metadata_provenance 留痕）。"""
    meta = hit["metadata"]
    rid = hit["id"]
    title = str(meta.get("title") or "").strip()
    rtype = meta.get("resource_type")
    if isinstance(rtype, dict):
        rt = str(rtype.get("type") or rtype.get("id") or "").strip().lower()
        if rt and rt != "dataset":
            return None
    doi = str(hit.get("conceptdoi") or hit.get("doi") or "").strip()
    if not doi:  # RDM 形状：pids.doi.identifier
        pids = hit.get("pids")
        if isinstance(pids, dict) and isinstance(pids.get("doi"), dict):
            doi = str(pids["doi"].get("identifier") or "").strip()
    desc = re.sub(r"\s+", " ", html.unescape(
        re.sub(r"<[^>]+>", " ", str(meta.get("description") or "")))).strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    published = str(meta.get("publication_date") or "").strip()[:10]
    if not published:  # 兜底顶层 created（ISO 时间戳）
        published = str(hit.get("created") or "").strip()[:10]
    return {
        "dataset_uid": f"zenodo:{rid}",
        "dataset_name": title,
        "source": ZENODO_SOURCE_LABEL,
        "public_accession": str(rid),
        "collection_doi": doi or None,
        "species": _zenodo_species_from_text(title, desc) or None,  # 文本抠取，不全
        "tissue": None,           # Zenodo 无组织结构化字段（槽位放弃，不猜值）
        "disease": None,          # 同上
        "chemistry": None,        # 无建库方案字段（keywords 覆盖率低且自由词，不冒充）
        "platform": None,
        "count": None,
        "unit": None,
        "has_raw_data": None,     # 判不了原始数据可及性（未知非无）
        "published_date": published or None,
        "url": ZENODO_RECORD_TMPL.format(record_id=rid),
        "download_url": None,     # 不拼文件直链（不猜）；下载入口在记录页
        "description": desc or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": ZENODO_API,
            "fields": {
                "species": {"origin": "extracted from title/description free text via species "
                                      "vocabulary; incomplete by construction", "complete": False},
                "tissue_disease": {"origin": "no structured fields in Zenodo; slots abandoned",
                                   "complete": False},
                "has_raw_data": {"origin": "raw-data availability not derivable from record metadata",
                                 "complete": False},
                "download_url": {"origin": "no per-file direct link assembled; record page is the "
                                           "download entry", "complete": False},
            },
        },
    }


def _search_zenodo(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """Zenodo 公开 REST API → 候选记录（不落盘）。

    两条路径：
    - **record id 直查**（query 全数字，sync_updates「按编号搜回」走这里）：数字 id 不是全文词，
      字段查询打不中——`GET /api/records/<id>` 直查，同一道形状闸；
    - **关键词**：Lucene 字段限定 `(metadata.title:"kw" OR metadata.description:"kw") AND type=dataset`；
      物种（词表内→学名，词表外→原词）AND 进 title/description 字段查询做服务端文本过滤
      （Zenodo 无物种字段，这是文本级近似）。
    限速 30 req/min 留余量 20/min；形状漂移 fail-closed 如实报错。返回 (records, warnings)。"""
    query_s = query.strip()
    if _ZENODO_ID_RE.fullmatch(query_s):
        payload = _fetch_logged(
            f"{ZENODO_API}/{query_s}", project_root=project_root,
            endpoint=ZENODO_API, query=f"id:{query_s}", min_interval=_ZENODO_MIN_INTERVAL,
        )
        if not _zenodo_core_ok(payload):
            raise CurateError(
                "network_error",
                f"Zenodo 记录 {query_s} 的响应形状变了（缺 id/标题）。本次没有拿回任何记录、"
                "没有入库；可到 https://zenodo.org/ 人工核对。",
            )
        rec = _zenodo_to_record(payload)
        return ([rec] if rec is not None else []), []

    kw = _zenodo_lucene_quote(query_s)
    parts = [f'(metadata.title:"{kw}" OR metadata.description:"{kw}")']
    if species:
        latin = _SPECIES_TO_LATIN.get(species.lower())
        word = _zenodo_lucene_quote(latin or species)
        parts.append(f'(metadata.title:"{word}" OR metadata.description:"{word}")')
    lucene = " AND ".join(parts)
    url = (f"{ZENODO_API}?q={urllib.parse.quote(lucene)}&type=dataset"
           f"&size={min(int(limit), _ZENODO_MAX_SIZE)}")
    payload = _fetch_logged(
        url, project_root=project_root, endpoint=ZENODO_API, query=query_s,
        min_interval=_ZENODO_MIN_INTERVAL,
    )
    hits = _zenodo_validated_hits(payload)
    records: list[dict] = []
    seen: set[str] = set()
    for hit in hits[:limit]:
        rec = _zenodo_to_record(hit)
        if rec is None or rec["dataset_uid"] in seen:
            continue
        seen.add(rec["dataset_uid"])
        records.append(rec)
    warnings: list[str] = []
    if records:
        warnings.append(
            "Zenodo 是通用开放仓储，生物数据集只占一部分；物种是从标题/描述文本里抠的、不全，"
            "组织/疾病/文件清单无结构化字段（留空，不猜值）。"
        )
    return records, warnings


# ==============================================================================================
# refine.bio 适配器：公开 REST API（2026-08-14 接入，第 11 源；实测证据见
# research/staging/refinebio/mapping.md §0）。
# 要点：全文检索走 `/v1/search/`（ElasticSearch；search= + technology/organism/platform/
# num_downloadable_samples__gt 过滤）——08-08 调研的「?search= 400」是打在 /v1/experiments/
# 上，/v1/search/ 实测可用；但全文是**模糊 OR 匹配**（"spatial transcriptomics" 实测命中
# 1.9 万条），服务端只起召回作用，内容级甄别必须在本地做（见 ingest_refinebio.py 三道闸）。
# 四槽位（organism/disease/specimen_part/technology）原生结构化：experiment 级有
# organism_names/technology/sample_metadata_fields，disease/specimen_part 取值在 samples
# 端点（10-30s/页，检索适配器不拉，ingest 脚本对入选候选单页 best-effort 富化）。
# accession 直达：`GET /v1/experiments/{主 accession}/`（只认主 accession；GSE 副号
# （alternate_accession_code）404 → 如实说明）。无官方限速文档 → 出口 ≤60 req/min。
# 响应两形状：search 列表项（扁平，platform_names/technology 单数）与 experiment 详情
# （嵌套 samples/annotations，platforms/technologies 复数），形状闸只钉两版共有核心
# （accession_code + title），版本特异字段防御式读取，漂移 fail-closed。
# ==============================================================================================

REFINEBIO_API = "https://api.refine.bio/v1"
REFINEBIO_SEARCH_API = f"{REFINEBIO_API}/search/"
REFINEBIO_EXPERIMENTS_API = f"{REFINEBIO_API}/experiments/"
REFINEBIO_SOURCE_LABEL = "refine.bio"
REFINEBIO_EXP_TMPL = "https://www.refine.bio/experiments/{accession}"
_REFINEBIO_MIN_INTERVAL = 1.0   # 无官方限速文档（2026-08-14 实测响应头无限速标注）；礼貌 ≤60 req/min
_REFINEBIO_MAX_LIMIT = 500      # search limit 实测 1000 可用；适配器出口保守取 500
#: 「形似 GEO/SRA/ENA/AE 主 accession」判定（sync_updates 按编号搜回 + accession 直达用）。
_REFINEBIO_ACCESSION_RE = re.compile(
    r"^(?:(?:GSE|SRP|ERP|DRP|PRJNA|PRJEB|PRJDB)\d+|E-[A-Z]{3,5}-\d+)$", re.I)


def _refinebio_organism_param(species: str) -> str:
    """通用名 → refine.bio organism 过滤值（UPPER_SNAKE 学名，如 HOMO_SAPIENS）；
    词表外 → ""（不做服务端过滤，不乱猜映射）。
    G-04：映射真源收敛到 corpus_net.refinebio_organism_param，两个入口同口径——
    此前本函数把带空格的词表外词当二名法学名透传（"white mouse" → WHITE_MOUSE 假过滤）。"""
    return corpus_net.refinebio_organism_param(species)


def _refinebio_core_ok(node: Any) -> bool:
    """单条 refine.bio 实验（search 命中或 accession 直查详情）的核心形状：
    accession_code(str 非空) + title(str 非空)。search 列表项与详情两形状共有；
    description/organism_names/样本计数等缺失容忍（诚实缺省）。"""
    return (isinstance(node, dict)
            and bool(str(node.get("accession_code") or "").strip())
            and bool(str(node.get("title") or "").strip()))


def _refinebio_validated_results(payload: Any) -> list[dict]:
    """refine.bio 分页响应形状闸：通过 → results 条目列表；漂移 → CurateError 如实报错
    （fail-closed：身份字段是公开契约核心，一条畸形即视为理解过期，不挑挑拣拣凑合用）。"""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or not all(_refinebio_core_ok(r) for r in results):
        raise CurateError(
            "network_error",
            "refine.bio 官方 API 的响应形状变了（缺 results 列表，或条目缺 accession_code/标题）。"
            "本次没有拿回任何记录、没有入库；可到 https://www.refine.bio/ 人工核对。",
        )
    return results


def _refinebio_species(hit: dict) -> str:
    """organism_names（UPPER_SNAKE 学名列表）→ 通用名（既有词表；词表外保留原名，不编）。"""
    names = hit.get("organism_names")
    if not isinstance(names, list):
        return ""
    latins = [str(n).strip().replace("_", " ").lower() for n in names if str(n or "").strip()]
    return map_species(latins)


def _refinebio_str_list(value: Any) -> list[str]:
    """防御式读字符串列表字段（platform_names/platforms、technologies 等两形状变体）。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _refinebio_source_database(hit: dict) -> str | None:
    """镜像源库：详情形状原生给 source_database；search 列表项不供 → 按 accession 命名空间
    推导（SRP=SRA / GSE=GEO / ERP·DRP=ENA / E-*=ArrayExpress——命名空间即注册表，非猜值）。"""
    native = str(hit.get("source_database") or "").strip()
    if native:
        return native
    acc = str(hit.get("accession_code") or "").strip().upper()
    if acc.startswith("SRP"):
        return "SRA"
    if acc.startswith("GSE"):
        return "GEO"
    if acc.startswith(("ERP", "DRP")):
        return "ENA"
    if acc.startswith("E-"):
        return SOURCE_ARRAYEXPRESS
    return None


def _refinebio_to_record(hit: dict) -> dict | None:
    """一条 refine.bio 实验（已过 `_refinebio_core_ok` 形状闸）→ 本项目记录 schema。

    字段口径（2026-08-14 实测 //search/ 列表项与 //experiments/{acc}/ 详情两形状）：
    accession/url 用主 accession_code；species 取原生 organism_names（四槽位优势之一）；
    count/unit = num_downloadable_samples 个 "samples"（统一 Salmon 处理后可下载样本数——
    该源核心价值字段）；platform 取 platform_names（详情形状回退 platforms）原文；
    published_date = source_first_published；description 已是纯文本，截 400。
    tissue/disease：experiment 级只有 sample_metadata_fields（槽位存在性），取值在 samples
    端点（10-30s/页，检索适配器不拉）→ 这里恒 None（诚实缺省；ingest 脚本对入选候选经
    `_refinebio_apply_sample_annotations` 单页富化并改写 provenance 对应轴）。
    chemistry/collection_doi/download_url/filesize/has_raw_data：端点不供（publication_doi
    是**论文** DOI 不是数据集 DOI，不冒充 collection_doi；下载走 dataset 令牌聚合流，
    不拼直链）→ 恒 None（不猜值；metadata_provenance 留痕）。"""
    accession = str(hit.get("accession_code") or "").strip()
    title = re.sub(r"\s+", " ", str(hit.get("title") or "")).strip()
    desc = re.sub(r"\s+", " ", str(hit.get("description") or "")).strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    platforms = _refinebio_str_list(hit.get("platform_names")) or _refinebio_str_list(hit.get("platforms"))
    technologies = _refinebio_str_list(hit.get("technologies")) or _refinebio_str_list(hit.get("technology"))
    n_dl = hit.get("num_downloadable_samples")
    count = str(int(n_dl)) if isinstance(n_dl, int) and not isinstance(n_dl, bool) and n_dl > 0 else None
    published = str(hit.get("source_first_published") or "").strip()[:10]
    return {
        "dataset_uid": f"refinebio:{accession}",
        "dataset_name": title,
        "source": REFINEBIO_SOURCE_LABEL,
        "public_accession": accession,
        "alternate_accession": str(hit.get("alternate_accession_code") or "").strip() or None,
        "collection_doi": None,   # publication_doi 是论文 DOI，不冒充数据集 DOI
        "species": _refinebio_species(hit) or None,
        "tissue": None,           # 取值在 samples 端点（慢），检索层不拉；ingest 富化回填
        "disease": None,          # 同上
        "chemistry": None,        # 无建库方案结构化字段（protocol_description 自由文本，不猜）
        "platform": ", ".join(platforms[:4]) or None,
        "count": count,
        "unit": "samples" if count else None,
        "has_raw_data": None,     # 实验级判不了原始数据可及性（未知非无；ingest 可凭样本页回填 True）
        "published_date": published or None,
        "url": REFINEBIO_EXP_TMPL.format(accession=accession),
        "download_url": None,     # 下载走 dataset 令牌聚合流（POST /v1/dataset/），不拼直链
        "description": desc or None,
        "filesize": None,
        "metadata_provenance": {
            "source_endpoint": REFINEBIO_SEARCH_API,
            "fields": {
                "species": {"origin": "native organism_names slot (UPPER_SNAKE latin names) "
                                      "mapped via project species vocabulary", "complete": True},
                "tissue_disease": {"origin": "values live in the samples endpoint (specimen_part/"
                                             "disease slots); not fetched at search level — "
                                             "ingest-time single-page enrichment may backfill",
                                   "complete": False},
                "technology": {"origin": "native technology slot", "values": technologies,
                               "complete": True},
                "count": {"origin": "num_downloadable_samples (harmonized processed samples)",
                          "complete": True},
                "collection_doi": {"origin": "endpoint only offers publication_doi (paper DOI), "
                                             "not a dataset DOI; left null rather than misrepresented",
                                   "complete": False},
                "download_url": {"origin": "downloads go through the dataset-token aggregation flow "
                                           "(POST /v1/dataset/); experiment page is the entry",
                                 "complete": False},
                "alternate_accession": {"value": str(hit.get("alternate_accession_code") or "").strip() or None,
                                        "source_database": _refinebio_source_database(hit),
                                        "complete": True},
            },
        },
    }


def _refinebio_apply_sample_annotations(record: dict, samples: list[dict]) -> None:
    """ingest 富化：samples 端点一页 → tissue/disease/has_raw_data 三轴回填（只认本页证据）。

    tissue = specimen_part 去重保序 join（≤3 个，`_is_informative` 过滤占位值）；disease 同口径
    （disease 字段；disease_stage 粒度不符不进）。has_raw_data：本页任一样本 has_raw=True → True
    （正面证据）；本页全 False → 仍 None（单页不是完整清单，「没见到」≠「没有」——三态纪律
    见 NEXT_SOURCE_ROADMAP §4.2）。provenance 对应轴改写为「samples 端点单页聚合，覆盖可能不全」。"""
    tissues: list[str] = []
    diseases: list[str] = []
    any_raw = False
    for s in samples:
        if not isinstance(s, dict):
            continue
        for field, acc in (("specimen_part", tissues), ("disease", diseases)):
            v = str(s.get(field) or "").strip()
            if v and _is_informative(v) and v.lower() not in {x.lower() for x in acc}:
                acc.append(v)
        if s.get("has_raw") is True:
            any_raw = True
    if tissues:
        record["tissue"] = "; ".join(tissues[:3])
    if diseases:
        record["disease"] = "; ".join(diseases[:3])
    if any_raw:
        record["has_raw_data"] = True
    prov = record.get("metadata_provenance")
    if isinstance(prov, dict) and isinstance(prov.get("fields"), dict):
        prov["fields"]["tissue_disease"] = {
            "origin": f"aggregated from one samples-endpoint page ({len(samples)} samples); "
                      "partial coverage by construction", "complete": False}
        prov["fields"]["has_raw_data"] = {
            "origin": "positive evidence only: any sample in the fetched page with has_raw=true; "
                      "absence stays null (unknown, not no)", "complete": False}


def _refinebio_apply_detail_annotations(record: dict, detail: dict) -> None:
    """ingest 富化回退（samples 端点不可用时的次一级证据）：experiment 详情的 annotations
    是 SRA 样本键值对（实测每个实验通常只挂 1 条）——只认组织/疾病语义明确的键
    （sample_tissue / sample_organism_part / sample_specimen_part → tissue；sample_disease →
    disease；sample_cell_type 是细胞类型不是组织、sample_source_name 多为个体编号，都不冒充）。
    has_raw_data 不动（详情无文件级证据）。provenance 写明证据等级低于 samples 端点聚合。"""
    anns = detail.get("annotations")
    if not isinstance(anns, list):
        return
    tissues: list[str] = []
    diseases: list[str] = []
    for a in anns:
        data = a.get("data") if isinstance(a, dict) else None
        if not isinstance(data, dict):
            continue
        for keys, acc in ((("sample_tissue", "sample_organism_part", "sample_specimen_part"), tissues),
                          (("sample_disease",), diseases)):
            for k in keys:
                v = str(data.get(k) or "").strip()
                if v and _is_informative(v) and v.lower() not in {x.lower() for x in acc}:
                    acc.append(v)
    changed = False
    if tissues and record.get("tissue") in (None, ""):
        record["tissue"] = "; ".join(tissues[:3])
        changed = True
    if diseases and record.get("disease") in (None, ""):
        record["disease"] = "; ".join(diseases[:3])
        changed = True
    if changed:
        prov = record.get("metadata_provenance")
        if isinstance(prov, dict) and isinstance(prov.get("fields"), dict):
            prov["fields"]["tissue_disease"] = {
                "origin": f"experiment-detail annotations (SRA per-sample key-values, "
                          f"{len(anns)} annotation entr{'ies' if len(anns) != 1 else 'y'}; "
                          "weaker evidence than samples-endpoint aggregation)",
                "complete": False}


def _search_refinebio(
    query: str,
    *,
    species: str = "",
    limit: int = 20,
    project_root: Path,
) -> tuple[list[dict], list[str]]:
    """refine.bio 公开 API → 候选记录（不落盘）。

    两条路径：
    - **accession 直查**（query 形似 GEO/SRA/AE 主 accession，sync_updates「按编号搜回」走这里）：
      `GET /v1/experiments/<acc>/`——只认主 accession；副号（如主号是 SRP 时的 GSE）404，
      如实说明并返回空（不假装搜过）；
    - **关键词**：`GET /v1/search/?search=<kw>`（ES 全文，模糊 OR 匹配——召回含弱相关，
      note 如实写明，内容级甄别是调用方的事）；species 词表内 → organism=UPPER_SNAKE 学名
      服务端过滤，词表外不过滤（不乱猜）。
    限速 ≤60 req/min；形状漂移 fail-closed 如实报错。返回 (records, warnings)。"""
    query_s = query.strip()
    if _REFINEBIO_ACCESSION_RE.match(query_s):
        try:
            payload = _fetch_logged(
                f"{REFINEBIO_EXPERIMENTS_API}{query_s}/", project_root=project_root,
                endpoint=REFINEBIO_EXPERIMENTS_API, query=f"accession:{query_s}",
                min_interval=_REFINEBIO_MIN_INTERVAL,
            )
        except CurateError as exc:
            if "HTTP 404" in str(exc.hint):
                return [], [
                    f"refine.bio 没有主 accession 为 {query_s} 的实验（注意：直达只认主 "
                    "accession，GSE 这类常见副号查不到——可到 https://www.refine.bio/ 人工核对）。",
                ]
            raise
        if not _refinebio_core_ok(payload):
            raise CurateError(
                "network_error",
                f"refine.bio 实验 {query_s} 的响应形状变了（缺 accession_code/标题）。本次没有拿回"
                "任何记录、没有入库；可到 https://www.refine.bio/ 人工核对。",
            )
        return [_refinebio_to_record(payload)], []

    url = f"{REFINEBIO_SEARCH_API}?search={urllib.parse.quote(query_s)}&limit={min(int(limit), _REFINEBIO_MAX_LIMIT)}"
    organism = _refinebio_organism_param(species)
    if organism:
        url += f"&organism={organism}"
    species_note = ""
    if str(species or "").strip() and not organism:
        # G-04：词表外物种不过滤但必须用户可见（与 corpus_net.search_refinebio
        # 同口径）——否则无法区分「没有这个物种的数据」与「这个词没被认出来」。
        species_note = f"物种词「{str(species).strip()}」不在已知词表里，这次没有按物种过滤。"
    payload = _fetch_logged(
        url, project_root=project_root, endpoint=REFINEBIO_SEARCH_API, query=query_s,
        min_interval=_REFINEBIO_MIN_INTERVAL,
    )
    results = _refinebio_validated_results(payload)
    records: list[dict] = []
    seen: set[str] = set()
    for hit in results[:limit]:
        rec = _refinebio_to_record(hit)
        if rec is None or rec["dataset_uid"] in seen:
            continue
        seen.add(rec["dataset_uid"])
        records.append(rec)
    warnings: list[str] = []
    if species_note:
        warnings.append(species_note)
    if records:
        warnings.append(
            "refine.bio 是 GEO/SRA/ArrayExpress 的统一加工镜像（ Salmon 定量），与库中 GEO/AE "
            "来源可能指向同一研究（按 accession 去重）；其全文检索是模糊匹配，结果含弱相关条目，"
            "入库前需人工甄别；组织/疾病取值需另拉 samples 端点（此处未拉，留空不猜）。"
        )
    return records, warnings


#: 源适配器注册表（闸 1）。arrayexpress 为首发；2026-08-06 接入 cellxgene / hubmap /
#: single_cell_portal 三支；2026-08-08 接入 hca（Azul facet + 分页本地匹配）/ 10x（官网私有
#: 搜索 API，形状校验 fail-closed）两支；2026-08-07 接入 geo（NCBI E-utilities esearch→esummary
#: 两段式，无 key 限速 ≤3 req/s）；2026-08-08 接入 zenodo（公开 REST API，字段限定 Lucene 查询，
#: 通用仓储如实标注，限速 30 req/min 留余量）；2026-08-14 接入 refinebio（公开 REST API，
#: ES 全文模糊匹配如实标注，≤60 req/min；GEO/SRA/AE 镜像，需按 accession 跨源去重）。
#: 未注册源 → source_not_registered（fail-closed）。
SOURCE_ADAPTERS: dict[str, dict[str, Any]] = {
    "arrayexpress": {
        "label": AE_SOURCE_LABEL,
        "search": _search_arrayexpress,
        "description": "ArrayExpress（EBI BioStudies）：关键词搜索 + 详情两段式富化。",
    },
    "cellxgene": {
        "label": CXG_SOURCE_LABEL,
        "search": _search_cellxgene,
        "description": "CELLxGENE Discover：全量拉取（进程内缓存 300s）+ 本地关键词过滤。",
    },
    "hubmap": {
        "label": HUBMAP_SOURCE_LABEL,
        "search": _search_hubmap,
        "description": "HuBMAP：POST Elasticsearch 查询（公共 Published 数据集 + 空间/多重成像类型"
                       "动态 allowlist）；species/disease 端点不供，反标回填。",
    },
    "single_cell_portal": {
        "label": SCP_SOURCE_LABEL,
        "search": _search_scp,
        "description": "Broad Single Cell Portal：全量列表（缓存 300s）+ 本地过滤 + 逐条详情富化"
                       "（文件清单 → has_raw_data/filesize）；物种等字段反标回填。",
    },
    "hca": {
        "label": HCA_SOURCE_LABEL,
        "search": _search_hca,
        "description": "Human Cell Atlas（Azul）：genusSpecies facet 物种过滤（服务端）+ 分页拉取后"
                       "本地关键词匹配（无服务端全文检索）；文件簇取 fileTypeSummaries。",
    },
    "10x": {
        "label": TENX_SOURCE_LABEL,
        "search": _search_tenx,
        "description": "10x Genomics：官网私有搜索 API（search= 关键词 + tag[species]= 物种 facet）；"
                       "私有接口无官方契约，响应过形状校验，漂移即如实失败。",
    },
    "geo": {
        "label": GEO_SOURCE_LABEL,
        "search": _search_geo,
        "description": "NCBI GEO（E-utilities）：esearch（\"GSE\"[Entry Type] 枚举 + [Organism] "
                       "物种过滤）→ esummary 批量富化；实验类型 gdstype 存 platform 字段；"
                       "无 key 限速 ≤3 req/s。",
    },
    "zenodo": {
        "label": ZENODO_SOURCE_LABEL,
        "search": _search_zenodo,
        "description": "Zenodo：通用开放仓储（生物数据集只占一部分）。字段限定 Lucene 查询"
                       "（metadata.title/description 短语 + type=dataset）；数字 record id 直查"
                       "支持 sync 按编号搜回；限速 30 req/min 留余量 20/min；物种从文本抠取（不全），"
                       "组织/疾病无结构化字段（诚实缺省）。",
    },
    "refinebio": {
        "label": REFINEBIO_SOURCE_LABEL,
        "search": _search_refinebio,
        "description": "refine.bio：GEO/SRA/ArrayExpress 的统一加工镜像（Salmon 定量）。ES 全文"
                       "检索（模糊 OR 匹配，召回含弱相关，需人工甄别）+ 主 accession 直查支持 "
                       "sync 按编号搜回（副号 404 如实说明）；四槽位原生结构化（organism 入库即填，"
                       "disease/specimen_part 取值在 samples 端点，检索层不拉、诚实缺省）；"
                       "count=可下载处理样本数；无官方限速文档，出口 ≤60 req/min。",
    },
}

#: 口语说法 → 注册表键的唯一真源与解析器在 corpus_net（SOURCE_ALIASES / resolve_source_key），
#: 调用点经 corpus_net.resolve_source_key 按各自注册表键集解析（键名注册表契约不变）。


# ==============================================================================================
# 实体级去重：search_online / sync 共用
#
# 问题：search_online 此前**零去重**——同一数据集换关键词反复搜会反复入库（import 的内容 hash
# 去重是**整集文件**粒度，盖不到「联网候选 vs 库中既有记录」的实体粒度；sync 此前也只按
# 编号后缀比对）。身份口径：uid 键**带来源**（编号只在来源命名空间内有效）；url 键**不带来源**
# （规范页面链接跨来源同指一个实体——手动导入的「用户上传」与官方源候选同 url 就是同一数据集）。
# 归一化与 locate_record 同一真源（corpus._cmp_key_cached：NFC + 去零宽 + casefold），url 再
# rstrip('/') 吸收尾斜杠差异。uid/url 皆缺的记录不参与去重（无法核验 ≠ 重复，宁可保留不错杀）。
# ==============================================================================================

def _record_identity_keys(record: dict, fallback_source: str = "") -> set[str]:
    """单条记录的实体身份键集合。返回空集 = 这条记录无法核验身份，不参与去重。"""
    rec = record if isinstance(record, dict) else {}
    src = _cmp_key_cached(str(rec.get("source") or "") or fallback_source)
    uid = _cmp_key_cached(str(rec.get("dataset_uid") or ""))
    url = _cmp_key_cached(str(rec.get("url") or "")).rstrip("/")
    keys: set[str] = set()
    if uid:
        keys.add(f"u|{src}|{uid}")
    if url:
        keys.add(f"l|{url}")
    return keys


def _external_identity_index(project_root: Path) -> tuple[set[str], list[str]]:
    """一趟扫 external 全部 .json（官方快照 + upload_*），收集实体身份键
    （与 _scan_external_files 同一趟扫哲学）。

    返回 (身份键集合, 解析失败被跳过的文件名清单)。坏文件口径与 `_external_dataset_uids`
    对齐（同族遗留）：跳过不炸，但坏文件名必须随结果带回、
    由调用方如实呈现——撞重的那条恰好损坏时去重闸对它是失明的，不许静默。"""
    index: set[str] = set()
    skipped: list[str] = []
    ext_dir = _external_dir(project_root)
    if not ext_dir.is_dir():
        return _scope_union_identity_index(index, skipped, project_root)
    for p in sorted(ext_dir.glob("*.json")):
        if not p.is_file():
            continue
        try:
            existing = _load_file_records(p)
        except CurateError:
            skipped.append(p.name)
            continue
        for record in existing:
            index |= _record_identity_keys(record)
    return _scope_union_identity_index(index, skipped, project_root)


def _scope_union_identity_index(index: set[str], skipped: list[str], project_root: Path) -> tuple[set[str], list[str]]:
    """任务 3：绑定补丁作用域时把本人补丁 adds 的身份键并入去重比对集
    （共享 external 扫描结果之上做并集；未绑定 → 入参原样返回，逐字节不变）。
    补丁损坏 → load_patch 抛 PatchError（fail-closed），写操作如实失败。"""
    scope = _patch_scope()
    if not scope:
        return index, skipped
    from .patch_package import load_patch

    patch = load_patch(Path(project_root), scope)
    for record in patch["adds"]:
        index |= _record_identity_keys(record)
    return index, skipped


def _dedup_skipped_note(skipped_files: list[str]) -> str:
    """去重扫描遇坏文件的如实提示：坏文件里的记录没参与撞重比对（去重闸对它们失明）。"""
    return (f"注意：外部库里有 {len(skipped_files)} 个文件损坏无法解析"
            f"（{'、'.join(skipped_files)}），本次去重比对没有覆盖它们——若撞重条目恰在坏文件里，"
            "可能被当成新候选；修复或移走这些文件后再操作即可。")


def _split_existing_candidates(records: list[dict], project_root: Path,
                               fallback_source: str) -> tuple[list[dict], list[dict], list[str]]:
    """候选按实体身份分成（新候选, 已在库中, 去重扫描跳过的坏文件名）。无身份键的一律按新候选保留。"""
    index, skipped_files = _external_identity_index(project_root)
    fresh: list[dict] = []
    existing: list[dict] = []
    for r in records:
        keys = _record_identity_keys(r, fallback_source)
        if keys and keys & index:
            existing.append(r)
        else:
            fresh.append(r)
    return fresh, existing, skipped_files


def _skipped_existing_projection(records: list[dict], cap: int = 10) -> list[dict]:
    """skipped_existing 的轻量投影（uid + 名称），供 plan/apply 如实回显。"""
    return [{"dataset_uid": str(r.get("dataset_uid") or ""),
             "dataset_name": str(r.get("dataset_name") or "")} for r in records[:cap]]


# ==============================================================================================
# curate.search_online：联网搜索官方源 → 候选 preview（不落盘）→ 确认入库
# ==============================================================================================

def _validate_limit(limit: Any) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        raise CurateError("bad_param", f"limit 只能是 1–100 的整数：{limit!r}。") from None
    if not 1 <= value <= 100:
        raise CurateError("bad_param", f"limit 只能是 1–100 的整数：{limit!r}。")
    return value


def _search_preimage(source_key: str, query: str, species: str, records: list[dict]) -> dict:
    return {
        "action": "curate.search_online",
        "source": source_key,
        "query": query,
        "species": species,
        "record_count": len(records),
        "records_digest": records_content_digest(records),
    }


def plan_search_online(
    query: Any,
    source: Any = "arrayexpress",
    species: Any = None,
    limit: Any = 20,
    *,
    project_root: Path,
) -> dict:
    """curate.search_online 第一步：**真联网**查询官方源 → 候选 preview（**不落盘**，只写请求账本）。

    候选 records 随 plan 返回（`candidates` 键）供 apply 原样回传；preview 展示条数/来源/样本标题。
    实体级去重：已在库中的候选（同来源同编号/同页面链接）过滤并回显 `skipped_existing`——
    零新候选不报错（candidates 可为空，apply 走零写入诚实回报）。
    未注册源 → source_not_registered；网络失败 → network_error；在线零候选 → no_candidates。"""
    query_s = str(query or "").strip()
    if not query_s:
        raise CurateError("bad_param", "搜索关键词不能为空。")
    limit_n = _validate_limit(limit)
    species_s = str(species or "").strip()
    source_key = corpus_net.resolve_source_key(source, valid_keys=SOURCE_ADAPTERS)
    adapter = SOURCE_ADAPTERS.get(source_key) if source_key else None
    if adapter is None:
        raise CurateError(
            "source_not_registered",
            f"暂不支持联网搜索来源「{source}」。"
            f"目前能联网搜索的来源：{'、'.join(str(a['label']) for a in SOURCE_ADAPTERS.values())}；"
            "其它来源（ENCODE、EBI SCEA 等）的联网搜索会在后续版本接入。",
        )
    search: Callable[..., tuple[list[dict], list[str]]] = adapter["search"]
    records, warnings = search(query_s, species=species_s, limit=limit_n, project_root=Path(project_root))
    if not records:
        raise CurateError(
            "no_candidates",
            f"{adapter['label']} 查询 {query_s!r}（物种：{species_s or '不限'}）没有可用候选。"
            "未写入任何内容；可换关键词重试。",
        )
    # 实体级去重：已在库中的候选（同来源同编号、或同页面链接）诚实过滤并如实回显——
    # 不重复入库。token 只覆盖**新候选**（apply 落盘前按届时库态同口径重检，防 TOCTOU）。
    # 零新候选不是错误：candidates=[] + skipped_existing 全量回显，apply 走零写入诚实回报。
    fresh, existing, dedup_skipped = _split_existing_candidates(records, Path(project_root), adapter["label"])
    if existing:
        warnings = list(warnings) + [
            f"{len(existing)} 条候选已在库中（同编号或同链接），已自动跳过、不重复入库。"]
    if dedup_skipped:
        warnings = list(warnings) + [_dedup_skipped_note(dedup_skipped)]
    token = make_confirm_token(_search_preimage(source_key, query_s, species_s, fresh))
    return {
        "action": "curate.search_online",
        "dry_run": True,
        "source": source_key,
        "source_label": adapter["label"],
        "query": query_s,
        "species": species_s,
        "limit": limit_n,
        "record_count": len(fresh),
        "sample_titles": [str(r.get("dataset_name") or "") for r in fresh[:5]],
        "warnings": warnings,
        "candidates": fresh,  # 候选只进内存随 plan 返回，不落盘；apply 原样回传
        "skipped_existing": _skipped_existing_projection(existing),
        "skipped_existing_count": len(existing),
        "confirm_token": token,
    }


def apply_search_online(plan_result: dict, *, confirm_token: Any, project_root: Path) -> dict:
    """curate.search_online 第二步：token 比对 → 实体级重检→ 候选经 uploads.ingest_dataset 管线入库。

    token 由 plan_result 的（动作参数 + 候选内容指纹）重算：候选被调包/改动 → token_mismatch，零写入。
    实体级重检：plan→apply 之间同一实体可能已被其它通路入库——按届时库态再过滤一遍；
    全部撞重则零写入并如实回报。重检与落盘同住 `uploads.ingest_critical_section`
    （跨进程 OS 文件锁），跨进程微竞态结构性闭合。
    来源标签规范化为官方源名（适配器 label 作兜底 form_source；记录自带 source 优先保留）。"""
    if not isinstance(plan_result, dict):
        raise CurateError("bad_param", "plan_result 必须是 plan_search_online 返回的字典。")
    candidates = plan_result.get("candidates")
    # 零新候选（全部已在库中）是 plan 的合法产出——仅当 plan 带 skipped_existing 标记时放行
    # 否则空 candidates 仍是 bad_param（旧契约逐位不变）。
    if not isinstance(candidates, list) \
            or (not candidates and not plan_result.get("skipped_existing")) \
            or not all(isinstance(r, dict) for r in candidates):
        raise CurateError("bad_param", "plan_result 缺少候选记录（candidates）；请重新 plan。")
    source_key = str(plan_result.get("source") or "").strip().lower()
    adapter = SOURCE_ADAPTERS.get(source_key)
    if adapter is None:
        raise CurateError("source_not_registered", f"来源 {source_key!r} 未注册，拒绝入库。")
    expected = make_confirm_token(_search_preimage(
        source_key,
        str(plan_result.get("query") or ""),
        str(plan_result.get("species") or ""),
        candidates,
    ))
    _check_token(expected, confirm_token)
    # 重检 → 落盘全程住进摄取临界区（跨进程 OS 文件锁）——重检与落盘之间
    # 不再有任何通路能插队（残留的跨进程微竞态在此收口）。
    with ingest_critical_section(Path(project_root)):
        fresh, existing_now, dedup_skipped = _split_existing_candidates(
            candidates, Path(project_root), adapter["label"])
        #  skipped 全口径 = plan 侧已过滤的 + apply 侧重检新发现的（candidates 为空时撞重全发生在
        #  plan 侧——不把 plan 的 skipped_existing 并进来，零写入回报就会漏报成「0 条撞重」）。
        plan_skipped = [x for x in (plan_result.get("skipped_existing") or []) if isinstance(x, dict)]
        plan_skipped_count = int(plan_result.get("skipped_existing_count") or len(plan_skipped) or 0)
        total_skipped = plan_skipped_count + len(existing_now)
        skipped_shown = (plan_skipped + _skipped_existing_projection(existing_now))[:10]
        dedup_notes = [_dedup_skipped_note(dedup_skipped)] if dedup_skipped else []
        if not fresh:
            return {
                "action": "curate.search_online",
                "dry_run": False,
                "source": source_key,
                "source_label": adapter["label"],
                "query": str(plan_result.get("query") or ""),
                "filename": None,
                "saved_to": None,
                "record_count": 0,
                "sources": {},
                "warnings": [f"候选共 {len(candidates) + plan_skipped_count} 条全部已在库中（同编号或同链接），未重复入库。"] + dedup_notes,
                "skipped_existing": skipped_shown,
                "skipped_existing_count": total_skipped,
            }
        raw = json.dumps(
            {"source": adapter["label"], "note": CURATE_SEARCH_NOTE, "records": fresh},
            ensure_ascii=False,
        ).encode("utf-8")
        res = ingest_dataset(
            raw_bytes=raw,
            safe_name=new_upload_name(f"curate_{source_key}.json"),
            project_root=Path(project_root),
            form_source=adapter["label"],
            note=CURATE_SEARCH_NOTE,
        )
    warnings = list(res.warnings)
    if existing_now:
        warnings.append(f"{len(existing_now)} 条候选在确认前已入库（同编号或同链接），本次未重复写入。")
    warnings.extend(dedup_notes)
    return {
        "action": "curate.search_online",
        "dry_run": False,
        "source": source_key,
        "source_label": adapter["label"],
        "query": str(plan_result.get("query") or ""),
        "filename": res.filename,
        "saved_to": res.saved_to,
        "record_count": res.record_count,
        "sources": res.sources,
        "warnings": warnings,
        "skipped_existing": skipped_shown,
        "skipped_existing_count": total_skipped,
    }


# ==============================================================================================
# curate.remove / curate.restore：回收站式可逆删除（移动而非删除）
# ==============================================================================================

# ---- 任务 3 补丁作用域分支：登录账户的删除/恢复改打本人补丁包 ----------------
#
# 语义映射：
# - 删除本人补丁新增（adds）→ 移入补丁 trash（可逆，对应 legacy 的回收站）；
# - 删除基线/共享库记录 → **屏蔽**（blocks，仅本人视图不可见，共享内容零改动）——
#   这正是「补丁包支持新增与屏蔽」的屏蔽半；
# - 恢复：trash → adds；blocks → 解除屏蔽。
# filename 入参的补丁形态："patch:<uid>"（list 视图的合成名）或裸 dataset_uid。

def _list_patch_curations(root: Path, account_id: str) -> dict:
    """curate.list 的补丁形态：keys 与 legacy 尽量同形（前端管理面可直接渲染）。"""
    from . import patch_package as pp

    info = pp.summarize_patch(root, account_id)
    files: list[dict] = []
    for r in info["adds"]:
        uid = pp.record_uid(r)
        src = str(r.get("source") or "").strip() or "（未标注）"
        files.append({
            "filename": f"patch:{uid}" if uid else "patch:（无编号）",
            "size_bytes": None,
            "modified_at": "",
            "is_upload": True,
            "curatable": True,
            "record_count": 1,
            "sources": {src: 1},
            "dataset_uid": uid,
            "dataset_name": str(r.get("dataset_name") or ""),
        })
    recycle = [
        {"recycle_name": f"patch:{uid}", "original_filename": f"patch:{uid}",
         "size_bytes": None, "record_count": 1}
        for uid in info["trash_uids"]
    ]
    return {
        "action": "curate.list",
        "mode": "patch",
        "external_dir": EXTERNAL_DIR_NAME,
        "file_count": len(files),
        "files": files,
        "recycle_dir": "patch:trash",
        "recycle_count": len(recycle),
        "recycle": recycle,
        "blocks": list(info["blocks"]),
        "patch": {
            "add_count": len(info["adds"]),
            "block_count": len(info["blocks"]),
            "trash_count": info["trash_count"],
            "max_adds": pp.MAX_PATCH_ADD_RECORDS,
        },
    }


def _patch_uid_arg(filename: Any) -> str:
    """remove/restore 的 filename 入参 → uid："patch:<uid>" 合成名或裸 uid。"""
    name = str(filename or "").strip()
    if name.lower().startswith("patch:"):
        name = name[len("patch:"):].strip()
    return name


def _patch_remove_preimage(account_id: str, uid: str, kind: str, gen: tuple) -> dict:
    return {"action": "curate.remove", "scope": account_id, "uid": uid, "kind": kind, "gen": gen}


def _patch_plan_remove(filename: Any, *, project_root: Path, account_id: str) -> dict:
    """补丁形态 remove 第一步：解析 uid → 判定 trash（本人新增）或 block（共享层）→ preview + token。"""
    from . import patch_package as pp

    uid = _patch_uid_arg(filename)
    if not uid:
        raise CurateError("bad_param", "请给出要删除/屏蔽记录的数据集编号（dataset_uid）。")
    patch = pp.load_patch(project_root, account_id)
    own = [r for r in patch["adds"] if pp.record_uid(r) == uid]
    if own:
        kind = "trash"
        rec = own[0]
        name = str(rec.get("dataset_name") or "")
        src = str(rec.get("source") or "")
        effect = "从你的补丁包删除（先入回收区，可恢复）。"
    else:
        if uid in {str(u) for u in patch["blocks"]}:
            raise CurateError("bad_param", f"编号 {uid} 已在你的屏蔽列表里，无需重复屏蔽。")
        visible = pp.effective_visible_uids(project_root, account_id)
        if uid not in visible:
            raise CurateError(
                "unknown_file",
                f"你的语料视图中没有编号为 {uid} 的记录（补丁形态下只能删除自己的新增，"
                "或屏蔽你可见的基线/共享库记录）。",
            )
        kind = "block"
        name, src = "", ""
        from .corpus import load_full_corpus  # 惰性；绑定上下文内即本人视图
        for r in load_full_corpus(project_root / "database" / "base", project_root):
            raw = r.raw if isinstance(getattr(r, "raw", None), dict) else {}
            if pp.record_uid(raw) == uid:
                name = r.dataset_name
                src = str(raw.get("source") or "")
                break
        effect = "从你的视图屏蔽（只影响你自己的视图，共享基线不动；之后可恢复）。"
    token = make_confirm_token(_patch_remove_preimage(
        account_id, uid, kind, pp.patch_generation(project_root, account_id)))
    return {
        "action": "curate.remove",
        "mode": "patch",
        "dry_run": True,
        "filename": f"patch:{uid}",
        "dataset_uid": uid,
        "dataset_name": name,
        "record_count": 1,
        "sources": {src: 1} if src else {},
        "modified_at": "",
        "effect": effect,
        "confirm_token": token,
        "_kind": kind,
    }


def _patch_apply_remove(filename: Any, *, confirm_token: Any, project_root: Path) -> dict:
    """补丁形态 remove 第二步：重算 plan 比 token（补丁任何写入都会使 gen 变化 → 旧 token 失效）。"""
    from . import patch_package as pp

    account_id = _patch_scope() or ""
    preview = _patch_plan_remove(filename, project_root=project_root, account_id=account_id)
    _check_token(preview["confirm_token"], confirm_token)
    uid = preview["dataset_uid"]
    if preview["_kind"] == "trash":
        res = pp.trash_adds(project_root, account_id, [uid])
        if not res["moved"]:
            raise CurateError("unknown_file", f"补丁包新增里没有编号 {uid}（可能已被删除）。")
        return {
            "action": "curate.remove", "mode": "patch", "dry_run": False,
            "filename": f"patch:{uid}", "dataset_uid": uid,
            "moved_to": "patch:trash", "record_count": 1, "restorable": True,
        }
    res = pp.block_uids(project_root, account_id, [uid])
    if not res["blocked"]:
        raise CurateError("bad_param", f"编号 {uid} 已在屏蔽列表或属于你的新增，未重复屏蔽。")
    return {
        "action": "curate.remove", "mode": "patch", "dry_run": False,
        "filename": f"patch:{uid}", "dataset_uid": uid,
        "moved_to": "patch:blocks", "record_count": 1, "restorable": True,
    }


def _patch_restore_preimage(account_id: str, uid: str, kind: str, gen: tuple) -> dict:
    return {"action": "curate.restore", "scope": account_id, "uid": uid, "kind": kind, "gen": gen}


def _patch_plan_restore(recycle_name: Any, *, project_root: Path, account_id: str) -> dict:
    """补丁形态 restore 第一步：uid 在 trash → 放回 adds；uid 在 blocks → 解除屏蔽。"""
    from . import patch_package as pp

    uid = _patch_uid_arg(recycle_name)
    if not uid:
        raise CurateError("bad_param", "请给出要恢复记录的数据集编号（dataset_uid）。")
    patch = pp.load_patch(project_root, account_id)
    trash_uids = {pp.record_uid(r) for r in patch["trash"] if pp.record_uid(r)}
    if uid in trash_uids:
        kind = "untrash"
        effect = "从回收区放回你的补丁包。"
    elif uid in {str(u) for u in patch["blocks"]}:
        kind = "unblock"
        effect = "解除屏蔽（该记录重新出现在你的视图中）。"
    else:
        raise CurateError(
            "unknown_file",
            f"编号 {uid} 既不在你的补丁回收区也不在屏蔽列表里，没有可恢复的项。",
        )
    token = make_confirm_token(_patch_restore_preimage(
        account_id, uid, kind, pp.patch_generation(project_root, account_id)))
    return {
        "action": "curate.restore",
        "mode": "patch",
        "dry_run": True,
        "recycle_name": f"patch:{uid}",
        "dataset_uid": uid,
        "target_filename": f"patch:{uid}",
        "record_count": 1,
        "will_conflict": False,
        "effect": effect,
        "confirm_token": token,
        "_kind": kind,
    }


def _patch_apply_restore(recycle_name: Any, *, confirm_token: Any, project_root: Path) -> dict:
    from . import patch_package as pp

    account_id = _patch_scope() or ""
    preview = _patch_plan_restore(recycle_name, project_root=project_root, account_id=account_id)
    _check_token(preview["confirm_token"], confirm_token)
    uid = preview["dataset_uid"]
    if preview["_kind"] == "untrash":
        res = pp.restore_adds(project_root, account_id, [uid])
        if not res["restored"]:
            raise CurateError("unknown_file", f"补丁回收区里没有编号 {uid}（或它已在新版本中）。")
    else:
        res = pp.unblock_uids(project_root, account_id, [uid])
        if not res["unblocked"]:
            raise CurateError("unknown_file", f"屏蔽列表里没有编号 {uid}。")
    return {
        "action": "curate.restore", "mode": "patch", "dry_run": False,
        "recycle_name": f"patch:{uid}", "dataset_uid": uid,
        "restored_to": "我的补丁包（仅本账户可见）", "record_count": 1,
    }


def _curatable_external_file(filename: Any, project_root: Path) -> tuple[str, Path]:
    """remove 目标三重闸：叶子文件名（base 结构性不可达）→ 存在（unknown_file）→ upload_* 命名空间
    （not_curatable，官方快照不走对话式管护）。"""
    name = _leaf_name(filename)
    target = _external_dir(project_root) / name
    if not target.is_file():
        raise CurateError("unknown_file", f"外部库里没有文件「{name}」。")
    if not name.startswith(CURATABLE_PREFIX):
        raise CurateError(
            "not_curatable",
            f"「{name}」不是可管护的上传文件：对话式删除只覆盖你自己上传或联网搜来的文件；"
            "官方数据快照不能在这里删改。",
        )
    return name, target


def _remove_preimage(name: str, records: list[dict]) -> dict:
    return {
        "action": "curate.remove",
        "filename": name,
        "record_count": len(records),
        "records_digest": records_content_digest(records),
    }


def plan_remove(filename: Any, *, project_root: Path) -> dict:
    """curate.remove 第一步：preview（文件条数/来源/修改时间）+ token。零副作用。
    绑定补丁作用域时走补丁形态（删除本人新增 / 屏蔽共享层记录）。"""
    root = Path(project_root)
    scope = _patch_scope()
    if scope:
        return _patch_plan_remove(filename, project_root=root, account_id=scope)
    name, target = _curatable_external_file(filename, root)
    records = _load_file_records(target)
    token = make_confirm_token(_remove_preimage(name, records))
    info = _file_info(target, project_root=root)
    return {
        "action": "curate.remove",
        "dry_run": True,
        "filename": name,
        "record_count": len(records),
        "sources": info.get("sources", {}),
        "modified_at": info.get("modified_at", ""),
        "effect": "移入回收站（可逆），不是真删除；之后可以从回收站恢复。",
        "confirm_token": token,
    }


def apply_remove(filename: Any, *, confirm_token: Any, project_root: Path) -> dict:
    """curate.remove 第二步：token 比对 → **移动**到 `.userdata/recycle/<timestamp>_<filename>`
    + 追加 manifest.jsonl + invalidate_external_cache()（即时不可见）。
    绑定补丁作用域时走补丁形态（trash / block）。"""
    root = Path(project_root)
    scope = _patch_scope()
    if scope:
        return _patch_apply_remove(filename, confirm_token=confirm_token, project_root=root)
    name, target = _curatable_external_file(filename, root)
    records = _load_file_records(target)  # 重算内容指纹：plan→apply 之间文件被改 → token_mismatch
    _check_token(make_confirm_token(_remove_preimage(name, records)), confirm_token)

    rec_dir = _recycle_dir(root)
    rec_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    dest = rec_dir / f"{stamp}_{name}"
    while dest.exists():  # 同微秒防冲突：绝不覆盖回收站里已有文件
        dest = rec_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{name}"
    shutil.move(str(target), str(dest))
    _append_jsonl(_recycle_manifest(root), {
        "ts": _now_iso(),
        "action": "remove",
        "original_path": f"{EXTERNAL_DIR_NAME}/{name}",
        "recycle_name": dest.name,
        "record_count": len(records),
    })
    invalidate_external_cache()
    return {
        "action": "curate.remove",
        "dry_run": False,
        "filename": name,
        "moved_to": f"{USERDATA_DIR_NAME}/{RECYCLE_DIR_NAME}/{dest.name}",
        "record_count": len(records),
        "restorable": True,
    }


def _restore_preimage(recycle_name: str, records: list[dict]) -> dict:
    return {
        "action": "curate.restore",
        "recycle_name": recycle_name,
        "record_count": len(records),
        "records_digest": records_content_digest(records),
    }


def _recycle_file(recycle_name: Any, project_root: Path) -> tuple[str, Path]:
    name = _leaf_name(recycle_name)
    target = _recycle_dir(project_root) / name
    if not target.is_file():
        raise CurateError("unknown_file", f"回收站中不存在文件 {name}。")
    return name, target


def plan_restore(recycle_name: Any, *, project_root: Path) -> dict:
    """curate.restore 第一步：preview（将移回的原始文件名/条数/是否冲突）+ token。零副作用。
    绑定补丁作用域时走补丁形态（回收区放回 / 解除屏蔽）。"""
    root = Path(project_root)
    scope = _patch_scope()
    if scope:
        return _patch_plan_restore(recycle_name, project_root=root, account_id=scope)
    name, target = _recycle_file(recycle_name, root)
    m = _RECYCLE_STAMP_RE.match(name)
    original = m.group("orig") if m else name
    records = _load_file_records(target)
    conflict = (_external_dir(root) / original).exists()
    token = make_confirm_token(_restore_preimage(name, records))
    return {
        "action": "curate.restore",
        "dry_run": True,
        "recycle_name": name,
        "target_filename": original,
        "record_count": len(records),
        "will_conflict": conflict,
        "confirm_token": token,
    }


def apply_restore(recycle_name: Any, *, confirm_token: Any, project_root: Path) -> dict:
    """curate.restore 第二步：token 比对 → 从回收站移回 external 原文件名 + manifest + 缓存失效。
    绑定补丁作用域时走补丁形态（untrash / unblock）。"""
    root = Path(project_root)
    scope = _patch_scope()
    if scope:
        return _patch_apply_restore(recycle_name, confirm_token=confirm_token, project_root=root)
    name, target = _recycle_file(recycle_name, root)
    m = _RECYCLE_STAMP_RE.match(name)
    original = m.group("orig") if m else name
    records = _load_file_records(target)
    _check_token(make_confirm_token(_restore_preimage(name, records)), confirm_token)

    dest = _external_dir(root) / original
    if dest.exists():
        raise CurateError(
            "bad_param",
            f"外部库已存在同名文件「{original}」，为避免覆盖没有恢复；请先处理该文件。",
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(dest))
    _append_jsonl(_recycle_manifest(root), {
        "ts": _now_iso(),
        "action": "restore",
        "recycle_name": name,
        "restored_to": f"{EXTERNAL_DIR_NAME}/{original}",
        "record_count": len(records),
    })
    invalidate_external_cache()
    return {
        "action": "curate.restore",
        "dry_run": False,
        "recycle_name": name,
        "restored_to": f"{EXTERNAL_DIR_NAME}/{original}",
        "record_count": len(records),
    }


# ==============================================================================================
# curate.check_updates：检查来源更新（2026-08-03 新能力）
# 设计蓝本：设计文档 §1.3/§2.3。**只读、不落盘、不抛**——
# 它不是 plan/apply 两步确认的写动作，故不走 run_curate_action 分发（ACTIONS 不变）。
# ==============================================================================================

#: 可检查的来源注册表：本地快照文件（相对 project_root）+ 官网核对入口。
#: `online=True` 的是配了在线通道的源——**不伪造能力**：离线快照源
#: 如实报告本地快照条数/日期与官网入口，并指路「联网搜…」。
#: 在线通道两支（2026-08-03 扩）：arrayexpress 走 `_fill_online_check`（既有 BioStudies 支，
#: 网络失败保留 mode="online" + note）；encode/10x/hca/geo/zenodo 走 `_fill_online_check_net`（corpus_net
#: 工具组，拉不到/响应形状变了 → 如实降级 mode="snapshot" + note，mode 仍是 online/snapshot 二值）。
#: 10x 指向 `database/base/` 的冻结基准文件——这里**只读**它做条数统计，写入依旧结构性不可达。
CHECK_UPDATE_SOURCES: dict[str, dict[str, Any]] = {
    "arrayexpress": {
        "label": AE_SOURCE_LABEL,
        "file": f"{EXTERNAL_DIR_NAME}/arrayexpress.json",
        "site_url": "https://www.ebi.ac.uk/biostudies/arrayexpress",
        "online": True,
    },
    "cellxgene": {
        "label": SOURCE_CELLXGENE,
        "file": f"{EXTERNAL_DIR_NAME}/cellxgene.json",
        "site_url": "https://cellxgene.cziscience.com/",
        "online": False,
    },
    "ebi_scea": {
        "label": SOURCE_SCEA,
        "file": f"{EXTERNAL_DIR_NAME}/ebi_scea.json",
        "site_url": "https://www.ebi.ac.uk/gxa/sc/",
        "online": False,
    },
    "encode": {
        "label": SOURCE_ENCODE,
        "file": f"{EXTERNAL_DIR_NAME}/encode.json",
        "site_url": "https://www.encodeproject.org/",
        "online": True,
        "net_kind": "encode",
    },
    "hca": {
        "label": SOURCE_HCA,
        "file": f"{EXTERNAL_DIR_NAME}/hca.json",
        "site_url": "https://data.humancellatlas.org/",
        "online": True,
        "net_kind": "hca",
    },
    # 2026-08-06 三源接入：hubmap/scp 仍为离线快照（无在线比对通道，不伪造能力）；
    # geo 于 2026-08-07 接入 NCBI E-utilities 在线通道（esearch pdat 窗口 + esummary 富化）。
    "hubmap": {
        "label": SOURCE_HUBMAP,
        "file": f"{EXTERNAL_DIR_NAME}/hubmap.json",
        "site_url": "https://portal.hubmapconsortium.org/",
        "online": False,
    },
    "scp": {
        "label": SOURCE_SCP,
        "file": f"{EXTERNAL_DIR_NAME}/single_cell_portal.json",
        "site_url": "https://singlecell.broadinstitute.org/single_cell",
        "online": False,
    },
    "geo": {
        "label": SOURCE_GEO,
        "file": f"{EXTERNAL_DIR_NAME}/geo.json",
        "site_url": "https://www.ncbi.nlm.nih.gov/geo/",
        "online": True,
        "net_kind": "geo",
    },
    "10x": {
        "label": SOURCE_10X,
        "file": "database/base/10x-Visium.json",
        "site_url": "https://www.10xgenomics.com/datasets",
        "online": True,
        "net_kind": "tenx",
    },
    # 2026-08-08 接入 zenodo（第 10 源）：type=dataset&sort=mostrecent 最近页 vs 本地快照
    # record id 水位线差分；通用开放仓储，比对口径是全领域 dataset（note 如实写明）。
    "zenodo": {
        "label": ZENODO_SOURCE_LABEL,
        "file": f"{EXTERNAL_DIR_NAME}/zenodo.json",
        "site_url": "https://zenodo.org/",
        "online": True,
        "net_kind": "zenodo",
    },
    # 2026-08-14 接入 refinebio（第 11 源）：/v1/search/ ordering=-source_first_published
    # 最近页 vs 本地快照 accession 水位线差分；GEO/SRA/AE 镜像，比对口径是全库最新
    # （不限单细胞切片主题，note 如实写明）。
    "refinebio": {
        "label": REFINEBIO_SOURCE_LABEL,
        "file": f"{EXTERNAL_DIR_NAME}/refinebio.json",
        "site_url": "https://www.refine.bio/",
        "online": True,
        "net_kind": "refinebio",
    },
}

_AE_RECENT_LIMIT = 10      # 在线比对拉取的「最近条目」条数（一页，限速纪律不变）
_NEW_CANDIDATES_SHOW = 20  # new_candidates 最多回传几条（accession + title）。
                           # 2026-08-15 G-01 修复：必须 ≥ sync_updates 的 max_import clamp 上限
                           # （[1,20]），否则供给端会卡死每源入库上限——此前 =5 时
                           # _SYNC_MAX_IMPORT=10 永远吃不满，「5→10 放松批」一半是死代码。
_AE_ACCESSION_RE = re.compile(r"E-[A-Z]+-[0-9]+")


def _snapshot_local_info(path: Path, _err: list[str] | None = None) -> tuple[int, str | None]:
    """本地快照的 (条数, 快照日期)。解析失败/文件缺失 → (0, None)，绝不抛。

    快照日期**只认文件里显式声明的元信息**（snapshot_date/retrieved_at/generated_at/collected_at）；
    找不到 → None，由调用方在 note_zh 如实说明——不用文件 mtime 冒充快照日期
    （git 检出/复制会改写 mtime，那是「文件什么时候落盘的」，不是「快照什么时候采的」）。
    G-27：损坏 ≠ 空库——解析失败经 _err 出参如实带出（调用方写进 note），
    不再与「快照真的 0 条」同形（与 D5 各读取函数的 _err 出参同 pattern）。"""
    if not path.is_file():
        return 0, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        if _err is not None:
            _err.append(f"{path.name}：{type(exc).__name__}")
        return 0, None
    records = [r for r in extract_records(payload) if isinstance(r, dict)]
    snapshot_date: str | None = None
    if isinstance(payload, dict):
        for key in ("snapshot_date", "retrieved_at", "generated_at", "collected_at"):
            value = str(payload.get(key) or "").strip()
            if value:
                snapshot_date = value
                break
    return len(records), snapshot_date


def _snapshot_corrupt_note(errs: list[str]) -> str:
    """D5：快照损坏 ≠ 空库——diff 是把损坏快照当空集比出来的，新增数字可能虚报，
    必须让用户可见。无损坏 → ""（不污染 note）。"""
    if not errs:
        return ""
    return (f"注意：本地快照文件损坏无法解析（{errs[0]}），上面的新增数字是把快照当空库比出来的，"
            "可能虚报——修复或移走该快照文件后再比对一次。")


def _local_keys(
    path: Path,
    *,
    uid_prefix: str,
    accession_re: "re.Pattern[str]",
    fields: tuple[str, ...],
    upper: bool,
) -> tuple[set[str], list[str]]:
    """本地快照键提取的唯一骨架：`dataset_uid` 前缀剥除 + 指定字段正则双通道。

    各来源只喂 4 个差异参数：uid 前缀、accession 正则、扫描字段清单、大小写归一
    （upper=True 时 uid 尾段与被扫字段先归一为大写）。损坏 ≠ 不存在纪律随骨架生效：
    解析失败经 errs 出参如实带出，调用方写进 note，不与「空快照」同形。"""
    if not path.is_file():
        return set(), []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        return set(), [f"{path.name}：{type(exc).__name__}"]
    keys: set[str] = set()
    for r in extract_records(payload):
        if not isinstance(r, dict):
            continue
        uid = str(r.get("dataset_uid") or "")
        if uid.startswith(uid_prefix):
            tail = uid[len(uid_prefix):].strip()
            keys.add(tail.upper() if upper else tail)
        for field in fields:
            text = str(r.get(field) or "")
            m = accession_re.search(text.upper() if upper else text)
            if m:
                keys.add(m.group(0))
    keys.discard("")
    return keys, []


def _ae_local_accessions(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 ArrayExpress 快照里已有的 accession 集合（dataset_uid 的 ae: 前缀 + URL 正则双通道）。

    损坏 ≠ 不存在——解析失败经 _err 出参如实带出（调用方写进 note），不与「空快照」同形。"""
    keys, errs = _local_keys(path, uid_prefix="ae:", accession_re=_AE_ACCESSION_RE,
                             fields=("url", "download_url"), upper=False)
    if _err is not None:
        _err.extend(errs)
    return keys


#: AE 版本监控小钉：BioStudies arrayexpress/search 顶层键的已知快照。
#: 响应冒出快照外新顶层字段（疑似端点版本更新）或消费字段 hits 缺席 → note 附一句提示，
#: **不报错**（最小实现：只提示、不影响比对结果）。
_AE_SEARCH_KNOWN_TOPLEVEL = {"hits", "totalHits"}


def _ae_endpoint_drift_note(payload: Any) -> str:
    """AE 端点版本漂移监控钉：响应顶层键集 vs 已知快照。无漂移 → ""（不污染 note）。"""
    if not isinstance(payload, dict):
        return ""
    keys = set(payload)
    if "hits" not in keys:
        return "另注意：BioStudies 接口这次响应里没有了 hits 字段，疑似端点改版，建议人工核对该接口。"
    extra = sorted(keys - _AE_SEARCH_KNOWN_TOPLEVEL)
    if extra:
        return ("另注意：BioStudies 接口响应出现了新顶层字段（" + "、".join(extra)
                + "），疑似端点版本更新，建议人工留意。")
    return ""


def _fill_online_check(entry: dict, spec: dict, local_count: int, path: Path, root: Path) -> None:
    """在线比对（仅有适配器的源）：拉官方源「最近条目」与本地 accession 比对。网络失败不抛，如实写明。"""
    query = "single cell"  # 宽查询：要的是按发布日期排序的「最近条目」，不是主题过滤
    url = (f"{AE_SEARCH_API}?query={urllib.parse.quote(query)}&pageSize={_AE_RECENT_LIMIT}"
           f"&sortBy=release_date&sortOrder=descending")
    try:
        payload = _fetch_logged(url, project_root=root, endpoint=AE_SEARCH_API,
                                query="check_updates:recent")
    except CurateError as exc:
        # G-14：与 net 支统一降级语义——在线比对没完成就如实降级 mode="snapshot"，
        # 不再保留 mode="online"（同一语义两种 mode，下游按 mode 判断时行为不一）。
        _degrade_to_snapshot(entry, spec, local_count, path, exc.hint)
        return
    drift_zh = _ae_endpoint_drift_note(payload)  # AE 版本监控小钉：漂移只提示，不报错
    hits = payload.get("hits") if isinstance(payload, dict) else None
    hits = [h for h in (hits or []) if isinstance(h, dict)]
    recent: list[dict] = []
    for h in hits:
        acc = str(h.get("accession") or "").strip()
        if acc:
            recent.append({"accession": acc, "title": str(h.get("title") or "").strip()})
    local_errs: list[str] = []
    local_accs = _ae_local_accessions(path, _err=local_errs)
    new = [r for r in recent if r["accession"] not in local_accs]
    if new:
        note = (f"已在线比对：官方源按发布日期最近的 {len(recent)} 条里，{len(new)} 条目录里还没有"
                f"（目录共收录 {local_count} 条）。要把某条入库可以说「联网搜…」。")
    else:
        note = f"已在线比对：官方源最近 {len(recent)} 条目录里都有了（目录共收录 {local_count} 条）。"
    entry.update({
        "online_recent": len(recent),
        "new_candidates": new[:_NEW_CANDIDATES_SHOW],
        "new_count": len(new),
        "note_zh": _snapshot_corrupt_note(local_errs) + note + drift_zh,
    })


# ---- 2026-08-03：encode/10x 在线比对（走 corpus_net 工具组；失败如实降级 snapshot）----
# ---- 2026-08-08：hca 同通道接入（Azul 最近条目 vs 本地快照键差分）--------------------------
# ---- 2026-08-07：geo 同通道接入（E-utilities pdat 窗口最近条目 vs 本地 GSE 编号差分）--------
# ---- 2026-08-08：zenodo 同通道接入（type=dataset&sort=mostrecent 最近页 vs 本地 record id 差分）
_ENCODE_ACCESSION_RE = re.compile(r"ENCSR[0-9A-Z]+")


def _encode_local_accessions(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 ENCODE 快照里已有的 accession 集合（dataset_uid 的 encode: 前缀 + URL 正则双通道）。
    损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    keys, errs = _local_keys(path, uid_prefix="encode:", accession_re=_ENCODE_ACCESSION_RE,
                             fields=("url", "download_url", "public_accession"), upper=False)
    if _err is not None:
        _err.extend(errs)
    return keys


def _hca_local_keys(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 HCA 快照里已有的项目 uuid 集合（dataset_uid 的 hca: 前缀 + URL 末段双通道，小写）。
    D5：损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        if _err is not None:
            _err.append(f"{path.name}：{type(exc).__name__}")
        return set()
    keys: set[str] = set()
    for r in extract_records(payload):
        if not isinstance(r, dict):
            continue
        uid = str(r.get("dataset_uid") or "")
        if uid.startswith("hca:"):
            keys.add(uid[4:].strip().lower())
        url_tail = str(r.get("url") or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
        if url_tail:
            keys.add(url_tail)
    keys.discard("")
    return keys


def _tenx_local_keys(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 10x 基准里已有的条目键集合（dataset_uid/url 末段 slug + 归一化 dataset_name 双通道）。
    D5：损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        if _err is not None:
            _err.append(f"{path.name}：{type(exc).__name__}")
        return set()
    keys: set[str] = set()
    for r in extract_records(payload):
        if not isinstance(r, dict):
            continue
        uid = str(r.get("dataset_uid") or "").strip().lower()
        if uid:
            keys.add(uid)
        url_slug = str(r.get("url") or "").rstrip("/").rsplit("/", 1)[-1].strip().lower()
        if url_slug:
            keys.add(url_slug)
        name = re.sub(r"\s+", " ", str(r.get("dataset_name") or "").strip().lower())
        if name:
            keys.add(name)
    return keys


def _tenx_item_keys(item: dict) -> set[str]:
    """一条 10x 在线条目的候选键（accession=slug / URL 末段 / 归一化标题），与本地键同口径。"""
    keys = {
        str(item.get("accession") or "").strip().lower(),
        str(item.get("url") or "").rstrip("/").rsplit("/", 1)[-1].strip().lower(),
        re.sub(r"\s+", " ", str(item.get("title") or "").strip().lower()),
    }
    keys.discard("")
    return keys


def _geo_local_accessions(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 GEO 快照里已有的 GSE 编号集合（dataset_uid 的 geo: 前缀 + GSE 正则双通道，大写）。
    损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    keys, errs = _local_keys(path, uid_prefix="geo:", accession_re=_GEO_ACCESSION_RE,
                             fields=("url", "download_url", "public_accession"), upper=True)
    if _err is not None:
        _err.extend(errs)
    return keys


#: Zenodo record id 形态（lookbehind 使 group(0) 即裸 id，与 uid 前缀剥除同口径）。
_ZENODO_URL_ID_RE = re.compile(r"(?<=/records/)\d+")


def _zenodo_local_accessions(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 Zenodo 快照里已有的 record id 集合（dataset_uid 的 zenodo: 前缀 + URL /records/<id> 双通道）。
    损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    keys, errs = _local_keys(path, uid_prefix="zenodo:", accession_re=_ZENODO_URL_ID_RE,
                             fields=("url", "download_url", "public_accession"), upper=False)
    if _err is not None:
        _err.extend(errs)
    return keys


#: refine.bio 主 accession 形态（uid 前缀剥除后的本地键 + 在线条目键同口径，大写）。
_REFINEBIO_ACC_RE = re.compile(r"(?:GSE|SRP|ERP|DRP|PRJNA|PRJEB|PRJDB)\d+|E-[A-Z]{3,5}-\d+")


def _refinebio_local_accessions(path: Path, _err: list[str] | None = None) -> set[str]:
    """本地 refine.bio 快照里已有的 accession 集合（dataset_uid 的 refinebio: 前缀 + 各字段
    accession 正则双通道，大写）。损坏 ≠ 不存在——解析失败经 _err 出参如实带出。"""
    keys, errs = _local_keys(path, uid_prefix="refinebio:", accession_re=_REFINEBIO_ACC_RE,
                             fields=("url", "download_url", "public_accession"), upper=True)
    if _err is not None:
        _err.extend(errs)
    return keys


def _degrade_to_snapshot(entry: dict, spec: dict, local_count: int, path: Path, reason_zh: str) -> None:
    """在线比对拉不到/解析失败 → 如实降级 mode="snapshot"（mode 仍二值），note 写清原因 + 本地事实。

    为什么不保留 mode="online"：encode/10x 的在线通道是页面/搜索抓取，失败时一条在线数据都没看到，
    挂 "online" 会让前端卡片误以为比对已完成——降级 snapshot 才是对「实际做到了什么」的诚实描述。"""
    snapshot_date = None
    local_errs: list[str] = []
    if path.is_file():
        _, snapshot_date = _snapshot_local_info(path, _err=local_errs)
    if not path.is_file():
        note = (f"这次在线比对没能完成（{reason_zh}），改为报告本地副本的情况。"
                f"本地没有找到这个来源的快照文件（{spec['file']}）。"
                f"可到官网 {spec['site_url']} 核对；网络恢复后再说一次即可重试。")
    elif local_errs:
        # G-27：损坏 ≠ 空库——local_count 此时是假象 0，note 不许引用它
        note = (f"这次在线比对没能完成（{reason_zh}），而本地快照文件（{spec['file']}）损坏无法解析"
                f"（{local_errs[0]}），本地情况不可知——修复或移走该文件后重试。"
                f"也可到官网 {spec['site_url']} 核对。")
        entry["snapshot_error"] = local_errs[0]
    else:
        date_zh = f"，快照日期 {snapshot_date}" if snapshot_date else "，快照日期未在文件里标注"
        note = (f"这次在线比对没能完成（{reason_zh}），改为报告本地副本的情况：本地 {local_count} 条{date_zh}。"
                f"可到官网 {spec['site_url']} 核对；网络恢复后再说一次即可重试。")
    entry.update({
        "mode": "snapshot",
        "snapshot_date": snapshot_date,
        "online_recent": None,
        "new_candidates": None,
        "note_zh": note,
    })


def _accession_item_keys(item: dict, case: str = "keep") -> set[str]:
    value = str(item.get("accession") or "").strip()
    if case == "upper":
        value = value.upper()
    elif case == "lower":
        value = value.lower()
    return {value} - {""}


# Online update wiring is data, not an if/elif program. Adding a source requires
# one registry row with four narrow callables; a missing row fails closed below.
_ONLINE_CHECK_NET: dict[str, dict[str, Any]] = {
    "encode": {
        "fetch": lambda root: corpus_net.encode_recent_items(project_root=root, limit=_AE_RECENT_LIMIT),
        "local": _encode_local_accessions,
        "item_keys": _accession_item_keys,
        "scope_zh": "官方源最近",
    },
    "hca": {
        "fetch": lambda root: corpus_net.hca_recent_items(project_root=root, limit=_AE_RECENT_LIMIT),
        "local": _hca_local_keys,
        "item_keys": lambda item: _accession_item_keys(item, "lower"),
        "scope_zh": "HCA 官方源最近",
    },
    "geo": {
        "fetch": lambda root: corpus_net.geo_recent_items(project_root=root, limit=_AE_RECENT_LIMIT),
        "local": _geo_local_accessions,
        "item_keys": lambda item: _accession_item_keys(item, "upper"),
        "scope_zh": "GEO 官方源最近",
    },
    "zenodo": {
        "fetch": lambda root: corpus_net.zenodo_recent_items(project_root=root, limit=_AE_RECENT_LIMIT),
        "local": _zenodo_local_accessions,
        "item_keys": _accession_item_keys,
        "scope_zh": "Zenodo 官方源全领域最新",
    },
    "refinebio": {
        "fetch": lambda root: corpus_net.refinebio_recent_items(project_root=root, limit=_AE_RECENT_LIMIT),
        "local": _refinebio_local_accessions,
        "item_keys": lambda item: _accession_item_keys(item, "upper"),
        "scope_zh": "refine.bio 官方源全库最新",
    },
    "tenx": {
        "fetch": lambda root: corpus_net.tenx_dataset_items(project_root=root),
        "local": _tenx_local_keys,
        "item_keys": _tenx_item_keys,
        "scope_zh": "10x 官网当前清单",
    },
}


def _fill_online_check_net(entry: dict, spec: dict, local_count: int, path: Path, root: Path) -> None:
    """encode/10x/hca/geo/zenodo 在线比对：经 corpus_net 拉最新清单 → 与本地键集合差分 → 如实报新增候选。

    corpus_net 一律返回 {ok, items, ...} 不抛异常；ok=False（网络失败/parse_changed）→ 降级 snapshot。"""
    kind = str(spec.get("net_kind") or "")
    local_errs: list[str] = []  # D5：快照损坏如实带出（损坏 ≠ 空库，否则 diff 虚报新增）
    wiring = _ONLINE_CHECK_NET.get(kind)
    if wiring is None:
        # G-13：未接线的 net_kind（拼错/新增源忘接线）不许静默按 10x 通道比对——
        # 差分结果会全错且无任何提示。如实降级并写明原因（不抛：check_updates 逐源容错契约）。
        _degrade_to_snapshot(entry, spec, local_count, path,
                             f"来源配置里的 net_kind={kind!r} 没有对应的在线比对通道（疑似新增源忘接线）")
        return
    res = wiring["fetch"](root)
    local_keys = wiring["local"](path, _err=local_errs)
    item_keys = wiring["item_keys"]
    if not res.get("ok"):
        _degrade_to_snapshot(entry, spec, local_count, path,
                             str(res.get("note_zh") or res.get("error") or "未知原因"))
        return
    recent: list[dict] = []
    new: list[dict] = []
    for it in res.get("items") or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        keys = item_keys(it)
        acc = str(it.get("accession") or "").strip() or title
        if not keys:
            continue
        cand = {"accession": acc, "title": title}
        recent.append(cand)
        if keys.isdisjoint(local_keys):
            new.append(cand)
    scope_zh = str(wiring["scope_zh"])
    channel_note = ""
    if kind == "zenodo":
        # 通用仓储口径如实标注：比对的是全领域 type=dataset 最新条目（不限生物），
        # 「目录里还没有」不等于「生物领域新增」。
        channel_note = ("注意：Zenodo 是通用开放仓储，这里比对的是其中全领域 type=dataset 的"
                        "最新条目（不限生物领域），新增候选需要人工甄别是否生物相关。")
    if kind == "refinebio":
        # 镜像口径 + 切片口径双重如实标注：比对的是 refine.bio 全库最新（不限单细胞/空间
        # 切片主题），且本地快照本就是人工甄别切片——「目录里还没有」不等于「该收」；
        # 镜像条目与 GEO/AE 在库记录可能指向同一研究，新增候选先按 accession 查重再甄别。
        channel_note = ("注意：refine.bio 是 GEO/SRA/ArrayExpress 的统一加工镜像，这里比对的是"
                        "其全库最新条目（不限单细胞/空间切片主题）；本地快照是人工甄别切片，"
                        "新增候选需先按 accession 与 GEO/AE 在库记录查重、再人工甄别是否切片主题。")
    if kind == "geo":  # GEO 三通道降级（corpus_net 侧编排）：实际走的通道必须如实写进 note，
        # 绝不允许降级后还假装是主通道的数据（fail-closed + 如实标注铁律）。
        ch = str(res.get("channel") or "ncbi_eutils")
        if ch == "egeod_mirror":
            scope_zh = "E-GEOD 镜像（BioStudies）里的最新条目"
            channel_note = ("注意：NCBI 连不上，本次比对走的是 ArrayExpress 的 GEO 镜像"
                            "（E-GEOD，只有 2016 年前的老数据）——2016 年后的 GEO 新数据这里看不到。")
        elif ch == "europepmc_literature":
            scope_zh = "Europe PMC 文献里提到的 GSE"
            channel_note = ("注意：NCBI 和 E-GEOD 镜像都没通，本次是 Europe PMC 文献维度弱兜底"
                            "（只能证明有文献提到这些 GSE），不能代表 GEO 数据集的更新情况。")
    if new:
        note = (f"已在线比对：{scope_zh} {len(recent)} 条里，{len(new)} 条目录里还没有"
                f"（目录共收录 {local_count} 条）。要把某条入库可以说「联网搜…」。")
    else:
        note = f"已在线比对：{scope_zh} {len(recent)} 条目录里都有了（目录共收录 {local_count} 条）。"
    entry.update({
        "online_recent": len(recent),
        "new_candidates": new[:_NEW_CANDIDATES_SHOW],
        "new_count": len(new),
        "note_zh": channel_note + _snapshot_corrupt_note(local_errs) + note,
    })


def check_updates(sources: Any = None, *, project_root: Path) -> dict:
    """curate.check_updates 能力本体：检查来源有没有更新。**只读、不落盘、不抛异常**。

    - `sources=None` → 检查注册表全部来源；给列表 → 逐个按口语名解析（认不出的给
      `mode="unknown"` 条目如实说明，不因一个名字连累其余）。
    - 有在线通道的源（ArrayExpress / ENCODE / 10x / HCA / GEO / Zenodo / refine.bio）→ `mode="online"`：
      ArrayExpress 经 `_fetch_logged`（限速唯一出口）真在线拉最近条目比对；ENCODE / 10x / HCA /
      GEO / Zenodo / refine.bio 经 corpus_net 工具组拉最新清单差分。**网络失败/响应形状变化不抛**：
      各支统一如实降级 mode="snapshot" + note 写明原因（2026-08-15 G-14 统一降级语义——
      此前 AE 支保留 mode="online"，同一语义两种 mode，下游按 mode 判断时行为不一）。
    - 离线快照源 → `mode="snapshot"`：如实报告本地条数与快照日期（找不到就 null 并说明）、
      官网核对入口，并指路「联网搜…」。
    """
    root = Path(project_root)
    if sources is None:
        requested: list[tuple[str | None, str]] = [(key, key) for key in CHECK_UPDATE_SOURCES]
    else:
        items = sources if isinstance(sources, (list, tuple)) else [sources]
        requested = [(corpus_net.resolve_source_key(name, valid_keys=CHECK_UPDATE_SOURCES),
                      str(name or "").strip()) for name in items]

    entries: list[dict] = []
    for key, asked in requested:
        if key is None:
            entries.append({
                "source": asked or "（空）",
                "mode": "unknown",
                "note_zh": (f"不认识来源「{asked}」。可以检查的来源："
                            + "、".join(str(s["label"]) for s in CHECK_UPDATE_SOURCES.values()) + "。"),
            })
            continue
        spec = CHECK_UPDATE_SOURCES[key]
        # 官方快照是随包静态资源（只读）→ frozen 布局实例根从 resource 层读（source 下 = 项目根）。
        path = resource_file_for(root, str(spec["file"]))
        local_errs: list[str] = []
        local_count, snapshot_date = _snapshot_local_info(path, _err=local_errs)
        entry: dict[str, Any] = {
            "source": key,
            "label": spec["label"],
            "local_count": local_count,
            "site_url": spec["site_url"],
        }
        if local_errs:
            # G-27：损坏 ≠ 空库——local_count 是假象 0，结构化字段也如实标注
            entry["snapshot_error"] = local_errs[0]
        if spec["online"]:
            entry["mode"] = "online"
            if spec.get("net_kind"):
                _fill_online_check_net(entry, spec, local_count, path, root)  # 失败会降级 snapshot
            else:
                _fill_online_check(entry, spec, local_count, path, root)
        else:
            if not path.is_file():
                note = (f"本地没有找到这个来源的快照文件（{spec['file']}）。可到官网 {spec['site_url']} 核对；"
                        "要新数据可以说「联网搜…」。")
            elif local_errs:
                # G-27：损坏 ≠ 空库——不许把假象 0 条报给用户
                note = (f"这个来源只有本地副本，但快照文件（{spec['file']}）损坏无法解析（{local_errs[0]}），"
                        f"条数与快照日期不可知——修复或移走该文件后重试。"
                        f"可到官网 {spec['site_url']} 核对。要新数据可以说「联网搜…」。")
            else:
                date_zh = f"，快照日期 {snapshot_date}" if snapshot_date else "，快照日期未在文件里标注"
                note = (f"这个来源只有本地副本（{local_count} 条{date_zh}），本工具不能在线核对它有没有更新；"
                        f"可到官网 {spec['site_url']} 核对。要新数据可以说「联网搜…」。")
            entry.update({
                "mode": "snapshot",
                "snapshot_date": snapshot_date,
                "note_zh": note,
            })
        entries.append(entry)

    online_labels = "、".join(str(s["label"]) for s in CHECK_UPDATE_SOURCES.values() if s["online"])
    return {
        "checked_at": _now_iso(),
        "sources": entries,
        "hint_zh": (f"只有部分来源能在线核对更新（当前：{online_labels}）；"
                    "其余来源只给出本地副本的信息和官网地址，请自行核对。要新数据可以说「联网搜…」。"),
    }


# ==============================================================================================
# curate.sync_updates：检查更新 → 有新增则自动入库的复合流
#
# 设计蓝本：goose subrecipes 的同构物——**固定流程折叠成一个工具，步骤顺序写死在代码里**
# （不交给 LLM 逐步编排），步骤间只传窄接口结构化产物。闭环成立的交集 = 能在线比对的源
# （ArrayExpress / ENCODE / 10x / HCA / GEO / Zenodo / refine.bio）∩ 有入库适配器的源
# （SOURCE_ADAPTERS 九源）
# = ArrayExpress / 10x / HCA / GEO / Zenodo / refine.bio（refine.bio 2026-08-14 起；
# ENCODE 只有在线比对、无入库适配器）。
# 其余来源逐条如实写明哪一段做不到，不伪造闭环。
# ==============================================================================================

#: 每个来源一次最多自动入库的疑似新增条数（自动入库是写操作，宁少勿滥；
#: 超出部分如实报「还有 N 条没自动入库」，交给用户点名联网搜）。
#: 2026-08-08 约束放松批 5→10：实测 10x 一次检查发现 7+ 条新增只入 5 条、用户要反复催；
#: 同步加全请求总预算 `_SYNC_TOTAL_MAX_IMPORT`（评审——每源放宽的
#: 放大效应由总闸兜底，多源连跑也不失控）。
_SYNC_MAX_IMPORT = 10

#: 单次 sync_updates 调用跨来源累计自动入库的总预算（写操作总闸；诚实标注超预算部分，
#: 用户再说一次即可续跑——sync 是原子调用、记账+回收站可回退，预算只是单次写入量上限）。
_SYNC_TOTAL_MAX_IMPORT = 30


def _external_dataset_uids(root: Path) -> tuple[set[str], list[str]]:
    """外部库全部 *.json 文件里的 dataset_uid 集合（小写）+ 解析失败被跳过的文件名清单。

    sync 的去重判据：`check_updates` 的「疑似新增」只比对官方快照文件，不知道以往
    sync/联网搜/手动导入已经收进外部库的条目——不查这里就会把已入库的条目反复再入库。

    坏文件口径：解析失败**跳过、不炸掉整个 sync**（与
    `_scan_external_files` / `_external_identity_index` 的宽容装载同口径），但坏文件名
    必须随结果带回、由调用方如实呈现——既不零容错炸全局，也不对坏文件静默失明。"""
    uids: set[str] = set()
    skipped: list[str] = []
    ext = _external_dir(root)
    if not ext.is_dir():
        return _scope_union_uids(uids, skipped, root)
    for path in sorted(ext.glob("*.json")):
        try:
            records = _load_file_records(path)
        except CurateError:
            skipped.append(path.name)
            continue
        for record in records:
            if isinstance(record, dict):
                uid = _cmp_key_cached(str(record.get("dataset_uid") or ""))   # P1-4：与实体身份同一归一化真源
                if uid:
                    uids.add(uid)
    return _scope_union_uids(uids, skipped, root)


def _scope_union_uids(uids: set[str], skipped: list[str], root: Path) -> tuple[set[str], list[str]]:
    """任务 3：绑定补丁作用域时并入本人补丁 adds 的 uid（小写同口径）；未绑定 → 原样返回。"""
    scope = _patch_scope()
    if not scope:
        return uids, skipped
    from .patch_package import load_patch, record_uid

    patch = load_patch(Path(root), scope)
    for record in patch["adds"]:
        uid = _cmp_key_cached(record_uid(record))
        if uid:
            uids.add(uid)
    return uids, skipped


def _uid_matches_accession(uid: str, probe: str) -> bool:
    """uid 与待查编号的等值判定：整体等值，或 uid 以「:<编号>」收尾（uid 形如 source:accession）。

    G-03：此前用裸 `uid.endswith(probe)` 后缀匹配，互为首尾缀的编号会误判
    （probe="1234" 会命中 "zenodo:91234"）——把真新增误判「已在库」而跳过，或捡回错记录。"""
    return uid == probe or uid.endswith(":" + probe)


def _sync_collect_records(
    source_key: str, candidates: list[dict], existing_uids: set[str], *,
    max_import: int, root: Path,
) -> tuple[list[dict], list[str], int]:
    """逐编号把疑似新增搜回完整记录（适配器 search，dataset_uid 等值判定——G-03 起不再后缀匹配）。

    返回 (去重后的记录, 警告, 因已在外部库而跳过的条数)。单编号失败只进警告——
    一个编号失败不连累其余（与 check_updates 的「逐条如实」同哲学）。"""
    adapter = SOURCE_ADAPTERS.get(source_key)
    records: list[dict] = []
    warnings: list[str] = []
    seen: set[str] = set()
    skipped = 0
    identity_index, dedup_skipped = _external_identity_index(root)   # 实体级库态（一趟扫）
    if dedup_skipped:
        warnings.append(_dedup_skipped_note(dedup_skipped))
    for cand in candidates[:max_import]:
        acc = str(cand.get("accession") or "").strip()
        if not acc:
            continue
        probe = _cmp_key_cached(acc)   # 与实体身份同一归一化真源
        if any(_uid_matches_accession(uid, probe) for uid in existing_uids):
            skipped += 1
            continue
        try:
            hits, search_warnings = adapter["search"](acc, species="", limit=5, project_root=root)
            warnings.extend(str(w) for w in search_warnings)
        except CurateError as exc:
            warnings.append(f"{acc}：{exc.hint}")
            continue
        picked = [r for r in hits
                  if _uid_matches_accession(_cmp_key_cached(str(r.get("dataset_uid") or "")), probe)]
        if not picked:
            warnings.append(f"{acc}：按编号在线查，没有拿回能匹配该编号的记录。")
            continue
        for record in picked:
            # 同实体（同来源编号/同页面链接）已在外部库 → 跳过（盖到编号后缀比对
            # 抓不到的「换皮重复」——编号不同、链接相同）。
            keys = _record_identity_keys(record)
            if keys and keys & identity_index:
                skipped += 1
                continue
            uid = _cmp_key_cached(str(record.get("dataset_uid") or ""))
            if uid and uid not in seen:
                seen.add(uid)
                records.append(record)
    return records, warnings, skipped


def sync_updates(sources: Any = None, *, max_import: Any = _SYNC_MAX_IMPORT, project_root: Path) -> dict:
    """curate.sync_updates 能力本体：**检查更新 → 有新增则自动入库**的确定性复合流。

    步骤顺序写死在本函数里（不交给 LLM 编排）：
      1. `check_updates` 只读在线比对（同一真源；网络失败按既有契约如实降级，不抛）；
      2. 对每个「mode=online 且有疑似新增且有入库适配器」的来源：逐编号搜回完整记录
         （每源最多 `max_import` 条、全请求累计最多 `_SYNC_TOTAL_MAX_IMPORT` 条；
         外部库已有的跳过不重复入库），合并成**一个**
         sync 批次文件经 `uploads.ingest_dataset` 入库（与 apply_search_online 同一落盘
         管线、同一 upload_* 命名空间、同一本账——回收站可撤回）；
      3. 闭不了环的来源（只有离线快照 / 无入库适配器）在条目 note_zh 如实写明哪段做不到。

    外部库去重扫描遇坏 JSON：跳过不炸，坏文件名在返回的
    `skipped_files` 清单里如实列出（hint_zh 同步提示），不静默失明。

    不设 confirm_token 两步：token 是「跨信任边界的人机确认」机制，本函数是一次原子调用、
    没有边界要跨——与 agent 图内 `_loop_search_online` 的 plan→apply 原子化同一授权口径
    （全自动化：记账 + 回收站可回退）。

    加固：
      - 整任务跨进程文件锁（`sync_updates_critical_section`）覆盖「检查+联网+入库」全程；
        **唯一会抛的情形 = 另一个 sync 正在跑**（CurateError(sync_busy)，立即失败不排队，
        fail-closed 拒绝并发写）；其余一切失败沿用「不抛、逐源如实降级」契约。
      - 返回扩展 operation receipt（additive，既有字段逐位兼容）：`operation_id` /
        `created_files[]`（本次成功写入的 external 文件名，可整次撤回）/ `failed_sources[]`
        （逐源错误明细）/ `skipped_existing`（疑似新增其实已在库、未重复入库的累计条数）；
        receipt 同步落 `.userdata/curate_operations.jsonl`（`recall_sync_operation` 的撤回依据），
        实例级 `last_sync_at`/`last_operation_id` 落 `.userdata/curate_sync_state.json`。
    """
    root = Path(project_root)
    try:
        cap = max(1, min(20, int(max_import)))
    except (TypeError, ValueError):
        cap = _SYNC_MAX_IMPORT
    # 整任务跨进程锁（检查+联网+入库全程）。锁被占（另一进程/线程的
    # sync 正在跑）→ 立即 CurateError(sync_busy)，不排队——sync 是分钟级任务，排队等于
    # 假装会很快；写侧在 ingest_critical_section 里还有一层收口，但「按旧比对结果重写库」
    # 的重活发生在锁外，必须整任务锁住（见 sync_updates_critical_section 注释）。
    operation_id = _new_operation_id()
    with sync_updates_critical_section(root):
        checked = check_updates(sources, project_root=root)
        existing_uids, skipped_files = _external_dataset_uids(root)
        importable = set(SOURCE_ADAPTERS)  # 有入库适配器的来源才可能闭环

        entries: list[dict] = []
        imported_total = 0
        skipped_existing = 0
        for ent in checked.get("sources") or []:
            out: dict[str, Any] = {
                "source": ent.get("source"),
                "label": ent.get("label"),
                "mode": ent.get("mode"),
                "local_count": ent.get("local_count"),
                "new_count": ent.get("new_count"),
                "imported_count": 0,
                "filename": None,
                "imported_titles": [],
                "note_zh": str(ent.get("note_zh") or ""),
            }
            key = str(ent.get("source") or "")
            new_count = int(ent.get("new_count") or 0)
            if ent.get("mode") != "online" or new_count <= 0:
                entries.append(out)
                continue
            if key not in importable:
                out["note_zh"] = (
                    f"检到疑似新增 {new_count} 条，但这个来源还没有联网入库适配器，本工具不能自动入库"
                    "——可去官网核对，或把条目主题拿去已注册的源「联网搜…」。"
                )
                entries.append(out)
                continue
            # 全请求总预算闸：跨来源累计写入了了 `_SYNC_TOTAL_MAX_IMPORT`
            # 条后不再写——本源的疑似新增如实报「预算用完」，用户再说一次即可续跑。
            remaining_budget = _SYNC_TOTAL_MAX_IMPORT - imported_total
            if remaining_budget <= 0:
                out["note_zh"] = (
                    f"检到疑似新增 {new_count} 条，但本次请求的自动入库总预算"
                    f"（{_SYNC_TOTAL_MAX_IMPORT} 条）已用完，一条都没有自动入库"
                    "——再说一次「检查更新并同步」即可继续。"
                )
                entries.append(out)
                continue
            per_source_cap = min(cap, remaining_budget)
            # 逐源局部容错——本源搜回/写入抛错只毁本源条目
            # （如实记错误、imported_count=0、filename=None），已成功入库的其他来源及其回执
            # 不受影响；此前一个来源异常会让整次 sync 以失败呈现、已写文件回执全丢。
            try:
                records, warnings, skipped = _sync_collect_records(
                    key, list(ent.get("new_candidates") or []), existing_uids, max_import=per_source_cap, root=root)
                skipped_existing += skipped
                notes: list[str] = []
                if records:
                    # 搜回（联网逐编号查询）与落盘之间的窗口——进临界区后按届时库态终检一遍
                    # 再写；终检撞重并入「已在外部库」口径（与 apply_search_online 同一把锁收口）。
                    with ingest_critical_section(root):
                        # 坏文件名提示已由本函数顶层的 skipped_files（_external_dataset_uids
                        # 同目录扫描）如实带出，这里只取身份键做落盘前终检。
                        late_index, _late_skipped_files = _external_identity_index(root)
                        final_records = [r for r in records if not (_record_identity_keys(r) & late_index)]
                        late_skipped = len(records) - len(final_records)
                        if final_records:
                            raw = json.dumps(
                                {"source": SOURCE_ADAPTERS[key]["label"], "note": CURATE_SYNC_NOTE,
                                 "records": final_records},
                                ensure_ascii=False,
                            ).encode("utf-8")
                            res = ingest_dataset(
                                raw_bytes=raw,
                                safe_name=new_upload_name(f"curate_sync_{key}.json"),
                                project_root=root,
                                form_source=SOURCE_ADAPTERS[key]["label"],
                                note=CURATE_SYNC_NOTE,
                            )
                            out["imported_count"] = int(res.record_count or len(final_records))
                            out["filename"] = res.filename
                            out["imported_titles"] = [str(r.get("dataset_name") or "") for r in final_records[:5]]
                            imported_total += out["imported_count"]
                            notes.append(f"已自动入库 {out['imported_count']} 条（{res.filename}）")
                    if late_skipped:
                        skipped += late_skipped
                        skipped_existing += late_skipped
                    records = final_records   # 下方「另有 N 条没有自动入库」按真实写入口径算
                if skipped:
                    notes.append(f"{skipped} 条疑似新增其实已在外部库里，没有重复入库")
                if new_count > len(records) + skipped:
                    if per_source_cap < cap:
                        notes.append(f"另有 {new_count - len(records) - skipped} 条没有自动入库"
                                     f"（本次请求的自动入库总预算 {_SYNC_TOTAL_MAX_IMPORT} 条将用尽），"
                                     "再说一次「检查更新并同步」即可继续")
                    elif new_count > per_source_cap:
                        notes.append(f"另有 {new_count - len(records) - skipped} 条没有自动入库"
                                     f"（一次最多自动入库 {per_source_cap} 条），要它们可以说「联网搜…」")
                    # 其余情形（2026-08-15 G-01 文案修正）：差额来自逐编号搜回失败，
                    # 原因已由下方 warnings 如实写明——不再冒充「上限」口径。
                if warnings:
                    notes.append("；".join(warnings[:3]))
                if not records and not skipped:
                    notes.append("自动入库这一步没有拿回任何记录，未写入任何内容")
                out["note_zh"] = "；".join(str(n).rstrip("。") for n in notes) + "。"
            except Exception as exc:
                hint = getattr(exc, "hint", None) or str(exc)
                out["note_zh"] = (f"自动入库这一步在本来源发生错误，本来源没有写入（{hint}）；"
                                  "已成功入库的其他来源不受影响。")
                out["error"] = type(exc).__name__
            entries.append(out)

        closable_labels = "、".join(
            str(SOURCE_ADAPTERS[k]["label"]) for k in SOURCE_ADAPTERS
            if k in CHECK_UPDATE_SOURCES and CHECK_UPDATE_SOURCES[k].get("online")
        ) or "（目前没有来源能自动闭环）"
        hint = (f"自动入库只覆盖「能在线比对且有入库适配器」的来源（当前：{closable_labels}）。"
                "其余来源的疑似新增请核对官网后用「联网搜…」收入。")
        if skipped_files:
            hint += (f"注意：外部库里有 {len(skipped_files)} 个文件损坏无法解析"
                     f"（{'、'.join(skipped_files)}），本次同步的去重比对没有覆盖它们，"
                     "修复或移走这些文件后再同步一次即可。")
        created_files = [str(e["filename"]) for e in entries if e.get("filename")]
        failed_sources = [
            {"source": e.get("source"), "label": e.get("label"),
             "error": e.get("error"), "note_zh": e.get("note_zh")}
            for e in entries if e.get("error")
        ]
        # operation receipt 落账（append-only JSONL）——recall 按
        # operation_id 从这里取 created_files；写失败静默降级（sync 已成功，账本只是撤回依据，
        # 不能因为记不上账让已完成的导入「看起来失败」——与联网账本同一纪律）。
        try:
            _append_jsonl(_sync_ledger_path(root), {
                "ts": _now_iso(),
                "operation_id": operation_id,
                "checked_at": checked.get("checked_at"),
                "imported_total": imported_total,
                "skipped_existing": skipped_existing,
                "created_files": created_files,
                "failed_sources": failed_sources,
            })
        except OSError:
            pass
        # 实例级同步事实（last_sync_at/last_operation_id）持久化；busy 由 sync_status 实时探测锁。
        try:
            _persist_sync_state(root, operation_id=operation_id, checked_at=checked.get("checked_at"))
        except OSError:
            pass
        return {
            "checked_at": checked.get("checked_at"),
            "sources": entries,
            "imported_total": imported_total,
            "skipped_files": skipped_files,
            "hint_zh": hint,
            # --- additive：operation receipt（既有字段逐位兼容）---
            "status": "ok",
            "operation_id": operation_id,
            "created_files": created_files,
            "failed_sources": failed_sources,
            "skipped_existing": skipped_existing,
        }


# ==============================================================================================
# 接口层统一分发（MCP `curate_datasets` / Web `/api/curate/*` / CLI `scripts/curate_datasets.py`
# 三入口共用）：动作→plan/apply 函数映射 + fail-closed 入参闸 + write_boundary 文案，单一真源，
# 不在各入口各写一份。
# ==============================================================================================

def write_boundary_zh(action: str, *, dry_run: bool) -> str:
    """三入口共用的写盘/联网边界声明（单一真源，防三处文案漂移）。

    语言水平与失败回执一致（「不是可管护的上传文件…」那句的人话口径）：机制词
    （upload_* 命名空间 / curate.restore / 写盘）不上屏——落盘位置走 `saved_to`/`moved_to`
    等结构化字段；但诚实语义一个字不能丢：可逆、没真删、绝不碰冻结基准（copy 评审）。

    补 check_updates（只读）与 sync_updates（写盘 + 可整次撤回）
    两个动作的边界文案——它们不走 run_curate_action（无 plan→apply 两步），但文案真源
    仍住在这里，MCP 等入口直接调本函数，不各写一份。"""
    if action == "list":
        return "纯只读清点：没有改动任何文件，也没有联网。"
    if action == "check_updates":
        return "纯只读检查：没有改动任何文件（在线比对会联网，并记一行请求账本）。"
    if action == "sync_updates":
        return "已把能自动入库的疑似新增导入外部库（database/external/ 下你自己的上传区）；官方基准库 database/base/ 一个字节都没动。本次导入可整次撤回。"
    if dry_run:
        note = "本次只是预览，没有写入任何数据；"
        if action == "search_online":
            note += ("这次预览会真实联网查询官方来源，并在 .userdata/curate_net_ledger.jsonl "
                     "记一行请求记录（不记秘密）；")
        return note + "只有你确认后，才会真正写入。"
    if action == "import":
        return "已存进外部库（database/external/ 下你自己的上传区）；官方基准库 database/base/ 一个字节都没动。"
    if action == "search_online":
        return "已把联网搜到并确认的候选存进外部库（database/external/ 下你自己的上传区）；官方基准库 database/base/ 一个字节都没动。"
    if action == "remove":
        return "已移入回收站（可逆，之后可以从回收站恢复）；没有真删除任何字节。"
    return "已从回收站把文件移回外部库 database/external/。"


def run_curate_action(
    action: Any,
    *,
    dry_run: bool = True,
    query: Any = None,
    source: Any = None,
    species: Any = None,
    limit: Any = None,
    filename: Any = None,
    payload_bytes: bytes | None = None,
    plan_result: dict | None = None,
    confirm_token: Any = None,
    force: bool = False,
    project_root: Path,
) -> dict:
    """三入口统一分发：动作 → plan/apply 函数映射 + fail-closed 入参闸。

    - 未知动作 → bad_action（require_action）；
    - apply（dry_run=False）缺 confirm_token → bad_param（list 只读、无 apply 形态）；
    - 各动作缺必传参 → bad_param（import 缺 payload；remove/restore 缺 filename；
      search_online apply 缺 plan_result）。
    返回真源函数的结果 + `write_boundary` 边界声明。"""
    name = require_action(action)
    root = Path(project_root)
    if name == "list":
        result = list_curations(project_root=root)
        result["write_boundary"] = write_boundary_zh(name, dry_run=True)
        return result
    if not dry_run and not str(confirm_token or "").strip():
        raise CurateError(
            "bad_param",
            "apply（dry_run=False）必须回传 plan 返回的 confirm_token；请先 plan 拿到预览与 token 再确认。",
        )
    if name == "import":
        if payload_bytes is None:
            raise CurateError("bad_param", "curate.import 需要 payload（数据集 JSON 内容字节）。")
        fname = filename or "curate_import.json"
        if dry_run:
            result = plan_import(payload_bytes, fname, source, project_root=root)
        else:
            result = apply_import(
                payload_bytes, fname, source,
                confirm_token=confirm_token, force=bool(force), project_root=root,
            )
    elif name == "search_online":
        if dry_run:
            result = plan_search_online(
                query, source or "arrayexpress", species,
                20 if limit is None else limit, project_root=root,
            )
        else:
            if not isinstance(plan_result, dict):
                raise CurateError(
                    "bad_param",
                    "curate.search_online 的 apply 需要原样回传 plan 返回的完整结果（plan_result，含 candidates）。",
                )
            result = apply_search_online(plan_result, confirm_token=confirm_token, project_root=root)
    elif name == "remove":
        if not str(filename or "").strip():
            raise CurateError("bad_param", "curate.remove 需要 filename（external 库里的文件名）。")
        result = (plan_remove(filename, project_root=root) if dry_run
                  else apply_remove(filename, confirm_token=confirm_token, project_root=root))
    else:  # restore
        if not str(filename or "").strip():
            raise CurateError(
                "bad_param",
                "curate.restore 需要 filename（回收站里的文件名，含时间戳前缀；curate.list 可查）。",
            )
        result = (plan_restore(filename, project_root=root) if dry_run
                  else apply_restore(filename, confirm_token=confirm_token, project_root=root))
    result["write_boundary"] = write_boundary_zh(name, dry_run=dry_run)
    return result


# ==============================================================================================
# curate.sync_updates 加固（2026-08-22 落地包）：整任务跨进程锁 + operation receipt
# + 按 operation_id 批量撤回 + 实例级同步状态
#
# 设计蓝本：设计文档 §7（评审①阻断4 裁决）——
#   ① 新增跨进程 `sync_updates.lock`（沿用 uploads.ingest_critical_section 文件锁模式），覆盖整次
#      sync（检查+联网+入库），冲突**立即**返回 sync_busy，不排队；
#   ② 返回 operation receipt（operation_id / created_files[] / failed_sources[] / skipped_existing /
#      逐源明细，保留既有字段兼容）；
#   ③ 新增按 operation_id 批量撤回（回收站语义，撤掉该次全部成功写入文件，可重入、失败不破坏既有数据）；
#   ④ 新增实例级同步状态（last_sync_at / last_operation_id / busy）——「上次同步」是**实例级事实**，
#      不得存 per-profile localStorage。
# ==============================================================================================

#: 同步整任务锁文件名（与 uploads 的 upload_ingest.lock 平行：进程内线程锁 + 跨进程 OS 文件锁，
#: 但**非阻塞**——冲突立即 sync_busy，不用 uploads 的 60s 等待超时语义）。
SYNC_LOCK_FILE_NAME = "sync_updates.lock"

#: sync operation receipt 账本（append-only JSONL，`.userdata/` 下）：sync_updates 每次执行成功
#: 返回时记一行，`recall_sync_operation` 按 operation_id 从这里取 created_files。
SYNC_OPERATIONS_NAME = "curate_operations.jsonl"

#: 实例级同步状态（`.userdata/` 下 JSON）：`{last_sync_at, last_operation_id}`；busy 实时探测锁、不落盘。
SYNC_STATE_NAME = "curate_sync_state.json"

# 进程内线程锁 + 同线程重入深度（与 uploads.ingest_critical_section 同构；锁序恒定：sync 锁 →
# ingest 锁，无死锁面——ingest 临界区从不反向拿 sync 锁）。
_sync_lock = threading.Lock()
_sync_lock_state = threading.local()


def _sync_lock_path(project_root: Path) -> Path:
    return instance_data_dir_for(Path(project_root), USERDATA_DIR_NAME) / SYNC_LOCK_FILE_NAME


def _sync_ledger_path(project_root: Path) -> Path:
    return instance_data_dir_for(Path(project_root), USERDATA_DIR_NAME) / SYNC_OPERATIONS_NAME


def _sync_state_path(project_root: Path) -> Path:
    return instance_data_dir_for(Path(project_root), USERDATA_DIR_NAME) / SYNC_STATE_NAME


def _new_operation_id() -> str:
    """一次 sync 操作的唯一标识：`sync_<YYYYMMDD_HHMMSS_microseconds>`（与回收站时间戳同格式前缀）。"""
    return f"sync_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"


def _acquire_os_sync_lock_nowait(project_root: Path):
    """非阻塞获取跨进程 OS 文件锁（平台骨架唯一锚点在 `fs_utils.acquire_file_lock`）。

    成功 → 返回打开的句柄；已被占（本进程其它线程 / 其它进程）→ 返回 None。**不等待、不重试**——
    sync 是分钟级整任务，冲突说明另一个 sync 正在跑，立即如实 sync_busy 让调用方决定重试，
    与 uploads 摄取锁的 60s 退避等待语义刻意不同（摄取是秒级操作，排队合理；sync 排队无意义）。"""
    try:
        return fs_utils.acquire_file_lock(_sync_lock_path(project_root), wait=False)
    except fs_utils.FileLockBusy:
        return None


def _release_os_sync_lock(fh) -> None:
    """释放 `_acquire_os_sync_lock_nowait` 拿到的锁并关闭句柄。"""
    fs_utils.release_file_lock(fh)


@contextlib.contextmanager
def sync_updates_critical_section(project_root: Path):
    """sync_updates 的**整任务**临界区（进程内线程锁 + 跨进程 OS 文件锁；**同线程可重入**）。

    与 `uploads.ingest_critical_section` 同构但**非阻塞**：锁已被占（另一进程/线程的 sync 正在跑）
    → 立即 `CurateError(sync_busy)`，不排队等待。覆盖「检查+联网+入库」全程——写侧在
    `ingest_critical_section` 里还有一层收口，但「按旧比对结果重写库」的重活发生在锁外，
    两个 sync 同时比对会各自按过期结果重复入库，必须整任务锁住。

    重入语义：同线程已持锁时直接放行（sync_updates 内部嵌套场景不会自锁死）。"""
    depth = getattr(_sync_lock_state, "depth", 0)
    if depth > 0:                       # 重入：外层已持双锁，直接放行
        _sync_lock_state.depth = depth + 1
        try:
            yield
        finally:
            _sync_lock_state.depth = depth
        return
    if not _sync_lock.acquire(blocking=False):  # 锁序恒定：线程锁 → OS 锁（单一顺序，无死锁面）
        # 同进程并发 sync：不阻塞排队（排队会在前者放锁后静默重跑一整轮分钟级同步），
        # 与 OS 锁占用同一出口——立即 sync_busy，兑现本函数 docstring 的非阻塞契约。
        raise CurateError(
            "sync_busy",
            "另一个「同步数据集」正在运行。本次没有做任何检查、"
            "也没有写入任何内容；请稍后重试，或先用「检查更新」只读看一眼。",
        )
    try:
        fh = _acquire_os_sync_lock_nowait(project_root)
        if fh is None:
            raise CurateError(
                "sync_busy",
                "另一个「同步数据集」正在运行（同步整任务文件锁被占用）。本次没有做任何检查、"
                "也没有写入任何内容；请稍后重试，或先用「检查更新」只读看一眼。",
            )
        try:
            _sync_lock_state.depth = 1
            yield
        finally:
            _sync_lock_state.depth = 0
            _release_os_sync_lock(fh)
    finally:
        _sync_lock.release()


def sync_lock_busy(project_root: Path) -> bool:
    """实例级同步状态的 busy 判定：非阻塞探测 sync 锁是否被占用。**只读、不写盘、不阻塞**。

    跨进程实时：另一进程持锁即 busy=True。进程崩溃遗留的锁文件不永久 busy——OS 层文件锁随
    进程消亡自动释放，下次探测即恢复 False。"""
    fh = _acquire_os_sync_lock_nowait(project_root)
    if fh is None:
        return True
    _release_os_sync_lock(fh)
    return False


def _persist_sync_state(project_root: Path, *, operation_id: str, checked_at: Any) -> None:
    """把实例级同步事实写进 `.userdata/curate_sync_state.json`（原子：临时文件 + 替换）。

    写失败抛 OSError 由调用方降级（sync 已完成，状态文件只是「上次同步时间」的事实源）。"""
    path = _sync_state_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps({"last_sync_at": checked_at, "last_operation_id": operation_id},
                   ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(str(tmp), str(path))


def _read_sync_operations(project_root: Path) -> list[dict]:
    """读 operation receipt 账本（缺失 → 空；单行损坏跳过不连累其它行——与 _read_manifest 同口径）。"""
    path = _sync_ledger_path(project_root)
    if not path.is_file():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _find_sync_operation(operation_id: Any, project_root: Path) -> dict | None:
    """按 operation_id 查 receipt 账本（无则 None）。"""
    want = str(operation_id or "").strip()
    if not want:
        return None
    for entry in _read_sync_operations(project_root):
        if str(entry.get("operation_id") or "") == want:
            return entry
    return None


def sync_status(*, project_root: Path) -> dict:
    """实例级同步事实（`GET /api/curate/sync-status` 的真源）：上次同步时间 / 上次操作 id / 是否 busy。

    `last_sync_at` / `last_operation_id` 从 `.userdata/curate_sync_state.json` 读（sync_updates
    每次完成时持久化；缺失 → null，如实「还没同步过」）；`busy` 实时探测 sync 锁（不写盘）。
    **只读、不抛**——状态文件损坏按「还没同步过」如实降级，不掀翻端点。

    「上次同步」是**实例级事实**：不得存 per-profile localStorage。"""
    root = Path(project_root)
    state: dict[str, Any] = {}
    path = _sync_state_path(root)
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                state = loaded
        except (ValueError, OSError):
            state = {}
    return {
        "last_sync_at": state.get("last_sync_at"),
        "last_operation_id": state.get("last_operation_id"),
        "busy": sync_lock_busy(root),
    }


def recall_sync_operation(operation_id: Any, *, project_root: Path) -> dict:
    """按 operation_id **整次撤回**一次 sync 的全部成功写入（回收站语义：移动到 `.userdata/recycle/`）。

    设计：—「完整事后回执 + 整次一键撤回」替代「先预览再应用」。撤的是
    `created_files[]`（该次 sync 成功写入 external 的全部文件），与 apply_remove 同一回收站管线
    （时间戳前缀 + manifest 账本 + 缓存即时失效），可逆、不真删、不碰冻结基准。

    契约：
      - operation 不存在 → `CurateError(unknown_operation)`（fail-closed 指名，不静默空转）；
      - **可重入**：created_files 已不在 external（已被撤回 / 已被单独 remove / 被外部移走）→
        计入 `skipped_files` 跳过，不报错；已在回收站的不重复移动；
      - **单文件失败不连累其余**：某文件移动抛错只记进 `failed_files`，其余照常处理；已成功撤回的
        保持已撤回（不回滚）——「失败不破坏既有数据」指撤回过程永不毁坏未撤文件与回收站账本。"""
    if _patch_scope():
        # 任务 3：绑定账户作用域时，sync 入库经写漏斗改落**该账户补丁包 adds**
        # （receipt 里记的是合成批次号，external 下没有对应文件可撤回）。与其让撤回流程逐文件
        # 「已不在外部库」空转、回报一句误导性的「没有可撤回的文件」，不如当场如实指路：
        # 补丁条目的删除/恢复走 curate.remove / curate.restore（回收站语义同样在账户补丁内）。
        raise CurateError(
            "bad_param",
            "当前登录账户的同步入库落在你的补丁包（仅你可见），没有可按批次撤回的共享库文件；"
            "要撤销这些条目，请用「删除数据集」按编号移除（进你的补丁回收站，可恢复）。",
        )
    root = Path(project_root)
    op_id = str(operation_id or "").strip()
    op = _find_sync_operation(op_id, root)
    if op is None:
        raise CurateError(
            "unknown_operation",
            f"找不到同步操作「{op_id}」——请用返回的 operation_id 原样撤回，"
            "或到「同步状态」查最近一次操作的 operation_id。",
        )
    files = [str(f) for f in (op.get("created_files") or []) if str(f).strip()]
    rec_dir = _recycle_dir(root)
    rec_dir.mkdir(parents=True, exist_ok=True)
    recalled: list[str] = []
    skipped: list[str] = []
    failed: list[dict[str, str]] = []
    for name in files:
        try:
            target = _external_dir(root) / name
            if not target.is_file():
                skipped.append(name)   # 已不在 external（已撤回过/被移除）→ 可重入跳过
                continue
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            dest = rec_dir / f"{stamp}_{name}"
            while dest.exists():       # 同微秒防冲突：绝不覆盖回收站里已有文件（与 apply_remove 同口径）
                dest = rec_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{name}"
            shutil.move(str(target), str(dest))
            _append_jsonl(_recycle_manifest(root), {
                "ts": _now_iso(),
                "action": "recall",
                "operation_id": op_id,
                "original_path": f"{EXTERNAL_DIR_NAME}/{name}",
                "recycle_name": dest.name,
                "record_count": len(_load_file_records(dest)),
            })
            recalled.append(name)
        except Exception as exc:
            hint = getattr(exc, "hint", None) or str(exc)
            failed.append({"filename": name, "error": type(exc).__name__, "hint": str(hint)})
    if recalled:
        invalidate_external_cache()
    status = "ok" if not failed else ("partial" if recalled else "failed")
    hint_parts: list[str] = []
    if recalled:
        hint_parts.append(f"已撤回 {len(recalled)} 个文件（移入回收站，可恢复）")
    if skipped:
        hint_parts.append(f"{len(skipped)} 个文件已不在外部库，跳过（可能已被撤回）")
    if failed:
        hint_parts.append(f"{len(failed)} 个文件撤回失败（见 failed_files 明细），已撤回的不受影响")
    return {
        "operation_id": op_id,
        "status": status,
        "recalled_files": recalled,
        "skipped_files": skipped,
        "failed_files": failed,
        "hint_zh": "；".join(hint_parts) + "。" if hint_parts else "这次操作没有可撤回的文件。",
    }
