#!/usr/bin/env python3
"""decide/understand 工具通道**格式问题微验证**。

与 `evaluate_agent_live.py`（全图打分）互补：本脚本把 `_invoke_tool_channel` 的每一次
真实调用**原样捕获**（messages/tools/choice/原始应答含 tool_calls 与 invalid_tool_calls），
供离线分类「不可读」的成因（散文 / 多调用 / 幻觉工具名 / 参数坏 JSON），并把捕获到的
真实 decide 现场**逐臂回放**（auto / required / 低温等），用同一套 `_decide_answer_kind`
口径量各臂的格式合规率与动作分布——只读实验，不改生产代码。

用法：
  捕获（真 API，每用例独立沙箱，与 live 验证同纪律——绝不碰真库）：
    python scripts/probe_decide_format.py --ids k05,l07 --rounds 3
  回放（真 API，对捕获现场逐臂重放）：
    python scripts/probe_decide_format.py --replay [--replay-rounds 8]

产物 `eval/agent_decide_capture.jsonl`（逐次调用一条记录，**不入库**，与 run jsonl 同例）。
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402
from evaluate_agent_live import build_tools, load_cases  # noqa: E402

CAPTURE_PATH = ROOT / "eval" / "agent_decide_capture.jsonl"


def _ser_msg(m):
    return {"type": type(m).__name__, "content": ax._message_text(m)}


def _ser_answer(a):
    if a is None:
        return None
    tcs = []
    for c in (getattr(a, "tool_calls", None) or []):
        if isinstance(c, dict):
            tcs.append({"name": c.get("name"), "args": c.get("args")})
        else:
            tcs.append({"name": getattr(c, "name", ""), "args": getattr(c, "args", None)})
    itcs = []
    for c in (getattr(a, "invalid_tool_calls", None) or []):
        if isinstance(c, dict):
            itcs.append({"name": c.get("name"), "args": c.get("args"),
                         "error": str(c.get("error") or "")[:200]})
        else:
            itcs.append({"name": getattr(c, "name", ""), "args": getattr(c, "args", None),
                         "error": str(getattr(c, "error", "") or "")[:200]})
    return {"content": ax._message_text(a)[:2000], "tool_calls": tcs, "invalid_tool_calls": itcs}


def capture(ids: list[str], rounds: int) -> None:
    """真跑全图，spy 记录每次工具通道调用的出入。"""
    cases = []
    for fn in ("agent_live_cases_v1.jsonl", "agent_live_cases_aug.jsonl"):
        cases.extend(load_cases(ROOT / "eval" / fn))
    cases = [c for c in cases if c["id"] in ids]
    if not cases:
        print("没有命中的用例。", file=sys.stderr)
        raise SystemExit(2)
    cfg = load_llm_config()
    print(f"model={cfg.model} cases={len(cases)} rounds={rounds}")

    records: list[dict] = []
    orig = ax._invoke_tool_channel

    def spy(chat_model, *, tools, messages, choice, json_prompt=None,
            refallback_on_empty=False, name_to_verb=None, usage_sink=None, usage_node=""):
        answer, note, fb, je = orig(
            chat_model, tools=tools, messages=messages, choice=choice,
            json_prompt=json_prompt, refallback_on_empty=refallback_on_empty,
            name_to_verb=name_to_verb, usage_sink=usage_sink, usage_node=usage_node)
        kind = ""
        if usage_node == "decide" and answer is not None:
            kind, _payload = ax._decide_answer_kind(answer, ax._DECIDE_TOOL_NAME_TO_VERB)
        records.append({"case": cur[0], "round": cur[1], "node": usage_node, "choice": choice,
                        "messages": [_ser_msg(m) for m in messages], "tools": tools,
                        "answer": _ser_answer(answer), "kind": kind, "fb": fb})
        return answer, note, fb, je

    ax._invoke_tool_channel = spy
    old_tools, old_root = ax.LOOP_TOOLS, ax._agent_project_root
    cur = ["", 0]
    try:
        with CAPTURE_PATH.open("w", encoding="utf-8", newline="\n") as fh:
            for rd in range(1, rounds + 1):
                for case in cases:
                    cur[0], cur[1] = case["id"], rd
                    records.clear()
                    with tempfile.TemporaryDirectory() as tmp:
                        ax.LOOP_TOOLS = build_tools(case.get("tools") or {})
                        ax._agent_project_root = lambda: Path(tmp)
                        try:
                            plan, _trace = ax.plan_with_agent(
                                str(case["utterance"]), has_results=False, result_total=0,
                                config=cfg, retrieval=None,
                                current_query="", current_filters=None)
                            exc = ""
                        except Exception as err:  # noqa: BLE001
                            plan, exc = {}, f"{type(err).__name__}: {str(err)[:120]}"
                    for rec in records:
                        rec["steps"] = [s.get("verb") for s in (plan.get("steps") or [])]
                        rec["exc"] = exc
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
                    n_decide = sum(1 for r in records if r["node"] == "decide")
                    n_bad = sum(1 for r in records if r["node"] == "decide" and r["kind"] == "invalid")
                    print(f"[r{rd} {case['id']:>5}] decide 调用 {n_decide} 次，不可读 {n_bad} 次，"
                          f"steps={len(plan.get('steps') or [])} {exc[:60]}")
    finally:
        ax._invoke_tool_channel = orig
        ax.LOOP_TOOLS, ax._agent_project_root = old_tools, old_root
    print(f"捕获完成 → {CAPTURE_PATH}")


def _rebuild_messages(ser: list[dict]):
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
    out = []
    for m in ser:
        if m["type"] == "AIMessage":
            out.append(AIMessage(content=m["content"]))
        elif m["type"] == "SystemMessage":
            out.append(SystemMessage(content=m["content"]))
        else:
            out.append(HumanMessage(content=m["content"]))
    return out


def _salvage(answer: dict) -> str:
    """离线 salvage 分析：不可读应答里有多少能被 json_repair 救回（不调用 API）。"""
    try:
        import json_repair
    except ImportError:
        return "no_json_repair"
    name_to_verb = ax._DECIDE_TOOL_NAME_TO_VERB
    for c in (answer.get("invalid_tool_calls") or []):
        raw = c.get("args")
        if isinstance(raw, str) and raw.strip():
            fixed = json_repair.repair_json(raw, return_objects=True)
            if isinstance(fixed, dict) and str(c.get("name") or "") in name_to_verb:
                return "invalid_tool_call_repaired"
    content = str(answer.get("content") or "")
    if content.strip():
        fixed = json_repair.repair_json(content, return_objects=True)
        if isinstance(fixed, dict) and (fixed.get("verb") or fixed.get("done")):
            return "prose_json_repaired"
    return "unsalvageable"


def replay(rounds: int) -> None:
    """对捕获到的真实 decide 现场逐臂回放，量格式合规率与动作分布。"""
    recs = [json.loads(l) for l in CAPTURE_PATH.open(encoding="utf-8") if l.strip()]
    decides = [r for r in recs if r["node"] == "decide"]
    # 去重：同一现场（messages+choice 相同）只回放一次
    uniq: dict[str, dict] = {}
    for r in decides:
        h = hashlib.sha256(json.dumps(r["messages"], ensure_ascii=False, sort_keys=True)
                           .encode()).hexdigest()[:16]
        uniq.setdefault(h, r)
    states = list(uniq.values())
    print(f"捕获 decide 调用 {len(decides)} 次，去重现场 {len(states)} 个")

    # 离线 salvage 分类（原臂不可读的成因归类）
    from collections import Counter
    salvage = Counter()
    causes = Counter()
    for r in decides:
        a = r.get("answer")
        if r["kind"] != "invalid" or a is None:
            continue
        if len(a.get("tool_calls") or []) > 1:
            causes["multi_tool_call"] += 1
        elif a.get("tool_calls"):
            causes["bad_name_or_args"] += 1
        elif a.get("invalid_tool_calls"):
            causes["invalid_tool_calls(bad_json_args)"] += 1
        elif str(a.get("content") or "").strip():
            causes["prose"] += 1
        else:
            causes["empty"] += 1
        salvage[_salvage(a)] += 1
    print(f"原臂不可读成因分布: {dict(causes)}")
    print(f"原臂不可读 salvage 分布: {dict(salvage)}")

    cfg = load_llm_config()
    model_std = ax._build_chat_model(cfg)
    model_t0 = ax._build_chat_model(dataclasses.replace(cfg, temperature=0.0))
    arms = [("auto_t0.2", model_std, "auto"), ("required_t0.2", model_std, "required"),
            ("auto_t0", model_t0, "auto"), ("required_t0", model_t0, "required")]
    for arm_name, model, choice in arms:
        stat = Counter()
        for st in states:
            bound = model.bind_tools(st["tools"], tool_choice=choice, parallel_tool_calls=False)
            msgs = _rebuild_messages(st["messages"])
            for _ in range(rounds):
                try:
                    ans = bound.invoke(msgs)
                except Exception as exc:  # noqa: BLE001
                    stat[f"exc:{type(exc).__name__}"] += 1
                    continue
                kind, _p = ax._decide_answer_kind(ans, ax._DECIDE_TOOL_NAME_TO_VERB)
                stat[kind] += 1
        total = sum(stat.values())
        valid = total - stat.get("invalid", 0) - sum(v for k, v in stat.items()
                                                     if k.startswith("exc:"))
        print(f"[{arm_name}] n={total} 合规={valid} ({valid / max(total, 1) * 100:.0f}%) "
              f"分布={dict(stat)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="b12,g04,k02,k05,k09b,k11a,k11b,l07,l07a")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--replay", action="store_true")
    ap.add_argument("--replay-rounds", type=int, default=8)
    args = ap.parse_args()
    if args.replay:
        replay(args.replay_rounds)
    else:
        capture([s.strip() for s in args.ids.split(",") if s.strip()], args.rounds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
