# -*- coding: utf-8 -*-
"""report_provisioning 报告扩展（integrity/load 维）确定性测试。

全程禁网：构造假台账帧写到 tmp 目录，monkeypatch 脚本的 `_CURRENT`/`_EVAL` 接缝后跑 build()。
覆盖：含/不含 "i"/"l" 槽两维计数与覆盖率、verified/loaded 率分母=已实测数、unknown 不算 problem、
problem 派生口径含 integrity/load 两维（且按向量现算、不盲信物化 totals/np）、JSON 与 MD 同步、
schema additive（旧键保留）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

import report_provisioning as RP  # noqa: E402


def _vec(r="ok", s="match", i=None, l=None):  # noqa: E741
    v = {"r": r, "h": 206 if r == "ok" else 404, "s": s, "srv": 100, "v": "2026-08-01"}
    if i is not None:
        v["i"] = i
    if l is not None:
        v["l"] = l
    return v


def _run(tmp_path, monkeypatch, by_uid, totals):
    cur = {"schema": "biodata-inspection/v0", "snapshot_id": "t" * 16,
           "snapshot_date": "2026-08-01T00:00:00Z", "source": "test",
           "totals": totals, "by_uid": by_uid}
    ledger = tmp_path / "current.json"
    ledger.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    eval_dir = tmp_path / "eval"
    monkeypatch.setattr(RP, "_CURRENT", ledger)
    monkeypatch.setattr(RP, "_EVAL", eval_dir)
    assert RP.build() == 0
    report = json.loads((eval_dir / "provisioning_report.json").read_text(encoding="utf-8"))
    md = (eval_dir / "provisioning_report.md").read_text(encoding="utf-8")
    return report, md


def _totals(n_files, reach_ok, size_match, problem):
    return {"files": n_files, "reach_ok": reach_ok, "reach_dead": n_files - reach_ok,
            "reach_unknown": 0, "size_match": size_match,
            "size_mismatch": n_files - size_match, "size_unknown": 0,
            "problem": problem, "unprobed": 0}


def test_missing_ledger_returns_2(tmp_path, monkeypatch):
    monkeypatch.setattr(RP, "_CURRENT", tmp_path / "nope.json")
    monkeypatch.setattr(RP, "_EVAL", tmp_path / "eval")
    assert RP.build() == 2
    assert not (tmp_path / "eval").exists()


def test_seed_frames_without_i_l_all_unknown(tmp_path, monkeypatch):
    """旧帧（无 i/l 槽）：两维全 unknown、实测 0、覆盖率 0%；problem 只由 reachable/size 派生。"""
    by_uid = {
        "ds-a": {"lv": "d", "nf": 2, "np": 1,
                 "f": {"u1": _vec(), "u2": _vec(s="mismatch")}},
        "ds-b": {"lv": "d", "nf": 2, "np": 0,
                 "f": {"u3": _vec(), "u4": _vec()}},
    }
    report, md = _run(tmp_path, monkeypatch, by_uid, _totals(4, 4, 3, 1))

    fl = report["file_level"]
    assert fl["integrity"] == {"verified": 0, "mismatch": 0, "unknown": 4,
                               "measured": 0, "verified_rate": 0.0, "coverage": 0.0}
    assert fl["load"] == {"loaded": 0, "failed": 0, "unknown": 4,
                          "measured": 0, "loaded_rate": 0.0, "coverage": 0.0}
    # 旧占位键保留且与未实测语义一致（additive 兼容）
    assert fl["integrity_verified_rate"] == 0.0
    assert fl["load_verified_rate"] == 0.0
    # problem 只来自 size-mismatch；unknown 不计
    assert fl["problem"] == 1 and fl["problem_rate"] == 25.0
    assert report["dataset_level"]["clean"] == 1
    assert report["dataset_level"]["provisioning_success_rate"] == 50.0
    assert [d["uid"] for d in report["degraded_datasets"]] == ["ds-a"]
    assert "实测覆盖率 0.0%" in md and "unknown 4" in md


def test_i_l_slots_counted_and_rates_over_measured(tmp_path, monkeypatch):
    """含 i/l 槽：计数、verified/loaded 率（分母=已实测数）、覆盖率（分母=全部文件）。"""
    by_uid = {
        "ds-a": {"lv": "d", "nf": 4, "np": 0,
                 "f": {"u1": _vec(i="verified", l="loaded"),
                       "u2": _vec(i="verified", l="loaded"),
                       "u3": _vec(i="mismatch", l="failed"),
                       "u4": _vec()}},          # 未实测 → unknown
    }
    report, md = _run(tmp_path, monkeypatch, by_uid, _totals(4, 4, 4, 0))

    fl = report["file_level"]
    ib, lb = fl["integrity"], fl["load"]
    assert (ib["verified"], ib["mismatch"], ib["unknown"]) == (2, 1, 1)
    assert ib["measured"] == 3 and ib["verified_rate"] == 66.67   # 2/3，分母=实测数
    assert ib["coverage"] == 75.0                                  # 3/4
    assert (lb["loaded"], lb["failed"], lb["unknown"]) == (2, 1, 1)
    assert lb["measured"] == 3 and lb["loaded_rate"] == 66.67 and lb["coverage"] == 75.0
    # 兼容旧键镜像真实实测率
    assert fl["integrity_verified_rate"] == 66.67
    assert fl["load_verified_rate"] == 66.67
    assert "66.67%（实测 3 个）" in md and "实测覆盖率 75.0%" in md


def test_problem_rule_includes_integrity_and_load(tmp_path, monkeypatch):
    """i=mismatch 与 l=failed 计入 problem（即便 reachable/size 全绿），unknown 不算；
    且报告按向量现算，不盲信物化的 totals.problem / np。"""
    by_uid = {
        # 物化 np 故意填 0（陈旧）：报告须按向量派生出 1 个问题文件
        "ds-a": {"lv": "d", "nf": 2, "np": 0,
                 "f": {"u1": _vec(i="mismatch"), "u2": _vec()}},
        "ds-b": {"lv": "d", "nf": 2, "np": 0,
                 "f": {"u3": _vec(l="failed"), "u4": _vec(i="verified", l="loaded")}},
        "ds-c": {"lv": "d", "nf": 1, "np": 0,
                 "f": {"u5": _vec()}},            # 全 unknown → 非 problem
    }
    # 物化 totals.problem 也故意为 0，验证报告不盲信
    report, md = _run(tmp_path, monkeypatch, by_uid, _totals(5, 5, 5, 0))

    fl, dl = report["file_level"], report["dataset_level"]
    assert fl["problem"] == 2 and fl["problem_rate"] == 40.0
    assert dl["clean"] == 1 and dl["degraded"] == 2
    assert dl["provisioning_success_rate"] == 33.33   # 仅 ds-c 全绿
    assert {d["uid"] for d in report["degraded_datasets"]} == {"ds-a", "ds-b"}
    assert "integrity-mismatch 或 load-failed" in md
    assert fl["problem_rule"].startswith("reachable==dead")


def test_md_and_json_headline_consistent(tmp_path, monkeypatch):
    """MD 与 JSON 同步：头号指标、口径说明行、覆盖率行两边一致。"""
    by_uid = {"ds-a": {"lv": "d", "nf": 1, "np": 0, "f": {"u1": _vec(i="verified", l="loaded")}},
              "ds-b": {"lv": "d", "nf": 1, "np": 0, "f": {"u2": _vec()}}}
    report, md = _run(tmp_path, monkeypatch, by_uid, _totals(2, 2, 2, 0))

    dl = report["dataset_level"]
    assert f"**{dl['provisioning_success_rate']}%**" in md
    assert "口径：problem 判定含 integrity/load 两维" in md
    assert "integrity 实测覆盖 50.0%（1/2）、load 实测覆盖 50.0%（1/2）" in md
    assert "未测」不是「失败" in md
    # schema 版本键未 bump（additive）
    assert report["schema"] == "biodata-provisioning/v0"
