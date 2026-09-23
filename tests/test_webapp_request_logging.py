"""webobs 测试钉：webapp 统一请求日志。

钉四件事：
1. 正常请求落一条 INFO 请求日志（method + 路径 + 状态码 + 耗时），响应行为不变；
2. route_turn 兜底（非流式）触发时必有 ERROR 日志且带完整堆栈，响应文案逐位不变；
3. route_turn 兜底（流式 worker）同上——SSE 仍只发 error 帧通用文案；
4. 脱敏口径：body 里的 api_key 与 URL query string 绝不出现在任何日志行。
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.agent import turn  # noqa: E402

client = TestClient(webapp.app, base_url="http://127.0.0.1")

_LOGGER_NAME = webapp.logger.name

#: 规则直达档（agent=false）：不碰 LLM 分流、纯离线（与 test_corner_backend 同一组参数）。
_AGENT_OFF = {"agent": False, "provider": "mock", "use_llm": False}

_FALLBACK_DETAIL = "处理这句话时出了内部错误，请重试。"


def _boom(*_args, **_kwargs):
    raise RuntimeError("-webobs 探针异常")


def _error_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno >= logging.ERROR]


def test_normal_request_logs_method_path_status(caplog: pytest.LogCaptureFixture):
    """正常路径不炸 + 确实落日志：method/路径/状态码/耗时四要素齐全，响应本身不受影响。"""
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        res = client.get("/api/health")
    assert res.status_code == 200
    records = [r for r in caplog.records if r.name == _LOGGER_NAME and "/api/health" in r.getMessage()]
    assert records, "正常请求应落一条请求日志"
    rec = records[-1]
    assert rec.levelno == logging.INFO
    msg = rec.getMessage()
    assert "GET" in msg and "200" in msg and "ms" in msg


def test_route_turn_fallback_logs_full_traceback_non_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """非流式分支：route_turn 抛异常 → 响应文案不变，服务端必须有带完整堆栈的 ERROR 日志。"""
    monkeypatch.setattr(turn, "route_turn", _boom)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        res = client.post("/api/utterance", json={"utterance": "你好", **_AGENT_OFF})
    assert res.status_code == 200
    assert res.json() == {"ok": False, "detail": _FALLBACK_DETAIL}
    errors = _error_records(caplog)
    assert errors, "route_turn 兜底触发必须落 ERROR 日志（此前零日志零堆栈）"
    rec = errors[-1]
    assert rec.exc_info is not None and rec.exc_info[0] is RuntimeError, "必须带完整堆栈（exc_info）"
    tb = "".join(logging.Formatter().formatException(rec.exc_info))
    assert "-webobs 探针异常" in tb


def test_route_turn_fallback_logs_full_traceback_stream(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """流式分支：worker 线程里的兜底同样落带堆栈的 ERROR 日志；SSE 只发 error 帧通用文案。"""
    monkeypatch.setattr(turn, "route_turn", _boom)
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        res = client.post("/api/utterance", json={"utterance": "你好", "stream": True, **_AGENT_OFF})
    assert res.status_code == 200
    assert '"event": "error"' in res.text, "流式兜底应发 error 帧"
    assert _FALLBACK_DETAIL in res.text
    errors = _error_records(caplog)
    assert errors, "流式 worker 兜底触发必须落 ERROR 日志"
    rec = errors[-1]
    assert rec.exc_info is not None and rec.exc_info[0] is RuntimeError
    tb = "".join(logging.Formatter().formatException(rec.exc_info))
    assert "-webobs 探针异常" in tb


def test_request_log_never_contains_api_key_or_query_string(caplog: pytest.LogCaptureFixture):
    """脱敏钉：请求体 api_key 与 URL query string 绝不出现在任何日志行（含请求日志与异常日志）。"""
    secret = "-webobs-SECRET-9f8e7d6c"
    with caplog.at_level(logging.INFO, logger=_LOGGER_NAME):
        res = client.post("/api/utterance", json={"utterance": "你好", "api_key": secret, **_AGENT_OFF})
        res2 = client.get(f"/api/health?probe={secret}")
    assert res.status_code == 200 and res2.status_code == 200
    assert caplog.records, "两次请求都应落过请求日志"
    for r in caplog.records:
        assert secret not in r.getMessage(), f"敏感信息进了日志行: {r.getMessage()}"
