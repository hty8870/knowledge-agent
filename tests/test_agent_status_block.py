# -*- coding: utf-8 -*-
"""decide 状态栏专项钉。

字段纪律：只放真实机械状态——步数/写步/失败/联网暂停/已搜主题/finish_vetoes/
reask_writes/清单未决；虚构的「统一重问预算」与不可达的「8/8 到顶」提示不进。
操作策略行只在「剩余=1」这个**可达**状态出现。
（MAX_STEPS 3→6、新增写步数字段，断言同步。）
（MAX_STEPS 6→8 + 到顶结算闸 + 批内熔断 + sync 点名半闸 +
 search_online 的 network_error 失败不占写步预算——断言同步。）
"""
from __future__ import annotations

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：decide 状态栏测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-status-test")


def _step(verb="curate.check_updates", ok=True, error_code=None, result=None):
    s = {"verb": verb, "verb_zh": verb, "ok": ok}
    if error_code:
        s["error_code"] = error_code
    if result is not None:
        s["result"] = result
    return s


# ---------- 纯函数 ----------
def test_empty_state_all_zero_no_hint():
    block = agent_exec._agent_status_block_zh({}, steps=[], moratorium=False)
    assert "执行状态（系统机械账本，实时）" in block
    assert "已执行步数 0（最多 8 步）" in block
    assert "剩余步数 8" in block
    assert "写步数 0（最多 2 次写）" in block
    assert "失败步数 0" in block and "联网暂停 否" in block
    assert "finish 核销被拒 0" in block and "重问放行写步 0" in block
    assert "最后一次执行机会" not in block      # 剩余=8 不出操作策略行
    assert "清单未决" not in block              # 无清单不出该字段


def test_seven_steps_one_failed_hint_line_reachable():
    steps = [_step(ok=True), _step(ok=True), _step(ok=True), _step(ok=True),
             _step(ok=True), _step(ok=True),
             _step(ok=False, error_code="network_error")]
    block = agent_exec._agent_status_block_zh({}, steps=steps, moratorium=False)
    assert "已执行步数 7（最多 8 步）" in block
    assert "剩余步数 1" in block and "失败步数 1" in block
    assert "剩余步数 = 1：下一步执行后就必须调用 finish" in block   # 可达且真实


def test_write_steps_and_ban_verbs_fields():
    """写步数现算（成败都计）；二连败禁提字段仅 ban_verbs 非空时出现。"""
    steps = [_step(verb="curate.search_online", ok=True),
             _step(verb="curate.sync_updates", ok=False, error_code="bad_param")]
    block = agent_exec._agent_status_block_zh({}, steps=steps, moratorium=False)
    assert "写步数 2（最多 2 次写）" in block
    assert "二连败禁提" not in block          # 一胜一败不触发禁提
    banned = agent_exec._failed_tool_ban([
        _step(verb="curate.check_updates", ok=False, error_code="bad_param"),
        _step(verb="curate.search_online", ok=False, error_code="shape_invalid")])
    assert banned == frozenset({"curate.check_updates", "curate.search_online"})
    block2 = agent_exec._agent_status_block_zh({}, steps=[], moratorium=False, ban_verbs=banned)
    assert "二连败禁提" in block2
    # 联网二连败不走逐动作禁（走整族暂停）
    assert agent_exec._failed_tool_ban([
        _step(verb="curate.search_online", ok=False, error_code="network_error"),
        _step(verb="curate.search_online", ok=False, error_code="network_error")]) == frozenset()


def test_write_budget_exempts_search_network_error():
    """search_online 的 network_error 失败可证零副作用
    （plan 取数阶段抛异常、apply 未跑）→ 不占写步预算；其余失败码（bad_result_shape 等
    无法自证零写入）与 sync 的任何失败照旧计入。"""
    assert agent_exec._write_steps_used([]) == 0
    assert agent_exec._write_steps_used([
        _step(verb="curate.search_online", ok=False, error_code="network_error")]) == 0
    # 非网络失败码不豁免
    assert agent_exec._write_steps_used([
        _step(verb="curate.search_online", ok=False, error_code="bad_result_shape")]) == 1
    # sync 的 network_error 不豁免（契约上 sync 不产生该码；即便出现也无法自证零写入）
    assert agent_exec._write_steps_used([
        _step(verb="curate.sync_updates", ok=False, error_code="network_error")]) == 1
    # 成功写步照计
    assert agent_exec._write_steps_used([
        _step(verb="curate.search_online", ok=True, result={"record_count": 3})]) == 1


def test_moratorium_and_searched_topics():
    steps = [
        _step(verb="curate.search_online", ok=True),
        _step(verb="curate.search_online", ok=True),
    ]
    block = agent_exec._agent_status_block_zh({}, steps=steps, moratorium=True)
    assert "联网暂停 是" in block
    assert "已搜主题 2" in block
    # 失败步不计入已搜主题
    steps2 = [_step(verb="curate.search_online", ok=False)]
    assert "已搜主题 0" in agent_exec._agent_status_block_zh({}, steps=steps2, moratorium=False)


def test_vetoes_and_reask_writes_counts():
    state = {"finish_vetoes": 2, "reask_writes": [{"verb": "curate.search_online", "step_no": 2}]}
    block = agent_exec._agent_status_block_zh(state, steps=[], moratorium=False)
    assert "finish 核销被拒 2" in block
    assert "重问放行写步 1" in block


def test_checklist_unsettled_count():
    checklist = [
        {"task_id": "t1", "text": "检查 ArrayExpress 更新", "expect_verb": "curate.check_updates",
         "anchor": "检查ArrayExpress"},
        {"task_id": "t2", "text": "告诉我库里多少条", "expect_verb": "curate.db_status",
         "anchor": "库里多少条"},
    ]
    steps = [_step(verb="curate.check_updates", ok=True,
                   result={"checked_at": "t", "sources": [
                       {"source": "arrayexpress", "mode": "online", "new_count": 0,
                        "new_candidates": []}], "hint_zh": ""})]
    block = agent_exec._agent_status_block_zh({"checklist": checklist}, steps=steps, moratorium=False)
    assert "清单未决 1" in block        # t1 已核销（零新增条件豁免/步骤命中），t2 db_status 未做


def test_checklist_item_states_block_renders_all_four_statuses():
    """清单逐项状态行进状态栏——
    done(第N步)/missing/exempt(零新增)/declined(表外婉拒) 四态代码现算。"""
    checklist = [
        {"task_id": "t1", "text": "检查 ArrayExpress 更新", "expect_verb": "curate.check_updates",
         "anchor": "检查ArrayExpress", "sources": ["ArrayExpress"]},
        {"task_id": "t2", "text": "告诉我库里多少条", "expect_verb": "curate.db_status",
         "anchor": "库里多少条", "sources": []},
        {"task_id": "t3", "text": "有新增就搜来入库", "expect_verb": "curate.search_online",
         "anchor": "有新增就搜来入库", "sources": []},
        {"task_id": "t4", "text": "打包下载", "expect_verb": "unsupported",
         "anchor": "打包下载", "sources": []},
    ]
    steps = [_step(verb="curate.check_updates", ok=True,
                   result={"checked_at": "t", "sources": [
                       {"source": "arrayexpress", "mode": "online", "new_count": 0,
                        "new_candidates": []}], "hint_zh": ""})]
    states = agent_exec._checklist_item_states(checklist, steps, "你要的「打包下载」这一步没有做")
    assert [s["status"] for s in states] == ["done", "missing", "exempt", "declined"]
    assert states[0]["step_no"] == 1
    block = agent_exec._agent_status_block_zh(
        {"checklist": checklist, "declined_zh": "你要的「打包下载」这一步没有做"},
        steps=steps, moratorium=False)
    assert "清单未决 1" in block
    assert "[t1] 已做（第1步）" in block and "[t2] 未做" in block
    assert "[t3] 豁免（零新增）" in block and "[t4] 已婉拒（表外）" in block


# ---------- decide 集成：prompt 尾部恒注入 ----------
class _FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        item = self.answers.pop(0)
        return item


def _tool_call(verb, **args):
    return AIMessage(content="",
                     tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


def _raw_tool_call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "t1"}])


def test_decide_prompt_contains_status_block(monkeypatch):
    stub = dict(agent_exec.LOOP_TOOLS["curate.check_updates"])
    stub["run"] = lambda slots, root: {"checked_at": "t", "sources": [
        {"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
         "new_count": 0, "new_candidates": []}], "hint_zh": ""}
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.check_updates", stub)

    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新", source="ArrayExpress"),
        _raw_tool_call("finish", {"completion_report": "检查更新：已做（第1步）。"}),
        AIMessage(content="已检查完成。"),
    )
    agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model)
    assert len(model.invocations) >= 2
    decide_messages = model.invocations[1]
    human_texts = [getattr(m, "content", "") for m in decide_messages]
    joined = "\n".join(str(t) for t in human_texts)
    assert "执行状态（系统机械账本，实时）" in joined
    assert "已执行步数 1（最多 8 步）" in joined    # decide 时已完成 1 步
    # 动态块在 prompt 尾部（用户原话与已完成步骤之后）
    assert joined.rfind("执行状态") > joined.rfind("已完成步骤")


def test_understand_prompt_has_no_status_block(monkeypatch):
    """understand 不注状态栏（首步无循环状态，注入只会重复——设计纪律的反向钉）。"""
    stub = dict(agent_exec.LOOP_TOOLS["curate.check_updates"])
    stub["run"] = lambda slots, root: {"checked_at": "t", "sources": [], "hint_zh": ""}
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.check_updates", stub)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新", source="ArrayExpress"),
        _raw_tool_call("finish", {"completion_report": "检查更新：已做（第1步）。"}),
        AIMessage(content="已检查完成。"),
    )
    agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model)
    understand_messages = model.invocations[0]
    joined = "\n".join(str(getattr(m, "content", "")) for m in understand_messages)
    assert "执行状态（系统机械账本" not in joined


def test_intent_checklist_section_in_status_block():
    """「不少于我」（2026-09-01）：state 带 intent_checklist 时状态栏逐项现算——
    未决计数 + 逐项状态行 + 「全部核销后才许 finish」规则行；缺省零变化。"""
    cl = [
        {"verb": "cite.export", "verb_zh": "导出引文", "quoted": "导出成引文", "plane": "inloop"},
        {"verb": "pack.download", "verb_zh": "打包下载", "quoted": "打包下载", "plane": "frontend"},
    ]
    block = agent_exec._agent_status_block_zh({"intent_checklist": cl},
                                              steps=[_step("cite.export")], moratorium=False)
    assert "句内动作未决 0" in block
    assert "[cite.export] 已做（第1步）" in block
    assert "[pack.download] 已交前端（环内别再做）" in block
    assert "全部核销后才许 finish" in block
    block2 = agent_exec._agent_status_block_zh({"intent_checklist": cl},
                                               steps=[], moratorium=False)
    assert "句内动作未决 1" in block2 and "[cite.export] 未做" in block2
    # 缺省（无 intent_checklist）→ 无该段，旧输出逐位不变
    block3 = agent_exec._agent_status_block_zh({}, steps=[], moratorium=False)
    assert "句内动作" not in block3
