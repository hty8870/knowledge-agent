# -*- coding: utf-8 -*-
"""多源检索质量评测（开发态，非冻结门）。

与冻结门 scripts/evaluate_recommendation.py 的区别与定位：
  - 冻结门：**base-only**（sources=None），二元 must_match，守护确定性契约，**不可退化**。
  - 本台：**全部来源**（load_full_corpus），衡量词表扩充是否让外部平台库
    （CELLxGENE/HCA/EBI SCEA/ArrayExpress/ENCODE）真正可检出，并给出跨源召回/违规/覆盖/弃权画像。
    **纯新增、零改动现有代码路径**（只读地跑真实 parse_query + DatasetRetriever），
    不影响冻结门、不碰确定性。作为词表扩充召回成果的**回归护栏** + 未来检索优化的度量底座。

外部裁判（独立于 retriever 自身逻辑）：一条记录是否满足某期望约束 = 该字段**子串包含** target。
species/tissue/disease 与冻结门同口径（各自字段子串）。platform/technology/assay 这里用
**platform+assay+chemistry 的并集袋**做子串——这是刻意从宽的**超集**裁判（≥ 冻结门的 platform+chemistry、
也 ≥ retriever 的 assay+chemistry / 精确 platform_family），只会更宽松、**永不制造假违规**，适合开发态
多字段实况；它不等同于冻结门口径，也不参与冻结门。has_raw_data 用记录布尔。

指标：
  - Resolve_Correct        : 该弃权的弃权 / 该解析的解析（含 no_result 场景应解析且空集）
  - Constraint_Violation_% : 返回项里违反期望约束的比例（目标 0%）
  - Hit@k                  : 期望有结果的查询里，top-k 至少 1 条全满足期望约束的比例
  - Recall_fill@k          : top-k 实得数 / min(k, 库中满足期望约束的总数)（衡量"够不够填满"）
  - Source_Coverage        : 全体返回结果覆盖到的来源数 / 可用来源数（衡量多源真的被检出）
  - NoResult_Correct       : 标 no_result_expected 的查询是否真的空集
  - Abstain_Correct        : 标 abstain_expected 的查询是否真的弃权
  - Avg_Matched_Fields     : 返回项平均命中的期望约束数（软质量）

用法：
  py evaluate_multisource.py                    # 跑默认 eval/multisource_queries.json，打印指标
  py evaluate_multisource.py --queries X.json   # 指定查询集
  py evaluate_multisource.py --out report.json  # 存 JSON 报告
  py evaluate_multisource.py --show             # 逐条打印明细
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))

from dataset_recommender.llm.config import get_settings          # noqa: E402
from dataset_recommender.corpus.corpus import load_full_corpus, source_of, available_sources  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query      # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever    # noqa: E402

DEFAULT_QUERIES = AGENT_ROOT / "eval" / "multisource_queries.json"


def _field_text(record, key: str) -> str:
    if key == "species":
        return (record.species or "").lower()
    if key == "tissue":
        return (record.tissue or "").lower()
    if key == "disease":
        return (record.disease or "").lower()
    if key in ("technology", "platform", "assay"):
        plat = str(record.raw.get("platform", "")) if isinstance(record.raw, dict) else ""
        return (plat + " " + (record.assay or "") + " " + (record.chemistry or "")).lower()
    return ""


def _satisfies_one(record, key, value) -> bool:
    if key == "has_raw_data":
        return record.has_raw_data is True
    vals = value if isinstance(value, list) else [value]
    text = _field_text(record, key)
    return any(str(v).lower() in text for v in vals)


def _satisfies_all(record, expect: dict) -> bool:
    return all(_satisfies_one(record, k, v) for k, v in expect.items())


def _violates_not(record, expect_not: dict) -> bool:
    """命中任一 expect_not（负向约束）即为违规——让多源门也能看见 exclusion 是否真执行。"""
    return any(_satisfies_one(record, k, v) for k, v in expect_not.items())


def _corpus_support(records, expect: dict) -> int:
    return sum(1 for r in records if _satisfies_all(r, expect))


def run_eval(queries_path: "str | Path | None" = None, top_k: int = 5) -> "tuple[dict, list]":
    """加载全量语料 + 跑评测集，返回 (metrics, rows)。供 CLI 与 pytest 共用（纯只读，不改任何状态）。
    metrics 内含原始计数（*_num/*_den/*_total）供断言用。"""
    settings = get_settings()
    records = load_full_corpus(settings.data_dir, settings.project_root)
    _avail = available_sources(settings.data_dir, settings.project_root)
    all_source_names = {str(s.get("value") or "") for s in _avail if s.get("value")}
    n_sources = len(_avail)
    retriever = DatasetRetriever(top_k=top_k)
    k = top_k

    data = json.loads(Path(queries_path or DEFAULT_QUERIES).read_text(encoding="utf-8"))
    queries = data["queries"] if isinstance(data, dict) else data

    n = len(queries)
    resolve_correct = 0
    abstain_total = abstain_correct = 0
    noresult_total = noresult_correct = 0
    hit_total = hit_hit = 0
    recall_num = recall_den = 0.0
    violations = returned = 0
    matched_fields_sum = matched_fields_cnt = 0
    sources_seen = set()
    rows = []

    for item in queries:
        q = item["query"]
        expect = item.get("expect", {})
        expect_not = item.get("expect_not", {})
        exp_abstain = bool(item.get("abstain_expected", False))
        exp_noresult = bool(item.get("no_result_expected", False))
        min_hits = int(item.get("min_hits", 0))

        intent = parse_query(q)
        hits = retriever.retrieve(records, intent, top_k=k)

        # resolve/abstain correctness
        if exp_abstain:
            abstain_total += 1
            ok = intent.abstain is True
            abstain_correct += ok
            resolve_correct += ok
        else:
            ok_resolve = intent.abstain is False
            resolve_correct += ok_resolve

        # no-result correctness
        if exp_noresult:
            noresult_total += 1
            noresult_correct += (len(hits) == 0 and not intent.abstain)

        # hit@k + recall_fill (only for queries expected to have results)
        expect_results = (not exp_abstain) and (not exp_noresult) and expect
        if expect_results:
            hit_total += 1
            support = _corpus_support(records, expect)
            got_valid = [h for h in hits if _satisfies_all(h.record, expect) and not _violates_not(h.record, expect_not)]
            hit_hit += (len(got_valid) >= 1)
            if support > 0:
                # 分子只计**真正满足期望约束**的返回项（got_valid）：当 expect 比 parser 实际解析更严时，
                # 个别 survivor 可能不满足 expect —— 用 got_valid 而非 len(hits) 杜绝把无效项算成召回。
                recall_num += min(len(got_valid), k)
                recall_den += min(support, k)

        # violation over ALL returned (any expect / expect_not present)
        if expect or expect_not:
            for h in hits:
                returned += 1
                if _satisfies_all(h.record, expect) and not _violates_not(h.record, expect_not):
                    matched_fields_sum += sum(1 for kk, vv in expect.items() if _satisfies_one(h.record, kk, vv))
                    matched_fields_cnt += 1
                else:
                    violations += 1
                sources_seen.add(source_of(h.record))
        else:
            for h in hits:
                sources_seen.add(source_of(h.record))

        rows.append({
            "query": q, "abstain": intent.abstain, "hits": len(hits),
            "expect": expect, "min_hits": min_hits,
            "min_hits_ok": (len(hits) >= min_hits) if (expect_results) else None,
            "top_sources": [source_of(h.record) for h in hits[:3]],
        })

    metrics = {
        "n_queries": n,
        "Resolve_Correct_%": round(100 * resolve_correct / n, 1) if n else 0,
        "Constraint_Violation_%": round(100 * violations / returned, 2) if returned else 0.0,
        "Hit@%d_%%" % k: round(100 * hit_hit / hit_total, 1) if hit_total else 0.0,
        "Recall_fill@%d_%%" % k: round(100 * recall_num / recall_den, 1) if recall_den else 0.0,
        "Source_Coverage": f"{len(sources_seen)}/{n_sources}",
        "NoResult_Correct": f"{noresult_correct}/{noresult_total}",
        "Abstain_Correct": f"{abstain_correct}/{abstain_total}",
        "Avg_Matched_Fields": round(matched_fields_sum / matched_fields_cnt, 2) if matched_fields_cnt else 0.0,
        # 原始计数（供 pytest 断言 / 程序化消费）
        "_raw": {
            "n_records": len(records), "n_sources": n_sources, "returned": returned,
            "violations": violations, "hit_total": hit_total, "hit_hit": hit_hit,
            "abstain_total": abstain_total, "abstain_correct": abstain_correct,
            "noresult_total": noresult_total, "noresult_correct": noresult_correct,
            "sources_seen": sorted(sources_seen),
            # 语料里**实际存在**的来源名（不只是个数）。护栏据此区分「基线来源回退了」
            # 与「新接了来源但评测集还没覆盖它」——只报个数区分不了这两件事。
            "all_sources": sorted(all_source_names),
        },
    }
    return metrics, rows


def main() -> int:
    try:   # 仅 CLI 路径需要 UTF-8 stdout；import 时不改 stdout（否则破坏 pytest 捕获）
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES))
    ap.add_argument("--out", default="")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--top-k", type=int, default=5)
    args = ap.parse_args()

    metrics, rows = run_eval(args.queries, args.top_k)
    k = args.top_k
    raw = metrics["_raw"]
    abstain_correct, abstain_total = raw["abstain_correct"], raw["abstain_total"]
    noresult_correct, noresult_total = raw["noresult_correct"], raw["noresult_total"]

    print(f"加载 {raw['n_records']} 条记录（全部来源 {raw['n_sources']} 个）；{metrics['n_queries']} 条评测查询。\n")
    print("============ 多源检索质量指标 ============")
    for kk, vv in metrics.items():
        if kk == "_raw":
            continue
        print(f"  {kk:24s} {vv}")
    print("=========================================")
    viol_ok = metrics["Constraint_Violation_%"] == 0.0
    abs_ok = abstain_correct == abstain_total
    nores_ok = noresult_correct == noresult_total
    print(f"验收：硬违规=0% [{'PASS' if viol_ok else 'FAIL'}]  "
          f"弃权正确={abstain_correct}/{abstain_total} [{'PASS' if abs_ok else 'FAIL'}]  "
          f"空集正确={noresult_correct}/{noresult_total} [{'PASS' if nores_ok else 'FAIL'}]")

    if args.show:
        print("\n--- 明细 ---")
        for r in rows:
            flag = ""
            if r["min_hits_ok"] is False:
                flag = f"  <<< 少于期望 {r['min_hits']}"
            print(f"  {r['query'][:30]:32s} abstain={str(r['abstain']):5s} hits={r['hits']} src={r['top_sources']}{flag}")

    if args.out:
        Path(args.out).write_text(json.dumps({"metrics": metrics, "rows": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n报告已写入 {args.out}")

    return 0 if (viol_ok and abs_ok and nores_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
