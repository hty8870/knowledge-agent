# -*- coding: utf-8 -*-
"""检索入参校验单一真源（app/request_validation， 批）的三层钉：

1. 校验束单元：query 四道闸 / ISO 日期 / 倒挂窗口 / 来源（形状/空白/未知）的逐条行为与机器码；
2. 漂移修复回归：Web feasibility 补来源校验与倒挂窗口、task-pack 补倒挂窗口、recommend 补
   控制字符与纯符号闸——这四处在 前是 Web/MCP 行为级漂移（一端拒收一端静默跑），
   现在两端同源，本文件钉死 Web 侧不再回退到「静默判负」；
3. X-Error-Code 头：Web 错误翻译 additive 带机器码（此前只在 MCP 端保留）。

TestClient 必须 base_url='http://127.0.0.1'（Host 守卫）；LLM 一律关（纯校验路径不触发）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.app.request_validation import (
    MAX_QUERY_CHARS,
    ParamValidationError,
    validate_date_window,
    validate_iso_date,
    validate_query,
    validate_sources,
)
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow, RecommendParams
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


# ---------------------------------------------------------------- 校验束单元
def test_validate_query_four_gates():
    assert validate_query("人类肺癌单细胞") == "人类肺癌单细胞"
    with pytest.raises(ParamValidationError, match="不能为空"):
        validate_query("   ")
    with pytest.raises(ParamValidationError) as ei:
        validate_query("肺癌\u200b数据")  # 零宽空格（Cf）
    assert ei.value.code == "bad_query"
    with pytest.raises(ParamValidationError) as ei:
        validate_query("。。。？！")
    assert ei.value.code == "bad_query"
    with pytest.raises(ParamValidationError) as ei:
        validate_query("肺" * (MAX_QUERY_CHARS + 1))
    assert ei.value.code == "bad_query"
    # 制表/换行/回车是唯一放行的控制类字符（与 MCP 历史口径逐位一致）
    assert validate_query("肺癌\t数据") == "肺癌\t数据"


def test_validate_iso_date_strict():
    assert validate_iso_date(None, name="date_from") == ""
    assert validate_iso_date("  ", name="date_from") == ""
    assert validate_iso_date("2024-01-01", name="date_from") == "2024-01-01"
    for bad in ("not-a-date", "2024/01/01", "2024-02-30"):
        with pytest.raises(ParamValidationError) as ei:
            validate_iso_date(bad, name="date_from")
        assert ei.value.code == "bad_param"


def test_validate_date_window_inverted():
    validate_date_window("2020-01-01", "2024-01-01")  # 正常窗口放行
    validate_date_window("", "2024-01-01")            # 单边/空放行
    with pytest.raises(ParamValidationError, match="颠倒"):
        validate_date_window("2024-01-01", "2020-01-01")


_KNOWN = ["10x Genomics", "CELLxGENE Discover"]


def test_validate_sources_gates():
    validate_sources(None, known=_KNOWN)
    validate_sources([], known=_KNOWN)
    validate_sources(["10x Genomics"], known=_KNOWN)
    with pytest.raises(ParamValidationError) as ei:  # 形状闸
        validate_sources("10x Genomics", known=_KNOWN)
    assert ei.value.code == "bad_param"
    with pytest.raises(ParamValidationError) as ei:
        validate_sources(["  "], known=_KNOWN)
    assert ei.value.code == "bad_source"
    with pytest.raises(ParamValidationError) as ei:
        validate_sources(["不存在的来源XYZ"], known=_KNOWN)
    assert ei.value.code == "bad_source"
    assert "不存在的来源XYZ" in ei.value.hint and "收录" in ei.value.hint


# ------------------------------------------------------- 漂移修复回归（Web 侧）
def test_feasibility_unknown_source_now_rejected():
    """ 漂移修复：拼错来源此前静默归零候选（冒充「这方向没数据」），必须 400 点名。"""
    res = client.post("/api/feasibility", json={"query": "人类肺数据", "sources": ["不存在的来源XYZ"]})
    assert res.status_code == 400, res.text[:200]
    assert res.headers.get("X-Error-Code") == "bad_source"
    # 合法来源对照：不误伤（200 且走真实确定性检索）
    ok = client.post("/api/feasibility", json={"query": "人类肺数据", "sources": ["10x Genomics"]})
    assert ok.status_code == 200, ok.text[:200]


def test_feasibility_inverted_date_window_rejected():
    res = client.post("/api/feasibility", json={
        "query": "人类肺数据", "date_from": "2024-01-01", "date_to": "2020-01-01"})
    assert res.status_code == 400, res.text[:200]
    assert res.headers.get("X-Error-Code") == "bad_param"
    assert "颠倒" in res.json()["detail"]


def test_task_pack_preview_inverted_date_window_rejected():
    """ 漂移修复：task-pack 此前只查格式不查倒挂（MCP 端有闸、Web 端没有）。"""
    res = client.post("/api/task-pack/preview", json={
        "query": "人类肺数据", "date_from": "2024-01-01", "date_to": "2020-01-01"})
    assert res.status_code == 400, res.text[:200]
    assert "颠倒" in res.json()["detail"]


def test_recommend_control_char_and_pure_symbol_now_rejected():
    """ 口径统一：控制/不可见字符与纯符号 query 此前只在 MCP 被拒、Web 照跑。"""
    res = client.post("/api/recommend", json={"query": "肺癌\u200b数据", "use_llm": False})
    assert res.status_code == 400
    assert res.headers.get("X-Error-Code") == "bad_query"
    res2 = client.post("/api/recommend", json={"query": "。。。？！", "use_llm": False})
    assert res2.status_code == 400
    assert res2.headers.get("X-Error-Code") == "bad_query"


def test_recommend_too_long_query_keeps_rejection_with_code_header():
    res = client.post("/api/recommend", json={"query": "肺" * 2001, "use_llm": False})
    assert res.status_code == 400
    assert res.headers.get("X-Error-Code") == "bad_query"
    assert "2000" in res.json()["detail"]


# ------------------------------------------------------- RecommendParams 适配层
def test_recommend_params_defaults_single_truth():
    """默认值单一真源在 dataclass 上（防再漂）：与历史 kwargs 默认逐位一致。"""
    p = RecommendParams(query="q")
    assert p.top_k is None and p.use_llm is None
    assert p.rerank_backend == "off" and p.recall_backend == "off"
    assert p.strategy == "fixed" and p.preferred_recall == "cross_encoder"
    assert p.sources is None and p.auto_parse_sources is False
    assert p.base_llm_config is None


def test_run_with_meta_kwargs_compat_channel():
    """kwargs 兼容通道：dict 解包/未知字段 fail-closed/双传即 TypeError（存量测试依赖此通道）。"""
    wf = DatasetRecommendationWorkflow()
    a = wf.run_with_meta(query="人类肺癌单细胞", top_k=3, use_llm=False)
    b = wf.run_with_meta(RecommendParams(query="人类肺癌单细胞", top_k=3, use_llm=False))
    assert [r.get("dataset_uid") for r in a.retrieved_data] == \
           [r.get("dataset_uid") for r in b.retrieved_data]
    with pytest.raises(TypeError):  # 未知字段：dataclass 构造 fail-closed
        wf.run_with_meta(query="q", nonexistent_lever=True)
    with pytest.raises(TypeError):  # 双传即误用
        wf.run_with_meta(RecommendParams(query="q"), top_k=5)
