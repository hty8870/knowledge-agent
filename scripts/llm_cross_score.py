# -*- coding: utf-8 -*-
"""Cross-model LLM semantic labelling of LTR query-document pairs.

Stage-3b of the LTR data pipeline. A single LLM pseudo-label is biased by that
model's own judgement style; this script labels every candidate pair with
several heterogeneous models (served by an OpenAI-compatible gateway) and
aggregates a consensus grade. The prompt, the 0-3 anchors and the metadata
truncation are reused verbatim from ``llm_label_pairs`` so the consensus label
stays semantically comparable to the single-model one.

For every candidate the script adds:

- ``xscore``: ``{model_id: 0..3 | None}`` per-judge grades
- ``llm_semantic_single``: the previous single-model grade (preserved)
- ``llm_semantic``: overwritten with the rounded median of the non-null
  cross-model grades (the consensus label consumed by training)
- ``xscore_n`` / ``xscore_spread``: judge count and max-min disagreement

The gateway key is read from the ``OPENCODE_GO_API_KEY`` environment variable
and is never written to disk. Output is incremental and resumable: pairs whose
``xscore`` already covers every requested model are skipped.

Usage:
  OPENCODE_GO_API_KEY=... python scripts/llm_cross_score.py --limit 2   # smoke
  OPENCODE_GO_API_KEY=... python scripts/llm_cross_score.py             # full
"""
from __future__ import annotations

import argparse
import io
import json
import os
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from llm_label_pairs import PROMPT, load_uid_texts, pair_key  # noqa: E402

API_URL = "https://opencode.ai/zen/go/v1/chat/completions"
MESSAGES_URL = "https://opencode.ai/zen/go/v1/messages"
#: Models served by the gateway's Anthropic-style /messages endpoint.
MESSAGES_MODELS = {"qwen3.8-flash", "minimax-m3", "minimax-m2.7", "minimax-m2.5",
                   "qwen3.8-max", "qwen3.7-max", "qwen3.7-plus", "qwen3.6-plus"}
#: Models served by the gateway's OpenAI Responses endpoint. These reject the
#: temperature parameter (fixed at 1), so scores carry sampling variance.
RESPONSES_MODELS = {"gpt-5.6-luna", "grok-4.6", "muse-spark-1.3-contributor",
                    "muse-spark-1.2-contributor"}
DEFAULT_MODELS = ["deepseek-v4-pro", "mimo-v2.5", "gpt-5.6-luna"]
DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_print_lock = threading.Lock()
_session_id = str(uuid.uuid4())


RESPONSES_URL = "https://opencode.ai/zen/go/v1/responses"


def call_gateway(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int = 180) -> str:
    if model in MESSAGES_MODELS:
        return _call_messages(prompt, model, api_key, max_tokens, timeout)
    if model in RESPONSES_MODELS:
        return _call_responses(prompt, model, api_key, max_tokens, timeout)
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"].get("content")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return str(content)


def _call_responses(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "input": prompt,
        "max_output_tokens": max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(RESPONSES_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    if data.get("status") not in (None, "completed"):
        raise ValueError(f"response status={data.get('status')}")
    content = "".join(
        block.get("text", "") for out in data.get("output", [])
        if isinstance(out, dict) and out.get("type") == "message"
        for block in out.get("content", [])
        if isinstance(block, dict) and block.get("type") == "output_text")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return content


def _call_messages(prompt: str, model: str, api_key: str, max_tokens: int, timeout: int) -> str:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(MESSAGES_URL, data=body, headers={
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
        "x-opencode-session": _session_id,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    content = "".join(
        block.get("text", "") for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text")
    if not content:
        raise ValueError("empty content (reasoning exhausted budget)")
    return content


def score_one(query: str, uid: str, texts: dict, model: str,
              api_key: str, max_tokens: int, retries: int = 3):
    meta = texts.get(uid) or {"title": uid, "description": ""}
    prompt = PROMPT.format(query=query, title=meta["title"], description=meta["description"])
    for attempt in range(retries + 1):
        try:
            text = call_gateway(prompt, model, api_key, max_tokens)
            start = text.find("{")
            payload, _ = json.JSONDecoder().raw_decode(text, start)
            score = int(payload["score"])
            if 0 <= score <= 3:
                return model, score
        except urllib.error.HTTPError as exc:
            wait = 20 * (attempt + 1) if exc.code == 429 else 1.5 * (attempt + 1)
            time.sleep(wait)
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return model, None


def aggregate(xscore: dict):
    vals = [v for v in xscore.values() if v is not None]
    if not vals:
        return None, 0, None
    median = statistics.median(vals)
    return int(round(median)), len(vals), max(vals) - min(vals)


def done_keys_for(dst: Path, models: list) -> set:
    done = set()
    if not dst.exists():
        return done
    for line in dst.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        for cand in row["candidates"]:
            xs = cand.get("xscore") or {}
            if all(m in xs for m in models):
                done.add(pair_key(row["query_id"], cand["uid"]))
    return done


def run_split(split: str, in_dir: Path, texts: dict, args, api_key: str) -> None:
    src = in_dir / f"synth_{split}_vec.jsonl"
    dst = in_dir / f"synth_{split}_xscore.jsonl"
    done = done_keys_for(dst, args.models)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]
    print(f"[{split}] rows={len(rows)} already_scored={len(done)} models={args.models}", flush=True)
    t0 = time.time()
    with dst.open("a", encoding="utf-8", newline="\n") as out:
        for i, row in enumerate(rows, 1):
            todo = [c for c in row["candidates"]
                    if c.get("uid") and pair_key(row["query_id"], c["uid"]) not in done]
            if not todo:
                continue
            query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
            query = query or row["query"]
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {}
                for cand in todo:
                    for model in args.models:
                        fut = pool.submit(score_one, query, cand["uid"], texts, model,
                                          api_key, args.max_tokens)
                        futures[fut] = cand
                for fut in as_completed(futures):
                    cand = futures[fut]
                    model, score = fut.result()
                    cand.setdefault("xscore", {})[model] = score
            for cand in todo:
                if "llm_semantic_single" not in cand:
                    cand["llm_semantic_single"] = cand.get("llm_semantic")
                consensus, n_judges, spread = aggregate(cand.get("xscore") or {})
                cand["llm_semantic"] = consensus
                cand["xscore_n"] = n_judges
                cand["xscore_spread"] = spread
                cand["label_model"] = "xscore:" + ",".join(args.models)
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            out.flush()
            if i % 10 == 0 or i == len(rows):
                rate = i / max(1e-9, time.time() - t0)
                with _print_lock:
                    print(f"  [{split}] {i}/{len(rows)} queries "
                          f"({rate:.2f} q/s, elapsed {time.time()-t0:.0f}s)", flush=True)


def run_split_pipelined(split: str, in_dir: Path, texts: dict, args, api_key: str) -> None:
    """Pipelined engine: one global task pool across all rows (no per-row
    barrier), so workers stay saturated instead of idling behind a row's
    slowest judge. Rows are written as soon as all their scores land, in any
    order; resume semantics are unchanged (query-level checkpoint file)."""
    src = in_dir / f"synth_{split}_vec.jsonl"
    dst = in_dir / f"synth_{split}_xscore.jsonl"
    done = done_keys_for(dst, args.models)
    rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.limit:
        rows = rows[: args.limit]
    pending = []
    for row in rows:
        todo = [c for c in row["candidates"]
                if c.get("uid") and pair_key(row["query_id"], c["uid"]) not in done]
        if todo:
            pending.append((row, todo))
    total_calls = sum(len(t) * len(args.models) for _, t in pending)
    print(f"[{split}] pipelined rows={len(pending)} calls={total_calls} "
          f"workers={args.concurrency} models={args.models}", flush=True)
    t0 = time.time()
    write_lock = threading.Lock()
    finished_rows = 0
    with dst.open("a", encoding="utf-8", newline="\n") as out:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {}
            for row, todo in pending:
                query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
                query = query or row["query"]
                for cand in todo:
                    for model in args.models:
                        fut = pool.submit(score_one, query, cand["uid"], texts, model,
                                          api_key, args.max_tokens)
                        futures[fut] = (row, todo, cand)
            for fut in as_completed(futures):
                row, todo, cand = futures[fut]
                model, score = fut.result()
                cand.setdefault("xscore", {})[model] = score
                if all(all(m in (c.get("xscore") or {}) for m in args.models) for c in todo):
                    with write_lock:
                        if row.get("_written"):
                            continue
                        row["_written"] = True
                        for c in todo:
                            if "llm_semantic_single" not in c:
                                c["llm_semantic_single"] = c.get("llm_semantic")
                            consensus, n_judges, spread = aggregate(c.get("xscore") or {})
                            c["llm_semantic"] = consensus
                            c["xscore_n"] = n_judges
                            c["xscore_spread"] = spread
                            c["label_model"] = "xscore:" + ",".join(args.models)
                        out.write(json.dumps(row, ensure_ascii=False) + "\n")
                        out.flush()
                        finished_rows += 1
                        if finished_rows % 25 == 0:
                            rate = finished_rows / max(1e-9, time.time() - t0)
                            eta = (len(pending) - finished_rows) / max(1e-9, rate) / 3600
                            print(f"  [{split}] {finished_rows}/{len(pending)} rows "
                                  f"({rate:.2f} rows/s, eta {eta:.1f}h)", flush=True)
    print(f"[{split}] pipelined done: {finished_rows} rows in {time.time()-t0:.0f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", type=lambda s: [m.strip() for m in s.split(",") if m.strip()],
                        default=DEFAULT_MODELS)
    parser.add_argument("--splits", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                        default=["train", "heldout"])
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--limit", type=int, default=0, help="max queries per split (smoke)")
    parser.add_argument("--concurrency", type=int, default=16)
    parser.add_argument("--max-tokens", type=int, default=4096)
    parser.add_argument("--engine", choices=("rowwise", "pipelined"), default="pipelined")
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    api_key = os.environ.get("OPENCODE_GO_API_KEY")
    if not api_key:
        print("OPENCODE_GO_API_KEY is not set; aborting", file=sys.stderr)
        return 2
    texts = load_uid_texts()
    runner = run_split_pipelined if args.engine == "pipelined" else run_split
    for split in args.splits:
        runner(split, args.in_dir, texts, args, api_key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
