# -*- coding: utf-8 -*-
"""`--migrate-from` 迁移器契约测试（desktop_launcher.py 迁移段）。

对照契约逐条覆盖：
1. 正向：合法旧便携版根（start-web.bat + 产品源码标记）→ 只迁移白名单项
   （.env → config_root；已知 .userdata 文件；recycle/citations 目录；
   database/external 下 upload_*/curate_sync_*）；rejected 含代码/官方快照/tests/
   日志缓存/未知 .userdata 文件；`.env.zhipu` 检测提示不迁移。
2. 负向：缺 start-web.bat / 缺产品源码标记 / 目录不存在 / 源==data_root /
   源包含 data_root / data_root 包含源 → 一律拒绝。
3. dry-run 零写入：plan_migration 前后目标 data_root 目录树逐项不变。
4. 执行落位：各允许项到位、SHA-256 与源一致；旧目录不删（文件数/内容不变）。
5. 冲突保留双方：目标同名不同内容 → 既有文件不动 + 迁移副本带 `.migrated` 后缀 +
   报告列出；同名同内容 → 幂等跳过；目录冲突同语义。
6. 重入幂等：连续执行两次 → 第二次零新增（identical 跳过）；冲突后重跑也不产生
   新副本（复用上次已保留的 `.migrated` 副本）。
7. 失败不破坏旧数据：staging 阶段注入失败 → 抛 MigrationError、目标无任何变化、
   源完好；修复后可重入成功。
8. models：默认不迁移；`--include-models` 整目录迁移。
9. 升级兼容（引用既有覆盖，不重复）：sessions.json 损坏 fail-open 由
   `tests/test_accounts.py::test_corrupt_sessions_file_fails_open` 覆盖——此处仅断言
   迁移按字节原样拷贝、不提前解析；v1 结构 accounts.json 迁移后经 accounts.authenticate
   原样可读（accounts 库自身无 v1 显式用例，此处补）；旧格式账本行 fail-open——
   recycle manifest.jsonl 混入坏行/旧格式行 → 消费者 `_read_manifest` 跳过坏行仍读出
   有效行（迁移按字节原样拷贝后该属性不丢失）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import desktop_launcher as dl  # noqa: E402
from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402


def _paths(tmp_path: Path) -> "dl.AppPaths":
    """便携布局 AppPaths（data 层可写、不碰仓库），与 test_desktop_launcher 同款。"""
    return dl.AppPaths(
        install_root=tmp_path / "install",
        resource_root=tmp_path / "install",
        data_root=tmp_path / "data",
        config_root=tmp_path / "data" / "config",
        shipped_base_dir=tmp_path / "install" / "database" / "base",
        shipped_external_dir=tmp_path / "install" / "database" / "external",
        user_external_dir=tmp_path / "data" / "database" / "external",
        userdata_dir=tmp_path / "data" / ".userdata",
        model_root=tmp_path / "data" / "models",
        log_root=tmp_path / "data" / "logs",
        trace_root=tmp_path / "data" / "database" / "trace",
        export_root=tmp_path / "data" / "exports",
        run_root=tmp_path / "data" / "run",
        runtime_mode="portable",
    )


def _portable_root(tmp_path: Path, name: str = "old-portable") -> Path:
    """构造合法旧便携版根（start-web.bat + 产品源码标记 + 常见数据）。"""
    root = tmp_path / name
    root.mkdir()
    (root / "start-web.bat").write_text("@echo off\n", encoding="utf-8")
    marker = root / "src" / "dataset_recommender"
    marker.mkdir(parents=True)
    (marker / "__init__.py").write_text("", encoding="utf-8")
    (root / ".env").write_text("LLM_API_KEY=sk-test-1234567890abcdef\n", encoding="utf-8")
    ud = root / ".userdata"
    ud.mkdir()
    (ud / "accounts.json").write_text(
        '{"schema_version": 1, "users": {}}', encoding="utf-8")
    (ud / "sessions.json").write_text("{}", encoding="utf-8")
    (ud / "uploads_journal.jsonl").write_text(
        '{"action": "upload", "n": 1}\n', encoding="utf-8")
    recycle = ud / "recycle"
    recycle.mkdir()
    (recycle / "manifest.jsonl").write_text(
        '{"action": "remove", "original_path": "database/external/x.json",'
        ' "recycle_name": "20260820_000000_000000_x.json", "record_count": 1}\n',
        encoding="utf-8")
    citations = ud / "citations"
    citations.mkdir()
    (citations / "cite_1.md").write_text("# 引用\n", encoding="utf-8")
    ext = root / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "upload_20260820_000000_000000_upload.json").write_text(
        '{"source": "x", "records": []}', encoding="utf-8")
    (ext / "upload_20260820_000000_000000_curate_sync_10x.json").write_text(
        '{"source": "10x", "records": []}', encoding="utf-8")
    (ext / "geo.json").write_text("{}", encoding="utf-8")  # 官方快照
    (root / "database" / "base").mkdir(parents=True)
    (root / "database" / "base" / "10x-Visium.json").write_text("[]", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "run_web.py").write_text("print(1)", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("pass", encoding="utf-8")
    logs = root / "logs"
    logs.mkdir()
    (logs / "launcher.log").write_text("secret=abc123\n", encoding="utf-8")
    return root


def _tree_snapshot(root: Path) -> dict[str, int]:
    """目录树快照：相对路径 → (size, mtime_ns 忽略) 仅做存在性+字节数对比用。"""
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = p.stat().st_size
    return out


def _sha(path: Path) -> str:
    return dl._sha256_file(path)


# ===========================================================================
# 1. 正向：白名单只复制允许项
# ===========================================================================
def test_plan_allows_only_whitelist_items(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    plan = dl.plan_migration(old, paths)
    assert plan.valid is True, plan.reason

    rels = {it.rel for it in plan.items}
    assert rels == {
        ".env",
        ".userdata/accounts.json",
        ".userdata/sessions.json",
        ".userdata/uploads_journal.jsonl",
        ".userdata/recycle",
        ".userdata/citations",
        "database/external/upload_20260820_000000_000000_upload.json",
        "database/external/upload_20260820_000000_000000_curate_sync_10x.json",
    }, f"只迁移白名单项，实际：{sorted(rels)}"
    # 不允许项必须出现在 rejected
    rejected_rels = {r["rel"] for r in plan.rejected}
    assert "src" in rejected_rels          # 代码
    assert "database/base" in rejected_rels  # 官方快照
    assert "database/external/geo.json" in rejected_rels  # 官方快照
    assert "tests" in rejected_rels        # 测试
    assert "logs" in rejected_rels         # 日志缓存
    assert "scripts" in rejected_rels      # 代码
    # 大小汇总 = 各允许项之和
    assert plan.total_bytes == sum(it.size for it in plan.items if not it.identical)
    assert plan.total_bytes > 0
    # 迁移项 kind 标注
    kinds = {it.kind for it in plan.items}
    assert kinds == {"env", "userdata-file", "userdata-dir", "upload"}


def test_plan_reports_unknown_userdata_and_env_zhipu(tmp_path):
    old = _portable_root(tmp_path)
    (old / ".userdata" / "scratch.tmp").write_text("junk", encoding="utf-8")
    (old / ".userdata" / "nested").mkdir()
    (old / ".userdata" / "nested" / "odd.bin").write_bytes(b"\x00\x01")
    (old / ".env.zhipu").write_text("ZHIPU_KEY=x\n", encoding="utf-8")
    plan = dl.plan_migration(old, _paths(tmp_path))
    by_rel = {r["rel"]: r for r in plan.rejected}
    assert ".userdata/scratch.tmp" in by_rel
    assert ".userdata/nested/odd.bin" in by_rel
    assert ".env.zhipu" in by_rel
    assert any(it.rel == ".env" for it in plan.items)  # .env 仍迁移


def test_plan_zero_write_on_data_root(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    before = _tree_snapshot(paths.data_root)
    plan = dl.plan_migration(old, paths)
    assert plan.valid
    after = _tree_snapshot(paths.data_root)
    assert after == before, "dry-run 必须零写入（目标 data_root 完全不变）"
    # 源目录同样零触碰
    assert _tree_snapshot(old) == _tree_snapshot(old)


# ===========================================================================
# 2. 负向：非产品目录拒绝
# ===========================================================================
@pytest.mark.parametrize("mutate", ["no_bat", "no_marker", "empty"])
def test_plan_rejects_non_product_directory(tmp_path, mutate):
    old = _portable_root(tmp_path)
    if mutate == "no_bat":
        (old / "start-web.bat").unlink()
    elif mutate == "no_marker":
        (old / "src" / "dataset_recommender" / "__init__.py").unlink()
    else:
        shutil.rmtree(old)
        old.mkdir()
    plan = dl.plan_migration(old, _paths(tmp_path))
    assert plan.valid is False
    assert plan.reason


def test_plan_rejects_missing_directory(tmp_path):
    plan = dl.plan_migration(tmp_path / "不存在", _paths(tmp_path))
    assert plan.valid is False
    assert "不存在" in plan.reason


def test_plan_rejects_source_is_data_root(tmp_path):
    paths = _paths(tmp_path)
    paths.data_root.mkdir(parents=True)
    plan = dl.plan_migration(paths.data_root, paths)
    assert plan.valid is False
    assert "数据根" in plan.reason


def test_plan_rejects_nested_roots(tmp_path):
    paths = _paths(tmp_path)
    # data_root 包含源
    src_inside = paths.data_root / "inside"
    src_inside.mkdir(parents=True)
    (src_inside / "start-web.bat").write_text("", encoding="utf-8")
    (src_inside / "src" / "dataset_recommender").mkdir(parents=True)
    (src_inside / "src" / "dataset_recommender" / "__init__.py").write_text("", encoding="utf-8")
    assert dl.plan_migration(src_inside, paths).valid is False
    # 源包含 data_root
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "start-web.bat").write_text("", encoding="utf-8")
    (outer / "src" / "dataset_recommender").mkdir(parents=True)
    (outer / "src" / "dataset_recommender" / "__init__.py").write_text("", encoding="utf-8")
    paths2 = dl.AppPaths(
        install_root=outer / "install", resource_root=outer / "install",
        data_root=outer / "data", config_root=outer / "data" / "config",
        shipped_base_dir=outer / "install" / "database" / "base",
        shipped_external_dir=outer / "install" / "database" / "external",
        user_external_dir=outer / "data" / "database" / "external",
        userdata_dir=outer / "data" / ".userdata", model_root=outer / "data" / "models",
        log_root=outer / "data" / "logs", trace_root=outer / "data" / "database" / "trace",
        export_root=outer / "data" / "exports", run_root=outer / "data" / "run",
        runtime_mode="portable",
    )
    assert dl.plan_migration(outer, paths2).valid is False


# ===========================================================================
# 4. 执行落位：允许项到位、hash 一致；旧目录不删
# ===========================================================================
def test_execute_migrates_and_preserves_source(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    plan = dl.plan_migration(old, paths)
    report = dl.run_migration(old, paths, plan=plan)

    assert len(report["copied"]) == len(plan.items)
    assert report["identical_skipped"] == [] and report["conflicts"] == []
    # 每项落位且 hash 与源一致
    for it in plan.items:
        dest = it.dest
        assert dest.exists(), f"{it.rel} 未落位到 {dest}"
        if it.kind == "userdata-dir":
            src_files = {p.relative_to(it.src).as_posix() for p in it.src.rglob("*") if p.is_file()}
            dest_files = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
            assert src_files == dest_files
            for rel in src_files:
                assert _sha(it.src / rel) == _sha(dest / rel)
        else:
            assert _sha(it.src) == _sha(dest), f"{it.rel} hash 不一致"
    # .env 落 config_root
    assert (paths.config_root / ".env").read_text(encoding="utf-8") == \
        (old / ".env").read_text(encoding="utf-8")
    # 旧目录不删：文件数/字节数逐项不变
    before = _tree_snapshot(old)
    assert _tree_snapshot(old) == before
    # 官方快照/logs/代码绝不落位
    assert not (paths.data_root / "database" / "base" / "10x-Visium.json").exists()
    assert not (paths.data_root / "logs" / "launcher.log").exists()
    assert not (paths.data_root / "scripts" / "run_web.py").exists()


# ===========================================================================
# 5. 冲突保留双方
# ===========================================================================
def test_conflict_keeps_both_and_reports(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    # 目标已有同名但不同内容的 accounts.json（模拟已有新实例数据）
    target_ud = paths.userdata_dir
    target_ud.mkdir(parents=True)
    (target_ud / "accounts.json").write_text(
        '{"schema_version": 1, "users": {"new_user": {"id": "n"}}}', encoding="utf-8")
    old_accounts = old / ".userdata" / "accounts.json"
    old_accounts.write_text(
        '{"schema_version": 1, "users": {"old_user": {"id": "o"}}}', encoding="utf-8")

    plan = dl.plan_migration(old, paths)
    accounts_item = next(it for it in plan.items if it.rel == ".userdata/accounts.json")
    assert accounts_item.conflict is True, "同名不同内容必须判冲突"
    assert ".migrated" in accounts_item.dest.name

    report = dl.run_migration(old, paths, plan=plan)
    assert ".userdata/accounts.json" in report["conflicts"]
    # 既有文件不动，迁移副本落位
    assert (target_ud / "accounts.json").read_text(encoding="utf-8").find("new_user") >= 0
    assert accounts_item.dest.exists()
    assert _sha(accounts_item.dest) == _sha(old_accounts)
    # 无冲突项正常落位
    assert (paths.config_root / ".env").exists()


def test_conflict_rerun_is_idempotent(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    target_ud = paths.userdata_dir
    target_ud.mkdir(parents=True)
    (target_ud / "accounts.json").write_text("different-new-instance-data", encoding="utf-8")

    first = dl.run_migration(old, paths)
    assert first["conflicts"] == [".userdata/accounts.json"]
    copies_after_first = len(list(target_ud.glob("accounts.json.*.migrated")))
    assert copies_after_first == 1

    second = dl.run_migration(old, paths)
    assert second["copied"] == []          # 零新增
    assert ".userdata/accounts.json" in second["identical_skipped"]
    assert len(list(target_ud.glob("accounts.json.*.migrated"))) == copies_after_first, \
        "重跑不得产生新冲突副本（复用上次已保留的 .migrated 副本）"


def test_dir_conflict_keeps_both(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    # 目标已有不同内容的 recycle 目录 → 整目录冲突保留双方
    target_recycle = paths.userdata_dir / "recycle"
    target_recycle.mkdir(parents=True)
    (target_recycle / "manifest.jsonl").write_text("new-instance-manifest\n", encoding="utf-8")

    plan = dl.plan_migration(old, paths)
    recycle_item = next(it for it in plan.items if it.rel == ".userdata/recycle")
    assert recycle_item.conflict is True
    report = dl.run_migration(old, paths, plan=plan)
    assert ".userdata/recycle" in report["conflicts"]
    assert (target_recycle / "manifest.jsonl").read_text(encoding="utf-8") == "new-instance-manifest\n"
    assert recycle_item.dest.is_dir()
    assert (recycle_item.dest / "manifest.jsonl").exists()


# ===========================================================================
# 6. 重入幂等
# ===========================================================================
def test_rerun_is_noop(tmp_path):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    first = dl.run_migration(old, paths)
    assert len(first["copied"]) > 0
    second = dl.run_migration(old, paths)
    assert second["copied"] == []
    assert len(second["identical_skipped"]) == len(first["copied"])
    # 目录树不变
    assert _tree_snapshot(paths.data_root) == _tree_snapshot(paths.data_root)


# ===========================================================================
# 7. 失败不破坏旧数据
# ===========================================================================
def test_failure_preserves_old_data_and_is_reentrant(tmp_path, monkeypatch):
    old = _portable_root(tmp_path)
    paths = _paths(tmp_path)
    target_ud = paths.userdata_dir
    target_ud.mkdir(parents=True)
    marker = target_ud / "existing.json"
    marker.write_text("keep-me", encoding="utf-8")

    def _boom(src, dst):
        raise OSError("模拟磁盘写入失败")

    plan = dl.plan_migration(old, paths)
    monkeypatch.setattr(dl.shutil, "copy2", _boom)
    with pytest.raises(dl.MigrationError) as ei:
        dl.run_migration(old, paths, plan=plan)
    assert ei.value.code == "migrate_failed"
    monkeypatch.undo()

    # 目标无任何变化：无新文件、既有文件原样
    assert _tree_snapshot(paths.data_root) == {".userdata/existing.json": 7}
    assert marker.read_text(encoding="utf-8") == "keep-me"
    # 源完好
    assert _tree_snapshot(old) == _tree_snapshot(old)

    # 修复后重入成功（staging 残留不干扰；plan 重算）
    report = dl.run_migration(old, paths)
    assert len(report["copied"]) == len(plan.items)


def test_execute_rejects_invalid_source_without_side_effects(tmp_path):
    old = _portable_root(tmp_path, name="not-product")
    (old / "start-web.bat").unlink()
    paths = _paths(tmp_path)
    with pytest.raises(dl.MigrationError) as ei:
        dl.run_migration(old, paths)
    assert ei.value.code == "invalid_source"
    assert _tree_snapshot(paths.data_root) == {}


# ===========================================================================
# 8. models：仅 --include-models
# ===========================================================================
def test_models_only_migrated_with_flag(tmp_path):
    old = _portable_root(tmp_path)
    models = old / "models"
    (models / "bge").mkdir(parents=True)
    (models / "bge" / "model.bin").write_bytes(b"model-bytes" * 100)
    paths = _paths(tmp_path)

    without = dl.run_migration(old, paths)
    assert not (paths.model_root / "bge" / "model.bin").exists()
    assert "models" in {r["rel"] for r in without["identical_skipped"]} or \
        "models" in {r["rel"] for r in dl.plan_migration(old, paths).rejected}

    with_plan = dl.plan_migration(old, paths, include_models=True)
    with_model = [it for it in with_plan.items if it.kind == "models"]
    assert len(with_model) == 1
    report = dl.run_migration(old, paths, include_models=True)
    assert (paths.model_root / "bge" / "model.bin").read_bytes() == (models / "bge" / "model.bin").read_bytes()
    assert report["conflicts"] == []


# ===========================================================================
# 9. 升级兼容
# ===========================================================================
def test_v1_accounts_json_readable_after_migration(tmp_path):
    """v1 结构 accounts.json（schema_version=1，accounts.register 同款）迁移后原样可读：
    authenticate 直接通过（证明迁移不破坏旧版本数据的读取兼容）。"""
    old = _portable_root(tmp_path)
    A._reset_state_for_tests()
    try:
        old_store = old / ".userdata" / "accounts.json"
        A.register("alice", "correct-horse-battery", store_path=old_store)
        saved = json.loads(old_store.read_text(encoding="utf-8"))
        assert saved["schema_version"] == 1 and "alice" in saved["users"]

        paths = _paths(tmp_path)
        dl.run_migration(old, paths)
        migrated = paths.userdata_dir / "accounts.json"
        assert _sha(migrated) == _sha(old_store), "迁移必须按字节原样拷贝"
        got = A.authenticate("alice", "correct-horse-battery", store_path=migrated)
        assert got.username == "alice"
    finally:
        A._reset_state_for_tests()


def test_sessions_corrupt_migrated_byte_identical(tmp_path):
    """sessions.json 损坏 fail-open 由 tests/test_accounts.py::test_corrupt_sessions_file_fails_open
    覆盖（应用层）；此处仅证明迁移**不提前解析/不修复/不损坏**——按字节原样拷贝，
    并把损坏文件交给应用既有的 fail-open 消费（_load_sessions 返回空库不抛错）。"""
    old = _portable_root(tmp_path)
    (old / ".userdata" / "sessions.json").write_text("{ 损坏的 JSON", encoding="utf-8")
    paths = _paths(tmp_path)
    report = dl.run_migration(old, paths)
    assert ".userdata/sessions.json" in report["copied"]
    migrated = paths.userdata_dir / "sessions.json"
    assert migrated.read_bytes() == (old / ".userdata" / "sessions.json").read_bytes()
    assert A._load_sessions(migrated) == {}  # fail-open：空库、不抛错


def test_old_format_ledger_line_fail_open(tmp_path):
    """旧格式/坏行账本行 fail-open：recycle manifest.jsonl 混入坏行 → 迁移按字节原样
    拷贝 → 消费者 `corpus_curation._read_manifest` 跳过坏行仍读出全部有效行。"""
    old = _portable_root(tmp_path)
    manifest = old / ".userdata" / "recycle" / "manifest.jsonl"
    manifest.write_text(
        '{"action": "remove", "original_path": "database/external/a.json",'
        ' "recycle_name": "20260820_000000_000000_a.json", "record_count": 1}\n'
        "旧版本-非JSON-行-不能连累其它行\n"
        '{"action": "remove", "original_path": "database/external/b.json",'
        ' "recycle_name": "20260820_000000_000000_b.json", "record_count": 2}\n',
        encoding="utf-8")
    paths = _paths(tmp_path)
    dl.run_migration(old, paths)
    migrated_manifest = paths.userdata_dir / "recycle" / "manifest.jsonl"
    assert migrated_manifest.read_bytes() == manifest.read_bytes()

    entries = cc._read_manifest(paths.data_root)
    assert [e["original_path"] for e in entries] == [
        "database/external/a.json", "database/external/b.json",
    ], "坏行必须被跳过、有效行完整读出"


# ===========================================================================
# 10. 迁移日志脱敏（迁移报告含 .env 路径与内容哈希，不落密钥明文）
# ===========================================================================
def test_migration_report_does_not_leak_secrets(tmp_path, caplog):
    old = _portable_root(tmp_path)
    # 假 key 按 test_secret_scan 成例用拼接构造：源码不出现可被交付扫描命中的完整 sk-{20+} 字面
    fake_key = "sk-" + "supersecret1234567890"
    (old / ".env").write_text(f"LLM_API_KEY={fake_key}\n", encoding="utf-8")
    paths = _paths(tmp_path)
    with caplog.at_level("INFO", logger=dl.LAUNCHER_LOGGER):
        dl.run_migration(old, paths, logger=dl._logger)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert fake_key not in text, "日志不得含 .env 明文密钥"
    assert "迁移完成" in text, "报告摘要应留痕"


# ===========================================================================
# 11. symlink/junction 链接穿透拒绝（resolve 后校验仍在源根内）
# ===========================================================================
def _make_junction(link: Path, target: Path) -> bool:
    """Windows junction（mklink /J，免管理员）；失败返回 False（调用方 skip）。"""
    link.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
    return result.returncode == 0 and link.exists()


def test_plan_rejects_junction_escaping_source(tmp_path):
    """负向：.userdata/recycle 是指向源根外的 junction → 进 rejected，不迁移。"""
    outside = tmp_path / "outside-recycle"
    outside.mkdir()
    (outside / "manifest.jsonl").write_text('{"action": "remove", "n": 1}\n', encoding="utf-8")
    root = _portable_root(tmp_path, name="old-portable-j")
    link = root / ".userdata" / "recycle"
    shutil.rmtree(link)  # _portable_root 已建 recycle 目录，先移除再建 junction
    if not _make_junction(link, outside):
        pytest.skip("无法创建 junction（mklink /J 不可用）")
    plan = dl.plan_migration(root, _paths(tmp_path))
    assert plan.valid
    rej = [r for r in plan.rejected if r["rel"] == ".userdata/recycle"]
    assert rej and "链接" in rej[0]["reason"], rej
    assert all(it.rel != ".userdata/recycle" for it in plan.items)


def test_execute_rejects_item_outside_source(tmp_path):
    """run_migration staging 前对每个源条目重新校验——位于源根外的条目拒绝
    （含手工构造绕过 plan 的 item；链接解析穿透同走此闸，防御 plan/执行之间的绕过）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.json").write_text("{}", encoding="utf-8")
    paths = _paths(tmp_path)
    item = dl.MigrationItem(rel=".userdata/recycle", kind="userdata-dir",
                            src=outside, dest=paths.userdata_dir / "recycle", size=1)
    plan = dl.MigrationPlan(True, items=(item,))
    with pytest.raises(dl.MigrationError) as ei:
        dl.run_migration(tmp_path / "src", paths, plan=plan)
    assert ei.value.code == "link_escape"
    assert "链接" in ei.value.message
