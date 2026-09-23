# -*- coding: utf-8 -*-
"""补丁作用域（contextvars）的**集成测试**（补丁包）。

单元测试钉死 `patch_package` 的机制不变量；本文件钉死它接入 `corpus` / `uploads` /
`corpus_curation` 后的**接线语义**与最高危点——双账号交叉隔离与缓存键含补丁代际：

- 绑定作用域后 `corpus` 读函数合并补丁（blocks 过滤 + adds 追加/按 source 筛选）；
- `corpus_cache_generation` 绑定前后键不同、两账号键不同、未绑定仍是历史 5 元组；
- **双账号交叉**：A 的 blocks/adds 绝不出现在 B 的视图；
- `uploads.ingest_dataset` 绑定时落补丁文件、共享 external 零新文件；
- `corpus_curation` 补丁形态全流程（plan_import 撞重 / remove 本人新增→trash、基线→block /
  restore 往返 / recall_sync 绑定时如实拒答）；
- 每个功能点都配一个**未绑定对照**（缺省形态逐字节不变）。

所有语料都来自 tmp_path 的最小 base/external，绝不触碰仓库 784 条真库与真实 .userdata。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataset_recommender.corpus import corpus as C
from dataset_recommender.corpus import corpus_curation as CC
from dataset_recommender.corpus import patch_package as pp
from dataset_recommender.corpus import uploads
from dataset_recommender.corpus.patch_package import (
    bind_patch_scope,
    current_patch_scope,
    load_patch,
    record_uid,
)

AID_A = "acct_test_001"
AID_B = "acct_test_002"


def _base_dir(root: Path) -> Path:
    d = root / "database" / "base"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _ext_dir(root: Path) -> Path:
    d = root / "database" / "external"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_base(root: Path, records: list[dict]) -> Path:
    base = _base_dir(root)
    (base / "base.json").write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    return base


def _write_patch(root: Path, account_id: str, *, adds=(), blocks=()) -> None:
    path = pp.patch_path_for(root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": pp.SCHEMA_VERSION,
        "account_id": account_id,
        "updated_at": 0.0,
        "adds": list(adds),
        "blocks": list(blocks),
        "trash": [],
    }, ensure_ascii=False), encoding="utf-8")


def _uids(records) -> list[str]:
    return [record_uid(r.raw if isinstance(getattr(r, "raw", None), dict) else {}) for r in records]


# ---------------------------------------------------------------- corpus 读侧合并


def test_load_full_corpus_bound_applies_blocks_and_adds(tmp_path):
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    with bind_patch_scope(AID_A):
        full = C.load_full_corpus(data_dir, tmp_path)
    assert sorted(_uids(full)) == ["A1", "B1"]   # B2 被屏蔽、A1 追加


def test_load_normalized_corpus_bound_sources_none_blocks_only(tmp_path):
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    with bind_patch_scope(AID_A):
        recs = C.load_normalized_corpus(data_dir, tmp_path, sources=None)
    assert _uids(recs) == ["B1"]   # blocks 生效，sources=None 不追加 adds


def test_load_normalized_corpus_bound_sources_filters_adds_by_source(tmp_path):
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
    ])
    _write_patch(tmp_path, AID_A, adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
        {"dataset_uid": "A2", "dataset_name": "Add 2", "species": "Mouse", "source": "SRC_B"},
    ])
    data_dir = _base_dir(tmp_path)
    with bind_patch_scope(AID_A):
        recs = C.load_normalized_corpus(data_dir, tmp_path, sources=["SRC_A"])
    assert _uids(recs) == ["A1"]   # base 未选中、SRC_B 的 add 被 source 筛选掉


def test_available_sources_bound_counts_blocks_and_adds(tmp_path):
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    with bind_patch_scope(AID_A):
        sources = C.available_sources(data_dir, tmp_path)
    by = {s["value"]: s["count"] for s in sources}
    assert by["10x Genomics"] == 1   # B2 被 blocks 扣掉
    assert by["SRC_A"] == 1


def test_corpus_cache_generation_bound_vs_unbound_and_accounts(tmp_path):
    data_dir = _base_dir(tmp_path)
    _write_patch(tmp_path, AID_A, blocks=["X"])
    _write_patch(tmp_path, AID_B, blocks=["Y"])

    unbound = C.corpus_cache_generation(data_dir, tmp_path)
    assert len(unbound) == 5   # 未绑定仍是历史 5 元组

    with bind_patch_scope(AID_A):
        ka = C.corpus_cache_generation(data_dir, tmp_path)
    with bind_patch_scope(AID_B):
        kb = C.corpus_cache_generation(data_dir, tmp_path)

    assert len(ka) == 7 and len(kb) == 7
    assert ka != unbound and kb != unbound
    assert ka != kb                # 两账号键不同
    assert ka[:5] == unbound       # 追加段与历史前 5 元组同形


def test_double_account_cross_isolation(tmp_path):
    """高危点：A 的 blocks/adds 绝不泄进 B 的视图。"""
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    with bind_patch_scope(AID_A):
        view_a = set(_uids(C.load_full_corpus(data_dir, tmp_path)))
    with bind_patch_scope(AID_B):
        view_b = set(_uids(C.load_full_corpus(data_dir, tmp_path)))

    assert view_a == {"B1", "A1"}
    assert view_b == {"B1", "B2"}   # B 看不见 A 的 block 和 add


def test_corpus_read_unbound_unchanged(tmp_path):
    """未绑定对照：补丁一概不生效，与历史逐字节同形。"""
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    assert current_patch_scope() is None
    assert sorted(_uids(C.load_full_corpus(data_dir, tmp_path))) == ["B1", "B2"]


def test_corpus_read_unbound_normalized_and_sources_unchanged(tmp_path):
    """未绑定对照：load_normalized_corpus / available_sources 同样逐字节不变。"""
    _write_base(tmp_path, [
        {"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"},
        {"dataset_uid": "B2", "dataset_name": "Base 2", "species": "Mouse"},
    ])
    _write_patch(tmp_path, AID_A, blocks=["B2"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    data_dir = _base_dir(tmp_path)
    assert current_patch_scope() is None
    assert _uids(C.load_normalized_corpus(data_dir, tmp_path, sources=None)) == ["B1", "B2"]
    assert _uids(C.load_normalized_corpus(data_dir, tmp_path, sources=["SRC_A"])) == []
    by = {s["value"]: s["count"] for s in C.available_sources(data_dir, tmp_path)}
    assert by["10x Genomics"] == 2
    assert "SRC_A" not in by


# ---------------------------------------------------------------- uploads.ingest_dataset 作用域路由


def test_uploads_ingest_bound_writes_patch_not_external(tmp_path):
    raw = json.dumps(
        [{"dataset_uid": "UP-1", "dataset_name": "Up 1", "species": "Human"}],
        ensure_ascii=False,
    ).encode("utf-8")
    safe = uploads.new_upload_name("x.json", "20260715_010203_000004")
    with bind_patch_scope(AID_A):
        res = uploads.ingest_dataset(
            raw_bytes=raw, safe_name=safe, project_root=tmp_path, form_source="src")

    assert res.record_count == 1
    assert res.saved_to == "我的补丁包（仅本账户可见）"
    patch = load_patch(tmp_path, AID_A)
    assert [record_uid(r) for r in patch["adds"]] == ["UP-1"]
    ext = tmp_path / "database" / "external"
    assert not ext.exists() or list(ext.glob("*.json")) == []   # 共享 external 零新文件


def test_uploads_ingest_unbound_writes_external_not_patch(tmp_path):
    raw = json.dumps(
        [{"dataset_uid": "UP-2", "dataset_name": "Up 2", "species": "Human"}],
        ensure_ascii=False,
    ).encode("utf-8")
    safe = uploads.new_upload_name("x.json", "20260715_010203_000004")
    res = uploads.ingest_dataset(raw_bytes=raw, safe_name=safe, project_root=tmp_path)
    assert res.saved_to.startswith("database/external/")
    assert (tmp_path / res.saved_to).is_file()
    assert not pp.patch_path_for(tmp_path, AID_A).exists()


# ---------------------------------------------------------------- corpus_curation 补丁形态


def test_curation_list_bound_patch_mode(tmp_path):
    _write_patch(tmp_path, AID_A, blocks=["B1"], adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human", "source": "SRC_A"},
    ])
    with bind_patch_scope(AID_A):
        res = CC.list_curations(project_root=tmp_path)
    assert res["mode"] == "patch"
    assert res["blocks"] == ["B1"]
    assert [f["dataset_uid"] for f in res["files"]] == ["A1"]


def test_curation_list_unbound_legacy(tmp_path):
    _ext_dir(tmp_path)
    (tmp_path / "database" / "external" / "upload_20260715_010203_000004_x.json").write_text(
        json.dumps([{"dataset_name": "A", "species": "Human"}]), encoding="utf-8")
    res = CC.list_curations(project_root=tmp_path)
    assert "mode" not in res
    assert res["file_count"] == 1


def test_curation_plan_import_bound_duplicate(tmp_path):
    records = [{"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human"}]
    _write_patch(tmp_path, AID_A, adds=records)
    payload = json.dumps(records, ensure_ascii=False).encode("utf-8")
    with bind_patch_scope(AID_A):
        plan = CC.plan_import(payload, "x.json", project_root=tmp_path)
    assert plan["duplicate"]["is_duplicate"] is True
    assert plan["duplicate"]["matched_files"] == ["我的补丁包"]


def test_curation_plan_import_unbound_legacy(tmp_path):
    records = [{"dataset_name": "A", "species": "Human"}]
    _ext_dir(tmp_path)
    (tmp_path / "database" / "external" / "upload_x.json").write_text(
        json.dumps(records), encoding="utf-8")
    payload = json.dumps(records, ensure_ascii=False).encode("utf-8")
    res = CC.plan_import(payload, "x.json", project_root=tmp_path)
    assert res["duplicate"]["is_duplicate"] is True
    assert res["duplicate"]["matched_files"] == ["upload_x.json"]


def test_curation_remove_own_add_goes_to_trash(tmp_path):
    _write_patch(tmp_path, AID_A, adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human"},
    ])
    with bind_patch_scope(AID_A):
        plan = CC.plan_remove("patch:A1", project_root=tmp_path)
        assert plan["_kind"] == "trash"
        out = CC.apply_remove("patch:A1", confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert out["moved_to"] == "patch:trash"
    patch = load_patch(tmp_path, AID_A)
    assert patch["adds"] == []
    assert [record_uid(r) for r in patch["trash"]] == ["A1"]


def test_curation_remove_base_entry_blocks(tmp_path):
    _write_base(tmp_path, [{"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"}])
    _write_patch(tmp_path, AID_A)
    with bind_patch_scope(AID_A):
        plan = CC.plan_remove("patch:B1", project_root=tmp_path)
        assert plan["_kind"] == "block"
        out = CC.apply_remove("patch:B1", confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert out["moved_to"] == "patch:blocks"
    assert load_patch(tmp_path, AID_A)["blocks"] == ["B1"]


def test_curation_remove_unbound_legacy(tmp_path):
    fname = "upload_20260715_010203_000004_x.json"
    _ext_dir(tmp_path)
    (tmp_path / "database" / "external" / fname).write_text(
        json.dumps([{"dataset_name": "A", "species": "Human"}]), encoding="utf-8")
    res = CC.plan_remove(fname, project_root=tmp_path)
    assert "mode" not in res and "dataset_uid" not in res
    assert res["filename"] == fname


def test_curation_restore_own_add_roundtrip(tmp_path):
    _write_patch(tmp_path, AID_A, adds=[
        {"dataset_uid": "A1", "dataset_name": "Add 1", "species": "Human"},
    ])
    with bind_patch_scope(AID_A):
        plan = CC.plan_remove("patch:A1", project_root=tmp_path)
        CC.apply_remove("patch:A1", confirm_token=plan["confirm_token"], project_root=tmp_path)
        rplan = CC.plan_restore("patch:A1", project_root=tmp_path)
        assert rplan["_kind"] == "untrash"
        rout = CC.apply_restore("patch:A1", confirm_token=rplan["confirm_token"], project_root=tmp_path)
    assert rout["restored_to"] == "我的补丁包（仅本账户可见）"
    patch = load_patch(tmp_path, AID_A)
    assert [record_uid(r) for r in patch["adds"]] == ["A1"]
    assert patch["trash"] == []


def test_curation_restore_base_block_roundtrip(tmp_path):
    _write_base(tmp_path, [{"dataset_uid": "B1", "dataset_name": "Base 1", "species": "Human"}])
    _write_patch(tmp_path, AID_A)
    with bind_patch_scope(AID_A):
        plan = CC.plan_remove("patch:B1", project_root=tmp_path)
        CC.apply_remove("patch:B1", confirm_token=plan["confirm_token"], project_root=tmp_path)
        rplan = CC.plan_restore("patch:B1", project_root=tmp_path)
        assert rplan["_kind"] == "unblock"
        CC.apply_restore("patch:B1", confirm_token=rplan["confirm_token"], project_root=tmp_path)
    assert load_patch(tmp_path, AID_A)["blocks"] == []


def test_curation_restore_unbound_legacy(tmp_path):
    fname = "upload_20260715_010203_000004_x.json"
    _ext_dir(tmp_path)
    (tmp_path / "database" / "external" / fname).write_text(
        json.dumps([{"dataset_name": "A", "species": "Human"}]), encoding="utf-8")
    plan = CC.plan_remove(fname, project_root=tmp_path)
    CC.apply_remove(fname, confirm_token=plan["confirm_token"], project_root=tmp_path)
    entries = list(CC._recycle_dir(tmp_path).glob("*.json"))
    assert len(entries) == 1
    rplan = CC.plan_restore(entries[0].name, project_root=tmp_path)
    assert "mode" not in rplan and "dataset_uid" not in rplan
    assert rplan["target_filename"] == fname


def test_curation_recall_sync_bound_honest_refusal(tmp_path):
    with bind_patch_scope(AID_A):
        with pytest.raises(CC.CurateError) as ei:
            CC.recall_sync_operation("whatever", project_root=tmp_path)
    assert ei.value.code == "bad_param"
    assert "补丁包" in ei.value.hint


def test_curation_recall_sync_unbound_legacy(tmp_path):
    with pytest.raises(CC.CurateError) as ei:
        CC.recall_sync_operation("whatever", project_root=tmp_path)
    assert ei.value.code == "unknown_operation"
