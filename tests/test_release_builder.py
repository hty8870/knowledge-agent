from __future__ import annotations

import importlib.util
import hashlib
import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "scripts" / "build_release.py"
SPEC = importlib.util.spec_from_file_location("build_release", SCRIPT)
assert SPEC and SPEC.loader
build_release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_release
SPEC.loader.exec_module(build_release)


def _minimal_project(root: Path) -> None:
    (root / "src" / "dataset_recommender" / "app").mkdir(parents=True)
    (root / "web" / "static").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    (root / "src" / "dataset_recommender" / "app" / "mcp_server.py").write_text(
        "print('ok')\n", encoding="utf-8"
    )
    (root / "src" / "dataset_recommender" / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    (root / "src" / "dataset_recommender" / "app" / "webapp.py").write_text(
        'WEB_API_VERSION = "1.2.0"\n', encoding="utf-8"
    )
    (root / "web" / "static" / "index.html").write_text("<!doctype html>\n", encoding="utf-8")
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


def _rewrite_self_consistent_candidate(
    original: Path,
    rewritten: Path,
    mutate,
) -> None:
    with zipfile.ZipFile(original) as source:
        payloads = {
            name: source.read(name)
            for name in source.namelist()
            if name != build_release.MANIFEST_NAME
        }
        manifest = json.loads(
            source.read(build_release.MANIFEST_NAME).decode("utf-8")
        )

    mutate(payloads, manifest)
    entries = [
        {
            "path": name,
            "size": len(payloads[name]),
            "sha256": hashlib.sha256(payloads[name]).hexdigest(),
        }
        for name in sorted(payloads)
    ]
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode("ascii"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\n")
    manifest["files"] = entries
    manifest["file_count"] = len(entries)
    manifest["content_digest_sha256"] = digest.hexdigest()
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")

    with zipfile.ZipFile(rewritten, "w") as target:
        for name in sorted(payloads):
            target.writestr(build_release._zip_info(name), payloads[name])
        target.writestr(
            build_release._zip_info(build_release.MANIFEST_NAME), manifest_bytes
        )


def test_build_is_allowlisted_verified_and_deterministic(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    (root / ".env.example").write_text("LLM_API_KEY=\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=do-not-ship\n", encoding="utf-8")
    (root / "models").mkdir()
    (root / "models" / "weights.bin").write_bytes(b"model")
    (root / "outputs").mkdir()
    (root / "outputs" / "trace.log").write_text("private", encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "private_note.md").write_text("private", encoding="utf-8")
    (root / "database" / "external" / "private-upload.json").write_text(
        '[{"source":"user upload","private":true}]\n', encoding="utf-8"
    )
    (root / "协同").mkdir()
    (root / "协同" / "claim.md").write_text("internal", encoding="utf-8")

    first = build_release.build_release(root, tmp_path / "out1")
    second = build_release.build_release(root, tmp_path / "out2")

    assert first["ok"] is True
    assert first["archive_sha256"] == second["archive_sha256"]
    archive = Path(first["archive"])
    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
        assert ".env.example" in names
        assert ".env" not in names
        assert "models/weights.bin" not in names
        assert "outputs/trace.log" not in names
        assert "database/base/10x-Visium.json" in names
        assert "database/external/README.md" in names
        assert "database/external/arrayexpress.json" in names
        assert "database/external/private-upload.json" not in names
        assert "docs/private_note.md" not in names
        manifest = json.loads(bundle.read(build_release.MANIFEST_NAME).decode("utf-8"))
    assert manifest["selection_policy"]["mode"] == "allowlist"
    assert manifest["product_version"] == "1.2.0"
    assert manifest["file_count"] == len(manifest["files"])
    assert Path(first["sha256_file"]).read_text(encoding="ascii").startswith(first["archive_sha256"])
    assert first["sidecar_verified"] is True


def test_verify_rejects_unmanifested_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    original = Path(result["archive"])
    tampered = tmp_path / "tampered.zip"

    with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            target.writestr(info, source.read(info.filename))
        target.writestr(
            build_release._zip_info("unexpected.txt"), b"not in manifest"
        )

    with pytest.raises(build_release.ReleaseError, match="unmanifested"):
        build_release.verify_release(tampered)


def test_verify_rejects_path_traversal_before_manifest_parsing(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", b"unsafe")

    with pytest.raises(build_release.ReleaseError, match="unsafe archive path"):
        build_release.verify_release(archive)


def test_verify_rejects_non_object_manifest_without_traceback(tmp_path: Path) -> None:
    archive = tmp_path / "manifest-list.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(
            build_release._zip_info(build_release.MANIFEST_NAME), b"[]\n"
        )

    with pytest.raises(build_release.ReleaseError, match="root must be an object"):
        build_release.verify_release(archive)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda _payloads, manifest: manifest.update(product="Other"), "unsupported product"),
        (
            lambda _payloads, manifest: manifest.update(
                selection_policy={"mode": "everything", "excluded_categories": ["none"]}
            ),
            "allowlist selection policy",
        ),
    ],
)
def test_verify_rejects_self_consistent_manifest_policy_lies(
    tmp_path: Path,
    mutation,
    message: str,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "policy-lie.zip"
    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, mutation)

    with pytest.raises(build_release.ReleaseError, match=message):
        build_release.verify_release(rewritten)


def test_verify_rejects_self_consistent_but_incomplete_candidate(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "incomplete.zip"

    def omit_readme(payloads, _manifest):
        payloads.pop("README.md")

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, omit_readme)
    with pytest.raises(build_release.ReleaseError, match="missing required policy files"):
        build_release.verify_release(rewritten)


def test_verify_rejects_self_consistent_non_allowlisted_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "non-allowlisted.zip"

    def add_unreviewed(payloads, _manifest):
        payloads["unreviewed.txt"] = b"not allowlisted"

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, add_unreviewed)
    with pytest.raises(build_release.ReleaseError, match="non-allowlisted"):
        build_release.verify_release(rewritten)


def test_verify_rejects_manifest_version_that_disagrees_with_packaged_code(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "version-lie.zip"

    def lie_about_version(_payloads, manifest):
        manifest["product_version"] = "9.9.9"

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, lie_about_version)
    with pytest.raises(build_release.ReleaseError, match="does not match packaged"):
        build_release.verify_release(rewritten)


def test_verify_rejects_private_home_path_even_when_manifest_is_self_consistent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "private-home.zip"

    def add_private_path(payloads, _manifest):
        private_path = ("C:" + "\\Users\\" + "Alice\\private").encode("ascii")
        payloads["README.md"] += b"\n" + private_path + b"\n"

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, add_private_path)
    with pytest.raises(build_release.ReleaseError, match="user-specific home path"):
        build_release.verify_release(rewritten)


def test_verify_rejects_file_directory_prefix_conflict(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "prefix-conflict.zip"

    def add_conflict(payloads, _manifest):
        payloads["tests/prefix"] = b"file"
        payloads["tests/prefix/child.py"] = b"child"

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, add_conflict)
    with pytest.raises(build_release.ReleaseError, match="file/directory prefix conflict"):
        build_release.verify_release(rewritten)


def test_collect_rejects_symlink_when_supported(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = root / "src" / "leak.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available to this Windows account")

    with pytest.raises(build_release.ReleaseError, match="link/junction"):
        build_release.collect_release_files(root)


def test_archive_name_cannot_escape_output_directory(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    with pytest.raises(build_release.ReleaseError, match="simple .zip filename"):
        build_release.build_release(root, tmp_path / "out", "../escape.zip")


def test_build_rejects_expected_version_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    with pytest.raises(build_release.ReleaseError, match="release version mismatch"):
        build_release.build_release(
            root, tmp_path / "out", expected_version="9.9.9"
        )


def test_build_fails_closed_when_a_reviewed_public_dataset_is_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    missing = root / "database" / "external" / "arrayexpress.json"
    missing.unlink()
    (missing.parent / "upload_20260714_010203_000004_arrayexpress.json").write_text(
        '[{"private":true}]\n', encoding="utf-8"
    )
    with pytest.raises(build_release.ReleaseError, match="missing reviewed public"):
        build_release.build_release(root, tmp_path / "out")


@pytest.mark.parametrize("relative", sorted(build_release.REQUIRED_RELEASE_FILES))
def test_collect_fails_closed_when_any_required_runtime_file_is_missing(
    tmp_path: Path, relative: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    # REQUIRED_RELEASE_FILES 按包内（历史布局）路径组织；仓库源路径经 source_relpath 解析
    # （一级目录整理：requirements/ launchers/ docs/ src 下的新位置）。
    root.joinpath(*Path(build_release.source_relpath(relative)).parts).unlink()
    with pytest.raises(build_release.ReleaseError, match="missing required runtime"):
        build_release.collect_release_files(root)


@pytest.mark.parametrize(
    "unsafe_name",
    [
        "C:/escape.txt",
        "NUL.txt",
        "folder/trailing.",
        "folder/",
        "file.txt:stream",
        "./file.txt",
        "folder//file.txt",
        "tests/COM¹.txt",
        "tests/CONIN$.txt",
        " tests/file.txt",
    ],
)
def test_verify_rejects_windows_unsafe_or_directory_paths(
    tmp_path: Path, unsafe_name: str
) -> None:
    archive = tmp_path / "unsafe-portable.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(unsafe_name, b"unsafe")
    with pytest.raises(build_release.ReleaseError, match="unsafe|forbidden"):
        build_release.verify_release(archive)


def test_verify_rejects_casefold_collisions_and_symlinks(tmp_path: Path) -> None:
    collision = tmp_path / "collision.zip"
    with zipfile.ZipFile(collision, "w") as bundle:
        bundle.writestr(build_release._zip_info("README.md"), b"one")
        bundle.writestr(build_release._zip_info("readme.md"), b"two")
    with pytest.raises(build_release.ReleaseError, match="portable path collision"):
        build_release.verify_release(collision)

    symlink = tmp_path / "symlink.zip"
    info = zipfile.ZipInfo("src/link")
    info.create_system = 3
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(symlink, "w") as bundle:
        bundle.writestr(info, b"../outside")
    with pytest.raises(build_release.ReleaseError, match="non-regular"):
        build_release.verify_release(symlink)


@pytest.mark.parametrize(
    "compression",
    [zipfile.ZIP_STORED, zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA],
)
def test_verify_rejects_non_deflate_compression(
    tmp_path: Path, compression: int
) -> None:
    archive = tmp_path / f"unsupported-compression-{compression}.zip"
    with zipfile.ZipFile(archive, "w", compression=compression) as bundle:
        bundle.writestr(build_release.MANIFEST_NAME, b"{}\n")

    with pytest.raises(build_release.ReleaseError, match="Deflate compression"):
        build_release.verify_release(archive)


def test_verify_sidecar_rejects_digest_or_filename_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    archive = Path(result["archive"])
    sidecar = Path(result["sha256_file"])
    sidecar.write_text("0" * 64 + f"  {archive.name}\n", encoding="ascii")
    with pytest.raises(build_release.ReleaseError, match="does not match"):
        build_release.verify_sidecar(archive)


def test_build_preserves_existing_pair_by_default(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    output = tmp_path / "out"
    result = build_release.build_release(root, output)
    archive = Path(result["archive"])
    sidecar = Path(result["sha256_file"])
    before = (archive.read_bytes(), sidecar.read_bytes())

    with pytest.raises(build_release.ReleaseError, match="already exists"):
        build_release.build_release(root, output)

    assert (archive.read_bytes(), sidecar.read_bytes()) == before


def test_transactional_replace_restores_old_pair_on_final_verification_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    output = tmp_path / "out"
    first = build_release.build_release(root, output)
    archive = Path(first["archive"])
    sidecar = Path(first["sha256_file"])
    before_hashes = (
        build_release._sha256_file(archive),
        build_release._sha256_file(sidecar),
    )
    (root / "README.md").write_text("# Changed candidate\n", encoding="utf-8")

    original_verify = build_release.verify_release

    def fail_after_promotion(path: Path) -> dict[str, object]:
        result = original_verify(path)
        if Path(path).name == build_release.DEFAULT_ARCHIVE_NAME:
            raise build_release.ReleaseError("injected final verification failure")
        return result

    monkeypatch.setattr(build_release, "verify_release", fail_after_promotion)
    with pytest.raises(build_release.ReleaseError, match="injected final"):
        build_release.build_release(root, output, replace_existing=True)

    assert (
        build_release._sha256_file(archive),
        build_release._sha256_file(sidecar),
    ) == before_hashes
    assert not list(output.glob(".*.tmp"))
    assert not list(output.glob(".*.previous"))


def test_transactional_replace_restores_old_pair_on_cancellation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    output = tmp_path / "out"
    first = build_release.build_release(root, output)
    archive = Path(first["archive"])
    sidecar = Path(first["sha256_file"])
    before = (archive.read_bytes(), sidecar.read_bytes())
    (root / "README.md").write_text("# Cancelled candidate\n", encoding="utf-8")

    original_verify = build_release.verify_release

    def interrupt_after_promotion(path: Path) -> dict[str, object]:
        result = original_verify(path)
        if Path(path).name == build_release.DEFAULT_ARCHIVE_NAME:
            raise KeyboardInterrupt("injected cancellation")
        return result

    monkeypatch.setattr(build_release, "verify_release", interrupt_after_promotion)
    with pytest.raises(KeyboardInterrupt, match="injected cancellation"):
        build_release.build_release(root, output, replace_existing=True)

    assert (archive.read_bytes(), sidecar.read_bytes()) == before
    assert not list(output.glob(".*.tmp"))
    assert not list(output.glob(".*.previous"))


def test_build_and_verify_enforce_file_count_and_archive_size_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    monkeypatch.setattr(build_release, "MAX_RELEASE_FILES", 1)
    with pytest.raises(build_release.ReleaseError, match="file-count"):
        build_release.collect_release_files(root)

    archive = tmp_path / "oversized.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("README.md", b"payload")
    monkeypatch.setattr(build_release, "MAX_RELEASE_ARCHIVE_BYTES", 1)
    with pytest.raises(build_release.ReleaseError, match="compressed size"):
        build_release.verify_release(archive)


@pytest.mark.parametrize(
    "private_path",
    [
        "C:" + "\\Users\\" + "Alice\\Desktop\\private",
        "/" + "home/" + "alice/private",
    ],
)
def test_collect_rejects_user_specific_home_paths(
    tmp_path: Path, private_path: str
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    (root / "README.md").write_text(
        f"# Demo\nLocal evidence: {private_path}\n", encoding="utf-8"
    )
    with pytest.raises(build_release.ReleaseError, match="user-specific home path"):
        build_release.collect_release_files(root)


@pytest.mark.skipif(
    not (PROJECT_ROOT / "docs" / "agent").exists(),
    reason="真树收集门依赖本仓不含的发布输入，仅在具备完整发布输入的树可跑",
)
def test_current_project_release_inputs_are_private_home_path_free() -> None:
    files = build_release.collect_release_files(PROJECT_ROOT)
    assert files


# ---------------------------------------------------------------- 内容级敏感词扫描门


def _taint_readme(root: Path, token: str) -> None:
    (root / "README.md").write_text(
        f"# Demo\ninternal remark: {token}\n", encoding="utf-8"
    )


def test_build_rejects_forbidden_token_in_allowlisted_file(tmp_path: Path) -> None:
    """命中样本应 fail：allowlist 选中的文本文件含禁词 → 打包被拦（门生效）。"""
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    _taint_readme(root, build_release.FORBIDDEN_TOKENS[0])
    with pytest.raises(build_release.ReleaseError, match="forbidden internal token"):
        build_release.build_release(root, tmp_path / "out")


def test_verify_rejects_forbidden_token_in_archive(tmp_path: Path) -> None:
    """verify 路径同样拦禁词：一个带禁词的候选包不应验过（纵深防御）。"""
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    rewritten = tmp_path / "tainted.zip"

    def add_token(payloads, _manifest):
        payloads["README.md"] = (
            payloads["README.md"]
            + b"\n"
            + build_release.FORBIDDEN_TOKENS[0].encode("utf-8")
            + b"\n"
        )

    _rewrite_self_consistent_candidate(Path(result["archive"]), rewritten, add_token)
    with pytest.raises(build_release.ReleaseError, match="forbidden internal token"):
        build_release.verify_release(rewritten)


def test_content_gate_allows_clean_allowlist(tmp_path: Path) -> None:
    """干净树应过：无禁词的 allowlist 不触发门。"""
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    result = build_release.build_release(root, tmp_path / "out")
    assert result["ok"] is True


def test_forbidden_token_detector_is_excluded_and_exempted(tmp_path: Path) -> None:
    """防滥用钉：扫描豁免的检测器文件必须同时被排除出释放包（否则禁词会借豁免外泄）。"""
    assert build_release.RELEASE_EXCLUDED_TOOL_FILES
    exemption = next(iter(build_release.RELEASE_EXCLUDED_TOOL_FILES))
    assert build_release._allowlisted_release_path(exemption), (
        "豁免路径应落在 allowlist 覆盖内，否则无需靠 _excluded 显式排除"
    )
    assert build_release._excluded(exemption) is True

    # 检测器文件自身豁免：命中禁词也不报（它是 FORBIDDEN_TOKENS 的定义源）
    build_release._reject_release_forbidden_tokens_bytes(
        ("x\n" + build_release.FORBIDDEN_TOKENS[0] + "\n").encode("utf-8"),
        exemption,
    )
    # 同一串禁词出现在普通文件里必须报
    with pytest.raises(build_release.ReleaseError, match="forbidden internal token"):
        build_release._reject_release_forbidden_tokens_bytes(
            ("x\n" + build_release.FORBIDDEN_TOKENS[0] + "\n").encode("utf-8"),
            "README.md",
        )


def test_delivery_tool_file_is_excluded_from_release_allowlist(tmp_path: Path) -> None:
    """端到端：把内嵌全部禁词的检测器文件种进最小项目，collect 必须排除它、构建必须通过。"""
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    (root / "scripts" / "make_delivery.py").write_text(
        "FORBIDDEN_TOKENS = ("
        + ", ".join(repr(t) for t in build_release.FORBIDDEN_TOKENS)
        + ")\n",
        encoding="utf-8",
    )
    files = build_release.collect_release_files(root)
    rels = {f.relative for f in files}
    assert "scripts/make_delivery.py" not in rels, "检测器文件被纳入释放包——禁词会随包外泄"
    result = build_release.build_release(root, tmp_path / "out")
    assert result["ok"] is True


def test_build_rejects_contact_email_in_metadata_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _minimal_project(root)
    snapshot = root / "database" / "external" / "arrayexpress.json"
    snapshot.write_text('[{"description":"person@example.invalid"}]\n', encoding="utf-8")
    with pytest.raises(build_release.ReleaseError, match="contact email"):
        build_release.build_release(root, tmp_path / "out")
