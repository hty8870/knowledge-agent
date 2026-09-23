# -*- coding: utf-8 -*-
"""
多源元数据字段覆盖审计（纯本地、不联网）。

目的：在动手建「逐条件证据核验 / 候选并排比较」之前，先证伪一件事——
现有各来源的数据集级元数据，到底够不够支撑「按条件核验证据」。
如果关键约束维度（species/tissue/disease/platform/assay/has_raw_data/发表年）在某些源根本没有值，
那么针对那些源的逐条件 pass/fail 就是空中楼阁，本审计把它量化成表。

判据（对每个来源、每个字段）：
  - present / absent 率（非空 = 有证据可核）
  - distinct（去重取值数，粗判「受控词表 vs 自由文本」）
  - has_raw_data 三态（True/False/None）分布
  - description 长度分位（复核「模板化、中位 ~316 字」这一天花板说法是否跨源成立）

用法：  py scripts/audit_metadata_coverage.py
输出：  eval/metadata_coverage.md  +  eval/metadata_coverage.json
"""
from __future__ import annotations

import io
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
except Exception:
    pass

from dataset_recommender.corpus.corpus import load_full_corpus, source_of  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402

DATA_DIR = AGENT_ROOT / "database" / "base"
PROJECT_ROOT = AGENT_ROOT
OUT_JSON = AGENT_ROOT / "eval" / "metadata_coverage.json"
OUT_MD = AGENT_ROOT / "eval" / "metadata_coverage.md"

# 报告下游（证据/比较）真正要核验的硬约束维度 + 辅助维度。
STR_FIELDS = ["species", "tissue", "disease", "chemistry", "platform_family", "assay", "count", "unit"]


def _year(rec: DatasetRecord) -> str:
    raw = rec.raw if isinstance(rec.raw, dict) else {}
    return str(raw.get("published_date", "")).strip()[:4]


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _percentiles(vals: list[int]) -> dict:
    if not vals:
        return {"n": 0}
    s = sorted(vals)

    def q(p: float) -> int:
        if len(s) == 1:
            return s[0]
        idx = min(len(s) - 1, max(0, int(round(p * (len(s) - 1)))))
        return s[idx]

    return {
        "n": len(s),
        "min": s[0],
        "p25": q(0.25),
        "median": int(statistics.median(s)),
        "p75": q(0.75),
        "max": s[-1],
        "mean": int(statistics.mean(s)),
    }


def main() -> None:
    recs = load_full_corpus(DATA_DIR, PROJECT_ROOT)
    by_source: dict[str, list[DatasetRecord]] = defaultdict(list)
    for r in recs:
        by_source[source_of(r)].append(r)

    # 来源排序：10x 置顶，其余按数量降序
    order = sorted(by_source, key=lambda s: (s != "10x Genomics", -len(by_source[s])))

    report: dict = {"total_records": len(recs), "sources": {}}

    for src in order:
        rows = by_source[src]
        n = len(rows)
        entry: dict = {"n": n, "fields": {}}

        for f in STR_FIELDS:
            vals = [(getattr(r, f) or "").strip() for r in rows]
            present = [v for v in vals if v]
            entry["fields"][f] = {
                "present": len(present),
                "present_rate": _pct(len(present), n),
                "distinct": len(set(v.lower() for v in present)),
            }

        # 发表年 present
        years = [_year(r) for r in rows]
        y_present = [y for y in years if y]
        entry["fields"]["published_year"] = {
            "present": len(y_present),
            "present_rate": _pct(len(y_present), n),
            "distinct": len(set(y_present)),
            "range": (f"{min(y_present)}–{max(y_present)}" if y_present else ""),
        }

        # has_raw_data 三态
        tri = Counter()
        for r in rows:
            tri[{True: "true", False: "false", None: "none"}[r.has_raw_data]] += 1
        entry["has_raw_data"] = {
            "true": tri["true"], "false": tri["false"], "none": tri["none"],
            "known_rate": _pct(tri["true"] + tri["false"], n),
        }

        # description 长度分位
        dlens = [len((r.description or "").strip()) for r in rows]
        d_nonempty = [x for x in dlens if x > 0]
        entry["description"] = {
            "present": len(d_nonempty),
            "present_rate": _pct(len(d_nonempty), n),
            "char_len": _percentiles(d_nonempty),
        }
        report["sources"][src] = entry

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    json.dump(report, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # ---------- Markdown ----------
    md: list[str] = []
    md.append("# 多源元数据字段覆盖审计\n")
    md.append(f"> 纯本地快照统计、不联网。语料合计 **{report['total_records']}** 条，{len(report['sources'])} 个来源。\n")
    md.append("> 用途：证伪「逐条件证据核验 / 候选比较」下游对各源是否建得动——"
              "某维度在某源 present 率≈0，则该源上针对该维度的 pass/fail 无证据可核。\n")

    # 硬约束维度覆盖总表（present 率）
    dims = ["species", "tissue", "disease", "platform_family", "assay", "published_year"]
    md.append("\n## 硬约束维度 present 率（%）—— 越低＝该源越无法核验该条件\n")
    header = "| 来源 | n | " + " | ".join(dims) + " | has_raw_data 已知 |"
    md.append(header)
    md.append("|" + "---|" * (len(dims) + 3))
    for src in order:
        e = report["sources"][src]
        cells = [f"{e['fields'][d]['present_rate']}" for d in dims]
        md.append(f"| {src} | {e['n']} | " + " | ".join(cells)
                  + f" | {e['has_raw_data']['known_rate']} |")

    # description 天花板
    md.append("\n## description 长度（字符）分位 —— 复核「模板化、内容稀薄」天花板\n")
    md.append("| 来源 | present% | 中位 | p25 | p75 | max |")
    md.append("|---|---|---|---|---|---|")
    for src in order:
        e = report["sources"][src]
        d = e["description"]
        cl = d["char_len"]
        md.append(f"| {src} | {d['present_rate']} | {cl.get('median','-')} | "
                  f"{cl.get('p25','-')} | {cl.get('p75','-')} | {cl.get('max','-')} |")

    # 逐源明细
    md.append("\n## 逐源字段明细（present / distinct）\n")
    for src in order:
        e = report["sources"][src]
        md.append(f"\n### {src}（{e['n']} 条）\n")
        md.append("| 字段 | present | present% | distinct |")
        md.append("|---|---|---|---|")
        for f in STR_FIELDS + ["published_year"]:
            fd = e["fields"][f]
            md.append(f"| {f} | {fd['present']} | {fd['present_rate']} | {fd['distinct']} |")
        hr = e["has_raw_data"]
        md.append(f"\nhas_raw_data：true={hr['true']} / false={hr['false']} / none={hr['none']}"
                  f"（已知率 {hr['known_rate']}%）\n")

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n-> {OUT_JSON}\n-> {OUT_MD}")


if __name__ == "__main__":
    main()
