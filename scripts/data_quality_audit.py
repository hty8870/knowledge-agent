# -*- coding: utf-8 -*-
"""数据内容一致性审计（只读、纯本地、不联网）。

与 `audit_metadata_coverage.py`（字段**覆盖**率）互补：本审计查**内容一致性**——
  1. 标注字段 vs 描述文本 不一致（如某标注 Mouse 的肾数据集，描述文本出现 Human Kidney）；
  2. n_files=0（无文件级清单，多为发现层外部来源）。

**只读**：绝不修改任何数据（`database/base/` 是冻结 767，红线禁改；external 是离线快照）。
仅把可疑记录量化成报告，供人工决定是否清洗来源；MCP `recommend_datasets` 候选也会内联同款
内容一致性 caveat（复用 `data_quality.record_caveats`），本脚本是其全库汇总版。

用法：  py scripts/data_quality_audit.py
输出：  eval/data_quality_audit.md  +  eval/data_quality_audit.json（stdout 同步打印）
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))

from dataset_recommender.corpus import downloads
from dataset_recommender.domains.biodata import vocabulary  # noqa: E402
from dataset_recommender.corpus.corpus import load_full_corpus, source_of  # noqa: E402
from dataset_recommender.corpus.data_quality import DIMS_CHECKED, field_description_conflict  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402

DATA_DIR = AGENT_ROOT / "database" / "base"
PROJECT_ROOT = AGENT_ROOT
OUT_JSON = AGENT_ROOT / "eval" / "data_quality_audit.json"
OUT_MD = AGENT_ROOT / "eval" / "data_quality_audit.md"


def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def _uid(rec: DatasetRecord) -> str:
    raw = rec.raw if isinstance(rec.raw, dict) else {}
    return str(raw.get("dataset_uid") or "").strip()


def main() -> int:
    recs = load_full_corpus(DATA_DIR, PROJECT_ROOT)
    by_source: dict[str, list[DatasetRecord]] = defaultdict(list)
    for r in recs:
        by_source[source_of(r)].append(r)
    order = sorted(by_source, key=lambda s: (s != "10x Genomics", -len(by_source[s])))

    conflicts: list[dict] = []
    per_source: dict[str, dict] = {}
    for src in order:
        rows = by_source[src]
        n = len(rows)
        dim_conflict = Counter()
        n_files_zero = 0
        for r in rows:
            if not downloads.file_count(_uid(r)):
                n_files_zero += 1
            for dim in DIMS_CHECKED:
                c = field_description_conflict(r, dim, vocabulary.CATALOG)
                if c:
                    dim_conflict[dim] += 1
                    conflicts.append({
                        "source": src,
                        "dataset_name": r.dataset_name,
                        "dataset_uid": _uid(r),
                        "dim": dim,
                        "field": c["field"],
                        "description_mentions": c["description_mentions"],
                    })
        per_source[src] = {
            "n": n,
            "conflicts": {d: dim_conflict[d] for d in DIMS_CHECKED},
            "conflict_records": len({(c["dataset_name"], c["source"]) for c in conflicts if c["source"] == src}),
            "n_files_zero": n_files_zero,
            "n_files_zero_rate": _pct(n_files_zero, n),
        }

    report = {
        "total_records": len(recs),
        "total_conflicts": len(conflicts),
        "per_source": per_source,
        "conflicts": conflicts,
    }
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    json.dump(report, open(OUT_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    md: list[str] = []
    md.append("# 数据内容一致性审计\n")
    md.append(f"> 只读、纯本地。语料合计 **{report['total_records']}** 条；"
              f"字段/描述不一致命中 **{report['total_conflicts']}** 处。\n")
    md.append("> 用途：定位需人工核对的可疑记录（标注可能有误，或跨物种/组织研究）与无文件级清单记录。"
              "本审计**不改任何数据**。\n")
    md.append("\n## 逐源汇总\n")
    md.append("| 来源 | n | 物种冲突 | 组织冲突 | 冲突记录数 | n_files=0 | n_files=0 率% |")
    md.append("|---|---|---|---|---|---|---|")
    for src in order:
        e = per_source[src]
        md.append(f"| {src} | {e['n']} | {e['conflicts']['species']} | {e['conflicts']['tissue']} "
                  f"| {e['conflict_records']} | {e['n_files_zero']} | {e['n_files_zero_rate']} |")

    md.append("\n## 字段 vs 描述 不一致明细（前 50）\n")
    if conflicts:
        md.append("| 来源 | 数据集 | 维度 | 标注 | 描述文本疑似提到 |")
        md.append("|---|---|---|---|---|")
        for c in conflicts[:50]:
            name = (c["dataset_name"] or "")[:44]
            ment = "、".join(c["description_mentions"])
            md.append(f"| {c['source']} | {name} | {c['dim']} | {c['field']} | {ment} |")
        if len(conflicts) > 50:
            md.append(f"\n> 其余 {len(conflicts) - 50} 处见 {OUT_JSON.name}。\n")
    else:
        md.append("（无字段/描述不一致命中。）\n")

    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md))
    print(f"\n-> {OUT_JSON}\n-> {OUT_MD}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass
    raise SystemExit(main())
