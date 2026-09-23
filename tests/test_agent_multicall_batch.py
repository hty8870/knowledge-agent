# -*- coding: utf-8 -*-
"""多 tool_call **同批只读消费**（2026-08-14 批）的确定性门。**全离线**（FakeModel + 假注册表）。

实测依据（探针落盘 `research/reports/multicall-legality-probe/`）：A 类独立只读
批量第 2..N 个调用 schema/语义合法率 100%（n=371，双臂）；写动词预发保真仅 45%（
108 运行）——故 decide 多调用的第 2..N 个里**只读白名单**（check_updates/db_status）
且互相独立的续步随首步同批执行；写动词/幻觉名出现即截断、整尾回炉再判。

本文件钉死（与 test_agent_decide_channel.py 的既有「取第一个」钉互补——混批/zero 采纳时
旧措辞与旧行为逐位不变）：
- 全只读批量：第 2..N 个同批执行（步骤顺序不变、每调用一条 execute trace、少一轮 decide 往返）；
- 混批回退：写动词在批中 → 该写及其后的只读（报数可能在写给后）整尾回炉，下一轮 decide 再判；
- 幻觉工具名在批中 → 同上截断；
- 去重：与首步/已执行步同指纹的同批调用只留一个；
- 脏参数（非声明槽位键）剔除但**不截断**后续干净只读；
- MAX_STEPS 预算：同批采纳不许越过步数硬上界；
- 失败不连坐：同批某只读步失败，其余独立只读步照常执行并如实记录。
- 2026-08-20（依赖占位批量计划 v2）：占位接地续步（compare/compat/
  fair/cite.export 的矩阵槽带 `$<N>.top[<i>].dataset_uid`）放行进批——写动词 cite.export
  带占位也可进批（无占位照旧截断，见 test_write_cite_export_without_placeholder_truncates）；
  写库动词/回滚/换线永不进批；矩阵外流向截断整尾（端到端钉在 test_agent_batch_plan.py）。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：多调用批量测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-batch-test")

UTTER_RO = "检查ArrayExpress和ENCODE有没有更新，完了看看库里多少条"
UTTER_RO3 = "检查10x、ArrayExpress和ENCODE有没有更新，完了看看库里多少条"
UTTER_MIX = "检查ArrayExpress和ENCODE有没有更新，若有新的人类肺数据就搜来入库，完了看看库里多少条"

AE_TWO_NEW = [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
               "local_count": 10, "online_recent": 12, "new_count": 2,
               "new_candidates": [{"accession": "E-MTAB-1", "title": "human lung atlas"},
                                  {"accession": "E-MTAB-2", "title": "human lung tumor"}]}]
SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260807_curate_arrayexpress.json", "warnings": []}
_FAIL_SOURCES: set[str] = set()   # T8 用：点名这些来源的 check 假工具抛网络错


class _FakeModel:
    """bind_tools 记录档位参数并返回自身；invoke 依次弹预置项。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
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


def _batch(*calls):
    """多 tool_call 应答：[(name_or_verb, args), ...]。"""
    return AIMessage(content="", tool_calls=[
        {"name": str(name).replace(".", "_"), "args": args, "id": f"t{i + 1}"}
        for i, (name, args) in enumerate(calls)])


def _finish(report):
    return AIMessage(content="", tool_calls=[
        {"name": "finish", "args": {"completion_report": report}, "id": "tf"}])


def _checklist(*items):
    return AIMessage(content=json.dumps([
        {"text": t, "anchor": a, "expect_verb": v} for t, a, v in items
    ], ensure_ascii=False))


def _plan(utterance, model):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model)


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _fake_tools(monkeypatch):
    """假注册表（替身形状同真表）：check 返两条疑似新增（_FAIL_SOURCES 里的来源抛网络错）、
    search 返两条入库、db_status 返空库快照。"""
    _FAIL_SOURCES.clear()

    def _check_run(slots, root):
        if str((slots or {}).get("source") or "") in _FAIL_SOURCES:
            raise RuntimeError("网络抖动")
        return {"checked_at": "t", "sources": AE_TWO_NEW, "hint_zh": ""}

    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {
            "run": _check_run,
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True},
        "curate.search_online": {
            "run": lambda slots, root: dict(SEARCH_OK),
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False},
        "curate.sync_updates": {
            "run": lambda slots, root: {"checked_at": "t", "sources": [], "imported_total": 0,
                                        "skipped": 0, "failures": [], "hint_zh": ""},
            "label_zh": "检查更新并同步入库", "card_kind": "sync_updates", "readonly": False},
        "curate.db_status": {
            "run": lambda slots, root: {
                "generated_at": "t", "sources": [], "total_records": 0,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
    })
    yield
    _FAIL_SOURCES.clear()


def _nodes(trace, node):
    return [t for t in trace if t["node"] == node]


_Q3 = "检查ArrayExpress和ENCODE有没有更新"


# ---------------------------------------------------------------- 同批采纳（全只读批量）

def test_readonly_batch_executes_together():
    """decide 一次回 [check(ENCODE), db_status]（全只读白名单）→ 两个同批执行：steps 全量
    快照 3 步且顺序不变、每调用一条 execute trace、只再花一轮 decide（finish）——
    对照旧策需 3 轮 decide。trace 如实留痕「一次给了 2 个调用…同批采纳执行」。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE",
                                         "confidence": "high", "reason": "查ENCODE"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条",
                                     "confidence": "high", "reason": "报数"})),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：已做（第2步）。\n"
                "3. 看库里多少条：已做（第3步）。"),
    )
    plan, trace = _plan(UTTER_RO, model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.check_updates", "curate.db_status"]
    assert [s["slots"].get("source") for s in plan["steps"][:2]] == ["ArrayExpress", "ENCODE"]
    decides = _nodes(trace, "decide")
    assert len(decides) == 2, "同批采纳后只需再一轮 decide（旧策要 3 轮）"
    assert "一次给了 2 个调用" in decides[0]["detail"]
    assert "1 个只读且互相独立，同批采纳执行" in decides[0]["detail"]
    assert len(_nodes(trace, "execute")) == 3, "每个被采纳的调用各自一条 execute trace"
    validates = _nodes(trace, "validate")
    assert any("同批另有 1 个只读续步一并过检" in v["detail"] for v in validates)
    assert plan.get("observation") is not None, "db_status 在批中执行仍挂 plan.observation 契约"


# ---------------------------------------------------------------- 混批回退（写动词截断）

def test_write_in_batch_truncates_tail():
    """批 = [check(ENCODE), search_online(写), db_status]：首步照常执行；写动词出现即截断——
    其后的 db_status（报数可能在写给后）**整尾回炉**，下一轮 decide 再判；报数最终落在
    入库之后（顺序语义保住）。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE",
                                         "confidence": "high", "reason": "查ENCODE"}),
               ("curate.search_online", {"quoted": "若有新的人类肺数据就搜来入库",
                                         "keywords": "人类肺", "source": "ArrayExpress"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _tool_call("curate.search_online", quoted="若有新的人类肺数据就搜来入库",
                   keywords="人类肺", source="ArrayExpress"),
        _tool_call("curate.db_status", quoted="完了看看库里多少条"),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：已做（第2步）。\n"
                "3. 搜来入库：已做（第3步）。\n"
                "4. 看库里多少条：已做（第4步）。"),
    )
    plan, trace = _plan(UTTER_MIX, model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.check_updates",
                     "curate.search_online", "curate.db_status"], (
            "写动词及其后的只读都必须回炉重判——报数落在入库之后")
    decides = _nodes(trace, "decide")
    assert len(decides) == 4, "回炉的写与报数各占一轮 decide（旧策语义不变）"
    assert "按顺序先执行第一个" in decides[0]["detail"], "零采纳时留痕措辞与旧版逐位一致"


def test_write_first_in_batch_adopts_nothing_extra():
    """批 = [db_status, search_online(写)]：首步 db_status 执行；写（在第 2 位）永不进批——
    回炉后由下一轮 decide 重新提议、照常执行（写操作本身不丢，只是不给批量偷跑）。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新", source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.db_status", {"quoted": "看看库里多少条"}),
               ("curate.search_online", {"quoted": "有新的人类肺数据就联网搜来入库",
                                         "keywords": "人类肺", "source": "ArrayExpress"})),
        _tool_call("curate.search_online", quoted="有新的人类肺数据就联网搜来入库",
                   keywords="人类肺", source="ArrayExpress"),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 看库里多少条：已做（第2步）。\n"
                "3. 搜来入库：已做（第3步）。"),
    )
    plan, trace = _plan("看看库里多少条；检查ArrayExpress更新，有新的人类肺数据就联网搜来入库",
                        model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.db_status", "curate.search_online"]
    decides = _nodes(trace, "decide")
    assert len(decides) == 3
    assert "按顺序先执行第一个" in decides[0]["detail"]


# ---------------------------------------------------------------- 幻觉名截断 / 去重 / 脏参数

def test_hallucinated_name_in_batch_truncates_tail():
    """批 = [check(ENCODE), 幻觉工具名, db_status]：幻觉名视同截断点——其后只读也回炉
    （ hallucinated 之后的状态假设不可信），下一轮 decide 再提议 db_status。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE"}),
               ("curate_teleport", {"quoted": _Q3, "source": "MARS"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _tool_call("curate.db_status", quoted="完了看看库里多少条"),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：已做（第2步）。\n"
                "3. 看库里多少条：已做（第3步）。"),
    )
    plan, trace = _plan(UTTER_RO, model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.check_updates", "curate.db_status"]
    decides = _nodes(trace, "decide")
    assert len(decides) == 3, "幻觉名之后的 db_status 回炉，多花一轮 decide 重提"
    assert "按顺序先执行第一个" in decides[0]["detail"]


def test_duplicate_calls_in_batch_run_once():
    """批 = [check(ENCODE), check(ENCODE)（与首步同指纹）, db_status]：重复调用只执行一次，
    不同指纹的 db_status 照常同批。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE"}),
               ("curate.check_updates", {"quoted": _Q3, "source": "ENCODE"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：已做（第2步）。\n"
                "3. 看库里多少条：已做（第3步）。"),
    )
    plan, trace = _plan(UTTER_RO, model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.check_updates", "curate.db_status"], (
        "同指纹调用只执行一次（3 步而非 4 步）")
    detail = _nodes(trace, "decide")[0]["detail"]
    assert "一次给了 3 个调用" in detail and "1 个只读且互相独立，同批采纳执行" in detail
    assert "其余 1 个回炉再判" in detail


def test_dirty_args_dropped_without_truncating():
    """批 = [check(AE), check(ENCODE) 带 keywords 脏参数, db_status]：脏参数调用剔除
    （回炉重判）但**不截断**——其后的干净 db_status 照常同批；被剔的 ENCODE 检查由
    下一轮 decide 干净重提后执行。"""
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x、ArrayExpress和ENCODE有没有更新",
                   source="10x Genomics", confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": "检查10x、ArrayExpress和ENCODE有没有更新",
                                         "source": "ArrayExpress"}),
               ("curate.check_updates", {"quoted": "检查10x、ArrayExpress和ENCODE有没有更新",
                                         "source": "ENCODE", "keywords": "human lung"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _tool_call("curate.check_updates", quoted="检查10x、ArrayExpress和ENCODE有没有更新",
                   source="ENCODE"),
        _finish("1. 检查10x更新：已做（第1步）。\n"
                "2. 检查ArrayExpress更新：已做（第2步）。\n"
                "3. 看库里多少条：已做（第3步）。\n"
                "4. 检查ENCODE更新：已做（第4步）。"),
    )
    plan, trace = _plan(UTTER_RO3, model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.check_updates",
                     "curate.db_status", "curate.check_updates"], (
        "脏参数的 ENCODE 不当批执行（最后一轮才干净执行）；db_status 不受影响同批")
    sources = [s["slots"].get("source") for s in plan["steps"] if s["verb"] == "curate.check_updates"]
    assert sources == ["10x Genomics", "ArrayExpress", "ENCODE"], (
        "脏参数的 ENCODE 不当批执行（第三轮才干净执行），db_status 不受影响同批")
    # db_status 在第 3 位（ENCODE 干净重提之前）——只读独立步的顺序语义不依赖被剔步
    assert plan["steps"][2]["verb"] == "curate.db_status"
    detail = _nodes(trace, "decide")[0]["detail"]
    assert "1 个只读且互相独立，同批采纳执行" in detail
    assert "其余 1 个回炉再判" in detail


# ---------------------------------------------------------------- 预算 / 失败不连坐

def test_write_cite_export_without_placeholder_truncates():
    """写动词截断系列补位：cite.export 是写动词（引文落盘）——**无占位**
    在批中照旧截断整尾（其后 db_status 也可能写给后状态，回炉重判），与的 search_online
    写动词同口径；**矩阵内有占位**才放行（见下一条）。"""
    calls = [
        {"name": "rank", "args": {"query": "人类肺"}},
        {"name": "cite_export", "args": {"quoted": "导出第一条引文"}},
        {"name": "curate_db_status", "args": {"quoted": "完了看看库里多少条"}},
    ]
    accepted, dropped = agent_exec._batch_readonly_extras(
        calls, {"verb": "rank", "query": "人类肺"},
        {"steps": [], "utterance": "搜人类肺数据，导出第一条引文"})
    assert accepted == [], "无占位的写动词（cite.export）不进批"
    assert dropped == 2, "cite.export 与其后的 db_status 整尾回炉"


def test_placeholder_grounded_cite_export_enters_batch(monkeypatch):
    """矩阵内占位放行：cite.export（uids=[\"$1.top[0].dataset_uid\"]) 随
    rank 主步进批（写动词**带占位接地**即可进批——v2 与只读白名单的关键差别）。"""
    table = dict(agent_exec.LOOP_TOOLS)
    table["cite.export"] = {
        "run": lambda slots, root: {"n_datasets": 1, "uids": [], "files": [],
                                    "out_dir": "", "note_zh": ""},
        "label_zh": "导出引文", "card_kind": "cite_export", "readonly": False,
        "needs_context": True,
    }
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", table)
    calls = [
        {"name": "rank", "args": {"query": "人类肺"}},
        {"name": "cite_export",
         "args": {"quoted": "导出第一条引文", "uids": ["$1.top[0].dataset_uid"]}},
    ]
    accepted, dropped = agent_exec._batch_readonly_extras(
        calls, {"verb": "rank", "query": "人类肺"},
        {"steps": [], "utterance": "搜人类肺数据，导出第一条引文"})
    assert dropped == 0, "占位接地的 cite.export 不截断"
    assert len(accepted) == 1 and accepted[0]["verb"] == "cite.export"
    assert accepted[0]["uids"] == ["$1.top[0].dataset_uid"], "原始引用形态原样暂存"
    assert accepted[0]["_batch_pos"] == 2, "批内序号标定（execute 解析源定位用）"


def test_batch_respects_max_steps_budget(monkeypatch):
    """MAX_STEPS=3 时批 = [check(ENCODE), check(10x), db_status]：首步占 1、预算余 1——
    c1 走主路径、check(10x) 恰好顶满预算同批采纳，db_status 回炉；步数硬上界不被
    批量消费越过（随后 decide 照常强制停环，db_status 始终没执行）。"""
    monkeypatch.setattr(agent_exec, "MAX_STEPS", 3)
    utter = "检查ArrayExpress和ENCODE有没有更新，10x也看看，完了看看库里多少条"
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE"}),
               ("curate.check_updates", {"quoted": "10x也看看", "source": "10x"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
    )
    plan, trace = _plan(utter, model)
    verbs = [s["verb"] for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates"] * 3, "预算顶满即止：db_status 不得越过硬上界"
    decides = _nodes(trace, "decide")
    assert "其余 1 个回炉再判" in decides[0]["detail"]
    assert any("已连续执行 3 步" in d["detail"] for d in decides), "预算硬上界照常强制停环"


def test_failed_readonly_step_does_not_cascade():
    """同批某只读步失败（网络抖动）不连坐：其余独立只读步照常执行、各自如实记 ok；
    decide 带全量真实结果再判（失败步 ok=False 进实录）。"""
    _FAIL_SOURCES.add("ENCODE")
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=_Q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": _Q3, "source": "ENCODE"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：做不到（据第2步的真实结果：网络抖动）。\n"
                "3. 看库里多少条：已做（第3步）。"),
    )
    plan, trace = _plan(UTTER_RO, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == [
        "curate.check_updates", "curate.check_updates", "curate.db_status"]
    assert [s["ok"] for s in steps] == [True, False, True], "失败不连坐：独立只读步照常执行"
    assert len(_nodes(trace, "execute")) == 3


def test_batch_breaker_recuts_between_extras(monkeypatch):
    """2026-08-15 **批内熔断**：批 = [check（ENCODE),
    check(HCA), check(10x), db_status]，前两个联网二连败 -> 联网暂停即刻生效——
    第三个联网 extra（10x）**不执行、不记步**（初筛对批前状态，不重过就是盲飞）；
    离线 db_status 不连坐照常执行。缺口由 decide 带新状态再判（finish 先被 pending 硬闸
    否决一次——10x 点名源没处理过——补交代后收尾）。"""
    # 本文件夹具不 stub 清单轻量调用——本测试必须 stub（否则它会消耗一条 FakeModel 应答）。
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))

    def _check_run(slots, root):
        src = str((slots or {}).get("source") or "")
        if src in ("ENCODE", "HCA"):
            err = RuntimeError("network_error: 网络抖动")
            err.code = "network_error"
            err.hint = "网络抖动"
            raise err
        return {"checked_at": "t", "sources": [], "hint_zh": ""}

    table = dict(agent_exec.LOOP_TOOLS)
    table["curate.check_updates"] = {**table["curate.check_updates"], "run": _check_run}
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", table)

    q4 = "检查ArrayExpress、ENCODE、HCA和10x有没有更新"
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=q4, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": q4, "source": "ENCODE"}),
               ("curate.check_updates", {"quoted": q4, "source": "HCA"}),
               ("curate.check_updates", {"quoted": q4, "source": "10x"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：做不到（据第2步的真实结果：网络失败）。\n"
                "3. 检查HCA更新：做不到（据第3步的真实结果：网络失败）。\n"
                "4. 看库里多少条：已做（第4步）。"),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 检查ENCODE更新：做不到（据第2步的真实结果：网络失败）。\n"
                "3. 检查HCA更新：做不到（据第3步的真实结果：网络失败）。\n"
                "4. 检查10x更新：做不到（据第2、3步的真实结果：联网二连败暂停）。\n"
                "5. 看库里多少条：已做（第4步）。"),
    )
    plan, trace = _plan(q4 + "，完了看看库里多少条", model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == [
        "curate.check_updates", "curate.check_updates", "curate.check_updates",
        "curate.db_status"]
    assert [s["ok"] for s in steps] == [True, False, False, True]
    assert [s["slots"].get("source") for s in steps[:3]] == [
        "ArrayExpress", "ENCODE", "HCA"], "10x 被批内熔断拦下、不产生步骤"
    # 熔断剔步「留痕不留步」——步骤照旧不产生（上行钉不变）
    # 但 execute trace 必须如实交代这一步为什么没跑（原钉"连 execute trace 都不产生"
    # 是刻意行为的旧口径，现为观测缺口已修）。
    exec_nodes = _nodes(trace, "execute")
    assert len(exec_nodes) == 5, "3 条真跑 + 1 条批内熔断留痕 + 1 条 db_status"
    breaker = [t for t in exec_nodes if str(t["label_zh"]).startswith("批内熔断")]
    assert len(breaker) == 1 and breaker[0]["ok"] is False
    assert "联网暂停" in breaker[0]["detail"] and "下一轮" in breaker[0]["detail"]
    decides = _nodes(trace, "decide")
    assert "一次给了 4 个调用" in decides[0]["detail"]


def test_batch_breaker_ban_branch_leaves_trace(monkeypatch):
    """批 = [check（AE), check（ENCODE), check（10x),
    db_status]，两个 check 以**非网络码**二连败 → `_failed_tool_ban` 禁提 check_updates，
    10x extra 被批内熔断——不执行、不记步（行为不变），但 execute trace 如实留痕
    「连续失败两次被禁提」（此前熔断剔步零留痕，与 decide 的"同批采纳执行"矛盾）。"""
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))

    def _check_run(slots, root):
        err = RuntimeError("bad_param: 参数不对")
        err.code = "bad_param"
        err.hint = "参数不对"
        raise err

    table = dict(agent_exec.LOOP_TOOLS)
    table["curate.check_updates"] = {**table["curate.check_updates"], "run": _check_run}
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", table)

    q3 = "检查ArrayExpress、ENCODE和10x有没有更新"
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted=q3, source="ArrayExpress",
                   confidence="high", reason="查更新"),
        _batch(("curate.check_updates", {"quoted": q3, "source": "ENCODE"}),
               ("curate.check_updates", {"quoted": q3, "source": "10x"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条"})),
        _finish("1. 检查ArrayExpress更新：做不到（据第1步的真实结果：参数不对）。\n"
                "2. 检查ENCODE更新：做不到（据第2步的真实结果：参数不对）。\n"
                "3. 看库里多少条：已做（第3步）。"),
        _finish("1. 检查ArrayExpress更新：做不到（据第1步的真实结果：参数不对）。\n"
                "2. 检查ENCODE更新：做不到（据第2步的真实结果：参数不对）。\n"
                "3. 检查10x更新：做不到（据第1、2步的真实结果：该动作被禁提）。\n"
                "4. 看库里多少条：已做（第3步）。"),
    )
    plan, trace = _plan(q3 + "，完了看看库里多少条", model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == [
        "curate.check_updates", "curate.check_updates", "curate.db_status"]
    assert [s["ok"] for s in steps] == [False, False, True], \
        "10x 被批内熔断（禁提分支）拦下、不产生步骤；db_status 不连坐"
    breaker = [t for t in _nodes(trace, "execute") if str(t["label_zh"]).startswith("批内熔断")]
    assert len(breaker) == 1 and breaker[0]["ok"] is False
    assert "连续失败两次" in breaker[0]["detail"] and "禁提" in breaker[0]["detail"]


# ---------------------------------------------------------------- understand 首步同批

_UTTER_U_BATCH = "检查ArrayExpress有没有更新，完了看看库里多少条"


def test_understand_first_step_batch_executes_together():
    """understand 首步一次回 [check(AE), db_status]（全只读白名单）→ 复制 decide 的
    raw_batch 通道：两个随首步同批执行（validate 同口径双检 → execute 逐个真跑留痕），
    decide 只再花一轮（finish）。understand trace 如实缀「随首步同批执行」。"""
    model = _FakeModel(
        _batch(("curate.check_updates", {"quoted": "检查ArrayExpress有没有更新",
                                         "source": "ArrayExpress",
                                         "confidence": "high", "reason": "查更新"}),
               ("curate.db_status", {"quoted": "完了看看库里多少条",
                                     "confidence": "high", "reason": "报数"})),
        _finish("1. 检查ArrayExpress更新：已做（第1步）。\n"
                "2. 看库里多少条：已做（第2步）。"),
    )
    plan, trace = _plan(_UTTER_U_BATCH, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["curate.check_updates", "curate.db_status"]
    assert [s["ok"] for s in steps] == [True, True]
    assert len(_nodes(trace, "execute")) == 2, "首步与同批续步各自一条 execute trace"
    understand = _nodes(trace, "understand")
    assert len(understand) == 1
    assert "一次给了 2 个调用" in understand[0]["detail"]
    assert "1 个只读且互相独立，随首步同批执行" in understand[0]["detail"]
    validates = _nodes(trace, "validate")
    assert any("同批另有 1 个只读续步一并过检" in v["detail"] for v in validates), (
        "validate 对 understand 产的 raw_batch 同口径双检")
    assert len(_nodes(trace, "decide")) == 1, "同批采纳后 decide 只需一轮（finish）"
    assert plan.get("observation") is not None, "db_status 同批执行仍挂 plan.observation 契约"


def test_understand_first_step_batch_write_truncates_tail():
    """understand 首步批 = [db_status, search_online(写)]：**写步仍单发**——首步 db_status
    照常执行，写动词（第 2 位）截断回炉，下一轮 decide 干净重提后执行（写操作不丢）。"""
    utter = "看看库里多少条；若有新的人类肺数据就联网搜来入库"
    model = _FakeModel(
        _batch(("curate.db_status", {"quoted": "看看库里多少条"}),
               ("curate.search_online", {"quoted": "若有新的人类肺数据就联网搜来入库",
                                         "keywords": "人类肺", "source": "ArrayExpress"})),
        _tool_call("curate.search_online", quoted="若有新的人类肺数据就联网搜来入库",
                   keywords="人类肺", source="ArrayExpress"),
        _finish("1. 看库里多少条：已做（第1步）。\n"
                "2. 联网搜来入库：已做（第2步）。"),
    )
    plan, trace = _plan(utter, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["curate.db_status", "curate.search_online"], (
        "写动词不当批偷跑：首步只执行 db_status，search_online 回炉后由 decide 重提执行")
    assert [s["ok"] for s in steps] == [True, True]
    understand = _nodes(trace, "understand")
    assert len(understand) == 1
    assert "随首步同批执行" not in understand[0]["detail"], "零采纳时不缀同批留痕"
    assert len(_nodes(trace, "decide")) == 2, "回炉的写步占一轮 decide（重提）+ 一轮 finish"
