# -*- coding: utf-8 -*-
"""
阶段一推荐质量自动评测（评测规范第三节）。

用法：
  py scripts/evaluate_recommendation.py                 # 跑默认 eval/eval_queries.json，打印指标
  py scripts/evaluate_recommendation.py --validate      # 只校验 eval 集：每条查询在库里的 ground-truth 支持数
  py scripts/evaluate_recommendation.py --out report.json           # 存报告
  py scripts/evaluate_recommendation.py --baseline before.json      # 与基线对比（优化前后表）
  py scripts/evaluate_recommendation.py --queries eval/eval_queries_dev.json \
      --expect-top1 XX --expect-top5 XX --expect-max-violation 0 --expect-min-noresult 1.0
                                                        # 自定阈值看门狗（默认=冻结基线常量，主集行为不变）

评测判据（独立于 retriever 自身逻辑的外部裁判）：
  - Top-1 Accuracy : 第 1 条是否满足全部 must_match
  - Top-5 Hit Rate : 前 5 条是否≥1 条满足全部 must_match
  - Constraint Violation Rate : 返回项里违反 must_match 的比例（目标 0%）
  - FASTQ Violation Rate      : 要 FASTQ 的查询里，返回项 has_raw_data!=True 的比例（目标 0%）
  - Average Matched Fields    : 返回项平均 matched_fields 数
  - Duplicate Family Rate     : Top-5 中同 family 的重复项比例
  - No Result Correctness     : 标了 no_result_expected 的查询是否真的返回空
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

# --- 路径与真实管线导入 ---
AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))


def _ensure_cli_utf8_stdout() -> None:
    """CLI-only encoding shim; importing the evaluator must never replace pytest/app stdout."""
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

from dataset_recommender.llm.config import get_settings          # noqa: E402
from dataset_recommender.corpus.data_loader import load_raw_records  # noqa: E402
from dataset_recommender.retrieval.normalizer import normalize_records, DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query      # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever, normalize_family_key  # noqa: E402
from dataset_recommender.domains.biodata.vocabulary import derive_modality, derive_platform_family  # noqa: E402

DEFAULT_QUERIES = AGENT_ROOT / "eval" / "eval_queries.json"

# 冻结基线逐项 pin（正式基线：Top1 97.7% / Top5 97.7%）。
# 旧门只判 `Top5≥90` floor、从不判 Top1 —— 会让 97.6%→90.x% 的真回退静默全绿，
# 与"frozen"之名不符（验证确认的真缺陷）。改为 Top1 与 Top5 都 pin 到正式基线；
# 否定/模态等**受控重基线**若合法调整基线，在此同步更新常量。
# 受控重基线（已获项目负责人授权）：「或」/hedge 由整句弃权改为照做，
# adv05/adv06/adv07 从 `no_result_expected` 变为可命中题 → 分母 41→44、NoResult 13/13→10/10，
# Top1/Top5 **升**到 97.7（三道新题全部 Top1 命中），硬违规/FASTQ 仍 0。详见 QUALITY_GATES.md。
# 以下为 CLI 默认值的**模块级**常量；held-out 等非主集评测用 --expect-* 覆盖，
# 默认值保持与冻结基线逐位相同（主集默认行为不变）。
FROZEN_TOP1 = 97.7
FROZEN_TOP5 = 97.7
EPS = 0.05  # 容 rate() 一位小数舍入噪声，仍能抓住真实回退


# ---------- 外部裁判：一条记录是否满足 must_match ----------
def _record_field_text(record: DatasetRecord, key: str) -> str:
    if key == "species":
        return (record.species or "").lower()
    if key == "tissue":
        return (record.tissue or "").lower()
    if key == "disease":
        return (record.disease or "").lower()
    if key in ("technology", "platform"):
        plat = str(record.raw.get("platform", "")) if isinstance(record.raw, dict) else ""
        return (plat + " " + (record.chemistry or "")).lower()
    if key == "modality":
        # 裁判**独立重算**（从 raw platform + chemistry），不回读 record.modality——保住外部裁判的看门狗角色：
        # 即便 normalizer 存错了 record.modality，裁判仍从原始字段独立判定，能抓到存储错。
        plat = str(record.raw.get("platform", "")) if isinstance(record.raw, dict) else ""
        return derive_modality(derive_platform_family(plat), record.chemistry or "")
    return ""


JUDGE_KEYS = frozenset({"species", "tissue", "disease", "technology", "platform", "assay", "modality", "has_raw_data"})


def _validate_expectation_map(name: str, mapping: dict) -> None:
    unknown = set(mapping) - JUDGE_KEYS
    if unknown:
        raise ValueError(f"{name} 含未知维度键 {sorted(unknown)}（合法：{sorted(JUDGE_KEYS)}）")


def constraint_satisfied(record: DatasetRecord, key, value) -> bool:
    if key == "has_raw_data":
        # 布尔精确：True→只有 has_raw_data is True；False→只有 is False（None 两态都不满足）
        if not isinstance(value, bool):
            raise ValueError("has_raw_data 期望值必须是 bool")
        return record.has_raw_data is value
    terms = value if isinstance(value, list) else [value]
    text = _record_field_text(record, key)
    return any(str(t).strip().lower() in text for t in terms if str(t).strip())


def satisfies_must(record: DatasetRecord, must_match: dict) -> bool:
    return all(constraint_satisfied(record, k, v) for k, v in must_match.items())


def satisfies_expected(record: DatasetRecord, must_match: dict, must_not_match: dict) -> bool:
    """正向 must_match 全满足 **且** 不命中任何 must_not_match（负向约束进入同一外部裁判，
    使 Top1/Top5/support/违规率都能看见 exclusion——否则"解析器彻底忽略 exclusion"仍可能全绿）。"""
    return (all(constraint_satisfied(record, k, v) for k, v in must_match.items())
            and all(not constraint_satisfied(record, k, v) for k, v in must_not_match.items()))


# ---------- 加载 & 推荐入口（对新旧 retriever 稳定）----------
def load_pipeline():
    settings = get_settings()
    raw = load_raw_records(settings.data_dir)
    records = normalize_records(raw)
    return settings, records


def recommend(query: str, records, settings, top_k: int = 5):
    intent = parse_query(query, settings.keyword_mapping)
    retriever = DatasetRetriever(top_k=top_k)
    return retriever.retrieve(records, intent, top_k=top_k)


# ---------- 指标 ----------
def evaluate(queries, records, settings, top_k: int = 5) -> dict:
    per_query = []
    top1_hits = top1_total = 0
    top5_hits = top5_total = 0
    rr_sum = 0.0
    returned_items = 0
    violations = 0
    fastq_items = 0
    fastq_violations = 0
    matched_fields_sum = 0
    dup_items = 0
    dup_total = 0
    noresult_total = noresult_correct = 0

    for q in queries:
        must = q.get("must_match", {})
        must_not = q.get("must_not_match", {})
        _validate_expectation_map(f"{q.get('id')}.must_match", must)
        _validate_expectation_map(f"{q.get('id')}.must_not_match", must_not)
        expect_empty = q.get("no_result_expected", False)
        results = recommend(q["query"], records, settings, top_k=top_k)
        support = sum(1 for r in records if satisfies_expected(r, must, must_not))

        qrow = {
            "id": q.get("id"), "query": q["query"], "category": q.get("category"),
            "support": support, "n_returned": len(results),
            "expect_empty": expect_empty,
        }

        if expect_empty:
            noresult_total += 1
            ok = len(results) == 0
            noresult_correct += 1 if ok else 0
            qrow["no_result_ok"] = ok
        else:
            # Top-1 / Top-5
            top1_total += 1
            top5_total += 1
            top1_ok = bool(results) and satisfies_expected(results[0].record, must, must_not)
            top5_ok = any(satisfies_expected(c.record, must, must_not) for c in results[:5])
            top1_hits += 1 if top1_ok else 0
            top5_hits += 1 if top5_ok else 0
            # MRR（Top1/Top5 类天花板指标下，名次移动
            # （第 2→第 1、第 6→第 5）旧指标不可见——排序质量类优化（RRF/换型）的裁判前置。
            # 只加性呈现、不进验收门（冻结指标口径不变）。
            first_rank = next((i for i, c in enumerate(results, 1)
                               if satisfies_expected(c.record, must, must_not)), None)
            rr = (1.0 / first_rank) if first_rank else 0.0
            rr_sum += rr
            qrow["top1_ok"] = top1_ok
            qrow["top5_ok"] = top5_ok
            qrow["rr"] = round(rr, 3)

        # 违规率 / FASTQ / 匹配字段 / 重复（对所有返回项统计）
        fam_seen = {}
        for c in results:
            returned_items += 1
            matched_fields_sum += len(getattr(c, "matched_fields", []) or [])
            if not satisfies_expected(c.record, must, must_not):
                violations += 1
            if must.get("has_raw_data") is True:
                fastq_items += 1
                if c.record.has_raw_data is not True:
                    fastq_violations += 1
            fam = normalize_family_key(c.record)
            fam_seen[fam] = fam_seen.get(fam, 0) + 1
        for fam, cnt in fam_seen.items():
            dup_total += cnt
            if cnt > 1:
                dup_items += cnt - 1
        per_query.append(qrow)

    def rate(a, b):
        return round(100.0 * a / b, 1) if b else 0.0

    metrics = {
        "n_queries": len(queries),
        "Top1_Accuracy_%": rate(top1_hits, top1_total),
        "Top5_Hit_Rate_%": rate(top5_hits, top5_total),
        "MRR": round(rr_sum / top1_total, 4) if top1_total else 0.0,
        "Constraint_Violation_%": rate(violations, returned_items),
        "FASTQ_Violation_%": rate(fastq_violations, fastq_items),
        "Avg_Matched_Fields": round(matched_fields_sum / returned_items, 2) if returned_items else 0.0,
        "Duplicate_Family_%": rate(dup_items, dup_total),
        "NoResult_Correct": f"{noresult_correct}/{noresult_total}",
        "_counts": {
            "returned_items": returned_items, "violations": violations,
            "fastq_items": fastq_items, "fastq_violations": fastq_violations,
        },
    }
    return {"metrics": metrics, "per_query": per_query}


# ---------- 展示 ----------
def print_validation(queries, records):
    print(f"{'id':6s} {'cat':12s} {'support':>7s}  query")
    print("-" * 70)
    problems = []
    for q in queries:
        must = q.get("must_match", {})
        support = sum(1 for r in records if satisfies_must(r, must))
        expect_empty = q.get("no_result_expected", False)
        flag = ""
        if expect_empty and support > 0:
            flag = f"  ⚠ 标了无结果但有 {support} 条支持"
            problems.append(q.get("id"))
        if not expect_empty and support == 0:
            flag = "  ⚠ 应有结果但库里 0 支持"
            problems.append(q.get("id"))
        print(f"{str(q.get('id')):6s} {str(q.get('category')):12s} {support:7d}  {q['query']}{flag}")
    print("-" * 70)
    print(f"共 {len(queries)} 条；问题查询：{problems if problems else '无'}")


def print_metrics(report, baseline=None, expect=None):
    """expect：达标阈值 dict，键 top1/top5/max_violation/max_fastq_violation/min_noresult_rate；
    None 时用冻结基线常量（主集默认行为逐位不变）。"""
    exp = {
        "top1": FROZEN_TOP1,
        "top5": FROZEN_TOP5,
        "max_violation": 0.0,
        "max_fastq_violation": 0.0,
        "min_noresult_rate": 1.0,
    }
    if expect:
        exp.update(expect)
    m = report["metrics"]
    print("\n================ 阶段一推荐质量指标 ================")
    keys = ["Top1_Accuracy_%", "Top5_Hit_Rate_%", "MRR", "Constraint_Violation_%",
            "FASTQ_Violation_%", "Avg_Matched_Fields", "Duplicate_Family_%", "NoResult_Correct"]
    if baseline:
        bm = baseline["metrics"]
        print(f"{'指标':24s} {'优化前':>10s} {'优化后':>10s}")
        print("-" * 48)
        for k in keys:
            print(f"{k:24s} {str(bm.get(k)):>10s} {str(m.get(k)):>10s}")
    else:
        for k in keys:
            print(f"  {k:24s} {m.get(k)}")
    print("=" * 51)
    # 达标判定
    def num(x):
        try:
            return float(x)
        except Exception:
            return None
    ok_violation = (num(m["Constraint_Violation_%"]) or 0) <= exp["max_violation"]
    ok_fastq = (num(m["FASTQ_Violation_%"]) or 0) <= exp["max_fastq_violation"]
    ok_top1 = (num(m["Top1_Accuracy_%"]) or 0) >= exp["top1"] - EPS
    ok_top5 = (num(m["Top5_Hit_Rate_%"]) or 0) >= exp["top5"] - EPS
    nr = str(m.get("NoResult_Correct", "0/0"))
    nc, _, nt = nr.partition("/")
    try:
        noresult_rate = int(nc) / int(nt) if nt not in ("", "0") else None
    except ValueError:
        noresult_rate = None
    ok_noresult = noresult_rate is not None and noresult_rate >= exp["min_noresult_rate"]
    print(f"验收：硬违规≤{exp['max_violation']}% [{'PASS' if ok_violation else 'FAIL'}]  "
          f"FASTQ违规≤{exp['max_fastq_violation']}% [{'PASS' if ok_fastq else 'FAIL'}]  "
          f"Top1≥{exp['top1']}% [{'PASS' if ok_top1 else 'FAIL'}]  "
          f"Top5≥{exp['top5']}% [{'PASS' if ok_top5 else 'FAIL'}]  "
          f"NoResult={nr}(≥{exp['min_noresult_rate']:.0%}) [{'PASS' if ok_noresult else 'FAIL'}]")
    return ok_violation and ok_fastq and ok_top1 and ok_top5 and ok_noresult


def _load_queries(path: str) -> list:
    """读评测查询文件：顶层取 "queries"（兼容裸 list）；跳过 _comment 之类无 query 字段的元信息项。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("queries", [])
    return [r for r in rows if isinstance(r, dict) and r.get("query")]


def main():
    _ensure_cli_utf8_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--baseline")
    ap.add_argument("--top-k", type=int, default=5)
    # 达标阈值参数化（held-out 等非主集用）；默认值=冻结基线常量，主集默认行为逐位不变。
    ap.add_argument("--expect-top1", type=float, default=FROZEN_TOP1,
                    help=f"Top1 达标下限（默认 {FROZEN_TOP1}，即冻结基线）")
    ap.add_argument("--expect-top5", type=float, default=FROZEN_TOP5,
                    help=f"Top5 达标下限（默认 {FROZEN_TOP5}，即冻结基线）")
    ap.add_argument("--expect-max-violation", type=float, default=0.0,
                    help="硬违规率达标上限 %%（默认 0.0）")
    ap.add_argument("--expect-max-fastq-violation", type=float, default=0.0,
                    help="FASTQ 违规率达标上限 %%（默认 0.0）")
    ap.add_argument("--expect-min-noresult", type=float, default=1.0,
                    help="NoResult 正确率达标下限（0~1 小数，默认 1.0=全对）")
    args = ap.parse_args()

    settings, records = load_pipeline()
    queries = _load_queries(args.queries)
    print(f"加载 {len(records)} 条记录；{len(queries)} 条评测查询。")

    if args.validate:
        print_validation(queries, records)
        return

    report = evaluate(queries, records, settings, top_k=args.top_k)
    baseline = None
    if args.baseline and Path(args.baseline).exists():
        baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
    expect = {
        "top1": args.expect_top1,
        "top5": args.expect_top5,
        "max_violation": args.expect_max_violation,
        "max_fastq_violation": args.expect_max_fastq_violation,
        "min_noresult_rate": args.expect_min_noresult,
    }
    ok = print_metrics(report, baseline, expect=expect)

    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已存：{args.out}")

    # 正式指标不达标 → 非零退出，让 QUALITY_GATES 的 $LASTEXITCODE 门真正闭合（实现 发现的存量门漏洞）。
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
