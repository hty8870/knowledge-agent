# -*- coding: utf-8 -*-
"""快照 diff = 存活变化报告：比较活台账两帧，回答「自上次以来谁死了 / 谁恢复了 / 谁大小变了」。

这是「活台账」相对「一次性快照」的核心增量：把 append-only 的两帧内容寻址快照对齐，逐文件比较
reachable / size 档，报告状态迁移。纯本地、只读、离线。

用法：
  py scripts/diff_snapshots.py                 # 自动取 snapshots/ 里最新两帧（旧→新）
  py scripts/diff_snapshots.py OLD.jsonl NEW.jsonl
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_AGENT = Path(__file__).resolve().parent.parent
_SNAP_DIR = _AGENT / "src" / "dataset_recommender" / "data" / "inspection" / "snapshots"


def load_snap(path: Path) -> "tuple[dict, dict]":
    meta: dict = {}
    rows: dict[str, dict] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if "_meta" in r:
                meta = r["_meta"]
            else:
                rows[r["k"]] = r
    return meta, rows


def _problem(v: dict) -> bool:
    return v.get("r") == "dead" or v.get("s") == "mismatch"


def auto_pick() -> "tuple[Path, Path] | None":
    snaps = sorted(_SNAP_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    if len(snaps) < 2:
        return None
    return snaps[-2], snaps[-1]


def diff(old_path: Path, new_path: Path) -> int:
    ometa, old = load_snap(old_path)
    nmeta, new = load_snap(new_path)

    newly_dead, recovered, newly_mismatch, resolved_mismatch, appeared, disappeared = [], [], [], [], [], []
    inconclusive = []   # 问题腿转 unknown（dead→unknown / mismatch→unknown）：problem 计数下降，但属「本帧未测到/不结论」，**非确认修复**。
    for k in set(old) | set(new):
        o, n = old.get(k), new.get(k)
        if o is None:
            appeared.append(k)
            continue
        if n is None:
            disappeared.append(k)
            continue
        if o.get("r") != "dead" and n.get("r") == "dead":
            newly_dead.append(k)
        if o.get("r") == "dead" and n.get("r") == "ok":
            recovered.append(k)
        if o.get("s") != "mismatch" and n.get("s") == "mismatch":
            newly_mismatch.append(k)
        if o.get("s") == "mismatch" and n.get("s") == "match":
            resolved_mismatch.append(k)
        # 诚实核对：dead→unknown / mismatch→unknown 会让 problem 计数掉，但那是「没复测到」不是「修好了」。
        # 单列此桶，让六桶增量与头条净变化对得上，避免头条虚报「问题变少」。
        if (o.get("r") == "dead" and n.get("r") == "unknown") or (o.get("s") == "mismatch" and n.get("s") == "unknown"):
            inconclusive.append(k)

    prob_o = sum(_problem(v) for v in old.values())
    prob_n = sum(_problem(v) for v in new.values())

    def _blk(title, keys):
        print(f"\n{title}: {len(keys)}")
        for k in sorted(keys)[:20]:
            uid, url = k.split("\t", 1)
            print(f"  - {uid} / {url.rsplit('/', 1)[-1]}")

    print("== 存活变化报告 ==")
    print(f"旧帧 {old_path.name}  ({ometa.get('snapshot_date', '?')}, source={ometa.get('source', '?')}, 文件 {len(old)}, problem {prob_o})")
    print(f"新帧 {new_path.name}  ({nmeta.get('snapshot_date', '?')}, source={nmeta.get('source', '?')}, 文件 {len(new)}, problem {prob_n})")
    print(f"problem 净变化：{prob_n - prob_o:+d}（{prob_o} → {prob_n}）")
    if inconclusive:
        print(f"  ⚠ 其中 {len(inconclusive)} 条属「问题腿转 unknown（本帧未复测到/不结论）」——"
              f"problem 计数下降**不等于确认修复**，链接可能仍坏，只是这帧没测到。见下「转 unknown」桶。")
    _blk("新失效 newly_dead (ok/unknown → dead)", newly_dead)
    _blk("已恢复 recovered (dead → ok，确认修复)", recovered)
    _blk("新大小不一致 newly_size_mismatch", newly_mismatch)
    _blk("大小已修复 resolved_size_mismatch (mismatch → match，确认修复)", resolved_mismatch)
    _blk("转 unknown / 不结论 became_inconclusive (dead→unknown 或 mismatch→unknown，非确认修复)", inconclusive)
    _blk("新增文件 appeared", appeared)
    _blk("消失文件 disappeared", disappeared)
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) == 2:
        old_p, new_p = Path(args[0]), Path(args[1])
    elif not args:
        picked = auto_pick()
        if not picked:
            print(f"[diff] snapshots/ 里少于两帧，无法 diff。先跑 patrol_links.py 攒第二帧。", file=sys.stderr)
            return 2
        old_p, new_p = picked
    else:
        print("用法：py scripts/diff_snapshots.py [OLD.jsonl NEW.jsonl]（无参=自动取最新两帧）", file=sys.stderr)
        return 2
    if not old_p.exists() or not new_p.exists():
        print(f"[diff] 快照文件不存在：{old_p if not old_p.exists() else new_p}", file=sys.stderr)
        return 2
    return diff(old_p, new_p)


if __name__ == "__main__":
    raise SystemExit(main())
