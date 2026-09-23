#!/usr/bin/env python3
"""多 tool_call **第 2..N 个调用合法率**微验证（只读实验）。

与 `probe_multicall_fidelity.py`（测「预规划 vs 带真实状态重规划」的逐位保真度）
互补：本验证回答一个更窄的问题——**只读且互相独立**的批量调用，
第 2..N 个调用的合法率上限有多高（schema 合法 + 语义合法），以及**提示词变体**
（明确邀请独立只读批量）能否稳定诱发多调用——为只读白名单是否真并行提供依据。

事实背景（本验证再确认）：请求侧恒上 `parallel_tool_calls=False`，部分 provider 不完全遵守；
现行 decide/understand 提示词都明确要求「恰好一个」调用（agent_exec.py:1353/:537），
check_updates 的 decide_zh 更有「一次只查一个来源…其余由后续步骤逐一再查」（:813）——
A 类多调用被提示词主动压制，变体臂即改这三处。

三类测量：
  A 类·互相独立的只读批量（「检查 10x、ArrayExpress 和 ENCODE 更新，完了看库容」型）：
    多调用出现率；第 2..N 个调用的 schema 合法率（工具名在工具面/参数键与类型合法，
    source 枚举单列）与语义合法率（来源/事项与原话相符，非幻觉、不越出指令、同批不重复）。
  B 类·串行依赖链（「检查更新，有新增就搜来入库，完了报数」型）：
    多调用出现率；第 2..N 个调用在**前一步结果未知**时的参数可执行性
    （合理占位/猜测 vs 明显错误），并标注依赖类型（写/条件依赖 = 永不并行类）。
  变体臂（仅 A 类）：打松三处「一次一个」锚，同案例对拍多调用率与合法率变化。

用法：
  采集：python scripts/probe_multicall_legality.py [--rounds 2] [--resume]
  分析：python scripts/probe_multicall_legality.py --analyze [--judge]

产物：输出根目录（仓库外历史管线目录）下的 capture.jsonl /
trajectories/ / report.md / summary.md / judge_verdicts.jsonl），不入库、不碰冻结基准。
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import hashlib
import json
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402
from dataset_recommender.retrieval import search_request as _sr  # noqa: E402
from evaluate_agent_live import _source_equiv, build_tools, load_cases  # noqa: E402
from probe_multicall_fidelity import (  # noqa: E402
    _name_to_verb_union, _norm_args, _ser_calls, _ser_invalid)

OUT_DIR = ROOT / "research" / "reports" / "multicall-legality-probe"

# --------------------------------------------------------------------------- 验证集与期望真值
# A 类：互相独立的只读批量指令。sources=原话点名的来源集合（"all"=原话要求全部来源）；
# db=原话是否含报数诉求。语义判定按此真值机械比对。
A_CASES: dict[str, dict] = {
    "k09": {"sources": ["10x Genomics", "ArrayExpress", "ENCODE"], "db": True},
    "k09a": {"sources": ["10x Genomics", "ArrayExpress", "ENCODE"], "db": True},
    "k09b": {"sources": ["10x Genomics", "ArrayExpress", "ENCODE"], "db": True},
    "i02a": {"sources": "all", "db": False},
    "i02b": {"sources": "all", "db": False},
    "i07a": {"sources": ["ArrayExpress", "ENCODE"], "db": False},
    "i07b": {"sources": ["ArrayExpress", "ENCODE"], "db": False},
    "a04a": {"sources": "all", "db": False},
    "a04b": {"sources": "all", "db": False},
    "b05a": {"sources": ["10x Genomics", "ArrayExpress"], "db": False},
    "b05b": {"sources": ["10x Genomics", "ArrayExpress"], "db": False},
    "b08a": {"sources": ["ENCODE"], "db": True},
    "b08b": {"sources": ["ENCODE"], "db": True},
    "i08a": {"sources": ["ENCODE"], "db": True},
    "i08b": {"sources": ["ENCODE"], "db": True},
}
# B 类：串行依赖链（检查→条件搜索/同步→报数；含 check_zero 无新增分支与失败分支）。
B_CASES = ["b12", "g04", "j03", "k02", "k05", "k11", "l07",
           "b03a", "k01a", "i09a", "k10a"]

_WRITE_VERBS = frozenset({"curate.search_online", "curate.sync_updates"})
_READONLY_VERBS = frozenset({"curate.check_updates", "curate.db_status"})

#: 已知来源全集（归一形，canonical ∪ aliases）——all 模式下识别幻觉来源。
_KNOWN_SOURCES: frozenset = frozenset(
    ax._norm_source(x)
    for canonical, aliases in _sr.SOURCE_ALIASES
    for x in (canonical, *aliases))


def _mentioned_sources(utterance: str) -> set[str]:
    """原话点名的来源（规范名集合）——别名子串命中即算（与 `_source_equiv` 同词表真源）。"""
    low = utterance.casefold()
    out = set()
    for canonical, aliases in _sr.SOURCE_ALIASES:
        if any(str(n).casefold() in low for n in (canonical, *aliases) if str(n).strip()):
            out.add(canonical)
    return out


# --------------------------------------------------------------------------- 提示词变体臂
# 三处「一次一个」锚的替换对（旧串必须在模块常量里逐字命中一次，否则验证直接报错——
# 提示词漂移宁可 fail-loud 也不静默测错对象）。
# 变体文本已**采纳进生产**（agent_exec.py 三锚点替换）。此后 _swap_once 幂等：
# 旧串缺席且新串在场视为「已是变体」直接放行；两者都缺席仍报错（真漂移）。

_SYS_OLD = "1. 从工具表里挑**恰好一个**工具调用；表里没有对应动作时选 none。"
_SYS_NEW = ("1. 从工具表里挑工具调用；表里没有对应动作时选 none。原话一口气要求多件"
            "**彼此独立且只读**的事（如「检查 A、B、C 有没有更新」）时，一次为每件事各发"
            "一个调用（同一工具可发多次，每个来源一个）；其余情况恰好一个。")
_BULLET_OLD = "- 还需要再做一步 → 调用**恰好一个**对应的工具"
_BULLET_NEW = ("- 还需要再做一步 → 调用对应的工具；若接下来要做的几件事**彼此独立且都是"
               "只读**（如逐来源检查更新、读库容），一次把它们各发一个调用；"
               "有先后依赖或会写库的动作仍一次只发一个")
_CHECK_OLD = "一次只查一个来源——原话点名多个来源时本步查最先点名的，其余由后续步骤逐一再查；"
_CHECK_NEW = "一个调用只查一个来源——原话点名多个来源时，可为每个来源各发一个本工具调用（它们彼此独立）；"


def _swap_once(text: str, old: str, new: str, where: str) -> str:
    n_old, n_new = text.count(old), text.count(new)
    if n_old == 0 and n_new >= 1:
        return text  # 起变体已是生产基线：幂等放行
    if n_old != 1:
        raise SystemExit(f"变体锚串在 {where} 命中 {n_old} 次（预期 1）——提示词已漂移，先校准探针")
    return text.replace(old, new)


class _VariantArm:
    """变体臂的补丁与恢复（understand 系统提示词 + decide 双壳 + decide 工具面描述）。"""

    def __init__(self) -> None:
        self._saved: dict = {}

    def __enter__(self):
        self._saved = {
            "sys": ax._TOOLS_SYSTEM_ZH,
            "dt": ax._DECIDE_TOOLS_RULES_ZH,
            "dj": ax._DECIDE_RULES_ZH,
            "specs": ax._DECIDE_TOOL_SPECS,
        }
        ax._TOOLS_SYSTEM_ZH = _swap_once(ax._TOOLS_SYSTEM_ZH, _SYS_OLD, _SYS_NEW, "_TOOLS_SYSTEM_ZH")
        dt = _swap_once(ax._DECIDE_TOOLS_RULES_ZH, _BULLET_OLD, _BULLET_NEW, "_DECIDE_TOOLS_RULES_ZH")
        ax._DECIDE_TOOLS_RULES_ZH = _swap_once(dt, _CHECK_OLD, _CHECK_NEW, "_DECIDE_TOOLS_RULES_ZH")
        ax._DECIDE_RULES_ZH = _swap_once(ax._DECIDE_RULES_ZH, _CHECK_OLD, _CHECK_NEW, "_DECIDE_RULES_ZH")
        specs = copy.deepcopy(ax._DECIDE_TOOL_SPECS)
        n_hit = 0
        for spec in specs:
            desc = spec.get("function", {}).get("description") or ""
            if _CHECK_OLD in desc:
                spec["function"]["description"] = desc.replace(_CHECK_OLD, _CHECK_NEW)
                n_hit += 1
        if n_hit == 0 and _CHECK_NEW in str(specs):
            pass  # 变体已是生产基线：幂等放行
        elif n_hit != 1:
            raise SystemExit(f"变体锚串在 _DECIDE_TOOL_SPECS 命中 {n_hit} 次（预期 1）")
        ax._DECIDE_TOOL_SPECS = specs
        return self

    def __exit__(self, *exc):
        ax._TOOLS_SYSTEM_ZH = self._saved["sys"]
        ax._DECIDE_TOOLS_RULES_ZH = self._saved["dt"]
        ax._DECIDE_RULES_ZH = self._saved["dj"]
        ax._DECIDE_TOOL_SPECS = self._saved["specs"]
        return False


# --------------------------------------------------------------------------- schema 机械校验

def _schema_index() -> dict[str, dict]:
    """工具名 → parameters JSON Schema（understand 全表 ∪ decide 表，模块真源）。"""
    idx: dict[str, dict] = {}
    for spec in list(ax._get_tool_specs()[0]) + list(ax._DECIDE_TOOL_SPECS):
        fn = spec.get("function") or {}
        if fn.get("name"):
            idx[fn["name"]] = fn.get("parameters") or {}
    return idx


def _schema_check(name, args, idx: dict[str, dict]) -> tuple[str, list[str]]:
    """→ (verdict, reasons)。verdict ∈ ok / enum_bad（键型合法但 source 不在受控枚举）/
    bad（名字不在工具面 / args 非对象 / 未知键 / 类型不符）。required 恒空是项目刻意设计
    （agent_schemas.verb_parameters_schema），不作必填判定。"""
    if not name or name not in idx:
        return "bad", [f"工具名 {name!r} 不在工具面"]
    if not isinstance(args, dict):
        return "bad", ["args 不是对象"]
    props = idx[name].get("properties") or {}
    reasons: list[str] = []
    enum_bad = False
    _ptype = {"string": str, "boolean": bool, "integer": int, "number": (int, float)}
    for k, v in args.items():
        if k not in props:
            reasons.append(f"未知参数 {k!r}")
            continue
        prop = props[k] or {}
        allowed = set()
        if prop.get("type"):
            allowed.add(prop["type"])
        for sub in prop.get("anyOf") or []:
            if isinstance(sub, dict) and sub.get("type"):
                allowed.add(sub["type"])
        if allowed and v is not None:
            if not any(isinstance(v, _ptype.get(t, ())) and not isinstance(v, bool) == (t != "boolean")
                       for t in allowed if t != "null"):
                # bool 是 int 子类，单独卡：声明 boolean 只收 bool；声明 integer/number 不收 bool
                reasons.append(f"参数 {k!r} 类型 {type(v).__name__} 不符 {sorted(allowed)}")
        if "enum" in prop and v is not None and v not in (prop["enum"] or []):
            enum_bad = True
            reasons.append(f"参数 {k!r}={v!r} 不在受控枚举")
    if reasons:
        return ("enum_bad" if enum_bad and all("枚举" in r for r in reasons) else "bad"), reasons
    return "ok", []


# --------------------------------------------------------------------------- 语义判定

def _a_semantic(case_id: str, verb: str, args, seen: set) -> tuple[str, str]:
    """A 类语义四分类：legal / wrong（越出指令或幻觉来源）/ dup（同批重复）/ broad（点名却查全部）。"""
    exp = A_CASES[case_id]
    if verb == "curate.db_status":
        return ("legal", "") if exp["db"] else ("wrong", "原话没有报数诉求")
    if verb != "curate.check_updates":
        return "wrong", f"越出只读指令的动作 {verb}"
    src = str((args or {}).get("source") or "").strip()
    key = ax._norm_source(src) or "<all>"
    if key in seen:
        return "dup", f"同批重复检查 {src or '全部'}"
    seen.add(key)
    if not src:
        return ("legal", "查全部") if exp["sources"] == "all" else (
            "broad", "原话点名来源但调用未填 source（查全部，偏宽）")
    if exp["sources"] == "all":
        return ("legal", "") if ax._norm_source(src) in _KNOWN_SOURCES else (
            "wrong", f"幻觉来源 {src!r}")
    if any(_source_equiv(src, s) for s in exp["sources"]):
        return "legal", ""
    return "wrong", f"来源 {src!r} 不在原话点名集合 {exp['sources']}"


_B_JUDGE_PROMPT_ZH = (
    "用户原话：{utt}\n"
    "模型在前一步结果还不知道时，预先发出了这个工具调用：{verb} 参数 {args}\n"
    "判断该调用的**参数**是否是按原话的合理填写：主题词/来源与原话一致、或是其合理翻译"
    "或合理具体化 → 合理；来源或主题与原话明显对不上、凭空发明编号或主题 → 明显错误。\n"
    "只输出一行：「合理」或「明显错误」，然后一个竖线和一句不超过 30 字的理由。")

_JUDGE_SALT = "mcl1-v1"


def _b_param_judge(model, cache: dict, cache_path: Path, utterance: str,
                   verb: str, args) -> tuple[str, str]:
    """B 类写动词参数可执行性裁判（合理猜测 vs 明显错误），提示词与结论留档。"""
    key = hashlib.sha256(json.dumps([_JUDGE_SALT, utterance, verb, _norm_args(args)],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    if key in cache:
        return cache[key]
    prompt = _B_JUDGE_PROMPT_ZH.format(
        utt=utterance, verb=verb, args=json.dumps(_norm_args(args), ensure_ascii=False))
    try:
        ans = model.invoke(prompt)
        text = ax._message_text(ans).strip()
    except Exception as exc:  # noqa: BLE001
        text = f"裁判调用失败：{type(exc).__name__}"
    first = text.splitlines()[0] if text else ""
    verdict = ("reasonable", "") if first.startswith("合理") else ("wrong", "")
    cache[key] = (verdict[0], text[:200])
    with cache_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"hash": key, "verb": verb, "prompt": prompt,
                             "verdict": "合理" if verdict[0] == "reasonable" else "明显错误",
                             "raw": text[:300]}, ensure_ascii=False) + "\n")
    time.sleep(0.5)
    return cache[key]


def _b_semantic(utterance: str, verb: str, args, judge) -> tuple[str, str, str]:
    """B 类 → (dependency, verdict, note)。
    dependency ∈ readonly_independent / write_conditional（写或条件依赖前序结果）；
    verdict ∈ reasonable / wrong。"""
    mentioned = _mentioned_sources(utterance)
    if verb in _READONLY_VERBS:
        dep = "readonly_independent"
        if verb == "curate.db_status":
            return dep, "reasonable", ""
        src = str((args or {}).get("source") or "").strip()
        if not src:
            return dep, "reasonable", "查全部"
        if any(_source_equiv(src, m) for m in mentioned):
            return dep, "reasonable", ""
        return dep, "wrong", f"来源 {src!r} 原话没提"
    dep = "write_conditional"
    if verb == "curate.sync_updates":
        src = str((args or {}).get("source") or "").strip()
        if not src or any(_source_equiv(src, m) for m in mentioned):
            return dep, "reasonable", ""
        return dep, "wrong", f"来源 {src!r} 原话没提"
    if judge is None:
        return dep, "unjudged", "未跑裁判"
    verdict, raw = judge(utterance, verb, args)
    return dep, verdict, raw[:120]


# --------------------------------------------------------------------------- 采集

def _load_cases_by_ids(ids: list[str]) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for fn in ("agent_live_cases_hard22.jsonl", "agent_live_cases_aug.jsonl"):
        for c in load_cases(ROOT / "eval" / fn):
            seen.setdefault(c["id"], c)
    missing = [i for i in ids if i not in seen]
    if missing:
        raise SystemExit(f"用例 id 未命中：{missing}")
    return seen


def capture(rounds: int, sleep_s: float, resume: bool) -> None:
    ids = list(A_CASES) + B_CASES
    cases = _load_cases_by_ids(ids)
    cfg = load_llm_config()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "trajectories").mkdir(exist_ok=True)
    cap_path = OUT_DIR / "capture.jsonl"
    done: set[str] = set()
    if resume and cap_path.is_file():
        for line in cap_path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                done.add(f"{r['cls']}|{r['arm']}|{r['case']}|{r['round']}")
    n_runs = len(A_CASES) * rounds * 2 + len(B_CASES) * rounds
    print(f"model={cfg.model} A类={len(A_CASES)}×{rounds}轮×2臂 B类={len(B_CASES)}×{rounds}轮 "
          f"共 {n_runs} 次运行，已完成={len(done)}（resume）")

    orig = ax._invoke_tool_channel
    old_tools, old_root = ax.LOOP_TOOLS, ax._agent_project_root
    cur: dict = {"events": [], "execs": []}

    def spy(chat_model, *, tools, messages, choice, json_prompt=None,
            refallback_on_empty=False, name_to_verb=None, usage_sink=None, usage_node=""):
        answer, note, fb, je = orig(
            chat_model, tools=tools, messages=messages, choice=choice,
            json_prompt=json_prompt, refallback_on_empty=refallback_on_empty,
            name_to_verb=name_to_verb, usage_sink=usage_sink, usage_node=usage_node)
        if usage_node in ("understand", "decide") and answer is not None:
            usage = ax._usage_record(answer, usage_node)
            cur["events"].append({
                "seq": len(cur["events"]) + len(cur["execs"]),
                "node": usage_node, "m": len(cur["execs"]), "choice": choice, "fb": fb,
                "note": note, "calls": _ser_calls(answer), "invalid": _ser_invalid(answer),
                "usage": usage})
        return answer, note, fb, je

    def _logging_run(verb: str, run):
        def wrapped(slots, root):
            try:
                result = run(slots, root)
            except Exception:
                cur["execs"].append({"seq": len(cur["events"]) + len(cur["execs"]),
                                     "verb": verb, "slots": dict(slots or {}), "ok": False})
                raise
            cur["execs"].append({"seq": len(cur["events"]) + len(cur["execs"]),
                                 "verb": verb, "slots": dict(slots or {}), "ok": True})
            return result
        return wrapped

    def _run_one(cls: str, arm: str, case: dict, rd: int, fh) -> None:
        cur["events"], cur["execs"] = [], []
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_tools(case.get("tools") or {})
            ax.LOOP_TOOLS = {v: {**s, "run": _logging_run(v, s["run"])} for v, s in spec.items()}
            ax._agent_project_root = lambda: Path(tmp)
            case_ctx = case.get("context") or {}
            try:
                plan, _trace = ax.plan_with_agent(
                    str(case["utterance"]),
                    has_results=bool(case_ctx.get("has_results", False)),
                    result_total=int(case_ctx.get("result_total") or 0),
                    config=cfg, retrieval=None,
                    current_query=str(case_ctx.get("current_query") or ""),
                    current_filters=case_ctx.get("current_filters"))
                exc = ""
            except Exception as err:  # noqa: BLE001
                plan, exc = {}, f"{type(err).__name__}: {str(err)[:160]}"
        rec = {"type": "run", "cls": cls, "arm": arm, "case": case["id"], "round": rd,
               "utterance": case["utterance"], "events": cur["events"],
               "executions": cur["execs"],
               "steps": [s.get("verb") for s in (plan.get("steps") or [])],
               "exc": exc, "ms": int((time.monotonic() - started) * 1000)}
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        fh.flush()
        (OUT_DIR / "trajectories" / f"{cls}_{arm}_{case['id']}_r{rd}.json").write_text(
            json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        n_multi = sum(1 for e in rec["events"] if len(e["calls"]) >= 2)
        print(f"[{cls} {arm:>7} r{rd} {case['id']:>5}] 事件 {len(rec['events'])} 次"
              f"（多调用 {n_multi}），执行 {len(rec['executions'])} 步，"
              f"{rec['ms'] / 1000:.1f}s {exc[:60]}")

    ax._invoke_tool_channel = spy
    try:
        with cap_path.open("a", encoding="utf-8", newline="\n") as fh:
            for rd in range(1, rounds + 1):
                for cid in A_CASES:
                    for arm in ("current", "variant"):
                        if f"A|{arm}|{cid}|{rd}" in done:
                            continue
                        if arm == "variant":
                            with _VariantArm():
                                _run_one("A", arm, cases[cid], rd, fh)
                        else:
                            _run_one("A", arm, cases[cid], rd, fh)
                        time.sleep(sleep_s)
                for cid in B_CASES:
                    if f"B|current|{cid}|{rd}" in done:
                        continue
                    _run_one("B", "current", cases[cid], rd, fh)
                    time.sleep(sleep_s)
    finally:
        ax._invoke_tool_channel = orig
        ax.LOOP_TOOLS, ax._agent_project_root = old_tools, old_root
    print(f"采集完成 → {cap_path}")


# --------------------------------------------------------------------------- 分析

def analyze(with_judge: bool) -> None:
    cap_path = OUT_DIR / "capture.jsonl"
    if not cap_path.is_file():
        raise SystemExit(f"找不到 {cap_path}，先跑采集。")
    runs = [json.loads(l) for l in cap_path.open(encoding="utf-8") if l.strip()]
    n2v = _name_to_verb_union()
    idx = _schema_index()
    judge_model = None
    judge_cache: dict = {}
    judge_path = OUT_DIR / "judge_verdicts.jsonl"
    if with_judge:
        cfg = load_llm_config()
        judge_model = ax._build_chat_model(dataclasses.replace(cfg, temperature=0.0))
        if judge_path.is_file():
            for line in judge_path.open(encoding="utf-8"):
                if line.strip():
                    r = json.loads(line)
                    judge_cache[r["hash"]] = (
                        "reasonable" if r["verdict"] == "合理" else "wrong", r.get("raw") or "")

    def judge(utterance, verb, args):
        return _b_param_judge(judge_model, judge_cache, judge_path, utterance, verb, args)

    call_rows: list[dict] = []   # 每个 k≥2 调用一行
    grp: dict[tuple, dict] = {}

    def _g(cls, arm):
        key = (cls, arm)
        if key not in grp:
            grp[key] = {"runs": 0, "runs_multi": 0, "events": 0, "multi": 0,
                        "batch": Counter(), "exec_steps": 0, "exc": 0,
                        "in_tok": 0, "out_tok": 0, "cache_tok": 0, "ms": 0,
                        "out_tok_multi": 0, "out_tok_single": 0,
                        "n_multi_ev_usage": 0, "n_single_ev_usage": 0}
        return grp[key]

    for rec in runs:
        g = _g(rec["cls"], rec["arm"])
        g["runs"] += 1
        g["exec_steps"] += len(rec["executions"])
        g["ms"] += rec["ms"]
        if rec["exc"]:
            g["exc"] += 1
            continue
        run_multi = False
        for ev in rec["events"]:
            g["events"] += 1
            u = ev.get("usage") or {}
            g["in_tok"] += int(u.get("input") or 0)
            g["out_tok"] += int(u.get("output") or 0)
            g["cache_tok"] += int(u.get("cache_read") or 0)
            calls = ev["calls"]
            if u:
                if len(calls) >= 2:
                    g["out_tok_multi"] += int(u.get("output") or 0)
                    g["n_multi_ev_usage"] += 1
                else:
                    g["out_tok_single"] += int(u.get("output") or 0)
                    g["n_single_ev_usage"] += 1
            if len(calls) < 2:
                continue
            run_multi = True
            g["multi"] += 1
            g["batch"][min(len(calls), 6)] += 1
            seen: set = set()
            # c1 也过一遍 schema/语义（参照系），但只把 k≥2 计入合法率分子分母
            for k0, call in enumerate(calls):
                verb = n2v.get(str(call.get("name") or ""))
                schema_v, reasons = _schema_check(call.get("name"), call.get("args"), idx)
                row = {"cls": rec["cls"], "arm": rec["arm"], "case": rec["case"],
                       "round": rec["round"], "node": ev["node"], "k": k0 + 1,
                       "name": call.get("name"), "verb": verb,
                       "args": _norm_args(call.get("args")), "schema": schema_v,
                       "schema_note": ";".join(reasons)}
                if rec["cls"] == "A":
                    sem, note = _a_semantic(rec["case"], verb or "", call.get("args"), seen)
                    row.update(sem=sem, sem_note=note)
                else:
                    dep, verdict, note = _b_semantic(
                        rec["utterance"], verb or "", call.get("args"),
                        judge if with_judge else None)
                    row.update(dep=dep, sem=verdict, sem_note=note)
                call_rows.append(row)
        if run_multi:
            g["runs_multi"] += 1

    with (OUT_DIR / "calls.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for r in call_rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------------- 聚合 ----------------
    lines = ["# 多 tool_call 第 2..N 合法率探针报告", ""]
    lines.append("parallel_tool_calls=False 恒上；凡出现多调用即模型不遵守该参数的直接证据。")
    lines.append("")
    for (cls, arm), g in sorted(grp.items()):
        dist = ", ".join(f"{k} 个×{v}" for k, v in sorted(g["batch"].items()))
        lines.append(f"## {cls} 类 · {arm} 臂")
        lines.append("")
        lines.append(f"- 运行 {g['runs']}（异常 {g['exc']}），事件 {g['events']} 次，"
                     f"**多调用事件 {g['multi']} 次（事件率 {g['multi'] / max(g['events'], 1) * 100:.0f}%；"
                     f"运行覆盖率 {g['runs_multi']}/{g['runs']}）**；批次大小：{dist or '无'}")
        sub = [r for r in call_rows if r["cls"] == cls and r["arm"] == arm]
        for label, rows in (("c1（参照）", [r for r in sub if r["k"] == 1]),
                            ("**k≥2（测量对象）**", [r for r in sub if r["k"] >= 2])):
            if not rows:
                continue
            sc = Counter(r["schema"] for r in rows)
            sm = Counter(r["sem"] for r in rows)
            lines.append(f"- {label}：n={len(rows)}；schema ok {sc.get('ok', 0)}"
                         f"（enum_bad {sc.get('enum_bad', 0)}、bad {sc.get('bad', 0)}）；"
                         f"语义分布 {dict(sm)}")
        if cls == "B":
            dep = Counter(r.get("dep") for r in sub if r["k"] >= 2)
            lines.append(f"- k≥2 依赖类型：{dict(dep)}")
        if g["runs"]:
            lines.append(f"- 开销：平均每次运行 input {g['in_tok'] // max(g['runs'], 1)} tok"
                         f"（缓存命中 {g['cache_tok'] // max(g['runs'], 1)}）/ output "
                         f"{g['out_tok'] // max(g['runs'], 1)} tok，{g['ms'] // max(g['runs'], 1) / 1000:.1f}s；"
                         f"多调用事件平均 output "
                         f"{g['out_tok_multi'] // max(g['n_multi_ev_usage'], 1)} tok vs 单调用 "
                         f"{g['out_tok_single'] // max(g['n_single_ev_usage'], 1)} tok")
        lines.append("")
    lines.append("## 语义不合法/存疑明细（k≥2）")
    lines.append("")
    for r in call_rows:
        if r["k"] >= 2 and (r["schema"] != "ok" or r["sem"] not in ("legal", "reasonable")):
            lines.append(f"- [{r['cls']}/{r['arm']}] {r['case']} r{r['round']} {r['node']} k={r['k']} "
                         f"{r['verb'] or r['name']} schema={r['schema']} sem={r['sem']}"
                         f"｜args={json.dumps(r['args'], ensure_ascii=False)[:110]}"
                         f"｜{r['schema_note'] or r['sem_note']}")
    (OUT_DIR / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"分析完成 → {OUT_DIR / 'report.md'}（k≥2 调用 "
              f"{sum(1 for r in call_rows if r['k'] >= 2)} 个）")
    print("\n".join(lines[:60]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()
    if args.analyze:
        analyze(args.judge)
    else:
        capture(args.rounds, args.sleep, args.resume)
    return 0


if __name__ == "__main__":
    sys.exit(main())
