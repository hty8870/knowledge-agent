from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "smoke_release_first_launch.py"
SPEC = importlib.util.spec_from_file_location("runtime_first_launch_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = smoke
SPEC.loader.exec_module(smoke)


def test_native_platform_has_exact_entry_command(tmp_path: Path):
    commands = {
        "windows": smoke._entry_command("windows", tmp_path),
        "macos": smoke._entry_command("macos", tmp_path),
        "linux": smoke._entry_command("linux", tmp_path),
    }
    assert commands["windows"][:3] == ["cmd.exe", "/d", "/c"]
    assert commands["windows"][-1].endswith("打开前端.bat")
    assert commands["macos"] == ["/bin/bash", str(tmp_path / "打开前端.command")]
    assert commands["linux"] == ["/bin/sh", str(tmp_path / "打开前端.sh")]


def test_smoke_environment_removes_python_leakage(monkeypatch):
    for key in ("BIODATA_PYTHON", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PORT"):
        monkeypatch.setenv(key, "leak")
    env = smoke._clean_environment()
    for key in ("BIODATA_PYTHON", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "PORT"):
        assert key not in env
    assert env["BIODATA_LAUNCH_PROBE"] == "1"
    assert env["BIODATA_SKIP_SETUP"] == "1"
    assert env["BIODATA_NO_BROWSER"] == "1"


def test_run_smoke_refuses_cross_platform_claim(tmp_path: Path):
    other = next(name for name in ("windows", "macos", "linux") if name != smoke._native_platform())
    with pytest.raises(RuntimeError, match="native runner"):
        smoke.run_smoke(other, tmp_path, 60)


def test_script_is_shipped_by_release_allowlist():
    builder = smoke._load_builder()
    candidates = {
        path.resolve().relative_to(ROOT).as_posix()
        for path in builder._iter_allowed_candidates(ROOT)
    }
    assert "scripts/smoke_release_first_launch.py" in candidates
