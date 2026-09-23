# -*- coding: utf-8 -*-
"""LLM pseudo-labelling of synthesized LTR query–document pairs.

Stage-3 of the LTR data pipeline. The deterministic judge
(``constraint_satisfied``) already scores constraint satisfaction; this script
adds an orthogonal *semantic relevance* grade (0–3) from an LLM judging the
query against the dataset's title/description. The two label families stay in
separate fields — they are combined only later, at training time.

Two modes:
- ``--calibrate N``: sample N pairs from the held-out split (stratified by the
  deterministic grade), label each with both the reference model
  (``--ref-model``) and the candidate bulk model (``--model``), and write a
  comparison report with exact / ±1 / quadratic-kappa / binary agreement. Run
  this first; only bulk-label with the flash model if it agrees with the
  reference well enough.
- default: bulk-label every candidate pair, incrementally and resumably.

Anchors are fixed inside the prompt with one worked example per grade; the
model answers strict JSON ``{"score": 0..3, "reason": ...}`` at temperature 0.

Usage:
  python scripts/llm_label_pairs.py --calibrate 150
  python scripts/llm_label_pairs.py --model deepseek-v4-flash   # bulk, resumable
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from dataset_recommender.llm.llm_client import call_openai_compatible, load_llm_config  # noqa: E402

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_REF_MODEL = "deepseek-chat"
DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
SPLITS = ("train", "heldout")
MAX_DESC_CHARS = 600

PROMPT = """你是生物医学数据检索的相关性评审员。给定一条用户检索查询和一个公开单细胞数据集的元数据，请判断该数据集对该查询的语义相关程度，打 0-3 分：

3 = 高度相关：数据集主题、样本属性与查询的研究需求高度匹配，用户大概率会直接使用
2 = 基本相关：大方向匹配，但样本类型/研究设计等细节一般，或元数据信息不足以确认完全匹配
1 = 弱相关：仅部分沾边，元数据里有明显偏离查询需求的信号
0 = 不相关：主题或样本属性与查询需求不符

锚定示例：
- 查询「肺癌血液的数据」+ 元数据「非小细胞肺癌患者外周血单细胞图谱」→ 3
- 查询「肺癌血液的数据」+ 元数据「肺癌类器官单细胞测序」→ 2（癌种对但样本来源不同）
- 查询「肺癌血液的数据」+ 元数据「健康人肺组织图谱」→ 1（器官沾边但无癌症、无血液）
- 查询「肺癌血液的数据」+ 元数据「小鼠胚胎脑发育图谱」→ 0

只输出 JSON：{{"score": <0|1|2|3>, "reason": "<一句话>"}}

查询：{query}

数据集元数据：
标题：{title}
描述：{description}"""


def pair_key(query_id: str, uid: str) -> str:
    return f"{query_id}::{uid}"


def load_uid_texts() -> dict[str, dict]:
    """uid -> {title, description} from the frozen catalog."""
    from evaluate_recommendation import load_pipeline

    _settings, records = load_pipeline()
    out = {}
    for r in records:
        raw = getattr(r, "raw", None)
        if not isinstance(raw, dict):
            continue
        uid = str(raw.get("dataset_uid") or "")
        if not uid:
            continue
        out[uid] = {
            "title": str(raw.get("title") or "")[:200],
            "description": str(raw.get("description") or "")[:MAX_DESC_CHARS],
        }
    return out


_print_lock = threading.Lock()


def label_one(query: str, uid: str, texts: dict[str, dict], cfg, retries: int = 3) -> dict:
    meta = texts.get(uid) or {"title": uid, "description": ""}
    prompt = PROMPT.format(query=query, title=meta["title"], description=meta["description"])
    for attempt in range(retries + 1):
        try:
            result = call_openai_compatible(prompt, cfg)
            text = getattr(result, "text", result) or ""
            start = str(text).find("{")
            payload = json.loads(str(text)[start:])
            score = int(payload["score"])
            if 0 <= score <= 3:
                return {"llm_semantic": score, "llm_reason": str(payload.get("reason", ""))[:200]}
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return {"llm_semantic": None, "llm_reason": "label_failed"}


def iter_pairs(in_dir: Path, splits=SPLITS):
    for split in splits:
        path = in_dir / f"synth_{split}_nl.jsonl"
        if not path.exists():
            path = in_dir / f"synth_{split}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
            query = query or row["query"]
            for cand in row["candidates"]:
                yield split, row, query, cand


def calibrate(n: int, in_dir: Path, cfg_base, args) -> dict:
    rng = random.Random(20260910)
    pool: list[tuple] = []
    for split, row, query, cand in iter_pairs(in_dir, splits=("heldout",)):
        if cand.get("grade", 0) >= 1 and cand.get("uid"):
            pool.append((row["query_id"], query, cand))
    by_grade: dict[int, list] = {}
    for item in pool:
        by_grade.setdefault(item[2]["grade"], []).append(item)
    sample = []
    per = max(1, n // max(1, len(by_grade)))
    for g, items in sorted(by_grade.items()):
        rng.shuffle(items)
        sample.extend(items[:per])
    rng.shuffle(sample)
    sample = sample[:n]
    print(f"calibration sample: {len(sample)} pairs "
          f"({{g: len(v) for g, v in by_grade.items()}} available)", flush=True)

    texts = load_uid_texts()
    ref_cfg = replace(cfg_base, model=args.ref_model)
    cand_cfg = replace(cfg_base, model=args.model)
    rows = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool_exec:
        futures = {}
        for query_id, query, cand in sample:
            key = pair_key(query_id, cand["uid"])
            futures[pool_exec.submit(label_one, query, cand["uid"], texts, ref_cfg)] = (key, "ref")
            futures[pool_exec.submit(label_one, query, cand["uid"], texts, cand_cfg)] = (key, "cand")
        done = 0
        for fut in as_completed(futures):
            key, which = futures[fut]
            res = fut.result()
            rows.append({"key": key, "which": which, **res})
            done += 1
            if done % 40 == 0:
                with _print_lock:
                    print(f"  … {done}/{len(futures)} labels", flush=True)

    grades = {pair_key(q, c["uid"]): c["grade"] for q, _q, c in sample}
    paired: dict[str, dict] = {}
    for r in rows:
        paired.setdefault(r["key"], {})[r["which"]] = r["llm_semantic"]
    both = [(v["ref"], v["cand"]) for k, v in paired.items()
            if v.get("ref") is not None and v.get("cand") is not None]
    exact = sum(1 for a, b in both if a == b) / max(1, len(both))
    within1 = sum(1 for a, b in both if abs(a - b) <= 1) / max(1, len(both))
    binary = sum(1 for a, b in both if (a >= 2) == (b >= 2)) / max(1, len(both))
    # quadratic weighted kappa on 0..3
    num = den = 0.0
    hist_r = [0] * 4
    hist_c = [0] * 4
    for a, b in both:
        hist_r[a] += 1
        hist_c[b] += 1
    total = max(1, len(both))
    for i in range(4):
        for j in range(4):
            w = (i - j) ** 2 / 9.0
            observed = sum(1 for a, b in both if a == i and b == j) / total
            expected = hist_r[i] * hist_c[j] / (total * total)
            num += w * observed
            den += w * expected
    kappa = 1 - num / den if den else 0.0
    report = {
        "n_sampled": len(sample),
        "n_paired": len(both),
        "ref_model": args.ref_model,
        "cand_model": args.model,
        "exact": round(exact, 4),
        "within_1": round(within1, 4),
        "binary_01_vs_23": round(binary, 4),
        "quadratic_kappa": round(kappa, 4),
        "labels": rows,
    }
    out_path = in_dir / "_calibration.json"
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "labels"}, ensure_ascii=False, indent=2))
    return report


def bulk(in_dir: Path, cfg, args) -> None:
    texts = load_uid_texts()
    for split in SPLITS:
        src = in_dir / f"synth_{split}_nl.jsonl"
        if not src.exists():
            src = in_dir / f"synth_{split}.jsonl"
        dst = in_dir / f"synth_{split}_labeled.jsonl"
        done_keys = set()
        if dst.exists():
            for line in dst.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    done_keys.update(pair_key(row["query_id"], c["uid"]) for c in row["candidates"])
        rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
        if args.limit:
            rows = rows[: args.limit]
        print(f"[{split}] rows={len(rows)} already_labeled={len(done_keys)}", flush=True)
        with dst.open("a", encoding="utf-8", newline="\n") as out:
            for i, row in enumerate(rows, 1):
                query = row.get("query_nl") if row.get("rewrite_status") == "ok" else row["query"]
                query = query or row["query"]
                todo = [c for c in row["candidates"]
                        if c.get("uid") and pair_key(row["query_id"], c["uid"]) not in done_keys]
                if not todo:
                    continue
                with ThreadPoolExecutor(max_workers=args.concurrency) as pool_exec:
                    results = list(pool_exec.map(
                        lambda c: label_one(query, c["uid"], texts, cfg), todo))
                for cand, res in zip(todo, results):
                    cand.update(res)
                    cand["label_model"] = args.model
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                out.flush()
                if i % 50 == 0:
                    print(f"  [{split}] {i}/{len(rows)} queries", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--ref-model", default=DEFAULT_REF_MODEL)
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--calibrate", type=int, default=0, metavar="N")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    cfg = load_llm_config()
    if cfg.api_key is None:
        print("no LLM api key configured; aborting", file=sys.stderr)
        return 2
    if args.calibrate:
        calibrate(args.calibrate, args.in_dir, cfg, args)
    else:
        bulk(args.in_dir, replace(cfg, model=args.model), args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
