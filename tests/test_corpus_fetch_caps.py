# -*- coding: utf-8 -*-
"""联网取数响应体大小上限的
确定性门。**全程禁网**：在 urlopen 接缝注入假响应，read(n) 按真 http.client.HTTPResponse
语义按参数 n 截断（不得返回无限长对象）。

背景：corpus_net._raw_get / corpus_curation._fetch 的 resp.read() 此前无界——异常/恶意
对端可用超大响应把进程内存吃爆（内存耗尽面）。本次对齐 llm_client.py 的 8MiB 限读范式
（resp.read(cap + 1) + 长度判定，超限即停）给两条联网通道各加 64MiB 上限。为什么是
64MiB 而非 8MiB：本通道取的是官方源**元数据 JSON / 搜索 HTML**（10x 全量清单、CELLxGENE
全库单次拉取都是 MB 级，天然大于 LLM 单次响应），64MiB 是正常上界加宽裕；超限属对端异常
（确定性失败），与「JSON 解析失败不重试」同哲学——退避重试只会再白读 64MB+。

钉四条：
  1. corpus_net._raw_get 超限 → 抛 _NetError 且**只调一次 urlopen**（不进退避重试）；
  2. corpus_net._raw_get 正常小响应 → (bytes, status) 原样返回，回归不破；
  3. corpus_curation._fetch 超限 → CurateError(network_error) 且只调一次 urlopen
     （超限 raise 位于 except 链之外，不会被 except ValueError 误捕改写）；
  4. corpus_curation._fetch 正常小 JSON → (parsed_dict, 200) 回归不破。
"""
import json

import pytest

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus import corpus_net as cn


class _FakeResp:
    """假 HTTP 响应：read(n) 按真 read 语义截断（返回不超过 n 字节），支持 with 上下文。"""

    def __init__(self, body: bytes, status: int = 200):
        self._body = body
        self.status = status

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            return self._body
        return self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _stub_urlopen(monkeypatch, module, body: bytes) -> list:
    """把 module 侧的 urllib.request.urlopen 换成返回固定 body 的假身；返回调用记录列表。

    传 min_interval=0.0 调被测函数即可避开限速 sleep：两处 _polite_wait 的死线 =
    「上次请求时刻 + 0.0」≤ 当前时刻，恒不睡（读实现确认过，比 monkeypatch 真身更稳）。"""
    calls: list = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return _FakeResp(body)

    monkeypatch.setattr(module.urllib.request, "urlopen", fake_urlopen)
    return calls


# ---------------------------------------------------------------- corpus_net._raw_get

def test_raw_get_oversize_raises_net_error_without_retry(monkeypatch):
    """S-6：响应体超 64MiB 上限 → _NetError（对端异常），只调一次 urlopen、不退避重试。"""
    calls = _stub_urlopen(monkeypatch, cn, b"x" * (cn._MAX_RESPONSE_BYTES + 1))
    with pytest.raises(cn._NetError, match="上限") as exc_info:
        cn._raw_get("https://example.org/big.bin", timeout=1, min_interval=0.0, headers={})
    assert "不重试" in str(exc_info.value) and "example.org/big.bin" in str(exc_info.value)
    assert len(calls) == 1, "超限是确定性失败（对端异常），重试只会再白读 64MB+"


def test_raw_get_small_body_unchanged(monkeypatch):
    """回归门：上限以内的小响应原样返回 (bytes, status)，限读不改变正常路径。"""
    calls = _stub_urlopen(monkeypatch, cn, b'{"hits": [1, 2]}')
    body, status = cn._raw_get(
        "https://example.org/small.json", timeout=1, min_interval=0.0, headers={})
    assert body == b'{"hits": [1, 2]}' and status == 200
    assert len(calls) == 1


# ---------------------------------------------------------------- corpus_curation._fetch

def test_fetch_oversize_raises_curate_error_without_retry(monkeypatch):
    """S-6：官方源响应体超 64MiB 上限 → CurateError(network_error)，只调一次 urlopen、不重试。"""
    calls = _stub_urlopen(monkeypatch, cc, b"x" * (cc._FETCH_MAX_BYTES + 1))
    with pytest.raises(cc.CurateError, match="上限") as exc_info:
        cc._fetch("https://example.org/big.json", timeout=1, min_interval=0.0)
    assert exc_info.value.code == "network_error"
    assert "不重试" in exc_info.value.hint and "不是合法 JSON" not in exc_info.value.hint, (
        "超限 CurateError 必须从 except 链之外抛出——在 try 内 raise 会被 "
        "except ValueError（CurateError 继承 ValueError）捕获改写成「不是合法 JSON」"
    )
    assert len(calls) == 1, "超限是确定性失败（对端异常），退避重试只会再白读 64MB+"


def test_fetch_small_json_unchanged(monkeypatch):
    """回归门：上限以内的小 JSON 照常解析返回 (dict, 200)，限读不改变正常路径。"""
    calls = _stub_urlopen(monkeypatch, cc, json.dumps({"hits": []}).encode("utf-8"))
    payload, status = cc._fetch("https://example.org/small.json", timeout=1, min_interval=0.0)
    assert payload == {"hits": []} and status == 200
    assert len(calls) == 1
