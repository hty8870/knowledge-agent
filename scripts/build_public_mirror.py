from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = ROOT / "packaging" / "public-mirror" / "policy.json"
FILES_PATH = ROOT / "packaging" / "public-mirror" / "files.txt"

# 多 profile（2026-09-23 骨肉分离批）：`assembled`（默认，= 现行装配体公开仓，逐位不变）、`skeleton`（骨架公开仓）、`biodata-pack`
# （领域包公开仓）。profile X 读取 policy.<X>.json + files.<X>.txt；assembled 沿用原文件名，行为逐位不变。
# profile 一律显式传参，进程内不留隐式状态（缺省恒为 assembled）。
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _resolve_profile(profile: str | None) -> str:
    chosen = profile or "assembled"
    if not _PROFILE_RE.fullmatch(chosen):
        raise MirrorBuildError(f"invalid mirror profile name: {chosen!r}")
    return chosen


def _policy_path(profile: str) -> Path:
    if profile == "assembled":
        return POLICY_PATH
    return POLICY_PATH.with_name(f"policy.{profile}.json")


def _files_path(profile: str) -> Path:
    if profile == "assembled":
        return FILES_PATH
    return FILES_PATH.with_name(f"files.{profile}.txt")
MANIFEST_PATH = "public-mirror.json"
COMMIT_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
EMAIL_RE = re.compile(
    r"(?i)(?<![A-Z0-9._%+\-])[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}(?![A-Z0-9._%+\-])"
)
IPV4_RE = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
HOME_RE = re.compile(r"(?i)(?:[A-Z]:[\\/]Users[\\/][^<>:\"|?*\\/\s]+|/home/[^/\s]+)")
TEXT_SUFFIXES = {
    ".bat", ".cfg", ".command", ".css", ".csv", ".dockerignore", ".example",
    ".htm", ".html", ".in", ".ini", ".iss", ".isl", ".js", ".json", ".jsonl",
    ".lock", ".manifest", ".markdown", ".md", ".mjs", ".ps1", ".py", ".rst", ".sh",
    ".spec", ".toml", ".ts", ".tsv", ".txt", ".xml", ".yaml", ".yml",
}
TEXT_FILENAMES = {
    ".dockerignore", ".gitattributes", ".gitignore", "Dockerfile", "LICENSE",
}


class MirrorBuildError(RuntimeError):
    pass


def _git(root: Path, *args: str, timeout: int = 60) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise MirrorBuildError(
            f"git {' '.join(args)} failed for {root}: {detail[-1] if detail else 'unknown error'}"
        )
    return result.stdout.strip()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _relative(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("/"):
        raise MirrorBuildError(f"invalid mirror path: {value!r}")
    path = PurePosixPath(value)
    if any(part in ("", ".", "..") for part in path.parts):
        raise MirrorBuildError(f"non-canonical mirror path: {value!r}")
    return path.as_posix()


def load_policy(root: Path = ROOT, profile: str | None = None) -> dict[str, Any]:
    try:
        policy = json.loads((root / _policy_path(_resolve_profile(profile)).relative_to(ROOT)).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MirrorBuildError("cannot read public mirror policy") from exc
    if policy.get("schema_version") != 1 or policy.get("transform_version") != "3":
        raise MirrorBuildError("unsupported public mirror policy schema/transform")
    mappings = policy.get("mappings")
    if not isinstance(mappings, dict):
        raise MirrorBuildError("policy mappings must be an object")
    normalized: dict[str, str] = {}
    for destination, source in mappings.items():
        destination = _relative(destination)
        source = _relative(source)
        if destination in normalized:
            raise MirrorBuildError(f"duplicate mapped destination: {destination}")
        normalized[destination] = source
    policy["mappings"] = normalized
    policy["generated_paths"] = [_relative(x) for x in policy.get("generated_paths") or []]
    if policy["generated_paths"] != [MANIFEST_PATH]:
        raise MirrorBuildError("public-mirror.json must be the only generated path")
    return policy


def load_output_paths(root: Path = ROOT, profile: str | None = None) -> list[str]:
    path = root / _files_path(_resolve_profile(profile)).relative_to(ROOT)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MirrorBuildError(f"cannot read mirror allowlist: {path}") from exc
    output = [_relative(line.strip()) for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if output != sorted(output) or len(output) != len(set(output)):
        raise MirrorBuildError("mirror allowlist must be sorted and unique")
    if MANIFEST_PATH in output:
        raise MirrorBuildError("generated manifest must not appear in copy allowlist")
    return output


def _tracked(root: Path) -> set[str]:
    raw = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), "ls-files", "-z"],
        capture_output=True,
        timeout=60,
    )
    if raw.returncode:
        raise MirrorBuildError(f"cannot enumerate tracked files in {root}")
    return {chunk.decode("utf-8") for chunk in raw.stdout.split(b"\0") if chunk}


def _delivery_ignored(root: Path) -> set[str]:
    """Tracked paths explicitly classified as private by .deliveryignore."""
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), "ls-files", "-z", "-ci",
         "--exclude-from=.deliveryignore"],
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise MirrorBuildError("cannot evaluate .deliveryignore classification")
    return {chunk.decode("utf-8") for chunk in result.stdout.split(b"\0") if chunk}


def _validate_classification(root: Path, paths: list[str], mappings: dict[str, str], *, profile: str = "assembled") -> None:
    """Require every tracked path to be public source or explicitly private.

    This closes the main quality gap of a plain allowlist: a newly added source
    file can no longer disappear from the public repository merely because the
    release operator forgot to edit ``files.txt``.

    多 profile 语义：非装配体 profile 的「私有分类」额外并上**装配体清单**——
    一个 tracked 文件只要声明在任一公开 profile（本 profile 或装配体/其他 profile 经装配体并集）
    或 `.deliveryignore`，即视为已分类；这保证「加文件只进装配体清单」不会让
    skeleton/biodata-pack 的分类门误红，并集覆盖由 `tests/test_public_mirror_profiles.py` 钉死。
    """
    tracked = _tracked(root)
    public_sources = {mappings.get(destination, destination) for destination in paths}
    private_sources = _delivery_ignored(root)
    if profile != "assembled":
        assembled_policy = load_policy(root, "assembled")
        assembled_paths = load_output_paths(root, "assembled")
        private_sources |= {assembled_policy["mappings"].get(d, d) for d in assembled_paths}
    unclassified = sorted(tracked - public_sources - private_sources)
    if unclassified:
        raise MirrorBuildError(
            "tracked paths are not classified for public delivery: " + ", ".join(unclassified[:30])
        )


def _is_text(path: Path) -> bool:
    return path.name in TEXT_FILENAMES or path.suffix.lower() in TEXT_SUFFIXES


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw or raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return None
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
        except UnicodeDecodeError:
            continue
    return None


def _global_ips(text: str) -> list[str]:
    hits: list[str] = []
    context = re.compile(
        r"(?i)(?:https?://|host\b|server\b|endpoint\b|deploy_host|trusted_hosts|公网|服务器|生产)"
    )
    for line in text.splitlines():
        if not context.search(line):
            continue
        for token in IPV4_RE.findall(line):
            try:
                address = ipaddress.ip_address(token)
            except ValueError:
                continue
            if address.is_global:
                hits.append(token)
    return hits


def _portable_key(relative: str) -> str:
    return unicodedata.normalize("NFC", relative).casefold()


def _scan_source(relative: str, path: Path, policy: dict[str, Any]) -> None:
    if not _is_text(path):
        return
    text = _read_text(path)
    if text is None:
        raise MirrorBuildError(f"public source is not UTF-8/GBK scannable: {relative}")
    allowed_private_refs = set(policy.get("allowed_private_reference_paths") or [])
    mechanism_file = relative in allowed_private_refs
    if HOME_RE.search(text) and not mechanism_file:
        raise MirrorBuildError(f"public source contains a user home path: {relative}")
    if relative not in allowed_private_refs:
        for fragment in policy.get("forbidden_text_fragments") or []:
            if fragment in text:
                raise MirrorBuildError(f"public source references private material {fragment!r}: {relative}")
    emails = EMAIL_RE.findall(text)
    allowed_email = relative in set(policy.get("allowed_email_paths") or [])
    def placeholder_email(email: str) -> bool:
        domain = email.rsplit("@", 1)[-1].lower()
        return (
            domain in {"example.com", "example.org", "example.net"}
            or domain.endswith((".example.com", ".example.org", ".example.net", ".invalid"))
            or domain == "users.noreply.github.com"
            or domain == "kernel32.dll"
        )

    unsafe_emails = [email for email in emails if not (allowed_email or placeholder_email(email))]
    if unsafe_emails and not mechanism_file:
        raise MirrorBuildError(f"public source contains an undeclared email at {relative}")
    allowed_ip_prefixes = tuple(policy.get("allowed_global_ip_prefixes") or [])
    allowed_ip_paths = set(policy.get("allowed_global_ip_paths") or [])
    if (_global_ips(text) and not relative.startswith(allowed_ip_prefixes)
            and relative not in allowed_ip_paths and not mechanism_file):
        raise MirrorBuildError(f"public source contains a live global IP at {relative}")


def _tree_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for relative in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(files[relative]).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def render_tree(
    private_root: Path,
    *,
    source_commit: str,
    source_dirty: bool,
    profile: str | None = None,
) -> tuple[dict[str, bytes], dict[str, Any]]:
    profile = _resolve_profile(profile)
    policy = load_policy(private_root, profile)
    paths = load_output_paths(private_root, profile)
    tracked = _tracked(private_root)
    mappings: dict[str, str] = policy["mappings"]
    _validate_classification(private_root, paths, mappings, profile=profile)
    forbidden_prefixes = tuple(policy.get("forbidden_output_prefixes") or [])
    outputs: dict[str, bytes] = {}
    portable: dict[str, str] = {}

    for destination in paths:
        if destination.startswith(forbidden_prefixes):
            raise MirrorBuildError(f"private-only path is present in public allowlist: {destination}")
        source = mappings.get(destination, destination)
        if source not in tracked:
            raise MirrorBuildError(f"mirror source is not tracked: {source} -> {destination}")
        path = private_root / source
        if not path.is_file() or path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise MirrorBuildError(f"mirror source is not a regular file: {source}")
        key = _portable_key(destination)
        if key in portable:
            raise MirrorBuildError(
                f"portable path collision: {portable[key]!r} vs {destination!r}"
            )
        portable[key] = destination
        _scan_source(source, path, policy)
        outputs[destination] = path.read_bytes()

    unmapped_destinations = sorted(set(mappings) - set(paths))
    if unmapped_destinations:
        raise MirrorBuildError("mapped destinations missing from allowlist: " + ", ".join(unmapped_destinations))

    policy_payload = (
        (private_root / _policy_path(profile).relative_to(ROOT)).read_bytes()
        + b"\0"
        + (private_root / _files_path(profile).relative_to(ROOT)).read_bytes()
    )
    runtime_version = _runtime_version(private_root)
    manifest = {
        "schema_version": 2,
        "transform_version": policy["transform_version"],
        "private_repository": policy["private_repository"],
        "public_repository": policy["public_repository"],
        "source_private_commit": source_commit,
        "source_dirty": source_dirty,
        "runtime_version": runtime_version,
        "policy_sha256": _sha256_bytes(policy_payload),
        "tree_sha256": _tree_digest(outputs),
        "file_count": len(outputs),
    }
    outputs[MANIFEST_PATH] = _canonical_json(manifest)
    return outputs, manifest


def _runtime_version(root: Path) -> str:
    text = (root / "src/dataset_recommender/app/webapp.py").read_text(encoding="utf-8")
    match = re.search(r'^WEB_API_VERSION\s*=\s*"([0-9]+\.[0-9]+\.[0-9]+)"', text, re.MULTILINE)
    if not match:
        raise MirrorBuildError("WEB_API_VERSION is missing")
    return match.group(1)


def _source_state(root: Path, *, allow_dirty: bool) -> tuple[str, bool]:
    commit = _git(root, "rev-parse", "HEAD")
    if not COMMIT_RE.fullmatch(commit):
        raise MirrorBuildError("private HEAD is not a full commit id")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=no"))
    if dirty and not allow_dirty:
        raise MirrorBuildError("private tracked tree is dirty; commit before certifying public")
    return commit, dirty


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for relative, payload in files.items():
        destination = root.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def _public_files(root: Path) -> set[str]:
    return _tracked(root)


def compare_public(public_root: Path, expected: dict[str, bytes]) -> list[str]:
    actual_paths = _public_files(public_root)
    expected_paths = set(expected)
    errors: list[str] = []
    for relative in sorted(expected_paths - actual_paths):
        errors.append(f"missing:{relative}")
    for relative in sorted(actual_paths - expected_paths):
        errors.append(f"extra:{relative}")
    for relative in sorted(expected_paths & actual_paths):
        path = public_root.joinpath(*PurePosixPath(relative).parts)
        try:
            payload = path.read_bytes()
        except OSError:
            errors.append(f"unreadable:{relative}")
            continue
        if payload != expected[relative]:
            errors.append(f"content:{relative}")
    return errors


def apply_public(public_root: Path, expected: dict[str, bytes]) -> None:
    if _git(public_root, "status", "--porcelain"):
        raise MirrorBuildError("public worktree must be clean before apply")
    tracked = _public_files(public_root)
    expected_paths = set(expected)
    for relative in sorted(tracked - expected_paths, reverse=True):
        path = public_root.joinpath(*PurePosixPath(relative).parts)
        if path.exists():
            path.unlink()
    for relative in sorted(expected_paths):
        destination = public_root.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = expected[relative]
        if destination.is_file() and destination.read_bytes() == payload:
            continue
        temporary = destination.with_name(f".{destination.name}.mirror.tmp")
        temporary.write_bytes(payload)
        os.replace(temporary, destination)


def snapshot_allowlist(private_root: Path, public_root: Path, profile: str | None = None) -> int:
    profile = _resolve_profile(profile)
    policy = load_policy(private_root, profile)
    paths = sorted(_public_files(public_root) - set(policy["generated_paths"]))
    target = private_root / _files_path(profile).relative_to(ROOT)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(paths) + "\n", encoding="utf-8", newline="\n")
    return len(paths)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="deterministically build the public repository mirror")
    parser.add_argument("--profile", default="assembled",
                        help="mirror profile: assembled（默认，装配体）| skeleton | biodata-pack（policy.<X>.json + files.<X>.txt）")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "apply"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--public-root", type=Path, required=True)
        sub.add_argument("--private-root", type=Path, default=ROOT)
        if name == "check":
            sub.add_argument("--allow-dirty-private", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--private-root", type=Path, default=ROOT)
    validate.add_argument("--allow-dirty-private", action="store_true")
    snapshot = subparsers.add_parser("snapshot-allowlist")
    snapshot.add_argument("--public-root", type=Path, required=True)
    snapshot.add_argument("--private-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)

    try:
        profile = _resolve_profile(args.profile)
        private_root = args.private_root.resolve()
        public_root = args.public_root.resolve() if hasattr(args, "public_root") else None
        if args.command == "snapshot-allowlist":
            count = snapshot_allowlist(private_root, public_root, profile)
            print(f"public mirror allowlist snapshotted: {count} paths")
            return 0
        allow_dirty = bool(getattr(args, "allow_dirty_private", False))
        commit, dirty = _source_state(private_root, allow_dirty=allow_dirty)
        expected, manifest = render_tree(private_root, source_commit=commit, source_dirty=dirty, profile=profile)
        if args.command == "validate":
            print(
                f"public mirror source valid: files={manifest['file_count']} "
                f"tree={manifest['tree_sha256']} source={manifest['source_private_commit']}"
            )
            return 0
        if args.command == "check":
            errors = compare_public(public_root, expected)
            if errors:
                print(f"public mirror differs in {len(errors)} paths")
                for error in errors[:100]:
                    print(f"  {error}")
                return 1
            print(
                f"public mirror exact: files={manifest['file_count']} "
                f"tree={manifest['tree_sha256']} source={manifest['source_private_commit']}"
            )
            return 0
        apply_public(public_root, expected)
        print(
            f"public mirror applied: files={manifest['file_count']} "
            f"tree={manifest['tree_sha256']} source={manifest['source_private_commit']}"
        )
        return 0
    except MirrorBuildError as exc:
        print(f"public mirror build failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
