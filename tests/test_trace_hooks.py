# -*- coding: utf-8 -*-
"""可追溯性挂钩集成的端到端钉：

- **ON**：一轮真实 turn（FakeModel 替身走完整 agent 图：understand→validate→execute→
  decide→narrate）→ 五类以上事件、seq 连续、route_decision 并入 understand 原始投票、
  可经 `export_turn` 导出单 JSON；
- **OFF**：`AGENT_TRACE=off` → 零文件产出、响应无 trace_turn_id（行为逐位不变）；
- **fail-soft**：落盘全程抛异常 / 载荷构造抛异常 → turn 照常返回（观测设施绝不掀翻主流程）；
- **rollback 端到端**：写动词（search_online 替身真写文件）→ state_snapshot 留锚 →
  rollback dry-run 零副作用 → apply 移回收站 → 复跑幂等 → 无 preimage 改/删 fail-closed 拒动。

全离线：项目根重定向 tmp_path（trace/快照/回收站/账本全落 tmp），LOOP_TOOLS 用替身，
绝不碰真实库、绝不联网。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：挂钩集成测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import action_plan as _ap  # noqa: E402
from dataset_recommender.agent import agent_exec, turn  # noqa: E402
from dataset_recommender.agent.trace import (  # noqa: E402
    TraceRecorder, bind_recorder, snapshot_store, trace_root)
from dataset_recommender.agent.trace import events as ev  # noqa: E402
from dataset_recommender.agent.trace import export as trace_export  # noqa: E402
from dataset_recommender.agent.trace import recorder as trace_recorder  # noqa: E402
from dataset_recommender.agent.trace import rollback as trace_rollback  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-trace-hooks")

# conftest 把 _run_route_consensus 全局 stub 成 general；本文件多数测试正好依赖该 stub
# （脚本替身不预置投票应答），唯独 test_scoped_route_consensus_votes_fully_traced 测真投票，
# 在测试体内用 import 期存的真引用后执行 setattr 恢复（后执行生效）。
_REAL_RUN_ROUTE_CONSENSUS = agent_exec._run_route_consensus

UTTERANCE = "检查ArrayExpress是否有更新"

CHECK_OK = {"checked_at": "2026-08-17T00:00:00+08:00",
            "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                         "local_count": 10, "online_recent": 10, "new_count": 0,
                         "new_candidates": []}],
            "hint_zh": ""}

SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260817_curate_arrayexpress.json", "warnings": []}


class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（用尽后 pop 抛 IndexError——
    各节点按「LLM 缺席」fail-safe 处理）。"""

    def __init__(self, *answers):
        self.answers = list(answers)

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[
        {"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


@pytest.fixture
def _tmp_root(monkeypatch, tmp_path):
    """项目根重定向 tmp——trace/快照/回收站/账本全部落 tmp，绝不碰真实库。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    return tmp_path


def _install_check_tool(monkeypatch):
    def run(slots, root):
        return dict(CHECK_OK)
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {"run": run, "label_zh": "检查来源更新",
                                 "card_kind": "check_updates", "readonly": True},
    })


def _drive_turn(monkeypatch, tmp_path):
    """一轮真实 turn：understand 提 check_updates → 真跑 → decide 一次给两个调用
    （finish + 尾随只读——batch_emission 现场）→ narrate。返回 route_turn 的结果。"""
    _install_check_tool(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=UTTERANCE, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "检查来源更新：已做（第1步），没有新增。"},
             "id": "t2"},
            {"name": "curate_db_status", "args": {"quoted": UTTERANCE}, "id": "t3"},
        ]),
        AIMessage(content="ArrayExpress 没有更新。"),
    )
    monkeypatch.setattr(agent_exec, "_build_chat_model", lambda config: model)
    return turn.route_turn(UTTERANCE, config=CFG)


def _read_events(root, result):
    path = trace_root(root) / "anonymous" / f"{result['trace_turn_id']}.jsonl"
    assert path.is_file(), "trace ON：turn 日志必须落盘"
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------- ON：五类以上 + seq 连续 + 导出

def test_turn_emits_five_plus_kinds_continuous_seq_and_exports(monkeypatch, _tmp_root):
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert result.get("trace_turn_id"), "trace ON：additive 回显 turn id（报障给号）"

    events = _read_events(_tmp_root, result)
    kinds = {e["kind"] for e in events}
    assert {"route_decision", "llm_call", "tool_call", "batch_emission",
            "finish_reason"} <= kinds, f"五类事件齐全，实际：{sorted(kinds)}"
    # seq 从 0 连续（DSH 连续契约的读取侧校验）
    assert [e["seq"] for e in events] == list(range(len(events)))

    # llm_call：understand/decide 各一次（digest + 档位 + 延迟），不记全文
    llm_nodes = [e["payload"]["node"] for e in events if e["kind"] == "llm_call"]
    assert "understand" in llm_nodes and "decide" in llm_nodes
    for e in events:
        if e["kind"] == "llm_call":
            assert e["payload"]["prompt"]["sha256"] and e["payload"]["channel"]

    # tool_call：check_updates 真跑留痕（含预算计数现场）
    tool_evs = [e for e in events if e["kind"] == "tool_call"]
    assert [e["payload"]["verb"] for e in tool_evs] == ["curate.check_updates"]
    assert tool_evs[0]["payload"]["ok"] is True
    assert tool_evs[0]["payload"]["budgets"]["steps"] == 1

    # batch_emission：decide 一次给了 2 个调用，finish 收尾、尾随 1 个不消费
    batch_evs = [e for e in events if e["kind"] == "batch_emission"]
    assert len(batch_evs) == 1
    assert batch_evs[0]["payload"]["n_calls"] == 2
    assert batch_evs[0]["payload"]["adopted"] == 0
    assert batch_evs[0]["payload"]["dropped"] == 1

    # finish_reason：正常收尾
    fin = [e for e in events if e["kind"] == "finish_reason"]
    assert fin[-1]["payload"]["kind"] == "completed"
    assert fin[-1]["payload"]["steps"] == 1

    # route_decision：错误分析第一现场——understand 原始投票并入 votes
    rd = [e for e in events if e["kind"] == "route_decision"]
    assert len(rd) == 1
    payload = rd[0]["payload"]
    assert payload["route"] == "tool" and payload["via"] == "agent"
    assert payload["plan_verb"] == "curate.check_updates"
    assert payload["votes"]["understand"]["verb"] == "curate.check_updates"
    assert payload["votes"]["understand"]["confidence"] == "high"
    assert payload["votes"]["understand"]["slots"]["source"] == "ArrayExpress"

    # 导出：单 JSON、六键健康指标、与 JSONL 同一真源
    exported = trace_export.export_turn(_tmp_root, result["trace_turn_id"])
    assert exported["event_count"] == len(events)
    assert exported["seq_gaps"] == [] and exported["bad_tail_lines"] == 0
    assert exported["corrupt"] == [] and exported["unknown_kinds"] == []
    for kind in kinds:
        assert exported["kinds"][kind] >= 1


# ---------------------------------------------------------------- OFF：零文件产出、行为逐位不变

def test_trace_off_produces_zero_files_and_no_turn_id(monkeypatch, _tmp_root):
    monkeypatch.setenv("AGENT_TRACE", "off")
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert "trace_turn_id" not in result, "OFF：additive 键不出现（响应逐位不变）"
    assert not trace_root(_tmp_root).exists(), "OFF：零文件产出"


# ---------------------------------------------------------------- fail-soft：emit 炸不影响 turn

def test_emit_failure_never_breaks_the_turn(monkeypatch, _tmp_root):
    def boom(self, kind, payload):
        raise RuntimeError("磁盘满了（模拟）")
    monkeypatch.setattr(trace_recorder.TraceRecorder, "append", boom)
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool" and result["via"] == "agent"
    assert result["plan"]["verb"] == "curate.check_updates"


def test_payload_constructor_failure_never_breaks_the_turn(monkeypatch, _tmp_root):
    """载荷构造本身抛异常同样 fail-soft（warn-once + 丢弃）——全路径不掀翻主流程。"""
    def boom(**kwargs):
        raise ValueError("坏载荷（模拟）")
    monkeypatch.setattr(ev, "llm_call_payload", boom)
    result = _drive_turn(monkeypatch, _tmp_root)
    assert result["route"] == "tool"
    events = _read_events(_tmp_root, result)
    kinds = {e["kind"] for e in events}
    assert "llm_call" not in kinds, "构造失败的 llm_call 如实丢弃"
    assert {"route_decision", "tool_call", "finish_reason"} <= kinds, "其余事件照常落盘"


# -------------------------------------------------- 写动词快照 + rollback 端到端（幂等 + fail-closed）

def _runtime():
    return SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))


def test_write_verb_snapshot_and_rollback_roundtrip(monkeypatch, _tmp_root):
    ext = _tmp_root / "database" / "external"
    ext.mkdir(parents=True)
    fname = SEARCH_OK["filename"]

    def run(slots, root):
        (ext / fname).write_text(json.dumps({"rows": ["x"]}, ensure_ascii=False),
                                 encoding="utf-8")
        return dict(SEARCH_OK)

    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {"run": run, "label_zh": "联网搜索入库",
                                 "card_kind": "search_online", "readonly": False},
    })
    rec = TraceRecorder(_tmp_root, "test", "rb1", enabled=True)
    state = {"utterance": "联网搜人类肺数据入库",
             "plan": {"verb": "curate.search_online",
                      "slots": {"quoted": "联网搜人类肺数据入库", "source": "ArrayExpress",
                                "keywords": "人类肺", "species": "人类"}},
             "loop_plan": None, "steps": [], "pending_reask_write": False}
    with bind_recorder(rec):
        out = agent_exec.execute(state, runtime=_runtime())
    step = out["steps"][0]
    assert step["ok"] is True and (ext / fname).is_file(), "替身 search_online 真写文件"

    # state_snapshot 事件留锚：created 含新文件；tool_call 同场（写动词 readonly=False）
    path = trace_root(_tmp_root) / "test" / "rb1.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()]
    snap = next(e for e in events if e["kind"] == "state_snapshot")
    assert snap["payload"]["verb"] == "curate.search_online"
    assert [c["name"] for c in snap["payload"]["created"]] == [fname]
    tool = next(e for e in events if e["kind"] == "tool_call")
    assert tool["payload"]["readonly"] is False
    assert tool["payload"]["budgets"]["write_steps"] == 1
    sid = snap["payload"]["snapshot_id"]

    # dry-run：只出计划，零副作用
    plan = trace_rollback.plan_rollback(_tmp_root, sid)
    assert plan["recycle_created"] == [fname] and plan["restore_bytes"] == []
    assert (ext / fname).is_file(), "dry-run 绝不动文件"

    # apply：新文件移入回收站（移动非删除 + manifest 行）
    res = trace_rollback.apply_rollback(_tmp_root, sid)
    assert [e["name"] for e in res["applied"]["recycled"]] == [fname]
    assert not (ext / fname).exists()
    recycle = _tmp_root / ".userdata" / "recycle"
    assert recycle.is_dir() and any(p.name.endswith(fname) for p in recycle.iterdir())

    # 幂等：复跑如实 skipped，不重复动手
    res2 = trace_rollback.apply_rollback(_tmp_root, sid)
    assert res2["applied"]["recycled"] == []
    assert any(e["name"] == fname for e in res2["skipped"])

    # fail-closed：modified 且无 preimage → 拒动、如实报、文件原样
    victim = ext / "keep_existing.json"
    victim.write_text("v1", encoding="utf-8")
    store = snapshot_store(_tmp_root)
    sid2 = store.capture("curate.sync_updates", preimage_paths=[])
    victim.write_text("v2", encoding="utf-8")
    store.finalize(sid2)
    plan2 = trace_rollback.plan_rollback(_tmp_root, sid2)
    assert plan2["restore_bytes"] == []
    assert [e["name"] for e in plan2["unrestorable"]] == ["keep_existing.json"]
    res3 = trace_rollback.apply_rollback(_tmp_root, sid2)
    assert res3["applied"]["restored"] == []
    assert victim.read_text(encoding="utf-8") == "v2", "无 preimage 拒动——宁可少退不毁数据"


# ---------------------------------------------------------------- webapp additive 回显

def test_utterance_response_body_echoes_trace_turn_id_only_when_present():
    from dataset_recommender.app import webapp
    base = {"route": "search", "query": "x", "plan": None, "echo_zh": "",
            "retrieval": None, "via": "rule_direct", "needs_agent": False,
            "suggestions": [], "result_payload": None, "preliminary_final": False}
    body_off = webapp._utterance_response_body(dict(base))
    assert "trace_turn_id" not in body_off, "缺席（OFF）→ 不加键，响应逐位不变"
    body_on = webapp._utterance_response_body(dict(base, trace_turn_id="abc123"))
    assert body_on["trace_turn_id"] == "abc123"


# ------------------------------------------- route_consensus 原始投票留痕

def test_scoped_route_consensus_votes_fully_traced(monkeypatch, tmp_path):
    """分流共识的**全部原始投票**留痕（route_decision.votes.route_consensus + 每票
    一条 llm_call）——scoped 路由常驻，无需开关装配。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    monkeypatch.setattr(agent_exec, "_run_route_consensus", _REAL_RUN_ROUTE_CONSENSUS)  # 测真投票
    _install_check_tool(monkeypatch)
    vote = AIMessage(content=json.dumps(
        {"route": "action", "reason": "检查更新是管护动作"}, ensure_ascii=False))
    model = _FakeModel(
        vote, vote,  # route_consensus 并行两票一致 → action（不加投第三票）
        _tool_call("curate.check_updates", quoted=UTTERANCE, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        AIMessage(content="", tool_calls=[
            {"name": "finish",
             "args": {"completion_report": "检查来源更新：已做（第1步）。"}, "id": "t2"}]),
        AIMessage(content="ArrayExpress 没有更新。"),
    )
    monkeypatch.setattr(agent_exec, "_build_chat_model", lambda config: model)
    result = turn.route_turn(UTTERANCE, config=CFG)
    assert result["route"] == "tool"
    events = _read_events(tmp_path, result)
    rd = next(e for e in events if e["kind"] == "route_decision")
    rc = rd["payload"]["votes"]["route_consensus"]
    assert rc["route"] == "action"
    assert len(rc["votes"]) == 2, "两票一致即定，无第三票"
    assert {v["temperature"] for v in rc["votes"]} == {0.0, 0.8}
    assert all(v["ok"] and v["route"] == "action" and v["raw"] for v in rc["votes"])
    assert rd["payload"]["votes"]["understand"]["verb"] == "curate.check_updates"
    # 每票一条 llm_call（票在并行线程够不到 contextvar，由节点代发）
    rc_calls = [e for e in events
                if e["kind"] == "llm_call" and e["payload"]["node"] == "route_consensus"]
    assert len(rc_calls) == 2
    assert all(e["payload"]["channel"] == "consensus_vote" for e in rc_calls)
