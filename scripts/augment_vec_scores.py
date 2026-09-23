# -*- coding: utf-8 -*-
"""Attach dense-vector scores to synthesized LTR pairs (offline, deterministic).

Stage-4 of the LTR data pipeline. The production fusion layer
(``retrieval/vector_recall.py``) ranks the hard-filter survivor set by fusing the
lexical score with a dense-embedding cosine; a learned fusion ranker therefore
needs, per candidate, the same two signals the production rules see. This script
replays that signal generation onto the synthesized dataset:

- the query is embedded exactly as production does (``expand_query_bilingual``
  on the parsed intent, then the local sentence-transformers embedder);
- each candidate record is serialized with the same recipe as production
  (``_candidate_text``: labelled structured fields + description truncated to
  400 chars) and embedded once per corpus record (cached by ``dataset_uid``);
- ``vec_score`` is the raw cosine similarity (both vectors are L2-normalised by
  the embedder).

Fully offline and deterministic given the local model directory; no LLM calls.
Output is incremental and resumable. Records without a resolvable uid keep
``vec_score: null``.

Usage:
  python scripts/augment_vec_scores.py --limit 20   # smoke run
  python scripts/augment_vec_scores.py              # full run, resumable
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.vector_recall import (  # noqa: E402
    default_model_dir,
    expand_query_bilingual,
    load_embedder,
)

DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
SPLITS = ("train", "heldout")


def read_jsonl(path: Path) -> list[dict]:
    """Tolerant reader: a file being appended concurrently may end in a partial line."""
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def candidate_text(record) -> str:
    """Same serialisation as vector_recall._candidate_text (labelled fields, desc<=400)."""
    desc = (getattr(record, "description", "") or "").strip().replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:400]
    return (
        f"{record.dataset_name} | 物种 {record.species} | 组织 {record.tissue} | 疾病 {record.disease} "
        f"| 平台 {record.platform_family} | 实验 {record.assay} | {desc}"
    )


def load_uid_records() -> dict[str, object]:
    from evaluate_recommendation import load_pipeline

    _settings, records = load_pipeline()
    out = {}
    for r in records:
        raw = getattr(r, "raw", None)
        uid = str(raw.get("dataset_uid") or "") if isinstance(raw, dict) else ""
        if uid:
            out[uid] = r
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    embedder = load_embedder(default_model_dir())
    if embedder is None:
        print("embedding model not available locally; run model_worker --download first",
              file=sys.stderr)
        return 2

    started = time.time()
    uid_records = load_uid_records()
    vec_cache: dict[str, list[float]] = {}

    from evaluate_recommendation import load_pipeline  # for keyword_mapping
    settings, _records = load_pipeline()

    stats = {"queries": 0, "pairs": 0, "vec_null": 0, "records_embedded": 0}
    for split in SPLITS:
        for tier in (f"synth_{split}_labeled.jsonl", f"synth_{split}_nl.jsonl", f"synth_{split}.jsonl"):
            src = args.in_dir / tier
            if src.exists():
                break
        else:
            continue
        dst = args.in_dir / f"synth_{split}_vec.jsonl"
        done = set()
        if dst.exists():
            done = {row["query_id"] for row in read_jsonl(dst)}
        rows = read_jsonl(src)
        todo = [r for r in rows if r["query_id"] not in done]
        if args.limit:
            todo = todo[: args.limit]
        print(f"[{split}] total={len(rows)} done={len(done)} todo={len(todo)} src={src.name}", flush=True)

        with dst.open("a", encoding="utf-8", newline="\n") as out:
            for i, row in enumerate(todo, 1):
                query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
                query = query or row["query"]
                intent = parse_query(query, settings.keyword_mapping)
                q_text = expand_query_bilingual(query, intent)
                q_vec = embedder([q_text])[0]

                new_uids = [
                    c["uid"] for c in row["candidates"]
                    if c.get("uid") and c["uid"] in uid_records and c["uid"] not in vec_cache
                ]
                if new_uids:
                    texts = [candidate_text(uid_records[u]) for u in new_uids]
                    for uid, vec in zip(new_uids, embedder(texts)):
                        vec_cache[uid] = vec
                    stats["records_embedded"] += len(new_uids)

                for cand in row["candidates"]:
                    vec = vec_cache.get(cand.get("uid") or "")
                    if vec is None:
                        cand["vec_score"] = None
                        stats["vec_null"] += 1
                    else:
                        cand["vec_score"] = sum(a * b for a, b in zip(q_vec, vec))
                    stats["pairs"] += 1
                stats["queries"] += 1
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                if i % 100 == 0:
                    out.flush()
                    print(f"  [{split}] {i}/{len(todo)} ({stats['records_embedded']} records embedded)",
                          flush=True)
        out.close()

    stats["elapsed_s"] = round(time.time() - started, 1)
    (args.in_dir / "_vec_stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
