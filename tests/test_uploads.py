# -*- coding: utf-8 -*-
"""共享上传摄取核心 `uploads.py` 单测（Web `/api/upload` 与 MCP `upload_dataset` 共用真源）。

钉死两条安全不变量 + 摄取语义：
- **只进外部库**：写入恒落 `<root>/database/external/`，`saved_to` 前缀 database/external/；base 不碰。
- **保留 upload_ 命名空间**：文件名 upload_<时间戳>_ 前缀，且非 .json → UploadError(bad_file)。
- 逐条打来源标签（form > wrapper > 默认）、可读校验提示、坏输入分类报错、同名防冲突不覆盖。
"""
import json
import re
from pathlib import Path

import pytest

from dataset_recommender.corpus import uploads
from dataset_recommender.corpus.uploads import (
    UploadError,
    ingest_dataset,
    new_upload_name,
    sanitize_upload_name,
)

_STAMP = "20260715_010203_000004"


def _ingest(root: Path, payload, *, filename="x.json", form_source="", note=None):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    safe = new_upload_name(filename, _STAMP)
    kw = dict(raw_bytes=raw, safe_name=safe, project_root=root, form_source=form_source)
    if note is not None:
        kw["note"] = note
    return ingest_dataset(**kw)


# ---- 文件名 / 命名空间 ----
def test_new_upload_name_reserves_prefix_and_requires_json():
    assert new_upload_name("arrayexpress.json", _STAMP) == f"upload_{_STAMP}_arrayexpress.json"
    assert re.fullmatch(r"upload_[0-9_]+_arrayexpress\.json", new_upload_name("arrayexpress.json", _STAMP))


def test_sanitize_upload_name_rejects_non_json():
    with pytest.raises(UploadError) as ei:
        sanitize_upload_name("evil.txt")
    assert ei.value.code == "bad_file"


def test_sanitize_upload_name_strips_path_components():
    # 目录穿越/分隔符被净化成下划线，且只取叶子
    assert sanitize_upload_name("../../etc/passwd.json") == "passwd.json"
    assert "/" not in sanitize_upload_name("a/b/c.json")


# ---- 落盘只进外部库 ----
def test_ingest_writes_external_not_base(tmp_path):
    res = _ingest(tmp_path, [{"dataset_name": "A", "species": "Human"}])
    assert res.record_count == 1
    assert res.saved_to.startswith("database/external/")
    assert res.filename.startswith("upload_") and res.filename.endswith("x.json")
    saved = tmp_path / res.saved_to
    assert saved.is_file()
    assert (tmp_path / "database" / "external").is_dir()
    assert not (tmp_path / "database" / "base").exists()   # 绝不创建/写 base


def test_ingest_normalized_wrapper_and_note(tmp_path):
    res = _ingest(tmp_path, [{"dataset_name": "A"}], note="用户上传（本地 MCP upload_dataset 工具）。")
    disk = json.loads((tmp_path / res.saved_to).read_text(encoding="utf-8"))
    assert set(disk) == {"source", "note", "record_count", "records"}
    assert "MCP" in disk["note"]


# ---- 来源打标 + 优先级 ----
def test_source_priority_form_over_wrapper_over_default(tmp_path):
    # 表单 source 优先于文件包裹层 source
    raw = json.dumps({"source": "包裹来源", "records": [{"dataset_name": "A"}]}, ensure_ascii=False).encode("utf-8")
    res = ingest_dataset(
        raw_bytes=raw, safe_name=new_upload_name("x.json", _STAMP),
        project_root=tmp_path, form_source="表单来源",
    )
    assert res.sources == {"表单来源": 1}


def test_per_record_source_preserved(tmp_path):
    res = _ingest(
        tmp_path,
        [{"dataset_name": "A", "source": "库甲"}, {"dataset_name": "B"}],
        form_source="兜底库",
    )
    assert res.sources == {"库甲": 1, "兜底库": 1}   # 自带 source 保留，缺的用兜底


def test_default_source_when_nothing_declared(tmp_path):
    res = _ingest(tmp_path, [{"dataset_name": "A"}])
    assert res.sources == {"用户上传": 1}   # DEFAULT_UPLOAD_SOURCE


# ---- 可读校验提示 ----
def test_warnings_missing_name_and_unknown_species(tmp_path):
    res = _ingest(tmp_path, [{"species": "Homo sapiens"}, {"dataset_name": "有", "species": "外星生物"}])
    joined = " ".join(res.warnings)
    assert "dataset_name" in joined and "通用名" in joined


def test_no_warnings_on_clean_input(tmp_path):
    res = _ingest(tmp_path, [{"dataset_name": "A", "species": "Human"}])
    assert res.warnings == []


# ---- 坏输入分类报错（写前拦截，不留半个文件）----
def test_bad_encoding_raises(tmp_path):
    with pytest.raises(UploadError) as ei:
        ingest_dataset(
            raw_bytes=b"\xff\xff\xff", safe_name=new_upload_name("x.json", _STAMP),
            project_root=tmp_path,
        )
    assert ei.value.code == "bad_encoding"
    assert not (tmp_path / "database" / "external").exists()


def test_invalid_json_raises(tmp_path):
    with pytest.raises(UploadError) as ei:
        ingest_dataset(
            raw_bytes=b"{ not json", safe_name=new_upload_name("x.json", _STAMP),
            project_root=tmp_path,
        )
    assert ei.value.code == "invalid_json"


def test_no_records_raises(tmp_path):
    with pytest.raises(UploadError) as ei:
        ingest_dataset(
            raw_bytes=b"[]", safe_name=new_upload_name("x.json", _STAMP),
            project_root=tmp_path,
        )
    assert ei.value.code == "no_records"
    assert not (tmp_path / "database" / "external").exists()   # 校验失败绝不落盘


def test_upload_error_is_value_error_with_code_and_hint():
    e = UploadError("some_code", "人读提示")
    assert isinstance(e, ValueError)
    assert e.code == "some_code" and e.hint == "人读提示"
    assert str(e) == "some_code: 人读提示"


# ---- 同名防冲突：同秒多次上传不互相覆盖 ----
def test_same_name_upload_does_not_overwrite(tmp_path):
    r1 = _ingest(tmp_path, [{"dataset_name": "A"}])
    r2 = _ingest(tmp_path, [{"dataset_name": "B"}])   # 同 safe_name → 防冲突后缀
    assert r1.filename != r2.filename
    files = list((tmp_path / "database" / "external").glob("*.json"))
    assert len(files) == 2   # 两个文件都在，无覆盖丢数据


# ---- invalidate_external_cache 被调用（即时可见）----
def test_ingest_invalidates_external_cache(tmp_path, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(uploads, "invalidate_external_cache", lambda: called.__setitem__("n", called["n"] + 1))
    _ingest(tmp_path, [{"dataset_name": "A"}])
    assert called["n"] == 1


# ---- 原子写：.tmp → 流水账 → os.replace 正名 ----
def test_ingest_leaves_no_tmp_and_journal_references_final(tmp_path):
    """正名文件在、`.tmp` 不残留、流水账 filename 指向正名文件。"""
    res = _ingest(tmp_path, [{"dataset_name": "A", "species": "Human"}])
    ext = tmp_path / "database" / "external"
    assert (ext / res.filename).is_file()
    assert not list(ext.glob("*.tmp")), ".tmp 不得残留"
    journal = tmp_path / ".userdata" / "uploads_journal.jsonl"
    lines = journal.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    assert json.loads(lines[0])["filename"] == res.filename


def test_journal_failure_cleans_tmp_and_writes_nothing(tmp_path, monkeypatch):
    """账写不进去 → 删 `.tmp` 回滚 + journal_failed；正名文件与 .tmp 都不留。"""

    def boom(_project_root, _entry):
        raise OSError("simulated journal write failure")

    monkeypatch.setattr(uploads, "_append_upload_journal", boom)
    with pytest.raises(UploadError) as ei:
        _ingest(tmp_path, [{"dataset_name": "A"}])
    assert ei.value.code == "journal_failed"
    ext = tmp_path / "database" / "external"
    assert not list(ext.glob("*.json"))
    assert not list(ext.glob("*.tmp"))


def test_tmp_written_before_journal_and_not_renamed_yet(tmp_path, monkeypatch):
    """落盘顺序钉：流水账被写入的那一刻，`.tmp` 已存在、正名 `*.json` 尚未出现。"""
    real_journal = uploads._append_upload_journal
    observed = {}

    def spy(project_root, entry):
        ext = tmp_path / "database" / "external"
        observed["tmp_exists"] = any(ext.glob("*.tmp"))
        observed["final_exists"] = any(ext.glob("*.json"))
        return real_journal(project_root, entry)

    monkeypatch.setattr(uploads, "_append_upload_journal", spy)
    res = _ingest(tmp_path, [{"dataset_name": "A"}])
    assert observed["tmp_exists"] is True
    assert observed["final_exists"] is False
    assert (tmp_path / "database" / "external" / res.filename).is_file()


def test_replace_failure_cleans_tmp_and_raises(tmp_path, monkeypatch):
    """正名（os.replace）失败 → 清理 `.tmp`、不留正名文件，OSError 原样上抛。"""

    def boom_replace(_src, _dst):
        raise OSError("simulated rename failure")

    monkeypatch.setattr(uploads.os, "replace", boom_replace)
    with pytest.raises(OSError):
        _ingest(tmp_path, [{"dataset_name": "A"}])
    ext = tmp_path / "database" / "external"
    assert not list(ext.glob("*.json"))
    assert not list(ext.glob("*.tmp"))


# ---- 无账 upload_*.json 启动告警扫描（安装器边缘修复第 7 项）----
def _orphan_fixture(tmp_path):
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    journal = tmp_path / ".userdata" / "uploads_journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    return ext, journal


def test_find_orphaned_uploads_reports_only_unjournaled(tmp_path):
    ext, journal = _orphan_fixture(tmp_path)
    (ext / "upload_20260715_010203_000004_a.json").write_text("{}", encoding="utf-8")
    (ext / "upload_20260715_010203_000004_b.json").write_text("{}", encoding="utf-8")
    (ext / "geo.json").write_text("{}", encoding="utf-8")   # 非 upload_ 前缀不参与
    journal.write_text(
        json.dumps({"filename": "upload_20260715_010203_000004_a.json"}) + "\n",
        encoding="utf-8",
    )
    assert uploads.find_orphaned_uploads(ext, journal) == ["upload_20260715_010203_000004_b.json"]


def test_find_orphaned_uploads_empty_when_all_journaled(tmp_path):
    ext, journal = _orphan_fixture(tmp_path)
    (ext / "upload_x.json").write_text("{}", encoding="utf-8")
    journal.write_text(json.dumps({"filename": "upload_x.json"}) + "\n", encoding="utf-8")
    assert uploads.find_orphaned_uploads(ext, journal) == []


def test_find_orphaned_uploads_empty_when_no_external_dir(tmp_path):
    assert uploads.find_orphaned_uploads(tmp_path / "missing", tmp_path / "j.jsonl") == []


def test_find_orphaned_uploads_treats_missing_journal_as_all_orphaned(tmp_path):
    ext, journal = _orphan_fixture(tmp_path)
    (ext / "upload_x.json").write_text("{}", encoding="utf-8")
    # 账本不存在 → 全部 upload_*.json 视为无账（保守告警）
    assert uploads.find_orphaned_uploads(ext, journal) == ["upload_x.json"]


def test_find_orphaned_uploads_skips_corrupt_journal_line(tmp_path):
    ext, journal = _orphan_fixture(tmp_path)
    (ext / "upload_x.json").write_text("{}", encoding="utf-8")
    journal.write_text("not-json\n" + json.dumps({"filename": "upload_x.json"}) + "\n", encoding="utf-8")
    assert uploads.find_orphaned_uploads(ext, journal) == []   # 损坏行跳过，合法行仍认账
