# -*- coding: utf-8 -*-
"""「并发分流与确定性 RAG 策略」· RAG flight 池级门（设计约定）。**全离线**：

- flight 池/deferred 准入：准入信号量满 → 第四请求标 deferred **不起线程**（禁止内联，
  r3）；join 点由调用线程同步补跑（=今天串行时序，正确性保底）。
- 有界性（barrier 驱动）：三槽占满后第四请求 deferred——不起额外线程、不堵死。
- abandoned+cancel：verdict=action 时置位；未起跑 future cancel 后不执行、槽位归还。
- join/done 幂等：重复 join 只执行一次，结果缓存。
- 预热闭合（r3）：主线程先 ensure vector/env 初始化，flight 线程全程零
  load_llm_config（workflow 两处 llm_available=False 短路生效）、_setup_determinism
  只在预热期主线程执行。
- _run 异常规范化：shape 与既有 status="error" 一致（rule_match_summary fail-open 双保险）。
"""
import json
import threading

import pytest

from dataset_recommender.agent import turn

_SP = {
    "top_k": None, "rerank": "off", "recall": "off", "strategy": "fixed",
    "facet_filters": None, "suppressed_constraints": None, "lenient_dims": None,
    "date_from": "", "date_to": "",
}

_OK_SUMMARY = {
    "status": "results", "total": 5, "top_titles": ["样本"],
    "abstain_reason": "", "unresolved_terms": [], "note": "",
}


@pytest.fixture(autouse=True)
def _fresh_pool(monkeypatch):
    """每测试一个干净的准入信号量（不串槽）；rule_match_summary 默认替换为确定性验证
    （**按真函数契约填 meta_out**——flight.meta 是发射闸与载荷的数据源）。"""
    monkeypatch.setattr(turn, "_RAG_SEMAPHORE", threading.Semaphore(turn._RAG_MAX_CONCURRENT))
    monkeypatch.setattr(turn, "rule_match_summary", _fake_rule_match_summary)
    yield


class _FakeMeta:
    """最小 WorkflowResult 替身：recommend_payload 需要的字段齐备（发射载荷数据源）。"""
    resolution_status = "results"
    result_total = 5
    answer = "规则生成说明"
    pipeline = "rule-based"
    llm_attempted = False
    llm_succeeded = False
    llm_response_used = False
    llm_provider = ""
    llm_mode = "disabled"
    fallback = "rule-based formatting"
    fallback_reason = ""
    retrieved_data = []
    facets = []
    clarification = None
    coverage_caveats = []
    unused_query_terms = []
    or_handling = None
    active_filters = []
    interpretation = ""
    search_trace = []
    audit = None


def _fake_rule_match_summary(*a, **k):
    holder = k.get("meta_out")
    if holder is not None:
        holder.append(_FakeMeta())
    return dict(_OK_SUMMARY)


def _flight(*, text: str = "human blood", search_params: dict | None = None):
    return turn._RagFlight(text, search_params=search_params or _SP)


def test_flight_start_and_join_returns_summary():
    """正常路径：start 提交线程池 → join 等完成 → summary 原样返回。"""
    flight = _flight()
    assert not flight.done()
    flight.start()
    assert flight._future is not None and not flight._deferred
    assert flight.join() == _OK_SUMMARY
    assert flight.done()
    assert flight.meta is not None  # meta_out 接住的 WorkflowResult（发射闸数据源）


def test_flight_join_and_done_are_idempotent(monkeypatch):
    """join/done 幂等：重复 join 只执行一次规则匹配，结果缓存。"""
    calls: list = []

    def counting(*a, **k):
        calls.append(1)
        return dict(_OK_SUMMARY)

    monkeypatch.setattr(turn, "rule_match_summary", counting)
    flight = _flight()
    flight.start()
    assert flight.join() == _OK_SUMMARY
    assert flight.join() == _OK_SUMMARY  # 幂等：第二次直接返回缓存
    assert flight.done()
    assert len(calls) == 1


def test_flight_deferred_when_pool_full():
    """准入信号量满（3 槽占满）→ 第四请求 deferred：不起线程（future None）。"""
    for _ in range(turn._RAG_MAX_CONCURRENT):
        turn._RAG_SEMAPHORE.acquire()  # 占满全部槽位（autouse fixture 的干净信号量）
    flight = _flight()
    flight.start()
    assert flight._deferred is True
    assert flight._future is None, "池满时不起线程（禁止内联跑，r3）"
    assert not flight.done()
    # join 点同步补跑（调用线程）——释放槽位后验证结果形状。
    turn._RAG_SEMAPHORE.release()
    assert flight.join() == _OK_SUMMARY
    assert flight.done()


def test_flight_deferred_join_runs_in_caller_thread(monkeypatch):
    """deferred 补跑在 join 点的**调用线程**同步执行（不新增线程）。"""
    threads: list = []

    def slow(*a, **k):
        threads.append(threading.current_thread().name)
        return dict(_OK_SUMMARY)

    monkeypatch.setattr(turn, "rule_match_summary", slow)
    for _ in range(turn._RAG_MAX_CONCURRENT):
        turn._RAG_SEMAPHORE.acquire()
    flight = _flight()
    flight.start()
    assert flight._deferred
    turn._RAG_SEMAPHORE.release()
    flight.join()
    assert threads == [threading.main_thread().name], "deferred 同步补跑必须在主线程"


def test_flight_bounded_pool_barrier_fourth_deferred(monkeypatch):
    """有界性（barrier 驱动，设计约定）：三槽全被阻塞验证占满后，第四请求 deferred——
    不起额外线程、不堵死（全部 join 有超时保护）。"""
    entered_all = threading.Event()
    entered_count = 0
    entered_lock = threading.Lock()
    release = threading.Event()

    def slow(*a, **k):
        nonlocal entered_count
        with entered_lock:
            entered_count += 1
            if entered_count == turn._RAG_MAX_CONCURRENT:
                entered_all.set()
        assert release.wait(timeout=15), "barrier 超时：测试线程与 flight 线程堵死"
        return dict(_OK_SUMMARY)

    monkeypatch.setattr(turn, "rule_match_summary", slow)
    flights = [_flight(text=f"q{i}") for i in range(turn._RAG_MAX_CONCURRENT)]
    for f in flights:
        f.start()
    assert entered_all.wait(timeout=15), "三槽应全部被阻塞探针占满"
    # 三槽占满：第四请求 deferred，不起额外线程。
    f4 = _flight(text="q4")
    f4.start()
    assert f4._deferred is True and f4._future is None
    # 放行：deferred 同步补跑不堵死；三线程完成并归还槽位。
    release.set()
    assert f4.join() == _OK_SUMMARY
    for f in flights:
        assert f.join() == _OK_SUMMARY


def test_flight_abandoned_cancel_prevents_unstarted_run(monkeypatch):
    """abandoned + 未起跑 future cancel → 任务不执行、槽位归还（僵尸上限 = 池容量）。"""
    calls: list = []

    def counting(*a, **k):
        calls.append(1)
        return dict(_OK_SUMMARY)

    monkeypatch.setattr(turn, "rule_match_summary", counting)
    for _ in range(turn._RAG_MAX_CONCURRENT - 1):
        turn._RAG_SEMAPHORE.acquire()  # 只留 1 个槽
    flight = _flight()
    flight.start()
    assert flight._future is not None and not flight._future.done()
    flight.cancel()  # 未起跑：cancel 应成功并归还槽位
    assert flight.abandoned
    assert turn._RAG_SEMAPHORE.acquire(blocking=False), "cancel 未起跑任务后槽位必须归还"
    turn._RAG_SEMAPHORE.release()
    assert flight.done(), "cancelled future 视为已结束（不会再跑）——发射闸另有 ¬abandoned 挡"
    assert flight.join() is None, "abandoned 未起跑 → join 直接返回 None（不补跑）"
    assert calls == []


def test_flight_abandoned_running_thread_runs_to_completion(monkeypatch):
    """abandoned 时线程已在跑：不可取消，等它跑完（僵尸被准入信号量封顶，不堵死）。"""
    entered = threading.Event()
    release = threading.Event()

    def slow(*a, **k):
        entered.set()
        assert release.wait(timeout=15)
        return dict(_OK_SUMMARY)

    monkeypatch.setattr(turn, "rule_match_summary", slow)
    flight = _flight()
    flight.start()
    assert entered.wait(timeout=15)
    flight.cancel()
    assert flight.abandoned
    release.set()
    assert flight.join() == _OK_SUMMARY  # 已起跑的线程跑完（结果不再用于发射）


def test_flight_abandoned_deferred_is_discarded():
    """abandoned 的 deferred 直接弃（不补跑）——设计约定。"""
    for _ in range(turn._RAG_MAX_CONCURRENT):
        turn._RAG_SEMAPHORE.acquire()
    flight = _flight()
    flight.start()
    assert flight._deferred
    flight.cancel()
    assert flight.abandoned
    assert flight.join() is None, "abandoned + deferred：join 直接弃（不补跑）"


def test_warmup_then_flight_never_touches_env(monkeypatch):
    """预热闭合（r3）：主线程先 ensure vector/env 初始化；flight 线程全程
    **零 load_llm_config**（workflow 两处 llm_available=False 短路生效）且不再执行
    `_setup_determinism`（写 os.environ 的初始化只在预热期主线程完成）。"""
    from dataset_recommender.llm import llm_client
    from dataset_recommender.retrieval import vector_recall as _vr

    llm_calls: list = []
    real_llm = llm_client.load_llm_config

    def spy_llm(*a, **k):
        llm_calls.append(1)
        return real_llm(*a, **k)

    monkeypatch.setattr(llm_client, "load_llm_config", spy_llm)
    det_threads: list = []
    real_det = _vr._setup_determinism

    def spy_det():
        det_threads.append(threading.current_thread().name)
        return real_det()

    monkeypatch.setattr(_vr, "_setup_determinism", spy_det)
    turn._warmup_rag_environment(None)
    assert det_threads and det_threads[0] == threading.main_thread().name, \
        "_setup_determinism 必须在主线程（预热期）完成"
    # 真实 pre-loop 管线（llm_available=False）：飞行中不应有任何 env 读取。
    flight = turn._RagFlight("human blood", search_params=_SP)
    flight.start()
    flight.join()
    assert flight.summary is not None
    assert llm_calls == [], "flight 线程全程零 load_llm_config（workflow 短路必须生效）"


def test_flight_run_error_normalizes_to_error_summary(monkeypatch):
    """_run 结构性防御：规则匹配抛异常 → status="error" 形状（fail-open 双保险）。"""
    def boom(*a, **k):
        raise RuntimeError("检索管线意外故障")

    monkeypatch.setattr(turn, "rule_match_summary", boom)
    flight = _flight()
    flight.start()
    summary = flight.join()
    assert summary is not None and summary["status"] == "error"
    assert summary["total"] == 0
    assert flight.meta is None
    assert not flight.has_hits


def test_flight_has_hits_matches_gate_semantics():
    """has_hits = status==results ∧ total>0（与既有机械闸同口径，design 设计约定）。"""
    flight = _flight()
    flight.meta = type("M", (), {"resolution_status": "results", "result_total": 3})()
    assert flight.has_hits
    flight.meta = type("M", (), {"resolution_status": "no_match", "result_total": 0})()
    assert not flight.has_hits
    flight.meta = type("M", (), {"resolution_status": "abstained", "result_total": 5})()
    assert not flight.has_hits
    flight.meta = None
    assert not flight.has_hits


def test_flight_ensure_payload_built_once(monkeypatch):
    """recommend_payload 建一次存 flight（发射与批次组卷不重跑）。"""
    calls: list = []

    def fake_payload(meta):
        calls.append(1)
        return {"ok": True, "result_total": int(meta.result_total)}

    import dataset_recommender.app.recommend_rows as rr
    monkeypatch.setattr(rr, "recommend_payload", fake_payload)
    flight = _flight()
    flight.meta = type("M", (), {"resolution_status": "results", "result_total": 3})()
    p1 = flight.ensure_payload()
    p2 = flight.ensure_payload()
    assert p1 is p2 and len(calls) == 1
    assert p1["result_total"] == 3


def test_verdict_hook_action_cancels_lazy_search_revives(monkeypatch):
    """turn 级 hook 语义（agent_exec 只回调）：action → cancel；marker 误伤（flight None
    且有 marker）→ lazy 补起重新入池。"""
    made: list = []

    class _SpyFlight(turn._RagFlight):
        def __init__(self, *a, **k):
            made.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(turn, "_RagFlight", _SpyFlight)
    holder = {"flight": None, "markers": ["打包"], "text": "x",
              "sources": None, "search_params": _SP}
    hook = turn._make_route_verdict_hook(holder)
    hook("search")  # 标记误伤 → lazy 补起
    assert holder["flight"] is not None and made == [1]
    assert holder["flight"]._started, "lazy 补起的 flight 已起跑（重新入池）"
    holder["flight"].join()
    hook("action")  # 已存在 flight → abandon
    assert holder["flight"].abandoned
