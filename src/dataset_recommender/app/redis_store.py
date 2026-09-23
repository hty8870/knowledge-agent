# -*- coding: utf-8 -*-
"""共享 Redis 连接真源 —— 会话后端与消息队列后端共用（唯一建连/配置解析点）。

定位：BioData Agent 默认形态是**单机、零外部依赖**（内存会话 + 进程内线程），本模块只服务
**opt-in** 形态——设了 `BIODATA_REDIS_URL` / `BIODATA_SESSION_STORE=redis` 之类开关才被走到。
因此：

- **顶层不 import redis**：最小安装（默认 `requirements.txt`）没有 redis 包，顶层引入会让
  整个应用起不来。真正的 import 推迟到 `_build_client()`，缺包时抛 `RedisUnavailable`
  （机器码 `redis_package_missing`，提示装 `requirements/requirements-mq.txt`）。
- **只建连、不 ping**：`get_client()` 不做连通性探测。Redis 不可用要按各消费方的语义处理
  （会话侧 fail-open：丢了 = 重新登录；MQ 侧可能 fail-closed），建连时就 ping 会把
  「暂时连不上」提前变成「配置即报错」，反而堵死降级路径。
- **单例 + 测试接缝**：模块级惰性单例复用连接池；测试用 `_reset_for_tests()` 清场，
  或直接 monkeypatch `_build_client` / `_build_rq_client` 注入 fakeredis。

消费点：`app/redis_sessions.py`（会话后端）、`app/rq_jobs.py` / `app/rq_worker.py`（MQ 后端）。

两种解码模式
------------
- `get_client()`：`decode_responses=True`——业务键（会话、job 状态 hash）全是 UTF-8 文本，
  读出来直接 `json.loads`，省掉两侧编解码分叉。
- `get_rq_client()`：**bytes 模式**——RQ 2.x 的 `Job.to_dict()` 对序列化结果做 zlib 压缩，
  job hash 的 `data` 字段必为二进制；用 decode 模式连接跑真实 worker 会在 `Job.fetch()` 的
  `hgetall` 抛 UnicodeDecodeError（任务卡在 QUEUED）。RQ 官方队列/Worker 必须走本通道。
  两个单例共用同一 URL/超时真源，只是解码模式不同。
"""
from __future__ import annotations

import os
from typing import Any

#: 缺省地址：本机默认端口 + DB 0（与 `redis-server` 默认配置一致，示例性质，投产另议）。
_DEFAULT_URL = "redis://127.0.0.1:6379/0"

#: 命令超时（秒）。宁可快速失败也不要挂住请求线程——会话读写都在请求路径上。
_TIMEOUT_SECONDS = 2

_CLIENT: "Any | None" = None      # 模块级惰性单例（decode 模式，连接池在 client 内部复用）
_RQ_CLIENT: "Any | None" = None   # bytes 模式单例（RQ 专用，见模块 docstring「两种解码模式」）


class RedisUnavailable(Exception):
    """Redis 侧不可用（缺包 / 建连失败 / 命令失败）。

    与 `accounts.AccountError` 同构的 `code` + 中文 `message` 约定，但**独立成类**：
    本模块被多个后端共用，不能反向依赖 accounts（会把循环依赖带进来），也不能让
    会话后端的错误语义绑死账户模块。翻译职责在调用方——`accounts` 把本异常翻成
    `AccountError`（code 原样透传）。
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _RedisAbsentError(Exception):
    """占位异常：未安装 redis 包时充当「永不匹配」的错误基类。

    让调用方可以统一写 `except redis_store.redis_error_types()`，不必到处判断包在不在。
    """


def redis_url() -> str:
    """Redis 连接串：env `BIODATA_REDIS_URL`（空白视为未设）→ 缺省本机 `redis://127.0.0.1:6379/0`。"""
    return (os.environ.get("BIODATA_REDIS_URL", "") or "").strip() or _DEFAULT_URL


def redis_error_types() -> "tuple[type[BaseException], ...]":
    """惰性返回 redis 的异常基类元组（顶层不 import redis，缺包时返回占位类）。

    `redis.RedisError` 覆盖连接/超时/协议/数据类型等全部命令级错误——会话后端据此
    统一做 fail-open/翻译，不逐个枚举子类。
    """
    try:
        import redis
    except ImportError:
        return (_RedisAbsentError,)
    return (redis.RedisError,)


def _build_client() -> "Any":
    """建一个 redis 客户端（不 ping）。缺包 → `RedisUnavailable("redis_package_missing")`。

    `decode_responses=True`：值按 str 进出，会话 JSON 直接 `json.loads`，省掉两侧编解码分叉。
    """
    try:
        import redis
    except ImportError as exc:
        raise RedisUnavailable(
            "redis_package_missing",
            "未安装 redis 依赖包，无法启用 Redis 后端；请先执行 "
            "pip install -r requirements/requirements-mq.txt，或去掉 Redis 相关环境变量以回到默认形态。",
        ) from exc
    return redis.Redis.from_url(
        redis_url(),
        decode_responses=True,
        socket_timeout=_TIMEOUT_SECONDS,
        socket_connect_timeout=_TIMEOUT_SECONDS,
    )


def _build_rq_client() -> "Any":
    """建一个 **bytes 模式** redis 客户端，专供 RQ 队列/Worker（不 ping）。

    RQ 2.x 的 job hash 含 zlib 压缩的二进制字段，decode 模式连接读它会抛 UnicodeDecodeError
    （实测：真实 worker 取不到任务、job 停在 QUEUED）。URL/超时与 `_build_client` 同源。
    """
    try:
        import redis
    except ImportError as exc:
        raise RedisUnavailable(
            "redis_package_missing",
            "未安装 redis 依赖包，无法启用消息队列后端；请先执行 "
            "pip install -r requirements/requirements-mq.txt，或去掉 Redis 相关环境变量以回到默认形态。",
        ) from exc
    return redis.Redis.from_url(
        redis_url(),
        decode_responses=False,
        socket_timeout=_TIMEOUT_SECONDS,
        socket_connect_timeout=_TIMEOUT_SECONDS,
    )


def get_client() -> "Any":
    """模块级惰性单例：首次调用建连，之后复用（连接池随 client 存活）。

    只建连不 ping——连通性由调用方在真实命令上感知并各自降级（见模块 docstring）。
    """
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = _build_client()
    return _CLIENT


def get_rq_client() -> "Any":
    """bytes 模式惰性单例，RQ 队列/Worker 专用（理由见 `_build_rq_client`）。"""
    global _RQ_CLIENT
    if _RQ_CLIENT is None:
        _RQ_CLIENT = _build_rq_client()
    return _RQ_CLIENT


def _reset_for_tests() -> None:
    """仅供测试：清掉模块级单例，使下次 `get_client()`/`get_rq_client()` 重新走 builder。"""
    global _CLIENT, _RQ_CLIENT
    _CLIENT = None
    _RQ_CLIENT = None
