# -*- coding: utf-8 -*-
"""在线 MCP 形态（2026-08-28，webapp 挂载 `/mcp` + Bearer 令牌）全链路测试。

三层覆盖：
① `mcp_tokens` 令牌库单测——铸币/解析/列表/吊销回环、落盘无明文（只存 sha256 摘要）、
   每账户 5 上限、跨账户吊销不得、损坏文件 fail-open、拒绝落仓库 database/；
② `scope_gate` + `_enforce_online_policy` 单测——LLM 成本闸（隐式路径 `should_use_llm`
   判否降级；显式 LLM 参数推离安全档 → ToolError 显式拒绝）与在线禁用工具表；
③ HTTP 集成——令牌管理三端点（护栏关 404 / 无会话 401 / 铸币→列表→吊销）与
   `/mcp` 端点（无令牌 401 / 坏令牌 401 / 真令牌 initialize 握手 + tools/list 不见
   禁用工具 + 显式 LLM 参数被 isError 拒绝 + 吊销后 401）。

fixture 先例照抄 test_webapp_account_guard.py：账户/会话/令牌库全部指到 tmp_path，
模块级缓存（accounts / mcp_tokens / _rate_buckets）进出都清。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.agent.action_plan import should_use_llm  # noqa: E402
from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import mcp_server  # noqa: E402
from dataset_recommender.app import mcp_tokens  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402
from dataset_recommender.llm.scope_gate import force_llm_off  # noqa: E402


@pytest.fixture()
def token_store(tmp_path, monkeypatch):
    """令牌库指到 tmp_path 并清进程内缓存（`_hydrate` 非空不重读，测试间必须清）。"""
    path = tmp_path / "mcp_tokens.json"
    monkeypatch.setenv("BIODATA_MCP_TOKENS_FILE", str(path))
    mcp_tokens._reset_state_for_tests()
    yield path
    mcp_tokens._reset_state_for_tests()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    A._reset_state_for_tests()
    webapp._rate_buckets.clear()
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "accounts.json"))
    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(tmp_path / "sessions.json"))
    monkeypatch.setenv("BIODATA_LLM_QUOTA_FILE", str(tmp_path / "llm_quota.json"))
    monkeypatch.setenv("BIODATA_MCP_TOKENS_FILE", str(tmp_path / "mcp_tokens.json"))
    mcp_tokens._reset_state_for_tests()
    # `with` 才会跑 lifespan——/mcp 的 session manager 任务组靠它起来（每次进入
    # lifespan 前 webapp 会 reset_online_runtime() 重建，测试间幂等）。
    with TestClient(webapp.app, base_url="http://127.0.0.1") as c:
        yield c
    mcp_tokens._reset_state_for_tests()
    webapp._rate_buckets.clear()
    A._reset_state_for_tests()


def _register(client, username="alice", invite="code-xyz"):
    return client.post("/api/account/register", json={
        "username": username, "password": "password12", "invite_code": invite})


def _gate_on(monkeypatch):
    monkeypatch.setenv("BIODATA_REQUIRE_ACCOUNT", "1")
    monkeypatch.setenv("BIODATA_INVITE_CODE", "code-xyz")


_MCP_HEADERS = {"Accept": "application/json, text/eventstream"}


def _mcp_post(client, body, token=None):
    headers = dict(_MCP_HEADERS)
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post("/mcp", json=body, headers=headers)


_INIT_BODY = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "pytest", "version": "1"}},
}


# ---------------------------------------------------------------- ① 令牌库单测


def test_mint_resolve_roundtrip(token_store):
    raw, rec = mcp_tokens.mint_token("acc-1", "alice", "kimi", store_path=token_store)
    assert raw.startswith("bdm_") and len(raw) > 40
    assert rec["label"] == "kimi" and rec["prefix"].startswith("bdm_")
    assert "digest" not in rec and raw not in json.dumps(rec)
    got = mcp_tokens.resolve_token(raw, store_path=token_store)
    assert got is not None and got["account_id"] == "acc-1" and got["username"] == "alice"
    assert got["token_id"] == rec["token_id"]


def test_store_file_contains_no_plaintext(token_store):
    raw, _ = mcp_tokens.mint_token("acc-1", "alice", store_path=token_store)
    body = token_store.read_text(encoding="utf-8")
    assert raw not in body                       # 落盘只有 sha256 摘要
    import hashlib
    assert hashlib.sha256(raw.encode()).hexdigest() in body


def test_resolve_rejects_unknown_and_garbage(token_store):
    assert mcp_tokens.resolve_token(None, store_path=token_store) is None
    assert mcp_tokens.resolve_token("", store_path=token_store) is None
    assert mcp_tokens.resolve_token("not-a-token", store_path=token_store) is None
    assert mcp_tokens.resolve_token("bdm_" + "x" * 43, store_path=token_store) is None


def test_list_is_scoped_per_account(token_store):
    _, rec_a = mcp_tokens.mint_token("acc-1", "alice", "a 的", store_path=token_store)
    mcp_tokens.mint_token("acc-2", "bob", store_path=token_store)
    mine = mcp_tokens.list_tokens("acc-1", store_path=token_store)
    assert [t["token_id"] for t in mine] == [rec_a["token_id"]]
    assert len(mcp_tokens.list_tokens("acc-2", store_path=token_store)) == 1
    assert mcp_tokens.list_tokens("acc-3", store_path=token_store) == []


def test_revoke_only_by_owner(token_store):
    raw, rec = mcp_tokens.mint_token("acc-1", "alice", store_path=token_store)
    assert mcp_tokens.revoke_token("acc-2", rec["token_id"], store_path=token_store) is False
    assert mcp_tokens.resolve_token(raw, store_path=token_store) is not None  # 还在
    assert mcp_tokens.revoke_token("acc-1", rec["token_id"], store_path=token_store) is True
    assert mcp_tokens.resolve_token(raw, store_path=token_store) is None      # 立即失效


def test_max_five_tokens_per_account(token_store):
    for _ in range(5):
        mcp_tokens.mint_token("acc-1", "alice", store_path=token_store)
    with pytest.raises(mcp_tokens.McpTokenError) as ei:
        mcp_tokens.mint_token("acc-1", "alice", store_path=token_store)
    assert ei.value.code == "too_many_tokens"
    # 别的账户不受限；吊销后可再开
    mcp_tokens.mint_token("acc-2", "bob", store_path=token_store)
    victim = mcp_tokens.list_tokens("acc-1", store_path=token_store)[0]
    mcp_tokens.revoke_token("acc-1", victim["token_id"], store_path=token_store)
    mcp_tokens.mint_token("acc-1", "alice", store_path=token_store)


def test_corrupt_store_file_fails_open(token_store):
    token_store.write_text("{{{ not json", encoding="utf-8")
    assert mcp_tokens.list_tokens("acc-1", store_path=token_store) == []
    assert mcp_tokens.resolve_token("bdm_xxx", store_path=token_store) is None


def test_store_path_never_inside_repo_database(monkeypatch):
    repo_db = Path(mcp_tokens.__file__).resolve().parents[3] / "database" / "x.json"
    monkeypatch.setenv("BIODATA_MCP_TOKENS_FILE", str(repo_db))
    with pytest.raises(mcp_tokens.McpTokenError) as ei:
        mcp_tokens.default_tokens_path(ROOT)
    assert ei.value.code == "bad_store_path"


# ---------------------------------------------------------------- ② LLM 成本闸 + 在线策略


def test_should_use_llm_forced_off_scope():
    cfg = LLMConfig(enable_llm=True, provider="zhipuai", api_key="sk-x")
    assert should_use_llm(cfg) == (True, "ready")     # 闸外：历史行为逐字节不变
    with force_llm_off():
        assert should_use_llm(cfg) == (False, "online_forced_off")
    assert should_use_llm(cfg) == (True, "ready")     # 出作用域即恢复


def test_enforce_online_policy_disabled_tools():
    for tool in ("provision_dataset", "verify_local_assets"):
        with pytest.raises(ToolError, match="online_tool_disabled"):
            mcp_server._enforce_online_policy(tool, {})


def test_enforce_online_policy_llm_params():
    with pytest.raises(ToolError, match="online_llm_disabled"):
        mcp_server._enforce_online_policy("recommend_datasets",
                                          {"query": "q", "use_llm": True})
    with pytest.raises(ToolError, match="online_llm_disabled"):
        mcp_server._enforce_online_policy("recommend_datasets",
                                          {"query": "q", "rerank": "llm"})
    with pytest.raises(ToolError, match="online_llm_disabled"):
        mcp_server._enforce_online_policy("biodata_llm_status", {"check_connection": True})
    # 安全档（含缺省）放行；不在表里的工具（如 plan_action）参数层放行
    mcp_server._enforce_online_policy("recommend_datasets", {"query": "q"})
    mcp_server._enforce_online_policy(
        "recommend_datasets",
        {"query": "q", "use_llm": False, "rerank": "off", "strategy": "fixed"})
    mcp_server._enforce_online_policy("plan_action", {"utterance": "x"})


# ---------------------------------------------------------------- ③ HTTP 集成：令牌管理端点


def test_mint_endpoint_404_when_gate_off(client, monkeypatch):
    monkeypatch.delenv("BIODATA_REQUIRE_ACCOUNT", raising=False)
    assert client.post("/api/account/mcp-token", json={}).status_code == 404
    assert client.get("/api/account/mcp-tokens").status_code == 404
    assert client.post("/api/account/mcp-token/revoke", json={"token_id": "x"}).status_code == 404


def test_mint_endpoint_401_anonymous(client, monkeypatch):
    _gate_on(monkeypatch)
    assert client.post("/api/account/mcp-token", json={}).status_code == 401
    assert client.get("/api/account/mcp-tokens").status_code == 401


def test_token_lifecycle_over_http(client, monkeypatch):
    _gate_on(monkeypatch)
    assert _register(client).status_code == 200
    r = client.post("/api/account/mcp-token", json={"label": "kimi"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] and body["token"].startswith("bdm_")
    assert body["config"]["url"].endswith("/mcp")
    assert body["config"]["headers"]["Authorization"] == f"Bearer {body['token']}"
    # 列表只有公开字段，无明文
    lst = client.get("/api/account/mcp-tokens").json()
    assert lst["ok"] and len(lst["tokens"]) == 1
    assert lst["tokens"][0]["label"] == "kimi"
    assert body["token"] not in json.dumps(lst)
    # 吊销
    tid = lst["tokens"][0]["token_id"]
    assert client.post("/api/account/mcp-token/revoke", json={"token_id": tid}).status_code == 200
    assert client.get("/api/account/mcp-tokens").json()["tokens"] == []
    # 再吊销同一枚 → 404
    assert client.post("/api/account/mcp-token/revoke", json={"token_id": tid}).status_code == 404


def test_mint_endpoint_rejects_extra_fields(client, monkeypatch):
    """extra=forbid：令牌端点不吃未声明字段（与其它端点同款入参纪律）。"""
    _gate_on(monkeypatch)
    assert _register(client).status_code == 200
    assert client.post("/api/account/mcp-token", json={"label": "x", "admin": True}).status_code == 422


# ---------------------------------------------------------------- ③ HTTP 集成：/mcp 端点


def test_mcp_endpoint_requires_token(client, monkeypatch):
    _gate_on(monkeypatch)
    assert _register(client).status_code == 200
    # 无令牌 → 401（带 WWW-Authenticate，JSON 错误体）
    r = _mcp_post(client, _INIT_BODY)
    assert r.status_code == 401 and "WWW-Authenticate" in r.headers
    assert r.json()["error"] == "invalid_token"
    # 坏令牌 → 401
    assert _mcp_post(client, _INIT_BODY, token="bdm_garbage").status_code == 401


def test_mcp_initialize_and_tool_policy_over_http(client, monkeypatch):
    _gate_on(monkeypatch)
    assert _register(client).status_code == 200
    token = client.post("/api/account/mcp-token", json={}).json()["token"]

    # initialize 握手（json_response=True → 单次 POST 单次 JSON 应答）
    r = _mcp_post(client, _INIT_BODY, token=token)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    init_result = r.json()["result"]
    assert init_result["serverInfo"]["name"] == "biodata"

    # tools/list：在线禁用工具不得出现
    r = _mcp_post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}, token=token)
    assert r.status_code == 200
    names = {t["name"] for t in r.json()["result"]["tools"]}
    assert "recommend_datasets" in names
    assert "provision_dataset" not in names and "verify_local_assets" not in names

    # 显式 LLM 参数推离安全档 → isError（显式拒绝，不静默降级）
    r = _mcp_post(client, {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "recommend_datasets",
                   "arguments": {"query": "human breast cancer", "use_llm": True}},
    }, token=token)
    assert r.status_code == 200
    result = r.json()["result"]
    assert result["isError"] is True
    assert "online_llm_disabled" in result["content"][0]["text"]

    # 吊销后 → 立即 401
    tid = client.get("/api/account/mcp-tokens").json()["tokens"][0]["token_id"]
    client.post("/api/account/mcp-token/revoke", json={"token_id": tid})
    assert _mcp_post(client, _INIT_BODY, token=token).status_code == 401
