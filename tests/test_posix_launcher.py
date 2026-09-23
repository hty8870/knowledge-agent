"""mac/Linux 一键启动链的不变量门。

覆盖四类可静态/半静态验证的契约（真实交互起服无法在 Windows/Git Bash 跑，故用 shim 与
fixture 覆盖逻辑分支）：

1. **bash -n 语法**：launch_web.sh + 两个根入口脚本必须能被 bash/`sh` 解析（没有 bash 就 skip）。
2. **五级根定位**：打开前端.sh 必须在「同层 / biodata-agent 子目录 / 任一子目录 / 多套一层 /
   仓库克隆的 launchers/ 下」五种布局下都定位到项目根（tmp_path 造 fixture 实测）。
3. **python 探测**：用假 python3 shim（临时 PATH）验证 `resolve_base_python` 接受 >=3.10、
   拒绝 <3.10，以及 `BIODATA_PYTHON` 显式覆盖校验。
4. **RC zip 契约**：build_release 包内脚本必须有正确 mode（.sh/.command = 0o100755，其余 0o100644）。

与 test_launcher_first_run_setup.py 的边界：测试对象不同（前者钉 launch_web.ps1，本文件钉
launch_web.sh / 打开前端.*），互不重叠，避免各自回归被对方漏掉。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "scripts" / "launch_web.sh"
ENTRY_SH = ROOT / "launchers" / "打开前端.sh"
ENTRY_CMD = ROOT / "launchers" / "打开前端.command"

SCRIPT = ROOT / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
assert SPEC and SPEC.loader
build_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_release
SPEC.loader.exec_module(build_release)

BASH = shutil.which("bash")
SH = shutil.which("sh")


def _msys(path: Path) -> str:
    """把 Windows 绝对路径转成 Git Bash (MSYS) 的 POSIX 路径，供 bash 脚本当 $0 使用。"""
    s = os.path.normpath(str(path)).replace("\\", "/")
    m = re.match(r"^([A-Za-z]):(/.+)$", s)
    if m:
        return f"/{m.group(1).lower()}{m.group(2)}"
    return s


# ----------------------------------------------------------------- ① bash -n 语法


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_posix_scripts_pass_bash_syntax_check() -> None:
    for path in (LAUNCHER, ENTRY_SH, ENTRY_CMD):
        result = subprocess.run(
            [BASH, "-n", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


@pytest.mark.skipif(SH is None, reason="sh not available")
def test_entry_scripts_pass_sh_syntax_check() -> None:
    """入口脚本必须 POSIX 兼容——`sh 打开前端.sh` 在工作环境（dash）下也要能跑。"""
    for path in (ENTRY_SH, ENTRY_CMD):
        result = subprocess.run(
            [SH, "-n", str(path)], capture_output=True, text=True
        )
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


# ----------------------------------------------------------------- ② 四级根定位


def _plant_layout(base: Path, kind: str) -> Path:
    """按布局类型在 base 下造目录树，并放置打开的入口脚本副本。返回「脚本所在目录」。"""
    base.mkdir(parents=True, exist_ok=True)

    def project(proj: Path) -> None:
        (proj / "scripts").mkdir(parents=True, exist_ok=True)
        (proj / "scripts" / "run_web.py").write_text("print('ok')\n", encoding="utf-8")

    if kind == "repo_launchers":
        # 仓库克隆布局（一级目录整理）：入口在项目根的 launchers/ 下。
        script_dir = base / "launchers"
        script_dir.mkdir()
    else:
        script_dir = base
    shutil.copyfile(ENTRY_SH, script_dir / "打开前端.sh")

    if kind in ("same_level", "repo_launchers"):
        project(base)                      # 入口与项目同层 / 项目根的 launchers/ 下
    elif kind == "biodata_subdir":
        project(base / "biodata-agent")        # 提交包布局 <包>\biodata-agent\
    elif kind == "renamed_subdir":
        project(base / "renamed-project")      # 改名/不同名解压，任一子目录
    elif kind == "one_extra_nesting":
        project(base / "unpacked" / "biodata-agent")  # 多套一层
    else:
        raise ValueError(kind)
    return script_dir


@pytest.mark.skipif(BASH is None, reason="bash not available")
@pytest.mark.parametrize(
    "kind,expected_rel",
    [
        ("same_level", "."),
        ("biodata_subdir", "biodata-agent"),
        ("renamed_subdir", "renamed-project"),
        ("one_extra_nesting", "unpacked/biodata-agent"),
        ("repo_launchers", ".."),
    ],
)
def test_root_locate_finds_project_in_all_layouts(
    tmp_path: Path, kind: str, expected_rel: str
) -> None:
    script_dir = _plant_layout(tmp_path / kind, kind)
    entry = script_dir / "打开前端.sh"
    result = subprocess.run(
        [BASH, _msys(entry)],
        env={**os.environ, "BIODATA_LOCATE_ONLY": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    resolved = (script_dir / expected_rel).resolve()
    assert result.stdout.strip() == _msys(resolved)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_root_locate_fails_when_project_absent(tmp_path: Path) -> None:
    base = tmp_path / "empty"
    base.mkdir(parents=True)
    shutil.copyfile(ENTRY_SH, base / "打开前端.sh")
    result = subprocess.run(
        [BASH, _msys(base / "打开前端.sh")],
        env={**os.environ, "BIODATA_LOCATE_ONLY": "1"},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "not found" in result.stderr


# ----------------------------------------------------------------- ③ python 探测 shim


def _run_bash(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [BASH, "-c", script], env=env, capture_output=True, text=True
    )


def _probe_driver() -> str:
    """driver：source launch_web.sh 后关闭严格模式，调用 resolve_base_python。"""
    return (
        f'source "{_msys(LAUNCHER)}"\n'
        "set +euo pipefail\n"
        'res="$(resolve_base_python 2>/dev/null)"\n'
        "rc=$?\n"
        'echo "RC=$rc"\n'
        'echo "RES=$res"\n'
    )


def _parse_probe(stdout: str) -> tuple[int, str]:
    lines = stdout.splitlines()
    rc_str = lines[0].split("=", 1)[1] if lines and lines[0].startswith("RC=") else "-1"
    res = lines[1].split("=", 1)[1] if len(lines) > 1 and lines[1].startswith("RES=") else ""
    return int(rc_str), res


def _write_shim(bin_dir: Path, name: str, body: str) -> None:
    path = bin_dir / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_python_probe_accepts_good_and_rejects_bad(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    # 接受：假装 >=3.10 的 python（版本检查 exit 0）。
    _write_shim(bin_dir, "python3", "#!/bin/sh\nexit 0\n")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = _run_bash(_probe_driver(), env)
    assert out.returncode == 0, out.stderr
    rc, res = _parse_probe(out.stdout)
    assert rc == 0
    # 命中 PATH 里的 python3 候选，而非 /opt/homebrew 等绝对候补。
    assert res and res.endswith("/bin/python3")
    assert "/opt/homebrew" not in res and "/usr/local" not in res

    # 拒绝：假装 <3.10 的 python（版本检查 exit 9）。python3/python 两个名字都要
    # 钉死——CI runner 的 PATH 里有 setup-python 装的真 python，只 shim python3 会被
    # 真 python 兜底接住，导致拒绝分支在 CI 上假绿（CI 实证）。
    _write_shim(bin_dir, "python3", "#!/bin/sh\nexit 9\n")
    _write_shim(bin_dir, "python", "#!/bin/sh\nexit 9\n")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = _run_bash(_probe_driver(), env)
    assert out.returncode == 0, out.stderr
    rc, res = _parse_probe(out.stdout)
    assert rc == 1
    assert res == ""


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_python_probe_prefers_path_before_absolute_candidates(tmp_path: Path) -> None:
    """PATH 里的可用 python3 应优先于 /opt/homebrew 候补。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, "python3", "#!/bin/sh\nexit 0\n")
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = _run_bash(_probe_driver(), env)
    rc, res = _parse_probe(out.stdout)
    assert rc == 0
    assert res and res.endswith("/bin/python3")
    assert "/opt/homebrew" not in res and "/usr/local" not in res


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_python_probe_respects_explicit_biodata_python(tmp_path: Path) -> None:
    """BIODATA_PYTHON 显式覆盖：指向可用 python 则采纳，指向不可用则报错返回非零。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_shim(bin_dir, "good", "#!/bin/sh\nexit 0\n")
    _write_shim(bin_dir, "bad", "#!/bin/sh\nexit 9\n")

    good_env = {**os.environ, "BIODATA_PYTHON": _msys(bin_dir / "good")}
    out = _run_bash(_probe_driver(), good_env)
    assert out.returncode == 0 and out.stdout.splitlines()[0] == "RC=0"
    assert out.stdout.splitlines()[1] == f"RES={_msys(bin_dir / 'good')}"

    bad_env = {**os.environ, "BIODATA_PYTHON": _msys(bin_dir / "bad")}
    out = _run_bash(_probe_driver(), bad_env)
    assert out.returncode == 0
    assert out.stdout.splitlines()[0] == "RC=1"
    assert out.stdout.splitlines()[1] == "RES="


def test_posix_launcher_does_not_silently_reuse_dev_or_mcp_venvs() -> None:
    src = LAUNCHER.read_text(encoding="utf-8")
    assert "workspace_python" not in src
    assert "shared_python" not in src
    assert ".venv-biodata-mcp" not in src
    assert "BIODATA_PYTHON" in src


def test_posix_first_launch_probe_requires_clean_runtime_without_pytest() -> None:
    src = LAUNCHER.read_text(encoding="utf-8")
    assert '${BIODATA_LAUNCH_PROBE:-0}' in src
    assert "BIODATA_LAUNCH_PROBE_OK" in src
    assert "-c 'import pytest'" in src
    probe = src.index('${BIODATA_LAUNCH_PROBE:-0}')
    wizard = src.index('invoke_first_run_setup "$root" "$python"', probe)
    assert probe < wizard


# ----------------------------------------------------------------- ④ RC zip 契约


def _minimal_project(root: Path) -> None:
    """造一个 build_release 能通过的极简项目（复刻 test_release_builder._minimal_project）。"""
    (root / "src" / "dataset_recommender" / "app").mkdir(parents=True)
    (root / "web" / "static").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "src" / "dataset_recommender" / "app" / "mcp_server.py").write_text(
        "print('ok')\n", encoding="utf-8"
    )
    (root / "src" / "dataset_recommender" / "__init__.py").write_text(
        "VERSION = 1\n", encoding="utf-8"
    )
    (root / "src" / "dataset_recommender" / "app" / "webapp.py").write_text(
        'WEB_API_VERSION = "1.2.0"\n', encoding="utf-8"
    )
    (root / "web" / "static" / "index.html").write_text(
        "<!doctype html>\n", encoding="utf-8"
    )
    (root / "scripts" / "smoke_test.py").write_text("print('pass')\n", encoding="utf-8")
    (root / "scripts" / "web_smoke_test.py").write_text("print('pass')\n", encoding="utf-8")
    (root / "scripts" / "launch_web.ps1").write_text("# launcher\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    (root / "requirements").mkdir()
    (root / "requirements" / "requirements.txt").write_text("pytest==9.1.1\n", encoding="utf-8")
    (root / "requirements" / "requirements-ci.lock").write_text("pytest==9.1.1\n", encoding="utf-8")
    (root / "launchers").mkdir()
    (root / "launchers" / "start-web.bat").write_text("@echo off\n", encoding="utf-8")
    (root / "automation").mkdir()
    (root / "automation" / "quality-gates.json").write_text("{}\n", encoding="utf-8")
    (root / "docs").mkdir()
    (root / "docs" / "AUTOMATION_AND_RELEASE.md").write_text(
        "# Automation\n", encoding="utf-8"
    )
    (root / "database" / "base").mkdir(parents=True)
    (root / "database" / "base" / "10x-Visium.json").write_text("[]\n", encoding="utf-8")
    for relative in build_release.PUBLIC_EXTERNAL_FILES:
        path = root.joinpath(*Path(relative).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public\n" if path.suffix == ".md" else "[]\n", encoding="utf-8")
    for relative in build_release.EVAL_INPUT_FILES | {"database/SOURCES.yml"}:
        source = build_release.source_relpath(relative)
        path = root.joinpath(*Path(source).parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sources: []\n" if path.suffix == ".yml" else "{}\n", encoding="utf-8")

    # mac/Linux 一键启动链（本次新增的可执行条目 + 既有 run_web.sh）。
    (root / "scripts" / "run_web.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "scripts" / "launch_web.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (root / "launchers" / "打开前端.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "launchers" / "打开前端.command").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "launchers" / "打开前端.bat").write_text("@echo off\n", encoding="utf-8")


def _entry_mode(info: zipfile.ZipInfo) -> int:
    return (info.external_attr >> 16) & 0xFFFF


def test_release_zip_preserves_executable_mode(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    assert result["ok"] is True

    with zipfile.ZipFile(result["archive"]) as bundle:
        infos = {i.filename: i for i in bundle.infolist()}

    for rel in build_release.RELEASE_EXECUTABLE_FILES:
        assert rel in infos, f"release zip missing executable entry: {rel}"
        assert _entry_mode(infos[rel]) == 0o100755, f"{rel} should be 0o100755"

    for rel in ("README.md", "scripts/launch_web.ps1", "打开前端.bat"):
        assert rel in infos, f"release zip missing non-executable entry: {rel}"
        assert _entry_mode(infos[rel]) == 0o100644, f"{rel} should stay 0o100644"


def test_executable_allowlist_matches_real_scripts() -> None:
    """可执行白名单必须与本仓库真实存在的脚本一一对应，防止列错路径。

    白名单按包内（历史布局）路径组织；仓库源路径经 source_relpath 解析（一级目录整理：打开前端.* 在 launchers/ 下）。
    """
    for rel in build_release.RELEASE_EXECUTABLE_FILES:
        path = ROOT.joinpath(*Path(build_release.source_relpath(rel)).parts)
        assert path.is_file(), f"RELEASE_EXECUTABLE_FILES entry not in repo: {rel}"


# ----------------------------------------------------------------- ⑤ assemble.py 基本契约


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_assemble_py_produces_exec_preserving_zip(tmp_path: Path) -> None:
    """assemble.py 应从 RC zip + PDF 产出 mac/Linux 交付 zip，且入口/引擎脚本 exec 位保留。"""
    import importlib.util as ilu

    asm_script = ROOT / "packaging" / "delivery-posix" / "assemble.py"
    spec = ilu.spec_from_file_location("asm", asm_script)
    assert spec and spec.loader
    asm = ilu.module_from_spec(spec)
    sys.modules["asm"] = asm
    spec.loader.exec_module(asm)

    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    rc_result = build_release.build_release(root, tmp_path / "rc")
    pdf = tmp_path / "BioData Agent 使用说明书.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    archive = asm.build_delivery(
        Path(rc_result["archive"]),
        pdf,
        tmp_path / "deliver",
        date_str="2026-08-24",
    )
    assert archive.is_file()
    with zipfile.ZipFile(archive) as bundle:
        infos = {i.filename: _entry_mode(i) for i in bundle.infolist()}
    assert infos["打开前端.sh"] == 0o100755
    assert infos["打开前端.command"] == 0o100755
    assert infos["biodata-agent/scripts/run_web.sh"] == 0o100755
    assert infos["biodata-agent/scripts/launch_web.sh"] == 0o100755
    assert infos["biodata-agent/打开前端.sh"] == 0o100755
    assert infos["biodata-agent/README.md"] == 0o100644
    assert infos["从这里开始.txt"] == 0o100644
    assert infos["BioData Agent 使用说明书.pdf"] == 0o100644
