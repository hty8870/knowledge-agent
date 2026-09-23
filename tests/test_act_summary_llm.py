# -*- coding: utf-8 -*-
"""p10 · LLM 执行结果总结层的**确定性**测试（无网络）。

覆盖：mock 短路（`call_mock_llm` 绝不用于总结）、disabled/no_key 门、成功路径、
fail-open（provider 失败/异常→summary_zh=None，异常文本不泄 key）、prompt 接地护栏
（数字/文件名逐字进 prompt、ok=False 含「没有完成」铁律）、端点 same-origin 403 /
extra=forbid 422 / 无 key fail-open。
真 LLM 产出质量只能靠真 provider 复验——不在本门覆盖（见模块头诚实说明）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.llm import act_summary_llm, llm_client
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig, LLMResult
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


def _cfg(enable=True, provider="openai-compatible", key="sk-test", mock=False):
    return LLMConfig(enable_llm=enable, mock_llm=mock, provider=provider, api_key=key)


def _facts(ok=True):
    return {
        "verb_zh": "联网检索并入库",
        "utterance": "帮我联网搜一下人类肺的单细胞数据",
        "ok": ok,
        "done_lines": ["在 ArrayExpress 找到 3 个候选", "已把 2 条写进 upload_arrayexpress_20260803.json"],
        "gap_lines": [] if ok else ["联网查询失败，0 条入库"],
        "policy_lines": ["只写入外部库，绝不动基准语料"],
    }


# ---------------------------------------------------------------- 门控

def test_mock_is_short_circuited():
    """mock 一律判否——call_mock_llm 忽略 prompt 吐 curator 表，绝不用于执行总结。"""
    ok, reason = act_summary_llm.should_use_llm(_cfg(mock=True))
    assert ok is False and reason == "mock_not_used"
    ok2, reason2 = act_summary_llm.should_use_llm(_cfg(provider="mock"))
    assert ok2 is False and reason2 == "mock_not_used"


def test_mock_config_never_calls_provider(monkeypatch):
    """即便显式传 mock config（enable=True），也绝不调任何 provider、零网络。"""
    def _boom(*a, **k):
        raise AssertionError("mock 不该走真 provider")
    monkeypatch.setattr(llm_client, "call_openai_compatible", _boom)
    monkeypatch.setattr(llm_client, "call_zhipuai", _boom)
    out = act_summary_llm.summarize_action_with_llm(_facts(), config=_cfg(mock=True))
    assert out["summary_zh"] is None
    assert out["summary_source"] is None
    assert out["llm_status"] == "mock_not_used"


def test_disabled_blocked():
    ok, reason = act_summary_llm.should_use_llm(_cfg(enable=False))
    assert ok is False and reason == "disabled"


def test_no_key_blocked():
    ok, reason = act_summary_llm.should_use_llm(_cfg(key=None))
    assert ok is False and reason == "no_key"


def test_ready_when_all_conditions_met():
    ok, reason = act_summary_llm.should_use_llm(_cfg())
    assert ok is True and reason == "ready"


# ---------------------------------------------------------------- 成功 / fail-open

def test_success_returns_summary(monkeypatch):
    """假 LLM 成功（桩 zhipuai）：summary_zh 有值、source='llm'、status='ok'、带 model。"""
    monkeypatch.setattr(llm_client, "call_zhipuai",
                        lambda prompt, cfg: LLMResult(text="找到了 3 个候选，已入库 2 条。",
                                                      attempted=True, succeeded=True,
                                                      response_used=False, provider="zhipuai",
                                                      model="glm-x"))
    out = act_summary_llm.summarize_action_with_llm(_facts(), config=_cfg(provider="zhipuai"))
    assert out["summary_zh"] == "找到了 3 个候选，已入库 2 条。"
    assert out["summary_source"] == "llm"
    assert out["llm_status"] == "ok"
    assert out["llm_model"] == "glm-x"


def test_fail_open_on_provider_exception_no_key_leak(monkeypatch):
    """provider 抛异常 → fail-open：summary_zh=None、status 以 'error:' 开头、且**不含 key 字样**。"""
    def _boom(*a, **k):
        raise RuntimeError("network down, credential sk-test rejected")
    monkeypatch.setattr(llm_client, "call_openai_compatible", _boom)
    out = act_summary_llm.summarize_action_with_llm(_facts(), config=_cfg())
    assert out["summary_zh"] is None
    assert out["summary_source"] is None
    assert out["llm_status"].startswith("error:")
    assert "sk-test" not in out["llm_status"]


def test_fail_open_on_provider_failure(monkeypatch):
    """provider 返回失败 → summary_zh=None、status=failed:*。"""
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text=None, attempted=True, succeeded=False,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x", error="HTTP 500 boom"))
    out = act_summary_llm.summarize_action_with_llm(_facts(), config=_cfg())
    assert out["summary_zh"] is None
    assert out["llm_status"].startswith("failed:")


def test_empty_llm_text_fails_open(monkeypatch):
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text="   ", attempted=True, succeeded=True,
                                                      response_used=False, provider="openai-compatible",
                                                      model="gpt-x"))
    out = act_summary_llm.summarize_action_with_llm(_facts(), config=_cfg())
    assert out["summary_zh"] is None
    assert out["llm_status"].startswith("failed:")


def test_config_error_fails_open(monkeypatch):
    """config 加载异常也 fail-open（脱敏、永不抛）。"""
    def _boom(*a, **k):
        raise RuntimeError("env broken")
    monkeypatch.setattr(act_summary_llm, "load_llm_config", _boom)
    out = act_summary_llm.summarize_action_with_llm(_facts())
    assert out["summary_zh"] is None
    assert out["llm_status"].startswith("config_error:")


# ---------------------------------------------------------------- prompt 接地护栏

def test_prompt_grounds_numbers_and_filenames_verbatim():
    """done/gap 行的数字与文件名必须**逐字**出现在 prompt 里（接地防编造的确定性证据）。"""
    prompt = act_summary_llm.build_act_summary_prompt(_facts())
    assert "3 个候选" in prompt
    assert "2 条" in prompt
    assert "upload_arrayexpress_20260803.json" in prompt
    assert "联网检索并入库" in prompt                      # verb_zh 进 prompt
    assert "帮我联网搜一下人类肺的单细胞数据" in prompt     # utterance 进 prompt
    assert "只写入外部库，绝不动基准语料" in prompt         # policy 行进 prompt


def test_prompt_failure_branch_forbids_done_wording():
    """ok=False 的 prompt：事实块标明「没有成功」，且铁律含「没有完成」类表述、禁说「已」。"""
    prompt = act_summary_llm.build_act_summary_prompt(_facts(ok=False))
    assert "没有成功" in prompt
    assert "没有完成" in prompt
    assert "0 条入库" in prompt                            # gap 行数字逐字接地


def test_prompt_guardrails_present():
    """铁律常驻：只用事实区块、数字原文照用、1–3 句、不复述「你说/用户说」。"""
    prompt = act_summary_llm.build_act_summary_prompt(_facts())
    assert "----- 事实 -----" in prompt
    assert "原文照用" in prompt
    assert "1–3 句" in prompt
    assert "用户说" in prompt


def test_prompt_pure_deterministic():
    a = act_summary_llm.build_act_summary_prompt(_facts())
    b = act_summary_llm.build_act_summary_prompt(_facts())
    assert a == b


# ---------------------------------------------------------------- 端点

@pytest.fixture
def no_key_server_config(monkeypatch):
    """端点 LLM 配置链桩掉：server 与请求有效 config 都无 key——保证零网络、且不依赖真 .env。"""
    cfg = LLMConfig(enable_llm=True, mock_llm=False, provider="zhipuai", api_key=None)
    monkeypatch.setattr(webapp, "load_llm_config", lambda *a, **k: cfg)
    return cfg


def test_endpoint_rejects_cross_origin():
    res = client.post("/api/act/summary", json={"verb_zh": "列出外部库", "ok": True},
                      headers={"Origin": "http://evil.example.com"})
    assert res.status_code == 403
    assert "同源" in res.json()["detail"] or "Host" in res.json()["detail"]


def test_endpoint_rejects_unknown_fields_422():
    res = client.post("/api/act/summary",
                      json={"verb_zh": "列出外部库", "ok": True, "done_line": ["x"]})
    assert res.status_code == 422


def test_endpoint_missing_verb_zh_422():
    res = client.post("/api/act/summary", json={"ok": True})
    assert res.status_code == 422


def test_endpoint_no_key_fails_open(no_key_server_config):
    """无 key 配置 → HTTP 200、ok=True、summary_zh=None（fail-open，前端留事实句）、status 记原因。"""
    res = client.post("/api/act/summary", json={
        "verb_zh": "联网检索并入库", "ok": False,
        "gap_lines": ["联网查询失败，0 条入库"],
        "provider": "zhipuai", "use_llm": True,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["summary_zh"] is None
    assert body["llm_status"] == "no_key"


def test_endpoint_mock_short_circuits():
    """默认入参（provider=mock）→ 零网络短路，summary_zh=None、status=mock_not_used。"""
    res = client.post("/api/act/summary", json={"verb_zh": "列出外部库", "ok": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["summary_zh"] is None
    assert body["llm_status"] == "mock_not_used"


def test_endpoint_llm_success_path(monkeypatch, no_key_server_config):
    """端到端（桩 provider）：请求级 key + 假 LLM 成功 → summary_zh 上屏。"""
    # 覆盖 fixture 的无 key 桩：模拟请求级 key 经 _temporary_env 注入后 load_llm_config 载出的有效配置。
    monkeypatch.setattr(webapp, "load_llm_config",
                        lambda *a, **k: LLMConfig(enable_llm=True, mock_llm=False,
                                                  provider="zhipuai", api_key="sk-req"))
    def _fake(prompt, cfg):
        return LLMResult(text="没有完成：联网查询失败，0 条入库。", attempted=True, succeeded=True,
                         response_used=False, provider="zhipuai", model="glm-x")
    monkeypatch.setattr(llm_client, "call_zhipuai", _fake)
    res = client.post("/api/act/summary", json={
        "verb_zh": "联网检索并入库", "ok": False,
        "gap_lines": ["联网查询失败，0 条入库"],
        "provider": "zhipuai", "use_llm": True, "api_key": "sk-req",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["summary_zh"] == "没有完成：联网查询失败，0 条入库。"
    assert body["summary_source"] == "llm"
    assert body["llm_status"] == "ok"
    assert body["llm_model"] == "glm-x"


# ---------------------------------------------------------------- 一句话模式

def test_brief_prompt_hard_rules():
    """brief prompt 铁律：一句、≤35 字、只用事实、数字原文照用、ok=False 直说没做成不粉饰。"""
    prompt = act_summary_llm.build_act_brief_prompt(_facts())
    assert "一句" in prompt
    assert "35 个字" in prompt
    assert "原文照用" in prompt
    assert "----- 事实 -----" in prompt
    assert "3 个候选" in prompt                              # 事实行与长总结同一真源，逐字接地
    assert "upload_arrayexpress_20260803.json" in prompt
    prompt_fail = act_summary_llm.build_act_brief_prompt(_facts(ok=False))
    assert "没有成功" in prompt_fail
    assert "没有完成" in prompt_fail                          # ok=False 直说没做成，禁说「已」
    assert "0 条入库" in prompt_fail


def test_brief_prompt_pure_deterministic():
    assert act_summary_llm.build_act_brief_prompt(_facts()) == \
           act_summary_llm.build_act_brief_prompt(_facts())


def test_brief_success_returns_the_sentence(monkeypatch):
    """fake provider 一句话产出：成功回总结句本身（str，不是 dict）。"""
    def _fake(prompt, cfg):
        assert "不超过 35 字" in prompt                     # 走的确实是 brief prompt
        return LLMResult(text="找到 3 个候选，入库 2 条。", attempted=True, succeeded=True,
                         response_used=False, provider="openai-compatible", model="gpt-x")
    monkeypatch.setattr(llm_client, "call_openai_compatible", _fake)
    out = act_summary_llm.summarize_brief_with_llm(_facts(), config=_cfg())
    assert out == "找到 3 个候选，入库 2 条。"


def test_brief_fail_open_returns_none(monkeypatch):
    """无 key / mock / provider 失败 / 空回 → 一律 None（不伪造简洁，前端留事实句）。"""
    def _boom(*a, **k):
        raise AssertionError("闸口没过的路径不该调 provider")
    monkeypatch.setattr(llm_client, "call_openai_compatible", _boom)
    assert act_summary_llm.summarize_brief_with_llm(_facts(), config=_cfg(key=None)) is None
    assert act_summary_llm.summarize_brief_with_llm(_facts(), config=_cfg(mock=True)) is None
    assert act_summary_llm.summarize_brief_with_llm(_facts(), config=_cfg(enable=False)) is None
    monkeypatch.setattr(llm_client, "call_openai_compatible",
                        lambda prompt, cfg: LLMResult(text=None, attempted=True, succeeded=False,
                                                      response_used=False,
                                                      provider="openai-compatible",
                                                      model="gpt-x", error="HTTP 500 boom"))
    assert act_summary_llm.summarize_brief_with_llm(_facts(), config=_cfg()) is None


def test_endpoint_brief_mock_short_circuits():
    """brief=true ∧ 默认 mock 入参 → 零网络短路，响应形状不变、status 记真实原因。"""
    res = client.post("/api/act/summary", json={"verb_zh": "列出外部库", "ok": True, "brief": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"ok", "summary_zh", "summary_source", "llm_status", "llm_model"}
    assert body["ok"] is True
    assert body["summary_zh"] is None
    assert body["summary_source"] is None
    assert body["llm_status"] == "mock_not_used"


def test_endpoint_brief_success_path(monkeypatch, no_key_server_config):
    """brief 端到端（桩 provider）：请求级 key + 假 LLM 一句话 → summary_zh 上屏，形状不变。"""
    monkeypatch.setattr(webapp, "load_llm_config",
                        lambda *a, **k: LLMConfig(enable_llm=True, mock_llm=False,
                                                  provider="zhipuai", api_key="sk-req"))
    captured: dict = {}

    def _fake(prompt, cfg):
        captured["prompt"] = prompt
        return LLMResult(text="没有完成：联网查询失败，0 条入库。", attempted=True, succeeded=True,
                         response_used=False, provider="zhipuai", model="glm-x")
    monkeypatch.setattr(llm_client, "call_zhipuai", _fake)
    res = client.post("/api/act/summary", json={
        "verb_zh": "联网检索并入库", "ok": False,
        "gap_lines": ["联网查询失败，0 条入库"],
        "provider": "zhipuai", "use_llm": True, "api_key": "sk-req",
        "brief": True,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"ok", "summary_zh", "summary_source", "llm_status", "llm_model"}
    assert body["summary_zh"] == "没有完成：联网查询失败，0 条入库。"
    assert body["summary_source"] == "llm"
    assert body["llm_status"] == "ok"
    assert "不超过 35 字" in captured["prompt"]              # 端点走的确实是 brief prompt
    assert len(body["summary_zh"]) <= 35                    # 桩遵守了铁律（真 LLM 质量不在本门）
