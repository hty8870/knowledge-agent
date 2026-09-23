# -*- coding: utf-8 -*-
"""p11 · LLM 检索回执层（`/api/search/reply`）的**确定性**测试（无网络）。

镜像 test_act_summary_llm.py 的覆盖：mock 短路（`call_mock_llm` 绝不用于回执改写）、
disabled/no_key 门、成功路径、fail-open（provider 失败/异常→reply_zh=None，异常文本不泄 key）、
prompt 接地护栏（数字/命中关键词逐字进 prompt、0 命中直说没找到、建议白名单硬约束）、
端点 same-origin 403 / extra=forbid 422 / 无 key fail-open / 端到端桩成功。
真 LLM 产出质量只能靠真 provider 复验——不在本门覆盖（见模块头诚实说明）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.llm import act_summary_llm, llm_client, search_reply_llm
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig, LLMResult
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


def _cfg(enable=True, provider="openai-compatible", key="sk-test", mock=False):
    return LLMConfig(enable_llm=enable, mock_llm=mock, provider=provider, api_key=key)


def _facts(total=94, shown=5, status="", has_relax=False, suggest=None):
    if suggest is None:
        suggest = ["继续说一句话细化条件（比如换物种、平台、疾病）"] if total > 0 else []
    return {
        "utterance": "小鼠空间转录组",
        "query": "小鼠 空间转录组",
        "note": "",
        "total": total,
        "shown": shown,
        "hit_keywords": ["Mouse", "Spatial Transcriptomics"],
        "resolution_status": status,
        "has_relax": has_relax,
        "can_suggest": suggest,
    }


# ---------------------------------------------------------------- 门控（经 search_reply_with_llm 全程）

def test_mock_is_short_circuited():
    """mock 一律判否（闸口与执行总结同一份实现——search_reply_llm 复用 act_summary_llm 的判定）。"""
    ok, reason = search_reply_llm.should_use_llm(_cfg(mock=True))
    assert ok is False and reason == "mock_not_used"
    ok2, reason2 = search_reply_llm.should_use_llm(_cfg(provider="mock"))
    assert ok2 is False and reason2 == "mock_not_used"


def test_mock_config_never_calls_provider(monkeypatch):
    """即便显式传 mock config（enable=True），也绝不调任何 provider、零网络。"""
    def _boom(*a, **k):
        raise AssertionError("mock 不该走真 provider")
    # 注意：provider 分发单一真源在 llm_client.call_llm，桩必须打在那里。
    monkeypatch.setattr(llm_client, "call_openai_compatible", _boom)
    monkeypatch.setattr(llm_client, "call_zhipuai", _boom)
    out = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg(mock=True))
    assert out["reply_zh"] is None
    assert out["reply_source"] is None
    assert out["llm_status"] == "mock_not_used"


def test_disabled_and_no_key_blocked():
    out = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg(enable=False))
    assert out["reply_zh"] is None and out["llm_status"] == "disabled"
    out2 = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg(key=None))
    assert out2["reply_zh"] is None and out2["llm_status"] == "no_key"


# ---------------------------------------------------------------- 成功 / fail-open

def test_success_returns_reply(monkeypatch):
    """假 LLM 成功（桩 zhipuai）：reply_zh 有值、source='llm'、status='ok'、带 model。"""
    monkeypatch.setattr(llm_client, "call_zhipuai",
                        lambda prompt, cfg: LLMResult(text="查到 94 条匹配，先给你看前 5 条。",
                                                      attempted=True, succeeded=True,
                                                      response_used=False, provider="zhipuai",
                                                      model="glm-x"))
    out = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg(provider="zhipuai"))
    assert out["reply_zh"] == "查到 94 条匹配，先给你看前 5 条。"
    assert out["reply_source"] == "llm"
    assert out["llm_status"] == "ok"
    assert out["llm_model"] == "glm-x"


def test_fail_open_on_provider_exception_no_key_leak(monkeypatch):
    """provider 抛异常 → fail-open：reply_zh=None、status 以 'error:' 开头、且**不含 key 字样**。"""
    def _boom(*a, **k):
        raise RuntimeError("network down, credential sk-test rejected")
    monkeypatch.setattr(llm_client, "call_openai_compatible", _boom)
    out = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg())
    assert out["reply_zh"] is None
    assert out["reply_source"] is None
    assert out["llm_status"].startswith("error:")
    assert "sk-test" not in out["llm_status"]


def test_fail_open_on_provider_failure_and_empty_text(monkeypatch):
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text=None, attempted=True, succeeded=False,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x", error="HTTP 500 boom"))
    out = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg())
    assert out["reply_zh"] is None
    assert out["llm_status"].startswith("failed:")
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text="   ", attempted=True, succeeded=True,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x"))
    out2 = search_reply_llm.search_reply_with_llm(_facts(), config=_cfg())
    assert out2["reply_zh"] is None
    assert out2["llm_status"].startswith("failed:")


# ---------------------------------------------------------------- prompt 接地护栏

def test_prompt_grounds_numbers_and_keywords_verbatim():
    """命中数/展示数/命中关键词必须**逐字**出现在 prompt 里（接地防编造的确定性证据）。"""
    prompt = search_reply_llm.build_search_reply_prompt(_facts())
    assert "命中条数：94" in prompt
    assert "结果区展示条数：5" in prompt
    assert "Mouse、Spatial Transcriptomics" in prompt
    assert "小鼠空间转录组" in prompt                      # utterance 进 prompt
    assert "小鼠 空间转录组" in prompt                     # 实际检索词进 prompt
    assert "继续说一句话细化条件（比如换物种、平台、疾病）" in prompt   # 建议白名单原行进 prompt


def test_prompt_zero_hit_honesty_and_relax_gate():
    """0 命中：铁律要求直说没有找到；「有放宽方式：是」才许提放宽。弃权/需澄清照事实标出。"""
    prompt0 = search_reply_llm.build_search_reply_prompt(_facts(total=0, shown=0, has_relax=True))
    assert "命中条数：0" in prompt0
    assert "直说没有找到" in prompt0
    assert "有放宽方式：是" in prompt0
    prompt_ab = search_reply_llm.build_search_reply_prompt(_facts(total=0, shown=0, status="abstained"))
    assert "弃权" in prompt_ab
    assert "有放宽方式：否" in prompt_ab
    prompt_cl = search_reply_llm.build_search_reply_prompt(_facts(total=0, shown=0, status="clarification_required"))
    assert "需澄清" in prompt_cl


def test_prompt_suggestion_whitelist_is_a_hard_constraint():
    """建议只能白名单原样挑一条；空名单 → prompt 写死（无）+ 禁止给任何建议。"""
    prompt = search_reply_llm.build_search_reply_prompt(_facts())
    assert "原样挑一条" in prompt
    prompt_none = search_reply_llm.build_search_reply_prompt(_facts(suggest=[]))
    assert "可建议动作：（无）" in prompt_none
    assert "绝对不许给任何建议" in prompt_none


def test_prompt_guardrails_present_and_deterministic():
    prompt = search_reply_llm.build_search_reply_prompt(_facts())
    assert "----- 事实 -----" in prompt
    assert "原文照用" in prompt
    assert "1–2 句" in prompt
    assert "60 个字" in prompt
    assert "用户说" in prompt
    assert prompt == search_reply_llm.build_search_reply_prompt(_facts())


# ---------------------------------------------------------------- 端点

@pytest.fixture
def no_key_server_config(monkeypatch):
    """端点 LLM 配置链桩掉：server 与请求有效 config 都无 key——保证零网络、且不依赖真 .env。"""
    cfg = LLMConfig(enable_llm=True, mock_llm=False, provider="zhipuai", api_key=None)
    monkeypatch.setattr(webapp, "load_llm_config", lambda *a, **k: cfg)
    return cfg


def test_endpoint_rejects_cross_origin():
    res = client.post("/api/search/reply", json={"total": 1, "shown": 1},
                      headers={"Origin": "http://evil.example.com"})
    assert res.status_code == 403
    assert "同源" in res.json()["detail"] or "Host" in res.json()["detail"]


def test_endpoint_rejects_unknown_fields_422():
    res = client.post("/api/search/reply", json={"total": 1, "shown": 1, "reply": "x"})
    assert res.status_code == 422


def test_endpoint_negative_counts_422():
    res = client.post("/api/search/reply", json={"total": -1, "shown": 0})
    assert res.status_code == 422


def test_endpoint_no_key_fails_open(no_key_server_config):
    """无 key 配置 → HTTP 200、ok=True、reply_zh=None（fail-open，前端留事实句）、status 记原因。"""
    res = client.post("/api/search/reply", json={
        "utterance": "人类肺癌", "query": "人类 肺癌", "total": 0, "shown": 0,
        "provider": "zhipuai", "use_llm": True,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["reply_zh"] is None
    assert body["llm_status"] == "no_key"


def test_endpoint_mock_short_circuits():
    """默认入参（provider=mock）→ 零网络短路，reply_zh=None、status=mock_not_used。"""
    res = client.post("/api/search/reply", json={"total": 3, "shown": 3})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"ok", "reply_zh", "reply_source", "llm_status", "llm_model"}
    assert body["ok"] is True
    assert body["reply_zh"] is None
    assert body["reply_source"] is None
    assert body["llm_status"] == "mock_not_used"


def test_endpoint_llm_success_path(monkeypatch, no_key_server_config):
    """端到端（桩 provider）：请求级 key + 假 LLM 成功 → reply_zh 上屏。"""
    # 覆盖 fixture 的无 key 桩：模拟请求级 key 经 _temporary_env 注入后 load_llm_config 载出的有效配置。
    monkeypatch.setattr(webapp, "load_llm_config",
                        lambda *a, **k: LLMConfig(enable_llm=True, mock_llm=False,
                                                  provider="zhipuai", api_key="sk-req"))
    captured: dict = {}

    def _fake(prompt, cfg):
        captured["prompt"] = prompt
        return LLMResult(text="查到 94 条匹配，先给你看前 5 条。", attempted=True, succeeded=True,
                         response_used=False, provider="zhipuai", model="glm-x")
    monkeypatch.setattr(llm_client, "call_zhipuai", _fake)
    res = client.post("/api/search/reply", json={
        "utterance": "小鼠空间转录组", "query": "小鼠 空间转录组",
        "total": 94, "shown": 5, "hit_keywords": ["Mouse", "Spatial"],
        "can_suggest": ["继续说一句话细化条件（比如换物种、平台、疾病）"],
        "provider": "zhipuai", "use_llm": True, "api_key": "sk-req",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"ok", "reply_zh", "reply_source", "llm_status", "llm_model"}
    assert body["reply_zh"] == "查到 94 条匹配，先给你看前 5 条。"
    assert body["reply_source"] == "llm"
    assert body["llm_status"] == "ok"
    assert body["llm_model"] == "glm-x"
    assert "不超过 60 字" in captured["prompt"]              # 端点走的确实是检索回执 prompt
    assert "命中条数：94" in captured["prompt"]              # 事实逐字接地
