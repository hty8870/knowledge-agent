# -*- coding: utf-8 -*-
"""在线 MCP 形态的 LLM 成本闸：contextvar 级「本调用禁用真 LLM」。

为什么需要它：在线 MCP（webapp 挂载 `/mcp`）的工具调用用的是**服务端配置的 LLM key**，
任何持有令牌的人若能把 `use_llm` / `rerank=llm` 打开，就是在烧服务器账单。显式 LLM
参数由 MCP 层（mcp_server 的在线策略）显式拒绝；但 `plan_action` 等工具的 LLM 使用是
**隐式**的（内部 `should_use_llm` 只问服务端配没配 key，不问调用来源）——参数层拦不到，
需要一个随调用上下文传播的开关。

为什么用 contextvar 而不是 env/全局：webapp 进程同时服务网页请求（试用通道合法地用
服务端 key、按账户每日额度计量）与在线 MCP 调用，进程级开关会互相串扰。contextvar
随 anyio worker 线程上下文传播（`anyio.to_thread.run_sync` copy_context 实读确认），
异步任务之间天然隔离。

与 patch_package.bind_patch_scope 同款先例：集成零签名变更，缺省 False = 历史行为
逐字节不变。
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Iterator

_FORCE_OFF: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "biodata_llm_force_off", default=False)


def llm_forced_off() -> bool:
    """当前调用上下文是否被强制禁用真 LLM（在线 MCP 成本闸）。"""
    return _FORCE_OFF.get()


@contextlib.contextmanager
def force_llm_off() -> Iterator[None]:
    """本调用上下文中禁用真 LLM：`should_use_llm` 一律判否 → 隐式 LLM 路径降级为规则版
    （结果里的 `source: llm|rule` 字段如实反映降级，不静默）。"""
    token = _FORCE_OFF.set(True)
    try:
        yield
    finally:
        _FORCE_OFF.reset(token)
