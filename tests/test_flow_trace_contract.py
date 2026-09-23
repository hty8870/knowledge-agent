# -*- coding: utf-8 -*-
"""信息流（flow_trace.js）真行为门：把 tests/js/flow_trace_spec.mjs 在 node 里真跑一遍。

三门都不执行 JS（web_smoke 静态查串、node --check 只验语法）， 信息流的核心不变量——
① 事件→工具行映射（preliminary→初步检索 rank 行；tool_start/step 一工具一行；route_consensus
  等分流元事件除名返回 null）；② 行状态机去重（同 id pending→done 更新同一行，不 append
  两行，修「图 2」重复消息）；③ 计数压缩（各工具调用次数按类别加和缩减为一行「执行了 N 次
  检索。」，失败如实标注，无工具行断串）；④ 覆盖丢弃（supersede 即丢弃，连存储也丢）——
只有真跑才有回归网。规格本体在 tests/js/flow_trace_spec.mjs（与既有 *_spec.mjs 同一范式）。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "js" / "flow_trace_spec.mjs"


def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def test_flow_trace_spec_passes_in_node():
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过信息流真行为门（full 质量门的语法检查环节必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"flow_trace_spec 失败：\n{proc.stdout}\n{proc.stderr}"
    assert "OK flow_trace_spec.mjs" in proc.stdout or "PASS" in proc.stdout
