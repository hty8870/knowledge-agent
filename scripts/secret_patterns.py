# -*- coding: utf-8 -*-
"""兼容壳：真源已迁至 `src/dataset_recommender/secret_patterns.py`
（mcp_server 值级脱敏与质量门/交付扫描共用，分层方向 scripts → src 不反转）。
本文件只剩重导出——既有引用点（make_delivery / quality_gate / precommit_check /
tests/test_secret_scan.py）零改动。"""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dataset_recommender.secret_patterns import (  # noqa: E402,F401
    SECRET_VALUE_PATTERNS,
    find_secret_patterns,
    redact,
)
