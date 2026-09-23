# -*- coding: utf-8 -*-
"""对抗用例自动扩增器：把 `eval/agent_live_cases_v1.jsonl` 的 135 条
人工用例扩增出变体用例，持续压榨 agent（原集接近满分，评估意义衰减）。

核心设计约束——**expect 纪律**：
    变体的 expect 由本生成器从原用例**机械继承/机械合并**，LLM 只负责写新的
    utterance 文本和 difficulty 标注，绝不让 LLM 自由编写 expect（LLM 写的
    expect 错误率高）。

变体类型（按用例族适配）：
    paraphrase  换措辞（同义改写，expect 原样继承）——a/b/c/i 族
    noise       加噪（口语化/错字/中英混杂/啰嗦前缀，expect 原样继承）——a/b/i/j 族
    degrade     劣质指令化（缺主语/语序乱/信息残缺可推断，expect 原样继承，
                cat 改写为 J劣质指令）——b/k 族
    mix         混合意图（同族两条用例事项合并成一句，expect **机械合并**：
                must_steps 并集保序去重、max_steps 求和封顶 8——规则写死在
                merge_cases 里，LLM 不得染指）——b/k/l 族内两两组合

形状自检：每条变体写出前过 harness（scripts/evaluate_agent_live.py）的
`load_cases` 启动自检（单行临时文件），不合者丢弃并计数；最终产物整体再过一遍。

跑法（仓库根）：
    PYTHONPATH=src ./.venv/Scripts/python.exe scripts/augment_agent_cases.py \
        --out eval/agent_live_cases_aug.jsonl [--per-case 2] [--only k] [--seed 42] \
        [--limit 12] [--cases 主集路径]
离线自验（零 LLM 调用，只跑流程骨架 + 机械合并逻辑）：
    PYTHONPATH=src ./.venv/Scripts/python.exe scripts/augment_agent_cases.py --dry-run --limit 5
"""
from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import random
import re
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_recommender.llm.llm_client import call_llm, load_llm_config  # noqa: E402

_eal = None  # harness 模块缓存（延迟 import：被测工作树他人未提交改动可能暂坏，
             # 模块 import 不该连坐；shape_check 被真正调用时才硬失败）


def _harness():
    global _eal
    if _eal is None:
        import evaluate_agent_live
        _eal = evaluate_agent_live
    return _eal

#: 用例族 → 该族适配的变体类型（按序循环取用，凑够 --per-case 个）。
FAMILY_TYPES: dict[str, list[str]] = {
    "a": ["paraphrase", "noise"],
    "b": ["paraphrase", "noise", "degrade"],
    "c": ["paraphrase"],
    "i": ["paraphrase", "noise"],
    "j": ["noise"],
    "k": ["degrade", "mix"],
    "l": ["mix"],
}

#: mix 允许参与机械合并的 expect 键全集——出现此外任何键的用例不做混合意图
#: （must_when/steps_exact/report_* 等路径条件/截断/汇报断言没有安全的机械合并规则）。
_MIX_EXPECT_KEYS = {
    "first", "must_steps", "must_steps_unordered", "forbid_steps",
    "max_steps", "ideal_steps", "zero_writes", "no_ungrounded",
    "check_sources", "search_topics",
}

#: LLM 限速：每秒 ≤2 次调用（测试里可置 0）。
_MIN_INTERVAL = 0.5
_last_call_at = 0.0

_NORM_UTT_RE = re.compile(r"[\s，。！？、,.!?]+")


def _norm_utt(utterance: str) -> str:
    """utterance 撞车判定的归一形：标点/空白剥离（防「暂时不要检查10x的更新了。」
    与无句号版被判成两条）。"""
    return _NORM_UTT_RE.sub("", utterance or "")

_TYPE_BRIEF = {
    "paraphrase": "换措辞：同义改写，换措辞、换语序，语义与原句完全等价",
    "noise": "加噪：口语化、允许少量错别字、中英混杂、可加啰嗦前缀"
             "（如「那个啥，帮我…」），但核心指令仍清晰可辨",
    "degrade": "劣质指令化：缺主语、语序随意、信息残缺但可推断"
               "（参考「检査一下」「下栽下来」式错字口语碎片风）",
    "mix": "混合意图：把下面两句话合并成一句自然的中文指令，保留两句的"
           "全部事项、条件、来源名与主题词，可用「然后/接着/最后」连接",
}


# --------------------------------------------------------------------------- 机械规则（LLM 不得染指）

def variant_id(base_id: str, index: int) -> str:
    """普通变体 id：原 id + 字母后缀（k01 → k01a/k01b/…）。"""
    return f"{base_id}{chr(ord('a') + index)}"


def mix_id(id1: str, id2: str) -> str:
    """混合意图 id：k01+k05 → k01_k05x。"""
    return f"{id1}_{id2}x"


def _ordered_union(seq1: list, seq2: list) -> list:
    """并集保序去重（条目可哈希化口径：JSON 序列化文本）。"""
    out: list = []
    seen: set[str] = set()
    for item in list(seq1) + list(seq2):
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            out.append(copy.deepcopy(item))
    return out


def _must_key(want) -> tuple:
    """must 条目的语义去重键：字符串形 "curate.search_online" 与
    无约束对象形 {"verb": "curate.search_online"} 同键——否则并集后
    must_steps_unordered 会把「同一个搜索步」要求成两个不同步骤。"""
    if isinstance(want, str):
        return (want, "", "")
    return (str(want.get("verb") or ""), str(want.get("source") or ""),
            str(want.get("keywords") or ""))


def _must_union(seq1: list, seq2: list) -> list:
    """must_steps 并集保序、按 _must_key 语义去重（同键保留先出现者——
    c1 的对象形通常比 c2 的字符串形约束更细）。"""
    out: list = []
    seen: set[tuple] = set()
    for item in list(seq1) + list(seq2):
        key = _must_key(item)
        if key not in seen:
            seen.add(key)
            out.append(copy.deepcopy(item))
    return out


def _merge_outcome(o1, o2):
    """tools 剧本 outcome 递归合并；语义冲突（同一键不同结局）返回 None。"""
    if o1 == o2:
        return copy.deepcopy(o1)
    if (isinstance(o1, dict) and isinstance(o2, dict)
            and "by_source" in o1 and "by_source" in o2):
        t1, t2 = o1["by_source"], o2["by_source"]
        table: dict = {}
        for key in list(t1) + [k for k in t2 if k not in t1]:
            if key in t1 and key in t2:
                sub = _merge_outcome(t1[key], t2[key])
                if sub is None:
                    return None
                table[key] = sub
            elif key in t1:
                table[key] = copy.deepcopy(t1[key])
            else:
                table[key] = copy.deepcopy(t2[key])
        out = {"by_source": table}
        d1, d2 = o1.get("default"), o2.get("default")
        if d1 is not None and d2 is not None:
            merged = _merge_outcome(d1, d2)
            if merged is None:
                return None
            out["default"] = merged
        elif d1 is not None:
            out["default"] = copy.deepcopy(d1)
        else:
            out["default"] = copy.deepcopy(d2)
        return out
    return None


def mix_eligible(case: dict) -> bool:
    """混合意图参与资格：expect 键全在机械合并安全集内、有 first、
    有 must_steps/_unordered、无 context / allow_no_exec。"""
    if case.get("context") or case.get("allow_no_exec"):
        return False
    exp = case.get("expect") or {}
    if not set(exp) <= _MIX_EXPECT_KEYS:
        return False
    if "first" not in exp:
        return False
    return bool(exp.get("must_steps") or exp.get("must_steps_unordered"))


def merge_cases(c1: dict, c2: dict) -> dict | None:
    """混合意图的机械合并（写死规则，LLM 不得自由发挥 expect）：
    - first：两例 first 并集保序去重；
    - must_steps：并集保序去重；任一例是 unordered 形则整体落到
      must_steps_unordered（无序断言不能升格成有序）；
    - forbid_steps / check_sources / search_topics：并集保序去重；
    - max_steps：两例皆有才求和封顶 8；单边有则丢弃（小上界可能与合并后
      must 步数矛盾）；
    - ideal_steps：求和；超过合并后 max_steps 则丢弃（自检要求 ideal<=max）；
    - zero_writes：两例皆 true 才保留；no_ungrounded：任一例 true 即保留；
    - tools：逐动词 outcome 递归合并，语义冲突 → 返回 None（放弃该对）。
    其余字段（steps_exact/report_*/must_when 等）由 mix_eligible 提前排除。
    """
    e1, e2 = dict(c1["expect"]), dict(c2["expect"])
    exp: dict = {}

    first1 = e1["first"] if isinstance(e1["first"], list) else [e1["first"]]
    first2 = e2["first"] if isinstance(e2["first"], list) else [e2["first"]]
    exp["first"] = _ordered_union(first1, first2)

    m1 = e1.get("must_steps") or e1.get("must_steps_unordered") or []
    m2 = e2.get("must_steps") or e2.get("must_steps_unordered") or []
    merged_must = _must_union(m1, m2)
    if "must_steps_unordered" in e1 or "must_steps_unordered" in e2:
        exp["must_steps_unordered"] = merged_must
    else:
        exp["must_steps"] = merged_must

    if e1.get("forbid_steps") or e2.get("forbid_steps"):
        exp["forbid_steps"] = _ordered_union(
            e1.get("forbid_steps") or [], e2.get("forbid_steps") or [])

    if "max_steps" in e1 and "max_steps" in e2:
        exp["max_steps"] = min(int(e1["max_steps"]) + int(e2["max_steps"]), 8)
    # 单边 max_steps 不继承——小上界可能与合并后 must 的步数矛盾
    # （l01 无 max + l02 max=1 → 继承 1 会让三步 must 永不可满足）；缺省即不限。

    if "ideal_steps" in e1 and "ideal_steps" in e2:
        ideal = int(e1["ideal_steps"]) + int(e2["ideal_steps"])
        if "max_steps" not in exp or ideal <= exp["max_steps"]:
            exp["ideal_steps"] = ideal
    elif "ideal_steps" in e1:
        if "max_steps" not in exp or int(e1["ideal_steps"]) <= exp["max_steps"]:
            exp["ideal_steps"] = int(e1["ideal_steps"])
    elif "ideal_steps" in e2:
        if "max_steps" not in exp or int(e2["ideal_steps"]) <= exp["max_steps"]:
            exp["ideal_steps"] = int(e2["ideal_steps"])

    if e1.get("zero_writes") and e2.get("zero_writes"):
        exp["zero_writes"] = True
    if e1.get("no_ungrounded") or e2.get("no_ungrounded"):
        exp["no_ungrounded"] = True
    for key in ("check_sources", "search_topics"):
        if e1.get(key) or e2.get(key):
            exp[key] = _ordered_union(e1.get(key) or [], e2.get(key) or [])

    tools: dict = {}
    t1, t2 = c1.get("tools") or {}, c2.get("tools") or {}
    for verb in list(t1) + [v for v in t2 if v not in t1]:
        if verb in t1 and verb in t2:
            merged = _merge_outcome(t1[verb], t2[verb])
            if merged is None:
                return None
            tools[verb] = merged
        elif verb in t1:
            tools[verb] = copy.deepcopy(t1[verb])
        else:
            tools[verb] = copy.deepcopy(t2[verb])

    return {"cat": c1["cat"], "tools": tools, "expect": exp}


# --------------------------------------------------------------------------- 形状自检（harness 唯一真源）

def shape_check(case: dict) -> bool:
    """单条变体过 harness 的 load_cases 启动自检（单行临时文件；
    不合 → False，由调用方丢弃计数）。"""
    text = json.dumps(case, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".jsonl", delete=False) as fh:
        fh.write(text)
        tmp = Path(fh.name)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                _harness().load_cases(tmp)
            except SystemExit:
                return False
        return True
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------- LLM 出口（只写 utterance + difficulty）

def build_prompt(case: dict, vtype: str, partner: dict | None = None) -> str:
    lines = [
        "你是评测用例扩增器，为中文生物数据 agent 的探针用例生成对抗变体。",
        f"变体类型：{_TYPE_BRIEF[vtype]}。",
        "硬性约束：",
        "- 只写新的中文指令文本；不得新增或删除事项、条件、来源名、主题词。",
        "- 来源名（10x / ENCODE / ArrayExpress / CELLxGENE）与英文主题词"
        "（如 human lung）必须原样保留、逐字出现。",
        "- 只输出 JSON，不要任何解释："
        "{\"utterance\": \"...\", \"difficulty\": \"easy|medium|hard\"}",
    ]
    if vtype == "mix" and partner is not None:
        lines.append(f"原句一：{case['utterance']}")
        lines.append(f"原句二：{partner['utterance']}")
    else:
        lines.append(f"原句：{case['utterance']}")
    return "\n".join(lines)


def _parse_llm_variant(text: str) -> dict | None:
    """从 LLM 回复里抠出 {utterance, difficulty}；容错代码围栏与前后废话。"""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    utt = obj.get("utterance")
    if not isinstance(utt, str) or not utt.strip():
        return None
    difficulty = obj.get("difficulty")
    if difficulty not in ("easy", "medium", "hard"):
        difficulty = "medium"
    return {"utterance": utt.strip(), "difficulty": difficulty}


def _throttle() -> None:
    global _last_call_at
    gap = time.monotonic() - _last_call_at
    if gap < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - gap)
    _last_call_at = time.monotonic()


def llm_utterance(prompt: str, cfg, stats: dict) -> dict | None:
    """调 LLM 拿 {utterance, difficulty}；失败重试 1 次，仍失败 → None（计数）。"""
    for _attempt in range(2):
        _throttle()
        try:
            res = call_llm(prompt, cfg)
        except Exception:  # 单例失败不拖死全局
            res = None
        if res is not None and getattr(res, "succeeded", False) and res.text:
            parsed = _parse_llm_variant(res.text)
            if parsed is not None:
                return parsed
        stats["llm_retries"] += 1
    stats["llm_failed"] += 1
    return None


# --------------------------------------------------------------------------- 变体装配

def build_variant(case: dict, vtype: str, utterance: str, difficulty: str,
                  index: int, partner: dict | None = None) -> dict | None:
    """装配变体用例 dict：expect/tools 机械继承（或机械合并），LLM 只供文本。"""
    if vtype == "mix":
        if partner is None:
            return None
        merged = merge_cases(case, partner)
        if merged is None:
            return None
        return {
            "id": mix_id(case["id"], partner["id"]),
            "cat": merged["cat"],
            "utterance": utterance,
            "tools": merged["tools"],
            "expect": merged["expect"],
            "note": f"aug:mix:{case['id']}+{partner['id']}; difficulty:{difficulty}",
        }
    variant = {
        "id": variant_id(case["id"], index),
        "cat": "J劣质指令" if vtype == "degrade" else case["cat"],
        "utterance": utterance,
    }
    if case.get("context"):
        variant["context"] = copy.deepcopy(case["context"])
    variant["tools"] = copy.deepcopy(case.get("tools") or {})
    variant["expect"] = copy.deepcopy(case["expect"])  # expect 纪律：原样继承
    if case.get("allow_no_exec"):
        variant["allow_no_exec"] = True
    variant["note"] = f"aug:{vtype}_of:{case['id']}; difficulty:{difficulty}"
    return variant


def find_mix_partner(case: dict, pool: list[dict], seed: int,
                     attempt: int = 0) -> dict | None:
    """同族内找混合意图搭档：双方都 mix_eligible 且 merge_cases 不冲突。
    起点由 seed + attempt 决定（确定性），按主集顺序轮转——同一例的多个
    mix 变体由此拿到不同搭档，不会重复合并同一对。"""
    fam = case["id"][0]
    others = [c for c in pool if c["id"][0] == fam and c["id"] != case["id"]]
    if not others:
        return None
    start = (random.Random(f"{seed}:{case['id']}").randrange(len(others))
             + attempt) % len(others)
    for i in range(len(others)):
        cand = others[(start + i) % len(others)]
        if mix_eligible(case) and mix_eligible(cand) \
                and merge_cases(case, cand) is not None:
            return cand
    return None


# --------------------------------------------------------------------------- 主流程

def load_master(path: Path) -> list[dict]:
    cases: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            cases.append(json.loads(ln))
    return cases


def select_cases(cases: list[dict], only: str | None, limit: int | None) -> list[dict]:
    """--only 子串过滤（命中 id/cat/utterance）；--limit 按族轮转取样，
    保证小 --limit 下各族至少抽到 1 例。"""
    if only:
        cases = [c for c in cases
                 if only in c["id"] or only in str(c.get("cat") or "")
                 or only in c["utterance"]]
    if limit is None or limit >= len(cases):
        return cases
    by_fam: dict[str, list[dict]] = {}
    for c in cases:
        by_fam.setdefault(c["id"][0], []).append(c)
    fams = sorted(by_fam)
    out: list[dict] = []
    depth = 0
    while len(out) < limit:
        added = False
        for fam in fams:
            if depth < len(by_fam[fam]):
                out.append(by_fam[fam][depth])
                added = True
                if len(out) >= limit:
                    break
        if not added:
            break
        depth += 1
    return out


def run(cases: list[dict], per_case: int, seed: int, dry_run: bool,
        cfg, stats: dict, pool: list[dict] | None = None) -> list[dict]:
    """逐例生成变体：LLM 写 utterance → 机械继承/合并 expect → 形状自检
    （不合丢弃计数）。单例失败跳过计数，不拖死全局。
    pool：混合意图搭档搜索池——缺省用 cases 自身；main 传主集全量，
    使 --limit/--only 小选集里的 k/l 族也能找到同族搭档。"""
    partner_pool = pool if pool is not None else cases
    variants: list[dict] = []
    used_ids: set[str] = set()
    for case in cases:
        fam = case["id"][0]
        types = FAMILY_TYPES.get(fam)
        if not types:
            stats["family_skipped"] += 1
            continue
        made = 0
        seen_utts = {_norm_utt(case["utterance"])}  # 例内去重（标点空白归一）
        for i in range(per_case):
            vtype = types[i % len(types)]
            partner = None
            if vtype == "mix":
                partner = find_mix_partner(case, partner_pool, seed, attempt=i)
                if partner is None:
                    stats["mix_fallback"] += 1
                    vtype = "paraphrase"  # 无搭档族兜底，保证 l 族也有产出
            if dry_run:
                gen = {"utterance": f"[dry-run {vtype} #{i}] {case['utterance']}",
                       "difficulty": "dry"}
            else:
                gen = llm_utterance(build_prompt(case, vtype, partner), cfg, stats)
                if gen is None:
                    continue  # llm_failed 已计数
            variant = build_variant(case, vtype, gen["utterance"],
                                    gen["difficulty"], made, partner)
            if variant is None:
                stats["merge_conflict"] += 1
                continue
            if variant["id"] in used_ids:
                variant["id"] = variant_id(case["id"], made + per_case)
            if _norm_utt(variant["utterance"]) in seen_utts:
                stats["duplicate_utterance"] += 1  # 与原文或本例前序变体撞车
                continue
            if not shape_check(variant):
                stats["shape_dropped"] += 1
                continue
            seen_utts.add(_norm_utt(variant["utterance"]))
            used_ids.add(variant["id"])
            variants.append(variant)
            made += 1
    return variants


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="agent 真机探针对抗用例自动扩增器")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "eval" / "agent_live_cases_aug.jsonl",
                    help="产物 jsonl 路径（dry-run 不写出）")
    ap.add_argument("--cases", type=Path,
                    default=ROOT / "eval" / "agent_live_cases_v1.jsonl",
                    help="主集路径")
    ap.add_argument("--per-case", type=int, default=2, help="每例变体数")
    ap.add_argument("--only", type=str, default=None, help="只处理 id/cat/句含子串的用例")
    ap.add_argument("--limit", type=int, default=None, help="只处理前 N 例（按族轮转）")
    ap.add_argument("--seed", type=int, default=42, help="混合意图搭档选择种子")
    ap.add_argument("--dry-run", action="store_true",
                    help="零 LLM 调用，只跑流程骨架 + 机械合并逻辑，不写出")
    args = ap.parse_args(argv)

    master = load_master(args.cases)
    cases = select_cases(master, args.only, args.limit)
    stats = {"llm_retries": 0, "llm_failed": 0, "shape_dropped": 0,
             "merge_conflict": 0, "mix_fallback": 0, "family_skipped": 0,
             "duplicate_utterance": 0}
    cfg = None if args.dry_run else load_llm_config()
    variants = run(cases, args.per_case, args.seed, args.dry_run, cfg, stats,
                   pool=master)

    print(f"主集用例：{len(cases)} 例；生成变体：{len(variants)} 条"
          f"（dry_run={args.dry_run}）")
    print(f"计数：LLM 重试 {stats['llm_retries']} / 失败跳过 {stats['llm_failed']}；"
          f"形状自检丢弃 {stats['shape_dropped']}；合并冲突 {stats['merge_conflict']}；"
          f"混合无搭档降级 {stats['mix_fallback']}；族无适配类型跳过 "
          f"{stats['family_skipped']}；原文撞车 {stats['duplicate_utterance']}")

    if args.dry_run:
        print("--dry-run：不写出产物。")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as fh:
        for v in variants:
            fh.write(json.dumps(v, ensure_ascii=False) + "\n")
    # 最终闸：产物整体再过一遍 harness 启动自检（逐条已检，这步防拼接级问题）。
    _harness().load_cases(args.out)
    print(f"产物已写出并过 harness 启动自检：{args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
