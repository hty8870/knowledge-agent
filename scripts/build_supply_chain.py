# -*- coding: utf-8 -*-
"""BioData Agent 安装器供应链产物生成（纯标准库、确定性）。

在隔离构建 venv（默认 `<仓库父目录>/build-venv`，也可用环境变量 BIODATA_BUILD_VENV
指定）上运行。`scripts/build_windows_runtime.py`**不改**——本脚本是它的供应链配套：
从 `packaging/requirements/runtime-win-x64.lock` 与构建 venv 已装包 metadata 生成：

  <out>/biodata-sbom.cdx.json                CycloneDX 1.5 SBOM（components=全部基础运行时锁内依赖）
  <out>/model-runtime-sbom.cdx.json          CycloneDX 1.5 SBOM（components=在线本地模型锁内依赖，随 frozen 分发 lock、安装时联网取字节）
  <out>/THIRD_PARTY_NOTICES.txt              依赖许可证清单（含全部锁内依赖名/版本/许可）
  <out>/licenses/<规范名>/...                 逐包许可证原文拷贝（dist-info LICENSE 等）
  <out>/build-tools.json                      构建工具版本与来源记录（pyinstaller/uv 实读、Inno 固定；含随包 uv 与在线模型 lock 及联网来源）
  <out>/installer-verification-report.json.template  安装器验证报告模板
  <out>/<installer>.sha256                   安装器 SHA-256 sidecar（--installer 时）

联网来源如实登记（不声称所有外部字节都由仓库 lock 覆盖）：可选本地模型的权重走
ModelScope（失败回退 HuggingFace）、运行库走 PyPI（uv 按 model-win-x64.lock 锁定版本）、
uv 管理的 CPython 由 uv 自行下载；`build-tools.json` 的 `local_model.network_sources` 与
`model-runtime-sbom.cdx.json` 的 metadata.properties 都会带上这条说明。

用法（与 build_windows_runtime.py 同一解释器约定）：
  <build-venv-python> scripts/build_supply_chain.py [--out <dir>] [--build-venv <dir>]
                      [--installer <path>] [--skip-license-copy]
  （也可用主树解释器 + --build-venv：许可证 metadata 经 importlib.metadata 按 site-packages
   路径直读，不依赖调用方解释器的已装包集合）

签名合同（与 packaging/signing/README.md 一致）：
  - 无签名凭据时，产物名**必须**含 `unsigned-dev`（本模块提供命名强制函数，契约测试钉死）；
  - 正式发布顺序与 Authenticode/RFC3161 流程见 packaging/signing/README.md，本脚本只负责
    与安装器无关的供应链文件与 sidecar，不接触 PFX/密码。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOCK = _REPO_ROOT / "packaging" / "requirements" / "runtime-win-x64.lock"
_APP_NAME = "BioDataAgent"
_APP_VERSION_SOURCE = _REPO_ROOT / "src" / "dataset_recommender" / "app" / "webapp.py"
#: 在线可选本地模型的锁：随 frozen 分发（_internal/packaging/requirements/model-win-x64.lock），
#: 但安装时才联网解析安装，不随基础安装包分发。
_MODEL_LOCK = _REPO_ROOT / "packaging" / "requirements" / "model-win-x64.lock"
#: 诚实联网来源说明：不得声称所有外部字节都被仓库 lock 直接覆盖。
_NETWORK_SOURCES_NOTE = (
    "可选本地语义模型在安装时联网：模型权重优先 ModelScope、失败回退 HuggingFace；"
    "运行库（PyTorch/transformers 等）由随包 uv 从 PyPI 按 model-win-x64.lock 的锁定版本安装；"
    "uv 管理的 CPython（3.12.13）由 uv 自行下载。除仓库内已锁定的包之外，仍会按上述来源联网取字节，"
    "并非所有外部字节都由仓库 lock 直接覆盖。"
)

UNSIGNED_DEV_MARKER = "unsigned-dev"
#: Inno Setup 版本（交付固定编译器版本；官方 jrsoftware GitHub release：
#: https://github.com/jrsoftware/issrc/releases/tag/is-6_7_3 → innosetup-6.7.3.exe 安装）。
INNO_VERSION = "6.7.3"

#: 从 dist-info 拷贝进 licenses/ 的文件名（大小写不敏感）。
_LICENSE_FILENAMES = (
    "license", "license.txt", "license.md", "copying", "copying.txt", "copying.md",
    "license.rst", "notice", "notice.txt", "notices.txt",
)


@dataclass(frozen=True)
class LockedPackage:
    name: str          # 锁文件里的原始名字
    version: str
    hashes: list[str]  # sha256 hex 列表（锁文件可能每包两个 hash）


@dataclass(frozen=True)
class InstalledPackage:
    name: str
    version: str
    licenses: list[str] = field(default_factory=list)
    home_page: str | None = None
    requires_python: str | None = None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_dist(name: str) -> str:
    """PEP 503 规范化：连字符/点/下划线统一为 `-`，小写。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_lock(lock_text: str) -> list[LockedPackage]:
    """解析 uv pip compile 的 lock 文本：钉版行 `name==version`（行尾反斜杠续行）+ 紧随的 `--hash=sha256:...` 行。

    确定性：按出现顺序返回（uv 输出本身按依赖拓扑排序，保持原序便于审计）。
    """
    packages: list[LockedPackage] = []
    current: LockedPackage | None = None
    for raw in lock_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("--hash="):
            if current is None:
                raise ValueError(f"hash line without a package: {raw!r}")
            for token in line[len("--hash="):].split(","):
                token = token.rstrip("\\").strip()
                if token.startswith("sha256:"):
                    digest = token[len("sha256:"):]
                    if not re.fullmatch(r"[0-9a-f]{64}", digest):
                        raise ValueError(f"malformed sha256 hash: {digest!r}")
                    current.hashes.append(digest)
            continue
        name, sep, version = line.rstrip("\\").partition("==")
        if not sep:
            continue
        name, version = name.strip(), version.strip()
        if not name or not re.fullmatch(r"[0-9a-z._-]+", name, re.IGNORECASE):
            raise ValueError(f"malformed pin line: {raw!r}")
        current = LockedPackage(name=name, version=version, hashes=[])
        packages.append(current)
    if not packages:
        raise ValueError("lock text contains no pinned packages")
    for pkg in packages:
        if not pkg.hashes:
            raise ValueError(f"locked package has no sha256 hashes: {pkg.name}")
    return packages


def _site_packages(build_venv: Path) -> Path:
    win = build_venv / "Lib" / "site-packages"
    if win.is_dir():
        return win
    posix = sorted(build_venv.glob("lib/python*/site-packages"))
    if posix:
        return posix[-1]
    raise ValueError(f"cannot locate site-packages under build venv: {build_venv}")


def _resolve_licenses(dist: importlib.metadata.Distribution) -> tuple[list[str], str]:
    metadata = dist.metadata
    expression = (metadata.get("License-Expression") or "").strip()
    if expression:
        return [expression], "License-Expression"
    legacy = (metadata.get("License") or "").strip()
    if legacy and legacy.upper() != "UNKNOWN":
        return [legacy], "License"
    classifiers = [
        classifier
        for classifier in (metadata.get_all("Classifier") or [])
        if classifier.startswith("License :: OSI Approved :: ")
    ]
    if classifiers:
        return [c.split(" :: ")[-1] for c in classifiers], "Classifier"
    return ["UNKNOWN"], "UNKNOWN"


def collect_installed_metadata(build_venv: Path) -> dict[str, InstalledPackage]:
    """从构建 venv 的 site-packages 读已装包 metadata（name→版本+许可证）。

    通过 `importlib.metadata.distributions(path=...)` 跨解释器直读 site-packages，
    调用方解释器不需要安装这些包。
    """
    site = _site_packages(build_venv)
    collected: dict[str, InstalledPackage] = {}
    for dist in importlib.metadata.distributions(path=[str(site)]):
        name = dist.metadata.get("Name") or dist.metadata["name"]
        licenses, _ = _resolve_licenses(dist)
        collected[_normalize_dist(name)] = InstalledPackage(
            name=name,
            version=dist.version,
            licenses=licenses,
            home_page=dist.metadata.get("Home-page") or None,
            requires_python=dist.metadata.get("Requires-Python") or None,
        )
    return collected


def _pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{re.sub(r'[-_.]+', '_', name).lower()}@{version}"


def _product_version() -> str:
    """从 webapp.py 读 WEB_API_VERSION（不 import 应用）。"""
    text = _APP_VERSION_SOURCE.read_text(encoding="utf-8-sig")
    match = re.search(r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$', text, re.MULTILINE)
    if not match:
        raise ValueError(f"WEB_API_VERSION missing in {_APP_VERSION_SOURCE}")
    return match.group(1)


def build_sbom(
    locked: list[LockedPackage],
    installed: dict[str, InstalledPackage],
    *,
    tool_versions: dict[str, str],
    timestamp: str | None = None,
) -> dict[str, Any]:
    """CycloneDX 1.5 SBOM：components = 全部锁内依赖；构建工具进 metadata.tools。"""
    now = timestamp or _utc_now()
    lock_digest = hashlib.sha256(
        "".join(f"{p.name}=={p.version}" for p in locked).encode("utf-8")
    ).hexdigest()
    components = []
    for pkg in locked:
        meta = installed.get(_normalize_dist(pkg.name))
        licenses = meta.licenses if meta else ["UNKNOWN"]
        license_entries = []
        for lic in licenses:
            normalized = re.sub(r"\s+", " ", lic).strip()
            if re.fullmatch(r"[A-Za-z0-9.+-]+", normalized):
                license_entries.append({"license": {"id": normalized}})
            else:
                license_entries.append({"license": {"name": normalized}})
        components.append(
            {
                "type": "library",
                "name": pkg.name,
                "version": pkg.version,
                "purl": _pypi_purl(pkg.name, pkg.version),
                "hashes": [{"alg": "SHA-256", "content": h} for h in pkg.hashes],
                "licenses": license_entries,
            }
        )
    tools_components = [
        {
            "type": "application",
            "name": name,
            "version": version,
            "publisher": "BioData Agent build pipeline",
        }
        for name, version in sorted(tool_versions.items())
    ]
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'https://biodata.local/sbom/{lock_digest}')}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "name": _APP_NAME,
                "version": _product_version(),
            },
            "tools": {"components": tools_components},
        },
        "components": components,
    }


def build_model_sbom(
    model_locked: list[LockedPackage], *, timestamp: str | None = None
) -> dict[str, Any]:
    """CycloneDX 1.5 SBOM：在线本地模型锁内依赖（随 frozen 分发 lock、安装时才联网取字节）。

    这些包不在构建 venv / 基础 frozen 里，因此许可证按 UNKNOWN 如实标注；hash 仍来自锁文件。
    联网来源以 metadata.properties 诚实列出。
    """
    now = timestamp or _utc_now()
    lock_digest = hashlib.sha256(
        "".join(f"{p.name}=={p.version}" for p in model_locked).encode("utf-8")
    ).hexdigest()
    components = []
    for pkg in model_locked:
        components.append(
            {
                "type": "library",
                "name": pkg.name,
                "version": pkg.version,
                "purl": _pypi_purl(pkg.name, pkg.version),
                "hashes": [{"alg": "SHA-256", "content": h} for h in pkg.hashes],
                "licenses": [{"license": {"name": "UNKNOWN (online-installed, not bundled)"}}],
            }
        )
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, f'https://biodata.local/model-sbom/{lock_digest}')}",
        "version": 1,
        "metadata": {
            "timestamp": now,
            "component": {
                "type": "application",
                "name": "BioDataAgent-local-model-runtime",
                "version": _product_version(),
            },
            "properties": [
                {"name": "biodata:network-sources", "value": _NETWORK_SOURCES_NOTE},
            ],
        },
        "components": components,
    }


def render_notices(sbom: dict[str, Any], *, header_note: str = "") -> str:
    """THIRD_PARTY_NOTICES.txt 正文：全部锁内依赖 + 版本 + 许可证。"""
    lines = [
        "BioData Agent — THIRD PARTY NOTICES",
        "",
        "This file lists the Python runtime dependencies of the frozen Windows runtime",
        "(packaging/requirements/runtime-win-x64.lock) and their license metadata as",
        "reported by the installed packages' metadata in the isolated build environment.",
        "Full license texts, where present in the installed packages, are copied into",
        "the licenses/ directory next to this file.",
    ]
    if header_note:
        lines.append("")
        lines.append(header_note)
    lines.append("")
    lines.append("Runtime dependencies:")
    lines.append("")
    for component in sbom["components"]:
        licenses = ", ".join(
            entry["license"].get("id") or entry["license"].get("name")
            for entry in component["licenses"]
        )
        lines.append(f"- {component['name']}=={component['version']} — {licenses}")
    lines.append("")
    lines.append("Build tools (not shipped as runtime dependencies):")
    lines.append("")
    for tool in sbom["metadata"]["tools"]["components"]:
        lines.append(f"- {tool['name']}=={tool['version']} (build-time)")
    return "\n".join(lines) + "\n"


def copy_license_files(
    build_venv: Path, locked: list[LockedPackage], out_dir: Path
) -> list[Path]:
    """把每个锁内依赖在 dist-info 里的许可证原文拷到 <out>/licenses/<规范名>/。"""
    site = _site_packages(build_venv)
    copied: list[Path] = []
    for pkg in locked:
        normalized = _normalize_dist(pkg.name)
        dist_dir = site / f"{normalized}-{pkg.version}.dist-info"
        if not dist_dir.is_dir():
            dist_dir = site / f"{pkg.name.replace('-', '_')}-{pkg.version}.dist-info"
        if not dist_dir.is_dir():
            continue
        target = out_dir / "licenses" / normalized
        target.mkdir(parents=True, exist_ok=True)
        for candidate in sorted(dist_dir.rglob("*")):
            if not candidate.is_file():
                continue
            relative = candidate.relative_to(dist_dir).parts
            inside_licenses_dir = any(part.lower() == "licenses" for part in relative)
            if candidate.name.lower() not in _LICENSE_FILENAMES and not inside_licenses_dir:
                continue
            dest = target / candidate.name
            if not dest.exists():  # 同名冲突先到先得（根 LICENSE 优先于 licenses/ 子目录内同名文件）
                dest.write_bytes(candidate.read_bytes())
                copied.append(dest)
    return copied


def build_tools_record(
    *,
    build_venv: Path,
    installed: dict[str, InstalledPackage],
    lock: Path,
    timestamp: str | None = None,
    model_lock: Path | None = None,
) -> dict[str, Any]:
    """构建工具版本与来源记录（pyinstaller/uv 实读 venv metadata，Inno 固定 6.7.3）。

    随包 uv 与在线模型 lock 在此登记；联网来源以 `local_model.network_sources` 如实列出。
    """
    pyinstaller = installed.get("pyinstaller")
    uv = installed.get("uv")
    python_version = "unknown"
    python = build_venv / "Scripts" / "python.exe"
    if python.is_file():
        probe = subprocess.run(
            [str(python), "--version"], capture_output=True, text=True, timeout=30
        )
        if probe.returncode == 0:
            python_version = probe.stdout.strip().removeprefix("Python ")
    commit, dirty = None, None
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, check=True,
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=_REPO_ROOT, check=True, capture_output=True, text=True, timeout=20,
            ).stdout.strip()
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    local_model = None
    if model_lock is not None and model_lock.is_file():
        model_locked = parse_lock(model_lock.read_text(encoding="utf-8"))
        local_model = {
            "lock_file": str(model_lock.relative_to(_REPO_ROOT)) if model_lock.is_relative_to(_REPO_ROOT) else str(model_lock),
            "lock_sha256": _sha256_file(model_lock),
            "package_count": len(model_locked),
            "shipped_worker": "tools/model_worker.py（frozen _internal/tools/，隔离 venv 内运行）",
            "network_sources": _NETWORK_SOURCES_NOTE,
        }
    return {
        "format": "biodata-build-tools/v1",
        "generated_at": timestamp or _utc_now(),
        "lock_file": str(lock.relative_to(_REPO_ROOT)) if lock.is_relative_to(_REPO_ROOT) else str(lock),
        "lock_sha256": _sha256_file(lock),
        "source": {"commit": commit, "dirty": dirty},
        "python": {"version": python_version},
        "tools": [
            {
                "name": "pyinstaller",
                "version": pyinstaller.version if pyinstaller else "UNKNOWN",
                "role": "frozen runtime builder (onedir, --noupx)",
                "source": "packaging/requirements/build-win-x64.lock",
            },
            {
                "name": "innosetup",
                "version": INNO_VERSION,
                "role": "Windows installer compiler (Setup.exe / SignedUninstaller)",
                "source": "jrsoftware GitHub release is-6_7_3（innosetup-6.7.3.exe 官方安装）",
            },
            {
                "name": "uv",
                "version": uv.version if uv else "UNKNOWN",
                "role": "shipped in frozen _internal/tools/uv.exe；manages isolated model venv (CPython 3.12.13 + PyPI packages)",
                "source": "packaging/requirements/build-win-x64.lock",
            },
        ],
        "local_model": local_model,
    }


def render_verification_template(
    *, tool_versions: dict[str, str], timestamp: str | None = None
) -> str:
    """安装器验证报告模板：字段齐全、值占位，供正式发布时逐项填写/机检。"""
    template: dict[str, Any] = {
        "format": "biodata-installer-verification/v1",
        "generated_at": timestamp or _utc_now(),
        "product": _APP_NAME,
        "product_version": _product_version(),
        "installer": {
            "file": "PENDING",
            "sha256_sidecar": "PENDING",
            "size_bytes": "PENDING",
        },
        "supply_chain": {
            "sbom": "PENDING",
            "third_party_notices": "PENDING",
            "build_tools_record": "PENDING",
        },
        "signature": {
            "status": "unsigned-dev" if not tool_versions.get("signing_credentials") else "PENDING",
            "algorithm": "Authenticode SHA-256",
            "timestamp_protocol": "RFC3161",
            "subject": "PENDING",
            "thumbprint": "PENDING",
            "certificate_provider": "PENDING",
        },
        "verification_steps": [
            "frozen runtime manifest checksum vs runtime-manifest.json",
            "Setup.exe Authenticode signature verify (signtool verify /pa)",
            "SignedUninstaller.exe signature verify",
            "installer sha256 sidecar matches bytes",
            "Microsoft Defender real-time scan result",
            "Migration + end-to-end installer tests (test_migrate_from.py / test_installer_e2e.py)",
        ],
        "results": {
            "frozen_manifest": "PENDING",
            "setup_signature_verify": "PENDING",
            "uninstaller_signature_verify": "PENDING",
            "sidecar_match": "PENDING",
            "defender_scan": "PENDING",
            "migration_e2e": "PENDING",
        },
        "notes": "无签名凭据时整体流程照跑，产物只带 unsigned-dev 标，禁止自签冒充正式。",
    }
    return json.dumps(template, ensure_ascii=False, indent=2) + "\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256sum_line(digest_hex: str, name: str) -> str:
    """sha256sum 风格 sidecar 单行：`<64hex>  <name>\\n`（与 build_release.py 一致）。"""
    if not re.fullmatch(r"[0-9a-f]{64}", digest_hex):
        raise ValueError("sha256 digest must be 64 lowercase hex characters")
    return f"{digest_hex}  {name}\n"


def write_sha256_sidecar(target: Path, out_dir: Path) -> Path:
    """为安装器/安装包写 `<name>.sha256` sidecar（相对名，无绝对路径）。"""
    if not target.is_file():
        raise ValueError(f"installer file does not exist: {target}")
    sidecar = out_dir / f"{target.name}.sha256"
    sidecar.write_text(sha256sum_line(_sha256_file(target), target.name), encoding="ascii")
    return sidecar


def installer_artifact_name(base: str, *, has_signing_credentials: bool) -> str:
    """产物命名合同：无签名凭据 → 强制带 `unsigned-dev`；有凭据 → 保持原名。

    安装器构建脚本（build_installer.py）按此约定命名产物；本函数是命名逻辑的真源（契约测试钉死）。
    """
    if not has_signing_credentials and UNSIGNED_DEV_MARKER not in base:
        return f"{base}-{UNSIGNED_DEV_MARKER}"
    return base


def assert_unsigned_dev_naming(name: str, has_signing_credentials: bool) -> str:
    """无签名凭据时产物名必含 `unsigned-dev`，否则抛错（fail-closed）。"""
    if not has_signing_credentials and UNSIGNED_DEV_MARKER not in name:
        raise ValueError(
            f"unsigned build artifact name must contain {UNSIGNED_DEV_MARKER!r}: {name!r}"
        )
    return name


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 BioData Agent 安装器供应链产物（SBOM / NOTICEs / 构建工具记录 / sidecar / 验证模板）"
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("BIODATA_BUILD_OUT") or str(_REPO_ROOT.parent / "build-out" / "supply-chain"),
        help="输出目录（默认 <仓库父目录>/build-out/supply-chain，仓库外）",
    )
    parser.add_argument(
        "--build-venv",
        default=os.environ.get("BIODATA_BUILD_VENV") or str(_REPO_ROOT.parent / "build-venv"),
        help="隔离构建 venv 目录（默认 <仓库父目录>/build-venv，仓库外）",
    )
    parser.add_argument(
        "--lock", type=Path, default=_DEFAULT_LOCK, help="runtime 锁文件路径",
    )
    parser.add_argument(
        "--installer", type=Path, default=None,
        help="安装器文件路径（存在则生成 <name>.sha256 sidecar 并校验 unsigned-dev 命名）",
    )
    parser.add_argument(
        "--skip-license-copy", action="store_true", help="不拷贝逐包许可证原文到 licenses/",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    out = Path(args.out).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    build_venv = Path(args.build_venv).expanduser().resolve()
    if not build_venv.is_dir():
        print(f"[supply-chain] 构建 venv 不存在：{build_venv}", file=sys.stderr)
        return 2
    lock = args.lock.resolve()
    if not lock.is_file():
        print(f"[supply-chain] runtime 锁文件不存在：{lock}", file=sys.stderr)
        return 2

    locked = parse_lock(lock.read_text(encoding="utf-8"))
    model_lock = _MODEL_LOCK if _MODEL_LOCK.is_file() else None
    model_locked = parse_lock(model_lock.read_text(encoding="utf-8")) if model_lock is not None else []
    installed = collect_installed_metadata(build_venv)
    pyinstaller = installed.get("pyinstaller")
    tool_versions = {
        "pyinstaller": pyinstaller.version if pyinstaller else "UNKNOWN",
        "innosetup": INNO_VERSION,
    }
    sbom = build_sbom(locked, installed, tool_versions=tool_versions)
    (out / "biodata-sbom.cdx.json").write_text(
        json.dumps(sbom, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    notices = render_notices(sbom, header_note=f"Generated {_utc_now()} from {lock.name}.")
    (out / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf-8")
    tools_record = build_tools_record(
        build_venv=build_venv, installed=installed, lock=lock, model_lock=model_lock
    )
    (out / "build-tools.json").write_text(
        json.dumps(tools_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if model_lock is not None:
        model_sbom = build_model_sbom(model_locked)
        (out / "model-runtime-sbom.cdx.json").write_text(
            json.dumps(model_sbom, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )
        print(f"[supply-chain] 在线模型 SBOM：{len(model_locked)} 个锁内依赖 → {out / 'model-runtime-sbom.cdx.json'}")
    (out / "installer-verification-report.json.template").write_text(
        render_verification_template(tool_versions=tool_versions), encoding="utf-8"
    )
    if not args.skip_license_copy:
        copied = copy_license_files(build_venv, locked, out)
        print(f"[supply-chain] licenses 拷贝：{len(copied)} 个文件 → {out / 'licenses'}")
    print(f"[supply-chain] SBOM：{len(locked)} 个锁内依赖 → {out / 'biodata-sbom.cdx.json'}")
    print(f"[supply-chain] NOTICEs：{out / 'THIRD_PARTY_NOTICES.txt'}")
    print(f"[supply-chain] 构建工具记录：{out / 'build-tools.json'}")

    if args.installer is not None:
        installer = args.installer.resolve()
        assert_unsigned_dev_naming(installer.name, has_signing_credentials=False)
        sidecar = write_sha256_sidecar(installer, out)
        print(f"[supply-chain] sidecar：{sidecar}")
        print(f"[supply-chain] 命名校验：{installer.name} 含 {UNSIGNED_DEV_MARKER}（无签名凭据，fail-closed）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
