# -*- coding: utf-8 -*-
"""元数据兼容分组。诚实边界：只回「元数据兼容」（必要非充分），始终带 caveat，绝不说「可整合」。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.content import compatibility  # noqa: E402
from dataset_recommender.corpus.corpus import load_full_corpus  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.normalizer import is_missing_value  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


def _corpus():
    s = get_settings()
    return load_full_corpus(s.data_dir, s.project_root)


def _seed_uid(corpus):
    """挑一个 species + chemistry 都非空的种子，保证能算出兼容集。"""
    for r in corpus:
        raw = r.raw if isinstance(r.raw, dict) else {}
        sp = str(r.species or "").strip()
        chem = str(r.chemistry or "").strip()
        uid = str(raw.get("dataset_uid") or "")
        if uid and sp and not is_missing_value(sp) and chem and not is_missing_value(chem):
            return uid
    return ""


def test_find_compatible_shape_and_caveat():
    corpus = _corpus()
    uid = _seed_uid(corpus)
    assert uid, "找不到可用种子"
    res = compatibility.find_compatible(uid, corpus, limit=10)
    assert res is not None
    assert res["caveat"] and "必要非充分" in res["caveat"]      # 诚实边界常显
    assert isinstance(res["compatible"], list) and len(res["compatible"]) <= 10
    assert res["seed"]["dataset_uid"] == uid
    # 每个兼容项都带凭据、且不是种子自己
    for c in res["compatible"]:
        assert c["dataset_uid"] != uid
        assert c["_compat_basis"]


def test_find_compatible_shares_species():
    corpus = _corpus()
    uid = _seed_uid(corpus)
    res = compatibility.find_compatible(uid, corpus, limit=50)
    seed_species = compatibility._tokens(res["seed"]["species"])
    for c in res["compatible"]:
        assert compatibility._tokens(c["species"]) & seed_species   # 至少共享一个物种


def test_find_compatible_unknown_uid_none():
    assert compatibility.find_compatible("___nope___", _corpus()) is None


def test_api_compatible_endpoint():
    client = TestClient(app, base_url="http://127.0.0.1")
    uid = _seed_uid(_corpus())
    r = client.get("/api/compatible", params={"uid": uid, "limit": 5}).json()
    assert r["ok"] and "caveat" in r and isinstance(r["compatible"], list)
    # 缺 uid → 400
    assert client.get("/api/compatible").status_code == 400
    # 未知 uid → 404
    assert client.get("/api/compatible", params={"uid": "___nope___"}).status_code == 404


def test_mcp_find_compatible_tool():
    from dataset_recommender.app import mcp_server as M
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    uid = _seed_uid(_corpus())
    out = M.find_compatible_datasets(uid=uid, limit=5)
    assert out["ok"] and out["caveat"] and isinstance(out["compatible"], list)
    with pytest.raises(ToolError):
        M.find_compatible_datasets(uid="   ")       # empty_key
    with pytest.raises(ToolError):
        M.find_compatible_datasets(uid="___nope___")  # not_found
