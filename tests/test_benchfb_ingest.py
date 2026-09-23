# -*- coding: utf-8 -*-
"""scripts/benchfb_ingest.py 的行为测试：校验、去重、合并、审阅报告、候选 JSONL。"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import benchfb_ingest as ing  # noqa: E402


def _pkg(records, install="ab12", schema="biodata-benchfb/1"):
    return {
        "schema": schema,
        "exported_at": "2026-08-13T11:00:00Z",
        "install_id": install,
        "app": {"cache_generation": "20260813-bf1", "ua": "UA", "lang": "zh-CN"},
        "records": records,
    }


def _rec(rid, q, kind="search", stars=None, useful=None, comment="", t=1000):
    rec = {
        "id": rid, "t": t, "kind": kind, "q": q, "src": "hero", "conv": "c1",
        "env": {"model": "deepseek-v4", "provider": "deepseek", "endpoint_host": "api.deepseek.com"},
        "route": {"route": "search", "via": "llm", "query": q, "echo_zh": "", "plan": {"trace": [{"node": "understand", "ok": True}]}},
        "route_ms": 800,
        "search": {"req": {"query": q, "model": "deepseek-v4"},
                   "res": {"results": [{"dataset_uid": f"uid-{i}", "dataset_name": f"数据{i}", "source": "10x"} for i in range(1, 4)],
                           "result_total": 3, "resolution_status": "results",
                           "search_trace": {"steps": [{"id": "llm_rerank", "status": "used", "duration_ms": 1200}]}},
                   "cached": False, "ms": 2300},
        "action": None, "err": "",
        "rating": None,
    }
    if stars or useful or comment:
        rec["rating"] = {
            "stars": stars, "useful_idx": useful or [], "comment": comment, "rated_at": 2000,
            "useful_resolved": [{"idx": i, "uid": f"uid-{i}", "name": f"数据{i}"} for i in (useful or [])],
        }
    return rec


def test_ingest_merges_dedupes_and_builds_candidates(tmp_path: Path) -> None:
    pkg_a = _pkg([_rec("r1", "人类肺癌", stars=4, useful=[1, 3], comment="第 3 条对"), _rec("r2", "小鼠脑", kind="none")])
    pkg_b = _pkg([_rec("r1", "人类肺癌", stars=4, useful=[1, 3]), _rec("r3", "空间转录组", kind="tool")], install="ab12")
    (tmp_path / "a.json").write_text(json.dumps(pkg_a, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "b.json").write_text(json.dumps(pkg_b, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    rc = ing.main([str(tmp_path), "--out", str(out)])
    assert rc == 0
    merged = json.loads((out / "merged.json").read_text(encoding="utf-8"))
    ids = [r["id"] for r in merged["records"]]
    assert ids == ["r1", "r2", "r3"], f"去重键 (install_id, id)，同机同号只留一份：{ids}"
    lines = (out / "candidates.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    c1 = json.loads(lines[0])
    assert c1["query"] == "人类肺癌"
    assert c1["system_topk_uids"] == ["uid-1", "uid-2", "uid-3"]
    assert c1["rating"]["stars"] == 4
    assert c1["rating"]["useful_uids"] == ["uid-1", "uid-3"]
    assert c1["rating"]["comment"] == "第 3 条对"
    assert c1["timing_ms"]["search"] == 2300
    assert c1["env"]["endpoint_host"] == "api.deepseek.com"
    c3 = json.loads(lines[2])
    assert c3["kind"] == "tool"
    review = (out / "review.html").read_text(encoding="utf-8")
    assert "人类肺癌" in review and "已评分 1 条" in review
    assert 'class="hit"' in review, "用户标注的有用条目必须高亮进审阅报告"
    assert "uid-1" in review and "understand" in review


def test_ingest_tolerates_new_rating_shape(tmp_path: Path) -> None:
    """ 起评分结构改版：{completion, reasons, useful_idx, useful_uids, comment, rated_at}，
    不再写 stars。新形状记录（无 stars）不得报错，候选行原样带出 completion/reasons/useful_uids；
    tid（其轮次 id）也随候选行带出。"""
    rec = _rec("rn1", "人类空间转录组", t=3000)
    rec["tid"] = "t-abc123-1"
    rec["rating"] = {
        "completion": "partial", "reasons": ["排序不对", "其他"],
        "useful_idx": [2], "useful_uids": ["uid-2"], "comment": "第二条才是想要的", "rated_at": 3100,
    }
    (tmp_path / "new.json").write_text(json.dumps(_pkg([rec]), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    rc = ing.main([str(tmp_path), "--out", str(out)])
    assert rc == 0
    c = json.loads((out / "candidates.jsonl").read_text(encoding="utf-8").strip())
    assert c["tid"] == "t-abc123-1"
    assert c["rating"]["stars"] is None, "新形状无 stars 不报错、不编造"
    assert c["rating"]["completion"] == "partial"
    assert c["rating"]["reasons"] == ["排序不对", "其他"]
    assert c["rating"]["useful_uids"] == ["uid-2"]
    assert c["rating"]["comment"] == "第二条才是想要的"
    review = (out / "review.html").read_text(encoding="utf-8")
    assert "部分完成" in review and "排序不对、其他" in review and "已评分 1 条" in review


def test_ingest_skips_bad_packages_without_crashing(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "wrong.json").write_text(json.dumps({"schema": "other/9", "records": []}), encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps(_pkg([_rec("r9", "好查询")]), ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "out"
    rc = ing.main([str(tmp_path), "--out", str(out)])
    assert rc == 0, "坏包记错跳过、好包照常入库，整批不炸"
    merged = json.loads((out / "merged.json").read_text(encoding="utf-8"))
    assert [r["id"] for r in merged["records"]] == ["r9"]


def test_ingest_empty_and_missing_inputs(tmp_path: Path) -> None:
    (tmp_path / "empty.json").write_text(json.dumps(_pkg([])), encoding="utf-8")
    out = tmp_path / "out"
    assert ing.main([str(tmp_path / "empty.json"), "--out", str(out)]) == 1, "零记录 → 退出码 1"
    assert ing.main([str(tmp_path / "不存在.json"), "--out", str(out)]) == 2, "找不到文件 → 退出码 2"


def test_candidate_tolerates_missing_segments() -> None:
    """error/none 类记录没有 search 段：候选行照样出，字段如实留空（不炸、不编造）。"""
    rec = {"id": "rx", "t": 1, "kind": "error", "q": "炸了", "err": "network",
           "route": None, "search": None, "action": None, "rating": None, "env": {}}
    c = ing._candidate(rec)
    assert c["system_topk_uids"] == [] and c["route"] == "" and c["error"] == "network"
