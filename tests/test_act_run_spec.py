# -*- coding: utf-8 -*-
"""行动流（act_run.js）的真行为门：把 tests/js/act_run_spec.mjs 在 node 里真跑一遍。

钉的两条不变量：
-  两通道一致：plan.trace 的 state/detail 必须随 arxStep 进行动流、随 arxFinish 快照进
  总结泡 details——失败节点亮 ✗ 且带原因，空闲通道与 busy 快照通道同一事实同一渲染；
-  僵尸行动流：arxFinish 后的 420ms 折叠窗口内 arxActive() 为假，新派发 arxBegin 另开
  新流（bump seq），旧折叠计时器不得把含在途步骤的新流判 null（步骤静默丢失）。

三门都不执行 JS（web_smoke 静态查串、node --check 只验语法），这两个时序/状态不变量
只有真跑才有回归网。规格本体在 tests/js/act_run_spec.mjs（与既有 *_spec.mjs 同一范式）。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tests" / "js" / "act_run_spec.mjs"


def _resolve_node() -> "str | None":
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def test_act_run_spec_passes_in_node():
    node = _resolve_node()
    if not node:
        pytest.skip("未解析到 node.js —— 跳过行动流真行为门（full 质量门的语法检查环节必有 node）。")
    proc = subprocess.run(
        [node, str(SPEC)], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert proc.returncode == 0, f"act_run_spec 失败：\n{proc.stdout}\n{proc.stderr}"
    assert "OK act_run_spec.mjs" in proc.stdout
