# -*- coding: utf-8 -*-
"""GET /api/citations/download 端点契约。

图内 `cite.export`（LOOP_TOOLS）把引文文件落在服务端 `.userdata/citations/`，
本端点把已写出的文件按名发回浏览器（additive 契约）。白名单纪律：

- 入参 `f` 只接受**裸文件名**（basename）：路径分隔符 `/` `\\`、`..`、`.`、空值一律 400；
- `resolve()` 后必须仍落在 `.userdata/citations/` 内（前缀判定，防符号链接/目录穿越）→ 否则 404；
- 文件不存在 → 404；
- 正常 → 200 + `Content-Disposition: attachment` + 按扩展名的 Content-Type。

测试直接往仓库 `.userdata/citations/` 写唯一命名的临时文件（与真实使用同一目录），
finally 清理；不碰任何冻结基准。"""
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from dataset_recommender.app.webapp import app

ROOT = Path(__file__).resolve().parents[1]
CITATIONS_DIR = ROOT / ".userdata" / "citations"


def _client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


def _unique(name: str) -> str:
    return f"cd1test-{uuid.uuid4().hex[:8]}-{name}"


def test_download_rejects_path_traversal_and_non_basenames() -> None:
    c = _client()
    for bad in ("", ".", "..", "a/b", "a\\b", "../outside.bib", "/etc/passwd",
                "..%2F..%2Fetc%2Fpasswd"):
        r = c.get("/api/citations/download", params={"f": bad})
        # 400 = 结构拒绝；字面量 %2F 名不可能在盘上存在 → 404 同样安全（绝不允许 200/越界读）
        assert r.status_code in (400, 404), f"f={bad!r} 应被拒绝，实际 {r.status_code}"
        assert r.headers.get("content-disposition") is None, f"f={bad!r} 不允许回 attachment"


def test_download_rejects_file_outside_citations_dir() -> None:
    """白名单外的真实文件（如仓库根 README.md）即使文件名合法也必须 404——目录前缀判定。"""
    c = _client()
    r = c.get("/api/citations/download", params={"f": "README.md"})
    assert r.status_code == 404


def test_download_rejects_symlink_escaping_citations_dir() -> None:
    """符号链接指向目录外 → resolve() 后前缀判定必须拦下（Windows 无特权建软链则跳过）。"""
    CITATIONS_DIR.mkdir(parents=True, exist_ok=True)
    target = ROOT / "README.md"
    link = CITATIONS_DIR / _unique("escape.bib")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        import pytest
        pytest.skip("当前环境不允许创建符号链接（Windows 需开发者模式/管理员）")
    try:
        r = _client().get("/api/citations/download", params={"f": link.name})
        assert r.status_code == 404, "软链越出 .userdata/citations/ 必须 404"
    finally:
        link.unlink(missing_ok=True)


def test_download_missing_file_is_404() -> None:
    r = _client().get("/api/citations/download", params={"f": _unique("ghost.bib")})
    assert r.status_code == 404


def test_download_serves_real_citation_with_attachment() -> None:
    CITATIONS_DIR.mkdir(parents=True, exist_ok=True)
    name = _unique("sample.bib")
    body = "@misc{cd1test, title={CD1 Test Dataset}}\n".encode("utf-8")
    path = CITATIONS_DIR / name
    path.write_bytes(body)
    try:
        c = _client()
        r = c.get("/api/citations/download", params={"f": name})
        assert r.status_code == 200
        assert r.content == body, "回传字节必须与落盘文件逐字节一致"
        cd = r.headers.get("content-disposition", "")
        assert cd.startswith("attachment;"), f"必须是 attachment，实际 {cd!r}"
        assert name in cd, "Content-Disposition 应带裸文件名"
        assert r.headers.get("content-type", "").startswith("application/x-bibtex"), (
            f".bib 应给 BibTeX Content-Type，实际 {r.headers.get('content-type')}"
        )
    finally:
        path.unlink(missing_ok=True)


def test_download_serves_ris_with_matching_content_type() -> None:
    CITATIONS_DIR.mkdir(parents=True, exist_ok=True)
    name = _unique("sample.ris")
    body = b"TY  - DATA\nER  -\n"
    path = CITATIONS_DIR / name
    path.write_bytes(body)
    try:
        r = _client().get("/api/citations/download", params={"f": name})
        assert r.status_code == 200
        assert r.content == body
        assert r.headers.get("content-type", "").startswith("application/x-research-info-systems")
    finally:
        path.unlink(missing_ok=True)
