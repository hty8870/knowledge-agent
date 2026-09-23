#!/usr/bin/env bash
# -----------------------------------------------------------------------------
# BioData Agent · macOS/Linux 启动器（launch_web.ps1 的 POSIX bash 移植）
#
# 与 Windows 的 scripts/launch_web.ps1 语义对齐：自动找 Python 3.10+ → 建/复用 .venv →
# 按需装依赖 → 首启三问向导（可全跳过、任何失败不阻断起服）→ 端口漂移（7860-7869）与
# 「同 install_root 正在运行则复用」→ 就绪后由 run_web.py --open 自动开浏览器。
#
# 直接用法：bash scripts/launch_web.sh [<project-root>]
#   无参时按本脚本自身位置推断项目根（scripts/..）。根入口 scripts/run_web.sh 已被
#   新一键脚本取代；浏览器打开走 run_web.py --open（不在本脚本重复造）。
#
# 设计说明：
#   * 本脚本必须用 bash 运行（/dev/tcp 健康探测、pipefail、数组、[[ ]]）。根入口
#     打开前端.sh / 打开前端.command 用 POSIX sh 定位项目根后再 exec bash 本脚本。
#   * 可被 tests/test_posix_launcher.py 以 `source 本脚本` 方式加载测试纯函数：
#     顶层只做赋值与函数定义，结尾用 BASH_SOURCE 守卫保证「执行时才跑 main」。
#   * 首启向导文案保持英文（与 Win 版/既有文档一致）；致命错误用中文给出自救提示。
# -----------------------------------------------------------------------------

set -euo pipefail

# ---------------------------------------------------------------------------
# 版本与服务的单一真源：服务名固定，版本优先从 webapp.py 的 WEB_API_VERSION 解析，
# 只保留同值字面量作 fallback（与 launch_web.ps1 同款，避免未来版本升级后「已运行」
# 检测漂移；tests/test_release_version_contract.py 钉住 fallback 与 WEB_API_VERSION 同步）。
# ---------------------------------------------------------------------------
EXPECTED_SERVICE='dataset-recommender-web'
EXPECTED_VERSION_FALLBACK='3.0.0'
EXPECTED_VERSION="$EXPECTED_VERSION_FALLBACK"

# 复用的「哪份安装」由健康检查 /api/health 回填（同版本多份安装并存时绝不静默吸附）。
REUSED_INSTALL_ROOT=''

# 首启向导的 LLM 提供商预设（与 launch_web.ps1 的 $Script:LlmPresets 逐项一致）。
# 每项格式：<n>|<label>|<wire>|<base_url>|<model>（用 | 分隔，避免转义麻烦）。
LLM_PRESETS=(
  '1|DeepSeek|openai-compatible|https://api.deepseek.com|deepseek-chat'
  '2|Kimi|openai-compatible|https://api.moonshot.cn/v1|kimi-k2.6'
  '3|Qwen|openai-compatible|https://dashscope.aliyuncs.com/compatible-mode/v1|qwen-plus'
  '4|GLM (Zhipu)|zhipuai|https://open.bigmodel.cn/api/paas/v4/|glm-5.1'
  '5|OpenRouter|openai-compatible|https://openrouter.ai/api/v1|openrouter/auto'
  '6|OpenAI|openai-compatible|https://api.openai.com/v1|gpt-4o-mini'
  '7|Other OpenAI-compatible endpoint|openai-compatible||'
  '8|Local model (Ollama)|openai-compatible|http://localhost:11434/v1|llama3.1'
)

# 读取 webapp.py 的真实版本号覆盖 fallback（失败则保留 fallback，绝不阻断启动）。
_parse_expected_version() {
  local webapp_py="${1:-}"
  if [ -n "$webapp_py" ] && [ -f "$webapp_py" ]; then
    local found
    found="$(grep -E '^WEB_API_VERSION = "[0-9]+\.[0-9]+\.[0-9]+"$' "$webapp_py" 2>/dev/null | head -n1 || true)"
    if [ -n "$found" ]; then
      EXPECTED_VERSION="${found#WEB_API_VERSION = \"}"
      EXPECTED_VERSION="${EXPECTED_VERSION%\"}"
    fi
  fi
}

# 打印致命错误（stderr），供调用方随后 exit 1。
err() {
  printf '[ERROR] %s\n' "$*" >&2
}

# ---------------------------------------------------------------------------
# Python 探测
# ---------------------------------------------------------------------------

# 判定某路径是否为「可执行的真实 Python 3.10+」：必须是常规文件、有执行位，且能真正
# 跑起来判别版本（避免 Store stub / 目录 / 无法执行的假路径进入候选）。
python_ok() {
  local path="$1"
  [ -n "$path" ] || return 1
  [ -f "$path" ] || return 1
  [ -x "$path" ] || return 1
  "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 9)' >/dev/null 2>&1
}

# 解析一个系统 Python（非 venv 专属）。顺序：
#   1) $BIODATA_PYTHON（显式覆盖，校验失败即报错）
#   2) PATH 里的 python3、python
#   3) /opt/homebrew/bin/python3、/usr/local/bin/python3（候选，必须真实可执行且 >= 3.10）
# 成功打印路径并返回 0；失败返回 1（不打印，由调用方决定报错文案）。
resolve_base_python() {
  if [ -n "${BIODATA_PYTHON:-}" ]; then
    if python_ok "$BIODATA_PYTHON"; then
      printf '%s\n' "$BIODATA_PYTHON"
      return 0
    fi
    err "BIODATA_PYTHON does not point to a usable Python 3.10+: $BIODATA_PYTHON"
    return 1
  fi

  local name p
  for name in python3 python; do
    if command -v "$name" >/dev/null 2>&1; then
      p="$(command -v "$name")"
      if python_ok "$p"; then
        printf '%s\n' "$p"
        return 0
      fi
    fi
  done

  for p in /opt/homebrew/bin/python3 /usr/local/bin/python3; do
    if python_ok "$p"; then
      printf '%s\n' "$p"
      return 0
    fi
  done

  return 1
}

# 判定 venv 里 web 依赖（fastapi/uvicorn/multipart）是否齐备（对齐 Test-WebDependencies）。
webdeps_ok() {
  local py="$1"
  "$py" -c 'import fastapi, uvicorn, multipart' >/dev/null 2>&1
}

# 判定 venv 里 agent 执行依赖（langgraph 链）是否齐备（对齐 Test-AgentExecDependencies）。
agent_exec_deps_ok() {
  local py="$1"
  "$py" -c 'import langgraph, langchain_core, langchain_openai' >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# 端口与「同 install_root 实例复用」
# ---------------------------------------------------------------------------

# 端口是否已被任何进程占用（对齐 Test-PortOpen：仅 TCP 连接成功即视为占用）。
# /dev/tcp 是 bash 内建；用文件描述符 3 做一次连接探测。
port_open() {
  local port="$1"
  (exec 3<>"/dev/tcp/127.0.0.1/${port}") 2>/dev/null
}

# 拉取 http 响应体：优先 curl，次选 python3 urllib（都没有则失败）。
http_get() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 2 "$url" 2>/dev/null || return 1
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    python3 - "$url" <<'PY' 2>/dev/null || return 1
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1], timeout=2) as resp:
        sys.stdout.write(resp.read().decode("utf-8", "replace"))
except Exception:
    sys.exit(1)
PY
    return 0
  fi
  return 1
}

# 判定某端口是否为「本项目的当前版本正在运行」（对齐 Test-ExpectedServer）：
# 健康检查必须 ok=true、service=dataset-recommender-web、version=精确匹配当前版本。
# 命中则把 install_root 记入 REUSED_INSTALL_ROOT，供复用分支指名道姓。
expected_server() {
  local port="$1"
  local body
  body="$(http_get "http://127.0.0.1:${port}/api/health")" || return 1
  printf '%s' "$body" | grep -q '"ok"[[:space:]]*:[[:space:]]*true' || return 1
  printf '%s' "$body" | grep -q "\"service\"[[:space:]]*:[[:space:]]*\"${EXPECTED_SERVICE}\"" || return 1
  printf '%s' "$body" | grep -q "\"version\"[[:space:]]*:[[:space:]]*\"${EXPECTED_VERSION}\"" || return 1
  REUSED_INSTALL_ROOT="$(printf '%s' "$body" | grep -o '"install_root"[[:space:]]*:[[:space:]]*"[^"]*"' | head -n1 | sed -E 's/.*"install_root"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/')"
  return 0
}

# 决策启动端口（对齐 Resolve-LaunchPort）：
#   * 首选端口空闲     -> 用首选
#   * 首选被「我们自己」占 -> 回传负值（调用方走复用分支）
#   * 否则在 7861-7869 里取第一个空闲端口
#   * 全被占           -> 报错退出
resolve_port() {
  local preferred="$1"
  if ! port_open "$preferred"; then
    printf '%s\n' "$preferred"
    return 0
  fi
  if expected_server "$preferred"; then
    printf '%s\n' "-${preferred}"
    return 0
  fi
  local c
  for c in $(seq "$((preferred + 1))" "$((preferred + 9))"); do
    if ! port_open "$c"; then
      printf '%s\n' "$c"
      return 0
    fi
  done
  err "Ports ${preferred}-$((preferred + 9)) are all occupied. Close an old BioData Agent window and try again."
  return 1
}

# 打开一个 URL（复用分支用；新实例走 run_web.py --open，不经这里）。BIODATA_NO_BROWSER=1 时跳过。
open_url() {
  local url="$1"
  [ "${BIODATA_NO_BROWSER:-0}" = "1" ] && return 0
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$url" >/dev/null 2>&1 || true
  elif command -v open >/dev/null 2>&1; then
    open "$url" >/dev/null 2>&1 || true
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys, webbrowser; webbrowser.open(sys.argv[1])' "$url" >/dev/null 2>&1 || true
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 首启三问向导（可选、问一次、全部可跳过、任何失败不阻断起服）。
# 文案保持英文（与 launch_web.ps1 及既有文档一致），致命错误中文。
# ---------------------------------------------------------------------------

# 隐藏输入读一个秘密（密钥）；优先用 stty 关回显，非 tty 时退化。
read_secret() {
  local prompt="$1"
  local secret=''
  local stty_orig
  stty_orig="$(stty -g 2>/dev/null || true)"
  if [ -n "$stty_orig" ]; then
    stty -echo 2>/dev/null || true
  fi
  printf '%s' "$prompt" >&2
  IFS= read -r secret
  if [ -n "$stty_orig" ]; then
    stty "$stty_orig" 2>/dev/null || true
    printf '\n' >&2
  fi
  printf '%s' "$secret"
}

# 写 .env 文件（对齐 Set-LlmEnvFile）。ENABLE_LLM 必须 true——显式 false 会让填好的密钥不生效。
set_llm_env_file() {
  local env_file="$1"
  printf '\n'
  local p n label
  for p in "${LLM_PRESETS[@]}"; do
    n="${p%%|*}"
    label="$(printf '%s' "$p" | cut -d'|' -f2)"
    printf '    %s) %s\n' "$n" "$label"
  done

  local choice
  printf 'Provider number (Enter to cancel): '
  IFS= read -r choice
  choice="$(printf '%s' "$choice" | tr -d '[:space:]')"

  local sel=''
  for p in "${LLM_PRESETS[@]}"; do
    if [ "${p%%|*}" = "$choice" ]; then sel="$p"; break; fi
  done
  if [ -z "$sel" ]; then
    printf '    Cancelled - no AI configured.\n'
    return 0
  fi

  local wire base model
  wire="$(printf '%s' "$sel" | cut -d'|' -f3)"
  base="$(printf '%s' "$sel" | cut -d'|' -f4)"
  model="$(printf '%s' "$sel" | cut -d'|' -f5)"

  if [ -z "$base" ]; then
    printf 'Base URL: '
    IFS= read -r base
    base="$(printf '%s' "$base" | tr -d '[:space:]')"
  fi
  if [ -z "$model" ]; then
    printf 'Model name: '
    IFS= read -r model
    model="$(printf '%s' "$model" | tr -d '[:space:]')"
  fi
  if [ -z "$base" ] || [ -z "$model" ]; then
    printf '    Base URL or model missing - no AI configured.\n'
    return 0
  fi

  local key
  key="$(read_secret 'API Key (input stays hidden): ')"
  if [ -z "$key" ]; then
    printf '    Empty key - no AI configured.\n'
    return 0
  fi

  if ! {
    printf '# Written by the BioData Agent first-run setup.\n'
    printf '# Delete this file to go back to the key-free, fully offline setup.\n'
    printf 'DATA_DIR=database/base\n'
    printf 'TOP_K=10\n'
    printf 'ENABLE_LLM=true\n'
    printf 'MOCK_LLM=false\n'
    printf 'LLM_PROVIDER=%s\n' "$wire"
    printf 'LLM_API_KEY=%s\n' "$key"
    printf 'LLM_BASE_URL=%s\n' "$base"
    printf 'LLM_MODEL=%s\n' "$model"
    printf 'LLM_TIMEOUT=60\n'
    printf 'LLM_TEMPERATURE=0.2\n'
    printf 'LLM_MAX_TOKENS=8000\n'
  } > "$env_file" 2>/dev/null; then
    printf '    Could not write .env - no AI configured.\n'
    return 0
  fi
  # 尽量收紧 .env 权限（含密钥），失败不阻断。
  chmod 600 "$env_file" 2>/dev/null || true
  printf '    Saved to .env (provider: %s, model: %s). The key is never printed.\n' "$label" "$model"
}

# 目录存在 ≠ 模型已下好：fetch_embedding_model.py 建目录先于下载，失败会留空目录。
# 必须真的找到至少一个文件才认为模型已装（对齐 Test-LocalModelPresent）。
local_model_present() {
  local root="$1"
  local dir="$root/models/cross_encoders"
  [ -d "$dir" ] || return 1
  [ -n "$(find "$dir" -type f -print -quit 2>/dev/null)" ]
}

# 模型依赖安装：先装 requirements-embeddings.txt，再跑 fetch_embedding_model.py 拉权重。
# uv 建的 .venv 没有 pip，须与主依赖路径同样分流（对齐 Install-LocalSemanticModel）。
install_local_semantic_model() {
  local root="$1" py="$2"
  local req="$root/requirements-embeddings.txt"
  # 仓库克隆布局（2026-08-27 一级目录整理）：requirements 群在 requirements/ 下。
  [ -f "$req" ] || req="$root/requirements/requirements-embeddings.txt"
  local fetch="$root/scripts/fetch_embedding_model.py"
  if [ ! -f "$req" ] || [ ! -f "$fetch" ]; then
    printf '    Model installer is not part of this package - skipped.\n'
    return 0
  fi
  printf '    Installing model dependencies (several minutes)...\n'
  local install_rc=0
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$py" -r "$req" >/dev/null 2>&1 || install_rc=$?
  else
    "$py" -m pip install --disable-pip-version-check -r "$req" >/dev/null 2>&1 || install_rc=$?
  fi
  if [ "$install_rc" -ne 0 ]; then
    printf '    Dependency install failed - skipped. The app still works.\n'
    return 0
  fi
  printf '    Downloading model weights (about 2.2 GB, ModelScope first)...\n'
  if ! "$py" "$fetch" >/dev/null 2>&1; then
    printf '    Model download failed - skipped. The app still works.\n'
    return 0
  fi
  if ! local_model_present "$root"; then
    printf '    Download reported success but no weights are on disk - skipped.\n'
    return 0
  fi
  printf '    Local semantic model is ready.\n'
}

# 安装 agent 执行依赖（langgraph 链），失败可跳过（对齐 Install-AgentExecDependencies）。
install_agent_exec_deps() {
  local root="$1" py="$2"
  local req="$root/requirements-langchain.txt"
  # 仓库克隆布局（2026-08-27 一级目录整理）：requirements 群在 requirements/ 下。
  [ -f "$req" ] || req="$root/requirements/requirements-langchain.txt"
  if [ ! -f "$req" ]; then
    printf '    requirements-langchain.txt is not part of this package - skipped.\n'
    return 0
  fi
  printf '    Installing agent execution dependencies (langgraph, usually under a minute)...\n'
  local install_rc=0
  if command -v uv >/dev/null 2>&1; then
    uv pip install --python "$py" -r "$req" >/dev/null 2>&1 || install_rc=$?
  else
    "$py" -m pip install --disable-pip-version-check -r "$req" >/dev/null 2>&1 || install_rc=$?
  fi
  if [ "$install_rc" -ne 0 ]; then
    printf '    Install failed - skipped. AI execution falls back to the built-in planner.\n'
    return 0
  fi
  if agent_exec_deps_ok "$py"; then
    printf '    Agent execution (langgraph) is ready.\n'
  else
    printf '    Import check failed after install - AI execution falls back to the built-in planner.\n'
  fi
}

# 首启向导（对齐 Invoke-FirstRunSetup）：
#   * marker 落在项目根（不在 .venv——venv 可能在别处，写不存在的目录会反复重问）
#   * BIODATA_SKIP_SETUP=1 跳过；非交互（stdin 非 tty）绝不等输入
#   * 三个问题都可回车跳过；any error 被调用方兜住，绝不阻断起服
invoke_first_run_setup() {
  local root="$1" py="$2"
  local marker="$root/.biodata-setup-done"
  [ -e "$marker" ] && return 0
  [ "${BIODATA_SKIP_SETUP:-0}" = "1" ] && return 0
  if [ ! -t 0 ]; then return 0; fi  # 非交互：绝不阻塞

  local env_file="$root/.env"
  local ask_ai=0 ask_model=0 ask_agent=0
  [ -f "$env_file" ] || ask_ai=1
  local_model_present "$root" || ask_model=1
  agent_exec_deps_ok "$py" || ask_agent=1

  if [ "$ask_ai" -eq 1 ] || [ "$ask_model" -eq 1 ] || [ "$ask_agent" -eq 1 ]; then
    printf '\n=== First-run setup - all steps are optional, Enter skips ===\n'

    if [ "$ask_ai" -eq 1 ]; then
      printf '\n[AI] Searching works fully offline and needs no API key.\n'
      printf '     A key is only needed for AI re-ranking / polished wording / Chinese overview.\n'
      printf '     Note: this configures the SERVER. In the web UI you still pick the same\n'
      printf '     provider under Settings -> AI / API and switch the AI toggle on.\n'
      printf 'Configure an AI API key now? [y/N]: '
      local ans
      IFS= read -r ans
      case "$ans" in
        [yY]*) set_llm_env_file "$env_file" ;;
        *) printf '    Skipped. You can add it any time in the web UI: Settings -> AI / API.\n' ;;
      esac
    fi

    if [ "$ask_agent" -eq 1 ]; then
      printf '\n[Agent] Optional langgraph dependencies for the multi-step AI executor.\n'
      printf '        Without them AI execution falls back to the built-in single-step planner.\n'
      printf 'Install agent execution dependencies now? [y/N]: '
      IFS= read -r ans
      case "$ans" in
        [yY]*) install_agent_exec_deps "$root" "$py" ;;
        *) printf '    Skipped. You can install them later: pip install -r requirements-langchain.txt\n' ;;
      esac
    fi

    if [ "$ask_model" -eq 1 ]; then
      printf '\n[Model] Optional local semantic re-ranking model (about 3 GB download, ~5 GB on disk, slow).\n'
      printf '        Without it the app falls back to rule-based ordering and stays fully usable.\n'
      printf 'Download the local model now? [y/N]: '
      IFS= read -r ans
      case "$ans" in
        [yY]*) install_local_semantic_model "$root" "$py" ;;
        *) printf '    Skipped. You can install it later - see the manual, section 10.4.\n' ;;
      esac
    fi

    printf '\n=== Setup finished ===\n'
  fi

  # 项目目录只读时写失败则下次再问；尽力而为，不阻断。
  : > "$marker" 2>/dev/null || true
}

# ---------------------------------------------------------------------------
# 主流程（对齐 launch_web.ps1 的 try 主体）
# ---------------------------------------------------------------------------

main() {
  local root="${1:-}"
  if [ -z "$root" ]; then
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  fi

  # 版本号单一真源：先解析 webapp.py，失败保留 fallback。
  _parse_expected_version "$root/src/dataset_recommender/app/webapp.py"

  local runner="$root/scripts/run_web.py"
  if [ ! -f "$runner" ]; then
    err "Project files are incomplete: scripts/run_web.py was not found under $root"
    return 1
  fi

  # PORT 环境变量：必须是 1-65526 的整数（对齐 ps1，非数字/越界报错）。
  local preferred_port=7860
  if [ -n "${PORT:-}" ]; then
    if ! printf '%s' "$PORT" | grep -Eq '^[0-9]+$'; then
      err 'PORT must be an integer from 1 to 65526.'
      return 1
    fi
    if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65526 ]; then
      err 'PORT must be an integer from 1 to 65526.'
      return 1
    fi
    preferred_port="$PORT"
  fi

  # 端口决策。
  local port_decision
  port_decision="$(resolve_port "$preferred_port")" || return 1

  # 复用分支：当前版本已在运行（无论哪份安装），直接开浏览器并退出。
  if [ "$port_decision" -lt 0 ]; then
    local running_port=$((-port_decision))
    local running_url="http://127.0.0.1:${running_port}"
    printf 'The current BioData Agent is already running at %s\n' "$running_url"
    if [ -n "$REUSED_INSTALL_ROOT" ]; then
      local reused here
      reused="${REUSED_INSTALL_ROOT%/}"
      here="${root%/}"
      if [ "$reused" = "$here" ]; then
        printf 'Reusing this install: %s\n' "$reused"
      else
        printf 'NOTE: reusing the instance served from a DIFFERENT install:\n'
        printf '  running:  %s\n' "$reused"
        printf '  this one: %s\n' "$here"
        printf 'Close the running one first if you meant to start this copy.\n'
      fi
    fi
    open_url "$running_url"
    return 0
  fi

  local port="$port_decision"
  if [ "$port" -ne "$preferred_port" ]; then
    printf 'Port %s is occupied by an older/different service; using port %s for the current build.\n' "$preferred_port" "$port"
  fi
  local url="http://127.0.0.1:${port}"

  # Python 环境选择顺序（首启文档须描述同一行为）：
  #   1) $BIODATA_PYTHON（显式覆盖，已校验）  2) 项目内 .venv
  #   3) 都没有 -> 新建项目内 .venv。不得静默复用 workspace/MCP 环境：否则测试依赖和
  #      无关升级会泄漏进运行环境；高级用户仍可用 BIODATA_PYTHON 明确选择共享解释器。
  local venv_python="$root/.venv/bin/python"

  local python=''
  if [ -n "${BIODATA_PYTHON:-}" ]; then
    python="$(resolve_base_python)" || return 1
  elif python_ok "$venv_python"; then
    python="$venv_python"
  else
    local base_python
    base_python="$(resolve_base_python || true)"
    if [ -n "$base_python" ]; then
      printf '[1/3] Creating the project-local Python environment (.venv)...\n'
      "$base_python" -m venv "$root/.venv"
    elif command -v uv >/dev/null 2>&1; then
      printf '[1/3] Creating the project-local Python environment with uv (.venv)...\n'
      uv venv "$root/.venv" --python 3.12
    else
      err 'Python 3.10+ was not found. Install Python or uv, then run this launcher again.'
      err '  Ubuntu/Debian:  sudo apt install python3 python3-venv'
      err '  fedora/RHEL:    sudo dnf install python3'
      err '  macOS:         brew install python@3.12  （或在 python.org 下载安装包）'
      err '  Windows/mac/Linux 通用：安装后可用 BIODATA_PYTHON 指向解释器绝对路径。'
      return 1
    fi
    if ! python_ok "$venv_python"; then
      err 'Could not create the project-local Python environment.'
      return 1
    fi
    # venv 建成但缺 pip 的半成品（Debian/Ubuntu 未装 python3-venv 时的典型形态）——
    # 先 ensurepip 自救，仍不行再给明确指引。uv 建的 venv 刻意无 pip，走 uv 安装，跳过此检查。
    if ! command -v uv >/dev/null 2>&1; then
      if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
        "$venv_python" -m ensurepip --upgrade >/dev/null 2>&1 || true
      fi
      if ! "$venv_python" -m pip --version >/dev/null 2>&1; then
        err 'Python 环境缺少 pip（常见于 Debian/Ubuntu 未装 python3-venv）。'
        err '  Ubuntu/Debian:  sudo apt install python3-venv 后重试'
        return 1
      fi
    fi
    python="$venv_python"
  fi

  # 依赖检查与安装（仅首启需要联网；失败给清晰中文自救提示）。
  if ! webdeps_ok "$python"; then
    # 仓库克隆布局（2026-08-27 一级目录整理）：requirements 群在 requirements/ 下。
    local req_main="$root/requirements.txt"
    [ -f "$req_main" ] || req_main="$root/requirements/requirements.txt"
    if [ ! -f "$req_main" ]; then
      err 'requirements.txt is missing; dependencies cannot be installed.'
      return 1
    fi
    printf '[2/3] Installing required packages (first launch only)...\n'
    local install_rc=0
    if command -v uv >/dev/null 2>&1; then
      uv pip install --python "$python" -r "$req_main" >/dev/null 2>&1 || install_rc=$?
    else
      # 老 pip 认不出新 manylinux 标签会退到源码构建再失败——先尽力自升级（失败不阻断）。
      "$python" -m pip install --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
      "$python" -m pip install --disable-pip-version-check -r "$req_main" >/dev/null 2>&1 || install_rc=$?
    fi
    if [ "$install_rc" -ne 0 ]; then
      err '依赖安装失败。请检查网络后重试；若在公司代理内网，先设置 HTTP_PROXY / HTTPS_PROXY。'
      err '  若你的 Python 低于 3.10，请安装 Python 3.10+ 并用 BIODATA_PYTHON 指向它。'
      err '  也确认 requirements.txt 存在且未被改动。'
      return 1
    fi
  fi

  if ! webdeps_ok "$python"; then
    err '安装后必需依赖仍不可用，请检查网络 / Python 环境后重试。'
    return 1
  fi

  # CI/发布候选首启探针：必须由全新 zip 自建项目内运行 venv，Web 依赖可导入，
  # 且生产 requirements 没把 pytest 带进来。探针在向导、浏览器和服务启动前退出。
  if [ "${BIODATA_LAUNCH_PROBE:-0}" = "1" ]; then
    if [ "$python" != "$venv_python" ]; then
      err "Launch probe did not use the project-local venv: $python"
      return 1
    fi
    if "$python" -c 'import pytest' >/dev/null 2>&1; then
      err 'Launch probe found dev-only pytest in the runtime venv.'
      return 1
    fi
    printf 'BIODATA_LAUNCH_PROBE_OK python=%s pytest=absent\n' "$python"
    return 0
  fi

  # 可选一次性部署步骤；向导任何失败都被兜住，服务器照常起。
  if ! invoke_first_run_setup "$root" "$python"; then
    printf '[setup] Skipped (error in the first-run setup).\n' >&2
  fi

  printf '[3/3] Starting the current BioData Agent build...\n'
  printf 'Project: %s\n' "$root"
  printf 'URL:     %s\n' "$url"
  printf 'Close this window to stop the server.\n'

  export PYTHONIOENCODING=utf-8
  export PORT="$port"
  cd "$root"

  # 浏览器打开走 run_web.py --open（服务就绪自动开页）；BIODATA_NO_BROWSER=1 时不带。
  if [ "${BIODATA_NO_BROWSER:-0}" = "1" ]; then
    exec "$python" "$runner"
  else
    exec "$python" "$runner" --open
  fi
}

# 执行而非 source 时才运行 main（便于测试 source 后只取纯函数）。
# 显式捕获 main 退出码，规避 set -e 在复合命令里不提前退出的边缘情况。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
  exit $?
fi
