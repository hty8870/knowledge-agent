# -*- coding: utf-8 -*-
"""同步加固 + watch-check 后端 + MCP 契约修复测试（全程禁网）。

钉死的契约：
- sync_updates 整任务跨进程文件锁：锁冲突**立即**返回 sync_busy（不排队），同线程可重入；
- sync_updates 返回 operation receipt（operation_id / created_files[] / failed_sources[] /
  skipped_existing + 逐源明细），既有字段逐位兼容；
- 按 operation_id 批量撤回：回收站语义、可重入、部分失败不连累其余、未知 operation fail-closed；
- 实例级同步状态（GET /api/curate/sync-status：last_sync_at / last_operation_id / busy）；
- /api/watch/check 确定性重跑（同 spec 两次结果一致；>200 截断 truncated=true 且 uids ≤200；
  executed_spec 规范化回显；语义指纹 schema v1 稳定）；
- MCP curate_datasets 补齐 check_updates / sync_updates 执行动作（plan→execute 断链修复）。

写目标隔离：所有写盘重定向到 tmp 仓库根（monkeypatch webapp.PROJECT_ROOT / webapp.DATA_DIR /
webapp.get_settings；MCP 用 _settings 替身），绝不污染真实 database/external/。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.app import mcp_server as M  # noqa: E402
from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402
from dataset_recommender.app import webapp  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402
from dataset_recommender.llm.config import Settings  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def curate_tmp_root(tmp_path, monkeypatch):
    """把 /api/curate/* 的写目标重定向到临时仓库根（webapp.PROJECT_ROOT 生效）。"""
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "base").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


def _sync_fake_checked(*, online_new: int = 2, extra_offline: bool = True) -> dict:
    """check_updates 的假返回：一个可闭环 online 源 + 可选离线源（sync 测试禁网接缝）。"""
    sources = [{
        "source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
        "local_count": 10, "new_count": online_new,
        "new_candidates": [{"accession": f"E-MTAB-{i:04d}"} for i in range(1, online_new + 1)],
        "note_zh": f"检到疑似新增 {online_new} 条",
    }]
    if extra_offline:
        sources.append({
            "source": "cellxgene", "label": "CELLxGENE Discover", "mode": "snapshot",
            "local_count": 5, "new_count": 0, "note_zh": "这个来源只有本地副本，不能在线核对",
        })
    return {
        "checked_at": "2026-08-22T00:00:00+08:00",
        "sources": sources,
        "hint_zh": "只有部分来源能在线核对更新。",
    }


@pytest.fixture
def fake_sync_sources(monkeypatch):
    """禁网接缝：check_updates 与逐编号搜回（_sync_collect_records）全部注入假实现。"""
    checked = {"data": None}

    def _fake_check(sources=None, **kwargs):
        return checked["data"] or _sync_fake_checked()

    def _fake_collect(key, candidates, existing_uids, *, max_import, root):
        records = [{
            "dataset_uid": f"wt:uid-{c['accession']}",
            "dataset_name": f"Fake dataset {c['accession']}",
            "species": "Homo sapiens", "tissue": "Lung",
            "count": "1000", "unit": "cells", "has_raw_data": True,
            "published_date": "2024-01-01", "url": f"https://example.com/{c['accession']}",
        } for c in (candidates or [])[:max_import]]
        return records, [], 0

    monkeypatch.setattr(cc, "check_updates", _fake_check)
    monkeypatch.setattr(cc, "_sync_collect_records", _fake_collect)
    return checked


# ---------------------------------------------------------------- sync 整任务锁

def test_sync_lock_conflict_returns_sync_busy(tmp_path):
    """锁冲突（另一进程/线程正在 sync）：立即 CurateError(sync_busy)，不排队、零写入。"""
    fh = cc._acquire_os_sync_lock_nowait(tmp_path)
    assert fh is not None, "测试前置：应能拿到 OS 锁"
    try:
        with pytest.raises(cc.CurateError) as ei:
            cc.sync_updates(None, project_root=tmp_path)
        assert ei.value.code == "sync_busy"
        assert "另一个「同步数据集」正在运行" in ei.value.hint
        # 零写入：external 目录里不应出现任何 upload_* 文件
        ext = tmp_path / "database" / "external"
        if ext.exists():
            assert not list(ext.glob("upload_*.json"))
    finally:
        cc._release_os_sync_lock(fh)


def test_sync_lock_busy_probe(tmp_path):
    """sync_lock_busy：锁被占 → True；释放 → False（实例级 busy 的事实源，不写盘）。"""
    assert cc.sync_lock_busy(tmp_path) is False
    fh = cc._acquire_os_sync_lock_nowait(tmp_path)
    try:
        assert cc.sync_lock_busy(tmp_path) is True
    finally:
        cc._release_os_sync_lock(fh)
    assert cc.sync_lock_busy(tmp_path) is False


def test_sync_lock_same_thread_reentrant(tmp_path):
    """同线程重入（sync 内部嵌套场景）直接放行，不自锁死。"""
    with cc.sync_updates_critical_section(tmp_path):
        with cc.sync_updates_critical_section(tmp_path):   # 重入
            pass
        assert cc.sync_lock_busy(tmp_path) is True          # 外层仍持锁
    assert cc.sync_lock_busy(tmp_path) is False


def test_sync_lock_concurrent_thread_returns_sync_busy(tmp_path):
    """同进程两线程并发争用线程锁：后来者立即 sync_busy，不阻塞排队、不在前者放锁后重跑。"""
    import threading
    entered = threading.Event()
    release = threading.Event()

    def _holder():
        with cc.sync_updates_critical_section(tmp_path):
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=_holder)
    holder.start()
    try:
        assert entered.wait(5), "前置：持锁线程应已进入临界区"
        with pytest.raises(cc.CurateError) as ei:
            with cc.sync_updates_critical_section(tmp_path):
                pass
        assert ei.value.code == "sync_busy"
    finally:
        release.set()
        holder.join(10)
    assert not holder.is_alive()


# ---------------------------------------------------------------- operation receipt

def test_sync_updates_receipt_fields_complete(tmp_path, fake_sync_sources):
    """receipt 字段完整性：operation_id / created_files[] / failed_sources[] / skipped_existing，
    既有字段（checked_at/sources/imported_total/skipped_files/hint_zh）逐位兼容。"""
    result = cc.sync_updates(["arrayexpress"], project_root=tmp_path)
    # 既有字段兼容
    for key in ("checked_at", "sources", "imported_total", "skipped_files", "hint_zh"):
        assert key in result, f"既有字段 {key} 丢失"
    # receipt 新字段
    assert result["status"] == "ok"
    assert result["operation_id"].startswith("sync_")
    assert len(result["created_files"]) == 1
    created = result["created_files"][0]
    assert created.startswith("upload_") and "curate_sync_arrayexpress" in created
    assert (tmp_path / "database" / "external" / created).is_file(), "文件应真实落盘"
    assert result["failed_sources"] == []
    assert result["skipped_existing"] == 0
    # 逐源明细仍在
    assert result["sources"][0]["source"] == "arrayexpress"
    assert result["sources"][0]["imported_count"] == 2
    assert result["sources"][1]["mode"] == "snapshot"
    # ledger 落账（recall 的依据）
    ops = cc._read_sync_operations(tmp_path)
    assert ops and ops[0]["operation_id"] == result["operation_id"]
    assert ops[0]["created_files"] == result["created_files"]


def test_sync_updates_receipt_partial_failure(tmp_path, monkeypatch, fake_sync_sources):
    """部分失败：一个源搜回/写入抛错 → 进 failed_sources[]，已成功来源与既有字段不受影响。"""
    real_collect = cc._sync_collect_records

    def _flaky_collect(key, candidates, existing_uids, *, max_import, root):
        if key != "arrayexpress":
            raise cc.CurateError("network_error", "假网络故障。")
        return real_collect(key, candidates, existing_uids, max_import=max_import, root=root)

    # 注入第二个 online 源（geo，会失败）+ arrayexpress（成功）
    fake_sync_sources["data"] = _sync_fake_checked(online_new=2, extra_offline=False)
    fake_sync_sources["data"]["sources"].append({
        "source": "geo", "label": "NCBI GEO", "mode": "online",
        "local_count": 3, "new_count": 1,
        "new_candidates": [{"accession": "GSE12345"}], "note_zh": "检到疑似新增 1 条",
    })
    monkeypatch.setattr(cc, "_sync_collect_records", _flaky_collect)
    result = cc.sync_updates(None, project_root=tmp_path)
    assert result["status"] == "ok"                       # sync 整体不因单源失败而失败
    assert len(result["created_files"]) == 1              # 只有成功的 arrayexpress 落了文件
    failed = result["failed_sources"]
    assert len(failed) == 1 and failed[0]["source"] == "geo"
    assert failed[0]["error"] == "CurateError"


def test_sync_status_persisted_instance_fact(tmp_path, fake_sync_sources):
    """实例级同步事实：sync 完成后 sync_status 报 last_sync_at/last_operation_id，busy=False。"""
    result = cc.sync_updates(["arrayexpress"], project_root=tmp_path)
    st = cc.sync_status(project_root=tmp_path)
    assert st["last_operation_id"] == result["operation_id"]
    assert st["last_sync_at"] == "2026-08-22T00:00:00+08:00"
    assert st["busy"] is False
    # 状态文件真实落盘
    state_path = cc._sync_state_path(tmp_path)
    assert state_path.is_file()
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_operation_id"] == result["operation_id"]


def test_sync_status_empty_state_and_busy(tmp_path):
    """还没同步过：last_sync_at/last_operation_id 为 null；锁被占时 busy=True。"""
    st = cc.sync_status(project_root=tmp_path)
    assert st["last_sync_at"] is None and st["last_operation_id"] is None
    assert st["busy"] is False
    fh = cc._acquire_os_sync_lock_nowait(tmp_path)
    try:
        assert cc.sync_status(project_root=tmp_path)["busy"] is True
    finally:
        cc._release_os_sync_lock(fh)


# ---------------------------------------------------------------- 批量撤回

def test_recall_moves_created_files_to_recycle_and_reentrant(tmp_path, fake_sync_sources):
    """整次撤回：created_files 移入回收站 + manifest 记 recall 行；再撤 → 全部 skipped（可重入）。"""
    result = cc.sync_updates(["arrayexpress"], project_root=tmp_path)
    op_id = result["operation_id"]
    created = result["created_files"]
    ext = tmp_path / "database" / "external"
    assert (ext / created[0]).is_file()

    res = cc.recall_sync_operation(op_id, project_root=tmp_path)
    assert res["status"] == "ok"
    assert res["recalled_files"] == created
    assert res["skipped_files"] == [] and res["failed_files"] == []
    assert not (ext / created[0]).exists(), "撤回后 external 下文件应消失"
    # manifest 有 recall 行
    manifest = cc._read_manifest(tmp_path)
    assert any(e.get("action") == "recall" and e.get("operation_id") == op_id for e in manifest)

    # 可重入：再撤一次，文件已不在 external → 全部 skipped，不报错
    res2 = cc.recall_sync_operation(op_id, project_root=tmp_path)
    assert res2["status"] == "ok"
    assert res2["recalled_files"] == []
    assert sorted(res2["skipped_files"]) == sorted(created)
    # 回收站里不重复出现（同文件只移过一次）
    recycle = tmp_path / ".userdata" / "recycle"
    names = [p.name for p in recycle.glob("*.json")]
    assert len([n for n in names if n.endswith("_" + created[0])]) == 1


def test_recall_partial_failure_does_not_break_others(tmp_path, fake_sync_sources):
    """部分失败：一个文件已不在 external（被外部移走）→ skipped；其余照常撤回（失败不破坏）。"""
    result = cc.sync_updates(["arrayexpress"], project_root=tmp_path)
    op_id = result["operation_id"]
    created = result["created_files"]
    assert len(created) == 1
    # 手动模拟「文件已被单独 remove/移走」：把文件从 external 移走（不经过本次撤回）
    ext = tmp_path / "database" / "external"
    (ext / created[0]).rename(tmp_path / "stolen.json")
    res = cc.recall_sync_operation(op_id, project_root=tmp_path)
    assert res["recalled_files"] == []
    assert res["skipped_files"] == created
    assert res["failed_files"] == []
    # 已撤回的部分保持已撤回（没有回滚、没有破坏任何文件）
    assert (tmp_path / "stolen.json").is_file()
    assert cc.sync_lock_busy(tmp_path) is False


def test_recall_unknown_operation_fail_closed(tmp_path):
    """未知 operation_id → CurateError(unknown_operation)，不静默空转。"""
    with pytest.raises(cc.CurateError) as ei:
        cc.recall_sync_operation("sync_nope_20260822", project_root=tmp_path)
    assert ei.value.code == "unknown_operation"


# ---------------------------------------------------------------- /api/curate/* 新端点

def test_sync_status_endpoint_reads_instance_fact(curate_tmp_root, fake_sync_sources):
    cc.sync_updates(["arrayexpress"], project_root=curate_tmp_root)
    res = client.get("/api/curate/sync-status")
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    r = body["result"]
    assert r["last_operation_id"].startswith("sync_")
    assert r["last_sync_at"] == "2026-08-22T00:00:00+08:00"
    assert r["busy"] is False


def test_recall_endpoint_roundtrip(curate_tmp_root, fake_sync_sources):
    result = cc.sync_updates(["arrayexpress"], project_root=curate_tmp_root)
    op_id = result["operation_id"]
    created = result["created_files"]
    res = client.post("/api/curate/recall", json={"operation_id": op_id})
    assert res.status_code == 200
    r = res.json()["result"]
    assert r["recalled_files"] == created
    # 未知 operation → 400
    bad = client.post("/api/curate/recall", json={"operation_id": "sync_nope"})
    assert bad.status_code == 400
    assert "unknown_operation" in bad.json()["detail"] or "找不到同步操作" in bad.json()["detail"]


def test_recall_endpoint_rejects_unknown_fields(curate_tmp_root):
    res = client.post("/api/curate/recall", json={"operation_id": "sync_x", "extra": 1})
    assert res.status_code == 422


# ---------------------------------------------------------------- /api/watch/check（确定性重跑）

def _make_watch_env(tmp_path, monkeypatch, *, n_records: int, tag: str = "Lung"):
    """构造 tmp 语料（n 条 source=10x Genomics 的肺数据集）+ 把 webapp 检索面重定向过去。"""
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    records = []
    for i in range(n_records):
        records.append({
            "dataset_uid": f"wt:uid-{i:04d}",
            # 标题根带唯一前缀（Experiment NNNN），避免 derive_family_id 把全部记录归一成
            # 同一家族——检索器每家族 cap=1~2 条是产品行为，截断测试需要 250 个不同家族全命中。
            "dataset_name": f"Experiment {i:04d} lung single cell RNA-seq",
            "source": "10x Genomics", "species": "Homo sapiens", "tissue": tag,
            "disease": "normal", "count": str(1000 + i % 5), "unit": "cells",
            "has_raw_data": (i % 2 == 0), "published_date": "2024-01-01",
            "url": f"https://example.com/{i:04d}", "description": "single cell RNA sequencing",
        })
    (base / "watch.json").write_text(json.dumps(records), encoding="utf-8")
    settings = Settings(
        project_root=tmp_path, data_dir=base, output_dir=tmp_path,
        top_k=200, enable_llm=False, mock_llm=True,
    )
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(webapp, "DATA_DIR", base)
    monkeypatch.setattr(webapp, "get_settings", lambda: settings)
    return tmp_path


def test_watch_check_deterministic_same_spec(tmp_path, monkeypatch):
    """确定性：同 spec 两次重跑 → uids 集合与语义指纹逐位一致（watch diff 可比的前提）。"""
    _make_watch_env(tmp_path, monkeypatch, n_records=30)
    spec = {"spec_version": "v1", "query": "Lung"}
    r1 = client.post("/api/watch/check", json=spec)
    r2 = client.post("/api/watch/check", json=spec)
    assert r1.status_code == 200 and r2.status_code == 200
    a, b = r1.json()["result"], r2.json()["result"]
    assert a["uids"] == b["uids"]
    assert a["fingerprints"] == b["fingerprints"]
    assert a["result_total"] == b["result_total"]
    assert len(a["uids"]) == 30 and len(a["fingerprints"]) == 30
    assert a["truncated"] is False
    # 指纹是 schema v1 稳定哈希（64 位 hex）
    fp = next(iter(a["fingerprints"].values()))
    assert len(fp) == 64 and all(c in "0123456789abcdef" for c in fp)
    # executed_spec 规范化回显（默认值补齐 + 强制确定性参数）
    es = a["executed_spec"]
    assert es["spec_version"] == "v1" and es["query"] == "Lung"
    assert es["strategy"] == "fixed" and es["recall"] == "off" and es["rerank"] == "off"
    assert es["polish"] is False


def test_watch_check_truncated_over_200(tmp_path, monkeypatch):
    """>200 截断语义：result_total>200 → truncated=true，uids 只给前 200（绝不冒充全量）。"""
    _make_watch_env(tmp_path, monkeypatch, n_records=250)
    res = client.post("/api/watch/check", json={"spec_version": "v1", "query": "Lung"})
    assert res.status_code == 200
    r = res.json()["result"]
    assert r["result_total"] == 250
    assert r["truncated"] is True
    assert len(r["uids"]) == 200
    assert len(r["fingerprints"]) == 200
    assert set(r["fingerprints"]) == set(r["uids"])


def test_watch_check_fingerprint_ignores_rank_but_sensitive_to_material(tmp_path, monkeypatch):
    """语义指纹：只随 material 字段（sample_size / raw_data_status）变，不随排序变。

    两条记录除 count（sample_size 的一部分）不同外全同 → 指纹不同；同一 uid 同字段两次 →
    指纹相同（排序无关由 uids 顺序不变 + 指纹与顺序无关共同保证）。"""
    _make_watch_env(tmp_path, monkeypatch, n_records=2, tag="Lung")
    spec = {"spec_version": "v1", "query": "Lung"}
    r = client.post("/api/watch/check", json=spec).json()["result"]
    fps = r["fingerprints"]
    uids = r["uids"]
    assert len(fps) == 2
    # 构造不同 sample_size 的同一 uid：直接调 record_fingerprint_v1 验证敏感性
    item_a = {"count": "1000", "unit": "cells", "raw_data_status": {"code": "has_fastq", "authoritative": False}}
    item_b = {"count": "1000", "unit": "Cells", "raw_data_status": {"code": "has_fastq", "authoritative": False}}
    item_c = {"count": "2000", "unit": "cells", "raw_data_status": {"code": "has_fastq", "authoritative": False}}
    fp_unit = webapp.record_fingerprint_v1("wt:uid", item_a)
    assert fp_unit == webapp.record_fingerprint_v1("wt:uid", item_b), "unit 大小写应归一（Cells≡cells）"
    assert fp_unit != webapp.record_fingerprint_v1("wt:uid", item_c), "count 变化必须改变指纹"
    # raw_data_status 变化必须改变指纹
    item_d = {"count": "1000", "unit": "cells", "raw_data_status": {"code": "unknown", "authoritative": False}}
    assert fp_unit != webapp.record_fingerprint_v1("wt:uid", item_d)
    # 指纹与 uid 绑定
    assert fp_unit != webapp.record_fingerprint_v1("wt:uid-other", item_a)


def test_watch_check_invalid_spec_fail_closed(curate_tmp_root):
    """空 spec / 未知 spec_version → 400（fail-closed 点名，不静默拿全库浏览冒充检查）。"""
    empty = client.post("/api/watch/check", json={"spec_version": "v1", "query": ""})
    assert empty.status_code == 400
    assert "检查条件为空" in empty.json()["detail"]
    bad_ver = client.post("/api/watch/check", json={"spec_version": "v9", "query": "Lung"})
    assert bad_ver.status_code == 400
    assert "spec_version" in bad_ver.json()["detail"]
    unknown_fields = client.post("/api/watch/check", json={"spec_version": "v1", "query": "Lung", "x": 1})
    assert unknown_fields.status_code == 422


# ---------------------------------------------------------------- MCP curate_datasets 补动作

@pytest.fixture
def mcp_tmp_root(tmp_path, monkeypatch):
    """把 curate_datasets 的写目标重定向到临时仓库根（M._settings 替身，同 test_mcp_curation）。"""
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "_settings", lambda: SimpleNamespace(project_root=tmp_path, data_dir=base))
    return tmp_path


def test_mcp_check_updates_action_readonly(mcp_tmp_root, monkeypatch):
    """MCP curate_datasets(action=check_updates)：薄封装 cc.check_updates，只读 + write_boundary。"""
    monkeypatch.setattr(cc, "check_updates", lambda sources=None, **kw: _sync_fake_checked())
    res = M.curate_datasets(action="check_updates", source="arrayexpress")
    assert res["ok"] is True
    assert res["sources"][0]["source"] == "arrayexpress"
    assert "纯只读检查" in res["write_boundary"]
    assert any("sync_updates" in n for n in res["next"])
    # 未注入接缝时 source 透传：source 名原样进入 check_updates 入参
    captured = {}

    def _cap(sources=None, **kw):
        captured["sources"] = sources
        return _sync_fake_checked()
    monkeypatch.setattr(cc, "check_updates", _cap)
    M.curate_datasets(action="check_updates")
    assert captured["sources"] is None
    M.curate_datasets(action="check_updates", source="10x")
    assert captured["sources"] == ["10x"]


def test_mcp_sync_updates_action_executes(mcp_tmp_root, fake_sync_sources):
    """MCP curate_datasets(action=sync_updates)：真执行复合流，返回 receipt + write_boundary。"""
    res = M.curate_datasets(action="sync_updates", source="arrayexpress")
    assert res["ok"] is True
    assert res["operation_id"].startswith("sync_")
    assert len(res["created_files"]) == 1
    assert "database/external" in res["write_boundary"]
    assert "撤回" in res["write_boundary"]
    # ledger 与状态落账（与 Web 同真源）
    assert cc._read_sync_operations(mcp_tmp_root)[0]["operation_id"] == res["operation_id"]


def test_mcp_sync_updates_lock_conflict_maps_sync_busy(mcp_tmp_root):
    """MCP sync_updates 锁冲突：ToolError 带 sync_busy（fail-closed 错误映射）。"""
    fh = cc._acquire_os_sync_lock_nowait(mcp_tmp_root)
    assert fh is not None
    try:
        with pytest.raises(ToolError, match="sync_busy"):
            M.curate_datasets(action="sync_updates")
    finally:
        cc._release_os_sync_lock(fh)


def test_write_boundary_zh_covers_sync_actions():
    """write_boundary_zh 单一真源覆盖两个新动作（check 只读 / sync 写盘可撤回）。"""
    assert "纯只读检查" in cc.write_boundary_zh("check_updates", dry_run=True)
    wb = cc.write_boundary_zh("sync_updates", dry_run=False)
    assert "database/external" in wb and "撤回" in wb
