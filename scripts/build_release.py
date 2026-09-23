from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import sys
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Single source of truth for the forbidden-internal-token list and the text-file suffix set:
# reuse make_delivery's own definitions so the customer-delivery gate and this release gate
# cannot drift apart (do NOT copy a second list here — DEVELOPMENT.md §11 / make_delivery.md).
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from make_delivery import FORBIDDEN_TOKENS, _CONTACT_EMAIL_RE, _TEXT_SUFFIXES  # noqa: E402

MANIFEST_NAME = "release-manifest.json"
DEFAULT_ARCHIVE_NAME = "biodata-agent-release-candidate.zip"
PRODUCT_NAME = "BioData Agent"
ARTIFACT_TYPE = "release-candidate"
ARTIFACT_AUDIENCE = "reviewable source and trusted-local Windows runtime"

# The release is assembled from an allowlist. Directories not named here—most
# importantly .git, models, outputs, collaboration notes, and personal work
# material—cannot enter the artifact by accident.
#
# database/external 官方快照口径（与安装器一致， 对齐）：`packaging/pyinstaller/
# biodata-agent.spec` 的 `_EXTERNAL_SNAPSHOTS` 逐项打包 `git ls-files database/external/`
# 的 11 个文件（README.md + 10 个官方快照 JSON）。此处必须与之完全一致——只列出被 git
# 跟踪、经过审计的官方冻结快照；用户上传（gitignored 的 upload_*.json）绝不入包。
PUBLIC_EXTERNAL_FILES = {
    "database/external/README.md",
    "database/external/arrayexpress.json",
    "database/external/cellxgene.json",
    "database/external/ebi_scea.json",
    "database/external/encode.json",
    "database/external/geo.json",
    "database/external/hca.json",
    "database/external/hubmap.json",
    "database/external/refinebio.json",
    "database/external/single_cell_portal.json",
    "database/external/zenodo.json",
}
PUBLIC_DOC_FILES = {
    "docs/AUTOMATION_AND_RELEASE.md",
    "docs/CHANGELOG.md",
    "docs/PUBLIC_MIRROR.md",
}
EVAL_INPUT_FILES = {
    "eval/baseline_L0.json",
    "eval/evaluation-manifest.json",
    "eval/eval_queries.json",
    "eval/eval_queries_dev.json",
    "eval/eval_queries_public_validation.json",
    "eval/multisource_queries.json",
}
REQUIRED_RELEASE_FILES = {
    "README.md",
    "SECURITY.md",
    "automation/quality-gates.json",
    "database/SOURCES.yml",
    "docs/AUTOMATION_AND_RELEASE.md",
    "eval/evaluation-manifest.json",
    "eval/eval_queries_dev.json",
    "eval/eval_queries_public_validation.json",
    "mcp_server.py",
    "requirements-ci.lock",
    "requirements.txt",
    "scripts/launch_web.ps1",
    "scripts/smoke_test.py",
    "scripts/web_smoke_test.py",
    "src/dataset_recommender/app/webapp.py",
    "start-web.bat",
    "web/static/index.html",
}
# 进入发布候选包的「可执行」条目：zip 内写 0o100755（否则 .sh/.command 用户拿到无法直接执行）。
# scripts/run_web.sh 既有文件、本次新增的 launch_web.sh 与两个一键入口。
RELEASE_EXECUTABLE_FILES = frozenset({
    "scripts/run_web.sh",
    "scripts/launch_web.sh",
    "打开前端.sh",
    "打开前端.command",
})
ROOT_FILES = {
    ".env.example",
    ".env.zhipu.example",
    ".gitattributes",
    ".gitignore",
    "DEVELOPMENT.md",
    "MODULES.md",
    "PRODUCT.md",
    "README.md",
    "SECURITY.md",
    "database/SOURCES.yml",
    "mcp_server.py",
    "requirements-embeddings.txt",
    "requirements-ci.lock",
    "requirements-ci.txt",
    "requirements-langchain.txt",
    "requirements-webview.txt",
    "requirements.txt",
    "start-web.bat",
    "打开前端.bat",
    "打开前端.sh",
    "打开前端.command",
    "测试说明.txt",
} | PUBLIC_EXTERNAL_FILES | PUBLIC_DOC_FILES | EVAL_INPUT_FILES
ROOT_DIRS = {
    ".agents/skills",
    "automation",
    "database/base",
    "docs/agent",
    "scripts",
    "src",
    "tests",
    "web",
    "使用教程",
}
EXCLUDED_PARTS = {
    ".git",
    ".github",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "dist",
    "models",
    "outputs",
    "venv",
}
EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo"}
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)

# 仓库一级目录整理：requirements 群迁入 requirements/、启动脚本迁入
# launchers/、mcp_server.py 迁入 src/dataset_recommender/app/、测试说明.txt 迁入 docs/。
# **交付包（zip/manifest）布局不变**——ROOT_FILES / REQUIRED_RELEASE_FILES 始终按包内
# 路径组织；本表把「包内相对路径 → 仓库源相对路径」钉死，与 make_delivery 的
# LEGACY_DELIVERY_PATHS（反向）覆盖同一批文件。
SOURCE_PATH_OVERRIDES = {
    "eval/evaluation-manifest.json": "eval/evaluation-manifest.public.json",
    "mcp_server.py": "src/dataset_recommender/app/mcp_server.py",
    "requirements-embeddings.txt": "requirements/requirements-embeddings.txt",
    "requirements-ci.lock": "requirements/requirements-ci.lock",
    "requirements-ci.txt": "requirements/requirements-ci.txt",
    "requirements-langchain.txt": "requirements/requirements-langchain.txt",
    "requirements-webview.txt": "requirements/requirements-webview.txt",
    "requirements.txt": "requirements/requirements.txt",
    "start-web.bat": "launchers/start-web.bat",
    "打开前端.bat": "launchers/打开前端.bat",
    "打开前端.command": "launchers/打开前端.command",
    "打开前端.sh": "launchers/打开前端.sh",
    "测试说明.txt": "docs/测试说明.txt",
}
# 反向（仓库源相对路径 → 包内相对路径）：目录遍历收到被迁移文件时归一到包内路径。
_ARCHIVE_PATH_OVERRIDES = {source: archive for archive, source in SOURCE_PATH_OVERRIDES.items()}


def source_relpath(relative: str) -> str:
    """包内相对路径 → 仓库源相对路径（未迁移文件恒等）。"""
    return SOURCE_PATH_OVERRIDES.get(relative, relative)
MAX_RELEASE_FILE_BYTES = 64 * 1024 * 1024
MAX_RELEASE_TOTAL_BYTES = 512 * 1024 * 1024
MAX_RELEASE_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_RELEASE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_RELEASE_FILES = 10_000
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "CLOCK$",
    "CONIN$",
    "CONOUT$",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
    *(f"{prefix}{suffix}" for prefix in ("COM", "LPT") for suffix in ("¹", "²", "³")),
}
PRIVATE_HOME_PATTERNS = (
    re.compile(rb"(?i)[a-z]:[\\/]+users[\\/]+[a-z0-9._-]+(?=[\\/])"),
    re.compile(rb"/" + rb"home/" + rb"[a-z0-9._-]+(?=/)", re.IGNORECASE),
)

# Delivery/authoring tooling that must NEVER ship in the release candidate. Each entry is:
#   1. excluded from the allowlist (see `_excluded`), so it never lands in the archive, and
#   2. exempted from the forbidden-token content gate (see `_reject_release_forbidden_tokens_bytes`),
#      because `scripts/make_delivery.py` is the single source of FORBIDDEN_TOKENS and therefore
#      legitimately contains every one of them (this is the customer-delivery packager, not a
#      runtime/review item).
# A path may appear here only if it is genuinely a token-source / delivery tool. The guard test
# `test_forbidden_token_detector_must_not_be_shippable` pins that every exempted path is also
# excluded from the archive — fail-closed: an exemption can never be used to sneak the tokens in.
RELEASE_EXCLUDED_TOOL_FILES = frozenset({"scripts/make_delivery.py"})


class ReleaseError(RuntimeError):
    """Raised when the release contract cannot be satisfied."""


def product_version(root: Path = PROJECT_ROOT) -> str:
    """Read the product version without importing the Web application."""
    version_file = root / "src" / "dataset_recommender" / "app" / "webapp.py"
    try:
        payload = version_file.read_bytes()
    except OSError as exc:
        raise ReleaseError(f"cannot read product version from {version_file}") from exc
    return _product_version_from_bytes(payload, str(version_file))


def _product_version_from_bytes(payload: bytes, source: str) -> str:
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"product version source is not valid UTF-8: {source}") from exc
    match = re.search(
        r'^WEB_API_VERSION = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$',
        text,
        re.MULTILINE,
    )
    if not match:
        raise ReleaseError(f"WEB_API_VERSION is missing or invalid in {source}")
    return match.group(1)


@dataclass(frozen=True)
class ReleaseFile:
    path: Path
    relative: str
    size: int
    sha256: str


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_link_like(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _allowed_env_example(relative: str) -> bool:
    return relative in {".env.example", ".env.zhipu.example"}


def _reject_private_home_bytes(payload: bytes, relative: str) -> None:
    """Keep user-specific workstation paths out of the distributable artifact."""
    if any(pattern.search(payload) for pattern in PRIVATE_HOME_PATTERNS):
        raise ReleaseError(
            f"release input contains a user-specific home path: {relative}"
        )


def _reject_private_home_paths(path: Path, relative: str) -> None:
    _reject_private_home_bytes(path.read_bytes(), relative)


def _reject_release_forbidden_tokens_bytes(payload: bytes, relative: str) -> None:
    """Content-level gate: refuse a text file that contains a forbidden internal/personal token.

    This is the fix for the allowlist-only path check: an allowlisted text file (README,
    docs, scripts, …) that embeds a真名/内部专名/本机路径 would otherwise ship
    unchecked inside the release candidate. The token list is the single source imported from
    make_delivery.FORBIDDEN_TOKENS (do not duplicate it here).

    Only text-suffixed files are scanned (same `_TEXT_SUFFIXES` as make_delivery), so control
    files that merely *name* internal material to exclude it — e.g. a personal-report HTML
    filename pattern in `.gitignore` — are not false-positived.

    `RELEASE_EXCLUDED_TOOL_FILES` is exempted: those are the files that DEFINE the token list
    (make_delivery.py) and are simultaneously excluded from the archive, so they can never ship
    a token through the exemption — the guard test pins that coupling.
    """
    if relative in RELEASE_EXCLUDED_TOOL_FILES:
        return
    if PurePosixPath(relative).suffix.lower() not in _TEXT_SUFFIXES:
        return
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        # Non-UTF-8 text-suffixed files are skipped, matching make_delivery's scan_forbidden.
        return
    for lineno, line in enumerate(text.splitlines(), start=1):
        for token in FORBIDDEN_TOKENS:
            if token in line:
                raise ReleaseError(
                    f"release input contains forbidden internal token {token!r} "
                    f"at {relative}:{lineno}"
                )


def _reject_release_forbidden_tokens_paths(path: Path, relative: str) -> None:
    _reject_release_forbidden_tokens_bytes(path.read_bytes(), relative)


def _reject_metadata_contact_bytes(payload: bytes, relative: str) -> None:
    """Keep contact-email values out of redistributed metadata without logging them."""
    if not relative.startswith("database/") or not relative.endswith(".json"):
        return
    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"metadata snapshot is not valid UTF-8: {relative}") from exc
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _CONTACT_EMAIL_RE.search(line):
            raise ReleaseError(f"metadata snapshot contains a contact email at {relative}:{lineno}")


def _validate_relative(relative: str) -> PurePosixPath:
    if "\\" in relative:
        raise ReleaseError(f"archive path contains a backslash: {relative!r}")
    pure = PurePosixPath(relative)
    if (
        not relative
        or relative.endswith("/")
        or pure.is_absolute()
        or ".." in pure.parts
        or re.match(r"^[a-zA-Z]:", relative)
        or pure.as_posix() != relative
    ):
        raise ReleaseError(f"unsafe archive path: {relative!r}")
    if any(not part or part == "." for part in pure.parts):
        raise ReleaseError(f"non-canonical archive path: {relative!r}")
    for part in pure.parts:
        if (
            ":" in part
            or part != part.strip(" ")
            or part.endswith(".")
            or any(ord(char) < 32 or ord(char) == 127 for char in part)
            or part.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES
        ):
            raise ReleaseError(f"Windows-unsafe archive path: {relative!r}")
    return pure


def _portable_path_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


def _validate_zip_info(info: zipfile.ZipInfo) -> None:
    _validate_relative(info.filename)
    if info.flag_bits & 0x1:
        raise ReleaseError(f"encrypted archive entries are forbidden: {info.filename}")
    if info.is_dir():
        raise ReleaseError(f"directory archive entries are forbidden: {info.filename}")
    if info.file_size > MAX_RELEASE_FILE_BYTES:
        raise ReleaseError(f"release file exceeds size limit: {info.filename}")
    if info.create_system == 3:
        mode = (info.external_attr >> 16) & 0xFFFF
        file_type = stat.S_IFMT(mode)
        if file_type not in {0, stat.S_IFREG}:
            raise ReleaseError(f"non-regular archive entry is forbidden: {info.filename}")
    if info.compress_type != zipfile.ZIP_DEFLATED:
        raise ReleaseError(
            f"release archive entry must use Deflate compression: {info.filename}"
        )


def _excluded(relative: str) -> bool:
    pure = _validate_relative(relative)
    lowered = [part.lower() for part in pure.parts]
    if any(part in {item.lower() for item in EXCLUDED_PARTS} for part in lowered):
        return True
    if any(part.startswith(".env") for part in lowered) and not _allowed_env_example(relative):
        return True
    if Path(pure.name).suffix.lower() in EXCLUDED_SUFFIXES:
        return True
    if relative in RELEASE_EXCLUDED_TOOL_FILES:
        return True
    return False


def _allowlisted_release_path(relative: str) -> bool:
    return relative in ROOT_FILES or any(
        relative.startswith(f"{directory}/") for directory in ROOT_DIRS
    )


def _iter_allowed_candidates(root: Path) -> Iterable[Path]:
    for name in sorted(ROOT_FILES):
        path = root.joinpath(*PurePosixPath(source_relpath(name)).parts)
        if path.is_file():
            yield path

    for directory in sorted(ROOT_DIRS):
        base = root.joinpath(*PurePosixPath(directory).parts)
        if not base.exists():
            continue
        if not base.is_dir() or _is_link_like(base):
            raise ReleaseError(f"allowlisted directory is not a normal directory: {directory}")
        for path in sorted(base.rglob("*"), key=lambda item: item.as_posix()):
            if path.is_dir():
                if _is_link_like(path):
                    raise ReleaseError(f"link/junction is forbidden in release inputs: {path}")
                continue
            if not path.is_file():
                raise ReleaseError(f"unsupported release input type: {path}")
            # 被迁移文件由 ROOT_FILES 循环按包内路径名义入包；目录遍历到同一物理文件时
            # 跳过，避免同一文件以两个包内路径重复入包（当前只命中 src/ 下的 mcp_server.py）。
            if path.relative_to(root).as_posix() in _ARCHIVE_PATH_OVERRIDES:
                continue
            yield path


def collect_release_files(root: Path = PROJECT_ROOT) -> list[ReleaseFile]:
    root = root.resolve()
    if not root.is_dir():
        raise ReleaseError(f"project root does not exist: {root}")
    missing_public = sorted(
        relative
        for relative in PUBLIC_EXTERNAL_FILES
        if not root.joinpath(*PurePosixPath(relative).parts).is_file()
    )
    if missing_public:
        raise ReleaseError(
            "release inputs are missing reviewed public external datasets: "
            + ", ".join(missing_public)
        )

    collected: dict[str, ReleaseFile] = {}
    total_size = 0
    for path in _iter_allowed_candidates(root):
        if _is_link_like(path):
            raise ReleaseError(f"link/junction is forbidden in release inputs: {path}")
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError as exc:
            raise ReleaseError(f"release input escapes project root: {path}") from exc
        # 被迁移文件归一到包内（历史根目录）路径，交付包布局与迁移前逐位一致。
        relative = _ARCHIVE_PATH_OVERRIDES.get(relative, relative)
        if _excluded(relative):
            continue
        if relative == MANIFEST_NAME:
            raise ReleaseError(f"source tree may not provide reserved file {MANIFEST_NAME}")
        if relative in collected:
            raise ReleaseError(f"duplicate release path: {relative}")
        size = path.stat().st_size
        if size > MAX_RELEASE_FILE_BYTES:
            raise ReleaseError(f"release input exceeds per-file size limit: {relative}")
        total_size += size
        if total_size > MAX_RELEASE_TOTAL_BYTES:
            raise ReleaseError("release inputs exceed total uncompressed size limit")
        if len(collected) + 1 > MAX_RELEASE_FILES:
            raise ReleaseError("release inputs exceed file-count limit")
        _reject_private_home_paths(path, relative)
        collected[relative] = ReleaseFile(
            path=path,
            relative=relative,
            size=size,
            sha256=_sha256_file(path),
        )

    missing_required = sorted(REQUIRED_RELEASE_FILES - collected.keys())
    if missing_required:
        raise ReleaseError(
            "release inputs are missing required runtime/readiness files: "
            + ", ".join(missing_required)
        )
    if not any(item.relative.startswith("src/") for item in collected.values()):
        raise ReleaseError("release inputs are incomplete: src/ is empty")
    if not any(
        item.relative.startswith("database/base/") and item.relative.endswith(".json")
        for item in collected.values()
    ):
        raise ReleaseError("release inputs are incomplete: database/base has no JSON corpus")
    return [collected[key] for key in sorted(collected)]


def _git_snapshot(root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=all"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            ).stdout.strip()
        )
        return commit or None, dirty
    except (FileNotFoundError, subprocess.SubprocessError):
        return None, None


def _content_digest(files: Iterable[ReleaseFile]) -> str:
    digest = hashlib.sha256()
    for item in files:
        digest.update(item.relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\0")
        digest.update(item.sha256.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def make_manifest(
    root: Path, files: list[ReleaseFile], resolved_product_version: str
) -> dict[str, object]:
    commit, dirty = _git_snapshot(root)
    return {
        "schema_version": 1,
        "product": PRODUCT_NAME,
        "product_version": resolved_product_version,
        "artifact_type": ARTIFACT_TYPE,
        "artifact_audience": ARTIFACT_AUDIENCE,
        "source_commit": commit,
        "source_dirty": dirty,
        "content_digest_sha256": _content_digest(files),
        "file_count": len(files),
        "selection_policy": {
            "mode": "allowlist",
            "excluded_categories": [
                "credentials and non-example env files",
                "git metadata and GitHub workflow internals",
                "local models, caches, virtual environments, outputs, and logs",
                "user uploads and non-allowlisted external datasets",
                "private collaboration claims, personal work notes, generated reports, and non-public documentation",
            ],
        },
        "files": [
            {"path": item.relative, "size": item.size, "sha256": item.sha256}
            for item in files
        ],
    }


def _zip_info(relative: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    mode = 0o100755 if relative in RELEASE_EXECUTABLE_FILES else 0o100644
    info.external_attr = (mode & 0xFFFF) << 16
    info.flag_bits |= 0x800
    return info


def build_release(
    root: Path = PROJECT_ROOT,
    output_dir: Path | None = None,
    archive_name: str = DEFAULT_ARCHIVE_NAME,
    expected_version: str | None = None,
    replace_existing: bool = False,
) -> dict[str, object]:
    root = root.resolve()
    if Path(archive_name).name != archive_name or not archive_name.lower().endswith(".zip"):
        raise ReleaseError("archive name must be a simple .zip filename")
    output_dir = (output_dir or (root / "artifacts")).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / archive_name
    sidecar = archive.with_suffix(archive.suffix + ".sha256")
    existing_pair = (archive.exists(), sidecar.exists())
    if existing_pair[0] != existing_pair[1]:
        raise ReleaseError(
            "refusing to build over an incomplete release pair; recover or use a new output directory"
        )
    if existing_pair[0] and not replace_existing:
        raise ReleaseError(
            "release pair already exists; preserve it and use a new output directory"
        )

    resolved_product_version = product_version(root)
    if expected_version is not None and expected_version != resolved_product_version:
        raise ReleaseError(
            "release version mismatch: "
            f"expected {expected_version!r}, product is {resolved_product_version!r}"
        )
    files = collect_release_files(root)
    # Content-level gate: every allowlisted text file must be free of forbidden internal tokens
    # (真名 / 内部专名 / 本机路径). This runs BEFORE packaging so a leaky allowlist entry can never
    # reach the artifact. `make_delivery`-sourced FORBIDDEN_TOKENS is the single source.
    for item in files:
        _reject_release_forbidden_tokens_paths(item.path, item.relative)
        _reject_metadata_contact_bytes(item.path.read_bytes(), item.relative)
    manifest = make_manifest(root, files, resolved_product_version)
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(manifest_bytes) > MAX_RELEASE_MANIFEST_BYTES:
        raise ReleaseError("release manifest exceeds size limit")

    transaction_id = uuid.uuid4().hex
    temporary = output_dir / f".{archive.name}.{transaction_id}.tmp"
    temporary_sidecar = output_dir / f".{sidecar.name}.{transaction_id}.tmp"
    backup_archive = output_dir / f".{archive.name}.{transaction_id}.previous"
    backup_sidecar = output_dir / f".{sidecar.name}.{transaction_id}.previous"
    old_archive_backed_up = False
    old_sidecar_backed_up = False
    new_archive_promoted = False
    new_sidecar_promoted = False
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
            for item in files:
                bundle.writestr(_zip_info(item.relative), item.path.read_bytes())
            bundle.writestr(_zip_info(MANIFEST_NAME), manifest_bytes)
        verification = verify_release(temporary)
        archive_sha256 = _sha256_file(temporary)
        temporary_sidecar.write_text(
            f"{archive_sha256}  {archive.name}\n", encoding="ascii", newline="\n"
        )
        verify_sidecar(
            temporary,
            sidecar_path=temporary_sidecar,
            expected_archive_name=archive.name,
        )

        if existing_pair[0]:
            os.replace(archive, backup_archive)
            old_archive_backed_up = True
            os.replace(sidecar, backup_sidecar)
            old_sidecar_backed_up = True
        os.replace(temporary, archive)
        new_archive_promoted = True
        os.replace(temporary_sidecar, sidecar)
        new_sidecar_promoted = True
        verification = verify_release(archive)
        verify_sidecar(archive)
    except BaseException:
        if new_sidecar_promoted and sidecar.exists():
            sidecar.unlink()
        if new_archive_promoted and archive.exists():
            archive.unlink()
        if old_archive_backed_up and backup_archive.exists():
            os.replace(backup_archive, archive)
        if old_sidecar_backed_up and backup_sidecar.exists():
            os.replace(backup_sidecar, sidecar)
        raise
    else:
        if backup_archive.exists():
            backup_archive.unlink()
        if backup_sidecar.exists():
            backup_sidecar.unlink()
    finally:
        for transaction_file in (temporary, temporary_sidecar):
            if transaction_file.exists():
                transaction_file.unlink()

    return {
        "ok": True,
        "archive": str(archive),
        "archive_sha256": archive_sha256,
        "sha256_file": str(sidecar),
        "file_count": verification["file_count"],
        "content_digest_sha256": verification["content_digest_sha256"],
        "product_version": resolved_product_version,
        "source_commit": manifest["source_commit"],
        "source_dirty": manifest["source_dirty"],
        "sidecar_verified": True,
    }


def verify_release(archive: Path) -> dict[str, object]:
    archive = archive.resolve()
    if not archive.is_file():
        raise ReleaseError(f"release archive does not exist: {archive}")
    if archive.stat().st_size > MAX_RELEASE_ARCHIVE_BYTES:
        raise ReleaseError("release archive exceeds compressed size limit")

    with zipfile.ZipFile(archive, "r") as bundle:
        infos = bundle.infolist()
        if len(infos) > MAX_RELEASE_FILES + 1:
            raise ReleaseError("release archive exceeds file-count limit")
        names = [info.filename for info in infos]
        if len(names) != len(set(names)):
            raise ReleaseError("release archive contains duplicate paths")
        portable_names: dict[str, str] = {}
        total_size = 0
        info_by_name: dict[str, zipfile.ZipInfo] = {}
        for info in infos:
            _validate_zip_info(info)
            name = info.filename
            portable_key = _portable_path_key(name)
            previous = portable_names.get(portable_key)
            if previous is not None and previous != name:
                raise ReleaseError(
                    f"release archive has a portable path collision: {previous!r}, {name!r}"
                )
            portable_names[portable_key] = name
            info_by_name[name] = info
            total_size += info.file_size
            if total_size > MAX_RELEASE_TOTAL_BYTES:
                raise ReleaseError("release archive exceeds total uncompressed size limit")
            if name != MANIFEST_NAME and _excluded(name):
                raise ReleaseError(f"release archive contains an excluded path: {name}")
        portable_keys = set(portable_names)
        for portable_key, name in portable_names.items():
            parts = portable_key.split("/")
            for index in range(1, len(parts)):
                prefix_key = "/".join(parts[:index])
                if prefix_key in portable_keys:
                    raise ReleaseError(
                        "release archive has a file/directory prefix conflict: "
                        f"{portable_names[prefix_key]!r}, {name!r}"
                    )
        if MANIFEST_NAME not in names:
            raise ReleaseError(f"release archive is missing {MANIFEST_NAME}")
        if info_by_name[MANIFEST_NAME].file_size > MAX_RELEASE_MANIFEST_BYTES:
            raise ReleaseError("release manifest exceeds size limit")

        manifest_bytes = bundle.read(MANIFEST_NAME)
        _reject_private_home_bytes(manifest_bytes, MANIFEST_NAME)
        try:
            manifest = json.loads(manifest_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ReleaseError("release manifest is not valid UTF-8 JSON") from exc
        if not isinstance(manifest, dict):
            raise ReleaseError("release manifest root must be an object")
        if type(manifest.get("schema_version")) is not int or manifest.get("schema_version") != 1:
            raise ReleaseError("release manifest has an unsupported schema")
        if manifest.get("product") != PRODUCT_NAME:
            raise ReleaseError("release manifest has an unsupported product")
        if manifest.get("artifact_type") != ARTIFACT_TYPE:
            raise ReleaseError("release manifest has an unsupported schema or artifact type")
        if manifest.get("artifact_audience") != ARTIFACT_AUDIENCE:
            raise ReleaseError("release manifest has an unsupported artifact audience")
        manifest_version = manifest.get("product_version")
        if not isinstance(manifest_version, str) or not re.fullmatch(
            r"[0-9]+\.[0-9]+\.[0-9]+", manifest_version
        ):
            raise ReleaseError("release manifest has an invalid product_version")
        selection_policy = manifest.get("selection_policy")
        if not isinstance(selection_policy, dict) or selection_policy.get("mode") != "allowlist":
            raise ReleaseError("release manifest must declare allowlist selection policy")
        excluded_categories = selection_policy.get("excluded_categories")
        if (
            not isinstance(excluded_categories, list)
            or not excluded_categories
            or any(not isinstance(item, str) or not item for item in excluded_categories)
        ):
            raise ReleaseError("release manifest has invalid excluded policy categories")
        source_commit = manifest.get("source_commit")
        if source_commit is not None and (
            not isinstance(source_commit, str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", source_commit)
        ):
            raise ReleaseError("release manifest has an invalid source_commit")
        if manifest.get("source_dirty") is not None and type(manifest.get("source_dirty")) is not bool:
            raise ReleaseError("release manifest has an invalid source_dirty value")

        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise ReleaseError("release manifest files must be a list")
        expected_names: set[str] = {MANIFEST_NAME}
        entry_paths: list[str] = []
        digest = hashlib.sha256()
        for raw in entries:
            if not isinstance(raw, dict):
                raise ReleaseError("release manifest contains a non-object file entry")
            relative = raw.get("path")
            if not isinstance(relative, str):
                raise ReleaseError("release manifest file path must be a string")
            _validate_relative(relative)
            if relative == MANIFEST_NAME or relative in expected_names:
                raise ReleaseError(f"release manifest contains a duplicate/reserved path: {relative}")
            if not _allowlisted_release_path(relative):
                raise ReleaseError(f"release manifest contains a non-allowlisted path: {relative}")
            expected_names.add(relative)
            entry_paths.append(relative)
            payload = bundle.read(relative) if relative in info_by_name else None
            if payload is None:
                raise ReleaseError(f"release archive is missing manifest file: {relative}")
            _reject_private_home_bytes(payload, relative)
            _reject_release_forbidden_tokens_bytes(payload, relative)
            _reject_metadata_contact_bytes(payload, relative)
            size = raw.get("size")
            if type(size) is not int or size < 0:
                raise ReleaseError(f"invalid size in release manifest for {relative}")
            sha256 = raw.get("sha256")
            if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", sha256):
                raise ReleaseError(f"invalid SHA-256 in release manifest for {relative}")
            if len(payload) != size:
                raise ReleaseError(f"size mismatch for {relative}")
            if not hmac.compare_digest(_sha256_bytes(payload), sha256):
                raise ReleaseError(f"SHA-256 mismatch for {relative}")
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(str(size).encode("ascii"))
            digest.update(b"\0")
            digest.update(sha256.encode("ascii"))
            digest.update(b"\n")

        if entry_paths != sorted(entry_paths):
            raise ReleaseError("release manifest file entries must be sorted")
        if set(names) != expected_names:
            extra = sorted(set(names) - expected_names)
            raise ReleaseError(f"release archive contains unmanifested files: {extra}")
        if type(manifest.get("file_count")) is not int or manifest.get("file_count") != len(entries):
            raise ReleaseError("release manifest file_count is incorrect")
        content_digest = digest.hexdigest()
        if manifest.get("content_digest_sha256") != content_digest:
            raise ReleaseError("release content digest is incorrect")
        packaged_names = expected_names - {MANIFEST_NAME}
        missing_policy_files = sorted(
            (REQUIRED_RELEASE_FILES | PUBLIC_EXTERNAL_FILES) - packaged_names
        )
        if missing_policy_files:
            raise ReleaseError(
                "release archive is missing required policy files: "
                + ", ".join(missing_policy_files)
            )
        if not any(name.startswith("src/") for name in packaged_names):
            raise ReleaseError("release archive is incomplete: src/ is empty")
        if not any(
            name.startswith("database/base/") and name.endswith(".json")
            for name in packaged_names
        ):
            raise ReleaseError("release archive is incomplete: database/base has no JSON corpus")
        packaged_version = _product_version_from_bytes(
            bundle.read("src/dataset_recommender/app/webapp.py"),
            "src/dataset_recommender/app/webapp.py",
        )
        if packaged_version != manifest_version:
            raise ReleaseError(
                "release manifest product_version does not match packaged WEB_API_VERSION"
            )

    return {
        "ok": True,
        "archive": str(archive),
        "archive_sha256": _sha256_file(archive),
        "file_count": len(entries),
        "content_digest_sha256": content_digest,
        "product_version": manifest_version,
        "source_commit": source_commit,
        "source_dirty": manifest.get("source_dirty"),
    }


def verify_sidecar(
    archive: Path,
    sidecar_path: Path | None = None,
    expected_archive_name: str | None = None,
) -> dict[str, object]:
    archive = archive.resolve()
    sidecar = (
        sidecar_path.resolve()
        if sidecar_path is not None
        else archive.with_suffix(archive.suffix + ".sha256")
    )
    expected_name = expected_archive_name or archive.name
    if not archive.is_file() or not sidecar.is_file():
        raise ReleaseError("archive and adjacent .sha256 sidecar are both required")
    if sidecar.stat().st_size > 512:
        raise ReleaseError("archive SHA-256 sidecar is unexpectedly large")
    try:
        lines = sidecar.read_text(encoding="ascii").splitlines()
    except UnicodeDecodeError as exc:
        raise ReleaseError("archive SHA-256 sidecar must be ASCII") from exc
    if len(lines) != 1:
        raise ReleaseError("archive SHA-256 sidecar must contain exactly one line")
    match = re.fullmatch(r"([0-9a-f]{64})  (.+)", lines[0])
    if not match or match.group(2) != expected_name:
        raise ReleaseError("archive SHA-256 sidecar has an invalid format or filename")
    actual = _sha256_file(archive)
    if not hmac.compare_digest(match.group(1), actual):
        raise ReleaseError("archive SHA-256 sidecar does not match the archive")
    return {
        "ok": True,
        "archive": str(archive),
        "archive_sha256": actual,
        "sha256_file": str(sidecar),
        "sidecar_verified": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or verify the allowlisted BioData Agent release candidate."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build", help="Build and immediately verify a release candidate.")
    build.add_argument("--root", type=Path, default=PROJECT_ROOT)
    build.add_argument("--output-dir", type=Path, default=None)
    build.add_argument("--archive-name", default=DEFAULT_ARCHIVE_NAME)
    build.add_argument(
        "--expected-version",
        default=None,
        help="fail unless WEB_API_VERSION exactly matches this version",
    )
    build.add_argument(
        "--replace-existing",
        action="store_true",
        help="explicitly replace a complete same-name pair using transactional rollback",
    )
    verify = subparsers.add_parser("verify", help="Verify a previously built release candidate.")
    verify.add_argument("--archive", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            result = build_release(
                args.root,
                args.output_dir,
                args.archive_name,
                args.expected_version,
                args.replace_existing,
            )
        else:
            result = verify_release(args.archive)
            result.update(verify_sidecar(args.archive))
    except (OSError, ReleaseError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
