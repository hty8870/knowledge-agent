[CmdletBinding()]
param(
    [ValidateSet('codex', 'claude')]
    [string] $Client,

    [ValidateSet('none', 'openai', 'zhipuai', 'compatible')]
    [string] $Provider,

    [string] $BaseUrl,
    [string] $Model,

    [ValidateSet('user', 'local', 'project')]
    [string] $ClaudeScope = 'user',

    [string] $ProjectPath = (Split-Path -Parent $PSScriptRoot),
    [string] $PythonPath,
    [string] $SecretsPath = (Join-Path $env:LOCALAPPDATA 'BioDataAgent\secrets\llm.env'),

    [switch] $SkipInstall,
    [switch] $SkipLlmCheck,
    [switch] $RequireLlm,
    [int] $KeepSecretBackups = 3,
    [switch] $Force,
    [switch] $PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Select-RequiredValue {
    param(
        [Parameter(Mandatory = $true)][string] $Label,
        [Parameter(Mandatory = $true)][hashtable] $Choices
    )
    Write-Host $Label
    foreach ($key in ($Choices.Keys | Sort-Object)) {
        Write-Host "  $key. $($Choices[$key])"
    }
    while ($true) {
        $answer = (Read-Host 'Enter a number').Trim()
        if ($Choices.ContainsKey($answer)) { return $Choices[$answer] }
        Write-Warning 'Invalid selection. Try again.'
    }
}

function Resolve-AbsolutePath {
    param([Parameter(Mandatory = $true)][string] $Path, [switch] $AllowMissing)
    if ($AllowMissing) {
        return [IO.Path]::GetFullPath($Path)
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

function Assert-SafeValue {
    param([Parameter(Mandatory = $true)][string] $Name, [Parameter(Mandatory = $true)][string] $Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Name must not be empty." }
    if ($Value.Contains("`r") -or $Value.Contains("`n") -or $Value.Contains([char]0)) {
        throw "$Name contains a newline or NUL and cannot be stored safely."
    }
}

function Get-ProviderDefaults {
    param([Parameter(Mandatory = $true)][string] $Name)
    switch ($Name) {
        'openai' {
            return @{ Provider = 'openai-compatible'; BaseUrl = 'https://api.openai.com/v1'; Model = 'gpt-4o-mini' }
        }
        'zhipuai' {
            return @{ Provider = 'zhipuai'; BaseUrl = 'https://open.bigmodel.cn/api/paas/v4/'; Model = 'glm-5.1' }
        }
        'compatible' {
            return @{ Provider = 'openai-compatible'; BaseUrl = ''; Model = '' }
        }
        default {
            return @{ Provider = 'none'; BaseUrl = ''; Model = '' }
        }
    }
}

function Read-SecretPlainText {
    $secure = Read-Host 'Enter the API key (input is hidden)' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    Assert-SafeValue -Name 'API Key' -Value $plain
    return $plain
}

function Backup-File {
    param([Parameter(Mandatory = $true)][string] $Path, [Parameter(Mandatory = $true)][string] $Stamp)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $backup = "$Path.bak.$Stamp"
    Copy-Item -LiteralPath $Path -Destination $backup -Force
    return $backup
}

function Remove-OldBackups {
    # Keep only the most recent $Keep <Path>.bak.* backups; delete the rest so stale plaintext-key
    # history stops piling up (ACL-restricted, but still worth pruning). $Keep<=0 removes all.
    # Called only on the success path; the failure path leaves backups intact for rollback.
    param([Parameter(Mandatory = $true)][string] $Path, [int] $Keep = 3)
    $dir = Split-Path -Parent $Path
    $leaf = Split-Path -Leaf $Path
    if (-not (Test-Path -LiteralPath $dir)) { return }
    $backups = @(Get-ChildItem -LiteralPath $dir -Filter "$leaf.bak.*" -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending)
    if ($backups.Count -le [Math]::Max(0, $Keep)) { return }
    $backups | Select-Object -Skip ([Math]::Max(0, $Keep)) | ForEach-Object {
        try { Remove-Item -LiteralPath $_.FullName -Force } catch { }
    }
}

function Protect-SecretFile {
    param([Parameter(Mandatory = $true)][string] $Path)
    if ($env:OS -ne 'Windows_NT') { return }
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $Path /inheritance:r /grant:r "${identity}:F" | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to restrict the secret file ACL: $Path" }
}

function Resolve-BasePython {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $candidate = (& $py.Source -3 -c 'import sys; print(sys.executable)' | Select-Object -Last 1)
        if ($LASTEXITCODE -eq 0 -and $candidate) { return ([string]$candidate).Trim() }
    }
    foreach ($name in @('python3', 'python')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        & $command.Source -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 9)'
        if ($LASTEXITCODE -eq 0) { return $command.Source }
    }
    return $null
}

function Ensure-McpPython {
    param([Parameter(Mandatory = $true)][string] $TargetPython, [switch] $Skip)
    if ($Skip) {
        if (-not (Test-Path -LiteralPath $TargetPython -PathType Leaf)) {
            throw "-SkipInstall was set but Python does not exist: $TargetPython"
        }
    }
    elseif (-not (Test-Path -LiteralPath $TargetPython -PathType Leaf)) {
        $venv = Split-Path -Parent (Split-Path -Parent $TargetPython)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $venv) | Out-Null
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source venv --no-project --python 3.12 $venv
            if ($LASTEXITCODE -ne 0) { throw 'uv failed to create the MCP virtual environment.' }
        }
        else {
            $base = Resolve-BasePython
            if (-not $base) { throw 'Neither uv nor Python 3.10+ was found. Client config was not changed.' }
            & $base -m venv $venv
            if ($LASTEXITCODE -ne 0) { throw 'Python failed to create the MCP virtual environment.' }
        }
    }

    & $TargetPython -c 'import sys; assert sys.version_info >= (3,10)'
    if ($LASTEXITCODE -ne 0) { throw 'MCP Python must be version 3.10 or newer.' }

    if (-not $Skip) {
        $uv = Get-Command uv -ErrorAction SilentlyContinue
        if ($uv) {
            & $uv.Source pip install --python $TargetPython 'mcp==1.28.1'
        }
        else {
            & $TargetPython -m pip install 'mcp==1.28.1'
        }
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install mcp==1.28.1.' }
    }

    & $TargetPython -c "import importlib.metadata as m; from mcp.server.fastmcp import FastMCP; print('Python/MCP ready:', m.version('mcp'))"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to import the MCP SDK.' }
}

function Get-ClientConfigPath {
    param([Parameter(Mandatory = $true)][string] $ClientName)
    if ($ClientName -eq 'codex') {
        $codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME '.codex' }
        return Join-Path $codexHome 'config.toml'
    }
    if ($ClaudeScope -eq 'project') { return Join-Path $ProjectPath '.mcp.json' }
    return Join-Path $HOME '.claude.json'
}

function Test-McpExists {
    param([Parameter(Mandatory = $true)][string] $Executable, [Parameter(Mandatory = $true)][string] $ClientName)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    try {
        if ($ClientName -eq 'codex') {
            & $Executable mcp get biodata --json *> $null
        }
        else {
            & $Executable mcp get biodata *> $null
        }
        return $LASTEXITCODE -eq 0
    }
    finally {
        $ErrorActionPreference = $old
    }
}

function Remove-McpEntry {
    param([Parameter(Mandatory = $true)][string] $Executable, [Parameter(Mandatory = $true)][string] $ClientName)
    if ($ClientName -eq 'codex') {
        & $Executable mcp remove biodata | Out-Null
    }
    else {
        & $Executable mcp remove --scope $ClaudeScope biodata | Out-Null
    }
    if ($LASTEXITCODE -ne 0) { throw "Failed to remove the current $ClientName/biodata entry." }
}

function Add-McpEntry {
    param(
        [Parameter(Mandatory = $true)][string] $Executable,
        [Parameter(Mandatory = $true)][string] $ClientName,
        [Parameter(Mandatory = $true)][string] $Python,
        [Parameter(Mandatory = $true)][string] $Server,
        [string] $EnvFile
    )
    $envPairs = @('PYTHONDONTWRITEBYTECODE=1', 'PYTHONUTF8=1')
    if ($EnvFile) { $envPairs += "BIODATA_LLM_ENV_FILE=$EnvFile" }

    if ($ClientName -eq 'codex') {
        $args = @('mcp', 'add', 'biodata')
        foreach ($pair in $envPairs) { $args += @('--env', $pair) }
        $args += @('--', $Python, $Server)
    }
    else {
        # Claude's --env is variadic; --transport between env values and name prevents the name
        # from being parsed as another KEY=value item (documented behavior in Claude Code 2.1.x).
        $args = @('mcp', 'add', '--env') + $envPairs + @(
            '--transport', 'stdio', '--scope', $ClaudeScope, 'biodata', '--', $Python, $Server
        )
    }
    & $Executable @args | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to register $ClientName/biodata." }
}

if (-not $Client) {
    $Client = Select-RequiredValue -Label 'Choose a client:' -Choices @{ '1' = 'codex'; '2' = 'claude' }
}
if (-not $Provider) {
    $Provider = Select-RequiredValue -Label 'Choose an LLM API (none keeps the default offline mode):' -Choices @{
        '1' = 'none'; '2' = 'openai'; '3' = 'zhipuai'; '4' = 'compatible'
    }
}

$ProjectPath = Resolve-AbsolutePath -Path $ProjectPath
# Delivery packages keep mcp_server.py at the project root; in the Git repo clone it
# lives under src\dataset_recommender\app\ since the 2026-08-27 top-level cleanup.
$server = Join-Path $ProjectPath 'mcp_server.py'
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) {
    $repoLayout = Join-Path $ProjectPath 'src\dataset_recommender\app\mcp_server.py'
    if (Test-Path -LiteralPath $repoLayout -PathType Leaf) { $server = $repoLayout }
}
if (-not (Test-Path -LiteralPath $server -PathType Leaf)) { throw "Server not found: $server" }
$server = Resolve-AbsolutePath -Path $server

if (-not $PythonPath) {
    $PythonPath = Join-Path $env:LOCALAPPDATA 'BioDataAgent\mcp-venv\Scripts\python.exe'
}
$PythonPath = Resolve-AbsolutePath -Path $PythonPath -AllowMissing
$SecretsPath = Resolve-AbsolutePath -Path $SecretsPath -AllowMissing

$defaults = Get-ProviderDefaults -Name $Provider
if (-not $BaseUrl) { $BaseUrl = $defaults.BaseUrl }
if (-not $Model) { $Model = $defaults.Model }
if ($Provider -eq 'compatible' -and -not $BaseUrl -and -not $PlanOnly) { $BaseUrl = Read-Host 'Enter the OpenAI-compatible Base URL' }
if ($Provider -eq 'compatible' -and -not $Model -and -not $PlanOnly) { $Model = Read-Host 'Enter the model name' }

if ($Provider -ne 'none' -and -not $PlanOnly) {
    Assert-SafeValue -Name 'Base URL' -Value $BaseUrl
    Assert-SafeValue -Name 'Model' -Value $Model
    $uri = $null
    if (-not [Uri]::TryCreate($BaseUrl, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('http', 'https')) {
        throw 'Base URL must be an absolute http/https URL.'
    }
    if ($uri.UserInfo) { throw 'Base URL must not contain embedded credentials.' }
}

$configPath = Get-ClientConfigPath -ClientName $Client
$plan = [ordered]@{
    client = $Client
    provider = $defaults.Provider
    project = $ProjectPath
    server = $server
    python = $PythonPath
    secrets_file = if ($Provider -eq 'none') { $null } else { $SecretsPath }
    client_config = $configPath
    claude_scope = if ($Client -eq 'claude') { $ClaudeScope } else { $null }
    live_llm_check = ($Provider -ne 'none' -and -not $SkipLlmCheck)
}
if ($PlanOnly) {
    $plan | ConvertTo-Json -Depth 4
    exit 0
}

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$clientBackup = $null
$secretBackup = $null
$secretCreated = $false
$clientWasPresent = $false
$clientMutated = $false
$clientExe = $null
$plainKey = $null
$llmCheckFailed = $false

try {
    Ensure-McpPython -TargetPython $PythonPath -Skip:$SkipInstall

    & $PythonPath $server --selfcheck
    if ($LASTEXITCODE -ne 0) { throw 'The real MCP protocol self-check failed. Client config was not changed.' }

    if ($Provider -ne 'none') {
        $plainKey = Read-SecretPlainText
        $secretBackup = Backup-File -Path $SecretsPath -Stamp $stamp
        if ($secretBackup) { Protect-SecretFile -Path $secretBackup }
        $secretCreated = -not (Test-Path -LiteralPath $SecretsPath -PathType Leaf)
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $SecretsPath) | Out-Null
        $utf8NoBom = New-Object Text.UTF8Encoding($false)
        # Create (or truncate) the file and restrict its ACL FIRST, then write the plaintext, so the
        # secret only ever lands in an already-locked-down file. This avoids the old order's brief
        # inherited-ACL window (WriteAllText content first, Protect afterwards).
        [IO.File]::WriteAllText($SecretsPath, '', $utf8NoBom)
        Protect-SecretFile -Path $SecretsPath
        $lines = @(
            '# BioData Agent MCP LLM configuration (managed by scripts/setup_mcp.ps1)',
            'ENABLE_LLM=true',
            'MOCK_LLM=false',
            "LLM_PROVIDER=$($defaults.Provider)",
            "LLM_API_KEY=$plainKey",
            "LLM_BASE_URL=$BaseUrl",
            "LLM_MODEL=$Model",
            'LLM_TIMEOUT=60',
            ''
        )
        [IO.File]::WriteAllText($SecretsPath, ($lines -join "`n"), $utf8NoBom)  # writes into the locked file; truncation preserves the ACL
        Protect-SecretFile -Path $SecretsPath                                   # defensive re-assert
        $plainKey = $null
        $lines = $null

        if (-not $SkipLlmCheck) {
            $previousEnvFile = $env:BIODATA_LLM_ENV_FILE
            $llmCheckOk = $false
            try {
                $env:BIODATA_LLM_ENV_FILE = $SecretsPath
                & $PythonPath $server --llm-check
                $llmCheckOk = ($LASTEXITCODE -eq 0)
            }
            finally {
                $env:BIODATA_LLM_ENV_FILE = $previousEnvFile
            }
            if (-not $llmCheckOk) {
                if ($RequireLlm) {
                    throw 'The live LLM API check failed and -RequireLlm was set. Client config was not changed.'
                }
                # Matches the server's "never crash, degrade" philosophy: a failed probe (often a flaky
                # proxy, quota, or cert issue) is downgraded to a warning; still register the OFFLINE-capable
                # server (deterministic retrieval needs no LLM; LLM is opt-in per call). The key is stored
                # safely; rerun --llm-check later. Pass -RequireLlm to make a failed probe fatal (old behavior).
                $llmCheckFailed = $true
                Write-Warning 'Live LLM API check failed (proxy/network/key/quota). Registering the OFFLINE-capable server anyway; deterministic retrieval works without LLM. Rerun the server with --llm-check later, or pass -RequireLlm to make this fatal.'
            }
        }
    }

    $clientCommand = Get-Command $Client -ErrorAction SilentlyContinue
    if (-not $clientCommand) { throw "$Client CLI was not found. Client config was not changed." }
    $clientExe = $clientCommand.Source
    $clientWasPresent = Test-McpExists -Executable $clientExe -ClientName $Client
    if ($clientWasPresent -and -not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
        throw "The existing biodata entry cannot be backed up at the expected path: $configPath"
    }
    if ($clientWasPresent -and -not $Force) {
        $answer = (Read-Host "$Client already has a biodata entry. A backup is ready. Replace it? [y/N]").Trim().ToLowerInvariant()
        if ($answer -notin @('y', 'yes')) { throw 'The existing biodata entry was not replaced.' }
    }

    $clientBackup = Backup-File -Path $configPath -Stamp $stamp
    if ($clientWasPresent) {
        Remove-McpEntry -Executable $clientExe -ClientName $Client
        $clientMutated = $true
    }
    $envFileForClient = if ($Provider -eq 'none') { $null } else { $SecretsPath }
    Add-McpEntry -Executable $clientExe -ClientName $Client -Python $PythonPath -Server $server -EnvFile $envFileForClient
    $clientMutated = $true

    if (-not (Test-McpExists -Executable $clientExe -ClientName $Client)) {
        throw 'The client could not read the newly registered entry.'
    }

    Write-Host ''
    Write-Host 'Setup complete. Restart the client, then call:'
    Write-Host '  1) biodata_status'
    Write-Host '  2) biodata_llm_status(check_connection=false)'
    if ($Provider -ne 'none') {
        Write-Host '  3) biodata_llm_status(check_connection=true)'
        Write-Host '  4) recommend_datasets(query="human lung cancer single-cell data with FASTQ", top_k=1, rerank="llm")'
    }
    Write-Host "Server: $server"
    Write-Host "Python: $PythonPath"
    if ($clientBackup) { Write-Host "Client config backup: $clientBackup" }
    if ($secretBackup) { Write-Host "Previous secret file backup: $secretBackup" }
    if ($llmCheckFailed) {
        Write-Warning 'NOTE: the offline server is registered, but the live LLM API check did NOT pass. Fix the network/key, then call biodata_llm_status(check_connection=true) to confirm before relying on LLM features.'
    }
    Write-Host 'The API key was not written to the project or client config and was not printed.'
    # Success cleanup: prune old secret backups, keeping the most recent N (default 3; -KeepSecretBackups 0 clears all).
    if ($Provider -ne 'none') { Remove-OldBackups -Path $SecretsPath -Keep $KeepSecretBackups }
}
catch {
    $failure = $_
    $plainKey = $null
    Write-Warning "Setup failed. Restoring the previous state: $($failure.Exception.Message)"

    if ($clientMutated -and $clientExe) {
        if ($clientBackup -and (Test-Path -LiteralPath $clientBackup -PathType Leaf)) {
            Copy-Item -LiteralPath $clientBackup -Destination $configPath -Force
        }
        elseif (-not $clientWasPresent) {
            try { Remove-McpEntry -Executable $clientExe -ClientName $Client } catch { }
        }
    }

    if ($Provider -ne 'none') {
        if ($secretBackup -and (Test-Path -LiteralPath $secretBackup -PathType Leaf)) {
            Copy-Item -LiteralPath $secretBackup -Destination $SecretsPath -Force
        }
        elseif ($secretCreated -and (Test-Path -LiteralPath $SecretsPath -PathType Leaf)) {
            Remove-Item -LiteralPath $SecretsPath -Force
        }
    }
    throw $failure
}
