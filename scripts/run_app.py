# -*- coding: utf-8 -*-
"""桌面窗口模式开发入口（source 模式）：`python scripts/run_app.py [--no-tray]`。

与 frozen 的 BioDataAgent.exe 同一入口链（desktop_launcher.main），只是预置 --window：
- 正常情况：起回环服务 → 弹 pywebview 原生窗口（关窗即退出）；BIODATA_NO_BROWSER=1
  可只起服务不开窗（CI/无头）。
- pywebview 未装（可选依赖）：自动回退开系统浏览器，行为与浏览器开发模式一致。
- 浏览器开发通道（截图/DevTools/Playwright 工作流）**不受影响**：照旧用
  `scripts/run_web.py` 或 launchers/start-web.bat，两者永远保留。

调试开关：BIODATA_SHELL_DEBUG=1 → 窗口内右键可开 DevTools（壳内排障）。
"""
from __future__ import annotations

import sys
from pathlib import Path

# source 模式锚定：desktop_launcher 位于 src/dataset_recommender/app/，导入前须先把 src
# 放进 sys.path（与 packaging/pyinstaller/entry_web.py 同口径）。
_SRC_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from dataset_recommender.app.desktop_launcher import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(["--window", *sys.argv[1:]]))
