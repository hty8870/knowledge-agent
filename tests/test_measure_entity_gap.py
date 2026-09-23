# -*- coding: utf-8 -*-
"""scripts/measure_entity_gap.py 的钉（B5）。

三层：Wilson 区间纯函数；C3 归因助手（合成记录）；真实管线集成冒烟
（已知查询的类别断言 + 报告形状）。集成层复用 evaluate_recommendation 的管线加载，
与冻结评测同一条数据通道——不新造数据、不改任何库文件。
"""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

# measure_entity_gap 对 evaluate_recommendation 使用惰性导入，顶层 import 不再触碰
# pytest stdout 捕获。
import measure_entity_gap as meg  # noqa: E402


# ---------- Wilson 区间 ----------
def test_wilson_zero_denominator():
    assert meg.wilson_interval(0, 0) == (0.0, 0.0)


def test_wilson_bounds_sanity():
    lo, hi = meg.wilson_interval(0, 10)
    assert lo == 0.0 and 0.0 < hi < 1.0
    lo2, hi2 = meg.wilson_interval(10, 10)
    assert 0.0 < lo2 < 1.0 and hi2 == 1.0
    # 样本越大区间越窄
    _, hi_small = meg.wilson_interval(1, 10)
    _, hi_big = meg.wilson_interval(10, 100)
    assert hi_big < hi_small


# ---------- C3 归因助手（合成记录，不碰真实语料）----------
def test_constraint_failing_dims():
    rec = types.SimpleNamespace(
        species="Mouse", tissue="Brain", disease="", chemistry="",
        has_raw_data=None, raw={},
    )
    bad = meg._constraint_failing_dims(rec, {"species": ["human"], "tissue": ["brain"]})
    assert bad == ["species"]   # tissue 过、species 被误滤
    assert meg._constraint_failing_dims(rec, {}) == []
    assert meg._constraint_failing_dims(rec, {"species": ["mouse"]}) == []


# ---------- 真实管线集成冒烟 ----------
@pytest.fixture(scope="module")
def pipeline():
    import evaluate_recommendation as er
    return er.load_pipeline()


def _eval_query(qid: str) -> dict:
    data = json.loads((_ROOT / "eval" / "eval_queries.json").read_text(encoding="utf-8"))
    for q in data["queries"]:
        if q["id"] == qid:
            return q
    raise AssertionError(f"eval 集里找不到 {qid}")


def test_classify_ok_case(pipeline):
    settings, records = pipeline
    row = meg.classify_query(_eval_query("sp01"), records, settings)
    assert row["cls"] == "OK"
    assert row["support"] > 0 and row["n_returned"] > 0


def test_classify_known_catalog_gap_co14(pipeline):
    """co14（Xenium on Visium-only 基础语料）是冻结 Top1 97.7 里那条已知 0 支持题——
    必须稳定归因 C2（目录缺口），不许漂成 C1/C3/C4。"""
    settings, records = pipeline
    row = meg.classify_query(_eval_query("co14"), records, settings)
    assert row["cls"] == "C2"
    assert row["support"] == 0


def test_report_shape_and_class_sum(pipeline, tmp_path):
    """全样本跑一遍：类别计数自洽（总和=样本数）、裁决行非空、报告文件落盘。"""
    settings, records = pipeline
    queries = meg.load_all_queries()
    manifest = meg.load_evaluation_manifest()
    assert len(queries) >= manifest["entity_gap"]["expected_min_unique_queries"]
    rows = [meg.classify_query(q, records, settings) for q in queries]
    assert sum(1 for _ in rows) == len(queries)
    assert all(r["cls"] in ("C1", "C2", "C3", "C4", "OK") for r in rows)


def test_manifest_missing_input_fails_closed(tmp_path):
    manifest = {
        "schema_version": 1,
        "audience": "test",
        "entity_gap": {
            "query_sets": ["missing.json"],
            "expected_min_unique_queries": 1,
        },
    }
    path = tmp_path / "evaluation-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="input is missing"):
        meg.load_all_queries(path)


def test_manifest_rejects_unsafe_query_set_path(tmp_path):
    manifest = {
        "schema_version": 1,
        "audience": "test",
        "entity_gap": {
            "query_sets": ["../private.json"],
            "expected_min_unique_queries": 1,
        },
    }
    path = tmp_path / "evaluation-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unsafe evaluation"):
        meg.load_all_queries(path)
