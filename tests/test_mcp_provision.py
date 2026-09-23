# -*- coding: utf-8 -*-
"""MCP 工具 provision_dataset（v1.29.0，写工具 ②）测试。全程禁网。

禁网接缝：MCP 工具不传 opener，测试把 `download_executor.provision` 包一层注入假 HTTP
出口（与 tests/test_download_executor.py 的 FakeResp 同一模式）；下载索引同样换成虚构
两文件数据集 10x:A。dest 校验（fail-closed）、错误码契约、max_files 闸与 dry_run 均确定性。
"""
from __future__ import annotations

import asyncio
import hashlib
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.app import mcp_server as M  # noqa: E402
from dataset_recommender.corpus import download_executor as DE  # noqa: E402
from dataset_recommender.corpus import download_plan as DP  # noqa: E402


DATA = b"fake h5 payload for mcp provision tests"


class FakeResp:
    """模拟 urllib 响应：.status / .read(n) / 上下文管理（与 test_download_executor 同款）。"""

    def __init__(self, data: bytes, status: int = 200):
        self._data, self.status, self._pos = data, status, 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def getcode(self):
        return self.status

    def read(self, n=-1):
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos:] if n is None or n < 0 else self._data[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


@pytest.fixture
def fake_downloads(monkeypatch):
    """把下载索引换成虚构两文件数据集 10x:A（不依赖真实语料/索引）。"""
    rec = {
        "url": "https://www.10xgenomics.com/datasets/a",
        "primary_download_url": "https://cf.10xgenomics.com/x/f.h5",
        "primary_bytes": len(DATA), "primary_title": "Fake A",
        "files": [
            {"filename": "f.h5", "download_url": "https://cf.10xgenomics.com/x/f.h5",
             "bytes": len(DATA), "md5sum": _md5(DATA), "category": "outputs", "pipeline": ""},
            {"filename": "g.h5", "download_url": "https://cf.10xgenomics.com/x/g.h5",
             "bytes": len(DATA), "md5sum": _md5(DATA), "category": "outputs", "pipeline": ""},
        ],
        "n_files": 2,
    }
    monkeypatch.setattr(DE.downloads, "get", lambda uid: rec if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "files_for", lambda uid: rec["files"] if uid == "10x:A" else [])
    monkeypatch.setattr(DP.downloads, "get", lambda uid: rec if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "primary_url",
                        lambda uid: rec["primary_download_url"] if uid == "10x:A" else None)
    monkeypatch.setattr(DP.downloads, "fastq_url", lambda uid: None)
    monkeypatch.setattr(DP.downloads, "is_available", lambda: True)
    return rec


@pytest.fixture
def fake_network(monkeypatch):
    """禁网：包一层真 provision，注入假 HTTP 出口 + 即时 sleep。MCP 工具内部 `DE.provision`
    是函数体内懒导入后的模块属性查找，故 monkeypatch 模块属性即可生效。"""
    real = DE.provision

    def run(uids, out, **kw):
        return real(uids, out, opener=lambda url, timeout: FakeResp(DATA),
                    sleep=lambda s: None, **kw)

    monkeypatch.setattr(DE, "provision", run)
    return run


# ---------------------------------------------------------------- schema / 注册

def test_provision_tool_registered_with_expected_schema():
    """新工具已注册（与 _EXPECTED_TOOLS 单一真源一致）且入参 schema 符合契约：
    dataset_uid + dest_dir 必填，安全闸参数（scope/include_flagged/max_files/dry_run）可选。"""
    assert "provision_dataset" in M._EXPECTED_TOOLS
    assert len(M._EXPECTED_TOOLS) == 19   # v1.30.0 加 curate_datasets（写工具 ③）：18→19
    tool = next(t for t in asyncio.run(M.mcp.list_tools()) if t.name == "provision_dataset")
    schema = tool.inputSchema
    assert set(schema.get("required", [])) == {"dataset_uid", "dest_dir"}
    props = set(schema["properties"])
    assert {"dataset_uid", "dest_dir", "scope", "include_flagged", "max_files", "dry_run"} <= props
    # docstring 必须显式写明写盘边界与台账回写路径（产品决策变化的可见承诺）
    doc = M.provision_dataset.__doc__
    assert "dest_dir" in doc and "绝不写 database/" in doc
    assert "record_provision_results.py" in doc


# ---------------------------------------------------------------- dest 校验（fail-closed）

def test_dest_relative_path_rejected(tmp_path, fake_downloads):
    with pytest.raises(ToolError, match="bad_out_dir"):
        M.provision_dataset("10x:A", "relative/out")


def test_dest_empty_rejected(tmp_path, fake_downloads):
    with pytest.raises(ToolError, match="bad_out_dir"):
        M.provision_dataset("10x:A", "   ")


def test_dest_protected_frozen_base_rejected(tmp_path, fake_downloads):
    """dest 落进冻结基准 database/base → protected_out_dir（红线，绝不写 database/）。"""
    protected = str(DE._repo_root() / "database" / "base")
    with pytest.raises(ToolError, match="protected_out_dir"):
        M.provision_dataset("10x:A", protected)


def test_dest_protected_external_rejected(tmp_path, fake_downloads):
    """：database/external 是只许 upload_*.json 的元数据库，下载物落进去同样拒。"""
    protected = str(DE._repo_root() / "database" / "external")
    with pytest.raises(ToolError, match="protected_out_dir"):
        M.provision_dataset("10x:A", protected)


def test_dest_protected_inrepo_data_rejected(tmp_path, fake_downloads):
    protected = str(DE._repo_root() / "src" / "dataset_recommender" / "data")
    with pytest.raises(ToolError, match="protected_out_dir"):
        M.provision_dataset("10x:A", protected)


# ---------------------------------------------------------------- 入参错误码契约

def test_empty_uid_rejected(tmp_path):
    with pytest.raises(ToolError, match="empty_uid"):
        M.provision_dataset("", str(tmp_path))


def test_unknown_uid_fail_closed(tmp_path, fake_downloads):
    with pytest.raises(ToolError, match="unknown_uid"):
        M.provision_dataset("10x:NOPE", str(tmp_path / "out"))


def test_bad_scope_rejected(tmp_path, fake_downloads):
    with pytest.raises(ToolError, match="bad_param"):
        M.provision_dataset("10x:A", str(tmp_path / "out"), scope="everything")


def test_max_files_invalid_and_hard_cap(tmp_path, fake_downloads):
    with pytest.raises(ToolError, match="bad_param"):
        M.provision_dataset("10x:A", str(tmp_path / "out"), max_files=0)
    with pytest.raises(ToolError, match="bad_param"):
        M.provision_dataset("10x:A", str(tmp_path / "out"),
                            max_files=M._MAX_PROVISION_FILES + 1)


def test_max_files_gate_raises_not_truncates(tmp_path, fake_downloads):
    """计划 2 文件 > max_files=1 → bad_param 报错（安全闸：宁可报错也不静默截断）。"""
    with pytest.raises(ToolError, match="bad_param"):
        M.provision_dataset("10x:A", str(tmp_path / "out"), scope="all", max_files=1)


# ---------------------------------------------------------------- dry_run / 真下载

def test_dry_run_plans_without_downloading(tmp_path, fake_downloads):
    """dry_run=True：只给计划、不下载、不落任何数据文件（目录可被创建，但里面必须是空的）。"""
    dest = tmp_path / "out"
    r = M.provision_dataset("10x:A", str(dest), dry_run=True)
    assert r["ok"] and r["dry_run"] is True
    assert r["planned_files"] == 1 and r["scope"] == "primary"   # 默认只下 1 个主文件
    assert r["planned_bytes"] == len(DATA)
    assert all(v == 0 for v in r["counts"].values())
    assert r["files"][0]["filename"] == "f.h5" and r["files"][0]["md5sum"] == _md5(DATA)
    assert "没有下载任何文件" in r["summary_zh"]
    assert not dest.exists() or list(dest.iterdir()) == []


def test_provision_downloads_and_verifies(tmp_path, fake_downloads, fake_network):
    """真下载（假 HTTP）：默认 scope=primary 只落 1 个主文件、md5 核对 ok、返回写盘边界与下一步。"""
    dest = tmp_path / "out"
    r = M.provision_dataset("10x:A", str(dest))
    assert r["ok"] and r["dry_run"] is False
    assert r["counts"]["ok"] == 1 and r["planned_files"] == 1
    saved = dest / "10x_A" / "f.h5"
    assert saved.is_file() and saved.read_bytes() == DATA
    assert r["files"][0]["saved_as"] == "10x_A/f.h5"
    assert "绝不写 database/" in r["write_boundary"]
    assert any("verify_local_assets" in n for n in r["next"])
    assert any("record_provision_results.py" in n for n in r["next"])


def test_provision_scope_all_downloads_both(tmp_path, fake_downloads, fake_network):
    dest = tmp_path / "out"
    r = M.provision_dataset("10x:A", str(dest), scope="all", max_files=10)
    assert r["counts"]["ok"] == 2
    assert (dest / "10x_A" / "g.h5").is_file()
