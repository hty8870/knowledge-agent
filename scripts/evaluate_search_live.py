# -*- coding: utf-8 -*-
"""search 档集成验证（eval-search-live）：route_consensus search 路线的**真 LLM**
端到端覆盖证据。与 scripts/evaluate_agent_live.py 的分工：那边钉 action 档（剧本化假工具
+ 真 LLM 三出口）；本验证钉 **search 档**——不换 LOOP_TOOLS，rank/rerank 真跑确定性 RAG
管线（冻结 base 库，只读），LLM 全部走 `load_llm_config()` 当前配置（真实 provider）。

被测链路 = /api/utterance 的完整生产路径（turn.route_turn 单一真源）：
  用户原话 → pre-loop 确定性 RAG（search_params 与端点缺省同口径）→ 初步结果上屏闸
  （preliminary 事件）→ route_consensus 分流（2~3 票真 LLM 调用）→ scoped search 套件
  ReAct 环（rank / rerank / search.rerun / route.request）→ 批次组卷。

用例集：`eval/search_live_cases_v1.jsonl`（一行一个用例；字段见下）。
产物：`eval/search_live_run_<tag>.jsonl`（逐例原始记录：路由票型/工具调用序列/各批
Top1/用量/耗时）+ `eval/search_live_report_<tag>.md`（聚合报告 + 失败画廊）。

用例字段：
  id / cat / utterance / note（观察点说明）
  expect（全软断言，逐项计分，不存在「一错全否」；值 "any"/"optional" = 只记录不计分）：
    route: search / action / general / any——route_consensus 共识落档（trace 里
           route_consensus 节点的票型明细同步落 run 记录，含每票温度/原文/解析结果）
    prelim: yes / no / any——初步结果上屏闸（pre-loop 有命中 ∧ 无规则动作标记 → 应发
            preliminary 事件；弃权零命中或带动作标记 → 不应发）
    rank / rerank: required / optional / forbidden——环内检索工具触发期望
            （良好表述的 query 不该触发 rerank；中英错位/口语化是它的设计触发面）
    display_batch: yes / any——display 硬纪律（prompts/loop_search.md：用户等着看结果，
            收尾前至少一次检索 display=true 或 search.rerun 采纳上屏）
    screen: yes / no / any——收尾时用户屏上有结果（result_payload 非 None，
            仅 preliminary 批也算）
    must_match / must_not_match: {字段:[子串...]}——active 批 Top1 的
            species/tissue/disease/raw/platform 结构化字段必须全含/不得含（正确性主判据）。
            子串位置可嵌一层非空列表 = 任一命中组（组内任一子串命中即算），用于受控同义类
            （b01/b03 的 disease 期望从字面 cancer 校准为恶性肿瘤同义类——
            库内疾病字段用 carcinoma 等正式学名词；这是让期望编码真实需求
            "Top1 必须是恶性肿瘤数据集"，不是放宽断言）
    top1_contains: [子串...]——active 批 Top1 标题的辅助证据，不参与 PASS/FAIL
    min_total: int——active 批 result_total 下限
    final_total_max: int——active 批 result_total 上限（无批视 0；库外主题如实零命中用）

自动参与的维（无需在 expect 点名）：
    votes: 共识票型健康——存在 route_consensus 节点时，有效票 ≥2 才计过
           （废票过半 = 分流器出口歪，与落档对错分开计）。

启动自检（load 即跑，中文报错带行号）：id 唯一、cat 合法、expect 字段拼写合法、
枚举值合法、结构化字段与 top1_contains 形状合法、min_total/final_total_max 严格 int。

用法：
  python scripts/evaluate_search_live.py --selftest          # 离线自验（零 API）
  python scripts/evaluate_search_live.py --tag v1 # 集成全量（真 LLM，花额度）
  python scripts/evaluate_search_live.py --tag v1 --only s0  # 子集
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402
from dataset_recommender.agent import agent_exec as ax  # noqa: E402
from dataset_recommender.agent import turn  # noqa: E402

#: 与 /api/utterance 端点缺省同口径的 search_params（webapp.UtteranceRequest 缺省：
#: top_k=None→下游默认 10、rerank/recall off、strategy fixed、polish true）。
SEARCH_PARAMS: dict = {
    "top_k": 10, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None,
    "lenient_dims": None, "date_from": None, "date_to": None, "polish": True,
}

_CATS = ("A良好表述", "B劣质模糊", "C对照非search")
_ROUTES = ("search", "action", "general", "any")
_YN = ("yes", "no", "any")
_TOOL_EXPECT = ("required", "optional", "forbidden")
_EXPECT_KEYS = ("route", "prelim", "rank", "rerank", "display_batch", "screen",
                "top1_contains", "must_match", "must_not_match",
                "min_total", "final_total_max")
_TOP1_FIELDS = ("species", "tissue", "disease", "raw", "platform")


# ---------------------------------------------------------------- 用例加载与启动自检

def load_cases(path: Path) -> list[dict]:
    """读 jsonl + 启动自检；违规中文报错带行号并 SystemExit(2)（同 evaluate_agent_live 纪律）。"""
    errors: list[str] = []
    cases: list[dict] = []
    seen: set[str] = set()
    lines = path.read_text(encoding="utf-8").splitlines()
    for ln, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        where = f"{path.name}:{ln}"
        try:
            case = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"{where} JSON 解析失败：{exc}")
            continue
        cid = str(case.get("id") or "")
        if not cid:
            errors.append(f"{where} 缺 id")
        elif cid in seen:
            errors.append(f"{where} id 重复：{cid}")
        seen.add(cid)
        if str(case.get("cat") or "") not in _CATS:
            errors.append(f"{where} cat 非法（{case.get('cat')!r}），合法值 {_CATS}")
        if not str(case.get("utterance") or "").strip():
            errors.append(f"{where} 缺 utterance")
        expect = case.get("expect")
        if not isinstance(expect, dict) or not expect:
            errors.append(f"{where} expect 缺失或不是非空对象")
            continue
        for key in expect:
            if key not in _EXPECT_KEYS:
                errors.append(f"{where} expect 字段拼写非法：{key}（合法 {_EXPECT_KEYS}）")
        if "route" in expect and expect["route"] not in _ROUTES:
            errors.append(f"{where} expect.route 非法：{expect['route']!r}")
        for key in ("prelim", "display_batch", "screen"):
            if key in expect and expect[key] not in _YN:
                errors.append(f"{where} expect.{key} 非法：{expect[key]!r}（合法 {_YN}）")
        for key in ("rank", "rerank"):
            if key in expect and expect[key] not in _TOOL_EXPECT:
                errors.append(f"{where} expect.{key} 非法：{expect[key]!r}（合法 {_TOOL_EXPECT}）")
        if "top1_contains" in expect:
            v = expect["top1_contains"]
            if not (isinstance(v, list) and v and all(isinstance(s, str) and s for s in v)):
                errors.append(f"{where} expect.top1_contains 必须是非空字符串列表")
        for key in ("must_match", "must_not_match"):
            if key not in expect:
                continue
            value = expect[key]
            if not isinstance(value, dict) or not value:
                errors.append(f"{where} expect.{key} 必须是非空对象")
                continue
            for field, terms in value.items():
                if field not in _TOP1_FIELDS:
                    errors.append(
                        f"{where} expect.{key}.{field} 非法（合法字段 {_TOP1_FIELDS}）")
                if not (isinstance(terms, list) and terms and all(
                        (isinstance(s, str) and s)
                        or (isinstance(s, list) and s
                            and all(isinstance(x, str) and x for x in s))
                        for s in terms)):
                    errors.append(f"{where} expect.{key}.{field} 必须是非空列表"
                                  "（元素为子串，或非空子串列表 = 任一命中组）")
        for key in ("min_total", "final_total_max"):
            if key in expect and (isinstance(expect[key], bool)
                                  or not isinstance(expect[key], int) or expect[key] < 0):
                errors.append(f"{where} expect.{key} 必须是非负 int")
        cases.append(case)
    if errors:
        print("用例集启动自检未通过：", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        raise SystemExit(2)
    return cases


# ---------------------------------------------------------------- 记录提取

_CONSENSUS_ROUTE_RE = re.compile(r"走「(\w+)」路线")


def _consensus_of(trace: list[dict]) -> tuple[str, list[dict], dict | None]:
    """route_consensus 节点 → (落档, 全部原始投票, 节点条目)；无节点（agent 跌保底）→ ("", [], None)。"""
    for entry in trace or []:
        if entry.get("node") == "route_consensus":
            votes = list(entry.get("route_votes") or [])
            m = _CONSENSUS_ROUTE_RE.search(str(entry.get("detail") or ""))
            return (m.group(1) if m else ""), votes, entry
    return "", [], None


def _active_batch(result: dict) -> dict | None:
    batches = list(result.get("result_batches") or [])
    if not batches:
        return None
    active_id = str(result.get("active_batch") or "")
    for b in batches:
        if str(b.get("batch_id") or "") == active_id:
            return b
    return batches[-1]


def _batch_top1_fields(batch: dict | None) -> dict[str, str]:
    """active 批 Top1 的真实卡片字段；缺什么留空，不从标题猜。"""
    empty = {"dataset_name": "", "species": "", "tissue": "", "disease": "",
             "raw": "", "platform": ""}
    if not batch:
        return empty
    payload = batch.get("payload") or {}
    rows = payload.get("results") or []
    if not rows:
        return empty
    row = rows[0] if isinstance(rows[0], dict) else {}
    return {
        "dataset_name": str(row.get("dataset_name") or ""),
        "species": str(row.get("species") or ""),
        "tissue": str(row.get("tissue") or ""),
        "disease": str(row.get("disease") or ""),
        "raw": str(row.get("raw_data_status") or row.get("raw") or ""),
        "platform": str(row.get("platform") or ""),
    }


def _batch_top1(batch: dict | None) -> str:
    return _batch_top1_fields(batch)["dataset_name"]


def _batch_total(batch: dict | None) -> int:
    if not batch:
        return 0
    return int((batch.get("payload") or {}).get("result_total") or 0)


def extract_record(case: dict, result: dict, events: list[dict], ms: int,
                   exc: str = "") -> dict:
    """route_turn 返回体 + 事件流 → 逐例原始记录（路由票型/工具序列/Top1 全留痕）。"""
    plan = result.get("plan") or {}
    trace = list(plan.get("trace") or [])
    consensus_route, votes, _rc_entry = _consensus_of(trace)
    steps = [s for s in (plan.get("steps") or []) if isinstance(s, dict)]
    prelim_payloads = [e["entry"] for e in events if e.get("kind") == "preliminary"]
    batches = list(result.get("result_batches") or [])
    loop_batches = [b for b in batches if str(b.get("kind") or "") != "preliminary"]
    active = _active_batch(result)
    active_top1_fields = _batch_top1_fields(active)
    step_briefs = []
    for s in steps:
        res = s.get("result") if isinstance(s.get("result"), dict) else {}
        brief = {"verb": s.get("verb"), "ok": bool(s.get("ok"))}
        if s.get("verb") in ("rank", "rerank", "search.rerun"):
            brief["query"] = res.get("query") or res.get("rewritten_query") or ""
            brief["total"] = res.get("total")
            brief["displayed"] = bool(res.get("displayed") or res.get("adopted"))
            if s.get("verb") == "rerank":
                brief["rewritten"] = bool(res.get("rewritten"))
                brief["rewritten_query"] = res.get("rewritten_query") or ""
        if s.get("verb") == "route.request":
            brief["target_route"] = (s.get("slots") or {}).get("target_route")
        step_briefs.append(brief)
    return {
        "id": case["id"], "cat": case["cat"], "utterance": case["utterance"],
        "note": str(case.get("note") or ""),
        "ms": ms, "exc": exc,
        # ---- 路由 ----
        "turn_route": result.get("route"), "via": result.get("via"),
        "consensus_route": consensus_route,
        "votes": [
            {"temperature": v.get("temperature"), "bound": v.get("bound"),
             "ok": bool(v.get("ok")), "route": v.get("route") or "",
             "reason": v.get("reason") or "", "raw": str(v.get("raw") or "")[:200],
             "error": v.get("error") or ""}
            for v in votes],
        # ---- 上屏 ----
        "preliminary_fired": bool(prelim_payloads),
        "preliminary_total": (int((prelim_payloads[0] or {}).get("result_total") or 0)
                              if prelim_payloads else 0),
        "preliminary_top1": (_batch_top1({"payload": prelim_payloads[0]})
                             if prelim_payloads else ""),
        "preliminary_top1_fields": (_batch_top1_fields({"payload": prelim_payloads[0]})
                                    if prelim_payloads else _batch_top1_fields(None)),
        "preliminary_final": bool(result.get("preliminary_final")),
        "batches": [{"batch_id": b.get("batch_id"), "kind": b.get("kind"),
                     "query_effective": b.get("query_effective"),
                     "total": _batch_total(b), "top1": _batch_top1(b),
                     "top1_fields": _batch_top1_fields(b)}
                    for b in batches],
        "active_batch": result.get("active_batch"),
        "screen": result.get("result_payload") is not None,
        "active_top1": active_top1_fields["dataset_name"],
        "active_top1_fields": active_top1_fields,
        "active_total": _batch_total(active),
        # ---- 工具环 ----
        "plan_verb": plan.get("verb"),
        "steps": step_briefs,
        "report_source": plan.get("report_source"),
        "report_zh": str(plan.get("report_zh") or "")[:400],
        "echo_zh": str(result.get("echo_zh") or "")[:200],
        "llm_usage": plan.get("llm_usage"),
        "nodes": [t.get("node") for t in trace],
    }


# ---------------------------------------------------------------- 计分

def score_case(case: dict, rec: dict) -> list[dict]:
    """全软断言逐项计分（"any"/"optional" 只记录不进 checks）。"""
    expect = case.get("expect") or {}
    checks: list[dict] = []

    def add(dim, ok, detail="", *, required=True):
        checks.append({"dim": dim, "ok": bool(ok), "detail": detail,
                       "required": bool(required)})

    votes = rec.get("votes") or []
    if rec.get("exc"):
        add("route", False, f"执行异常：{rec['exc']}")
        return checks
    if not rec.get("consensus_route") and not votes:
        # agent 路径跌保底（via != agent / plan 无 trace）——route 维记败并点名，votes 不适用
        if expect.get("route", "any") != "any":
            add("route", False,
                f"agent 未走通（via={rec.get('via')!r}），无共识票型可取")
        return checks

    n_valid = sum(1 for v in votes if v.get("ok"))
    add("votes", n_valid >= 2,
        f"{len(votes)} 票（有效 {n_valid}）："
        + ", ".join(f"{v.get('route') or '废'}@{v.get('temperature')}" for v in votes))

    want_route = expect.get("route", "any")
    if want_route != "any":
        add("route", rec.get("consensus_route") == want_route,
            f"共识落档 {rec.get('consensus_route')!r}（期望 {want_route!r}）")

    want_prelim = expect.get("prelim", "any")
    if want_prelim != "any":
        add("prelim", bool(rec.get("preliminary_fired")) == (want_prelim == "yes"),
            f"preliminary {'已发' if rec.get('preliminary_fired') else '未发'}"
            f"（期望 {want_prelim}；pre-loop total={rec.get('preliminary_total')}）")

    verbs = [str(s.get("verb") or "") for s in rec.get("steps") or []]
    for tool in ("rank", "rerank"):
        want = expect.get(tool, "optional")
        used = tool in verbs
        if want == "required":
            add(tool, used, f"工具序列 {verbs}" if not used else "")
        elif want == "forbidden":
            add(tool, not used, f"不应触发却出现在工具序列 {verbs}" if used else "")

    want_display = expect.get("display_batch", "any")
    if want_display == "yes":
        loop_kinds = [b.get("kind") for b in rec.get("batches") or []
                      if b.get("kind") != "preliminary"]
        add("display_batch", bool(loop_kinds),
            "环内无上屏批（display 硬纪律：收尾前至少一次 display=true / 采纳上屏）"
            if not loop_kinds else f"环内上屏批 {loop_kinds}")

    want_screen = expect.get("screen", "any")
    if want_screen != "any":
        add("screen", bool(rec.get("screen")) == (want_screen == "yes"),
            f"收尾屏上{'有' if rec.get('screen') else '无'}结果（期望 {want_screen}）")

    top1_want = expect.get("top1_contains") or []
    if top1_want:
        top1 = str(rec.get("active_top1") or "")
        low = top1.lower()
        misses = [s for s in top1_want if s.lower() not in low]
        # 标题只留辅助证据。标题含 lung 不能让 Normal Lung 冒充肺癌。
        add("title_evidence", not misses,
            f"active 批 Top1 缺子串 {misses}：{top1[:120]!r}" if misses
            else f"Top1 标题辅助命中：{top1[:120]}", required=False)

    top1_fields = rec.get("active_top1_fields") or {}

    def _term_hit(term, low: str) -> bool:
        """子串命中判定；term 为列表时是任一命中组（组内任一子串命中即算，受控同义类用）。"""
        if isinstance(term, list):
            return any(str(x).lower() in low for x in term)
        return str(term).lower() in low

    for field, terms in (expect.get("must_match") or {}).items():
        actual = str(top1_fields.get(field) or "")
        low = actual.lower()
        misses = [term for term in terms if not _term_hit(term, low)]
        add(f"top1:{field}", not misses,
            (f"Top1.{field} 缺子串 {misses}：{actual[:160]!r}" if misses
             else f"Top1.{field} 命中：{actual[:160]}"))
    for field, terms in (expect.get("must_not_match") or {}).items():
        actual = str(top1_fields.get(field) or "")
        low = actual.lower()
        hits = [term for term in terms if _term_hit(term, low)]
        add(f"top1_not:{field}", not hits,
            (f"Top1.{field} 命中禁用子串 {hits}：{actual[:160]!r}" if hits
             else f"Top1.{field} 未命中禁用词"))

    if "min_total" in expect:
        add("min_total", int(rec.get("active_total") or 0) >= expect["min_total"],
            f"active 批 total={rec.get('active_total')}（下限 {expect['min_total']}）")
    if "final_total_max" in expect:
        add("final_total_max",
            int(rec.get("active_total") or 0) <= expect["final_total_max"],
            f"active 批 total={rec.get('active_total')}（上限 {expect['final_total_max']}）"
            if int(rec.get("active_total") or 0) > expect["final_total_max"] else "")
    return checks


# ---------------------------------------------------------------- 主流程

def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
                             capture_output=True, text=True, timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "unknown"


def _worktree_dirty() -> bool:
    try:
        out = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                             capture_output=True, text=True, timeout=15)
        return out.returncode == 0 and bool(out.stdout.strip())
    except Exception:  # noqa: BLE001
        return True


def _make_sandbox_root(tmp: Path) -> Path:
    """运行级沙箱根：写面（账本/候选池/审计落盘）隔离进 tmp，**读面与生产逐位一致**。

    `_agent_project_root` 不只是写面根——`_prompt_md` 经它解析 prompts/（图内提示词的
    文件真源）、`_loop_db_status` 经它读 database/、成功经验库注入经它读
    .userdata/curate_examples.jsonl。裸 tmp 会让 route_consensus 静默跌内置降级提示词
    （本任务首轮实跑踩中，作废重跑）：读面三件（prompts/、database/base、
    database/external、成功经验库）镜像进沙箱，写面自然落在沙箱里，绝不碰真库。
    """
    import shutil

    root = Path(tmp)
    shutil.copytree(ROOT / "prompts", root / "prompts")
    for sub in ("base", "external"):
        src = ROOT / "database" / sub
        if src.is_dir():
            shutil.copytree(src, root / "database" / sub)
    examples = ROOT / ".userdata" / "curate_examples.jsonl"
    if examples.is_file():
        (root / ".userdata").mkdir(exist_ok=True)
        shutil.copy2(examples, root / ".userdata" / "curate_examples.jsonl")
    return root


def _execute_case(case: dict, cfg, sandbox_root: Path) -> dict:
    """单用例：沙箱根由 main 装配（读面镜像见 `_make_sandbox_root`），走完整 route_turn 生产路径。"""
    started = time.monotonic()
    events: list[dict] = []

    def _collect(kind: str, entry: dict) -> None:
        events.append({"kind": kind, "entry": entry})

    old_root = ax._agent_project_root
    result: dict = {}
    exc = ""
    ax._agent_project_root = lambda: sandbox_root
    try:
        result = turn.route_turn(
            str(case["utterance"]),
            has_results=False, result_total=0,
            current_query="", current_filters=None,
            config=cfg, use_agent=True,
            on_event=_collect, principal="",
            search_params=dict(SEARCH_PARAMS),
        )
    except Exception as err:  # noqa: BLE001——route_turn 契约永不抛，这里留结构性防御
        exc = f"{type(err).__name__}: {str(err)[:160]}"
    finally:
        ax._agent_project_root = old_root
    ms = int((time.monotonic() - started) * 1000)
    rec = extract_record(case, result or {}, events, ms, exc=exc)
    rec["checks"] = score_case(case, rec)
    rec["passed"] = bool(rec["checks"]) and all(
        c["ok"] for c in rec["checks"] if c.get("required", True))
    return rec


def _write_report(report_path: Path, *, tag: str, cases_path: Path, cfg,
                  records: list[dict], elapsed_s: float) -> None:
    def _rate(items):
        return (f"{sum(items)}/{len(items)} ({(sum(items) / len(items) * 100) if items else 0:.1f}%)")

    by_cat: dict[str, list[bool]] = {}
    dim_stat: dict[str, list[bool]] = {}
    for r in records:
        by_cat.setdefault(r["cat"], []).append(r["passed"])
        for c in r["checks"]:
            dim_stat.setdefault(c["dim"], []).append(c["ok"])

    cases_sha = hashlib.sha256(cases_path.read_bytes()).hexdigest()
    harness_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:12]
    agent_exec_sha = hashlib.sha256(Path(ax.__file__).read_bytes()).hexdigest()[:12]

    lines: list[str] = []
    lines.append(f"# search 档真机探针报告 · {tag}")
    lines.append("")
    lines.append(f"- 总分：**{_rate([r['passed'] for r in records])}**（全软断言逐维计分，"
                 "单例全维过才算过）")
    lines.append(f"- model={getattr(cfg, 'model', '?')} provider={getattr(cfg, 'provider', '?')}"
                 f" base_url={getattr(cfg, 'base_url', '?')}")
    lines.append(f"- commit={_git_commit()} dirty={_worktree_dirty()} 用时={elapsed_s:.0f}s")
    lines.append(f"- cases sha256={cases_sha}")
    lines.append(f"- harness sha256[:12]={harness_sha} agent_exec sha256[:12]={agent_exec_sha}")
    lines.append("")
    lines.append("## 分类小计")
    lines.append("")
    for cat, items in by_cat.items():
        lines.append(f"- {cat}：{_rate(items)}")
    lines.append("")
    lines.append("## 维度小计")
    lines.append("")
    lines.append("| 维度 | 通过率 |")
    lines.append("|---|---|")
    for dim, items in dim_stat.items():
        lines.append(f"| {dim} | {_rate(items)} |")
    lines.append("")
    lines.append("## 逐例总览（路由票型 / 工具调用序列 / Top1）")
    lines.append("")
    lines.append("| id | 共识落档（票型） | 工具序列 | prelim | active 批 Top1 | 结果 |")
    lines.append("|---|---|---|---|---|---|")
    for r in records:
        vote_str = ",".join(f"{v['route'] or '废'}" for v in r.get("votes") or []) or "—"
        seq = " → ".join(str(s.get("verb")) for s in r.get("steps") or []) or "—"
        prelim = (f"✓({r['preliminary_total']})" if r.get("preliminary_fired") else "✗")
        top1 = str(r.get("active_top1") or "—")
        if len(top1) > 60:
            top1 = top1[:57] + "..."
        mark = "PASS" if r["passed"] else ("ERR" if r["exc"] else "FAIL")
        lines.append(f"| {r['id']} | {r.get('consensus_route') or '—'}（{vote_str}）"
                     f" | {seq} | {prelim} | {top1} | {mark} |")
    lines.append("")
    fails = [r for r in records if not r["passed"]]
    lines.append("## 失败画廊")
    lines.append("")
    if not fails:
        lines.append("（无失败用例）")
    for r in fails:
        lines.append(f"### {r['id']} · {r['utterance']}")
        if r["exc"]:
            lines.append(f"- 执行异常：{r['exc']}")
        for c in r["checks"]:
            if not c["ok"]:
                lines.append(f"- [{c['dim']}] {c['detail']}")
        lines.append("")
    lines.append("## 怎么读")
    lines.append("")
    lines.append("- 票型列：route_consensus 每票的落档（温度 0.0/0.8 两票，分歧加投 0.5）；"
                 "原始投票（含原文）在 run jsonl 的 votes 字段。")
    lines.append("- prelim 列：✓(N) = pre-loop 初步结果已上屏（N=命中数）；✗ = 未发。")
    lines.append("- Top1 正确性按 run JSON 的 active_top1_fields（species/tissue/disease/raw/platform）"
                 "与 must_match/must_not_match 判；标题子串仅作辅助证据，不参与 PASS/FAIL。")
    lines.append("- 本报告是**行为证据**而非门禁：search 档的 LLM 行为有温度抖动，"
                 "复跑结论以维度小计的趋势为准。")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _run_selftest(cases_path: Path) -> int:
    """离线自验（零 API）：用例集自检 + 计分双向合成断言。"""
    failures: list[str] = []

    def check(name: str, fn) -> None:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")

    # ① 真用例集自检全过
    cases = load_cases(cases_path)
    if not cases:
        failures.append("用例集为空")

    # ② 计分双向合成断言
    good_rec = {
        "exc": "", "votes": [{"ok": True, "route": "search", "temperature": 0.0},
                             {"ok": True, "route": "search", "temperature": 0.8}],
        "consensus_route": "search", "preliminary_fired": True, "preliminary_total": 26,
        "batches": [{"kind": "preliminary"}, {"kind": "rank"}],
        "screen": True, "active_top1": "Human Lung Cancer FFPE",
        "active_top1_fields": {"dataset_name": "Human Lung Cancer FFPE",
                               "species": "Human", "tissue": "Lung",
                               "disease": "lung cancer", "raw": "✅ 包含 FASTQ",
                               "platform": "visium"},
        "active_total": 26,
        "steps": [{"verb": "rank", "ok": True}],
    }
    good_case = {"id": "g", "expect": {
        "route": "search", "prelim": "yes", "rerank": "forbidden",
        "display_batch": "yes", "screen": "yes",
        "top1_contains": ["lung"],
        "must_match": {"disease": ["lung cancer"], "species": ["human"]},
        "must_not_match": {"species": ["mouse"]}, "min_total": 1}}

    def _good_passes():
        checks = score_case(good_case, good_rec)
        assert checks and all(c["ok"] for c in checks), \
            f"合成好样本应全过：{[c for c in checks if not c['ok']]}"

    def _bad_route_fails():
        rec = dict(good_rec, consensus_route="general")
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["route"]["ok"] is False and "general" in d["route"]["detail"]

    def _bad_prelim_fails():
        rec = dict(good_rec, preliminary_fired=False)
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["prelim"]["ok"] is False

    def _forbidden_rerank_fails():
        rec = dict(good_rec, steps=[{"verb": "rerank", "ok": True}])
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["rerank"]["ok"] is False and "rerank" in d["rerank"]["detail"]

    def _missing_display_fails():
        rec = dict(good_rec, batches=[{"kind": "preliminary"}])
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["display_batch"]["ok"] is False

    def _top1_miss_fails():
        rec = dict(good_rec, active_top1="Human Normal Lung Atlas",
                   active_top1_fields=dict(good_rec["active_top1_fields"], disease="healthy"))
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["top1:disease"]["ok"] is False and "lung cancer" in d["top1:disease"]["detail"]
        assert d["title_evidence"]["ok"] is True and d["title_evidence"]["required"] is False

    def _title_miss_is_auxiliary():
        rec = dict(good_rec, active_top1="opaque title")
        checks = score_case(good_case, rec)
        title = next(c for c in checks if c["dim"] == "title_evidence")
        assert title["ok"] is False and title["required"] is False
        assert all(c["ok"] for c in checks if c.get("required", True))

    def _zero_hit_case():
        case = {"id": "z", "expect": {"route": "search", "prelim": "no",
                                      "final_total_max": 0}}
        rec = dict(good_rec, preliminary_fired=False, batches=[],
                   screen=False, active_total=0, steps=[])
        checks = score_case(case, rec)
        assert all(c["ok"] for c in checks), [c for c in checks if not c["ok"]]
        rec2 = dict(rec, screen=True, active_total=3,
                    batches=[{"kind": "rank"}])
        d = {c["dim"]: c for c in score_case(case, rec2)}
        assert d["final_total_max"]["ok"] is False

    def _fallback_route_fails():
        rec = dict(good_rec, votes=[], consensus_route="", via="llm")
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["route"]["ok"] is False and "via" in d["route"]["detail"]
        assert "votes" not in d  # 无共识节点 → votes 不适用不参评

    def _exc_short_circuits():
        rec = dict(good_rec, exc="AgentUnavailable: x")
        checks = score_case(good_case, rec)
        assert len(checks) == 1 and checks[0]["dim"] == "route" and not checks[0]["ok"]

    def _weak_votes_fail():
        rec = dict(good_rec, votes=[{"ok": True, "route": "search", "temperature": 0.0},
                                    {"ok": False, "route": "", "temperature": 0.8}])
        d = {c["dim"]: c for c in score_case(good_case, rec)}
        assert d["votes"]["ok"] is False

    def _anyof_group_scoring():
        # 任一命中组（受控同义类）：carcinoma 满足癌症类期望；healthy 不满足
        case = {"id": "a", "expect": {"route": "search", "prelim": "yes",
                                      "must_match": {"disease": [["cancer", "carcinoma"]]},
                                      "must_not_match": {"species": [["mouse", "rat"]]}}}
        rec = dict(good_rec, active_top1_fields=dict(good_rec["active_top1_fields"],
                                                     disease="Ovarian Papillary Serous Carcinoma"))
        d = {c["dim"]: c for c in score_case(case, rec)}
        assert d["top1:disease"]["ok"] is True and d["top1_not:species"]["ok"] is True
        rec2 = dict(rec, active_top1_fields=dict(good_rec["active_top1_fields"], disease="healthy"))
        d2 = {c["dim"]: c for c in score_case(case, rec2)}
        assert d2["top1:disease"]["ok"] is False
        rec3 = dict(rec, active_top1_fields=dict(good_rec["active_top1_fields"], species="Mouse"))
        d3 = {c["dim"]: c for c in score_case(case, rec3)}
        assert d3["top1_not:species"]["ok"] is False

    for name, fn in list(locals().items()):
        if name.startswith("_") and callable(fn) and name != "_run_selftest":
            check(name, fn)

    # ③ 坏样本自检报错带行号
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "bad.jsonl"
        bad.write_text('{"id": "x", "cat": "不存在", "utterance": "u", "expect": {"route": "nowhere"}}\n',
                       encoding="utf-8")
        try:
            load_cases(bad)
            failures.append("坏样本未被自检拦下")
        except SystemExit:
            pass

    if failures:
        print("selftest 失败：", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print(f"selftest 全过（{len(cases)} 条用例自检 + 计分双向断言 + 坏样本拦截）。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="eval/search_live_cases_v1.jsonl")
    ap.add_argument("--tag", default="v1")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--selftest", action="store_true",
                    help="离线自验（零 API）：用例集自检 + 计分双向合成断言 + 坏样本拦截")
    args = ap.parse_args()

    cases_path = ROOT / args.cases
    if args.selftest:
        return _run_selftest(cases_path)

    cases = load_cases(cases_path)  # 启动自检不过 → 中文报错行号 + SystemExit(2)
    if args.only:
        cases = [c for c in cases if args.only in c["id"] or args.only in c["cat"]]
    if args.limit is not None:
        cases = cases[: args.limit]
    if not cases:
        # 空集路径（--limit 0 / --only 未命中）：不落任何产物文件（同 evaluate_agent_live
        # 两次误跑覆写历史产物事故的纪律）。
        print("用例集为空（健康检查路径），不写产物文件。")
        return 0
    cfg = load_llm_config()
    print(f"model={getattr(cfg, 'model', '?')} cases={len(cases)}")

    run_path = ROOT / "eval" / f"search_live_run_{args.tag}.jsonl"
    report_path = ROOT / "eval" / f"search_live_report_{args.tag}.md"
    records: list[dict] = []
    t0 = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        sandbox_root = _make_sandbox_root(Path(tmp))
        with run_path.open("w", encoding="utf-8", newline="\n") as fh:
            for i, case in enumerate(cases, 1):
                rec = _execute_case(case, cfg, sandbox_root)
                records.append(rec)
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                fh.flush()
                mark = "PASS" if rec["passed"] else ("ERR " if rec["exc"] else "FAIL")
                print(f"[{i:>3}/{len(cases)}] {mark} {case['id']} "
                      f"route={rec.get('consensus_route') or '—'} "
                      f"steps={[s['verb'] for s in rec.get('steps') or []]} "
                      f"{rec['ms'] / 1000:.1f}s")
    _write_report(report_path, tag=args.tag, cases_path=cases_path, cfg=cfg,
                  records=records, elapsed_s=time.monotonic() - t0)
    n_pass = sum(1 for r in records if r["passed"])
    print(f"完成：{n_pass}/{len(records)} 通过；产物 {run_path.name} / {report_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
