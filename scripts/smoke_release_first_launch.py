#!/usr/bin/env python3
"""Build, extract, and first-launch the source zip on the current native OS.

This is an opt-in release/CI smoke, not part of normal startup.  It exercises the
customer entry point for the selected platform, lets that entry point create a
fresh project-local ``.venv`` and install ``requirements.txt``, then requires the
launcher probe to prove that runtime imports work and the dev-only ``pytest``
package is absent.

Run one platform per native runner; do not claim macOS coverage from Git Bash on
Windows.  The CI matrix invokes this file independently on Windows, macOS, Linux.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BUILD_RELEASE = ROOT / "scripts" / "build_release.py"
MARKER = "BIODATA_LAUNCH_PROBE_OK"
RUNTIME_IMPORTS = "import dotenv, fastapi, httpx, multipart, uvicorn"


def _native_platform() -> str:
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    raise RuntimeError(f"unsupported smoke platform: {sys.platform}")


def _load_builder():
    spec = importlib.util.spec_from_file_location("runtime_smoke_build_release", BUILD_RELEASE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load release builder: {BUILD_RELEASE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _extract_with_modes(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(destination)
        if os.name != "nt":
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o777
                if mode:
                    destination.joinpath(*Path(info.filename).parts).chmod(mode)


def _entry_command(platform: str, root: Path) -> list[str]:
    if platform == "windows":
        return ["cmd.exe", "/d", "/c", str(root / "打开前端.bat")]
    if platform == "macos":
        return ["/bin/bash", str(root / "打开前端.command")]
    return ["/bin/sh", str(root / "打开前端.sh")]


def _clean_environment() -> dict[str, str]:
    env = dict(os.environ)
    for key in (
        "BIODATA_PYTHON",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "VIRTUAL_ENV_PROMPT",
        "PORT",
    ):
        env.pop(key, None)
    env.update(
        {
            "BIODATA_LAUNCH_PROBE": "1",
            "BIODATA_SKIP_SETUP": "1",
            "BIODATA_NO_BROWSER": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PYTHONUTF8": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def _tail(text: str, lines: int = 80) -> str:
    return "\n".join(text.splitlines()[-lines:])


def run_smoke(platform: str, work_root: Path, timeout_s: int) -> dict[str, object]:
    native = _native_platform()
    if platform != native:
        raise RuntimeError(
            f"requested {platform!r} smoke on {native!r}; use a native runner for honest coverage"
        )

    builder = _load_builder()
    release_dir = work_root / "release"
    extracted = work_root / "extracted"
    result = builder.build_release(
        ROOT,
        release_dir,
        archive_name=f"biodata-agent-first-launch-{platform}.zip",
    )
    archive = Path(str(result["archive"]))
    _extract_with_modes(archive, extracted)

    command = _entry_command(platform, extracted)
    launched = subprocess.run(
        command,
        cwd=extracted,
        env=_clean_environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
    )
    combined = f"{launched.stdout}\n{launched.stderr}"
    if launched.returncode != 0 or MARKER not in combined:
        raise RuntimeError(
            "first-launch entry point failed\n"
            f"command={command!r}\nreturncode={launched.returncode}\n"
            f"output_tail=\n{_tail(combined)}"
        )

    runtime_python = (
        extracted / ".venv" / "Scripts" / "python.exe"
        if platform == "windows"
        else extracted / ".venv" / "bin" / "python"
    )
    if not runtime_python.is_file():
        raise RuntimeError(f"project-local runtime Python was not created: {runtime_python}")

    runtime = subprocess.run(
        [str(runtime_python), "-c", RUNTIME_IMPORTS],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if runtime.returncode != 0:
        raise RuntimeError(f"runtime dependency import failed: {_tail(runtime.stderr)}")
    pytest_probe = subprocess.run(
        [str(runtime_python), "-c", "import pytest"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if pytest_probe.returncode == 0:
        raise RuntimeError("dev-only pytest is importable from the clean runtime venv")

    return {
        "ok": True,
        "platform": platform,
        "archive": archive.name,
        "entry": command[-1],
        "runtime_python": str(runtime_python.relative_to(extracted)),
        "runtime_imports": "ok",
        "pytest": "absent",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--platform",
        choices=("windows", "macos", "linux"),
        default=_native_platform(),
    )
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="keep/use this directory instead of an automatically removed temporary directory",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.timeout < 60:
        print(json.dumps({"ok": False, "error": "--timeout must be >= 60"}), file=sys.stderr)
        return 2
    try:
        if args.work_dir is not None:
            work_root = args.work_dir.resolve()
            if work_root.exists() and any(work_root.iterdir()):
                raise RuntimeError(f"--work-dir must be empty: {work_root}")
            work_root.mkdir(parents=True, exist_ok=True)
            result = run_smoke(args.platform, work_root, args.timeout)
        else:
            with tempfile.TemporaryDirectory(prefix="biodata-first-launch-") as temp:
                result = run_smoke(args.platform, Path(temp), args.timeout)
    except (OSError, RuntimeError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
