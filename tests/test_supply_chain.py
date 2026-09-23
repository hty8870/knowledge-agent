# -*- coding: utf-8 -*-
"""供应链契约测试：SBOM / THIRD_PARTY_NOTICES / sidecar / unsigned-dev 命名强制。

全部 hermetic（不依赖 build-venv 存在）：锁文件从仓库读取，许可证元数据用
tmp_path 伪造的 site-packages / dist-info，签名命名逻辑直接测函数与源码静态断言。
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_supply_chain as bsc  # noqa: E402

LOCK = ROOT / "packaging" / "requirements" / "runtime-win-x64.lock"


def _lock_text() -> str:
    return LOCK.read_text(encoding="utf-8")


def _locked() -> list[bsc.LockedPackage]:
    return bsc.parse_lock(_lock_text())


def _fake_installed(locked: list[bsc.LockedPackage]) -> dict[str, bsc.InstalledPackage]:
    return {
        bsc._normalize_dist(pkg.name): bsc.InstalledPackage(
            name=pkg.name, version=pkg.version, licenses=["MIT"]
        )
        for pkg in locked
    }


# ── lock 解析 ──────────────────────────────────────────────────────────────

def test_lock_parsing_covers_every_pin_line():
    locked = _locked()
    pin_count = sum(
        1
        for line in _lock_text().splitlines()
        if "==" in line.strip()
        and not line.strip().startswith("#")
        and not line.strip().startswith("--")
    )
    assert pin_count >= 50  # 锁文件规模下限，防整块依赖丢失
    assert len(locked) == pin_count
    names = {pkg.name for pkg in locked}
    assert len(names) == len(locked), "lock contains duplicate package names"
    for pkg in locked:
        assert re.fullmatch(r"[0-9a-z._-]+", pkg.name, re.IGNORECASE)
        assert re.fullmatch(r"[0-9][0-9a-zA-Z.+-]*", pkg.version)
        assert pkg.hashes, f"{pkg.name} 无 SHA-256"
        assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in pkg.hashes)


def test_lock_parser_rejects_malformed_input():
    import pytest

    with pytest.raises(ValueError):
        bsc.parse_lock("no-equals-line\n")
    with pytest.raises(ValueError):
        bsc.parse_lock("--hash=sha256:xyz\n")
    with pytest.raises(ValueError):
        bsc.parse_lock("foo==1.0\n")  # 无 hash → fail-closed


# ── SBOM 形状 ──────────────────────────────────────────────────────────────

def test_sbom_shape_covers_all_locked_dependencies():
    locked = _locked()
    sbom = bsc.build_sbom(
        locked,
        _fake_installed(locked),
        tool_versions={"pyinstaller": "6.22.2", "innosetup": bsc.INNO_VERSION},
        timestamp="2026-08-20T00:00:00Z",
    )
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    assert sbom["serialNumber"].startswith("urn:uuid:")
    assert sbom["metadata"]["component"]["name"] == "BioDataAgent"
    assert sbom["metadata"]["timestamp"] == "2026-08-20T00:00:00Z"
    tool_names = {t["name"] for t in sbom["metadata"]["tools"]["components"]}
    assert tool_names == {"pyinstaller", "innosetup"}

    components = {c["name"]: c for c in sbom["components"]}
    assert len(components) == len(locked)
    for pkg in locked:
        component = components[pkg.name]
        assert component["type"] == "library"
        assert component["version"] == pkg.version
        assert component["purl"] == f"pkg:pypi/{re.sub(r'[-_.]+', '_', pkg.name).lower()}@{pkg.version}"
        assert [h["content"] for h in component["hashes"]] == pkg.hashes
        assert all(h["alg"] == "SHA-256" for h in component["hashes"])
        assert component["licenses"], f"{pkg.name} 缺许可证条目"
        assert component["licenses"][0]["license"].get("id") == "MIT"


def test_sbom_purl_is_normalized():
    assert bsc._pypi_purl("Foo-Bar.baz", "1.2.3") == "pkg:pypi/foo_bar_baz@1.2.3"


# ── THIRD_PARTY_NOTICES ────────────────────────────────────────────────────

def test_notices_include_every_locked_dependency_with_version_and_license():
    locked = _locked()
    sbom = bsc.build_sbom(
        locked,
        _fake_installed(locked),
        tool_versions={"pyinstaller": "6.22.2", "innosetup": bsc.INNO_VERSION},
    )
    notices = bsc.render_notices(sbom, header_note="test header")
    assert "test header" in notices
    for pkg in locked:
        line = f"- {pkg.name}=={pkg.version} — MIT"
        assert line in notices, f"NOTICEs 缺少锁内依赖：{pkg.name}"
    assert "- pyinstaller==6.22.2 (build-time)" in notices
    assert "THIRD PARTY NOTICES" in notices


# ── SHA-256 sidecar ────────────────────────────────────────────────────────

def test_sha256_sidecar_format_and_roundtrip(tmp_path):
    target = tmp_path / "BioDataAgent-Setup-1.0.0-unsigned-dev.exe"
    payload = b"installer bytes" * 1000
    target.write_bytes(payload)

    line = bsc.sha256sum_line(hashlib.sha256(payload).hexdigest(), target.name)
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)\n", line)
    assert match is not None
    assert match.group(2) == target.name

    sidecar = bsc.write_sha256_sidecar(target, tmp_path)
    assert sidecar == tmp_path / f"{target.name}.sha256"
    content = sidecar.read_text(encoding="ascii")
    assert content == f"{hashlib.sha256(payload).hexdigest()}  {target.name}\n"
    assert not re.search(r"[\\/]", content), "sidecar 不得含路径分隔符"


def test_sha256_sidecar_rejects_bad_digest_and_missing_file(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        bsc.sha256sum_line("short", "x.exe")
    with pytest.raises(ValueError):
        bsc.write_sha256_sidecar(tmp_path / "missing.exe", tmp_path)


# ── unsigned-dev 命名强制（签名合同）────────────────────────────────────────

def test_unsigned_dev_marker_is_enforced_when_no_signing_credentials():
    # 无凭据：原名自动补 unsigned-dev
    name = bsc.installer_artifact_name("BioDataAgent-Setup-1.0.0", has_signing_credentials=False)
    assert "unsigned-dev" in name
    # 有凭据：保持原名
    signed = bsc.installer_artifact_name("BioDataAgent-Setup-1.0.0", has_signing_credentials=True)
    assert "unsigned-dev" not in signed


def test_unsigned_dev_naming_assert_fails_closed():
    import pytest

    assert bsc.assert_unsigned_dev_naming("x-unsigned-dev.exe", has_signing_credentials=False) == "x-unsigned-dev.exe"
    assert bsc.assert_unsigned_dev_naming("x.exe", has_signing_credentials=True) == "x.exe"
    with pytest.raises(ValueError):
        bsc.assert_unsigned_dev_naming("x.exe", has_signing_credentials=False)


def test_unsigned_dev_marker_is_statically_present_in_build_script():
    """静态断言 build 脚本逻辑：删除命名强制会让本测试红。"""
    source = Path(bsc.__file__).read_text(encoding="utf-8")
    assert 'UNSIGNED_DEV_MARKER = "unsigned-dev"' in source
    assert "def installer_artifact_name" in source
    assert "def assert_unsigned_dev_naming" in source
    assert "禁止自签冒充正式" in (ROOT / "packaging" / "signing" / "README.md").read_text(encoding="utf-8")


def test_inno_version_placeholder_removed():
    """PENDING_W4_MERGE 占位必须已补实（Inno 6.7.3，官方 jrsoftware release）。"""
    source = Path(bsc.__file__).read_text(encoding="utf-8")
    assert "PENDING_W4_MERGE" not in source
    assert 'INNO_VERSION = "6.7.3"' in source
    assert "jrsoftware" in source


# ── 许可证元数据解析（伪造 site-packages，hermetic）────────────────────────

def _write_dist_info(site: Path, dist_name: str, version: str, metadata: str) -> None:
    dist_dir = site / f"{dist_name}-{version}.dist-info"
    dist_dir.mkdir(parents=True)
    (dist_dir / "METADATA").write_text(metadata, encoding="utf-8")


def test_installed_metadata_resolves_license_in_priority_order(tmp_path):
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(
        site, "modern_pkg", "1.0.0",
        "Metadata-Version: 2.4\nName: modern-pkg\nVersion: 1.0.0\nLicense-Expression: MPL-2.0\n",
    )
    _write_dist_info(
        site, "legacy_pkg", "2.0.0",
        "Metadata-Version: 2.1\nName: legacy-pkg\nVersion: 2.0.0\nLicense: BSD-3-Clause\n",
    )
    _write_dist_info(
        site, "classifier_pkg", "3.0.0",
        "Metadata-Version: 2.1\nName: classifier-pkg\nVersion: 3.0.0\n"
        "Classifier: License :: OSI Approved :: Apache Software License\n",
    )
    _write_dist_info(
        site, "bare_pkg", "4.0.0",
        "Metadata-Version: 2.1\nName: bare-pkg\nVersion: 4.0.0\n",
    )

    installed = bsc.collect_installed_metadata(tmp_path / "venv")
    assert installed["modern-pkg"].licenses == ["MPL-2.0"]
    assert installed["legacy-pkg"].licenses == ["BSD-3-Clause"]
    assert installed["classifier-pkg"].licenses == ["Apache Software License"]
    assert installed["bare-pkg"].licenses == ["UNKNOWN"]


def test_license_file_copy_lands_in_out_dir(tmp_path):
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    dist_dir = site / "certifi-2026.7.22.dist-info"
    dist_dir.mkdir()
    (dist_dir / "LICENSE").write_text("certifi license text\n", encoding="utf-8")
    licenses_dir = dist_dir / "licenses"
    licenses_dir.mkdir()
    (licenses_dir / "BSD.txt").write_text("BSD text\n", encoding="utf-8")

    locked = [bsc.LockedPackage(name="certifi", version="2026.7.22", hashes=["a" * 64])]
    copied = bsc.copy_license_files(tmp_path / "venv", locked, tmp_path / "out")
    assert (tmp_path / "out" / "licenses" / "certifi" / "LICENSE").read_text(encoding="utf-8") == "certifi license text\n"
    assert (tmp_path / "out" / "licenses" / "certifi" / "BSD.txt").read_text(encoding="utf-8") == "BSD text\n"
    assert len(copied) == 2


# ── 构建工具记录 ────────────────────────────────────────────────────────────

def test_build_tools_record_has_pyinstaller_and_inno_6373(tmp_path):
    venv = tmp_path / "venv"
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(
        site, "pyinstaller", "6.22.2",
        "Metadata-Version: 2.1\nName: PyInstaller\nVersion: 6.22.2\nLicense: GPL-2.0-or-later\n",
    )
    installed = bsc.collect_installed_metadata(venv)
    record = bsc.build_tools_record(build_venv=venv, installed=installed, lock=LOCK)
    tools = {t["name"]: t["version"] for t in record["tools"]}
    assert tools["pyinstaller"] == "6.22.2"
    assert tools["innosetup"] == "6.7.3"   # 占位 PENDING_W4_MERGE 已补实（官方 jrsoftware release）
    assert record["lock_file"].endswith("runtime-win-x64.lock")
    assert re.fullmatch(r"[0-9a-f]{64}", record["lock_sha256"])


# ── CLI 冒烟（伪造 venv，真跑入口）─────────────────────────────────────────

def test_cli_smoke_with_fake_build_venv(tmp_path):
    site = tmp_path / "venv" / "Lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(
        site, "certifi", "2026.7.22",
        "Metadata-Version: 2.1\nName: certifi\nVersion: 2026.7.22\nLicense: MPL-2.0\n",
    )
    out = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_supply_chain.py"),
            "--build-venv", str(tmp_path / "venv"),
            "--out", str(out),
            "--skip-license-copy",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sbom = json.loads((out / "biodata-sbom.cdx.json").read_text(encoding="utf-8"))
    assert sbom["bomFormat"] == "CycloneDX"
    assert len(sbom["components"]) == len(_locked())
    assert (out / "THIRD_PARTY_NOTICES.txt").is_file()
    assert (out / "build-tools.json").is_file()
    assert (out / "installer-verification-report.json.template").is_file()
    tools_record = json.loads((out / "build-tools.json").read_text(encoding="utf-8"))
    assert tools_record["tools"][1]["name"] == "innosetup"
    assert tools_record["tools"][1]["version"] == "6.7.3"   # 占位已补实
    assert "jrsoftware" in tools_record["tools"][1]["source"]


# ── 随包 uv 与在线模型 lock 登记（供应链诚实收口）─────────────────────

MODEL_LOCK = ROOT / "packaging" / "requirements" / "model-win-x64.lock"


def test_model_lock_exists_and_covers_heavy_model_dependencies():
    assert MODEL_LOCK.is_file()
    model_locked = bsc.parse_lock(MODEL_LOCK.read_text(encoding="utf-8"))
    assert len(model_locked) >= 30  # 35 包：torch/transformers/sentence-transformers/modelscope 等
    names = {pkg.name for pkg in model_locked}
    assert {"torch", "transformers", "sentence-transformers", "modelscope"} <= names


def test_model_sbom_lists_model_lock_packages_and_honest_network_sources():
    model_locked = bsc.parse_lock(MODEL_LOCK.read_text(encoding="utf-8"))
    sbom = bsc.build_model_sbom(model_locked, timestamp="2026-08-21T00:00:00Z")
    assert sbom["bomFormat"] == "CycloneDX"
    assert len(sbom["components"]) == len(model_locked)
    for component, pkg in zip(sbom["components"], model_locked):
        assert component["name"] == pkg.name
        assert [h["content"] for h in component["hashes"]] == pkg.hashes
    props = {p["name"]: p["value"] for p in sbom["metadata"]["properties"]}
    note = props["biodata:network-sources"]
    for source in ("PyPI", "ModelScope", "HuggingFace", "CPython"):
        assert source in note
    assert "并非所有外部字节都由仓库 lock 直接覆盖" in note


def test_build_tools_record_registers_uv_and_model_lock(tmp_path):
    venv = tmp_path / "venv"
    site = venv / "Lib" / "site-packages"
    site.mkdir(parents=True)
    _write_dist_info(
        site, "pyinstaller", "6.22.2",
        "Metadata-Version: 2.1\nName: PyInstaller\nVersion: 6.22.2\nLicense: GPL-2.0-or-later\n",
    )
    _write_dist_info(
        site, "uv", "0.11.33",
        "Metadata-Version: 2.1\nName: uv\nVersion: 0.11.33\nLicense: MIT\n",
    )
    installed = bsc.collect_installed_metadata(venv)
    record = bsc.build_tools_record(
        build_venv=venv, installed=installed, lock=LOCK, model_lock=MODEL_LOCK
    )
    tools = {t["name"]: t for t in record["tools"]}
    assert tools["uv"]["version"] == "0.11.33"
    assert "tools/uv.exe" in tools["uv"]["role"]
    assert record["local_model"]["package_count"] == len(
        bsc.parse_lock(MODEL_LOCK.read_text(encoding="utf-8"))
    )
    assert record["local_model"]["lock_file"].endswith("model-win-x64.lock")
    assert re.fullmatch(r"[0-9a-f]{64}", record["local_model"]["lock_sha256"])
    for source in ("PyPI", "ModelScope", "HuggingFace", "CPython"):
        assert source in record["local_model"]["network_sources"]


def test_runtime_build_verifies_frozen_local_model_assets(tmp_path):
    import pytest

    import build_windows_runtime as bwr

    app_dir = tmp_path / "BioDataAgent"
    for rel in bwr._LOCAL_MODEL_ASSETS:
        target = app_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    bwr._verify_local_model_assets(app_dir)  # 三件齐全 → 不抛

    (app_dir / "_internal" / "tools" / "model_worker.py").unlink()
    with pytest.raises(SystemExit):
        bwr._verify_local_model_assets(app_dir)  # 缺 worker → fail-closed


def test_installer_build_verifies_frozen_local_model_assets(tmp_path):
    import build_windows_installer as bwi

    runtime_dir = tmp_path / "BioDataAgent"
    for rel in bwi._LOCAL_MODEL_ASSETS:
        target = runtime_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    assert bwi.verify_local_model_assets(runtime_dir) == []  # 三件齐全 → 空

    (runtime_dir / "_internal" / "tools" / "uv.exe").unlink()
    assert bwi.verify_local_model_assets(runtime_dir) == ["_internal/tools/uv.exe"]


# ── 构建验证：加 MCP 真协议自检（--selfcheck 探测，fail-closed）──────────────

class _FakeSubprocessResult:
    """够用的 subprocess.CompletedProcess 替身（只提供本组用到的字段）。"""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_app_dir(tmp_path: Path, with_mcp_exe: bool = True) -> Path:
    app_dir = tmp_path / "BioDataAgent"
    app_dir.mkdir(parents=True, exist_ok=True)
    if with_mcp_exe:
        (app_dir / "BioDataAgentMCP.exe").write_bytes(b"fake exe")
    return app_dir


def test_runtime_build_runs_mcp_selfcheck_and_passes_on_selfcheck_ok(tmp_path, monkeypatch):
    import build_windows_runtime as bwr

    app_dir = _make_app_dir(tmp_path)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return _FakeSubprocessResult(
            0, "  [PASS] tools/list 可见 19 个工具\nSELFCHECK_OK tools=19 corpus_total=8022\n"
        )

    monkeypatch.setattr(bwr.subprocess, "run", fake_run)
    bwr.verify_mcp_selfcheck(app_dir)  # 不抛 = 通过

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == [str(app_dir / "BioDataAgentMCP.exe"), "--selfcheck"]
    assert kwargs["timeout"] == bwr._MCP_SELFCHECK_TIMEOUT
    # 数据根必须重定向到临时目录（frozen 只读语料从 _MEIPASS 读，构建机不被写实例数据）
    data_root = str(kwargs["env"]["BIODATA_DATA_ROOT"])
    assert data_root and "biodata-mcp-selfcheck-" in data_root


def test_runtime_build_mcp_selfcheck_fails_closed_on_bad_exit_or_missing_marker(tmp_path, monkeypatch):
    import pytest

    import build_windows_runtime as bwr

    app_dir = _make_app_dir(tmp_path)
    # 退出码 0 但无 SELFCHECK_OK → fail-closed
    monkeypatch.setattr(bwr.subprocess, "run",
                        lambda *a, **k: _FakeSubprocessResult(0, "[biodata-mcp] 自检中 …"))
    with pytest.raises(SystemExit):
        bwr.verify_mcp_selfcheck(app_dir)
    # 非零退出 + SELFCHECK_OK 字样 → fail-closed
    monkeypatch.setattr(bwr.subprocess, "run",
                        lambda *a, **k: _FakeSubprocessResult(1, "SELFCHECK_FAIL 协议握手失败"))
    with pytest.raises(SystemExit):
        bwr.verify_mcp_selfcheck(app_dir)


def test_runtime_build_mcp_selfcheck_fails_closed_when_exe_missing(tmp_path):
    import pytest

    import build_windows_runtime as bwr

    app_dir = _make_app_dir(tmp_path, with_mcp_exe=False)
    with pytest.raises(SystemExit, match="缺少 MCP 入口"):
        bwr.verify_mcp_selfcheck(app_dir)


def test_installer_build_runs_mcp_selfcheck_and_reports_fail_closed(tmp_path, monkeypatch):
    import build_windows_installer as bwi

    runtime_dir = _make_app_dir(tmp_path)
    monkeypatch.setattr(bwi.subprocess, "run",
                        lambda *a, **k: _FakeSubprocessResult(0, "SELFCHECK_OK tools=19 corpus_total=8022"))
    ok, detail = bwi.verify_mcp_selfcheck(runtime_dir)
    assert ok is True and "SELFCHECK_OK" in detail

    # 产物存在才跑：缺 exe → 失败（双 EXE 是 onedir 契约）
    ok, detail = bwi.verify_mcp_selfcheck(tmp_path / "no-mcp-exe")
    assert ok is False and "缺少 MCP 入口" in detail

    # 输出无 SELFCHECK_OK → 失败
    monkeypatch.setattr(bwi.subprocess, "run",
                        lambda *a, **k: _FakeSubprocessResult(0, "[biodata-mcp] 自检中 …"))
    ok, detail = bwi.verify_mcp_selfcheck(runtime_dir)
    assert ok is False and "未通过" in detail
