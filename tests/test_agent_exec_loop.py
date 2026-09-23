# -*- coding: utf-8 -*-
"""agent 长程多步执行（2026-08-04 设计文档）的确定性门。**全离线**：

- fake chat_model + monkeypatch LOOP_TOOLS 驱动整个循环（execute → decide → validate → execute）；
- 项目根重定向到 tmp_path：审计账本**真写真断言**（.userdata/curate_net_ledger.jsonl），
  绝不碰真实库；
- 钉死：① check→search 两步链；② 0 新增 → decide done；③④ decide 非法输出 fail-safe 停环；
  ⑤ MAX_STEPS 机械上界；⑥ 工具失败 ok=False 如实记 + 如实汇报；⑦ db_status 零回归；
  ⑧ 非 loop 动词不进 execute、plan.steps 缺席。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：多步循环测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-loop-test")


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


def _plan(utterance, model):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    """图内工具/审计的项目根重定向到 tmp——账本真写真断言，绝不污染真实库。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    # 清单轻量调用统一 stub（本文件钉的是循环行为，不是清单——FakeModel 的
    # 应答序列不预置清单应答；清单自身的端到端钉在 test_agent_decide_routing.py，
    # 那边测试体内后执行的 setattr 会覆盖本 stub）。
    monkeypatch.setattr(agent_exec, "_task_checklist_call",
                        lambda *a, **k: ([], 0, ""))
    return tmp_path


def _ledger_rows(root):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _check_run(sources_out):
    def run(slots, root):
        return {"checked_at": "2026-08-04T00:00:00+08:00", "sources": sources_out, "hint_zh": ""}
    return run


def _search_run(result):
    def run(slots, root):
        return dict(result)
    return run


def _search_run_unregistered(slots, root):
    """未注册源 fail-fast 替身（零网络）：2026-08-08 起 10x 已接入联网适配器（W2e），
    真 `_loop_search_online` 对 10x 不再 fail-fast——agent 层要钉的是「工具抛
    source_not_registered → step ok=False 如实记」的处置路径，不再绑定「10x 未注册」
    这个会漂移的世界事实（fail-fast 本身的语义钉在 corpus 层测试）。
    str 形态随 CurateError 契约「code: hint」（账本行记的是 str(exc)）。"""
    err = RuntimeError("source_not_registered: 暂不支持联网搜索来源 10x")
    err.code = "source_not_registered"
    err.hint = "暂不支持联网搜索来源 10x"
    raise err


def _install_tools(monkeypatch, **runs):
    """按真动词名替换 LOOP_TOOLS 的 run（label/card_kind/只读标记保持真实形状）。"""
    table = {
        "curate.db_status": {"label_zh": "汇报数据库状态", "card_kind": "db_status",
                             "readonly": True, "report": True, "observation": True},
        "curate.check_updates": {"label_zh": "检查来源更新", "card_kind": "check_updates",
                                 "readonly": True},
        "curate.search_online": {"label_zh": "联网搜索入库", "card_kind": "search_online",
                                 "readonly": False},
    }
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        verb: {"run": runs[verb], **meta} for verb, meta in table.items() if verb in runs
    })


AE_TWO_NEW = [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
               "local_count": 10, "online_recent": 12, "new_count": 2,
               "new_candidates": [{"accession": "E-MTAB-1", "title": "human lung atlas"},
                                  {"accession": "E-MTAB-2", "title": "human lung tumor"}]}]

SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260804_curate_arrayexpress.json", "warnings": []}


# ---------------------------------------------------------------- ① check → search 两步链

def test_check_then_search_two_step_chain(monkeypatch, _tmp_project_root):
    """「检查…若有…就联网搜来入库」：check 真跑 → decide 提出 search → validate → execute 真跑
    → decide done → narrate。plan.steps 长度 2、两步皆 ok，plan.verb 仍是首步动词，
    账本两行（每步一行审计）。"""
    _install_tools(monkeypatch,
                   **{"curate.check_updates": _check_run(AE_TWO_NEW),
                      "curate.search_online": _search_run(SEARCH_OK)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺", "species": "人类"},
            ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，已联网搜到 2 条并入库。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库", model)
    assert plan["verb"] == "curate.check_updates", "plan.verb 恒为首步动词（前端契约不炸）"
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [
        ("curate.check_updates", True), ("curate.search_online", True),
    ]
    assert steps[0]["card_kind"] == "check_updates" and steps[1]["card_kind"] == "search_online"
    assert steps[1]["result"]["filename"] == SEARCH_OK["filename"]
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "execute", "decide",
        "validate", "execute", "decide", "narrate",
    ]
    assert plan.get("report_zh") == "检查到 2 条疑似新增，已联网搜到 2 条并入库。"
    rows = _ledger_rows(_tmp_project_root)
    assert [r["endpoint"] for r in rows] == [
        "agent_exec:curate.check_updates", "agent_exec:curate.search_online",
    ], "每个真跑的工具都要落一行审计"
    assert all(r["ok"] for r in rows) and all(r["ts"] for r in rows)


# ---------------------------------------------------------------- ② 0 新增 → decide done

def test_zero_new_means_done_and_report_says_no_update(monkeypatch, _tmp_project_root):
    """「若有则…」的条件语义：check 结果 new_count=0 → decide done（不再发起第二步）→
    steps 长度 1，汇报如实说「没有更新」。"""
    zero = [dict(AE_TWO_NEW[0], new_count=0, new_candidates=[])]
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(zero)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="ArrayExpress 没有更新，不需要下载。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    steps = plan.get("steps") or []
    assert len(steps) == 1 and steps[0]["verb"] == "curate.check_updates"
    assert "没有更新" in (plan.get("report_zh") or "")
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "execute", "decide", "narrate"]


def test_zero_new_done_also_works_without_llm_report(monkeypatch, _tmp_project_root):
    """decide done 后 narrate 的 LLM 也缺席 → 确定性兜底同样如实说「没有疑似新增」。"""
    zero = [dict(AE_TWO_NEW[0], new_count=0, new_candidates=[])]
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(zero)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
        # narrate 没有第三个 answer → IndexError → 确定性兜底
    )
    plan, _ = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    assert len(plan.get("steps") or []) == 1
    assert "没有疑似新增" in (plan.get("report_zh") or "")


# ---------------------------------------------------------------- ③ decide 选白名单外动词 → 停环

def test_decide_with_verb_outside_loop_tools_stops_the_loop(monkeypatch, _tmp_project_root):
    """decide 提议 pack.download（词表内但**不在 LOOP_TOOLS**）→ 当 done 停环、绝不执行，
    trace 如实记一笔；steps 只有首步。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "pack.download", "quoted": "下载下来", "limit": 5}, ensure_ascii=False)),
        AIMessage(content="检查到 2 条疑似新增；下载这一步没有在图内做。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"]
    decide_entries = [t for t in trace if t["node"] == "decide"]
    assert len(decide_entries) == 1 and "不在允许自动执行的范围" in decide_entries[0]["detail"]
    assert len(_ledger_rows(_tmp_project_root)) == 1, "停环后绝不多跑工具"


# ---------------------------------------------------------------- ④ decide 输出非法 JSON → 重问一次仍非法 → 停环

def test_decide_with_unparsable_output_stops_the_loop(monkeypatch, _tmp_project_root):
    """decide 散文应答 → 非法 → 重问一次→
    重问应答仍是散文 → 照旧停环；steps 只有首步。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="我觉得应该再检查一下（这不是 JSON）"),
        AIMessage(content="检查到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    assert len(plan.get("steps") or []) == 1
    decide_entries = [t for t in trace if t["node"] == "decide"]
    assert len(decide_entries) == 1 and "能读懂" in decide_entries[0]["detail"]


def test_decide_repeat_of_an_executed_step_stops_the_loop(monkeypatch, _tmp_project_root):
    """decide 提议与已**成功**执行步骤 verb+slots 全同 → 机械去重拦下，按 done 停环
    （2026-08-08 起去重闸只比 ok 步——失败步的同指纹重试放行，见
    tests/test_agent_failure_semantics.py 第 4 节）。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ArrayExpress是否有更新",
             "source": "ArrayExpress"}, ensure_ascii=False)),
        AIMessage(content="检查到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    assert len(plan.get("steps") or []) == 1
    assert any("重复" in t["detail"] for t in trace if t["node"] == "decide")


# ---------------------------------------------------------------- sync 主题闸（2026-08-08 磁吸族）

def test_sync_topic_gate_matrix():
    """对拍钉：`_validate_raw` 对 sync_updates + 原话含主题维度值别名（物种/组织/疾病/技术，
    复用 `vocabulary.CATALOG` × `query_parser._alias_occurrences` 真源）→ 机械违规；
    无主题词 / 来源名 / 平台名 → 放行（sync 是不限定主题条件下载的正解，既有划界不回归）。"""
    blocked = [
        ("检查ArrayExpress更新，有新的人类肺数据就搜来入库", "有新的人类肺数据就搜来入库"),
        ("联网搜 human lung 数据入库，然后检查ENCODE更新", "联网搜 human lung 数据入库"),
        ("检查更新，有新的肺癌数据就同步进来", "有新的肺癌数据就同步进来"),
        ("检查更新，有新的 ATAC 数据就同步", "有新的 ATAC 数据就同步"),
    ]
    for utter, quoted in blocked:
        violations = agent_exec._validate_raw(
            {"verb": "curate.sync_updates", "quoted": quoted}, utter)
        assert any("限定了主题" in v for v in violations), utter
    allowed = [
        ("检查ArrayExpress更新并同步入库", "同步入库"),
        ("看看有没有什么新数据，有的话拿回来", "有的话拿回来"),
        ("帮我 check 一下 ENCODE 有没有 new data，有就 download 下来", "有就 download 下来"),
        ("检查10x更新并同步入库", "同步入库"),          # 来源名不算主题
        ("同步一下 Visium 的更新", "Visium 的更新"),      # 平台名不算主题（防撞 10x 来源名）
    ]
    for utter, quoted in allowed:
        violations = agent_exec._validate_raw(
            {"verb": "curate.sync_updates", "quoted": quoted}, utter)
        assert not any("限定了主题" in v for v in violations), (utter, violations)


def test_sync_topic_gate_pushes_repair_to_check(monkeypatch, _tmp_project_root):
    """全链路（b12 族）：understand 磁吸选 sync → 主题闸拦下 → repair 改判 check →
    正常执行；sync 绝不进 execute（它不过滤主题，会把所有疑似新增都入库）。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.sync_updates", quoted="检查ArrayExpress更新", source="ArrayExpress",
                   confidence="high", reason="检查并同步"),
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新", source="ArrayExpress",
                   confidence="high", reason="限定主题先检查"),  # repair 改判
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n"
            "2. 搜人类肺入库：没做——条件不成立（第1步结果显示无新增）")),
        AIMessage(content="已检查更新。"),
    )
    plan, trace = _plan("检查ArrayExpress更新，有新的人类肺数据就搜来入库", model)
    assert plan["verb"] == "curate.check_updates", "repair 后必须改判 check（sync 被主题闸拦下）"
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "repair", "validate", "execute", "decide", "narrate"]
    assert "限定了主题" in trace[2]["detail"] and trace[2]["ok"] is False  # 索引随环首 +1
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"]
    assert len(_ledger_rows(_tmp_project_root)) == 1, "sync 绝不执行（只跑了只读 check）"


# ------------------------------------------------- sync 主题闸分句作用域（坐实）

def test_sync_topic_gate_branch_scoped_messages():
    """k11 问题：「检查CELLxGENE更新，有新增就同步，然后检查下ArrayExpress，有新的人类肺数据
    就搜来入库」——主题词「人类」属于**后面 ArrayExpress 分支**，旧版全域扫描拿它拦
    CELLxGENE 分支的 sync，理由张冠李戴把 repair 逼进死胡同（AgentPlanInvalid 0/3）。
    现按分句作用域 + 别支豁免疫苗：主题在 sync 所引片段内 → 消息 A（旧文案）；主题在别支
    且四条件齐备（本支有同步意图/填了 source/主题归属另一支）→ 放行；条件不齐 → 消息 B。"""
    k11 = "检查CELLxGENE更新，有新增就同步，然后检查下ArrayExpress，有新的人类肺数据就搜来入库，最后看看库里多少条"
    # 别支豁免：CELLxGENE 分支自带「同步」意图、填了 source、主题归属 ArrayExpress 支 → 放行
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "检查CELLxGENE更新，有新增就同步",
         "source": "CELLxGENE Discover"}, k11)
    assert not any("主题" in x for x in v), v
    # 主题支内的 sync 照拦（sync 的 source 正是主题归属支 ArrayExpress）→ 消息 B
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "有新增就同步", "source": "ArrayExpress"}, k11)
    assert any("不会按主题过滤" in x for x in v), v
    # 不带 source 的全量 sync 永不豁免 → 消息 B
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "检查CELLxGENE更新，有新增就同步"}, k11)
    assert any("不会按主题过滤" in x for x in v), v
    # 纯检查分支被误选 sync（作用域无同步意图）不豁免 → 消息 B（g04 假设例）
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "再检查一遍ENCODE", "source": "ENCODE"},
        "检查ArrayExpress更新，有新的人类肺数据就搜来入库，然后再检查一遍ENCODE，完了告诉我库里多少条")
    assert any("不会按主题过滤" in x for x in v), v
    # 主题在 sync 所引片段内 → 消息 A（b12 族真阳性逐位保留）
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "有新的人类肺数据就搜来入库"},
        "检查ArrayExpress更新，有新的人类肺数据就搜来入库")
    assert any(x.startswith("原话限定了主题") for x in v), v
    # 条件回指：主题在上一句、sync 在「有的话…」句 → 作用域前扩，仍按消息 A 拦
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "有的话同步回来"},
        "检查一下有没有新的人类肺数据，有的话同步回来")
    assert any(x.startswith("原话限定了主题") for x in v), v
    # quoted 对不上原话 → 退回整句（fail-closed，旧行为）
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "不存在的片段"},
        "检查更新，有新的肺癌数据就同步进来")
    assert any(x.startswith("原话限定了主题") for x in v), v
    # 全句无主题 → 两路都静默（b09/f05/d05 族不回归）
    v = agent_exec._validate_raw(
        {"verb": "curate.sync_updates", "quoted": "有的话拿回来"},
        "看看ArrayExpress有没有新数据，有的话拿回来")
    assert not any("主题" in x for x in v), v


# ------------------------------------------------- 点名源闸：受控规范名逐字也算点名（b08 坐实）

def test_named_source_verbatim_canonical_counts_as_named():
    """b08 问题：「检查一下ENCODE有没有更新，顺便看看库里多少条」——词表刻意不收裸 encode
    （普通英文动词），旧版 `spans 空 → 放行`，understand 槽位落空后无闸可拦：白跑一遍全量
    检查、decide 续步再查 ENCODE，顶超 max_steps。现逐字规范名（全大写 ENCODE）算点名，
    缺槽位 → violation 走 repair 补 source（消息须明示「动词本身不用换」——k11 坐实
    repair 会把 check 改判 sync 撞主题闸）。小写 encode（普通英文词）绝不认。"""
    utter = "检查一下ENCODE有没有更新，顺便看看库里多少条"
    v = agent_exec._named_source_violation("curate.check_updates", {}, utter)
    assert v is not None and "ENCODE" in v and "动词本身不用换" in v, v
    # 填了规范名 → 放行（豁免语义吸收进点名判定）
    assert agent_exec._named_source_violation(
        "curate.check_updates", {"source": "ENCODE"}, utter) is None
    # 填错来源 → 照拦
    assert agent_exec._named_source_violation(
        "curate.check_updates", {"source": "HuBMAP"}, utter) is not None
    # 小写 encode 是普通英文动词——不认点名，缺槽位也不拦
    assert agent_exec._named_source_violation(
        "curate.check_updates", {}, "factors that encode proteins") is None


def test_autofill_named_source_from_quoted():
    """b08/k11 连根修复：quoted 逐字点名唯一来源 + source 缺槽位 = 确定解补位（绕过
    repair 扯皮链）。quoted 没点名/点名多个/槽位已填/非点名动词 → 一律不动（交回
    violation 通道或保持原样）。"""
    # b08：quoted 含全大写 ENCODE（逐字规范名）→ 补 ENCODE
    raw = {"verb": "curate.check_updates", "quoted": "检查一下ENCODE有没有更新"}
    filled = agent_exec._autofill_named_source(
        "curate.check_updates", raw, "检查一下ENCODE有没有更新，顺便看看库里多少条")
    assert filled == "ENCODE" and raw["source"] == "ENCODE", (filled, raw)
    # k11：quoted 含 cellxgene（别名）→ 补受控规范名
    raw = {"verb": "curate.check_updates", "quoted": "检查CELLxGENE更新"}
    filled = agent_exec._autofill_named_source(
        "curate.check_updates", raw,
        "检查CELLxGENE更新，有新增就同步，然后检查下ArrayExpress，有新的人类肺数据就搜来入库")
    assert filled == "CELLxGENE Discover" and raw["source"] == "CELLxGENE Discover", (filled, raw)
    # quoted 点名两个来源 → 歧义不补（交 violation 通道）
    raw = {"verb": "curate.check_updates", "quoted": "检查10x和ArrayExpress有没有更新"}
    assert agent_exec._autofill_named_source(
        "curate.check_updates", raw, "先检查10x和ArrayExpress有没有更新") is None
    assert "source" not in raw
    # quoted 没点名（原话别处点名了）→ 不补
    raw = {"verb": "curate.check_updates", "quoted": "检查一下有没有更新"}
    assert agent_exec._autofill_named_source(
        "curate.check_updates", raw, "检查一下有没有更新，顺便看看ENCODE") is None
    # 槽位已填 → 不动
    raw = {"verb": "curate.check_updates", "quoted": "检查一下ENCODE", "source": "ENCODE"}
    assert agent_exec._autofill_named_source(
        "curate.check_updates", raw, "检查一下ENCODE有没有更新") is None
    # 非点名动词（sync_updates）→ 不动
    raw = {"verb": "curate.sync_updates", "quoted": "有新增就同步"}
    assert agent_exec._autofill_named_source(
        "curate.sync_updates", raw, "检查ENCODE更新，有新增就同步") is None


# ------------------------------------------------- 幻觉取消镜像闸（j03 r1/r2 坐实）

def test_cancelled_true_requires_denial_morpheme():
    """j03 问题：reasoner 在毫无否定词的原话上幻觉 cancelled=true，把该跑的 search_online
    标成「你说了不做」整步取消。铁律 3 的机械镜像：原话无否定语素 + cancelled=true →
    violation 走 repair。语素表宁宽勿窄；「要不要/是不是」是征询不是叫停，刻意排除。"""
    j03 = ("帮我看看库里多少条数据然后顺便检查一下ArrayExpress有没有更新"
           "有新增的人类肺数据就搜来入库最后再告诉我一遍库里多少条")
    # 幻觉取消 → 拦
    v = agent_exec._validate_raw(
        {"verb": "curate.search_online", "quoted": "有新增的人类肺数据就搜来入库",
         "keywords": "人类肺", "cancelled": True}, j03)
    assert any("否定语素" in x for x in v), v
    # 合法取消（算了/别）→ 静默
    v = agent_exec._validate_raw(
        {"verb": "curate.search_online", "quoted": "联网搜人类肺数据入库",
         "keywords": "人类肺", "cancelled": True}, "联网搜人类肺数据入库……算了别搜了")
    assert not any("否定语素" in x for x in v), v
    # 「要不要」是征询——模型幻觉取消照样拦
    v = agent_exec._validate_raw(
        {"verb": "curate.check_updates", "quoted": "要不要检查一下ArrayExpress的更新",
         "source": "ArrayExpress", "cancelled": True},
        "要不要检查一下ArrayExpress的更新？")
    assert any("否定语素" in x for x in v), v
    # cancelled=false / 字符串「true」（parse 层只认 JSON 布尔）→ 静默
    v = agent_exec._validate_raw(
        {"verb": "curate.search_online", "quoted": "搜人类肺", "keywords": "人类肺",
         "cancelled": False}, "搜人类肺数据")
    assert not any("否定语素" in x for x in v), v
    v = agent_exec._validate_raw(
        {"verb": "curate.search_online", "quoted": "搜人类肺", "keywords": "人类肺",
         "cancelled": "true"}, "搜人类肺数据")
    assert not any("否定语素" in x for x in v), v


# ---------------------------------------------------------------- ⑤ MAX_STEPS 机械上界

def test_max_steps_forces_done_without_asking_the_llm(monkeypatch, _tmp_project_root):
    """decide 连续提出合法新步 → 真跑满 MAX_STEPS=8 后 decide **不再调 LLM**、强制 done。
    （2026-08-08 约束放松批：上界 3→6；2026-08-15：6→8，本钉同步扩到 8 步；
    8 步里只排 1 次写步，不触发写步预算闸——那条闸有自己的专项钉。
    本钉同时是「到顶未结清→如实标注没做完」的钉：9 件诉求只跑成 8 件，
    库容步被截断 → 结算闸判未决 → 汇报缀「剩下的没有执行」。）"""
    _install_tools(
        monkeypatch,
        **{
            "curate.check_updates": _check_run(AE_TWO_NEW),
            "curate.search_online": _search_run(SEARCH_OK),
            "curate.db_status": lambda slots, root: {
                "total_records": 1, "sources": [], "external_files": [], "recycle": [],
                "ledger": {},
            },
        },
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ENCODE是否有更新",
             "source": "ENCODE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
             "source": "10x"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HCA是否有更新",
             "source": "HCA"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查CELLxGENE是否有更新",
             "source": "CELLxGENE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HuBMAP是否有更新",
             "source": "HuBMAP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查SCP是否有更新",
             "source": "SCP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        AIMessage(content="八步都做完了。"),
    )
    plan, trace = _plan(
        "检查ArrayExpress是否有更新，检查ENCODE是否有更新，检查10x是否有更新，检查HCA是否有更新，"
        "检查CELLxGENE是否有更新，检查HuBMAP是否有更新，检查SCP是否有更新，"
        "若有新的人类肺数据就联网搜来入库，然后数数库里多少条", model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.check_updates", "curate.check_updates",
        "curate.check_updates", "curate.check_updates", "curate.check_updates",
        "curate.check_updates", "curate.search_online",
    ]
    decide_entries = [t for t in trace if t["node"] == "decide"]
    assert "最多" in decide_entries[-1]["detail"]
    # understand 1 次 + decide 真发起 7 次 + narrate 1 次 = 9；第 8 次 decide 被机械上界拦下、不调 LLM
    assert len(model.invocations) == 9
    assert len(_ledger_rows(_tmp_project_root)) == 8
    # 库容步被截断（pending 结算未决）→ 到顶**不**结清，如实标注「没做完」照旧。
    assert "剩下的没有执行" in (plan.get("report_zh") or "")


def test_max_steps_settled_does_not_lie(monkeypatch, _tmp_project_root):
    """2026-08-15 到顶结算闸：恰好跑满 8 步且原话交代
    的事全部结清（6 源检查 + 条件搜索 + 库容）→ truncated 旗标本真，但汇报**不许**缀
    「剩下的没有执行」——改缀「预算刚好用完、事已做完」的机械事实。"""
    _install_tools(
        monkeypatch,
        **{
            "curate.check_updates": _check_run(AE_TWO_NEW),
            "curate.search_online": _search_run(SEARCH_OK),
            "curate.db_status": lambda slots, root: {
                "total_records": 1, "sources": [], "external_files": [], "recycle": [],
                "ledger": {},
            },
        },
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ENCODE是否有更新",
             "source": "ENCODE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
             "source": "10x"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HCA是否有更新",
             "source": "HCA"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查CELLxGENE是否有更新",
             "source": "CELLxGENE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HuBMAP是否有更新",
             "source": "HuBMAP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.db_status", "quoted": "数数库里多少条"}, ensure_ascii=False)),
        AIMessage(content="八步都做完了，库容也数了。"),
    )
    plan, trace = _plan(
        "检查ArrayExpress是否有更新，检查ENCODE是否有更新，检查10x是否有更新，检查HCA是否有更新，"
        "检查CELLxGENE是否有更新，检查HuBMAP是否有更新，"
        "若有新的人类肺数据就联网搜来入库，然后数数库里多少条", model)
    steps = plan.get("steps") or []
    assert len(steps) == 8 and steps[-1]["verb"] == "curate.db_status"
    assert len(model.invocations) == 9   # 第 8 次 decide 被上界拦下不调 LLM
    report = plan.get("report_zh") or ""
    assert "剩下的没有执行" not in report
    assert "已全部完成" in report
    decide_entries = [t for t in trace if t["node"] == "decide"]
    assert "已全部执行" in decide_entries[-1]["detail"]


def test_max_steps_settled_with_failed_step_does_not_lie(monkeypatch, _tmp_project_root):
    """到顶结算只认 ok 步——含失败步跑满 8 步时，即便 pending/清单
    两账都结清（失败步「碰过」点名源在 finish 核销语境算处理过，是刻意口径），也不许缀
    「已全部完成」——同一份汇报里明明写着 ENCODE 没查成，自相矛盾。退回旧口径如实标注。"""
    def _check_run(slots, root):
        if str((slots or {}).get("source") or "") == "ENCODE":
            err = RuntimeError("network_error: 网络抖动")
            err.code = "network_error"
            err.hint = "网络抖动"
            raise err
        return {"checked_at": "t", "sources": [], "hint_zh": ""}

    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run,
           "curate.db_status": lambda slots, root: {
               "total_records": 1, "sources": [], "external_files": [], "recycle": [],
               "ledger": {}}},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ENCODE是否有更新",
             "source": "ENCODE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
             "source": "10x"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HCA是否有更新",
             "source": "HCA"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查CELLxGENE是否有更新",
             "source": "CELLxGENE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HuBMAP是否有更新",
             "source": "HuBMAP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查SCP是否有更新",
             "source": "SCP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.db_status", "quoted": "数数库里多少条"}, ensure_ascii=False)),
        AIMessage(content="七个来源的检查都跑了，ENCODE 这次网络没连上；库里条数也数了。"),
    )
    plan, trace = _plan(
        "检查ArrayExpress是否有更新，检查ENCODE是否有更新，检查10x是否有更新，检查HCA是否有更新，"
        "检查CELLxGENE是否有更新，检查HuBMAP是否有更新，检查SCP是否有更新，"
        "完了数数库里多少条", model)
    steps = plan.get("steps") or []
    assert len(steps) == 8
    assert [s["ok"] for s in steps] == [True, False, True, True, True, True, True, True]
    report = plan.get("report_zh") or ""
    assert "已全部完成" not in report, "含失败步到顶不许谎报「已全部完成」"
    assert "剩下的没有执行" in report
    decide_entries = [t for t in trace if t["node"] == "decide"]
    assert "已全部执行" not in decide_entries[-1]["detail"]


def test_max_steps_checklist_unavailable_degrade_is_traced(monkeypatch, _tmp_project_root):
    """清单没建成（checklist_unavailable）时到顶结算
    退化为只剩 pending 三道口径——降级**口径维持现状**（结算语义待定），但 decide 的到顶 trace 必须如实标注「清单对账缺席」：understand 的
    checklist_unavailable trace 与到顶 trace 隔了整个循环，不缀明复盘时无法关联。"""
    monkeypatch.setattr(agent_exec, "_task_checklist_call",
                        lambda *a, **k: ([], 0, "BoomError"))  # 覆盖 autouse stub：清单两连败
    _install_tools(
        monkeypatch,
        **{
            "curate.check_updates": _check_run(AE_TWO_NEW),
            "curate.search_online": _search_run(SEARCH_OK),
            "curate.db_status": lambda slots, root: {
                "total_records": 1, "sources": [], "external_files": [], "recycle": [],
                "ledger": {},
            },
        },
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ENCODE是否有更新",
             "source": "ENCODE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
             "source": "10x"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HCA是否有更新",
             "source": "HCA"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查CELLxGENE是否有更新",
             "source": "CELLxGENE"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查HuBMAP是否有更新",
             "source": "HuBMAP"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        AIMessage(content=json.dumps(
            {"verb": "curate.db_status", "quoted": "数数库里多少条"}, ensure_ascii=False)),
        AIMessage(content="八步都做完了，库容也数了。"),
    )
    plan, trace = _plan(
        "检查ArrayExpress是否有更新，检查ENCODE是否有更新，检查10x是否有更新，检查HCA是否有更新，"
        "检查CELLxGENE是否有更新，检查HuBMAP是否有更新，"
        "若有新的人类肺数据就联网搜来入库，然后数数库里多少条", model)
    decide_entries = [t for t in trace if t["node"] == "decide"]
    # 降级发生处可观测：到顶 trace 如实标注清单对账缺席
    assert "checklist_unavailable" in decide_entries[-1]["detail"]
    assert "清单对账缺席" in decide_entries[-1]["detail"]
    # 语义不变（本批只留痕）：结算口径不动，结清时汇报照旧缀「已全部完成」
    assert "已全部完成" in (plan.get("report_zh") or "")


def test_sync_empty_source_all_online_scope_is_reported(monkeypatch, _tmp_project_root):
    """2026-08-15 （exec-gates M5）：点名单源场景下 sync 空槽 = 按全部在线源
    同步（半闸刻意放行，语义不动）——validate trace 与最终汇报都必须明说全量口径，
    不让「只同步了点名的那个源」成为可误读的默认。"""
    sync_result = {
        "checked_at": "t", "imported_total": 0, "hint_zh": "",
        "sources": [
            {"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
             "new_count": 0, "imported_count": 0, "imported_titles": [],
             "note_zh": "没有需要入库的新增"},
            {"source": "geo", "label": "NCBI GEO", "mode": "online",
             "new_count": 0, "imported_count": 0, "imported_titles": [],
             "note_zh": "没有需要入库的新增"},
        ],
    }
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.sync_updates": {"run": _search_run(sync_result),
                                "label_zh": "检查更新并同步入库", "card_kind": "sync_updates",
                                "readonly": False}})
    model = _FakeModel(
        _tool_call("curate.sync_updates", quoted="检查GEO有没有更新，有新增就同步",
                   confidence="high", reason="同步"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="都同步好了。"),
    )
    plan, trace = _plan("检查GEO有没有更新，有新增就同步", model)
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [("curate.sync_updates", True)]
    validate_details = [t["detail"] for t in trace if t["node"] == "validate"]
    assert any("按全部在线源同步" in d for d in validate_details), "validate 必须留痕全量口径"
    assert "按全部在线源" in (plan.get("report_zh") or ""), "最终汇报必须明说全量口径"


def test_search_online_zero_write_is_ok_and_reported_honestly(monkeypatch, _tmp_project_root):
    """search_online 候选全部撞重**零写入**（record_count=0
    filename=None——corpus 层既有契约，test_corpus_curation.py 钉死）是合法结果：
    形状闸不许误判 bad_result_shape、不许谎称"不符合形状契约"；trace 与兜底汇报如实说
    "未重复入库"，不拼"已入库到 外部库"占位词（连带 exec-tools L7）。"""
    zero = dict(SEARCH_OK, record_count=0, filename=None,
                warnings=["候选共 2 条全部已在库中（同编号或同链接），未重复入库。"])
    _install_tools(monkeypatch, **{"curate.search_online": _search_run(zero)})
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜人类肺数据入库",
                   keywords="人类肺", source="ArrayExpress",
                   confidence="high", reason="搜肺"),
        AIMessage(content='{"done": true}'),
        # narrate 不预置 → FakeModel 弹空 IndexError → 走确定性兜底汇报路径
    )
    plan, trace = _plan("联网搜人类肺数据入库", model)
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [("curate.search_online", True)], \
        "零写入是合法结果，不许被形状闸误判成失败步"
    assert steps[0]["result"]["filename"] is None
    exec_details = [t["detail"] for t in trace if t["node"] == "execute"]
    assert any("未重复入库" in d for d in exec_details)
    report = plan.get("report_zh") or ""
    assert "未重复入库" in report
    assert "已入库到" not in report


# ---------------------------------------------------------------- ⑤b 写步预算闸

def test_write_step_budget_rejects_third_write_but_keeps_reads(monkeypatch, _tmp_project_root):
    """写步（search_online/sync_updates，成败都计）用满 MAX_WRITE_STEPS=2 后：
    decide 的 prompt 注入「写步预算已用完」段；模型仍提第三次写 → 裁决层机械拒绝
    （按 done 收尾 + 如实点名「还要入库可以再说一次」），绝不真跑第三次写。"""
    _install_tools(
        monkeypatch,
        **{"curate.search_online": _search_run(SEARCH_OK),
           "curate.db_status": lambda slots, root: {
               "total_records": 1, "sources": [], "external_files": [], "recycle": [],
               "ledger": {}}})
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜人类肺数据入库", keywords="人类肺",
                   source="ArrayExpress", confidence="high", reason="搜肺"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜小鼠脑数据入库",
             "source": "ArrayExpress", "keywords": "小鼠脑"}, ensure_ascii=False)),
        # decide#2：不顾预算提示再提第三次写 → 必须被机械拦下
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜人类心脏数据入库",
             "source": "ArrayExpress", "keywords": "人类心"}, ensure_ascii=False)),
        AIMessage(content="肺和脑都搜好了；心脏这次没有搜。"),
    )
    plan, trace = _plan("联网搜人类肺数据入库，联网搜小鼠脑数据入库，联网搜人类心脏数据入库", model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.search_online", "curate.search_online"]
    assert all(s["ok"] for s in plan["steps"])
    # decide#2 的 prompt 必须带写步预算注入段（机械约束摆给 LLM 看）
    assert "写步预算已用完" in model.invocations[2][0].content
    trace_text = "；".join(t.get("detail", "") for t in trace)
    assert "写步已达上限" in trace_text
    assert len(_ledger_rows(_tmp_project_root)) == 2
    # 被拦写步的如实点名句（裁决层单元级直钉：declined 句会进 narrate 确定性兜底汇报）
    _nxt, note, declined, _vf = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.search_online"},
        {"utterance": "联网搜人类心脏数据入库",
         "steps": [{"verb": "curate.search_online", "ok": True},
                   {"verb": "curate.sync_updates", "ok": True}]})
    assert _nxt is None and "写步已达上限" in note
    assert "最多自动写" in declined and "再说一次" in declined


def test_write_budget_rejection_reenters_and_trailing_readonly_runs(monkeypatch, _tmp_project_root):
    """（对抗评审头条）：写步预算拦下第三次写 ≠ 整条请求完成——
    拒绝回灌重问一次，模型改提尾随的只读 db_status → 必须照常执行。"""
    _install_tools(
        monkeypatch,
        **{"curate.search_online": _search_run(SEARCH_OK),
           "curate.db_status": lambda slots, root: {
               "total_records": 1, "sources": [], "external_files": [], "recycle": [],
               "ledger": {}}})
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜人类肺数据入库", keywords="人类肺",
                   source="ArrayExpress", confidence="high", reason="搜肺"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜小鼠脑数据入库",
             "source": "ArrayExpress", "keywords": "小鼠脑"}, ensure_ascii=False)),
        # decide#2：第三次写 → 预算闸拦下（回灌重问）；模型改提尾随的只读库容汇报
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜人类心脏数据入库",
             "source": "ArrayExpress", "keywords": "人类心"}, ensure_ascii=False)),
        _tool_call("curate.db_status", quoted="告诉我库里多少条", confidence="high",
                   reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 搜人类肺入库：已做（第1步）\n2. 搜小鼠脑入库：已做（第2步）\n"
            "3. 搜人类心脏入库：写步预算已用完，没做\n4. 告诉我库里多少条：已做（第3步）")),
        AIMessage(content="肺和脑都搜好了，心脏没有搜（写步预算用完），库里共 1 条。"),
    )
    plan, trace = _plan("联网搜人类肺数据入库，联网搜小鼠脑数据入库，联网搜人类心脏数据入库，"
                        "告诉我库里多少条", model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.search_online", "curate.search_online", "curate.db_status"], \
        "写预算拦下第三次写后，尾随的只读 db_status 必须照常执行（P0-1 重入）"
    trace_text = "；".join(t.get("detail", "") for t in trace)
    assert "被系统拒绝" in trace_text and "回灌重问" in trace_text


# ---------------------------------------------------------------- ⑥ 工具失败如实记 + 如实汇报

def test_failed_step_is_recorded_and_reported_honestly(monkeypatch, _tmp_project_root):
    """工具在任何网络请求之前抛 source_not_registered（fail-fast 替身，见
    `_search_run_unregistered` 注释）→ step ok=False + error 出人读 hint、
    error_code 另存机器码（上屏只读 error）；decide 停环，汇报如实带失败；账本失败行 ok=false。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(
            [{"source": "10x", "label": "10x Genomics", "mode": "online",
              "local_count": 12, "online_recent": 14, "new_count": 2,
              "new_candidates": [{"accession": "x", "title": "10x new dataset"}]}]),
            "curate.search_online": _search_run_unregistered,
        },
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新",
                   source="10x", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "下载下来",
             "source": "10x", "keywords": "10x"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        # narrate 没有 answer → 确定性兜底（同一批事实）
    )
    plan, trace = _plan("检查10x是否有更新，若有则下载下来", model)
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [
        ("curate.check_updates", True), ("curate.search_online", False),
    ]
    assert "暂不支持联网搜索来源" in steps[1]["error"]
    assert steps[1].get("error_code") == "source_not_registered"
    report = plan.get("report_zh") or ""
    assert "没有完成" in report and "暂不支持联网搜索来源" in report
    rows = _ledger_rows(_tmp_project_root)
    assert [r["ok"] for r in rows] == [True, False]
    assert "source_not_registered" in rows[1]["error"]


# ---------------------------------------------------------------- ⑥b LLM 汇报谎称未发生的写动作 → 机械后检弃用

def test_llm_report_claiming_an_undone_write_is_discarded(monkeypatch, _tmp_project_root):
    """机械后检（2026-08-04 真机抓到的幻觉）：decide 判 done、steps 里没有成功的入库步，
    narrate 的 LLM 却写「已执行下载」→ 该汇报弃用，回退确定性拼接（同一批事实）。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，因有新增，已执行下载。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有则下载下来", model)
    report = plan.get("report_zh") or ""
    assert "已执行下载" not in report and "已下载" not in report
    assert "疑似新增 2 条" in report, "兜底汇报与谎称汇报是同一批事实"
    assert any("与实录不符" in t["detail"] for t in trace if t["node"] == "narrate")
    # 批B ：被弃用的 LLM 汇报原文必须留痕可复盘（trace 附加字段）
    narrate_entries = [t for t in trace if t["node"] == "narrate"]
    assert "已执行下载" in (narrate_entries[-1].get("discarded_report_zh") or "")


def test_report_naming_an_untouched_source_is_discarded(monkeypatch, _tmp_project_root):
    """批B （集成）：只有检查 10x 一步真跑，LLM 汇报首句却称
    「检查了10x和ArrayExpress的更新」——只读侧假性声称。来源接地后检必须弃用该汇报、
    回退确定性拼接（兜底只提真碰过的 10x）。"""
    tenx_new = [{"source": "10x", "label": "10x Genomics", "mode": "online",
                 "local_count": 12, "online_recent": 14, "new_count": 2,
                 "new_candidates": [{"accession": "x", "title": "10x new dataset"}]}]
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(tenx_new)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x和ArrayExpress有没有更新",
                   source="10x", confidence="high", reason="先查10x"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查了10x和ArrayExpress的更新：10x 疑似新增 2 条；ArrayExpress未在步骤中检查。"),
    )
    plan, trace = _plan("先检查10x和ArrayExpress有没有更新", model)
    report = plan.get("report_zh") or ""
    assert "ArrayExpress" not in report, "没碰过的来源不许出现在汇报里"
    assert "疑似新增 2 条" in report, "兜底如实保留真做的一步"
    narrate_entries = [t for t in trace if t["node"] == "narrate"]
    assert "ArrayExpress" in (narrate_entries[-1].get("discarded_report_zh") or "")


def test_report_denying_import_with_exotic_wording_is_discarded(monkeypatch, _tmp_project_root):
    """批A D1 P0（2026-08-06 真机）：入库步成功（20 条落盘），LLM 汇报却写
    「结果已保存。未执行导入操作。」——旧穷举否认词表漏「导入」词族整句透传。
    模式化否认后检（写动作词 × 否认语素）必须弃用该汇报。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(dict(SEARCH_OK, record_count=20))},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="check ArrayExpress for updates",
                   source="ArrayExpress", confidence="high", reason="check"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "import them",
             "source": "ArrayExpress", "keywords": "human lung"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="已检查更新并联网搜索 human lung，结果已保存。未执行导入操作。"),
    )
    plan, trace = _plan("Check ArrayExpress for updates, and if there are new human lung datasets, import them.", model)
    report = plan.get("report_zh") or ""
    assert "未执行导入" not in report, "否认成功写动作的汇报必须弃用"
    assert "已入库到" in report, "兜底如实说入库结果"
    narrate_entries = [t for t in trace if t["node"] == "narrate"]
    assert "未执行导入" in (narrate_entries[-1].get("discarded_report_zh") or "")


def test_decide_with_fabricated_keywords_is_stopped(monkeypatch, _tmp_project_root):
    """批A D5（2026-08-06 真机）：原话零主题词（「看看有没有什么新数据，有的话拿回来」），
    decide 却臆造 keywords="single cell" 触发真入库。keywords 接地校验必须把这一步拦下——
    写操作的参数不许发明，loop 停在这一步、绝不真跑。
    2026-08-08 对称化：violation 先带检查意见重问一次（模型认错停手），重问不改才停环——
    「臆造写步绝不真跑」的不变量一字不变。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(SEARCH_OK)},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="看看有没有什么新数据",
                   confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "有的话拿回来",
             "keywords": "single cell"}, ensure_ascii=False)),
        # violation 重问一次：模型认错（散文 → invalid → fail-safe 停环）
        AIMessage(content="原话没给主题词，不能臆造关键词联网搜索。"),
        AIMessage(content="有 2 条疑似新增，但原话没给主题词，没有联网搜索。"),
    )
    plan, trace = _plan("看看有没有什么新数据，有的话拿回来", model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"], \
        "臆造 keywords 的写步绝不真跑"
    assert any("找不到出处" in t["detail"] for t in trace if t["node"] == "decide")
    assert any("已带检查意见重问一次" in t["detail"] for t in trace if t["node"] == "decide"), \
        "violation 重问留痕必须进 trace"
    assert len(model.invocations) == 4, "understand + decide + 重问 + narrate"
    rows = _ledger_rows(_tmp_project_root)
    assert [r["endpoint"] for r in rows] == ["agent_exec:curate.check_updates"], "账本只有只读步"


def test_translated_keywords_from_utterance_pass(monkeypatch, _tmp_project_root):
    """接地校验的另一侧不许误伤：「人类肺」→ keywords "human lung" 是合法翻译
    （vocabulary 词条接地），两步链照常真跑。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(SEARCH_OK)},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "human lung"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，已联网搜到 2 条并入库。"),
    )
    plan, _ = _plan("检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库", model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.search_online",
    ], "合法翻译的 keywords 不许被拦"


def test_decide_keywords_grounded_in_step_results_pass(monkeypatch, _tmp_project_root):
    """批C（依据是三模型 A/B 实测，.fix-shots/model_ab/）：
    原话零主题词的「若有则下载」，decide 把检查步骤**真实发现**的条目标题逐字拿来当
    keywords——出处之二（步骤结果）接地，校验放行、两步链真跑。与 D5 的分界线：
    出处必须是实有结果，臆造依旧拦死。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(SEARCH_OK)},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "下载下来",
             "source": "ArrayExpress", "keywords": "human lung atlas"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，已按发现的条目联网搜到 2 条并入库。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有则下载下来", model)
    assert [(s["verb"], s["ok"]) for s in plan.get("steps") or []] == [
        ("curate.check_updates", True), ("curate.search_online", True),
    ], "keywords 逐字取自检查步骤发现的真实条目标题 → 放行并真跑"
    assert len(_ledger_rows(_tmp_project_root)) == 2, "两个真跑的工具各落一行审计"


def test_decide_keywords_mismatched_with_step_results_still_stopped(monkeypatch, _tmp_project_root):
    """批C 的护栏侧：步骤结果里没有的条目照样不许搜——检查步骤发现的是 human lung 系列，
    decide 却提议 keywords="human heart"（两头无出处）→ 拦下、写步绝不真跑。
    2026-08-08 对称化：violation 先带检查意见重问一次（模型认错停手），重问不改才停环。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(SEARCH_OK)},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "下载下来",
             "source": "ArrayExpress", "keywords": "human heart"}, ensure_ascii=False)),
        # violation 重问一次：模型认错（散文 → invalid → fail-safe 停环）
        AIMessage(content="发现的是肺数据，原话没有心脏主题，不能联网搜索。"),
        AIMessage(content="发现的是肺数据，没有联网搜索。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有则下载下来", model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"], \
        "步骤结果里找不到出处的 keywords 依旧拦死"
    assert any("找不到出处" in t["detail"] for t in trace if t["node"] == "decide")
    assert [r["endpoint"] for r in _ledger_rows(_tmp_project_root)] == [
        "agent_exec:curate.check_updates"], "账本只有只读步"


def test_llm_report_denying_a_done_write_is_discarded(monkeypatch, _tmp_project_root):
    """机械后检的另一侧（2026-08-04 真机抓到）：入库步明明成功（文件已写），LLM 汇报却说
    「未执行入库操作」→ 该汇报弃用，回退确定性拼接（兜底如实说「已入库到 …」）。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(AE_TWO_NEW),
           "curate.search_online": _search_run(SEARCH_OK)},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "human lung"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="联网搜索 human lung 返回 20 条，结果文件为 upload_x.json。未执行入库操作。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库", model)
    report = plan.get("report_zh") or ""
    assert "未执行入库" not in report and "未入库" not in report
    assert "已入库到" in report, "兜底汇报如实说入库结果"
    assert any("与实录不符" in t["detail"] for t in trace if t["node"] == "narrate")


# ---------------------------------------------------------------- ⑦ db_status 单步零回归

def test_db_status_single_step_contract_is_unchanged(monkeypatch, _tmp_project_root):
    """单步 db_status：observation/report 既有路径逐位保留（report_zh = LLM 汇报原文），
    steps 同步实录一步，trace = understand → validate → execute → decide → narrate。"""
    obs = {"total_records": 5712, "sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                                               "local_count": 5712, "snapshot_date": None}],
           "external_files": [], "recycle": [], "ledger": {}}
    _install_tools(monkeypatch, **{"curate.db_status": lambda slots, root: dict(obs)})
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="汇报数据库的当前状态", confidence="high", reason="问库况"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="本地库共 5712 条、1 个来源。"),
    )
    plan, trace = _plan("汇报数据库的当前状态", model)
    assert plan.get("observation", {}).get("total_records") == 5712
    assert plan.get("report_zh") == "本地库共 5712 条、1 个来源。"
    steps = plan.get("steps") or []
    assert len(steps) == 1 and steps[0]["card_kind"] == "db_status" and steps[0]["ok"]
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "execute", "decide", "narrate"]


# ---------------------------------------------------------------- ⑧ 非 loop 动词不进 execute

def test_non_loop_verb_never_enters_execute(monkeypatch, _tmp_project_root):
    """pack.download 不在 LOOP_TOOLS → execute 空过、decide 不发起、plan.steps 缺席
    （既有前端 runner 派发路径零变化）。"""
    model = _FakeModel(
        _tool_call("pack.download", quoted="下载top3", limit=3, confidence="high", reason="要文件"),
    )
    plan, trace = agent_exec.plan_with_agent(
        "下载top3", has_results=True, result_total=10,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )
    assert plan["verb"] == "pack.download"
    assert "steps" not in plan
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "narrate"]
    assert _ledger_rows(_tmp_project_root) == []


# ---------------------------------------------------------------- ⑨ 多点名源的续步不被护栏误杀（批B P1）

def test_multi_named_sources_continuation_is_not_blocked(monkeypatch, _tmp_project_root):
    """原话点名两个来源（「先检查10x和ArrayExpress…」）时，decide 续步检查**第二个**点名源
    曾被 `_named_source_violation` 只认第一个而恒判违规 → fail-safe 停环、后半截全部没做。
    修复后合法集合 = 任一点名源；单源场景行为逐位不变（见 test_agent_exec.py 既有三条）。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x和ArrayExpress有没有更新",
                   source="10x", confidence="high", reason="先查10x"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查10x和ArrayExpress有没有更新",
             "source": "ArrayExpress"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="10x 与 ArrayExpress 都检查了。"),
    )
    plan, trace = _plan("先检查10x和ArrayExpress有没有更新", model)
    assert [s["slots"]["source"] for s in plan.get("steps") or []] == ["10x", "ArrayExpress"], \
        "第二个点名源的续步必须能真跑"
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "execute", "decide",
        "validate", "execute", "decide", "narrate",
    ]


# ---------------------------------------------------------------- ⑩ 取消态也必须有如实汇报（批B）

def test_cancelled_plan_gets_a_deterministic_report(monkeypatch, _tmp_project_root):
    """「…算了别搜了」：cancelled=true → execute 空过（零步骤、零写盘）→ narrate 此前
    不给 report_zh（用户面对空白）。现必须有一句确定性取消回执。"""
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜人类肺数据入库", source="ArrayExpress",
                   keywords="人类肺", cancelled=True, confidence="high", reason="用户改主意"),
    )
    plan, trace = _plan("联网搜人类肺数据入库……算了别搜了", model)
    assert plan.get("cancelled") is True
    assert not plan.get("steps"), "取消态绝不实录步骤"
    report = plan.get("report_zh") or ""
    assert "已按你的要求取消" in report and "没有执行任何操作" in report
    assert plan.get("report_source") == "deterministic"
    assert _ledger_rows(_tmp_project_root) == []


def test_understand_json_fallback_writes_audit_row(monkeypatch, _tmp_project_root):
    """2026-08-06 B4：understand 两级 tools 通道都被拒 → 跌 JSON-in-prompt 兜底时，必须往
    .userdata/agent_fallbacks.jsonl 落一行抓现场账（生产偶发「直连通道不可用」离线复现不了，
    落账让下次真机发生即自动留证）；账本与联网账本同目录、审计失败不掀翻主流程。"""

    class _RejectingModel(_FakeModel):
        def invoke(self, messages):
            self.invocations.append(messages)
            if len(self.invocations) <= 2:   # required → auto 两档都拒（思考模型形态）
                raise Exception("BadRequestError: Thinking mode does not support this tool_choice")
            return self.answers.pop(0)

    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _RejectingModel(
        AIMessage(content=json.dumps(
            {"verb": "none", "confidence": "high", "reason": "只是问问"}, ensure_ascii=False)),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    log_path = _tmp_project_root / ".userdata" / "agent_fallbacks.jsonl"
    assert log_path.is_file(), "跌 JSON 兜底必须落抓现场账"
    rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(rows) == 1 and rows[0]["node"] == "understand"
    assert rows[0]["reason"] and rows[0]["ts"], "触发原因与时间必须落账"


# ---------------------------------------------------------------- ⑪ 混合诉求机械闸 e2e

def _finish(report):
    return AIMessage(content="", tool_calls=[
        {"name": "finish", "args": {"completion_report": report}, "id": "tf"}])


def _rank_run(slots, root, ctx=None):
    q = str((slots or {}).get("query") or "")
    # display 槽已退役（2026-09-13 rerank 处置批）：healthy 即 auto-show（displayed 恒 True），
    # 结果带机械健康度键（屏面基线/告警注入的事实源）。
    return {"query": q, "total": 2,
            "top": [{"dataset_uid": "GSE-T1", "dataset_name": f"DS-{q}-1",
                     "species": "Human", "tissue": "Lung", "disease": "",
                     "source": "10x Genomics", "rank": 1},
                    {"dataset_uid": "GSE-T2", "dataset_name": f"DS-{q}-2",
                     "species": "Human", "tissue": "Lung", "disease": "",
                     "source": "10x Genomics", "rank": 2}],
            "displayed": True, "health": "healthy", "batch": None}


def _install_mixed_tools(monkeypatch, **runs):
    """混合 e2e 用假注册表：在 _install_tools 三件套外加 rank（形状同真表）。"""
    table = {
        "curate.db_status": {"label_zh": "汇报数据库状态", "card_kind": "db_status",
                             "readonly": True, "report": True, "observation": True},
        "curate.check_updates": {"label_zh": "检查来源更新", "card_kind": "check_updates",
                                 "readonly": True},
        "curate.search_online": {"label_zh": "联网搜索入库", "card_kind": "search_online",
                                 "readonly": False},
        "rank": {"label_zh": "检索数据集", "card_kind": "rank",
                 "readonly": True, "needs_context": True},
    }
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        verb: {"run": runs[verb], **meta} for verb, meta in table.items() if verb in runs
    })


def test_hybrid_gate_check_then_search_e2e(monkeypatch, _tmp_project_root):
    """「检查10x更新，然后帮我找人类肺数据集」：机械意图闸在 route_consensus 检出
    动作∧检索双信号 → 零投票直走 general（trace 首条如实留痕「机械意图闸」）；
    general 全集面下 understand 先办动作（check_updates）、decide 再办检索（rank——
    检索健康即自动上屏，2026-09-13 起 display 槽已退役），finish 逐件核销——混合诉求
    两半都真做、顺序与原话一致。"""
    _install_mixed_tools(monkeypatch,
                         **{"curate.check_updates": _check_run(AE_TWO_NEW),
                            "rank": _rank_run})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x更新", source="10x Genomics",
                   confidence="high", reason="先查更新"),
        _tool_call("rank", quoted="帮我找人类肺数据集", query="人类肺 单细胞"),
        _finish("1. 检查10x更新：已做（第1步）。\n"
                "2. 帮我找人类肺数据集：已做（第2步）。"),
        # narrate 不预置 → IndexError → 确定性兜底汇报（同一批事实）
    )
    plan, trace = _plan("检查10x更新，然后帮我找人类肺数据集", model)
    assert trace[0]["node"] == "route_consensus"
    assert "机械意图闸" in trace[0]["detail"], "闸触发必须如实留痕（零投票走 general）"
    assert trace[0]["route_votes"] == []
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [
        ("curate.check_updates", True), ("rank", True)]
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "execute", "decide",
        "validate", "execute", "decide", "narrate",
    ]


def test_hybrid_gate_search_then_db_status_e2e(monkeypatch, _tmp_project_root):
    """「帮我找乳腺癌数据，顺便看看数据库状态」：闸 → general；understand 首步 rank
    （检索是主诉求，健康结果自动上屏）→ decide 续办 db_status——面内面外逐件核销，
    两半都真执行。"""
    def _db_run(slots, root):
        return {"generated_at": "t", "sources": [], "total_records": 3,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}}

    _install_mixed_tools(monkeypatch,
                         **{"rank": _rank_run, "curate.db_status": _db_run})
    model = _FakeModel(
        _tool_call("rank", quoted="帮我找乳腺癌数据", query="乳腺癌 单细胞"),
        _tool_call("curate.db_status", quoted="顺便看看数据库状态"),
        _finish("1. 帮我找乳腺癌数据：已做（第1步）。\n"
                "2. 看看数据库状态：已做（第2步）。"),
    )
    plan, trace = _plan("帮我找乳腺癌数据，顺便看看数据库状态", model)
    assert trace[0]["node"] == "route_consensus"
    assert "机械意图闸" in trace[0]["detail"]
    steps = plan.get("steps") or []
    assert [(s["verb"], s["ok"]) for s in steps] == [
        ("rank", True), ("curate.db_status", True)]
    assert plan.get("observation") is not None, "db_status 照常挂 plan.observation 契约"
