# -*- coding: utf-8 -*-
"""MCP 工具 curate_datasets（v1.30.0，写工具 ③）测试。**全程禁网**：`_fetch` 接缝注入假响应。

钉死的契约：
- 工具注册 / 入参 schema（action 必填，fail-closed）；
- dry_run=True（默认）= plan 零写盘（search_online 的 plan 只写请求账本）；
- apply（dry_run=False）缺 confirm_token → bad_param；token 不符 → token_mismatch 零写入；
- CurateError → ToolError("code: hint")（isError=true）错误映射；
- 返回带 write_boundary 边界声明；
- 写目标重定向到 tmp 仓库根（SimpleNamespace 替身，同 test_mcp_server 的 mcp_tmp_root 模式），
  绝不污染真实 database/external/。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.app import mcp_server as M  # noqa: E402
from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402


# ---------------------------------------------------------------- 夹具

@pytest.fixture
def curate_tmp_root(tmp_path, monkeypatch):
    """把 curate_datasets 的写目标重定向到临时仓库根（工具只读 s.project_root）。"""
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "_settings", lambda: SimpleNamespace(project_root=tmp_path, data_dir=base))
    return tmp_path


# BioStudies 假响应（与 tests/test_corpus_curation.py 同型）：搜索 1 条命中 + 详情富化。
_SEARCH_PAYLOAD = {
    "totalHits": 1,
    "hits": [
        {"accession": "E-MTAB-0001", "title": "Human lung single cell RNA-seq",
         "content": "single cell RNA sequencing of Homo sapiens lung", "release_date": "2023-05-01"},
    ],
}
_DETAIL_1 = {
    "section": {
        "attributes": [
            {"name": "Organism", "value": "Homo sapiens"},
            {"name": "Study type", "value": "RNA-seq of coding RNA from single cells"},
        ],
        "subsections": [],
    }
}


@pytest.fixture
def fake_fetch(monkeypatch):
    """禁网：_fetch 注入假响应。MCP 工具懒导入的是 `dataset_recommender.*`（src 注入）实例，
    故补丁打在同一个模块对象上。"""
    def _fake(url, **kwargs):
        if "arrayexpress/search" in url:
            return _SEARCH_PAYLOAD, 200
        if "E-MTAB-0001" in url:
            return _DETAIL_1, 200
        raise cc.CurateError("network_error", "假网络故障。")
    monkeypatch.setattr(cc, "_fetch", _fake)
    return _fake


def _payload_json(records, *, source="测试源"):
    return json.dumps({"source": source, "records": records}, ensure_ascii=False)


def _import_via_tool(records, *, filename="a.json"):
    """plan→apply 走工具导入一份，返回 apply 结果。"""
    raw = _payload_json(records)
    plan = M.curate_datasets(action="import", payload_json=raw, filename=filename)
    return M.curate_datasets(action="import", payload_json=raw, filename=filename,
                             confirm_token=plan["confirm_token"], dry_run=False)


# ---------------------------------------------------------------- 注册 / schema

def test_curate_tool_registered_with_expected_schema():
    """工具已注册（_EXPECTED_TOOLS 单一真源，19 个），入参 schema：action 必填、其余可选。"""
    assert "curate_datasets" in M._EXPECTED_TOOLS
    assert len(M._EXPECTED_TOOLS) == 19
    tool = next(t for t in asyncio.run(M.mcp.list_tools()) if t.name == "curate_datasets")
    schema = tool.inputSchema
    assert set(schema.get("required", [])) == {"action"}
    props = set(schema["properties"])
    assert {"action", "query", "source", "species", "limit", "filename",
            "payload_json", "confirm_token", "force", "dry_run"} <= props
    # docstring 首句承诺：管护写工具、plan 默认 dry_run、apply 才写盘/联网
    doc = M.curate_datasets.__doc__
    assert "写工具" in doc and "dry_run" in doc and "confirm_token" in doc
    assert "apply" in doc and "写盘" in doc


# ---------------------------------------------------------------- fail-closed 入参闸

def test_unknown_action_is_bad_action(curate_tmp_root):
    with pytest.raises(ToolError, match="bad_action"):
        M.curate_datasets(action="drop_everything")


def test_apply_without_confirm_token_is_bad_param(curate_tmp_root):
    with pytest.raises(ToolError, match="bad_param"):
        M.curate_datasets(action="remove", filename="upload_x.json", dry_run=False)


def test_import_without_payload_is_bad_param(curate_tmp_root):
    with pytest.raises(ToolError, match="bad_param"):
        M.curate_datasets(action="import", filename="a.json")


def test_remove_official_snapshot_is_not_curatable(curate_tmp_root):
    ext = curate_tmp_root / "database" / "external"
    (ext / "arrayexpress.json").write_text(json.dumps({"records": [{"dataset_name": "官方"}]}),
                                           encoding="utf-8")
    with pytest.raises(ToolError, match="not_curatable"):
        M.curate_datasets(action="remove", filename="arrayexpress.json")


def test_remove_unknown_file(curate_tmp_root):
    with pytest.raises(ToolError, match="unknown_file"):
        M.curate_datasets(action="remove", filename="upload_nope.json")


def test_unregistered_source_fail_closed(curate_tmp_root):
    # 同 web 侧口径：encode 「认识但没接搜索」（前本例用 hca，该批 hca/10x 已接入）。
    with pytest.raises(ToolError, match="source_not_registered"):
        M.curate_datasets(action="search_online", query="lung", source="encode")


def test_error_message_shape_is_code_colon_hint(curate_tmp_root):
    """ToolError 消息约定「code: hint」：客户端按 isError + 机器码分类。"""
    with pytest.raises(ToolError) as exc:
        M.curate_datasets(action="nope")
    assert str(exc.value).startswith("bad_action: ")


# ---------------------------------------------------------------- list（纯只读）

def test_list_is_read_only_and_carries_write_boundary(curate_tmp_root):
    _import_via_tool([{"dataset_name": "甲", "source": "测试源"}])
    r = M.curate_datasets(action="list", dry_run=False)   # list 无 apply 形态，dry_run 参数无意义
    assert r["ok"] is True and r["action"] == "curate.list"
    assert r["file_count"] == 1
    assert r["files"][0]["curatable"] is True
    assert "write_boundary" in r and "只读" in r["write_boundary"]


# ---------------------------------------------------------------- import：plan 零写盘 + apply token 契约

def test_import_plan_writes_nothing_and_returns_token(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "新集", "species": "Human"}])
    r = M.curate_datasets(action="import", payload_json=raw, filename="new.json", source="实验室")
    assert r["ok"] is True and r["dry_run"] is True
    assert r["confirm_token"] and r["record_count"] == 1
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "plan 不得落盘"
    assert "预览" in r["write_boundary"] and "没有写入" in r["write_boundary"]
    assert r["next"], "plan 应给出下一步提示"


def test_import_apply_with_wrong_token_writes_nothing(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "新集"}])
    M.curate_datasets(action="import", payload_json=raw, filename="new.json")
    with pytest.raises(ToolError, match="token_mismatch"):
        M.curate_datasets(action="import", payload_json=raw, filename="new.json",
                          confirm_token="deadbeefdeadbeef", dry_run=False)
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "token 不符必须零写入"


def test_import_apply_happy_path_and_duplicate_gate(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "新集", "species": "Mouse", "url": "https://e/x"}])
    plan = M.curate_datasets(action="import", payload_json=raw, filename="new.json", source="实验室")
    r = M.curate_datasets(action="import", payload_json=raw, filename="new.json", source="实验室",
                          confirm_token=plan["confirm_token"], dry_run=False)
    assert r["ok"] is True and r["dry_run"] is False and r["record_count"] == 1
    assert r["filename"].startswith("upload_") and r["filename"].endswith("new.json")
    assert "database/external" in r["write_boundary"] and "database/base" in r["write_boundary"]
    saved = curate_tmp_root / r["saved_to"]
    assert saved.is_file()
    assert list((curate_tmp_root / "database" / "base").iterdir()) == [], "base 一尘不染"
    # 内容整集撞重：默认 duplicate_content 拒绝、零写入；force=True 覆盖放行
    plan2 = M.curate_datasets(action="import", payload_json=raw, filename="new.json")
    assert plan2["duplicate"]["is_duplicate"] is True
    with pytest.raises(ToolError, match="duplicate_content"):
        M.curate_datasets(action="import", payload_json=raw, filename="new.json",
                          confirm_token=plan2["confirm_token"], dry_run=False)
    r3 = M.curate_datasets(action="import", payload_json=raw, filename="new.json",
                           confirm_token=plan2["confirm_token"], dry_run=False, force=True)
    assert r3["forced"] is True
    assert len(list((curate_tmp_root / "database" / "external").iterdir())) == 2


# ---------------------------------------------------------------- remove / restore：回收站往返

def test_remove_and_restore_roundtrip_via_recycle_bin(curate_tmp_root):
    applied = _import_via_tool([{"dataset_name": "待删", "source": "测试源"}])
    name = applied["filename"]

    plan = M.curate_datasets(action="remove", filename=name)
    assert plan["dry_run"] is True and plan["confirm_token"]
    assert "回收站" in plan["effect"]
    assert (curate_tmp_root / "database" / "external" / name).is_file(), "plan 不得动文件"

    r = M.curate_datasets(action="remove", filename=name,
                          confirm_token=plan["confirm_token"], dry_run=False)
    assert r["dry_run"] is False and r["restorable"] is True
    assert "回收站" in r["write_boundary"]
    assert not (curate_tmp_root / "database" / "external" / name).exists(), "remove 后 external 不可见"
    recycle_name = Path(r["moved_to"]).name
    assert (curate_tmp_root / ".userdata" / "recycle" / recycle_name).is_file()
    manifest = (curate_tmp_root / ".userdata" / "recycle" / "manifest.jsonl").read_text(encoding="utf-8")
    assert name in manifest

    plan_r = M.curate_datasets(action="restore", filename=recycle_name)
    assert plan_r["target_filename"] == name
    r2 = M.curate_datasets(action="restore", filename=recycle_name,
                           confirm_token=plan_r["confirm_token"], dry_run=False)
    assert r2["dry_run"] is False
    assert (curate_tmp_root / "database" / "external" / name).is_file(), "restore 后应移回原文件名"


# ---------------------------------------------------------------- search_online：禁网 mock

def test_search_online_plan_preview_only_then_apply_ingests(curate_tmp_root, fake_fetch):
    plan = M.curate_datasets(action="search_online", query="lung", species="Human", limit=5)
    assert plan["ok"] is True and plan["dry_run"] is True
    assert plan["record_count"] == 1 and plan["candidates"], "候选随 plan 返回供 apply 回传"
    assert plan["confirm_token"]
    # plan 不落数据文件，只记请求账本
    assert list((curate_tmp_root / "database" / "external").iterdir()) == []
    ledger = curate_tmp_root / ".userdata" / "curate_net_ledger.jsonl"
    assert ledger.is_file() and "arrayexpress" in ledger.read_text(encoding="utf-8")
    assert "联网" in plan["write_boundary"]

    # apply：把 plan 返回原样作为 payload_json 回传 → 经 ingest 管线入库
    r = M.curate_datasets(action="search_online", payload_json=json.dumps(plan, ensure_ascii=False),
                          confirm_token=plan["confirm_token"], dry_run=False)
    assert r["dry_run"] is False and r["record_count"] == 1
    assert (curate_tmp_root / r["saved_to"]).is_file()
    disk = json.loads((curate_tmp_root / r["saved_to"]).read_text(encoding="utf-8"))
    assert disk["records"][0]["source"] == "ArrayExpress"


def test_search_online_apply_with_tampered_candidates_is_token_mismatch(curate_tmp_root, fake_fetch):
    plan = M.curate_datasets(action="search_online", query="lung")
    tampered = dict(plan)
    tampered["candidates"] = [dict(plan["candidates"][0], dataset_name="被调包的标题")]
    with pytest.raises(ToolError, match="token_mismatch"):
        M.curate_datasets(action="search_online", payload_json=json.dumps(tampered, ensure_ascii=False),
                          confirm_token=plan["confirm_token"], dry_run=False)
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "调包必须零写入"


def test_search_online_apply_requires_plan_payload(curate_tmp_root, fake_fetch):
    with pytest.raises(ToolError, match="bad_param"):
        M.curate_datasets(action="search_online", confirm_token="x" * 16, dry_run=False)


def test_search_online_network_failure_maps_network_error(curate_tmp_root, monkeypatch):
    def _boom(url, **kwargs):
        raise cc.CurateError("network_error", "假网络故障：全部重试失败。")
    monkeypatch.setattr(cc, "_fetch", _boom)
    with pytest.raises(ToolError, match="network_error"):
        M.curate_datasets(action="search_online", query="lung")
