# -*- coding: utf-8 -*-
"""把 `scripts/evaluate_curate_agent.py` 的 execution-based 金标（eval/eval_curate_agent.json）
折进 pytest——管护链路（action_plan 护栏 → run_curate_action 两步 → 沙盒终态）的回归门
随全量测试一起跑，不再只是手工脚本。断言真源在脚本的 run_case（同一函数，不抄第二份）。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import evaluate_curate_agent as eca  # noqa: E402

_GOLD = Path(__file__).resolve().parents[1] / "eval" / "eval_curate_agent.json"
_CASES = {c["id"]: c for c in json.loads(_GOLD.read_text(encoding="utf-8"))["cases"]}


@pytest.mark.parametrize("case_id", sorted(_CASES))
def test_curate_execution_case(case_id):
    row = eca.run_case(_CASES[case_id])
    assert row["ok"], "；".join(row["failures"])
