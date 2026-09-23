# -*- coding: utf-8 -*-
"""「本次查询命中」只读约束：query_parser.active_filters 单一真源，webapp / MCP 一致露出。

背景：数据细化侧栏原本只显示「还没选中的可加筛选」，看不到「这句查询已经命中了哪些硬约束」。
新增 active_filters 把命中约束拍成 chip 列表，前端只读展示 + MCP understood 同步（单一真源，不漂移）。
"""
import json
import sys
from pathlib import Path

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.query_parser import active_filters, parse_query  # noqa: E402

_KM = get_settings().keyword_mapping


def _af(q: str):
    return active_filters(parse_query(q, _KM))


def test_species_disease_fastq():
    dims = {g["dim"]: g for g in _af("推荐有 FASTQ 的人类乳腺癌数据")}
    assert dims["species"]["values"] == ["Human"]
    assert "Breast Cancer" in dims["disease"]["values"]
    assert dims["has_raw_data"]["values"] == ["需要 FASTQ"]
    assert all(g.get("label") and g.get("values") for g in dims.values())


def test_date_range_surfaced():
    d = next((g for g in _af("小鼠大脑 2023 年") if g["dim"] == "date"), None)
    assert d and "2023" in d["values"][0]


def test_vague_query_is_empty():
    assert _af("我想要一些数据") == []


def test_dim_order_stable():
    """维度按 DIMENSIONS 固定序（species 先于 disease），展示稳定。"""
    order = [g["dim"] for g in _af("人类乳腺癌")]
    assert order.index("species") < order.index("disease")


def test_api_and_mcp_share_single_source():
    """/api/recommend 的 query_constraints == MCP understood.active_filters == helper 直算（三处同真源）。"""
    from dataset_recommender.app import mcp_server as M
    from dataset_recommender.app.webapp import RecommendRequest, api_recommend
    q = "推荐有 FASTQ 的人类乳腺癌数据"
    mcp_af = M.parse_constraints(q)["understood"]["active_filters"]
    request = Request({
        "type": "http",
        "headers": [(b"host", b"127.0.0.1")],
        "scheme": "http",
        "server": ("127.0.0.1", 80),
    })
    body = json.loads(api_recommend(RecommendRequest(query=q), request).body.decode("utf-8"))
    assert body["query_constraints"] == mcp_af == _af(q)
    assert body["query_constraints"]   # 非空
