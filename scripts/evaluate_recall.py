# -*- coding: utf-8 -*-
"""附加分级评测（内部仪器）：量化「可选向量召回」相对确定性 rule-only 排序的排序质量差异。

**边界**：与官方 `evaluate_recommendation.py` 完全独立；**不改、不替**官方二元评测口径。
衡量「存活集内的排序好坏」（nDCG@5 / MRR@5），不是「推荐对不对」（那由官方脚本裁决）。

**走生产路径**：直接调 `retriever.retrieve(recall_backend=...)`，真正加载并运行 vector_recall 的
本地模型（cross_encoder / dense），因此本脚本的数字就是生产行为。cross_encoder 本地确定 → 可复现。

严谨性：
- IDCG 理想集合 = 检索器**真实可排序的存活集**（passes_hard_filter），而非"全库满足 must_match"。
- MRR 实为 **MRR@5**（只在 top-5 上算），已如实命名。
- dev/test 切分：调参只看 dev；test 冻结配置后只跑一次。Δ 给 **bootstrap 95% CI**（对查询重采样）。
- 全配置断言硬违规==0。

用法：
  set PYTHONIOENCODING=utf-8
  py scripts/evaluate_recall.py                                  # rule vs cross_encoder，扩充集(35)全量
  py scripts/evaluate_recall.py --backend dense --recall-alpha 0.5
  py scripts/evaluate_recall.py --split dev        # 只看 dev（调参）
  py scripts/evaluate_recall.py --split test       # 只看 test（冻结后确认）
  py scripts/evaluate_recall.py --out eval/recall_report.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.llm.config import get_settings                # noqa: E402
from dataset_recommender.corpus.data_loader import load_raw_records        # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_records        # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query            # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever, passes_hard_filter  # noqa: E402
from dataset_recommender.retrieval.vector_recall import load_embedder, load_cross_encoder  # noqa: E402
from evaluate_recommendation import constraint_satisfied, satisfies_must  # noqa: E402

# 分级答案不随公开仓发布（防对可见集过拟合）；用 --queries 指向自建的分级答案文件。
DEFAULT_QUERIES = AGENT_ROOT / "eval" / "eval_recall_graded.json"
# dev/test 切分：test 覆盖多样 facet，冻结后只跑一次；其余为 dev（调参）。
TEST_IDS = {"g02", "g05", "g09", "g11", "g14", "g17", "g20", "g23", "g25", "g27", "g29", "g31", "g34"}


def graded_relevance(record, must, ideal):
    if not satisfies_must(record, must):
        return 0
    return 1 + sum(1 for k, v in ideal.items() if constraint_satisfied(record, k, v))


def dcg_at_k(rels, k):
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels[:k]))


def ndcg_at_k(returned_rels, ideal_rels, k=5):
    idcg = dcg_at_k(sorted(ideal_rels, reverse=True), k)
    return dcg_at_k(returned_rels, k) / idcg if idcg > 0 else 0.0


def mrr_at_5(returned_rels, threshold=2):
    for i, rel in enumerate(returned_rels[:5]):
        if rel >= threshold:
            return 1.0 / (i + 1)
    return 0.0


def run_config(records, settings, queries, recall_backend, recall_alpha, top_k=5, recall_fusion="linear"):
    retriever = DatasetRetriever(top_k=top_k)
    rows, total_violations = [], 0
    for q in queries:
        must, ideal = q.get("must_match", {}), q.get("ideal", {})
        intent = parse_query(q["query"], settings.keyword_mapping)
        results = retriever.retrieve(
            records, intent, top_k=top_k,
            recall_backend=recall_backend, recall_alpha=recall_alpha, recall_fusion=recall_fusion,
        )
        returned_rels = [graded_relevance(c.record, must, ideal) for c in results]
        violations = sum(1 for c in results if not satisfies_must(c.record, must))
        total_violations += violations
        # IDCG 口径修正：理想集合=检索器真实可排序存活集(passes_hard_filter)。
        survivors_rels = [graded_relevance(r, must, ideal) for r in records if passes_hard_filter(r, intent)]
        rows.append({
            "id": q.get("id"), "ndcg": round(ndcg_at_k(returned_rels, survivors_rels, 5), 4),
            "mrr": round(mrr_at_5(returned_rels), 4), "violations": violations,
        })
    return rows, total_violations


def bootstrap_ci(deltas, n=2000, seed=42):
    if not deltas:
        return (0.0, 0.0)
    rnd = random.Random(seed)
    m = len(deltas)
    means = sorted(sum(deltas[rnd.randrange(m)] for _ in range(m)) / m for _ in range(n))
    return (means[int(0.025 * n)], means[int(0.975 * n)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--backend", choices=["cross_encoder", "dense"], default="cross_encoder")
    ap.add_argument("--recall-alpha", type=float, default=0.5, help="仅 dense：稠密分权重")
    ap.add_argument("--fusion", choices=["linear", "rrf"], default="linear",
                    help="仅 dense：词面分×稠密分的融合法（linear=min-max+α；rrf=名次融合 k=60）")
    ap.add_argument("--split", choices=["all", "dev", "test"], default="all")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args()

    settings = get_settings()
    records = normalize_records(load_raw_records(settings.data_dir))
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]
    if args.split == "dev":
        queries = [q for q in queries if q.get("id") not in TEST_IDS]
    elif args.split == "test":
        queries = [q for q in queries if q.get("id") in TEST_IDS]

    ready = (load_cross_encoder() if args.backend == "cross_encoder" else load_embedder()) is not None
    print(f"加载 {len(records)} 条记录；{len(queries)} 条分级查询（split={args.split}）；backend={args.backend}")
    print(f"本地模型就绪={ready}" + ("" if ready else
          "  → 召回将回退为规则顺序（对比全为 tie）。先跑 scripts/fetch_embedding_model.py"))

    base_rows, base_viol = run_config(records, settings, queries, "off", args.recall_alpha, args.top_k)
    rc_rows, rc_viol = run_config(records, settings, queries, args.backend, args.recall_alpha, args.top_k,
                                  recall_fusion=args.fusion)

    print(f"\n============ rule-only vs rule+recall({args.backend}) ============")
    print(f"{'id':5s} {'nDCG@5':>16s}  {'MRR@5':>14s}  win/tie/loss")
    print("-" * 60)
    wins = ties = losses = 0
    bnd = rnd_ = bmr = rmr = 0.0
    dndcg, dmrr = [], []
    for b, r in zip(base_rows, rc_rows):
        d = r["ndcg"] - b["ndcg"]
        tag = "win" if d > 1e-9 else ("loss" if d < -1e-9 else "tie")
        wins += tag == "win"; ties += tag == "tie"; losses += tag == "loss"
        bnd += b["ndcg"]; rnd_ += r["ndcg"]; bmr += b["mrr"]; rmr += r["mrr"]
        dndcg.append(r["ndcg"] - b["ndcg"]); dmrr.append(r["mrr"] - b["mrr"])
        print(f"{b['id']:5s} {b['ndcg']:6.4f}->{r['ndcg']:6.4f}  {b['mrr']:5.3f}->{r['mrr']:5.3f}  {tag}")
    n = len(queries)
    lo, hi = bootstrap_ci(dndcg)
    lom, him = bootstrap_ci(dmrr)
    print("-" * 60)
    print(f"平均 nDCG@5: {bnd/n:.4f} → {rnd_/n:.4f}  (Δ {(rnd_-bnd)/n:+.4f}, 95%CI[{lo:+.4f},{hi:+.4f}])")
    print(f"平均 MRR@5 : {bmr/n:.4f} → {rmr/n:.4f}  (Δ {(rmr-bmr)/n:+.4f}, 95%CI[{lom:+.4f},{him:+.4f}])")
    print(f"win/tie/loss (按 nDCG@5): {wins}/{ties}/{losses}")
    print(f"硬违规断言：rule-only={base_viol}  rule+recall={rc_viol}  "
          f"[{'PASS' if base_viol == 0 and rc_viol == 0 else 'FAIL'}]（两者都必须为 0）")
    print("注：cross_encoder 本地确定→可复现；样本小，Δ 以 bootstrap CI 描述，非强推断。")

    if args.out:
        report = {
            "backend": args.backend, "split": args.split, "n_queries": n, "model_ready": ready,
            "fusion": args.fusion,
            "avg_ndcg_base": bnd / n, "avg_ndcg_recall": rnd_ / n,
            "avg_mrr_base": bmr / n, "avg_mrr_recall": rmr / n,
            "ndcg_ci": [lo, hi], "mrr_ci": [lom, him],
            "win": wins, "tie": ties, "loss": losses,
            "violations_base": base_viol, "violations_recall": rc_viol,
            "base": base_rows, "recall": rc_rows,
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已存：{args.out}")


if __name__ == "__main__":
    main()
