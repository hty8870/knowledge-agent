"""启动器「首次部署向导」的不变量门。

这个向导是**客户双击 .bat 后第一眼看到的东西**，又只在真实交互控制台里才会走到，
pytest 跑不到它的交互路径。所以这里钉的是静态不变量——三条都是验证真抓出来的回归：

1. **marker 不能写在 `.venv\\` 里**：venv 可能根本不在项目内（`BIODATA_PYTHON`、workspace venv、
   共享 venv），往不存在的目录写会抛异常 → marker 永远写不进去 → **每次启动都重问**。
2. **目录存在 ≠ 模型已下好**：`fetch_embedding_model.py` 在开始下载**之前**就把目标目录建好了，
   下载失败会留下空目录；只判目录存在会让脚本永远不再提示，而权重一个字节都没有。
   （这正是本项目「填了字段≠已核验」那条红线的同型违规。）
3. **uv 建的 venv 没有自己的 pip**：主依赖路径已经为此分流，模型依赖安装必须同样分流，
   否则 `-m pip` 必然失败。

外加两条安全不变量：非交互环境绝不阻塞启动；写 .env 时 `ENABLE_LLM` 必须是 true
（`.env.example` 明确写了「显式 false 永远覆盖『有 key 就开』」，写错等于密钥填了也不生效）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch_web.ps1"


@pytest.fixture(scope="module")
def src() -> str:
    return LAUNCHER.read_text(encoding="utf-8-sig")


def test_wizard_functions_exist(src: str) -> None:
    for name in (
        "Invoke-FirstRunSetup",
        "Set-LlmEnvFile",
        "Install-LocalSemanticModel",
        "Read-PlainSecret",
        "Test-LocalModelPresent",
        "Test-AgentExecDependencies",
        "Install-AgentExecDependencies",
    ):
        assert re.search(rf"^function {re.escape(name)} \{{", src, re.MULTILINE), f"缺函数 {name}"


def test_wizard_is_invoked_before_server_start(src: str) -> None:
    """向导必须在依赖装好之后、服务启动之前被调用，且包在 try/catch 里。"""
    call = src.index("Invoke-FirstRunSetup -Root $root")
    start = src.index("[3/3] Starting the current BioData Agent build")
    assert call < start, "向导必须在 [3/3] 启动之前调用"
    assert re.search(r"try \{ Invoke-FirstRunSetup .*?\}\s*\r?\n\s*catch \{", src), \
        "向导调用必须被 try/catch 包住——它失败绝不能挡住服务启动"


def test_marker_is_not_inside_venv(src: str) -> None:
    """回归：marker 曾写在 .venv\\ 下，venv 不在项目内时抛异常 → 每次启动重问。"""
    assert re.search(r"\$marker = Join-Path \$Root '\.biodata-setup-done'", src), \
        "marker 必须落在项目根"
    assert ".venv\\.biodata-setup-done" not in src, \
        "marker 不得依赖项目内 .venv 是否存在"


def test_launcher_does_not_silently_reuse_dev_or_mcp_venvs(src: str) -> None:
    """ah-d1：默认只用本项目 .venv；共享解释器必须由用户显式指定。"""
    assert "$workspacePython" not in src
    assert "$knownSharedPython" not in src
    assert ".venv-biodata-mcp" not in src
    assert "$env:BIODATA_PYTHON" in src
    assert "project-local .venv" in src


def test_first_launch_probe_requires_clean_runtime_without_pytest(src: str) -> None:
    assert "$env:BIODATA_LAUNCH_PROBE -eq '1'" in src
    assert "BIODATA_LAUNCH_PROBE_OK" in src
    assert "import pytest" in src
    probe = src.index("$env:BIODATA_LAUNCH_PROBE -eq '1'")
    wizard = src.index("Invoke-FirstRunSetup -Root $root", probe)
    assert probe < wizard, "探针必须在交互向导和服务启动前退出"


def test_model_presence_requires_a_real_file(src: str) -> None:
    """回归：只判目录存在会把「下载失败留下的空目录」当成已安装。"""
    m = re.search(r"function Test-LocalModelPresent \{(.*?)\n\}", src, re.DOTALL)
    assert m, "缺 Test-LocalModelPresent"
    body = m.group(1)
    assert "Get-ChildItem" in body and "-Recurse -File" in body, \
        "必须真的找到文件，不能只判目录存在"
    assert re.search(r"Test-LocalModelPresent -Root \$Root", src), \
        "向导必须用这个函数判断，而不是自己再判一次目录"
    assert not re.search(r"Test-Path[^\n]*'models\\cross_encoders'\)\)\) \{\s*\r?\n\s*Write-Host '\[Model\]", src), \
        "向导里不得残留裸的目录存在性判断"


def test_model_dependency_install_handles_uv(src: str) -> None:
    """回归：uv 建的 venv 没有 pip，模型依赖安装必须像主依赖路径一样分流。"""
    m = re.search(r"function Install-LocalSemanticModel \{(.*?)\n\}\r?\n\r?\nfunction ", src, re.DOTALL)
    assert m, "缺 Install-LocalSemanticModel"
    body = m.group(1)
    assert "Get-Command uv" in body, "模型依赖安装必须处理 uv 环境"
    assert "uv.Source pip install" in body, "uv 环境下必须走 uv pip install"
    assert "-m pip install" in body, "非 uv 环境仍走 python -m pip"


def test_never_blocks_when_non_interactive(src: str) -> None:
    """非交互（stdin 重定向 / 自动化）时必须直接返回，绝不等输入。"""
    m = re.search(r"function Invoke-FirstRunSetup \{(.*?)\n\}", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "[Console]::IsInputRedirected" in body, "缺非交互守卫"
    assert "$env:BIODATA_SKIP_SETUP" in body, "缺自动化跳过开关"
    guard = body.index("[Console]::IsInputRedirected")
    first_prompt = body.index("Read-Host")
    assert guard < first_prompt, "非交互守卫必须在任何提问之前"


def test_env_file_enables_llm_and_uses_generic_keys(src: str) -> None:
    """写 .env 时必须 ENABLE_LLM=true，且用通用 LLM_* 变量名（与 .env.example 一致）。"""
    m = re.search(r"function Set-LlmEnvFile \{(.*?)\n\}", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "'ENABLE_LLM=true'" in body, "必须写 true——显式 false 会让填了的密钥不生效"
    assert "'ENABLE_LLM=false'" not in body
    for key in ("LLM_PROVIDER=", "LLM_API_KEY=", "LLM_BASE_URL=", "LLM_MODEL="):
        assert key in body, f".env 缺 {key}"
    assert "UTF8Encoding($false)" in body, ".env 不能带 BOM（会污染第一个变量名）"


def test_api_key_is_never_echoed(src: str) -> None:
    """密钥必须用 -AsSecureString 读，且不得被打印。"""
    m = re.search(r"function Read-PlainSecret \{(.*?)\n\}", src, re.DOTALL)
    assert m and "-AsSecureString" in m.group(1), "密钥必须隐藏输入"
    assert "ZeroFreeBSTR" in src, "非托管内存必须释放"
    assert not re.search(r"Write-Host[^\r\n]*\$key", src), "绝不能把密钥打印出来"


def test_launcher_messages_stay_ascii(src: str) -> None:
    """Windows PowerShell 5.1 按 ANSI 读 .ps1，脚本内不得出现非 ASCII（会变乱码）。"""
    bad = [(i + 1, ln) for i, ln in enumerate(src.splitlines())
           if any(ord(ch) > 127 for ch in ln)]
    assert not bad, f"launch_web.ps1 出现非 ASCII 字符（PS 5.1 会显示为乱码）：{bad[:3]}"


def test_agent_exec_dependency_install_handles_uv(src: str) -> None:
    """agent 执行依赖（langgraph）安装必须与主依赖/模型依赖同款 uv 分流，且失败可跳过。"""
    m = re.search(r"function Install-AgentExecDependencies \{(.*?)\n\}\r?\n\r?\nfunction ", src, re.DOTALL)
    assert m, "缺 Install-AgentExecDependencies"
    body = m.group(1)
    assert "Get-Command uv" in body, "agent 依赖安装必须处理 uv 环境"
    assert "uv.Source pip install" in body, "uv 环境下必须走 uv pip install"
    assert "-m pip install" in body, "非 uv 环境仍走 python -m pip"
    assert "requirements-langchain.txt" in body, "必须安装 requirements-langchain.txt"
    assert "falls back to the built-in planner" in body, "安装失败必须如实说明走保底规划器"


def test_agent_exec_step_is_asked_only_when_missing(src: str) -> None:
    """langgraph 已可导入时不得再问（回归者不再被打扰）；问了就必须真装。"""
    m = re.search(r"function Invoke-FirstRunSetup \{(.*?)\n\}", src, re.DOTALL)
    assert m
    body = m.group(1)
    assert "Test-AgentExecDependencies -Python $Python" in body, "向导必须用导入检测判断是否已装"
    assert "$askAgent" in body, "缺 askAgent 分支"
    assert "Install-AgentExecDependencies -Root $Root" in body, "答 y 必须真走安装"
    assert re.search(r"import langgraph, langchain_core, langchain_openai", src), \
        "导入检测必须覆盖 langgraph / langchain_core / langchain_openai 三个包"


def test_expected_version_is_parsed_from_webapp(src: str) -> None:
    """版本号单一真源是 webapp.py 的 WEB_API_VERSION：启动器必须运行时解析它，
    只保留同值字面量作 fallback（后者由 test_release_version_contract 钉住同步）。"""
    assert re.search(r"^\$ExpectedVersion = '[0-9]+\.[0-9]+\.[0-9]+'$", src, re.MULTILINE), \
        "缺 fallback 字面量（契约门要求保留）"
    parse = src.index('WEB_API_VERSION = "([0-9]+')
    assign = src.index("$ExpectedVersion = $versionMatch.Groups[1].Value")
    assert parse < assign, "必须先解析 webapp.py 再覆盖 $ExpectedVersion"
    literal = re.search(r"^\$ExpectedVersion = '[0-9]+\.[0-9]+\.[0-9]+'$", src, re.MULTILINE).start()
    assert literal < parse, "fallback 字面量必须在解析之前声明"


def test_browser_opens_after_health_ready(src: str) -> None:
    """浏览器必须等 /api/health 就绪再开（语义模型预热可达数十秒），且保留超时兜底仍开。"""
    assert "Start-Sleep -Seconds 3; Start-Process" not in src, \
        "固定 3 秒 sleep 开浏览器的旧法必须移除"
    assert "/api/health" in src and "Invoke-RestMethod" in src, "缺 health 轮询"
    poll = src[src.index("$pollCommand"):src.index("$pollCommand") + 400]
    assert "Start-Process '$url'" in poll, "轮询结束后必须真的打开浏览器"


def test_reuse_branch_names_the_reused_install(src: str) -> None:
    """同版本多份安装并存时（开发副本 vs 提交包副本），复用分支必须打印被复用实例的
    install_root，且与本机路径不同时显式提醒——绝不静默吸附（契约additive）。"""
    assert "$script:ReusedInstallRoot = [string]$health.install_root" in src, \
        "Test-ExpectedServer 必须记下 health 返回的 install_root"
    reuse = src.index("already running at $runningUrl")
    note = src.index("DIFFERENT install", reuse)
    assert note > reuse, "复用提示必须出现在复用分支里"
    assert "Reusing this install" in src[reuse:note], "同路径时也要如实说明复用的是本安装"
