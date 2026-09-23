# Windows、Python 与中文路径约定

## 1. 不假设 `py` 存在

Windows 环境可能只有以下一种或几种：

- 项目 `.venv\Scripts\python.exe`；
- `uv` 管理的 Python；
- Python Launcher `py.exe`；
- PATH 中的 `python.exe` / `python3.exe`；
- 某些 CLI 工具自带但未加入 PATH 的运行时。

因此项目指令先解析并复用 `$Python` 绝对路径，而不是硬编码 `py`。

## 2. PowerShell 解析示例

下面只做发现和版本检查，不安装依赖：

```powershell
$ErrorActionPreference = 'Stop'
$Python = $null

function Resolve-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string] $Executable,
        [string[]] $PrefixArgs = @()
    )

    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $null }

    $PreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        $Probe = & $Executable @PrefixArgs -c 'import sys; ok = sys.version_info >= (3, 10); print(sys.executable) if ok else None; raise SystemExit(0 if ok else 9)' 2>&1
        $ExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $PreviousPreference
    }
    if ($ExitCode -ne 0) { return $null }

    $Resolved = [string]($Probe | Select-Object -Last 1)
    if ([string]::IsNullOrWhiteSpace($Resolved)) { return $null }
    return [IO.Path]::GetFullPath($Resolved.Trim())
}

$RepoRoot = git rev-parse --show-toplevel 2>$null
if ($LASTEXITCODE -ne 0) { throw '当前目录不在 Git 仓库中。' }
$RepoRoot = [IO.Path]::GetFullPath(([string]($RepoRoot | Select-Object -Last 1)).Trim())

$PythonHint = $env:BIODATA_PYTHON
if ($PythonHint) {
    $Python = Resolve-PythonCandidate -Executable $PythonHint
}

$ProjectVenv = Join-Path $RepoRoot '.venv\Scripts\python.exe'
if (-not $Python -and (Test-Path -LiteralPath $ProjectVenv)) {
    $Python = Resolve-PythonCandidate -Executable $ProjectVenv
}

if (-not $Python) {
    $Py = Get-Command py -ErrorAction SilentlyContinue
    if ($Py) {
        $Python = Resolve-PythonCandidate -Executable $Py.Source -PrefixArgs @('-3')
    }
}

if (-not $Python) {
    foreach ($Name in @('python3', 'python')) {
        $Command = Get-Command $Name -ErrorAction SilentlyContinue
        if (-not $Command) { continue }
        $Python = Resolve-PythonCandidate -Executable $Command.Source
        if ($Python) { break }
    }
}

if (-not $Python -and $env:BIODATA_ALLOW_UV_DISCOVERY -eq '1') {
    $Uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($Uv) {
        $PreviousPreference = $ErrorActionPreference
        $ErrorActionPreference = 'SilentlyContinue'
        try {
            $UvCandidate = & $Uv.Source python find '>=3.10' 2>&1
            $UvExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $PreviousPreference
        }
        if ($UvExitCode -eq 0 -and $UvCandidate) {
            $UvPath = ([string]($UvCandidate | Select-Object -Last 1)).Trim()
            $Python = Resolve-PythonCandidate -Executable $UvPath
        }
    }
}

if (-not $Python) {
    throw '未找到 Python 3.10+；停止验证并报告环境缺失。'
}

& $Python -c "import sys; assert sys.version_info >= (3,10); print(sys.executable); print(sys.version)"
if ($LASTEXITCODE -ne 0) { throw '最终 Python 校验失败。' }
Write-Host "本任务使用：$Python"
Write-Host "仓库根目录：$RepoRoot"
```

发现解释器后还要检查任务依赖，例如 pytest：

```powershell
& $Python -c "import pytest; print(pytest.__version__)"
```

缺依赖时先报告；安装依赖是写入和可能联网的动作，应遵守用户授权和项目环境策略。

## 3. `uv` 的角色

如果系统只有 `uv`，默认**不自动调用**，因为 [`uv python find`](https://docs.astral.sh/uv/concepts/python-versions/#finding-a-python-executable) 仍可能初始化用户缓存，在严格只读或受限沙箱中失败。确认允许项目外缓存访问后，可仅为当前进程设置 `$env:BIODATA_ALLOW_UV_DISCOVERY = '1'`，再运行发现流程；它只查找已存在的解释器，不调用 `uv run`、`uv sync` 或 `uv python install`。

找到解释器不代表项目依赖已经安装，仍需单独检查。创建环境、下载 Python 或安装包都可能需要联网和写权限；执行前先核对当前 `uv --help`、用户授权和项目约定。

若客户端提供自己的运行时定位能力（例如工作区依赖定位器），先取得绝对路径并设置当前进程的 `$env:BIODATA_PYTHON`，再运行上面的统一验证；不要把客户端私有路径写死进项目文件。

## 4. PowerShell 与中文路径

- Windows 文件操作优先使用 PowerShell `-LiteralPath`，避免通配符和中文路径转义问题。
- 路径始终整体加引号；不要把 PowerShell 枚举出的路径拼进另一个 shell 执行删除/移动。
- JSON 中反斜杠写成 `\\`；TOML 可使用单引号字面量路径。
- 文件统一 UTF-8 无 BOM、LF；读取中文 Markdown 时显式指定 UTF-8。
- 长时间命令使用可轮询的进程/会话，不要用隐藏的后台进程后假定成功。
- stdio MCP 的 stdout 只用于协议；诊断日志写 stderr。

## 5. 只读检查避免缓存

用户明确要求项目只读时：

- Python 使用 `-B` 或设置 `PYTHONDONTWRITEBYTECODE=1`；
- pytest 使用 `-p no:cacheprovider`；
- 不在项目内创建虚拟环境、临时脚本或报告；
- 执行前确认命令不会写数据、日志或缓存。

示例：

```powershell
$env:PYTHONDONTWRITEBYTECODE = '1'
Set-Location -LiteralPath $RepoRoot
& $Python -B -m pytest -p no:cacheprovider tests\test_example.py -q
if ($LASTEXITCODE -ne 0) { throw '只读 pytest 检查失败。' }
```

只读任务通常不需要运行会写入的 `--yes`（清理孤儿任务记录）或上传流程。

## 6. 命令失败语义

- `CommandNotFoundException` 表示命令没有执行，不是测试失败，更不是测试通过。
- 本地 PATH 与另一个客户端的 PATH 可能不同；始终记录实际 `sys.executable`。
- 若文档命令与当前 CLI `--help` 冲突，以当前 CLI 为准，并更新文档或报告差异。
- 不使用 shell 别名作为 MCP command；MCP 配置应指向真实可执行文件绝对路径。
