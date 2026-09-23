# -*- coding: utf-8 -*-
"""按 trace 快照回退 mutating curate 操作 + CLI。

回退语义（对快照 finalize 出的三清单）：
- created 里仍存在的文件 → **移入回收站**（`.userdata/recycle/<timestamp>_<name>` +
  manifest 行，action="trace_rollback"）——与 curate.remove 同一「移动而非删除」纪律，
  回退删除也绝不真删字节；
- modified / deleted 且有 preimage 字节 → 写回原字节；
- modified / deleted 但**无 preimage → fail-closed 拒动**，如实列入 unrestorable
  （宁可少退不毁数据）；
- 已不在快照所述状态的文件（如已被人手工动过）如实列入 skipped，不强行覆盖。

默认 **dry-run** 只出计划；`--apply` 才动手。执行结果写回快照 meta：每次都记
rollback_attempted_at；只有本次确有恢复且无 unrestorable/errors 才记 rolled_back_at。
未完整回滚保持未完成态，供上层 fail-closed 挡住更早快照，不伪装成「已回过」。

CLI：
  PYTHONPATH=src python -m dataset_recommender.agent.trace.rollback --snapshot <id> [--root .]
  PYTHONPATH=src python -m dataset_recommender.agent.trace.rollback --snapshot <id> --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from ...app.runtime_paths import instance_data_dir_for
from ...corpus.corpus import EXTERNAL_DIR_NAME, invalidate_external_cache
from ...corpus.corpus_curation import _append_jsonl, _now_iso, _recycle_dir, _recycle_manifest
from ...corpus.uploads import ingest_critical_section
from .recorder import default_project_root
from .snapshot import SnapshotError, SnapshotStore

__all__ = ["plan_rollback", "apply_rollback", "main"]


def _plan(root: Path, store: SnapshotStore, meta: dict) -> dict[str, Any]:
    """回退计划（dry-run 与 apply 共用同一真源——apply 不做计划外的事）。"""
    diff = meta.get("diff") or {}
    watch = store.watch_dir
    missing = set(meta.get("preimage_missing") or [])
    plan: dict[str, Any] = {
        "snapshot_id": meta.get("snapshot_id"),
        "verb": meta.get("verb"),
        "finalized": bool(meta.get("finalized")),
        "already_rolled_back_at": meta.get("rolled_back_at"),
        "recycle_created": [],   # 将移入回收站的新文件
        "restore_bytes": [],     # 将用 preimage 字节恢复的文件（modified/deleted）
        "skipped": [],           # 现状已与快照口径不符（如实，不动）
        "unrestorable": [],      # 无 preimage 的 modified/deleted（fail-closed，拒动）
    }
    for entry in diff.get("created") or []:
        name = entry["name"]
        target = watch / name
        if not target.is_file():
            plan["skipped"].append({"name": name, "reason": "created 文件已不存在（可能已被移除）"})
        else:
            plan["recycle_created"].append(name)
    for entry in diff.get("modified") or []:
        name = entry["name"]
        if name in missing:
            plan["unrestorable"].append({"name": name, "reason": "modified 且无 preimage 字节"})
            continue
        target = watch / name
        if not target.is_file():
            plan["skipped"].append({"name": name, "reason": "modified 文件现已不存在"})
        else:
            plan["restore_bytes"].append(name)
    for entry in diff.get("deleted") or []:
        name = entry["name"]
        if name in missing:
            plan["unrestorable"].append({"name": name, "reason": "deleted 且无 preimage 字节"})
            continue
        if (watch / name).is_file():
            plan["skipped"].append({"name": name, "reason": "deleted 文件现已存在（不覆盖）"})
        else:
            plan["restore_bytes"].append(name)
    return plan


def plan_rollback(project_root: Path, snapshot_id: str) -> dict[str, Any]:
    """dry-run 计划：纯只读，零副作用。"""
    store = SnapshotStore(Path(project_root))
    meta = store.load(snapshot_id)
    if not meta.get("finalized"):
        raise SnapshotError(f"快照 {snapshot_id} 未 finalize（操作后未 diff），无法回退。")
    return _plan(Path(project_root), store, meta)


def _recycle_one(root: Path, name: str, *, snapshot_id: str) -> str:
    """把 external（用户层）里的一个文件移入回收站（与 corpus_curation.apply_remove 同纪律：
    时间戳前缀防覆盖 + manifest 行；action 如实标 trace_rollback）。返回 recycle_name。"""
    target = instance_data_dir_for(root, EXTERNAL_DIR_NAME) / name
    rec_dir = _recycle_dir(root)
    rec_dir.mkdir(parents=True, exist_ok=True)
    dest = rec_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{name}"
    while dest.exists():  # 同微秒防冲突：绝不覆盖回收站里已有文件
        dest = rec_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{name}"
    shutil.move(str(target), str(dest))
    _append_jsonl(_recycle_manifest(root), {
        "ts": _now_iso(),
        "action": "trace_rollback",
        "original_path": f"{EXTERNAL_DIR_NAME}/{name}",
        "recycle_name": dest.name,
        "snapshot_id": snapshot_id,
    })
    return dest.name


def apply_rollback(project_root: Path, snapshot_id: str) -> dict[str, Any]:
    """真回退：先出计划（与 dry-run 同一函数），再按计划逐项执行；任何单项失败
    如实记入 errors 并继续其余项（部分回退好过零回退，失败项下次可再来）。
    全程住摄取临界区（与 corpus 写路径同一把跨进程锁）。"""
    root = Path(project_root)
    store = SnapshotStore(root)
    meta = store.load(snapshot_id)
    if not meta.get("finalized"):
        raise SnapshotError(f"快照 {snapshot_id} 未 finalize（操作后未 diff），无法回退。")
    plan = _plan(root, store, meta)
    result: dict[str, Any] = {
        "snapshot_id": snapshot_id,
        "dry_run": False,
        "applied": {"recycled": [], "restored": []},
        "skipped": plan["skipped"],
        "unrestorable": plan["unrestorable"],
        "errors": [],
    }
    with ingest_critical_section(root):
        for name in plan["recycle_created"]:
            try:
                recycle_name = _recycle_one(root, name, snapshot_id=snapshot_id)
                result["applied"]["recycled"].append({"name": name, "recycle_name": recycle_name})
            except OSError as exc:
                result["errors"].append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
        for name in plan["restore_bytes"]:
            try:
                data = store.preimage_path(snapshot_id, name).read_bytes()
                (instance_data_dir_for(root, EXTERNAL_DIR_NAME) / name).write_bytes(data)
                result["applied"]["restored"].append({"name": name, "bytes": len(data)})
            except OSError as exc:
                result["errors"].append({"name": name, "error": f"{type(exc).__name__}: {exc}"})
    invalidate_external_cache()
    # 诚实状态：尝试过 ≠ 已回滚。零实际恢复、unrestorable 或 errors 任一存在
    # 都不能写 rolled_back_at，否则 agent 候选闸下次会跳过这一步、越序去动更早快照。
    # 已成功回过的快照二次执行保持 rolled_back_at（幂等复跑不会把成功状态抹掉）。
    attempted_at = _now_iso()
    meta["rollback_attempted_at"] = attempted_at
    applied_count = (len(result["applied"]["recycled"])
                     + len(result["applied"]["restored"]))
    completed_now = applied_count > 0 and not result["unrestorable"] and not result["errors"]
    if completed_now and not meta.get("rolled_back_at"):
        meta["rolled_back_at"] = attempted_at
    result["rolled_back"] = bool(meta.get("rolled_back_at"))
    meta["rollback_result"] = {
        "recycled": [e["name"] for e in result["applied"]["recycled"]],
        "restored": [e["name"] for e in result["applied"]["restored"]],
        "skipped": result["skipped"],
        "unrestorable": result["unrestorable"],
        "errors": result["errors"],
        "completed": completed_now,
    }
    store.save_meta(snapshot_id, meta)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m dataset_recommender.agent.trace.rollback",
        description="按 trace 快照回退 mutating curate 操作（默认 dry-run；--apply 才动手）。")
    parser.add_argument("--root", default=None, help="项目根（缺省自动解析）")
    parser.add_argument("--snapshot", required=True, help="快照 id")
    parser.add_argument("--apply", action="store_true", help="真执行回退（缺省只出计划）")
    args = parser.parse_args(argv)

    root = Path(args.root) if args.root else default_project_root()
    try:
        payload = apply_rollback(root, args.snapshot) if args.apply else plan_rollback(root, args.snapshot)
    except SnapshotError as exc:
        print(f"回退失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    if args.apply:
        n_err = len(payload.get("errors") or [])
        n_bad = len(payload.get("unrestorable") or [])
        if n_err or n_bad:
            print(f"注意：{n_err} 项执行失败、{n_bad} 项无 preimage 被拒动（如实上报，未动手）。",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
