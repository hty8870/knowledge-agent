"""网页版灰度部署口：Host 守卫白名单（BIODATA_TRUSTED_HOSTS）与 run_web 绑定（BIODATA_WEB_HOST）。

两口均为 additive：缺省时与历史行为逐字节一致（仅 loopback Host / 127.0.0.1 绑定），
本文件同时钉死「放行公网 Host 不等于放开跨源写」的纵深语义。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
import run_web  # noqa: E402


client = TestClient(webapp.app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------- 解析层


def test_parse_accepts_public_ip_and_domain():
    parsed = webapp._parse_trusted_request_hosts("203.0.113.10, biodata.example.org")
    assert parsed == frozenset({"203.0.113.10", "biodata.example.org"})


@pytest.mark.parametrize(
    "raw",
    [
        "127.0.0.1:8080",      # 带端口
        "http://biodata.example.org",  # 带 scheme
        "user@biodata.example.org",    # userinfo
        "*.example.org",       # 通配
        "intranet",            # 单段
        "intranet.lan",        # 内网后缀域名
    ],
)
def test_parse_rejects_unsafe_entries(raw: str):
    with pytest.raises(ValueError):
        webapp._parse_trusted_request_hosts(raw)


def test_parse_strips_trailing_dot():
    assert webapp._parse_trusted_request_hosts("biodata.example.org.") == frozenset(
        {"biodata.example.org"}
    )


def test_parse_empty_is_empty_set():
    assert webapp._parse_trusted_request_hosts("") == frozenset()
    assert webapp._parse_trusted_request_hosts(None) == frozenset()


def test_module_default_trusts_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(webapp._TRUSTED_HOSTS_ENV, raising=False)
    assert webapp._parse_trusted_request_hosts(os.getenv(webapp._TRUSTED_HOSTS_ENV)) == frozenset()
    # 模块级常量在测试进程里按导入时环境解析；本仓库测试不设该变量 → 恒空
    assert webapp._TRUSTED_REQUEST_HOSTS == frozenset()


# ---------------------------------------------------------------- 行为层：Host 守卫


def test_public_host_is_rejected_without_trust():
    response = client.get("/api/health", headers={"Host": "203.0.113.10"})
    assert response.status_code == 403


def test_trusted_public_host_is_accepted_for_reads(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        webapp, "_TRUSTED_REQUEST_HOSTS", frozenset({"203.0.113.10"})
    )
    response = client.get("/api/health", headers={"Host": "203.0.113.10"})
    assert response.status_code == 200
    assert response.json()["version"] == webapp.WEB_API_VERSION


def test_trusted_public_host_still_rejects_cross_origin_writes(
    monkeypatch: pytest.MonkeyPatch,
):
    """放行 Host ≠ 放开跨源：白名单里的公网入口仍受同源闸约束。"""
    monkeypatch.setattr(
        webapp, "_TRUSTED_REQUEST_HOSTS", frozenset({"203.0.113.10"})
    )
    response = client.post(
        "/api/interpret",
        json={"query": "human breast cancer"},
        headers={"Host": "203.0.113.10", "Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_trusted_public_host_accepts_same_origin_writes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        webapp, "_TRUSTED_REQUEST_HOSTS", frozenset({"203.0.113.10"})
    )
    response = client.post(
        "/api/interpret",
        json={"query": "human breast cancer"},
        headers={"Host": "203.0.113.10", "Origin": "http://203.0.113.10"},
    )
    assert response.status_code == 200


def test_untrusted_public_host_still_rejected_when_trust_set_has_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        webapp, "_TRUSTED_REQUEST_HOSTS", frozenset({"biodata.example.org"})
    )
    response = client.get("/api/health", headers={"Host": "203.0.113.10"})
    assert response.status_code == 403


# ---------------------------------------------------------------- 行为层：run_web 绑定


def _serve_capture(order: list) -> None:
    def serve(*args, **kwargs):
        order.append(kwargs)

    return serve


def test_run_web_default_bind_is_loopback(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("BIODATA_WEB_HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(run_web, "start_background_warm", lambda *a, **k: None)
    order: list = []
    monkeypatch.setattr(run_web.uvicorn, "run", _serve_capture(order))
    run_web.main()
    assert order and order[0]["host"] == "127.0.0.1"
    assert order[0]["workers"] == 1


def test_run_web_web_host_env_overrides_bind(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BIODATA_WEB_HOST", "0.0.0.0")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(run_web, "start_background_warm", lambda *a, **k: None)
    order: list = []
    monkeypatch.setattr(run_web.uvicorn, "run", _serve_capture(order))
    run_web.main()
    assert order and order[0]["host"] == "0.0.0.0"


def test_run_web_blank_web_host_falls_back_to_loopback(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("BIODATA_WEB_HOST", "   ")
    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.setattr(run_web, "start_background_warm", lambda *a, **k: None)
    order: list = []
    monkeypatch.setattr(run_web.uvicorn, "run", _serve_capture(order))
    run_web.main()
    assert order and order[0]["host"] == "127.0.0.1"
