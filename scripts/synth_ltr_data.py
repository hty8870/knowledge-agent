# -*- coding: utf-8 -*-
"""Synthesize graded query–document training pairs from the frozen catalog.

Stage-1 data builder for learning-to-rank experiments on the recall-fusion layer.
Fully deterministic and offline: no LLM calls, no network access. Given the same
catalog and the same seed, repeated runs produce byte-identical output.

Method:
- Sample constraint combinations from the controlled vocabulary (species / tissue
  / disease, plus an optional raw-data requirement). Combinations are pre-screened
  for corpus support (records matching each vocabulary value are precomputed as
  sets, so support is a set intersection) before any pipeline execution. Some
  dimensions are phrased as soft preferences ("最好…") so the pipeline's hard
  filter admits candidates that violate them, producing graded relevance variance
  without any human or LLM labelling: a candidate satisfying every expressed
  constraint is a positive by construction.
- Each synthesized query is verified against the real query parser (must parse as
  executable and keep the intended hard dimensions), then executed through the
  real retrieval pipeline; the returned top-k candidates become the labelled
  pairs, including hard negatives (surfaced by ranking but violating a soft
  constraint).
- Relevance judging reuses the benchmark's external judge
  (``evaluate_recommendation.constraint_satisfied``) so labels share the frozen
  benchmark's satisfaction semantics.
- Train/held-out split is by constraint-combination cluster (no cluster appears
  in both splits), and synthesized queries are de-duplicated against the frozen
  benchmark query set verbatim.

Usage:
  python scripts/synth_ltr_data.py                 # full run into research/ltr_data/
  python scripts/synth_ltr_data.py --limit 40      # small debug run
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from evaluate_recommendation import (  # noqa: E402
    constraint_satisfied,
    load_pipeline,
    recommend,
)
from dataset_recommender.retrieval.query_parser import (  # noqa: E402
    active_filters,
    parse_query,
)
from dataset_recommender.domains.biodata.vocabulary import CATALOG  # noqa: E402

DEFAULT_SEED = 20260907
DEFAULT_OUT_DIR = AGENT_ROOT / "research" / "ltr_data"
DEFAULT_TARGET_QUERIES = 1700
DEFAULT_TOP_K = 12
HOLDOUT_MIN_QUERIES = 150

CORE_DIMS = ("species", "tissue", "disease")
RAW_KEY = "has_raw_data"

#: Minimum records satisfying every hard dimension (and the raw-data clause when
#: present) for a combination to be worth executing through the pipeline.
MIN_HARD_SUPPORT = 3
#: How many distinct query phrasings may reuse the same constraint cluster.
#: Clusters stay the split unit, so extra phrasings never leak across splits.
PHRASINGS_PER_CLUSTER = 4

# Query text templates. {body} is the space-joined hard-constraint phrases,
# {soft} an optional soft-preference clause, {raw} an optional raw-data clause.
# Every template below is verified to parse as executable with and without the
# optional clauses; wording variants containing "公开数据集" in fixed positions
# were measured to make the parser abstain and must not be reintroduced.
TEMPLATES = (
    "{body}{soft}{raw}",
    "找{body}相关的数据集{soft}{raw}",
    "给我一些{body}的数据{soft}{raw}",
    "有没有{body}的数据{soft}{raw}",
    "求{body}方面的数据集{soft}{raw}",
    "搜一下{body}的数据{soft}{raw}",
    "需要{body}的数据集{soft}{raw}",
    "{body}的数据集有哪些{soft}{raw}",
    "帮忙找{body}的单细胞数据{soft}{raw}",
)
# Verified to make the parser emit a `prefer` filter (true soft preference) for
# the soft dimension — checked against parse_query/active_filters. The inverted
# form "，{X}优先" (value before 优先) is instead hardened into an `include`
# filter and silently destroys the graded-label variance; do not reintroduce it.
# The main loop additionally asserts every soft dimension parsed as `prefer`.
SOFT_PREFIXES = (
    "，最好{soft}",
    "，最好是{soft}",
    "，倾向于{soft}",
    "，优先考虑{soft}",
    "，如果有{soft}更好",
    "，如果有{soft}的话",
    "，优先{soft}",
)
RAW_CLAUSES = ("，需要包含 FASTQ 原始数据", "，要能下载到原始数据", "，必须有 fastq")

# Combination patterns: (hard dims, soft dims, force raw-data clause, weight).
# Two-soft-dim patterns let a candidate violate two expressed constraints,
# which is what produces grade-0 pairs.
PATTERNS = (
    (("disease",), ("tissue",), False, 0.18),
    (("species", "disease"), ("tissue",), False, 0.22),
    (("tissue", "disease"), (), True, 0.12),
    (("species", "tissue"), ("disease",), False, 0.13),
    (("disease",), ("tissue",), True, 0.08),
    (("species",), ("disease",), False, 0.08),
    (("species", "disease"), (), True, 0.04),
    (("disease",), ("tissue", "species"), False, 0.10),
    (("tissue",), ("disease", "species"), False, 0.05),
)


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def dim_entries(dim: str) -> list[dict]:
    """Controlled-vocabulary values for one dimension with a typeable phrase and judge terms.

    Judge terms must cover every surface the retrieval hard filter can match on:
    the display value, non-CJK aliases, and the entry's ``targets`` (the match
    stems the pipeline actually substring-matches records against, e.g.
    "cervical" for Cervical Cancer) — omitting targets would misjudge hard
    constraints the pipeline itself satisfied."""
    out = []
    for entry in CATALOG.get(dim, []):
        display = str(entry.get("display") or "").strip()
        aliases = [str(a).strip() for a in entry.get("aliases", []) if str(a).strip()]
        targets = [str(t).strip() for t in entry.get("targets", []) if str(t).strip()]
        if not display:
            continue
        cjk = sorted((a for a in aliases if _is_cjk(a)), key=len, reverse=True)
        phrase = cjk[0] if cjk else display
        judge = sorted({
            display.lower(),
            *(a.lower() for a in aliases if not _is_cjk(a)),
            *(t.lower() for t in targets),
        })
        out.append({"value": display, "phrase": phrase, "judge": judge})
    return sorted(out, key=lambda e: e["value"])


def cluster_id(constraints: dict) -> str:
    """Stable id for a constraint combination; the train/held-out split unit."""
    parts = sorted(f"{d}={v}" for d, v in constraints.items())
    return "|".join(parts)


def grade_candidate(record, judge_map: dict) -> tuple[int, dict]:
    """Grade one candidate against the full constraint set with the benchmark judge.

    grade 2 = every constraint satisfied, 1 = exactly one violated, 0 = two or more.
    Returns (grade, per-dimension satisfaction map)."""
    matched = {
        dim: constraint_satisfied(record, dim, terms)
        for dim, terms in judge_map.items()
    }
    violated = sum(1 for ok in matched.values() if not ok)
    return (2 if violated == 0 else 1 if violated == 1 else 0), matched


def benchmark_queries() -> set[str]:
    path = AGENT_ROOT / "eval" / "eval_queries.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text(encoding="utf-8"))
    return {str(q.get("query", "")).strip() for q in data.get("queries", [])}


def build_query_specs(
    target: int,
    rng: random.Random,
    entries: dict,
    match_sets: dict,
    raw_set: frozenset,
) -> tuple[list[dict], int]:
    """Sample constraint combinations and render query texts until `target` unique specs.

    A combination is only kept when at least MIN_HARD_SUPPORT records satisfy every
    hard dimension (plus the raw-data clause when present) and at least one record
    additionally satisfies all soft dimensions, so each executed query can yield
    both positive and graded-negative pairs. Returns (specs, low_support_drops)."""
    dims_with_entries = [d for d in CORE_DIMS if entries.get(d)]
    patterns = [p for p in PATTERNS if all(d in dims_with_entries for d in p[0] + p[1])]
    weights = [p[3] for p in patterns]
    specs: list[dict] = []
    cluster_counts: dict[str, int] = {}
    seen_texts: set[str] = set()
    dropped_support = 0
    attempts = 0
    max_attempts = max(target * 400, 500_000)
    while len(specs) < target and attempts < max_attempts:
        attempts += 1
        hard_dims, soft_dims, force_raw, _w = rng.choices(patterns, weights=weights, k=1)[0]
        constraints: dict[str, dict] = {}
        for dim in hard_dims + soft_dims:
            constraints[dim] = rng.choice(entries[dim])
        want_raw = force_raw or rng.random() < 0.12

        hard_set: frozenset | None = None
        for dim in hard_dims:
            s = match_sets[(dim, constraints[dim]["value"])]
            hard_set = s if hard_set is None else hard_set & s
        assert hard_set is not None  # every pattern has at least one hard dim
        if want_raw:
            hard_set = hard_set & raw_set
        full_set = hard_set
        for dim in soft_dims:
            full_set = full_set & match_sets[(dim, constraints[dim]["value"])]
        if hard_set is None or len(hard_set) < MIN_HARD_SUPPORT or not full_set:
            dropped_support += 1
            continue

        cid = cluster_id({d: c["value"] for d, c in constraints.items()} | ({"raw": "1"} if want_raw else {}))
        if cluster_counts.get(cid, 0) >= PHRASINGS_PER_CLUSTER:
            continue
        body = " ".join(constraints[d]["phrase"] for d in hard_dims)
        soft = ""
        if soft_dims:
            soft = "".join(
                rng.choice(SOFT_PREFIXES).format(soft=constraints[d]["phrase"])
                for d in soft_dims
            )
        raw = rng.choice(RAW_CLAUSES) if want_raw else ""
        text = rng.choice(TEMPLATES).format(body=body, soft=soft, raw=raw).strip()
        text = " ".join(text.split())
        if text in seen_texts:
            continue
        cluster_counts[cid] = cluster_counts.get(cid, 0) + 1
        seen_texts.add(text)
        specs.append({
            "query": text,
            "cluster_id": cid,
            "hard_dims": list(hard_dims),
            "soft_dims": list(soft_dims),
            "constraints": {d: c["value"] for d, c in constraints.items()},
            "want_raw": want_raw,
            "support": len(hard_set),
        })
    return specs, dropped_support


def assign_splits(specs: list[dict], rng: random.Random, holdout_min: int) -> None:
    """Assign whole clusters to held-out until it holds enough queries.

    The floor is capped on small runs so a debug-sized `--target-queries`/`--limit`
    cannot put every cluster into held-out and leave the training file empty."""
    clusters = sorted({s["cluster_id"] for s in specs})
    rng.shuffle(clusters)
    holdout_target = min(holdout_min, max(1, int(round(len(specs) * 0.15))))
    holdout_clusters: set[str] = set()
    count = 0
    for cid in clusters:
        if count >= holdout_target:
            break
        if len(holdout_clusters) >= len(clusters) - 1:
            break  # always leave at least one cluster for the training split
        holdout_clusters.add(cid)
        count += sum(1 for s in specs if s["cluster_id"] == cid)
    for s in specs:
        s["split"] = "heldout" if s["cluster_id"] in holdout_clusters else "train"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--target-queries", type=int, default=DEFAULT_TARGET_QUERIES)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--limit", type=int, default=0, help="debug: only run the first N specs")
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    started = time.time()
    rng = random.Random(args.seed)
    settings, records = load_pipeline()
    entries = {dim: dim_entries(dim) for dim in CORE_DIMS}
    banned = benchmark_queries()

    # Precompute the set of records matching each vocabulary value once, so the
    # support check per sampled combination is a set intersection instead of a
    # full corpus rescan.
    match_sets = {
        (dim, e["value"]): frozenset(
            i for i, r in enumerate(records) if constraint_satisfied(r, dim, e["judge"])
        )
        for dim in CORE_DIMS
        for e in entries[dim]
    }
    raw_set = frozenset(i for i, r in enumerate(records) if getattr(r, "has_raw_data", None) is True)

    specs, dropped_support = build_query_specs(args.target_queries, rng, entries, match_sets, raw_set)
    if args.limit:
        specs = specs[: args.limit]

    stats = {
        "seed": args.seed,
        "top_k": args.top_k,
        "catalog_records": len(records),
        "benchmark_queries_excluded": len(banned),
        "specs_sampled": len(specs),
        "dropped_low_support": dropped_support,
        "dropped_benchmark_collision": 0,
        "dropped_parser_reject": 0,
        "dropped_soft_not_honoured": 0,
        "dropped_few_candidates": 0,
        "queries_written": 0,
        "pairs_written": 0,
        "grade_counts": {"0": 0, "1": 0, "2": 0},
        "splits": {"train": 0, "heldout": 0},
        "elapsed_s": 0.0,
    }

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Execute every spec through the pipeline first; the train/held-out split is
    # assigned afterwards on the surviving queries, so parser rejects cannot
    # shrink the held-out set below its floor.
    surviving: list[dict] = []
    for i, spec in enumerate(specs, 1):
        query = spec["query"]
        if query in banned:
            stats["dropped_benchmark_collision"] += 1
            continue
        intent = parse_query(query, settings.keyword_mapping)
        if getattr(intent, "parse_status", "") != "executable":
            stats["dropped_parser_reject"] += 1
            continue
        include_dims = {
            f.get("dim") for f in active_filters(intent) if f.get("polarity") == "include"
        }
        if not set(spec["hard_dims"]).issubset(include_dims):
            stats["dropped_parser_reject"] += 1
            continue
        if spec["want_raw"] and getattr(intent, "has_raw_data_required", None) is not True:
            stats["dropped_parser_reject"] += 1
            continue
        # Every soft dimension must have been honoured as a soft preference by the
        # parser; a soft clause hardened into an include filter would silently
        # remove the graded-label variance the dataset exists for.
        prefer_dims = {
            f.get("dim") for f in active_filters(intent) if f.get("polarity") == "prefer"
        }
        if not set(spec["soft_dims"]).issubset(prefer_dims):
            stats["dropped_soft_not_honoured"] += 1
            continue

        candidates = recommend(query, records, settings, top_k=args.top_k)
        if len(candidates) < 3:
            stats["dropped_few_candidates"] += 1
            continue

        judge_map = {d: entries[d][next(i for i, e in enumerate(entries[d])
                                      if e["value"] == v)]["judge"]
                     for d, v in spec["constraints"].items()}
        if spec["want_raw"]:
            judge_map[RAW_KEY] = True
        # Union the parser's own extracted values (which may be stems not present
        # in display/aliases) into the judge terms, so the judge scores the same
        # constraint the parser filtered on.
        for f in active_filters(intent):
            dim = f.get("dim")
            if isinstance(judge_map.get(dim), list):
                judge_map[dim] = sorted({
                    *judge_map[dim],
                    *(str(v).lower() for v in f.get("values") or []),
                })

        rows = []
        for pos, cand in enumerate(candidates, 1):
            raw_rec = getattr(cand.record, "raw", None)
            uid = str(raw_rec.get("dataset_uid") or "") if isinstance(raw_rec, dict) else ""
            grade, matched = grade_candidate(cand.record, judge_map)
            stats["grade_counts"][str(grade)] += 1
            rows.append({
                "uid": uid,
                "pos": pos,
                "lex_score": cand.score,
                "grade": grade,
                "matched": matched,
            })
        stats["pairs_written"] += len(rows)
        stats["queries_written"] += 1
        surviving.append({
            "query_id": f"sq-{i:05d}",
            "query": query,
            "cluster_id": spec["cluster_id"],
            "constraints": spec["constraints"],
            "hard_dims": spec["hard_dims"],
            "soft_dims": spec["soft_dims"],
            "has_raw_data_required": spec["want_raw"],
            "candidates": rows,
        })
        if i % 200 == 0:
            print(f"  … {i}/{len(specs)} specs processed", flush=True)

    assign_splits(surviving, rng, HOLDOUT_MIN_QUERIES)
    paths = {"train": out_dir / "synth_train.jsonl", "heldout": out_dir / "synth_heldout.jsonl"}
    handles = {k: p.open("w", encoding="utf-8", newline="\n") for k, p in paths.items()}
    try:
        for line in surviving:
            split = line["split"]
            stats["splits"][split] += 1
            handles[split].write(json.dumps(line, ensure_ascii=False) + "\n")
    finally:
        for fh in handles.values():
            fh.close()

    stats["elapsed_s"] = round(time.time() - started, 1)
    (out_dir / "_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
