# -*- coding: utf-8 -*-
"""provisioning success rate 报告：把活台账（current.json）聚合成头号指标，写进 eval/。

沿用 `audit_metadata_coverage.py` 的形态：纯本地、只读、离线，产出 `eval/provisioning_report.{md,json}`。
两个层级（「provisioning success rate」分文件级 + 数据集级，别混成一个数）：
  文件级：15,119 个文件里 reachable/size/integrity/load 各档占比、problem 占比。
  数据集级：一个数据集只要有 1 个问题文件即算「未全绿」（用户感知是数据集级）；clean 占比 = 头号指标。

problem 派生口径（与 inspection.py `_derive` 单点规则一致，报告按向量现算、不盲信 totals 的物化计数）：
  problem := reachable==dead 或 size==mismatch 或 integrity==mismatch 或 load==failed（unknown 不算 problem）。
additive：integrity/load 两维改读台账真实档位（`"i"`/`"l"` 槽，由 provision / load_smoke
回写落盘），给 verified/loaded 率（分母=已实测数）与实测覆盖率；unknown 如实计为「未测」而非失败。
用法：py scripts/report_provisioning.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

_AGENT = Path(__file__).resolve().parent.parent
_CURRENT = _AGENT / "src" / "dataset_recommender" / "data" / "inspection" / "current.json"
_EVAL = _AGENT / "eval"

_PROBLEM_RULE = ("reachable==dead 或 size==mismatch 或 integrity==mismatch 或 load==failed"
                 "（unknown 不算 problem）")


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 2) if d else 0.0


def _is_problem(v: dict) -> bool:
    """与 inspection.py `_derive` 同口径的 problem 判定（unknown 不算 problem）。"""
    return ((v.get("r") == "dead") or (v.get("s") == "mismatch")
            or (v.get("i") == "mismatch") or (v.get("l") == "failed"))


def build() -> int:
    if not _CURRENT.exists():
        print(f"[report] 活台账缺失：{_CURRENT}\n       该文件应随仓库分发；缺失时无法出报告。", file=sys.stderr)
        return 2
    cur = json.loads(_CURRENT.read_text(encoding="utf-8"))
    by_uid = cur.get("by_uid", {})
    totals = cur.get("totals", {})

    n_files = totals.get("files", 0)
    n_ds = len(by_uid)

    # 逐文件现算：problem（含 integrity/load 维）与两维实测计数。problem/reason 本就不落盘、
    # 由读取侧派生（inspection.py 同规则），报告沿用这一读取侧派生，避免物化计数滞后于 "i"/"l" 回写。
    n_problem = 0
    integ = {"verified": 0, "mismatch": 0, "unknown": 0}
    load = {"loaded": 0, "failed": 0, "unknown": 0}
    ds_problem: dict[str, tuple[int, int]] = {}   # uid -> (n_files, n_problem)
    for uid, r in by_uid.items():
        nf = npb = 0
        for v in (r.get("f") or {}).values():
            nf += 1
            integ[v.get("i") if v.get("i") in ("verified", "mismatch") else "unknown"] += 1
            load[v.get("l") if v.get("l") in ("loaded", "failed") else "unknown"] += 1
            if _is_problem(v):
                npb += 1
        n_problem += npb
        ds_problem[uid] = (nf, npb)

    clean_ds = sum(1 for nf, npb in ds_problem.values() if npb == 0)
    degraded = sorted(
        ({"uid": u, "n_files": nf, "n_problem": npb}
         for u, (nf, npb) in ds_problem.items() if npb > 0),
        key=lambda x: (-x["n_problem"], -x["n_files"]),
    )

    integ_measured = integ["verified"] + integ["mismatch"]
    load_measured = load["loaded"] + load["failed"]
    integrity_block = {
        **integ,
        "measured": integ_measured,
        "verified_rate": _pct(integ["verified"], integ_measured),   # 分母=已实测数
        "coverage": _pct(integ_measured, n_files),                  # 实测覆盖率（占全部文件）
    }
    load_block = {
        **load,
        "measured": load_measured,
        "loaded_rate": _pct(load["loaded"], load_measured),
        "coverage": _pct(load_measured, n_files),
    }

    report = {
        "schema": "biodata-provisioning/v0",
        "snapshot_id": cur.get("snapshot_id"),
        "snapshot_date": cur.get("snapshot_date"),
        "source": cur.get("source"),
        "file_level": {
            "n_files": n_files,
            "reachable": {"ok": totals.get("reach_ok", 0), "dead": totals.get("reach_dead", 0),
                          "unknown": totals.get("reach_unknown", 0),
                          "ok_rate": _pct(totals.get("reach_ok", 0), n_files)},
            "size": {"match": totals.get("size_match", 0), "mismatch": totals.get("size_mismatch", 0),
                     "unknown": totals.get("size_unknown", 0),
                     "match_rate": _pct(totals.get("size_match", 0), n_files)},
            "integrity": integrity_block,
            "load": load_block,
            # 兼容旧键（v0 起恒 0.0 的占位）：现镜像真实实测率；未实测（measured=0）时仍为 0.0。
            "integrity_verified_rate": integrity_block["verified_rate"],
            "load_verified_rate": load_block["loaded_rate"],
            "problem": n_problem,
            "problem_rate": _pct(n_problem, n_files),
            # 口径说明：problem 自 起可能来自 integrity/load 两维（不只 reachable/size）。
            "problem_rule": _PROBLEM_RULE,
        },
        "dataset_level": {
            "n_datasets": n_ds,
            "clean": clean_ds,
            "degraded": n_ds - clean_ds,
            # 头号指标（数据集级 provisioning success rate）：所有文件均无 problem 的数据集占比。
            "provisioning_success_rate": _pct(clean_ds, n_ds),
        },
        "degraded_datasets": degraded,
    }

    _EVAL.mkdir(parents=True, exist_ok=True)
    (_EVAL / "provisioning_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    ib, lb = integrity_block, load_block
    fl, dl = report["file_level"], report["dataset_level"]
    md = [
        "# Provisioning 成功率报告\n",
        f"- 快照：**{report['snapshot_date']}**（id `{report['snapshot_id']}`，source={report['source']}）",
        f"- 数据来源：活台账 `data/inspection/current.json`（离线、只读）",
        "",
        "## 头号指标 · 数据集级 provisioning success rate",
        f"**{dl['provisioning_success_rate']}%**　（{dl['clean']}/{dl['n_datasets']} 个数据集全部文件无 problem）",
        f"- 存在问题文件的数据集：**{dl['degraded']}** 个",
        f"- 口径：problem 判定含 integrity/load 两维（{_PROBLEM_RULE}）；当前 integrity 实测覆盖 "
        f"{ib['coverage']}%（{ib['measured']}/{fl['n_files']}）、load 实测覆盖 {lb['coverage']}%（{lb['measured']}/{fl['n_files']}）",
        "",
        "## 文件级明细（共 %d 个文件）" % fl["n_files"],
        "| 维度 | 通过 | 说明 |",
        "|---|---|---|",
        f"| reachable | {fl['reachable']['ok_rate']}% | ok {fl['reachable']['ok']} / dead {fl['reachable']['dead']} / unknown {fl['reachable']['unknown']}（HTTP 200/206 存活） |",
        f"| size 一致 | {fl['size']['match_rate']}% | match {fl['size']['match']} / mismatch {fl['size']['mismatch']} / unknown {fl['size']['unknown']}（服务器大小 vs 记录） |",
        f"| integrity(md5) | {ib['verified_rate']}%（实测 {ib['measured']} 个） | verified {ib['verified']} / mismatch {ib['mismatch']} / unknown {ib['unknown']}；实测覆盖率 {ib['coverage']}%（unknown≠失败，是未测） |",
        f"| load | {lb['loaded_rate']}%（实测 {lb['measured']} 个） | loaded {lb['loaded']} / failed {lb['failed']} / unknown {lb['unknown']}；实测覆盖率 {lb['coverage']}%（unknown≠失败，是未测） |",
        f"| **problem** | — | **{fl['problem']} 个**（{fl['problem_rate']}%）＝ dead 或 size-mismatch 或 integrity-mismatch 或 load-failed（unknown 不算） |",
        "",
        "> 诚实边界：integrity/load 的 unknown 是「未测」不是「失败」，不计入 problem；problem 由 reachable/size/integrity/load 四维的已实测档判定。",
    ]
    if degraded:
        md += ["", f"## 问题数据集清单（{len(degraded)} 个，前 40）",
               "| dataset_uid | 文件数 | 问题数 |", "|---|---|---|"]
        for d in degraded[:40]:
            md.append(f"| {d['uid']} | {d['n_files']} | {d['n_problem']} |")
    (_EVAL / "provisioning_report.md").write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"[report] 数据集级 provisioning success rate = {dl['provisioning_success_rate']}% "
          f"（{dl['clean']}/{dl['n_datasets']}）| 问题文件 {fl['problem']}/{fl['n_files']} | "
          f"integrity 实测 {ib['measured']}（verified {ib['verified_rate']}%）| "
          f"load 实测 {lb['measured']}（loaded {lb['loaded_rate']}%）")
    print(f"[report] -> eval/provisioning_report.json\n[report] -> eval/provisioning_report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
