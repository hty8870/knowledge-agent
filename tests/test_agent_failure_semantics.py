# -*- coding: utf-8 -*-
"""失败语义二分 + 连续失败处置二分（2026-08-06 批；蓝本 pydantic-ai ModelRetry/ToolFailed 二分、
12-factor-agents factor 9「错误进 context + 确定性熔断」、OpenHands stuck detector）的确定性门。
联网二连败不硬停、改**联网暂停**（moratorium）。

钉四条：
  1. **终态失败死路拦截**：工具以终态码（source_not_registered）失败 → (verb, 目标源) 记死路账；
     decide 再提议同 verb 同目标源（哪怕换了关键词）→ 机械停环 + declined 如实点名，不消耗
     LLM 往返去重试注定的死路。换目标源不在拦截范围（那是另一条路）。
  2. **可纠正码不记死路**：bad_param / no_candidates / network_error 是 ModelRetry 语义——
     换参重试的价值真实存在，机械层不得拦截。
  3. **连续失败处置二分**：非网络码二连败
     （bad_param 等）**不再硬停**——decide 照常调 LLM，prompt 注入「失败工具禁提」段，
     刚失败的动作提议被机械拒绝（note 如实点名），链上剩余独立事项（db_status）照常放行；
     旧口径「任意两步失败即停」会把两个不同动作的独立失败误当原地空转、连坐剩余事项。
     联网二连败（network_error × 2）维持 **联网暂停**（整族禁提联网工具）；
     一胜一败两者都不触发（失败后的成功路径不受误伤）。
  3b. **写步预算闸**：写步（search_online/sync_updates，成败都计）
     用满 MAX_WRITE_STEPS 次后写工具提议被机械拒绝（declined 如实点名「预算已用完、
     还要入库可以再说一次」），只读工具照常放行——总步数放宽不放大单请求写入上界。
  4. **失败步同指纹重试的精确豁免面**（2026-08-08 放行、收窄）：
     `_is_duplicate_step` 的比对集 = 成功步 + 非 network_error 失败步——network_error 是
     唯一真·可重试码（失败步什么都没做成，同指纹重试放行）；bad_result_shape 是确定性
     失败，同指纹重试必败照样拦截（不白烧步数）；bad_param/no_candidates 换参重试指纹
     天然不同，不受影响。
  5. **「先新后旧」注入段**（重试挤占 MAX_STEPS 预算）：存在失败步时
     decide 双壳 prompt 注入「先把没做过的新事做完，重试最多一次放在最后」——纯劝导无闸。
全离线：fake chat_model + monkeypatch LOOP_TOOLS（与 test_agent_exec_loop.py 同一 harness）。
"""
import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：多步循环测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-fail-semantics-test")


class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（用尽后 pop 抛 IndexError——
    decide/narrate 都按「LLM 缺席」fail-safe 处理）。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    return tmp_path


def _failing_tool(code, hint):
    def run(slots, root):
        from dataset_recommender.corpus import corpus_curation as cc
        raise cc.CurateError(code, hint)
    return run


# ---------------------------------------------------------------- 1. 终态失败死路拦截

def test_terminal_failure_blocks_same_source_retry(monkeypatch):
    """source_not_registered 失败后，decide 换关键词再试同一来源 → 死路机械拦截；
    重入：拦截不等于整条完成——回灌重问一次（模型改 finish 收尾）
    汇报兜底仍如实点名没做的事。"""
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.search_online", {
        "run": _failing_tool("source_not_registered", "暂不支持联网搜索来源「10x」。"),
        "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False,
        "decide_zh": "测试替身",
    })
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜 10x 的肺数据", source="10x",
                   keywords="lung", confidence="high", reason="联网搜"),
        # decide：LLM 不知道死路，换关键词再试同一来源（槽位变了，重复步拦截管不到）
        AIMessage(content='{"verb": "curate.search_online", "quoted": "联网搜 10x 的肺数据",'
                          ' "source": "10x", "keywords": "肺"}'),
        # 重入：拒绝回灌后模型如实 finish（不再提死路）
        AIMessage(content='{"done": true}'),
        AIMessage(content="10x 这个来源本工具接不了，这次没有搜成。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "联网搜 10x 的肺数据", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert len(plan["steps"]) == 1, "死路拦截后不得真跑第二次"
    assert plan["steps"][0]["ok"] is False
    assert plan["steps"][0]["error_code"] == "source_not_registered"
    reask_text = "；".join(t.get("detail", "") for t in trace)
    assert "被系统拒绝" in reask_text and "回灌重问" in reask_text, "死路拒绝必须走 P0-1 回灌重问"
    report = plan.get("report_zh") or ""
    assert "接不了" in report or "没有搜成" in report or "没有做" in report, \
        f"兜底汇报必须如实点名没做的事：{report}"


def test_terminal_failure_does_not_block_other_source(monkeypatch):
    """死路账按 (verb, 目标源) 记：换来源重试是另一条路，机械层不得拦截。"""
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.search_online", {
        "run": _failing_tool("source_not_registered", "暂不支持联网搜索来源「10x」。"),
        "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False,
        "decide_zh": "测试替身",
    })
    state = {
        "utterance": "联网搜 10x 或 ArrayExpress 的肺数据",
        "steps": [{"verb": "curate.search_online", "verb_zh": "联网搜索入库",
                   "slots": {"source": "10x", "keywords": "lung"}, "ok": False,
                   "error": "暂不支持", "error_code": "source_not_registered",
                   "card_kind": "search_online", "readonly": False, "ms": 1}],
        "dead_ends": [{"verb": "curate.search_online", "code": "source_not_registered",
                       "source": "10x"}],
    }
    nxt, note, declined, _fb = agent_exec._parse_decide_answer(
        '{"verb": "curate.search_online", "quoted": "联网搜 10x 或 ArrayExpress 的肺数据",'
        ' "source": "ArrayExpress", "keywords": "肺"}', state)
    assert nxt is not None, f"换来源不该被死路账拦截：{note}"
    # 同来源才被拦截
    nxt2, note2, declined2, _fb2 = agent_exec._parse_decide_answer(
        '{"verb": "curate.search_online", "quoted": "联网搜 10x 或 ArrayExpress 的肺数据",'
        ' "source": "10x", "keywords": "肺"}', state)
    assert nxt2 is None and declined2, "同来源终态重试必须被机械拦截"


def test_retryable_codes_do_not_create_dead_ends():
    """bad_param / no_candidates / network_error 是 ModelRetry 语义：不进终态码表。"""
    assert "source_not_registered" in agent_exec._TERMINAL_STEP_CODES
    for retryable in ("bad_param", "no_candidates", "network_error"):
        assert retryable not in agent_exec._TERMINAL_STEP_CODES, retryable


# ---------------------------------------------------------------- 2. 连续失败处置二分（2026-08-08 再二分）

def _nonnetwork_failing_check_registry(monkeypatch):
    """失败禁提测试组的假注册表：check_updates 恒以 bad_param（非网络码）失败、db_status 返 0 条替身。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {
            "run": _failing_tool("bad_param", "测试：参数不合格"),
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True},
        "curate.db_status": {
            "run": lambda slots, root: {
                "generated_at": "t", "sources": [], "total_records": 0,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
    })


def test_nonnetwork_double_failure_bans_verb_but_keeps_independent_tasks(monkeypatch, _tmp_project_root):
    """评审主治：非网络码二连败 **不再硬停**——decide 照常调 LLM
    （prompt 带「失败工具禁提」注入段），独立的 db_status 事项照常放行并真跑。
    旧口径「任意两步失败即停」会把两个不同动作的独立失败误当原地空转、连坐剩余事项。"""
    _nonnetwork_failing_check_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        # decide#1（只有一败，未触发禁提）：换源再查（不同槽位，重复步拦截管不到）
        AIMessage(content='{"verb": "curate.check_updates", "quoted": "检查ENCODE有没有更新",'
                          ' "source": "ENCODE"}'),
        # decide#2（非网络二连败 → 禁提 check_updates）：提议离线 db_status → 放行
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        AIMessage(content="检查两次都没成；库里共 0 条。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新，检查ENCODE有没有更新，再告诉我库里多少条",
        has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert [(s["verb"], s["ok"]) for s in plan["steps"]] == [
        ("curate.check_updates", False), ("curate.check_updates", False),
        ("curate.db_status", True)], "非网络二连败只禁提失败动作，独立 db_status 必须照常执行"
    # decide#2 的 prompt 必须带失败禁提注入段（机械约束摆给 LLM 看）
    assert "失败工具禁提" in model.invocations[2][0].content
    assert "检查来源更新" in model.invocations[2][0].content
    trace_text = "；".join(t.get("detail", "") for t in trace)
    assert "防原地空转" not in trace_text, "非网络二连败不再走硬停"


def test_nonnetwork_double_failure_banned_verb_rejected(monkeypatch, _tmp_project_root):
    """禁提的机械兜底：非网络二连败后 decide 仍提议同一失败动作 → 裁决层机械拒绝
    （按 done 收尾 + 如实点名），不再真跑第三步。"""
    _nonnetwork_failing_check_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        AIMessage(content='{"verb": "curate.check_updates", "quoted": "检查ENCODE有没有更新",'
                          ' "source": "ENCODE"}'),
        # decide#2：不顾禁提再提同一动作（10x 源，重复步拦截管不到）→ 必须被机械拦下
        AIMessage(content='{"verb": "curate.check_updates", "quoted": "检查10x有没有更新",'
                          ' "source": "10x"}'),
        AIMessage(content="检查更新连续失败两次，这一步没有做。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新，检查ENCODE有没有更新，检查10x有没有更新",
        has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert len(plan["steps"]) == 2, "被禁提的动作绝不真跑第三步"
    assert all(s["ok"] is False for s in plan["steps"])
    trace_text = "；".join(t.get("detail", "") for t in trace)
    assert "不再" in trace_text and "失败" in trace_text


def _network_failing_search_registry(monkeypatch):
    """联网暂停测试组的假注册表：search 恒以 network_error 失败、db_status 返 0 条替身。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {
            "run": _failing_tool("network_error", "网络抖动，稍后重试"),
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False},
        "curate.db_status": {
            "run": lambda slots, root: {
                "generated_at": "t", "sources": [], "total_records": 0,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
    })


def test_moratorium_does_not_ban_snapshot_source_check():
    """：联网暂停只禁触网调用——离线快照源（CELLxGENE）的
    check_updates 只读本地快照，不连坐；在线源（ArrayExpress）的 check 仍被禁。"""
    # 单元面：_is_network_call 的判定矩阵
    assert agent_exec._is_network_call("curate.check_updates", "CELLxGENE Discover") is False
    assert agent_exec._is_network_call("curate.check_updates", "EBI Single Cell Expression Atlas") is False
    assert agent_exec._is_network_call("curate.check_updates", "HuBMAP") is False
    assert agent_exec._is_network_call("curate.check_updates", "Broad Single Cell Portal") is False
    assert agent_exec._is_network_call("curate.check_updates", "ArrayExpress") is True
    assert agent_exec._is_network_call("curate.check_updates", "ENCODE") is True
    assert agent_exec._is_network_call("curate.check_updates", "") is True     # 查全部=含在线源
    assert agent_exec._is_network_call("curate.search_online", "CELLxGENE Discover") is True
    assert agent_exec._is_network_call("curate.sync_updates", "HuBMAP") is True
    # 裁决面：联网二连败现场，快照源 check 提议照常放行
    steps = [{"verb": "curate.search_online", "ok": False, "error_code": "network_error"},
             {"verb": "curate.search_online", "ok": False, "error_code": "network_error"}]
    nxt, note, _d, _v = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "source": "CELLxGENE Discover",
         "quoted": "检查CELLxGENE有没有更新"},
        {"utterance": "检查CELLxGENE有没有更新", "steps": steps})
    assert nxt is not None, f"联网暂停不得连坐离线快照源检查：{note}"
    nxt2, note2, _d2, _v2 = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "source": "ArrayExpress",
         "quoted": "检查ArrayExpress有没有更新"},
        {"utterance": "检查ArrayExpress有没有更新", "steps": steps})
    assert nxt2 is None and "联网暂停中" in note2, "在线源检查在联网暂停中必须仍被禁"


def test_network_moratorium_lets_offline_db_status_through(monkeypatch, _tmp_project_root):
    """联网二连败 **不再硬停**——decide 照常调 LLM（prompt 带联网暂停注入）
    离线 db_status 提议照常放行并真跑（链上剩余的离线事项不被一刀切误伤）。"""
    _network_failing_search_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜肺", keywords="lung",
                   source="ArrayExpress", confidence="high", reason="联网搜"),
        # decide#1（只有一败，未触发暂停）：换词重试
        AIMessage(content='{"verb": "curate.search_online", "quoted": "联网搜肺",'
                          ' "source": "ArrayExpress", "keywords": "肺"}'),
        # decide#2（联网二连败 → 暂停中）：提议离线 db_status → 放行
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        AIMessage(content="联网两次都没成；库里共 0 条。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "联网搜肺，再告诉我库里多少条", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert [(s["verb"], s["ok"]) for s in plan["steps"]] == [
        ("curate.search_online", False), ("curate.search_online", False),
        ("curate.db_status", True)], "联网暂停下离线 db_status 必须照常执行"
    assert plan["steps"][0]["error_code"] == "network_error"
    # decide#2 的 prompt 必须带联网暂停注入段（机械约束摆给 LLM 看）
    assert "联网暂停" in model.invocations[2][0].content
    # 注入段（修订）：离线 db_status 与离线快照源检查都不连坐
    assert "离线工具 curate.db_status 与**离线快照源**" in model.invocations[2][0].content
    trace_text = "；".join(t.get("detail", "") for t in trace)
    assert "防原地空转" not in trace_text, "联网二连败不得走硬停"


def test_network_moratorium_rejects_network_proposals(monkeypatch, _tmp_project_root):
    """联网暂停中提议联网工具 → `_adjudicate_decide_obj` 机械拒绝：按 done 收尾、
    note 如实写「联网暂停中」，绝不真跑第三次联网。"""
    _network_failing_search_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜肺", keywords="lung",
                   source="ArrayExpress", confidence="high", reason="联网搜"),
        AIMessage(content='{"verb": "curate.search_online", "quoted": "联网搜肺",'
                          ' "source": "ArrayExpress", "keywords": "肺"}'),
        # decide#2（暂停中）仍提议联网 search → 机械拒绝
        _tool_call("curate.search_online", quoted="联网搜肺", keywords="human lung",
                   source="ArrayExpress"),
        AIMessage(content="联网两次都没成。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "联网搜肺", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert len(plan["steps"]) == 2, "联网提议被联网暂停拦下，绝不真跑第三次"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "联网暂停中" in decides[-1]["detail"]


def test_network_moratorium_lifts_after_a_success():
    """暂停是状态现算不是锁：尾两步不再二连败 → 联网提议照常放行（恢复路径不受误伤）。"""
    steps = [
        {"verb": "curate.search_online", "ok": False, "error_code": "network_error",
         "slots": {"source": "ArrayExpress", "keywords": "lung"}},
        {"verb": "curate.db_status", "ok": True, "slots": {}},
    ]
    assert not agent_exec._network_moratorium(steps)
    state = {"utterance": "联网搜肺，完了再检查下ENCODE", "steps": steps, "dead_ends": []}
    nxt, note, _declined, _fb = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "quoted": "再检查下ENCODE", "source": "ENCODE"}, state)
    assert nxt is not None, f"无二连败后联网提议必须照常放行：{note}"


def test_network_moratorium_unit_matrix():
    """判定矩阵：联网二连败 → True；非网络码二连败 / 一胜一败 / 只有一败 → False。"""
    net = {"ok": False, "error_code": "network_error"}
    shape = {"ok": False, "error_code": "bad_result_shape"}
    ok = {"ok": True}
    assert agent_exec._network_moratorium([net, net]) is True
    assert agent_exec._network_moratorium([shape, shape]) is False
    assert agent_exec._network_moratorium([net, shape]) is False  # 混合码 → 硬停侧
    assert agent_exec._network_moratorium([net, ok]) is False
    assert agent_exec._network_moratorium([net]) is False
    # 联网暂停下 db_status（离线）提议放行 / 联网提议拒绝——裁决层单元钉
    state = {"utterance": "再告诉我库里多少条", "steps": [dict(net), dict(net)], "dead_ends": []}
    nxt, _note, _d, _fb = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.db_status", "quoted": "库里多少条"}, state)
    assert nxt is not None, "联网二连败后 db_status（离线）提议必须放行"
    nxt2, note2, _d2, _fb2 = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "quoted": "再告诉我库里多少条"}, state)
    assert nxt2 is None and "联网暂停中" in note2, "联网二连败后联网提议必须机械拒绝"


# ---------------------------------------------------------------- 4. 失败步同指纹重试的精确豁免面（k03 放行 / f04 收窄）

def test_duplicate_gate_network_error_is_the_only_retriable_exemption():
    """`_is_duplicate_step` 比对集 = 成功步 + 非 network_error 失败步（收窄矩阵钉）：network_error 失败步放行同指纹重试；bad_result_shape 等确定性
    失败照样拦截（重试必败，不白烧步数）；成功步恒拦截；失败→重试成功→再同指纹被成功步拦下。"""
    ok_ae = {"verb": "curate.check_updates", "ok": True, "slots": {"source": "ArrayExpress"}}
    net_ae = {"verb": "curate.check_updates", "ok": False, "error_code": "network_error",
              "slots": {"source": "ArrayExpress"}}
    shape_ae = {"verb": "curate.check_updates", "ok": False, "error_code": "bad_result_shape",
                "slots": {"source": "ArrayExpress"}}
    raw = {"source": "ArrayExpress"}
    assert agent_exec._is_duplicate_step("curate.check_updates", raw, []) is False
    assert agent_exec._is_duplicate_step("curate.check_updates", raw, [ok_ae]) is True, \
        "成功步同指纹仍必须拦截"
    assert agent_exec._is_duplicate_step("curate.check_updates", raw, [net_ae]) is False, \
        "network_error 失败步同指纹重试必须放行"
    assert agent_exec._is_duplicate_step("curate.check_updates", raw, [shape_ae]) is True, \
        "确定性失败（bad_result_shape）同指纹重试必须拦截"
    assert agent_exec._is_duplicate_step("curate.check_updates", raw, [net_ae, ok_ae]) is True, \
        "失败→重试成功→再同指纹：被成功步拦下"
    assert agent_exec._is_duplicate_step("curate.check_updates", {"source": "ENCODE"}, [ok_ae]) is False


def test_failed_step_same_fingerprint_retry_is_allowed(monkeypatch):
    """（全链路）：check 网络失败 → decide 提议同源重查（恒同指纹，旧口径
    在此恒杀）→ 去重闸放行、第二次真跑成功——可纠正码的重试价值真实存在。"""
    outcomes = iter([
        _failing_tool("network_error", "网络抖动，稍后重试"),
        (lambda slots, root: {"checked_at": "t", "sources": [], "hint_zh": ""}),
    ])
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.check_updates", {
        "run": lambda slots, root: next(outcomes)(slots, root),
        "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True,
        "decide_zh": "测试替身",
    })
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        # decide#1：同源重查（与失败步同指纹）
        AIMessage(content='{"verb": "curate.check_updates", "quoted": "检查ArrayExpress更新",'
                          ' "source": "ArrayExpress"}'),
        _tool_call("finish", completion_report="1. 检查ArrayExpress更新：已做（第2步）"),
        AIMessage(content="第一次网络抖动失败，重查成功。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress更新", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert [(s["verb"], s["ok"]) for s in plan["steps"]] == [
        ("curate.check_updates", False), ("curate.check_updates", True)], \
        "失败步的同指纹重试必须放行并真跑"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "重复" not in decides[0]["detail"], "失败步重试不得被去重闸拦下"


def test_deterministic_failure_same_fingerprint_retry_is_blocked(monkeypatch):
    """全链路：工具返回破形状（bad_result_shape，确定性失败）→ decide 提议同指纹
    重试 → 去重闸拦截停环（一律豁免失败步会把确定性失败也放去白烧一步，收窄后拦下）。"""
    def broken(slots, root):
        return {"broken": True}  # 形状闸拦下 → step error_code="bad_result_shape"

    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.check_updates", {
        "run": broken, "label_zh": "检查来源更新", "card_kind": "check_updates",
        "readonly": True, "decide_zh": "测试替身",
    })
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        # decide#1：同指纹重试（确定性失败，重试必败）
        AIMessage(content='{"verb": "curate.check_updates", "quoted": "检查ArrayExpress更新",'
                          ' "source": "ArrayExpress"}'),
        AIMessage(content="检查没完成。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress更新", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert len(plan["steps"]) == 1, "确定性失败的同指纹重试不得真跑第二次"
    assert plan["steps"][0]["ok"] is False
    assert plan["steps"][0]["error_code"] == "bad_result_shape"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "重复" in decides[0]["detail"], "确定性失败的同指纹重试必须被去重闸拦下"


def test_failed_step_block_reaches_decide_prompt(monkeypatch):
    """问题4（重试挤占预算）：存在失败步时 decide prompt 注入「先新后旧」次序段
    全部成功时整段不出现（双向钉；纯 prompt 劝导无机械闸）。"""
    _network_failing_search_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜肺", keywords="lung",
                   source="ArrayExpress", confidence="high", reason="联网搜"),
        # decide#1（一步失败在场）：按「先新后旧」提议离线 db_status
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 联网搜肺：做不到（据第1步网络失败）\n2. 库里多少条：已做（第2步）")),
        AIMessage(content="搜索没成；库里共 0 条。"),
    )
    plan, _trace = agent_exec.plan_with_agent(
        "联网搜肺，再告诉我库里多少条", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert "先把没做过的**新事**做完" in model.invocations[1][0].content, \
        "失败步在场时「先新后旧」注入段必须进 decide prompt"
    assert "重试失败的事最多一次" in model.invocations[1][0].content
    assert [s["verb"] for s in plan["steps"]] == [
        "curate.search_online", "curate.db_status"]
    # 全部成功 → 整段不出现
    monkeypatch.setitem(agent_exec.LOOP_TOOLS, "curate.check_updates", {
        "run": lambda slots, root: {"checked_at": "t", "sources": [], "hint_zh": ""},
        "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True,
        "decide_zh": "测试替身",
    })
    model2 = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查更新", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report="1. 检查更新：已做（第1步）"),
        AIMessage(content="检到无新增。"),
    )
    agent_exec.plan_with_agent(
        "检查更新", has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model2)
    assert "先把没做过" not in model2.invocations[1][0].content, "无失败步时注入段不得出现"


# ---------------------------------------------------------------- 3. sync_updates 的汇报事实链（真机冒烟坐实的三处缺口）

def _sync_step(imported_total=0, new_count=1, note="检到疑似新增 1 条，但这个来源还没有联网入库适配器，本工具不能自动入库——可去官网核对"):
    return {
        "verb": "curate.sync_updates", "verb_zh": "检查更新并同步入库", "ok": True,
        "card_kind": "sync_updates", "readonly": False, "slots": {}, "ms": 1,
        "result": {
            "checked_at": "2026-08-06T23:00:00+08:00",
            "imported_total": imported_total,
            "sources": [{
                "source": "10x", "label": "10x Genomics", "mode": "online",
                "local_count": 774, "new_count": new_count,
                "imported_count": imported_total, "filename": None,
                "imported_titles": [], "note_zh": note,
            }],
            "hint_zh": "自动入库只覆盖能在线比对且有入库适配器的来源。",
        },
    }


def test_sync_projection_carries_notes_and_counts():
    """只报 ok 不报 note_zh = LLM 会把「检到了但不能自动入库」写成「完成」。"""
    proj = agent_exec._step_projection(_sync_step())
    src = proj["result"]["sources"][0]
    assert src["note_zh"] and "不能自动入库" in src["note_zh"]
    assert src["new_count"] == 1
    assert proj["result"]["imported_total"] == 0


def test_sync_import_counts_as_write_for_postcheck():
    """sync 真入库了 → 汇报说「入库」是实话，不得误判 claimed_write；
    没入库 → 汇报说「已入库」必须被拦。"""
    wrote_step = _sync_step(imported_total=2, new_count=2, note="已自动入库 2 条")
    assert agent_exec._report_contradiction_reason("已自动入库 2 条到新文件。", [wrote_step]) is None
    dry_step = _sync_step(imported_total=0)
    assert agent_exec._report_contradiction_reason("已为你下载并入库。", [dry_step]) == "claimed_write"


def test_sync_true_counts_cover_new_and_imported():
    """「疑似新增 1 条」是真数字，不得误判 count_mismatch。"""
    step = _sync_step(imported_total=0, new_count=1)
    counts = agent_exec._step_true_counts([step])
    assert 1 in counts and 0 in counts
    assert agent_exec._report_contradiction_reason(
        "10x 疑似新增 1 条，但本工具不能自动入库。", [step]) is None


def test_sync_fallback_report_names_unclosed_part():
    """确定性兜底汇报必须点名「没闭环」的那部分，不许只写「完成」。"""
    report = agent_exec._steps_report_fallback_zh([_sync_step()])
    assert "不能自动入库" in report and "疑似新增 1 条" in report


# ---------------------------------------------------------------- 误伤修复（机械后检精度）

def _check_step(online_recent=12, local_count=1784, new_count=2, n_cands=2):
    """check_updates 成功步的最小真形状（与探针负载同口径）。"""
    return {
        "verb": "curate.check_updates", "ok": True, "card_kind": "check_updates",
        "result": {"checked_at": "t", "sources": [{
            "source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
            "local_count": local_count, "online_recent": online_recent,
            "new_count": new_count,
            "new_candidates": [{"accession": f"E-MTAB-{k}", "title": f"t{k}"}
                               for k in range(n_cands)],
            "snapshot_date": "2026-08-01",
        }], "hint_zh": ""},
    }


def _ok_search_step(record_count=2):
    """search_online 成功步的最小真形状。"""
    return {
        "verb": "curate.search_online", "ok": True, "card_kind": "search_online",
        "result": {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
                   "record_count": record_count, "filename": "upload_x.json",
                   "sample_titles": [], "warnings": []},
    }


def test_check_recent_and_local_counts_are_reportable():
    """v8 误伤修复①：「在线发现 12 条近期记录」「本地库原有 1784 条」的 12/1784 是投影里的
    真数字（online_recent / local_count）——旧比对基准没登记，如实汇报反被误判
    count_mismatch（v8 该拦截簇主力）。真谎称（凭空 99 条）必须照拦。"""
    step = _check_step()
    counts = agent_exec._step_true_counts([step])
    assert {12, 1784, 2} <= counts
    assert agent_exec._report_contradiction_reason(
        "在线比对发现 12 条近期记录，其中 2 条为新增。", [step]) is None
    assert agent_exec._report_contradiction_reason(
        "在线比对发现 99 条近期记录。", [step]) == "count_mismatch"


def test_db_status_total_is_reportable():
    """v8 误伤修复①配套：多步链收尾 db_status 的 total_records 登记进比对基准。"""
    step = {"verb": "curate.db_status", "ok": True, "card_kind": "db_status",
            "result": {"generated_at": "t", "sources": [], "total_records": 4756,
                       "external_files": [], "recycle": [],
                       "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}}}
    assert 4756 in agent_exec._step_true_counts([step])


def test_sync_step_name_quote_and_zero_import_are_not_claims():
    """v8 误伤修复②：照抄步骤名「检查更新并同步入库」+ 如实报零入库（「导入0条」）
    → 旧版 claimed_write 误判（v8 该拦截簇主力，b06/b10/d05 同型）；真谎称照拦。"""
    dry = _sync_step(imported_total=0, new_count=0, note="没有疑似新增")
    assert agent_exec._report_contradiction_reason(
        "检查ENCODE更新并同步入库：没有疑似新增，因此未导入任何数据（导入0条）。",
        [dry]) is None
    assert agent_exec._report_contradiction_reason(
        "已为你下载并入库。", [dry]) == "claimed_write"


def test_zero_hit_search_honest_denial_is_not_flagged():
    """v8 误伤修复③：search 零命中时「没搜到、没入库」是诚实措辞（wrote 口径收窄到
    record_count>0，否认侧不参与）；真入库 2 条后的「未执行入库」照拦（denied_write）。"""
    zero = _ok_search_step(record_count=0)
    assert agent_exec._report_contradiction_reason(
        "联网搜索后没搜到符合条件的数据集，因此没有入库。", [zero]) is None
    two = _ok_search_step(record_count=2)
    assert agent_exec._report_contradiction_reason(
        "联网搜索完成，找到 2 条。未执行数据入库操作。", [two]) == "denied_write"
