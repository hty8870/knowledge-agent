# -*- coding: utf-8 -*-
"""Web 后端入参与安全加固的回归门。

钉死的病形：
-   /api/upload 无体积上限 → 64MB 闸，超限 413 人话；
-   /api/task-pack/preview 日期未铺 _require_iso_date → 非法即 400；
-   Host 守卫放行 userinfo → 403（尾随点放行是有意行为，一并钉住）；
-   build 空指纹跳校验 → 缺一即 400；retrieval_date 未校验（引号注入响应头 /
        中文未捕获 500）→ 非法即 400；500 档缺安全头 → middleware 兜底；
-   _validate_pack_sources 对 str 逐字符枚举 → 先判型，给「sources 需要数组」人话。

写目标隔离：upload 成功路径用 monkeypatch 把 PROJECT_ROOT 重定向到 tmp 仓库根，
绝不污染真实 database/external/。全程禁网、零 LLM。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


class _ShouldNotRun:
    """断言用：实例化/被调即失败——证明闸在重活之前生效。"""

    def __init__(self, *args, **kwargs):
        raise AssertionError("闸之后的重活被触达了")


def _empty_run():
    """_task_pack_retrieval 的赝品返回：零命中分支，形状与真源一致。"""
    return {
        "items": [], "records": [], "candidate_uids": [], "by_uid": {},
        "result_uids": set(),
        "honesty": {"active_filters": [], "coverage_caveats": [], "unused_query_terms": [],
                    "or_handling": {}, "search_trace_summary": "", "result_total": 0},
        "meta": SimpleNamespace(resolution_status="no_match"),
    }


# ---------------------------------------------------------------- /api/upload 64MB 闸

@pytest.fixture
def upload_tmp_root(tmp_path, monkeypatch):
    (tmp_path / "database" / "external").mkdir(parents=True, exist_ok=True)
    (tmp_path / "database" / "base").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    return tmp_path


def test_upload_over_64mb_rejected_413(upload_tmp_root):
    """ 病形：68.7MB 曾 200 落盘。现在超限 413 + 人话，且一个字节不落盘。"""
    big = b'{"records": [' + b" " * (65 * 1024 * 1024) + b"]}"
    res = client.post("/api/upload", files={"file": ("huge.json", big, "application/json")})
    assert res.status_code == 413
    assert "64 MB" in res.json()["detail"]
    assert list((upload_tmp_root / "database" / "external").iterdir()) == []


def test_upload_normal_size_still_works(upload_tmp_root):
    """闸不误伤：小文件正常入库（与修复前逐位同行为）。"""
    body = json.dumps([{"dataset_name": "小文件", "species": "Human"}]).encode("utf-8")
    res = client.post("/api/upload", files={"file": ("ok.json", body, "application/json")})
    assert res.status_code == 200
    assert res.json()["ok"] is True and res.json()["record_count"] == 1


# ---------------------------------------------------------------- preview 日期校验

def test_preview_rejects_garbage_dates_before_retrieval(monkeypatch):
    """ 病形：date_from=not-a-date 曾 200 谎称「没有命中」； 曾冒充生效条件。"""
    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", _ShouldNotRun)
    for bad in ("not-a-date", "2020-13-45", "2020-1-1", "今天"):
        res = client.post("/api/task-pack/preview", json={"query": "human liver", "date_from": bad})
        assert res.status_code == 400, bad
        assert "date_from" in res.json()["detail"]
    res = client.post("/api/task-pack/preview", json={"query": "human liver", "date_to": "2020-02-30"})
    assert res.status_code == 400 and "date_to" in res.json()["detail"]


def test_preview_valid_and_empty_dates_pass_through(monkeypatch):
    """合法 ISO / 空串（=不限）零误伤，且生效值原样进入检索参数。"""
    seen = []

    def fake_retrieval(params):
        seen.append(dict(params))
        return _empty_run()

    monkeypatch.setattr(webapp, "_task_pack_retrieval", fake_retrieval)
    res = client.post("/api/task-pack/preview",
                      json={"query": "human liver", "date_from": "2020-01-01", "date_to": "2024-12-31"})
    assert res.status_code == 200
    assert seen[0]["date_from"] == "2020-01-01" and seen[0]["date_to"] == "2024-12-31"
    res = client.post("/api/task-pack/preview", json={"query": "human liver"})
    assert res.status_code == 200
    assert seen[1]["date_from"] == "" and seen[1]["date_to"] == ""


# ---------------------------------------------------------------- build 指纹/日期/500 安全头

def _build_payload(**overrides):
    payload = {
        "plan_token": "tok", "snapshot_id": "snap", "content_digest": "dig",
        "selected_uids": ["uid-a"],
        "retrieval_params": {"query": "human liver"},
    }
    payload.update(overrides)
    return payload


def test_build_rejects_empty_fingerprints(monkeypatch):
    """ T2 病形：空指纹三件套 + 篡改 limit 曾 200 出包。现在缺一即 400，检索都不跑。"""
    monkeypatch.setattr(webapp, "_task_pack_retrieval", _ShouldNotRun)
    for empties in ({"plan_token": ""}, {"snapshot_id": ""}, {"content_digest": ""},
                    {"plan_token": "", "snapshot_id": "", "content_digest": ""}):
        res = client.post("/api/task-pack/build", json=_build_payload(**empties))
        assert res.status_code == 400, empties
        assert "指纹" in res.json()["detail"]


def test_build_rejects_bad_retrieval_date(monkeypatch):
    """ 回归病形：引号曾注入 Content-Disposition 第二参数；中文曾炸成未捕获 500。"""
    monkeypatch.setattr(webapp, "_task_pack_retrieval", _ShouldNotRun)
    for bad in ("今天", '2026-08-04", injected="yes', "2026-13-01"):
        res = client.post("/api/task-pack/build", json=_build_payload(retrieval_date=bad))
        assert res.status_code == 400, bad
        assert "retrieval_date" in res.json()["detail"]


def test_build_valid_date_and_mismatched_fingerprints_give_409(monkeypatch):
    """合法 retrieval_date 过校验、指纹严格比对不一致 → 409 诚实兜底（非 400/500/200）。"""
    monkeypatch.setattr(webapp, "_task_pack_retrieval", lambda params: _empty_run())
    res = client.post("/api/task-pack/build", json=_build_payload(retrieval_date="2026-08-04"))
    assert res.status_code == 409
    assert res.json()["ok"] is False


def test_500_responses_carry_security_headers(monkeypatch, capsys):
    """ 500 档病形：未捕获异常曾是唯一缺 XFO/nosniff/Referrer-Policy 的档位。
    现在 middleware 兜底：三头带齐、客户端只见通用文案（异常正文不上屏）、堆栈留 stderr。"""
    def boom(_params):
        raise RuntimeError("boom at Z:\\leak_canary\\corpus")

    monkeypatch.setattr(webapp, "_task_pack_retrieval", boom)
    res = client.post("/api/task-pack/preview", json={"query": "human liver"})
    assert res.status_code == 500
    assert res.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "no-referrer"
    assert "leak_canary" not in res.text
    assert "RuntimeError" in capsys.readouterr().err


# ---------------------------------------------------------------- Host 守卫 userinfo

def test_host_with_userinfo_rejected():
    """user@host 出现在 Host 头里永远是构造客户端 → 403 fail-closed。"""
    for host in ("user@127.0.0.1", "user:pass@127.0.0.1:8080", "user@localhost"):
        res = client.get("/api/health", headers={"Host": host})
        assert res.status_code == 403, host


def test_host_trailing_dot_still_accepted():
    """尾随点是有意的 FQDN 归一化（rstrip，注释已写明）——钉住现状，防误改。"""
    res = client.get("/api/health", headers={"Host": "127.0.0.1."})
    assert res.status_code == 200


# ---------------------------------------------------------------- sources 逐字符枚举

def test_build_sources_string_gets_shape_error_not_char_enum(monkeypatch):
    """ 病形：sources='10x Genomics' 曾报「不认识这些数据来源：1、0、x、 …」。"""
    monkeypatch.setattr(webapp, "_task_pack_retrieval", _ShouldNotRun)
    payload = _build_payload()
    payload["retrieval_params"]["sources"] = "10x Genomics"
    res = client.post("/api/task-pack/build", json=payload)
    assert res.status_code == 400
    detail = res.json()["detail"]
    assert "数组" in detail
    assert "1、0、x" not in detail
