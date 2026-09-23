# -*- coding: utf-8 -*-
"""trace.snapshot + trace.rollback：快照 diff/回退正确性/fail-closed/dry-run/幂等/CLI。

夹具在 tmp 项目根下造 `database/external/` 文件——快照 watch 目录恒为它（base 红线）。"""
from __future__ import annotations

import json

import pytest

from dataset_recommender.agent.trace.rollback import apply_rollback, main, plan_rollback
from dataset_recommender.agent.trace.snapshot import SnapshotError, SnapshotStore


@pytest.fixture()
def proj(tmp_path):
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "keep.json").write_text('{"records": [{"id": 1}]}', encoding="utf-8")
    return tmp_path


def _ext(proj):
    return proj / "database" / "external"


def _manifest_rows(proj):
    path = proj / ".userdata" / "recycle" / "manifest.jsonl"
    if not path.is_file():
        return []
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_capture_finalize_diff_created(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_20260817_x_curate_arrayexpress.json").write_text(
        '{"records": [{"id": 2}]}', encoding="utf-8")
    diff = store.finalize(sid)
    assert [e["name"] for e in diff["created"]] == ["upload_20260817_x_curate_arrayexpress.json"]
    assert diff["modified"] == [] and diff["deleted"] == []
    meta = store.load(sid)
    assert meta["finalized"] is True and meta["preimage_missing"] == []
    assert meta["diff"]["created"][0]["sha256"]


def test_rollback_created_file_goes_to_recycle_not_deleted(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    new_file = _ext(proj) / "upload_a.json"
    new_file.write_text('{"records": [{"id": 9}]}', encoding="utf-8")
    store.finalize(sid)
    # dry-run：只出计划，零副作用
    plan = plan_rollback(proj, sid)
    assert plan["recycle_created"] == ["upload_a.json"]
    assert new_file.is_file() and not (proj / ".userdata").exists()
    # apply：移入回收站（移动而非删除），manifest 留行，meta 记回退
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["recycled"]] == ["upload_a.json"]
    assert not new_file.is_file()
    recycle = proj / ".userdata" / "recycle"
    moved = list(recycle.glob("*_upload_a.json"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == '{"records": [{"id": 9}]}'
    rows = _manifest_rows(proj)
    assert rows and rows[0]["action"] == "trace_rollback"
    assert rows[0]["original_path"] == "database/external/upload_a.json"
    assert rows[0]["snapshot_id"] == sid
    assert store.load(sid)["rolled_back_at"]
    # 幂等：二次执行 → created 已不存在，如实 skipped，不再动
    result2 = apply_rollback(proj, sid)
    assert result2["applied"]["recycled"] == []
    assert any(s["name"] == "upload_a.json" for s in result2["skipped"])


def test_rollback_modified_with_preimage_restores_bytes(proj):
    store = SnapshotStore(proj)
    original = (_ext(proj) / "keep.json").read_bytes()
    sid = store.capture("curate.restore", preimage_paths=["keep.json"])
    (_ext(proj) / "keep.json").write_text('{"records": []}', encoding="utf-8")
    store.finalize(sid)
    plan = plan_rollback(proj, sid)
    assert plan["restore_bytes"] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["restored"]] == ["keep.json"]
    assert (_ext(proj) / "keep.json").read_bytes() == original


def test_rollback_deleted_with_preimage_restores_file(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.remove", preimage_paths=["keep.json"])
    (_ext(proj) / "keep.json").unlink()
    diff = store.finalize(sid)
    assert [e["name"] for e in diff["deleted"]] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert [e["name"] for e in result["applied"]["restored"]] == ["keep.json"]
    assert (_ext(proj) / "keep.json").is_file()


def test_rollback_modified_without_preimage_fail_closed(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")      # 没点名 preimage
    (_ext(proj) / "keep.json").write_text('{"records": [{"id": 3}]}', encoding="utf-8")
    store.finalize(sid)
    assert store.load(sid)["preimage_missing"] == ["keep.json"]
    plan = plan_rollback(proj, sid)
    assert [u["name"] for u in plan["unrestorable"]] == ["keep.json"]
    result = apply_rollback(proj, sid)
    assert result["applied"]["restored"] == []       # 拒动：宁可少退不毁数据
    assert (_ext(proj) / "keep.json").read_text(encoding="utf-8") == '{"records": [{"id": 3}]}'


def test_rollback_requires_finalized_snapshot(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    with pytest.raises(SnapshotError):
        plan_rollback(proj, sid)
    with pytest.raises(SnapshotError):      # apply 同闸：未 finalize 绝不 silently 空退
        apply_rollback(proj, sid)


def test_rollback_unknown_snapshot_honest_error(proj):
    with pytest.raises(SnapshotError):
        plan_rollback(proj, "ghost")


def test_snapshot_list_and_bad_meta(proj):
    store = SnapshotStore(proj)
    sid = store.capture("curate.sync_updates")
    rows = store.list_snapshots()
    assert rows and rows[0]["snapshot_id"] == sid and rows[0]["verb"] == "curate.sync_updates"
    bad = store.dir / "broken"
    bad.mkdir()
    (bad / "meta.json").write_text("{bad", encoding="utf-8")
    rows = store.list_snapshots()
    assert any(r.get("error") for r in rows if r["snapshot_id"] == "broken")


def test_capture_rejects_unsafe_preimage_name(proj):
    store = SnapshotStore(proj)
    with pytest.raises(SnapshotError):
        store.capture("curate.remove", preimage_paths=["../base/10x-Visium.json"])
    with pytest.raises(SnapshotError):
        store.capture("curate.remove", preimage_paths=["ghost.json"])


def test_cli_dry_run_and_apply(proj, capsys):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "upload_b.json").write_text('{"records": []}', encoding="utf-8")
    store.finalize(sid)
    rc = main(["--root", str(proj), "--snapshot", sid])
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["recycle_created"] == ["upload_b.json"]
    assert (_ext(proj) / "upload_b.json").is_file()   # dry-run 零副作用
    rc = main(["--root", str(proj), "--snapshot", sid, "--apply"])
    assert rc == 0
    assert not (_ext(proj) / "upload_b.json").is_file()
    rc = main(["--root", str(proj), "--snapshot", "ghost"])
    assert rc == 2
    assert "回退失败" in capsys.readouterr().err


def test_cli_apply_with_unrestorable_exits_nonzero(proj, capsys):
    store = SnapshotStore(proj)
    sid = store.capture("curate.search_online")
    (_ext(proj) / "keep.json").write_text('{"records": [{"id": 4}]}', encoding="utf-8")
    store.finalize(sid)
    rc = main(["--root", str(proj), "--snapshot", sid, "--apply"])
    assert rc == 1                                     # 有 fail-closed 拒动项：如实非零
    assert "无 preimage" in capsys.readouterr().err
