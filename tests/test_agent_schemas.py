# -*- coding: utf-8 -*-
"""agent_schemas（2026-08-06 pydantic 契约层）的确定性门。**全离线**：

- 返回模型：三个 LOOP_TOOLS 返回契约模型吃真形状/替身形状都过，破形状（缺键/错型）拦下；
- 形状闸接线：替身 run 返回破形状 → step.ok=False + error_code="bad_result_shape"，图不炸、
  汇报与账本如实（ok 语义 = 没抛异常**且形状合法**）；
- Step 模型：成功/失败两种实录形状 round-trip 与历史逐位一致；
- 数字交叉核验（机械后检第三路）：紧贴计数语境的数字与真实计数矛盾 → 弃用回退 +
  trace 留 discard_reason；真实数字/非计数语境数字/无基准 一律不拦；
- LLM-facing 入参 schema：与旧手写版逐字段 diff——只允许 source 多 enum、limit 多
  minimum/maximum，description 逐字不变、required 恒空；
- decide 各面规则壳的单锚点结构钉：scoped/rescue 均从 prompts/loop_core.md 装配
  （2026-08-31 单锚点化后 legacy 双壳与其字节钉退役——规则本体只剩锚点一份，
  钉字对象从「手抄旧版逐字」改为「锚点文件经同一装配进各面」）；
- 错误码 Literal 别名（CurateCode/UploadCode/ActionPlanCode）集合与 raise 点实况一致。
"""
import importlib.util
import json

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec, agent_schemas
from dataset_recommender.corpus import corpus_curation, uploads
from dataset_recommender.llm.llm_client import LLMConfig

CFG = LLMConfig(enable_llm=True, api_key="sk-schema-test")

needs_langgraph = pytest.mark.skipif(
    importlib.util.find_spec("langgraph") is None,
    reason="langchain 扩展未安装：图内接线测试跳过",
)


# ---------------------------------------------------------------- 返回模型：真形状/替身形状过，破形状拦

AE_ENTRY = {"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
            "local_count": 10, "online_recent": 12, "new_count": 2,
            "new_candidates": [{"accession": "E-MTAB-1", "title": "human lung atlas"},
                               {"accession": "E-MTAB-2", "title": "human lung tumor"}]}

SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_20260804_curate_arrayexpress.json", "warnings": []}


def test_db_status_result_accepts_real_and_stub_shapes():
    # 替身紧凑形状（test_agent_exec.py / test_agent_exec_loop.py 的既有契约：无 generated_at、
    # ledger 内层空缺）必须照过——形状闸钉的是键集/类型，不是「字段必须填满」。
    agent_schemas.DbStatusResult.model_validate(
        {"total_records": 0, "sources": [], "external_files": [], "recycle": [], "ledger": {}})
    # 真实形状（corpus_status.db_status 实产）照过。
    agent_schemas.DbStatusResult.model_validate({
        "generated_at": "2026-08-06T00:00:00+00:00",
        "sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                     "local_count": 5712, "snapshot_date": None}],
        "total_records": 5712,
        "external_files": [{"filename": "upload_x.json", "record_count": 3,
                            "curatable": True, "modified_at": "2026-08-01T00:00:00"}],
        "recycle": [{"original_filename": "upload_y.json", "record_count": 1,
                     "moved_at": "2026-08-02T00:00:00"}],
        "ledger": {"entries": 2, "by_endpoint": {"ep": 2},
                   "recent": [{"ts": "t", "endpoint": "ep", "query": "q", "records": 2,
                               "error": None}]},
    })


def test_check_updates_result_accepts_all_mode_variants():
    """mode 三态（online / snapshot / unknown）+ AE 在线失败形态（无 new_count）都合法。"""
    agent_schemas.CheckUpdatesResult.model_validate({
        "checked_at": "2026-08-06T00:00:00+08:00",
        "sources": [
            dict(AE_ENTRY),  # online 成功
            {"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
             "local_count": 10, "online_recent": None, "new_candidates": None,
             "note_zh": "在线比对这次没能完成。"},  # online 失败：new_count 缺席
            {"source": "hca", "label": "Human Cell Atlas", "mode": "snapshot",
             "local_count": 100, "snapshot_date": None, "note_zh": "只有本地副本。"},
            {"source": "不认识的源", "mode": "unknown", "note_zh": "不认识来源。"},
        ],
        "hint_zh": "",
    })


def test_search_online_result_requires_full_seven_keys():
    agent_schemas.SearchOnlineResult.model_validate(dict(SEARCH_OK))
    broken = dict(SEARCH_OK)
    del broken["record_count"]  # 七键缺任一 = 上游形状已破，必须拦
    with pytest.raises(Exception):
        agent_schemas.SearchOnlineResult.model_validate(broken)


def test_search_online_result_zero_write_filename_none():
    """候选全部已在库中的**零写入**是合法诚实回报
    （corpus 层契约钉在 test_corpus_curation.py：`filename is None`）——filename 键
    必填但值可为 None，形状闸不得把它误判成 bad_result_shape。"""
    zero = dict(SEARCH_OK, record_count=0, filename=None,
                warnings=["候选共 2 条全部已在库中（同编号或同链接），未重复入库。"])
    agent_schemas.SearchOnlineResult.model_validate(zero)
    broken = dict(zero)
    del broken["filename"]  # 键本身仍必填——缺键 = 形状破，照拦
    with pytest.raises(Exception):
        agent_schemas.SearchOnlineResult.model_validate(broken)


@pytest.mark.parametrize("model,broken", [
    (agent_schemas.DbStatusResult,
     {"sources": [], "external_files": [], "recycle": [], "ledger": {}}),        # 缺 total_records
    (agent_schemas.DbStatusResult,
     {"total_records": "5712", "sources": "不是列表", "external_files": [],      # 错型
      "recycle": [], "ledger": {}}),
    (agent_schemas.CheckUpdatesResult, {"checked_at": "t", "hint_zh": ""}),      # 缺 sources
    (agent_schemas.CheckUpdatesResult,
     {"checked_at": "t", "hint_zh": "",
      "sources": [dict(AE_ENTRY, mode="deleted")]}),                             # mode 越出三态
    (agent_schemas.SearchOnlineResult, dict(SEARCH_OK, record_count="不是数字")),  # 错型
    (agent_schemas.SearchOnlineResult, dict(SEARCH_OK, record_count=None)),      # 语义缺失（apply 没带回条数）
])
def test_result_models_reject_broken_shapes(model, broken):
    with pytest.raises(Exception):
        model.model_validate(broken)


def test_result_models_allow_unknown_extra_fields():
    """extra="allow"：additive 演进（新字段）不误杀——闸的对象是既有键的缺失/错型。"""
    agent_schemas.SearchOnlineResult.model_validate(dict(SEARCH_OK, future_field={"x": 1}))
    agent_schemas.DbStatusResult.model_validate({
        "total_records": 1, "sources": [dict(AE_ENTRY, mode="online", future=1)],
        "external_files": [], "recycle": [], "ledger": {}, "future_top": 2})


# ---------------------------------------------------------------- Step 模型：实录形状与历史逐位一致

def test_step_model_dump_matches_legacy_success_shape():
    legacy = {"verb": "curate.search_online", "verb_zh": "联网搜索入库",
              "slots": {"keywords": "人类肺"}, "ok": True, "result": dict(SEARCH_OK),
              "card_kind": "search_online", "readonly": False, "ms": 12}
    dumped = agent_schemas.Step(**legacy).model_dump(exclude_none=True)
    assert dumped == legacy
    assert "error" not in dumped and "error_code" not in dumped, "成功步不带 error 键（历史形状）"


def test_step_model_dump_matches_legacy_failure_shape():
    legacy = {"verb": "curate.search_online", "verb_zh": "联网搜索入库", "slots": {},
              "ok": False, "error": "hint", "error_code": "source_not_registered",
              "card_kind": "search_online", "readonly": False, "ms": 3}
    dumped = agent_schemas.Step(**legacy).model_dump(exclude_none=True)
    assert dumped == legacy
    assert "result" not in dumped, "失败步不带 result 键（历史形状）"


def test_step_model_rejects_missing_required_field():
    with pytest.raises(Exception):
        agent_schemas.Step(verb_zh="x", ok=True, card_kind="db_status", readonly=True)


def test_loop_result_models_cover_the_whole_registry():
    """返回契约模型与 LOOP_TOOLS 注册表同 verb 集（替身与真表共享同一份形状闸）。"""
    assert set(agent_schemas.LOOP_RESULT_MODELS) == set(agent_exec.LOOP_TOOLS)


# ---------------------------------------------------------------- 形状闸接线（图内，langgraph）

class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（用尽后 pop 抛 IndexError）。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


def _tool_call(verb, **args):
    from langchain_core.messages import AIMessage

    return AIMessage(content="", tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


def _plan(utterance, model):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )


@pytest.fixture
def _tmp_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_checklist_call(monkeypatch):
    """清单轻量调用统一 stub（本文件钉钉图内行为/schema 契约，不含清单语义
    FakeModel 应答序列不预置清单应答）。"""
    monkeypatch.setattr(agent_exec, "_task_checklist_call",
                        lambda *a, **k: ([], 0, ""))


def _install_tools(monkeypatch, **runs):
    """按真动词名替换 LOOP_TOOLS 的 run（元数据保持真实形状；形状闸按 verb 查模型表，
    替身结果同样过闸——替身形状与真表同约在此被真闸住）。"""
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


@needs_langgraph
def test_broken_result_shape_fails_the_step_honestly(monkeypatch, _tmp_project_root):
    """形状闸接线：替身 db_status 返回残缺形状（缺 total_records）→ ValidationError 按
    工具失败同路记——step.ok=False + error_code="bad_result_shape"，图不炸、确定性汇报
    如实说「没有完成」，账本落 ok=False 行。"""
    _install_tools(monkeypatch, **{
        "curate.db_status": lambda slots, root: {
            "sources": [], "external_files": [], "recycle": [], "ledger": {}},  # 缺 total_records
    })
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="汇报数据库的当前状态", confidence="high", reason="问库况"),
        # decide/narrate 的 LLM 全缺席（answers 用尽）→ 确定性兜底
    )
    plan, trace = _plan("汇报数据库的当前状态", model)
    steps = plan.get("steps") or []
    assert len(steps) == 1 and steps[0]["ok"] is False, "残缺形状绝不许带 ok=True 出图"
    assert steps[0].get("error_code") == "bad_result_shape"
    assert "result" not in steps[0], "失败步形状与历史逐位一致（无 result 键）"
    assert "不符合登记的形状契约" in steps[0]["error"], "上屏只出人读中文， pydantic 原文不上屏"
    assert "没有完成" in (plan.get("report_zh") or ""), "确定性兜底如实说这一步没成"
    assert [t["ok"] for t in trace if t["node"] == "execute"] == [False]
    rows_path = _tmp_project_root / ".userdata" / "curate_net_ledger.jsonl"
    rows = [json.loads(ln) for ln in rows_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert [r["ok"] for r in rows] == [False], "形状闸拦下的步同样落审计（ok=False）"


@needs_langgraph
def test_good_result_shape_keeps_step_ok(monkeypatch, _tmp_project_root):
    """闸的另一侧：形状合法的替身结果照常 ok=True（闸只拦破形状，不误伤好结果）。"""
    _install_tools(monkeypatch, **{
        "curate.check_updates": lambda slots, root: {
            "checked_at": "t", "sources": [dict(AE_ENTRY)], "hint_zh": ""},
    })
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
    )
    plan, _ = _plan("检查ArrayExpress是否有更新", model)
    steps = plan.get("steps") or []
    assert len(steps) == 1 and steps[0]["ok"] is True
    assert steps[0]["result"]["sources"][0]["new_count"] == 2, "落盘仍是原始 dict（门卫不改写）"


# ---------------------------------------------------------------- 数字交叉核验（机械后检第三路）

def _check_step(new_count=2, candidates=2):
    entry = dict(AE_ENTRY, new_count=new_count,
                 new_candidates=[{"accession": f"E-MTAB-{i}", "title": f"t{i}"}
                                 for i in range(candidates)])
    return {"verb": "curate.check_updates", "verb_zh": "检查来源更新", "slots": {}, "ok": True,
            "result": {"checked_at": "t", "sources": [entry], "hint_zh": ""},
            "card_kind": "check_updates", "readonly": True, "ms": 1}


def _search_step(record_count=2):
    return {"verb": "curate.search_online", "verb_zh": "联网搜索入库", "slots": {}, "ok": True,
            "result": dict(SEARCH_OK, record_count=record_count),
            "card_kind": "search_online", "readonly": False, "ms": 1}


@pytest.mark.parametrize("report,steps,expected", [
    # 矛盾：紧贴计数语境的数字不属于任何真实计数 → 拦
    ("已联网搜到 5 条并入库。", [_search_step(2)], True),
    ("搜到了 3 条新数据。", [_search_step(2)], True),
    ("检查完成，疑似新增 3 条。", [_check_step(2, 2)], True),
    ("发现 7 条新增候选。", [_check_step(2, 2)], True),
    ("已为你下载 20 条。", [_search_step(2), _check_step(2, 2)], True),
    # 不拦：数字与真实计数一致（record_count / new_count / new_candidates 实列条数任一）
    ("已联网搜到 2 条并入库。", [_search_step(2)], False),
    ("检查到 2 条疑似新增，已联网搜到 2 条并入库。", [_check_step(2, 2), _search_step(2)], False),
    ("10x 与 ArrayExpress 都检查了。", [_check_step(2, 2)], False),  # 「10x」的数字不贴「条」
    ("ArrayExpress 没有更新，不需要下载。", [_check_step(0, 0)], False),  # 零新增如实说（无数字声称）
    # 不拦：非计数语境的数字（收录/记录/文件个数不归这一路判）
    ("目录共收录 774 条、来自 9 个来源。", [_check_step(2, 2)], False),
    ("官方源最近 10 条里 2 条目录里还没有。", [_check_step(2, 2)], False),  # online_recent 不是基准
    ("发现的是 E-MTAB-12345 这条。", [_check_step(2, 2)], False),  # 编号里的数字不贴「条」
    # 不拦：没有基准可比（steps 里没有成功的 search/check 步）
    ("已联网搜到 5 条并入库。", [], False),
])
def test_report_miscounts_steps_unit(report, steps, expected):
    assert agent_exec._report_miscounts_steps(report, steps) is expected


def test_true_counts_skip_failed_steps():
    failed = dict(_search_step(20), ok=False, error="x")
    del failed["result"]
    assert agent_exec._step_true_counts([failed]) == set(), "失败步不产计数基准"


@needs_langgraph
def test_miscount_report_is_discarded_with_reason_in_trace(monkeypatch, _tmp_project_root):
    """端到端：入库 2 条，LLM 汇报却写「搜到 5 条」→ 数字交叉核验判矛盾、弃用回退
    （兜底如实「搜到 2 条」），trace 留 discarded_report_zh + discard_reason="count_mismatch"。"""
    _install_tools(monkeypatch, **{
        "curate.check_updates": lambda slots, root: {
            "checked_at": "t", "sources": [dict(AE_ENTRY)], "hint_zh": ""},
        "curate.search_online": lambda slots, root: dict(SEARCH_OK),
    })
    from langchain_core.messages import AIMessage
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，已联网搜到 5 条并入库。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库", model)
    report = plan.get("report_zh") or ""
    assert "5 条" not in report, "改写数量的汇报必须弃用"
    assert "搜到 2 条" in report, "兜底汇报与真实计数同一批事实"
    assert plan.get("report_source") == "deterministic"
    narrate_entries = [t for t in trace if t["node"] == "narrate"]
    assert narrate_entries[-1].get("discard_reason") == "count_mismatch", "原因码进 trace 留痕"
    assert "5 条" in (narrate_entries[-1].get("discarded_report_zh") or "")


@needs_langgraph
def test_matching_count_report_is_kept(monkeypatch, _tmp_project_root):
    """核验不误伤：汇报数字与真实计数一致 → 原样保留、标 llm、无 discard 留痕。"""
    _install_tools(monkeypatch, **{
        "curate.search_online": lambda slots, root: dict(SEARCH_OK),
    })
    model = _FakeModel(
        _tool_call("curate.search_online", quoted="联网搜人类肺数据入库", source="ArrayExpress",
                   keywords="人类肺", confidence="high", reason="在线找"),
    )
    from langchain_core.messages import AIMessage
    model.answers = [
        model.answers[0],
        AIMessage(content='{"done": true}'),
        AIMessage(content="已联网搜到 2 条人类肺数据并入库。"),
    ]
    plan, trace = _plan("联网搜人类肺数据入库", model)
    assert plan.get("report_zh") == "已联网搜到 2 条人类肺数据并入库。"
    assert plan.get("report_source") == "llm"
    narrate_entries = [t for t in trace if t["node"] == "narrate"]
    assert "discard_reason" not in narrate_entries[-1]


# ---------------------------------------------------------------- LLM-facing 入参 schema：与旧手写版逐字段 diff

def _legacy_tool_parameters(spec):
    """2026-08-06 前 `_tool_specs` 的**手写版**逐字重建（diff 基准；冻结在此，防跟着新版漂移）。"""
    props = {
        "quoted": {
            "type": "string",
            "description": "用户原话里逐字出现的一段连续文字；执行类动作必填，给不出就改选 none。",
        },
        "confidence": {"type": "string", "enum": ["high", "low"], "description": "拿不准填 low。"},
        "reason": {"type": "string", "description": "一句中文理由，20 字以内。"},
    }
    if spec.kind == AP.EXEC:
        props["cancelled"] = {
            "type": "boolean",
            "description": "用户明确说不做这个动作时填 true（动词照选，由执行层决定不执行）。",
        }
    if spec.verb in AP.ROUTE_QUERY_VERBS:
        props["effective_query"] = {
            "type": "string",
            "description": "完整、可独立执行的检索句（本类动词必填；其余动词不填）。",
        }
    slot_descriptions = {
        # 2026-08-07 性能批：source 槽补「多点名来源只填第一个」——真机探针 b05/d08 坐实
        # 模型把「10x Genomics, ArrayExpress」挤进一个槽、被点名源护栏拦到 AgentPlanInvalid。
        # 2026-08-08 B1a：keywords 补出处锚（j03/l07）、target 补文件名实例。
        # （source 的多来源具体例在真机探针里把 k08 首步动词引偏，撤——见开发日志 B1a 条。）
        # source 描述按 verb 分派（三个 curate 动词各有口径）
        # 名单不再抄进文字（真源 = 生产侧 enum）；本 diff 基准随刻意更新同步。
        "source": (
            "数据来源，受控规范名（候选见本字段清单）。"
            "用户原话点名了来源时**必填**（填规范名）；**点名多个来源时只填最先点名的那个**"
            "（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；没点名就不填（不填=查全部）。"
        ),
        "keywords": (
            "联网搜的关键词：从原话提取主题词（病种、组织、技术等），"
            "不含「联网搜一下/检查/有没有」这类操作词；没有主题就不填。"
            "联网源（ArrayExpress / CELLxGENE / HuBMAP / Single Cell Portal）都是**英文源**："
            "主题词优先给英文（「人类肺」→ human lung），照填中文大概率搜不到。"
            "主题词必须有真实出处：原话里找得到（可以翻译成英文），"
            "或逐字取自已完成步骤的真实结果（如检查更新发现的疑似新增条目标题）；"
            "两头都没有就不填、不发明。"
        ),
        "species": "物种（如 Human / Mouse）；用户没说就不填。",
        "target": "用户点名的对象原文片段（如 upload_mouse_lung.json 这类文件名、或编号）；没有就不填。",
        # 2026-08-16 检索工具化 Phase 1：search.rerun 的 query 槽专职描述（与生产侧
        # `_SLOT_DESCRIPTIONS_ZH` 逐字一致——diff 基准随新槽位登记同步）。
        "query": (
            "改写后的检索句：把当前查询换成规则更容易正确解析的说法，语义等价、"
            "不新增用户没表达的条件；当前没有可改的查询就不填。"
        ),
        # display 槽已随 2026-09-13 rerank 处置批退役（上屏唯一出口是 results.show
        # 动词）——diff 基准同步摘除；新增 handle 槽（results.show 的试跑句柄），
        # target_route 与 reason 通用兜底（route.request 登记）
        # ——与生产侧 `_SLOT_DESCRIPTIONS_ZH` 逐字一致。
        "handle": (
            "要上屏的试跑结果句柄：latest（最近一次检索/重查试跑，缺省值）/ latest_rank / "
            "latest_rerank / 步号（如 \"3\"）；没有就不填（=latest）。"
        ),
        "target_route": (
            "要换到的处理路线：search=检索向（找数据/改条件/贴编号）、"
            "action=动作向（下载/联网搜库/检查更新/入库/管护）、"
            "general=全能兜底（拿不准就走它）；必填。"
        ),
        "reason": "补充理由（一句中文）；没有就不填。",
        # 2026-08-18 四工具批：compare 的 a/b 与 compat/fair 的 uid（与生产侧
        # `_SLOT_DESCRIPTIONS_ZH` 逐字一致——diff 基准随新槽位登记同步）。
        "a": (
            "第一个数据集的**编号或名称**（可选）：用户原话点名了对比对象就填（如 GSE…、"
            "E-MTAB…、cxg:…、数据集名）；「第一条/第二个/这个/它」这类**指代词不是编号**，"
            "不要填——不填时缺省会取当前结果第一条（结论里会说明）。"
        ),
        "b": (
            "第二个数据集的**编号或名称**（可选）：原话点名了才填；「第一个/前两条/这个/它」"
            "这类**指代词不是编号**，不要填——不填时缺省会取当前结果第二条（结论里会说明）。"
        ),
        "uid": (
            "数据集的**编号或名称**（可选）：用户原话点名了才填（如 GSE…、E-MTAB…、cxg:…、"
            "数据集名）；「第一条/第二条/这个/它」这类**指代词不是编号**，不要填——"
            "不填时缺省会取当前结果第一条（输出里会说明）。"
        ),
        # 2026-08-20：cite.export 的 uids 数组槽（与生产侧 `_SLOT_DESCRIPTIONS_ZH`
        # 逐字一致——diff 基准随新槽位登记同步）。
        "uids": (
            "要导出引文的数据集编号清单（数组，最多 20 个；元素可以是真实 dataset_uid，"
            "也可以是同批前序检索结果的占位引用 `$<N>.top[<i>].dataset_uid`，两种可混用）。"
            "用户原话点名了编号/条数才填；没点名就不填（缺省 = 当前结果）。"
        ),
        # 2026-09-13 夜批 rerank 双通道：relax 数组槽（与生产侧 `_SLOT_DESCRIPTIONS_ZH`
        # 逐字一致——diff 基准随新槽位登记同步）。
        "relax": (
            "定向放松清单（数组，元素取上次 rerank 结果 probe_zh 报告里列出的 key："
            "dim:维度 / exclude:维度 / raw / date / only:维度，可多选）。"
            "填了就走零-LLM 定向放松重检（不再改写）；报告里没有的 key 不要发明，"
            "拿不准就不填（走默认改写通道）。"
        ),
    }
    # 2026-08-17：query/reason 的按动词专职描述（与生产侧 `_QUERY_SLOT_DESCRIPTIONS_ZH` /
    # `_REASON_SLOT_DESCRIPTIONS_ZH` 逐字一致——diff 基准随缝合同步）。
    _per_verb_query_descriptions = {
        "rank": "要检索的完整检索句：物种/组织/疾病/平台等实体写规范名，去掉口语操作词；必填。",
        "rerank": "质量差、需要优化的**原始**检索句（逐字取当前查询或原话，不要自己先改）；必填。",
    }
    _per_verb_reason_descriptions = {
        "rerank": "为什么需要优化检索词（一句中文，如「原句太口语化」）；没有就不填。",
        "route.request": "为什么要换路线（一句中文，如「用户要找数据、不是管护动作」）；没有就不填。",
        # 2026-09-14 YOLO ask.relax 刻意更新（与生产侧 `_REASON_SLOT_DESCRIPTIONS_ZH`
        # 逐字一致——diff 基准随新动词登记同步）。
        "ask.relax": "为什么判定当前查询的条件不合理/互相打架（一句中文，会展示给用户）；没有就不填。",
    }
    # 评审裁决：三个 curate 动词的 source 描述分派（与生产侧 `_SOURCE_SLOT_DESCRIPTIONS_ZH`
    # 逐字一致——diff 基准随 2026-08-08 刻意更新同步）。
    _per_verb_source_descriptions = {
        "curate.search_online": (
            "数据来源，受控规范名（联网搜只覆盖本字段候选里列出的源；候选外的来源系统会如实"
            "回答接不了）。用户原话点名了来源时**必填**（填规范名）；**点名多个来源时只填最先"
            "点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；没点名就不填。"
        ),
        "curate.check_updates": (
            "数据来源，受控规范名（候选见本字段清单；其中只有部分来源能在线比对，离线快照源会"
            "如实报告本地快照信息）。用户原话点名了来源时**必填**（填规范名）；**点名多个来源时"
            "只填最先点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源挤进一个槽；"
            "没点名就不填（不填=查全部）。"
        ),
        "curate.sync_updates": (
            "数据来源，受控规范名（候选见本字段清单；只有能在线比对且有入库适配器的来源才会"
            "真的自动入库，其余来源如实写明做不到）。用户原话点名了来源时**必填**（填规范名）；"
            "**点名多个来源时只填最先点名的那个**（剩下的由后续步骤逐一处理），不许把多个来源"
            "挤进一个槽；没点名就不填（不填=查全部）。"
        ),
    }
    for slot in spec.slots:
        if slot == "limit":
            props["limit"] = {"type": "integer", "description": "用户明确说了条数才填数字，否则不填。"}
        else:
            # display 布尔槽已退役（2026-09-13 rerank 处置批）——类型分派同步摘除；
            # 现役槽只剩 array（uids/relax）与 string 两类。
            prop_type = ("array" if slot in ("uids", "relax") else "string")
            prop = {"type": prop_type,
                    "description": ((_per_verb_source_descriptions.get(spec.verb)
                                     if slot == "source" else None)
                                    or (_per_verb_query_descriptions.get(spec.verb)
                                        if slot == "query" else None)
                                    or (_per_verb_reason_descriptions.get(spec.verb)
                                        if slot == "reason" else None)
                                    or slot_descriptions.get(
                                        slot, f"{slot} 槽位：按原话里的说法填，没有就不填。"))}
            if prop_type == "array":
                prop["items"] = {"type": "string"}
            props[slot] = prop
    return {"type": "object", "properties": props, "required": []}


#: 新版相对旧版**唯一允许**增多的键（约束信息）：source→enum；limit→minimum/maximum；
#: uids→items（数组元素类型，2026-08-20 批）。
_ALLOWED_NEW_KEYS = {"source": {"enum"}, "limit": {"minimum", "maximum"},
                     "uids": {"items"}, "relax": {"items"}}


def test_generated_schema_diff_against_legacy_is_only_tightening():
    """pydantic 生成版与旧手写版逐字段 diff：字段集相同；每个属性的 description 逐字不变；
    只允许 source/limit 的约束信息增多；其余属性逐字段相等。"""
    for spec in AP.VERB_SPECS:
        legacy = _legacy_tool_parameters(spec)
        new = agent_schemas.verb_parameters_schema(spec)
        assert new["type"] == legacy["type"] == "object"
        assert new["required"] == legacy["required"] == [], "required 恒空（提示层与裁决层不合并）"
        assert set(new["properties"]) == set(legacy["properties"]), f"{spec.verb} 字段集漂移"
        for name, old_prop in legacy["properties"].items():
            new_prop = new["properties"][name]
            assert new_prop.get("description") == old_prop.get("description"), (
                f"{spec.verb}.{name} 的 description 逐字不变（LLM 提示面稳定）")
            assert new_prop.get("type") == old_prop.get("type")
            added = set(new_prop) - set(old_prop)
            removed = set(old_prop) - set(new_prop)
            assert not removed, f"{spec.verb}.{name} 丢了键：{removed}"
            assert added <= _ALLOWED_NEW_KEYS.get(name, set()), (
                f"{spec.verb}.{name} 只允许 {_ALLOWED_NEW_KEYS.get(name, set())} 增多，实际多了 {added}")
            for key in added:
                assert key in _ALLOWED_NEW_KEYS[name]


def test_generated_schema_constraint_values():
    """约束值抽查：confidence 枚举逐位；source 枚举**按 verb 分派**（评审裁决：
    check/sync = CHECK_UPDATE_SOURCES labels、search_online = SOURCE_ADAPTERS labels，
    程序取自真实注册表）；limit 上下界与裁决层 MAX_LIMIT 同源。"""
    from dataset_recommender.corpus import corpus_curation as cc

    pack = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME["pack.download"])["properties"]
    assert pack["confidence"]["enum"] == ["high", "low"]
    assert pack["limit"]["minimum"] == 1 and pack["limit"]["maximum"] == AP.MAX_LIMIT
    check = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME["curate.check_updates"])["properties"]
    assert check["source"]["enum"] == [str(s["label"]) for s in cc.CHECK_UPDATE_SOURCES.values()], \
        "check_updates 的 source 枚举必须程序取自 CHECK_UPDATE_SOURCES labels（不硬抄字符串）"
    assert "Zenodo" in check["source"]["enum"] and "ENCODE" in check["source"]["enum"]
    search = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME["curate.search_online"])["properties"]
    assert search["source"]["enum"] == [str(s["label"]) for s in cc.SOURCE_ADAPTERS.values()], \
        "search_online 的 source 枚举必须程序取自 SOURCE_ADAPTERS labels"
    assert "Zenodo" in search["source"]["enum"], "能搜的源必须在枚举里（旧版漏 Zenodo=N1 病灶）"
    assert "ENCODE" not in search["source"]["enum"], "接不了的源不得在枚举里（旧版含 ENCODE=N1 病灶）"
    # prompt 规则表与枚举同一真源：候选串逐位一致
    for verb in ("curate.check_updates", "curate.search_online", "curate.sync_updates"):
        enum = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME[verb])["properties"]["source"]["enum"]
        assert agent_schemas.source_candidates_zh(verb) == " / ".join(enum), verb
    route = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME["search.new"])["properties"]
    assert "effective_query" in route and "cancelled" not in route, "路由类不带 cancelled"
    none = agent_schemas.verb_parameters_schema(AP.VERB_BY_NAME["none"])["properties"]
    assert set(none) == {"quoted", "confidence", "reason"}


def test_tool_specs_uses_generated_parameters():
    """agent_exec._tool_specs 的 parameters 即生成版（接线点抽查）：17 动词全部过 pydantic。"""
    tools, name_to_verb = agent_exec._tool_specs()
    assert {t["function"]["name"] for t in tools} == {v.replace(".", "_") for v in AP.ACTION_VERBS}
    for tool in tools:
        verb = name_to_verb[tool["function"]["name"]]
        assert tool["function"]["parameters"] == agent_schemas.verb_parameters_schema(
            AP.VERB_BY_NAME[verb])
        assert tool["function"]["parameters"]["required"] == []


def test_decide_table_is_generated_from_the_registry():
    """清单行程序取自 LOOP_TOOLS 的 decide_zh（顺序用显式钉住的 _DECIDE_VERB_ORDER）；
    带 source 槽的行尾拼候选清单（与 schema 枚举同一真源）。"""
    parts = []
    for verb in agent_exec._DECIDE_VERB_ORDER:
        row = f"   - {verb}（{agent_exec.LOOP_TOOLS[verb]['decide_zh']}"
        if "source" in AP.VERB_BY_NAME[verb].slots:
            row += f"；source 候选：{agent_schemas.source_candidates_zh(verb)}"
        parts.append(row + "）")
    expected = "\n".join(parts)
    assert agent_exec._decide_tool_table_zh() == expected
    # 全表进 general 套件面（单锚点化后规则壳经装配生成，钉装配产物而非中间常量）。
    assert expected in agent_exec._SCOPED_DECIDE_RULES_BY_SUITE["general"]["tools"]
    assert set(agent_exec._DECIDE_VERB_ORDER) == set(agent_exec.LOOP_TOOLS), \
        "decide 清单顺序表与注册表漂移 = 有工具没进 decide prompt"


# ------------------------------------------------- 钉

def test_decide_rules_carry_reverse_condition_and_suspected_new_sentences():
    """纪律钉（2026-08-31 单锚点化后改指新锚点，legacy 双壳字节钉已退役）：
    - （反向条件丢失）：锚点 loop_core.md 诚实不变量第 5 条携带「反向条件」句
      经同一装配进 scoped 各面与 rescue 面；
    - （检出疑似新增被当成已入库）：纪律在 search_online 的 decide_zh 工具描述里
      （注册表唯一真源，程序拼进各面工具表）——「不许拿『新增里已经有了』提前替系统放弃」。"""
    for rules in agent_exec._SCOPED_DECIDE_RULES_BY_SUITE.values():
        for prompt in rules.values():
            assert "反向条件" in prompt and "A 没有时 B 反而必须做" in prompt, \
                "反向条件句必须随锚点进每个 scoped 面"
    assert "A 没有时 B 反而必须做" in \
        agent_exec._SCOPED_DECIDE_RULES_BY_SUITE["rescue"]["tools"]
    assert "不许拿「新增里已经有了」提前替系统放弃" in \
        agent_exec.LOOP_TOOLS["curate.search_online"]["decide_zh"]


def test_sync_updates_when_zh_carries_the_topic_word_litmus():
    """（劣质指令 sync 磁吸回潮）：sync_updates 的 when_zh 必须带机械判定口径——
    原话出现任何主题词（疾病/物种/组织/技术）就是限定主题，一律 check + search 两步。
    2026-08-08 复跑后刻意更新：口径**前置进首句**（缀句尾时 JSON 兜底档的模型只读
    首句样例就磁吸，探针 j03 三轮两轮仍选 sync）。"""
    when = AP.VERB_BY_NAME["curate.sync_updates"].when_zh
    assert "（**仅限不限定主题**——原话里出现任何主题词" in when, "判定口径必须前置进首句"
    assert "限定主题一律 check_updates + search_online 两步，不选本工具）" in when


def test_check_updates_when_zh_distinguishes_topic_question_from_source_question():
    """（主题问句误选 check）：check_updates 的 when_zh 必须带对照句——
    问**主题**在网上有没有新数据是 search_online；问**来源**有没有更新才选它。"""
    when = AP.VERB_BY_NAME["curate.check_updates"].when_zh
    assert "问某个**主题**在网上有没有新数据" in when
    assert "curate.search_online；问**来源**有没有更新才选它。" in when


def test_network_loop_tools_are_the_registry_minus_db_status():
    """联网暂停的禁提面 = 注册表减去纯本地的工具——注册表将来加工具时本钉逼出
    「新工具联不联网」的显式归类，防 `_NETWORK_LOOP_TOOLS` 静默漂移。
    2026-08-16 刻意更新：search.rerun 跑的是本地检索管线（与主检索同一条，不触网），
    与 db_status 同属「纯本地」排除项。
    刻意更新：rank/rerank 跑本地检索管线（不触网）、route.request 是
    本地元动词（只改后续路线，不触网），三者同属「纯本地」排除项。
    2026-08-17 rb1 刻意更新：curate.rollback 是本地文件操作（回收站移动/写字节，不触网），
    同属「纯本地」排除项。
    2026-08-18 四工具批刻意更新：compare.datasets / cite.export / compat.find /
    fair.check 全部本地（对比/引文/兼容/FAIR 自检不触网），同属「纯本地」排除项。
    2026-09-13 rerank 处置批刻意更新：results.show 重放的是本地检索管线
    （use_llm=False 确定性重放 + 指纹复核，不触网），同属「纯本地」排除项。
    2026-09-14 YOLO 批刻意更新：ask.relax 只做本地确定性预演生成选择卡
    （零 LLM、不触网），同属「纯本地」排除项。"""
    assert agent_exec._NETWORK_LOOP_TOOLS == frozenset(
        set(agent_exec.LOOP_TOOLS) - {"curate.db_status", "search.rerun", "rank", "rerank",
                                      "route.request", "curate.rollback",
                                      "compare.datasets", "cite.export",
                                      "compat.find", "fair.check", "results.show",
                                      "ask.relax"})
    assert "联网暂停" in agent_exec._NETWORK_MORATORIUM_BLOCK_ZH
    # 注入段如实写明离线 db_status 与离线快照源检查都不连坐
    assert "离线工具 curate.db_status 与**离线快照源**" in agent_exec._NETWORK_MORATORIUM_BLOCK_ZH


# ---------------------------------------------------------------- 错误码 Literal 别名（纯标注，集合钉住）

def test_error_code_literals_match_actual_raise_points():
    from typing import get_args

    assert set(get_args(corpus_curation.CurateCode)) == {
        "bad_action", "bad_param", "token_mismatch", "invalid_json", "no_records", "too_large",
        "duplicate_content", "network_error", "source_not_registered", "no_candidates",
        "unknown_file", "not_curatable",
        # sync 整任务锁冲突（真实 raise 点在 sync_updates_critical_section）
        "sync_busy",
        # 按 operation_id 撤回时查无此操作（真实 raise 点在 recall_sync_operation）
        "unknown_operation",
    }
    assert set(get_args(uploads.UploadCode)) == {
        "bad_file", "bad_encoding", "invalid_json", "no_records", "too_large", "journal_failed",
        "lock_busy",   # 2026-08-10 P1-5：跨进程摄取锁等待超时（真实 raise 点在 _acquire_os_ingest_lock）
    }
    assert set(get_args(AP.ActionPlanCode)) == {"empty_input", "too_large"}
    # 纯标注的运行时证明：字面量构造照常（不 enforce），码属性原样可读。
    err = corpus_curation.CurateError("network_error", "x")
    assert err.code == "network_error" and str(err) == "network_error: x"
