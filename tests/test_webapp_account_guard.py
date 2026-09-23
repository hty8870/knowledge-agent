"""网页版账号护栏：登录强制门、注册邀请码、账号级 LLM 日配额、限量试用通道。

全部 additive：闸关（缺省）= 本机单机形态逐字节不变；本文件钉死闸开后的 401/403/429
三态、「BYOK / mock / 未启用 / 服务端无 key 一律不计数」口径，以及 trial 通道的
锁定语义（请求级 key/端点/模型一律忽略——`_normalize_provider("trial")` 曾兜底成
zhipuai 让试用请求错烧正式 key，本文件的 normalize/覆盖链两条测试就是防它回归）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request as _RawRequest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import llm_quota  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig, load_llm_config  # noqa: E402

TRIAL_KEY = "sk-trial-unit-test"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    A._reset_state_for_tests()
    # 公网护栏硬化批给 login/register 加了 per-IP 进程内节流（guard on 才生效）；桶是模块级
    # 共享状态，测试间必须清，否则跨用例累计触发 429（本文件的注册/登录远超 5/10 次）。
    webapp._rate_buckets.clear()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(tmp_path / "llm_quota.json"))
    with TestClient(webapp.app, base_url="http://127.0.0.1") as c:
        yield c
    webapp._rate_buckets.clear()
    A._reset_state_for_tests()


def _register(client, username="alice", invite=None):
    payload = {"username": username, "password": "password12"}
    if invite is not None:
        payload["invite_code"] = invite
    return client.post("/api/account/register", json=payload)


def _raw_request(session_token: str | None = None) -> _RawRequest:
    """最小 starlette Request（`_gate_llm_quota` 只读 cookies）。
    注意 cookie 头必须是 `name=value` 整串——只塞裸 token 会被解析成无名 cookie。"""
    headers = (
        [(b"cookie", f"{A.SESSION_COOKIE}={session_token}".encode())]
        if session_token else []
    )
    return _RawRequest({
        "type": "http", "method": "POST", "path": "/api/x", "query_string": b"",
        "headers": headers, "client": ("127.0.0.1", 1), "server": ("t", 80), "scheme": "http",
    })


def _server_cfg(provider="openai-compatible", key="server-key"):
    return LLMConfig(enable_llm=True, provider=provider, api_key=key)


def _gate(request, **kw):
    defaults = dict(cfg=_server_cfg(), provider="openai-compatible",
                    use_llm=True, mock_llm=False, api_key=None)
    defaults.update(kw)
    webapp._gate_llm_quota(request, **defaults)


# ---------------------------------------------------------------- 登录强制门（HTTP 层）


def test_gate_off_anonymous_interpret_passes(client, monkeypatch):
    """闸关（缺省）= 现状逐字节不变：匿名调 /api/* 照常。"""
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    r = client.post("/api/interpret", json={"query": "human breast cancer"})
    assert r.status_code == 200


def test_gate_on_anonymous_api_401(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    r = client.post("/api/interpret", json={"query": "human breast cancer"})
    assert r.status_code == 401
    body = r.json()
    assert body["ok"] is False and body["error"] == "auth_required"


def test_gate_on_whitelist_paths_open(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    assert client.get("/api/health").status_code == 200
    w = client.get("/api/account/whoami")
    assert w.status_code == 200 and w.json()["user"] is None


def test_gate_on_whitelist_trailing_slash_normalized(client, monkeypatch):
    """白名单比对按 rstrip('/') 归一：/api/health/ 不得被 401（路由层自行 307 归一）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    assert client.get("/api/health/").status_code == 200


def test_gate_on_static_page_not_blocked(client, monkeypatch):
    """门只拦 /api/ 前缀：静态前端（登录页本身）必须可达。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    assert client.get("/").status_code == 200


def test_gate_on_garbage_cookie_401(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    client.cookies.set(A.SESSION_COOKIE, "garbage-token")
    assert client.post("/api/interpret", json={"query": "x"}).status_code == 401


def test_gate_on_registered_session_passes(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    assert _register(client, invite="code-xyz").status_code == 200
    assert client.post("/api/interpret", json={"query": "human breast cancer"}).status_code == 200


def test_health_reports_account_gate_and_trial(client, monkeypatch):
    """health 的 additive 字段是前端登录门/邀请框/试用预设的判定源；只报有无，绝不回显码与 key。
     匿名在护栏下只见可用性布尔（provider/base_url/模型名登录后才回）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    body = client.get("/api/health").json()
    assert body["account"] == {"required": True, "invite": True}
    trial = body["llm_server"]["trial"]
    assert trial["available"] is True
    assert trial["daily_limit"] == 30
    assert "model" not in trial                       # 匿名不回模型名
    assert "provider" not in body["llm_server"]       # 匿名不回服务端出口
    assert "base_url" not in body["llm_server"]
    # 登录后 health 回全量（一致性比对/试用模型名上屏的数据源）
    assert _register(client, invite="code-xyz").status_code == 200
    body = client.get("/api/health").json()
    trial = body["llm_server"]["trial"]
    assert trial["available"] is True
    assert trial["model"] == "deepseek-v4-flash"
    assert body["llm_server"]["provider"] and body["llm_server"]["base_url"]
    assert TRIAL_KEY not in json.dumps(body) and "code-xyz" not in json.dumps(body)


def test_health_gate_off_defaults(client, monkeypatch):
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.delenv("BIODATA_INVITE_CODE", raising=False)
    monkeypatch.delenv("BIODATA_TRIAL_API_KEY", raising=False)
    body = client.get("/api/health").json()
    assert body["account"] == {"required": False, "invite": False}
    assert body["llm_server"]["trial"]["available"] is False


# ---------------------------------------------------------------- 注册邀请码（HTTP 层）


def test_invite_ignored_when_gate_off(client, monkeypatch):
    """闸关时 invite_code 字段被忽略：配了邀请码环境变量也不影响本机注册。"""
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    assert _register(client).status_code == 200


def test_gate_on_invite_unconfigured_closes_registration(client, monkeypatch):
    """护栏模式 + 未配置邀请码 → 注册整体关闭（宁可关死：公网开放注册 = 任何人烧服务端 key）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.delenv("BIODATA_INVITE_CODE", raising=False)
    r = _register(client, invite="whatever")
    assert r.status_code == 403
    assert "暂未开放注册" in r.json()["detail"]


def test_gate_on_wrong_invite_403(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    assert _register(client, invite="code-abc").status_code == 403
    assert _register(client).status_code == 403   # 缺失与错误同文案同状态
    assert _register(client, invite="code-xyz").status_code == 200


def test_gate_on_login_needs_no_invite(client, monkeypatch):
    """邀请码只管注册不管登录：既有账号正常登入。"""
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    assert _register(client, invite="code-xyz").status_code == 200
    client.post("/api/account/logout")
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    r = client.post("/api/account/login", json={"username": "alice", "password": "password12"})
    assert r.status_code == 200


# ---------------------------------------------------------------- 配额闸（单元层：_gate_llm_quota 直调）


@pytest.fixture()
def session_cookie(client, monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    assert _register(client, invite="code-xyz").status_code == 200
    return client.cookies.get(A.SESSION_COOKIE)


def test_quota_not_counted_when_gate_off(client, monkeypatch, session_cookie):
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    _gate(_raw_request(session_cookie))
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0


def test_quota_byok_not_counted(client, monkeypatch, session_cookie):
    _gate(_raw_request(session_cookie), api_key="user-own-key")
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0


def test_quota_mock_and_disabled_and_keyless_not_counted(client, monkeypatch, session_cookie):
    req = _raw_request(session_cookie)
    _gate(req, provider="mock", cfg=_server_cfg(provider="mock"))
    _gate(req, use_llm=False)
    _gate(req, cfg=_server_cfg(key=None))           # 服务端无 key → 不会真烧
    _gate(req, cfg=LLMConfig(enable_llm=False, provider="openai-compatible", api_key="k"))
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0


def test_quota_counts_then_429(client, monkeypatch, session_cookie):
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "2")
    req = _raw_request(session_cookie)
    _gate(req)
    _gate(req)
    with pytest.raises(HTTPException) as excinfo:
        _gate(req)
    assert excinfo.value.status_code == 429
    assert "上限" in excinfo.value.detail
    # 超限不加计数：账本停在 2
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 2


def test_quota_exempt_user_never_limited(client, monkeypatch, session_cookie):
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "1")
    monkeypatch.setenv("BIODATA_LLM_QUOTA_EXEMPT", "alice")
    req = _raw_request(session_cookie)
    _gate(req)
    _gate(req)   # 第二次也不 429
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0  # 豁免不计


def test_quota_global_limit_independent(client, monkeypatch, session_cookie):
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "100")
    monkeypatch.setenv("BIODATA_LLM_DAILY_GLOBAL", "1")
    req = _raw_request(session_cookie)
    _gate(req)
    with pytest.raises(HTTPException) as excinfo:
        _gate(req)
    assert excinfo.value.status_code == 429
    assert "熔断" in excinfo.value.detail


def test_quota_trial_bucket_independent(client, monkeypatch, session_cookie):
    """试用桶（更紧）与正式桶互不挤占：trial 限 1，正式通道不受影响。"""
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_PER_USER", "1")
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "100")
    req = _raw_request(session_cookie)
    _gate(req, provider="trial", cfg=_server_cfg(provider="trial", key=TRIAL_KEY))
    with pytest.raises(HTTPException):
        _gate(req, provider="trial", cfg=_server_cfg(provider="trial", key=TRIAL_KEY))
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=True) == 1
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 0
    _gate(req)   # 正式通道照过


def test_quota_trial_429_message_mentions_trial(client, monkeypatch, session_cookie):
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_PER_USER", "0")  # 0 = 不限
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_GLOBAL", "1")
    req = _raw_request(session_cookie)
    _gate(req, provider="trial", cfg=_server_cfg(provider="trial", key=TRIAL_KEY))
    with pytest.raises(HTTPException) as excinfo:
        _gate(req, provider="trial", cfg=_server_cfg(provider="trial", key=TRIAL_KEY))
    assert excinfo.value.status_code == 429


# ---------------------------------------------------------------- 配额闸（HTTP 集成：/api/dream）


def test_dream_quota_429_surfaces_over_http(client, monkeypatch):
    """HTTP 全链：注册 → 第一次 dream 200（真计数）→ 第二次 429。
    LLM 出口替身掉，只验证闸门与计数（网络层另有 llm_client 测试）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    monkeypatch.setenv("BIODATA_LLM_DAILY_PER_USER", "1")
    fake = _server_cfg()
    monkeypatch.setattr(webapp, "load_llm_config", lambda **kw: fake)
    monkeypatch.setattr(
        webapp.dream, "dream_from_conversations",
        lambda conversations, config: {"candidates": [], "generated": False})
    assert _register(client, invite="code-xyz").status_code == 200
    payload = {"conversations": [], "provider": "openai-compatible"}
    assert client.post("/api/dream", json=payload).status_code == 200
    r = client.post("/api/dream", json=payload)
    assert r.status_code == 429
    assert llm_quota.usage_snapshot(webapp.PROJECT_ROOT, "alice", trial=False) == 1


# ---------------------------------------------------------------- llm_quota 账本（纯单元）


def test_ledger_corrupt_file_rebuilt(tmp_path):
    bad = tmp_path / "q.json"
    bad.write_bytes(b"{not json")
    llm_quota.check_and_increment(
        tmp_path, "alice", trial=False, per_user_limit=5, global_limit=5, quota_path=bad)
    assert llm_quota.usage_snapshot(tmp_path, "alice", trial=False, quota_path=bad) == 1


def test_ledger_storage_failure_fail_open(tmp_path):
    """账本写不进（路径是目录）→ 放行不抛（可用性优先，provider 侧消费上限兜底）。"""
    llm_quota.check_and_increment(
        tmp_path, "alice", trial=False, per_user_limit=1, global_limit=1,
        quota_path=tmp_path)   # 目录路径：读失败→空账，os.replace 到目录→故障→放行
    llm_quota.check_and_increment(
        tmp_path, "alice", trial=False, per_user_limit=1, global_limit=1, quota_path=tmp_path)


def test_ledger_drops_old_days(tmp_path):
    path = tmp_path / "q.json"
    path.write_text(json.dumps({"days": {"2000-01-01": {"u:alice": 9, "global": 9}}}), encoding="utf-8")
    llm_quota.check_and_increment(
        tmp_path, "alice", trial=False, per_user_limit=5, global_limit=5, quota_path=path)
    days = json.loads(path.read_text(encoding="utf-8"))["days"]
    assert "2000-01-01" not in days and len(days) == 1


def test_ledger_zero_limit_means_unlimited(tmp_path):
    path = tmp_path / "q.json"
    for _ in range(3):
        llm_quota.check_and_increment(
            tmp_path, "alice", trial=False, per_user_limit=0, global_limit=0, quota_path=path)
    assert llm_quota.usage_snapshot(tmp_path, "alice", trial=False, quota_path=path) == 3


# ---------------------------------------------------------------- trial 通道配置（llm_client + webapp 覆盖链）


def test_normalize_provider_recognizes_trial():
    """回归钉：trial 曾兜底成 zhipuai —— 试用请求会错烧正式 key。"""
    assert webapp._normalize_provider("trial") == "trial"
    assert webapp._normalize_provider(" Trial ") == "trial"


def test_trial_config_defaults_and_thinking_off(monkeypatch, tmp_path):
    """trial 默认使用 DeepSeek，且关闭会与工具选择冲突的思考档。"""
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.delenv("ENABLE_LLM", raising=False)
    monkeypatch.delenv("BIODATA_TRIAL_THINKING", raising=False)
    cfg = load_llm_config(project_root=tmp_path, provider_override="trial")
    assert cfg.provider == "trial"
    assert cfg.api_key == TRIAL_KEY
    assert cfg.base_url == "https://api.deepseek.com/v1"
    assert cfg.model == "deepseek-v4-flash"
    assert cfg.thinking is False
    assert cfg.enable_llm is True


def test_trial_config_never_falls_back_to_embed_key(monkeypatch, tmp_path):
    """试用凭据不回落 embedding key；不同服务商的 key 不得串用。"""
    monkeypatch.delenv("BIODATA_TRIAL_API_KEY", raising=False)
    monkeypatch.setenv("BIODATA_EMBED_API_KEY", "embed-key-not-for-trial")
    cfg = load_llm_config(project_root=tmp_path, provider_override="trial")
    assert cfg.api_key is None


def test_trial_config_thinking_escape_hatch(monkeypatch, tmp_path):
    """BIODATA_TRIAL_THINKING 逃逸口：enabled→True、none→None（不发参数，始终思考的
    模型用）；disabled/未设/未知值 → False（默认 deepseek-v4-flash 需要 disabled）。"""
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("BIODATA_TRIAL_THINKING", "enabled")
    assert load_llm_config(project_root=tmp_path, provider_override="trial").thinking is True
    monkeypatch.setenv("BIODATA_TRIAL_THINKING", "none")
    assert load_llm_config(project_root=tmp_path, provider_override="trial").thinking is None
    monkeypatch.setenv("BIODATA_TRIAL_THINKING", "disabled")
    assert load_llm_config(project_root=tmp_path, provider_override="trial").thinking is False
    monkeypatch.setenv("BIODATA_TRIAL_THINKING", "bogus")
    assert load_llm_config(project_root=tmp_path, provider_override="trial").thinking is False


def test_trial_config_never_falls_back_to_generic_keys(monkeypatch, tmp_path):
    """试用凭据只认 BIODATA_TRIAL_API_KEY：LLM_API_KEY/OPENAI_API_KEY 绝不变成试用通道凭据。"""
    monkeypatch.delenv("BIODATA_TRIAL_API_KEY", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "sk-not-for-trial")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-not-for-trial-either")
    cfg = load_llm_config(project_root=tmp_path, provider_override="trial")
    assert cfg.api_key is None


def test_trial_config_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("BIODATA_TRIAL_BASE_URL", "https://trial-gateway.example/v1")
    monkeypatch.setenv("BIODATA_TRIAL_MODEL", "trial-model-x")
    cfg = load_llm_config(project_root=tmp_path, provider_override="trial")
    assert cfg.base_url == "https://trial-gateway.example/v1"
    assert cfg.model == "trial-model-x"


def test_trial_request_overrides_lock_channel(monkeypatch, tmp_path):
    """端到端覆盖链：请求带 key/端点/模型全被锁掉，服务端试用 key 生效、正式 key 被遮罩。"""
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("LLM_API_KEY", "sk-server-zhipu")
    overrides = webapp._build_request_overrides(
        provider="trial", use_llm=True, mock_llm=False,
        api_key="sk-user-trying-to-inject", base_url="https://evil.example/v1",
        model="evil-model", server_provider="zhipuai",
        server_base_url="https://open.bigmodel.cn/api/paas/v4/")
    with webapp._temporary_env(overrides):
        cfg = load_llm_config(project_root=tmp_path)
    assert cfg.provider == "trial"
    assert cfg.api_key == TRIAL_KEY                        # 请求级 key 被忽略
    assert cfg.base_url == "https://api.deepseek.com/v1"         # 请求级端点被锁掉
    assert cfg.model == "deepseek-v4-flash"                      # 请求级模型被锁掉
    assert cfg.thinking is False


def test_trial_request_masks_server_formal_key(monkeypatch, tmp_path):
    """试用请求绝不顺带把服务端正式 key 漏进配置（provider 切换遮罩生效）。"""
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("ZHIPUAI_API_KEY", "sk-server-zhipu")
    monkeypatch.setenv("ZAI_API_KEY", "sk-server-zhipu")
    overrides = webapp._build_request_overrides(
        provider="trial", use_llm=True, mock_llm=False, api_key=None,
        base_url=None, model=None, server_provider="zhipuai",
        server_base_url="https://open.bigmodel.cn/api/paas/v4/")
    assert overrides.get("ZHIPUAI_API_KEY") == "" and overrides.get("LLM_API_KEY") == ""
    assert "BIODATA_TRIAL_API_KEY" not in overrides   # 试用 key 绝不进遮罩名单
    assert "BIODATA_EMBED_API_KEY" not in overrides   # embed key 同样绝不进遮罩名单


# ---------------------------------------------------------------- trial 额度回显端点（/api/account/trial-quota）


def test_trial_quota_endpoint_404_when_gate_off(client, monkeypatch):
    """本机单机形态（闸关）：端点不存在——试用通道本就是部署形态产物。"""
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    assert client.get("/api/account/trial-quota").status_code == 404


def test_trial_quota_endpoint_401_anonymous(client, monkeypatch):
    """护栏形态未登录：中间件 401（端点不进白名单）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    r = client.get("/api/account/trial-quota")
    assert r.status_code == 401 and r.json()["error"] == "auth_required"


def test_trial_quota_endpoint_reports_remaining(client, monkeypatch):
    """已登录：如实回显可用性/锁定模型/已用/剩余；key 绝不回显。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("BIODATA_TRIAL_DAILY_PER_USER", "30")
    assert _register(client, invite="code-xyz").status_code == 200
    # 手工记 4 轮试用账（与 _gate_llm_quota 同口径：trial 独立桶）
    for _ in range(4):
        llm_quota.check_and_increment(
            webapp.PROJECT_ROOT, "alice", trial=True, per_user_limit=30, global_limit=500)
    r = client.get("/api/account/trial-quota")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is True
    assert body["model"] == "deepseek-v4-flash"
    assert body["daily_limit"] == 30 and body["used"] == 4 and body["remaining"] == 26
    assert body["unlimited"] is False
    assert TRIAL_KEY not in r.text   # key 绝不回显


def test_trial_quota_endpoint_unavailable_without_server_key(client, monkeypatch):
    """服务端没配试用 key：available=False（前端按通道不可用隐藏额度块）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    monkeypatch.delenv("BIODATA_TRIAL_API_KEY", raising=False)
    assert _register(client, invite="code-xyz").status_code == 200
    body = client.get("/api/account/trial-quota").json()
    assert body["available"] is False


def test_trial_quota_endpoint_exempt_user_unlimited(client, monkeypatch):
    """豁免名单账号：remaining=None + unlimited=True（前端显示「不限量」）。"""
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")
    monkeypatch.setenv("BIODATA_TRIAL_API_KEY", TRIAL_KEY)
    monkeypatch.setenv("BIODATA_LLM_QUOTA_EXEMPT", "alice")
    assert _register(client, invite="code-xyz").status_code == 200
    body = client.get("/api/account/trial-quota").json()
    assert body["unlimited"] is True and body["remaining"] is None
