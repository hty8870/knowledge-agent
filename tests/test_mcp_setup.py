from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "setup_mcp.ps1"


def _powershell() -> str:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        pytest.skip("PowerShell is not available")
    return executable


def test_example_configs_are_valid_and_never_embed_a_key():
    tutorial_dir = ROOT / "使用教程" / "MCP安装"
    offline = json.loads((tutorial_dir / "mcp.example.json").read_text(encoding="utf-8"))
    llm = json.loads((tutorial_dir / "mcp.llm.example.json").read_text(encoding="utf-8"))
    offline_env = offline["mcpServers"]["biodata"]["env"]
    llm_env = llm["mcpServers"]["biodata"]["env"]
    assert "BIODATA_LLM_ENV_FILE" not in offline_env
    assert llm_env["BIODATA_LLM_ENV_FILE"].startswith("REPLACE_WITH_")
    assert all("API_KEY" not in key for key in llm_env)


def test_setup_script_is_ascii_and_powershell_parses():
    raw = SCRIPT.read_bytes()
    assert all(byte < 128 for byte in raw), "Windows PowerShell 5.1 requires this no-BOM script to stay ASCII"
    command = (
        "$t=$null;$e=$null;"
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{SCRIPT}',[ref]$t,[ref]$e);"
        "if($e.Count){$e|%{$_.Message};exit 1}"
    )
    result = subprocess.run(
        [_powershell(), "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    ("client", "provider", "expected_provider"),
    [("codex", "openai", "openai-compatible"), ("claude", "zhipuai", "zhipuai")],
)
def test_plan_only_is_non_mutating_and_secret_free(client: str, provider: str, expected_provider: str):
    result = subprocess.run(
        [
            _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SCRIPT),
            "-PlanOnly", "-Client", client, "-Provider", provider,
            "-ProjectPath", str(ROOT), "-PythonPath", r"C:\fake\python.exe",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    plan = json.loads(result.stdout)
    assert plan["client"] == client
    assert plan["provider"] == expected_provider
    assert plan["live_llm_check"] is True
    assert "API_KEY" not in result.stdout and "sk-" not in result.stdout
