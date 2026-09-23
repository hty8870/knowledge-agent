# -*- coding: utf-8 -*-
"""Web 端点 `/api/curate/plan` + `/api/curate/apply` 测试（TestClient，**全程禁网**）。

钉死的契约：
- 两个端点都先过 `_require_same_origin`（跨源 403）；
- plan 零副作用（不写数据文件；search_online 的 plan 只写请求账本）；
- apply 成功 / token_mismatch → HTTP 400 且零写入；
- CurateError → HTTP 400，detail 为人读 hint（机器码不进上屏文案， 文案收口）；
- Pydantic 请求模型 extra="forbid"（未知字段 422）。

写目标隔离：monkeypatch `webapp.PROJECT_ROOT` 到 tmp 仓库根（端点调用时才读该全局），
绝不污染真实 database/external/。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def curate_tmp_root(tmp_path, monkeypatch):
    """把 /api/curate/* 的写目标重定向到临时仓库根。"""
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "base").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


_SEARCH_PAYLOAD = {
    "totalHits": 1,
    "hits": [
        {"accession": "E-MTAB-0001", "title": "Human lung single cell RNA-seq",
         "content": "single cell RNA sequencing of Homo sapiens lung", "release_date": "2023-05-01"},
    ],
}
_DETAIL_1 = {"section": {"attributes": [{"name": "Organism", "value": "Homo sapiens"}], "subsections": []}}


@pytest.fixture
def fake_fetch(monkeypatch):
    """禁网：_fetch 注入假响应（webapp 懒导入 `dataset_recommender.*` 实例，补丁打同一对象）。"""
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


# ---------------------------------------------------------------- same-origin 闸

def test_plan_and_apply_reject_cross_origin(curate_tmp_root):
    bodies = {"/api/curate/plan": {"action": "list"},
              "/api/curate/apply": {"action": "list", "confirm_token": "x"}}
    for path, body in bodies.items():
        res = client.post(path, json=body, headers={"Origin": "http://evil.example.com"})
        assert res.status_code == 403, path
        assert "同源" in res.json()["detail"] or "Host" in res.json()["detail"]


def test_plan_and_apply_reject_non_loopback_host():
    for path in ("/api/curate/plan", "/api/curate/apply"):
        res = client.post(path, json={"action": "list", "confirm_token": "x"},
                          headers={"Host": "evil.example.com"})
        assert res.status_code == 403, path


# ---------------------------------------------------------------- fail-closed 入参

def test_unknown_action_400_with_code(curate_tmp_root):
    res = client.post("/api/curate/plan", json={"action": "drop_everything"})
    assert res.status_code == 400
    assert "未知管护动作" in res.json()["detail"]


def test_apply_without_confirm_token_400_bad_param(curate_tmp_root):
    res = client.post("/api/curate/apply", json={"action": "remove", "filename": "upload_x.json",
                                                 "confirm_token": "  "})
    assert res.status_code == 400
    assert "confirm_token" in res.json()["detail"]


def test_apply_requires_confirm_token_field(curate_tmp_root):
    """confirm_token 是 apply 模型的必填字段：缺整个字段 → 422。"""
    res = client.post("/api/curate/apply", json={"action": "list"})
    assert res.status_code == 422


def test_unknown_fields_rejected_422(curate_tmp_root):
    res = client.post("/api/curate/plan", json={"action": "list", "limitt": 5})
    assert res.status_code == 422


# ---------------------------------------------------------------- plan 零副作用

def test_plan_import_zero_side_effect(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "新集", "species": "Human"}])
    res = client.post("/api/curate/plan", json={
        "action": "import", "payload_json": raw, "filename": "new.json", "source": "实验室",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True and body["dry_run"] is True
    result = body["result"]
    assert result["confirm_token"] and result["record_count"] == 1
    assert "write_boundary" in result
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "plan 不得落盘"


def test_plan_list_read_only(curate_tmp_root):
    res = client.post("/api/curate/plan", json={"action": "list"})
    assert res.status_code == 200
    result = res.json()["result"]
    assert result["action"] == "curate.list" and result["file_count"] == 0
    assert "只读" in result["write_boundary"]


# ---------------------------------------------------------------- apply：成功与 token_mismatch

def test_apply_import_success_and_token_mismatch_400(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "新集", "species": "Mouse", "url": "https://e/x"}])
    plan = client.post("/api/curate/plan", json={
        "action": "import", "payload_json": raw, "filename": "new.json",
    }).json()["result"]

    bad = client.post("/api/curate/apply", json={
        "action": "import", "payload_json": raw, "filename": "new.json",
        "confirm_token": "deadbeefdeadbeef",
    })
    assert bad.status_code == 400
    assert "预览已经失效" in bad.json()["detail"]
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "token 不符必须零写入"

    ok = client.post("/api/curate/apply", json={
        "action": "import", "payload_json": raw, "filename": "new.json",
        "confirm_token": plan["confirm_token"],
    })
    assert ok.status_code == 200, ok.text
    result = ok.json()["result"]
    assert result["dry_run"] is False and result["record_count"] == 1
    assert result["filename"].startswith("upload_")
    assert (curate_tmp_root / result["saved_to"]).is_file()
    assert "database/external" in result["write_boundary"]
    assert list((curate_tmp_root / "database" / "base").iterdir()) == [], "base 一尘不染"


def test_apply_remove_and_restore_roundtrip(curate_tmp_root):
    raw = _payload_json([{"dataset_name": "待删", "source": "测试源"}])
    plan = client.post("/api/curate/plan", json={
        "action": "import", "payload_json": raw, "filename": "a.json",
    }).json()["result"]
    applied = client.post("/api/curate/apply", json={
        "action": "import", "payload_json": raw, "filename": "a.json",
        "confirm_token": plan["confirm_token"],
    }).json()["result"]
    name = applied["filename"]

    rplan = client.post("/api/curate/plan", json={"action": "remove", "filename": name}).json()["result"]
    assert (curate_tmp_root / "database" / "external" / name).is_file(), "plan 不得动文件"
    rm = client.post("/api/curate/apply", json={
        "action": "remove", "filename": name, "confirm_token": rplan["confirm_token"],
    })
    assert rm.status_code == 200
    assert not (curate_tmp_root / "database" / "external" / name).exists()
    recycle_name = Path(rm.json()["result"]["moved_to"]).name

    not_curatable = client.post("/api/curate/plan", json={"action": "remove",
                                                          "filename": "arrayexpress.json"})
    assert not_curatable.status_code == 400
    assert "外部库里没有文件" in not_curatable.json()["detail"]

    splan = client.post("/api/curate/plan", json={"action": "restore",
                                                  "filename": recycle_name}).json()["result"]
    back = client.post("/api/curate/apply", json={
        "action": "restore", "filename": recycle_name, "confirm_token": splan["confirm_token"],
    })
    assert back.status_code == 200
    assert (curate_tmp_root / "database" / "external" / name).is_file()


# ---------------------------------------------------------------- search_online（禁网 mock）

def test_search_online_plan_and_apply_via_web(curate_tmp_root, fake_fetch):
    res = client.post("/api/curate/plan", json={
        "action": "search_online", "query": "lung", "species": "Human", "limit": 5,
    })
    assert res.status_code == 200, res.text
    plan = res.json()["result"]
    assert plan["record_count"] == 1 and plan["candidates"] and plan["confirm_token"]
    assert list((curate_tmp_root / "database" / "external").iterdir()) == [], "plan 不落数据文件"
    assert (curate_tmp_root / ".userdata" / "curate_net_ledger.jsonl").is_file(), "plan 只记请求账本"

    ok = client.post("/api/curate/apply", json={
        "action": "search_online", "confirm_token": plan["confirm_token"], "plan_result": plan,
    })
    assert ok.status_code == 200, ok.text
    result = ok.json()["result"]
    assert result["record_count"] == 1
    disk = json.loads((curate_tmp_root / result["saved_to"]).read_text(encoding="utf-8"))
    assert disk["records"][0]["source"] == "ArrayExpress"


def test_search_online_apply_tampered_candidates_400(curate_tmp_root, fake_fetch):
    plan = client.post("/api/curate/plan", json={
        "action": "search_online", "query": "lung",
    }).json()["result"]
    tampered = dict(plan)
    tampered["candidates"] = [dict(plan["candidates"][0], dataset_name="被调包的标题")]
    res = client.post("/api/curate/apply", json={
        "action": "search_online", "confirm_token": plan["confirm_token"], "plan_result": tampered,
    })
    assert res.status_code == 400
    assert "预览已经失效" in res.json()["detail"]
    assert list((curate_tmp_root / "database" / "external").iterdir()) == []


def test_search_online_unregistered_source_400(curate_tmp_root):
    # encode 在 check_updates 注册表里、但不在 SOURCE_ADAPTERS（前本例用 hca；
    # hca/10x 该批已接入，换成仍未接搜索的 encode 继续钉）。
    res = client.post("/api/curate/plan", json={"action": "search_online", "query": "lung",
                                                "source": "encode"})
    assert res.status_code == 400
    assert "暂不支持联网搜索来源" in res.json()["detail"]
