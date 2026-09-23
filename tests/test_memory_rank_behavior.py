# -*- coding: utf-8 -*-
"""用户记忆纯排序核「真行为」门——用 node 跑 tests/js/memory_rank_spec.js 的行为断言。

为什么要它：`web_smoke_test.py` 只对前端 JS 做静态字符串检查、`node --check` 只验语法，两门都
**测不出排序结果对不对**（旧 memoryScore 的正确性此前只能「node 手验」）。把排序/淘汰逻辑抽成纯核
`web/static/js/panel/memory_rank.js` 后，这里经真实 node 进程对 IDF 区分度 / useCount 强化 / 时间衰减 /
偏好分层 / 偏好防淘汰下确定性断言——让排序数学有回归网，未来重构误伤打分会在 pytest 里当场亮红。

node 解析对齐 automation/quality-gates.json（BIODATA_NODE > node > node.exe）。full 门本就要求 node
（javascript-syntax 门），故正常环境必有；仅在确无 node 的裸环境跳过（不掩盖回归——full 门仍会跑）。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "js" / "memory_rank_spec.mjs"


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def test_memory_rank_behavior_spec_passes_under_node() -> None:
    assert SPEC.is_file(), f"缺少行为规格文件：{SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门（full 质量门的 javascript-syntax 环境必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    assert proc.returncode == 0, f"memory_rank 行为规格失败：\n{combined}"
    assert "MEMORY_RANK_SPEC_OK" in (proc.stdout or ""), f"未见成功标记：\n{combined}"
