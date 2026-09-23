# -*- coding: utf-8 -*-
"""Redis 会话后端测试（`accounts` 的 opt-in 后端，用 fakeredis 注入，禁真网络）。

覆盖两类保证：
1. **默认形态零改动**——env 未设时走内存 + JSON 快照，且绝不触碰 Redis（假工厂被调即 fail）；
2. **Redis 后端语义与文件后端逐条对齐**——往返、TTL、未知/过期/畸形条目、fail-open 与
   fail-closed 边界、env 笔误 fail-closed、测试复位清理单例。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fakeredis
import pytest
import redis

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import accounts as A  # noqa: E402
from dataset_recommender.app import redis_store  # noqa: E402

_DAY = 24 * 3600
_KEY = "biodata:sess:{}"


@pytest.fixture(autouse=True)
def _isolate_accounts_state():
    """每个测试前后复位 accounts 与 redis_store 的单例（跨测试不串状态）。"""
    A._reset_state_for_tests()
    yield
    A._reset_state_for_tests()


@pytest.fixture()
def fake_redis(monkeypatch):
    """把共享建连接缝换成 fakeredis（不连真 Redis）。"""
    client = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(redis_store, "_build_client", lambda: client)
    return client


@pytest.fixture()
def redis_mode(monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "redis")


def test_default_env_uses_file_backend_and_never_touches_redis(tmp_path, monkeypatch):
    """env 未设 → 默认内存 + JSON 快照路径；Redis 建连绝不被触发。"""
    monkeypatch.delenv("BIODATA_SESSION_STORE", raising=False)
    monkeypatch.delenv("BIODATA_REDIS_URL", raising=False)

    def _must_not_build():
        raise AssertionError("默认形态不应建 Redis 连接")

    monkeypatch.setattr(redis_store, "_build_client", _must_not_build)

    user = A.PublicUser(id="u-default", username="alice")
    sessions = tmp_path / "sessions.json"
    token = A.create_session(user, sessions_path=sessions)
    assert A.resolve_session(token, sessions_path=sessions) == user
    assert sessions.exists()   # 落盘快照仍在（默认路径逐字节不变）


def test_redis_roundtrip_persists_json_and_ttl(fake_redis, redis_mode):
    user = A.PublicUser(id="u2", username="bob")
    token = A.create_session(user)
    assert A.resolve_session(token) == user

    key = _KEY.format(token)
    assert fake_redis.exists(key) == 1
    ttl = fake_redis.ttl(key)
    assert 0 < ttl <= 30 * _DAY
    payload = json.loads(fake_redis.get(key))
    assert payload["user_id"] == "u2"
    assert payload["username"] == "bob"
    assert payload["expires_at"] > time.time()


def test_unknown_token_and_destroy(fake_redis, redis_mode):
    assert A.resolve_session("never-issued") is None

    user = A.PublicUser(id="u3", username="carol")
    token = A.create_session(user)
    assert A.resolve_session(token) == user

    A.destroy_session(token)
    assert A.resolve_session(token) is None
    assert fake_redis.exists(_KEY.format(token)) == 0


def test_expired_entry_is_deleted(fake_redis, redis_mode):
    token = "expired-token"
    key = _KEY.format(token)
    fake_redis.set(key, json.dumps({"user_id": "u4", "username": "dave", "expires_at": time.time() - 10}))
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0


def test_malformed_entry_is_deleted(fake_redis, redis_mode):
    token = "malformed-token"
    key = _KEY.format(token)

    fake_redis.set(key, "{not json at all")
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0

    fake_redis.set(key, json.dumps({"username": "missing-user-id"}))   # 结构不符同样丢弃
    assert A.resolve_session(token) is None
    assert fake_redis.exists(key) == 0


class _FailingClient:
    """任何命令都抛 redis.RedisError 的假客户端（模拟 Redis 抖动）。"""

    def set(self, *args, **kwargs):
        raise redis.RedisError("boom")

    def get(self, *args, **kwargs):
        raise redis.RedisError("boom")

    def delete(self, *args, **kwargs):
        raise redis.RedisError("boom")


def test_command_failure_create_fails_closed_resolve_fails_open(monkeypatch, redis_mode):
    monkeypatch.setattr(redis_store, "_build_client", lambda: _FailingClient())

    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u6", username="erin"))
    assert excinfo.value.code == "session_store_unavailable"

    assert A.resolve_session("any-token") is None   # fail-open：登录态丢了就重新登录
    A.destroy_session("any-token")                  # 静默吞掉，不抛


def test_missing_redis_package_semantics(monkeypatch, redis_mode):
    """缺包（最小安装）时：create 如实报 redis_package_missing，resolve/destroy fail-open。"""
    def _missing():
        raise redis_store.RedisUnavailable("redis_package_missing", "未安装 redis 依赖包。")

    monkeypatch.setattr(redis_store, "_build_client", _missing)

    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u7", username="frank"))
    assert excinfo.value.code == "redis_package_missing"

    assert A.resolve_session("any-token") is None
    A.destroy_session("any-token")


def test_typo_session_store_env_fails_closed(monkeypatch):
    monkeypatch.setenv("BIODATA_SESSION_STORE", "rediss")   # 拼错：不许静默降级到内存后端
    with pytest.raises(A.AccountError) as excinfo:
        A.create_session(A.PublicUser(id="u8", username="gina"))
    assert excinfo.value.code == "bad_config"


def test_client_factory_injection_seam():
    """构造参数 `client_factory` 直接注入客户端（不经共享单例）——MQ 后端同款接缝。"""
    from dataset_recommender.app import redis_sessions

    client = fakeredis.FakeStrictRedis(decode_responses=True)
    store = redis_sessions.RedisSessionStore(
        ttl_seconds=60, token_bytes=8, client_factory=lambda: client,
    )
    user = A.PublicUser(id="u-factory", username="frank")
    token = store.create(user)
    assert store.resolve(token)["user_id"] == "u-factory"
    store.destroy(token)
    assert store.resolve(token) is None


def test_reset_state_clears_client_singleton(monkeypatch, redis_mode):
    builds = []

    def _factory():
        builds.append(1)
        return fakeredis.FakeStrictRedis(decode_responses=True)

    monkeypatch.setattr(redis_store, "_build_client", _factory)

    user = A.PublicUser(id="u9", username="henry")
    A.create_session(user)
    A.create_session(user)
    assert len(builds) == 1   # 单例复用：两次写入只建一次连接

    A._reset_state_for_tests()
    A.create_session(user)
    assert len(builds) == 2   # 复位后重新建连（测试隔离生效）
