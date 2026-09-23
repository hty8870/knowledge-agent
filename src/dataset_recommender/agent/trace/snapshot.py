# -*- coding: utf-8 -*-
"""mutating curate 操作的**文件级快照存储**。

快照回答「这次写操作动了哪些文件、之前长什么样」——rollback 的锚：
- `capture`：watch 目录（恒 `database/external/`，base 红线结构性不可达）的
  inventory（name → {sha256, size}）+ `preimage_paths` 点名文件的完整字节
  （remove/restore 类动词的目标在 slots 里；create 类动词传空——新文件回退
  不需要 preimage）。
- `finalize`（操作后）：重扫 + diff 出 created/modified/deleted 三清单写回 meta。
- meta.json 与 preimage 字节落 `database/trace/snapshots/<id>/`（.gitignore 已覆盖）。

无 preimage 的 modified/deleted 文件如实记 `preimage_missing`——rollback 对它
fail-closed 拒动（宁可少退不毁数据）。
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from ...app.runtime_paths import instance_data_dir_for
from .recorder import trace_root

__all__ = ["SnapshotStore", "SnapshotError", "snapshot_store"]

#: watch 目录（相对项目根）。红线：只许 database/external/——base 冻结基准结构性不可达
#: （与 corpus_curation 同一红线）；回收站目录不 watch（它本身是 undo 的载体）。
WATCH_DIR_REL = Path("database") / "external"

_SNAPSHOTS_DIR_NAME = "snapshots"


class SnapshotError(ValueError):
    """快照 id 不存在 / meta 损坏 / 文件名非法（含路径分隔符、..）。"""


def _safe_name(name: Any) -> str:
    """external 文件名校验：只接受裸文件名（拒绝分隔符与 ..——防穿越，与
    corpus_curation._leaf_name 同纪律）。"""
    s = str(name or "").strip()
    if not s or "/" in s or "\\" in s or ".." in s:
        raise SnapshotError(f"非法文件名：{s!r}")
    return s


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inventory(watch_dir: Path) -> dict[str, dict[str, Any]]:
    """目录现状：{name: {sha256, size}}，只收文件（跳过子目录与不可读项——坏项如实缺席，
    不静默当不存在：读不了的文件不列，diff 时就会显成 deleted，逼出真相）。"""
    inv: dict[str, dict[str, Any]] = {}
    if not watch_dir.is_dir():
        return inv
    for p in sorted(watch_dir.iterdir()):
        if not p.is_file():
            continue
        try:
            data = p.read_bytes()
        except OSError:
            continue
        inv[p.name] = {"sha256": _sha256_bytes(data), "size": len(data)}
    return inv


def _diff(before: dict, after: dict) -> dict[str, list]:
    """inventory diff → created/modified/deleted（各元素携带 after/before 侧的哈希与大小）。"""
    created = [{"name": n, **after[n]} for n in sorted(after) if n not in before]
    modified = [{"name": n, "before": before[n], "after": after[n]}
                for n in sorted(after) if n in before and after[n]["sha256"] != before[n]["sha256"]]
    deleted = [{"name": n, **before[n]} for n in sorted(before) if n not in after]
    return {"created": created, "modified": modified, "deleted": deleted}


class SnapshotStore:
    """快照存储（每项目根一个实例即可；方法自带文件锁之外的天然幂等——id 唯一）。"""

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root)
        self.dir = trace_root(self.project_root) / _SNAPSHOTS_DIR_NAME
        # watch 目录 = external **用户层**（写侧唯一目录；source/portable 下与项目根
        # database/external 同目录，历史逐字节一致）。官方快照在 shipped 层、只读、不 watch。
        self.watch_dir = instance_data_dir_for(self.project_root, "database/external")

    def _snap_dir(self, snapshot_id: str) -> Path:
        sid = _safe_name(snapshot_id)
        d = self.dir / sid
        if not (d / "meta.json").is_file():
            raise SnapshotError(f"快照不存在：{sid}")
        return d

    def capture(self, verb: str, *, preimage_paths: list[str] | tuple = ()) -> str:
        """操作前调用：落 inventory + 点名文件 preimage 字节，返回 snapshot_id。"""
        sid = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        d = self.dir / sid
        before_dir = d / "before"
        before_dir.mkdir(parents=True, exist_ok=False)
        preimages: list[str] = []
        for name in preimage_paths:
            safe = _safe_name(name)
            src = self.watch_dir / safe
            if not src.is_file():
                raise SnapshotError(f"preimage 目标不存在：{safe}")
            (before_dir / safe).write_bytes(src.read_bytes())
            preimages.append(safe)
        meta = {
            "snapshot_id": sid,
            "verb": str(verb or ""),
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "watch_dir": str(WATCH_DIR_REL).replace("\\", "/"),
            "inventory": _inventory(self.watch_dir),
            "preimages": sorted(preimages),
            "finalized": False,
        }
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return sid

    def load(self, snapshot_id: str) -> dict[str, Any]:
        d = self._snap_dir(snapshot_id)
        try:
            return json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"快照 meta 损坏：{snapshot_id}（{exc}）") from exc

    def save_meta(self, snapshot_id: str, meta: dict) -> None:
        d = self._snap_dir(snapshot_id)
        (d / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def finalize(self, snapshot_id: str) -> dict[str, list]:
        """操作后调用：diff 出 created/modified/deleted 写回 meta 并返回（diff 结果同时
        供 state_snapshot 事件载荷——同一真源，不两份）。"""
        meta = self.load(snapshot_id)
        diff = _diff(meta.get("inventory") or {}, _inventory(self.watch_dir))
        preimages = set(meta.get("preimages") or [])
        meta["finalized"] = True
        meta["finalized_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        meta["diff"] = diff
        # fail-closed 口径：被改/被删却没有 preimage 字节的文件，rollback 拒动并如实列出。
        meta["preimage_missing"] = sorted(
            ({e["name"] for e in diff["modified"]} | {e["name"] for e in diff["deleted"]})
            - preimages)
        self.save_meta(snapshot_id, meta)
        return diff

    def preimage_path(self, snapshot_id: str, name: str) -> Path:
        return self._snap_dir(snapshot_id) / "before" / _safe_name(name)

    def list_snapshots(self) -> list[dict[str, Any]]:
        """全部快照的轻量清单（id/verb/时间/是否已定稿；meta 损坏的如实带 error 键）。"""
        out: list[dict[str, Any]] = []
        if not self.dir.is_dir():
            return out
        for d in sorted(self.dir.iterdir()):
            if not d.is_dir():
                continue
            try:
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                out.append({"snapshot_id": d.name, "verb": meta.get("verb"),
                            "captured_at": meta.get("captured_at"),
                            "finalized": bool(meta.get("finalized")),
                            "rolled_back_at": meta.get("rolled_back_at")})
            except (OSError, json.JSONDecodeError) as exc:
                out.append({"snapshot_id": d.name, "error": type(exc).__name__})
        return out


def snapshot_store(project_root: Path) -> SnapshotStore:
    return SnapshotStore(project_root)
