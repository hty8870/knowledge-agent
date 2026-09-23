"""用户上传 / 手动放入外部库的可扩展性 + 确定性守卫。

核心不变量：
- 上传只进 `database/external/` 外部库 → 成为一个**可勾选来源**；`sources=None`（官方评测/CLI）**绝不并入** → 767 基准不变。
- 逐条带 `source` → 计数口径正确（不被误判为 10x Genomics）。
- 写入新文件后 `invalidate_external_cache()` → 即时可见。
- 某个文件损坏 → 被跳过、不抛异常、不连累其它库（手动放入坏文件的健壮性）。
"""
import json
import re
from pathlib import Path

from dataset_recommender.corpus.corpus import (
    BASE_SOURCE,
    available_sources,
    invalidate_external_cache,
    load_full_corpus,
    load_normalized_corpus,
    source_of,
)
from dataset_recommender.corpus.data_loader import load_raw_records
from dataset_recommender.app.webapp import _new_upload_name


def _write(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")


def _make_base(tmp_path: Path) -> Path:
    d = tmp_path / "database" / "base"
    d.mkdir(parents=True, exist_ok=True)
    _write(
        d / "base.json",
        [
            {"dataset_name": "Base A", "species": "Human", "url": "https://e/a"},
            {"dataset_name": "Base B", "species": "Mouse", "url": "https://e/b"},
        ],
    )
    return d


def test_web_upload_names_cannot_shadow_public_snapshot_names():
    name = _new_upload_name("arrayexpress.json", "20260714_010203_000004")
    assert name == "upload_20260714_010203_000004_arrayexpress.json"
    assert re.fullmatch(r"upload_[0-9_]+_arrayexpress\.json", name)
    assert name != "arrayexpress.json"


def test_uploaded_source_is_selectable_and_not_in_base(tmp_path):
    """上传/放入 database/external 的库成为可勾选来源；sources=None 绝不并入 → 基准恒定。"""
    base = _make_base(tmp_path)
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    # 模拟 /api/upload 落盘的规范化包裹（逐条已打 source）
    _write(
        ext / "mylab.json",
        {"source": "张三实验室", "record_count": 1,
         "records": [{"dataset_name": "My Set", "species": "Human", "source": "张三实验室"}]},
    )
    invalidate_external_cache()

    none = load_normalized_corpus(base, tmp_path, None)
    assert [r.dataset_name for r in none] == ["Base A", "Base B"]          # 上传不进 base-only 路径
    assert all(source_of(r) == BASE_SOURCE for r in none)

    only = load_normalized_corpus(base, tmp_path, ["张三实验室"])
    assert [r.dataset_name for r in only] == ["My Set"]
    assert all(source_of(r) == "张三实验室" for r in only)

    srcs = {s["value"]: s["count"] for s in available_sources(base, tmp_path)}
    assert srcs.get("张三实验室") == 1
    assert srcs[BASE_SOURCE] == 2
    assert len(load_full_corpus(base, tmp_path)) == 3


def test_invalidate_external_cache_makes_new_upload_visible(tmp_path):
    """写入新文件后 invalidate_external_cache → 重新读盘即可见（即时可检索）。"""
    base = _make_base(tmp_path)
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    assert len(load_full_corpus(base, tmp_path)) == 2   # 先加载：外部为空被缓存
    _write(ext / "late.json",
           {"source": "迟到库", "records": [{"dataset_name": "Late", "source": "迟到库"}]})
    invalidate_external_cache()
    full = load_full_corpus(base, tmp_path)
    assert any(source_of(r) == "迟到库" for r in full)


def test_external_cache_stale_read_never_reinstalls(tmp_path, monkeypatch):
    """慢读取横跨 invalidate：迟到的旧结果返回给当时的调用者可以，但绝不得回写缓存
    （lru_cache 陈旧回装竞态的回归测试：旧值回写会让上传后的新数据持续不可见）。"""
    import threading
    from dataset_recommender.corpus import corpus as C
    key = (str(tmp_path / "shipped"), str(tmp_path / "ext"))
    invalidate_external_cache()
    started = threading.Event()
    finish = threading.Event()
    calls = {"n": 0}

    def _slow(_shipped, _user):
        calls["n"] += 1
        if calls["n"] == 1:
            started.set()
            finish.wait(10)
            return ("old",)
        return ("new",)

    monkeypatch.setattr(C, "_load_external_normalized", _slow)
    out: list = []
    t = threading.Thread(target=lambda: out.append(C._external_normalized(*key)))
    t.start()
    try:
        assert started.wait(5)
        invalidate_external_cache()     # 读取进行中发生失效
        finish.set()
        t.join(10)
    finally:
        finish.set()
        t.join(10)
    assert out == [("old",)]
    # 旧值未回写：下一次调用重新装载拿到新值
    assert C._external_normalized(*key) == ("new",)
    assert calls["n"] == 2


def test_upload_without_declared_source_defaults_labelled(tmp_path):
    """未声明 source 的外部记录：source_of 回退到 BASE_SOURCE（故上传端必须逐条打标，本测锁定该语义）。"""
    base = _make_base(tmp_path)
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    # 已打标（模拟 api_upload 的行为）→ 计为自定义来源
    _write(ext / "stamped.json",
           {"records": [{"dataset_name": "Stamped", "source": "用户上传"}]})
    invalidate_external_cache()
    srcs = {s["value"]: s["count"] for s in available_sources(base, tmp_path)}
    assert srcs.get("用户上传") == 1


def test_load_raw_records_lenient_skips_broken_file(tmp_path):
    """lenient=True（外部库 database/external/）：坏 .json 被跳过、不抛异常，其它文件照常装载。"""
    d = tmp_path / "dir"
    d.mkdir()
    _write(d / "good.json", [{"dataset_name": "Good", "species": "Human"}])
    (d / "broken.json").write_text("{ this is not valid json ", encoding="utf-8")
    recs = load_raw_records(d, lenient=True)
    assert [r.record.get("dataset_name") for r in recs] == ["Good"]


def test_load_raw_records_strict_raises_on_broken_file(tmp_path):
    """lenient=False（默认，基准 database/base/）：坏 .json 必须抛出，绝不静默少数据（确定性守卫）。"""
    import pytest

    d = tmp_path / "dir"
    d.mkdir()
    _write(d / "good.json", [{"dataset_name": "Good"}])
    (d / "broken.json").write_text("{ not valid json ", encoding="utf-8")
    with pytest.raises(ValueError):
        load_raw_records(d)          # 默认严格 → 坏文件让基准装载整体失败（响亮，不静默）


def test_extract_records_bounded_recursion():
    """深度嵌套的合法 JSON → 有界降级为空，绝不 RecursionError（否则会崩掉整目录装载）。"""
    from dataset_recommender.corpus.data_loader import extract_records

    deep = 1
    for _ in range(3000):
        deep = [deep]
    assert extract_records(deep) == []   # 不崩、返回空
