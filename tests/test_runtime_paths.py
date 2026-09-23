# -*- coding: utf-8 -*-
"""运行时路径解耦（resource/data 双根单一真源）的契约测试。

覆盖（对应设计裁决与向后兼容红线）：
1. 三模式解析：source / portable（env 显式根）/ frozen（sys.frozen + sys._MEIPASS）。
2. env 覆盖优先级：BIODATA_RESOURCE_ROOT/BIODATA_DATA_ROOT 各自独立覆盖 frozen/source 缺省。
3. 向后兼容：source 模式每个子路径 == 仓库根相对的历史绝对路径（逐字节一致）。
4. 双层 external 合并：shipped+user、同文件名去重（user 层优先）、写只落 user 层、
   invalidate_external_cache 语义保持。
5. resource 只读性：frozen（双根分离）布局下写操作不触碰 resource 层。

环境隔离：`get_app_paths` 是进程级缓存单例，本文件每个测试前后清缓存（含
llm.config.get_settings 的 lru_cache），保证 env 改动不跨测试/跨文件泄漏——
其他测试文件默认在 source 模式（env 未设）下运行，行为与历史逐字节一致。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from dataset_recommender.app.runtime_paths import (
    DATA_ROOT_ENV,
    RESOURCE_ROOT_ENV,
    AppPaths,
    get_app_paths,
    instance_data_dir_for,
    reset_app_paths_cache,
    resource_file_for,
)
from dataset_recommender.llm.config import get_settings as _llm_get_settings

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _isolate_runtime_paths_cache():
    """每个测试前后清 runtime_paths 与 get_settings 缓存：env 改动即时生效、不跨测试泄漏。"""
    reset_app_paths_cache()
    _llm_get_settings.cache_clear()
    yield
    reset_app_paths_cache()
    _llm_get_settings.cache_clear()


def _set_split_env(monkeypatch, resource_root: Path, data_root: Path) -> None:
    """模拟「双根分离」布局（frozen 或 portable 显式分根）：resource 只读 + data 可写。"""
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(resource_root))
    monkeypatch.setenv(DATA_ROOT_ENV, str(data_root))


# ---------------------------------------------------------------------------
# 1. 三模式解析
# ---------------------------------------------------------------------------

def test_source_mode_matches_historical_layout():
    """source 模式：全部子路径 == 仓库根相对的历史绝对路径（向后兼容红线，逐字节一致）。"""
    p = get_app_paths()
    assert p.runtime_mode == "source"
    assert p.install_root == _REPO_ROOT
    assert p.resource_root == _REPO_ROOT
    assert p.data_root == _REPO_ROOT
    assert p.config_root == _REPO_ROOT
    assert p.shipped_base_dir == _REPO_ROOT / "database" / "base"
    assert p.shipped_external_dir == _REPO_ROOT / "database" / "external"
    assert p.user_external_dir == _REPO_ROOT / "database" / "external"
    assert p.userdata_dir == _REPO_ROOT / ".userdata"
    assert p.model_root == _REPO_ROOT / "models"
    assert p.log_root == _REPO_ROOT / "logs"
    assert p.trace_root == _REPO_ROOT / "database" / "trace"
    assert p.export_root == _REPO_ROOT / "outputs"
    assert p.run_root == _REPO_ROOT / "run"


def test_portable_mode_single_root_uses_same_relative_layout(tmp_path, monkeypatch):
    """portable 同根：mode=portable，resource == data == 显式根，子路径相对结构不变。"""
    root = tmp_path / "portable-install"
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(root))
    monkeypatch.setenv(DATA_ROOT_ENV, str(root))
    p = get_app_paths()
    assert p.runtime_mode == "portable"
    assert p.resource_root == root and p.data_root == root
    assert p.shipped_base_dir == root / "database" / "base"
    assert p.user_external_dir == root / "database" / "external"
    assert p.userdata_dir == root / ".userdata"
    assert p.model_root == root / "models"
    assert p.export_root == root / "outputs"     # 单根保持历史 outputs 命名
    assert p.config_root == root


def test_frozen_mode_layout(monkeypatch, tmp_path):
    """frozen：install=exe 目录；resource=_MEIPASS；data=LOCALAPPDATA/BioDataAgent；子路径按设计落位。"""
    meipass = tmp_path / "bundle"
    local = tmp_path / "localappdata"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    p = get_app_paths()
    assert p.runtime_mode == "frozen"
    # install_root = exe 所在目录（sys.executable 的父目录，测试进程的 python 真实位置）
    assert p.install_root == Path(sys.executable).resolve().parent
    assert p.resource_root == meipass
    assert p.data_root == local / "BioDataAgent"
    assert p.config_root == p.data_root / "config"        # .env 在这
    assert p.shipped_base_dir == meipass / "database" / "base"
    assert p.shipped_external_dir == meipass / "database" / "external"
    assert p.user_external_dir == p.data_root / "database" / "external"
    assert p.userdata_dir == p.data_root / ".userdata"
    assert p.model_root == p.data_root / "models"
    assert p.log_root == p.data_root / "logs"
    assert p.trace_root == p.data_root / "database" / "trace"
    assert p.export_root == p.data_root / "exports"
    assert p.run_root == p.data_root / "run"


def test_runtime_mode_labels(monkeypatch, tmp_path):
    """三种 runtime_mode 标签互斥且正确。"""
    assert get_app_paths().runtime_mode == "source"
    monkeypatch.setenv(DATA_ROOT_ENV, str(tmp_path / "d"))
    reset_app_paths_cache()
    assert get_app_paths().runtime_mode == "portable"
    monkeypatch.delenv(DATA_ROOT_ENV)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    reset_app_paths_cache()
    assert get_app_paths().runtime_mode == "frozen"


# ---------------------------------------------------------------------------
# 2. env 覆盖优先级
# ---------------------------------------------------------------------------

def test_env_roots_override_frozen_defaults(monkeypatch, tmp_path):
    """frozen 状态下 env 显式根仍然优先（设计裁决：显式环境变量 > frozen 状态）。"""
    env_res = tmp_path / "env-res"
    env_data = tmp_path / "env-data"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "meipass"), raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv(RESOURCE_ROOT_ENV, str(env_res))
    monkeypatch.setenv(DATA_ROOT_ENV, str(env_data))
    p = get_app_paths()
    assert p.resource_root == env_res
    assert p.data_root == env_data
    assert p.shipped_base_dir == env_res / "database" / "base"
    assert p.userdata_dir == env_data / ".userdata"


def test_env_data_root_only_keeps_resource_on_source_root(monkeypatch, tmp_path):
    """只设 BIODATA_DATA_ROOT（数据分离部署）：resource 回落源码项目根，data 显式指定。"""
    data = tmp_path / "data"
    monkeypatch.setenv(DATA_ROOT_ENV, str(data))
    p = get_app_paths()
    assert p.resource_root == _REPO_ROOT
    assert p.data_root == data
    assert p.runtime_mode == "portable"
    assert p.user_external_dir == data / "database" / "external"
    assert p.shipped_external_dir == _REPO_ROOT / "database" / "external"


# ---------------------------------------------------------------------------
# 3. 运行时辅助解析（写盘走 data、读静态资源走 resource）
# ---------------------------------------------------------------------------

def test_instance_data_dir_for_and_resource_file_for(tmp_path, monkeypatch):
    """双根分离：实例根（data_root）→ 写盘落 data 层、读资源落 resource 层；
    非实例根（测试注入/其它安装）→ 根相对，历史逐字节一致。"""
    res = tmp_path / "res"
    data = tmp_path / "data"
    _set_split_env(monkeypatch, res, data)
    p = get_app_paths()
    assert instance_data_dir_for(p.data_root, ".userdata") == data / ".userdata"
    assert instance_data_dir_for(p.data_root, "database/external") == data / "database" / "external"
    assert resource_file_for(p.data_root, "database/base/10x.json") == res / "database" / "base" / "10x.json"
    # 非实例根（比如测试注入的临时根）：保持根相对
    assert instance_data_dir_for(tmp_path / "other", "database/external") == tmp_path / "other" / "database" / "external"
    assert resource_file_for(tmp_path / "other", "database/base/x.json") == tmp_path / "other" / "database" / "base" / "x.json"


def test_split_layout_detection_requires_instance_root(tmp_path, monkeypatch):
    """uses_split_layout 只在「双根分离 且 传入根 == 实例 data_root」时生效。"""
    res = tmp_path / "res"
    data = tmp_path / "data"
    _set_split_env(monkeypatch, res, data)
    from dataset_recommender.app.runtime_paths import uses_split_layout
    assert uses_split_layout(data) is True
    assert uses_split_layout(tmp_path / "elsewhere") is False
    assert uses_split_layout(_REPO_ROOT) is False


# ---------------------------------------------------------------------------
# 4. 向后兼容：source 模式各消费者解析 == 历史绝对路径
# ---------------------------------------------------------------------------

def test_consumers_source_mode_match_historical_paths():
    """source 模式：每个路径消费者的输出与「仓库根相对」的历史绝对路径逐字节一致。"""
    from dataset_recommender.app.accounts import default_sessions_path, default_store_path
    from dataset_recommender.app.webapp import (
        CONFIG_ROOT, DATA_DIR, PROJECT_ROOT, RESOURCE_ROOT, STATIC_DIR,
    )
    from dataset_recommender.agent.trace.recorder import trace_root
    from dataset_recommender.corpus.corpus import _external_layers
    from dataset_recommender.corpus.corpus_curation import _net_ledger_path
    from dataset_recommender.retrieval.vector_recall import default_cross_encoder_dir, default_model_dir

    assert PROJECT_ROOT == _REPO_ROOT
    assert CONFIG_ROOT == _REPO_ROOT
    assert RESOURCE_ROOT == _REPO_ROOT
    assert STATIC_DIR == _REPO_ROOT / "web" / "static"
    assert DATA_DIR == _REPO_ROOT / "database" / "base"
    assert default_store_path(_REPO_ROOT) == _REPO_ROOT / ".userdata" / "accounts.json"
    assert default_sessions_path(_REPO_ROOT) == _REPO_ROOT / ".userdata" / "sessions.json"
    assert trace_root(_REPO_ROOT) == _REPO_ROOT / "database" / "trace"
    assert _net_ledger_path(_REPO_ROOT) == _REPO_ROOT / ".userdata" / "curate_net_ledger.jsonl"
    assert _external_layers(_REPO_ROOT) == (
        _REPO_ROOT / "database" / "external", _REPO_ROOT / "database" / "external",
    )
    assert default_model_dir("m") == _REPO_ROOT / "models" / "embeddings" / "m"
    assert default_cross_encoder_dir("m") == _REPO_ROOT / "models" / "cross_encoders" / "m"


def test_get_settings_source_mode_matches_historical():
    """get_settings：project_root/data_dir/output_dir 与历史逐字节一致（DATA_DIR 未设）。"""
    s = _llm_get_settings()
    assert s.project_root == _REPO_ROOT
    assert s.data_dir == _REPO_ROOT / "database" / "base"
    assert s.output_dir == _REPO_ROOT / "outputs"


# ---------------------------------------------------------------------------
# 5. 双层 external 合并（shipped + user）：去重 / 用户层优先 / 写只落 user / invalidate
# ---------------------------------------------------------------------------

def _write_records_file(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"records": records}, ensure_ascii=False), encoding="utf-8")


def _rec(name: str, source: str) -> dict:
    return {"dataset_name": name, "species": "Human", "url": f"https://example.org/{name}", "source": source}


def _make_split_corpus(tmp_path, monkeypatch):
    """搭一个双根分离的迷你语料：shipped 官方快照 + user 上传，返回 (AppPaths, shipped_root, data_root)。"""
    res = tmp_path / "res"
    data = tmp_path / "data"
    _set_split_env(monkeypatch, res, data)
    p = get_app_paths()
    _write_records_file(p.shipped_external_dir / "official.json", [_rec("官方A", "OfficialSource")])
    _write_records_file(p.shipped_external_dir / "dup.json", [_rec("官方重复", "OfficialSource")])
    _write_records_file(p.user_external_dir / "upload_1.json", [_rec("用户B", "UserSource")])
    _write_records_file(p.user_external_dir / "dup.json", [_rec("用户重复", "UserSource")])
    _write_records_file(p.shipped_base_dir / "base.json",
                        [{"dataset_name": "基础C", "species": "Human", "url": "https://example.org/base"}])
    return p, res, data


def test_external_dual_layer_merge_dedup_user_wins(tmp_path, monkeypatch):
    """双层合并：官方+用户都装载；同文件名去重且**用户层优先**（dup.json 只取用户版）。"""
    from dataset_recommender.corpus.corpus import invalidate_external_cache, load_full_corpus

    p, _res, _data = _make_split_corpus(tmp_path, monkeypatch)
    invalidate_external_cache()
    full = load_full_corpus(p.data_root / "database" / "base", p.data_root)  # base 参数会被收口到 shipped
    names = [r.dataset_name for r in full]
    assert "官方A" in names and "用户B" in names and "基础C" in names
    assert "官方重复" not in names and "用户重复" in names, "同文件名必须用户层优先（shipped 被覆盖）"
    assert full.count(next(r for r in full if r.dataset_name == "用户重复")) == 1, "每文件只装载一次"


def test_external_write_lands_only_in_user_layer(tmp_path, monkeypatch):
    """写操作只落 user 层：ingest_dataset 落 data_root/database/external，resource 层零触碰。"""
    from dataset_recommender.corpus.corpus import invalidate_external_cache, load_full_corpus
    from dataset_recommender.corpus.uploads import ingest_dataset, new_upload_name

    p, res, _data = _make_split_corpus(tmp_path, monkeypatch)
    invalidate_external_cache()
    payload = json.dumps({"records": [_rec("新上传", "UserSource")]}, ensure_ascii=False).encode("utf-8")
    result = ingest_dataset(raw_bytes=payload, safe_name=new_upload_name("new.json"), project_root=p.data_root)
    # 落盘于用户层
    assert (p.user_external_dir / result.filename).is_file()
    assert result.saved_to == f"database/external/{result.filename}"
    # resource 层零触碰（官方快照目录没有新文件）
    assert {f.name for f in res.rglob("*") if f.is_file()} == {"official.json", "dup.json", "base.json"}
    # 新文件即时可见（清缓存后）
    invalidate_external_cache()
    full = load_full_corpus(p.data_root / "database" / "base", p.data_root)
    assert any(r.dataset_name == "新上传" for r in full)
    # 流水账落在 data 层 userdata
    assert (p.userdata_dir / "uploads_journal.jsonl").is_file()


def test_external_invalidate_semantics_preserved(tmp_path, monkeypatch):
    """invalidate_external_cache 语义保持：新文件在清缓存前不可见（lru 缓存键不变）、清后可见。"""
    from dataset_recommender.corpus.corpus import invalidate_external_cache, load_full_corpus

    p, _res, _data = _make_split_corpus(tmp_path, monkeypatch)
    invalidate_external_cache()
    before = len(load_full_corpus(p.data_root / "database" / "base", p.data_root))
    _write_records_file(p.user_external_dir / "upload_late.json", [_rec("晚到", "UserSource")])
    assert len(load_full_corpus(p.data_root / "database" / "base", p.data_root)) == before, "未失效 → 缓存命中旧快照"
    invalidate_external_cache()
    after = len(load_full_corpus(p.data_root / "database" / "base", p.data_root))
    assert after == before + 1, "失效后 → 新文件即时可见可检索"


def test_base_dir_falls_back_to_shipped_in_split_layout(tmp_path, monkeypatch):
    """frozen 布局：调用方从 data_root 派生的 database/base 不存在时，base 从 shipped（resource）收口。"""
    from dataset_recommender.corpus.corpus import load_full_corpus

    p, _res, _data = _make_split_corpus(tmp_path, monkeypatch)
    full = load_full_corpus(p.data_root / "database" / "base", p.data_root)  # data_root/database/base 不存在
    assert any(r.dataset_name == "基础C" for r in full), "base 必须从 shipped 资源层读到"


def test_resource_layer_is_readonly_in_split_layout(tmp_path, monkeypatch):
    """resource 层只读性：双根分离下 corpus 装载只读 shipped；写路径（上传）结构上不触碰它。"""
    from dataset_recommender.corpus.corpus import invalidate_external_cache, load_full_corpus

    p, res, _data = _make_split_corpus(tmp_path, monkeypatch)
    invalidate_external_cache()
    load_full_corpus(p.data_root / "database" / "base", p.data_root)
    # 读取不产生任何写入：shipped 目录内容与装载前逐字节一致
    before = {f.name: f.read_bytes() for f in sorted(res.rglob("*")) if f.is_file()}
    load_full_corpus(p.data_root / "database" / "base", p.data_root)
    after = {f.name: f.read_bytes() for f in sorted(res.rglob("*")) if f.is_file()}
    assert before == after


# ---------------------------------------------------------------------------
# 6. 双根分离下关键消费者解析（懒解析路径）
# ---------------------------------------------------------------------------

def test_consumers_split_layout_resolve_to_data_layer(tmp_path, monkeypatch):
    """双根分离：账户/账本/trace/模型/回收站等写盘侧全部落 data 层；官方快照读 resource 层。"""
    from dataset_recommender.app.accounts import default_sessions_path, default_store_path
    from dataset_recommender.agent.trace.recorder import trace_root
    from dataset_recommender.corpus.corpus_curation import _net_ledger_path, _recycle_dir
    from dataset_recommender.retrieval.vector_recall import default_cross_encoder_dir, default_model_dir

    p, res, data = _make_split_corpus(tmp_path, monkeypatch)
    assert default_store_path(p.data_root) == data / ".userdata" / "accounts.json"
    assert default_sessions_path(p.data_root) == data / ".userdata" / "sessions.json"
    assert trace_root(p.data_root) == data / "database" / "trace"
    assert _net_ledger_path(p.data_root) == data / ".userdata" / "curate_net_ledger.jsonl"
    assert _recycle_dir(p.data_root) == data / ".userdata" / "recycle"
    assert default_model_dir("m") == data / "models" / "embeddings" / "m"
    assert default_cross_encoder_dir("m") == data / "models" / "cross_encoders" / "m"
    assert str(res) != str(data)  # 布置确认：双根确实分离


def test_get_settings_split_layout(tmp_path, monkeypatch):
    """双根分离：get_settings 的 data_dir=shipped_base、output_dir=exports、project_root=data_root。"""
    p, res, data = _make_split_corpus(tmp_path, monkeypatch)
    s = _llm_get_settings()
    assert s.project_root == data
    assert s.data_dir == res / "database" / "base"
    assert s.output_dir == data / "exports"
