"""自助 LLM 端点：网页填入安全的 base_url/model → 临时 env → LLMConfig。

钉死三点：
1) openai-compatible：base_url/model 同时覆盖高优先级 LLM_* 与 OPENAI_* env。
2) 缺省（留空）时**不写** override → 绝不 pop 掉服务器 .env 里已有的端点配置。
3) 临时 key 同时覆盖 LLM_API_KEY 与供应商 key；zhipuai/openai-compatible 仍各自分流。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.llm.llm_client import load_llm_config  # noqa: E402
from dataset_recommender.app.webapp import (  # noqa: E402
    RecommendRequest,
    _build_request_overrides,
    _temporary_env,
    app as _app,
)

_client = TestClient(_app, base_url="http://127.0.0.1")


def test_request_model_accepts_base_url_and_model():
    req = RecommendRequest(query="x", provider="openai-compatible", base_url="https://api.deepseek.com/v1", model="deepseek-chat")
    assert req.base_url == "https://api.deepseek.com/v1"
    assert req.model == "deepseek-chat"
    # 缺省仍为 None（不影响旧调用）
    assert RecommendRequest(query="x").base_url is None
    assert RecommendRequest(query="x").model is None
    assert RecommendRequest(query="x").provider == "mock"
    assert RecommendRequest(query="x").use_llm is False


def test_custom_openai_endpoint_flows_into_llm_config():
    ov = _build_request_overrides(
        provider="openai-compatible", use_llm=True, mock_llm=False,
        api_key="sk-test", base_url="https://api.deepseek.com/v1", model="deepseek-chat",
    )
    assert ov["OPENAI_BASE_URL"] == "https://api.deepseek.com/v1"
    assert ov["LLM_BASE_URL"] == "https://api.deepseek.com/v1"
    assert ov["OPENAI_MODEL"] == "deepseek-chat"
    assert ov["LLM_MODEL"] == "deepseek-chat"
    assert ov["LLM_API_KEY"] == "sk-test"
    assert ov["OPENAI_API_KEY"] == "sk-test"
    with _temporary_env(ov):
        cfg = load_llm_config()
        assert cfg.provider == "openai-compatible"
        assert cfg.base_url == "https://api.deepseek.com/v1"
        assert cfg.model == "deepseek-chat"
        assert cfg.api_key == "sk-test"


def test_empty_base_url_does_not_pop_server_config(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://preset.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "preset-model")
    ov = _build_request_overrides(
        provider="openai-compatible", use_llm=True, mock_llm=False,
        api_key=None, base_url="", model="",
    )
    # 留空 → 不写这两个 key（_temporary_env 只在 key 存在时才动 env；缺省即保留服务器配置）
    assert "OPENAI_BASE_URL" not in ov
    assert "OPENAI_MODEL" not in ov
    with _temporary_env(ov):
        cfg = load_llm_config()
        assert cfg.base_url == "https://preset.example/v1"
        assert cfg.model == "preset-model"


def test_zhipuai_custom_routes_to_zhipu_env_not_openai():
    ov = _build_request_overrides(
        provider="zhipuai", use_llm=True, mock_llm=False,
        api_key="zk", base_url="https://zhipu.custom/api/v4/", model="glm-x",
    )
    assert ov["ZHIPUAI_BASE_URL"] == "https://zhipu.custom/api/v4/"
    assert ov["LLM_BASE_URL"] == "https://zhipu.custom/api/v4/"
    assert ov["ZHIPUAI_MODEL"] == "glm-x"
    assert ov["LLM_MODEL"] == "glm-x"
    assert ov["LLM_API_KEY"] == "zk"
    assert ov["ZAI_API_KEY"] == "zk"
    # 绝不串到 openai 分支
    assert "OPENAI_BASE_URL" not in ov
    assert "OPENAI_MODEL" not in ov


def test_mock_provider_ignores_base_url_model():
    ov = _build_request_overrides(
        provider="mock", use_llm=True, mock_llm=True,
        api_key=None, base_url="https://whatever/v1", model="ignored",
    )
    # mock 路径不需要端点/模型，不注入任何 base/model env
    assert "OPENAI_BASE_URL" not in ov
    assert "OPENAI_MODEL" not in ov
    assert "ZHIPUAI_BASE_URL" not in ov
    assert ov["MOCK_LLM"] == "true"


# ---------------------------------------------------- provider 缺省静默 mock 留痕

_MOCK_NOTICE = "未指定 provider，本次按 mock 处理"


def test_use_llm_without_provider_warns_about_implicit_mock():
    """use_llm=true 但不带 provider → 静默拽回 mock 必须在 warnings 留痕（行为不变）。"""
    res = _client.post("/api/recommend", json={"query": "人类肺组织的单细胞数据", "use_llm": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["provider"] == "mock"
    assert any(_MOCK_NOTICE in w for w in body["warnings"])


def test_explicit_mock_provider_does_not_warn():
    """显式 provider="mock" 是调用方的明确选择，不留痕。"""
    res = _client.post(
        "/api/recommend",
        json={"query": "人类肺组织的单细胞数据", "use_llm": True, "provider": "mock"},
    )
    assert res.status_code == 200, res.text
    assert not any(_MOCK_NOTICE in w for w in res.json()["warnings"])


def test_explicit_mock_llm_flag_does_not_warn():
    """显式 mock_llm=true 同样是明确选择，不留痕。"""
    res = _client.post(
        "/api/recommend",
        json={"query": "人类肺组织的单细胞数据", "mock_llm": True},
    )
    assert res.status_code == 200, res.text
    assert not any(_MOCK_NOTICE in w for w in res.json()["warnings"])


def test_use_llm_false_does_not_warn():
    """没开 LLM 时 provider 缺省无关紧要，不留痕。"""
    res = _client.post("/api/recommend", json={"query": "人类肺组织的单细胞数据", "use_llm": False})
    assert res.status_code == 200, res.text
    assert not any(_MOCK_NOTICE in w for w in res.json()["warnings"])
