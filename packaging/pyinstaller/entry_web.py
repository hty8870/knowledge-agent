# -*- coding: utf-8 -*-
"""BioDataAgent.exe 冻结入口——desktop_launcher.main 薄转发。

实测发现早期产物（uvicorn.run 直启）无启动器行为：`--tray-selfcheck` 被忽略、
无托盘、无单实例 mutex、无固定端口持久化/attach。
现把 Web exe 入口统一改走 `desktop_launcher.main`（启动器契约 1：入口 main 供
spec 直接引用）——托盘、单实例、固定端口、二次启动 attach、脱敏滚动日志全部由启动器
接管。本文件保留文件名作为 spec 的脚本入口，仅做一行薄转发：main 内部先
`_guard_streams()`（windowed 流安全）再 `_parse_args`（--tray-selfcheck 等），
与 launcher 内部 `_default_app()` 惰性 import webapp.app 等价（不再在此直引 webapp）。

source 模式（如有人直接 `python packaging/pyinstaller/entry_web.py`）：
desktop_launcher 自带 `__file__` 锚定 sys.path → src，行为与 frozen 一致。
"""
from __future__ import annotations

import sys

# source 模式（有人直接 `python packaging/pyinstaller/entry_web.py`）：desktop_launcher
# 位于 src/dataset_recommender/app/，其 __file__ 锚定只能在该模块被导入后生效——导入
# 它之前必须先把 src 放进 sys.path（frozen 下 PyInstaller pathex=src 已含，此段为空操作）。
if not getattr(sys, "frozen", False):
    from pathlib import Path

    _SRC_ROOT = Path(__file__).resolve().parents[2] / "src"  # packaging/pyinstaller → 仓库根/src
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))

from dataset_recommender.app.desktop_launcher import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
