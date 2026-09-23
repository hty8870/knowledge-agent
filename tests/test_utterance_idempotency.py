# -*- coding: utf-8 -*-
"""`/api/utterance` 断流重发幂等门（**零网络**）。

背景：前端流式中途失败会把同一句话以非流式**再发一次**；agent 会真执行写工具、worker 线程
在客户端断开后仍会收尾——同一句话可能真实执行两遍（重复入库、账本两行）。修复 =
客户端 req_id + 服务端占用注册表（`_utterance_idem_*`）。本文件钉两件事：

1. **注册表行为**：占用 / 收尾 / 等待唤醒 / TTL 淘汰 / 上限 FIFO 淘汰；
2. **端点行为**（`turn.route_turn` 一律 monkeypatch 成计数+可控的假实现）：
   ① 两个并发同 req_id 请求 → 只执行一次、两响应同体；
   ② 完成后再发同 req_id → 缓存体、仍只执行一次；
   ③ 不同 req_id → 各执行一次；④ 无 req_id → 不占用、各执行一次（行为与旧版逐位一致）；
   ⑤ owner 异常 → waiter 拿到同一份错误体；⑥ 非 owner stream:true → 单帧 final、体一致。
"""
from __future__ import annotations

import json
import threading
import time

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from dataset_recommender.agent import turn
from dataset_recommender.app import webapp
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

#: 假 route_turn 的固定返回体（喂 `_utterance_response_body` 所需的最小键集）。
FAKE_RESULT = {
    "route": "none",
    "query": "",
    "plan": {"verb": "none", "source": "rule"},
    "echo_zh": "假回声",
    "retrieval": None,
    "via": "rule",
}


@pytest.fixture(autouse=True)
def _clean_registry():
    """每个用例前后清空占用注册表：它是模块级进程态，串味会让用例互相干扰。"""
    webapp._UTT_IDEM.clear()
    yield
    webapp._UTT_IDEM.clear()


@pytest.fixture
def counting_route_turn(monkeypatch):
    """注入计数的假 route_turn：返回固定结果，调用次数可查。"""
    calls: list[str] = []

    def fake(text, **kwargs):
        calls.append(text)
        return dict(FAKE_RESULT)

    monkeypatch.setattr(turn, "route_turn", fake)
    return calls


# ---------------------------------------------------------------- 注册表单元行为

def test_sanitize_req_id_truncates_and_normalizes_blank():
    assert webapp._sanitize_req_id(None) is None
    assert webapp._sanitize_req_id("") is None
    assert webapp._sanitize_req_id("   ") is None
    assert webapp._sanitize_req_id(" abc ") == "abc"
    # 超长按完整值取定长哈希：两条「前 64 字符相同」的请求绝不共槽
    long_a, long_b = "x" * 100, "x" * 99 + "y"
    key_a, key_b = webapp._sanitize_req_id(long_a), webapp._sanitize_req_id(long_b)
    assert key_a.startswith("sha256:") and len(key_a) == 7 + 48
    assert key_a != key_b, "截断会让这两条撞同一个幂等槽，哈希必须分开"
    assert webapp._sanitize_req_id(long_a) == key_a   # 同值恒定同键（幂等性不变）


def test_claim_first_caller_is_owner_second_is_not():
    entry, is_owner = webapp._utterance_idem_claim("r-1")
    assert is_owner is True
    assert entry["state"] == "running" and entry["body"] is None
    again, is_owner2 = webapp._utterance_idem_claim("r-1")
    assert is_owner2 is False and again is entry   # 同一个条目对象


def test_store_wakes_waiter_with_cached_body():
    entry, _ = webapp._utterance_idem_claim("r-2")
    got: list[dict] = []

    def waiter():
        got.append(entry["body"])
        entry["event"].wait(timeout=2)
        got[-1] = entry["body"]

    t = threading.Thread(target=waiter)
    t.start()
    time.sleep(0.05)
    body = {"ok": True, "route": "none"}
    webapp._utterance_idem_store(entry, body)
    t.join(timeout=2)
    assert got == [body]
    assert entry["state"] == "done"


def test_ttl_eviction_lets_same_id_be_claimed_again():
    """done 条目 1h TTL：到期后同号视为新请求重新占用。
    （验证：running 不再按 1h TTL 淘汰——那是误杀在途 owner 的重复写窗口。）"""
    entry, is_owner = webapp._utterance_idem_claim("r-3")
    assert is_owner is True
    webapp._utterance_idem_store(entry, {"ok": True})
    entry["ts"] -= webapp._UTT_IDEM_TTL_SECONDS + 1   # 手工做旧（done 1h TTL）
    fresh, is_owner2 = webapp._utterance_idem_claim("r-3")
    assert is_owner2 is True and fresh is not entry   # TTL 过期 → 视为新请求重新占用


def test_running_entry_not_evicted_by_done_ttl():
    """验证：running 条目在 1h TTL 下绝不淘汰（只做 24h 泄漏兜底）。"""
    entry, is_owner = webapp._utterance_idem_claim("r-ttl-run")
    assert is_owner is True and entry["state"] == "running"
    entry["ts"] -= webapp._UTT_IDEM_TTL_SECONDS + 1   # 超 1h 但远不到 24h
    again, is_owner2 = webapp._utterance_idem_claim("r-ttl-run")
    assert is_owner2 is False and again is entry, "在途 owner 不得按 1h TTL 淘汰"
    entry["ts"] -= webapp._UTT_IDEM_RUNNING_TTL_SECONDS   # 再做旧过 24h 泄漏线
    fresh, is_owner3 = webapp._utterance_idem_claim("r-ttl-run")
    assert is_owner3 is True and fresh is not entry, "超 24h 泄漏兜底线的 running 才允许淘汰"


def test_same_req_id_different_fingerprint_conflicts_409():
    """验证：同号不同内容指纹 → 409 如实说明，绝不错等/错收另一句话的响应。"""
    webapp._utterance_idem_claim("r-fp", "fp-aaa")
    with pytest.raises(HTTPException) as exc_info:
        webapp._utterance_idem_claim("r-fp", "fp-bbb")
    assert exc_info.value.status_code == 409
    entry, is_owner = webapp._utterance_idem_claim("r-fp", "fp-aaa")
    assert is_owner is False, "同指纹的同号仍是合法重发"


# ---------------------------------------------------------------- 请求指纹（补全）

def _fp(**over):
    import types
    base = dict(model="", agent=True, has_results=False, result_total=0, query="",
                current_filters=None, sources=None, base_url="", api_key=None)
    base.update(over)
    return webapp._utterance_request_fp("同一句话", types.SimpleNamespace(**base), "mock", False, False)


def test_request_fp_covers_session_context_fields():
    """（验证）：同号不同**现场**（结果集/当前查询/生效条件/来源池/端点/key）
    必须撞指纹 409——断流重发会把现场原样带上（同指纹合法复用），改任何一个字段都不是重发。"""
    ref = _fp()
    assert _fp() == ref, "同现场同指纹（合法重发的判定基础）"
    for over in ({"has_results": True}, {"result_total": 7}, {"query": "上一句"},
                 {"current_filters": [{"dim": "species", "value": "Human"}]},
                 {"sources": ["10x Genomics"]}, {"base_url": "https://api.example.com"},
                 {"api_key": "sk-" + "A" * 24}, {"model": "m2"}, {"agent": False}):
        assert _fp(**over) != ref, f"现场字段 {sorted(over)} 变了指纹必须变"


def test_request_fp_api_key_only_enters_as_hash():
    """api_key 只以其哈希入料：换 key → 指纹变；材料里永不出现 key 原文
    （材料虽不落盘，也不给任何调试/日志路径留原文的机会）。"""
    assert _fp(api_key="sk-" + "A" * 24) != _fp(api_key="sk-" + "B" * 24)
    assert _fp(api_key=None) == _fp(api_key=""), "无 key 与空 key 同一档"


def test_max_entries_evicts_done_first_running_never_evicted(monkeypatch):
    """上限淘汰只动 done 条目——FIFO 淘汰 running owner 会让同号请求
    随后成为新 owner、同一句话真实执行两遍（那才是重复写窗口，正是本注册表要灭的事）。"""
    monkeypatch.setattr(webapp, "_UTT_IDEM_MAX_ENTRIES", 3)
    e0, _ = webapp._utterance_idem_claim("r-done-0")
    webapp._utterance_idem_store(e0, {"ok": True})
    webapp._utterance_idem_claim("r-running-1")
    webapp._utterance_idem_claim("r-running-2")
    # 表满（1 done + 2 running）：新占用淘汰最早的 done 腾位，running 一个不动
    webapp._utterance_idem_claim("r-running-3")
    assert "r-done-0" not in webapp._UTT_IDEM
    assert all(r in webapp._UTT_IDEM for r in ("r-running-1", "r-running-2", "r-running-3"))


def test_max_entries_all_running_rejects_new_claim(monkeypatch):
    """满员且全 running → 新占用 503 拒绝（宁可拒新，绝不淘汰在途 owner）。"""
    monkeypatch.setattr(webapp, "_UTT_IDEM_MAX_ENTRIES", 2)
    webapp._utterance_idem_claim("r-run-a")
    webapp._utterance_idem_claim("r-run-b")
    with pytest.raises(HTTPException) as exc_info:
        webapp._utterance_idem_claim("r-run-c")
    assert exc_info.value.status_code == 503


# ---------------------------------------------------------------- 端点级：占用去重

def _post(req_id: str | None, *, stream: bool = False):
    payload = {"utterance": "今天天气怎么样"}
    if req_id is not None:
        payload["req_id"] = req_id
    if stream:
        payload["stream"] = True
    return client.post("/api/utterance", json=payload)


def test_concurrent_same_req_id_executes_once_and_returns_same_body(monkeypatch):
    """① 两个并发同 req_id 请求：后到者等 owner 收尾拿缓存体——route_turn 只跑一遍。"""
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fake(text, **kwargs):
        calls.append(text)
        started.set()                 # 此刻 owner 已占用（占用在路由之前）
        assert release.wait(timeout=10)
        return dict(FAKE_RESULT)

    monkeypatch.setattr(turn, "route_turn", fake)
    responses: list = [None, None]

    def first():
        responses[0] = client.post("/api/utterance", json={"utterance": "今天天气怎么样", "req_id": "r-conc"})

    def second():
        responses[1] = client.post("/api/utterance", json={"utterance": "今天天气怎么样", "req_id": "r-conc"})

    t1 = threading.Thread(target=first)
    t1.start()
    assert started.wait(timeout=10), "owner 没跑起来"
    t2 = threading.Thread(target=second)
    t2.start()
    time.sleep(0.2)                   # 给 t2 时间走进 waiter 分支
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert calls == ["今天天气怎么样"], f"同号并发只许执行一次，实际 {len(calls)} 次"
    assert responses[0].status_code == 200 and responses[1].status_code == 200
    assert responses[0].json() == responses[1].json(), "等待方必须拿到与 owner 逐位同体的缓存响应"
    assert responses[1].json()["ok"] is True


def test_completed_req_id_returns_cached_body_without_reexecuting(counting_route_turn):
    """② owner 完成后同号再发：直接回缓存体，不二次执行。"""
    res1 = _post("r-done")
    assert res1.status_code == 200 and res1.json()["ok"] is True
    res2 = _post("r-done")
    assert res2.status_code == 200
    assert res2.json() == res1.json()
    assert len(counting_route_turn) == 1


def test_different_req_ids_each_execute(counting_route_turn):
    """③ 不同 req_id = 两次独立提交，各执行一次（用户真发两遍不受影响）。"""
    res1, res2 = _post("r-a"), _post("r-b")
    assert res1.status_code == 200 and res2.status_code == 200
    assert len(counting_route_turn) == 2


def test_missing_req_id_never_claims_and_executes_each_time(counting_route_turn):
    """④ 无 req_id：完全不进占用注册表，行为与旧版逐位一致。"""
    res1, res2 = _post(None), _post(None)
    assert res1.status_code == 200 and res2.status_code == 200
    assert len(counting_route_turn) == 2
    assert not webapp._UTT_IDEM, "无 req_id 的请求不得在注册表留痕"


def test_owner_failure_propagates_error_body_to_waiter(monkeypatch):
    """⑤ owner 路由抛异常：owner 与 waiter 拿到同一份 ok=False 错误体，且只执行一次。"""
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def fake(text, **kwargs):
        calls.append(text)
        started.set()
        assert release.wait(timeout=10)
        raise RuntimeError("模拟路由内部爆炸")

    monkeypatch.setattr(turn, "route_turn", fake)
    responses: list = [None, None]

    t1 = threading.Thread(target=lambda: responses.__setitem__(
        0, client.post("/api/utterance", json={"utterance": "今天天气怎么样", "req_id": "r-err"})))
    t1.start()
    assert started.wait(timeout=10)
    t2 = threading.Thread(target=lambda: responses.__setitem__(
        1, client.post("/api/utterance", json={"utterance": "今天天气怎么样", "req_id": "r-err"})))
    t2.start()
    time.sleep(0.2)
    release.set()
    t1.join(timeout=10)
    t2.join(timeout=10)
    assert len(calls) == 1
    for res in responses:
        assert res.status_code == 200, res.text
        assert res.json()["ok"] is False
        assert "出了内部错误" in res.json()["detail"]
    assert responses[0].json() == responses[1].json()


def test_non_owner_stream_true_gets_single_final_frame(counting_route_turn):
    """⑥ 非 owner 且 stream:true：回一个只含单帧 final 的 SSE 流，帧体与缓存体逐位一致。"""
    res1 = _post("r-sse")
    assert res1.status_code == 200
    res2 = _post("r-sse", stream=True)
    assert res2.status_code == 200, res2.text
    assert res2.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[len("data: "):]) for line in res2.text.splitlines()
              if line.startswith("data: ")]
    assert [e["event"] for e in events] == ["final"], "非 owner 的流式只许一帧 final"
    assert events[0]["data"] == res1.json()
    assert len(counting_route_turn) == 1
