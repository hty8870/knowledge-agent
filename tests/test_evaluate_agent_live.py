# -*- coding: utf-8 -*-
"""集成验证 harness（scripts/evaluate_agent_live.py）的离线单测。

覆盖两个增量（全部离线，绝不发 LLM 请求）：
1. 失败聚类 `_cluster_failures`：按 (首败维度, 首个 ok=false 的 agent 节点, verb)
   三元组聚簇 + 概括模板；
2. 新维 `number_grounded`：汇报整数必须能在 steps 工具返回 JSON 里数值相等命中，
   含词边界、千分位、步序号/百分比/年份/日期豁免与不适用口径。
score_case 其余维度的行为钉在 harness 自带 `--selftest` 里，这里不重复。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import evaluate_agent_live as eal  # noqa: E402

_DB_RESULT = {"total_records": 4756, "sources": [
    {"source": "10x", "label": "10x Genomics", "local_count": 774,
     "snapshot_date": "2026-08-01"}]}


def _step(verb="curate.db_status", ok=True, result=None):
    s = {"verb": verb, "ok": ok, "slots": {}, "readonly": True}
    if result is not None:
        s["result"] = result
    return s


def _dims(plan, expect=None):
    case = {"id": "t01", "cat": "A单步路由", "utterance": "u",
            "tools": {}, "expect": expect if expect is not None else {"first": "none"}}
    return {c["dim"]: c for c in eal.score_case(case, plan, [])}


# ------------------------------------------------------------------ number_grounded

def test_number_grounded_misses_grounded_and_thousands():
    steps = [_step(result=_DB_RESULT)]
    assert eal._number_grounded_misses(steps, "库内共 4756 条。") == []
    # 千分位逗号先剥离，4,756 ≡ 4756
    assert eal._number_grounded_misses(steps, "库内共 4,756 条。") == []
    # 负载深处的数字同样有出处（774 / 的 08、01）
    assert eal._number_grounded_misses(steps, "10x 有 774 条，快照 2026-08-01。") == []


def test_number_grounded_misses_ungrounded_and_word_boundary():
    steps = [_step(result=_DB_RESULT)]
    # 无出处 → 记缺
    assert eal._number_grounded_misses(steps, "库内共 9999 条。") == ["9999"]
    # 词边界：47560 与 47 都不许命中 4756
    assert eal._number_grounded_misses(steps, "库内共 47560 条。") == ["47560"]
    assert eal._number_grounded_misses(steps, "库内共 47 条。") == ["47"]
    # 多个无出处数字按序去重列出
    assert eal._number_grounded_misses(steps, "甲 111 乙 222 丙 111。") == ["111", "222"]


def test_number_grounded_exemptions():
    steps = [_step(result=_DB_RESULT)]
    # ① ≤9 且紧邻「步」的步骤序号（「3 步」「3步」）
    assert eal._number_grounded_misses(steps, "共跑 3 步，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "共跑3步，库内4756条。") == []
    # ② 百分比 / 年份 / 日期头
    assert eal._number_grounded_misses(steps, "覆盖率 100%，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "截至 2026 年，库内 4756 条。") == []
    assert eal._number_grounded_misses(steps, "快照 2026-08，库内 4756 条。") == []
    # 豁免只管豁免项：同句里别的无出处数字照旧记缺
    assert eal._number_grounded_misses(steps, "共跑 3 步，库内 8888 条。") == ["8888"]


def test_number_grounded_score_case_gating():
    # steps 非空 + report 非空 → 参评
    d = _dims({"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
               "report_zh": "库内共 4756 条。"})
    assert d["number_grounded"]["ok"] is True
    d = _dims({"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
               "report_zh": "库内共 9999 条。"})
    assert d["number_grounded"]["ok"] is False
    assert "9999" in d["number_grounded"]["detail"]
    # 空 steps / 空 report → 不适用不参评（不进分母）
    assert "number_grounded" not in _dims(
        {"verb": "none", "steps": [], "report_zh": "共 123 条。"})
    assert "number_grounded" not in _dims(
        {"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)],
         "report_zh": ""})
    assert "number_grounded" not in _dims(
        {"verb": "curate.db_status", "steps": [_step(result=_DB_RESULT)]})


def test_number_grounded_in_new_strict_dims():
    # 计入严格分、剔除出旧维口径（照 v4/v5 新维做法进 _NEW_STRICT_DIMS）
    assert "number_grounded" in eal._NEW_STRICT_DIMS
    assert "number_grounded" not in eal._V3_DIMS


def test_number_grounded_non_dict_results_ignored():
    # result 非 dict/list（异常形状）不参与出处池，也不许炸
    steps = [_step(result="not-a-dict"), _step(result=_DB_RESULT)]
    assert eal._number_grounded_misses(steps, "库内 4756 条，其他 42 个。") == ["42"]


# ------------------------------------------------------------------ 失败聚类

def _rec(cid, dim, node, verb, extra_ok_check=False):
    checks = ([{"dim": "first", "ok": True, "detail": ""}] if extra_ok_check else [])
    checks.append({"dim": dim, "ok": False, "detail": ""})
    trace = ([{"node": node, "ok": False}] if node != "—" else [])
    return {"id": cid, "verb": verb, "checks": checks, "trace": trace}


def test_cluster_failures_groups_by_triple():
    fails = [
        _rec("x01", "must_steps", "execute", "curate.db_status"),
        _rec("x02", "must_steps", "execute", "curate.db_status",
             extra_ok_check=True),  # 首个 ok=false 才是首败维（ok=true 的不算）
        _rec("x03", "must_steps", "narrate", "curate.db_status"),  # 节点不同 → 另一簇
    ]
    cl = eal._cluster_failures(fails)
    assert len(cl) == 2
    big, small = cl  # 按簇大小降序
    assert big[0] == ("must_steps", "execute", "curate.db_status")
    assert big[1] == ["x01", "x02"]
    assert big[2] == "少跑了步"
    assert small[1] == ["x03"]


def test_cluster_failures_summary_templates_and_unknown_dim():
    templates = {"chain_complete": "链没走完", "must_steps": "少跑了步",
                 "report_contains": "汇报缺内容", "faithful": "汇报与实录矛盾",
                 "first": "首步判错"}
    fails = [_rec(f"c{i:02d}", dim, "execute", "v")
             for i, dim in enumerate(templates)]
    fails.append(_rec("c99", "weird_dim", "execute", "v"))
    cl = eal._cluster_failures(fails)
    summaries = {key[0]: summary for key, _ids, summary in cl}
    for dim, text in templates.items():
        assert summaries[dim] == text
    assert summaries["weird_dim"] == "weird_dim"  # 未知名维度原样显示


def test_cluster_failures_fallbacks():
    # trace 没有 ok=false 节点 → "—"；verb 缺失 → "—"；`_` 前缀信号维不算首败维
    rec = {"id": "x01", "verb": None,
           "checks": [{"dim": "_guard_events", "ok": True, "detail": ""},
                      {"dim": "faithful", "ok": False, "detail": ""}],
           "trace": [{"node": "understand", "ok": True}]}
    (key, ids, summary), = eal._cluster_failures([rec])
    assert key == ("faithful", "—", "—")
    assert ids == ["x01"]
    assert summary == "汇报与实录矛盾"
    # 空失败列表 → 空聚类（全过运行报告里该节为空）
    assert eal._cluster_failures([]) == []
