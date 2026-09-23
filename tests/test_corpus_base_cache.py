# -*- coding: utf-8 -*-
"""（验证）：corpus._load_base 内容指纹键控缓存的回归门。

病形：基础语料每次调用现算 normalize_records——实测一次分流恰算 2 遍
（1548 条 = 2×774，median 89.5ms）。修复 = （路径 + 目录内容指纹）键控 lru_cache。

缓存引入的新风险必须钉死：
- 同一目录二次装载只归一化一次（调用计数钉，不用墙钟——时序断言必抖）；
- 文件改写（含**同尺寸**改写）/增/删 → 指纹变 → 自动重载（「改动即时可见」不靠每次现算）；
- invalidate_base_cache() 受控失效入口真失效；
- 每次调用拿到**新建 list**（列表级改写不跨调用污染）；
- BASE_SOURCE 就地打标在缓存下仍逐位正确。
"""
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import corpus as C  # noqa: E402


def _write_base(data_dir: Path, uids: list, name: str = "base.json") -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    payload = [{"dataset_uid": u, "dataset_name": f"数据集{u}", "species": "Human"} for u in uids]
    (data_dir / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _count_normalize(monkeypatch):
    calls = {"n": 0}
    real = C.normalize_records

    def counting(records):
        calls["n"] += 1
        return real(records)

    monkeypatch.setattr(C, "normalize_records", counting)
    return calls


def test_second_load_served_from_cache(monkeypatch, tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1", "a2"])
    calls = _count_normalize(monkeypatch)
    first = C._load_base(data_dir)
    second = C._load_base(data_dir)
    assert calls["n"] == 1, "同目录同内容二次装载不得重复归一化"
    assert [r.raw["dataset_uid"] for r in first] == ["a1", "a2"]
    assert [r.raw["dataset_uid"] for r in second] == ["a1", "a2"]


def test_file_rewrite_auto_reloads(monkeypatch, tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    calls = _count_normalize(monkeypatch)
    assert len(C._load_base(data_dir)) == 1
    _write_base(data_dir, ["a1", "b2", "c3"])   # 尺寸变 → 指纹变
    loaded = C._load_base(data_dir)
    assert calls["n"] == 2
    assert [r.raw["dataset_uid"] for r in loaded] == ["a1", "b2", "c3"]


def test_same_size_rewrite_auto_reloads(monkeypatch, tmp_path):
    """同尺寸改写（指纹只能靠 mtime_ns 那一维）也必须重载——这是「不靠每次现算」的底气。
    两次写之间必须跨过一个 mtime 刻度（验证本机 NTFS 惰性时间戳：紧挨写 47/50 次
    mtime_ns 逐位相同——毫秒内的同尺寸连写共享指纹，是 _base_fingerprint docstring 写明的边界）。"""
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    calls = _count_normalize(monkeypatch)
    assert C._load_base(data_dir)[0].raw["dataset_uid"] == "a1"
    time.sleep(0.02)   # 跨过 Windows 默认计时器刻度（~15.6ms）
    _write_base(data_dir, ["b9"])   # 「a1」→「b9」：payload 字节数逐位相同
    loaded = C._load_base(data_dir)
    assert calls["n"] == 2, "同尺寸改写（跨 mtime 刻度）未触发重载"
    assert loaded[0].raw["dataset_uid"] == "b9"


def test_file_add_and_delete_auto_reloads(monkeypatch, tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    calls = _count_normalize(monkeypatch)
    assert len(C._load_base(data_dir)) == 1
    _write_base(data_dir, ["b2"], name="extra.json")   # 增一个文件
    assert len(C._load_base(data_dir)) == 2
    (data_dir / "extra.json").unlink()                 # 再删掉
    assert len(C._load_base(data_dir)) == 1
    # 删掉后指纹回到第一代（仍在 maxsize=4 缓存内）→ 合法缓存命中，归一化不重复跑；
    # 内容正确性由上面 len==1 钉住。这里钉的是「增/删都改变了装载结果」，不是每次必重算。
    assert calls["n"] == 2


def test_invalidate_base_cache_forces_reload(monkeypatch, tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    calls = _count_normalize(monkeypatch)
    C._load_base(data_dir)
    C.invalidate_base_cache()
    C._load_base(data_dir)
    assert calls["n"] == 2, "受控失效入口必须真的失效"


def test_returned_list_is_a_fresh_copy(tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    first = C._load_base(data_dir)
    first.append(first[0])
    assert len(C._load_base(data_dir)) == 1, "调用方对返回列表的改写不得污染缓存"


def test_base_source_tagging_survives_caching(tmp_path):
    data_dir = tmp_path / "database" / "base"
    _write_base(data_dir, ["a1"])
    for _ in range(2):
        loaded = C._load_base(data_dir)
        assert all(r.raw.get("source") == C.BASE_SOURCE for r in loaded), "打标必须在缓存内容里"
