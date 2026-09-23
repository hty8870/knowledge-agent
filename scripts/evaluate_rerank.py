# -*- coding: utf-8 -*-
"""
附加评测（内部仪器）：量化「可选 LLM 重排」相对确定性 rule-only 排序的排序质量差异。

**边界**：本脚本与官方 `evaluate_recommendation.py` 完全独立；**不改、不替**官方二元评测口径。
它衡量的是「存活集内的排序好坏」（nDCG@5 / MRR），不是「推荐对不对」（那由官方脚本裁决）。

用法：
  set PYTHONIOENCODING=utf-8
  py scripts/evaluate_rerank.py                         # rule-only vs rule+rerank(llm)，打印对比
  py scripts/evaluate_rerank.py --provider openai-compatible
  py scripts/evaluate_rerank.py --out eval/rerank_report.json

裁判：复用官方 `evaluate_recommendation.py` 的 constraint_satisfied（字段读法与官方一致）。
分级相关性：relevance = 0（不满足 must_match）；否则 1 + 满足的 ideal 维度数。
安全断言：rule-only 与 rule+rerank 两个 run 内，返回项对 must_match 的**硬违规必须都为 0**。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))
# 注意：import evaluate_recommendation 会在其模块级包一次 TextIOWrapper；
# 这里用幂等的 reconfigure 设 UTF-8，避免二次包裹导致底层 buffer 被关闭。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.llm.config import get_settings                # noqa: E402
from dataset_recommender.corpus.data_loader import load_raw_records        # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_records        # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query            # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever          # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config          # noqa: E402
from evaluate_recommendation import constraint_satisfied, satisfies_must  # noqa: E402

# 分级答案不随公开仓发布（防对可见集过拟合）；用 --queries 指向自建的分级答案文件。
DEFAULT_QUERIES = AGENT_ROOT / "eval" / "eval_rerank_graded.json"


def graded_relevance(record, must: dict, ideal: dict) -> int:
    if not satisfies_must(record, must):
        return 0
    bonus = sum(1 for k, v in ideal.items() if constraint_satisfied(record, k, v))
    return 1 + bonus


def dcg_at_k(rels: list[int], k: int) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(rels[:k]))


def ndcg_at_k(returned_rels: list[int], ideal_rels: list[int], k: int = 5) -> float:
    idcg = dcg_at_k(sorted(ideal_rels, reverse=True), k)
    if idcg <= 0:
        return 0.0
    return dcg_at_k(returned_rels, k) / idcg


def mrr(returned_rels: list[int], threshold: int = 2) -> float:
    for i, rel in enumerate(returned_rels):
        if rel >= threshold:
            return 1.0 / (i + 1)
    return 0.0


def run_config(records, settings, queries, rerank_backend: str, llm_config, top_k: int = 5):
    retriever = DatasetRetriever(top_k=top_k)
    rows = []
    total_violations = 0
    for q in queries:
        must = q.get("must_match", {})
        ideal = q.get("ideal", {})
        intent = parse_query(q["query"], settings.keyword_mapping)
        results = retriever.retrieve(
            records, intent, top_k=top_k,
            rerank_backend=rerank_backend, llm_config=llm_config,
        )
        returned_rels = [graded_relevance(c.record, must, ideal) for c in results]
        # 硬违规：返回项里不满足 must_match 的（存活集内理应恒为 0）
        violations = sum(1 for c in results if not satisfies_must(c.record, must))
        total_violations += violations
        # 理想相关性分布（全体存活集，供 IDCG）
        survivors_rels = [
            graded_relevance(r, must, ideal) for r in records if satisfies_must(r, must)
        ]
        rows.append({
            "id": q.get("id"), "query": q["query"],
            "n_survivors": sum(1 for x in survivors_rels if x > 0),
            "returned_rels": returned_rels,
            "ndcg@5": round(ndcg_at_k(returned_rels, survivors_rels, 5), 4),
            "mrr": round(mrr(returned_rels), 4),
            "violations": violations,
        })
    return rows, total_violations


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--provider", default=None, help="重排 LLM provider（默认取 env/.env）")
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--out")
    args = ap.parse_args()

    settings = get_settings()
    records = normalize_records(load_raw_records(settings.data_dir))
    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))["queries"]

    llm_config = load_llm_config(project_root=settings.project_root, provider_override=args.provider)
    key_detected = bool(getattr(llm_config, "api_key", None))
    print(f"加载 {len(records)} 条记录；{len(queries)} 条分级查询。")
    print(f"重排 provider={llm_config.provider}  API key detected={key_detected}"
          + ("" if key_detected else "  → 重排将回退到 rule-only 顺序（对比会全为 tie）"))

    base_rows, base_viol = run_config(records, settings, queries, "off", None, args.top_k)
    rr_rows, rr_viol = run_config(records, settings, queries, "llm", llm_config, args.top_k)

    # 对比
    print("\n================ rule-only vs rule+rerank(llm) ================")
    print(f"{'id':5s} {'surv':>4s}  {'nDCG@5':>14s}  {'MRR':>12s}  win/tie/loss")
    print("-" * 66)
    wins = ties = losses = 0
    base_ndcg_sum = rr_ndcg_sum = base_mrr_sum = rr_mrr_sum = 0.0
    for b, r in zip(base_rows, rr_rows):
        d = r["ndcg@5"] - b["ndcg@5"]
        tag = "win" if d > 1e-9 else ("loss" if d < -1e-9 else "tie")
        wins += tag == "win"; ties += tag == "tie"; losses += tag == "loss"
        base_ndcg_sum += b["ndcg@5"]; rr_ndcg_sum += r["ndcg@5"]
        base_mrr_sum += b["mrr"]; rr_mrr_sum += r["mrr"]
        print(f"{b['id']:5s} {b['n_survivors']:4d}  "
              f"{b['ndcg@5']:6.4f}->{r['ndcg@5']:6.4f}  "
              f"{b['mrr']:5.3f}->{r['mrr']:5.3f}  {tag}")
    n = len(queries)
    print("-" * 66)
    print(f"平均 nDCG@5: rule-only {base_ndcg_sum/n:.4f}  →  rule+rerank {rr_ndcg_sum/n:.4f}"
          f"  (Δ {(rr_ndcg_sum-base_ndcg_sum)/n:+.4f})")
    print(f"平均 MRR   : rule-only {base_mrr_sum/n:.4f}  →  rule+rerank {rr_mrr_sum/n:.4f}"
          f"  (Δ {(rr_mrr_sum-base_mrr_sum)/n:+.4f})")
    print(f"win/tie/loss (按 nDCG@5): {wins}/{ties}/{losses}")
    print(f"硬违规断言：rule-only={base_viol}  rule+rerank={rr_viol}  "
          f"[{'PASS' if base_viol == 0 and rr_viol == 0 else 'FAIL'}]（两者都必须为 0）")
    print("注：样本小 + LLM 输出不完全可复现 → 上述为描述性对比，非推断统计。")

    if args.out:
        report = {
            "base": base_rows, "rerank": rr_rows,
            "summary": {
                "avg_ndcg_base": base_ndcg_sum / n, "avg_ndcg_rerank": rr_ndcg_sum / n,
                "avg_mrr_base": base_mrr_sum / n, "avg_mrr_rerank": rr_mrr_sum / n,
                "win": wins, "tie": ties, "loss": losses,
                "violations_base": base_viol, "violations_rerank": rr_viol,
                "llm_key_detected": key_detected,
            },
        }
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已存：{args.out}")


if __name__ == "__main__":
    main()
