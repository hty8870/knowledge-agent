# -*- coding: utf-8 -*-
"""N10 引文导出（RIS / BibTeX）。核心诚实点：collection_doi 指向**论文**、非数据集本身 →
条目一律**数据集类型**（RIS TY-DATA / BibTeX @misc，绝不 @article），DOI 只作关联论文放 note。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.content.reuse_pack import to_ris, to_bibtex  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

_PACK = {
    "table": [
        {"dataset_name": "Human lung atlas", "source": "CELLxGENE Discover",
         "identifier": "abc-uuid-123", "url": "https://cellxgene.example/x",
         "collection_doi": "10.1000/somepaper"},
        {"dataset_name": "10x demo | pipe", "source": "10x Genomics",
         "identifier": "", "url": "https://10x.example/y", "collection_doi": None},
    ]
}


def test_ris_entries_are_datasets_not_articles():
    ris = to_ris(_PACK)
    assert ris.count("TY  - DATA") == 2
    assert "TY  - JOUR" not in ris        # 绝不把数据集写成期刊文章
    assert "AN  - abc-uuid-123" in ris    # accession
    assert "UR  - https://cellxgene.example/x" in ris


def test_ris_doi_only_as_associated_paper_note():
    ris = to_ris(_PACK)
    # DOI 只出现在 N1 note，且明说是论文不是数据集
    assert "10.1000/somepaper" in ris
    assert "not the dataset" in ris
    # 第二条无 DOI → 不臆造
    assert ris.count("Associated publication DOI") == 1


def test_bibtex_entries_are_misc_not_article():
    bib = to_bibtex(_PACK)
    assert bib.count("@misc{") == 2
    assert "@article" not in bib
    # 绝不把 collection_doi 当数据集自己的 DOI 挂条目（无 entry-level doi = 字段）
    assert "\n  doi = " not in bib and "\n  doi=" not in bib
    # DOI 在 note 里，标注是论文
    assert "Associated publication DOI: 10.1000/somepaper" in bib
    assert "not the dataset" in bib


def test_bibtex_escapes_braces_and_pipes_survive():
    bib = to_bibtex(_PACK)
    # 数据集名里的 | 不该破坏 BibTeX；花括号被转义成圆括号
    assert "10x demo | pipe" in bib
    assert "{" in bib and "}" in bib  # 结构花括号仍在


def test_empty_pack_exports_empty():
    assert to_ris({"table": []}) == ""
    assert to_bibtex({"table": []}) == ""


def test_api_reuse_pack_returns_ris_and_bibtex():
    client = TestClient(app, base_url="http://127.0.0.1")
    ds = client.get("/api/datasets").json()
    uid = ""
    for rec in ds.get("records") or []:
        uid = str(rec.get("dataset_uid") or "").strip()
        if uid:
            break
    assert uid, "没有可用的 dataset_uid 做回归"
    r = client.post("/api/reuse-pack", json={"uids": [uid]}).json()
    assert r["ok"] and "ris" in r and "bibtex" in r
    assert "TY  - DATA" in r["ris"]
    assert "@misc{" in r["bibtex"]
