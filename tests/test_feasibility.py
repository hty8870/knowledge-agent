# -*- coding: utf-8 -*-
"""可行性概览。诚实约束：总细胞量必然是**下限**（AE 等无细胞数），显式标注、绝不当总量。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.retrieval import feasibility  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

_SURVIVORS = [
    {"species": "Homo sapiens", "platform_family": "Chromium", "source": "CELLxGENE Discover",
     "count": "10000", "unit": "cells", "download_url": "http://x", "published_date": "2021-05-01", "has_raw_data": False},
    {"species": "Homo sapiens", "platform_family": "", "source": "ArrayExpress",
     "count": "", "unit": "", "url": "http://y", "published_date": "2020-01-01"},   # AE：无细胞数
    {"species": "Mus musculus", "platform_family": "Chromium", "source": "10x Genomics",
     "count": "5000", "unit": "cells", "download_url": "http://z", "published_date": "2022-03-01", "has_raw_data": True},
]


def test_total_cells_is_a_lower_bound_excluding_no_count():
    r = feasibility.build_report(_SURVIVORS, 3, truncated=False)
    assert r["total_cells_lower_bound"] == 15000        # 只加有细胞数的两条
    assert r["datasets_with_cell_count"] == 2
    assert r["datasets_without_cell_count"] == 1        # AE 那条
    assert any("下限" in g and "ArrayExpress" in g for g in r["gaps"])   # 显式标注 AE 无细胞数
    assert "下限" in r["caveat"]


def test_distributions_and_rates():
    r = feasibility.build_report(_SURVIVORS, 3, truncated=False)
    sp = {x["value"]: x["count"] for x in r["species"]}
    assert sp["Homo sapiens"] == 2 and sp["Mus musculus"] == 1
    assert {x["value"] for x in r["sources"]} == {"CELLxGENE Discover", "ArrayExpress", "10x Genomics"}
    assert {x["value"] for x in r["years"]} == {"2020", "2021", "2022"}
    assert r["downloadable_count"] == 3
    assert r["fastq_count"] == 1


def test_truncation_flagged():
    r = feasibility.build_report(_SURVIVORS, 9000, truncated=True)
    assert r["truncated"] is True
    assert any("只统计了前" in g for g in r["gaps"])
    assert r["candidate_count"] == 9000 and r["aggregated_count"] == 3


def test_empty_survivors():
    r = feasibility.build_report([], 0, truncated=False)
    assert r["candidate_count"] == 0 and r["total_cells_lower_bound"] == 0
    assert r["species"] == [] and r["caveat"]


def test_api_feasibility_endpoint():
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/feasibility", json={"query": "人类肺癌数据", "sources": ["10x Genomics"]}).json()
    assert r["ok"] and "report" in r
    rep = r["report"]
    for k in ("candidate_count", "total_cells_lower_bound", "species", "sources", "gaps", "caveat"):
        assert k in rep
    # 缺 query → 拒绝（空串被 pydantic 422，纯空白被端点 strip 后 400）
    assert client.post("/api/feasibility", json={"query": ""}).status_code == 422
    assert client.post("/api/feasibility", json={"query": "   "}).status_code == 400


def test_mcp_assess_feasibility_tool():
    from dataset_recommender.app import mcp_server as M
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    out = M.assess_feasibility(query="人类肺癌数据", sources=["10x Genomics"])
    assert out["ok"] and "report" in out and "caveat" in out["report"]
    with pytest.raises(ToolError):
        M.assess_feasibility(query="   ")
    # 未知/拼错来源必须 fail-loud（bad_source），绝不静默过滤成空报告 → 假「无可复用数据」结论。
    with pytest.raises(ToolError, match="bad_source"):
        M.assess_feasibility(query="人类肺癌数据", sources=["NotARealSource"])
