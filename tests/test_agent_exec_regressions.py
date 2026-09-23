# -*- coding: utf-8 -*-
"""agent 图对抗评审修复（2026-08-04）的回归门。**全离线**：fake chat_model + 替身 LOOP_TOOLS
+ tmp 项目根（账本真写真断言，绝不碰真实库、绝不发真 LLM 请求）。

每条测试钉住一个 `.census/adversarial/` 坐实过、本轮已修的病形（原 strict-xfail 对抗用例
在此转正）：

- A3 understand「无 tool_call 且 content 不可解析」曾炸 NameError（fallback_reason 未绑定），
  图内降级重试从未工作 → 现在散文应答降级 JSON-in-prompt 再试一次、正常出 plan；
- understand 通道如实标注：content 直接给可解析 JSON 时不许再误标「工具调用模式」；
- A2 narrate 机械后检从穷举词表改为「写动作词 × 完成态语素」模式化判定——同族幻觉措辞
  （已完成下载/下载完成/已为你下载并保存/并完成了下载入库）不再透传，否定形态不误伤；
- R2-1 P0-1（A2 未闭合残余）转正：尾窗从定长 4 字扩到下一小句隔断、语素表补「好啦/
  搞定」族、动作词补「存」——「下载任务已完成/下载流程已完成/下载好啦/帮你存好了/
  下载搞定」不再透传；豁免窗口扩到整个小句（含语素之后），「下载完成不了」「合并下载
  失败了」等如实汇报不误伤；
- R2-1 P2-1 转正：机械后检弃用汇报时 trace 措辞保持中性（「措辞可能越界（疑似与实录
  不符）」）——后检分不清真谎称与措辞像谎称的实话，不许用不实话惩罚实话；
- B2 decide 查重指纹过 _norm_source 归一：大小写变体判重、不再重复执行；
- 续步 validate 违规 → fail-safe 收尾 narrate：已执行步骤不被 AgentPlanInvalid 掀翻；
- decide 婉拒表外动词 → 确定性兜底汇报点名「你要的这件事没做」；
- 跨组契约（additive）：plan.steps[i].readonly、plan.report_source ∈ {llm, deterministic}；
- 审计行 records 写实数（写步）或省略（只读步），不再恒 0；
- 真 LOOP_TOOLS 注册表形状契约（tests.md P2-2：替身副本与真表无同步门的补位）。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：回归门测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-regression-test")


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


def _search_run_unregistered(slots, root):
    """未注册源 fail-fast 替身（零网络）：2026-08-08 起 10x 已接入联网适配器（W2e），
    真 `_loop_search_online` 对 10x 不再 fail-fast——这里钉的是「写步失败时 LLM 如实汇报
    不被误弃」的否定豁免路径，不再绑定「10x 未注册」这个会漂移的世界事实。"""
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


# ---------------------------------------------------------------- A3 understand 降级重试真工作

def test_understand_prose_answer_falls_back_to_json_retry():
    """A3 转正：provider 有应答但无 tool_call、content 是散文（拒答/闲聊式回应）→
    图内降级 JSON-in-prompt 再问一次（原来在 f-string 处炸 NameError，降级从未跑到）。"""
    model = _FakeModel(
        AIMessage(content="抱歉，我不太明白这句话的意思。"),  # tools 模式散文
        AIMessage(content=json.dumps({"verb": "none", "confidence": "low",
                                      "reason": "只是描述"}, ensure_ascii=False)),
    )
    plan, trace = _plan("随便看看有什么数据", model)
    assert plan["verb"] == "none"
    assert len(model.invocations) == 2, "降级重试必须真发起第二次调用"
    assert "换一种问法" in trace[1]["detail"]  # [0] 是常驻环首 route_consensus（nl1）


def test_understand_content_json_answer_is_labeled_honestly():
    """mode 误标修复：没发 tool_call 但 content 本身是可解析 JSON → 走内容解析通道，
    trace/mode 不许谎称「工具调用模式」。"""
    model = _FakeModel(AIMessage(content=json.dumps(
        {"verb": "none", "confidence": "high", "reason": "闲聊"}, ensure_ascii=False)))
    plan, trace = _plan("今天天气怎么样", model)
    assert plan["verb"] == "none"
    assert trace[1]["detail"].startswith("内容 JSON 模式")  # [0] 是常驻环首（nl1）
    assert "工具调用模式" not in trace[1]["detail"]


# ------------------------------------------------------- 2026-08-05 understand 三级通道根治

class _ThinkingModel:
    """思考模式替身（2026-08-05 根因钉死后的行为模型，DeepSeek 实测原话建模）：
    `tool_choice="required"` 的请求在 invoke 时抛 400 风异常（报文含 tool_choice 字样），
    auto 档与纯文本调用正常应答。bind_tools 只记 choice；异常按次消费——
    一个请求失败不毒化下一个请求（与真实 HTTP 语义一致）。"""

    def __init__(self, *answers, auto_ok=True):
        self.answers = list(answers)
        self.auto_ok = auto_ok
        self.choices = []
        self._pending = None

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        self.choices.append(tool_choice)
        self._pending = tool_choice
        return self

    def invoke(self, messages):
        choice, self._pending = self._pending, None
        if choice == "required" or (choice == "auto" and not self.auto_ok):
            raise RuntimeError(
                "Error code: 400 - {'error': {'message': "
                "'Thinking mode does not support this tool_choice'}}")
        return self.answers.pop(0)


def test_understand_tool_choice_rejected_retries_auto_tier():
    """2026-08-05 根治：模型 400 拒 required 档（思考模式）→ 自动档重试、留在结构化通道，
    不跌 JSON 兜底。真机病灶：浏览器 DeepSeek 预设曾预填 v4-flash（思考模型），understand
    恒报「直连通道不可用（BadRequestError）」跌 JSON 档，target 槽偶发落空（「未指明删哪份」）。
    修复后同一模型经自动档照常结构化抽取。"""
    model = _ThinkingModel(
        _tool_call("curate.remove", quoted="GSE12345", target="GSE12345",
                   confidence="high", reason="点名删除"),
    )
    plan, trace = _plan("把 GSE12345 从本地删掉", model)
    assert plan["verb"] == "curate.remove"
    assert plan["slots"]["target"] == "GSE12345"
    assert model.choices == ["required", "auto"], "必须先试强制档、被拒后才降自动档"
    assert trace[1]["detail"].startswith("工具调用模式（模型不收强制档，已用自动档）")  # [0] 是常驻环首（nl1）


def test_understand_auto_tier_also_rejected_falls_back_to_json():
    """provider 连自动档都不收（彻底不支持 tool-calling）→ 两档都试过后才跌 JSON 兜底，
    trace 如实记两档连败，不谎称通道。"""
    model = _ThinkingModel(
        AIMessage(content=json.dumps({"verb": "none", "confidence": "low",
                                      "reason": "只是看看"}, ensure_ascii=False)),
        auto_ok=False,
    )
    plan, trace = _plan("随便看看有什么数据", model)
    assert plan["verb"] == "none"
    assert model.choices == ["required", "auto"], "两档都被拒才准跌 JSON 兜底"
    assert "换一种问法" in trace[1]["detail"]  # [0] 是常驻环首（nl1）
    assert "auto" in trace[1]["detail"]


# ---------------------------------------------------------------- A2 narrate 后检模式化判定

#: 对抗评审坐实透传的同族幻觉措辞（穷举词表盲区）。修好后的应然：一律判矛盾、弃用。
#: 后 5 条是 R2-1 P0-1 坐实的漏网变体：尾窗定长 4 字把「完成」切在窗外（任务/流程族），
#: 外加词表外语素（好啦/存好/搞定族）。
HALLUCINATED_VARIANTS = [
    "检查到 2 条疑似新增，已完成下载。",
    "检查到 2 条疑似新增，下载完成。",
    "检查到 2 条疑似新增，已为你下载并保存。",
    "检查到 2 条疑似新增，并完成了下载入库。",
    "检查到 2 条疑似新增，下载任务已完成。",
    "检查到 2 条疑似新增，下载流程已完成。",
    "检查到 2 条疑似新增，下载好啦。",
    "检查到 2 条疑似新增，帮你存好了。",
    "检查到 2 条疑似新增，下载搞定。",
]

#: R2-1 P0-1 误伤侧 / P2-1 坐实的如实汇报变体：否定/未遂语素在完成态语素**之后**
#: （豁免窗口只看语素之前时曾被错判谎称）。应然：原样保留、标 llm、trace 不含越界标注。
#: 2026-08-06 批A P2-1 来源接地后检上线后，填充词从「10x」改为 harness 真碰过的
#: 「ArrayExpress」——汇报点名未触碰来源现属按设计拦下的假性声称（本组变体测的是
#: 写动作豁免，来源提及只是填充；改用真碰过的来源不影响被测语义）。
TRUTHFUL_VARIANTS = [
    "检查了 ArrayExpress，下载完成不了。",
    "检查了 ArrayExpress，合并下载失败了。",
]


def _run_with_narrate(narrate_text, monkeypatch, tmp_root, sources=AE_TWO_NEW,
                      utterance="检查ArrayExpress是否有更新，若有新数据就下载下来"):
    """check 一步（无写动作）→ decide done → narrate 输出给定文本。返回 (plan, trace)。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(sources)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
        AIMessage(content=narrate_text),
    )
    return _plan(utterance, model)


@pytest.mark.parametrize("variant", HALLUCINATED_VARIANTS)
def test_hallucinated_write_claim_variants_are_blocked(monkeypatch, _tmp_project_root, variant):
    """A2 + R2-1 P0-1 转正：steps 无成功写步时，任何「写动作词 × 完成态」既遂声称都判
    矛盾——原词表外措辞（含尾窗截断族、好啦/搞定族）曾原样透传上屏。"""
    plan, trace = _run_with_narrate(variant, monkeypatch, _tmp_project_root)
    report = plan.get("report_zh") or ""
    assert report != variant, "幻觉措辞又被透传了"
    assert "疑似新增 2 条" in report, "兜底汇报与谎称汇报是同一批事实"
    assert plan.get("report_source") == "deterministic"
    assert any("措辞可能越界" in t["detail"] for t in trace if t["node"] == "narrate"), (
        "R2-1 P2-1：弃用汇报的 trace 必须是中性措辞——机械后检分不清真谎称与"
        "措辞像谎称的实话，不许断言「与实录不符」")


@pytest.mark.parametrize("truthful", TRUTHFUL_VARIANTS)
def test_truthful_post_marker_negation_is_kept(monkeypatch, _tmp_project_root, truthful):
    """R2-1 P0-1 误伤侧转正：否定/未遂语素在完成态语素之后的如实汇报（「下载完成不了」
    「合并下载失败了」）——豁免窗口必须覆盖语素之后的小句余量，不许错判谎称、
    静默回退确定性兜底。"""
    plan, trace = _run_with_narrate(truthful, monkeypatch, _tmp_project_root)
    assert plan.get("report_zh") == truthful
    assert plan.get("report_source") == "llm"
    assert not any("措辞可能越界" in t["detail"] for t in trace if t["node"] == "narrate")


def test_honest_negated_write_mention_is_kept(monkeypatch, _tmp_project_root):
    """模式化判定的另一侧：「没有更新，不需要下载」是否定形态，不是既遂声称——
    如实汇报不许被误伤成兜底（误伤代价只是措辞朴素，但能不误伤就不误伤）。"""
    zero = [dict(AE_TWO_NEW[0], new_count=0, new_candidates=[])]
    plan, _ = _run_with_narrate("ArrayExpress 没有更新，不需要下载。",
                                monkeypatch, _tmp_project_root, sources=zero)
    assert plan.get("report_zh") == "ArrayExpress 没有更新，不需要下载。"
    assert plan.get("report_source") == "llm"


def test_honest_failed_write_report_is_kept(monkeypatch, _tmp_project_root):
    """写步真失败（fail-fast 替身零网络，见 `_search_run_unregistered` 注释）时 LLM 如实说
    「没有完成」——否定语素豁免必须盖住失败汇报，不该误弃。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(
            [{"source": "10x", "label": "10x Genomics", "mode": "online",
              "local_count": 12, "online_recent": 14, "new_count": 2,
              "new_candidates": [{"accession": "x", "title": "10x new dataset"}]}]),
            "curate.search_online": _search_run_unregistered},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新",
                   source="10x", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "下载下来",
             "source": "10x", "keywords": "10x"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增；联网搜索没有完成：暂不支持联网搜索来源 10x。"),
    )
    plan, _ = _plan("检查10x是否有更新，若有则下载下来", model)
    assert plan.get("report_zh") == "检查到 2 条疑似新增；联网搜索没有完成：暂不支持联网搜索来源 10x。"
    steps = plan.get("steps") or []
    assert steps[1]["ok"] is False and steps[1]["readonly"] is False, "失败步同样带 readonly"


def test_honest_failed_write_negation_window_is_kept(monkeypatch, _tmp_project_root):
    """否定豁免的承重门（R2 tests P1-1 转正）：「数据下载没有完成」——动作词「下载」与后缀
    完成态语素「完成」之间夹「没有」，判定真正走 _WRITE_NEG_ZH 豁免分支。上面两条诚实用例的
    措辞走不到豁免（R2 实测删豁免仍绿）；这条若没了豁免，如实失败汇报会被错判谎称、
    静默回退确定性兜底。"""
    _install_tools(
        monkeypatch,
        **{"curate.check_updates": _check_run(
            [{"source": "10x", "label": "10x Genomics", "mode": "online",
              "local_count": 12, "online_recent": 14, "new_count": 2,
              "new_candidates": [{"accession": "x", "title": "10x new dataset"}]}]),
            "curate.search_online": _search_run_unregistered},
    )
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查10x是否有更新",
                   source="10x", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "下载下来",
             "source": "10x", "keywords": "10x"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="数据下载没有完成：暂不支持联网搜索来源 10x。"),
    )
    plan, _ = _plan("检查10x是否有更新，若有则下载下来", model)
    assert plan.get("report_zh") == "数据下载没有完成：暂不支持联网搜索来源 10x。"
    assert plan.get("report_source") == "llm"


# ---------------------------------------------------------------- B2 查重指纹归一

def test_decide_case_variant_duplicate_is_blocked(monkeypatch, _tmp_project_root):
    """B2 转正：decide 提议同 verb 同语义、仅大小写不同的重复步骤（ArrayExpress →
    arrayexpress）→ 指纹归一后判重、停环，同一检查只真跑一遍、账本一行。"""
    ran = {"check": 0}

    def check_run(slots, root):
        ran["check"] += 1
        return {"checked_at": "2026-08-04T00:00:00+08:00", "sources": AE_TWO_NEW, "hint_zh": ""}

    _install_tools(monkeypatch, **{"curate.check_updates": check_run})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.check_updates", "quoted": "检查ArrayExpress是否有更新",
             "source": "arrayexpress"}, ensure_ascii=False)),
        AIMessage(content="检查到 2 条疑似新增。"),
    )
    plan, trace = _plan("检查ArrayExpress是否有更新", model)
    assert ran["check"] == 1, "大小写变体不得绕过「不许重复已执行步骤」"
    assert len(plan.get("steps") or []) == 1
    assert any("重复" in t["detail"] for t in trace if t["node"] == "decide")
    assert len(_ledger_rows(_tmp_project_root)) == 1


# ---------------------------------------------------------------- 续步违规 fail-safe 收尾

def test_loop_validate_failure_failsafe_narrate_keeps_executed_steps(monkeypatch, _tmp_project_root):
    """P2-1 转正：续步 validate 违规（用计数桩模拟 decide 预检与 validate 口径将来漂移）
    → fail-safe 收尾 narrate，**不抛 AgentPlanInvalid**——已真跑的工具步（账本已落行）
    必须随 plan 如实交出去，绝不随异常被调用方整体回退丢弃。"""
    ran = {"check": 0, "search": 0}

    def check_run(slots, root):
        ran["check"] += 1
        return {"checked_at": "2026-08-04T00:00:00+08:00", "sources": AE_TWO_NEW, "hint_zh": ""}

    def search_run(slots, root):
        ran["search"] += 1
        return dict(SEARCH_OK)

    _install_tools(monkeypatch,
                   **{"curate.check_updates": check_run, "curate.search_online": search_run})

    real_validate = agent_exec._validate_raw
    calls = {"n": 0}

    def stub_validate(raw, utterance, steps=None):
        calls["n"] += 1
        if calls["n"] >= 3:   # 第1次=首步 validate、第2次=decide 预检（放行），第3次=续步 validate
            return ["注入的续步违规（回归桩：模拟 decide 预检与 validate 口径将来漂移）"]
        return real_validate(raw, utterance, steps=steps)

    monkeypatch.setattr(agent_exec, "_validate_raw", stub_validate)
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜人类肺数据入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        # narrate 没有第三个 answer → 确定性兜底（同一批事实）
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有就联网搜人类肺数据入库", model)
    assert ran["check"] == 1 and ran["search"] == 0, "续步被拦下，只跑了首步"
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["curate.check_updates"], "已执行步骤必须随 plan 交出"
    assert len(_ledger_rows(_tmp_project_root)) == 1
    assert "检查了来源更新" in (plan.get("report_zh") or ""), "兜底汇报如实讲已做步骤"
    assert [t["node"] for t in trace] == [
        "route_consensus", "understand", "validate", "execute", "decide", "validate", "narrate",
    ]
    assert trace[5]["ok"] is False, "续步违规在 trace 里如实留痕"  # 索引随环首 +1（nl1）


# ---------------------------------------------------------------- decide 婉拒动作的兜底汇报

def test_declined_verb_is_named_in_deterministic_report(monkeypatch, _tmp_project_root):
    """decide 提议表外动词（pack.download）被婉拒停环、narrate 的 LLM 又缺席时，
    确定性兜底汇报必须点名「你要的这一步没做」——不能只讲已做步骤。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "pack.download", "quoted": "下载下来", "limit": 5}, ensure_ascii=False)),
        # narrate 没有 answer → 确定性兜底
    )
    plan, trace = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    report = plan.get("report_zh") or ""
    assert "疑似新增 2 条" in report, "已做步骤照讲"
    assert "打包下载" in report and "没有做" in report, "婉拒的动作必须点名没做"
    assert plan.get("report_source") == "deterministic"
    assert any("不在允许自动执行的范围" in t["detail"] for t in trace if t["node"] == "decide")


# ---------------------------------------------------------------- 跨组契约：readonly / report_source

def test_steps_carry_readonly_and_llm_report_is_marked(monkeypatch, _tmp_project_root):
    """跨组契约（additive）：plan.steps 每步带 readonly（取自 LOOP_TOOLS 元数据）；
    LLM 汇报时 plan.report_source == "llm"。账本 records：写步写实数、只读步省略。"""
    _install_tools(monkeypatch,
                   **{"curate.check_updates": _check_run(AE_TWO_NEW),
                      "curate.search_online": lambda slots, root: dict(SEARCH_OK)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=json.dumps(
            {"verb": "curate.search_online", "quoted": "联网搜来入库",
             "source": "ArrayExpress", "keywords": "人类肺"}, ensure_ascii=False)),
        AIMessage(content='{"done": true}'),
        AIMessage(content="检查到 2 条疑似新增，已联网搜到 2 条并入库。"),
    )
    plan, _ = _plan("检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库", model)
    steps = plan.get("steps") or []
    assert [(s["verb"], s["readonly"]) for s in steps] == [
        ("curate.check_updates", True), ("curate.search_online", False),
    ], "每个 step 都必须带 readonly（前端 policy 行消费，不再靠 card_kind 硬推）"
    assert plan.get("report_source") == "llm"
    rows = _ledger_rows(_tmp_project_root)
    assert [r["endpoint"] for r in rows] == [
        "agent_exec:curate.check_updates", "agent_exec:curate.search_online",
    ]
    assert "records" not in rows[0], "只读步没有「写入条数」可记，字段省略（不再恒 0 失真）"
    assert rows[1].get("records") == 2, "写步审计记真实入库条数"


def test_deterministic_report_is_marked(monkeypatch, _tmp_project_root):
    """report_source 的另一档：narrate 的 LLM 缺席 → 确定性拼接标 deterministic
    （前端据此不再把兜底文案误标「AI 总结」）。"""
    _install_tools(monkeypatch, **{"curate.check_updates": _check_run(AE_TWO_NEW)})
    model = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress是否有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
    )
    plan, _ = _plan("检查ArrayExpress是否有更新，若有新数据就下载下来", model)
    assert plan.get("report_source") == "deterministic"
    assert "疑似新增 2 条" in (plan.get("report_zh") or "")


# ---------------------------------------------------------------- 真 LOOP_TOOLS 注册表形状契约

def test_loop_tools_registry_shape_is_pinned():
    """tests.md P2-2 转正：替身副本与真表之间没有同步门——钉住真表的形状本身：
    动词集合、每项必备键（run/label_zh/card_kind/readonly）、readonly 恰好是那几个只读工具。
    2026-08-16 刻意更新：注册表新增 search.rerun（检索工具化 Phase 1，只读本地检索，
    带 needs_context=True——execute 按此键注入现场上下文）。
    2026-08-17 nl1 转正刻意更新：rank/rerank/route.request 常驻入列（三者皆只读，
    rank/rerank 带 needs_context=True）。
    2026-08-17 rb1 回滚动词化刻意更新：curate.rollback 入列——**写**工具（readonly=False），
    needs_context=True（机械闸从 ctx.steps 现定回退目标）。2026-08-18 钉字：它改走独立
    MAX_ROLLBACK 预算，不再计入正向写步预算。
    2026-08-18 四工具批刻意更新：compare.datasets / cite.export / compat.find /
    fair.check 入列——环内结果处理四工具，全带 needs_context=True（默认对象取当前结果）；
    cite.export 是**写**工具（落盘引文产物，readonly=False），其余三个只读。
    2026-09-13 rerank 处置批刻意更新：results.show 入列——屏态唯一出口（只读，
    needs_context=True：句柄解析吃 ctx.steps 试跑实录、重放吃结构化现场）。
    2026-09-14 YOLO 批刻意更新：ask.relax 入列——「问用户放松哪个条件」选择卡
    （只读，needs_context=True：屏面基线/结构化现场/生效查询都经 ctx 注入）。"""
    table = agent_exec.LOOP_TOOLS
    assert set(table) == {"curate.db_status", "curate.check_updates", "curate.search_online",
                          "curate.sync_updates", "search.rerun", "rank", "rerank",
                          "route.request", "curate.rollback", "results.show",
                          "compare.datasets", "cite.export", "compat.find", "fair.check",
                          "ask.relax"}
    for verb, spec in table.items():
        assert callable(spec.get("run")), verb
        assert isinstance(spec.get("label_zh"), str) and spec["label_zh"], verb
        assert isinstance(spec.get("card_kind"), str) and spec["card_kind"], verb
        assert isinstance(spec.get("readonly"), bool), verb
    assert {v for v, s in table.items() if s["readonly"]} == {
        "curate.db_status", "curate.check_updates", "search.rerun", "rank", "rerank",
        "route.request", "compare.datasets", "compat.find", "fair.check", "results.show",
        "ask.relax",
    }, "readonly 集合漂移 = 前端 policy 行与审计口径同时失真"
    assert table["curate.db_status"].get("report") is True
    assert table["curate.db_status"].get("observation") is True
    assert table["search.rerun"].get("needs_context") is True
    assert table["curate.rollback"].get("needs_context") is True  # rb1：机械闸吃 ctx.steps
