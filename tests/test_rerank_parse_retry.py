"""解析失败的错误回灌专项钉。

纪律（验证）：
- 只重试「非空输出且机械结构无效」；空输出 / 异常 / partial（缺号排列）绝不重试；
- 「10x」型字母粘连不算数字（standalone 判定）；
- 重试结局是 trace 的附加字段 parse_retry，不覆盖既有 reason；
- 仍败照旧走既有 fail-open/fail-closed，行为与历史一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval import rerank  # noqa: E402


def _rec(name: str) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="human", tissue="breast", disease="breast cancer",
        chemistry="", count="", unit="", has_raw_data=True, url="", source_file="x.json",
        description=f"desc of {name}", raw={},
    )


def _cands(n: int) -> list[RetrievedCandidate]:
    return [RetrievedCandidate(record=_rec(f"D{i}"), score=float(n - i)) for i in range(n)]


def _names(cands):
    return [c.record.dataset_name for c in cands]


class _ScriptedCaller:
    """按脚本逐次返回的 llm_call 替身；记录调用次数与最后一问。"""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def __call__(self, prompt):
        self.calls.append(prompt)
        return self.outputs.pop(0) if self.outputs else None


# ---------- validator 单元 ----------
def test_grade_order_text_levels():
    assert rerank._grade_order_text("[3, 1, 2]", 3) == "valid"
    assert rerank._grade_order_text("排序是 2 1", 3) == "partial"   # standalone 数字可消费
    assert rerank._grade_order_text("我选10x", 12) == "invalid"     # 字母粘连不算数字
    assert rerank._grade_order_text("（没有数字）", 3) == "invalid"
    assert rerank._grade_order_text("", 3) == "invalid"


# ---------- rerank_candidates ----------
def test_rerank_retry_recovers_on_invalid_first():
    caller = _ScriptedCaller(["（一段没有任何数字的散文）", "[3, 1, 2]"])
    trace = {}
    out = rerank.rerank_candidates("q", _cands(3), backend="llm", llm_call=caller, trace=trace)
    assert len(caller.calls) == 2                      # 回灌了一次
    assert "机械结构无效" in caller.calls[1]           # 反馈里带失败原因
    assert _names(out)[0] == "D2"                      # 采用了重试后的 [3,1,2]
    assert trace.get("parse_retry") == "recovered"
    assert trace.get("status") == "used"


def test_rerank_retry_failed_keeps_legacy_fallback():
    caller = _ScriptedCaller(["无数字散文", "还是无数字"])
    trace = {}
    out = rerank.rerank_candidates("q", _cands(3), backend="llm", llm_call=caller, trace=trace)
    assert len(caller.calls) == 2
    assert _names(out) == ["D0", "D1", "D2"]           # 回退原序（与历史一致）
    assert trace.get("status") == "fallback" and trace.get("reason") == "invalid_order"
    assert trace.get("parse_retry") == "failed"        # 附加字段，不覆盖 reason


def test_rerank_glued_number_treated_as_invalid_not_candidate():
    """验证 点名的坑：「我选3x」不许被当成候选 3——判 invalid 触发回灌而不是静默错排。"""
    caller = _ScriptedCaller(["我选3x", "[2, 1, 3]"])
    out = rerank.rerank_candidates("q", _cands(3), backend="llm", llm_call=caller, trace={})
    assert len(caller.calls) == 2
    assert _names(out)[0] == "D1"


def test_rerank_partial_order_no_retry():
    caller = _ScriptedCaller(["2, 1"])                 # 缺号=partial：交集守卫/补回遗漏的本职
    out = rerank.rerank_candidates("q", _cands(3), backend="llm", llm_call=caller, trace={})
    assert len(caller.calls) == 1
    assert _names(out) == ["D1", "D0", "D2"]           # 既有宽容链：缺的按原序补回


def test_rerank_empty_output_no_retry():
    caller = _ScriptedCaller([None])
    trace = {}
    out = rerank.rerank_candidates("q", _cands(3), backend="llm", llm_call=caller, trace=trace)
    assert len(caller.calls) == 1                      # 空输出不重试
    assert trace.get("reason") == "llm_call_failed"
    assert "parse_retry" not in trace


# ---------- _LAST_PARSE_RETRY 并发隔离 + 无跨请求残留 ----------
def test_parse_retry_outcome_isolated_under_concurrency():
    """模块级 dict 时代，两线程交错写同一 channel 会互踩（A pop 到 B 的结局）。
    ContextVar 后两线程各拿各的结局。互踩窗口照 test_f5 的 _LAST_LLM_ERROR 钉用 barrier 强制。"""
    import threading

    barrier = threading.Barrier(2)
    outcomes = {}

    def run(name, second):
        def caller(_prompt):
            barrier.wait(timeout=10)   # 两线程同处「已判 invalid、待写结局」窗口
            return second

        rerank._maybe_retry_parse("（散文）", "p", caller=caller,
                                  validate=lambda t: "valid" if t == "[1, 2]" else "invalid",
                                  contract_zh="x", channel="rerank")
        outcomes[name] = rerank._parse_retry_take("rerank")

    t1 = threading.Thread(target=run, args=("a", "[1, 2]"))     # 二答 valid → recovered
    t2 = threading.Thread(target=run, args=("b", "（还是散文）"))  # 二答仍 invalid → failed
    t1.start(); t2.start(); t1.join(); t2.join()
    assert outcomes == {"a": "recovered", "b": "failed"}


def test_parse_retry_residue_does_not_cross_contexts():
    """重排重试状态不得跨请求残留。"""
    import contextvars

    rerank._maybe_retry_parse("（散文）", "p", caller=lambda _p: "[1, 2]",
                              validate=lambda t: "valid" if t == "[1, 2]" else "invalid",
                              contract_zh="x", channel="rerank")
    assert rerank._parse_retry_take("rerank") == "recovered"   # 本上下文里读得到
    fresh = contextvars.Context()                                   # 新上下文 = 新请求
    assert fresh.run(rerank._parse_retry_take, "rerank") is None


# ---------- 宽 except 回退必须留下异常本体 ----------
def test_rerank_exception_leaves_stderr_trace(capsys):
    """行为不变（回退原序、reason=llm_call_failed），但异常 repr 必须留一行 stderr；
    同一异常类型只提示一次。"""
    rerank._WARNED.clear()

    def boom(_prompt):
        raise RuntimeError("provider exploded")

    trace = {}
    out = rerank.rerank_candidates("q", _cands(2), backend="llm", llm_call=boom, trace=trace)
    assert _names(out) == ["D0", "D1"]                 # 回退原序
    assert trace.get("reason") == "llm_call_failed"
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "provider exploded" in err

    rerank.rerank_candidates("q", _cands(2), backend="llm", llm_call=boom)
    assert capsys.readouterr().err == ""               # 同型异常不重复刷屏
