#!/usr/bin/env python3
"""聚合多组保真度记录并生成对照表。

每条臂 = 输出根目录（仓库外历史管线目录）下的一个子目录，
默认目录和子目录都可作为记录来源。直接从 capture.jsonl / pairs.jsonl /
aligned.jsonl 重新计数（不解析 report.md，避免被报告文本格式绑死）。

用法：
  python scripts/aggregate_multicall_arms.py [臂名...]     # 缺省：自动发现全部含 capture.jsonl 的臂
产物：multicall-fidelity-probe/cross-model-table.md（不入库）
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))
BASE = ROOT / "research" / "reports" / "multicall-fidelity-probe"

#: 主目录臂的记录早于 record 的 "model" 字段——臂名→展示模型名的兜底映射。
_ARM_MODEL_LABEL = {".": "deepseek-chat"}

#: 与验证分析同口径的只读/写动词划分（report.md 的"分 verb"表用的就是这套名字前缀）。
READONLY_VERBS = ("curate.check_updates", "curate.db_status", "check_updates", "db_status")
WRITE_VERBS = ("curate.search_online", "curate.sync_updates", "search_online", "sync_updates")


def _load(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.open(encoding="utf-8") if l.strip()]


def _arm_dir(name: str) -> Path:
    return BASE if name == "." else BASE / name


def discover() -> list[str]:
    arms = []
    if (BASE / "capture.jsonl").is_file():
        arms.append(".")
    for d in sorted(BASE.iterdir()):
        if d.is_dir() and (d / "capture.jsonl").is_file() and d.name != "sim-selfcheck":
            arms.append(d.name)
    return arms


def arm_metrics(name: str, cases: set[str] | None = None,
                only_rounds: set[int] | None = None) -> dict:
    d = _arm_dir(name)

    def _keep(rec: dict) -> bool:
        if cases is not None and rec.get("case") not in cases:
            return False
        if only_rounds is not None and int(rec.get("round") or 0) not in only_rounds:
            return False
        return True

    runs = [r for r in _load(d / "capture.jsonl") if _keep(r)]
    pairs = [p for p in _load(d / "pairs.jsonl") if _keep(p)]
    aligned = [a for a in _load(d / "aligned.jsonl") if _keep(a)]

    n_runs = len(runs)
    n_exc = sum(1 for r in runs if r.get("exc"))
    steps = sum(len(r.get("executions") or []) for r in runs)
    models = sorted({str(r.get("model") or "?") for r in runs if r.get("model")})
    if not models and name in _ARM_MODEL_LABEL:
        models = [_ARM_MODEL_LABEL[name]]
    # 同一模型不同提示词臂（变体提示词）在模型名上加标注，避免与主臂混淆。
    prompt_arms = {str(r.get("prompt_arm") or "current") for r in runs}
    if prompt_arms == {"variant"}:
        models = [f"{m}（变体提示词）" for m in models]

    # 多调用率与评测报告同口径：分母是"c1 为 loop 动词且被实际采纳"的事件。
    from probe_multicall_fidelity import _LOOP_VERBS, _name_to_verb_union
    n2v = _name_to_verb_union()
    adopted = multi = 0
    batch = Counter()
    for r in runs:
        execs = r.get("executions") or []
        for ev in r.get("events") or []:
            calls = ev.get("calls") or []
            if not calls:
                continue
            v1 = n2v.get(str(calls[0].get("name") or ""))
            if v1 not in _LOOP_VERBS:
                continue
            m = int(ev.get("m") or 0)
            if not (m < len(execs) and execs[m].get("verb") == v1):
                continue
            adopted += 1
            batch[min(len(calls), 6)] += 1
            if len(calls) >= 2:
                multi += 1

    def _rates(items: list[dict]) -> dict:
        c = Counter(i["cls"] for i in items)
        con = c.get("一致", 0) + c.get("无害差异", 0)
        n = len(items)
        cond = c.get("条件错", 0)
        return {"n": n, "con": con, "rate": (con / n * 100) if n else 0.0,
                "param": c.get("参数错", 0), "cond": cond,
                "harmless": c.get("无害差异", 0),
                "loop_ended": sum(1 for i in items if i["cls"] == "条件错"
                                  and str(i.get("sub", "")).startswith("loop_ended")),
                "verb_changed": sum(1 for i in items if i["cls"] == "条件错"
                                    and i.get("sub") == "verb_changed"),
                "skipped": sum(1 for i in items if i.get("kind") == "skipped")}

    pos = _rates(pairs)
    pre_only = [a for a in aligned if a.get("kind") != "inserted"]
    ali = _rates(pre_only)
    # 只读/写分裂用对齐口径（与 DeepSeek 主报告 summary.md 的 96%/45% 头条同口径）。
    ro = _rates([a for a in pre_only if a.get("pre_verb") in READONLY_VERBS])
    wr = _rates([a for a in pre_only if a.get("pre_verb") in WRITE_VERBS])

    by_verb: dict[str, dict] = {}
    for a in pre_only:
        v = a.get("pre_verb") or "?"
        by_verb.setdefault(v, []).append(a)
    verb_rates = {v: _rates(items) for v, items in sorted(by_verb.items())}

    return {"arm": name, "models": models, "runs": n_runs, "exc": n_exc, "steps": steps,
            "events": adopted, "multi": multi,
            "multi_rate": (multi / adopted * 100) if adopted else 0.0,
            "batch": dict(sorted(batch.items())), "pos": pos, "ali": ali,
            "ro": ro, "wr": wr, "verb": verb_rates}


def main() -> int:
    import argparse
    cli = argparse.ArgumentParser()
    cli.add_argument("arms", nargs="*", help="记录目录名（. = 默认目录）；缺省自动发现")
    cli.add_argument("--cases", default=None,
                     help="逗号分隔的用例 id 白名单；对照时用同一子集重算各记录组")
    cli.add_argument("--round", default=None,
                     help="只统计某几轮的记录，逗号分隔；对照时各记录组使用同一轮次")
    args = cli.parse_args()
    names = args.arms or discover()
    cases = {s.strip() for s in args.cases.split(",") if s.strip()} if args.cases else None
    rounds = ({int(s) for s in args.round.split(",") if s.strip()} if args.round else None)
    arms = [arm_metrics(n, cases, rounds) for n in names]
    L: list[str] = ["# 多 tool_call 保真度 · 跨模型对照表", ""]
    if cases:
        L.append(f"> 仅统计 {len(cases)} 个用例的记录（--cases 子集口径）。")
        L.append("")
    if rounds:
        L.append(f"> 仅统计第 {','.join(str(r) for r in sorted(rounds))} 轮（--round 口径）。")
        L.append("")
    L.append("臂 | 模型 | 运行(异常) | 步数 | 多调用率 | 逐位一致率 | 参数错 | 条件错 | "
             "对齐一致率 | 只读一致率 | 写一致率")
    L.append("---|---|---|---|---|---|---|---|---|---|---")
    for a in arms:
        p, al, ro, wr = a["pos"], a["ali"], a["ro"], a["wr"]
        L.append(
            f"{a['arm']} | {','.join(a['models'])} | {a['runs']}({a['exc']}) | {a['steps']} | "
            f"{a['multi_rate']:.0f}%（{a['multi']}/{a['events']}） | "
            f"**{p['rate']:.1f}%**（{p['con']}/{p['n']}） | {p['param']} | {p['cond']} | "
            f"**{al['rate']:.1f}%**（{al['con']}/{al['n']}） | "
            f"{ro['rate']:.0f}%（{ro['con']}/{ro['n']}） | {wr['rate']:.0f}%（{wr['con']}/{wr['n']}）")
    L.append("")
    L.append("> 口径与各臂 report.md 一致：多调用率分母为\"c1 为 loop 动词且被采纳\"的通道事件；"
             "只读/写分裂用对齐口径（check_updates/db_status=只读，search_online/sync_updates=写）。")
    L.append("")
    for a in arms:
        L.append(f"## {a['arm']}（{','.join(a['models'])}）分 verb（对齐口径）")
        L.append("")
        L.append("verb | n | 一致率 | 参数错 | 条件错")
        L.append("---|---|---|---|---")
        for v, r in a["verb"].items():
            L.append(f"{v} | {r['n']} | {r['rate']:.0f}% | {r['param']} | {r['cond']}")
        p = a["pos"]
        L.append(f"\n条件错细分（逐位）：loop_ended {p['loop_ended']}、verb_changed {p['verb_changed']}")
        L.append("")
    suffix = ("-subset" if cases else "") + (f"-r{'_'.join(str(r) for r in sorted(rounds))}" if rounds else "")
    out = BASE / f"cross-model-table{suffix}.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"已写入 {out}")
    print("\n".join(L[:len(names) + 6]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
