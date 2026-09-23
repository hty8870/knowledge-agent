# -*- coding: utf-8 -*-
"""每账户语料补丁包（2026-08-26 任务 3：网页版「基线版本 + 用户补丁包」）。

数据模型
--------
- **基线**（共享、请求路径只读）：`database/base/` 冻结基准 + `database/external/` 官方快照层与
  运营者写层（未绑定作用域的写入——本机匿名、CLI、MCP、服务器侧运营 sync——仍落这里，逐字节不变）。
- **补丁包**（每登录账户一份）：`.userdata/patches/<account_id>.json`
  `{schema_version, account_id, updated_at, adds[], blocks[], trash[]}`：
  - `adds`：该账户新增的数据集记录（上传/导入/联网搜入库/同步入库的全部写漏斗终点）；
  - `blocks`：该账户屏蔽的 dataset_uid（基线/共享库记录从**该账户视图**中消失，共享内容不受影响）；
  - `trash`：从 adds 里删下的记录（回收站式可逆，恢复即放回 adds）。

作用域绑定（contextvars，与 agent/trace/recorder.py 同款先例——集成零签名变更）
------------------------------------------------------------------------------
- Web 层每个请求解析会话：有账户 → `bind_patch_scope(user.id)`；匿名/本机无会话 → 不绑定。
- agent 执行侧的 SSE worker 线程不继承请求 context：`turn.route_turn` 入口按既有 `principal`
  （会话账户 id / "anonymous"）再绑一次，agent tool call 的写盘因此同样只进本人补丁包。
- **未绑定 = 缺省本机形态 / 官方冻结评测 / CLI / MCP：全部读写路径与历史逐字节一致**（结构性保证）。

读侧合并：`corpus.load_normalized_corpus` / `load_full_corpus` / `available_sources` /
`corpus_cache_generation` 在绑定作用域下应用补丁（块过滤 + 追加 adds，adds 按其 source 参与来源筛选）。
写侧路由：`uploads.ingest_dataset`（唯一入库写漏斗）与 `corpus_curation` 的 remove/restore/list
在绑定作用域下改写/读本账户补丁包。

红线
----
- 补丁库存放路径必须在仓库 `database/` 之外（运行时数据；守卫同 accounts._assert_runtime_path 口径）。
- 补丁文件**损坏即 fail-closed**（PatchError patch_corrupt）：绝不静默重建覆盖用户数据（账户库同哲学）。
- 写盘全部原子写（tmp + os.replace）；每账户一把线程锁，计数/去重/落盘同一临界区。
- 冻结基准结构性免疫：补丁只影响绑定作用域内的视图，不碰 base/external 任何一个字节。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from ..app.runtime_paths import atomic_write_json, instance_data_dir_for, repo_database_dir

SCHEMA_VERSION = 1
#: 单账户补丁 adds 累计上限（网页版多人共享部署的防洪水闸；本机 external 全库上限
#: EXTERNAL_TOTAL_MAX_RECORDS=100 万是同哲学实例级闸）。5000 条对「个人数据集收藏级」使用极宽。
MAX_PATCH_ADD_RECORDS = 5000
#: 补丁文件字节上限（防御性，正常 5000 条元数据 ≈ 10MB 内）。
MAX_PATCH_FILE_BYTES = 64 * 1024 * 1024

_ACCOUNT_ID_RE = re.compile(r"[0-9a-zA-Z_-]{3,64}")


class PatchError(ValueError):
    """带机器码的补丁包失败（与 uploads.UploadError 同形：code 供分类映射，hint 供人读）。"""

    def __init__(self, code: str, hint: str) -> None:
        self.code = code
        self.hint = hint
        super().__init__(f"{code}: {hint}")


# ---------------------------------------------------------------- 作用域绑定（contextvars）

_SCOPE: contextvars.ContextVar["str | None"] = contextvars.ContextVar(
    "biodata_corpus_patch_scope", default=None)


def current_patch_scope() -> "str | None":
    """当前 context 绑定的补丁账户 id；未绑定（本机匿名/CLI/MCP/评测）→ None。"""
    return _SCOPE.get()


@contextmanager
def bind_patch_scope(account_id: "str | None") -> "Iterator[None]":
    """把本 context 的语料读写绑到 `account_id` 的补丁包；None = 显式不绑定（复位用）。"""
    token = _SCOPE.set(account_id or None)
    try:
        yield
    finally:
        _SCOPE.reset(token)


@contextmanager
def unbound_patch_scope() -> "Iterator[None]":
    """显式清绑定（2026-08-26 corpus-sync 批）：语料读写强制落共享写层，与「从未绑定」逐位一致。

    用途：① 后台线程跑实例级任务（语料同步 job——sync 产物进共享写层 upload_*，用户上传
    都在各自补丁包、不在此层）；② 实例级哨兵计算（health 的 corpus.gen 要不带账户补丁代际，
    否则同一实例不同账户看到不同哨兵）。后台线程本就不继承请求 context（默认未绑定），
    这里显式钉死，防未来调用链变更把请求作用域泄进来。"""
    with bind_patch_scope(None):
        yield


# ---------------------------------------------------------------- 存储路径与守卫

def _assert_runtime_path(path: Path) -> Path:
    """运行时补丁库绝不许落进仓库 `database/`（冻结基准与元数据库）。目录真源同
    `runtime_paths.repo_database_dir`（2026-08-10 裁决）；守卫文案保留补丁包语境
    （PatchError 契约与 accounts/mcp_tokens 不同），故不共用 assert_runtime_path。"""
    resolved = path.expanduser().resolve()
    try:
        resolved.relative_to(repo_database_dir())
    except ValueError:
        return resolved
    raise PatchError(
        "bad_store_path",
        f"补丁包运行时不许落在仓库 database/ 目录内（收到 {resolved}）；请检查实例数据根配置。")


def _patches_dir(project_root: Path) -> Path:
    override = os.environ.get("BIODATA_PATCHES_DIR", "").strip()
    if override:
        return _assert_runtime_path(Path(override))
    return _assert_runtime_path(instance_data_dir_for(Path(project_root), ".userdata") / "patches")


def _validate_account_id(account_id: str) -> str:
    """账户 id 来自 accounts.register（token_hex(8)）；路径拼接前强制白名单校验，防路径穿越。"""
    aid = str(account_id or "").strip()
    if not _ACCOUNT_ID_RE.fullmatch(aid):
        raise PatchError("bad_account", "补丁账户标识非法。")
    return aid


def patch_path_for(project_root: Path, account_id: str) -> Path:
    return _patches_dir(Path(project_root)) / f"{_validate_account_id(account_id)}.json"


def patch_generation(project_root: Path, account_id: str) -> tuple:
    """补丁文件的廉价代际键（Web 大响应缓存的 O(1) 失效键，与 corpus_cache_generation 同哲学）。"""
    try:
        st = patch_path_for(Path(project_root), account_id).stat()
    except (OSError, PatchError):
        return (str(account_id), None, None)
    return (str(account_id), st.st_mtime_ns, st.st_size)


# ---------------------------------------------------------------- 读写存储

def _empty_patch(account_id: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "account_id": account_id,
        "updated_at": 0.0,
        "adds": [],
        "blocks": [],
        "trash": [],
    }


def _validate_patch_shape(data: Any, account_id: str) -> dict:
    """结构校验（fail-closed）：任一字段类型不符即 patch_corrupt，绝不静默丢数据重建。"""
    if not isinstance(data, dict):
        raise PatchError("patch_corrupt", "补丁包文件结构异常（不是对象），已停止以防覆盖；请从备份恢复。")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise PatchError("patch_corrupt", "补丁包 schema 版本不符，已停止以防误读。")
    if str(data.get("account_id") or "") != account_id:
        raise PatchError("patch_corrupt", "补丁包账户标识与文件名不符，已停止以防串号。")
    for key in ("adds", "blocks", "trash"):
        if not isinstance(data.get(key), list):
            raise PatchError("patch_corrupt", f"补丁包字段 {key} 结构异常，已停止以防覆盖。")
    if any(not isinstance(r, dict) for r in data["adds"]) or any(not isinstance(r, dict) for r in data["trash"]):
        raise PatchError("patch_corrupt", "补丁包记录条目结构异常，已停止以防覆盖。")
    if any(not isinstance(u, str) for u in data["blocks"]):
        raise PatchError("patch_corrupt", "补丁包屏蔽列表结构异常，已停止以防覆盖。")
    return data


def load_patch(project_root: Path, account_id: str) -> dict:
    """读补丁包。**fail-closed**：缺失/空 → 空补丁（首次使用）；存在但解析失败 → PatchError。"""
    aid = _validate_account_id(account_id)
    path = patch_path_for(Path(project_root), aid)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return _empty_patch(aid)
    except OSError as exc:
        raise PatchError("store_unavailable", "补丁包暂时不可读，请稍后重试。") from exc
    if not raw.strip():
        return _empty_patch(aid)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PatchError("patch_corrupt", "补丁包文件损坏，已停止以防覆盖；请从备份恢复或删除后重建。") from exc
    return _validate_patch_shape(data, aid)


_PATH_LOCKS: "dict[str, threading.RLock]" = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path)
    with _PATH_LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PATH_LOCKS[key] = lock
        return lock


def _save_patch(project_root: Path, patch: dict) -> None:
    """原子写走 runtime_paths.atomic_write_json；条数/字节闸在调用方。字节闸须先于落盘，
    故先 dumps 计量再写（atomic_write_json 内部再序列化一次，产出与计量字节逐字节一致）。"""
    path = patch_path_for(Path(project_root), patch["account_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    patch["updated_at"] = time.time()
    blob = json.dumps(patch, ensure_ascii=False)
    if len(blob.encode("utf-8")) > MAX_PATCH_FILE_BYTES:
        raise PatchError("too_large", "补丁包体积超过上限，未写入；请先清理不再需要的条目。")
    atomic_write_json(path, patch, indent=None)


def record_uid(record: dict) -> str:
    return str(record.get("dataset_uid") or "").strip()


def _content_digest(record: dict) -> str:
    blob = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 读侧合并

def apply_patch(
    records: list,
    patch: dict,
    *,
    include_adds: bool,
    source_filter: "set[str] | None" = None,
) -> list:
    """把补丁应用到一份已装配的语料列表上（返回新列表，入参不被改写）。

    - `blocks`：按 dataset_uid 过滤（对该账户隐藏基线/共享库记录），任何模式都生效；
    - `adds`：`include_adds=True` 时归一化后追加；`source_filter` 非空时按其 source 参与筛选
      （与外部库记录的来源筛选语义完全同型）；`include_adds=False`（base-only 口径）不追加。
    """
    blocks = {str(u).strip() for u in patch.get("blocks", []) if str(u).strip()}
    out = records
    if blocks:
        out = [
            r for r in out
            if record_uid(r.raw if isinstance(getattr(r, "raw", None), dict) else {}) not in blocks
        ]
    if include_adds and patch.get("adds"):
        from ..retrieval.normalizer import normalize_dataset_record  # 惰性：仅在真有补丁时付出 import
        aid = str(patch.get("account_id") or "")
        for raw in patch["adds"]:
            if source_filter is not None:
                src = str(raw.get("source") or "").strip()
                if src not in source_filter:
                    continue
            out = out + [normalize_dataset_record(raw, f"patch:{aid}")]
    return list(out)


def patch_adds_total(project_root: Path, account_id: str) -> int:
    """只读计数（预算闸用）；损坏语义同 load_patch（fail-closed）。"""
    return len(load_patch(Path(project_root), account_id)["adds"])


def effective_visible_uids(project_root: Path, account_id: str) -> set[str]:
    """该账户**有效语料视图**内的全部 dataset_uid（基线 ∪ 共享外部 ∪ 本人 adds − 本人 blocks）。

    写侧去重闸用：防止补丁 adds 与基线/共享库撞号（撞号会让 locate_record 按歧义拒答）。
    惰性 import corpus（本模块被 corpus 惰性回调，两边都惰性即无 import 环）。
    """
    from .corpus import load_full_corpus  # 惰性

    root = Path(project_root)
    patch = load_patch(root, account_id)
    blocks = {str(u).strip() for u in patch["blocks"] if str(u).strip()}
    uids: set[str] = set()
    for r in load_full_corpus(root / "database" / "base", root):
        uid = record_uid(r.raw if isinstance(getattr(r, "raw", None), dict) else {})
        if uid and uid not in blocks:
            uids.add(uid)
    for raw in patch["adds"]:
        uid = record_uid(raw)
        if uid:
            uids.add(uid)
    return uids


# ---------------------------------------------------------------- 写侧操作（全部原子 + 同临界区）

def ingest_records_to_patch(
    *,
    payload: Any,
    records: list[dict],
    project_root: Path,
    account_id: str,
    form_source: str = "",
    note: str = "",
):
    """绑定作用域下的入库终点（替代 uploads 的 external 落盘）：打标/提示与 uploads 同口径，
    按 uid 去重（本人 adds 撞重跳过；与基线/共享可见集撞号跳过并如实提示），计数闸后原子落盘。

    返回 `uploads.UploadResult`（接口层零改）：filename = 合成批次号，saved_to = 人读口径
    「我的补丁包」；跳过明细并入 warnings（如实可见）。
    """
    from datetime import datetime, timezone

    from .uploads import (  # 惰性：写时才付出（读路径/评测永不 import uploads）
        UploadResult, _append_upload_journal, _ingest_warnings, _tag_records_for_ingest,
    )

    root = Path(project_root)
    aid = _validate_account_id(account_id)
    path = patch_path_for(root, aid)
    with _lock_for(path):
        patch = load_patch(root, aid)   # 损坏 fail-closed：绝不覆盖
        fallback_source, _counts, missing_name, unknown_species = _tag_records_for_ingest(
            payload, records, form_source)

        existing_uids = {record_uid(r) for r in patch["adds"] if record_uid(r)}
        visible = effective_visible_uids(root, aid)   # 基线 ∪ 共享 ∪ 本人 adds − blocks
        accepted: list[dict] = []
        skipped_dup = 0
        skipped_collision = 0
        for r in records:
            uid = record_uid(r)
            if uid:
                if uid in existing_uids:
                    skipped_dup += 1
                    continue
                if uid in visible:
                    # 撞号基线/共享库：放行会让 locate_record 按「uid 撞库」报歧义（谁的视图里
                    # 都是两条同号记录）。v1 如实拒收并提示；「以我的版本遮蔽基线条目」是后续批。
                    skipped_collision += 1
                    continue
            accepted.append(r)

        if not accepted:
            # 全跳过 = 零写入（与 duplicate_content 同哲学：如实回报，不落盘）。
            return UploadResult(
                filename="",
                saved_to="我的补丁包（仅本账户可见）",
                record_count=0,
                sources={},
                warnings=_skipped_warnings(skipped_dup, skipped_collision)
                + _ingest_warnings(missing_name, unknown_species),
            )

        if len(patch["adds"]) + len(accepted) > MAX_PATCH_ADD_RECORDS:
            raise PatchError(
                "too_large",
                f"补丁包现有 {len(patch['adds'])} 条，再导入 {len(accepted)} 条会超过单账户上限 "
                f"{MAX_PATCH_ADD_RECORDS} 条——这个上限防的是单个账户的补丁拖垮其检索加载。"
                "请先删除不再需要的补丁条目。",
            )

        patch["adds"].extend(accepted)
        try:
            _append_upload_journal(root, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "action": "patch_ingest",
                "filename": f"patch:{aid}",
                "record_count": len(accepted),
                "sha256": hashlib.sha256(
                    json.dumps([_content_digest(r) for r in accepted]).encode("utf-8")
                ).hexdigest(),
                "note": note,
                "form_source": form_source or "",
            })
        except OSError as exc:
            raise PatchError(
                "journal_failed",
                f"摄取流水账写不进去（{exc}）——没有写入；请检查 .userdata 目录可写性。",
            ) from exc
        _save_patch(root, patch)

    batch = f"patch_{time.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
    return UploadResult(
        filename=batch,
        saved_to="我的补丁包（仅本账户可见）",
        record_count=len(accepted),
        sources=_count_sources(accepted, fallback_source),
        warnings=_skipped_warnings(skipped_dup, skipped_collision)
        + _ingest_warnings(missing_name, unknown_species),
    )


def _count_sources(records: list[dict], fallback: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in records:
        src = str(r.get("source") or "").strip() or fallback
        counts[src] = counts.get(src, 0) + 1
    return counts


def _skipped_warnings(skipped_dup: int, skipped_collision: int) -> list[str]:
    out: list[str] = []
    if skipped_dup:
        out.append(f"{skipped_dup} 条因 dataset_uid 已在你的补丁包中而跳过（去重）。")
    if skipped_collision:
        out.append(
            f"{skipped_collision} 条因 dataset_uid 与基线/共享库已有记录撞号而未收"
            "（同号会让按编号定位无法区分；如确需覆盖，请改用一个未占用的编号后再导入）。"
        )
    return out


def block_uids(project_root: Path, account_id: str, uids: list[str]) -> dict:
    """把 uid 加入屏蔽列表（该账户视图隐藏基线/共享库记录；共享内容零改动）。幂等。"""
    root = Path(project_root)
    aid = _validate_account_id(account_id)
    path = patch_path_for(root, aid)
    targets = [str(u).strip() for u in uids if str(u or "").strip()]
    with _lock_for(path):
        patch = load_patch(root, aid)
        # 若该 uid 在本人 adds 里，删除语义应是「删掉自己的新增」——调用方（corpus_curation）
        # 已先走 trash_adds；这里兜底：adds 里的 uid 不进 blocks（blocks 只针对共享层）。
        own = {record_uid(r) for r in patch["adds"] if record_uid(r)}
        existing = set(patch["blocks"])
        added = [u for u in targets if u not in existing and u not in own]
        if added:
            patch["blocks"].extend(added)
            _save_patch(root, patch)
    return {"blocked": added, "already": [u for u in targets if u in existing],
            "own_adds": [u for u in targets if u in own]}


def unblock_uids(project_root: Path, account_id: str, uids: list[str]) -> dict:
    """把 uid 移出屏蔽列表（恢复基线/共享库记录在该账户视图的可见性）。幂等。"""
    root = Path(project_root)
    aid = _validate_account_id(account_id)
    path = patch_path_for(root, aid)
    targets = {str(u).strip() for u in uids if str(u or "").strip()}
    with _lock_for(path):
        patch = load_patch(root, aid)
        before = list(patch["blocks"])
        patch["blocks"] = [u for u in before if u not in targets]
        removed = [u for u in before if u in targets]
        if removed:
            _save_patch(root, patch)
    return {"unblocked": removed, "not_blocked": sorted(targets - set(removed))}


def trash_adds(project_root: Path, account_id: str, uids: list[str]) -> dict:
    """把本人 adds 里的记录移入 trash（回收站式可逆删除）。返回 moved / not_found。"""
    root = Path(project_root)
    aid = _validate_account_id(account_id)
    path = patch_path_for(root, aid)
    targets = {str(u).strip() for u in uids if str(u or "").strip()}
    with _lock_for(path):
        patch = load_patch(root, aid)
        moved: list[str] = []
        keep: list[dict] = []
        for r in patch["adds"]:
            uid = record_uid(r)
            if uid and uid in targets:
                moved.append(uid)
                patch["trash"].append(r)
            else:
                keep.append(r)
        if moved:
            patch["adds"] = keep
            _save_patch(root, patch)
    return {"moved": moved, "not_found": sorted(targets - set(moved))}


def restore_adds(project_root: Path, account_id: str, uids: list[str]) -> dict:
    """把 trash 里的记录放回 adds（恢复删除）。uid 当前已在 adds → 跳过（防双重存在）。"""
    root = Path(project_root)
    aid = _validate_account_id(account_id)
    path = patch_path_for(root, aid)
    targets = {str(u).strip() for u in uids if str(u or "").strip()}
    with _lock_for(path):
        patch = load_patch(root, aid)
        current = {record_uid(r) for r in patch["adds"] if record_uid(r)}
        restored: list[str] = []
        keep: list[dict] = []
        for r in patch["trash"]:
            uid = record_uid(r)
            if uid and uid in targets and uid not in current:
                restored.append(uid)
                patch["adds"].append(r)
            else:
                keep.append(r)
        if restored:
            patch["trash"] = keep
            _save_patch(root, patch)
    return {"restored": restored, "not_found": sorted(targets - set(restored) - current),
            "already_present": sorted(targets & current)}


def summarize_patch(project_root: Path, account_id: str) -> dict:
    """补丁包清单视图（curate.list 的账户化形态用）。纯只读。"""
    patch = load_patch(Path(project_root), account_id)
    return {
        "account_id": patch["account_id"],
        "updated_at": patch.get("updated_at", 0.0),
        "adds": list(patch["adds"]),
        "blocks": list(patch["blocks"]),
        "trash_count": len(patch["trash"]),
        "trash_uids": [record_uid(r) for r in patch["trash"] if record_uid(r)],
    }
