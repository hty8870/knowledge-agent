# -*- coding: utf-8 -*-
"""RAG 工具组（rank / rerank / results.show）的常驻钉。

rank / rerank / results.show 常驻动词表与 LOOP_TOOLS 注册表；原环境开关与
OFF 逐位一致负向钉随代码一并摘除。

本文件钉：登记齐（动词表/豁免清单/注册表/返回契约/decide 面/套件面）、schema 形状、
display 槽退役（2026-09-13 rerank 处置批：上屏唯一出口是 results.show，rank healthy
auto-show、rerank 三态出口）、rerank 双闸（2026-09-18 触发子集批：触发闸=屏面健康
且充足即拒绝执行；语义闸=未授权语义变化在健康屏面驳回/空屏面放行标注）、
改写健全性检查三态、预算机械闸、联网归类（纯本地）。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import agent_schemas as SC


# ---------------------------------------------------------------- 登记齐

def test_verb_specs_registered():
    rank = AP.VERB_BY_NAME["rank"]
    rerank = AP.VERB_BY_NAME["rerank"]
    show = AP.VERB_BY_NAME["results.show"]
    # display 槽已退役（2026-09-13 rerank 处置批）：rank/rerank 不再声明该槽。
    assert rank.kind == AP.EXEC and rank.slots == ("query",)
    assert rank.requires_results is False
    assert rerank.kind == AP.EXEC and rerank.slots == ("query", "reason", "relax")
    assert rerank.requires_results is False
    # results.show：屏态唯一出口动词，handle 句柄指认试跑（缺省 latest）。
    assert show.kind == AP.EXEC and show.slots == ("handle",)
    assert show.requires_results is False
    # 尾部追加，旧动词一个不动；四工具追加在 route.request 之后。
    assert [s.verb for s in AP.VERB_SPECS[-4:]] == [
        "route.request", "compare.datasets", "compat.find", "fair.check"]
    assert "rank" in [s.verb for s in AP.VERB_SPECS] and "rerank" in [s.verb for s in AP.VERB_SPECS]
    assert "results.show" in [s.verb for s in AP.VERB_SPECS]
    # 刻意更新：curate.rollback 同为环内专属（回滚目标依赖本轮 steps
    # 实录的快照锚，前端单步 runner 没有这个现场）——补录豁免清单。
    # 刻意更新：compare.datasets / compat.find / fair.check 同为环内
    # 专属（默认对象依赖环内当前结果集现场）——补录豁免清单；cite.export 双通道不豁免。
    # 刻意更新（2026-09-13 rerank 处置批）：results.show 同为环内专属（handle 解析与
    # 指纹复核依赖环内 steps 实录的试跑现场）——补录豁免清单。
    # 刻意更新（2026-09-14 YOLO 批）：ask.relax 同为环内专属（选择卡由 utterance 响应的
    # ask_relax 键驱动，无前端 runner 独立入口）——补录豁免清单。
    assert AP.FRONTEND_UNWIRED_EXEC_VERBS == ("search.rerun", "rank", "rerank",
                                              "results.show", "curate.rollback",
                                              "compare.datasets", "compat.find",
                                              "fair.check", "ask.relax")
    assert set(AP.FRONTEND_UNWIRED_EXEC_VERBS) <= set(AP.EXEC_VERBS)


def test_loop_tools_registered():
    for verb, card in (("rank", "rank"), ("rerank", "rerank"),
                       ("results.show", "results_show")):
        spec = AX.LOOP_TOOLS[verb]
        assert spec["readonly"] is True
        assert spec["needs_context"] is True
        assert spec["card_kind"] == card
        assert callable(spec["run"]) and spec["decide_zh"]
    # decide 面：顺序表与注册表同集合（既有钉的延续）。
    assert set(AX._DECIDE_VERB_ORDER) == set(AX.LOOP_TOOLS)
    # 刻意更新：新四工具追加在 route.request 之后（顺序表尾部）。
    assert AX._DECIDE_VERB_ORDER[-5:] == (
        "route.request", "compare.datasets", "cite.export", "compat.find", "fair.check")
    # 2026-09-13 rerank 处置批：results.show 紧随 rerank 之后登记（检索族）。
    assert AX._DECIDE_VERB_ORDER[AX._DECIDE_VERB_ORDER.index("rerank") + 1] == "results.show"
    names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS]
    assert "rank" in names and "rerank" in names and "results_show" in names
    assert AX._DECIDE_TOOL_NAME_TO_VERB["rank"] == "rank"
    assert AX._DECIDE_TOOL_NAME_TO_VERB["rerank"] == "rerank"
    assert AX._DECIDE_TOOL_NAME_TO_VERB["results_show"] == "results.show"
    # 返回契约登记。
    assert SC.LOOP_RESULT_MODELS["rank"] is SC.RankResult
    assert SC.LOOP_RESULT_MODELS["rerank"] is SC.RerankResult
    assert SC.LOOP_RESULT_MODELS["results.show"] is SC.ShowResult


def test_suite_membership():
    """套件面登记（2026-09-13 rerank 处置批）：results.show 属 search 套件（general
    自动继承全表）；action/rescue 刻意不装——动作面没有试跑现场可放屏，救回面只许
    换词重检或诚实收尾。"""
    assert "results.show" in AX._SUITE_LOOP_VERBS["search"]
    assert "results.show" in AX._SUITE_LOOP_VERBS["general"]
    assert "results.show" not in AX._SUITE_LOOP_VERBS["action"]
    assert "results.show" not in AX._SUITE_LOOP_VERBS["rescue"]


def test_network_classification():
    """rank / rerank 跑本地管线不触网——联网暂停禁提面的**显式**归类钉
    （test_network_loop_tools_are_the_registry_minus_db_status 的对口）。
    刻意更新：curate.rollback 是本地文件操作（不触网），同归纯本地。
     刻意更新：compare/cite/compat/fair 全本地（结果处理不触网）。
     刻意更新（2026-09-13 rerank 处置批）：results.show 重放本地确定性管线
     （use_llm=False + 指纹复核，不触网），同归纯本地。
     刻意更新（2026-09-14 YOLO 批）：ask.relax 只做本地确定性预演生成选择卡
     （零 LLM、不触网），同归纯本地。"""
    assert AX._NETWORK_LOOP_TOOLS == frozenset(
        set(AX.LOOP_TOOLS) - {"curate.db_status", "search.rerun", "rank", "rerank",
                              "route.request", "curate.rollback",
                              "compare.datasets", "cite.export",
                              "compat.find", "fair.check", "results.show",
                              "ask.relax"})


# ---------------------------------------------------------------- schema 形状

def test_args_schema():
    rank_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["rank"])
    assert rank_schema["required"] == []  # 铁律：required 恒空
    # display 槽已退役（2026-09-13 rerank 处置批）：rank/rerank schema 都不再有它。
    assert "display" not in rank_schema["properties"]
    assert "要检索的完整检索句" in rank_schema["properties"]["query"]["description"]
    rerank_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["rerank"])
    assert "display" not in rerank_schema["properties"]
    assert "原始" in rerank_schema["properties"]["query"]["description"]
    assert "优化检索词" in rerank_schema["properties"]["reason"]["description"]
    # results.show：handle 字符串槽，描述写清缺省与句柄取值。
    show_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["results.show"])
    assert show_schema["required"] == []
    assert "handle" in show_schema["properties"]
    assert "display" not in show_schema["properties"]
    assert show_schema["properties"]["handle"]["type"] == "string"
    assert "latest" in show_schema["properties"]["handle"]["description"]
    # search.rerun 的 query 描述逐字不变（既有钉的口径，本文件再钉一层防漂移）。
    rerun_schema = SC.verb_parameters_schema(AP.VERB_BY_NAME["search.rerun"])
    assert rerun_schema["properties"]["query"]["description"] == (
        "改写后的检索句：把当前查询换成规则更容易正确解析的说法，语义等价、"
        "不新增用户没表达的条件；当前没有可改的查询就不填。")


# ---------------------------------------------------------------- display 槽退役钉

def test_display_slot_retired():
    """display 槽退役（2026-09-13 rerank 处置批）：上屏唯一出口是 results.show 动词
    （rank 健康结果由后端不经 LLM 机械直调）。模型旧习惯带来的 display 入参
    被静默丢弃（不进 slots、不报错）；results.show 的 handle 槽正常透传。"""
    utter = "找找人类肺癌的数据"
    base = {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "human lung cancer"}
    for bogus in (True, "TRUE", False, "false", "yes", 1, None):
        plan = AP.build_plan_from_raw({**base, "display": bogus}, utter,
                                      has_results=False, result_total=0)
        assert "display" not in plan["slots"], bogus
    plan = AP.build_plan_from_raw(
        {"verb": "results.show", "quoted": "把结果放上去", "handle": "latest_rerank"},
        "把结果放上去", has_results=True, result_total=3)
    assert plan["verb"] == "results.show"
    assert plan["slots"].get("handle") == "latest_rerank"


# ---------------------------------------------------------------- 改写健全性检查三态

class _FakeAnswer:
    def __init__(self, content):
        self.content = content


class _FakeModel:
    def __init__(self, content=None, exc=None):
        self._content, self._exc = content, exc

    def invoke(self, messages):
        if self._exc is not None:
            raise self._exc
        return _FakeAnswer(self._content)


def test_rewrite_query_sanity():
    # 模型缺席 → 退回原句，rewritten=False（如实标注，不静默）。
    assert AX._rewrite_query(None, "坏query") == ("坏query", False)
    # 正常改写 → 采纳。
    assert AX._rewrite_query(_FakeModel("mouse lung glioma"), "坏query") == (
        "mouse lung glioma", True)
    # 健谈模型多吐解释行 → 只取首行；包裹引号剥掉。
    assert AX._rewrite_query(_FakeModel('"mouse lung"\n因为……'), "坏query") == (
        "mouse lung", True)
    # 三态退回：空 / 与原句相同 / 超 200 字符 / 调用异常。
    assert AX._rewrite_query(_FakeModel(""), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel("坏query"), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel("x" * 201), "坏query") == ("坏query", False)
    assert AX._rewrite_query(_FakeModel(exc=RuntimeError("boom")), "坏query") == (
        "坏query", False)


def test_rewrite_query_records_usage():
    """rerank 的独立改写是真实 LLM 调用，给了 usage_sink
    就必须过 `_usage_record` 进账（末端聚合 plan.llm_usage）；读不到用量自然跳过。"""

    class _UsageModel:
        def invoke(self, messages):
            return SimpleNamespace(
                content="mouse lung",
                usage_metadata={"input_tokens": 8, "output_tokens": 2,
                                "input_token_details": {"cache_read": 3}})

    sink: list = []
    assert AX._rewrite_query(_UsageModel(), "坏query", usage_sink=sink) == (
        "mouse lung", True)
    assert sink == [{"node": "rerank_rewrite", "input": 8, "cache_read": 3, "output": 2}]
    # 无用量的替身：台账保持空（不伪造）。
    sink = []
    AX._rewrite_query(_FakeModel("mouse lung"), "坏query", usage_sink=sink)
    assert sink == []


class _CaptureModel:
    """捕获 invoke 入参的替身（注入布局钉用）。"""

    def __init__(self, content="new query"):
        self.calls: list = []
        self._content = content

    def invoke(self, messages):
        self.calls.append(messages)
        return _FakeAnswer(self._content)


def test_rewrite_query_injection_layout():
    """health_zh / extra_system_zh（2026-09-16 改写环臂批）：三参数全缺省时与历史
    行为逐位一致；注入时 human 顺序 = 原句 → 健康快照 → 探测报告，extra 只进 system。"""
    m = _CaptureModel()
    AX._rewrite_query(m, "坏query")
    _sysm, hum = m.calls[0]
    assert hum.content == "坏query"
    # probe-only 与 2026-09-13 前行为逐位一致（回归钉）。
    m2 = _CaptureModel()
    AX._rewrite_query(m2, "坏query", probe_zh="PROBE")
    assert m2.calls[0][1].content == "坏query\n\nPROBE"
    # 三注入同给：顺序钉 + extra 只进 system 不进 human。
    m3 = _CaptureModel()
    AX._rewrite_query(m3, "坏query", probe_zh="PROBE", health_zh="HEALTH",
                      extra_system_zh="EXTRA")
    sysm3, hum3 = m3.calls[0]
    assert hum3.content == "坏query\n\nHEALTH\n\nPROBE"
    assert sysm3.content.endswith("EXTRA")
    assert "EXTRA" not in hum3.content
    # anchor_zh（2026-09-17 上下文工程批）：紧跟 query 之后、health 之前；缺省逐位不变。
    m4 = _CaptureModel()
    AX._rewrite_query(m4, "坏query", anchor_zh="ANCHOR", health_zh="HEALTH")
    assert m4.calls[0][1].content == "坏query\n\nANCHOR\n\nHEALTH"


def test_loop_rerank_anchors_original_utterance(fake_pipeline, monkeypatch):
    """（2026-09-17 上下文工程批，借鉴 Aivis 经验八）：环内 query 被转述（与 utterance
    不一致）时，改写调用注入用户原话锚点；逐字相同时免注（防冗余 token）。"""
    ax, calls = fake_pipeline
    seen: dict = {}

    def _fake_rewrite(model, q, **kw):
        seen.update(kw)
        return ("human lung adenocarcinoma", True)

    monkeypatch.setattr(ax, "_rewrite_query", _fake_rewrite)
    # 转述不一致 → 注入锚点。
    ax._loop_rerank({"query": "肺癌数据"}, None,
                    {"chat_model": object(), "utterance": "那个肺癌的数据有没有"})
    anchor = str(seen.get("anchor_zh") or "")
    assert "用户原话" in anchor and "那个肺癌的数据有没有" in anchor
    # 逐字相同 → 免注（空串）。
    seen.clear()
    ax._loop_rerank({"query": "那个肺癌的数据有没有"}, None,
                    {"chat_model": object(), "utterance": "那个肺癌的数据有没有"})
    assert seen.get("anchor_zh") == ""


def test_rewrite_health_zh_render():
    """机械健康快照（2026-09-16 改写环臂批）：事实全时全渲染（含排除极性），
    不可得的面如实略过；结果数是底线事实恒在。"""
    zh = AX._rewrite_health_zh(
        health="degenerate", total=0, resolution_status="abstained",
        filters=[{"polarity": "include", "label": "物种", "values": ["Human"]},
                 {"polarity": "exclude", "label": "疾病", "values": ["glioma"]}],
        top_titles=["甲", "乙"])
    assert "没有经条件核验的结果" in zh
    assert "abstained" in zh
    assert "结果数：0" in zh
    assert "物种=Human" in zh and "排除疾病=glioma" in zh
    assert "甲；乙" in zh
    # 不可得的面不出现对应行（health 非法值/无 filters/无 titles）。
    zh2 = AX._rewrite_health_zh(health="weird", total=3)
    assert "健康度" not in zh2 and "执行的条件" not in zh2 and "top 示例" not in zh2
    assert "结果数：3" in zh2


def test_rewrite_health_zh_lite_minimal():
    """lite=True（2026-09-21 注入稀释消融批）：只留健康度+结果数两行最小屏面信号，
    解析状态/执行的条件/top 示例一律略去；lite=False 逐位不变。"""
    kw = dict(health="degenerate", total=0, resolution_status="abstained",
              filters=[{"polarity": "include", "label": "物种", "values": ["Human"]}],
              top_titles=["甲"])
    lite = AX._rewrite_health_zh(**kw, lite=True)
    assert "没有经条件核验的结果" in lite and "结果数：0" in lite
    assert "abstained" not in lite and "物种" not in lite and "top 示例" not in lite
    full = AX._rewrite_health_zh(**kw)
    assert "abstained" in full and "物种=Human" in full and "甲" in full


# ---------------------------------------------------------------- 工具本体（管线替身）

def _fake_meta(total=7, rows=None, filters=None):
    # active_filters 用**生产真源形状**（workflow 投影字典的列表）——dict 形替身曾掩盖
    # 「dict() 强转列表必炸」的真 bug（run2 复盘）。
    return SimpleNamespace(
        result_total=total,
        active_filters=filters if filters is not None else [
            {"filter_id": "include:species", "polarity": "include", "dim": "species",
             "label": "物种", "values": ["Human"]},
        ],
        retrieved_data=rows if rows is not None else [
            {"dataset_name": f"DS{i}", "species": "Human", "tissue": "lung",
             "disease": "adenocarcinoma", "source": "GEO"} for i in range(5)
        ],
    )


@pytest.fixture
def fake_pipeline(monkeypatch):
    """替身标准管线：run_with_meta 返假 meta；recommend_payload 返最小同形 dict。"""
    import dataset_recommender.app.workflow as wf
    import dataset_recommender.app.recommend_rows as rr

    calls: list[dict] = []

    class _FakeFlow:
        def run_with_meta(self, p=None, **kwargs):
            # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
            calls.append(vars(p) if p is not None else kwargs)
            return _fake_meta()

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _FakeFlow)
    monkeypatch.setattr(rr, "recommend_payload", lambda meta: {
        "ok": True, "result_total": meta.result_total,
        "query_constraints": meta.active_filters,
        "results": [{"dataset_name": "DS0", "species": "Human", "tissue": "lung",
                     "disease": "adenocarcinoma", "source": "GEO"}],
    })
    return AX, calls


def _parse_flow(total=7, rows=None, calls=None):
    """带真解析的替身管线类（2026-09-18 触发子集批）：rerank 语义闸要
    `_prepare_context`——复用 `parse_query` 真源（与 relax_live 钉的 _ZeroFlow 同径），
    run_with_meta 返假 meta（传 calls 则同 `_FakeFlow` 记账）。给「语义闸」钉用，
    纯 `_FakeFlow` 会因缺 `_prepare_context` 被 diff 记 parse_error 而恒 blocked——
    那不是要钉的行为。"""
    from dataset_recommender.retrieval.query_parser import parse_query

    class _ParseFlow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                return []  # 探测报告/relax_live 的情报源；替身无可放松项即可

        def _prepare_context(self, query, **kwargs):
            return (parse_query(query, None), [], [],
                    None, None, None, None, None)

        def run_with_meta(self, p=None, **kwargs):
            if calls is not None:
                calls.append(vars(p) if p is not None else kwargs)
            return _fake_meta(total=total, rows=rows)

    return _ParseFlow


def test_loop_rank_healthy_auto_shows(fake_pipeline):
    """（2026-09-13 rerank 处置批，pin ④）：display 槽已退役——**healthy 即自动上屏**
    （规则程序可断言的好 → 不经 LLM 机械直调 results.show 路径出批）。"""
    ax, calls = fake_pipeline
    out = ax._loop_rank({"query": "human lung cancer"}, None,
                        {"search_sources": None, "utterance": "找找肺癌的数据"})
    assert out["query"] == "human lung cancer"
    assert out["total"] == 7
    assert out["health"] == "healthy" and out["resolution_status"] == ""
    assert out["filters"] == [
        {"filter_id": "include:species", "polarity": "include", "dim": "species",
         "label": "物种", "values": ["Human"]},
    ]
    # auto-show 时 top digest 取自载荷行（与上屏内容同投影——替身载荷只给 1 行）。
    assert out["top"][0]["dataset_name"] == "DS0" and len(out["top"]) == 1
    assert out["displayed"] is True
    batch = out["batch"]
    assert batch["kind"] == "rank"  # kind = 内容来源动词
    assert batch["label"] == "human lung cancer"
    # query_raw = 本轮用户原话（契约）——ctx 带 utterance
    # 时绝不许填成模型产出的 rank query；ctx 缺席（直调/测试）退回 query。
    assert batch["query_raw"] == "找找肺癌的数据"
    assert batch["query_effective"] == "human lung cancer"
    assert batch["payload"]["result_total"] == 7
    out2 = ax._loop_rank({"query": "human lung cancer"}, None, {"search_sources": None})
    assert out2["batch"]["query_raw"] == "human lung cancer"
    # auto-show 时 top digest 取自卡片行（与载荷同投影）。
    assert out["top"][0]["dataset_name"] == "DS0"
    assert calls and calls[0]["query"] == "human lung cancer"
    assert calls[0]["use_llm"] is False
    assert "rerank_audit" not in calls[0]
    # 返回契约形状闸：登记模型能接住真实返回。
    ax._LOOP_RESULT_MODELS["rank"].model_validate(out)


def test_loop_rank_degenerate_never_shows(fake_pipeline, monkeypatch):
    """（2026-09-13 rerank 处置批）：零命中 = degenerate → 只如实回报试跑事实，
    绝不上屏（无 batch、displayed=False）——屏态唯一出口是 results.show。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    class _ZeroFlow:
        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta(total=0, rows=[])

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _ZeroFlow)
    out = ax._loop_rank({"query": "no such thing"}, None, {"search_sources": None})
    assert out["total"] == 0 and out["health"] == "degenerate"
    assert out["displayed"] is False and out["batch"] is None
    ax._LOOP_RESULT_MODELS["rank"].model_validate(out)


def test_loop_rank_degenerate_relax_live_digest(fake_pipeline, monkeypatch):
    """（2026-09-14 YOLO 批）：degenerate 试跑顺带 ask.relax 适用性情报——结果内嵌
    relax_live 紧凑 digest（key/label/count，与弹卡预演同一 relaxation_options 真源）；
    healthy 档不嵌（无打扰）；情报不可得（管线无预演能力）静默缺省不炸。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    from dataset_recommender.retrieval.query_parser import parse_query

    # healthy 档不嵌（无打扰）——先用 fake_pipeline 的 _FakeFlow（7 条健康结果）验掉，
    # 再切零命中替身（monkeypatch 在本测试内持续生效，顺序不能反）。
    ok = ax._loop_rank({"query": "human lung cancer"}, None, {"search_sources": None})
    # 注意：_FakeFlow 无 _prepare_context/retriever——若误进 degenerate 分支情报
    # 会静默缺省（不炸环），但 healthy 本就不该有 relax_live。
    assert ok["health"] == "healthy" and "relax_live" not in ok

    class _ZeroFlow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                return [{"key": "dim:disease", "kind": "drop", "label": "疾病",
                         "count": 2, "candidates": []}]

        def _prepare_context(self, query, **kwargs):
            return (parse_query(query, None), [], [],
                    None, None, None, None, None)

        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta(total=0, rows=[])

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _ZeroFlow)
    out = ax._loop_rank({"query": "human lung adenocarcinoma FASTQ"}, None,
                        {"search_sources": None})
    assert out["health"] == "degenerate"
    assert out["relax_live"] == [{"key": "dim:disease", "label": "疾病", "count": 2}]
    ax._LOOP_RESULT_MODELS["rank"].model_validate(out)


def test_loop_rank_watch_not_auto_shown(fake_pipeline, monkeypatch):
    """（2026-09-13 rerank 处置批）：watch（有结果但走了自动降级）不是「规则程序
    可断言的好」→ 不 auto-show，只如实回报健康度，上屏与否归模型经 results.show。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    class _DegradedFlow:
        def run_with_meta(self, p=None, **kwargs):
            m = _fake_meta()
            m.resolution_status = "degraded"
            return m

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _DegradedFlow)
    out = ax._loop_rank({"query": "human lung"}, None, {"search_sources": None})
    assert out["health"] == "watch" and out["resolution_status"] == "degraded"
    assert out["displayed"] is False and out["batch"] is None


def test_loop_rank_empty_query_raises(fake_pipeline):
    ax, calls = fake_pipeline
    with pytest.raises(AX._SearchRerunParamError):
        ax._loop_rank({"query": "  "}, None, {})


def test_loop_rerank_rewritten(fake_pipeline, monkeypatch):
    """（2026-09-13 rerank 处置批，pin ⑤）：空屏面（baseline degenerate）+ 试跑
    healthy → 0→N 规则程序可断言的严格更好 → auto_shown 自动上屏。
    （2026-09-18 触发子集批扩钉：改写带未授权语义变化时**空屏面放行但如实标注**——
    disclosure_zh 列语义变化明细、semantic_blocked=True；可能的错误结果远大于
    什么结果都不给，标注义务不省。）"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    # 带真解析的替身管线：语义闸走真实 diff（原句疾病=lung cancer → 改写新增
    # species/tissue + 疾病改写为 adenocarcinoma = 未授权语义变化，真 blocked）。
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow(calls=calls))
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("human lung adenocarcinoma", True))
    out = ax._loop_rerank({"query": "那个肺癌的数据"}, None,
                          {"chat_model": object(), "utterance": "那个肺癌的数据有没有"})
    assert out["original_query"] == "那个肺癌的数据"
    assert out["rewritten_query"] == "human lung adenocarcinoma"
    assert out["rewritten"] is True
    assert calls[0]["query"] == "human lung adenocarcinoma"
    assert out["rejected"] is False and out["auto_shown"] is True
    assert out["displayed"] is True
    assert out["baseline"]["health"] == "degenerate"  # 空屏面基线
    assert "新旧对照" in out["comparison_zh"]
    # 空屏面放行标注：语义闸如实记账（blocked 但放行），disclosure 列变化明细。
    assert out["semantic_blocked"] is True and out["reject_reason"] == ""
    assert "语义变化" in out["disclosure_zh"] and "仍按改写句上屏" in out["disclosure_zh"]
    batch = out["batch"]
    assert batch["kind"] == "rerank"
    assert batch["label"] == "human lung adenocarc"  # label = 生效的 rewritten_query（≤20 字截断）
    # query_raw = 本轮用户原话；原始坏 query 在结果顶层 original_query 键里。
    assert batch["query_raw"] == "那个肺癌的数据有没有"
    assert batch["query_effective"] == "human lung adenocarcinoma"
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_injects_health_snapshot(fake_pipeline, monkeypatch):
    """（2026-09-16 改写环臂批）：改写通道把机械健康快照（环内最近检索事实）随
    health_zh 注入 `_rewrite_query`（探测报告同注入，既有行为不变）；无环内检索
    事实时快照按屏面状态退化（无结果 → degenerate 档渲染）。"""
    ax, calls = fake_pipeline
    seen: dict = {}

    def _fake_rewrite(model, q, **kw):
        seen.update(kw)
        return ("human lung adenocarcinoma", True)

    monkeypatch.setattr(ax, "_rewrite_query", _fake_rewrite)
    rank_step = {"ok": True, "verb": "rank", "result": {
        "health": "degenerate", "total": 0, "resolution_status": "abstained",
        "filters": [{"polarity": "include", "label": "物种", "values": ["Human"]}],
        "top": []}}
    ax._loop_rerank({"query": "那个肺癌的数据"}, None,
                    {"chat_model": object(), "utterance": "u", "steps": [rank_step]})
    health_zh = str(seen.get("health_zh") or "")
    assert "健康快照" in health_zh
    assert "没有经条件核验的结果" in health_zh
    assert "abstained" in health_zh
    assert "物种=Human" in health_zh
    assert "probe_zh" in seen  # 探测报告同注入（2026-09-13 夜批既有行为）
    # 无环内事实：退化为屏面状态快照（无结果 → 没有经条件核验的结果 / 结果数 0）。
    seen.clear()
    ax._loop_rerank({"query": "那个肺癌的数据"}, None, {"chat_model": object()})
    health_zh2 = str(seen.get("health_zh") or "")
    assert "没有经条件核验的结果" in health_zh2 and "结果数：0" in health_zh2
    """（2026-09-13 rerank 处置批，pin ③）生死线：屏面健康 + 试跑 degenerate（零命中）
    → 机械驳回（rejected=True、无 batch、屏面不动），对照事实照给。
    （2026-09-18 触发子集批刻意更新：基线 total 7→3——total>3 的健康屏面
    会先被触发闸 refuse，根本走不到改写；3 是过闸边界内。
    2026-09-18 专家化批刻意更新：rank 步补 displayed+batch——`_screen_baseline`
    只认真实上屏批次，纯试跑不再冒充屏面。）"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    class _ZeroFlow:
        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta(total=0, rows=[])

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _ZeroFlow)
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: ("worse query", True))
    ctx = {"chat_model": object(), "utterance": "重查一下",
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 3, "query": "human lung",
                                 "displayed": True, "batch": {"kind": "rank"}}}]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out["rejected"] is True and out["auto_shown"] is False
    assert out["reject_reason"] == "trial_degenerate"
    assert out["displayed"] is False and out["batch"] is None
    assert out["health"] == "degenerate"
    assert out["baseline"]["health"] == "healthy" and out["baseline"]["total"] == 3
    assert "新旧对照" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_both_healthy_advisory_only(fake_pipeline, monkeypatch):
    """（2026-09-13 rerank 处置批）：双方都健康时机械层**只报事实不判「更好」**——
    仅 advisory 对照（baseline/comparison_zh），不驳回也不自动上屏；
    上屏与否归模型经 results.show 裁决。
    （2026-09-18 触发子集批刻意更新：①基线 total 5→3——total>3 的健康屏面先被
    触发闸 refuse；②改写换成语义干净版「Human Lung」（约束逐位不变），语义闸
    不拦——带语义变化的改写在健康屏面被驳回由新钉
    test_loop_rerank_semantic_gate_rejects_on_healthy 接管。
    2026-09-18 专家化批刻意更新：rank 步补 displayed+batch（`_screen_baseline`
    只认真实上屏批次）。）"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow())
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("Human Lung", True))
    ctx = {"chat_model": object(), "utterance": "换个说法重查",
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 3, "query": "human lung",
                                 "displayed": True, "batch": {"kind": "rank"}}}]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out["health"] == "healthy"
    assert out["rejected"] is False and out["auto_shown"] is False
    assert out["semantic_blocked"] is False and out["reject_reason"] == ""
    assert out["displayed"] is False and out["batch"] is None
    assert out["baseline"]["health"] == "healthy" and out["baseline"]["total"] == 3
    assert "新旧对照" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_screen_baseline_ignores_rejected_trial(fake_pipeline, monkeypatch):
    """（2026-09-18 专家化批，WP1 回归钉）：`_screen_baseline` 只认真实上屏批次——
    屏面 A（rank displayed healthy）之后紧跟被拒试跑 B（rerank rejected degenerate、
    displayed=False、batch=None）时，基线必须仍取 A：B 只是情报，不是屏面。
    旧口径拿 B 当基线会把健康屏误判为空屏 → 试跑 healthy 即 auto-show 顶掉 A
    （绕过模型裁决）。本钉断言基线取 A，双方健康仅 advisory（不 auto-show、不驳回）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow())
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("Human Lung", True))
    ctx = {"chat_model": object(), "utterance": "换个说法重查",
           "steps": [
               {"verb": "rank", "ok": True,
                "result": {"health": "healthy", "total": 3, "query": "human lung",
                           "displayed": True, "batch": {"kind": "rank"}}},
               {"verb": "rerank", "ok": True,
                "result": {"health": "degenerate", "total": 0, "query": "worse query",
                           "rejected": True, "reject_reason": "trial_degenerate",
                           "displayed": False, "batch": None}},
           ]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out["baseline"]["health"] == "healthy"
    assert out["baseline"]["query"] == "human lung" and out["baseline"]["total"] == 3
    assert out["rejected"] is False and out["auto_shown"] is False
    assert out["displayed"] is False and out["batch"] is None
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_repair_adopted_auto_shows(fake_pipeline, monkeypatch):
    """（2026-09-18 专家化批 WP3，采纳档）：原句因未收录词弃权 + 受控词表唯一最近邻
    → 零 LLM 局部纠错打补丁（applied="repair"，改写器**不得被调**）；空屏面 +
    试跑 healthy → 自动上屏并如实标注纠正明细（disclosure_zh，核验义务交用户）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow(calls=calls))
    spy: list = []
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: spy.append(q) or ("x", True))
    out = ax._loop_rerank({"query": "有没有肺纤微化的单系胞图谱"}, None,
                          {"chat_model": object(), "utterance": "有没有肺纤微化的单系胞图谱"})
    assert spy == []  # repair 采纳档零 LLM——改写器不得被调
    assert out["applied"] == "repair" and out["rewritten"] is True
    assert out["rewritten_query"] == "有没有肺纤维化的单系胞图谱"
    repair = out["repair"]
    assert repair["adopted"] is True
    assert repair["adopted_fixes"][0]["term"] == "纤微化"
    assert repair["adopted_fixes"][0]["alias"] == "纤维化"
    assert out["health"] == "healthy" and out["auto_shown"] is True
    assert "纤微化" in out["disclosure_zh"] and "纤维化" in out["disclosure_zh"]
    assert "已按纠正后的查询上屏" in out["disclosure_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_repair_ambiguous_advisory_injected(fake_pipeline, monkeypatch):
    """（2026-09-18 专家化批 WP3，歧义档）：未收录词有等距多候选 → repair 不采纳
    （adopted=False，候选清单随结果 advisory 回传——歧义不靠命中数裁决），回落
    改写通道兜底，且候选菜单经 repair_zh 注入改写调用（机械事实交模型裁决）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow(calls=calls))
    seen: dict = {}

    def _fake_rewrite(model, q, **kw):
        seen.update(kw)
        return ("human lung adenocarcinoma", True)

    monkeypatch.setattr(ax, "_rewrite_query", _fake_rewrite)
    out = ax._loop_rerank({"query": "怕金森病的单细胞数据"}, None,
                          {"chat_model": object(), "utterance": "怕金森病的单细胞数据"})
    repair = out["repair"]
    assert repair["adopted"] is False and out["applied"] == "rewrite"
    assert [c["alias"] for c in repair["terms"][0]["candidates"]] == ["帕金森", "肾病"]
    repair_zh = str(seen.get("repair_zh") or "")
    assert "词表近邻候选" in repair_zh and "怕金森病" in repair_zh
    assert "帕金森" in repair_zh and "肾病" in repair_zh
    assert "未收录词纠错候选" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_repair_adopted_but_empty_probe_advisory(fake_pipeline, monkeypatch):
    """（2026-09-18 专家化批 WP3，覆盖校验措辞钉）：repair 采纳但纠正后试跑零存活
    → 纠错事实句降级为「仅供参考，请核实」（评测实证：可见误纠全部伴随零存活——
    覆盖不过的纠正不配断言式呈现）；空屏基线 + 空试跑两侧都不失东西：不上屏、
    不驳回、零 LLM（改写器不得被调）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow",
                        _parse_flow(total=0, rows=[], calls=calls))
    spy: list = []
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: spy.append(q) or ("x", True))
    out = ax._loop_rerank({"query": "有没有肺纤微化的单系胞图谱"}, None,
                          {"chat_model": object(), "utterance": "有没有肺纤微化的单系胞图谱"})
    assert spy == []  # repair 采纳档零 LLM——改写器不得被调
    assert out["applied"] == "repair" and out["repair"]["adopted"] is True
    assert out["health"] == "degenerate" and out["total"] == 0
    assert out["auto_shown"] is False and out["displayed"] is False
    assert out["rejected"] is False and "disclosure_zh" not in out
    assert "仅供参考" in out["comparison_zh"]
    assert "纤微化" in out["comparison_zh"] and "纤维化" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_repair_eligible_adversarial():
    """（2026-09-18 专家化批 WP3，资格闸）：正确缩写（HCC/snRNA-seq）/编号
    （GSE123456）/单字符一票不纠——对抗集误纠率必须 0；普通中文词/拼音词可纠。"""
    assert AX._repair_eligible("HCC") is False       # 含 ASCII 大写（缩写形）
    assert AX._repair_eligible("snRNA") is False
    assert AX._repair_eligible("GSE123456") is False  # 含数字（编号形）
    assert AX._repair_eligible("癌") is False         # 归一化后 <2 字符
    assert AX._repair_eligible("纤微化") is True
    assert AX._repair_eligible("pinxue") is True      # 纯小写拼音可纠（候选另受守卫）


def test_repair_candidates_ascii_substring_guard():
    """（2026-09-18 专家化批 WP3，子串守卫）：纯 ASCII 词的近似包含档要求别名
    ≥4 字符——「pinxue」不得假命中「pig」（3 字符别名，残渣切分假命中）；
    中文整串档不受影响（「纤微化」整串距离 1 命中「纤维化」）。
    （2026-09-18 WP5 拼音批刻意更新：pinxue 经拼音索引**精确**命中「贫血」
    （dist=0 via=pinyin）——真命中非假命中；守卫钉的是 pig 类假包含仍为空。）"""
    hits = AX._repair_candidates("pinxue")
    assert [h["alias"] for h in hits] == ["贫血"]
    assert hits[0]["via"] == "pinyin" and hits[0]["dist"] == 0
    hits = AX._repair_candidates("纤微化")
    assert hits and hits[0]["alias"] == "纤维化" and hits[0]["dist"] == 1


def test_repair_candidates_adversarial_tightening():
    """（2026-09-18 专家化批 WP3，评测驱动的候选收紧钉）：①整串档较短者须 ≥3
    字符——两字词单字符替换证据不足（『开头→舌头』评测实证误纠）；②包含档
    不允许跳过 alias 字符（『干性→脑干』假包含，脑字无处对齐）且词须严格长于
    别名（等长归整串档）；真残渣包含保留（『里贫雪病』→贫血/肾病 候选在列，
    歧义交 advisory）；正确 typo（浓毒症→脓毒症）保留。"""
    assert AX._repair_candidates("开头") == []
    assert AX._repair_candidates("干性") == []
    assert [c["alias"] for c in AX._repair_candidates("里贫雪病")] == ["肾病", "贫血"]
    assert [c["alias"] for c in AX._repair_candidates("浓毒症")] == ["脓毒症"]


def test_repair_absorbed_window():
    """（2026-09-18 专家化批 WP3，吸收判定）：丢失约束的出处紧贴替换点（±2 字符）
    = 被纠错粘连合并（『肺』+纤微化→肺纤维化）→ absorbed；出处远离替换点或替换词
    不在原句 → 不 absorbed。"""
    fixes = [{"term": "纤微化"}]
    assert AX._repair_absorbed("有没有肺纤微化的单系胞图谱", fixes, "tissue", "lung") is True
    far = "有没有肺纤微化的单系胞图谱，小鼠的"
    assert AX._repair_absorbed(far, fixes, "species", "mouse") is False
    assert AX._repair_absorbed(far, [{"term": "不在句中"}], "tissue", "lung") is False


def test_repair_diff_ok_whitelist():
    """（2026-09-18 专家化批 WP3，白名单）：纠错补丁的语义校验——**降级解析为基线**
    （剥掉未收录词，与主管线自动降级同一段 strip_terms）；硬/软新增必须落在被纠
    实体维度内，丢失必须被吸收，排除增删/角色迁移/极性翻转/原始数据·时间变化/
    自由词新增/解析无改善一律不采纳。采纳时记账 authorized_added/lost_absorbed。"""
    flow = _parse_flow()()
    fix = [{"term": "纤微化", "alias": "纤维化", "dim": "disease",
            "display": "肺纤维化", "targets": ["fibrosis"]}]
    # 采纳档：『纤微化』→『纤维化』——disease 新增（纠错产物，授权），tissue lung
    # 丢失但出处『肺』紧贴替换点（被吸收），其余上下文（modality）原样保留。
    ok, account = AX._repair_diff_ok(flow, "有没有肺纤微化的单系胞图谱",
                                     "有没有肺纤维化的单系胞图谱", fix, {})
    assert ok is True
    assert "disease" in (account["authorized_added"].get("hard") or {})
    assert account["lost_absorbed"]["hard"]["tissue"] == ["lung"]
    assert account["unresolved"] == {"before": ["纤微化"], "after": []}
    # 新增越出被纠维度 → 拒（粘连出意外语义）
    ok2, acc2 = AX._repair_diff_ok(flow, "有没有纤微化的数据",
                                   "有没有纤维化的数据，小鼠的", fix, {})
    assert ok2 is False and acc2["reject_reason"].startswith("added_outside_fix_dims")
    # 排除极性变动 → 拒
    ok3, acc3 = AX._repair_diff_ok(flow, "不要小鼠的纤微化数据",
                                   "不要大鼠的纤维化数据", fix, {})
    assert ok3 is False and acc3["reject_reason"] == "excluded_changed"
    # 解析无改善（未收录词数不减、补丁句仍不可执行）→ 拒
    ok4, acc4 = AX._repair_diff_ok(flow, "有没有 xyzabc 的数据",
                                   "有没有 xyzabd 的数据", fix, {})
    assert ok4 is False and acc4["reject_reason"] == "no_parse_improvement"


def test_repair_pinyin_word_exact_adopted():
    """（2026-09-18 WP5 拼音纠错，逐词档）：纯拼音整词精确命中拼音索引（中文 alias
    无声调连写，`vocabulary.pinyin_alias_index` 单通道）→ dist=0 via=pinyin；
    逐词拼音命中只自动采纳精确档唯一命中（ganai→肝癌），模糊档只进 advisory。"""
    hits = AX._repair_pinyin_hits("ganai")
    assert [h["alias"] for h in hits] == ["肝癌"]
    assert hits[0]["dist"] == 0 and hits[0]["via"] == "pinyin"
    flow = _parse_flow()()
    r = AX._repair_route(flow, "ganai 单细胞数据", {})
    assert r["adopted"] is True and r["patched_query"] == "肝癌 单细胞数据"
    assert [(f["term"], f["alias"]) for f in r["adopted_fixes"]] == [("ganai", "肝癌")]


def test_repair_pinyin_span_rejoins_split_words():
    """（2026-09-18 WP5 拼音纠错，span 档）：解析器按空格切碎的整词拼音
    （fei xian wei hua 各碎片太短不够逐词资格）被拼回长串查索引——相邻判定要求
    间隔只含空白/ASCII 标点（CJK「的」正确断开 run）；精确唯一命中 → 坐标倒序
    splice 采纳，被覆盖的碎片不再参与逐词采纳。"""
    spans = AX._repair_pinyin_spans("有没有 fei xian wei hua 的图谱",
                                    ["fei", "xian", "wei", "hua"])
    assert len(spans) == 1 and spans[0]["term"] == "fei xian wei hua"
    assert spans[0]["candidates"][0]["alias"] == "肺纤维化"
    assert spans[0]["candidates"][0]["dist"] == 0
    flow = _parse_flow()()
    r = AX._repair_route(flow, "有没有 fei xian wei hua 的图谱", {})
    assert r["adopted"] is True
    assert r["patched_query"] == "有没有 肺纤维化 的图谱"
    assert [(f["term"], f["alias"]) for f in r["adopted_fixes"]] == [
        ("fei xian wei hua", "肺纤维化")]


def test_repair_pinyin_homophone_never_auto_adopted():
    """（2026-09-18 WP5 拼音纠错，同音歧义纪律）：拼音同音碰撞（feiyan→肺癌/肺炎
    等距并列，词表实证 13 键碰撞）一票否决全部自动采纳——进 advisory 候选清单
    交改写通道/用户裁决（歧义不靠命中数裁决，可见误纠=0 红线优先于覆盖率）。"""
    hits = AX._repair_pinyin_hits("feiyan")
    assert sorted(h["alias"] for h in hits) == ["肺炎", "肺癌"]
    flow = _parse_flow()()
    r = AX._repair_route(flow, "feiyan 的数据", {})
    assert r["adopted"] is False and r["patched_query"] == ""
    assert sorted(c["alias"] for c in r["terms"][0]["candidates"]) == ["肺炎", "肺癌"]


def test_repair_substring_never_auto_adopted():
    """（2026-09-18 WP5 红线修复钉）：近似包含档（via="substring"）证据弱——残渣
    嵌影命中（『黏液纤毛→血液』窗口『黏液』dist=1，扩容集评测实证自动采纳后
    构成可见误纠红线事故；『区单细→单核』『艾滋潜伏→艾滋病』同档）——一律
    advisory 不自动采纳；候选情报保留（交改写通道/用户裁决）。"""
    hits = AX._repair_candidates("黏液纤毛")
    assert hits and hits[0]["alias"] == "血液" and hits[0]["via"] == "substring"
    flow = _parse_flow()()
    r = AX._repair_route(flow, "CF 咽部 snRNA，黏液纤毛。", {})
    assert r is not None and r["adopted"] is False
    assert any(c["alias"] == "血液" for c in r["terms"][0]["candidates"])
    # 真·残渣嵌影情报不丢：里贫雪病 → 贫血/肾病 候选仍在列（WP3 钉的 advisory 档）
    assert sorted(c["alias"] for c in AX._repair_candidates("里贫雪病")) == ["肾病", "贫血"]


def test_loop_rerank_trigger_gate_refuses_healthy_rich_screen(fake_pipeline, monkeypatch):
    """（2026-09-18 触发子集批，触发闸 pin ①）：屏面已核验健康且结果充足（total>3）
    → 如实拒绝（refused=True / refuse_reason=screen_healthy），**零 LLM、零试跑**
    （改写器与管线都不得被调）——好结果上重查只有下行风险；拒绝是数据不是故障。
    （2026-09-18 专家化批刻意更新：rank 步补 displayed+batch——`_screen_baseline`
    只认真实上屏批次，纯试跑不再冒充屏面。）"""
    ax, calls = fake_pipeline
    spy: list = []
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: spy.append(q) or ("x", True))
    ctx = {"chat_model": object(), "utterance": "换个说法重查",
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 7, "query": "human lung",
                                 "displayed": True, "batch": {"kind": "rank"}}}]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out["refused"] is True and out["refuse_reason"] == "screen_healthy"
    assert out["rejected"] is False and out["auto_shown"] is False
    assert out["batch"] is None and out["displayed"] is False
    assert "结果区已有不错的结果" in out["note_zh"] and "search.rerun" in out["note_zh"]
    assert spy == [] and calls == []  # 零 LLM、零试跑
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)
    # 回归钉（同批）：拒绝步本身不得冒充下一轮基线（它零检索事实）——把它压进
    # steps 后再调，触发闸必须仍按底下的 rank 步拒绝，而不是被「total=0」放行。
    ctx2 = {**ctx, "steps": ctx["steps"] + [{"verb": "rerank", "ok": True, "result": out}]}
    out2 = ax._loop_rerank({"query": "human lung"}, None, ctx2)
    assert out2["refused"] is True and out2["refuse_reason"] == "screen_healthy"
    assert spy == [] and calls == []


def test_loop_rerank_trigger_gate_unknown_rich_screen(fake_pipeline, monkeypatch):
    """（2026-09-18 触发子集批，触发闸 pin ②）：unknown（主检索直出、未经本环核验）
    且结果充足同样拒绝——unknown 是环内记账状态不是屏面质量问题，好屏面一视同仁。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: (_ for _ in ()).throw(AssertionError("不得调用改写器")))
    out = ax._loop_rerank({"query": "human lung"}, None,
                          {"chat_model": object(), "has_results": True, "result_total": 10})
    assert out["refused"] is True and out["refuse_reason"] == "screen_healthy"
    assert out["baseline"]["health"] == "unknown" and out["baseline"]["total"] == 10
    assert calls == []
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_trigger_gate_boundary_passes(fake_pipeline, monkeypatch):
    """（2026-09-18 触发子集批，触发闸 pin ③）：边界内（healthy 但 total=3）照常执行
    ——闸只拦「健康且充足」，小屏面仍是重检的合法触发面。
    （2026-09-18 专家化批刻意更新：rank 步补 displayed+batch——`_screen_baseline`
    只认真实上屏批次；本钉语义不变（过闸），但过闸理由回到设计口径。）"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow(calls=calls))
    spy: list = []
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: spy.append(q) or ("Human Lung", True))
    ctx = {"chat_model": object(),
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 3, "query": "human lung",
                                 "displayed": True, "batch": {"kind": "rank"}}}]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out.get("refused") is not True
    assert spy == ["human lung"] and calls  # 改写与试跑都真跑了
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_semantic_gate_rejects_on_healthy(fake_pipeline, monkeypatch):
    """（2026-09-18 触发子集批，语义闸 pin ①）：屏面健康（过触发闸的小屏面）+
    改写带未授权语义变化（新增疾病硬约束）→ 机械驳回 rejected=True +
    reject_reason=semantic_change（生死线扩档），屏面不动，语义对比明细如实附后。
    （2026-09-18 专家化批刻意更新：rank 步补 displayed+batch——`_screen_baseline`
    只认真实上屏批次，纯试跑不再冒充屏面。）"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow())
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("human lung adenocarcinoma", True))
    ctx = {"chat_model": object(), "utterance": "重查一下",
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 3, "query": "human lung",
                                 "displayed": True, "batch": {"kind": "rank"}}}]}
    out = ax._loop_rerank({"query": "human lung"}, None, ctx)
    assert out["rejected"] is True and out["reject_reason"] == "semantic_change"
    assert out["semantic_blocked"] is True
    assert out["health"] == "healthy"  # 试跑本身健康——驳回只因为语义被改
    assert out["displayed"] is False and out["batch"] is None
    assert "改写前后约束对比" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_semantic_gate_unknown_advisory_warns(fake_pipeline, monkeypatch):
    """（2026-09-18 触发子集批，语义闸 pin ②）：unknown 小屏面 + 语义被改 +
    试跑健康 → 不驳回也不自动上屏（机械层只报事实），advisory 对照附语义警示，
    上屏与否归模型经 results.show。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _parse_flow())
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda model, q, **_: ("human lung adenocarcinoma", True))
    out = ax._loop_rerank({"query": "human lung"}, None,
                          {"chat_model": object(), "has_results": True, "result_total": 2})
    assert out["rejected"] is False and out["auto_shown"] is False
    assert out["semantic_blocked"] is True
    assert "约束语义变化" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_fallback_to_original(fake_pipeline, monkeypatch):
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: (q, False))
    out = ax._loop_rerank({"query": "坏query"}, None, {"chat_model": None})
    assert out["rewritten"] is False
    assert out["rewritten_query"] == "坏query"
    assert calls[0]["query"] == "坏query"


def test_loop_rerank_degenerate_relax_live_digest(fake_pipeline, monkeypatch):
    """（2026-09-14 YOLO 批）：rerank 试跑仍 degenerate 时同样内嵌 relax_live——
    按**生效检索句**（rewritten_query）预演，与 `_loop_ask_relax` 弹卡口径同源。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf
    from dataset_recommender.retrieval.query_parser import parse_query

    class _ZeroFlow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                return [{"key": "dim:species", "kind": "drop", "label": "物种",
                         "count": 3, "candidates": []}]

        def _prepare_context(self, query, **kwargs):
            return (parse_query(query, None), [], [],
                    None, None, None, None, None)

        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta(total=0, rows=[])

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _ZeroFlow)
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: (q, False))
    out = ax._loop_rerank({"query": "human lung adenocarcinoma FASTQ"}, None,
                          {"chat_model": None})
    assert out["health"] == "degenerate" and out["rejected"] is False
    assert out["relax_live"] == [{"key": "dim:species", "label": "物种", "count": 3}]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


# ---------------------------------------------------------------- 结构化条件契约（设计约定）
#
# 环内 rank/rerank 只传 query/sources 曾丢失 facet/suppressed/lenient/date 结构化条件，
# 造成「同词重跑却放宽条件、uid 集合变化、弱批顶掉更优批」。修复后必须原样携带并 fail-closed。

def test_loop_rank_carries_structured_conditions(fake_pipeline):
    """（设计约定）：rank 把 facet/suppressed/lenient/date 原样带进管线——
    缺任何一项都是弱批顶掉好结果的回归。"""
    ax, calls = fake_pipeline
    ctx = {
        "search_sources": ["GEO"],
        "search_facet_filters": [{"dim": "species", "value": "homo sapiens"}],
        "search_suppressed_constraints": ["exclude:species"],
        "search_lenient_dims": ["disease"],
        "search_date_from": "2020-01-01",
        "search_date_to": "2024-12-31",
    }
    ax._loop_rank({"query": "human lung cancer"}, None, ctx)
    assert calls and calls[0]["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]
    assert calls[0]["suppressed_constraints"] == ["exclude:species"]
    assert calls[0]["lenient_dims"] == ["disease"]
    assert calls[0]["date_from"] == "2020-01-01"
    assert calls[0]["date_to"] == "2024-12-31"


def test_loop_rank_scope_kept_backfills_applied(fake_pipeline, monkeypatch):
    """（设计约定）：scope 完整保留时，payload 回填 applied_*（与 search.rerun 同义务）
    且显式日期出现在 interpretation 投影里（逐位相等才出批）。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(
        "dataset_recommender.app.recommend_rows.recommend_payload",
        lambda meta: {
            "ok": True, "result_total": meta.result_total, "query_constraints": meta.active_filters,
            "results": [{"dataset_name": "DS0", "species": "Human", "tissue": "lung",
                         "disease": "adenocarcinoma", "source": "GEO"}],
            "interpretation": {"intent": {"date_from": "2020-01-01", "date_to": "2024-12-31"}},
        },
    )
    ctx = {"search_sources": None, "search_date_from": "2020-01-01", "search_date_to": "2024-12-31",
           "search_facet_filters": [{"dim": "species", "value": "homo sapiens"}]}
    out = ax._loop_rank({"query": "human lung"}, None, ctx)
    assert out["batch"] is not None and out["batch"]["payload"]["ok"] is True
    assert out["batch"]["payload"]["applied_facets"] == [{"dim": "species", "value": "homo sapiens"}]
    assert out["batch"]["payload"]["applied_suppressed"] == []
    assert out["batch"]["payload"]["applied_lenient"] == []


def test_loop_rank_fail_closed_when_date_lost(fake_pipeline):
    """（设计约定）：显式日期在 interpretation 投影里带不出 → fail-closed——不回填 batch、
    如实带结构化标记，绝不放宽重跑顶掉已上屏的好结果。"""
    ax, calls = fake_pipeline  # fake recommend_payload 无 interpretation → 显式日期必然带不出
    ctx = {"search_sources": None, "search_date_from": "2020-01-01", "search_date_to": "2024-12-31"}
    out = ax._loop_rank({"query": "human lung"}, None, ctx)
    assert out["batch"] is None
    assert out["structured_context_lost"] is True
    assert out["disclosure_zh"]
    # 健康度改判 degenerate（现场丢失即「没有经条件核验的结果」），total/top 如实报试跑事实。
    assert out["health"] == "degenerate"


def test_loop_rerank_carries_structured_conditions(fake_pipeline, monkeypatch):
    """（设计约定）：rerank 的改写只换检索句，结构化条件同样原样带进管线。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: ("human lung adenocarcinoma", True))
    ctx = {"search_sources": ["GEO"], "search_facet_filters": [{"dim": "tissue", "value": "lung"}],
           "search_lenient_dims": ["disease"], "search_date_from": "2021-01-01"}
    ax._loop_rerank({"query": "肺癌的数据"}, None, ctx)
    assert calls and calls[0]["facet_filters"] == [{"dim": "tissue", "value": "lung"}]
    assert calls[0]["lenient_dims"] == ["disease"]
    assert calls[0]["date_from"] == "2021-01-01"


def test_loop_rerank_fail_closed_when_date_lost(fake_pipeline, monkeypatch):
    """（设计约定）：rerank 同样 fail-closed——显式日期带不出则不出批。"""
    ax, calls = fake_pipeline
    monkeypatch.setattr(ax, "_rewrite_query", lambda model, q, **_: ("human lung", True))
    ctx = {"search_sources": None, "search_date_from": "2020-01-01"}
    out = ax._loop_rerank({"query": "肺癌"}, None, ctx)
    assert out["batch"] is None
    assert out["structured_context_lost"] is True


# ---------------------------------------------------------------- 预算机械闸

def _state_with_steps(verbs):
    return {"utterance": "找找人类肺癌的数据",
            "steps": [{"verb": v, "ok": True} for v in verbs]}


def test_rank_budget_gate():
    assert AX.MAX_RANK == 2
    state = _state_with_steps(["rank", "rank"])
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "x"}, state)
    assert nxt is None and violation == ""
    assert "最多新检索" in refused and "预算已用完" in refused
    # 未用满 → 放行。
    nxt, *_ = AX._adjudicate_decide_obj(
        {"verb": "rank", "quoted": "找找人类肺癌的数据", "query": "human lung cancer"},
        _state_with_steps(["rank"]))
    assert nxt is not None and nxt["verb"] == "rank"


def test_rerank_budget_gate():
    assert AX.MAX_RERANK == 1
    state = _state_with_steps(["rerank"])
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "rerank", "quoted": "找找人类肺癌的数据", "query": "x"}, state)
    assert nxt is None and violation == ""
    assert "最多优化重检" in refused and "预算已用完" in refused


def test_show_budget_gate():
    """（2026-09-13 rerank 处置批，pin ⑧）：results.show 独立预算闸——用满后再提议
    机械拒绝、按 done 收尾、如实点名（防 show↔rerank ping-pong）。"""
    assert AX.MAX_SHOW == 2
    state = _state_with_steps(["results.show", "results.show"])
    nxt, note, refused, violation = AX._adjudicate_decide_obj(
        {"verb": "results.show", "quoted": "找找人类肺癌的数据"}, state)
    assert nxt is None and violation == ""
    assert "最多" in refused and "预算已用完" in refused and "更新结果区" in refused
    # 未用满 → 放行（handle 与已记步不同，不触发去重闸）。
    nxt, *_ = AX._adjudicate_decide_obj(
        {"verb": "results.show", "quoted": "找找人类肺癌的数据", "handle": "latest_rank"},
        _state_with_steps(["results.show"]))
    assert nxt is not None and nxt["verb"] == "results.show"


def test_budget_counters_count_failures_too():
    """提议过即消耗（不论成败）——防「失败换个说法再提」绕过上限空转。"""
    steps = [{"verb": "rank", "ok": False}, {"verb": "rank", "ok": True}]
    assert AX._rank_used(steps) == 2
    assert AX._rerank_used([{"verb": "rerank", "ok": False}]) == 1
    assert AX._show_used([{"verb": "results.show", "ok": False},
                          {"verb": "results.show", "ok": True}]) == 2
    # 预算注入段存在且与 search.rerun 段同构（拼装进双壳的逻辑与既有段同一代码路径）。
    assert "新检索预算已用完" in AX._RANK_BUDGET_BLOCK_ZH
    assert "优化重检预算已用完" in AX._RERANK_BUDGET_BLOCK_ZH
    assert "上屏" in AX._SHOW_BUDGET_BLOCK_ZH


# ---------------------------------------------------------------- 同批预算绕过

def test_batch_extras_count_against_rag_budget():
    """decide 侧：同批只读消费的第 2..N 个调用必须
    对「已执行 + 首步 + 已采纳同批步」的合成 steps 增量裁决——对原始 state 裁决时
    一枚 decide 回 3 个 rerank 会全过（MAX_RERANK=1 形同虚设）。"""
    state = {"utterance": "找找人类肺癌的数据", "steps": []}
    calls = [{"name": "rank", "args": {"query": q, "quoted": "找找人类肺癌的数据"}}
             for q in ("a", "b", "c")]
    accepted, dropped = AX._batch_readonly_extras(
        calls, {"verb": "rank", "query": "a", "quoted": "找找人类肺癌的数据"}, state)
    # MAX_RANK=2：首步占 1，同批至多再放行 1 个；第 3 个被预算闸机械剔除。
    assert [r["query"] for r in accepted] == ["b"] and dropped == 1
    # MAX_RERANK=1：首步已用满，同批 rerank 一个都不许过。
    calls = [{"name": "rerank", "args": {"query": q, "quoted": "找找人类肺癌的数据"}}
             for q in ("a", "b", "c")]
    accepted, dropped = AX._batch_readonly_extras(
        calls, {"verb": "rerank", "query": "a", "quoted": "找找人类肺癌的数据"}, state)
    assert accepted == [] and dropped == 2


def test_execute_batch_fuse_rechecks_rag_budget(monkeypatch):
    """execute 侧（与批内熔连同哲学）：主步/前序
    extra 真消耗预算后，后续 extra 执行前用当前实录重过预算闸——被剔的不执行、
    不记步、trace 如实留痕。"""
    monkeypatch.setattr(AX, "_audit_loop_tool", lambda *a, **k: None)
    monkeypatch.setitem(AX.LOOP_TOOLS["rank"], "run",
                        lambda slots, root, ctx=None: {
                            "query": str((slots or {}).get("query") or ""), "total": 1})
    runtime = SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))
    state = {
        "utterance": "找找人类肺癌的数据", "plan": {"verb": "none"},
        "steps": [{"verb": "rank", "ok": True, "slots": {"query": "old"}}],
        "loop_plan": {"verb": "rank", "slots": {"query": "a"}},
        "loop_batch": [{"verb": "rank", "slots": {"query": "b"}}],
    }
    out = AX.execute(state, runtime=runtime)
    # 既有 1 + 主步 1 = MAX_RANK(2) 用满 → 同批 extra 熔断：只记主步。
    assert [s["slots"]["query"] for s in out["steps"]] == ["a"]
    assert any("批内熔断" in str(t.get("label_zh") or "") and "预算" in str(t.get("detail") or "")
               for t in out["trace"])


# ---------------------------------------------------------------- 返回契约形状闸

def test_result_models_shape():
    with pytest.raises(Exception):
        SC.RankResult.model_validate({"query": "x"})  # 缺 total
    ok = SC.RankResult.model_validate({"query": "x", "total": 3})
    assert ok.displayed is False and ok.batch is None and ok.top == []
    with pytest.raises(Exception):
        SC.RerankResult.model_validate({"original_query": "x", "total": 1})  # 缺 rewritten*
    ok = SC.RerankResult.model_validate({
        "original_query": "x", "rewritten_query": "y", "rewritten": True, "total": 1})
    assert ok.rewritten is True
    # results.show 返回契约（2026-09-13 rerank 处置批）：shown 必填；成败两态都能接住。
    with pytest.raises(Exception):
        SC.ShowResult.model_validate({})  # 缺 shown
    ok = SC.ShowResult.model_validate({"shown": False, "note_zh": "没有找到试跑记录。"})
    assert ok.shown is False and ok.batch is None
    ok = SC.ShowResult.model_validate({
        "shown": True, "handle": "latest", "origin": "rank",
        "query_effective": "human lung", "total": 7, "overridden": False,
        "batch": {"kind": "rank", "label": "human lung", "query_raw": "找肺癌",
                  "query_effective": "human lung", "payload": {"ok": True}}})
    assert ok.shown is True and ok.origin == "rank" and ok.batch["kind"] == "rank"


# ---------------------------------------------------------------- 屏态机制（2026-09-13 rerank 处置批）

def test_screen_alert_block_zh():
    """（pin ⑥）「结果不健康」告警注入段——**纯情报员**：最近试跑 degenerate 时成段
    （点名 rerank 是对策之一，不改 workflow）；健康/无试跑时恒空段。"""
    degenerate = [{"verb": "rank", "ok": True,
                   "result": {"health": "degenerate", "total": 0,
                              "query": "human lung", "resolution_status": ""}}]
    block = AX._screen_alert_block_zh(degenerate)
    assert "结果不健康" in block and "命中 0 条" in block
    assert "rerank" in block and "对策" in block
    # 各退化原因如实分档。
    assert "解析层弃权" in AX._screen_alert_block_zh(
        [{"verb": "rank", "ok": True,
          "result": {"health": "degenerate", "total": 0,
                     "query": "q", "resolution_status": "abstained"}}])
    assert "向量语义兜底" in AX._screen_alert_block_zh(
        [{"verb": "rank", "ok": True,
          "result": {"health": "degenerate", "total": 5,
                     "query": "q", "resolution_status": "vector_fallback"}}])
    assert "没能完整保留当前筛选条件" in AX._screen_alert_block_zh(
        [{"verb": "rank", "ok": True,
          "result": {"health": "degenerate", "total": 5, "query": "q",
                     "structured_context_lost": True}}])
    # 最近试跑健康 → 恒空段（更早的 degenerate 步不回溯告警）。
    healthy_after = degenerate + [{"verb": "rank", "ok": True,
                                   "result": {"health": "healthy", "total": 7,
                                              "query": "human lung"}}]
    assert AX._screen_alert_block_zh(healthy_after) == ""
    assert AX._screen_alert_block_zh([]) == ""
    # 失败步（ok=False）不算试跑事实。
    assert AX._screen_alert_block_zh([{"verb": "rank", "ok": False}]) == ""


def test_loop_show_replays_and_shows(fake_pipeline):
    """（pin ⑦ 对照组）：句柄解析到 rank 试跑 → 重放同一条确定性管线 + 指纹复核通过
    → shown=True 出批（kind=试跑来源动词）；overridden 反映屏面原态。"""
    ax, calls = fake_pipeline
    ctx = {"search_sources": None, "utterance": "把结果放上去",
           "has_results": True, "result_total": 3,
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 7, "query": "human lung"}}]}
    out = ax._loop_show({}, None, ctx)
    assert out["shown"] is True and out["handle"] == "latest"
    assert out["origin"] == "rank" and out["query_effective"] == "human lung"
    assert out["total"] == 7 and out["overridden"] is True
    assert out["batch"]["kind"] == "rank"  # kind = 内容来源，前端按此渲染无感
    assert out["batch"]["payload"]["result_total"] == 7
    assert calls and calls[-1]["query"] == "human lung"
    assert calls[-1]["use_llm"] is False
    ax._LOOP_RESULT_MODELS["results.show"].model_validate(out)


def test_loop_show_fingerprint_mismatch_fails_closed(fake_pipeline, monkeypatch):
    """（pin ⑦）指纹不符 fail-closed：重放命中数与试跑记录不一致 → shown=False、
    如实说明「结果已过期」，绝不出批（不拿旧现场冒充现在的）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    class _ShiftedFlow:
        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta(total=3)  # 试跑记录 total=7 → 重放只有 3

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _ShiftedFlow)
    ctx = {"search_sources": None,
           "steps": [{"verb": "rank", "ok": True,
                      "result": {"health": "healthy", "total": 7, "query": "human lung"}}]}
    out = ax._loop_show({}, None, ctx)
    assert out["shown"] is False and out.get("batch") is None
    assert "不一致" in out["note_zh"] or "过期" in out["note_zh"]
    ax._LOOP_RESULT_MODELS["results.show"].model_validate(out)


def test_loop_show_handle_resolution_and_idempotency(fake_pipeline):
    """句柄三态 + 幂等档：latest_rank/latest_rerank/步号各认各的；试跑已上屏
    （displayed）时不重复出批（shown=True、batch=None、如实回报）。"""
    ax, calls = fake_pipeline
    steps = [
        {"verb": "rank", "ok": True,
         "result": {"health": "healthy", "total": 7, "query": "q1"}},
        {"verb": "rerank", "ok": True,
         # total 必须与替身管线重放的命中数一致（指纹复核的通过条件）。
         "result": {"health": "healthy", "total": 7, "rewritten_query": "q2"}},
    ]
    # latest = 最近一次试跑（rerank 步）；latest_rank 回溯到 rank 步。
    out = ax._loop_show({"handle": "latest"}, None, {"search_sources": None, "steps": steps})
    assert out["shown"] is True and out["origin"] == "rerank"
    assert out["query_effective"] == "q2"
    out = ax._loop_show({"handle": "latest_rank"}, None,
                        {"search_sources": None, "steps": steps})
    assert out["origin"] == "rank" and out["query_effective"] == "q1"
    # 数字步号（1 起）。
    out = ax._loop_show({"handle": "2"}, None, {"search_sources": None, "steps": steps})
    assert out["origin"] == "rerank"
    # 不存在的句柄 → fail-closed 如实回报。
    out = ax._loop_show({"handle": "9"}, None, {"search_sources": None, "steps": steps})
    assert out["shown"] is False and "没有找到" in out["note_zh"]
    # 幂等档：试跑已被 auto-show（displayed=True）→ 不重复出批。
    shown_steps = [{"verb": "rank", "ok": True,
                    "result": {"health": "healthy", "total": 7, "query": "q1",
                               "displayed": True}}]
    out = ax._loop_show({}, None, {"search_sources": None, "steps": shown_steps})
    assert out["shown"] is True and out["batch"] is None
    assert "已在结果区" in out["note_zh"]


# ---------------------------------------------------------------- 探测报告与双通道（2026-09-13 夜批）

def test_relax_to_suppressed_translation():
    """（pin ⑩）relax 槽 key → suppressed_constraints 的机械翻译：dim/exclude/raw/date/only
    五族全覆盖；only:X = 删其余全部；非法 key 抛 `_RerankRelaxParamError`（如实拒绝）。"""
    from dataclasses import replace as _replace

    from dataset_recommender.retrieval.query_parser import parse_query

    intent = parse_query("human lung adenocarcinoma FASTQ", None)
    assert intent.constraints.get("species") and intent.constraints.get("disease")
    assert intent.has_raw_data_required is True
    assert AX._relax_to_suppressed(intent, ["dim:disease"]) == ["include:disease"]
    assert AX._relax_to_suppressed(intent, ["raw"]) == ["raw:required"]
    # 多选合并 + 保序去重。
    assert AX._relax_to_suppressed(intent, ["dim:disease", "dim:tissue"]) == [
        "include:disease", "include:tissue"]
    # only:tissue = 只留 tissue 正向，其余（species/disease 正向 + FASTQ）全删。
    assert AX._relax_to_suppressed(intent, ["only:tissue"]) == [
        "include:species", "include:disease", "raw:required"]
    # 排除维与时间范围。
    intent2 = parse_query("human lung 不要小鼠 2024年以后", None)
    if intent2.excluded_constraints.get("species"):
        assert "exclude:species" in AX._relax_to_suppressed(intent2, ["exclude:species"])
    if intent2.date_from:
        assert AX._relax_to_suppressed(intent2, ["date"]) == ["date:range"]
    # 非法 key：未知维度 / 当前无此约束 / 无 FASTQ 要求时松 raw / 无日期时松 date。
    bad_intent = _replace(intent, has_raw_data_required=None, date_from="", date_to="")
    for bad in ("dim:nonsense", "dim:platform", "not-a-key"):
        with pytest.raises(AX._RerankRelaxParamError):
            AX._relax_to_suppressed(intent, [bad])
    for bad in ("raw", "date"):
        with pytest.raises(AX._RerankRelaxParamError):
            AX._relax_to_suppressed(bad_intent, [bad])


def test_rewrite_query_probe_injection():
    """（pin ⑪）probe_zh 非空时拼进改写调用的 HumanMessage（query 在前、报告在后）；
    缺省空串 → HumanMessage 与原行为逐位一致（历史兼容钉）。"""

    class _CapModel:
        def __init__(self):
            self.seen = None

        def invoke(self, messages):
            self.seen = messages
            return _FakeAnswer("mouse lung")

    m = _CapModel()
    assert AX._rewrite_query(m, "坏query", probe_zh="参考：探测报告") == ("mouse lung", True)
    assert m.seen[1].content == "坏query\n\n参考：探测报告"
    m2 = _CapModel()
    AX._rewrite_query(m2, "坏query")
    assert m2.seen[1].content == "坏query"


def test_relaxation_probe_zh_degrades_honestly():
    """（pin ⑫）探测是情报员不是闸：_prepare_context 异常 → 如实说明不阻塞；
    弃权态 → 明示「无可放松，优先考虑改写」；无硬约束 → 明示无可放松。"""

    class _BrokenFlow:
        def _prepare_context(self, *a, **k):
            raise RuntimeError("boom")

    assert "未能完成" in AX._relaxation_probe_zh(_BrokenFlow(), "q", None, {})

    class _AbstainFlow:
        def _prepare_context(self, *a, **k):
            return (SimpleNamespace(abstain=True), [], [], None, None, None, None, None)

    txt = AX._relaxation_probe_zh(_AbstainFlow(), "q", None, {})
    assert "没有可放松的维度" in txt and "改写查询本身" in txt

    class _NoConstraintFlow:
        class retriever:
            @staticmethod
            def relaxation_options(records, intent, **kw):
                return []

        def _prepare_context(self, *a, **k):
            return (SimpleNamespace(abstain=False), [], [], None, None, None, None, None)

    assert "没有可放松的条件" in AX._relaxation_probe_zh(_NoConstraintFlow(), "q", None, {})


def test_loop_rerank_relax_channel(fake_pipeline, monkeypatch):
    """（pin ⑬）relax 通道：跳过改写（零 LLM——改写函数被调用即炸）、原句重检、
    suppressed 合并翻译结果、applied/relaxed 如实标注、探测报告不回传。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    from dataset_recommender.retrieval.query_parser import parse_query

    class _FlowWithPrepare:
        def run_with_meta(self, p=None, **kwargs):
            calls.append(vars(p) if p is not None else kwargs)
            return _fake_meta()

        def _prepare_context(self, query, **kwargs):
            return (parse_query(query, None), [], [], None, None, None, None, None)

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _FlowWithPrepare)
    monkeypatch.setattr(ax, "_rewrite_query",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("不得改写")))
    out = ax._loop_rerank({"query": "human lung adenocarcinoma FASTQ",
                           "relax": ["dim:disease"]}, None,
                          {"search_sources": None, "chat_model": object()})
    assert out["applied"] == "relax" and out["rewritten"] is False
    assert out["relaxed"] == ["include:disease"]
    assert out["rewritten_query"] == "human lung adenocarcinoma FASTQ"  # 原句重检
    assert calls[0]["query"] == "human lung adenocarcinoma FASTQ"
    assert "include:disease" in (calls[0].get("suppressed_constraints") or [])
    assert out["probe_zh"] == ""
    assert "定向放松" in out["comparison_zh"]
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)


def test_loop_rerank_relax_invalid_key_rejected(fake_pipeline, monkeypatch):
    """（pin ⑭）relax 槽含报告外 key → bad_param 机械拒绝（不静默忽略、不改写）。"""
    ax, calls = fake_pipeline
    import dataset_recommender.app.workflow as wf

    from dataset_recommender.retrieval.query_parser import parse_query

    class _FlowWithPrepare:
        def run_with_meta(self, p=None, **kwargs):
            return _fake_meta()

        def _prepare_context(self, query, **kwargs):
            return (parse_query(query, None), [], [], None, None, None, None, None)

    monkeypatch.setattr(wf, "DatasetRecommendationWorkflow", _FlowWithPrepare)
    with pytest.raises(AX._RerankRelaxParamError):
        ax._loop_rerank({"query": "human lung adenocarcinoma",
                         "relax": ["dim:nonsense"]}, None, {"search_sources": None})
    assert calls == []  # 管线未被触发


def test_loop_rerank_rewrite_channel_returns_probe(fake_pipeline, monkeypatch):
    """（pin ⑮）改写通道：探测报告随改写调用注入（probe_zh 入参）且原样回传结果顶层
    （decide 侧据此决定下一轮 relax；改写模型与 decide 看到同一份）。"""
    ax, calls = fake_pipeline
    seen: list[str] = []

    def _spy_rewrite(model, q, usage_sink=None, probe_zh="", health_zh="", anchor_zh="",
                     repair_zh=""):  # 2026-09-18 专家化批 WP3：签名随 `_rewrite_query` 扩参
        seen.append(probe_zh)
        return "human lung adenocarcinoma", True

    monkeypatch.setattr(ax, "_rewrite_query", _spy_rewrite)
    monkeypatch.setattr(ax, "_relaxation_probe_zh", lambda *a, **k: "参考：探测报告文本")
    out = ax._loop_rerank({"query": "那个肺癌的数据"}, None,
                          {"chat_model": object(), "utterance": "那个肺癌的数据有没有"})
    assert seen == ["参考：探测报告文本"]
    assert out["probe_zh"] == "参考：探测报告文本"
    assert out["applied"] == "rewrite" and out["relaxed"] == []
    ax._LOOP_RESULT_MODELS["rerank"].model_validate(out)
