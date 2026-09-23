# -*- coding: utf-8 -*-
"""Web `/api/datasets` 与 MCP `browse_datasets` 分页上限的**单一真源**。

值取 **100**：以 Web 既有上限（2026-08-06 起）为准，MCP 从硬上限 200 收窄到 100——
browse 全库 5000+ 条，100 条/页已远大于任何真实对话的一屏需求；两入口同一常量源后，
超限错误语义一致（Web 422「limit 最大 100。」/ MCP bad_param ToolError，isError=true）。

改这个值 = 同时改 Web 与 MCP 两入口的对外上限，属公开契约变化，必须连同
`tests/test_webapp_security.py`（422 钉）、`tests/test_mcp_server.py`（bad_param 钉）与本文件注释原子更新。

超限语义一律是**显式拒绝**（Web 422 / MCP bad_param），不静默夹取——调用方必须知道
自己的请求被收窄了，静默截断会让「少传的几条」变成不可见的错误数据。
"""
MAX_DATASETS_LIMIT = 100
