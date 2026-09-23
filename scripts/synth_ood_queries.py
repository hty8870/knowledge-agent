# -*- coding: utf-8 -*-
"""Generate an OOD (out-of-distribution) query set: real-register, corner-case
queries for evaluating the retrieval+ranking pipeline off the template
distribution.

Ten classes × 30 queries = 300, LLM-generated (cheap flash-tier judge) with the
controlled vocabulary as the constraint space, so every constraint-carrying
query keeps a deterministic ground truth. Classes 5/8/10 carry no retrieval
constraints by design (they test degrade / honest-miss / fast-path behavior).

Each line of ``research/ood_eval/ood_queries.jsonl``:
  {qid, query, ood_class, constraints, excluded, expected, parse: {...}}

Usage:
  OPENCODE_GO_API_KEY=... python scripts/synth_ood_queries.py            # all classes
  OPENCODE_GO_API_KEY=... python scripts/synth_ood_queries.py --only typo_pinyin --n 5
"""
from __future__ import annotations

import argparse
import io
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

from llm_cross_score import call_gateway  # noqa: E402
from synth_ltr_data import dim_entries  # noqa: E402
from dataset_recommender.retrieval.query_parser import active_filters, parse_query  # noqa: E402

OUT_DIR = AGENT_ROOT / "research" / "ood_eval"
GEN_MODEL = "deepseek-flash"
PER_CLASS = 30
BATCH = 10

CLASS_SPECS = {
    "typo_pinyin": "错字/拼音混输：主要是中文句子里夹一两个错字、拼音词或谐音字（如 肺腺癌→肺线癌/feixianai 数据、单细胞→danxibao、胶质母细胞瘤→胶質/交质），语法可以不顺；整条全拼音的最多五分之一。",
    "colloquial_context": "真人口语带语境：像研究生随口说的/微信里发的，带个人背景或缘由（如 老板让找…、做毕设想…、我们课题组要…），约束埋在闲聊里。",
    "mixed_english": "中英混杂或全英文：约束词用英文术语/缩写表达（如 human NSCLC scRNA-seq, with raw fastq），可以是完整英文句。",
    "jargon_abbrev": "领域黑话缩写：用 TCGA 式缩写、癌种缩写（LUAD/GBM/CRC/HCC）、技术黑话（FFPE、空间转录组、snRNA、VDJ），少解释。",
    "underspecified": "欠定/泛化：几乎不给有效约束（如 有没有单细胞数据、找点能下载的测序数据、有什么好用的数据集），测试系统面对空约束的行为。constraints 给 {}。",
    "multi_intent": "多意图/比较：一条查询里要两个及以上并列对象（如 肺癌和肝癌的都想要、结肠原位和肝转移都看），constraints 用 disease 列表表达多个值。",
    "negation": "否定/排除：明确排除某约束（如 不要小鼠的、除了脑以外、只要人的不要其他物种），constraints 给肯定约束，excluded 给被排除的 {dim: value}。",
    "wrong_premise": "错误前提：要的东西在公开单细胞目录里不存在（如 恐龙化石单细胞测序、唐代古DNA单细胞、外星人组织），测试系统是否诚实回答没有。constraints 给 {}。",
    "verbose_ramble": "长啰嗦：50-120 字，先说一大段研究背景/之前做过什么，最后才带出需求，约束埋在句子里。",
    "accession_direct": "编号直达：直接给 GEO/ENA 风格的登录号问有没有（如 GSE123456 有吗、有没有 PRJNA 开头的脑数据），编号可虚构。constraints 给 {}。",
}

PROMPT = """为生物医学单细胞数据检索系统构造评测查询。类别：{classname}
要求：{spec}

从下列受控词表中挑选约束值（需要约束的类别）：
- species 可选: {species}
- tissue 可选: {tissue}
- disease 可选: {disease}

生成 {n} 条互不相同的查询。只输出 JSON 数组，每个元素：
[{{"query": "<查询原文>", "constraints": {{"disease": "<词表值>", ...}}, "excluded": {{}}, "expected": "<一句话说明理想行为>"}}, ...]
constraints 的值必须是词表里的英文原值；query 文本里不要直接出现该英文原值（用中文别名/错字/黑话/口语表达它）。不要输出任何其他内容。"""


def vocab_block(rng: random.Random) -> tuple[str, str, str]:
    def pick(dim, k):
        entries = dim_entries(dim)
        vals = [e["value"] for e in entries]
        return ", ".join(rng.sample(vals, min(k, len(vals))))
    return pick("species", 5), pick("tissue", 15), pick("disease", 15)


def parse_items(text: str) -> list[dict]:
    start = text.find("[")
    arr, _ = json.JSONDecoder().raw_decode(text, start)
    return [x for x in arr if isinstance(x, dict) and x.get("query")]


def gen_batch(classname: str, spec: str, n: int, api_key: str, rng: random.Random,
              retries: int = 3) -> list[dict]:
    species, tissue, disease = vocab_block(rng)
    prompt = PROMPT.format(classname=classname, spec=spec, n=n,
                           species=species, tissue=tissue, disease=disease)
    for attempt in range(retries + 1):
        try:
            return parse_items(call_gateway(prompt, GEN_MODEL, api_key, 8192))
        except Exception:
            time.sleep(2 * (attempt + 1))
    return []


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", default=None)
    parser.add_argument("--n", type=int, default=PER_CLASS)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260911,
                        help="rng seed for vocab sampling (use a fresh one per expansion epoch)")
    parser.add_argument("--qid-prefix", default="ood",
                        help="qid prefix (use a distinct one per expansion epoch, e.g. ood2)")
    args = parser.parse_args()
    import os
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass
    api_key = os.environ.get("OPENCODE_GO_API_KEY")
    if not api_key:
        print("OPENCODE_GO_API_KEY is not set", file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    valid = {dim: {e["value"] for e in dim_entries(dim)} for dim in ("species", "tissue", "disease")}
    classes = {args.only: CLASS_SPECS[args.only]} if args.only else CLASS_SPECS
    out_path = OUT_DIR / "ood_queries.jsonl"
    seen = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                seen.add(json.loads(line)["query"].strip().lower())
    written = 0
    with out_path.open("a", encoding="utf-8", newline="\n") as out:
        for classname, spec in classes.items():
            got: list[dict] = []
            calls = max(1, (args.n + BATCH - 1) // BATCH)
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                futs = [pool.submit(gen_batch, classname, spec, BATCH, api_key, rng)
                        for _ in range(calls)]
                for fut in as_completed(futs):
                    got.extend(fut.result())
            kept = 0
            for item in got:
                q = str(item.get("query") or "").strip()
                cons = item.get("constraints") or {}
                if not (4 <= len(q) <= 160):
                    continue
                if q.lower() in seen:
                    continue
                bad = any(dim in valid and val not in valid[dim]
                          for dim, val in cons.items() if isinstance(val, str))
                if bad:
                    continue
                seen.add(q.lower())
                parsed = parse_query(q)
                filters = active_filters(parsed)
                row = {
                    "qid": f"{args.qid_prefix}-{classname}-{kept:03d}",
                    "query": q,
                    "ood_class": classname,
                    "constraints": cons,
                    "excluded": item.get("excluded") or {},
                    "expected": str(item.get("expected") or "")[:200],
                    "parse": {
                        "filters": filters or [],
                        "unresolved": list(getattr(parsed, "unresolved_terms", []) or []),
                    },
                }
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
                kept += 1
                written += 1
                if kept >= args.n:
                    break
            out.flush()
            print(f"[{classname}] generated={len(got)} kept={kept}", flush=True)
    print(f"total written: {written} -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
