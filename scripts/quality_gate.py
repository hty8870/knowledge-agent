#!/usr/bin/env python3
"""Run BioData's manifest-defined quality gates under a no-network policy.

The JSON manifest is the source of truth used by both developers and GitHub CI.
This runner deliberately does not install dependencies, download models, load
secrets, or intentionally call external services. The runner adds process-level
tripwires but is not an operating-system egress sandbox. Missing tools and
command failures are fail-closed so a degraded environment cannot be reported
as a pass.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "automation" / "quality-gates.json"

# 被门命令（pytest/smoke 等）若打印出 secret 形状的值，会随 stdout/stderr 尾**原样**落进 report JSON
# （env 名已脱敏，但命令**输出**不受 env 脱敏保护）。落盘前用同一批锚定模式做 redaction，防持久化二次泄漏。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from secret_patterns import redact as _redact_secrets  # noqa: E402
PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")
TOOL_PLACEHOLDERS = {"python", "node", "powershell"}
REQUIRED_GATE_FIELDS = {
    "id",
    "description",
    "commands",
    "required_tools",
    "timeout_seconds",
    "offline",
    "network",
    "secrets",
    "model_downloads",
    "fallback",
}
SECRET_NAME_MARKERS = (
    "API_KEY",
    "TOKEN",
    "SECRET",
    "PASSWORD",
    "CREDENTIAL",
    "PRIVATE_KEY",
)
EXPLICIT_SECRET_NAMES = {
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "DASHSCOPE_API_KEY",
    "QWEN_API_KEY",
    "ZAI_API_KEY",
    "ZHIPUAI_API_KEY",
    "ZHIPUAI_TOKEN",
    "GLM_API_KEY",
    "ANTHROPIC_API_KEY",
    "HF_TOKEN",
    "HUGGINGFACE_HUB_TOKEN",
}
INJECTION_ENV_NAMES = {
    "PYTHONHOME",
    "PYTHONINSPECT",
    "PYTHONSTARTUP",
    "PYTHONUSERBASE",
    "PYTHONWARNINGS",
    "PYTHONBREAKPOINT",
    "PYTHONPATH",
    "PYTEST_ADDOPTS",
    "PYTEST_PLUGINS",
    "COVERAGE_PROCESS_START",
    "COVERAGE_RCFILE",
    "NODE_OPTIONS",
    "NODE_PATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
}


class ManifestError(ValueError):
    """Raised when the machine-readable quality contract is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ManifestError(f"manifest not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ManifestError("manifest root must be an object")
    _validate_manifest(payload)
    return payload


def _validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != 1:
        raise ManifestError("schema_version must be 1")

    policy = manifest.get("execution_policy")
    if not isinstance(policy, dict):
        raise ManifestError("execution_policy must be an object")
    expected_policy = {
        "offline_after_dependency_install": True,
        "network": "forbidden",
        "network_enforcement": (
            "reviewed command allowlist plus best-effort dead-proxy and offline environment; "
            "this is not an operating-system egress sandbox"
        ),
        "secrets": "forbidden-and-sanitized",
        "model_downloads": "forbidden",
        "environment_injection": (
            "known Python, pytest, coverage, Node preload, and upper/lower-case proxy "
            "variables are removed before reviewed replacements are set"
        ),
    }
    for key, expected in expected_policy.items():
        if policy.get(key) != expected:
            raise ManifestError(f"execution_policy.{key} must be {expected!r}")

    tools = manifest.get("tools")
    if not isinstance(tools, dict) or not TOOL_PLACEHOLDERS.issubset(tools):
        raise ManifestError("tools must define python, node, and powershell")
    for name, spec in tools.items():
        if not isinstance(spec, dict):
            raise ManifestError(f"tool {name!r} must be an object")
        if not isinstance(spec.get("environment_override"), str):
            raise ManifestError(f"tool {name!r} needs environment_override")
        candidates = spec.get("candidates")
        if not isinstance(candidates, list) or not candidates or not all(
            isinstance(item, str) and item for item in candidates
        ):
            raise ManifestError(f"tool {name!r} needs non-empty candidates")

    raw_gates = manifest.get("gates")
    if not isinstance(raw_gates, list) or not raw_gates:
        raise ManifestError("gates must be a non-empty array")
    gates: dict[str, dict[str, Any]] = {}
    for gate in raw_gates:
        if not isinstance(gate, dict):
            raise ManifestError("each gate must be an object")
        missing = REQUIRED_GATE_FIELDS - set(gate)
        if missing:
            raise ManifestError(f"gate missing fields: {sorted(missing)}")
        gate_id = gate["id"]
        if not isinstance(gate_id, str) or not re.fullmatch(r"[a-z0-9-]+", gate_id):
            raise ManifestError(f"invalid gate id: {gate_id!r}")
        if gate_id in gates:
            raise ManifestError(f"duplicate gate id: {gate_id}")
        gates[gate_id] = gate

        if gate["offline"] is not True:
            raise ManifestError(f"gate {gate_id} must be offline")
        if gate["network"] != "forbidden":
            raise ManifestError(f"gate {gate_id} must forbid network")
        if gate["secrets"] != "forbidden":
            raise ManifestError(f"gate {gate_id} must forbid secrets")
        if gate["model_downloads"] != "forbidden":
            raise ManifestError(f"gate {gate_id} must forbid model downloads")
        if not isinstance(gate["timeout_seconds"], int) or gate["timeout_seconds"] <= 0:
            raise ManifestError(f"gate {gate_id} needs a positive timeout_seconds")
        fallback = gate["fallback"]
        if not isinstance(fallback, dict) or any(
            fallback.get(key) != "fail"
            for key in ("missing_tool", "timeout", "command_failure")
        ):
            raise ManifestError(f"gate {gate_id} fallback must fail closed")

        required_tools = gate["required_tools"]
        if not isinstance(required_tools, list) or not all(
            isinstance(item, str) and item in tools for item in required_tools
        ):
            raise ManifestError(f"gate {gate_id} has invalid required_tools")
        commands = gate["commands"]
        if not isinstance(commands, list) or not commands:
            raise ManifestError(f"gate {gate_id} needs commands")
        placeholders: set[str] = set()
        for command in commands:
            if not isinstance(command, list) or not command or not all(
                isinstance(arg, str) and arg for arg in command
            ):
                raise ManifestError(f"gate {gate_id} commands must be argv arrays")
            placeholders.update(
                match.group(1) for arg in command for match in PLACEHOLDER_RE.finditer(arg)
            )
        unknown = placeholders - TOOL_PLACEHOLDERS - {"file"}
        if unknown:
            raise ManifestError(f"gate {gate_id} has unknown placeholders: {sorted(unknown)}")
        undeclared = (placeholders & TOOL_PLACEHOLDERS) - set(required_tools)
        if undeclared:
            raise ManifestError(f"gate {gate_id} has undeclared tools: {sorted(undeclared)}")
        has_foreach = "foreach_glob" in gate
        if ("file" in placeholders) != has_foreach:
            raise ManifestError(f"gate {gate_id} must pair {{file}} with foreach_glob")
        if has_foreach and not isinstance(gate["foreach_glob"], str):
            raise ManifestError(f"gate {gate_id} foreach_glob must be a string")

    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict) or set(profiles) != {"fast", "full"}:
        raise ManifestError("profiles must define exactly fast and full")
    for profile_name, profile in profiles.items():
        if not isinstance(profile, dict) or not isinstance(profile.get("description"), str):
            raise ManifestError(f"profile {profile_name} needs a description")
        profile_gates = profile.get("gates")
        if not isinstance(profile_gates, list) or not profile_gates:
            raise ManifestError(f"profile {profile_name} needs gates")
        missing_ids = set(profile_gates) - set(gates)
        if missing_ids:
            raise ManifestError(f"profile {profile_name} references unknown gates: {sorted(missing_ids)}")
        if len(profile_gates) != len(set(profile_gates)):
            raise ManifestError(f"profile {profile_name} repeats a gate")
    if manifest.get("default_profile") not in profiles:
        raise ManifestError("default_profile must name a profile")


def _executable(value: str) -> str | None:
    """Return an executable path without interpreting arguments or shell syntax."""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(value)


def _resolve_tools(manifest: dict[str, Any], gate_ids: list[str]) -> dict[str, str]:
    gates = {gate["id"]: gate for gate in manifest["gates"]}
    needed = {tool for gate_id in gate_ids for tool in gates[gate_id]["required_tools"]}
    resolved: dict[str, str] = {}
    for tool_name in sorted(needed):
        spec = manifest["tools"][tool_name]
        override_name = spec["environment_override"]
        override = os.environ.get(override_name)
        if override:
            path = _executable(override)
            if not path:
                raise ManifestError(
                    f"{override_name} points to a missing executable: {override!r}; "
                    "explicit overrides never fall back silently"
                )
            resolved[tool_name] = path
            continue

        # The running interpreter is always the most reliable Python on Windows,
        # including environments where `python` and `py` are absent from PATH.
        if tool_name == "python" and Path(sys.executable).is_file():
            resolved[tool_name] = str(Path(sys.executable).resolve())
            continue
        for candidate in spec["candidates"]:
            path = _executable(candidate)
            if path:
                resolved[tool_name] = path
                break
        if tool_name not in resolved:
            raise ManifestError(
                f"required tool {tool_name!r} was not found; tried "
                f"{spec['candidates']!r} and {override_name}"
            )
    return resolved


def _sanitized_environment(
    runtime_temp: Path,
) -> tuple[dict[str, str], int, list[str]]:
    env = os.environ.copy()
    sanitized = 0
    for name in list(env):
        upper = name.upper()
        if name in EXPLICIT_SECRET_NAMES or any(marker in upper for marker in SECRET_NAME_MARKERS):
            if env[name]:
                sanitized += 1
            # Keep an explicit empty value so python-dotenv cannot repopulate it.
            env[name] = ""
    for name in EXPLICIT_SECRET_NAMES:
        env[name] = ""

    cleared_injection = sorted(
        name for name in INJECTION_ENV_NAMES if name in env and env.get(name)
    )
    for name in INJECTION_ENV_NAMES:
        env.pop(name, None)

    # Point application config at a real, empty env file. An empty variable
    # would make load_env_candidates fall back to the repository's .env files.
    empty_llm_env = runtime_temp / "empty-llm.env"
    empty_llm_env.write_text("# quality gate: intentionally empty\n", encoding="utf-8")

    # Best-effort process-level network tripwire. This is additive to the
    # manifest contract and the absence of any live-network command.
    dead_proxy = "http://127.0.0.1:9"
    proxy_tripwires = {
        "HTTP_PROXY": dead_proxy,
        "HTTPS_PROXY": dead_proxy,
        "ALL_PROXY": dead_proxy,
        "NO_PROXY": "127.0.0.1,localhost,::1",
    }
    for name, value in proxy_tripwires.items():
        env[name] = value
        if os.name != "nt":
            env[name.lower()] = value
    env.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "BIODATA_CI_OFFLINE": "1",
            "BIODATA_ALLOW_MODEL_DOWNLOAD": "0",
            "BIODATA_LLM_ENV_FILE": str(empty_llm_env),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUTF8": "1",
            "PYTHONNOUSERSITE": "1",
            "PYTHONSAFEPATH": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "TMP": str(runtime_temp),
            "TEMP": str(runtime_temp),
            "TMPDIR": str(runtime_temp),
        }
    )
    # The test suite imports `dataset_recommender.*` from root/src (single
    # package identity, ); the repository root stays for historical
    # root-level module compatibility (mcp_server moved into the package as
    # dataset_recommender.app.mcp_server on ). Keep only those two
    # reviewed paths; never inherit caller-controlled entries.
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT), str(REPO_ROOT / "src")))
    return env, sanitized, cleared_injection


def _selected_gates(manifest: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    if profile not in manifest["profiles"]:
        raise ManifestError(
            f"unknown profile {profile!r}; choose one of {sorted(manifest['profiles'])}"
        )
    by_id = {gate["id"]: gate for gate in manifest["gates"]}
    return [by_id[gate_id] for gate_id in manifest["profiles"][profile]["gates"]]


def _expand_commands(gate: dict[str, Any], tools: dict[str, str]) -> list[list[str]]:
    files: list[str | None] = [None]
    if "foreach_glob" in gate:
        files = [
            path.relative_to(REPO_ROOT).as_posix()
            for path in sorted(REPO_ROOT.glob(gate["foreach_glob"]))
            if path.is_file()
        ]
        if not files:
            raise ManifestError(
                f"gate {gate['id']} matched no files for {gate['foreach_glob']!r}"
            )

    expanded: list[list[str]] = []
    for file_name in files:
        replacements = {**tools, "file": file_name or ""}
        for template in gate["commands"]:
            command = []
            for arg in template:
                command.append(
                    PLACEHOLDER_RE.sub(lambda match: replacements[match.group(1)], arg)
                )
            expanded.append(command)
    return expanded


def _display_command(command: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(command)
    return shlex.join(command)


def _tail(value: str, limit: int = 8000) -> str:
    # 先对整段做 secret redaction 再截断——防一个 secret 值恰好横跨截断边界而漏网。
    value = _redact_secrets(value) or ""
    return value if len(value) <= limit else "…[truncated]\n" + value[-limit:]


def _run_profile(
    manifest: dict[str, Any], profile: str, dry_run: bool, runtime_temp: Path
) -> tuple[dict[str, Any], int]:
    gates = _selected_gates(manifest, profile)
    gate_ids = [gate["id"] for gate in gates]
    tools = _resolve_tools(manifest, gate_ids)
    env, sanitized_count, cleared_injection = _sanitized_environment(runtime_temp)
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": _utc_now(),
        "repository_root": str(REPO_ROOT),
        "profile": profile,
        "dry_run": dry_run,
        "execution_policy": manifest["execution_policy"],
        "tools": tools,
        "environment": {
            "secret_values_sanitized": sanitized_count,
            "network_policy": "forbidden",
            "network_enforcement": "best-effort tripwire; not an OS egress sandbox",
            "network_proxy_tripwire": True,
            "model_downloads_disabled": True,
            "injection_variables_cleared": cleared_injection,
            "pythonpath": env["PYTHONPATH"],
            "pytest_plugin_autoload_disabled": True,
        },
        "status": "planned" if dry_run else "running",
        "gates": [],
    }

    for gate in gates:
        commands = _expand_commands(gate, tools)
        gate_report: dict[str, Any] = {
            "id": gate["id"],
            "description": gate["description"],
            "status": "planned" if dry_run else "running",
            "timeout_seconds": gate["timeout_seconds"],
            "offline": gate["offline"],
            "network": gate["network"],
            "secrets": gate["secrets"],
            "model_downloads": gate["model_downloads"],
            "fallback": gate["fallback"],
            "commands": [],
        }
        report["gates"].append(gate_report)
        print(f"[quality-gate] {gate['id']}: {gate['description']}", flush=True)

        gate_started = time.monotonic()
        for command in commands:
            command_report: dict[str, Any] = {
                "argv": command,
                "display": _display_command(command),
                "status": "planned" if dry_run else "running",
            }
            gate_report["commands"].append(command_report)
            print(f"  $ {command_report['display']}", flush=True)
            if dry_run:
                continue

            started = time.monotonic()
            try:
                result = subprocess.run(
                    command,
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=gate["timeout_seconds"],
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
                command_report.update(
                    {
                        "status": "timeout",
                        "duration_seconds": round(time.monotonic() - started, 3),
                        "stdout_tail": _tail(stdout),
                        "stderr_tail": _tail(stderr),
                    }
                )
                gate_report["status"] = "failed"
                gate_report["failure"] = "timeout (fallback=fail)"
                report["status"] = "failed"
                print(f"  FAIL: timeout after {gate['timeout_seconds']}s", flush=True)
                return report, 1

            command_report.update(
                {
                    "status": "passed" if result.returncode == 0 else "failed",
                    "exit_code": result.returncode,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "stdout_tail": _tail(result.stdout),
                    "stderr_tail": _tail(result.stderr),
                }
            )
            # 控制台实时回显同样脱敏——CI 作业日志会持久化它，是比 report 更显眼的泄漏面。
            live_stdout = _redact_secrets(result.stdout) or ""
            live_stderr = _redact_secrets(result.stderr) or ""
            if live_stdout:
                print(live_stdout, end="" if live_stdout.endswith("\n") else "\n", flush=True)
            if live_stderr:
                print(
                    live_stderr,
                    end="" if live_stderr.endswith("\n") else "\n",
                    file=sys.stderr,
                    flush=True,
                )
            if result.returncode != 0:
                gate_report["status"] = "failed"
                gate_report["failure"] = (
                    f"command exited {result.returncode} (fallback=fail)"
                )
                gate_report["duration_seconds"] = round(time.monotonic() - gate_started, 3)
                report["status"] = "failed"
                print(f"  FAIL: exit code {result.returncode}", flush=True)
                return report, 1

        gate_report["status"] = "planned" if dry_run else "passed"
        gate_report["duration_seconds"] = round(time.monotonic() - gate_started, 3)

    report["status"] = "planned" if dry_run else "passed"
    return report, 0


def _write_report(path: Path, report: dict[str, Any]) -> None:
    target = path if path.is_absolute() else REPO_ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"[quality-gate] report: {target}", flush=True)


def _print_manifest(manifest: dict[str, Any]) -> None:
    by_id = {gate["id"]: gate for gate in manifest["gates"]}
    print("BioData quality profiles (manifest schema 1)")
    print(
        "Policy: offline; network forbidden; secrets sanitized; model downloads forbidden; "
        "fallback fail-closed."
    )
    print(
        "Enforcement: reviewed command allowlist plus best-effort process tripwires; "
        "not an OS egress sandbox."
    )
    for name, profile in manifest["profiles"].items():
        marker = " (default)" if name == manifest["default_profile"] else ""
        print(f"\n{name}{marker}: {profile['description']}")
        for gate_id in profile["gates"]:
            print(f"  - {gate_id}: {by_id[gate_id]['description']}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run manifest-defined BioData quality gates under a no-network/no-secrets policy."
        )
    )
    parser.add_argument(
        "--profile", help="quality profile (defaults to default_profile in the manifest)"
    )
    parser.add_argument("--list", action="store_true", help="list profiles and gates, then exit")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="resolve and print commands without running them",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        help="write a machine-readable report (relative paths are rooted at the repository)",
    )
    parser.add_argument(
        "--manifest", type=Path, default=DEFAULT_MANIFEST, help=argparse.SUPPRESS
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report: dict[str, Any]
    try:
        manifest = _load_manifest(args.manifest.resolve())
        profile = args.profile or manifest["default_profile"]
        if args.list:
            _print_manifest(manifest)
            report = {
                "schema_version": 1,
                "generated_at": _utc_now(),
                "repository_root": str(REPO_ROOT),
                "status": "listed",
                "profiles": manifest["profiles"],
                "execution_policy": manifest["execution_policy"],
            }
            exit_code = 0
        else:
            # Keep pytest and subprocess scratch data in an ignored project
            # directory. This is reliable in restricted Windows sessions where
            # the account's global %TEMP% may be unreadable, and it is removed
            # automatically when the profile exits.
            temp_parent = REPO_ROOT / "outputs"
            temp_parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="quality-gate-", dir=temp_parent, ignore_cleanup_errors=True
            ) as runtime_temp:
                report, exit_code = _run_profile(
                    manifest, profile, args.dry_run, Path(runtime_temp)
                )
    except ManifestError as exc:
        print(f"QUALITY_GATE_CONFIG_ERROR: {exc}", file=sys.stderr, flush=True)
        report = {
            "schema_version": 1,
            "generated_at": _utc_now(),
            "repository_root": str(REPO_ROOT),
            "profile": args.profile or "manifest-default",
            "dry_run": args.dry_run,
            "status": "configuration-error",
            "error": str(exc),
        }
        exit_code = 2

    if args.report_json:
        _write_report(args.report_json, report)
    if not args.list:
        print(f"[quality-gate] result: {report['status']}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
