from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app import accounts as A  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    A._reset_state_for_tests()
    path = tmp_path / "accounts.json"
    yield path
    A._reset_state_for_tests()


def test_register_and_authenticate(store):
    u = A.register("Alice_1", "correct horse battery", store_path=store)
    assert u.username == "alice_1" and u.id
    got = A.authenticate("alice_1", "correct horse battery", store_path=store)
    assert got.id == u.id


def test_password_not_stored_plaintext(store):
    A.register("bob", "s3cretpassword", store_path=store)
    text = store.read_text(encoding="utf-8")
    assert "s3cretpassword" not in text
    assert "pwd_hash" in text and "salt" in text


def test_salted_hashes_differ_for_same_password(store):
    A.register("user1", "samepassword", store_path=store)
    A.register("user2", "samepassword", store_path=store)
    users = json.loads(store.read_text(encoding="utf-8"))["users"]
    assert users["user1"]["pwd_hash"] != users["user2"]["pwd_hash"]
    assert users["user1"]["salt"] != users["user2"]["salt"]


def test_wrong_password_and_missing_user_same_error(store):
    # Anti-enumeration: wrong password and unknown user return the SAME error code.
    A.register("carol", "rightpassword", store_path=store)
    with pytest.raises(A.AccountError) as e1:
        A.authenticate("carol", "wrongpassword", store_path=store)
    A._reset_state_for_tests()  # avoid lockout across the two probes
    with pytest.raises(A.AccountError) as e2:
        A.authenticate("nobody", "whatever12", store_path=store)
    assert e1.value.code == e2.value.code == "invalid_credentials"


def test_register_validation(store):
    with pytest.raises(A.AccountError) as e:
        A.register("ab", "longenoughpw", store_path=store)  # username too short
    assert e.value.code == "bad_username"
    with pytest.raises(A.AccountError) as e:
        A.register("gooduser", "short", store_path=store)  # password too short
    assert e.value.code == "weak_password"
    A.register("dupe", "password12", store_path=store)
    with pytest.raises(A.AccountError) as e:
        A.register("dupe", "password12", store_path=store)
    assert e.value.code == "username_taken"


def test_username_normalized_case_insensitive(store):
    A.register("MixedCase", "password12", store_path=store)
    got = A.authenticate("mixedcase", "password12", store_path=store)
    assert got.username == "mixedcase"


def test_sessions_lifecycle(store):
    u = A.register("dave", "password12", store_path=store)
    token = A.create_session(u)
    assert A.resolve_session(token).id == u.id
    assert A.resolve_session("garbage-token") is None
    assert A.resolve_session(None) is None
    A.destroy_session(token)
    assert A.resolve_session(token) is None


def test_session_expiry_is_pruned(store):
    u = A.register("erin", "password12", store_path=store)
    token = A.create_session(u)
    A._SESSIONS[token]["expires_at"] = 0.0  # force expiry
    assert A.resolve_session(token) is None
    assert token not in A._SESSIONS


def test_brute_force_lockout(store):
    A.register("frank", "rightpassword", store_path=store)
    for _ in range(A._LOCK_THRESHOLD):
        with pytest.raises(A.AccountError) as e:
            A.authenticate("frank", "wrongpassword", store_path=store)
        assert e.value.code == "invalid_credentials"
    # Correct password, but now locked out.
    with pytest.raises(A.AccountError) as e:
        A.authenticate("frank", "rightpassword", store_path=store)
    assert e.value.code == "locked"


def test_persistence_survives_session_reset(store):
    u = A.register("gina", "password12", store_path=store)
    A._reset_state_for_tests()  # clears in-memory sessions/fails only, not the store file
    got = A.authenticate("gina", "password12", store_path=store)
    assert got.id == u.id


def test_default_store_path_is_userdata(tmp_path):
    p = A.default_store_path(tmp_path)
    assert p.parts[-2:] == (".userdata", "accounts.json")


def test_tokens_are_unpredictable_and_unique(store):
    u = A.register("henry", "password12", store_path=store)
    tokens = {A.create_session(u) for _ in range(20)}
    assert len(tokens) == 20  # no collisions
    assert all(len(t) >= 32 for t in tokens)


def test_corrupt_store_fails_closed_and_does_not_wipe(store):
    # Review #1: a present-but-unparseable store must NOT reset to empty (which would let the
    # next register wipe all accounts + allow username takeover). It must fail closed.
    A.register("victim", "password12", store_path=store)
    store.write_bytes(b"{ this is not valid json ")
    with pytest.raises(A.AccountError) as e:
        A.authenticate("victim", "password12", store_path=store)
    assert e.value.code == "store_corrupt"
    with pytest.raises(A.AccountError) as e2:
        A.register("attacker", "password12", store_path=store)
    assert e2.value.code == "store_corrupt"


def test_absent_and_empty_store_start_empty(store):
    assert not store.exists()
    assert A.register("first", "password12", store_path=store).username == "first"  # absent -> ok
    store.write_text("", encoding="utf-8")
    assert A.register("second", "password12", store_path=store).username == "second"  # empty -> ok


def test_sweep_prunes_expired_sessions_and_stale_fails(store):
    # Review #2: bounded in-memory growth.
    u = A.register("ivan", "password12", store_path=store)
    tok = A.create_session(u)
    A._SESSIONS[tok]["expires_at"] = 0.0
    A._FAILS["staleuser"] = [0.0]
    A._sweep(A._now())
    assert tok not in A._SESSIONS
    assert "staleuser" not in A._FAILS


# ---------------------------------------------------------------- 会话落盘（.userdata/sessions.json）


@pytest.fixture()
def sessions(tmp_path):
    A._reset_state_for_tests()
    path = tmp_path / "sessions.json"
    yield path
    A._reset_state_for_tests()


def test_sessions_persist_across_memory_reset(store, sessions):
    """服务重启（内存清场）后会话仍解析——「每次开前端都要重新登录」的根因修复。"""
    u = A.register("persist", "password12", store_path=store)
    token = A.create_session(u, sessions_path=sessions)
    A._reset_state_for_tests()
    got = A.resolve_session(token, sessions_path=sessions)
    assert got is not None and got.id == u.id


def test_destroy_session_removes_from_disk(store, sessions):
    u = A.register("gone", "password12", store_path=store)
    token = A.create_session(u, sessions_path=sessions)
    A.destroy_session(token, sessions_path=sessions)
    A._reset_state_for_tests()   # 连内存也清掉，只靠盘——盘上也不该有
    assert A.resolve_session(token, sessions_path=sessions) is None


def test_corrupt_sessions_file_fails_open(store, sessions):
    """会话可再生：文件损坏 = 全体登出（空库），绝不抛错阻断应用——与账户库 fail-closed 相反。"""
    u = A.register("victim2", "password12", store_path=store)
    token = A.create_session(u, sessions_path=sessions)
    sessions.write_bytes(b"{ not json ")
    A._reset_state_for_tests()
    assert A.resolve_session(token, sessions_path=sessions) is None
    # 损坏后能继续写新会话（自愈覆盖坏文件）
    token2 = A.create_session(u, sessions_path=sessions)
    A._reset_state_for_tests()
    assert A.resolve_session(token2, sessions_path=sessions) is not None


def test_default_sessions_path_is_userdata(tmp_path):
    p = A.default_sessions_path(tmp_path)
    assert p.parts[-2:] == (".userdata", "sessions.json")


def test_expired_session_pruned_from_disk(store, sessions):
    u = A.register("expire", "password12", store_path=store)
    token = A.create_session(u, sessions_path=sessions)
    A._SESSIONS[token]["expires_at"] = 0.0   # force expiry（内存真源）
    assert A.resolve_session(token, sessions_path=sessions) is None
    A._reset_state_for_tests()
    assert A.resolve_session(token, sessions_path=sessions) is None
    assert token not in json.loads(sessions.read_text(encoding="utf-8"))


def test_runtime_path_env_override_rejects_repo_database(monkeypatch, tmp_path):
    """ 验证：BIODATA_ACCOUNTS_FILE/BIODATA_SESSIONS_FILE 误配进
    仓库 database/（冻结基准/元数据库）必须 fail-closed；仓库外路径照常放行。"""
    inside = A._repo_database_dir() / "base" / "accounts.json"
    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(inside))
    with pytest.raises(A.AccountError) as e:
        A.default_store_path(ROOT)
    assert e.value.code == "bad_store_path"

    monkeypatch.setenv("BIODATA_SESSIONS_FILE", str(A._repo_database_dir() / "sessions.json"))
    with pytest.raises(A.AccountError) as e:
        A.default_sessions_path(ROOT)
    assert e.value.code == "bad_store_path"

    monkeypatch.setenv("BIODATA_ACCOUNTS_FILE", str(tmp_path / "ok.json"))
    assert A.default_store_path(ROOT) == (tmp_path / "ok.json").resolve()
