# -*- coding: utf-8 -*-
"""后端安全 / 联网 / 文案修复的回归门。

每条用例钉住一类回归病形：
- 联网限速并发绕过（`_polite_wait` 裸 dict 无锁）；
- write_boundary 成功回执机制词三连上屏；
- 10x「部分覆盖」诚实声明逐字手抄两份（随全量 API 通道退役，改钉不许复活）；
- duplicate_content hint 残留「（force=True）」；
- `_NONVALUE` 缺失哨兵第二定义（与 normalizer 真源漂移隐患）；
- 全站零安全响应头（localhost 点击劫持面）；
- Host 守卫对畸形 Host fail-open（starlette 洗白成绑定地址）；
- 同源闸在 pydantic 校验之后（跨源非法 body 吃 422 而非 403）；
- `/api/recommend` 非法日期静默吞 / 冒充生效条件上屏；
- MCP `internal_error` 外抛异常正文（可含本机绝对路径）；
- rerank `_LAST_LLM_ERROR` 模块级槽并发互踩误标。

纪律与邻组一致：真实语料写路径一律走 tmp fixture；禁网；TestClient base_url 用 loopback。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402
from dataset_recommender.corpus import corpus_net as cn  # noqa: E402
from dataset_recommender.retrieval import rerank as rr  # noqa: E402
from dataset_recommender.app import webapp  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402
from dataset_recommender.retrieval.normalizer import MISSING_VALUE_TOKENS, DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402

client = TestClient(webapp.app, base_url="http://127.0.0.1")


# ======================================================================================
# 限速锁——8 线程并发下相邻放行间隔仍 ≥ min_interval
# ======================================================================================

def test_polite_wait_enforces_interval_under_8_thread_concurrency():
    """病形：无锁时 8 线程 1.00s 内全部放行、相邻间隔违规 6 次。
    持锁后每个放行槽位两两间隔必须 ≥ interval（阈值留 20% 调度余量，防宿主机抖动误红）。"""
    host = "concurrency-canary.test"
    interval = 0.3
    cn._last_request_by_host.clear()
    slots: list[float] = []
    slots_lock = threading.Lock()

    def worker() -> None:
        cn._polite_wait(host, interval)
        with slots_lock:
            slots.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert all(not t.is_alive() for t in threads), "限速等待卡死"
    ordered = sorted(slots)
    assert len(ordered) == 8
    gaps = [b - a for a, b in zip(ordered, ordered[1:])]
    assert all(g >= interval * 0.8 for g in gaps), f"并发下限速被绕过：gaps={gaps}"
    assert ordered[-1] - started >= interval * 7 * 0.8, "8 个请求不该瞬时全放"


# ======================================================================================
# write_boundary 成功回执——机制词退结构化字段，诚实语义一字不丢
# ======================================================================================

@pytest.mark.parametrize("action", ["import", "search_online"])
def test_write_boundary_success_receipts_speak_human(action):
    text = cc.write_boundary_zh(action, dry_run=False)
    # 机制词不上屏（落盘位置走 saved_to / moved_to 等结构化字段）
    for jargon in ("upload_*", "命名空间", "curate.restore", "写盘", "force=True"):
        assert jargon not in text, f"{action} 成功回执仍含机制词 {jargon!r}：{text}"
    # 诚实语义不丢：写进了哪 + 官方基准一个字节没动
    assert "database/external/" in text and "database/base/" in text


def test_write_boundary_remove_restore_keep_reversibility():
    rm = cc.write_boundary_zh("remove", dry_run=False)
    assert "回收站" in rm and "可逆" in rm and "没有真删除" in rm
    assert "curate.restore" not in rm and ".userdata" not in rm
    rs = cc.write_boundary_zh("restore", dry_run=False)
    assert "回收站" in rs and "database/external/" in rs


def test_write_boundary_list_and_preview_promises_unchanged():
    """只读/预览档的既有钉死断言（tests/test_mcp_curation.py 同款关键字）不许被文案重写碰掉。"""
    assert "只读" in cc.write_boundary_zh("list", dry_run=True)
    preview = cc.write_boundary_zh("import", dry_run=True)
    assert "预览" in preview and "没有写入" in preview
    assert "联网" in cc.write_boundary_zh("search_online", dry_run=True)


# ======================================================================================
# 10x 诚实声明单一真源 → 整段退役
# ======================================================================================
# 原问题：「部分覆盖」声明逐字手抄两份。10x 接入官网私有搜索 API（全量 786 条）后，
# 「页面只列精选条目」的事实本身不再成立——
# 声明与 partial 概念随之退役，改钉「不许复活」+ 新的诚实面（私有接口漂移如实失败）。

def test_tenx_partial_coverage_note_is_retired():
    """全量 API 通道落地后，「受 10x 官网页面技术限制…部分条目」声明不许在任何一侧复活。"""
    assert not hasattr(cn, "TENX_PARTIAL_COVERAGE_NOTE_ZH"), "10x 已是全量通道，partial 声明应退役"
    for mod in (cc, cn):
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "受 10x 官网页面技术限制" not in src
        assert "部分覆盖" not in src


def test_check_updates_tenx_full_coverage_has_no_partial_caveat(monkeypatch, tmp_path):
    """curation 侧新事实：10x 在线比对按全量清单差分，note 不再挂「部分覆盖」括注。"""
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True)
    (base / "10x-Visium.json").write_text(json.dumps([{"dataset_name": "丙"}]), encoding="utf-8")
    monkeypatch.setattr(cc.corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True, "total": 1,
        "items": [{"accession": "new-one", "title": "新条目", "url": "https://x/new-one"}],
    })
    res = cc.check_updates(["10x"], project_root=tmp_path)
    note = res["sources"][0]["note_zh"]
    assert "目录里还没有" in note
    assert "部分" not in note and "技术限制" not in note


# ======================================================================================
# duplicate_content——人话 hint 不带 API 参数名，参数名走结构化字段
# ======================================================================================

def test_duplicate_hint_drops_api_param_name(tmp_path):
    records = [{"dataset_name": "甲", "source": "测试源"}]
    raw = json.dumps({"records": records}, ensure_ascii=False).encode("utf-8")
    plan1 = cc.plan_import(raw, "a.json", None, project_root=tmp_path)
    cc.apply_import(raw, "a.json", None, confirm_token=plan1["confirm_token"], project_root=tmp_path)

    plan2 = cc.plan_import(raw, "a.json", None, project_root=tmp_path)
    dup = plan2["duplicate"]
    assert dup["is_duplicate"] is True
    assert "force" not in dup["hint"], f"人话 hint 不该出现 API 参数名：{dup['hint']}"
    assert "允许重复" in dup["hint"]
    assert dup["force_param"] == "force", "参数名退到结构化字段，供 MCP/程序化调用方读取"

    with pytest.raises(cc.CurateError) as exc_info:
        cc.apply_import(raw, "a.json", None, confirm_token=plan2["confirm_token"], project_root=tmp_path)
    assert exc_info.value.code == "duplicate_content"
    assert "force" not in exc_info.value.hint and "允许重复" in exc_info.value.hint


# ======================================================================================
# _NONVALUE 与 normalizer 单一真源对齐（import 派生，防漂移）
# ======================================================================================

def test_nonvalue_derives_from_normalizer_single_source():
    # 真源词表（归一化后）必须全量被摄取侧表覆盖——真源扩词时本表自动跟进
    assert {cc._norm_token(t) for t in MISSING_VALUE_TOKENS} <= cc._NONVALUE
    # 旧手抄表的全量词逐项仍在（对齐不许丢词）
    legacy = {"", "na", "n a", "nan", "null", "none", "not applicable", "not available",
              "not specified", "not collected", "not provided", "not reported",
              "unknown", "undetermined", "missing"}
    assert legacy <= cc._NONVALUE
    # 行为抽查：真实取值仍算已标注，占位拼写仍被滤
    assert cc._is_informative("lung") and not cc._is_informative("N/A")
    assert not cc._is_informative("undetermined")


# ======================================================================================
# 安全响应头（含 Host 守卫直返的 403 也要带头）
# ======================================================================================

def test_security_headers_on_normal_and_guard_rejected_responses():
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.headers["X-Frame-Options"] == "SAMEORIGIN"
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["Referrer-Policy"] == "no-referrer"

    denied = client.get("/api/health", headers={"Host": "evil.example"})
    assert denied.status_code == 403
    assert denied.headers["X-Frame-Options"] == "SAMEORIGIN", "守卫直返的 403 也要带头（middleware 栈外序）"


# ======================================================================================
# 畸形 Host fail-closed（读原始头自解析，不信 starlette 洗白）
# ======================================================================================

@pytest.mark.parametrize("host", [
    "127.0.0.1:abc",            # 畸形端口（旧代码曾 200）
    "127.0.0.1:7981:80",        # 双端口（验证 旧代码 200）
    "127.0.0.1:abc@evil.com",   # userinfo 混入（验证 旧代码 200）
    "evil.example",             # 合法但非 loopback（既有行为，防回归放开）
])
def test_malformed_or_foreign_host_is_rejected_fail_closed(host):
    res = client.get("/api/health", headers={"Host": host})
    assert res.status_code == 403, f"Host {host!r} 应 403，实为 {res.status_code}"


def test_normal_loopback_host_still_accepted():
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/health", headers={"Host": "localhost:8000"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "[::1]:8000"}).status_code == 200


# ======================================================================================
# 同源闸提到 pydantic 校验之前——跨源非法 body 吃 403 而非 422
# ======================================================================================

def test_cross_origin_bad_body_gets_403_not_422():
    cross = client.post("/api/curate/plan", json={}, headers={"Origin": "https://evil.example"})
    assert cross.status_code == 403, "跨源请求不该先吃到参数校验细节"
    # 同源 + 非法 body 仍走正常 422（守卫没误伤校验链）
    same = client.post("/api/curate/plan", json={}, headers={"Origin": "http://127.0.0.1"})
    assert same.status_code == 422


def test_read_routes_do_not_require_origin():
    """GET 全只读：带外域 Origin 的读请求行为不变（既有契约，probe 逐项核过）。"""
    res = client.get("/api/health", headers={"Origin": "https://evil.example"})
    assert res.status_code == 200


# ======================================================================================
# /api/recommend 非法日期 → 400（不静默吞、不冒充生效）
# ======================================================================================

def _empty_meta() -> SimpleNamespace:
    return SimpleNamespace(
        answer="", pipeline="rules", llm_attempted=False, llm_succeeded=False,
        llm_response_used=False, llm_provider=None, llm_mode=None, prompt_name=None,
        fallback=False, fallback_reason=None, retrieved_data=[], relaxation_options=[],
    )


def test_recommend_invalid_dates_get_400_before_workflow(monkeypatch):
    constructed: list[bool] = []
    captured: list[dict] = []

    class FakeWorkflow:
        def __init__(self):
            constructed.append(True)

        def run_with_meta(self, p=None, **kwargs):
            # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
            captured.append(vars(p) if p is not None else kwargs)
            return _empty_meta()

    monkeypatch.setattr(webapp, "DatasetRecommendationWorkflow", FakeWorkflow)

    bad_format = client.post("/api/recommend",
                             json={"query": "human data", "use_llm": False, "date_from": "not-a-date"})
    assert bad_format.status_code == 400
    assert "date_from" in bad_format.json()["detail"]

    bad_calendar = client.post("/api/recommend",
                               json={"query": "human data", "use_llm": False, "date_to": "2020-13-45"})
    assert bad_calendar.status_code == 400
    assert "date_to" in bad_calendar.json()["detail"]
    assert constructed == [], "非法日期必须在跑工作流之前拦掉"

    ok = client.post("/api/recommend", json={
        "query": "human data", "use_llm": False,
        "date_from": "2020-01-01", "date_to": "2023-12-31",
    })
    assert ok.status_code == 200, ok.text
    assert captured[0]["date_from"] == "2020-01-01" and captured[0]["date_to"] == "2023-12-31"


# ======================================================================================
# MCP internal_error 脱敏——客户端只见类型名，细节留 stderr
# ======================================================================================

def test_mcp_internal_error_sanitizes_exception_text(monkeypatch, capsys):
    from dataset_recommender.app import mcp_server as M
    from mcp.server.fastmcp.exceptions import ToolError

    # 假内部绝对路径（非家目录：scripts/build_release.py 的隐私门会拒收含 home 路径的发布输入，
    # 测试语料也不能带）；泄漏面（异常正文带绝对路径）与真实病形同构。
    secret_path = "C:\\opt\\biodata\\secret\\base.json"

    def boom(*_args, **_kwargs):
        raise PermissionError(f"[Errno 13] Permission denied: {secret_path}")

    monkeypatch.setattr("dataset_recommender.corpus.corpus.load_full_corpus", boom)
    with pytest.raises(ToolError) as exc_info:
        M.get_dataset_introduction(uid="uid:x")
    msg = str(exc_info.value)
    assert msg.startswith("internal_error: PermissionError"), msg
    assert secret_path not in msg and "Permission denied" not in msg
    # 排查细节不丢：完整堆栈留在服务器侧 stderr（stdio 协议外）
    assert secret_path in capsys.readouterr().err


# ======================================================================================
# rerank 错误归因 per-call 隔离——并发下 401 不被误标成临时故障
# ======================================================================================

def _cand(name: str) -> RetrievedCandidate:
    rec = DatasetRecord(
        dataset_name=name, species="human", tissue="lung", disease="cancer",
        chemistry="gex", count="1", unit="cell", has_raw_data=True,
        url="https://example.test", source_file="test.json", description=name,
        raw={}, family_id=name,
    )
    return RetrievedCandidate(rec, 0.0, [], [], "")


def test_rerank_llm_error_attribution_is_per_call_under_concurrency(monkeypatch):
    """旧模块级槽的互踩窗口：A 清槽 → B 清槽 → A 写 → B 写 → A 读（401 被读成 503 的档）。
    ContextVar 后两线程在同一窗口内交错也必须各拿各的归因。"""
    barrier = threading.Barrier(2)

    def fake_with_error(_prompt, _config):
        which = threading.current_thread().name
        barrier.wait(timeout=10)  # 强制两线程同时处于「已清槽、待写槽」窗口
        if which == "auth-thread":
            return None, "LLM HTTPError 401: invalid api key"
        return None, "LLM HTTPError 503: overloaded"

    monkeypatch.setattr(rr, "_default_llm_call_with_error", fake_with_error)
    cfg = LLMConfig(api_key="sk-not-a-real-key", enable_llm=True)
    traces: dict[str, dict] = {}

    def run(name: str) -> None:
        trace: dict = {}
        rr.rerank_candidates("q", [_cand("A"), _cand("B")], backend="llm", config=cfg, trace=trace)
        traces[name] = trace

    t1 = threading.Thread(target=run, args=("auth-thread",), name="auth-thread")
    t2 = threading.Thread(target=run, args=("busy-thread",), name="busy-thread")
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)
    assert traces["auth-thread"]["reason"] == "llm_auth_failed", traces
    assert traces["busy-thread"]["reason"] == "llm_call_failed", traces
