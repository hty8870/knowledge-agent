# -*- coding: utf-8 -*-
"""Redis 会话后端 —— `accounts` 会话的可选共享存储（默认内存 + JSON 快照形态一行不动）。

为什么存在：默认形态是**单机单进程**（会话存进程内存 + `.userdata/sessions.json` 快照），
多进程 / 多实例部署时会话互不可见。本后端把会话放进 Redis，供 opt-in 形态复用；语义与
`accounts` 现有文件后端**逐条对齐**，避免「换个后端行为悄悄变了」。

存储形状：
- key `biodata:sess:<token>`；value 紧凑 JSON `{"user_id","username","expires_at"}`
  （`separators=(",",":")`，与文件快照的紧凑风格一致）。
- TTL 用 `SET ... EX` 交给 Redis 自己过期（进程重启不丢、也不需要 GC 任务）；
  `expires_at` 字段仍保留并双查——TTL 是 Redis 侧纪律，字段是跨后端统一口径。

fail 语义（与文件后端一致：会话是**可再生**的，丢了 = 重新登录，不丢账户数据）：
- `create` 失败 → 抛 `RedisUnavailable("session_store_unavailable")`（登录必须成功或如实报错，
  不能发一个永远解析不出来的 token）；缺包等更精确的 code 原样透传。
- `resolve` / `destroy` 失败 → fail-open（返回 None / 静默吞掉）——Redis 抖一下就把用户踢成
  500 是更坏的故障。
- 畸形 / 过期条目绝不交给调用方：删掉该 key 再返回 None（对齐文件后端 `_load_sessions`
  丢弃畸形条目的口径）。

本模块**不 import accounts**（防循环依赖）；`RedisUnavailable` 由 `accounts` 翻成 `AccountError`。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Callable

from . import redis_store

#: key 前缀：与项目其他 Redis 键（后续 job/队列）共用 `biodata:` 命名空间，便于统一运维排查。
_KEY_PREFIX = "biodata:sess:"


class RedisSessionStore:
    """Redis 会话后端。构造只存参数，不建连（建连推迟到首次真实命令，见 `redis_store`）。"""

    def __init__(
        self,
        *,
        ttl_seconds: int,
        token_bytes: int,
        client_factory: "Callable[[], Any] | None" = None,
    ) -> None:
        # ttl_seconds / token_bytes 由 accounts 传入其既有常量，保证两后端参数单一真源。
        self._ttl_seconds = int(ttl_seconds)
        self._token_bytes = int(token_bytes)
        # None = 用共享单例；测试注入 fakeredis 时传自己的工厂。
        self._client_factory = client_factory

    def _client(self) -> "Any":
        if self._client_factory is not None:
            return self._client_factory()
        return redis_store.get_client()

    @staticmethod
    def _key(token: str) -> str:
        return f"{_KEY_PREFIX}{token}"

    def create(self, user: Any) -> str:
        """写入一条会话并返回 token；失败抛 `RedisUnavailable`（登录侧翻成 AccountError）。"""
        token = secrets.token_urlsafe(self._token_bytes)
        payload = {
            "user_id": user.id,
            "username": user.username,
            "expires_at": time.time() + self._ttl_seconds,
        }
        try:
            self._client().set(
                self._key(token),
                json.dumps(payload, separators=(",", ":")),
                ex=self._ttl_seconds,
            )
        except redis_store.redis_error_types() as exc:
            raise redis_store.RedisUnavailable(
                "session_store_unavailable",
                "会话存储暂时不可用，请稍后重试。",
            ) from exc
        return token

    def resolve(self, token: "str | None") -> "dict[str, Any] | None":
        """解析 token → `{"user_id","username","expires_at"}` 或 None。

        Redis 命令失败 → None（fail-open）；条目畸形 / 已过期 → 删 key 后 None。
        """
        if not token:
            return None
        key = self._key(token)
        try:
            raw = self._client().get(key)
        except (redis_store.RedisUnavailable, *redis_store.redis_error_types()):
            return None
        if raw is None:
            return None
        sess = self._decode(raw)
        if sess is None:
            self._delete_quiet(key)   # 畸形条目绝不外泄，顺手清掉防反复命中
            return None
        if sess["expires_at"] < time.time():
            self._delete_quiet(key)
            return None
        return sess

    def destroy(self, token: "str | None") -> None:
        """删除会话；Redis 失败静默吞掉（fail-open：登出不该因存储抖动变成 500）。"""
        if not token:
            return
        self._delete_quiet(self._key(token))

    def _delete_quiet(self, key: str) -> None:
        try:
            self._client().delete(key)
        except (redis_store.RedisUnavailable, *redis_store.redis_error_types()):
            pass

    @staticmethod
    def _decode(raw: Any) -> "dict[str, Any] | None":
        """把 Redis 值解成受校验的会话 dict；任何畸形（非 JSON / 结构不符）→ None。

        校验口径与 `accounts._load_sessions` 一致：user_id / username 必须是 str，
        expires_at 必须是数值——不满足就当畸形条目丢弃，绝不把半截数据交给调用方。
        """
        try:
            text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
            data = json.loads(text)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return None
        if not (
            isinstance(data, dict)
            and isinstance(data.get("user_id"), str)
            and isinstance(data.get("username"), str)
            and isinstance(data.get("expires_at"), (int, float))
        ):
            return None
        return {
            "user_id": data["user_id"],
            "username": data["username"],
            "expires_at": float(data["expires_at"]),
        }
