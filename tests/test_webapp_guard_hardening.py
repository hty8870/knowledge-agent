"""BIODATA_REQUIRE_ACCOUNT=1（guard on）下把过宽端点/响应收口。

钉死两类行为：
- guard on：health 无 install_root 键、登录/注册 per-IP 节流（login 10/分钟、register 5/分钟）、
  register/login 不下发 session_token、switch 403、local-model install/cancel 403（status 仍 200）、
  telemetry 中继两端点 403、download 五端点 403、自定义 base_url 一律 400、introduction?llm=1
  计入账号日配额、cookie Secure 口（BIODATA_COOKIE_SECURE=1，独立于护栏开关）。
- guard off（本机形态）对照组：以上全部逐字节不变——health 有 install_root、register 下发
  session_token、switch 可用、download/plan 与 telemetry 端点照常、不加节流。

套路沿用 test_webapp_account_guard.py：TestClient + tmp 账号/会话/配额文件；进程内限流桶
`_rate_buckets` 每个测试前后清空（桶按 `前缀:IP` 共享，测试间必须隔离）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import llm_quota  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

# 测试专用假邀请码（非真实秘密；真实部署码绝不进仓库）。
TEST_INVITE = "test-invite-not-a-real-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    A._reset_state_for_tests()
    webapp._rate_buckets.clear()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(tmp_path / "llm_quota.json"))
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("BIODATA_INVITE_CODE", raising=False)
    monkeypatch.delenv("BIODATA_COOKIE_SECURE", raising=False)
    with TestClient(webapp.app, base_url="http://127.0.0.1") as c:
        yield c
    webapp._rate_buckets.clear()
    A._reset_state_for_tests()


def _guard_on(monkeypatch, invite=True):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    if invite:
        monkeypatch.setenv("BIODATA_INVITE_CODE", TEST_INVITE)


def _register(client, username="alice", invite=TEST_INVITE):
    return client.post("/api/account/register", json={
        "username": username, "password": "password12", "invite_code": invite})


# ---------------------------------------------------------------- health 脱敏


def test_guard_on_health_hides_install_root(client, monkeypatch):
    _guard_on(monkeypatch)
    body = client.get("/api/health").json()
    assert body["ok"] is True
    assert "install_root" not in body            # 整条 key 不出现（非置空）
    assert body["account"]["required"] is True   # 前端 gate 源不受影响
    # 匿名再收敛 llm_server 出口细节，只留可用性布尔（登录后回全量）
    assert "provider" not in body["llm_server"] and "base_url" not in body["llm_server"]
    assert "key_detected" in body["llm_server"] and "available" in body["llm_server"]["trial"]


def test_guard_off_health_keeps_install_root(client):
    body = client.get("/api/health").json()
    assert "install_root" in body and body["install_root"]


# ---------------------------------------------------------------- 登录/注册 per-IP 节流


def test_guard_on_login_rate_limited_11th_429(client, monkeypatch):
    """login 10 次/分钟/IP：前 10 次（用户不存在 → 401）照过，第 11 次 429。
    每次用不同用户名，避开 accounts 自身的按用户名失败锁定，只测 per-IP 桶。"""
    _guard_on(monkeypatch)
    codes = [
        client.post("/api/account/login", json={
            "username": f"nouser{i:02d}", "password": "password12"}).status_code
        for i in range(11)
    ]
    assert codes[:10] == [401] * 10, codes
    assert codes[10] == 429
    assert "过于频繁" in client.post("/api/account/login", json={
        "username": "nouser99", "password": "password12"}).json()["detail"]


def test_guard_on_register_rate_limited_6th_429(client, monkeypatch):
    """register 5 次/分钟/IP：未配邀请码时前 5 次 403（节流在邀请校验之前，同样计数），第 6 次 429。"""
    _guard_on(monkeypatch, invite=False)
    codes = [
        client.post("/api/account/register", json={
            "username": f"user{i:02d}", "password": "password12"}).status_code
        for i in range(6)
    ]
    assert codes[:5] == [403] * 5, codes
    assert codes[5] == 429


def test_guard_off_login_register_not_throttled(client):
    """对照组：闸关完全不加节流——连续 6 次注册、11 次登录没有任何 429。"""
    for i in range(6):
        r = client.post("/api/account/register", json={
            "username": f"user{i:02d}", "password": "password12"})
        assert r.status_code == 200, r.text
    codes = [
        client.post("/api/account/login", json={
            "username": f"nouser{i:02d}", "password": "password12"}).status_code
        for i in range(11)
    ]
    assert 429 not in codes


# ---------------------------------------------------------------- session_token 收口 + switch 关闭


def test_guard_on_register_login_no_session_token(client, monkeypatch):
    _guard_on(monkeypatch)
    r = _register(client)
    assert r.status_code == 200, r.text
    assert "session_token" not in r.json()
    client.post("/api/account/logout")
    r = client.post("/api/account/login", json={"username": "alice", "password": "password12"})
    assert r.status_code == 200, r.text
    assert "session_token" not in r.json()


def test_guard_on_switch_403(client, monkeypatch):
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    r = client.post("/api/account/switch", json={"token": "whatever"})
    assert r.status_code == 403
    assert "一键切换" in r.json()["detail"]


def test_guard_off_switch_still_works(client):
    """对照组：闸关时 register 下发 session_token，switch 凭它换发会话。"""
    r = _register(client, invite=None)
    assert r.status_code == 200, r.text
    token = r.json().get("session_token")
    assert token
    sw = client.post("/api/account/switch", json={"token": token})
    assert sw.status_code == 200 and sw.json()["user"]["username"] == "alice"


# ---------------------------------------------------------------- cookie Secure 口


def test_cookie_secure_off_by_default(client):
    _register(client, invite=None)
    cookie = client.cookies.get(A.SESSION_COOKIE)
    assert cookie  # 会话已建立
    set_cookie = client.post("/api/account/login", json={
        "username": "alice", "password": "password12"}).headers.get("set-cookie", "")
    assert "Secure" not in set_cookie
    assert "HttpOnly" in set_cookie and "strict" in set_cookie.lower()


def test_cookie_secure_when_env_on(client, monkeypatch):
    monkeypatch.setenv("BIODATA_COOKIE_SECURE", "1")
    set_cookie = _register(client, invite=None).headers.get("set-cookie", "")
    assert "Secure" in set_cookie


# ---------------------------------------------------------------local-model install/cancel


def test_guard_on_local_model_install_cancel_403_status_200(client, monkeypatch):
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    assert client.get("/api/local-model/status").status_code == 200   # 只读保留
    for path in ("/api/local-model/install", "/api/local-model/cancel"):
        r = client.post(path)
        assert r.status_code == 403, path
        assert "在线向量服务" in r.json()["detail"]


# ---------------------------------------------------------------- B7：telemetry 中继两端点


def test_guard_on_telemetry_relay_403(client, monkeypatch):
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    r = client.get("/api/telemetry/mcp-calls")
    assert r.status_code == 403 and "中继" in r.json()["detail"]
    r = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 0})
    assert r.status_code == 403 and "中继" in r.json()["detail"]


def test_guard_off_telemetry_relay_open(client):
    """对照组：闸关时两端点照常（读取端 200 空页；ack 0 不落盘）。"""
    r = client.get("/api/telemetry/mcp-calls")
    assert r.status_code == 200 and r.json()["ok"] is True
    r = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 0})
    assert r.status_code == 200 and r.json()["ok"] is True


# ---------------------------------------------------------------- B8：download 五端点


def test_guard_on_download_endpoints_403(client, monkeypatch):
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    posts = [
        ("/api/download/plan", {"uids": []}),
        ("/api/download/start", {"uids": []}),
        ("/api/download/cancel", {"job_id": "x"}),
        ("/api/download/update", {"add": [], "remove": ["x"]}),
    ]
    for path, payload in posts:
        r = client.post(path, json=payload)
        assert r.status_code == 403, path
        assert "任务包" in r.json()["detail"]
    r = client.get("/api/download/status", params={"job": "x"})
    assert r.status_code == 403 and "任务包" in r.json()["detail"]


def test_guard_off_download_plan_open(client):
    """对照组：闸关时 download/plan 不 403（未知 uid → 200 + unsupported 清单，零网络零落盘）。"""
    r = client.post("/api/download/plan", json={"uids": ["test:nonexistent-uid"]})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True


# ---------------------------------------------------------------- B9：自定义 base_url 禁入


def test_guard_on_custom_base_url_400(client, monkeypatch):
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    r = client.post("/api/diagnose", json={
        "provider": "openai-compatible", "base_url": "https://api.deepseek.com"})
    assert r.status_code == 400
    assert "自定义接口地址" in r.json()["detail"]


def test_guard_off_validate_endpoint_url_unchanged(client):
    """对照组：闸关时 _validate_endpoint_url 行为不变——合法公网 https 地址照过。"""
    assert webapp._validate_endpoint_url("https://api.deepseek.com") == "https://api.deepseek.com"
    assert webapp._validate_endpoint_url("") == ""


# ---------------------------------------------------------------introduction?llm=1 计入账号日配额


def _stub_introduction(monkeypatch, cfg):
    """把 introduction 的语料定位/item 构造/LLM 出口全部替身掉，只留配额闸真跑。"""
    monkeypatch.setattr(webapp, "locate_record", lambda *a, **k: (object(), None))
    monkeypatch.setattr(
        webapp, "_web_item_from_record",
        lambda record, include_introduction=False: {"introduction": {"stub": True}})
    monkeypatch.setattr(webapp, "load_llm_config", lambda **kw: cfg)
    from dataset_recommender.llm import intro_llm
    monkeypatch.setattr(
        intro_llm, "enrich_introduction_with_llm",
        lambda item, intro, config: intro)


def test_guard_on_introduction_llm_counts_quota(client, monkeypatch):
    """护栏模式 + llm=1 + 服务端有 key → 真消耗 → 计入账号日配额（此前只过进程内频率桶）。"""
    _guard_on(monkeypatch)
    assert _register(client).status_code == 200
    _stub_introduction(monkeypatch, LLMConfig(
        enable_llm=True, provider="openai-compatible", api_key="server-key"))
    r = client.get("/api/introduction", params={"uid": "x", "llm": 1})
    assert r.status_code == 200, r.text
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 1
    # 服务端无 key → LLM 不会真烧 → 不计（与 _gate_llm_quota 既有口径一致）
    _stub_introduction(monkeypatch, LLMConfig(
        enable_llm=True, provider="openai-compatible", api_key=None))
    r = client.get("/api/introduction", params={"uid": "x", "llm": 1})
    assert r.status_code == 200, r.text
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 1


def test_guard_off_introduction_llm_no_quota(client, monkeypatch):
    """对照组：闸关时 llm=1 不进配额（_gate_llm_quota 第一行即返），行为与现状一致。"""
    _stub_introduction(monkeypatch, LLMConfig(
        enable_llm=True, provider="openai-compatible", api_key="server-key"))
    r = client.get("/api/introduction", params={"uid": "x", "llm": 1})
    assert r.status_code == 200, r.text
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0
