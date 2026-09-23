"""阶段二下载链接接入模块单测：真实数据查询 + 优雅降级。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.corpus import downloads  # noqa: E402

_UID = "multiome-gemx-5k-mouse-kidney"
_PAGE = "https://www.10xgenomics.com/datasets/visium-v2-human-lymph-node"
_XENIUM = "xenium-prime-ffpe-human-ovarian-cancer"


def _is_real(u: str) -> bool:
    return bool(u) and ("cf.10xgenomics.com" in u or "s3-us-west-2" in u)


def test_data_available():
    assert downloads.is_available() is True


def test_primary_url_by_uid_is_real_directlink():
    assert _is_real(downloads.primary_url(_UID))


def test_lookup_by_page_url_works():
    assert _is_real(downloads.primary_url(_PAGE))


def test_download_cell_format():
    cell = downloads.download_cell(_UID)
    assert cell.startswith("[点击下载](") and cell.endswith(")")


def test_has_fastq_xenium_is_false():
    # Xenium 是成像平台，天然无 FASTQ —— 更正信号应为 False（非索引的过度声称 True）
    assert downloads.has_fastq(_XENIUM) is False


def test_all_real_urls_nonempty():
    urls = downloads.all_real_urls(_UID)
    assert len(urls) > 0 and all(_is_real(u) for u in urls)


def test_missing_key_returns_none():
    assert downloads.primary_url("no-such-dataset-xyz") is None
    assert downloads.fastq_url("no-such-dataset-xyz") is None
    assert downloads.has_fastq("no-such-dataset-xyz") is None
    assert downloads.all_real_urls("no-such-dataset-xyz") == set()
    assert downloads.download_cell("no-such-dataset-xyz") == "-"


def test_none_input_safe():
    assert downloads.primary_url(None) is None
    assert downloads.all_real_urls(None) == set()


def test_missing_data_file_degrades_gracefully(monkeypatch):
    # 数据文件缺失 → 全部返回 None/空、is_available False，绝不抛异常（monkeypatch 自动还原路径）
    downloads._load.cache_clear()
    monkeypatch.setattr(downloads, "_DATA_PATH", str(Path("/nonexistent") / "xyz.json"))
    try:
        assert downloads.is_available() is False
        assert downloads.primary_url(_UID) is None
        assert downloads.download_cell(_UID) == "-"
    finally:
        downloads._load.cache_clear()  # 清缓存，后续测试/进程重新加载真实数据


def test_partially_corrupt_file_degrades_gracefully(monkeypatch, tmp_path):
    # 顶层是合法 dict，但个别记录值退化成标量/列表（部分损坏/旧 schema）→ getter 绝不 AttributeError，
    # 坏记录返回 None/空、好记录照常。守住"永不崩→降级"合同。
    import json
    bad = {
        "uid_scalar": "https://x",
        "uid_int": 123,
        "uid_list": [1, 2],
        "uid_ok": {"primary_download_url": "https://cf.10xgenomics.com/ok",
                   "files": [{"download_url": "https://cf.10xgenomics.com/ok"}]},
    }
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    downloads._load.cache_clear()
    monkeypatch.setattr(downloads, "_DATA_PATH", str(p))
    try:
        assert downloads.primary_url("uid_scalar") is None   # 曾经 AttributeError
        assert downloads.has_fastq("uid_int") is None
        assert downloads.all_real_urls("uid_list") == set()
        assert downloads.download_cell("uid_scalar") == "-"
        assert downloads.primary_url("uid_ok") == "https://cf.10xgenomics.com/ok"  # 好记录仍可用
    finally:
        downloads._load.cache_clear()


# ---- 环境变量运行时生效 ----

def test_env_override_takes_effect_at_runtime(tmp_path, monkeypatch):
    """BIODATA_DOWNLOAD_LINKS 运行时变更必须生效——路径每次加载现读、含进缓存键，
    不再 import 期固化（此前长驻进程改 env 无效）。"""
    import json as _json

    p = tmp_path / "alt_links.json"
    p.write_text(_json.dumps({
        "uid-x": {"url": "https://example.org/ds",
                  "primary_download_url": "https://example.org/f.h5"},
    }), encoding="utf-8")
    monkeypatch.setenv("BIODATA_DOWNLOAD_LINKS", str(p))
    # 不清缓存：路径变了 → 缓存键自然不同 → 直接读到新文件
    assert downloads.primary_url("uid-x") == "https://example.org/f.h5"


# ---- 读盘失败不再负向缓存 ----

def test_corrupt_file_not_cached_and_recovers(tmp_path, monkeypatch):
    """坏 JSON → _load() 降级空表但不入缓存；同进程修复文件后无需 cache_clear/重启即恢复
    （修复前：失败被 lru_cache 缓存到进程结束）。"""
    import json as _json

    p = tmp_path / "links.json"
    p.write_text("{ 这不是 JSON", encoding="utf-8")
    monkeypatch.setenv("BIODATA_DOWNLOAD_LINKS", str(p))
    downloads._load.cache_clear()
    try:
        assert downloads._load() == ({}, {})   # 降级：不抛
        p.write_text(_json.dumps({"uid-r": {"url": "https://example.org/r",
                                            "primary_download_url": "https://example.org/r.h5"}}),
                     encoding="utf-8")
        # 同一路径、不清缓存：失败没入缓存 → 直接读到修复后的内容
        assert downloads.primary_url("uid-r") == "https://example.org/r.h5"
    finally:
        downloads._load.cache_clear()
