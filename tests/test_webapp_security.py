"""请求边界 + ENV_LOCK 性能 + datasets 分页的回归门。

覆盖：
- /api/upload 分块流式读超限立即中止（不整读）；原始 body 上限中间件
  （Content-Length 预检 + 实际字节计数）在 FastAPI 解析前拒绝；下载 uids 数量上限 422；
  公开字符串/数组参数预算（模型 max_length → 422）。
- ENV_LOCK 只保护「读配置/物化」——recommend / action/plan / utterance 流式
  的慢 LLM / worker / SSE 泵送全部在锁外（慢 recommend 不阻塞 health）。
- /api/datasets limit 上限 100；展示 item 只对当前页构造（分面走轻量投影）。
- /api/introduction?llm=1 同源检查 + 简单频率限制（llm=0 路径不加闸）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dataset_recommender.app.webapp as webapp  # noqa: E402
from dataset_recommender.app.webapp import UtteranceRequest  # noqa: E402

client = TestClient(webapp.app, base_url="http://127.0.0.1")


def _empty_meta() -> SimpleNamespace:
    """与 test_webapp_endpoint_security._empty_meta 同形的最小 WorkflowResult 替身。"""
    return SimpleNamespace(
        answer="",
        pipeline="rules",
        llm_attempted=False,
        llm_succeeded=False,
        llm_response_used=False,
        llm_provider=None,
        llm_mode=None,
        prompt_name=None,
        fallback=False,
        fallback_reason=None,
        retrieved_data=[],
        relaxation_options=[],
        interpretation={},
        resolution_status="results",
        result_total=0,
        facets=[],
        coverage_caveats=[],
        action_markers=[],
        unused_query_terms=[],
        or_handling={},
        active_filters=[],
        strategy=None,
        search_trace={},
        degraded_search=None,
        clarification=None,
        llm_called=False,
    )


def _lock_is_free() -> bool:
    acquired = webapp.ENV_LOCK.acquire(blocking=False)
    if acquired:
        webapp.ENV_LOCK.release()
    return acquired


# ================================================================ upload 分块流式读


class _FakeUploadFile:
    """`_read_upload_bounded` 的替身：按块吐数据、记录 close/read 次数。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)
        self.closed = False
        self.read_calls = 0

    async def read(self, size: int = -1) -> bytes:  # noqa: ARG002
        self.read_calls += 1
        return self._chunks.pop(0) if self._chunks else b""

    async def close(self) -> None:
        self.closed = True


def test_upload_chunked_read_aborts_and_closes_on_overflow():
    """分块流式读：累计超上限立即关闭文件并 413，绝不整读复制。"""
    chunk = b"x" * webapp._MAX_UPLOAD_CHUNK_BYTES
    file = _FakeUploadFile([chunk, chunk, b"overflow-tail"])

    with pytest.raises(Exception) as excinfo:
        asyncio.run(webapp._read_upload_bounded(file, max_bytes=len(chunk)))

    assert getattr(excinfo.value, "status_code", None) == 413
    assert file.closed is True, "超限后必须立即关闭 UploadFile（释放 multipart 临时文件）"
    assert file.read_calls == 2, f"超限后应停止读取，实际读了 {file.read_calls} 次"


def test_upload_chunked_read_returns_full_bytes_within_limit():
    file = _FakeUploadFile([b"abc", b"def", b"ghi"])
    data = asyncio.run(webapp._read_upload_bounded(file, max_bytes=1024))
    assert data == b"abcdefghi"
    assert file.closed is False


def test_upload_oversize_file_rejected_413(monkeypatch):
    """端点集成：超限文件 413 且零落盘（monkeypatch 小上限走真实请求路径）。"""
    monkeypatch.setattr(webapp, "_MAX_UPLOAD_BYTES", 4096)
    payload = json.dumps(
        [{"dataset_name": f"ds{i}", "species": "Human"} for i in range(200)]
    ).encode("utf-8")
    assert len(payload) > 4096
    response = client.post(
        "/api/upload",
        files={"file": ("big.json", payload, "application/json")},
    )
    assert response.status_code == 413, response.text
    assert "64 MB" in response.json()["detail"]


# ================================================================ 原始 body 上限中间件


async def _run_body_middleware(chunks: list[bytes], *, content_length: int | None) -> tuple[list, object, bytes, BaseException | None]:
    """直接驱动 `_RawBodyLimitMiddleware`：返回 (响应消息, 下游 app, 下游读到的字节, 逃逸异常)。"""

    class _ProbeApp:
        def __init__(self) -> None:
            self.called = False
            self.received = b""

        async def __call__(self, scope, receive, send):  # noqa: ANN001
            self.called = True
            while True:
                message = await receive()
                if message["type"] != "http.request":
                    continue
                self.received += message.get("body") or b""
                if not message.get("more_body"):
                    break
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    app = _ProbeApp()
    middleware = webapp._RawBodyLimitMiddleware(app)
    scope = {
        "type": "http",
        "method": "POST",
        "headers": ([("content-length", str(content_length))] if content_length is not None else []),
    }
    iterator = iter(chunks)

    async def receive():
        try:
            return {"type": "http.request", "body": next(iterator), "more_body": True}
        except StopIteration:
            return {"type": "http.request", "body": b"", "more_body": False}

    responses: list[dict] = []
    escaped: BaseException | None = None

    async def send(message: dict) -> None:
        responses.append(message)

    try:
        await middleware(scope, receive, send)
    except Exception as exc:  # noqa: BLE001 - 断言用，需捕获全部逃逸异常
        escaped = exc
    return responses, app, app.received, escaped


def test_raw_body_limit_content_length_precheck_413(monkeypatch):
    """Content-Length 预检：超限在 body 读取前直接 413（FastAPI 解析前拒绝）。"""
    monkeypatch.setattr(webapp, "_MAX_RAW_BODY_BYTES", 2048)
    response = client.post(
        "/api/interpret",
        json={"query": "肺" * 5000},
    )
    assert response.status_code == 413, response.text
    assert "64 MB" in response.json()["detail"]


def test_raw_body_limit_counts_actual_bytes_no_content_length(monkeypatch):
    """chunked / 缺失 Content-Length：按实际字节计数，超限立即中断下游并 413。

    计数路径以 HTTPException(413) 形式从下游 body 读取处逃逸（FastAPI 对 HTTPException
    原样重抛 → 路由层转 413 响应；此处单元驱动直接断言异常本身）。
    """
    monkeypatch.setattr(webapp, "_MAX_RAW_BODY_BYTES", 100)
    responses, app, received, escaped = asyncio.run(_run_body_middleware(
        [b"a" * 60, b"b" * 60], content_length=None))
    assert isinstance(escaped, Exception) and getattr(escaped, "status_code", None) == 413
    assert app.called is True, "下游应已被调用（它读取时被计数中断）"
    assert len(received) < 120, "超限后下游不应再读到剩余字节"


def test_raw_body_limit_lying_content_length_rejected_413(monkeypatch):
    """谎报 Content-Length（头 ≤ 上限但实际 body 超限）：真实栈下计数路径仍 413。"""
    monkeypatch.setattr(webapp, "_MAX_RAW_BODY_BYTES", 2048)
    body = json.dumps({"query": "肺" * 3000}).encode("utf-8")
    assert len(body) > 2048
    response = client.post(
        "/api/interpret",
        content=body,
        headers={"Content-Length": "10"},  # 谎报：预检放行，实际字节计数兜住
    )
    assert response.status_code == 413, response.text
    assert "64 MB" in response.json()["detail"]


def test_raw_body_limit_allows_small_bodies(monkeypatch):
    monkeypatch.setattr(webapp, "_MAX_RAW_BODY_BYTES", 100)
    responses, app, received, escaped = asyncio.run(_run_body_middleware(
        [b"hello", b"world"], content_length=10))
    assert escaped is None
    assert responses and responses[0]["status"] == 200
    assert app.called is True
    assert received == b"helloworld"


def test_raw_body_limit_skips_get_requests(monkeypatch):
    monkeypatch.setattr(webapp, "_MAX_RAW_BODY_BYTES", 1)
    # GET 不拦：/api/datasets 正常返回（即使 body 预算极小）
    response = client.get("/api/datasets", params={"limit": 1})
    assert response.status_code == 200


# ================================================================ uids 上限 + 参数预算


def test_download_uids_over_limit_422():
    response = client.post(
        "/api/download/plan",
        json={"uids": [f"uid-{i}" for i in range(webapp._MAX_DOWNLOAD_UIDS + 1)]},
    )
    assert response.status_code == 422, response.text
    assert "上限" in response.json()["detail"]


def test_download_start_uids_over_limit_422():
    response = client.post(
        "/api/download/start",
        json={"uids": [f"uid-{i}" for i in range(webapp._MAX_DOWNLOAD_UIDS + 1)]},
    )
    assert response.status_code == 422, response.text


def test_download_uids_at_limit_still_reaches_business_logic():
    response = client.post(
        "/api/download/plan",
        json={"uids": [f"uid-{i}" for i in range(webapp._MAX_DOWNLOAD_UIDS)]},
    )
    assert response.status_code in (200, 400), response.text


def test_recommend_sources_budget_rejected():
    response = client.post(
        "/api/recommend",
        json={"query": "human", "sources": [f"来源{i}" for i in range(webapp._MAX_SOURCES_ITEMS + 1)]},
    )
    assert response.status_code == 422, response.text


def test_recommend_api_key_budget_rejected():
    response = client.post(
        "/api/recommend",
        json={"query": "human", "api_key": "k" * (webapp._MAX_API_KEY_CHARS + 1)},
    )
    assert response.status_code == 422, response.text


def test_recommend_facet_filters_budget_rejected():
    response = client.post(
        "/api/recommend",
        json={"query": "human", "facet_filters": [
            {"dim": "species", "value": f"v{i}"} for i in range(webapp._MAX_FACET_FILTERS_ITEMS + 1)
        ]},
    )
    assert response.status_code == 422, response.text


# ================================================================ ENV_LOCK 收窄


def test_recommend_workflow_runs_outside_env_lock(monkeypatch):
    """workflow.run_with_meta（含 60s LLM 请求）必须在 ENV_LOCK 外执行，
    且请求级配置以 base_llm_config 传入（锁内物化，下游不再读 env）。"""
    observed: list[tuple] = []

    class FakeWorkflow:
        def __init__(self) -> None:
            pass

        def run_with_meta(self, p=None, **_kwargs):
            fields = vars(p) if p is not None else dict(_kwargs)  # 参数对象
            observed.append((_lock_is_free(), "base_llm_config" in fields))
            return _empty_meta()

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", FakeWorkflow)
    response = client.post("/api/recommend", json={"query": "human data", "use_llm": False})
    assert response.status_code == 200, response.text
    assert observed == [(True, True)], f"workflow 调用时必须锁空闲且带 base_llm_config：{observed}"


def test_slow_recommend_does_not_block_health(monkeypatch):
    """回归钉（报告建议的「慢 LLM 不阻塞 health」）：慢 workflow 在途时 /api/health 立即可用。"""
    import threading
    import time

    started = threading.Event()
    release = threading.Event()

    class SlowWorkflow:
        def __init__(self) -> None:
            pass

        def run_with_meta(self, *_args, **_kwargs):
            started.set()
            release.wait(5)
            return _empty_meta()

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", SlowWorkflow)

    def _do_recommend():
        client.post("/api/recommend", json={"query": "human data", "use_llm": True})

    thread = threading.Thread(target=_do_recommend, daemon=True)
    thread.start()
    try:
        assert started.wait(10), "慢 workflow 未启动"
        health = client.get("/api/health")
        assert health.status_code == 200, "慢 recommend 在途时 health 被 ENV_LOCK 阻塞"
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()


def test_action_plan_runs_outside_env_lock(monkeypatch):
    """plan_action（可能调 LLM）在锁外执行，config 显式传入。"""
    from dataset_recommender.agent import action_plan as ap_module

    observed: list[tuple] = []

    def fake_plan_action(utterance, *, has_results=False, result_total=0, config=None, **_kwargs):
        observed.append((_lock_is_free(), config is not None))
        return {"verb": "none", "reason_zh": "测试"}

    monkeypatch.setattr(ap_module, "plan_action", fake_plan_action)
    response = client.post(
        "/api/action/plan",
        json={"utterance": "帮我下载", "provider": "mock", "use_llm": True},
    )
    assert response.status_code == 200, response.text
    assert observed == [(True, True)], f"plan_action 调用时必须锁空闲且带 config：{observed}"


def test_utterance_stream_worker_runs_outside_env_lock(monkeypatch):
    """流式 SSE 的 route_turn worker 在锁外执行（不再持锁到 SSE 完成）。"""
    import dataset_recommender.agent.turn as turn_module

    observed: list[tuple] = []

    def fake_route_turn(*_args, **_kwargs):
        observed.append((_lock_is_free(), "config" in _kwargs))
        raise RuntimeError("trigger worker catch path")

    monkeypatch.setattr(turn_module, "route_turn", fake_route_turn)
    monkeypatch.setattr(webapp, "load_llm_config",
                        lambda project_root=None, provider_override=None: SimpleNamespace(
                            provider="mock", api_key=None, base_url=""))
    monkeypatch.setattr(webapp, "_build_request_overrides", lambda **_kwargs: {})
    webapp.get_settings()  # 预热 lru 缓存，避免 worker 首次调用读 env

    payload = UtteranceRequest(utterance="你好", agent=False)
    generator = webapp._utterance_event_stream(
        "你好", payload, provider="mock", requested_base_url="",
        mock_llm=False, use_llm=False,
    )
    events = []
    for line in generator:
        if line.startswith("data: "):
            events.append(json.loads(line[6:])["event"])
    assert events == ["error"], f"worker 兜底应产 error 帧：{events}"
    assert observed == [(True, True)], f"route_turn 调用时必须锁空闲且带 config：{observed}"


# ================================================================ datasets 分页


def test_datasets_limit_max_cap():
    # 超上限是「值语义错误」→ 422（limit<1/offset<0 的格式错误仍 400，见 test_api_contract）
    assert client.get("/api/datasets", params={"limit": webapp._MAX_DATASETS_LIMIT + 1}).status_code == 422
    assert client.get("/api/datasets", params={"limit": webapp._MAX_DATASETS_LIMIT}).status_code == 200


def test_datasets_builds_items_only_for_current_page(monkeypatch):
    """显式 limit 时只对当前页构造展示 item（此前为全库每条构造）。"""
    calls: list[str] = []

    def counting_build_item(record, include_introduction=False):
        calls.append(str(record.dataset_name))
        return {"platform": "", "source": "10x Genomics", "published_year": None}

    monkeypatch.setattr(webapp, "_web_item_from_record", counting_build_item)
    response = client.get("/api/datasets", params={"limit": 2, "offset": 1})
    assert response.status_code == 200
    assert len(calls) == 2, f"limit=2 时只应构造 2 个 item，实际 {len(calls)}"
    assert len(response.json()["records"]) == 2


def test_dataset_facet_bits_parity_with_build_item():
    """分面轻量投影与 `item_view.build_item` 同派生（改 build_item 必须同步这里）。"""
    from dataset_recommender.corpus.corpus import load_full_corpus

    records = load_full_corpus(webapp.DATA_DIR, webapp.PROJECT_ROOT)[:5]
    assert records, "语料为空，parity 测试无法运行"
    for record in records:
        species, platform, source, year = webapp._dataset_facet_bits(record)
        item = webapp._web_item_from_record(record, include_introduction=False)
        assert species == (record.species or "").strip()
        assert platform == item["platform"]
        assert source == item["source"]
        assert year == item["published_year"]


# ================================================================ introduction?llm=1


@pytest.fixture(autouse=True)
def _clear_rate_buckets():
    webapp._rate_buckets.clear()
    yield
    webapp._rate_buckets.clear()


def test_introduction_llm1_rejects_cross_origin(monkeypatch):
    """llm=1 是 GET 但可产生费用——跨源 Origin 必须 403（复用 _require_same_origin）。"""
    monkeypatch.setattr(webapp, "locate_record", lambda *a, **k: (None, None))
    response = client.get(
        "/api/introduction",
        params={"uid": "some-uid", "llm": 1},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403, response.text


def test_introduction_llm1_absent_origin_passes_origin_check(monkeypatch):
    monkeypatch.setattr(webapp, "locate_record", lambda *a, **k: (None, None))
    # 无 Origin（非浏览器请求）→ 同源检查通过；uid 不存在 → 404（不是 403/429）
    response = client.get("/api/introduction", params={"uid": "some-uid", "llm": 1})
    assert response.status_code == 404, response.text


def test_introduction_llm1_rate_limited(monkeypatch):
    """简单频率限制——超配额 429（monkeypatch 小配额 + 空 bucket）。"""
    monkeypatch.setattr(webapp, "locate_record", lambda *a, **k: (None, None))
    monkeypatch.setattr(webapp, "_LLM_INTRO_RATE_LIMIT", 2)
    codes = [
        client.get("/api/introduction", params={"uid": "u", "llm": 1}).status_code
        for _ in range(3)
    ]
    assert codes == [404, 404, 429], codes


def test_introduction_llm0_is_unaffected_by_rate_limit():
    """llm=0 是纯确定性只读，不加闸——高频请求不受限、跨源也不拦。"""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(webapp, "locate_record", lambda *a, **k: (None, None))
    monkeypatch.setattr(webapp, "_LLM_INTRO_RATE_LIMIT", 1)
    try:
        for _ in range(3):
            response = client.get("/api/introduction", params={"uid": "u", "llm": 0})
            assert response.status_code == 404, response.text
        cross_origin = client.get(
            "/api/introduction",
            params={"uid": "u", "llm": 0},
            headers={"Origin": "https://evil.example"},
        )
        assert cross_origin.status_code == 404, cross_origin.text
    finally:
        monkeypatch.undo()
