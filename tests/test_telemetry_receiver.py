# -*- coding: utf-8 -*-
"""遥测接收端契约测试。

契约口径：同目录 README.md（上传协议与接收端行为）。

DB 层抽象：生产用 PostgreSQL（DATABASE_URL），本测试注入 SQLite 内存库
（SQLAlchemy 方言差异由 app.build_engine / 类型 variant 收敛），全绿即可
验证端点语义：401 / 413（CL 预检 + 流式兜底）/ 408（读取超时）/ 415 / 422 / 429 /
落库字段与索引 / healthz DB 检查 / 文档关闭（安全加固）。
"""
import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# 让 `app` 模块可导入（services/telemetry-receiver 无 __init__.py；裸 `app`
# 不会与其他测试冲突——仓库没有同名顶层模块，src 包为 dataset_recommender.app）。
_RECEIVER_DIR = Path(__file__).resolve().parents[1] / "services" / "telemetry-receiver"
if str(_RECEIVER_DIR) not in sys.path:
    sys.path.insert(0, str(_RECEIVER_DIR))

# 模块级 app 实例由 Settings.from_env() 构建，先给环境变量兜底（fail-fast 设计）
os.environ.setdefault("INGEST_TOKEN", "test-token")
os.environ.setdefault("STATS_TOKEN", "test-stats-token")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import app as receiver_app  # noqa: E402
from app import IngestPayload, IpRateLimiter, Settings, create_app, ingest_packets  # noqa: E402
from telemetry_idempotency import event_receipts, legacy_packet_id, packet_receipts  # noqa: E402

TOKEN = "test-token"
STATS_TOKEN = "test-stats-token"
FAKE_API_KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz0123"


def _valid_body() -> dict:
    """body 形状示例。"""
    return {
        "schema": "biodata-telemetry/1",
        "packet_id": "pkt-test-0001",
        "install_id": "inst-1",
        "client_id": "client-test-0001",
        "profile_id": "profile-test-0001",
        "exported_at": "2026-08-19T00:00:00Z",
        "app": {"cache_generation": "20260819-01", "ua": "Mozilla/5.0", "lang": "zh-CN"},
        "usage_events": [
            {"event_id": "u1", "t": 0, "k": "search", "q": "10x"},
            {"event_id": "u2", "t": 1, "k": "open", "rank": 0},
        ],
        "benchfb_records": [{"id": "r1", "kind": "search", "rating": {"stars": 4}}],
    }


@pytest.fixture()
def app():
    return create_app(Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url="sqlite:///:memory:",
    ))


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# --- 正常路径 ---


def test_healthz_ok(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_ingest_ok_and_persists(client, app):
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and isinstance(body["id"], int)

    with app.state.engine.connect() as conn:
        row = conn.execute(ingest_packets.select()).mappings().fetchone()
    assert row is not None
    assert row.raw_bytes > 0
    assert row["id"] == body["id"]
    assert row["install_id"] == "inst-1"
    assert row["schema"] == "biodata-telemetry/1"
    assert row["ua"] == "Mozilla/5.0"
    assert row["cache_generation"] == "20260819-01"
    assert row["n_usage"] == 2
    assert row["n_benchfb"] == 1
    # payload 原样存 jsonb（含全部明细字段）
    assert row["payload"]["usage_events"][0] == {"event_id": "u1", "t": 0, "k": "search", "q": "10x"}
    assert row["payload"]["benchfb_records"][0]["rating"] == {"stars": 4}
    assert row["received_at"] is not None


def test_contract_v2_analysis_and_drop_report_persist(client, app):
    body = _valid_body()
    body.update({
        "contract_version": 2,
        "prompt_version": "route-p7",
        "experiment_id": "rank-e1",
        "experiment_arm": "candidate",
        "propensity": 0.2,
        "training_consent": True,
        "drop_report": {
            "revision": 7,
            "dropped_count": 4,
            "by_queue": {"usage": 2, "benchfb": 1, "storage_error": 1},
        },
    })
    body["packet_id"] = "pkt-contract-v2"
    body["usage_events"][0].update({
        "contract_version": 2, "prompt_version": "route-p7", "experiment_arm": "candidate",
        "experiment_id": "rank-e1", "propensity": 0.2,
    })
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200, resp.text
    with app.state.engine.connect() as conn:
        row = conn.execute(
            ingest_packets.select().where(ingest_packets.c.id == resp.json()["id"])
        ).mappings().one()
    payload = row["payload"]
    assert payload["contract_version"] == 2
    assert payload["training_consent"] is True
    assert payload["propensity"] == 0.2
    assert payload["drop_report"]["dropped_count"] == 4
    assert payload["usage_events"][0]["experiment_arm"] == "candidate"


def test_legacy_contract_defaults_to_v1(client, app):
    body = _valid_body()
    body["packet_id"] = "pkt-contract-v1"
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    with app.state.engine.connect() as conn:
        payload = conn.execute(
            select(ingest_packets.c.payload).where(ingest_packets.c.id == resp.json()["id"])
        ).scalar_one()
    assert payload["contract_version"] == 1
    assert payload["training_consent"] is False


@pytest.mark.parametrize("patch", [
    {"contract_version": 3},
    {"contract_version": 2, "propensity": 0},
    {"contract_version": 2, "experiment_arm": "x", "propensity": 1.1},
    {"contract_version": 2, "drop_report": {
        "revision": 1, "dropped_count": 9,
        "by_queue": {"usage": 1, "benchfb": 1, "storage_error": 0},
    }},
])
def test_contract_v2_invalid_shapes_rejected(client, patch):
    body = _valid_body()
    body["packet_id"] = "pkt-contract-invalid-" + str(abs(hash(json.dumps(patch, sort_keys=True))))
    body.update(patch)
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_same_packet_retry_is_idempotent(client, app):
    first = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    again = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["packet_id"] == "pkt-test-0001"
    with app.state.engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(ingest_packets)).scalar_one() == 1


def test_duplicate_retry_is_acked_even_after_profile_quota_reached():
    app2 = create_app(Settings(
        ingest_token=TOKEN, database_url="sqlite:///:memory:", per_install_daily_packets=1,
    ))
    with TestClient(app2) as c:
        first = c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        retry = c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        fresh = _valid_body(); fresh["packet_id"] = "pkt-test-fresh"
        rejected = c.post("/v1/ingest", json=fresh, headers={"X-Ingest-Token": TOKEN})
    assert first.status_code == 200
    assert retry.status_code == 200 and retry.json()["duplicate"] is True
    assert rejected.status_code == 429


def test_overlapping_packets_store_only_new_events(client, app):
    first = _valid_body()
    second = _valid_body()
    second["packet_id"] = "pkt-test-0002"
    second["usage_events"] = [
        {"event_id": "u2", "t": 1, "k": "open", "rank": 0},
        {"event_id": "u3", "t": 2, "k": "search", "q": "new"},
    ]
    second["benchfb_records"] = [
        {"id": "r1", "kind": "search"}, {"id": "r2", "kind": "search"},
    ]
    assert client.post("/v1/ingest", json=first, headers={"X-Ingest-Token": TOKEN}).status_code == 200
    response = client.post("/v1/ingest", json=second, headers={"X-Ingest-Token": TOKEN})
    assert response.status_code == 200
    assert response.json()["accepted_usage"] == 1
    assert response.json()["accepted_benchfb"] == 1
    with app.state.engine.connect() as conn:
        rows = conn.execute(ingest_packets.select().order_by(ingest_packets.c.id)).mappings().all()
    assert len(rows) == 2
    assert [e["event_id"] for e in rows[1]["payload"]["usage_events"]] == ["u3"]
    assert [e["id"] for e in rows[1]["payload"]["benchfb_records"]] == ["r2"]


def test_ingest_minimal_body_ok(client, app):
    """usage_events / benchfb_records 等可选字段可缺省。"""
    body = {"schema": "biodata-telemetry/1", "install_id": "inst-2"}
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    with app.state.engine.connect() as conn:
        row = conn.execute(ingest_packets.select()).mappings().fetchone()
    assert row["n_usage"] == 0 and row["n_benchfb"] == 0
    assert row["ua"] is None and row["cache_generation"] is None


def test_indexes_created(app):
    """install_id 与 received_at 都要有索引。"""
    with app.state.engine.connect() as conn:
        idx_names = [r[1] for r in conn.execute(text("PRAGMA index_list('ingest_packets')"))]
    assert "ix_ingest_packets_install_id" in idx_names
    assert "ix_ingest_packets_received_at" in idx_names


def test_cors_allows_loopback_and_rejects_arbitrary_origin(client):
    allowed = client.options(
        "/v1/ingest",
        headers={
            "Origin": "http://127.0.0.1:7860",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://127.0.0.1:7860"
    denied = client.options(
        "/v1/ingest",
        headers={"Origin": "https://example.invalid", "Access-Control-Request-Method": "POST"},
    )
    assert denied.status_code == 400
    assert denied.headers.get("access-control-allow-origin") is None


# --- token 校验 → 401 ---


def test_ingest_wrong_token_401(client):
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": "wrong"})
    assert resp.status_code == 401


def test_ingest_missing_token_401(client):
    resp = client.post("/v1/ingest", json=_valid_body())
    assert resp.status_code == 401


# --- body 体积 → 413 ---


def _body_with_pad(total_bytes: int) -> bytes:
    """构造 pad 后总长恰为 total_bytes 的原始 JSON body（精确控制体积边界）。

    schema 收紧后顶层 extra="forbid"——pad 放顶层会被 422 拦（绕过体积语义），
    移入 usage_events 明细元素（形状宽松、字符串长度上限与 body 上限同值，不拦）。
    """
    body = _valid_body()
    base = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    body["usage_events"][0]["pad"] = "x" * (total_bytes - len(base))
    return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def test_ingest_oversize_413(client):
    raw = _body_with_pad(receiver_app.MAX_BODY_BYTES + 10)  # 超出 2MiB 上限
    assert len(raw) > receiver_app.MAX_BODY_BYTES
    resp = client.post("/v1/ingest", content=raw, headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"})
    assert resp.status_code == 413
    assert resp.json()["detail"] == {
        "code": "payload_too_large", "max_body_bytes": receiver_app.MAX_BODY_BYTES,
    }


def test_ingest_under_limit_size_ok(client):
    raw = _body_with_pad(receiver_app.MAX_BODY_BYTES - 10)  # 略小于 2MiB 上限
    assert len(raw) < receiver_app.MAX_BODY_BYTES
    resp = client.post("/v1/ingest", content=raw, headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"})
    assert resp.status_code == 200


# --- 慢连接/大 Content-Length 悬挂 DoS 加固 ---


def test_ingest_declared_cl_over_limit_413_without_reading_body(client):
    """Content-Length 谎报超大（如 999999999）只发 2 字节 → 不等 body 直接 413（悬挂修复）。

    修复前会一直等 body 到超时；修复后 CL 预检第一时间拒绝。
    """
    def tiny_body():
        yield b'{"'
    resp = client.post(
        "/v1/ingest",
        content=tiny_body(),
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json", "Content-Length": "999999999"},
    )
    assert resp.status_code == 413


def test_ingest_no_content_length_oversize_streamed_413(client):
    """缺 Content-Length 头（分块流式、无头可查）→ 流式累计超 2MiB 即 413（缺头/谎报兜底）。"""
    def big_chunked():
        for _ in range(300):
            yield b"x" * 8192   # 合计约 2.4MB > 2MiB
    resp = client.post(
        "/v1/ingest",
        content=big_chunked(),
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert resp.status_code == 413


def test_ingest_body_read_timeout_408():
    """body 读取超时 → 408：慢连接/半截悬挂，10s（测试注入 0.05s）内没读完就快速拒绝。

    同步 TestClient 会先把生成器内容**整体缓冲**再交给应用，测不到真实流式超时；
    改用 httpx.AsyncClient + ASGITransport 走真实流式路径（应用读到第一块后等不到
    下一块，wait_for 超时 → 408）。
    """
    import asyncio

    import httpx
    from httpx import ASGITransport

    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:", body_read_timeout=0.05))

    async def slow_body():
        yield b'{"schema":'
        await asyncio.sleep(0.3)
        yield b'"biodata-telemetry/1"}'

    async def main():
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            req = c.build_request(
                "POST", "/v1/ingest",
                content=slow_body(),
                headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"},
            )
            resp = await c.send(req)
            return resp.status_code

    assert asyncio.run(main()) == 408


# --- Content-Type → 415---


def test_ingest_wrong_content_type_415(client):
    resp = client.post(
        "/v1/ingest",
        content=json.dumps(_valid_body()),
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "text/plain"},
    )
    assert resp.status_code == 415


def test_ingest_missing_content_type_415(client):
    resp = client.post(
        "/v1/ingest",
        content=json.dumps(_valid_body()),
        headers={"X-Ingest-Token": TOKEN},
    )
    assert resp.status_code == 415


def test_ingest_content_type_with_charset_ok(client):
    resp = client.post(
        "/v1/ingest",
        content=json.dumps(_valid_body()),
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json; charset=utf-8"},
    )
    assert resp.status_code == 200


# --- schema 字段校验 → 422 ---


def test_ingest_missing_required_422(client):
    for missing in ("schema", "install_id"):
        body = _valid_body()
        del body[missing]
        resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422, f"缺少 {missing} 应 422"


def test_ingest_wrong_type_422(client):
    cases = [
        {"install_id": 123},                       # str 写成 int
        {"schema": 1},                             # str 写成 int
        {"usage_events": "not-a-list"},            # list 写成 str
        {"benchfb_records": [1, 2]},               # 元素非对象
    ]
    for over in cases:
        body = _valid_body()
        body.update(over)
        resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422, f"{over} 应 422"


def test_ingest_malformed_json_422(client):
    resp = client.post(
        "/v1/ingest",
        content="{not-json",
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_ingest_wrong_schema_value_422(client):
    """schema 字段值必须等于 biodata-telemetry/1，其余值 → 422 且不回显。"""
    body = _valid_body()
    body["schema"] = "biodata-telemetry/2"
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422
    assert "biodata-telemetry/2" not in resp.text, "422 detail 不得回显输入值"


def test_ingest_deep_nested_json_422(client):
    """深嵌套 JSON 触发 RecursionError → 捕获后 422（连同 ValueError 族）。"""
    deep = "[" * 50000 + "1" + "]" * 50000
    assert len(deep) < receiver_app.MAX_BODY_BYTES   # 体积在上限内，失败必须是解析深度而非体积
    resp = client.post(
        "/v1/ingest",
        content=deep.encode("utf-8"),
        headers={"X-Ingest-Token": TOKEN, "Content-Type": "application/json"},
    )
    assert resp.status_code == 422


def test_ingest_422_detail_does_not_echo_input(client):
    """422 detail 不回显输入——错误详情只留位置与类型，剔除 input/ctx。"""
    body = _valid_body()
    body["install_id"] = "secret-user-input-SECRET-MARKER"   # 合法值，不产生错误；作探测哨兵
    body["usage_events"] = "not-a-list"                       # 触发 list_type 错误，其 input 会被回显
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "secret-user-input-SECRET-MARKER" not in resp.text, "422 不得把输入值回显进响应"
    assert "not-a-list" not in resp.text, "422 不得把 input/ctx 回显进响应"
    assert detail["errors"][0]["loc"] == ["usage_events"], "错误详情应保留定位信息"


# --- 文档关闭---


def test_docs_and_openapi_disabled(client):
    """FastAPI 交互式文档与 OpenAPI 全部关闭（内部端点无需可发现性）。"""
    assert client.get("/docs").status_code == 404
    assert client.get("/redoc").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_deployment_preserves_approved_public_receiver_and_feedback_key_injection():
    compose = (_RECEIVER_DIR / "docker-compose.yml").read_text(encoding="utf-8")
    dockerfile = (_RECEIVER_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert '"8471:8471"' in compose
    assert '"127.0.0.1:8471:8471"' not in compose
    assert "ALLOWED_ORIGINS" in compose and "PROFILE_RATE_LIMIT_MAX" in compose
    assert "FEEDBACK_DECRYPT_KEY: ${FEEDBACK_DECRYPT_KEY:-}" in compose
    assert "UVICORN_WORKERS: ${UVICORN_WORKERS:-1}" in compose
    assert "--workers $${UVICORN_WORKERS:-1}" in compose
    assert "DB_POOL_SIZE: ${DB_POOL_SIZE:-5}" in compose and "DB_MAX_OVERFLOW: ${DB_MAX_OVERFLOW:-5}" in compose
    assert "COPY telemetry_idempotency.py ./" in dockerfile


# --- 限流 → 429 ---


def test_ingest_rate_limit_429(client):
    for _ in range(30):
        resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 429


def test_rate_limiter_periodic_purge(monkeypatch):
    """重写后：全表清理只在周期到点触发（低流量期限流表也不无限膨胀）。"""
    # __init__ 消耗第 1 个 tick（_last_purge 初值）
    ticks = iter([100.0, 101.0, 102.0, 102.0, 1000.0])
    monkeypatch.setattr("app.time.monotonic", lambda: next(ticks))
    limiter = IpRateLimiter(max_requests=30, window_seconds=60.0, purge_interval=300.0)
    assert limiter.allow("ip-a") is True     # t=101
    assert limiter.allow("ip-b") is True     # t=102
    assert limiter.allow("ip-b") is True     # t=102：距上次清理 2s < 300s，不触发全表清理
    # t=1000：距上次清理 900s ≥ 300s → 周期清理，ip-a/ip-b 已过期被清
    assert limiter.allow("ip-c") is True
    assert "ip-a" not in limiter._hits and "ip-b" not in limiter._hits
    assert list(limiter._hits.keys()) == ["ip-c"]


def test_rate_limiter_bounded_lru_eviction():
    """key 总数有界，表满按 LRU 驱逐最久未使用的 key。"""
    limiter = IpRateLimiter(max_requests=3, window_seconds=60.0, max_keys=2, purge_interval=300.0)
    assert limiter.allow("ip-a") is True
    assert limiter.allow("ip-b") is True
    assert limiter.allow("ip-a") is True     # 再次访问 ip-a → LRU 序变为 [b, a]
    assert limiter.allow("ip-c") is True     # 表满 → 驱逐最久未用（ip-b）
    assert "ip-a" in limiter._hits and "ip-c" in limiter._hits
    assert "ip-b" not in limiter._hits


def test_rate_limiter_no_full_scan_per_request(monkeypatch):
    """每次 allow 不再遍历全表——10000 个 key 插入期间零次全表清理。"""
    monkeypatch.setattr("app.time.monotonic", lambda: 100.0)   # 时间静止：无周期触发
    limiter = IpRateLimiter(max_requests=30, window_seconds=60.0, max_keys=10000, purge_interval=300.0)
    purges: list[float] = []
    orig = limiter._purge_expired
    def spy(now: float) -> None:
        purges.append(now)
        orig(now)
    limiter._purge_expired = spy
    for i in range(10000):
        assert limiter.allow(f"ip-{i}") is True
    assert len(purges) == 0, "未满上限前不得触发任何全表清理"
    assert limiter.allow("final") is True    # 第 10001 个 key：表满 → 一次惰性清理 + LRU 驱逐
    assert len(purges) == 1, "只允许在表满时触发一次清理"
    assert len(limiter._hits) == 10000, "key 数必须严格有界（max_keys）"


def test_rate_limiter_window_slides(monkeypatch):
    """滑动窗口语义保持：窗口内第 N+1 次拒绝，窗口过后恢复。"""
    # __init__ 消耗第 1 个 tick；32 个 100（30 次放行 + 窗口内第 31 次拒绝）+ 1 个窗口过后
    ticks = iter([100.0] * 32 + [161.0])
    monkeypatch.setattr("app.time.monotonic", lambda: next(ticks))
    limiter = IpRateLimiter(max_requests=30, window_seconds=60.0)
    for _ in range(30):
        assert limiter.allow("ip-a") is True
    assert limiter.allow("ip-a") is False    # 窗口内第 31 次 → 拒绝
    assert limiter.allow("ip-a") is True     # 时间越过窗口 → 过期条目已裁剪，恢复


def test_slow_db_work_is_offloaded_from_event_loop(monkeypatch, app):
    """两个 200ms DB 单元应并发约 200ms 完成；若留在 async loop 会约 400ms。"""
    def slow_store(_engine, payload, _settings, _raw_bytes, _client_ip):
        time.sleep(0.2)
        return {"ok": True, "id": 1, "packet_id": payload.packet_id, "duplicate": False,
                "accepted_usage": 1, "accepted_benchfb": 0}

    monkeypatch.setattr(receiver_app, "_store_ingest_packet", slow_store)

    async def run():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            a, b = _valid_body(), _valid_body()
            a["packet_id"], b["packet_id"] = "pkt-slow-0001", "pkt-slow-0002"
            started = time.perf_counter()
            responses = await asyncio.gather(
                ac.post("/v1/ingest", json=a, headers={"X-Ingest-Token": TOKEN}),
                ac.post("/v1/ingest", json=b, headers={"X-Ingest-Token": TOKEN}),
            )
            return time.perf_counter() - started, responses

    elapsed, responses = asyncio.run(run())
    assert [r.status_code for r in responses] == [200, 200]
    assert elapsed < 0.35, f"同步 DB 似乎仍阻塞事件循环：{elapsed:.3f}s"


# --- 模型单元检查（顺带钉住 body 形状的字段语义）---


def test_model_alias_schema_field():
    obj = json.loads(json.dumps(_valid_body()))
    payload = IngestPayload.model_validate(obj)
    assert payload.schema_name == "biodata-telemetry/1"
    assert payload.install_id == "inst-1"
    assert payload.app is not None and payload.app.lang == "zh-CN"
    assert len(payload.usage_events or []) == 2
    assert len(payload.benchfb_records or []) == 1


# --- schema 收紧（extra="forbid" / 嵌套校验 / 条数/长度上限）---


def test_ingest_unknown_top_level_field_422(client):
    """顶层未知字段 → 422（forbid），现有 6 字段包不受影响。"""
    body = _valid_body()
    body["surprise_field"] = {"x": 1}
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_ingest_unknown_app_field_422(client):
    """app 子对象未知字段 → 422（白名单只收 cache_generation/ua/lang）。"""
    body = _valid_body()
    body["app"]["secret"] = "abc"
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_ingest_usage_events_too_many_422(client):
    """usage_events 条数超上限（5000）→ 422。"""
    body = _valid_body()
    body["usage_events"] = [{"t": i} for i in range(5001)]
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_ingest_benchfb_records_too_many_422(client):
    """benchfb_records 条数超上限（1000）→ 422。"""
    body = _valid_body()
    body["benchfb_records"] = [{"id": str(i)} for i in range(1001)]
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_ingest_install_id_too_long_422(client):
    """install_id 超 128 字符 → 422。"""
    body = _valid_body()
    body["install_id"] = "i" * 129
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 422


def test_ingest_detail_shape_limits_unit():
    """明细元素形状校验单元级：非对象/键数/键名长度/字符串长度/深度。"""
    from app import (
        MAX_DETAIL_DEPTH,
        MAX_DETAIL_KEYS,
        MAX_DETAIL_STR_LEN,
        _check_detail_shape,
    )
    with pytest.raises(ValueError):
        _check_detail_shape([1, "x"], "usage_events")                    # 元素非对象
    with pytest.raises(ValueError):
        _check_detail_shape([{"k%d" % i: i for i in range(MAX_DETAIL_KEYS + 1)}], "usage_events")  # 键数超限
    with pytest.raises(ValueError):
        _check_detail_shape([{"x" * 65: 1}], "usage_events")             # 键名超长
    with pytest.raises(ValueError):
        _check_detail_shape([{"x": "s" * (MAX_DETAIL_STR_LEN + 1)}], "usage_events")  # 字符串超长
    deep = cur = {}
    for _ in range(MAX_DETAIL_DEPTH + 1):
        cur["k"] = {}
        cur = cur["k"]
    with pytest.raises(ValueError):
        _check_detail_shape([deep], "usage_events")                      # 嵌套超深
    # 合法形状（含嵌套数组）不抛
    _check_detail_shape([{"t": 0, "k": "search", "nested": {"a": [1, 2]}}], "usage_events")


# --- 日配额（全局字节 / 每 profile 包数）→ 429 ---


def test_ingest_daily_byte_budget_429():
    """全局每日字节上限：当日累计超预算 → 429 且不落库。"""
    s = Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url="sqlite:///:memory:",
        daily_bytes_budget=1_000_000,
    )
    app2 = create_app(s)
    with TestClient(app2) as c:
        first = _valid_body()
        first["usage_events"][0]["pad"] = "x" * 600_000
        assert c.post("/v1/ingest", json=first, headers={"X-Ingest-Token": TOKEN}).status_code == 200
        second = _valid_body()
        second["packet_id"] = "pkt-budget-0002"
        second["usage_events"][0]["event_id"] = "budget-u2"
        second["usage_events"][0]["pad"] = "y" * 600_000
        resp = c.post("/v1/ingest", json=second, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 429
        assert "byte budget" in resp.json()["detail"]
        with app2.state.engine.connect() as conn:
            n = conn.execute(text("SELECT COUNT(*) FROM ingest_packets")).scalar()
        assert n == 1  # 429 请求与日计数预留在同一事务，均回滚


def test_ingest_hot_path_does_not_scan_or_cast_payload_json():
    """性能回归：单次 ingest 的配额查询只读日汇总表，不扫历史 payload。"""
    s = Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url="sqlite:///:memory:"
    )
    app2 = create_app(s)
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement.lower())

    with TestClient(app2) as c:
        event.listen(app2.state.engine, "before_cursor_execute", capture)
        try:
            assert c.post(
                "/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN}
            ).status_code == 200
        finally:
            event.remove(app2.state.engine, "before_cursor_execute", capture)
    assert not any(
        "ingest_packets.payload" in statement and ("sum(" in statement or "cast(" in statement)
        for statement in statements
    )


def test_per_ip_daily_byte_budget_uses_hmac_bucket():
    s = Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url="sqlite:///:memory:",
        daily_bytes_budget=10_000, per_ip_daily_bytes=100,
    )
    app2 = create_app(s)
    with app2.state.engine.begin() as conn:
        assert receiver_app._reserve_daily_bytes(
            conn, raw_bytes=60, client_ip="198.51.100.10", settings=s
        ) == 60
    with app2.state.engine.begin() as conn:
        with pytest.raises(receiver_app.HTTPException) as exc:
            receiver_app._reserve_daily_bytes(
                conn, raw_bytes=60, client_ip="198.51.100.10", settings=s
            )
        assert exc.value.status_code == 429 and "for IP" in exc.value.detail
    with app2.state.engine.begin() as conn:
        assert receiver_app._reserve_daily_bytes(
            conn, raw_bytes=60, client_ip="198.51.100.11", settings=s
        ) == 120
        buckets = conn.execute(
            select(receiver_app.daily_usage.c.bucket).where(
                receiver_app.daily_usage.c.scope == "ip"
            )
        ).scalars().all()
    assert len(buckets) == 2
    assert all(ip not in bucket for bucket in buckets for ip in ("198.51.100.10", "198.51.100.11"))


def test_ingest_per_profile_daily_packet_budget_429():
    """每 profile 每日包数上限：同一 profile 已满 → 429；其它 profile 不受影响。"""
    s = Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:", per_install_daily_packets=2)
    app2 = create_app(s)
    with TestClient(app2) as c:
        with app2.state.engine.begin() as conn:
            for _ in range(2):
                i = _
                conn.execute(packet_receipts.insert().values(
                    packet_id=f"seed-{i}", identity="profile-test-0001", row_id=i + 1))
        resp = c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 429
        assert "packet budget" in resp.json()["detail"]
        body = _valid_body()
        body["install_id"] = "other-inst"
        body["profile_id"] = "profile-test-other"
        body["packet_id"] = "pkt-test-other"
        resp2 = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp2.status_code == 200


def test_settings_from_env_requires_independent_stats_token(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", TOKEN)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.delenv("STATS_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="STATS_TOKEN"):
        Settings.from_env()

    monkeypatch.setenv("STATS_TOKEN", TOKEN)
    with pytest.raises(RuntimeError, match="必须与 INGEST_TOKEN 不同"):
        Settings.from_env()


def test_settings_from_env_reads_database_pool_bounds(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "ingest-a")
    monkeypatch.setenv("STATS_TOKEN", "stats-b")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("DB_POOL_SIZE", "9")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "11")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "4.5")
    settings = Settings.from_env()
    assert (settings.db_pool_size, settings.db_max_overflow, settings.db_pool_timeout) == (9, 11, 4.5)


def test_existing_database_migrates_raw_bytes_and_seeds_daily_counter(tmp_path):
    path = tmp_path / "legacy.db"
    engine = receiver_app.build_engine(f"sqlite:///{path}")
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE ingest_packets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                received_at DATETIME NOT NULL,
                install_id TEXT NOT NULL,
                schema TEXT NOT NULL,
                ua TEXT,
                cache_generation TEXT,
                n_usage INTEGER NOT NULL DEFAULT 0,
                n_benchfb INTEGER NOT NULL DEFAULT 0,
                payload JSON NOT NULL
            )
        """))
        conn.execute(text("""
            INSERT INTO ingest_packets
                (received_at, install_id, schema, n_usage, n_benchfb, payload)
            VALUES (:received_at, 'legacy', 'biodata-telemetry/1', 0, 0, '{"x":"legacy"}')
        """), {"received_at": now})
    engine.dispose()

    app2 = create_app(Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url=f"sqlite:///{path}"
    ))
    with app2.state.engine.connect() as conn:
        row = conn.execute(select(ingest_packets.c.raw_bytes)).scalar_one()
        used = conn.execute(
            select(receiver_app.daily_usage.c.raw_bytes).where(
                receiver_app.daily_usage.c.scope == "global"
            )
        ).scalar_one()
        receipt = conn.execute(select(packet_receipts)).mappings().one()
    assert row > 0 and used == row
    assert receipt["packet_id"] == legacy_packet_id({"x": "legacy"})
    assert receipt["identity"] == "legacy" and receipt["row_id"] == 1


# --- 保留期清理（scripts/telemetry_retention.py）---


def test_retention_delete_expired(app, tmp_path):
    """保留清理：早于保留期（90 天）的行删除，新行保留；dry-run 只读报告。"""
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import select

    now = datetime.now(timezone.utc)
    with app.state.engine.begin() as conn:
        conn.execute(ingest_packets.insert().values(
            install_id="ret-old", schema="biodata-telemetry/1",
            received_at=now - timedelta(days=120), payload={"x": 1}))
        conn.execute(ingest_packets.insert().values(
            install_id="ret-new", schema="biodata-telemetry/1",
            received_at=now - timedelta(days=30), payload={"x": 2}))
        conn.execute(packet_receipts.insert().values(
            packet_id="ret-p-old", identity="p", row_id=1, received_at=now - timedelta(days=120)))
        conn.execute(packet_receipts.insert().values(
            packet_id="ret-p-new", identity="p", row_id=2, received_at=now - timedelta(days=30)))
        conn.execute(event_receipts.insert().values(
            event_key="ret-e-old", packet_id="ret-p-old", kind="usage", received_at=now - timedelta(days=120)))
        conn.execute(event_receipts.insert().values(
            event_key="ret-e-new", packet_id="ret-p-new", kind="usage", received_at=now - timedelta(days=30)))

    export_dir = tmp_path / "export"
    export_dir.mkdir()
    old_export = export_dir / "old.jsonl"
    new_export = export_dir / "new.jsonl"
    old_export.write_text("old", encoding="utf-8")
    new_export.write_text("new", encoding="utf-8")
    old_ts = (now - timedelta(days=120)).timestamp()
    os.utime(old_export, (old_ts, old_ts))
    settings = Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url="sqlite:///:memory:",
        retention_days=90, export_dir=str(export_dir),
    )
    assert receiver_app.count_expired(app.state.engine, days=90) == 1
    summary = receiver_app.run_retention_once(app.state.engine, settings)
    assert summary["deleted_packets"] == 1
    assert summary["deleted_export_files"] == 1 and summary["export_cleanup_ok"] is True
    assert not old_export.exists() and new_export.exists()
    with app.state.engine.connect() as conn:
        ids = [r[0] for r in conn.execute(select(ingest_packets.c.install_id))]
        packets = [r[0] for r in conn.execute(select(packet_receipts.c.packet_id))]
        events = [r[0] for r in conn.execute(select(event_receipts.c.event_key))]
    assert ids == ["ret-new"]
    assert packets == ["ret-p-new"] and events == ["ret-e-new"]
    state = receiver_app._read_service_state(app.state.engine, "last_retention")
    assert state and state["retention_days"] == 90


# --- mcp_records（additive）+ GET /v1/stats ---


def _mcp_body() -> dict:
    """带 mcp_records 的包（新键，additive；幂等键 = call_id）。"""
    body = _valid_body()
    body["packet_id"] = "pkt-mcp-0001"
    body["mcp_records"] = [
        {"call_id": "mcp-call-1", "ts": 1000, "tool": "recommend_datasets", "args": {"q": "10x"}, "ok": True},
        {"call_id": "mcp-call-2", "ts": 2000, "tool": "get_file_manifest", "ok": False, "error": "uid missing"},
    ]
    return body


# --- 意见反馈（加解密帮助函数 + 测试；协议与客户端 feedback_core.js 逐字段同源）---
# 测试用 cryptography 模拟客户端加密：开发者私钥 PEM/公钥 base64 + 临时 ECDH 密钥对 →
# HKDF-SHA256(salt=b"biodata-feedback-v1", info=b"biodata-feedback/1") → AES-256-GCM。


def _feedback_keypair():
    """生成开发者密钥对：返回 (PEM 私钥, base64 公钥未压缩点)。"""
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_b64 = base64.b64encode(priv.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode()
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()
    return pem, pub_b64


def _encrypt_feedback(plain: dict, pub_b64: str, *, feedback_id: str = "fb-test-0001",
                      identity: str = "profile-test-0001", with_diag: bool = True) -> dict:
    """模拟客户端加密一条意见 → FeedbackRecord 形状 dict。"""
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), base64.b64decode(pub_b64))
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), pub)
    aes_key = HKDF(algorithm=hashes.SHA256(), length=32,
                   salt=b"biodata-feedback-v1", info=b"biodata-feedback/1").derive(shared)
    nonce = os.urandom(12)
    ciphertext = AESGCM(aes_key).encrypt(
        nonce, json.dumps(plain, ensure_ascii=False).encode("utf-8"), None)
    return {
        "feedback_id": feedback_id,
        "identity": identity,
        "ephemeral_pubkey": base64.b64encode(ephemeral.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
        "with_diag": with_diag,
    }


def _feedback_body(*, priv_pem: str, pub_b64: str, packet_id: str = "pkt-fb-0001",
                   plain: dict | None = None, with_diag: bool = True) -> dict:
    """构造带 feedback_records 的完整上传包。"""
    body = _valid_body()
    body["packet_id"] = packet_id
    body["feedback_records"] = [_encrypt_feedback(
        plain if plain is not None else {"feedback_id": "fb-test-0001",
                                         "authorized_at": "2026-08-22T06:00:00Z",
                                         "text": "建议在结果页加导出按钮", "diag": None},
        pub_b64, with_diag=with_diag)]
    return body


def test_ingest_feedback_roundtrip_decrypts_and_masks():
    """加解密往返：PEM 私钥解密成功；明文过值级遮蔽（API Key 形态）后落库；
    with_diag 透传；响应 accepted_feedback=1、sanitized≥1、不回显明文。"""
    priv_pem, pub_b64 = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_pem))
    with TestClient(app) as c:
        plain = {"feedback_id": "fb-test-0001", "authorized_at": "2026-08-22T06:00:00Z",
                 "text": f"搜「肺癌」时我的 key 是 {FAKE_API_KEY} 很卡",
                 "diag": {"available": True, "errors": 2, "features": {"search": 3}}}
        body = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64, plain=plain, with_diag=True)
        resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True and data["accepted_feedback"] == 1
        assert data["sanitized"] >= 1  # sk-… 命中 API Key 遮蔽
        with app.state.engine.connect() as conn:
            row = conn.execute(ingest_packets.select()).mappings().fetchone()
        stored = row["payload"]["feedback_records"][0]
        assert stored["feedback_id"] == "fb-test-0001"
        assert stored["with_diag"] is True and stored["identity"] == "profile-test-0001"
        assert stored["text"] == "搜「肺癌」时我的 key 是 [API Key] 很卡"
        assert stored["diag"] == {"available": True, "errors": 2, "features": {"search": 3}}
        # 密文载荷不进 payload（明文已还原并遮蔽；ephemeral/nonce/ciphertext 不落库）
        assert "ciphertext" not in stored and "ephemeral_pubkey" not in stored
        # 明文（含 API Key 原值）不出现在响应/库存任何位置
        assert FAKE_API_KEY not in resp.text
        assert FAKE_API_KEY not in json.dumps(row["payload"], ensure_ascii=False)


def test_ingest_feedback_accepts_base64_der_key():
    """私钥支持 base64 DER（PKCS8）格式——环境变量单行化部署友好。"""
    priv_pem, pub_b64 = _feedback_keypair()
    der_b64 = base64.b64encode(serialization.load_pem_private_key(
        priv_pem.encode("utf-8"), password=None).private_bytes(
        serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption())).decode()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=der_b64))
    with TestClient(app) as c:
        body = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64)
        resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200, resp.text
        assert resp.json()["accepted_feedback"] == 1


def test_ingest_feedback_without_key_422_and_other_paths_untouched():
    """未配置 FEEDBACK_DECRYPT_KEY：带 feedback_records → 422 明确错误；
    同应用不带 feedback_records 的 usage/benchfb/mcp 包照常 200（路径完全不变）。"""
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:"))
    with TestClient(app) as c:
        # 需要一把能过 schema 的公钥/密文（校验先于解密）；私钥缺失在解密阶段报 422
        _pem, pub_b64 = _feedback_keypair()
        body = _feedback_body(priv_pem=_pem, pub_b64=pub_b64)
        resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422
        assert "feedback" in json.dumps(resp.json(), ensure_ascii=False)
        # 无 feedback_records 的既有包不受影响
        assert c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN}).status_code == 200
        assert c.post("/v1/ingest", json=_mcp_body(), headers={"X-Ingest-Token": TOKEN}).status_code == 200


def test_ingest_feedback_wrong_key_422():
    """密钥不匹配（密文由另一对密钥加密）→ 解密失败 422，包不落库。"""
    priv_a, pub_a = _feedback_keypair()
    priv_b, _pub_b = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_b))
    with TestClient(app) as c:
        body = _feedback_body(priv_pem=priv_a, pub_b64=pub_a)
        resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422
        with app.state.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(ingest_packets)).scalar_one() == 0


def test_ingest_feedback_same_packet_retry_idempotent():
    """同一包重试：packet 级幂等 → duplicate=true 且 accepted_feedback=0。"""
    priv_pem, pub_b64 = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_pem))
    with TestClient(app) as c:
        body = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64)
        first = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        again = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert first.status_code == 200 and again.status_code == 200
        assert first.json()["accepted_feedback"] == 1
        assert again.json()["duplicate"] is True and again.json()["accepted_feedback"] == 0


def test_ingest_feedback_replay_same_feedback_id_dedup():
    """事件级幂等：不同 packet 重传同一 feedback_id（新密文/新 ephemeral）→
    第二包 accepted_feedback==0；usage 事件照常收（互不影响）。"""
    priv_pem, pub_b64 = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_pem))
    with TestClient(app) as c:
        first = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64, packet_id="pkt-fb-0001")
        resp1 = c.post("/v1/ingest", json=first, headers={"X-Ingest-Token": TOKEN})
        assert resp1.json()["accepted_feedback"] == 1
        second = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64, packet_id="pkt-fb-0002")
        # 换新 usage event_id：验证 usage 事件与 feedback 去重互不影响（既有语义照旧）
        second["usage_events"] = [{"event_id": "u3", "t": 2, "k": "search", "q": "10x"},
                                  {"event_id": "u4", "t": 3, "k": "open", "rank": 0}]
        resp2 = c.post("/v1/ingest", json=second, headers={"X-Ingest-Token": TOKEN})
        assert resp2.json()["accepted_feedback"] == 0
        assert resp2.json()["accepted_usage"] == 2  # usage 事件独立于 feedback 去重
        # 换 identity 视为新事件（profile/install 语义）
        third = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64, packet_id="pkt-fb-0003")
        third["feedback_records"][0]["identity"] = "profile-rotated-9999"
        resp3 = c.post("/v1/ingest", json=third, headers={"X-Ingest-Token": TOKEN})
        assert resp3.json()["accepted_feedback"] == 1


def test_ingest_feedback_shape_422():
    """feedback_records 严格模型：未知字段 / feedback_id 非法 → 422。"""
    priv_pem, pub_b64 = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_pem))
    with TestClient(app) as c:
        base = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64)
        bads = [
            {"feedback_records": [{"extra_field": "x"}]},          # 缺必填 + 未知字段
            {"feedback_records": [dict(base["feedback_records"][0], api_key="sk-nope")]},  # extra="forbid"
            {"feedback_records": [dict(base["feedback_records"][0], feedback_id="short")]},  # <8 字符
            {"feedback_records": [dict(base["feedback_records"][0], ciphertext="x" * 30000)]},  # 超长
        ]
        for over in bads:
            body = _valid_body()
            body.update(over)
            resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
            assert resp.status_code == 422, f"{over} 应 422"


def test_ingest_feedback_tampered_ciphertext_422():
    """密文被篡改（GCM tag 校验失败）→ 422，不落库、不回显明文。"""
    priv_pem, pub_b64 = _feedback_keypair()
    app = create_app(Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:",
                              feedback_decrypt_key=priv_pem))
    with TestClient(app) as c:
        body = _feedback_body(priv_pem=priv_pem, pub_b64=pub_b64)
        record = body["feedback_records"][0]
        raw = bytearray(base64.b64decode(record["ciphertext"]))
        raw[0] ^= 0x01  # 翻一位
        record["ciphertext"] = base64.b64encode(bytes(raw)).decode()
        resp = c.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422
        with app.state.engine.connect() as conn:
            assert conn.execute(select(func.count()).select_from(ingest_packets)).scalar_one() == 0


def test_ingest_feedback_old_client_without_feedback_records_ok(client):
    """旧客户端包（无 feedback_records 键）照常 200，响应带 accepted_feedback=0（additive）。"""
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["accepted_feedback"] == 0


def test_ingest_mcp_records_ok_and_persists(client, app):
    """mcp_records 包 200；payload 原样存；response 带 accepted_mcp；receipt 落 kind='mcp'。"""
    import hashlib

    resp = client.post("/v1/ingest", json=_mcp_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True and body["accepted_mcp"] == 2
    with app.state.engine.connect() as conn:
        row = conn.execute(ingest_packets.select()).mappings().fetchone()
        assert len(row["payload"]["mcp_records"]) == 2
        assert row["payload"]["mcp_records"][0]["call_id"] == "mcp-call-1"
        keys = conn.execute(select(event_receipts.c.event_key).where(event_receipts.c.kind == "mcp")).scalars().all()
    # 幂等键 = sha256(identity|"mcp"|call_id)； mcp 的 identity = install_id
    # （整机口径：同一安装切换匿名账户重传同一 call_id 不再重复入库）
    expected = {
        hashlib.sha256("inst-1|mcp|mcp-call-1".encode()).hexdigest(),
        hashlib.sha256("inst-1|mcp|mcp-call-2".encode()).hexdigest(),
    }
    assert set(keys) == expected


def test_ingest_mcp_records_dedup_across_profiles_same_install(client, app):
    """：同 install_id 不同 profile_id 重传同 call_id → 第二包 accepted_mcp==0。"""
    first = _mcp_body()
    assert client.post("/v1/ingest", json=first, headers={"X-Ingest-Token": TOKEN}).status_code == 200
    second = _mcp_body()
    second["packet_id"] = "pkt-mcp-0003"
    second["profile_id"] = "profile-rotated-9999"  # 账户轮换；install 不变
    resp = client.post("/v1/ingest", json=second, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["accepted_mcp"] == 0  # 两个 call_id 均已按 install 口径收过
    # 对照：usage 事件仍按 profile 口径，换 profile 后 u1/u2 视为新事件
    assert resp.json()["accepted_usage"] == 2


def test_ingest_mcp_records_overlap_dedup(client, app):
    """重叠 batch：同 call_id 只收首次；新 call_id 照收；旧客户端不带该键不受影响。"""
    first = _mcp_body()
    assert client.post("/v1/ingest", json=first, headers={"X-Ingest-Token": TOKEN}).status_code == 200
    second = _mcp_body()
    second["packet_id"] = "pkt-mcp-0002"
    second["mcp_records"] = [
        {"call_id": "mcp-call-1", "ts": 1000, "tool": "recommend_datasets", "ok": True},
        {"call_id": "mcp-call-3", "ts": 3000, "tool": "search_datasets", "ok": True},
    ]
    resp = client.post("/v1/ingest", json=second, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["accepted_mcp"] == 1
    with app.state.engine.connect() as conn:
        rows = conn.execute(ingest_packets.select().order_by(ingest_packets.c.id)).mappings().all()
    assert [r["payload"]["mcp_records"][0]["call_id"] for r in rows] == ["mcp-call-1", "mcp-call-3"]


def test_ingest_mcp_records_same_packet_retry_idempotent(client, app):
    body = _mcp_body()
    first = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    again = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert first.status_code == 200 and again.status_code == 200
    assert again.json()["duplicate"] is True and again.json()["accepted_mcp"] == 0


def test_ingest_mcp_records_shape_422(client):
    """mcp_records 元素必须为 dict 且带字符串 call_id（与 usage/benchfb 同风格形状约束）。"""
    bads = [
        {"mcp_records": ["not-a-dict"]},                    # 元素非对象
        {"mcp_records": [{"ts": 1}]},                       # 缺 call_id
        {"mcp_records": [{"call_id": 123}]},                # call_id 非字符串
    ]
    for over in bads:
        body = _valid_body()
        body.update(over)
        resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 422, f"{over} 应 422"


def test_ingest_old_client_without_mcp_records_ok(client, app):
    """旧客户端包（无 mcp_records 键）照常 200，response 带 accepted_mcp=0（additive）。"""
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["accepted_mcp"] == 0


# --- 落库前净化（与客户端 telemetryStrip 同规则的防御纵深）---


def test_ingest_sanitizes_phone_in_free_text(client, app):
    """自由文本里的手机号落库前遮蔽为 [手机号]（边界字符保留），响应 sanitized≥1。"""
    body = _valid_body()
    body["usage_events"] = [{"event_id": "u9", "t": 9, "k": "search", "q": "联系我13812345678谢谢"}]
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["sanitized"] >= 1
    with app.state.engine.connect() as conn:
        row = conn.execute(ingest_packets.select()).mappings().fetchone()
    assert row["payload"]["usage_events"][0]["q"] == "联系我[手机号]谢谢"


def test_ingest_sanitizes_secret_keys_and_base_url(client, app):
    """秘密键整键剔除（递归）；base_url 只留 host；id 卡号/邮箱值级遮蔽。"""
    body = _valid_body()
    body["usage_events"] = [{
        "event_id": "u9", "t": 9, "k": "mcp",
        "api_key": FAKE_API_KEY,
        "nested": {"Password": "p@ss", "email": "zhang.san@example.com"},
        "base_url": "https://proxy.internal:8443/v1/chat?token=abc",
        "note": "证件 110101199003074321 备用",
    }]
    resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["sanitized"] >= 4  # api_key + Password + email 键 + base_url + 证件号
    with app.state.engine.connect() as conn:
        row = conn.execute(ingest_packets.select()).mappings().fetchone()
    stored = row["payload"]["usage_events"][0]
    assert "api_key" not in stored
    assert stored["nested"] == {}                       # Password 与 email 两键均剔除
    assert stored["base_url"] == "proxy.internal:8443"  # host（含端口）保留，路径/查询整段不采
    assert stored["note"] == "证件 [证件号] 备用"
    # 响应/库存任何位置都不得出现原始秘密值
    assert FAKE_API_KEY not in resp.text
    assert "p@ss" not in json.dumps(row["payload"], ensure_ascii=False)


def test_ingest_clean_packet_reports_sanitized_zero(client):
    """本无需处理的包：响应 sanitized==0（additive 新键恒在）。"""
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    assert resp.json()["sanitized"] == 0
    # 重复包路径：duplicate=true 且 sanitized==0（本次零写入）
    again = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert again.json()["duplicate"] is True and again.json()["sanitized"] == 0


# --- 自适应上传阈值（server_hint）---

_ALLOWED_HINT_BANDS = {(2, 30_000), (5, 120_000), (20, 300_000)}


def test_ingest_200_carries_server_hint_shape(client):
    """200 响应带 additive server_hint：pressure∈[0,1]、批量/间隔为合法档位组合。"""
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    assert resp.status_code == 200
    hint = resp.json()["server_hint"]
    assert isinstance(hint, dict) and set(hint) == {
        "pressure", "batch_threshold", "min_interval_ms", "max_body_bytes",
    }
    assert isinstance(hint["pressure"], float) and 0.0 <= hint["pressure"] <= 1.0
    assert isinstance(hint["batch_threshold"], int) and isinstance(hint["min_interval_ms"], int)
    assert (hint["batch_threshold"], hint["min_interval_ms"]) in _ALLOWED_HINT_BANDS
    assert hint["max_body_bytes"] == receiver_app.MAX_BODY_BYTES


def test_server_hint_low_band_when_idle(client):
    """空闲态（首请求：在途 1、窗口尝试 1、今日字节极小）→ 低档 2/30s。"""
    resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    hint = resp.json()["server_hint"]
    assert hint["batch_threshold"] == 2 and hint["min_interval_ms"] == 30_000
    assert hint["pressure"] < 0.3


def test_server_hint_present_on_duplicate_retry(client):
    """重复包路径（跳过配额检查）同样带 server_hint——压力提示与配额检查解耦。"""
    for _ in range(2):
        resp = client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200
    hint = resp.json()["server_hint"]
    assert hint["batch_threshold"] == 2 and hint["min_interval_ms"] == 30_000


def test_server_hint_daily_bytes_component_feeds_band(client):
    """今日已收字节分量真实流入压力：预算 5000 预灌 ~2000 字节 → 字节比 0.4+ 顶起中档。"""
    s = Settings(ingest_token=TOKEN, database_url="sqlite:///:memory:", daily_bytes_budget=5000)
    app2 = create_app(s)
    with TestClient(app2) as c:
        with app2.state.engine.begin() as conn:
            conn.execute(
                receiver_app.daily_usage.update().where(
                    receiver_app.daily_usage.c.scope == "global",
                    receiver_app.daily_usage.c.bucket == "all",
                ).values(raw_bytes=2000)
            )
        resp = c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200
        hint = resp.json()["server_hint"]
        # 预灌 2000 + 本次 ~500 字节 / 5000 预算 ≈ 0.5 → 中档
        assert hint["batch_threshold"] == 5 and hint["min_interval_ms"] == 120_000
        assert 0.3 <= hint["pressure"] < 0.7


def test_server_hint_rate_limiter_component_feeds_band(client):
    """限流窗口尝试分量真实流入压力：90 次请求 → 90/300=0.3 → 中档（换 profile 规避 30/min 闸）。"""
    for i in range(90):
        body = _valid_body()
        body["packet_id"] = f"pkt-hint-{i:04d}"
        body["profile_id"] = f"profile-hint-{i:04d}"
        resp = client.post("/v1/ingest", json=body, headers={"X-Ingest-Token": TOKEN})
        assert resp.status_code == 200, resp.text
    hint = resp.json()["server_hint"]
    assert hint["batch_threshold"] == 5 and hint["min_interval_ms"] == 120_000
    assert 0.3 <= hint["pressure"] < 0.7


def test_server_hint_pressure_components_unit():
    """分量单元：任一信号顶到最大即整体压力；分母取不到（0）按 0 处理。"""
    from app import _server_pressure_hint

    # 在途分量主导
    hint = _server_pressure_hint(in_flight=16, window_attempts=1, rate_limit_max=300,
                                 daily_used_bytes=0, daily_bytes_budget=100)
    assert hint["batch_threshold"] == 20 and hint["min_interval_ms"] == 300_000
    # 限流窗口分量主导
    hint = _server_pressure_hint(in_flight=0, window_attempts=210, rate_limit_max=300,
                                 daily_used_bytes=0, daily_bytes_budget=100)
    assert hint["batch_threshold"] == 20 and hint["min_interval_ms"] == 300_000
    # 今日字节分量主导
    hint = _server_pressure_hint(in_flight=0, window_attempts=0, rate_limit_max=300,
                                 daily_used_bytes=40, daily_bytes_budget=100)
    assert hint["batch_threshold"] == 5 and hint["min_interval_ms"] == 120_000
    # 分母为 0 → 该信号按 0（不抛错）
    hint = _server_pressure_hint(in_flight=0, window_attempts=0, rate_limit_max=0,
                                 daily_used_bytes=0, daily_bytes_budget=0)
    assert hint["batch_threshold"] == 2 and hint["min_interval_ms"] == 30_000


def test_server_hint_band_boundaries_unit():
    """离散档边界：<0.3 → 低档；0.3 起中档；0.7 起高档。"""
    from app import _server_pressure_hint

    for pressure_input, expected in (
        (0.0, (2, 30_000)),
        (0.29, (2, 30_000)),
        (0.3, (5, 120_000)),
        (0.69, (5, 120_000)),
        (0.7, (20, 300_000)),
        (1.0, (20, 300_000)),
    ):
        hint = _server_pressure_hint(
            in_flight=0, window_attempts=int(pressure_input * 300), rate_limit_max=300,
            daily_used_bytes=0, daily_bytes_budget=100)
        assert (hint["batch_threshold"], hint["min_interval_ms"]) == expected, pressure_input


def test_server_hint_pressure_clamped_and_rounded_unit():
    """pressure 钳到 [0,1] 且取 4 位小数（负/超 1 的输入不产生非法档位）。"""
    from app import _server_pressure_hint

    hint = _server_pressure_hint(in_flight=-5, window_attempts=-10, rate_limit_max=300,
                                 daily_used_bytes=-1, daily_bytes_budget=100)
    assert hint["pressure"] == 0.0 and hint["batch_threshold"] == 2
    hint = _server_pressure_hint(in_flight=99, window_attempts=9999, rate_limit_max=10,
                                 daily_used_bytes=999, daily_bytes_budget=10)
    assert hint["pressure"] == 1.0 and hint["batch_threshold"] == 20


def test_server_hint_inflight_gauge_unit():
    """InFlightGauge 增减正确；同一事件循环内同步增减无需锁。"""
    from app import InFlightGauge

    gauge = InFlightGauge()
    assert gauge.value == 0
    gauge.incr(); gauge.incr(); gauge.incr()
    assert gauge.value == 3
    gauge.decr(); gauge.decr()
    assert gauge.value == 1


def test_rate_limiter_window_attempts_unit():
    """限流器窗口内尝试计数：含被拒请求；窗口滑动即归零。"""
    limiter = IpRateLimiter(max_requests=3, window_seconds=60.0)
    assert limiter.allow("ip-a") is True and limiter.allow("ip-a") is True
    assert limiter.allow("ip-a") is True and limiter.allow("ip-a") is False   # 第 4 次被拒
    assert limiter.window_attempts == 4, "被拒请求也应计入压力信号"


# --- GET /v1/stats ---


def test_stats_requires_token_401(client):
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats", headers={"X-Ingest-Token": TOKEN}).status_code == 401
    assert client.get("/v1/stats", headers={"X-Stats-Token": "wrong"}).status_code == 401


def test_stats_ok(client, app):
    """独立管理 token；全部轻量聚合，不扫 payload 大字段。"""
    client.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
    client.post("/v1/ingest", json=_mcp_body(), headers={"X-Ingest-Token": TOKEN})
    resp = client.get("/v1/stats", headers={"X-Stats-Token": STATS_TOKEN})
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["packets_total"] == 2
    assert data["events_total"] == 5          # usage 2 + benchfb 1（两包事件 id 相同被 receipts 去重）+ mcp 2
    assert data["last_24h_packets"] == 2
    assert data["db_size_bytes"] is None      # SQLite 内存库无文件体积
    assert isinstance(data["oldest_packet_at"], str)
    assert data["retention"]["retention_days"] == 90
    assert isinstance(data["retention"]["completed_at"], str)


def test_stats_db_size_bytes_for_sqlite_file(tmp_path):
    """SQLite 文件库：db_size_bytes 返回真实文件大小。"""
    path = tmp_path / "stats.db"
    app = create_app(Settings(
        ingest_token=TOKEN, stats_token=STATS_TOKEN, database_url=f"sqlite:///{path}"
    ))
    with TestClient(app) as c:
        c.post("/v1/ingest", json=_valid_body(), headers={"X-Ingest-Token": TOKEN})
        data = c.get("/v1/stats", headers={"X-Stats-Token": STATS_TOKEN}).json()
    assert isinstance(data["db_size_bytes"], int) and data["db_size_bytes"] > 0
    assert data["packets_total"] == 1
