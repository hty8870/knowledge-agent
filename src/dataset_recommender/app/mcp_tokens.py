# -*- coding: utf-8 -*-
"""在线 MCP 接入令牌库（网页版「接入 AI 助手」在线形态的凭证）。

与 `accounts.py` 的会话库同构、但语义不同：

- 会话是浏览器登录态（30 天 TTL、可再生）；本模块是**长期 API 令牌**（无 TTL，
  用户手动吊销才失效），发给用户自己的 MCP 客户端（Kimi Code / Claude 等）做
  `Authorization: Bearer` 鉴权。
- 落盘只存 **sha256 摘要**（会话库存的是原始 token——那里的 token 本身就是会话
  句柄；这里是长期凭证，按 API key 惯例摘要化，泄库不泄可用凭证）。
- 令牌**可再生**（丢了 = 用户到设置里重新生成，不丢任何账户数据），故加载策略与
  会话库一致：缺失/损坏 → 空库（绝不阻断应用），与账户库的 fail-closed 相反。

单一真源：webapp 的令牌管理 API 与 mcp_server 的 TokenVerifier 都走本模块，
两端各自缓存进程内副本、写时落盘（同一台进程内共享；多进程部署以盘为准、
进程内缓存 `_hydrate` 一次性载入——与本项目会话库同款先例）。

路径：默认实例 userdata 层 `.userdata/mcp_tokens.json`（runtime_paths 单一真源），
`BIODATA_MCP_TOKENS_FILE` 显式覆盖；绝不许落进仓库 `database/`（同 accounts 裁决）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any

from .runtime_paths import (
    assert_runtime_path,
    atomic_write_json,
    instance_data_dir_for,
    repo_database_dir,
)

#: 令牌可见前缀（明文形态的标识段；列表回显只给到 prefix，绝不回显完整令牌）
_TOKEN_PREFIX = "bdm_"
_TOKEN_BYTES = 32              # token_urlsafe 的字节数 → 43 字符随机段
_MAX_TOKENS_PER_ACCOUNT = 5    # 每账户令牌数上限（防无界增长；吊销后可再开）
_MAX_LABEL_CHARS = 40

_LOCK = threading.RLock()
#: digest -> {token_id, account_id, username, label, prefix, created_at}
_TOKENS: dict[str, dict[str, Any]] = {}


class McpTokenError(Exception):
    """机器码 + 用户可读消息（与 AccountError 同款约定，webapp 映射成 HTTP 状态）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _repo_database_dir() -> Path:
    """仓库 `database/` 目录（真源在 runtime_paths.repo_database_dir）。"""
    return repo_database_dir()


def _assert_runtime_path(path: Path) -> Path:
    """运行时状态文件绝不许落进仓库 `database/`（同 accounts.py 的 2026-08-10 裁决；
    实现统一在 runtime_paths.assert_runtime_path）。"""
    return assert_runtime_path(path, McpTokenError)


def default_tokens_path(project_root: Path) -> Path:
    """令牌库默认路径 = 实例 userdata 层（`.userdata/mcp_tokens.json`）；env 覆盖优先。"""
    override = os.environ.get("BIODATA_MCP_TOKENS_FILE", "").strip()
    if override:
        return _assert_runtime_path(Path(override))
    return instance_data_dir_for(Path(project_root), ".userdata") / "mcp_tokens.json"


def _digest(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _load_tokens(path: Path) -> dict[str, dict[str, Any]]:
    """读令牌库。令牌**可再生**（丢 = 用户重新生成，不丢数据），故缺失/损坏/结构不符 →
    空库（全体令牌失效、需重新生成），与 sessions 同款 fail-open，绝不阻断应用。"""
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
    if not (isinstance(data, dict) and isinstance(data.get("tokens"), dict)):
        return {}
    # 逐条校验结构（畸形条目绝不交给 resolve，防放残）
    out: dict[str, dict[str, Any]] = {}
    for digest, rec in data["tokens"].items():
        if not isinstance(digest, str) or len(digest) != 64:
            continue
        if not isinstance(rec, dict):
            continue
        if not all(isinstance(rec.get(k), str) and rec.get(k)
                   for k in ("token_id", "account_id", "username", "created_at")):
            continue
        out[digest] = {
            "token_id": rec["token_id"],
            "account_id": rec["account_id"],
            "username": rec["username"],
            "label": str(rec.get("label") or "")[:_MAX_LABEL_CHARS],
            "prefix": str(rec.get("prefix") or "")[:16],
            "created_at": rec["created_at"],
        }
    return out


def _save_tokens(path: Path) -> None:
    """调用方须持 `_LOCK`。原子写走 runtime_paths.atomic_write_json（indent=2，与历史字节一致）。"""
    atomic_write_json(path, {"schema_version": 1, "tokens": _TOKENS})


def _hydrate(store_path: Path | None) -> None:
    """调用方须持 `_LOCK`。进程内缓存非空不重读：内存是运行期真源，盘上只是快照。"""
    if store_path is None or _TOKENS:
        return
    _TOKENS.update(_load_tokens(store_path))


def _public(rec: dict[str, Any]) -> dict[str, Any]:
    """对外回显形状：绝不带 digest（更不可能有明文令牌）。"""
    return {
        "token_id": rec["token_id"],
        "label": rec["label"],
        "prefix": rec["prefix"],
        "created_at": rec["created_at"],
    }


def mint_token(account_id: str, username: str, label: str = "", *,
               store_path: Path | None = None) -> tuple[str, dict[str, Any]]:
    """生成新令牌 → 返回 (明文令牌, 公开记录)。**明文仅此一次返回**，落盘只有摘要。"""
    label = (label or "").strip()[:_MAX_LABEL_CHARS]
    if not account_id:
        raise McpTokenError("bad_account", "账户信息缺失，无法生成令牌。")
    with _LOCK:
        _hydrate(store_path)
        owned = [d for d, r in _TOKENS.items() if r["account_id"] == account_id]
        if len(owned) >= _MAX_TOKENS_PER_ACCOUNT:
            raise McpTokenError(
                "too_many_tokens",
                f"每个账户最多 {_MAX_TOKENS_PER_ACCOUNT} 个在线接入令牌；请先吊销不再使用的令牌。")
        raw = _TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)
        rec = {
            "token_id": secrets.token_hex(6),
            "account_id": account_id,
            "username": username,
            "label": label,
            "prefix": raw[:10] + "…",
            "created_at": _now_iso(),
        }
        _TOKENS[_digest(raw)] = rec
        if store_path is not None:
            _save_tokens(store_path)
    return raw, _public(rec)


def list_tokens(account_id: str, *, store_path: Path | None = None) -> list[dict[str, Any]]:
    """该账户的令牌公开记录（按创建时间升序）。绝不包含明文/摘要。"""
    with _LOCK:
        _hydrate(store_path)
        return [_public(r) for r in _TOKENS.values() if r["account_id"] == account_id]


def revoke_token(account_id: str, token_id: str, *, store_path: Path | None = None) -> bool:
    """吊销令牌（仅属主可吊销——按 account_id 归属过滤）。返回是否真删到。"""
    with _LOCK:
        _hydrate(store_path)
        for digest, rec in list(_TOKENS.items()):
            if rec["token_id"] == token_id and rec["account_id"] == account_id:
                _TOKENS.pop(digest, None)
                if store_path is not None:
                    _save_tokens(store_path)
                return True
    return False


def resolve_token(raw_token: str | None, *, store_path: Path | None = None) -> dict[str, Any] | None:
    """Bearer 明文 → 令牌记录（含 account_id / username / token_id）；未知 → None。
    dict 按摘要直接命中 = 天然常数时间比对（不逐条 hmac 也比逐条扫强）。"""
    if not raw_token or not raw_token.startswith(_TOKEN_PREFIX):
        return None
    with _LOCK:
        _hydrate(store_path)
        rec = _TOKENS.get(_digest(raw_token))
        if rec is None:
            return None
        return dict(rec)


def _reset_state_for_tests() -> None:
    """仅供测试：清空进程内令牌缓存（不碰盘上文件）。"""
    with _LOCK:
        _TOKENS.clear()
