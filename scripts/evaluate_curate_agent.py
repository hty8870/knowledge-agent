# -*- coding: utf-8 -*-
"""对话式数据库管护（curate）的 **execution-based** 冻结评测门。

**借鉴 BIRD 的 EX 指标**：断言的不是 agent
输出了什么文本，而是**动作执行后沙盒里库的最终文件状态**——外部库/回收站/账本三个面。
与 evaluate_recommendation.py（检索二元门）互补：那条管「推荐对不对」，这条管「管护做完
库变成了什么样」。

被测链（全确定性、零网络、零 LLM）：
    utterance + raw（模拟 LLM 输出）→ action_plan.build_plan_from_raw（护栏/槽位/极性门）
    → corpus_curation.run_curate_action（plan → confirm_token → apply，沙盒 project_root）
    → 终态断言（external/recycle/manifest + 中间结果字段）
LLM → raw 那一段由集成验证 `evaluate_agent_live.py` 覆盖，不在本门（本门要的是可复现）。

负例纪律（对照 BIRD 排除空结果题的做法，**反向收录**）：「把 elephant 数据集删掉」必须
unknown_file 婉拒且**零副作用**；取消态/降级 none/词表外动词同理——文件系统一个字节都不许动。

金标：`eval/eval_curate_agent.json`（22 例）。改 agent_exec / action_plan / corpus_curation
前必跑——这三处是高 churn 区，本门就是它们的回归网。

用法：
  PYTHONPATH=src py scripts/evaluate_curate_agent.py                 # 全量 + 打印摘要
  PYTHONPATH=src py scripts/evaluate_curate_agent.py --out eval/curate_agent_report.json
  PYTHONPATH=src py scripts/evaluate_curate_agent.py --case c09      # 只跑一例（调试）
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dataset_recommender.agent import action_plan as AP                     # noqa: E402
from dataset_recommender.corpus import corpus_curation as CC                # noqa: E402
from dataset_recommender.corpus import corpus_status as CS                  # noqa: E402

GOLD = AGENT_ROOT / "eval" / "eval_curate_agent.json"


def _write_tree(root: Path, setup: dict) -> None:
    """按金标 setup 在沙盒里摆好 external / recycle 初态。"""
    ext = root / "database" / "external"
    rec = root / ".userdata" / "recycle"
    for name, records in (setup.get("external") or {}).items():
        ext.mkdir(parents=True, exist_ok=True)
        (ext / name).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    for name, text in (setup.get("external_text") or {}).items():
        ext.mkdir(parents=True, exist_ok=True)
        (ext / name).write_text(str(text), encoding="utf-8")
    for name, records in (setup.get("recycle") or {}).items():
        rec.mkdir(parents=True, exist_ok=True)
        (rec / name).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


def _names(root: Path, sub: str) -> list[str]:
    d = root / sub
    return sorted(p.name for p in d.glob("*.json")) if d.is_dir() else []


def _manifest_actions(root: Path) -> list[str]:
    path = root / ".userdata" / "recycle" / "manifest.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(str(json.loads(line).get("action") or ""))
        except Exception:
            continue
    return out


def _contains(names: list[str], substr: str) -> bool:
    return any(substr in n for n in names)


def _err(stage: str, exc: Exception) -> dict:
    code = str(getattr(exc, "code", "") or type(exc).__name__)
    return {"stage": stage, "error_code": code, "result": {}, "plan": {}}


def _run_step(step: dict, plan_slots: dict, root: Path) -> dict:
    """执行一个驱动步，返回 {"stage", "error_code", "result", "plan"}（异常如实记码记阶段，不抛）。"""
    action = str(step.get("action") or "")
    try:
        if action in ("remove", "restore", "import"):
            kwargs: dict = {}
            if action == "remove":
                kwargs["filename"] = str(step.get("filename") or plan_slots.get("target") or "").strip()
            elif action == "restore":
                target_sub = str(step.get("restore_target") or "").strip()
                kwargs["filename"] = next(
                    (n for n in _names(root, ".userdata/recycle") if target_sub in n), target_sub)
            else:
                kwargs["filename"] = str(step.get("filename") or "curate_import.json")
                if "payload_text" in step:
                    kwargs["payload_bytes"] = str(step["payload_text"]).encode("utf-8")
                else:
                    kwargs["payload_bytes"] = json.dumps(
                        step.get("payload") or [], ensure_ascii=False).encode("utf-8")
            try:
                pr = CC.run_curate_action(action, dry_run=True, project_root=root, **kwargs)
            except Exception as exc:
                return _err("plan", exc)
            if action == "remove" and step.get("tamper_before_apply"):
                target = root / "database" / "external" / kwargs["filename"]
                target.write_text(json.dumps([{"uid": "tampered"}], ensure_ascii=False), encoding="utf-8")
            try:
                ar = CC.run_curate_action(action, dry_run=False, project_root=root,
                                          confirm_token=pr["confirm_token"],
                                          force=bool(step.get("force")) if action == "import" else False,
                                          **kwargs)
            except Exception as exc:
                out = _err("apply", exc)
                out["plan"] = pr
                return out
            return {"stage": "apply", "error_code": "", "result": ar, "plan": pr}
        if action == "list":
            return {"stage": "run", "error_code": "", "plan": {},
                    "result": CC.run_curate_action("list", project_root=root)}
        if action == "db_status":
            res = CS.db_status(project_root=root)
            flat = dict(res)
            flat["external_files_len"] = len(res.get("external_files") or [])
            flat["recycle_len"] = len(res.get("recycle") or [])
            return {"stage": "run", "error_code": "", "plan": {}, "result": flat}
        return {"stage": "run", "error_code": f"unknown_drive_action:{action}", "result": {}, "plan": {}}
    except Exception as exc:  # 结构性防御：驱动层自身的意外（非业务 CurateError/UploadError）
        return _err("drive", exc)


def run_case(case: dict) -> dict:
    """单例：沙盒初态 → plan 断言 → 驱动执行 → 终态断言。返回 {id, ok, failures[]}。"""
    failures: list[str] = []
    cid = str(case.get("id") or "?")
    utterance = str(case.get("utterance") or "")
    raw = dict(case.get("raw") or {})

    plan = AP.build_plan_from_raw(raw, utterance, has_results=False, result_total=0)
    expect_verb = str(case.get("expect_verb") or "")
    if str(plan.get("verb") or "") != expect_verb:
        failures.append(f"verb：期望 {expect_verb}，实得 {plan.get('verb')}")
    if "expect_cancelled" in case and bool(plan.get("cancelled")) != bool(case["expect_cancelled"]):
        failures.append(f"cancelled：期望 {case['expect_cancelled']}，实得 {plan.get('cancelled')}")
    for rej in (case.get("expect_rejected") or []):
        if rej not in (plan.get("rejected") or []):
            failures.append(f"rejected 里该有 {rej}（词表外动词只报不做），实得 {plan.get('rejected')}")

    with tempfile.TemporaryDirectory(prefix="curate_eval_") as td:
        root = Path(td)
        _write_tree(root, case.get("setup") or {})
        steps = list((case.get("drive") or {}).get("steps") or [])
        slots = dict(plan.get("slots") or {})
        last: dict = {"stage": "", "error_code": "", "result": {}, "plan": {}}
        for step in steps:
            last = _run_step(step, slots, root)
            if last["error_code"]:
                break  # 失败即停：与真实两步链同口径（plan 挂了没有 apply）

        expect = case.get("expect") or {}
        exp_plan_err = str(expect.get("plan_error") or "")
        exp_apply_err = str(expect.get("apply_error") or "")
        if exp_plan_err or exp_apply_err:
            want = exp_plan_err or exp_apply_err
            if last["error_code"] != want:
                failures.append(f"错误码：期望 {want}，实得 {last['error_code'] or '（无错）'}")
        elif last["error_code"]:
            failures.append(f"不该失败却失败：{last['error_code']}")

        plan_res = last.get("plan") or {}
        for k, v in (expect.get("plan_fields") or {}).items():
            if plan_res.get(k) != v:
                failures.append(f"plan.{k}：期望 {v!r}，实得 {plan_res.get(k)!r}")
        result = last.get("result") or {}
        for k, v in (expect.get("result_fields") or {}).items():
            if result.get(k) != v:
                failures.append(f"result.{k}：期望 {v!r}，实得 {result.get(k)!r}")

        ext_names = _names(root, "database/external")
        rec_names = _names(root, ".userdata/recycle")
        actions = _manifest_actions(root)
        if "external_count" in expect and len(ext_names) != int(expect["external_count"]):
            failures.append(f"external 文件数：期望 {expect['external_count']}，实得 {len(ext_names)} {ext_names}")
        if "recycle_count" in expect and len(rec_names) != int(expect["recycle_count"]):
            failures.append(f"recycle 文件数：期望 {expect['recycle_count']}，实得 {len(rec_names)} {rec_names}")
        for s in (expect.get("external_contains") or []):
            if not _contains(ext_names, s):
                failures.append(f"external 该有含「{s}」的文件，实得 {ext_names}")
        for s in (expect.get("external_absent") or []):
            if _contains(ext_names, s):
                failures.append(f"external 不该有含「{s}」的文件，实得 {ext_names}")
        for s in (expect.get("recycle_contains") or []):
            if not _contains(rec_names, s):
                failures.append(f"recycle 该有含「{s}」的文件，实得 {rec_names}")
        for s in (expect.get("recycle_absent") or []):
            if _contains(rec_names, s):
                failures.append(f"recycle 不该有含「{s}」的文件，实得 {rec_names}")
        for a in (expect.get("manifest_contains") or []):
            if a not in actions:
                failures.append(f"回收站账本该有「{a}」行，实得 {actions}")
        for a in (expect.get("manifest_absent") or []):
            if a in actions:
                failures.append(f"回收站账本不该有「{a}」行，实得 {actions}")

    return {"id": cid, "utterance": utterance, "ok": not failures, "failures": failures}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", default=str(GOLD))
    ap.add_argument("--case", default="", help="只跑某一例（按 id）")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    cases = json.loads(Path(args.gold).read_text(encoding="utf-8"))["cases"]
    if args.case:
        cases = [c for c in cases if str(c.get("id")) == args.case]
        if not cases:
            print(f"金标里没有 id={args.case}")
            return 2

    rows = [run_case(c) for c in cases]
    failed = [r for r in rows if not r["ok"]]
    for r in rows:
        mark = "PASS" if r["ok"] else "FAIL"
        print(f"[{mark}] {r['id']}  {r['utterance']}")
        for f in r["failures"]:
            print(f"       - {f}")
    print("-" * 60)
    print(f"通过 {len(rows) - len(failed)}/{len(rows)}"
          + ("  —— 全部通过" if not failed else f"  —— 失败 {len(failed)} 例：{[r['id'] for r in failed]}"))
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"total": len(rows), "passed": len(rows) - len(failed), "cases": rows},
            ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已存：{args.out}")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
