from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    A._reset_state_for_tests()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    with TestClient(app, base_url="http://127.0.0.1") as c:
        yield c
    A._reset_state_for_tests()


def test_register_login_whoami_logout_flow(client):
    r = client.post("/api/account/register", json={"username": "alice", "password": "password12"})
    assert r.status_code == 200 and r.json()["user"]["username"] == "alice"
    assert A.SESSION_COOKIE in r.cookies
    w = client.get("/api/account/whoami")
    assert w.json()["user"]["username"] == "alice"
    assert client.post("/api/account/logout").status_code == 200
    assert client.get("/api/account/whoami").json()["user"] is None


def test_login_wrong_password_and_unknown_user_both_401(client):
    client.post("/api/account/register", json={"username": "bob", "password": "password12"})
    client.post("/api/account/logout")
    assert client.post("/api/account/login", json={"username": "bob", "password": "wrongpass1"}).status_code == 401
    assert client.post("/api/account/login", json={"username": "ghost", "password": "whatever12"}).status_code == 401


def test_register_duplicate_409(client):
    client.post("/api/account/register", json={"username": "carol", "password": "password12"})
    r = client.post("/api/account/register", json={"username": "carol", "password": "password12"})
    assert r.status_code == 409


def test_register_weak_password_400(client):
    assert client.post("/api/account/register", json={"username": "dave", "password": "short"}).status_code == 400


def test_session_cookie_is_httponly_and_samesite(client):
    r = client.post("/api/account/register", json={"username": "erin", "password": "password12"})
    setcookie = r.headers.get("set-cookie", "").lower()
    assert "httponly" in setcookie
    assert "samesite=strict" in setcookie


def test_whoami_anonymous_is_null(client):
    w = client.get("/api/account/whoami")
    assert w.status_code == 200 and w.json()["user"] is None


def test_password_never_echoed(client):
    r = client.post("/api/account/register", json={"username": "frank", "password": "sup3rsecretpw"})
    assert "sup3rsecretpw" not in r.text


def test_overlong_password_422_does_not_echo(client):
    # Review #3: request-body validation errors must not reflect the submitted value.
    long_pw = "x" * 500  # exceeds AccountCredentials max_length
    r = client.post("/api/account/register", json={"username": "gina", "password": long_pw})
    assert r.status_code == 422
    assert long_pw not in r.text


# ---------------------------------------------------------------- 会话持久化 + 记住我 + 一键切换


def test_session_survives_process_restart(client):
    """ 根因修复：会话落盘 → 清内存（模拟服务重启）后 whoami 仍能解析。"""
    r = client.post("/api/account/register", json={"username": "hank", "password": "password12"})
    assert r.status_code == 200
    A._reset_state_for_tests()   # 进程内会话/失败计数清空 = 模拟重启；盘上快照还在
    w = client.get("/api/account/whoami")
    assert w.json()["user"]["username"] == "hank"


def test_login_response_carries_session_token(client):
    client.post("/api/account/register", json={"username": "iris", "password": "password12"})
    client.post("/api/account/logout")
    r = client.post("/api/account/login", json={"username": "iris", "password": "password12"})
    token = r.json().get("session_token")
    assert token and len(token) >= 32


def test_switch_with_valid_token(client):
    """一键切换：A 登录后再登 B（cookie 已是 B，但 A 的会话仍存活）→ 用 A 的 token 切回 A。
    注意对照 test_switch_with_destroyed_token_401：显式登出会销毁会话，那种 token 不能复活。"""
    r = client.post("/api/account/register", json={"username": "jack", "password": "password12"})
    token = r.json()["session_token"]
    client.post("/api/account/register", json={"username": "mary", "password": "password12"})
    assert client.get("/api/account/whoami").json()["user"]["username"] == "mary"
    s = client.post("/api/account/switch", json={"token": token})
    assert s.status_code == 200 and s.json()["user"]["username"] == "jack"
    assert client.get("/api/account/whoami").json()["user"]["username"] == "jack"


def test_switch_with_garbage_token_401(client):
    assert client.post("/api/account/switch", json={"token": "garbage-token"}).status_code == 401


def test_switch_with_destroyed_token_401(client):
    """登出即销毁（含落盘快照）：旧 token 再走 switch 必须 401，不能复活。"""
    r = client.post("/api/account/register", json={"username": "kate", "password": "password12"})
    token = r.json()["session_token"]
    client.post("/api/account/logout")
    A._reset_state_for_tests()   # 连内存也清掉，只靠盘——盘上也不该有
    assert client.post("/api/account/switch", json={"token": token}).status_code == 401


def test_remember_false_cookie_has_no_max_age(client):
    r = client.post("/api/account/register",
                    json={"username": "liam", "password": "password12", "remember": False})
    setcookie = r.headers.get("set-cookie", "").lower()
    assert "max-age" not in setcookie
    r2 = client.post("/api/account/register",
                     json={"username": "mona", "password": "password12"})
    assert "max-age" in r2.headers.get("set-cookie", "").lower()
