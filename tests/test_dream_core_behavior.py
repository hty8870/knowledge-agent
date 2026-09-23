# -*- coding: utf-8 -*-
"""dream 纯核「真行为」门——用 node 跑 tests/js/dream_core_spec.js（与 memory_rank 行为门同构）。"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "js" / "dream_core_spec.mjs"


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def test_dream_core_behavior_spec_passes_under_node() -> None:
    assert SPEC.is_file(), f"缺少行为规格文件：{SPEC}"
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过纯核行为门（full 质量门的 javascript-syntax 环境必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)],
        capture_output=True, text=True, cwd=str(ROOT), timeout=60,
    )
    assert proc.returncode == 0, f"dream_core 行为门失败：\n{proc.stdout}\n{proc.stderr}"
    assert "DREAM_CORE_SPEC_OK" in proc.stdout
