# -*- coding: utf-8 -*-
"""Harvest real-pipeline candidates for the OOD query set — two tracks.

Track A (``candidates``) is production-faithful: parse; on ``unresolved_term``
abstain, strip the unresolved terms and re-parse (the workflow's auto-degrade),
respecting the hard gate (no remaining condition -> honest no result); other
abstain reasons stay empty. Track B (``candidates_rescue``) force-clears the
abstain and retrieves anyway, answering "what if the front layer never gave
up" — the ranking-upside analysis set.

Per candidate: uid, position, lexical score, dense-embedding cosine (same
embedder and candidate serialisation as ``augment_vec_scores``), and the
deterministic constraint grade against intended constraints (``excluded`` dims
count as an extra violation when satisfied). Constraint-free classes get
grade=None by design.

Output: ``research/ood_eval/synth_ood_vec.jsonl`` in the LTR row shape so the
labelling tooling (llm_cross_score) works on it unmodified.

Usage:
  python scripts/harvest_ood_candidates.py            # full set
  python scripts/harvest_ood_candidates.py --limit 3  # smoke
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from augment_vec_scores import candidate_text, load_uid_records  # noqa: E402
from evaluate_recommendation import constraint_satisfied, load_pipeline  # noqa: E402
from synth_ltr_data import dim_entries  # noqa: E402
from dataset_recommender.app.workflow import strip_terms  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever  # noqa: E402
from dataset_recommender.retrieval.vector_recall import (  # noqa: E402
    default_model_dir,
    expand_query_bilingual,
    load_embedder,
)

OOD_DIR = AGENT_ROOT / "research" / "ood_eval"
TOP_K = 12


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def judge_terms(dim: str, value: str) -> list[str]:
    for e in dim_entries(dim):
        if e["value"] == value:
            return e["judge"]
    return [value.lower()]


def has_condition(intent) -> bool:
    return (any(intent.constraints.values())
            or any(intent.excluded_constraints.values())
            or intent.has_raw_data_required is not None
            or bool(intent.date_from or intent.date_to))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=TOP_K)
    parser.add_argument("--only-new", action="store_true",
                        help="append mode: harvest only query_ids not already in the output file")
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    queries = [json.loads(l) for l in (OOD_DIR / "ood_queries.jsonl")
               .read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        queries = queries[: args.limit]

    settings, records = load_pipeline()
    uid_records = load_uid_records()
    embedder = load_embedder(default_model_dir())
    if embedder is None:
        print("embedding model not available locally", file=sys.stderr)
        return 2
    retriever = DatasetRetriever(top_k=args.top_k)
    vec_cache: dict[str, list[float]] = {}

    def build_cands(items, q, judge_map, excluded, qvec):
        cands = []
        for pos, item in enumerate(items, 1):
            rec = getattr(item, "record", item)
            raw = getattr(rec, "raw", None)
            uid = str(raw.get("dataset_uid") or "") if isinstance(raw, dict) else ""
            matched = {dim: constraint_satisfied(rec, dim, terms) for dim, terms in judge_map.items()}
            violated = sum(1 for ok in matched.values() if not ok)
            violated += sum(1 for dim, terms in excluded.items()
                            if constraint_satisfied(rec, dim, terms))
            grade = (2 if violated == 0 else 1 if violated == 1 else 0) if judge_map or excluded else None
            vec_score = None
            if qvec is not None and uid:
                if uid not in vec_cache and uid in uid_records:
                    vec_cache[uid] = embedder([candidate_text(uid_records[uid])])[0]
                if uid in vec_cache:
                    vec_score = round(cosine(qvec, vec_cache[uid]), 6)
            cands.append({"uid": uid, "pos": pos,
                          "lex_score": round(float(getattr(item, "score", 0.0)), 4),
                          "grade": grade, "matched": matched, "vec_score": vec_score})
        return cands

    out_path = OOD_DIR / "synth_ood_vec.jsonl"
    if args.only_new and out_path.exists():
        have = {json.loads(l)["query_id"] for l in
                out_path.read_text(encoding="utf-8").splitlines() if l.strip()}
        queries = [q for q in queries if q["qid"] not in have]
        print(f"only-new: {len(have)} already harvested, {len(queries)} to go", flush=True)
    mode = "a" if args.only_new else "w"
    t0 = time.time()
    with out_path.open(mode, encoding="utf-8", newline="\n") as out:
        for i, q in enumerate(queries, 1):
            judge_map = {}
            for dim, val in (q["constraints"] or {}).items():
                vals = val if isinstance(val, list) else [val]
                for v in vals:
                    judge_map[dim] = sorted(set(judge_map.get(dim, [])) | set(judge_terms(dim, v)))
            excluded = {dim: judge_terms(dim, val) for dim, val in (q["excluded"] or {}).items()}

            intent = parse_query(q["query"], settings.keyword_mapping)
            degraded = None
            eff_intent = intent
            if intent.abstain and intent.abstain_reason == "unresolved_term":
                dq = " ".join(strip_terms(intent.original_query,
                                          [t for t in intent.unresolved_terms if t.strip()]).split())
                if dq and dq != intent.original_query.strip():
                    r_intent = parse_query(dq, settings.keyword_mapping)
                    if r_intent.parse_status == "executable" and not r_intent.abstain and has_condition(r_intent):
                        degraded = {"ignored_terms": [t for t in intent.unresolved_terms if t.strip()],
                                    "query": dq}
                        eff_intent = r_intent
            results_a = [] if eff_intent.abstain else retriever.retrieve(records, eff_intent, top_k=args.top_k)
            rescue_intent = dataclasses.replace(intent, abstain=False, abstain_reason=None,
                                                parse_status='executable')
            results_b = retriever.retrieve(records, rescue_intent, top_k=args.top_k)

            qvec_a = embedder([expand_query_bilingual(q["query"], eff_intent)])[0]
            qvec_b = embedder([expand_query_bilingual(q["query"], rescue_intent)])[0] if intent.abstain else qvec_a
            row = {
                "query_id": q["qid"], "query": q["query"], "ood_class": q["ood_class"],
                "constraints": q["constraints"], "excluded": q["excluded"],
                "expected": q["expected"], "parse": q["parse"],
                "abstain": bool(intent.abstain),
                "abstain_reason": intent.abstain_reason or "",
                "degraded": degraded,
                "n_returned": len(results_a),
                "candidates": build_cands(results_a, q, judge_map, excluded, qvec_a),
                "candidates_rescue": build_cands(results_b, q, judge_map, excluded, qvec_b),
            }
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            if i % 50 == 0:
                print(f"  {i}/{len(queries)} ({time.time()-t0:.0f}s)", flush=True)
    print(f"harvested {len(queries)} queries -> {out_path} in {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
