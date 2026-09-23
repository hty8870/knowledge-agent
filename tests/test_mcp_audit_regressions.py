# -*- coding: utf-8 -*-
"""MCP 层安全与一致性修复的回归门。

钉住的病形：
- upload_dataset 的 path 读取面无限制：`.userdata/sessions.json`（明文会话 token）这类
        合法 JSON 可被摄入外部库再经 browse_datasets 读回（跨信任边界外泄链）→ 敏感位置
        fail-closed（.env* / 仓库内 .userdata/.git/.venv/database / 任意同名敏感目录）；
- build_task_pack 的 facet/suppressed/lenient 原样透传（recommend 先过共用 sanitize）：
        sanitize casefold 而 retriever 不归一 → 同值在 recommend 生效、在 task-pack 静默
        0 命中；12 项上限也未应用 → 收敛为同一套 sanitize；
- verify_local_assets 的 max_files 仅挡 <=0：传 10**9 可对任意目录全盘 md5（I/O DoS）
        → 硬上限 = lab_ledger.MAX_SCAN_FILES；
- 错误消息原样回显超长入参（响应放大器）→ _clip 统一截断 120 字符。

打桩测试全程 tmp/假对象，零写盘、零 LLM、零网络；真实语料只读。
"""
from __future__ import annotations

import json as _json
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from dataset_recommender.app import mcp_server as M


# ---------------------------------------------------------------- 敏感路径闸（upload_dataset）
# 复用 test_mcp_server.py 的隔离思路：把 project_root 重定向到 tmp，绝不碰真实仓库。
@pytest.fixture
def mcp_tmp_root(tmp_path, monkeypatch):
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "_settings", lambda: SimpleNamespace(project_root=tmp_path, data_dir=base))
    return tmp_path


def test_upload_dataset_rejects_userdata_sessions_json(mcp_tmp_root):
    """外泄链闭环测试：.userdata/sessions.json（明文会话 token 的合法 JSON）必须被
    sensitive_path 拒绝——修复前它会被摄入 database/external/ 并可经 browse 读回。"""
    ud = mcp_tmp_root / ".userdata"
    ud.mkdir()
    sessions = ud / "sessions.json"
    sessions.write_text(_json.dumps({"tok-abc": {"user": "lab"}}), encoding="utf-8")
    with pytest.raises(ToolError, match="sensitive_path"):
        M.upload_dataset(path=str(sessions), source="不该成功的来源")
    # fail-closed：外部库一个字节都没写
    ext = mcp_tmp_root / "database" / "external"
    assert not ext.exists() or list(ext.iterdir()) == []


def test_upload_dataset_rejects_env_files(mcp_tmp_root, tmp_path):
    """.env* 密钥文件（任意位置）拒绝；判定在 is_file 之前——探测不泄露存在性。"""
    for name in (".env", ".env.zhipu", ".env.backup"):
        with pytest.raises(ToolError, match="sensitive_path"):
            M.upload_dataset(path=str(tmp_path / name))
    hidden = tmp_path / ".env"
    hidden.write_text("API_KEY=secret", encoding="utf-8")
    with pytest.raises(ToolError, match="sensitive_path"):
        M.upload_dataset(path=str(hidden))


def test_upload_dataset_rejects_repo_database_and_venv_paths(mcp_tmp_root):
    """仓库内 database/（冻结+外部语料）与 .venv/ 拒绝——从 database/ 搬运毫无意义且污染检索面。"""
    for rel in ("database/base/10x.json", "database/external/upload_x.json", ".venv/lib/x.json"):
        p = mcp_tmp_root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("[]", encoding="utf-8")
        with pytest.raises(ToolError, match="sensitive_path"):
            M.upload_dataset(path=str(p))


def test_upload_dataset_rejects_git_dir_anywhere(mcp_tmp_root, tmp_path):
    """仓库外同名敏感目录（.git/.userdata/.venv）纵深防御：祖先是敏感目录名即拒。"""
    p = tmp_path / "elsewhere" / ".git" / "config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[]", encoding="utf-8")
    with pytest.raises(ToolError, match="sensitive_path"):
        M.upload_dataset(path=str(p))


def test_upload_dataset_outside_repo_json_still_ok(mcp_tmp_root, tmp_path):
    """回归：仓库外的正常数据集 JSON 照常可传（合法用户路径不受闸影响）。"""
    outside = tmp_path / "downloads"
    outside.mkdir(exist_ok=True)
    src = outside / "incoming.json"
    src.write_text(_json.dumps([{"dataset_name": "外部集", "species": "Human"}]), encoding="utf-8")
    r = M.upload_dataset(path=str(src), source="文件来源")
    assert r["ok"] is True and r["record_count"] == 1


def test_upload_dataset_repo_root_plain_json_still_ok(mcp_tmp_root):
    """回归：仓库根顶层的普通 JSON（非敏感四目录、非 .env*）不受影响。"""
    src = mcp_tmp_root / "top.json"
    src.write_text(_json.dumps([{"dataset_name": "顶层集", "species": "Mouse"}]), encoding="utf-8")
    r = M.upload_dataset(path=str(src))
    assert r["ok"] is True and r["record_count"] == 1


# ---------------------------------------------------------------- build_task_pack 过 sanitize
@pytest.fixture
def captured_workflow(monkeypatch, mcp_tmp_root):
    """打桩 _workflow：捕获 run_with_meta 收到的 kwargs（sanitize 是否生效的可观测点），
    返回空检索结果 → 走 build_task_pack 的空分支（回显实际生效的 retrieval_params）。"""
    captured: dict = {}

    class _FakeWF:
        def run_with_meta(self, p=None, **kwargs):
            # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
            captured.update(vars(p) if p is not None else kwargs)
            return SimpleNamespace(
                retrieved_data=[], active_filters=[], coverage_caveats=[],
                unused_query_terms=[], result_total=0, search_trace={"summary": ""},
            )

    monkeypatch.setattr(M, "_workflow", lambda: _FakeWF())
    return captured


def test_build_task_pack_facets_casefolded_like_recommend(captured_workflow):
    """核心病形：{"value":"Homo sapiens"} 原样透传时 retriever 按 casefold 键匹配必 0 命中；
    与 recommend 同过 sanitize 后值被 casefold，params 回显与实际生效一致。"""
    r = M.build_task_pack(
        mode="preview", query="human lung",
        facet_filters=[{"dim": "species", "value": "Homo sapiens"}],
    )
    assert captured_workflow["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]
    assert r["retrieval_params"]["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]


def test_build_task_pack_facets_capped_and_filtered(captured_workflow):
    """sanitize 的白名单（未知维度丢弃）与 12 项上限在 task-pack 入口同样生效。"""
    facets = [{"dim": "species", "value": f"species {i}"} for i in range(15)]
    facets.append({"dim": "bogus_dim", "value": "x"})   # 白名单外 → 丢弃
    M.build_task_pack(mode="preview", query="human lung", facet_filters=facets)
    eff = captured_workflow["facet_filters"]
    assert len(eff) == 12 and all(f["dim"] != "bogus_dim" for f in eff)


def test_build_task_pack_suppressed_and_lenient_sanitized(captured_workflow):
    """suppressed/lenient 同过 sanitize：非法项静默丢弃（安全默认），与 recommend 同口径。"""
    M.build_task_pack(
        mode="preview", query="human lung",
        suppressed_constraints=["bogus_filter"], lenient_dims=["not_a_dim"],
    )
    assert captured_workflow["suppressed_constraints"] == []
    assert captured_workflow["lenient_dims"] == []


def test_build_task_pack_valid_suppressed_passthrough(captured_workflow):
    """合法裸 dim 照常通过（回归：sanitize 不是一刀切清空）。"""
    M.build_task_pack(mode="preview", query="human lung", suppressed_constraints=["species"])
    assert captured_workflow["suppressed_constraints"] == ["species"]


# ---------------------------------------------------------------- verify_local_assets 硬上限
def test_verify_local_assets_max_files_hard_capped(monkeypatch, tmp_path):
    """传 10**9 不再是全盘 md5 扫描许可：钳到 lab_ledger.MAX_SCAN_FILES。"""
    from dataset_recommender.content import lab_ledger
    captured: dict = {}
    monkeypatch.setattr(lab_ledger, "build_manifest_index", lambda: {})
    monkeypatch.setattr(lab_ledger, "scan_directory",
                        lambda d, index, max_files: captured.update(max_files=max_files) or {})
    monkeypatch.setattr(lab_ledger, "build_ledger_report", lambda scan, index: {"datasets": [], "unmatched": []})
    M.verify_local_assets(str(tmp_path), max_files=10 ** 9)
    assert captured["max_files"] == lab_ledger.MAX_SCAN_FILES
    # 合法小值原样通过（不被钳）；<=0 / 非法值回缺省（历史行为不变）
    M.verify_local_assets(str(tmp_path), max_files=5)
    assert captured["max_files"] == 5
    M.verify_local_assets(str(tmp_path), max_files=0)
    assert captured["max_files"] == lab_ledger.MAX_SCAN_FILES


# ---------------------------------------------------------------- 错误回显截断
def test_clip_truncates_long_values():
    assert M._clip("x" * 300) == "x" * 120 + "…"
    assert M._clip("short") == "short"
    assert M._clip(12345) == "12345"            # 非字符串走 repr 口径


def test_validate_enum_clips_echo():
    """枚举错误不再回显 500 字符入参（响应放大器收口）。"""
    with pytest.raises(ToolError) as exc_info:
        M._validate_enum("rerank", "A" * 500, ("off", "llm"))
    assert "A" * 200 not in str(exc_info.value)
    assert "A" * 100 in str(exc_info.value)     # 前 120 字符保留（可读性/可诊断性不丢）


def test_upload_not_found_clips_path(mcp_tmp_root):
    """not_found 的路径回显同样截断。"""
    long_name = "p" * 500 + ".json"
    with pytest.raises(ToolError, match="not_found") as exc_info:
        M.upload_dataset(path=str(mcp_tmp_root / long_name))
    assert "p" * 200 not in str(exc_info.value)
