# -*- coding: utf-8 -*-
"""本地单机用户账户 —— 注册 / 登录 / 登出 + 会话（scrypt 哈希密码，会话落盘持久化）。

定位：BioData Agent 是 **loopback-only 单机工具**（webapp 中间件只接受本机 Host）。账户用于让
**共用一台机器**的多人各自拥有私有的用户记忆 / 收藏 / 历史命名空间——**不是**面向公网的认证系统。

安全要点：
- 密码用 `hashlib.scrypt` + 每用户随机盐哈希；**绝不**明文存储 / 打印 / 记日志。校验用 `hmac.compare_digest`。
- 会话 = 不透明随机 token（`secrets.token_urlsafe`）。2026-08-02 起**落盘持久化**
  （`.userdata/sessions.json`，原子写；此前只存进程内存、服务重启全体掉登录——「每次开前端都要
  重新登录」的根因）。会话是**可再生**的（丢了 = 重新登录，不丢账户数据），故会话库 fail-open
  （缺失/损坏 → 空库全体登出），与账户库的 fail-closed 相反。cookie 由接口层置 `HttpOnly` +
  `SameSite=Strict`（loopback http，无需 Secure）。
- 会话可选 **Redis 后端**（`BIODATA_SESSION_STORE=redis` + `BIODATA_REDIS_URL`，见
  `app/redis_sessions.py`）：供多进程 / 多实例形态共享登录态；**默认不设这两个变量时，内存 +
  JSON 快照路径逐字节不变**（Redis 侧模块也只在实际启用时才被 import）。Redis 后端的 fail 语义
  与文件后端一致——会话可再生，故 resolve/destroy 失败 fail-open（登出/丢掉，绝不因存储抖动 500），
  create 失败如实抛 `AccountError`；`BIODATA_SESSION_STORE` 写成其他非空值则 fail-closed 报
  `bad_config`（配置笔误不许静默降级成内存后端，那会让会话悄悄不跨进程）。
- 用户库 = 被 gitignore 的本地 JSON（默认 `.userdata/accounts.json`，`BIODATA_ACCOUNTS_FILE` 可覆盖）；
  **不进版本库、不进交付包**（不在 release allowlist 的 ROOT_DIRS 内）。会话库同理
  （`.userdata/sessions.json`，`BIODATA_SESSIONS_FILE` 可覆盖）。
- 登录失败信息**不区分**「用户不存在 / 密码错」（防用户名枚举）；对不存在用户仍走一次 scrypt（防时序旁路）。
- 简单失败计数节流：同用户名短时多次失败 → 短暂锁定，缓解在线暴力破解。

刻意不做（本地工具从简，晨审可扩）：密码找回、邮箱验证、细粒度权限、
服务端存用户记忆（记忆仍留在浏览器 localStorage、按账户 namespace —— 保「只存本地/不上传」冻结不变量）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SESSION_COOKIE = "biodata_session"

_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024   # n=2^14,r=8 需 ~16MB；显式给足，否则 scrypt 抛 memory-limit
_SALT_BYTES = 16
_SESSION_BYTES = 32
_SESSION_TTL = 30 * 24 * 3600       # 30 天（2026-08-08 由 7 天放宽：本地单机工具，过期需重登）
#: 会话 TTL 的人读口径（cookie max_age 与 UI 文案的单一真源，webapp/前端文案对齐它）。
SESSION_TTL_DAYS = 30
_MAX_USERS = 1000
_USERNAME_RE = re.compile(r"[a-z0-9_-]{3,32}")
_PASSWORD_MIN = 8
_PASSWORD_MAX = 200
_LOCK_THRESHOLD = 5                 # 窗口内失败达此次数 → 锁定
_LOCK_WINDOW = 300                  # 失败计数滑动窗口（秒）
_DUMMY_SALT = b"\x00" * _SALT_BYTES

_LOCK = threading.RLock()
_SESSIONS: dict[str, dict[str, Any]] = {}   # token -> {user_id, username, expires_at}
_FAILS: dict[str, list[float]] = {}         # username -> 近期失败时间戳
_REDIS_SESSION_STORE: "Any | None" = None   # Redis 会话后端惰性单例（None = 未启用或尚未建）


class AccountError(Exception):
    """机器码 + 人读提示；接口层翻成 4xx，不泄漏内部细节。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class PublicUser:
    id: str
    username: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "username": self.username}


from .runtime_paths import (
    assert_runtime_path,
    atomic_write_json,
    instance_data_dir_for,
    repo_database_dir,
)


def _repo_database_dir() -> Path:
    """仓库 `database/` 目录（真源在 runtime_paths.repo_database_dir；保留本别名供既有测试引用）。"""
    return repo_database_dir()


def _assert_runtime_path(path: Path) -> Path:
    """运行时状态文件（账户/会话库）绝不许落进仓库 `database/`——那里是冻结基准与
    元数据库，环境变量误配也不许把运行时写引进来（实现统一在 runtime_paths.assert_runtime_path）。"""
    return assert_runtime_path(path, AccountError)


def default_store_path(project_root: Path) -> Path:
    """账户库默认路径 = 实例 userdata 层（source/portable = project_root/.userdata，
    frozen 布局 = data_root/.userdata）；`BIODATA_ACCOUNTS_FILE` 显式覆盖优先（W1 起
    经 runtime_paths 单一真源解析，历史项目根语义逐字节不变）。"""
    override = os.environ.get("BIODATA_ACCOUNTS_FILE", "").strip()
    if override:
        return _assert_runtime_path(Path(override))
    return instance_data_dir_for(Path(project_root), ".userdata") / "accounts.json"


def default_sessions_path(project_root: Path) -> Path:
    """会话库默认路径，语义同 `default_store_path`（`.userdata/sessions.json`）。"""
    override = os.environ.get("BIODATA_SESSIONS_FILE", "").strip()
    if override:
        return _assert_runtime_path(Path(override))
    return instance_data_dir_for(Path(project_root), ".userdata") / "sessions.json"


def _now() -> float:
    return time.time()


def _load_store(path: Path) -> dict[str, Any]:
    """读用户库。**fail-closed**：文件缺失/空 → 空库（首次运行）；但文件**存在且非空却解析失败**
    → 抛 `store_corrupt`（绝不返回空库，否则下一次 register 会覆盖、永久丢账户 + 允许用户名被接管）。"""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return {"schema_version": 1, "users": {}}
    except OSError as exc:
        raise AccountError("store_unavailable", "本地账户库暂时不可读，请稍后重试。") from exc
    if not raw.strip():
        return {"schema_version": 1, "users": {}}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AccountError("store_corrupt", "本地账户库文件损坏，已停止以防覆盖；请从备份恢复或删除后重建。") from exc
    if not (isinstance(data, dict) and isinstance(data.get("users"), dict)):
        raise AccountError("store_corrupt", "本地账户库结构异常，已停止以防覆盖。")
    return data


def _sweep(now: float) -> None:
    """调用方须持 `_LOCK`。清过期会话 + 空/过期失败桶，防进程内无界增长（对抗评审 #2）。"""
    for token in [t for t, s in _SESSIONS.items() if s["expires_at"] < now]:
        _SESSIONS.pop(token, None)
    for uname in [u for u, ts in _FAILS.items() if not any(now - t < _LOCK_WINDOW for t in ts)]:
        _FAILS.pop(uname, None)


def _save_store(path: Path, store: dict[str, Any]) -> None:
    atomic_write_json(path, store)


def _load_sessions(path: Path) -> dict[str, dict[str, Any]]:
    """读会话库。会话**可再生**（丢了 = 重新登录，不丢账户数据），故与账户库的 fail-closed 相反：
    缺失 / 损坏 / 结构不符 → 空库（全体登出），绝不阻断应用、也绝不把畸形条目交给 resolve。"""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for token, sess in data.items():
        if (isinstance(sess, dict)
                and isinstance(sess.get("user_id"), str)
                and isinstance(sess.get("username"), str)
                and isinstance(sess.get("expires_at"), (int, float))):
            out[str(token)] = {
                "user_id": sess["user_id"],
                "username": sess["username"],
                "expires_at": float(sess["expires_at"]),
            }
    return out


def _save_sessions(path: Path) -> None:
    """调用方须持 `_LOCK`。原子写走 runtime_paths.atomic_write_json（紧凑格式，与历史字节一致）。"""
    atomic_write_json(path, _SESSIONS, indent=None)


def _hydrate_sessions(sessions_path: Path | None) -> None:
    """调用方须持 `_LOCK`。内存为空时把盘上活会话读回——进程重启（或测试清场）后首次触碰会话
    即恢复登录态；服务重启不再全体掉登录（用户「每次开前端都要重新登录」的根因）。
    `_SESSIONS` 非空不重读：内存是运行期真源，盘上只是它的快照。"""
    if sessions_path is None or _SESSIONS:
        return
    _SESSIONS.update(_load_sessions(sessions_path))


def _hash_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


def _normalize_username(username: str) -> str:
    return str(username or "").strip().lower()


def _validate_new_credentials(username: str, password: str) -> None:
    if not _USERNAME_RE.fullmatch(username):
        raise AccountError("bad_username", "用户名需为 3-32 位小写字母、数字、下划线或连字符。")
    if not isinstance(password, str) or not (_PASSWORD_MIN <= len(password) <= _PASSWORD_MAX):
        raise AccountError("weak_password", f"密码长度需在 {_PASSWORD_MIN}-{_PASSWORD_MAX} 位之间。")


def _check_not_locked(username: str) -> None:
    now = _now()
    recent = [t for t in _FAILS.get(username, []) if now - t < _LOCK_WINDOW]
    if recent:
        _FAILS[username] = recent
    else:
        _FAILS.pop(username, None)
    if len(recent) >= _LOCK_THRESHOLD:
        raise AccountError("locked", "登录尝试过于频繁，请稍后再试。")


def _record_fail(username: str) -> None:
    _FAILS.setdefault(username, []).append(_now())


def register(username: str, password: str, *, store_path: Path) -> PublicUser:
    uname = _normalize_username(username)
    _validate_new_credentials(uname, password)
    with _LOCK:
        store = _load_store(store_path)
        users = store["users"]
        if uname in users:
            raise AccountError("username_taken", "用户名已存在，请换一个或直接登录。")
        if len(users) >= _MAX_USERS:
            raise AccountError("store_full", "本地账户数已达上限。")
        salt = secrets.token_bytes(_SALT_BYTES)
        record = {
            "id": secrets.token_hex(8),
            "username": uname,
            "salt": salt.hex(),
            "pwd_hash": _hash_password(password, salt).hex(),
            "created_at": _now(),
        }
        users[uname] = record
        _save_store(store_path, store)
        return PublicUser(record["id"], record["username"])


def authenticate(username: str, password: str, *, store_path: Path) -> PublicUser:
    uname = _normalize_username(username)
    with _LOCK:
        _sweep(_now())
        _check_not_locked(uname)
        store = _load_store(store_path)
        record = store["users"].get(uname)
        if record is None:
            # 用户不存在也走一次 scrypt（防时序旁路），统一报 invalid_credentials（防枚举）。
            _hash_password(password if isinstance(password, str) else "", _DUMMY_SALT)
            _record_fail(uname)
            raise AccountError("invalid_credentials", "用户名或密码错误。")
        salt = bytes.fromhex(record["salt"])
        candidate = _hash_password(password if isinstance(password, str) else "", salt)
        if not hmac.compare_digest(candidate.hex(), str(record["pwd_hash"])):
            _record_fail(uname)
            raise AccountError("invalid_credentials", "用户名或密码错误。")
        _FAILS.pop(uname, None)   # 成功即清空失败计数
        return PublicUser(record["id"], record["username"])


def _redis_store_if_enabled() -> "Any | None":
    """按 env 决定会话后端：`BIODATA_SESSION_STORE=redis` → 惰性建 `RedisSessionStore` 单例。

    - 空 / 未设 → None（默认内存 + JSON 快照路径，行为逐字节不变）；
    - `redis` → 建/复用单例（参数取本模块既有常量，保证两后端 TTL/熵同源）；
    - 其他非空值 → `AccountError("bad_config")`（配置笔误 fail-closed，绝不静默降级到内存后端——
      那会让会话悄悄不跨进程，是比直接报错更难查的故障）。

    Redis 侧模块在函数体内 import：默认形态不加载 redis 依赖链（最小安装无 redis 包）。
    """
    mode = os.environ.get("BIODATA_SESSION_STORE", "").strip().lower()
    if not mode:
        return None
    if mode != "redis":
        raise AccountError(
            "bad_config",
            f"BIODATA_SESSION_STORE 只支持 redis（当前收到 {mode!r}）；请改正或留空以使用默认内存后端。",
        )
    global _REDIS_SESSION_STORE
    if _REDIS_SESSION_STORE is None:
        from .redis_sessions import RedisSessionStore
        from .redis_store import RedisUnavailable
        try:
            _REDIS_SESSION_STORE = RedisSessionStore(
                ttl_seconds=_SESSION_TTL,
                token_bytes=_SESSION_BYTES,
            )
        except RedisUnavailable as exc:   # 建连期异常（如缺包）翻成本模块错误契约，code 原样透传
            raise AccountError(exc.code, exc.message) from exc
    return _REDIS_SESSION_STORE


def create_session(user: PublicUser, *, sessions_path: Path | None = None) -> str:
    store = _redis_store_if_enabled()
    if store is not None:
        from .redis_store import RedisUnavailable   # 惰性 import：默认形态零新 import 边
        try:
            return store.create(user)
        except RedisUnavailable as exc:
            raise AccountError(exc.code, exc.message) from exc
    token = secrets.token_urlsafe(_SESSION_BYTES)
    now = _now()
    with _LOCK:
        _hydrate_sessions(sessions_path)
        _sweep(now)
        _SESSIONS[token] = {"user_id": user.id, "username": user.username, "expires_at": now + _SESSION_TTL}
        if sessions_path is not None:
            _save_sessions(sessions_path)
    return token


def resolve_session(token: str | None, *, sessions_path: Path | None = None) -> PublicUser | None:
    if not token:
        return None
    store = _redis_store_if_enabled()
    if store is not None:
        sess = store.resolve(token)   # Redis 侧 fail-open：不可用/畸形/过期 → None
        if sess is None:
            return None
        return PublicUser(sess["user_id"], sess["username"])
    with _LOCK:
        _hydrate_sessions(sessions_path)
        sess = _SESSIONS.get(token)
        if not sess:
            return None
        if sess["expires_at"] < _now():
            _SESSIONS.pop(token, None)
            if sessions_path is not None:
                _save_sessions(sessions_path)
            return None
        return PublicUser(sess["user_id"], sess["username"])


def destroy_session(token: str | None, *, sessions_path: Path | None = None) -> None:
    if not token:
        return
    store = _redis_store_if_enabled()
    if store is not None:
        store.destroy(token)   # Redis 侧 fail-open：删除失败静默吞掉
        return
    with _LOCK:
        _hydrate_sessions(sessions_path)
        if _SESSIONS.pop(token, None) is not None and sessions_path is not None:
            _save_sessions(sessions_path)


def _reset_state_for_tests() -> None:
    """仅供测试：清空进程内会话与失败计数（不碰用户库文件），并复位 Redis 后端单例。"""
    global _REDIS_SESSION_STORE
    with _LOCK:
        _SESSIONS.clear()
        _FAILS.clear()
        _REDIS_SESSION_STORE = None
    # Redis 侧单例与共享连接只在「本进程确实 import 过 redis_store」时复位；默认形态
    # （env 未设）不引入任何 import 边，也不加载 redis 依赖链。
    import sys
    if "dataset_recommender.app.redis_store" in sys.modules:
        from . import redis_store
        redis_store._reset_for_tests()
