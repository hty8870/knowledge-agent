#!/usr/bin/env bash
# BioData Agent · Linux/macOS 启动器（跨平台核查补充，此前只有 .bat/.ps1）。
# 用法：bash scripts/run_web.sh [端口]   —— 默认 7860，也可用 PORT 环境变量覆盖。
set -euo pipefail
cd "$(dirname "$0")/.."
export PORT="${1:-${PORT:-7860}}"
echo "BioData Agent web 前端启动中：http://127.0.0.1:${PORT}/ （Ctrl+C 停止）"
exec python3 scripts/run_web.py
