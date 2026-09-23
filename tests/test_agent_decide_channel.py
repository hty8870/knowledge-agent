# -*- coding: utf-8 -*-
"""decide 的 tool-calling 主通道 + 图单例并发（2026-08-07 langgraph）的确定性门。**全离线**：

- decide 通道矩阵：finish / 空回答 / 散文拒答 / content JSON 双通道 / 幻觉工具名 /
  多 tool_call 取第一个（DeepSeek 不遵守 parallel_tool_calls=False）/
  unsupported_next_step 婉拒 / none 干净 done / tools 通道异常跌
  JSON 兜底（审计 node="decide"）/ parallel_tool_calls=False 契约；
- **非法应答重问一次**：主通道拿到非法应答
  重问一次；重问后的写动词 **放行 + 强制核销复核**（2026-08-07 设计决定 B 方案，取代
  2026-08-08 只读闸）——放行的写步落 `reask_writes` 台账，finish 报告必须引用其步骤号
  单独交代结果，否则核销硬闸拒收；重问仍非法 / JSON 兜底档非法 → 照旧停环不多问；
- **violation 重问对称化**（r3 坐实）：续步提议没过 `_validate_raw`
  校验 → 带检查意见重问一次，与非法应答**共享同一份重问预算**（每次 decide 至多一次）；
  重问后改对照常放行、仍违规 fail-safe 停环；去重/覆盖闸/死路/联网暂停等刻意机械停不重问；
- understand 侧多 tool_call 同样取第一个（不再判空跌兜底）；
- 图单例：编译计数器跨调用不增长；ThreadPoolExecutor 多请求交错跑同一 compiled graph
  不串味（评审的验收钉）
- **未决事项机械提示段**（finish 核销被「带步骤引用的假豁免/假已做」
  绕过族）——点名源未触碰 / 库容问句缺 db_status / 检出未入库 三条机械规则命中时注入
  decide 双壳 prompt；提示不是闸，三规则全不命中整段不出现。
"""
import json
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：decide 通道测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-channel-test")

UTTER = "检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库"
AE_TWO_NEW = [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
               "local_count": 10, "online_recent": 12, "new_count": 2,
               "new_candidates": [{"accession": "E-MTAB-1", "title": "human lung atlas"},
                                  {"accession": "E-MTAB-2", "title": "human lung tumor"}]}]
SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260807_curate_arrayexpress.json", "warnings": []}


class _FakeModel:
    """bind_tools 记录档位参数并返回自身；invoke 依次弹预置项（("raise",) 抛异常）。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.binds = []
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.binds.append({"tool_choice": tool_choice,
                           "parallel_tool_calls": parallel_tool_calls,
                           "tools": tools})
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        item = self.answers.pop(0)
        if item == ("raise",):
            raise RuntimeError("provider boom")
        return item


def _tool_call(verb, **args):
    return AIMessage(content="",
                     tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


def _raw_tool_call(name, args):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "t1"}])


def _plan(utterance, model, **kwargs):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model, **kwargs)


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _fake_tools(monkeypatch):
    """全用例共享的假注册表：check 返两条疑似新增、search 返两条入库（替身形状同真表）。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {
            "run": lambda slots, root: {"checked_at": "t", "sources": AE_TWO_NEW, "hint_zh": ""},
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True},
        "curate.search_online": {
            "run": lambda slots, root: dict(SEARCH_OK),
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False},
        "curate.db_status": {
            "run": lambda slots, root: {
                "generated_at": "t", "sources": [], "total_records": 0,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
    })


def _understand_check():
    return _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                      source="ArrayExpress", confidence="high", reason="查更新")


def _decide_nodes(trace):
    return [t for t in trace if t["node"] == "decide"]


# ---------------------------------------------------------------- finish / done 语义

def test_finish_tool_call_is_a_clean_done():
    """decide 回 finish 工具调用 = 结构化 done：停环、note 与旧版「判断完成」逐字一致。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("finish", {}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    # 2026-08-08 换纯净 utterance（无入库诉求——pending 硬闸不拦 finish）
    # 本钉钉的是「finish 工具调用=干净 done」的通道语义，不是条件入库场景。
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"]
    decides = _decide_nodes(trace)
    assert len(decides) == 1
    assert "大模型判断：要求的事已经完成" in decides[0]["detail"]


def test_empty_answer_is_reasked_once_then_done():
    """空回答 = 非法 → **重问一次**（
    旧钉「拿到但非法绝不多问第二次」细化为「重问一次」；重问后的写动词
    2026-08-07 设计决定 B 方案——放行 + 强制核销，见下方专项测试）
    重问仍非法 → 照旧停环，不再问第二次。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content=""),          # decide：空回答 → 非法
        AIMessage(content="检到 2 条。"),  # 重问：仍是散文 → 非法 → 停环
        # narrate 没有 answer → IndexError → 确定性兜底
    )
    plan, trace = _plan(UTTER, model)
    decides = _decide_nodes(trace)
    assert len(decides) == 1 and "没给出能读懂的答复" in decides[0]["detail"]
    assert "第一次回答没读懂，已重问一次" in decides[0]["detail"]
    # decide 消耗两次 invoke（首答 + 重问），没有第三次问；第四次是 narrate 的汇报
    assert len(model.invocations) == 4  # understand + decide + 重问 + narrate


def test_prose_refusal_is_reasked_then_done():
    """散文拒答 → 非法 → 重问一次 → 重问应答仍是散文 → 停环 done（不执行任何续步）。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content="我觉得差不多可以了"),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"]
    assert "没给出能读懂的答复" in _decide_nodes(trace)[0]["detail"]


# ---------------------------------------------------------------- 双通道 / 控制工具

def test_content_json_still_drives_the_loop():
    """模型没调工具但 content 是可解析 JSON（双通道，与 understand 同真源）→ 照常续步。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺", "species": "人类"},
            ensure_ascii=False)),
        _raw_tool_call("finish", {}),
        AIMessage(content="检到新增并已入库。"),
    )
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.search_online"]
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "execute", "decide",
        "validate", "execute", "decide", "narrate"]


def test_hallucinated_tool_name_is_invalid_done():
    """幻觉工具名（工具面里不存在）→ invalid → 重问一次 → 重问应答（散文）仍非法 →
    fail-safe 停环，绝不执行。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("download_everything", {"quoted": "下载下来"}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"]
    assert "没给出能读懂的答复" in _decide_nodes(trace)[0]["detail"]


def test_multiple_tool_calls_accept_first_in_order():
    """decide 一次回两个 tool_call → **取第一个按序执行**（取代
    「多调用机械拒绝」：DeepSeek 实测不遵守 parallel_tool_calls=False，实测 decide
    不可读 17/17 全是多调用、第一个调用 17/17 合法续步——循环带新状态会再判断后续，
    吃第一个不吞事）；trace 如实留痕「一次给了 2 个调用」。"""
    checklist_ans = AIMessage(content=json.dumps([
        {"text": "检查更新", "anchor": "检查ArrayExpress是否有更新",
         "expect_verb": "curate.check_updates"},
        {"text": "搜来入库", "anchor": "联网搜来入库", "expect_verb": "curate.search_online"},
    ], ensure_ascii=False))
    both = AIMessage(content="", tool_calls=[
        {"name": "curate_search_online",
         "args": {"quoted": "联网搜来入库", "keywords": "人类肺", "source": "ArrayExpress"},
         "id": "t1"},
        {"name": "curate_db_status", "args": {"quoted": "库里"}, "id": "t2"},
    ])
    finish = _raw_tool_call("finish", {"completion_report":
                                       "1. 检查ArrayExpress更新：已做（第1步）。\n"
                                       "2. 联网搜来入库：已做（第2步）。"})
    model = _FakeModel(_understand_check(), checklist_ans, both, finish)
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.search_online"], "多调用的第一个必须照常执行"
    assert "一次给了 2 个调用" in _decide_nodes(trace)[0]["detail"]


def test_unsupported_next_step_goes_through_declined_path():
    """unsupported_next_step(verb=pack.download)：婉拒的正式通道——trace 措辞与旧散文版
    逐位一致，declined_zh 进 narrate 的确定性兜底汇报（点名「打包下载」没做）。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("unsupported_next_step", {"verb": "pack.download"}),
        # narrate 没有第三个 answer → IndexError → 确定性兜底（必须点名没做的事）
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来，然后打包下载", model)
    decides = _decide_nodes(trace)
    assert len(decides) == 1
    assert "不在允许自动执行的范围" in decides[0]["detail"]
    report = plan.get("report_zh") or ""
    assert "打包下载" in report and "没有做" in report, "兜底汇报必须如实点名婉拒的那件事"


def test_none_means_clean_done_now():
    """content JSON 里 {"verb":"none"}：2026-08-07 起 = 干净的 done（旧版误入婉拒路径，
    会说出「你要的『没有操作』这一步没有做」的怪话——此钉防复活）。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content='{"verb": "none"}'),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan(UTTER, model)
    decides = _decide_nodes(trace)
    assert len(decides) == 1
    assert "大模型判断：要求的事已经完成" in decides[0]["detail"]
    assert "没有操作" not in (plan.get("report_zh") or "")


# ---------------------------------------------------------------- 非法应答重问一次

def test_invalid_answer_reask_allows_readonly_continuation():
    """问题主治①：decide 散文拒答 → 重问一次 → 重问后提议只读 db_status → 照常放行执行
    trace 如实记「第一次回答没读懂，已重问一次」。"""
    utter = "检查ArrayExpress是否有更新，完了告诉我库里有多少条"
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="我觉得差不多可以了"),            # decide#1：散文 → 非法
        _tool_call("curate.db_status", quoted="告诉我库里有多少条",
                   confidence="high", reason="查库况"),      # 重问后：只读续步
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）\n2. 告诉我库里有多少条：已做（第2步）"}),
        AIMessage(content="已检查更新，并汇报了库里条数。"),
    )
    plan, trace = _plan(utter, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "重问后的只读续步必须照常放行并真跑"
    decides = _decide_nodes(trace)
    assert "第一次回答没读懂，已重问一次" in decides[0]["detail"]
    assert "还需要一步" in decides[0]["detail"]
    reask = model.invocations[2]
    assert "你刚才的回答没能读懂" in reask[-1].content, "任务书钉的反馈文案必须进重问消息"


def test_invalid_answer_reask_write_verb_allowed_with_forced_accounting():
    """2026-08-07 设计决定 **B 方案**（取代 2026-08-08 只读闸；旧钉「重问后的写动作绝不
    执行」的误杀现场：g04/l07 型长链「首答散文→重问答对但首选写动词」被稳定截断）：
    重问后提议 search_online（写）→ **放行真跑**、落强制核销账（trace 如实留痕）；
    finish 报告不引用该步步骤号 → 核销硬闸拒收回灌（形态专句「没有单独交代」）；
    回灌后补上引用 → 接受收尾。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content="我觉得差不多可以了"),            # decide#1：散文 → 非法
        _tool_call("curate.search_online", quoted="联网搜来入库", source="ArrayExpress",
                   keywords="人类肺"),                       # 重问后：写动词提议 → 放行
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）\n2. 人类肺数据入库这件事办妥了。"}),  # 缺第2步引用 → 拒收
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）\n2. 搜来入库：已做（第2步），入 2 条"}),  # 补全 → 接受
        AIMessage(content="已检查更新并搜来入库。"),
    )
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.search_online"], "重问后的写动词必须放行并真跑"
    decides = _decide_nodes(trace)
    assert len(decides) == 2
    assert "第一次回答没读懂，已重问一次" in decides[0]["detail"]
    assert "重问后放行的写动作已记入强制核销账" in decides[0]["detail"]
    assert "没有单独交代" in decides[1]["detail"], "缺交代的 finish 必须触发强制核销否决"
    assert "已拒收收尾并把缺口回灌重问一次" in decides[1]["detail"]
    assert "大模型判断：要求的事已经完成" in decides[1]["detail"]


def test_reask_write_unaccounted_third_finish_is_failsafe_accepted():
    """重问写步三次 finish 都不单独交代 → 第三次 fail-safe 接受并如实标注（每请求至多
    回灌 2 次，防原地空转；标注进 trace，人读可复盘）。
    2026-08-09 调研-长程agent批 候选2：旧「第二击即放行」过软，升级为三击放行。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content=""),                               # decide#1：空回答 → 非法
        _tool_call("curate.search_online", quoted="联网搜来入库", source="ArrayExpress",
                   keywords="人类肺"),                       # 重问后：写动词 → 放行
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）"}),                  # 缺第2步交代 → 拒收回灌（第 1 次）
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）"}),                  # 仍缺 → 拒收回灌（第 2 次）
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）"}),                  # 仍缺 → fail-safe 接受
        AIMessage(content="已检查更新并搜来入库。"),
    )
    plan, trace = _plan(UTTER, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.search_online"]
    details = "\n".join(d["detail"] for d in _decide_nodes(trace))
    assert "仍未单独交代重问后放行的写步结果" in details
    assert "已回灌重问 2 次" in details


def test_reask_write_veto_unit():
    """强制核销复核的单元钉：台账写步缺步骤号引用 → reask_write_unaccounted 否决；
    引用了（含中文数字「第二步」）→ 放行；空台账 / 空报告 → 放行。"""
    writes = [{"verb": "curate.search_online", "verb_zh": "联网搜索入库", "step_no": 2}]
    line, shape = agent_exec._reask_write_veto("1. 检查：已做（第1步）", writes)
    assert shape == "reask_write_unaccounted" and "第 2 步" in line
    assert agent_exec._reask_write_veto(
        "1. 检查：已做（第1步）\n2. 入库：已做（第2步），入 2 条", writes) == (None, "")
    assert agent_exec._reask_write_veto("入库是据第二步的结果办的", writes) == (None, "")
    assert agent_exec._reask_write_veto("随便写点啥", []) == (None, "")


def test_invalid_answer_reask_finish_is_accepted():
    """重问后回 finish（核销报告合法）→ 正常收尾（finish/unsupported 走既有裁决）。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content=""),                              # decide#1：空回答 → 非法
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）"}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)  # 纯净 utterance（同上钉注）
    decides = _decide_nodes(trace)
    assert len(decides) == 1
    assert "第一次回答没读懂，已重问一次" in decides[0]["detail"]
    assert "大模型判断：要求的事已经完成" in decides[0]["detail"]


def test_valid_answer_is_never_reasked():
    """合法应答不重问（调用计数钉）：finish 一次到位，invocations 恒为 3。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）"}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)  # 纯净 utterance（同上钉注）
    assert len(model.invocations) == 3  # understand + decide + narrate
    assert "重问" not in _decide_nodes(trace)[0]["detail"]


# ---------------------------------------------------------------- violation 重问对称化（r3 坐实）

def test_violation_proposal_is_reasked_once_then_finish():
    """decide 续步提议没通过机械校验（quoted 非原话逐字）→ **带检查意见重问一次**
    （与非法应答重问同型对称）；重问后 finish（报告合法）→ 正常收尾。trace 如实留痕，
    调用计数 = understand + decide + 重问 + narrate。"""
    model = _FakeModel(
        _understand_check(),
        _tool_call("curate.check_updates", quoted="检查一遍所有来源", source="ArrayExpress",
                   confidence="high", reason="查更新"),   # quoted 非原话逐字 → violation
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）"}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    decides = _decide_nodes(trace)
    assert "已带检查意见重问一次" in decides[0]["detail"]
    assert "大模型判断：要求的事已经完成" in decides[0]["detail"]
    assert len(model.invocations) == 4, "understand + decide + 重问 + narrate，不许再多"
    reask = model.invocations[2]
    assert "检查意见" in reask[-2].content
    assert "不是用户原话里逐字出现的片段" in reask[-2].content, "违规清单必须如实回灌"


def test_violation_reask_still_violating_is_failsafe_done():
    """重问后仍违规 → 照旧 fail-safe 停环不多问（重问预算至多一次），违规提议绝不真跑。"""
    model = _FakeModel(
        _understand_check(),
        _tool_call("curate.check_updates", quoted="检查一遍所有来源", source="ArrayExpress"),
        _tool_call("curate.check_updates", quoted="把所有来源看一遍", source="ArrayExpress"),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    decides = _decide_nodes(trace)
    assert len(decides) == 1
    assert "已带检查意见重问一次" in decides[0]["detail"]
    assert "没通过检查" in decides[0]["detail"] and "按「已完成」收尾" in decides[0]["detail"]
    assert len(model.invocations) == 4
    assert len(plan.get("steps") or []) == 1, "违规提议绝不能真跑"


def test_no_double_reask_invalid_then_violation():
    """重问预算共享：非法应答已用过一次重问后，violation 不再问（每次 decide 至多一次）。"""
    model = _FakeModel(
        _understand_check(),
        AIMessage(content=""),                              # decide#1：空回答 → 非法重问
        _tool_call("curate.check_updates", quoted="检查一遍所有来源", source="ArrayExpress"),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    detail = _decide_nodes(trace)[0]["detail"]
    assert "第一次回答没读懂，已重问一次" in detail
    assert "没通过检查" in detail and "按「已完成」收尾" in detail
    assert "已带检查意见重问" not in detail, "非法重问已花掉预算，violation 不许再问"
    assert len(model.invocations) == 4


def test_violation_reask_allows_corrected_continuation():
    """主治场景：violation 重问后模型改对（quoted 逐字）→ 照常放行执行（长链接续）。"""
    model = _FakeModel(
        _understand_check(),
        _tool_call("curate.db_status", quoted="看看库里状态"),     # 非逐字 → violation
        _tool_call("curate.db_status", quoted="库里有多少条"),     # 重问后：逐字 → 放行
        _raw_tool_call("finish", {"completion_report":
            "1. 检查更新：已做（第1步）\n2. 库里有多少条：已做（第2步）"}),
        AIMessage(content="已检查更新并汇报库容。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，完了告诉我库里有多少条", model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "重问后改对的提议必须照常放行并真跑"
    decides = _decide_nodes(trace)
    assert "已带检查意见重问一次" in decides[0]["detail"]
    assert "还需要一步" in decides[0]["detail"]


def test_adjudication_violation_feedback_unit():
    """裁决第四件（重问反馈）只在 `_validate_raw` 违规这一种停法下非空：违规 → 带违规清单；
    放行 / 范围外机械停 → 空（刻意的机械停不重问）。"""
    state = {"utterance": "检查ArrayExpress是否有更新", "steps": [], "dead_ends": []}
    nxt, _note, _d, fb = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "quoted": "检查一遍所有来源",
         "source": "ArrayExpress"}, state)
    assert nxt is None and "不是用户原话里逐字出现的片段" in fb
    nxt2, _n2, _d2, fb2 = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.check_updates", "quoted": "检查ArrayExpress是否有更新",
         "source": "ArrayExpress"}, state)
    assert nxt2 is not None and fb2 == ""
    _n3, _nt3, declined3, fb3 = agent_exec._adjudicate_decide_obj({"verb": "curl"}, state)
    assert declined3 and fb3 == "", "范围外是刻意的机械停，不给重问反馈"


# ---------------------------------------------------------------- 未决事项机械提示段（2026-08-08 核销被「合法措辞」绕过族）

_CHECK_NEW_STEP = {"verb": "curate.check_updates", "ok": True,
                   "slots": {"source": "ArrayExpress"},
                   "result": {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                                           "mode": "online", "new_count": 2}]}}
_DB_OK_STEP = {"verb": "curate.db_status", "ok": True, "slots": {}}
_SEARCH_OK_STEP = {"verb": "curate.search_online", "ok": True,
                   "slots": {"keywords": "human lung", "source": "ArrayExpress"}}


def test_pending_hints_rule_matrix():
    """三条规则各自的命中/清除矩阵（**提示不是闸**，拿不准绝不报）：
    ①点名源未触碰（逐字规范名 ENCODE 也算点名——与点名源闸的豁免同口径）
    ②库容问句缺 db_status；③检出未入库（原话含「同步」时不报——sync 本身就是入库路径）。"""
    # 规则 1：ENCODE 未触碰 → 一行；被触碰（slots.source）→ 清除
    block = agent_exec._pending_hints_block_zh("检查一下ENCODE有没有更新", [])
    assert "原话点名的来源「ENCODE」还没有任何一步处理过" in block
    enc_touched = {"verb": "curate.check_updates", "ok": True, "slots": {"source": "ENCODE"},
                   "result": {"sources": []}}
    assert "ENCODE" not in agent_exec._pending_hints_block_zh(
        "检查一下ENCODE有没有更新", [enc_touched])
    # 规则 2：库容问句 + 无 ok db_status → 一行；db_status 做过 → 清除
    assert "还没有执行过 db_status" in agent_exec._pending_hints_block_zh("看看库里多少条", [])
    assert agent_exec._pending_hints_block_zh("看看库里多少条", [_DB_OK_STEP]) == ""
    # 规则 3：检出疑似新增 + 有入库诉求 + 无 ok 入库步 → 一行；搜过 → 清除；「同步」→ 不报
    block3 = agent_exec._pending_hints_block_zh("有新的人类肺数据就搜来入库", [_CHECK_NEW_STEP])
    assert "「检出」不等于「入库」" in block3
    assert "检出" not in agent_exec._pending_hints_block_zh(
        "有新的人类肺数据就搜来入库", [_CHECK_NEW_STEP, _SEARCH_OK_STEP])
    assert "检出" not in agent_exec._pending_hints_block_zh("有新增就同步进来", [_CHECK_NEW_STEP])
    # 三规则全不命中 → 整段不出现（无点名源/无库容问句/无入库诉求）
    assert agent_exec._pending_hints_block_zh("检查更新", []) == ""


def test_pending_hints_import_denial_polarity():
    """提示层收尾：规则 3 的入库诉求词表
    复用 `_DENIAL_MORPH_RE` 做同小句否定极性豁免——「有新增也不要入库」是**拒绝**入库，
    裸子串曾把它当诉求提示模型去入库。提示不是闸，但「提示零谎报」的可信度同守。
    粒度与硬闸 `_import_hard_request` 一致：逐命中回看同小句，混合句不被整句豁免。"""
    # 否定极性：同小句有否定语素 → 不报（此前裸子串误报）
    assert agent_exec._pending_hints_block_zh(
        "检查ArrayExpress有没有更新，有新增也不要入库，就告诉我有没有",
        [_CHECK_NEW_STEP]) == ""
    # 肯定诉求：照报（既有行为回归）
    assert "「检出」不等于「入库」" in agent_exec._pending_hints_block_zh(
        "有新的人类肺数据就搜来入库", [_CHECK_NEW_STEP])
    # 混合句：「别重复入库」否定 +「有新增就下载」肯定 → 肯定命中仍计入，照报
    assert "「检出」不等于「入库」" in agent_exec._pending_hints_block_zh(
        "别重复入库；有新增就下载回来", [_CHECK_NEW_STEP])


def test_pending_hints_block_reaches_both_decide_channels():
    """首步 check 检出疑似新增（new_count=2）后，decide 的 tools 主通道与 JSON 兜底 prompt
    都带「检出未入库」提示行（让 tools 通道抛异常逼出兜底档，两个 prompt 一起钉）。"""
    model = _FakeModel(
        _understand_check(),
        ("raise",),  # decide tools 通道异常 → JSON 兜底再问一次
        AIMessage(content='{"done": true}'),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    _plan(UTTER, model)
    tools_prompt = model.invocations[1][0].content
    json_prompt = model.invocations[2][0].content
    for prompt in (tools_prompt, json_prompt):
        assert "机械提示：以下事项可能还没做" in prompt
        assert "「检出」不等于「入库」" in prompt
        # 段末重锚输出契约（真机坐实：无此句时模型被「逐项核对」带进散文）
        assert "核对完仍按上面的规则回答" in prompt


def test_pending_hints_block_absent_when_nothing_pending():
    """三规则都无命中 → 整段不出现（提示段零谎报才有可信度）。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）"}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    _plan("检查ArrayExpress是否有更新", model)
    assert "机械提示" not in model.invocations[1][0].content


# ---------------------------------------------------------------- 兜底档与审计

def test_decide_tools_channel_exception_falls_back_to_json_with_audit(_tmp_project_root):
    """decide 的 tools 通道抛异常 → 散文 JSON 兜底再问一次（按当前面装配的 JSON 壳全文），
    拿到 {"done": true} → 正常停环；且往 agent_fallbacks.jsonl 落一行 node="decide" 的账。"""
    model = _FakeModel(
        _understand_check(),
        ("raise",),                       # decide tools 通道异常
        AIMessage(content='{"done": true}'),  # JSON 兜底拿到真答案
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan(UTTER, model)
    assert "大模型判断：要求的事已经完成" in _decide_nodes(trace)[0]["detail"]
    log_path = _tmp_project_root / ".userdata" / "agent_fallbacks.jsonl"
    rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]
    assert len(rows) == 1 and rows[0]["node"] == "decide", "decide 跌兜底必须如实记在哪个节点"
    assert rows[0]["reason"] == "RuntimeError"


def test_json_fallback_invalid_answer_is_not_reasked():
    """JSON 兜底档的非法应答**不多问**（通道异常已经跌过一次兜底，保命档不再加门槛——
    重问机制只管 tool 主通道的非法应答）：tools 通道抛异常 → 兜底回散文 → 非法 →
    直接停环，调用计数钉死没有第三次 decide 问答。"""
    model = _FakeModel(
        _understand_check(),
        ("raise",),                              # decide tools 通道异常
        AIMessage(content="我觉得差不多可以了"),    # JSON 兜底拿到散文 → 非法
        AIMessage(content="检到 2 条疑似新增。"),
    )
    plan, trace = _plan(UTTER, model)
    decides = _decide_nodes(trace)
    assert "没给出能读懂的答复" in decides[0]["detail"]
    assert "重问" not in decides[0]["detail"], "兜底档的非法应答不许再触发重问"
    assert len(model.invocations) == 4  # understand + decide(tools) + decide(JSON兜底) + narrate


def test_parallel_tool_calls_disabled_and_tier_choices():
    """契约钉：所有 bind_tools 都带 parallel_tool_calls=False；understand 用 required 起、
    decide 用 auto 起（done 是合法答案，不能强制）。"""
    model = _FakeModel(
        _understand_check(),
        _raw_tool_call("finish", {}),
        AIMessage(content="检到 2 条疑似新增。"),
    )
    _plan(UTTER, model)
    assert model.binds, "understand/decide 都必须走 bind_tools 通道"
    assert all(b["parallel_tool_calls"] is False for b in model.binds)
    assert model.binds[0]["tool_choice"] == "required"   # understand
    assert model.binds[1]["tool_choice"] == "auto"       # decide
    decide_tools = {t["function"]["name"] for t in model.binds[1]["tools"]}
    # general 套件 decide 面 = 8 loop（rank/rerank/route_request 常驻
    # 入列）+ finish + unsupported_next_step。
    # 2026-08-17 rb1 刻意更新：curate.rollback 入列（回滚动词化），9 loop + 2 控制。
    # 2026-08-18 四工具批刻意更新：compare.datasets / cite.export / compat.find /
    # fair.check 入列（环内结果处理四工具），13 loop + 2 控制。
    # 2026-09-13 rerank 处置批刻意更新：results_show 入列（屏态唯一出口动词，
    # general 继承 search 套件面），14 loop + 2 控制。
    # 2026-09-14 YOLO 批刻意更新：ask_relax 入列（「问用户放松哪个条件」选择卡，
    # general 继承 search 套件面），15 loop + 2 控制。
    assert decide_tools == {
        "curate_check_updates", "curate_search_online", "curate_sync_updates",
        "curate_db_status", "search_rerun", "rank", "rerank", "route_request",
        "curate_rollback", "compare_datasets", "cite_export", "compat_find",
        "fair_check", "results_show", "ask_relax",
        "finish", "unsupported_next_step",
    }, "decide 工具面 = 15 loop + 2 控制（不是全动词表，评审；2026-09-14 新增 ask.relax）"


def test_understand_accepts_first_of_multiple_tool_calls(_tmp_project_root):
    """understand 侧同策（取代「多调用判空跌兜底」）：实测
    understand 的「no_tool_calls」跌兜底 8/8 实为多调用（DeepSeek 不遵守
    parallel_tool_calls=False）——取第一个合法调用，不跌兜底、不白付一次 JSON 重问；
    第二个调用不偷跑（后续动作由循环带新状态再判断）。"""
    model = _FakeModel(
        AIMessage(content="", tool_calls=[
            {"name": "curate_check_updates",
             "args": {"quoted": "检查ArrayExpress是否有更新", "source": "ArrayExpress"},
             "id": "t1"},
            {"name": "curate_db_status", "args": {"quoted": "库里"}, "id": "t2"},
        ]),
        AIMessage(content=json.dumps([
            {"text": "检查更新", "anchor": "检查ArrayExpress是否有更新",
             "expect_verb": "curate.check_updates"},
        ], ensure_ascii=False)),
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）。"}),
        _tool_call("curate.search_online", quoted="联网搜来入库", keywords="人类肺",
                   source="ArrayExpress"),  # finish 被 pending 硬闸（检出未入库）否决后的续步
        _raw_tool_call("finish", {"completion_report": "1. 检查更新：已做（第1步）。\n"
                                  "2. 联网搜来入库：已做（第2步）。"}),
    )
    plan, trace = _plan(UTTER, model)
    assert plan["verb"] == "curate.check_updates"
    assert "工具调用模式" in trace[1]["detail"], "多调用取第一个后不得再跌兜底通道"  # [0] 是常驻环首
    log_path = _tmp_project_root / ".userdata" / "agent_fallbacks.jsonl"
    assert not log_path.is_file(), "多调用取第一个后不许再白付 JSON 兜底调用"
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert "curate.db_status" not in verbs, "多调用的第二个不许偷跑——后续由循环再判断"


# ---------------------------------------------------------------- 图单例与并发

def test_graph_compiled_once_across_calls():
    before = agent_exec._GRAPH_BUILDS
    for _ in range(2):
        model = _FakeModel(_understand_check(), _raw_tool_call("finish", {}),
                           AIMessage(content="检到 2 条。"))
        _plan(UTTER, model)
    assert agent_exec._GRAPH_BUILDS == before, "plan_with_agent 不得触发重编译"


def test_concurrent_requests_on_the_shared_graph_do_not_bleed(_tmp_project_root):
    """4 线程交错跑同一个 compiled graph（各自独立 fake model/剧本）：每线程的
    steps/trace 只含自己的动词与 quoted——编译产物共享、运行态 per-call。"""
    def job(i):
        utter = f"检查ArrayExpress是否有更新·第{i}路"
        model = _FakeModel(
            _tool_call("curate.check_updates", quoted=utter, source="ArrayExpress",
                       confidence="high", reason="查更新"),
            _raw_tool_call("finish", {}),
            AIMessage(content=f"第{i}路检到 2 条疑似新增。"),
        )
        plan, trace = _plan(utter, model)
        return plan, trace, utter

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(job, range(4)))
    for plan, trace, utter in results:
        steps = plan.get("steps") or []
        assert len(steps) == 1 and steps[0]["verb"] == "curate.check_updates"
        assert plan.get("quoted") == utter, "quoted 串味 = 跨请求 state 泄漏"
        assert steps[0]["slots"].get("source") == "ArrayExpress"
        assert [t["node"] for t in trace] == [
            "route_consensus", "understand", "validate", "execute", "decide", "narrate"]
