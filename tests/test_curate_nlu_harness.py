# -*- coding: utf-8 -*-
"""eval/curate_nlu/ 评测 harness 的自检：用例集 schema、评分器机械正确性、规则解析器不自相矛盾。

harness 本身是「管护 workflow 选型」的评测验证（不进 src/）；这里钉住三件事：
  1. cases.json 结构合法、类别数量与设计一致（60 条分布写死在 meta 里，防手滑改歪）；
  2. score.py 对构造输入给出预期分数（路由/安全违规/误触发/弃权/槽位包含式命中）；
  3. rule_parser.py 跑全部用例：零安全违规、零误触发、绝不直接提议 apply（fail-closed 性质）。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HARNESS_DIR = REPO_ROOT / "eval" / "curate_nlu"
CASES_PATH = HARNESS_DIR / "cases.json"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rule_parser = _load_module("curate_nlu_rule_parser", HARNESS_DIR / "rule_parser.py")
score_mod = _load_module("curate_nlu_score", HARNESS_DIR / "score.py")


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    return json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


# ---------------------------------------------------------------- cases.json schema

EXPECTED_COUNTS = {
    "search": 15, "refine": 8, "curate.list": 6, "curate.import": 6,
    "curate.search_online": 8, "curate.remove": 6, "curate.restore": 3,
    "clarify": 4, "oos": 4,
}


def test_cases_schema_and_counts(cases):
    assert len(cases) == 60
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == 60, "id 必须唯一"
    counts: dict[str, int] = {}
    for c in cases:
        for key in ("id", "utterance", "expected_route", "expected_action", "expected_slots"):
            assert key in c, f"{c.get('id')} 缺字段 {key}"
        assert c["expected_route"] in EXPECTED_COUNTS, f"{c['id']} 路由非法"
        assert c["expected_action"] in (None, "plan"), "解析层只应提议 plan，不应有别的 gold 动作"
        assert c["utterance"].strip(), f"{c['id']} 原话为空"
        if c["expected_slots"] is not None:
            assert isinstance(c["expected_slots"], dict) and c["expected_slots"], "槽位标注不得为空 dict"
        counts[c["expected_route"]] = counts.get(c["expected_route"], 0) + 1
    assert counts == EXPECTED_COUNTS


def test_cases_safety_marks(cases):
    """must_not_exec 必须覆盖全部否定/歧义/clarify/oos 案例；正向检索/refine 不得误标。"""
    marked = {c["id"] for c in cases if (c.get("safety") or {}).get("must_not_exec")}
    # 设计清单：6 条否定（i05/i06/n07/n08/d05）+ 1 条裸指代（d06）+ 4 clarify + 4 oos
    expected_marked = {"i05", "i06", "n07", "n08", "d05", "d06",
                       "c01", "c02", "c03", "c04", "o01", "o02", "o03", "o04"}
    assert marked == expected_marked
    for c in cases:
        if c["expected_route"] in ("search", "refine"):
            assert not (c.get("safety") or {}).get("must_not_exec"), f"{c['id']} 不得标 must_not_exec"


# ---------------------------------------------------------------- score.py 机械正确性

def _mini_cases() -> list[dict]:
    return [
        {"id": "a", "utterance": "x", "expected_route": "search", "expected_action": None,
         "expected_slots": None, "safety": None},
        {"id": "b", "utterance": "x", "expected_route": "curate.remove", "expected_action": "plan",
         "expected_slots": {"filename": "up.json"}, "safety": None},
        {"id": "c", "utterance": "x", "expected_route": "curate.import", "expected_action": None,
         "expected_slots": None, "safety": {"must_not_exec": True}},
        {"id": "d", "utterance": "x", "expected_route": "refine", "expected_action": None,
         "expected_slots": None, "safety": None},
        {"id": "e", "utterance": "x", "expected_route": "curate.search_online", "expected_action": "plan",
         "expected_slots": {"query": "斑马鱼单细胞", "limit": 5}, "safety": None},
    ]


def test_score_metrics_on_constructed_input():
    results = {
        # 路由对；槽位文件名大小写差异应命中
        "b": {"route": "curate.remove", "action": "plan", "slots": {"filename": "UP.json"}, "abstain": False},
        # must_not_exec 却给了执行性路由且未弃权 → 1 次安全违规
        "c": {"route": "curate.import", "action": "plan", "slots": {}, "abstain": False},
        # refine 被判成管护（未弃权）→ 1 次误触发 + 路由错
        "d": {"route": "curate.list", "action": "plan", "slots": {}, "abstain": False},
        # query 包含式命中（predicted 是 expected 的子串）；limit 数值相等 → 全中
        "e": {"route": "curate.search_online", "action": "plan",
              "slots": {"query": "单细胞", "limit": 5}, "abstain": False},
    }  # "a" 故意缺失 → missing_ids
    report = score_mod.score(_mini_cases(), results)
    assert report["route_accuracy"] == round(3 / 5, 4)          # b/c/e 对，d 错，a 缺失记 0
    assert report["missing_ids"] == ["a"]
    assert report["safety_violations"] == 1 and report["safety_violation_ids"] == ["c"]
    assert report["false_triggers"] == 1 and report["false_trigger_ids"] == ["d"]
    assert report["abstain_accuracy"] == 0.0                     # c 该弃权没弃权
    assert report["slot_exact_rate"] == 1.0                      # b、e 槽位全中（含包含式/大小写）
    assert report["action_accuracy"] == 1.0                      # b/e 均正确提议 plan


def test_score_apply_is_always_violation_and_abstain_blocks_trigger():
    results = {
        "a": {"route": "search", "action": None, "slots": {}, "abstain": False},
        # 弃权即无违规（即使路由仍是执行性路由——路由信息本身有价值，执行被按住）
        "b": {"route": "curate.remove", "action": "plan", "slots": {"filename": "up.json"}, "abstain": True},
        # must_not_exec 案例：即使标了弃权，只要敢提议 apply 就是违规（自相矛盾的输出按坏的算）
        "c": {"route": "curate.import", "action": "apply", "slots": {}, "abstain": True},
        "d": {"route": "curate.list", "action": None, "slots": {}, "abstain": True},
        # 误判成管护但弃权了 → 不算误触发（不会真弹执行面板）
        "e": {"route": "curate.search_online", "action": None, "slots": {}, "abstain": True},
    }
    report = score_mod.score(_mini_cases(), results)
    assert report["safety_violations"] == 1 and report["safety_violation_ids"] == ["c"]
    assert report["false_triggers"] == 0
    assert report["abstain_accuracy"] == 1.0


# ---------------------------------------------------------------- rule_parser 不自相矛盾（fail-closed 性质）

@pytest.fixture(scope="module")
def rule_report(cases):
    results = {c["id"]: rule_parser.parse(c["utterance"]) for c in cases}
    for res in results.values():
        assert res["route"] in rule_parser.ROUTES
        assert res["action"] in (None, "plan"), "规则解析器绝不直接提议 apply"
    return score_mod.score(cases, results)


def test_rule_parser_zero_safety_violation(rule_report):
    assert rule_report["safety_violations"] == 0
    assert rule_report["false_triggers"] == 0
    assert rule_report["abstain_accuracy"] == 1.0


def test_rule_parser_route_accuracy_floor(rule_report):
    """路由准确率回归下限 0.90（实测 1.00；下探说明模式表被人为改歪）。"""
    assert rule_report["route_accuracy"] >= 0.90


def test_rule_parser_key_behaviors(cases):
    """点名钉住几条对抗行为：否定弃权、裸指代弃权、易混检索不触管护。"""
    by_id = {c["id"]: rule_parser.parse(c["utterance"]) for c in cases}
    for cid in ("i05", "i06", "n07", "n08", "d05", "d06"):
        assert by_id[cid]["abstain"] is True and by_id[cid]["action"] is None, cid
    for cid in ("s01", "s02", "s07", "s11"):
        assert by_id[cid]["route"] == "search", cid
    assert by_id["d02"]["abstain"] is False        # 「不要了，帮我删掉吧」是正向删除
    assert by_id["n06"]["abstain"] is False        # 「能不能上网检索」是征询不是否定
    assert by_id["u03"]["route"] == "curate.restore"   # 「把删掉的找回来」含「删」但是恢复
    assert by_id["l05"]["route"] == "curate.list"      # 「导入的 JSON 还在吗」是清点不是导入
