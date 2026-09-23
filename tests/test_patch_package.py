# -*- coding: utf-8 -*-
"""每账户语料补丁包 `patch_package.py` 的单元测试（补丁包）。

测的是**机制不变量**，不依赖仓库 784 条真库（全部用 tmp_path 里的最小 base/external）：
- 空补丁加载/往返：缺失/空文件 → 空补丁；读写往返结构一致。
- 损坏文件 **fail-closed**：坏 JSON / 错 schema_version / 错 account_id / 非 list 字段
  全部抛 `PatchError(patch_corrupt)`，绝不静默重建覆盖用户数据。
- 账户 id 白名单：`../x` 等路径穿越/非法字符 → `PatchError(bad_account)`。
- 存储路径守卫：`BIODATA_PATCHES_DIR` 指向仓库 `database/` 内 → `PatchError(bad_store_path)`。
- `apply_patch`：blocks 按 dataset_uid 过滤 / adds 归一化追加 / source_filter 筛选 /
  `include_adds=False` 不追加（但 blocks 恒生效）。
- `block_uids`/`unblock_uids` 幂等 + 本人 adds 不进 blocks；`trash_adds`/`restore_adds`
  往返 + 防双重存在。
- `ingest_records_to_patch`：本人撞重跳过 / 与基线撞号拒收 / 上限闸 / 全跳过零写入不落盘。
- `patch_generation`：无文件 → (id, None, None)；写入后变化。

这是防回归墙：补丁是**多账号网页版隔离**的唯一边界，任何「静默重建」「撞号放行」
「blocks 漏过滤」「写入落错目录」都会直接破坏账号隔离或用户数据。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dataset_recommender.corpus import patch_package as pp
from dataset_recommender.corpus.patch_package import (
    PatchError,
    apply_patch,
    bind_patch_scope,
    block_uids,
    current_patch_scope,
    ingest_records_to_patch,
    load_patch,
    patch_generation,
    patch_path_for,
    restore_adds,
    trash_adds,
    unblock_uids,
)
from dataset_recommender.retrieval.normalizer import normalize_dataset_record

AID = "acct_test_001"


def _rec(uid: str, name: str = "ds", source: str | None = None):
    """构造一条最小语料记录（真实 DatasetRecord，带 .raw['dataset_uid']）。"""
    raw = {"dataset_uid": uid, "dataset_name": name, "species": "Human"}
    if source is not None:
        raw["source"] = source
    return normalize_dataset_record(raw, "unit-test")


def _seed_patch_file(root: Path, account_id: str, payload) -> Path:
    """把一份（可能损坏的）补丁写到该账户的补丁路径上，供 load_patch 读取。"""
    path = patch_path_for(root, account_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        path.write_bytes(payload)
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _valid_patch(account_id: str = AID, **overrides) -> dict:
    data = {
        "schema_version": pp.SCHEMA_VERSION,
        "account_id": account_id,
        "updated_at": 0.0,
        "adds": [],
        "blocks": [],
        "trash": [],
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------- 作用域绑定


def test_bind_patch_scope_sets_and_resets():
    assert current_patch_scope() is None
    with bind_patch_scope(AID):
        assert current_patch_scope() == AID
    assert current_patch_scope() is None
    with bind_patch_scope(None):
        assert current_patch_scope() is None


# ---------------------------------------------------------------- 空补丁加载 / 往返


def test_load_patch_missing_returns_empty(tmp_path):
    patch = load_patch(tmp_path, AID)
    assert patch["schema_version"] == pp.SCHEMA_VERSION
    assert patch["account_id"] == AID
    assert patch["adds"] == [] and patch["blocks"] == [] and patch["trash"] == []


def test_load_patch_empty_file_returns_empty(tmp_path):
    _seed_patch_file(tmp_path, AID, b"")
    assert load_patch(tmp_path, AID)["adds"] == []


def test_block_unblock_roundtrip_preserves_shape(tmp_path):
    """写入后读回：字段齐、类型对、往返一致（空补丁之外的「往返」半）。"""
    block_uids(tmp_path, AID, ["X-1"])
    patch = load_patch(tmp_path, AID)
    assert patch["schema_version"] == pp.SCHEMA_VERSION
    assert patch["account_id"] == AID
    assert isinstance(patch["blocks"], list) and patch["blocks"] == ["X-1"]
    assert isinstance(patch["adds"], list) and isinstance(patch["trash"], list)


# ---------------------------------------------------------------- 损坏文件 fail-closed


def test_load_patch_bad_json_fail_closed(tmp_path):
    _seed_patch_file(tmp_path, AID, b"{ not json")
    with pytest.raises(PatchError) as ei:
        load_patch(tmp_path, AID)
    assert ei.value.code == "patch_corrupt"


def test_load_patch_wrong_schema_version_fail_closed(tmp_path):
    _seed_patch_file(tmp_path, AID, _valid_patch(schema_version=999))
    with pytest.raises(PatchError) as ei:
        load_patch(tmp_path, AID)
    assert ei.value.code == "patch_corrupt"


def test_load_patch_wrong_account_id_fail_closed(tmp_path):
    _seed_patch_file(tmp_path, AID, _valid_patch(account_id="other_account_001"))
    with pytest.raises(PatchError) as ei:
        load_patch(tmp_path, AID)
    assert ei.value.code == "patch_corrupt"


@pytest.mark.parametrize("field", ["adds", "blocks", "trash"])
def test_load_patch_non_list_field_fail_closed(tmp_path, field):
    _seed_patch_file(tmp_path, AID, _valid_patch(**{field: "not-a-list"}))
    with pytest.raises(PatchError) as ei:
        load_patch(tmp_path, AID)
    assert ei.value.code == "patch_corrupt"


# ---------------------------------------------------------------- 账户 id 白名单（防路径穿越）


@pytest.mark.parametrize("bad", ["../x", "a/b", "a b", "", "ab", "x" * 65, "..", ".hidden"])
def test_patch_path_rejects_illegal_account_id(bad):
    with pytest.raises(PatchError) as ei:
        patch_path_for(Path("."), bad)
    assert ei.value.code == "bad_account"


def test_load_patch_rejects_illegal_account_id(tmp_path):
    with pytest.raises(PatchError) as ei:
        load_patch(tmp_path, "../x")
    assert ei.value.code == "bad_account"


# ---------------------------------------------------------------- 存储路径守卫


def test_bad_store_path_under_repo_database(tmp_path, monkeypatch):
    repo_database = Path(pp.__file__).resolve().parents[3] / "database"
    monkeypatch.setenv("BIODATA_PATCHES_DIR", str(repo_database / "patches"))
    with pytest.raises(PatchError) as ei:
        patch_path_for(tmp_path, AID)
    assert ei.value.code == "bad_store_path"


# ---------------------------------------------------------------- apply_patch（读侧合并）


def test_apply_patch_blocks_filter_by_uid():
    recs = [_rec("A-1"), _rec("B-2"), _rec("C-3")]
    patch = _valid_patch(blocks=["B-2"])
    out = apply_patch(recs, patch, include_adds=True)
    assert [r.raw["dataset_uid"] for r in out] == ["A-1", "C-3"]
    # 入参不被改写
    assert [r.raw["dataset_uid"] for r in recs] == ["A-1", "B-2", "C-3"]


def test_apply_patch_appends_normalized_adds():
    recs = [_rec("A-1")]
    patch = _valid_patch(adds=[{
        "dataset_uid": "P-1", "dataset_name": "Patch One", "species": "Mouse", "source": "SRC",
    }])
    out = apply_patch(recs, patch, include_adds=True)
    assert [r.raw["dataset_uid"] for r in out] == ["A-1", "P-1"]
    assert out[-1].raw["source"] == "SRC"
    assert out[-1].source_file == f"patch:{AID}"


def test_apply_patch_source_filter_filters_adds():
    recs = [_rec("A-1", source="KEEP")]
    patch = _valid_patch(adds=[
        {"dataset_uid": "P-1", "dataset_name": "P1", "species": "Mouse", "source": "KEEP"},
        {"dataset_uid": "P-2", "dataset_name": "P2", "species": "Mouse", "source": "DROP"},
    ])
    out = apply_patch(recs, patch, include_adds=True, source_filter={"KEEP"})
    assert [r.raw["dataset_uid"] for r in out] == ["A-1", "P-1"]


def test_apply_patch_include_adds_false_skips_adds_but_blocks_apply():
    recs = [_rec("A-1"), _rec("B-2")]
    patch = _valid_patch(
        blocks=["B-2"],
        adds=[{"dataset_uid": "P-1", "dataset_name": "P1", "species": "Mouse", "source": "X"}],
    )
    out = apply_patch(recs, patch, include_adds=False)
    assert [r.raw["dataset_uid"] for r in out] == ["A-1"]


# ---------------------------------------------------------------- block / unblock（幂等 + 本人 adds 不进 blocks）


def test_block_unblock_idempotent(tmp_path):
    r1 = block_uids(tmp_path, AID, ["X-1", "X-2"])
    assert r1["blocked"] == ["X-1", "X-2"]
    r2 = block_uids(tmp_path, AID, ["X-2", "X-3"])
    assert r2["blocked"] == ["X-3"]
    assert r2["already"] == ["X-2"]
    assert load_patch(tmp_path, AID)["blocks"] == ["X-1", "X-2", "X-3"]

    u1 = unblock_uids(tmp_path, AID, ["X-2", "X-9"])
    assert u1["unblocked"] == ["X-2"]
    assert u1["not_blocked"] == ["X-9"]
    assert load_patch(tmp_path, AID)["blocks"] == ["X-1", "X-3"]

    u2 = unblock_uids(tmp_path, AID, ["X-2"])
    assert u2["unblocked"] == [] and u2["not_blocked"] == ["X-2"]


def test_block_uid_in_own_adds_not_blocked(tmp_path):
    ingest_records_to_patch(
        payload={},
        records=[{"dataset_uid": "P-1", "dataset_name": "P1", "species": "Human"}],
        project_root=tmp_path,
        account_id=AID,
    )
    res = block_uids(tmp_path, AID, ["P-1"])
    assert res["blocked"] == [] and res["own_adds"] == ["P-1"]
    patch = load_patch(tmp_path, AID)
    assert patch["blocks"] == [] and len(patch["adds"]) == 1


# ---------------------------------------------------------------- trash / restore（往返 + 防双重存在）


def test_trash_restore_roundtrip_and_no_double(tmp_path):
    ingest_records_to_patch(
        payload={},
        records=[
            {"dataset_uid": "P-1", "dataset_name": "P1", "species": "Human"},
            {"dataset_uid": "P-2", "dataset_name": "P2", "species": "Human"},
        ],
        project_root=tmp_path,
        account_id=AID,
    )
    tr = trash_adds(tmp_path, AID, ["P-1", "missing"])
    assert tr["moved"] == ["P-1"] and tr["not_found"] == ["missing"]
    patch = load_patch(tmp_path, AID)
    assert [pp.record_uid(r) for r in patch["adds"]] == ["P-2"]
    assert [pp.record_uid(r) for r in patch["trash"]] == ["P-1"]

    rs = restore_adds(tmp_path, AID, ["P-1"])
    assert rs["restored"] == ["P-1"]
    patch = load_patch(tmp_path, AID)
    assert sorted(pp.record_uid(r) for r in patch["adds"]) == ["P-1", "P-2"]
    assert patch["trash"] == []

    # 防双重存在：再恢复同一 uid → already_present，且 adds 不出现重复条目
    rs2 = restore_adds(tmp_path, AID, ["P-1"])
    assert rs2["already_present"] == ["P-1"]
    patch = load_patch(tmp_path, AID)
    assert [pp.record_uid(r) for r in patch["adds"]].count("P-1") == 1


def test_trash_not_found_no_write(tmp_path):
    res = trash_adds(tmp_path, AID, ["nope"])
    assert res["moved"] == [] and res["not_found"] == ["nope"]
    assert not patch_path_for(tmp_path, AID).exists()


# ---------------------------------------------------------------- ingest_records_to_patch


def test_ingest_own_duplicate_skipped(tmp_path):
    rec = {"dataset_uid": "P-1", "dataset_name": "P1", "species": "Human"}
    r1 = ingest_records_to_patch(payload={}, records=[rec], project_root=tmp_path, account_id=AID)
    assert r1.record_count == 1
    r2 = ingest_records_to_patch(payload={}, records=[rec], project_root=tmp_path, account_id=AID)
    assert r2.record_count == 0
    assert any("去重" in w for w in r2.warnings)
    assert len(load_patch(tmp_path, AID)["adds"]) == 1


def test_ingest_baseline_collision_rejected_and_no_write(tmp_path):
    base_dir = tmp_path / "database" / "base"
    base_dir.mkdir(parents=True)
    (base_dir / "base.json").write_text(
        json.dumps([{"dataset_uid": "BASE-1", "dataset_name": "Base One", "species": "Human"}]),
        encoding="utf-8",
    )
    rec = {"dataset_uid": "BASE-1", "dataset_name": "Other", "species": "Mouse"}
    r = ingest_records_to_patch(payload={}, records=[rec], project_root=tmp_path, account_id=AID)
    assert r.record_count == 0
    assert any("撞号" in w for w in r.warnings)
    assert not patch_path_for(tmp_path, AID).exists()   # 零写入不落盘


def test_ingest_budget_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(pp, "MAX_PATCH_ADD_RECORDS", 2)
    records = [
        {"dataset_uid": "P-1", "dataset_name": "P1", "species": "Human"},
        {"dataset_uid": "P-2", "dataset_name": "P2", "species": "Human"},
        {"dataset_uid": "P-3", "dataset_name": "P3", "species": "Human"},
    ]
    with pytest.raises(PatchError) as ei:
        ingest_records_to_patch(payload={}, records=records, project_root=tmp_path, account_id=AID)
    assert ei.value.code == "too_large"
    assert not patch_path_for(tmp_path, AID).exists()


# ---------------------------------------------------------------- patch_generation


def test_patch_generation_missing_then_changes_after_write(tmp_path):
    assert patch_generation(tmp_path, AID) == (AID, None, None)
    block_uids(tmp_path, AID, ["X-1"])
    gen = patch_generation(tmp_path, AID)
    assert gen[0] == AID
    assert gen[1] is not None and gen[2] is not None
    assert gen != (AID, None, None)
