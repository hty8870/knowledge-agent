#!/usr/bin/env python3
"""多 tool_call **保真度微验证**（只读实验，不改生产代码）。

与 `probe_decide_format.py`（量「多调用取第一个」的合法性）
互补：本验证量的是**第 2 个及以后调用**的保真度——模型在批量
调用里「预先规划」的第 k 个调用（k≥2），和循环带着真实状态重新规划后**实际执行**的第
k 步，一致率有多高。这个数是「整批乐观执行」架构方向（参考 Claude Code toolOrchestration）
的 go / no-go 依据。

两臂成对采集（**同一次插桩运行**，比「跑两遍」严格——两次独立运行会把保真度和采样噪声
混在一起）：
  A 臂（现状语义）：正常循环，包装 LOOP_TOOLS 的 run 记录**实际执行**的调用序列
      （verb + slots，含失败步）。
  B 臂（批量采集）：spy `_invoke_tool_channel`（沿用既有验证同一挂钩点），把
      understand / decide 每次回复里**未经截断的全部 tool_calls** 留档（第一个之外
      的全部保留），同时记下此刻已执行步数 m。

比对双口径（同一批采集、同一份参数等价判定真源）：
  ①逐位口径（任务指定主口径）：被采纳批次（c1 是 loop 动词且确实成为第 m+1 步）的第 k
    个调用（k≥2）↔ A 臂执行序列的第 m+k 步。目标步不存在 = 循环提前收尾（条件错候选，
    按收尾原因细分 model_finish / guard / max_steps）。注意：一个条件性跳过会让后续
    预规划调用整体错位，逐位口径会级联放大条件错计数——所以再算②。
  ②序列对齐口径（防级联的辅助口径）：批次的 c2..cn 与「实际剩余执行序列」按动词骨架
    做 LCS 对齐（difflib），对齐上的逐对分类参数；pre 有而实际没有 = skipped（条件错）；
    对齐位置动词不同 = verb_changed（条件错）；实际有而 pre 没有 = 实际新增（信息项，
    不计入保真分母）。一个条件跳过只计一次。

参数比对先做**槽位投影**（只比 action_plan.VERB_SPECS 声明的执行槽位——quoted/reason/
confidence 等元数据键在执行前会被 validate 剥离，不参与保真度判定），再归一化精确比；
规则判不了的残余用 LLM 裁判（提示词与结论全量落盘，可复查）。

不一致三分类（占比直接决定架构方向）：
  参数错：verb 相同但参数实质不等价（编号/查询词/来源对不上）；
  条件错：前一步真实结果让这步根本不该走（loop_ended/skipped：真实状态下没走这步；
      verb_changed：带真实状态重规划后换了动作）；
  无害差异：措辞/别名/词序差异但语义等价（归一化规则命中或裁判判等价）——计入一致。

用法：
  采集（真 API，每用例独立沙箱，与 live 验证同纪律——绝不碰真库）：
    python scripts/probe_multicall_fidelity.py [--ids b12,k02,...] [--rounds 1] [--resume]
  分析（离线；--judge 时对规则判不了的残余参数对调一次 LLM 裁判，判定留档可复查）：
    python scripts/probe_multicall_fidelity.py --analyze [--judge]

产物（写入输出根目录 OUT_DIR——仓库外历史管线目录，不入库、不碰冻结基准）：
  capture.jsonl            一行一个用例一轮：events（B 臂批次）+ executions（A 臂序列）
  trajectories/<case>_r<n>.json  单条任务的两臂轨迹（可读版）
  pairs.jsonl              逐位口径比对记录（每个 k≥2 预规划调用一条，含分类与裁判结论）
  aligned.jsonl            序列对齐口径比对记录（含「实际新增」信息项）
  judge_verdicts.jsonl     LLM 裁判的提示词与结论（--judge 时；缓存，重跑不复调）
  report.md                聚合报告：双口径总一致率 / 分 verb / 分位置 / 三类不一致占比
"""
from __future__ import annotations

import argparse
import dataclasses
import difflib
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_recommender.agent import action_plan as ap  # noqa: E402
from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402
from evaluate_agent_live import _TOOL_META, _source_equiv, build_tools, load_cases  # noqa: E402

OUT_DIR = ROOT / "research" / "reports" / "multicall-fidelity-probe"

#: 对照采集：`--out-dir` 把采集/分析定向到子目录，
#: 与主臂产物隔离；`PROBE_THINKING_OFF=1` 时给主模型关思考档（
#: 对照口径需要同档）。两者都只作用于验证，不动生产代码。

#: 验证集（36 条多步维护链）：hard22 全量 22 条（多事项马拉松对抗集）+ aug 扩增集 14 条
#: 同类代表（覆盖 check_zero 无新增条件分支 / sync 变体 / 网络失败分支 / 超长链）。
DEFAULT_IDS = (
    # ---- hard22（22）：检查更新→搜回→入库→报数 马拉松链是产品主打场景，全取 ----
    "b12", "g04", "j03", "k02", "k05", "k09", "k11", "l07",
    "b12a", "b12b", "j03a", "j03b", "k02a", "k02b", "k05a", "k05b",
    "k09a", "k09b", "k11a", "k11b", "l07a", "l07b",
    # ---- aug（14）：条件分支与失败分支代表 ----
    "b03a",      # check_zero（无新增）→ 搜索不该走
    "b04a",      # check_ae2（有新增）→ 搜索该走 + 报数
    "b08a",      # check_zero + db_status（纯只读双事项）
    "b09a",      # check_ae2 + search + sync 三动词
    "b13a",      # sync + db_status 短链
    "i09a",      # by_source：encode 有新增（check_brain2）→ 条件性搜索
    "i10a",      # sync + check_zero
    "k01a",      # 全 check_zero：两个「有新增就搜」条件都不成立
    "k03a",      # arrayexpress 检查 raise 网络错误 → 失败分支
    "k06a",      # 反向条件：「没的话 ENCODE 和 10x 都检查一遍」
    "k10a",      # check_zero + search + db_status
    "l04_l02x",  # check_zero + 限定物种搜索 + db_status
    "l08a",      # db_status + sync_zero + check_zero
    "k01_k06x",  # 超长链（双段马拉松）
)

_LOOP_VERBS = frozenset(_TOOL_META)  # 图内可执行的四个 loop 动词（真源与 live 验证同一份）

#: 工具调用 args 里的元数据键（validate 建 plan 步时剥离，从不进执行槽位）——保真度
#: 比对前先投影掉，否则 quoted/reason/confidence 会把等价判定全淹成噪声。
_META_KEYS = frozenset({"quoted", "reason", "confidence"})

#: verb → 执行槽位名集合（action_plan.VERB_SPECS 真源，运行期取）。
_SLOT_KEYS: dict[str, frozenset] = {
    v.verb: frozenset(getattr(v, "slots", None) or ()) for v in ap.VERB_SPECS}


def _name_to_verb_union() -> dict[str, str]:
    """understand 全动词表 ∪ decide loop 表 + finish/unsupported——批次留档名的统一逆映射，
    全部从模块运行时取真源，不手抄第二份。"""
    _tools, understand_n2v = ax._get_tool_specs()
    m = dict(understand_n2v)
    m.update(ax._DECIDE_TOOL_NAME_TO_VERB)
    m[ax._DECIDE_FINISH_TOOL] = "<finish>"
    m[ax._DECIDE_UNSUPPORTED_TOOL] = "<unsupported>"
    return m


# --------------------------------------------------------------------------- 采集（A/B 臂同跑）

def _ser_calls(answer) -> list[dict]:
    out = []
    for c in (getattr(answer, "tool_calls", None) or []):
        if isinstance(c, dict):
            out.append({"name": c.get("name"), "args": c.get("args")})
        else:
            out.append({"name": getattr(c, "name", ""), "args": getattr(c, "args", None)})
    return out


def _ser_invalid(answer) -> list[dict]:
    out = []
    for c in (getattr(answer, "invalid_tool_calls", None) or []):
        if isinstance(c, dict):
            out.append({"name": c.get("name"), "error": str(c.get("error") or "")[:200]})
        else:
            out.append({"name": getattr(c, "name", ""),
                        "error": str(getattr(c, "error", "") or "")[:200]})
    return out


def _load_probe_cases(ids: list[str]) -> list[dict]:
    """hard22 优先、aug 补充，按 id 去重（两集有重叠用例）。"""
    seen: dict[str, dict] = {}
    for fn in ("agent_live_cases_hard22.jsonl", "agent_live_cases_aug.jsonl"):
        for c in load_cases(ROOT / "eval" / fn):
            seen.setdefault(c["id"], c)
    cases = [seen[i] for i in ids if i in seen]
    missing = [i for i in ids if i not in seen]
    if missing:
        print(f"警告：{len(missing)} 个 id 未命中用例集：{missing}", file=sys.stderr)
    if not cases:
        print("没有命中的用例。", file=sys.stderr)
        raise SystemExit(2)
    return cases


def capture(ids: list[str], rounds: int, sleep_s: float, resume: bool,
            out_dir: Path = OUT_DIR, prompt_arm: str = "current") -> None:
    cases = _load_probe_cases(ids)
    cfg = load_llm_config()
    if os.environ.get("PROBE_THINKING_OFF") == "1":
        cfg = dataclasses.replace(cfg, thinking=False)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "trajectories").mkdir(exist_ok=True)
    cap_path = out_dir / "capture.jsonl"
    done: set[str] = set()
    if resume and cap_path.is_file():
        for line in cap_path.open(encoding="utf-8"):
            if line.strip():
                r = json.loads(line)
                done.add(f"{r['case']}|{r['round']}")
    print(f"model={cfg.model} cases={len(cases)} rounds={rounds} 已完成={len(done)}（resume）")

    orig = ax._invoke_tool_channel
    old_tools, old_root = ax.LOOP_TOOLS, ax._agent_project_root
    cur: dict = {"events": [], "execs": []}

    def spy(chat_model, *, tools, messages, choice, json_prompt=None,
            refallback_on_empty=False, name_to_verb=None, usage_sink=None, usage_node=""):
        # provider 适配（仅此验证内生效，不动 src/）：zhipu 端点对
        # tool_choice="required" 不报 400 而是把请求挂住到超时（60s×重试≈180s），
        # 挂住的请求还占服务端并发槽、随后整账户 429(code 1302)；同一请求换 "auto"
        # 2s 内正常返回 tool_calls。故该 provider 下把 required 降为 auto——understand 的
        # auto 车道本就在 _invoke_tool_channel 的 refallback 设计内（
        # no_tool_calls(auto) 走的就是它），不改变测量口径（多调用保真度在 decide 批）。
        if choice == "required" and getattr(cfg, "provider", "") == "zhipuai":
            choice = "auto"
        answer, note, fb, je = orig(
            chat_model, tools=tools, messages=messages, choice=choice,
            json_prompt=json_prompt, refallback_on_empty=refallback_on_empty,
            name_to_verb=name_to_verb, usage_sink=usage_sink, usage_node=usage_node)
        if usage_node in ("understand", "decide") and answer is not None:
            cur["events"].append({
                "seq": len(cur["events"]) + len(cur["execs"]),
                "node": usage_node, "m": len(cur["execs"]), "choice": choice, "fb": fb,
                "calls": _ser_calls(answer), "invalid": _ser_invalid(answer)})
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

    ax._invoke_tool_channel = spy
    try:
        with cap_path.open("a", encoding="utf-8", newline="\n") as fh:
            for rd in range(1, rounds + 1):
                for case in cases:
                    if f"{case['id']}|{rd}" in done:
                        continue
                    cur["events"], cur["execs"] = [], []
                    started = time.monotonic()
                    with tempfile.TemporaryDirectory() as tmp:
                        spec = build_tools(case.get("tools") or {})
                        ax.LOOP_TOOLS = {v: {**s, "run": _logging_run(v, s["run"])}
                                         for v, s in spec.items()}
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
                    rec = {"type": "run", "case": case["id"], "round": rd,
                           "model": cfg.model, "prompt_arm": prompt_arm,
                           "cat": case.get("cat"), "utterance": case["utterance"],
                           "events": cur["events"], "executions": cur["execs"],
                           "plan_steps": [{"verb": s.get("verb"), "slots": s.get("slots"),
                                           "ok": s.get("ok")}
                                          for s in (plan.get("steps") or [])],
                           "exc": exc,
                           "ms": int((time.monotonic() - started) * 1000)}
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    (out_dir / "trajectories" / f"{case['id']}_r{rd}.json").write_text(
                        json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
                    n_multi = sum(1 for e in rec["events"] if len(e["calls"]) >= 2)
                    print(f"[r{rd} {case['id']:>9}] 执行 {len(rec['executions'])} 步，"
                          f"通道调用 {len(rec['events'])} 次（多调用 {n_multi} 次），"
                          f"{rec['ms'] / 1000:.1f}s {exc[:60]}")
                    time.sleep(sleep_s)
    finally:
        ax._invoke_tool_channel = orig
        ax.LOOP_TOOLS, ax._agent_project_root = old_tools, old_root
    print(f"采集完成 → {cap_path}")


# --------------------------------------------------------------------------- 分析（对齐 + 分类）

def _norm_scalar(v):
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v.strip()).casefold()
    return v


def _project_args(verb: str, args) -> dict:
    """槽位投影：只留 VERB_SPECS 声明的执行槽位（quoted/reason/confidence 等元数据键
    执行前必被剥离，不参与保真度判定）；动词未知时退化为仅去元数据键。"""
    if not isinstance(args, dict):
        return {}
    slots = _SLOT_KEYS.get(verb or "")
    if slots is None:
        return {str(k): v for k, v in args.items() if k not in _META_KEYS}
    return {str(k): v for k, v in args.items() if k in slots}


def _norm_args(args) -> dict:
    """归一化（去空值键、字符串压空白 + casefold）——调用方先做槽位投影。"""
    if not isinstance(args, dict):
        return {}
    return {str(k): _norm_scalar(v) for k, v in args.items()
            if v is not None and (not isinstance(v, str) or v.strip())}


def _tokens(s: str) -> frozenset:
    return frozenset(re.findall(r"\w+", s.casefold(), re.UNICODE))


def _classify_args(verb: str, pre_args, act_args):
    """同 verb 的参数比对（入参须已过槽位投影）→ (class, detail)。
    class ∈ exact / harmless / None（规则判不了，交裁判）。"""
    na, nb = _norm_args(pre_args), _norm_args(act_args)
    if na == nb:
        if pre_args == act_args:
            return "exact", ""
        return "harmless", "表面差异（大小写/空白/空值键），归一化后全等"
    notes, unresolved = [], []
    for k in sorted(set(na) | set(nb)):
        va, vb = na.get(k), nb.get(k)
        if va == vb:
            continue
        if k == "source" and va is not None and vb is not None and _source_equiv(str(va), str(vb)):
            notes.append(f"source 别名同槽 {va!r}≈{vb!r}")
        elif (k in ("keywords", "species", "query")
              and isinstance(va, str) and isinstance(vb, str) and _tokens(va) == _tokens(vb)):
            notes.append(f"{k} 词集相同 {va!r}≈{vb!r}")
        else:
            unresolved.append(f"{k}: {va!r} → {vb!r}")
    if not unresolved:
        return "harmless", "；".join(notes)
    return None, "；".join(notes + unresolved)


_JUDGE_PROMPT_ZH = (
    "你是工具调用参数等价裁判。同一动作「{verb}」的两次调用参数如下（JSON）：\n"
    "A（模型预先规划）: {a}\n"
    "B（带真实状态重新规划后实际执行）: {b}\n"
    "判断两份参数对该动作是否语义等价：来源别名（如 10x 与 10x Genomics）、同一主题的中英"
    "同义表述、措辞与词序差异都算等价；编号、查询主题、物种、过滤条件有实质不同才算不等价。\n"
    "示例1：A={{\"keywords\": \"human lung\"}} B={{\"keywords\": \"人类肺\"}} → 等价（同一主题的中英表述）。\n"
    "示例2：A={{\"keywords\": \"human lung\"}} B={{\"keywords\": \"mouse brain\"}} → 不等价（查询主题不同）。\n"
    "示例3：A={{\"source\": \"ArrayExpress\"}} B={{\"source\": \"arrayexpress\"}} → 等价（同一来源大小写差异）。\n"
    "只输出一行：「等价」或「不等价」，然后一个竖线和一句不超过 30 字的理由。")

#: 裁判提示词版本——改提示词即改哈希盐，旧缓存结论自动失效（judge_verdicts.jsonl 里
#: 每条结论都带当时提示词全文，可复查）。
_JUDGE_SALT = "v2-fewshot"


def _judge(model, verb: str, a, b, cache: dict, cache_path: Path) -> tuple[bool, str]:
    """LLM 裁判（简单可复查）：提示词与结论全量落 judge_verdicts.jsonl，按内容哈希缓存。"""
    key = hashlib.sha256(json.dumps([_JUDGE_SALT, verb, _norm_args(a), _norm_args(b)],
                                    ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    if key in cache:
        return cache[key]
    prompt = _JUDGE_PROMPT_ZH.format(
        verb=verb, a=json.dumps(_norm_args(a), ensure_ascii=False),
        b=json.dumps(_norm_args(b), ensure_ascii=False))
    try:
        ans = model.invoke(prompt)
        text = ax._message_text(ans).strip()
    except Exception as exc:  # noqa: BLE001
        text = f"裁判调用失败：{type(exc).__name__}"
    first = text.splitlines()[0] if text else ""
    equiv = first.startswith("等价")
    cache[key] = (equiv, text[:200])
    with cache_path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"hash": key, "verb": verb, "prompt": prompt,
                             "verdict": "等价" if equiv else "不等价",
                             "raw": text[:300]}, ensure_ascii=False) + "\n")
    time.sleep(0.5)
    return cache[key]


def _end_reason(rec: dict, n2v: dict[str, str]) -> str:
    """循环收尾原因（loop_ended 的细分）：模型 finish 收尾 / 步数硬上界 / 护栏或非法停环。"""
    execs = rec["executions"]
    if len(execs) >= ax.MAX_STEPS:
        return "max_steps"
    decides = [e for e in rec["events"] if e["node"] == "decide"]
    if not decides:
        return "no_decide（首步即非 loop 动词或护栏拒收）"
    last = decides[-1]
    if last["calls"]:
        v1 = n2v.get(str(last["calls"][0].get("name") or ""))
        if v1 == "<finish>":
            return "model_finish"
        if v1 == "<unsupported>":
            return "model_unsupported"
        if v1 in _LOOP_VERBS:
            return "guard_stop（续步提议被机械闸拒收）"
        return "invalid_stop（末次应答工具名不可读）"
    return "invalid_stop（末次应答无调用）"


class _JudgeBox:
    """裁判句柄容器：未开 --judge 时等价于规则判不了就记「未定」。"""

    def __init__(self, with_judge: bool, out_dir: Path = OUT_DIR):
        self.model = None
        self.cache: dict = {}
        self.path = out_dir / "judge_verdicts.jsonl"
        if with_judge:
            cfg = load_llm_config()
            self.model = ax._build_chat_model(dataclasses.replace(cfg, temperature=0.0))
            if self.path.is_file():
                for line in self.path.open(encoding="utf-8"):
                    if line.strip():
                        r = json.loads(line)
                        self.cache[r["hash"]] = (r["verdict"] == "等价", r.get("raw") or "")

    def verdict(self, verb: str, a, b) -> tuple[str, str]:
        """→ (cls, detail_suffix)。cls ∈ 无害差异 / 参数错 / 未定。"""
        if self.model is None:
            return "未定", ""
        equiv, why = _judge(self.model, verb, a, b, self.cache, self.path)
        return ("无害差异" if equiv else "参数错"), f"｜裁判：{why[:120]}"


def _args_pair_cls(jbox: _JudgeBox, verb: str, pre_args, act_args) -> tuple[str, str, str]:
    """(cls, sub, detail)：cls ∈ 一致 / 无害差异 / 参数错 / 未定。"""
    cls, detail = _classify_args(verb, pre_args, act_args)
    if cls == "exact":
        return "一致", "exact", ""
    if cls == "harmless":
        return "无害差异", "rule", detail
    cls2, suffix = jbox.verdict(verb, pre_args, act_args)
    if cls2 == "未定":
        return "未定", "needs_judge", detail
    return cls2, "judge", detail + suffix


def analyze(with_judge: bool, out_dir: Path = OUT_DIR) -> None:
    cap_path = out_dir / "capture.jsonl"
    if not cap_path.is_file():
        print(f"找不到 {cap_path}，先跑采集。", file=sys.stderr)
        raise SystemExit(2)
    runs = [json.loads(l) for l in cap_path.open(encoding="utf-8") if l.strip()]
    n2v = _name_to_verb_union()
    jbox = _JudgeBox(with_judge, out_dir)

    pairs: list[dict] = []      # 逐位口径
    aligned: list[dict] = []    # 序列对齐口径（含「实际新增」信息项）
    not_adopted: list[dict] = []  # c1 是合法 loop 动词但没成为下一步（veto/护栏/重问）
    run_stats = Counter()
    for rec in runs:
        execs = rec["executions"]
        end_reason = _end_reason(rec, n2v)
        run_stats["runs"] += 1
        run_stats["steps_total"] += len(execs)
        if rec["exc"]:
            run_stats["runs_exc"] += 1
            continue
        for ev in rec["events"]:
            calls = ev["calls"]
            if not calls:
                run_stats[f"events_empty_{ev['node']}"] += 1
                continue
            v1 = n2v.get(str(calls[0].get("name") or ""))
            if v1 not in _LOOP_VERBS:
                run_stats[f"c1_{(v1 or '未知工具名').strip('<>')}"] += 1
                if not v1:
                    not_adopted.append({"case": rec["case"], "round": rec["round"],
                                        "node": ev["node"], "m": ev["m"],
                                        "c1_name": calls[0].get("name"), "why": "工具名不可读"})
                continue
            m = int(ev["m"])
            adopted = m < len(execs) and execs[m]["verb"] == v1
            if not adopted:
                run_stats["c1_not_adopted"] += 1
                nxt = execs[m]["verb"] if m < len(execs) else f"（无第 {m + 1} 步：{end_reason}）"
                not_adopted.append({"case": rec["case"], "round": rec["round"],
                                    "node": ev["node"], "m": m, "c1_name": calls[0].get("name"),
                                    "c1_verb": v1,
                                    "c1_args": _project_args(v1, calls[0].get("args")),
                                    "why": f"未成为第 {m + 1} 步（实际：{nxt}）"})
                continue
            run_stats["adopted_events"] += 1
            run_stats[f"batch_size_{min(len(calls), 6)}"] += 1
            if len(calls) < 2:
                continue
            run_stats["multi_events"] += 1

            # ---- 口径①：逐位（c_k ↔ 第 m+k 步）----
            for k in range(2, len(calls) + 1):
                pre = calls[k - 1]
                pre_verb = n2v.get(str(pre.get("name") or ""))
                pre_args = _project_args(pre_verb or "", pre.get("args"))
                tgt = execs[m + k - 1] if m + k - 1 < len(execs) else None
                pair = {"case": rec["case"], "round": rec["round"], "node": ev["node"],
                        "k": k, "pos": m + k, "pre_name": pre.get("name"),
                        "pre_verb": pre_verb, "pre_args": pre_args,
                        "act_verb": tgt["verb"] if tgt else None,
                        "act_args": _project_args(tgt["verb"], tgt["slots"]) if tgt else None}
                if pre_verb not in _LOOP_VERBS:
                    pair.update(cls="参数错", sub="bad_name",
                                detail=f"第 {k} 个调用的工具名不可读：{pre.get('name')!r}")
                elif tgt is None:
                    pair.update(cls="条件错", sub=f"loop_ended/{end_reason}",
                                detail=f"预规划 {pre_verb}，但循环第 {m + k} 步不存在（{end_reason}）")
                elif tgt["verb"] != pre_verb:
                    pair.update(cls="条件错", sub="verb_changed",
                                detail=f"预规划 {pre_verb}，实际第 {m + k} 步改走 {tgt['verb']}")
                else:
                    cls, sub, detail = _args_pair_cls(jbox, pre_verb, pre_args, tgt["slots"])
                    pair.update(cls=cls, sub=sub, detail=detail)
                pairs.append(pair)

            # ---- 口径②：序列对齐（c2..cn ↔ 实际剩余序列，动词骨架 LCS）----
            pre_tail = [(n2v.get(str(c.get("name") or "")), c) for c in calls[1:]]
            act_tail = execs[m + 1:]
            sm = difflib.SequenceMatcher(
                a=[v for v, _c in pre_tail], b=[e["verb"] for e in act_tail], autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                if tag == "equal":
                    for off in range(i2 - i1):
                        pre_verb, pre = pre_tail[i1 + off]
                        tgt = act_tail[j1 + off]
                        pre_args = _project_args(pre_verb or "", pre.get("args"))
                        cls, sub, detail = _args_pair_cls(jbox, pre_verb, pre_args, tgt["slots"])
                        aligned.append({"case": rec["case"], "round": rec["round"],
                                        "node": ev["node"], "kind": "aligned",
                                        "pre_verb": pre_verb, "pre_args": pre_args,
                                        "act_verb": tgt["verb"],
                                        "act_args": _project_args(tgt["verb"], tgt["slots"]),
                                        "cls": cls, "sub": sub, "detail": detail})
                elif tag == "replace":
                    span = max(i2 - i1, j2 - j1)
                    for off in range(span):
                        pre_it = pre_tail[i1 + off] if i1 + off < i2 else None
                        act_it = act_tail[j1 + off] if j1 + off < j2 else None
                        if pre_it and act_it:
                            aligned.append({"case": rec["case"], "round": rec["round"],
                                            "node": ev["node"], "kind": "verb_changed",
                                            "pre_verb": pre_it[0],
                                            "pre_args": _project_args(pre_it[0] or "",
                                                                      pre_it[1].get("args")),
                                            "act_verb": act_it["verb"],
                                            "act_args": _project_args(act_it["verb"],
                                                                      act_it["slots"]),
                                            "cls": "条件错", "sub": "verb_changed",
                                            "detail": f"预规划 {pre_it[0]}，实际改走 {act_it['verb']}"})
                        elif pre_it:
                            aligned.append({"case": rec["case"], "round": rec["round"],
                                            "node": ev["node"], "kind": "skipped",
                                            "pre_verb": pre_it[0],
                                            "pre_args": _project_args(pre_it[0] or "",
                                                                      pre_it[1].get("args")),
                                            "act_verb": None, "act_args": None,
                                            "cls": "条件错", "sub": "skipped",
                                            "detail": f"预规划 {pre_it[0]}，真实状态下没走"})
                        else:
                            aligned.append({"case": rec["case"], "round": rec["round"],
                                            "node": ev["node"], "kind": "inserted",
                                            "pre_verb": None, "pre_args": None,
                                            "act_verb": act_it["verb"],
                                            "act_args": _project_args(act_it["verb"],
                                                                      act_it["slots"]),
                                            "cls": "实际新增", "sub": "inserted",
                                            "detail": f"实际多走了 {act_it['verb']}（预规划批次外）"})
                elif tag == "delete":
                    for off in range(i2 - i1):
                        pre_verb, pre = pre_tail[i1 + off]
                        aligned.append({"case": rec["case"], "round": rec["round"],
                                        "node": ev["node"], "kind": "skipped",
                                        "pre_verb": pre_verb,
                                        "pre_args": _project_args(pre_verb or "", pre.get("args")),
                                        "act_verb": None, "act_args": None,
                                        "cls": "条件错", "sub": "skipped",
                                        "detail": f"预规划 {pre_verb}，真实状态下没走"})
                elif tag == "insert":
                    for off in range(j2 - j1):
                        act_it = act_tail[j1 + off]
                        aligned.append({"case": rec["case"], "round": rec["round"],
                                        "node": ev["node"], "kind": "inserted",
                                        "pre_verb": None, "pre_args": None,
                                        "act_verb": act_it["verb"],
                                        "act_args": _project_args(act_it["verb"], act_it["slots"]),
                                        "cls": "实际新增", "sub": "inserted",
                                        "detail": f"实际多走了 {act_it['verb']}（预规划批次外）"})

    with (out_dir / "pairs.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for p in pairs:
            fh.write(json.dumps(p, ensure_ascii=False) + "\n")
    with (out_dir / "aligned.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for a in aligned:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")

    # ---------------- 聚合 ----------------
    def _rate_line(items, label_counts=True):
        c = Counter(i["cls"] for i in items)
        con = c.get("一致", 0) + c.get("无害差异", 0)
        return (f"{len(items)} | {con} | {(con / len(items) * 100) if items else 0:.0f}% | "
                f"{c.get('参数错', 0)} | {c.get('条件错', 0)} | {c.get('无害差异', 0)}"
                + (f" | {c.get('未定', 0)}" if c.get("未定") else ""))

    cls_c = Counter(p["cls"] for p in pairs)
    sub_c = Counter(f"{p['cls']}/{p['sub'].split('/')[0]}" for p in pairs)
    n = len(pairs)
    consistent = cls_c.get("一致", 0) + cls_c.get("无害差异", 0)
    pre_only = [a for a in aligned if a["kind"] != "inserted"]
    al_c = Counter(a["cls"] for a in pre_only)
    al_n = len(pre_only)
    al_con = al_c.get("一致", 0) + al_c.get("无害差异", 0)
    inserted = [a for a in aligned if a["kind"] == "inserted"]

    by_k: dict[int, list] = {}
    by_verb: dict[str, list] = {}
    al_by_verb: dict[str, list] = {}
    for p in pairs:
        by_k.setdefault(p["k"], []).append(p)
        by_verb.setdefault(p["pre_verb"] or "?", []).append(p)
    for a in pre_only:
        al_by_verb.setdefault(a["pre_verb"] or "?", []).append(a)

    lines = ["# 多 tool_call 保真度探针报告", ""]
    lines.append(f"- 运行：{run_stats['runs']} 条（异常 {run_stats['runs_exc']} 条），"
                 f"实际执行共 {run_stats['steps_total']} 步")
    lines.append(f"- 被采纳批次事件 {run_stats['adopted_events']} 次，其中多调用 "
                 f"{run_stats['multi_events']} 次（多调用率 "
                 f"{run_stats['multi_events'] / max(run_stats['adopted_events'], 1) * 100:.0f}%）；"
                 f"批次大小分布："
                 + ", ".join(f"{k.rsplit('_', 1)[-1]} 个×{v}" for k, v in sorted(run_stats.items())
                             if k.startswith("batch_size_")))
    rest = {k: v for k, v in sorted(run_stats.items())
            if k.startswith(("c1_", "events_empty"))}
    if rest:
        lines.append("- 其余事件：" + ", ".join(f"{k}={v}" for k, v in rest.items()))
    lines += ["",
              f"## 口径①：逐位比对（任务主口径，k≥2，n={n}）", "",
              f"- **语义一致率 {(consistent / n * 100) if n else 0:.1f}%**"
              f"（精确一致 {cls_c.get('一致', 0)} + 无害差异 {cls_c.get('无害差异', 0)}）",
              f"- 参数错 {cls_c.get('参数错', 0)}（{(cls_c.get('参数错', 0) / n * 100) if n else 0:.1f}%）"
              f"；条件错 {cls_c.get('条件错', 0)}（{(cls_c.get('条件错', 0) / n * 100) if n else 0:.1f}%）",
              "- 不一致细分：" + (", ".join(f"{k}×{v}" for k, v in sub_c.most_common()) or "无"),
              "",
              f"## 口径②：序列对齐（防级联；预规划调用 n={al_n}）", "",
              f"- **语义一致率 {(al_con / al_n * 100) if al_n else 0:.1f}%**"
              f"（精确一致 {al_c.get('一致', 0)} + 无害差异 {al_c.get('无害差异', 0)}）",
              f"- 参数错 {al_c.get('参数错', 0)}；条件错 {al_c.get('条件错', 0)}"
              f"（skipped {sum(1 for a in pre_only if a['kind'] == 'skipped')}、"
              f"verb_changed {sum(1 for a in pre_only if a['kind'] == 'verb_changed')}）",
              f"- 实际新增（信息项，不计入分母）：{len(inserted)} 个"
              + (("——" + ", ".join(f"{v}×{c}" for v, c in Counter(
                  a['act_verb'] for a in inserted).most_common())) if inserted else ""),
              ""]
    if cls_c.get("未定") or al_c.get("未定"):
        lines.append(f"- ⚠ 未定（未跑裁判）：逐位 {cls_c.get('未定', 0)} 条 / 对齐 "
                     f"{al_c.get('未定', 0)} 条——加 `--judge` 复跑分析。")
        lines.append("")
    lines += ["## 分位置（口径①，批次内第 k 个调用）", "",
              "k | n | 一致(含无害) | 一致率 | 参数错 | 条件错 | 无害差异",
              "---|---|---|---|---|---|---"]
    for k in sorted(by_k):
        lines.append(f"{k} | {_rate_line(by_k[k])}")
    lines += ["", "## 分 verb（口径①，预规划调用的动词）", "",
              "verb | n | 一致(含无害) | 一致率 | 参数错 | 条件错 | 无害差异",
              "---|---|---|---|---|---|---"]
    for v in sorted(by_verb):
        lines.append(f"{v} | {_rate_line(by_verb[v])}")
    lines += ["", "## 分 verb（口径②）", "",
              "verb | n | 一致(含无害) | 一致率 | 参数错 | 条件错 | 无害差异",
              "---|---|---|---|---|---|---"]
    for v in sorted(al_by_verb):
        lines.append(f"{v} | {_rate_line(al_by_verb[v])}")
    lines += ["", "## 不一致明细（口径①）", ""]
    for p in pairs:
        if p["cls"] in ("参数错", "条件错", "未定"):
            lines.append(f"- [{p['cls']}/{p['sub']}] {p['case']} r{p['round']} "
                         f"k={p['k']}（第 {p['pos']} 步）：{p['detail']}"
                         f"｜pre={json.dumps(p['pre_args'], ensure_ascii=False)[:120]}"
                         f"｜act={json.dumps(p['act_args'], ensure_ascii=False)[:120]}")
    lines += ["", "## 旁证：批次的 c1 未被执行的事件（veto/护栏/重问——乐观执行会把这些也放跑）", ""]
    for na in not_adopted:
        lines.append(f"- {na['case']} r{na['round']} {na['node']} m={na['m']}："
                     f"c1={na.get('c1_verb') or na.get('c1_name')} "
                     f"{json.dumps(na.get('c1_args'), ensure_ascii=False)[:100]}——{na['why']}")
    if not not_adopted:
        lines.append("（无）")
    (out_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"分析完成 → {out_dir / 'report.md'}（逐位 pairs {n} 条，对齐 {al_n} 条）")
    print("\n".join(lines[:22]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default=",".join(DEFAULT_IDS))
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=1.5, help="用例间隔秒数（限速）")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--analyze", action="store_true")
    ap.add_argument("--judge", action="store_true")
    ap.add_argument("--out-dir", default=None,
                    help="采集/分析目录（默认 multicall-fidelity-probe/ 主目录；跨模型对照传子目录名）")
    ap.add_argument("--variant-prompt", action="store_true",
                    help="变体提示词臂：打松三处\"一次一个\"锚（补丁复用 probe_multicall_legality "
                         "的 _VariantArm，进程内生效、不动 src/），测放松提示词后多调用率"
                         "上行是否以保真度为代价")
    args = ap.parse_args()
    out_dir = Path(args.out_dir) if args.out_dir else OUT_DIR
    if not out_dir.is_absolute():
        out_dir = OUT_DIR / out_dir
    if args.analyze:
        analyze(args.judge, out_dir)
    elif args.variant_prompt:
        from probe_multicall_legality import _VariantArm
        with _VariantArm():
            capture([s.strip() for s in args.ids.split(",") if s.strip()],
                    args.rounds, args.sleep, args.resume, out_dir, prompt_arm="variant")
    else:
        capture([s.strip() for s in args.ids.split(",") if s.strip()],
                args.rounds, args.sleep, args.resume, out_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
