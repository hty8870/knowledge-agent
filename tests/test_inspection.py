# -*- coding: utf-8 -*-
"""活台账只读层 inspection.py 单测：派生逻辑（problem/reason）+ 真数据集成 + 优雅降级。

契约要点被钉住：unknown≠problem（「不结论」不算失效）；数据缺失 → 全部 None/空（永不崩）。
"""
from dataset_recommender.corpus import inspection as I


# ---- _derive 纯逻辑（数据无关）----
def test_derive_dead_is_problem():
    s = I._derive("https://x/f", {"r": "dead", "h": 404, "s": "unknown", "srv": None, "v": "2026-07-10"})
    assert s["problem"] is True and "HTTP 404" in s["problem_reason"]


def test_derive_size_mismatch_is_problem():
    s = I._derive("https://x/f", {"r": "ok", "h": 206, "s": "mismatch", "srv": 123, "v": "2026-07-10"})
    assert s["problem"] is True and "大小" in s["problem_reason"]


def test_derive_unknown_is_not_problem():
    """核心契约：reachable/size 都 unknown（网络错误/未测）**不算** problem。"""
    s = I._derive("https://x/f", {"r": "unknown", "h": None, "s": "unknown", "srv": None, "v": "unknown"})
    assert s["problem"] is False and s["problem_reason"] is None


def test_derive_ok_match_clean():
    s = I._derive("https://x/f", {"r": "ok", "h": 206, "s": "match", "srv": 999, "v": "2026-07-10"})
    assert s["problem"] is False
    assert s["integrity"] == "unknown" and s["load"] == "unknown"   # 恒 unknown（诚实边界）


# ---- 真数据集成（活台账已播种）----
def test_available_and_snapshot():
    assert I.is_available() is True
    info = I.snapshot_info()
    assert info and info["snapshot_id"] and info["totals"]["files"] > 0


def test_real_problem_file_flagged():
    import json
    from pathlib import Path
    import pytest
    cur = json.loads(Path(I._DATA_PATH).read_text(encoding="utf-8"))
    entry = next(((u, r) for u, r in cur["by_uid"].items() if r.get("np", 0) > 0), None)
    if not entry:
        pytest.skip("活台账无问题数据集")
    uid, r = entry
    fs = I.file_status(uid)
    probs = [s for s in fs.values() if s["problem"]]
    assert len(probs) == r["np"]
    summ = I.dataset_summary(uid)
    assert summ["n_problem"] == r["np"] and summ["provisioning_ok"] is False


# ---- 优雅降级：数据缺失 → 全 None/空（永不崩）----
def test_degrades_when_data_missing(monkeypatch):
    monkeypatch.setattr(I, "_DATA_PATH", "Z:/definitely/not/here/current.json")
    I._load.cache_clear()
    try:
        assert I.is_available() is False
        assert I.status_for("any", "https://x") is None
        assert I.dataset_summary("any") is None
        assert I.snapshot_info() is None
        assert I.file_status("any") == {}
    finally:
        I._load.cache_clear()   # 恢复：后续测试重新读真实 current.json


# ---- 环境变量运行时生效 ----

def test_env_override_takes_effect_at_runtime(tmp_path, monkeypatch):
    """BIODATA_INSPECTION 运行时变更必须生效——路径每次加载现读、含进缓存键，
    不再 import 期固化（此前巡检重建旗标/改 env 后长驻进程用旧值）。"""
    import json as _json

    p = tmp_path / "current.json"
    p.write_text(_json.dumps({"by_uid": {"u1": {"files": {}}}}), encoding="utf-8")
    monkeypatch.setenv("BIODATA_INSPECTION", str(p))
    # 不清缓存：路径变了 → 缓存键自然不同 → 直接读到新文件
    assert I.is_available() is True


# ---- 读盘失败不再负向缓存 ----

def test_corrupt_file_not_cached_and_recovers(tmp_path, monkeypatch):
    """D6：坏 JSON → 降级 is_available()=False 但不入缓存；同进程修复后无需 cache_clear 即恢复。"""
    import json as _json

    p = tmp_path / "current.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("BIODATA_INSPECTION", str(p))
    I._load.cache_clear()
    try:
        assert I.is_available() is False   # 降级：不抛
        p.write_text(_json.dumps({"by_uid": {"u1": {"files": {}}}}), encoding="utf-8")
        assert I.is_available() is True    # 同路径、不清缓存：直接读到修复后的台账
    finally:
        I._load.cache_clear()
