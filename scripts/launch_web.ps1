[CmdletBinding()]
param(
    # Default resolved in the body: with [CmdletBinding()], $PSScriptRoot is still empty
    # while parameter defaults are evaluated (PS 5.1), so it cannot be used up here.
    [string] $ProjectPath = ''
)

# W1 (installer workstream): this launcher serves SOURCE/PORTABLE mode only. Its
# .env / first-run marker / models detection anchor on $root (the project/install
# root), which is exactly what runtime_paths reports as install_root in those modes,
# so behavior stays byte-identical. FROZEN (packaged exe) startup is taken over by
# the new launcher; this script is not shipped there and must only stay crash-free
# if invoked with a project layout (it is, by construction, since $root is resolved
# relative to $PSScriptRoot).

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($ProjectPath)) {
    $ProjectPath = Split-Path -Parent $PSScriptRoot
}

$ExpectedService = 'dataset-recommender-web'
# Fallback literal only - the real value is parsed from webapp.py just below (single
# source of truth), so a future version bump can never drift the "already running"
# detection. tests/test_release_version_contract.py pins this fallback to WEB_API_VERSION.
$ExpectedVersion = '3.0.0'

try {
    $webappPy = Join-Path (Split-Path -Parent $PSScriptRoot) 'src\dataset_recommender\app\webapp.py'
    $versionMatch = [regex]::Match((Get-Content -LiteralPath $webappPy -Raw -ErrorAction Stop), '(?m)^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"$')
    if ($versionMatch.Success) { $ExpectedVersion = $versionMatch.Groups[1].Value }
} catch { }

function Test-PythonExecutable {
    param([string] $Path)
    if ([string]::IsNullOrWhiteSpace($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    try {
        & $Path -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 9)' 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    catch {
        return $false
    }
}

function Test-WebDependencies {
    param([string] $Python)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python -c 'import fastapi, uvicorn, multipart' 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Resolve-BasePython {
    if (-not [string]::IsNullOrWhiteSpace($env:BIODATA_PYTHON)) {
        $explicit = [IO.Path]::GetFullPath($env:BIODATA_PYTHON)
        if (Test-PythonExecutable $explicit) { return $explicit }
        throw "BIODATA_PYTHON does not point to a usable Python 3.10+: $explicit"
    }

    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $resolved = (& $py.Source -3 -c 'import sys; print(sys.executable)' 2>$null | Select-Object -Last 1)
            if ($resolved -and (Test-PythonExecutable ([string]$resolved).Trim())) {
                return ([string]$resolved).Trim()
            }
        }
        catch {}
    }

    foreach ($name in @('python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command -and (Test-PythonExecutable $command.Source)) { return $command.Source }
    }

    $patterns = @(
        (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-*\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python*\python.exe'),
        (Join-Path $env:ProgramFiles 'Python*\python.exe')
    )
    foreach ($pattern in $patterns) {
        foreach ($candidate in @(Get-Item -Path $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            if (Test-PythonExecutable $candidate.FullName) { return $candidate.FullName }
        }
    }
    return $null
}

function Test-PortOpen {
    param([int] $Port)
    $client = New-Object Net.Sockets.TcpClient
    try {
        $result = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $result.AsyncWaitHandle.WaitOne(400)) { return $false }
        $client.EndConnect($result)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Close()
    }
}

function Test-ExpectedServer {
    param([int] $Port)
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/api/health" -TimeoutSec 2
        if ($health.ok -eq $true -and $health.service -eq $ExpectedService -and $health.version -eq $ExpectedVersion) {
            # Remember which install answered, so the reuse branch can say it out loud:
            # two same-version installs (dev copy vs submission package) must never merge silently.
            $script:ReusedInstallRoot = [string]$health.install_root
            return $true
        }
        return $false
    }
    catch {
        return $false
    }
}

function Resolve-LaunchPort {
    param([int] $PreferredPort)
    if (-not (Test-PortOpen $PreferredPort)) { return $PreferredPort }
    if (Test-ExpectedServer $PreferredPort) { return -$PreferredPort }

    foreach ($candidate in (($PreferredPort + 1)..($PreferredPort + 9))) {
        if (-not (Test-PortOpen $candidate)) { return $candidate }
    }
    throw "Ports $PreferredPort-$($PreferredPort + 9) are all occupied. Close an old BioData Agent window and try again."
}

# ---------------------------------------------------------------------------
# First-run setup (optional, asked once). Everything here is skippable and
# failure-tolerant: the server must still start even if these steps are
# declined or fail. Messages stay ASCII on purpose -- Windows PowerShell 5.1
# reads .ps1 as ANSI, so CJK literals here would render as mojibake.
# ---------------------------------------------------------------------------

$Script:LlmPresets = @(
    @{ n = '1'; label = 'DeepSeek';   wire = 'openai-compatible'; base = 'https://api.deepseek.com';                           model = 'deepseek-chat' },
    @{ n = '2'; label = 'Kimi';       wire = 'openai-compatible'; base = 'https://api.moonshot.cn/v1';                         model = 'kimi-k2.6' },
    @{ n = '3'; label = 'Qwen';       wire = 'openai-compatible'; base = 'https://dashscope.aliyuncs.com/compatible-mode/v1';  model = 'qwen-plus' },
    @{ n = '4'; label = 'GLM (Zhipu)'; wire = 'zhipuai';          base = 'https://open.bigmodel.cn/api/paas/v4/';              model = 'glm-5.1' },
    @{ n = '5'; label = 'OpenRouter'; wire = 'openai-compatible'; base = 'https://openrouter.ai/api/v1';                       model = 'openrouter/auto' },
    @{ n = '6'; label = 'OpenAI';     wire = 'openai-compatible'; base = 'https://api.openai.com/v1';                          model = 'gpt-4o-mini' },
    @{ n = '7'; label = 'Other OpenAI-compatible endpoint'; wire = 'openai-compatible'; base = ''; model = '' },
    @{ n = '8'; label = 'Local model (Ollama)'; wire = 'openai-compatible'; base = 'http://localhost:11434/v1'; model = 'llama3.1' }
)

function Read-PlainSecret {
    param([string] $Prompt)
    $secure = Read-Host $Prompt -AsSecureString
    if (-not $secure -or $secure.Length -eq 0) { return '' }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Set-LlmEnvFile {
    param([string] $EnvFile)

    Write-Host ''
    foreach ($p in $Script:LlmPresets) { Write-Host ("    {0}) {1}" -f $p.n, $p.label) }
    $choice = ([string](Read-Host 'Provider number (Enter to cancel)')).Trim()
    $sel = $Script:LlmPresets | Where-Object { $_.n -eq $choice } | Select-Object -First 1
    if (-not $sel) { Write-Host '    Cancelled - no AI configured.'; return }

    $base = $sel.base
    $model = $sel.model
    if ([string]::IsNullOrWhiteSpace($base)) { $base = ([string](Read-Host 'Base URL')).Trim() }
    if ([string]::IsNullOrWhiteSpace($model)) { $model = ([string](Read-Host 'Model name')).Trim() }
    if ([string]::IsNullOrWhiteSpace($base) -or [string]::IsNullOrWhiteSpace($model)) {
        Write-Host '    Base URL or model missing - no AI configured.'
        return
    }

    $key = Read-PlainSecret 'API Key (input stays hidden)'
    if ([string]::IsNullOrWhiteSpace($key)) { Write-Host '    Empty key - no AI configured.'; return }

    # ENABLE_LLM must be true: an explicit false always overrides "key present -> on".
    $lines = @(
        '# Written by the BioData Agent first-run setup.',
        '# Delete this file to go back to the key-free, fully offline setup.',
        'DATA_DIR=database/base',
        'TOP_K=10',
        'ENABLE_LLM=true',
        'MOCK_LLM=false',
        ('LLM_PROVIDER=' + $sel.wire),
        ('LLM_API_KEY=' + $key),
        ('LLM_BASE_URL=' + $base),
        ('LLM_MODEL=' + $model),
        'LLM_TIMEOUT=60',
        'LLM_TEMPERATURE=0.2',
        'LLM_MAX_TOKENS=8000'
    )
    [IO.File]::WriteAllLines($EnvFile, $lines, (New-Object Text.UTF8Encoding($false)))
    Write-Host ("    Saved to .env (provider: {0}, model: {1}). The key is never printed." -f $sel.label, $model)
}

# Directory presence is NOT proof of a model: fetch_embedding_model.py creates the
# target directory before it starts downloading, so a failed download leaves an empty
# folder behind. Require at least one real file.
function Test-LocalModelPresent {
    param([string] $Root)
    $dir = Join-Path $Root 'models\cross_encoders'
    if (-not (Test-Path -LiteralPath $dir)) { return $false }
    $first = Get-ChildItem -LiteralPath $dir -Recurse -File -ErrorAction SilentlyContinue | Select-Object -First 1
    return [bool]$first
}

function Install-LocalSemanticModel {
    param([string] $Root, [string] $Python)

    $req = Join-Path $Root 'requirements-embeddings.txt'
    if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
        # Repo clone layout (2026-08-27): requirements files live under requirements\.
        $req = Join-Path $Root 'requirements\requirements-embeddings.txt'
    }
    $fetch = Join-Path $Root 'scripts\fetch_embedding_model.py'
    if (-not (Test-Path -LiteralPath $req -PathType Leaf) -or -not (Test-Path -LiteralPath $fetch -PathType Leaf)) {
        Write-Host '    Model installer is not part of this package - skipped.'
        return
    }
    Write-Host '    Installing model dependencies (several minutes)...'
    # Mirror the main dependency path: a uv-created .venv has no pip of its own.
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { & $uv.Source pip install --python $Python -r $req }
    else { & $Python -m pip install --disable-pip-version-check -r $req }
    if ($LASTEXITCODE -ne 0) { Write-Host '    Dependency install failed - skipped. The app still works.'; return }

    Write-Host '    Downloading model weights (about 2.2 GB, ModelScope first)...'
    & $Python $fetch
    if ($LASTEXITCODE -ne 0) { Write-Host '    Model download failed - skipped. The app still works.'; return }
    if (-not (Test-LocalModelPresent -Root $Root)) {
        Write-Host '    Download reported success but no weights are on disk - skipped.'
        return
    }
    Write-Host '    Local semantic model is ready.'
}

function Test-AgentExecDependencies {
    param([string] $Python)
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Python -c 'import langgraph, langchain_core, langchain_openai' 2>$null
        return ($LASTEXITCODE -eq 0)
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Install-AgentExecDependencies {
    param([string] $Root, [string] $Python)

    $req = Join-Path $Root 'requirements-langchain.txt'
    if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
        # Repo clone layout (2026-08-27): requirements files live under requirements\.
        $req = Join-Path $Root 'requirements\requirements-langchain.txt'
    }
    if (-not (Test-Path -LiteralPath $req -PathType Leaf)) {
        Write-Host '    requirements-langchain.txt is not part of this package - skipped.'
        return
    }
    Write-Host '    Installing agent execution dependencies (langgraph, usually under a minute)...'
    # Mirror the main dependency path: a uv-created .venv has no pip of its own.
    $uv = Get-Command uv -ErrorAction SilentlyContinue
    if ($uv) { & $uv.Source pip install --python $Python -r $req }
    else { & $Python -m pip install --disable-pip-version-check -r $req }
    if ($LASTEXITCODE -ne 0) {
        Write-Host '    Install failed - skipped. AI execution falls back to the built-in planner.'
        return
    }
    if (Test-AgentExecDependencies -Python $Python) {
        Write-Host '    Agent execution (langgraph) is ready.'
    }
    else {
        Write-Host '    Import check failed after install - AI execution falls back to the built-in planner.'
    }
}

function Invoke-FirstRunSetup {
    param([string] $Root, [string] $Python)

    # Marker lives in the project root, NOT in .venv: the venv may live elsewhere
    # entirely (BIODATA_PYTHON, a workspace venv, a shared venv), and writing into a
    # directory that does not exist would throw and leave the wizard asking forever.
    $marker = Join-Path $Root '.biodata-setup-done'
    if (Test-Path -LiteralPath $marker) { return }          # asked once already
    if ($env:BIODATA_SKIP_SETUP -eq '1') { return }         # escape hatch for automation
    if ([Console]::IsInputRedirected) { return }            # non-interactive: never block

    $envFile = Join-Path $Root '.env'
    $askAi = -not (Test-Path -LiteralPath $envFile)
    $askModel = -not (Test-LocalModelPresent -Root $Root)
    $askAgent = -not (Test-AgentExecDependencies -Python $Python)

    # Nothing to ask: record that and stay silent, so returning users never see a
    # "First-run setup" banner followed by no questions.
    if ($askAi -or $askModel -or $askAgent) {
        Write-Host ''
        Write-Host '=== First-run setup - all steps are optional, Enter skips ==='

        if ($askAi) {
            Write-Host ''
            Write-Host '[AI] Searching works fully offline and needs no API key.'
            Write-Host '     A key is only needed for AI re-ranking / polished wording / Chinese overview.'
            Write-Host '     Note: this configures the SERVER. In the web UI you still pick the same'
            Write-Host '     provider under Settings -> AI / API and switch the AI toggle on.'
            if ((([string](Read-Host 'Configure an AI API key now? [y/N]')).Trim()) -match '^[yY]') {
                Set-LlmEnvFile -EnvFile $envFile
            }
            else {
                Write-Host '    Skipped. You can add it any time in the web UI: Settings -> AI / API.'
            }
        }

        if ($askAgent) {
            Write-Host ''
            Write-Host '[Agent] Optional langgraph dependencies for the multi-step AI executor.'
            Write-Host '        Without them AI execution falls back to the built-in single-step planner.'
            if ((([string](Read-Host 'Install agent execution dependencies now? [y/N]')).Trim()) -match '^[yY]') {
                Install-AgentExecDependencies -Root $Root -Python $Python
            }
            else {
                Write-Host '    Skipped. You can install them later: pip install -r requirements-langchain.txt'
            }
        }

        if ($askModel) {
            Write-Host ''
            Write-Host '[Model] Optional local semantic re-ranking model (about 3 GB download, ~5 GB on disk, slow).'
            Write-Host '        Without it the app falls back to rule-based ordering and stays fully usable.'
            if ((([string](Read-Host 'Download the local model now? [y/N]')).Trim()) -match '^[yY]') {
                Install-LocalSemanticModel -Root $Root -Python $Python
            }
            else {
                Write-Host '    Skipped. You can install it later - see the manual, section 10.4.'
            }
        }

        Write-Host ''
        Write-Host '=== Setup finished ==='
    }

    # Best effort only: if the project folder is read-only we simply ask again next time.
    try { Set-Content -LiteralPath $marker -Value '' -Encoding ascii -ErrorAction Stop } catch { }
}

try {
    $root = (Resolve-Path -LiteralPath $ProjectPath).Path
    $runner = Join-Path $root 'scripts\run_web.py'
    $requirements = Join-Path $root 'requirements.txt'
    if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
        # Repo clone layout (2026-08-27): requirements files live under requirements\.
        $requirements = Join-Path $root 'requirements\requirements.txt'
    }
    if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) {
        throw "Project files are incomplete: scripts\run_web.py was not found under $root"
    }

    $preferredPort = 7860
    if (-not [string]::IsNullOrWhiteSpace($env:PORT)) {
        $parsedPort = 0
        if (-not [int]::TryParse($env:PORT, [ref]$parsedPort) -or $parsedPort -lt 1 -or $parsedPort -gt 65526) {
            throw 'PORT must be an integer from 1 to 65526.'
        }
        $preferredPort = $parsedPort
    }

    $portDecision = Resolve-LaunchPort $preferredPort
    if ($portDecision -lt 0) {
        $runningPort = -$portDecision
        $runningUrl = "http://127.0.0.1:$runningPort"
        Write-Host "The current BioData Agent is already running at $runningUrl"
        if (-not [string]::IsNullOrWhiteSpace($script:ReusedInstallRoot)) {
            $reused = $script:ReusedInstallRoot.TrimEnd('\')
            $here = $root.TrimEnd('\')
            if ($reused -ieq $here) {
                Write-Host "Reusing this install: $reused"
            }
            else {
                Write-Host "NOTE: reusing the instance served from a DIFFERENT install:"
                Write-Host "  running:  $reused"
                Write-Host "  this one: $here"
                Write-Host 'Close the running one first if you meant to start this copy.'
            }
        }
        if ($env:BIODATA_NO_BROWSER -ne '1') { Start-Process $runningUrl }
        exit 0
    }
    $port = $portDecision
    if ($port -ne $preferredPort) {
        Write-Host "Port $preferredPort is occupied by an older/different service; using port $port for the current build."
    }
    $url = "http://127.0.0.1:$port"

    # Python environment selection order (first-run docs must describe the same
    # behavior): 1) $env:BIODATA_PYTHON (explicit override, validated)  2) project-local
    # .venv\Scripts\python.exe  3) create a fresh project-local .venv. Do not silently
    # reuse a workspace/MCP environment: that leaks dev-only packages into production
    # and lets unrelated upgrades change this installation. Advanced users may still
    # opt in to a shared interpreter explicitly through BIODATA_PYTHON.
    $venvPython = Join-Path $root '.venv\Scripts\python.exe'
    $python = $null
    if (-not [string]::IsNullOrWhiteSpace($env:BIODATA_PYTHON)) {
        $python = Resolve-BasePython
    }
    elseif (Test-PythonExecutable $venvPython) {
        $python = $venvPython
    }
    else {
        $basePython = Resolve-BasePython
        if ($basePython) {
            Write-Host '[1/3] Creating the project-local Python environment (.venv)...'
            & $basePython -m venv (Join-Path $root '.venv')
        }
        else {
            $uv = Get-Command uv -ErrorAction SilentlyContinue
            if (-not $uv) {
                throw 'Python 3.10+ was not found. Install Python or uv, then double-click this shortcut again.'
            }
            Write-Host '[1/3] Creating the project-local Python environment with uv (.venv)...'
            & $uv.Source venv (Join-Path $root '.venv') --python 3.12
        }
        if ($LASTEXITCODE -ne 0 -or -not (Test-PythonExecutable $venvPython)) {
            throw 'Could not create the project-local Python environment.'
        }
        $python = $venvPython
    }

    if (-not (Test-WebDependencies $python)) {
        if (-not (Test-Path -LiteralPath $requirements -PathType Leaf)) {
            throw 'requirements.txt is missing; dependencies cannot be installed.'
        }
        Write-Host '[2/3] Installing required packages (first launch only)...'
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source pip install --python $python -r $requirements
        }
        else {
            & $python -m pip install --disable-pip-version-check -r $requirements
        }
        if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check the network and try again.' }
    }

    if (-not (Test-WebDependencies $python)) {
        throw 'Required packages are still unavailable after installation.'
    }

    # CI/release smoke hook: a freshly extracted source zip must create its own runtime
    # venv, import every web dependency, and keep pytest out of that environment.
    # This exits before the interactive wizard, browser, or server; normal users never set it.
    if ($env:BIODATA_LAUNCH_PROBE -eq '1') {
        $actualPython = [IO.Path]::GetFullPath($python)
        $expectedPython = [IO.Path]::GetFullPath($venvPython)
        if ($actualPython -ine $expectedPython) {
            throw "Launch probe did not use the project-local venv: $actualPython"
        }
        $previousPreference = $ErrorActionPreference
        try {
            # A missing module is the expected success condition here. PowerShell may
            # promote native stderr to an ErrorRecord under Stop, so probe under Continue.
            $ErrorActionPreference = 'Continue'
            & $python -c 'import pytest' 2>$null
            $pytestPresent = ($LASTEXITCODE -eq 0)
        }
        finally {
            $ErrorActionPreference = $previousPreference
        }
        if ($pytestPresent) {
            throw 'Launch probe found dev-only pytest in the runtime venv.'
        }
        Write-Host "BIODATA_LAUNCH_PROBE_OK python=$actualPython pytest=absent"
        exit 0
    }

    # Optional one-time deployment steps. Any error inside the wizard is swallowed so
    # the server still starts. (Ctrl-C at a prompt is NOT catchable in PowerShell and
    # will end the script - that is the user cancelling on purpose, not a failure path.)
    try { Invoke-FirstRunSetup -Root $root -Python $python }
    catch { Write-Host "[setup] Skipped ($($_.Exception.Message))." }

    Write-Host '[3/3] Starting the current BioData Agent build...'
    Write-Host "Project: $root"
    Write-Host "URL:     $url"
    Write-Host 'Close this window to stop the server.'
    if ($env:BIODATA_NO_BROWSER -ne '1') {
        # Open the browser only once /api/health answers: model warm-up can take tens of
        # seconds, and the old fixed 3s sleep opened an error page on slow first starts.
        # After ~90s it opens anyway, so a broken health route can never hide the UI.
        $pollCommand = "for (`$i = 0; `$i -lt 90; `$i++) { try { `$h = Invoke-RestMethod -Uri '$url/api/health' -TimeoutSec 2; if (`$h.ok -eq `$true) { break } } catch { }; Start-Sleep -Seconds 1 }; Start-Process '$url'"
        Start-Process powershell.exe -WindowStyle Hidden -ArgumentList @('-NoProfile', '-Command', $pollCommand)
    }

    $env:PYTHONIOENCODING = 'utf-8'
    $env:PORT = [string]$port
    Set-Location -LiteralPath $root
    & $python $runner
    exit $LASTEXITCODE
}
catch {
    Write-Host ''
    Write-Host "[ERROR] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
}
