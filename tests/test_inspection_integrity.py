# -*- coding: utf-8 -*-
"""inspection.py 读取层 integrity 维 additive 扩展的派生逻辑测试。

钉住的契约：
  * 向量带 `"i"`（provision 回写落盘）→ integrity 透出 verified/mismatch，不再恒 unknown；
  * integrity==mismatch 算 problem 并给出中文 reason（与 dead/size_mismatch 同档披露）；
  * 旧向量（无 `"i"` 键）行为逐位不变：integrity=unknown、problem 只看 dead/size_mismatch；
  * `"i": "unknown"`（显式未实测）不算 problem——「不结论」≠「失效」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import inspection as I  # noqa: E402

URL = "https://cf.10xgenomics.com/x/f.h5"


# ---- _derive 纯逻辑 ----

def test_derive_integrity_verified_not_problem():
    s = I._derive(URL, {"r": "ok", "h": 200, "s": "match", "srv": 10, "v": "2026-08-01",
                        "i": "verified"})
    assert s["integrity"] == "verified" and s["problem"] is False
    assert s["problem_reason"] is None


def test_derive_integrity_mismatch_is_problem_with_reason():
    s = I._derive(URL, {"r": "ok", "h": 200, "s": "match", "srv": 10, "v": "2026-08-01",
                        "i": "mismatch"})
    assert s["integrity"] == "mismatch" and s["problem"] is True
    assert "md5" in s["problem_reason"] and "2026-08-01" in s["problem_reason"]


def test_derive_integrity_unknown_explicit_not_problem():
    s = I._derive(URL, {"r": "ok", "h": 200, "s": "match", "srv": 10, "v": "2026-08-01",
                        "i": "unknown"})
    assert s["integrity"] == "unknown" and s["problem"] is False


def test_derive_legacy_vector_without_i_unchanged():
    """旧格式向量（无 i 键）：integrity 仍 unknown，problem 仍只看 dead/size_mismatch。"""
    s = I._derive(URL, {"r": "ok", "h": 206, "s": "match", "srv": 9, "v": "2026-07-10"})
    assert s["integrity"] == "unknown" and s["load"] == "unknown" and s["problem"] is False
    s2 = I._derive(URL, {"r": "dead", "h": 404, "s": "unknown", "srv": None, "v": "2026-07-10"})
    assert s2["problem"] is True and "HTTP 404" in s2["problem_reason"]


def test_reason_priority_dead_beats_integrity():
    """reason 单点派生：dead 优先于 integrity mismatch（一个文件一句最要紧的话）。"""
    s = I._derive(URL, {"r": "dead", "h": 404, "s": "unknown", "srv": None,
                        "v": "2026-08-01", "i": "mismatch"})
    assert "HTTP 404" in s["problem_reason"]


# ---- 经 current.json 的读取路径（status_for / file_status / dataset_summary）----

def _write_current(tmp_path: Path) -> Path:
    cur = {
        "schema": "biodata-inspection/v0", "snapshot_id": "abc123",
        "snapshot_date": "2026-08-01T00:00:00Z", "source": "provision",
        "totals": {"files": 2, "problem": 1, "integrity_verified": 1, "integrity_mismatch": 1},
        "by_uid": {
            "10x:A": {"lv": "2026-08-01", "nf": 2, "np": 1, "f": {
                URL: {"r": "ok", "h": 200, "s": "match", "srv": 100,
                      "v": "2026-08-01", "i": "verified"},
                "https://cf.10xgenomics.com/x/g.h5": {"r": "ok", "h": 200, "s": "match",
                                                      "srv": 200, "v": "2026-08-01",
                                                      "i": "mismatch"},
            }},
            # 旧向量（无 i）混在同一帧里也得照常工作
            "10x:B": {"lv": "2026-07-10", "nf": 1, "np": 0, "f": {
                "https://cf.10xgenomics.com/y/h.h5": {"r": "ok", "h": 206, "s": "match",
                                                      "srv": 50, "v": "2026-07-10"},
            }},
        },
    }
    p = tmp_path / "current.json"
    p.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return p


def test_status_for_surfaces_integrity(tmp_path, monkeypatch):
    monkeypatch.setattr(I, "_DATA_PATH", str(_write_current(tmp_path)))
    I._load.cache_clear()
    try:
        ok = I.status_for("10x:A", URL)
        assert ok["integrity"] == "verified" and ok["problem"] is False
        bad = I.status_for("10x:A", "https://cf.10xgenomics.com/x/g.h5")
        assert bad["integrity"] == "mismatch" and bad["problem"] is True
        legacy = I.status_for("10x:B", "https://cf.10xgenomics.com/y/h.h5")
        assert legacy["integrity"] == "unknown" and legacy["problem"] is False
        # 摘要计数与逐文件派生一致（与 test_inspection.py 同一条不变量）
        fs = I.file_status("10x:A")
        assert sum(1 for s in fs.values() if s["problem"]) == 1
        summ = I.dataset_summary("10x:A")
        assert summ["n_problem"] == 1 and summ["provisioning_ok"] is False
    finally:
        I._load.cache_clear()
