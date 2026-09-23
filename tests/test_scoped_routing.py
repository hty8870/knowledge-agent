# -*- coding: utf-8 -*-
"""scoped 路由（2026-08-17 过夜批）的常驻钉。

scoped 路由成为**唯一路径**——route.request 常驻动词表
route_consensus 恒为环首、三套件面常驻装配；原环境开关与 OFF 负向钉
随代码一并摘除，退役实现只保留在 private Git 历史中。

本文件钉：route.request 登记齐、三套件面、分流共识（并行 2 票一致即定 / 分歧加投 /
多数决 / 平票与无有效票机械兜底 general）、adjudicate 三道闸（套件外动词 / 逃生口
预算≤1 / 目标非法或同线）、execute 换线回写、validate 套件首步闸、understand/repair
面收窄。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import agent_schemas as SC
from dataset_recommender.llm.llm_client import LLMConfig

# conftest 把 _run_route_consensus 全局 stub 成 general（图内测试的脚本替身不预置投票
# 应答）；本文件测的就是共识本身——import 期存真引用，文件级 fixture 恢复（conftest
# fixture 先执行，本 fixture 后执行生效）。
_REAL_RUN_ROUTE_CONSENSUS = AX._run_route_consensus


@pytest.fixture(autouse=True)
def _restore_real_route_consensus(monkeypatch):
    monkeypatch.setattr(AX, "_run_route_consensus", _REAL_RUN_ROUTE_CONSENSUS)


# ---------------------------------------------------------------- 登记齐

def test_route_request_registered():
    spec = AP.VERB_BY_NAME["route.request"]
    assert spec.kind == AP.ROUTE  # 无 quoted 要求；首步 plan 永不命中（不进 understand 面）
    assert spec.slots == ("target_route", "reason")
    entry = AX.LOOP_TOOLS["route.request"]
    assert entry["readonly"] is True and callable(entry["run"])
    assert SC.LOOP_RESULT_MODELS["route.request"] is SC.RouteRequestResult
    # 2026-08-18 四工具批刻意更新：route.request 不再是顺序表末位（compare/cite/compat/
    # fair 追加在其后）——换线元动词仍在表内，集合钉照旧。
    assert "route.request" in AX._DECIDE_VERB_ORDER
    assert set(AX._DECIDE_VERB_ORDER) == set(AX.LOOP_TOOLS)
    # 联网归类：route.request 是本地元动词，不进联网禁提面。
    assert "route.request" not in AX._NETWORK_LOOP_TOOLS
    # 只读同批消费排除（换线必须独占一轮）。
    assert "route.request" not in AX._readonly_loop_verbs()
    # 图结构：环首分流共识节点入图（常驻）。
    assert "route_consensus" in AX._get_graph().get_graph().nodes


def test_suites_and_faces():
    """四套件常驻装配（rank/rerank 同在注册表）。
    2026-08-18 四工具批：结果处理四工具（compare/cite/compat/fair）同时入 search 与
    action 套件——检索后追问（「对比前两条」「找兼容的」「FAIR 自检」）实测分流到两条线
    都可能，只有一条线装它们会让另一线的追问无工具可选而误跑 rank。
    2026-09-13 rerank 处置批：results.show（屏态唯一出口）入 search 套件（general 全集
    自带）；rescue 刻意不装——救回采纳走 search.rerun 自己的自动上屏，不经 show。
    understand 首步面**不装** results.show：首步没有环内试跑实录现场，无句柄可指。
    2026-09-14 YOLO 批刻意更新：ask.relax（问用户放松哪个条件）紧随 results.show
    入 search 套件；understand 首步面同样不装（首步没有屏面现场可判 degenerate）。"""
    assert AX._SUITE_LOOP_VERBS["search"] == (
        "rank", "rerank", "results.show", "ask.relax", "search.rerun", "curate.db_status",
        "compare.datasets", "cite.export", "compat.find", "fair.check")
    assert AX._SUITE_LOOP_VERBS["action"] == (
        "curate.check_updates", "curate.search_online", "curate.sync_updates",
        "curate.db_status", "curate.rollback",  # rb1：回滚动词入动作套件
        "compare.datasets", "cite.export", "compat.find", "fair.check",
    )
    assert set(AX._SUITE_LOOP_VERBS["general"]) == set(AX.LOOP_TOOLS)
    assert AX._SUITE_LOOP_VERBS["rescue"] == ("search.rerun",)
    # decide 套件面：常规套件有换线与 unsupported；rescue 只有重检+收尾。
    for suite, verbs in AX._SUITE_LOOP_VERBS.items():
        names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS_BY_SUITE[suite]]
        for v in verbs:
            assert v.replace(".", "_") in names, (suite, v)
        assert "finish" in names
        if suite == "rescue":
            assert names == ["search_rerun", "finish"]
        else:
            assert "route_request" in names and "unsupported_next_step" in names
    search_names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS_BY_SUITE["search"]]
    assert "curate_search_online" not in search_names  # 套件外工具不进面
    # understand 首步面：ROUTE 投影退役（search.new/refine.conditions/lookup.identifier
    # 不在任何套件面）；route.request 不进首步面；none 恒在。
    for suite, verbs in AX._SUITE_UNDERSTAND_VERBS.items():
        assert "none" in verbs and "route.request" not in verbs
        for retired in ("search.new", "refine.conditions", "lookup.identifier"):
            assert retired not in verbs, (suite, retired)
    assert set(AX._SUITE_UNDERSTAND_VERBS["search"]) == {
        "rank", "rerank", "search.rerun", "curate.db_status", "none",
        # 2026-08-18 四工具批：结果处理四工具入检索首步面（检索后追问「对比/兼容/FAIR」）。
        "compare.datasets", "cite.export", "compat.find", "fair.check"}
    # action 面 = 套件动作工具 + 全部单步 EXEC + none。
    action = set(AX._SUITE_UNDERSTAND_VERBS["action"])
    assert {"curate.check_updates", "curate.search_online", "curate.sync_updates",
            "curate.db_status", "pack.download", "curate.list", "none"} <= action
    assert "search.rerun" not in action
    # 双壳规则按套件装配：core 单源（同一份核心段进所有套件）+ 工具表按套件过滤。
    core = AX._SCOPED_CORE_ZH
    assert core  # prompts/loop_core.md 读到真内容（或内置降级）
    for suite in AX._SCOPED_ROUTES:
        rules = AX._SCOPED_DECIDE_RULES_BY_SUITE[suite]
        assert core in rules["tools"] and core in rules["json"]
    assert "curate.search_online" in AX._SCOPED_DECIDE_RULES_BY_SUITE["action"]["tools"]
    assert "curate.search_online" not in AX._SCOPED_DECIDE_RULES_BY_SUITE["search"]["tools"]
    # 2026-08-31 单锚点化结构钉（legacy 双壳与字节钉退役，规则本体只剩锚点一份）：
    # - 套件壳不再经 bullets 双注入锚点已携带的段落——finish 契约的拒收句恰出现一次
    #   （锚点 finish 契约节）；占位形状 = core 一次 + 路线差异段一次，恰两次；
    # - rescue 面从同一锚点过滤装配：剔除「依赖占位」节（面内只有 search.rerun + finish，
    #   没有消费占位形状的工具），工具表恰为 search.rerun 一行，诚实不变量/finish 契约仍在。
    for suite in AX._SCOPED_ROUTES:
        tools_rules = AX._SCOPED_DECIDE_RULES_BY_SUITE[suite]["tools"]
        assert tools_rules.count("有一件没交代系统会拒收收尾并重问一次") == 1, suite
        assert tools_rules.count("$<N>.top[<i>]") == (1 if suite == "rescue" else 2), suite
    for shell in AX._SCOPED_DECIDE_RULES_BY_SUITE["rescue"].values():
        assert "诚实不变量" in shell and "finish 契约" in shell
    rescue_tools = AX._SCOPED_DECIDE_RULES_BY_SUITE["rescue"]["tools"]
    assert "search.rerun" in rescue_tools
    for off_face in ("compare.datasets", "cite.export", "compat.find", "fair.check",
                     "curate.check_updates", "curate.search_online", "curate.sync_updates"):
        assert f"- {off_face}（" not in rescue_tools, off_face
    # 联网归类显式钉：纯本地 = db_status / search.rerun / rank / rerank / route.request /
    # curate.rollback（rb1 2026-08-17 回滚是本地文件操作，不触网）；2026-08-18 四工具批
    # 的 compare/cite/compat/fair 全本地（结果处理不触网）；2026-09-13 rerank 处置批的
    # results.show 全本地（重放确定性管线+指纹复核，不触网）；2026-09-14 YOLO 批的
    # ask.relax 全本地（零 LLM 确定性预演生成选择卡，不触网）。
    assert AX._NETWORK_LOOP_TOOLS == frozenset(
        set(AX.LOOP_TOOLS) - {"curate.db_status", "search.rerun", "rank", "rerank",
                              "route.request", "curate.rollback", "results.show",
                              "compare.datasets", "cite.export",
                              "compat.find", "fair.check", "ask.relax"})


# ---------------------------------------------------------------- 分流共识（共识帽 + 机械兜底）

class _RouteFakeModel:
    """按脚本出票的分流模型替身：invoke 依序弹出 content；bind 记录温度（不支持 bind
    的变体抛异常，测 bound=False 降级留痕）。"""

    def __init__(self, script, bind_ok=True):
        self._script = list(script)
        self.calls = 0
        self._bind_ok = bind_ok

    def bind(self, **kwargs):
        if not self._bind_ok:
            raise TypeError("bind not supported")
        return self

    def invoke(self, messages):
        self.calls += 1
        content = self._script.pop(0) if self._script else ""
        return SimpleNamespace(content=content)


def _rt(route, reason="r"):
    return f'{{"route": "{route}", "reason": "{reason}"}}'


def test_consensus_two_votes_agree():
    model = _RouteFakeModel([_rt("search"), _rt("search")])
    route, votes = AX._run_route_consensus(model, "找找人类肺癌数据")
    assert route == "search" and len(votes) == 2  # 一致即定，不加投
    assert model.calls == 2
    assert all(v["ok"] for v in votes)
    assert {v["temperature"] for v in votes} == {0.0, 0.8}


def test_consensus_disagree_third_vote_majority():
    model = _RouteFakeModel([_rt("search"), _rt("action"), _rt("search")])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "search" and len(votes) == 3 and model.calls == 3


def test_consensus_three_way_tie_falls_back_general():
    model = _RouteFakeModel([_rt("search"), _rt("action"), _rt("general")])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "general" and len(votes) == 3  # 三方平票 → 机械兜底，不许临场发挥


def test_consensus_invalid_votes_fall_back_general():
    # 全废票（散文/幻觉 route）→ 机械兜底 general；废票不折算成任何路线的一票。
    model = _RouteFakeModel(["我不知道", '{"route": "magic"}', "still not json"])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "general"
    assert all(not v["ok"] for v in votes)
    # 一张有效票 + 一张废票：分歧加投后按有效票多数决。
    model = _RouteFakeModel(["bad", _rt("action"), _rt("action")])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "action" and len(votes) == 3


def test_consensus_single_valid_vote_not_consensus():
    """只有 1 张有效票的「唯一最高」不是共识——
    [invalid, invalid, action] 曾被判成 action；有效票 <2 或平票一律机械兜底 general。"""
    # 两废一活：有效票仅 1 张 → general（不许把独票当多数决）。
    model = _RouteFakeModel(["bad", "worse", _rt("action")])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "general" and len(votes) == 3
    # 一废两活且两活不同：1:1 平票 → general。
    model = _RouteFakeModel(["bad", _rt("action"), _rt("search")])
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "general" and len(votes) == 3


def test_consensus_bind_failure_recorded():
    model = _RouteFakeModel([_rt("general"), _rt("general")], bind_ok=False)
    route, votes = AX._run_route_consensus(model, "x")
    assert route == "general"
    assert all(v["bound"] is False for v in votes)  # 温度岔开不可用，如实留痕


def test_route_consensus_node_records_votes():
    model = _RouteFakeModel([_rt("search", "找数据"), _rt("search", "找数据")])
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=model, decide_model=None, on_progress=None))
    state = {"utterance": "找找人类肺癌数据", "has_results": True,
             "result_total": 12, "current_query": "人类肺癌", "current_filters": [],
             "retrieval": None}
    out = AX.route_consensus(state, runtime=runtime)
    assert out["route_scope"] == "search"
    entry = out["trace"][0]
    assert entry["node"] == "route_consensus"
    assert len(entry["route_votes"]) == 2  # 全部原始投票落 trace 附加字段
    assert entry["route_votes"][0]["route"] == "search"


def test_route_consensus_node_rescue_shortcircuit():
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None))
    out = AX.route_consensus({"route_scope": "rescue", "utterance": "x"}, runtime=runtime)
    assert out["route_scope"] == "rescue" and "不发起分流投票" in out["trace"][0]["detail"]


# ---------------------------------------------------------------- 混合诉求机械预闸

class TestHybridIntentGate:
    """`_hybrid_intent_gate` 确定性单测：同句同含动作信号 ∧ 检索信号 → True；
    纯检索、纯动作、动作链（检查→联网搜→入库）一律 False——误伤率 0 优先，拿不准不闸。"""

    @pytest.mark.parametrize("text", [
        "检查数据库是否有更新，然后帮我找乳腺癌单细胞数据集",
        "检查10x更新，然后帮我找人类肺数据集",
        "帮我找乳腺癌数据，顺便看看数据库状态",
        "找肺癌数据然后打包前 3 条",
        "先同步一下10x，再搜小鼠脑数据",
        "有没有人类肺的单细胞数据，顺便检查下ENCODE有没有更新",
        "帮我搜乳腺癌数据集，再把结果导出引文",
        "看看库里有多少条数据，再找斑马鱼的",
    ])
    def test_mixed_intent_gated(self, text):
        assert AX._hybrid_intent_gate(text) is True

    @pytest.mark.parametrize("text", [
        "帮我找乳腺癌单细胞数据集",
        "找找人类肺癌数据",                    # 既有共识测试用例，不许被误闸
        "推荐有 FASTQ 下载链接的人类肺数据",     # 「下载链接」是产物名词（过滤条件），不是动作
        "只保留能下载的",
        "下载量大的数据集有哪些",                # 「下载量」名词用法
        "有没有小鼠脑的数据",
        "最近更新的人类肺数据集",                # 「最近更新的」是检索条件
        "我上传的肺数据帮我找找",                # 「我上传的…」名词用法
    ])
    def test_pure_search_not_gated(self, text):
        assert AX._hybrid_intent_gate(text) is False

    @pytest.mark.parametrize("text", [
        "检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库",  # 纯动作链（入库链否决）
        "检查ArrayExpress和ENCODE有没有更新，完了看看库里多少条",
        "把前 5 条打包下载",
        "帮我联网搜乳腺癌数据入库",              # 「联网搜…入库」= search_online 口语，动作链
        "把乳腺癌数据搜来入库",
        "数据库里有没有新的数据",                 # 库更新问句（gap 只含「新的」）
        "",
    ])
    def test_pure_action_or_chain_not_gated(self, text):
        assert AX._hybrid_intent_gate(text) is False


def test_route_consensus_node_hybrid_gate_shortcircuit():
    """闸触发时节点级行为：route_scope=general、一票未发（模型零调用）、投票留痕为空。"""
    model = _RouteFakeModel([_rt("search"), _rt("search")])
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=model, decide_model=None, on_progress=None))
    state = {"utterance": "检查10x更新，然后帮我找人类肺数据集",
             "has_results": False, "result_total": 0, "current_query": "",
             "current_filters": [], "retrieval": None}
    out = AX.route_consensus(state, runtime=runtime)
    assert out["route_scope"] == "general"
    assert model.calls == 0                        # 机械闸快进：未发起任何 LLM 投票
    entry = out["trace"][0]
    assert entry["node"] == "route_consensus"
    assert entry["route_votes"] == []              # 无一票发出，如实留空
    assert "机械意图闸" in entry["detail"]
    assert out["usage_ledger"] == []


class _PromptSpyModel:
    """记录每次 invoke 收到的消息文本的分流替身（哨兵/用量测试用）。"""

    def __init__(self, script, usage=None):
        self._script = list(script)
        self.prompts: list[str] = []
        self._usage = usage

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.prompts.append(" ".join(str(getattr(m, "content", "") or "")
                                     for m in messages))
        content = self._script.pop(0) if self._script else ""
        answer = SimpleNamespace(content=content)
        if self._usage is not None:
            answer.usage_metadata = self._usage
        return answer


def test_route_consensus_never_sees_result_titles():
    """2026-08-17 对抗评审（诚实不变量哨兵钉）：分流模型的输入只允许
    命中数/状态/生效条件——retrieval 里的 top_titles（结果集标题）绝不许进任何一票的
    prompt（`_context_zh` 会带标题，分流节点必须用专用的 `_route_context_zh`）。"""
    model = _PromptSpyModel([_rt("search"), _rt("search")])
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=model, decide_model=None, on_progress=None))
    state = {"utterance": "找找人类肺癌数据", "has_results": False,
             "result_total": 0, "current_query": "", "current_filters": [],
             "retrieval": {"status": "results", "total": 5,
                           "top_titles": ["SENTINEL_TITLE_肺癌甲", "SENTINEL_TITLE_肺癌乙"]}}
    out = AX.route_consensus(state, runtime=runtime)
    assert out["route_scope"] == "search" and model.prompts
    for prompt in model.prompts:
        assert "SENTINEL_TITLE" not in prompt      # 结果集内容绝不进分流
        assert "规则匹配命中 5 条" in prompt          # 命中数概览仍在
        assert "找找人类肺癌数据" in prompt           # 原话恰好出现一次（不重复拼）
        assert prompt.count("找找人类肺癌数据") == 1


def test_route_consensus_records_llm_usage():
    """共识投票是真实 LLM 调用，必须过 `_usage_record`
    进 usage_ledger 节点增量（末端聚合成 plan.llm_usage）——读不到用量的替身自然空。"""
    usage = {"input_tokens": 10, "output_tokens": 3,
             "input_token_details": {"cache_read": 4}}
    model = _PromptSpyModel([_rt("search"), _rt("search")], usage=usage)
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=model, decide_model=None, on_progress=None))
    state = {"utterance": "找找人类肺癌数据", "has_results": False,
             "result_total": 0, "current_query": "", "current_filters": [],
             "retrieval": None}
    out = AX.route_consensus(state, runtime=runtime)
    assert out["usage_ledger"] == [
        {"node": "route_consensus", "input": 10, "cache_read": 4, "output": 3},
        {"node": "route_consensus", "input": 10, "cache_read": 4, "output": 3},
    ]
    # 无用量的替身：台账空、键仍在（reducer 增量语义）。
    model = _RouteFakeModel([_rt("search"), _rt("search")])
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=model, decide_model=None, on_progress=None))
    out = AX.route_consensus(state, runtime=runtime)
    assert out["usage_ledger"] == []


# ---------------------------------------------------------------- adjudicate 三道闸

def _scoped_state(scope, steps_verbs=()):
    return {"utterance": "找找人类肺癌的数据", "route_scope": scope,
            "steps": [{"verb": v, "ok": True} for v in steps_verbs]}


def test_adjudicate_suite_gate():
    # search 路线提议联网搜库 → 套件外，机械拒绝并如实点名。
    nxt, note, refused, _ = AX._adjudicate_decide_obj(
        {"verb": "curate.search_online", "quoted": "找找人类肺癌的数据"},
        _scoped_state("search"))
    assert nxt is None and "不属于本回合的处理路线" in refused
    # search 路线提议 search.rerun → 放行。
    nxt, *_ = AX._adjudicate_decide_obj(
        {"verb": "search.rerun", "quoted": "找找人类肺癌的数据", "query": "human lung cancer"},
        _scoped_state("search"))
    assert nxt is not None and nxt["verb"] == "search.rerun"


def test_adjudicate_route_request_gates():
    # 合法换线 → 放行。
    nxt, *_ = AX._adjudicate_decide_obj(
        {"verb": "route.request", "target_route": "action"}, _scoped_state("search"))
    assert nxt is not None
    # 预算：已用过 1 次 → 机械拒绝并如实点名。
    nxt, note, refused, _ = AX._adjudicate_decide_obj(
        {"verb": "route.request", "target_route": "action"},
        _scoped_state("search", ["route.request"]))
    assert nxt is None and "每轮最多换 1 次路线" in refused
    # 同线空转 → 机械拒绝。
    nxt, note, refused, _ = AX._adjudicate_decide_obj(
        {"verb": "route.request", "target_route": "search"}, _scoped_state("search"))
    assert nxt is None and "目标路线不成立" in refused
    # 非法目标 → 机械拒绝。
    nxt, note, refused, _ = AX._adjudicate_decide_obj(
        {"verb": "route.request", "target_route": "magic"}, _scoped_state("search"))
    assert nxt is None and "目标路线不成立" in refused


# ---------------------------------------------------------------- execute 换线回写

def test_execute_writes_route_scope(monkeypatch):
    monkeypatch.setattr(AX, "_audit_loop_tool", lambda *a, **k: None)
    runtime = SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))
    state = {"utterance": "找找数据然后打包", "route_scope": "search",
             "plan": {"verb": "none"}, "steps": [],
             "loop_plan": {"verb": "route.request",
                           "slots": {"target_route": "action", "reason": "要打包"}}}
    out = AX.execute(state, runtime=runtime)
    assert out["route_scope"] == "action"
    step = out["steps"][0]
    assert step["verb"] == "route.request" and step["ok"] is True
    assert step["result"]["requested_route"] == "action"
    AX._LOOP_RESULT_MODELS["route.request"].model_validate(step["result"])
    # 非法 target：run 抛 bad_param，步失败如实记、不换线。
    state["loop_plan"] = {"verb": "route.request", "slots": {"target_route": "magic"}}
    out = AX.execute(state, runtime=runtime)
    assert "route_scope" not in out
    assert out["steps"][0]["ok"] is False
    assert out["steps"][0]["error_code"] == "bad_param"


# ---------------------------------------------------------------- validate 套件首步闸 / understand 面

def test_validate_suite_first_step_gate():
    runtime = SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))
    # search 路线首步提议打包 → 套件闸违规（走 repair 一次）。
    out = AX.validate({
        "utterance": "找找人类肺癌的数据，打包前5条",
        "route_scope": "search", "steps": [],
        "raw": {"verb": "pack.download", "quoted": "打包前5条"},
        "has_results": True, "result_total": 3,
    }, runtime=runtime)
    assert out["violations"] and "不在本路线面内" in out["violations"][0]
    # search 路线首步 search.rerun → 过闸。
    out = AX.validate({
        "utterance": "找找人类肺癌的数据",
        "route_scope": "search", "steps": [],
        "raw": {"verb": "search.rerun", "quoted": "找找人类肺癌的数据",
                "query": "human lung cancer"},
        "has_results": False, "result_total": 0,
    }, runtime=runtime)
    assert out["violations"] == [] and out["plan"]["verb"] == "search.rerun"


def test_scoped_understand_face_helper():
    tools, names, specs = AX._scoped_understand_face({"route_scope": "search"})
    verbs = set(names.values())
    # 2026-08-18 四工具批：结果处理四工具入检索首步面（检索后追问「对比/兼容/FAIR」）。
    assert verbs == {"rank", "rerank", "search.rerun", "curate.db_status", "none",
                     "compare.datasets", "cite.export", "compat.find", "fair.check"}
    assert {s.verb for s in specs} == {
        "rank", "rerank", "curate.db_status", "search.rerun", "none",
        "compare.datasets", "cite.export", "compat.find", "fair.check"}
    # rescue 也是正式套件：首步面只有 search.rerun / none。
    _tools, rescue_names, rescue_specs = AX._scoped_understand_face({"route_scope": "rescue"})
    assert set(rescue_names.values()) == {"search.rerun", "none"}
    assert {s.verb for s in rescue_specs} == {"search.rerun", "none"}


def test_parse_route_vote():
    assert AX._parse_route_vote('{"route": "search", "reason": "找数据"}') == (
        "search", "找数据", True)
    assert AX._parse_route_vote('{"route": "magic"}') == ("", "", False)
    assert AX._parse_route_vote("散文不是 JSON") == ("", "", False)


# ---------------------------------------------------------------- /JSON-only 模型 search 面全图

class _JsonOnlySpyModel:
    """JSON-only 替身（/）：bind_tools 抛错（模拟 provider 不支持
    tool-calling）→ understand 跌 JSON 兜底壳；invoke 依序弹 content 并记录全部
    prompt 文本（断言兜底壳提示词用）。bind 支持（共识投票的温度岔开走真 bind）。"""

    def __init__(self, script):
        self._script = list(script)
        self.prompts: list[str] = []

    def bind(self, **kwargs):
        return self

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        raise RuntimeError("provider does not support tool calling")

    def invoke(self, messages):
        self.prompts.append(" ".join(str(getattr(m, "content", "") or "")
                                     for m in messages))
        return SimpleNamespace(content=self._script.pop(0) if self._script else "")


def test_json_only_model_walks_search_face_full_graph(monkeypatch):
    """2026-08-17：真共识（本文件 autouse fixture 恢复真引用，不受
    conftest 全局 stub 的 general 面掩盖）把「找人类肺癌数据」分进 search 面；
    JSON-only 模型走 understand 的 JSON 兜底壳。钉两件事——
    ① 兜底壳提示词**零残留** search.new/refine.conditions/lookup.identifier
    （修复前：scoped 收窄面原样带全表铁律，铁律 5/7/8/10 指着面内不存在的
    三个动词下指令，规则与动词表自相矛盾）；
    ② 全图照样走通：首步 rank 过 validate 出 plan（套件面 × JSON 通道的组合
    此前在图内零覆盖，本条是对冲钉）。"""
    monkeypatch.setattr(AX, "_audit_loop_tool", lambda *a, **k: None)
    # 审计台账桩（复核中指）：JSON 兜底成功会经 _audit_fallback 写真实
    # .userdata/agent_fallbacks.jsonl——本测试不测审计持久化，stub 掉保持 hermetic。
    monkeypatch.setattr(AX, "_audit_fallback", lambda *a, **k: None)
    loop_tools = {v: dict(e) for v, e in AX.LOOP_TOOLS.items()}
    loop_tools["rank"] = {
        "run": lambda slots, root: {"query": str(slots.get("query") or ""), "total": 0},
        "label_zh": "检索数据集", "card_kind": "rank", "readonly": True,
    }
    monkeypatch.setattr(AX, "LOOP_TOOLS", loop_tools)
    model = _JsonOnlySpyModel([
        _rt("search", "找数据"), _rt("search", "找数据"),     # 共识两票（真共识）
        json.dumps({"verb": "rank", "query": "人类肺癌",
                    "quoted": "找人类肺癌数据", "confidence": "high",
                    "reason": "用户要检索"}, ensure_ascii=False),  # understand JSON 兜底
    ])
    plan, trace = AX.plan_with_agent(
        "找人类肺癌数据", has_results=False, result_total=0,
        config=LLMConfig(enable_llm=True, api_key="sk-scoped-json-test"),
        retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )
    assert plan["verb"] == "rank"
    assert plan["slots"]["query"] == "人类肺癌"
    nodes = [t["node"] for t in trace]
    assert nodes[:3] == ["route_consensus", "understand", "validate"]
    assert "换一种问法" in trace[1]["detail"]        # 如实标注：跌了 JSON 兜底壳
    understand_prompt = model.prompts[2]             # 两票共识之后的第一问 = understand
    assert "- rank（" in understand_prompt           # 面内检索工具在表里
    for retired in ("search.new", "refine.conditions", "lookup.identifier"):
        assert retired not in understand_prompt, retired   # 退役动词零残留
    assert "选表里的检索动词" in understand_prompt     # 铁律 5 的收窄面口径
