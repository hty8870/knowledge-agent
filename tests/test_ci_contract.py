from __future__ import annotations

import re
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
CI_REQUIREMENTS = ROOT / "requirements" / "requirements-ci.txt"
CI_LOCK = ROOT / "requirements" / "requirements-ci.lock"
RUNTIME_REQUIREMENTS = ROOT / "requirements" / "requirements.txt"

EXPECTED_ACTIONS = {
    "actions/checkout": ("de0fac2e4500dabe0009e67214ff5f5447ce83dd", "v6.0.2"),
    "actions/setup-python": ("a309ff8b426b58ec0e2a45f0f869d46889d02405", "v6.2.0"),
    "actions/upload-artifact": ("043fb46d1a93c77aae656e7c1c64a875d1fc6a0a", "v7.0.1"),
}


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workflow_has_safe_triggers_minimal_permissions_and_concurrency():
    workflow = _text(WORKFLOW)
    assert re.search(r"(?m)^on:\s*$", workflow)
    for event in ("push", "pull_request", "workflow_dispatch"):
        assert re.search(rf"(?m)^  {event}:\s*$", workflow)
    assert "pull_request_target" not in workflow
    assert re.search(r"(?m)^permissions: \{\}\s*$", workflow)
    assert re.search(r"(?m)^concurrency:\s*$", workflow)
    assert "cancel-in-progress: true" in workflow
    assert workflow.count("timeout-minutes:") >= 2
    assert "${{ secrets." not in workflow


def test_every_action_is_pinned_to_the_reviewed_full_commit_sha():
    workflow = _text(WORKFLOW)
    uses = re.findall(
        r"(?m)^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([0-9a-f]+)\s*(?:#\s*(\S+))?\s*$",
        workflow,
    )
    assert uses, "workflow must contain parseable uses entries"
    assert {name for name, _, _ in uses} == set(EXPECTED_ACTIONS)
    for name, sha, comment in uses:
        expected_sha, expected_version = EXPECTED_ACTIONS[name]
        assert re.fullmatch(r"[0-9a-f]{40}", sha)
        assert sha == expected_sha
        assert comment == expected_version
    assert len(re.findall(r"(?m)^\s*-?\s*uses:", workflow)) == len(uses)


def test_ci_uses_one_manifest_runner_across_fixed_linux_and_windows_legs():
    workflow = _text(WORKFLOW)
    assert "ubuntu-24.04" in workflow
    assert "windows-2025" in workflow
    assert 'python: "3.10"' in workflow
    assert 'python: "3.12"' in workflow
    assert 'python: "3.14"' in workflow
    # Public has an explicit evaluation manifest and independent public validation set,
    # so the primary Windows leg must run the real public full profile.
    assert "profile: fast" in workflow
    assert "profile: full" in workflow
    assert re.search(
        r"label: windows-primary-full\s+os: windows-2025\s+python: \"3\.12\"\s+profile: full",
        workflow,
    )
    assert re.search(
        r"label: ubuntu-min-fast\s+os: ubuntu-24\.04\s+python: \"3\.10\"\s+profile: fast",
        workflow,
    )
    assert "python scripts/quality_gate.py" in workflow
    assert "--profile ${{ matrix.profile }}" in workflow
    assert re.search(r"(?m)^  gate:\s*$", workflow)
    assert re.search(r"(?m)^    name: gate\s*$", workflow)
    assert "needs: [quality, runtime_smoke, delivery_scan]" in workflow
    assert "if: always()" in workflow


def test_ci_scans_delivery_surface_for_internal_or_sensitive_content():
    """源头防污门：每次 push/PR 即复核交付白名单集合，污染当场翻红，不再攒到专项清洗。"""
    workflow = _text(WORKFLOW)
    assert re.search(r"(?m)^  delivery_scan:\s*$", workflow)
    assert "python scripts/make_delivery.py --check" in workflow
    assert "DELIVERY_SCAN_RESULT: ${{ needs.delivery_scan.result }}" in workflow


def test_ci_runs_runtime_smoke_on_every_supported_source_platform():
    workflow = _text(WORKFLOW)
    assert re.search(r"(?m)^  runtime_smoke:\s*$", workflow)
    for platform in ("windows", "macos", "linux"):
        assert f"- platform: {platform}" in workflow
    assert "smoke_release_first_launch.py --platform ${{ matrix.platform }}" in workflow
    assert "RUNTIME_SMOKE_RESULT: ${{ needs.runtime_smoke.result }}" in workflow


def test_ci_is_offline_after_install_and_never_runs_real_llm_or_model_fetch():
    workflow = _text(WORKFLOW)
    assert "BIODATA_CI_OFFLINE: \"1\"" in workflow
    assert "HF_HUB_OFFLINE: \"1\"" in workflow
    assert "TRANSFORMERS_OFFLINE: \"1\"" in workflow
    assert "BIODATA_ALLOW_MODEL_DOWNLOAD: \"0\"" in workflow
    assert "--llm-check" not in workflow
    assert "fetch_embedding_model" not in workflow
    assert "secrets." not in workflow


def test_ci_installs_only_the_hash_locked_environment():
    source = _text(CI_REQUIREMENTS)
    assert re.search(r"(?m)^-r requirements\.txt\s*$", source)
    assert re.search(r"(?m)^pytest>=9\.1\.1\s*$", source)
    assert re.search(r"(?m)^mcp==1\.28\.1\s*$", source)

    runtime = _text(RUNTIME_REQUIREMENTS)
    runtime_names = {
        canonicalize_name(Requirement(line.strip()).name)
        for line in runtime.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r "))
    }
    assert "pytest" not in runtime_names, "pytest is test-only and must not ship in requirements.txt"

    lock = _text(CI_LOCK)
    assert "--hash=sha256:" in lock
    assert not re.search(r"(?m)^\s*(-e|--editable)\s", lock)
    assert not re.search(r"(?m)^\s*--(index-url|extra-index-url|trusted-host)\b", lock)
    assert not re.search(r"(?i)[a-z]:\\users\\", lock)
    posix_home_prefix = "/" + "home/"
    assert not re.search(re.escape(posix_home_prefix) + r"[^/]+/", lock)

    workflow = _text(WORKFLOW)
    assert (
        "python -m pip install --require-hashes --only-binary=:all: -r requirements/requirements-ci.lock"
        in workflow
    )
    assert "cache-dependency-path: requirements/requirements-ci.lock" in workflow
    assert "pip install -r requirements-ci.txt" not in workflow


def test_every_direct_requirement_is_present_and_satisfied_by_the_lock():
    direct: list[Requirement] = []
    for path in (RUNTIME_REQUIREMENTS, CI_REQUIREMENTS):
        for raw in _text(path).splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("-r "):
                continue
            direct.append(Requirement(line))

    resolved: dict[str, Version] = {}
    for raw in _text(CI_LOCK).splitlines():
        line = raw.strip().removesuffix("\\").strip()
        if not line or line.startswith(("#", "--")) or "==" not in line:
            continue
        requirement = Requirement(line)
        pins = list(requirement.specifier)
        if len(pins) == 1 and pins[0].operator == "==":
            resolved[canonicalize_name(requirement.name)] = Version(pins[0].version)

    assert direct
    for requirement in direct:
        name = canonicalize_name(requirement.name)
        assert name in resolved, f"{requirement} is missing from requirements-ci.lock"
        assert resolved[name] in requirement.specifier, (
            f"{requirement} is not satisfied by locked {name}=={resolved[name]}"
        )


def test_dependabot_covers_python_and_workflow_dependencies_weekly():
    config = _text(DEPENDABOT)
    assert config.startswith("version: 2\n")
    assert "package-ecosystem: pip" in config
    assert "package-ecosystem: github-actions" in config
    assert config.count("interval: weekly") == 2
    # 2026-08-27 一级目录整理：requirements 群迁入 requirements/，pip 生态目录随迁；
    # github-actions 生态目录恒为仓库根。
    directories = re.findall(r"(?m)^    directory: (.+)$", config)
    assert directories == ["/requirements", "/"]
