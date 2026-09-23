#!/bin/sh
# -----------------------------------------------------------------------------
# BioData Agent · macOS/Linux 一键启动入口（对齐 Windows 的「打开前端.bat」）。
#
# 定位项目根（含 scripts/run_web.py 的目录），使本脚本在以下布局都能工作：
#   1) 与项目同层（正常）      2) 提交包布局 <包>\biodata-agent\
#   3) 任意一层子目录（改名/不同名解压）  4) 由「解压到当前目录」多套一层
#   5) 仓库克隆布局：本脚本位于项目根的 launchers/ 下（2026-08-27 一级目录整理）
# 定位到项目根后，把控制权交给 bash 运行的 scripts/launch_web.sh（引擎需要 bash：
# /dev/tcp 健康探测、pipefail、数组）。本入口刻意保持 POSIX sh，以保证 `sh 打开前端.sh`
# 在 dash（Debian/Ubuntu 的 sh）下也能跑；真正的 bash 逻辑都在 launch_web.sh 里。
#
# 用法：sh 打开前端.sh   或   ./打开前端.sh   （macOS 可双击打开前端.command）
# -----------------------------------------------------------------------------
set -u

# 本脚本所在目录；用它作为定位基准。
self_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)" || exit 1

root=""

# 1) 与本脚本同层（正常：入口就在项目内）
if [ -f "$self_dir/scripts/run_web.py" ]; then
  root="$self_dir"
fi

# 2) 提交包布局：项目在旁边的 biodata-agent 子目录里（先看约定名）
if [ -z "$root" ] && [ -f "$self_dir/biodata-agent/scripts/run_web.py" ]; then
  root="$self_dir/biodata-agent"
fi

# 3) 任意一层直接子目录里含项目（改名/不同名解压，取最先匹配）
if [ -z "$root" ]; then
  for d in "$self_dir"/*; do
    if [ -d "$d" ] && [ -f "$d/scripts/run_web.py" ]; then
      root="$d"
      break
    fi
  done
fi

# 4) 容忍「解压到当前目录」多套一层（<包>\<任意>\biodata-agent\）
if [ -z "$root" ]; then
  for d in "$self_dir"/*; do
    if [ -d "$d" ] && [ -f "$d/biodata-agent/scripts/run_web.py" ]; then
      root="$d/biodata-agent"
      break
    fi
  done
fi

# 4.5) 仓库克隆布局：本脚本在项目根的 launchers/ 下 → 项目根即上一级目录
if [ -z "$root" ] && [ -f "$self_dir/../scripts/run_web.py" ]; then
  root="$(CDPATH= cd -- "$self_dir/.." && pwd)"
fi

# 5) 常见安装位置（入口被单独拷出来时用）。首个命中生效，其余命中点名不生效。
if [ -z "$root" ]; then
  for p in \
    "${HOME:-}/Desktop/biodata-agent" \
    "${HOME:-}/Downloads/biodata-agent" \
    "${HOME:-}/Documents/biodata-agent" \
    "${HOME:-}/biodata-agent"; do
    if [ -f "$p/scripts/run_web.py" ]; then
      if [ -z "$root" ]; then
        root="$p"
        guessed=1
      else
        printf '[!] Another install also found at %s - ignored.\n' "$p" >&2
      fi
    fi
  done
fi
if [ -n "${guessed:-}" ]; then
  printf '[i] Using install at %s\n' "$root" >&2
fi

if [ -z "$root" ]; then
  printf '[!] Project files not found (scripts/run_web.py).\n' >&2
  printf '    Keep this launcher next to the biodata-agent folder,\n' >&2
  printf '    or inside it.\n' >&2
  exit 1
fi

# 调试/测试钩子：只打印定位到的根并退出（不启服务）。
if [ "${BIODATA_LOCATE_ONLY:-0}" = "1" ]; then
  printf '%s\n' "$root"
  exit 0
fi

# 把控制权交给真正的引擎（需要 bash）。显式传入已定位的项目根，引擎不再猜。
exec bash "$root/scripts/launch_web.sh" "$root"
