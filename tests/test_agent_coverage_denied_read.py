# -*- coding: utf-8 -*-
"""2026-08-08 探针 v3 病灶修复批的确定性门（全离线：fake model + monkeypatch LOOP_TOOLS）：

1. **搜索覆盖闸** `_search_coverage_violation`（病灶 a06/d01：首搜成功后 decide 换措辞/加过滤
   再搜一遍同主题，rule 10 没拦住、第三步才被指纹去重拦下）——同 source 既往成功搜索的
   token 并集覆盖新提议即机械停环；多主题/换 source/首搜失败重试一律放行。
2. **第五路机械后检 denied_read** `_denies_done_read`（病灶 d07：明明搜过 X，LLM 汇报却说
   「未搜索 X」——既有四路管不到只读动作的否认）；结果动词（「没搜到」）是零结果时的诚实
   措辞，刻意豁免。
3. **decide 核销流程化钉字**（病灶 b12/g04：马拉松指令提前 finish）——INTRO 核销句 /
   rule 10 马拉松实例 / finish **必填 completion_report**（2026-08-08 第二刀：核销从纯外化
   articulation 升级为机械闸——`_unfinished_business` 扫出报告自认「没做」的事项，
   decide 拒收收尾并把缺口回灌重问一次；第二次仍自认未完成 → fail-safe 接受并如实标注）。
   2026-08-08 探针 v4 核销闸升级（`_completion_report_veto` 三形态，本文件第 4 节矩阵）：
   形态 A「已做」无合法步骤号（b08/k01）、形态 B 豁免行夹带依赖借口词（k03/k08）。
   2026-08-08 探针 v5 病灶2 再加举证责任：豁免行（条件不成立/做不到）同样必须引用
   合法步骤号（据第几步的结果得出），空口豁免与没做同罪——finish 工具 description 同步。
   prompt 全文字节钉在 tests/test_agent_schemas.py，本文件只钉病灶相关的点。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：多步循环测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-coverage-test")

UTTER = "上网找点小鼠脑的单细胞数据回来"
SEARCH_OK = {"source_label": "ArrayExpress", "query": "小鼠脑", "species": "",
             "sample_titles": ["mouse brain cortex seq"], "record_count": 2,
             "filename": "upload_20260808_curate_arrayexpress.json", "warnings": []}


def _search_step(keywords, record_count=2, source="ArrayExpress", ok=True):
    """既往 search_online 实录步（ok=False 时无 result——失败步不计入覆盖）。"""
    step = {"verb": "curate.search_online", "verb_zh": "联网搜索入库", "ok": ok,
            "card_kind": "search_online", "readonly": False,
            "slots": {"source": source, "keywords": keywords}, "ms": 1}
    if ok:
        step["result"] = dict(SEARCH_OK, query=keywords, record_count=record_count)
    else:
        step["error"] = "网络抖动"
    return step


def _check_step(source="ArrayExpress"):
    """既往 check_updates 实录步（来源感知：slots 与结果 payload 同点名一源）。"""
    return {"verb": "curate.check_updates", "verb_zh": "检查来源更新", "ok": True,
            "card_kind": "check_updates", "readonly": True, "slots": {"source": source},
            "ms": 1,
            "result": {"checked_at": "2026-08-08T00:00:00+08:00", "hint_zh": "",
                       "sources": [{"source": source.lower(), "label": source, "mode": "online",
                                    "local_count": 10, "online_recent": 12, "new_count": 0,
                                    "snapshot_date": "2026-08-01", "new_candidates": []}]}}


# ---------------------------------------------------------------- 共享助手：token 化口径

def test_keyword_content_tokens_is_the_shared_tokenizer():
    """`_keyword_content_tokens` 从 `_ungrounded_keyword_tokens` 逐位抽出：切词/停用词口径不变。"""
    assert agent_exec._keyword_content_tokens("Human Lung data") == ["human", "lung"]
    assert agent_exec._keyword_content_tokens("小鼠脑 数据集") == ["小鼠脑", "数据集"]
    assert agent_exec._keyword_content_tokens("") == []
    # 抽取零行为变化：接地核验的两端（不接地 token 列出 / 原话逐字命中放行）原样工作
    assert agent_exec._ungrounded_keyword_tokens("single cell", "上网找点数据回来") == [
        "single", "cell"]
    assert agent_exec._ungrounded_keyword_tokens("小鼠脑", UTTER) == []


# ---------------------------------------------------------------- 搜索覆盖闸：违规矩阵

def test_coverage_blocks_reworded_same_topic_after_hit():
    """a06 型：首搜有结果，decide 换措辞/加过滤（species）再搜同主题 → covered。"""
    steps = [_search_step("mouse brain", record_count=2)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress",
         "keywords": "mouse brain", "species": "小鼠"}, steps) == "covered"


def test_coverage_blocks_subset_rewording_after_hit():
    """d01 型：首搜有结果后换成子集措辞（human lung cancer → human lung）→ covered。"""
    steps = [_search_step("human lung cancer", record_count=2)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": "human lung"},
        steps) == "covered"


def test_coverage_allows_a_second_distinct_topic():
    """多主题合法用例不得误伤：「human lung 和 mouse brain 都要」第二搜放行。"""
    steps = [_search_step("human lung", record_count=2)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": "mouse brain"},
        steps) is None


def test_coverage_allows_same_topic_on_another_source():
    """换 source 再搜是另一条路（覆盖按同 source 判定）→ 放行。"""
    steps = [_search_step("human lung", record_count=2, source="ArrayExpress")]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "keywords": "human lung", "source": "ENCODE"},
        steps) is None


def test_coverage_allows_retry_after_a_failed_first_search():
    """首搜失败（ok=False）不计入覆盖——换不换措辞都放行。"""
    steps = [_search_step("mouse brain", ok=False)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": "mouse brain"},
        steps) is None


def test_coverage_allows_one_retry_after_zero_result():
    """零结果后允许一次换措辞重试（同 source 成功搜索步数 < 2）→ 放行。"""
    steps = [_search_step("mouse brain", record_count=0)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": "mouse brain"},
        steps) is None


def test_coverage_blocks_second_retry_after_zero_results():
    """零结果已重试过一次（两步成功搜索、token 并集覆盖）→ retry_exhausted。"""
    steps = [_search_step("mouse brain", record_count=0),
             _search_step("mouse brain cortex", record_count=0)]
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": "mouse brain"},
        steps) == "retry_exhausted"


def test_coverage_empty_keywords_and_no_prior_cases():
    """空 keywords：既往同 source 有成功搜索 → covered_empty；无既往搜索 → 放行。"""
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "source": "ArrayExpress", "keywords": ""},
        [_search_step("mouse brain", record_count=2)]) == "covered_empty"
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.search_online", "keywords": ""}, []) is None
    # 非 search_online 动词不归本闸管
    assert agent_exec._search_coverage_violation(
        {"verb": "curate.check_updates", "source": "ArrayExpress"},
        [_search_step("mouse brain", record_count=2)]) is None


# ---------------------------------------------------------------- 覆盖闸：_adjudicate_decide_obj 集成

def test_adjudicate_stops_covered_research_with_declined_sentence():
    """decide 提议被覆盖闸拦下 → done（nxt=None）+ declined 句非空（narrate 兜底要点名）。"""
    state = {"utterance": UTTER, "steps": [_search_step("小鼠脑", record_count=2)]}
    nxt, note, declined, _fb = agent_exec._adjudicate_decide_obj(
        {"verb": "curate.search_online", "quoted": UTTER,
         "source": "ArrayExpress", "keywords": "小鼠脑", "species": "小鼠"}, state)
    assert nxt is None, "同主题加料重搜必须按 done 停环"
    assert "搜索主题重复" in note
    assert "已经搜过" in declined, "被拦下的动作必须有一句人读说明带给汇报"


# ---------------------------------------------------------------- 第五路后检 denied_read：判定矩阵

def test_denied_read_bare_denial_is_caught():
    """裸否认「没有搜索」且有成功读步 → 直接矛盾。"""
    assert agent_exec._denies_done_read(
        "没有搜索。", [_search_step("human lung", record_count=2)]) is True


def test_denied_read_named_source_actually_checked_is_caught():
    """尾窗点名的来源 ∈ 成功读步实际触碰的来源集 → 否认了真做过的检查。"""
    steps = [_check_step("ArrayExpress")]
    assert agent_exec._denies_done_read(
        "检查了 10x，未检查 ArrayExpress 的更新。", steps) is True


def test_denied_read_topic_tokens_actually_searched_is_caught():
    """d07 型：双主题都真搜过，汇报却说「未搜索 mouse brain」→ 主题 token 否认命中。"""
    steps = [_search_step("human lung", record_count=2),
             _search_step("mouse brain", record_count=2)]
    assert agent_exec._denies_done_read(
        "已搜索 human lung，未搜索 mouse brain。", steps) is True


def test_denied_read_result_denial_is_honest_and_kept():
    """「没搜到 / 未检查出」是**结果**否认（record_count=0 时的诚实措辞）——刻意不拦。"""
    steps = [_search_step("human lung", record_count=0)]
    assert agent_exec._denies_done_read("没搜索到相关数据。", steps) is False
    assert agent_exec._denies_done_read(
        "未检查出疑似新增。", [_check_step("ArrayExpress")]) is False


def test_denied_read_unknown_topic_or_untouched_source_is_kept():
    """拿不准不拦：「未搜索 multiome」但搜索关键词确不含 multiome（实话）；
    「未检查 ArrayExpress」但该来源本就没碰过（实话）。裸「ENCODE」刻意不是别名
    （常见英文词，词表只认 encode portal/project 等限定形）——点名来源用可识别别名。"""
    assert agent_exec._denies_done_read(
        "未搜索 multiome 数据。", [_search_step("human lung", record_count=2)]) is False
    assert agent_exec._denies_done_read(
        "未检查 ArrayExpress 的更新。", [_check_step("10x")]) is False


def test_denied_read_needs_a_successful_read_step():
    """参与门槛：没有 ok 的读步（空 steps / 读步失败）→ 本路不判定。"""
    assert agent_exec._denies_done_read("没有搜索。", []) is False
    assert agent_exec._denies_done_read(
        "没有搜索。", [_search_step("human lung", ok=False)]) is False


# ---------------------------------------------------------------- _report_contradiction_reason：原因码 + 五路优先级

def test_denied_read_reason_code_and_route_priority():
    """denied_read 是独立原因码；优先级 untouched_source → count_mismatch → denied_read
    → 写动作两路（2026-08-08 四路扩五路）。"""
    steps = [_search_step("mouse brain", record_count=2)]
    # 前面四路都不命中时落到 denied_read
    assert agent_exec._report_contradiction_reason(
        "已搜索 human lung，未搜索 mouse brain。", steps) == "denied_read"
    # 点名未触碰来源优先于 denied_read
    assert agent_exec._report_contradiction_reason(
        "没搜索 mouse brain，也没检查 GEO 的更新。", steps) == "untouched_source"
    # 数字交叉核验优先于 denied_read
    assert agent_exec._report_contradiction_reason(
        "搜到 5 条，没搜索 mouse brain。", steps) == "count_mismatch"


# ---------------------------------------------------------------- decide 核销流程化钉字（病灶 b12/g04）

def test_decide_prompt_carries_the_checklist_discipline():
    """探针 v3 病灶 b12/g04（马拉松指令做两件就 finish）的核销纪律钉。
    2026-08-31 单锚点化后改指新锚点：纪律本体在 prompts/loop_core.md（intro 核销句 +
    诚实不变量第 4 条 + finish 契约），经同一装配进 scoped 各面双壳与 rescue 面双壳。"""
    faces = [p for rules in agent_exec._SCOPED_DECIDE_RULES_BY_SUITE.values()
             for p in rules.values()]
    faces += [agent_exec._SCOPED_DECIDE_RULES_BY_SUITE["rescue"]["tools"],
              agent_exec._SCOPED_DECIDE_RULES_BY_SUITE["rescue"]["json"]]
    for prompt in faces:
        assert "「已做 / 没做 / 条件不成立」" in prompt
        assert "做完一件不许就收工" in prompt
        assert "有一件没交代系统会拒收收尾并重问一次" in prompt
    # tools 壳的 finish 指令必须带 completion_report 必填（JSON 壳没有 finish 概念，不过此钉）。
    for rules in agent_exec._SCOPED_DECIDE_RULES_BY_SUITE.values():
        assert "completion_report" in rules["tools"]
    assert "completion_report" in \
        agent_exec._SCOPED_DECIDE_RULES_BY_SUITE["rescue"]["tools"]


def test_finish_tool_requires_completion_report():
    """finish 的 completion_report **必填**钉（2026-08-08 探针 b12/g04 提前收工病灶第二刀：
    核销从可选 checklist 的纯外化 articulation 升级为机械闸输入——`_unfinished_business`
    扫描报告，自认还有没做的事就拒收收尾并回灌重问一次）。钉住参数面，防回退。
    2026-08-08 探针 v5 病灶2 刻意更新：description 同步「豁免也要举证（据第几步的结果）」。"""
    finish = next(t for t in agent_exec._DECIDE_TOOL_SPECS
                  if t["function"]["name"] == "finish")
    params = finish["function"]["parameters"]
    assert params["required"] == ["completion_report"], \
        "completion_report 必须必填——核销是机械闸的输入，不再是可选外化约束"
    assert params["properties"]["completion_report"]["type"] == "string"
    assert "checklist" not in params["properties"]
    assert "据第几步" in params["properties"]["completion_report"]["description"], \
        "description 必须写明豁免行的举证责任（与 `_completion_report_veto` 机械闸同口径）"


# ---------------------------------------------------------------- 覆盖闸端到端（a06 型全链路）

class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（("raise",) 抛异常——模拟通道异常；
    用尽后 pop 抛 IndexError——decide/narrate 都按「LLM 缺席」fail-safe 处理）。"""

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
    return AIMessage(content="", tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    return tmp_path


def test_same_topic_research_is_stopped_by_the_coverage_gate(monkeypatch, _tmp_project_root):
    """a06 型端到端：首搜成功后 decide 换措辞（加 species 过滤）再搜同主题——指纹去重管不到
    槽位变体，覆盖闸拦下：只真搜一遍、decide trace 如实记「搜索主题重复」。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {
            "run": lambda slots, root: dict(SEARCH_OK),
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False,
        },
    })
    model = _FakeModel(
        _tool_call("curate.search_online", quoted=UTTER, source="ArrayExpress",
                   keywords="小鼠脑", confidence="high", reason="联网搜"),
        # decide：加 species 过滤再搜一遍同主题（槽位不同，指纹去重拦不到）
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": UTTER,
             "source": "ArrayExpress", "keywords": "小鼠脑", "species": "小鼠"},
            ensure_ascii=False)),
        AIMessage(content="已联网搜索到 2 条并入库。"),
    )
    plan, trace = agent_exec.plan_with_agent(
        UTTER, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model,
    )
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.search_online"], \
        "同主题重搜被覆盖闸拦下，只真搜一遍"
    assert any("搜索主题重复" in t["detail"] for t in trace if t["node"] == "decide")


# ---------------------------------------------------------------- finish 核销硬闸：_unfinished_business 矩阵

def test_unfinished_business_markers_hit_without_exemption():
    """命中词（没做/未做/还没有做/还没做/待做）且同行无豁免词 → 返回该行（原文引用）。
    2026-08-08 探针 v4 刻意更新：签名加 `n_steps`（形态 A 的步骤号越界判定用），
    本钉的「已做（第1步）」合法引用行传入 n_steps=1 保持放行口径。"""
    assert agent_exec._unfinished_business(
        "1. 检查ArrayExpress更新：已做（第1步）\n2. 再检查ENCODE：没做", 1
    ) == "2. 再检查ENCODE：没做"
    assert agent_exec._unfinished_business("2. 搜人类肺数据：未做") == "2. 搜人类肺数据：未做"
    assert agent_exec._unfinished_business("2. 搜人类肺数据：还没有做") is not None
    assert agent_exec._unfinished_business("2. 搜人类肺数据：还没做") is not None
    assert agent_exec._unfinished_business("2. 搜人类肺数据：待做") is not None


def test_unfinished_business_same_line_exemption_passes():
    """同行有豁免词（条件不成立/做不到/无法/不需要）**且带步骤号举证** → 放行：
    「条件不成立（第1步无新增）所以没做」是 rule 7 语义下的合法收尾，机械闸不误伤。
    2026-08-08 探针 v5 病灶2 刻意更新：豁免行也要举证（据第几步的结果得出）。"""
    assert agent_exec._unfinished_business(
        "2. 搜人类肺：没做——条件不成立（第1步没有新增）", 1) is None
    assert agent_exec._unfinished_business("2. 打包下载：做不到（第1步来源认不出），没做", 1) is None
    assert agent_exec._unfinished_business("2. 联网检查：无法访问（据第1步网络失败），未做", 1) is None
    assert agent_exec._unfinished_business("2. 下载：不需要（第1步无新增），没做", 1) is None


def test_unfinished_business_exemption_without_step_evidence_is_vetoed():
    """病灶2（探针 v5：k02/k09/k10/k11/l07 提前收工残余）——豁免行的**举证责任**：
    空口「条件不成立/做不到」（无步骤号或号码越界）与没做同罪否决；中文数字步骤号
    举证齐全 → 放行（对照）。"""
    assert agent_exec._unfinished_business(
        "2. 搜人类肺：没做——条件不成立（没有新增）", 1) is not None
    assert agent_exec._unfinished_business("2. 汇报库容：做不到，没做", 1) is not None
    assert agent_exec._unfinished_business(
        "2. 搜人类肺：没做——条件不成立（第3步没有新增）", 1) is not None, "号码越界同罪"
    assert agent_exec._unfinished_business(
        "2. 搜人类肺：没做——条件不成立（第一步没有新增）", 1) is None


def test_unfinished_business_all_done_or_empty_passes():
    """全部「已做」（带合法步骤号）/ 空报告 / 报告缺席 → 放行（拿不到核销结论时维持
    fail-safe 接受）。2026-08-08 探针 v4 刻意更新：传 n_steps=2 让两个「已做（第N步）」
    都是合法引用。"""
    assert agent_exec._unfinished_business(
        "1. 检查更新：已做（第1步）\n2. 汇报条数：已做（第2步）", 2) is None
    assert agent_exec._unfinished_business("") is None
    assert agent_exec._unfinished_business(None) is None


# ---------------------------------------------------------------- finish 核销硬闸升级：形态 A/B 矩阵（探针 v4）

def test_unfinished_business_done_claim_must_cite_a_real_step():
    """形态 A（探针 v4 病灶 b08/k01：没跑 db_status 却在核销里自称告知了库容）——
    标注「已做」的行必须引用**真实存在**的步骤号：无步骤号 / 号码越界 / 第0步
    都与「自认没做」同罪否决；合法引用（阿拉伯/中文数字、含空白写法）放行。"""
    # 无步骤号 → 否决（返回该行原文）
    assert agent_exec._unfinished_business("2. 汇报库容：已做", 1) == "2. 汇报库容：已做"
    # 号码越界（只完成 1 步却写第2步）→ 否决
    assert agent_exec._unfinished_business("2. 汇报库容：已做（第2步）", 1) is not None
    # 第0步 → 否决
    assert agent_exec._unfinished_business("2. 汇报库容：已做（第0步）", 1) is not None
    # 合法引用 → 放行（阿拉伯 / 中文数字 / 数字两侧带空白）
    assert agent_exec._unfinished_business("2. 汇报库容：已做（第2步）", 2) is None
    assert agent_exec._unfinished_business("2. 汇报库容：已做（第二步）", 2) is None
    assert agent_exec._unfinished_business("1. 检查：已做（第 1 步）", 1) is None
    # 混合报告：先否决后面那行「已做无号」（前面合法行不放倒钩）
    assert agent_exec._unfinished_business(
        "1. 检查：已做（第1步）\n2. 汇报库容：已做", 1) == "2. 汇报库容：已做"


def test_unfinished_business_dependency_excuse_is_not_a_valid_exemption():
    """形态 B（探针 v4 病灶 k03/k08：彼此独立的事写成「因前置步骤失败而未执行」）——
    豁免词命中行夹带依赖借口词（前置/前面/前件/该步骤/上一步）→ 不是合法豁免。
    合法豁免对照（条件不成立引用「第1步」不是依赖借口词）不误伤。"""
    assert agent_exec._unfinished_business(
        "2. 检查ENCODE：未做——前置步骤失败，无法执行", 1) is not None
    assert agent_exec._unfinished_business(
        "3. 看看库容：没做——因上一步失败而做不到", 1) is not None
    assert agent_exec._unfinished_business(
        "3. 再检查B：待做，该步骤失败后无法继续", 1) is not None
    assert agent_exec._unfinished_business(
        "2. 搜人类肺：没做——条件不成立（第1步检查没有新增）", 1) is None
    # 「该来源」不是依赖借口词（借口词表收的是「该步骤」）；2026-08-08 病灶2 起补步骤号举证
    assert agent_exec._unfinished_business(
        "2. 检查：做不到（第1步：该来源无法在线比对），没做", 1) is None


# ---------------------------------------------------------------- finish 核销硬闸：veto 回灌重问（b12/g04 型全链路）

MARATHON_UTTER = "检查ArrayExpress更新，完了告诉我库里多少条"


def _marathon_registry(monkeypatch):
    """马拉松用例的假注册表：check 返零新增、db_status 返 0 条（替身形状同真表契约）。"""
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.check_updates": {
            "run": lambda slots, root: {"checked_at": "t", "sources": [], "hint_zh": ""},
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True},
        "curate.db_status": {
            "run": lambda slots, root: {
                "generated_at": "t", "sources": [], "total_records": 0,
                "external_files": [], "recycle": [],
                "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
    })


def _marathon_plan(model):
    return agent_exec.plan_with_agent(
        MARATHON_UTTER, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model)


def test_finish_veto_rejects_unfinished_report_and_reasks(monkeypatch, _tmp_project_root):
    """b12/g04 型：第一次 finish 的核销报告自认「告诉我库里多少条：没做」→ 机械拒收并
    把缺口回灌重问；第二次提议 db_status 续步 → 照常执行。trace 如实记否决留痕。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：没做")),
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做（第2步）")),
        AIMessage(content="已检查更新，并查询了库里条数。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "核销否决后回灌重问，续步照常执行"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "已拒收收尾并把缺口回灌重问一次" in decides[0]["detail"]
    assert "还需要一步" in decides[0]["detail"]
    # 回灌重问带着缺口反馈（最后一条消息）再问一次
    reask = model.invocations[2]
    assert "你的核销报告里写着还有没做的事" in reask[-1].content
    assert len(model.invocations) == 5  # understand + decide + 回灌重问 + decide#2 + narrate


def test_finish_veto_third_strike_is_accepted_as_fail_safe(monkeypatch, _tmp_project_root):
    """第一次否决回灌、第二次否决回灌（反馈点名**下一步必须做的动作**，候选1）、
    第三次 finish 仍自认未完成 → fail-safe 接受收尾，trace 如实标注「已回灌重问 2 次」。
    2026-08-09 调研-长程agent批 候选2：旧「第二击即放行」过软（每请求至多回灌一次），
    升级为至多两次、三击放行。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：没做")),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：还没做")),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：仍然没做")),
        AIMessage(content="检到无新增。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == ["curate.check_updates"], \
        "第三次 finish 被接受后绝不执行续步"
    decides = [t for t in trace if t["node"] == "decide"]
    assert len(decides) == 1
    assert "已拒收收尾并把缺口回灌重问一次" in decides[0]["detail"]
    # 第二次否决的反馈点名下一步必须做的动作（候选1 硬性要求句）
    assert "下一步你必须提议「汇报数据库状态」" in decides[0]["detail"]
    assert "核销报告仍标注有未完成事项，按大模型最终判断收尾（已回灌重问 2 次）" in decides[0]["detail"]
    assert len(model.invocations) == 5  # understand + decide + 回灌×2 + narrate


def test_finish_veto_second_reask_completes_the_forced_item(monkeypatch, _tmp_project_root):
    """第二次否决点名强制动作后，模型照做（补 db_status）→ 续步照常执行并合法收尾。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：没做")),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：还没做")),
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做（第2步）")),
        AIMessage(content="已检查更新，并查询了库里条数。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "第二次否决点名后照做的续步必须照常执行"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "下一步你必须提议「汇报数据库状态」" in decides[0]["detail"]


def test_finish_veto_feedback_distinguishes_done_without_step(monkeypatch, _tmp_project_root):
    """形态 A 全链路（探针 v4 病灶 b08/k01）：核销报告把没跑的 db_status 标成「已做」
    （无步骤号）→ 拒收收尾并把缺口回灌重问；反馈文案带形态 A 专句
    「『已做』必须写明是第几步的结果」；补做 db_status 后第二次 finish 合法收尾。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做")),
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做（第2步）")),
        AIMessage(content="已检查更新，并查询了库里条数。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "形态 A 否决后回灌重问，续步照常执行"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "已拒收收尾并把缺口回灌重问一次" in decides[0]["detail"]
    assert "「已做」必须写明是第几步的结果" in decides[0]["detail"], "形态 A 反馈专句必须进 trace"
    reask = model.invocations[2]
    assert "「已做」必须写明是第几步的结果" in reask[-1].content, "形态 A 反馈专句必须回灌给模型"
    assert len(model.invocations) == 5  # understand + decide + 回灌重问 + decide#2 + narrate


def test_finish_veto_feedback_distinguishes_dependency_excuse(monkeypatch, _tmp_project_root):
    """形态 B 全链路（探针 v4 病灶 k03/k08）：彼此独立的事拿前件失败当理由
    （「因前置步骤失败而未做」）→ 同样拒收收尾并回灌重问；反馈文案带形态 B 专句
    「彼此独立的事不许拿前件失败当理由」。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n"
            "2. 告诉我库里多少条：未做——前置步骤有失败，无法执行")),
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做（第2步）")),
        AIMessage(content="已检查更新，并查询了库里条数。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "形态 B 否决后回灌重问，续步照常执行"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "彼此独立的事不许拿前件失败当理由" in decides[0]["detail"], "形态 B 反馈专句必须进 trace"
    reask = model.invocations[2]
    assert "彼此独立的事不许拿前件失败当理由" in reask[-1].content


def test_finish_veto_feedback_distinguishes_exempt_without_step(monkeypatch, _tmp_project_root):
    """病灶2 全链路（探针 v5 k02/k09/k10/k11/l07）：豁免行空口无凭（「做不到，没做」
    不写据第几步）→ 拒收收尾并回灌重问；反馈文案带「『条件不成立/做不到』必须写明是
    据第几步的结果得出的」；补做后第二次 finish（举证齐全）合法收尾。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：做不到，没做")),
        _tool_call("curate.db_status", quoted="库里多少条", confidence="high", reason="查库况"),
        _tool_call("finish", completion_report=(
            "1. 检查ArrayExpress更新：已做（第1步）\n2. 告诉我库里多少条：已做（第2步）")),
        AIMessage(content="已检查更新，并查询了库里条数。"),
    )
    plan, trace = _marathon_plan(model)
    assert [s["verb"] for s in plan.get("steps") or []] == [
        "curate.check_updates", "curate.db_status"], "豁免举证否决后回灌重问，续步照常执行"
    decides = [t for t in trace if t["node"] == "decide"]
    assert "必须写明是据第几步的结果得出的" in decides[0]["detail"], "病灶2 反馈专句必须进 trace"
    reask = model.invocations[2]
    assert "必须写明是据第几步的结果得出的" in reask[-1].content


# ---------------------------------------------------------------- 已搜主题清单注入（decide prompt 双通道）

def _search_registry(monkeypatch):
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.search_online": {
            "run": lambda slots, root: dict(SEARCH_OK),
            "label_zh": "联网搜索入库", "card_kind": "search_online", "readonly": False},
    })


def test_searched_topics_block_reaches_both_decide_channels(monkeypatch, _tmp_project_root):
    """首步真搜后，decide 的 tools 主通道与 JSON 兜底 prompt 都带「已经联网搜过的主题」段
    （keywords 原文 + 条数 + 来源）——让 tools 通道抛异常逼出兜底档，两个 prompt 一起钉。"""
    _search_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.search_online", quoted=UTTER, source="ArrayExpress",
                   keywords="小鼠脑", confidence="high", reason="联网搜"),
        ("raise",),  # decide tools 通道异常 → JSON 兜底再问一次
        AIMessage(content='{"done": true}'),
        AIMessage(content="已联网搜索到 2 条并入库。"),
    )
    agent_exec.plan_with_agent(
        UTTER, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None, chat_model=model)
    tools_prompt = model.invocations[1][0].content
    json_prompt = model.invocations[2][0].content
    for prompt in (tools_prompt, json_prompt):
        assert "已经联网搜过的主题（同一主题不许换措辞或加过滤条件再搜）" in prompt
        assert "「小鼠脑」→ 搜到 2 条（来源：ArrayExpress）" in prompt


def test_searched_topics_block_absent_without_search_steps(monkeypatch, _tmp_project_root):
    """没有 ok 的 search_online 步 → 整段不出现（check_updates 后的 decide prompt 为证）。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        _tool_call("finish", completion_report="1. 检查ArrayExpress更新：已做（第1步）"),
        AIMessage(content="检到无新增。"),
    )
    _marathon_plan(model)
    assert "已经联网搜过的主题" not in model.invocations[1][0].content


def test_json_channel_done_is_accepted_without_veto(monkeypatch, _tmp_project_root):
    """散文 JSON 的 {"done": true} 没有 completion_report——照旧接受、不重问（veto 只管
    tool 通道的 finish：兜底/双通道是通道异常时的保命档，不再加门槛——刻意的不对称）。"""
    _marathon_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检到无新增。"),
    )
    plan, trace = _marathon_plan(model)
    assert len(model.invocations) == 3, "JSON 通道的 done 不许触发回灌重问"
    decides = [t for t in trace if t["node"] == "decide"]
    assert len(decides) == 1
    assert "大模型判断：要求的事已经完成" in decides[0]["detail"]


# ---------------------------------------------------------------- 点名源护栏：逐字规范名豁免（w2a2 复跑病灶）

def test_named_source_verbatim_canonical_is_accepted():
    """2026-08-08 探针 w2a2 复跑病灶（b12/g04）：词表刻意不收裸「encode」（检索池口径，
    普通英文动词防撞车），但马拉松指令「再检查一遍ENCODE」里用户**原样写出了受控规范名**
    ——点名源闸不得把这种合法续步恒杀。豁免只认逐字出现的规范名，不扩散给没写出的来源。"""
    utter = ("检查ArrayExpress更新，有新的人类肺数据就搜来入库，"
             "然后再检查一遍ENCODE，完了告诉我库里多少条")
    assert agent_exec._named_source_violation(
        "curate.check_updates", {"source": "ENCODE"}, utter) is None
    # 没写出的来源照样拦（HuBMAP 不在这句话里）
    assert agent_exec._named_source_violation(
        "curate.check_updates", {"source": "HuBMAP"}, utter) is not None
    # 既有反例不回归：点名 10x 填 ENCODE（话里没有 ENCODE 字样）照拦
    assert agent_exec._named_source_violation(
        "curate.check_updates", {"source": "ENCODE"}, "检查10x是否有更新") is not None
