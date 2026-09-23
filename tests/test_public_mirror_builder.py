from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "build_public_mirror", ROOT / "scripts" / "build_public_mirror.py"
)
assert SPEC and SPEC.loader
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", f"safe.directory={root}", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _private_fixture(root: Path) -> str:
    paths = {
        ".deliveryignore": ".deliveryignore\n",
        "README.md": "# Public-safe source\n",
        "src/dataset_recommender/app/webapp.py": 'WEB_API_VERSION = "9.0.0"\n',
        "eval/evaluation-manifest.public.json": '{"audience":"public"}\n',
        "packaging/public-mirror/policy.json": json.dumps(
            {
                "schema_version": 1,
                "transform_version": "3",
                "private_repository": "hty8870/biodata-agent-private",
                "public_repository": "hty8870/biodata-agent",
                "mappings": {
                    "eval/evaluation-manifest.json": "eval/evaluation-manifest.public.json"
                },
                "generated_paths": ["public-mirror.json"],
                "forbidden_output_prefixes": ["private/"],
                "forbidden_text_fragments": ["private/"],
                "allowed_email_paths": [],
                "allowed_global_ip_prefixes": [],
                "allowed_global_ip_paths": [],
                "allowed_private_reference_paths": ["packaging/public-mirror/policy.json"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        "packaging/public-mirror/files.txt": (
            "README.md\n"
            "eval/evaluation-manifest.json\n"
            "packaging/public-mirror/files.txt\n"
            "packaging/public-mirror/policy.json\n"
            "src/dataset_recommender/app/webapp.py\n"
        ),
    }
    for relative, text in paths.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "init")
    _git(root, "config", "user.name", "fixture")
    _git(root, "config", "user.email", "fixture@example.com")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "fixture")
    return _git(root, "rev-parse", "HEAD")


def test_render_is_deterministic_and_applies_only_declared_mapping(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)

    first, first_manifest = mirror.render_tree(root, source_commit=commit, source_dirty=False)
    second, second_manifest = mirror.render_tree(root, source_commit=commit, source_dirty=False)

    assert first == second
    assert first_manifest == second_manifest
    assert first["README.md"] == b"# Public-safe source\n"
    assert first["eval/evaluation-manifest.json"] == b'{"audience":"public"}\n'
    assert "eval/evaluation-manifest.public.json" not in first
    assert first_manifest["source_private_commit"] == commit
    assert first_manifest["source_dirty"] is False
    assert first_manifest["file_count"] == 5


def test_exact_compare_rejects_extra_missing_and_changed_paths(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    commit = _private_fixture(private)
    expected, _ = mirror.render_tree(private, source_commit=commit, source_dirty=False)
    public = tmp_path / "public"
    public.mkdir()
    mirror._write_tree(public, expected)
    _git(public, "init")
    _git(public, "config", "user.name", "fixture")
    _git(public, "config", "user.email", "fixture@example.com")
    _git(public, "add", "-A")
    _git(public, "commit", "-m", "fixture")
    assert mirror.compare_public(public, expected) == []

    (public / "README.md").write_text("changed\n", encoding="utf-8")
    (public / "extra.txt").write_text("extra\n", encoding="utf-8")
    _git(public, "add", "extra.txt")
    errors = mirror.compare_public(public, expected)
    assert "content:README.md" in errors
    assert "extra:extra.txt" in errors


@pytest.mark.parametrize(
    "payload, message",
    [
        ("contact real.person@live-domain.test\n", "undeclared email"),
        ("production endpoint http://8.8.8.8/v1\n", "live global IP"),
        ("see private/design.md\n", "private material"),
        ("path " + "C:" + "\\Users\\alice\\secret.txt\n", "user home path"),
    ],
)
def test_public_surface_scan_fails_closed(tmp_path: Path, payload: str, message: str) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)
    (root / "README.md").write_text(payload, encoding="utf-8")
    with pytest.raises(mirror.MirrorBuildError, match=message):
        mirror.render_tree(root, source_commit=commit, source_dirty=True)


def test_allowlist_rejects_noncanonical_or_unsorted_paths(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    _private_fixture(root)
    (root / "packaging/public-mirror/files.txt").write_text(
        "src/dataset_recommender/app/webapp.py\nREADME.md\n", encoding="utf-8"
    )
    with pytest.raises(mirror.MirrorBuildError, match="sorted and unique"):
        mirror.load_output_paths(root)


def test_new_tracked_path_must_be_public_or_explicitly_private(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    commit = _private_fixture(root)
    extra = root / "src" / "dataset_recommender" / "new_feature.py"
    extra.write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", extra.relative_to(root).as_posix())
    _git(root, "commit", "-m", "unclassified")
    with pytest.raises(mirror.MirrorBuildError, match="not classified"):
        mirror.render_tree(root, source_commit=commit, source_dirty=False)
