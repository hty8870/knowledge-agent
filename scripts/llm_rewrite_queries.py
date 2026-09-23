# -*- coding: utf-8 -*-
"""Naturalise synthesized LTR queries with an LLM, keeping constraints intact.

Stage-2 of the LTR data pipeline: ``synth_ltr_data.py`` produces template-rendered
queries; this script rewrites each one into more natural phrasing via an
OpenAI-compatible chat model, without spending any judgement on labels.

Guards (all deterministic, no LLM in the loop):
- the rewritten query must re-parse as executable;
- every intended hard dimension must still parse as an `include` filter, and
  every soft dimension as a `prefer` filter (same assertions as the synthesizer);
- a raw-data requirement, when present, must still be detected.

A rewrite that fails validation is retried (up to ``--retries`` times); if all
attempts fail, the original template query is kept and ``rewrite_status`` is set
to ``"fallback"`` so downstream consumers can tell. Output is incremental and
resumable: query ids already present in the output file are skipped on reruns.

Usage:
  python scripts/llm_rewrite_queries.py --limit 30          # smoke run
  python scripts/llm_rewrite_queries.py                     # full run, resumable
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
from dataset_recommender.retrieval.query_parser import active_filters, parse_query  # noqa: E402

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_IN_DIR = AGENT_ROOT / "research" / "ltr_data"
SPLITS = ("train", "heldout")

# Style buckets rotate by query id so the rewritten corpus does not collapse
# onto a single LLM-favourite phrasing. Measured constraint: the project's query
# parser abstains on social-register phrasing (greetings, honorifics, trailing
# modal particles), so every style stays in "typed into a search box" register —
# personas that produce forum-style chatter were tried and rejected (see the
# research log, 2026-09-10).
STYLES = (
    "关键词串联式：逗号分隔的电报式短语，不成句",
    "简短完整的句子，把硬性约束放在句首",
    "简短完整的句子，把软偏好放在句尾",
    "口语化但直接的问句，不加称呼、不加寒暄",
    "备注式陈述（“做X研究，需要Y数据”这类需求说明口吻）",
    "需求陈述式开头（“需要/想找/打算做”），一句话说完",
)

PROMPT = """你是中文科研数据检索场景的用户模拟器。把下面这条「模板生成」的检索查询改写成更自然、但仍然是「直接输入搜索框」风格的中文查询。

要求：
1. 原查询表达的所有约束必须保留，语义不变（可以换措辞，但不能增删约束）：
{constraint_lines}
2. 疾病、组织、物种等生物医学实体名词保持原词、彼此分开，不要改写、泛化或合并（例如"前列腺 肿瘤"不要合成"前列腺肿瘤"）。
3. 软偏好只能用以下句式之一表达（这些是本系统认可的软偏好标记）：
   "最好X" / "最好是X" / "倾向于X" / "优先考虑X" / "如果有X更好" / "如果有X的话" / "优先X"
   注意"优先"必须放在名词前面（"优先小鼠"），绝不允许写成"小鼠优先"——那样会变成强制要求。
4. {raw_line}
5. 用以下风格写：{style}。
6. 这是在搜索框里输入的查询，不是在论坛发帖：禁止称呼、问候、自我介绍、语气助词结尾（呀/呢/哦/吗）、以及任何社交句式。
7. 只输出改写后的查询句本身，不要任何解释、引号或前后缀。

原查询：{query}"""

DIM_LABEL = {"species": "物种", "tissue": "组织/样本类型", "disease": "疾病/状态"}

_print_lock = threading.Lock()


def build_prompt(spec: dict, style: str) -> str:
    lines = []
    for dim in spec.get("hard_dims", []):
        value = spec["constraints"].get(dim)
        if value:
            lines.append(f"- 硬性约束：{DIM_LABEL.get(dim, dim)} = {value}")
    for dim in spec.get("soft_dims", []):
        value = spec["constraints"].get(dim)
        if value:
            lines.append(f"- 软偏好（非强制）：{DIM_LABEL.get(dim, dim)} = {value}")
    raw_line = (
        "原查询要求能下载原始测序数据（FASTQ），改写后必须保留这一硬性要求，且措辞用「需要包含 FASTQ 原始数据」或「要能下载到原始数据」这类贴近原句的说法。"
        if spec.get("has_raw_data_required")
        else "原查询没有原始数据要求，改写后也不要添加。"
    )
    return PROMPT.format(
        constraint_lines="\n".join(lines) or "- （无结构化约束）",
        raw_line=raw_line,
        style=style,
        query=spec["query"],
    )


def validate(text: str, spec: dict, keyword_mapping) -> bool:
    if not text or len(text) < 4 or "\n" in text:
        return False
    intent = parse_query(text, keyword_mapping)
    if getattr(intent, "parse_status", "") != "executable":
        return False
    include_dims = {
        f.get("dim") for f in active_filters(intent) if f.get("polarity") == "include"
    }
    prefer_dims = {
        f.get("dim") for f in active_filters(intent) if f.get("polarity") == "prefer"
    }
    if not set(spec["hard_dims"]).issubset(include_dims):
        return False
    if not set(spec["soft_dims"]).issubset(prefer_dims):
        return False
    if spec.get("has_raw_data_required") and getattr(intent, "has_raw_data_required", None) is not True:
        return False
    return True


def extract_text(result) -> str:
    text = getattr(result, "text", result)
    if text is None:
        return ""
    out = str(text).strip().strip('"').strip("「」").strip()
    return "" if out.lower() == "none" else out


def rewrite_one(spec: dict, cfg, keyword_mapping, retries: int, pause: float) -> dict:
    rng = random.Random(f"{spec['query_id']}:{spec['cluster_id']}")
    style = STYLES[rng.randrange(len(STYLES))]
    prompt = build_prompt(spec, style)
    last_error = ""
    rejected: list[str] = []
    for attempt in range(retries + 1):
        try:
            result = call_openai_compatible(prompt, cfg)
            text = extract_text(result)
            if validate(text, spec, keyword_mapping):
                out = {"query_nl": text, "rewrite_status": "ok", "rewrite_style": style}
                if rejected:
                    out["rewrite_rejected"] = rejected[0]  # keep first failed attempt for parser research
                return out
            last_error = "validation"
            if text and text != spec["query"]:
                rejected.append(text)
        except Exception as exc:  # rate limits, network — back off and retry
            last_error = f"{type(exc).__name__}"
            time.sleep(pause * (attempt + 1) * 2)
    out = {"query_nl": spec["query"], "rewrite_status": f"fallback:{last_error}", "rewrite_style": style}
    if rejected:
        out["rewrite_rejected"] = rejected[0]
    return out


def load_done_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        json.loads(line)["query_id"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--in-dir", type=Path, default=DEFAULT_IN_DIR)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between batches per worker")
    args = parser.parse_args()
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    base_cfg = load_llm_config()
    if base_cfg.api_key is None:
        print("no LLM api key configured; aborting", file=sys.stderr)
        return 2
    cfg = replace(base_cfg, model=args.model)

    from evaluate_recommendation import load_pipeline  # deferred: heavy import

    settings, _records = load_pipeline()

    stats = {"ok": 0, "fallback": 0, "skipped_done": 0, "model": args.model}
    for split in SPLITS:
        src = args.in_dir / f"synth_{split}.jsonl"
        dst = args.in_dir / f"synth_{split}_nl.jsonl"
        rows = [json.loads(line) for line in src.read_text(encoding="utf-8").splitlines() if line.strip()]
        done = load_done_ids(dst)
        todo = [r for r in rows if r["query_id"] not in done]
        if args.limit:
            todo = todo[: args.limit]
        stats["skipped_done"] += len(rows) - len(todo) if not args.limit else len(done)
        print(f"[{split}] total={len(rows)} done={len(done)} todo={len(todo)}", flush=True)

        with dst.open("a", encoding="utf-8", newline="\n") as out:
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futures = {
                    pool.submit(rewrite_one, spec, cfg, settings.keyword_mapping, args.retries, args.pause): spec
                    for spec in todo
                }
                for i, fut in enumerate(as_completed(futures), 1):
                    spec = futures[fut]
                    patch = fut.result()
                    stats["ok" if patch["rewrite_status"] == "ok" else "fallback"] += 1
                    line = {
                        **spec,
                        **patch,
                        "rewrite_model": args.model,
                    }
                    out.write(json.dumps(line, ensure_ascii=False) + "\n")
                    out.flush()
                    if i % 50 == 0:
                        with _print_lock:
                            print(f"  [{split}] {i}/{len(todo)}", flush=True)
                    time.sleep(0)  # yield; real pacing is per-worker retry backoff
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
