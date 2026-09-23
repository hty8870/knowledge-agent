from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "automation" / "quality-gates.json"
RUNNER = ROOT / "scripts" / "quality_gate.py"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _run(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=cwd or ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _probe_manifest(tmp_path: Path, code: str, timeout_seconds: int = 10) -> Path:
    manifest = _manifest()
    gate = {
        "id": "probe",
        "description": "test-only subprocess behavior probe",
        "commands": [["{python}", "-c", code]],
        "required_tools": ["python"],
        "timeout_seconds": timeout_seconds,
        "offline": True,
        "network": "forbidden",
        "secrets": "forbidden",
        "model_downloads": "forbidden",
        "fallback": {
            "missing_tool": "fail",
            "timeout": "fail",
            "command_failure": "fail",
        },
    }
    manifest["gates"] = [gate]
    for profile in manifest["profiles"].values():
        profile["gates"] = ["probe"]
    path = tmp_path / "probe-manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_manifest_is_explicit_offline_and_fail_closed():
    manifest = _manifest()
    assert manifest["schema_version"] == 1
    assert manifest["default_profile"] == "fast"
    assert set(manifest["profiles"]) == {"fast", "full"}
    policy = manifest["execution_policy"]
    assert policy["offline_after_dependency_install"] is True
    assert policy["network"] == "forbidden"
    assert "not an operating-system egress sandbox" in policy["network_enforcement"]
    assert policy["secrets"] == "forbidden-and-sanitized"
    assert policy["model_downloads"] == "forbidden"
    assert "upper/lower-case proxy" in policy["environment_injection"]
    assert "fail-closed" in policy["fallback"]

    ids = [gate["id"] for gate in manifest["gates"]]
    assert len(ids) == len(set(ids))
    assert set(manifest["profiles"]["fast"]["gates"]) < set(
        manifest["profiles"]["full"]["gates"]
    )
    for gate in manifest["gates"]:
        assert gate["offline"] is True
        assert gate["network"] == "forbidden"
        assert gate["secrets"] == "forbidden"
        assert gate["model_downloads"] == "forbidden"
        assert gate["timeout_seconds"] > 0
        assert gate["fallback"] == {
            "missing_tool": "fail",
            "timeout": "fail",
            "command_failure": "fail",
        }
        assert gate["commands"]
        assert all(isinstance(command, list) and command for command in gate["commands"])

    serialized = json.dumps(manifest).lower()
    assert "--llm-check" not in serialized
    assert "fetch_embedding_model" not in serialized
    assert not re.search(r"\$\{\{\s*secrets\.", serialized)


def test_full_profile_covers_python_web_mcp_and_frozen_evaluation():
    full = set(_manifest()["profiles"]["full"]["gates"])
    assert {
        "pytest-suite",
        "project-smoke",
        "web-smoke",
        "mcp-protocol-selfcheck",
        "frozen-recommendation-evaluation",
    } <= full


def test_list_describes_profiles_and_safety_policy():
    result = _run("--list")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fast (default)" in result.stdout
    assert "full:" in result.stdout
    assert "network forbidden" in result.stdout
    assert "not an OS egress sandbox" in result.stdout
    assert "fallback fail-closed" in result.stdout


def test_default_profile_comes_from_manifest(tmp_path: Path):
    report_path = tmp_path / "default-profile.json"
    result = _run("--dry-run", "--report-json", str(report_path))
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["profile"] == _manifest()["default_profile"] == "fast"


def test_fast_dry_run_is_repo_rooted_and_writes_machine_report(tmp_path: Path):
    report_path = tmp_path / "quality-report.json"
    result = _run(
        "--profile",
        "fast",
        "--dry-run",
        "--report-json",
        str(report_path),
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["repository_root"] == str(ROOT)
    assert report["profile"] == "fast"
    assert report["dry_run"] is True
    assert report["status"] == "planned"
    assert report["environment"]["pythonpath"] == os.pathsep.join(
        (str(ROOT), str(ROOT / "src"))
    )
    assert {gate["id"] for gate in report["gates"]} == set(
        _manifest()["profiles"]["fast"]["gates"]
    )
    assert all(gate["status"] == "planned" for gate in report["gates"])
    assert all(gate["commands"] for gate in report["gates"])


def test_invalid_explicit_tool_override_fails_closed(tmp_path: Path):
    report_path = tmp_path / "failed.json"
    env = os.environ.copy()
    env["BIODATA_NODE"] = str(tmp_path / "node-does-not-exist")
    result = _run(
        "--profile",
        "fast",
        "--dry-run",
        "--report-json",
        str(report_path),
        env=env,
    )
    assert result.returncode == 2
    assert "never fall back silently" in result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "configuration-error"
    assert "node-does-not-exist" in report["error"]


def test_secret_is_cleared_in_real_child_and_never_appears_in_report(tmp_path: Path):
    marker = "TOP_SECRET_SHOULD_NOT_LEAK_7419"
    manifest = _probe_manifest(
        tmp_path,
        "import os,sys; sys.exit(0 if os.getenv('OPENAI_API_KEY') == '' else 9)",
    )
    report_path = tmp_path / "sanitized.json"
    env = os.environ.copy()
    env["OPENAI_API_KEY"] = marker
    result = _run(
        "--profile",
        "fast",
        "--manifest",
        str(manifest),
        "--report-json",
        str(report_path),
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    assert marker not in combined
    assert json.loads(report_path.read_text(encoding="utf-8"))["environment"][
        "secret_values_sanitized"
    ] >= 1


def test_injection_variables_are_removed_from_real_child_process(tmp_path: Path):
    code = (
        "import os,sys; expected=os.pathsep.join((sys.argv[1], os.path.join(sys.argv[1], 'src'))); "
        "ok=(os.getenv('PYTHONPATH') == expected and not os.getenv('PYTEST_ADDOPTS') "
        "and not os.getenv('PYTEST_PLUGINS') and not os.getenv('NODE_OPTIONS') "
        "and os.getenv('http_proxy') == 'http://127.0.0.1:9' "
        "and os.getenv('https_proxy') == 'http://127.0.0.1:9' "
        "and os.getenv('PYTEST_DISABLE_PLUGIN_AUTOLOAD') == '1'); sys.exit(0 if ok else 8)"
    )
    manifest = _probe_manifest(tmp_path, code)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["gates"][0]["commands"][0].append(str(ROOT))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report_path = tmp_path / "injection.json"
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(tmp_path / "poison"),
            "PYTEST_ADDOPTS": "--collect-only",
            "PYTEST_PLUGINS": "poison_plugin",
            "NODE_OPTIONS": "--require poison.js",
            "http_proxy": "http://LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419.invalid",
            "https_proxy": "http://LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419.invalid",
        }
    )
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path), env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    cleared = {
        name.casefold()
        for name in report["environment"]["injection_variables_cleared"]
    }
    assert cleared >= {
        "pythonpath",
        "pytest_addopts",
        "pytest_plugins",
        "node_options",
        "http_proxy",
        "https_proxy",
    }
    assert "LOWERCASE_PROXY_MUST_NOT_SURVIVE_7419" not in (
        result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    )


def test_project_dotenv_cannot_repopulate_provider_secrets(tmp_path: Path):
    project = tmp_path / "project-with-env"
    project.mkdir()
    marker = "DOTENV_SECRET_MUST_NOT_LOAD_9317"
    (project / ".env").write_text(
        f"ZAI_API_KEY={marker}\nZHIPUAI_TOKEN={marker}\n", encoding="utf-8"
    )
    code = (
        "import os,sys; from pathlib import Path; "
        "from dataset_recommender.llm.config import load_env_candidates; "
        "loaded=load_env_candidates(Path(sys.argv[1])); "
        "ok=(loaded is not None and loaded.name == 'empty-llm.env' "
        "and os.getenv('ZAI_API_KEY') == '' and os.getenv('ZHIPUAI_TOKEN') == ''); "
        "sys.exit(0 if ok else 6)"
    )
    manifest = _probe_manifest(tmp_path, code)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["gates"][0]["commands"][0].append(str(project))
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    report_path = tmp_path / "dotenv.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    combined = result.stdout + result.stderr + report_path.read_text(encoding="utf-8")
    assert marker not in combined


def test_nonzero_child_fails_closed_and_records_exit_code(tmp_path: Path):
    manifest = _probe_manifest(tmp_path, "import sys; print('probe failed'); sys.exit(7)")
    report_path = tmp_path / "nonzero.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    command = report["gates"][0]["commands"][0]
    assert command["status"] == "failed"
    assert command["exit_code"] == 7
    assert "probe failed" in command["stdout_tail"]


def test_timed_out_child_fails_closed_and_records_timeout(tmp_path: Path):
    manifest = _probe_manifest(tmp_path, "import time; time.sleep(3)", timeout_seconds=1)
    report_path = tmp_path / "timeout.json"
    result = _run(
        "--manifest", str(manifest), "--profile", "fast", "--report-json", str(report_path)
    )
    assert result.returncode == 1
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    command = report["gates"][0]["commands"][0]
    assert command["status"] == "timeout"
    assert report["gates"][0]["failure"] == "timeout (fallback=fail)"


def test_manifest_validator_rejects_network_enabled_gate(tmp_path: Path):
    manifest = _manifest()
    manifest["gates"][0]["network"] = "allowed"
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run("--manifest", str(invalid), "--dry-run")
    assert result.returncode == 2
    assert "must forbid network" in result.stderr
