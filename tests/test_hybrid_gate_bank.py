# -*- coding: utf-8 -*-
""" 混合诉求闸 · 对抗负例库 + 能力账核销链 + 保底弃权（**全确定性、零 LLM**）。

覆盖实测坐实的两处病形：
- 【P0 混合句完成度】闸命中后 finish 的机械 checklist 不认 rank/rerank 作检索半核销、
  也没有动作半的强制账 → 混合句静默只做一半。修复：`required_capabilities` 能力账
  （route_consensus 产出 → state/trace）+ `_capabilities_unsettled` 零信任对账 +
  `_finish_veto_all` 第五路否决 + 清单 expect=rank 接受 `_SEARCH_SETTLE_VERBS` 核销。
- 【P1 误判】入库链否决 v1 是全句级（「检查更新，有新的就入库，再帮我找乳腺癌数据」
  反而不闸）；「帮我找可下载数据」因裸「下载」误判动作。修复：子句级判定
  （`_split_hybrid_clauses`；动作/检索信号须落在**不同子句**才闸，否决只对同子句生效）。
- plan_action 保底通道（单次单动词）对混合句**明确弃权**（route=none + 如实回音），
  不再只挑一半做。

负例库约定：每条用例入库前都经真实闸输出验证（不是猜的词表归属）；改词表/切分规则
（`_HYBRID_LEXICON_VERSION` 递增）时本库必须全量复跑。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import turn
from dataset_recommender.llm.llm_client import LLMConfig

# ---------------------------------------------------------------- A. 闸结果负例库（47 条）

#: 纯检索句（含 v1 误判族：裸「下载」/产物名词/名词用法/存在性问句）——一律不闸。
PURE_SEARCH_NOT_GATED = [
    "帮我找乳腺癌单细胞数据集",
    "帮我找可下载数据",                    # v1 误判实锤：裸「下载」与「找」同子句，产物形容词
    "找找能下载 FASTQ 的人类肺癌数据",      # 同上族
    "最近更新的人类肺数据集",              # 「最近更新的」是检索条件
    "我上传的肺数据帮我找找",              # 「我上传的…」名词用法
    "已上传的数据还能下载吗",              # 下载能力问句，无检索信号
    "哪个数据集去除了线粒体基因",
    "删除重复值怎么处理",                  # 「删除」是动作信号但全句无检索信号
    "有没有最近更新的人类肺数据",          # gap 带「更新」= 库更新问句
    "有没有小鼠脑的数据",
    "只保留能下载的",
    "下载量大的数据集有哪些",              # 「下载量」名词用法
    "推荐有 FASTQ 下载链接的人类肺数据",   # 「下载链接」产物名词（过滤条件）
    "找找人类肺癌数据",
    "有没有人类肺的单细胞数据",
]

#: 纯动作句/动作链（入库链否决只对同子句生效后仍成立）——一律不闸。
PURE_ACTION_NOT_GATED = [
    "检查更新，有新的就入库",
    "先把10x同步一下，有新的都入库",
    "把乳腺癌数据搜来入库",
    "把回收站里那个文件找回来",            # 「找回」是回滚动作，检索侧 (?<!找)回 不收
    "检查ArrayExpress是否有更新，若有新的人类肺数据就联网搜来入库",
    "检查ArrayExpress和ENCODE有没有更新，完了看看库里多少条",
    "把前 5 条打包下载",
    "帮我联网搜乳腺癌数据入库",            # 「联网搜…入库」= search_online 口语，动作链
    "数据库里有没有新的数据",              # 库更新问句（gap 只含「新的」）
    "同步一下10x，顺便检查下ENCODE有没有更新",
]

#: 真混合句（动作子句 ∧ 独立检索子句）——必须闸。含 v1 漏闸坐实与既有 8 正例。
MIXED_GATED = [
    # —— 既有正例（子句化后必须仍闸，防回归）——
    "检查数据库是否有更新，然后帮我找乳腺癌单细胞数据集",
    "检查10x更新，然后帮我找人类肺数据集",
    "帮我找乳腺癌数据，顺便看看数据库状态",
    "找肺癌数据然后打包前 3 条",
    "先同步一下10x，再搜小鼠脑数据",
    "有没有人类肺的单细胞数据，顺便检查下ENCODE有没有更新",
    "帮我搜乳腺癌数据集，再把结果导出引文",
    "看看库里有多少条数据，再找斑马鱼的",
    # —— 新增 ——
    "检查更新，有新的就入库，再帮我找乳腺癌数据",   # v1 漏闸实锤（全句级入库链否决误赦）
    "检查10x更新，再帮我找小鼠脑数据",
    "先检查ENCODE更新，再帮我找小鼠脑数据，顺便看看库里多少条",  # 三段式
    "帮我找肺癌数据，再导出引文",                   # 检索 + 环外动作（generic 能力）
    "检查下库里多少条，再推荐几个人类肺的数据集",
]

#: 歧义/零信号句——拿不准不闸（误伤率 0 优先）。
AMBIGUOUS_NOT_GATED = [
    "",
    "乳腺癌",
    "这个数据集能下载吗",
    "数据下载不了怎么办",
    "有没有更新",
    "更新了没有啊",
    "同步化什么意思",                      # 「细胞周期同步化」族生物学术语
    "已入库的数据还能找回吗",
    "帮我看看这个数据集的质量分",
]


@pytest.mark.parametrize("text", PURE_SEARCH_NOT_GATED)
def test_bank_pure_search_not_gated(text):
    assert AX._hybrid_intent_gate(text) is False


@pytest.mark.parametrize("text", PURE_ACTION_NOT_GATED)
def test_bank_pure_action_not_gated(text):
    assert AX._hybrid_intent_gate(text) is False


@pytest.mark.parametrize("text", MIXED_GATED)
def test_bank_mixed_gated(text):
    assert AX._hybrid_intent_gate(text) is True


@pytest.mark.parametrize("text", AMBIGUOUS_NOT_GATED)
def test_bank_ambiguous_not_gated(text):
    assert AX._hybrid_intent_gate(text) is False


def test_bank_size_and_version_pinned():
    """负例库规模钉（≥40 条铁律）与词表版本钉（改词表必须递增版本并复跑本库）。"""
    total = (len(PURE_SEARCH_NOT_GATED) + len(PURE_ACTION_NOT_GATED)
             + len(MIXED_GATED) + len(AMBIGUOUS_NOT_GATED))
    assert total >= 40
    assert AX._HYBRID_LEXICON_VERSION == "v3-2026-08-31"


def test_action_res_derived_from_families():
    """结构恒等：动作闸正则表 = 族表并集 + EXTRA 程序派生——族表是唯一手写清单，
    闸正则不手抄第二份（两份清单必漂移）；EXTRA 恰为「安装」一条（无能力族可归）。"""
    assert AX._HYBRID_ACTION_RES == tuple(
        pat for _, pat, _, _ in AX._HYBRID_ACTION_FAMILIES) + AX._HYBRID_ACTION_RES_EXTRA
    assert all(a is b for a, b in zip(
        AX._HYBRID_ACTION_RES,
        tuple(pat for _, pat, _, _ in AX._HYBRID_ACTION_FAMILIES)
        + AX._HYBRID_ACTION_RES_EXTRA))
    assert len(AX._HYBRID_ACTION_RES_EXTRA) == 1
    assert AX._HYBRID_ACTION_RES_EXTRA[0].pattern == "安装"


def test_clause_split_is_deterministic():
    """子句切分：连词/标点切开、滤空段；同子句双信号不闸是 v2 的误判修复根基。"""
    assert AX._split_hybrid_clauses("检查更新，有新的就入库，再帮我找乳腺癌数据") == [
        "检查更新", "有新的就入库", "帮我找乳腺癌数据"]
    assert AX._split_hybrid_clauses("") == []
    assert AX._split_hybrid_clauses("乳腺癌") == ["乳腺癌"]


# ---------------------------------------------------------------- B. 能力账产出与核销矩阵

def _step(verb, ok=True, result=None):
    return {"verb": verb, "ok": ok, "result": result or {}, "slots": {}}


_CHECK_ZERO = {"sources": [{"source": "10x", "mode": "online",
                            "new_candidates": [], "new_count": 0}]}
_CHECK_HAS_NEW = {"sources": [{"source": "10x", "mode": "online",
                               "new_candidates": [{"accession": "E-MTAB-1"}],
                               "new_count": 1}]}


class TestRequiredCapabilities:
    """`_hybrid_required_capabilities` 的产出形状与归族（route_consensus 闸分支的账）。"""

    def test_shape_three_part_chain(self):
        caps = AX._hybrid_required_capabilities("检查更新，有新的就入库，再帮我找乳腺癌数据")
        ids = [c["capability"] for c in caps]
        assert ids == ["action.check_updates", "action.import", "search"]
        for c in caps:
            assert set(c) >= {"capability", "verbs", "label_zh", "anchor"}
            assert isinstance(c["verbs"], list) and c["label_zh"] and c["anchor"]
        assert caps[0]["verbs"] == ["curate.check_updates", "curate.sync_updates"]
        assert caps[2]["verbs"] == list(AX._SEARCH_SETTLE_VERBS)

    def test_family_classification(self):
        caps = AX._hybrid_required_capabilities(
            "先检查ENCODE更新，再帮我找小鼠脑数据，顺便看看库里多少条")
        ids = [c["capability"] for c in caps]
        assert ids == ["action.check_updates", "search", "action.db_status"]

    def test_generic_family_has_empty_verbs(self):
        """环面给不出工具的动作（导出引文）归 generic：verbs 空 → 只能靠 declined 交代。"""
        caps = AX._hybrid_required_capabilities("帮我找肺癌数据，再导出引文")
        ids = [c["capability"] for c in caps]
        assert ids == ["search", "action.generic"]
        gen = caps[1]
        assert gen["verbs"] == [] and "导出引文" in gen["label_zh"]

    def test_same_family_deduped(self):
        caps = AX._hybrid_required_capabilities("检查10x更新，再检查ENCODE更新，再搜小鼠脑数据")
        assert [c["capability"] for c in caps].count("action.check_updates") == 1


class TestCapabilitySettlement:
    """`_capabilities_unsettled` / `_capability_item_states` 零信任对账矩阵。"""

    CAPS = AX._hybrid_required_capabilities("检查更新，有新的就入库，再帮我找乳腺癌数据")

    def test_all_missing_when_no_steps(self):
        unsettled = AX._capabilities_unsettled(list(self.CAPS), [])
        assert [u["capability"] for u in unsettled] == [
            "action.check_updates", "action.import", "search"]
        assert all(u["reason"] == "step_missing" for u in unsettled)

    def test_import_exempt_on_zero_new(self):
        """「有新的就入库」而 check 证明零新增 → 入库半天然豁免；检索半仍欠账。"""
        steps = [_step("curate.check_updates", result=_CHECK_ZERO)]
        states = {s["capability"]: s["status"]
                  for s in AX._capability_item_states(list(self.CAPS), steps)}
        assert states["action.check_updates"] == "done"
        assert states["action.import"] == "exempt"
        assert states["search"] == "missing"

    def test_import_not_exempt_when_new_found(self):
        """check 报出新增 → 豁免不成立，入库半必须真做（sync/search_online ok 步）。"""
        steps = [_step("curate.check_updates", result=_CHECK_HAS_NEW)]
        unsettled = AX._capabilities_unsettled(list(self.CAPS), steps)
        assert "action.import" in [u["capability"] for u in unsettled]
        # sync 步补上 → 入库半核销；检索半仍欠
        steps2 = steps + [_step("curate.sync_updates")]
        unsettled2 = AX._capabilities_unsettled(list(self.CAPS), steps2)
        assert [u["capability"] for u in unsettled2] == ["search"]

    def test_search_settled_by_any_search_verb(self):
        """检索半：rank/rerank/search.rerun 任一 ok 步即核销（与清单 rank 同真源）。"""
        for verb in AX._SEARCH_SETTLE_VERBS:
            steps = [_step("curate.check_updates", result=_CHECK_ZERO), _step(verb)]
            assert AX._capabilities_unsettled(list(self.CAPS), steps) == []
        # 失败步不算
        steps = [_step("curate.check_updates", result=_CHECK_ZERO),
                 _step("rank", ok=False)]
        assert [u["capability"] for u in AX._capabilities_unsettled(list(self.CAPS), steps)] == [
            "search"]

    def test_generic_declined_pool_one(self):
        """generic（环外动作）靠 declined_zh 交代；pool=1 只豁免一条。"""
        caps = [{"capability": "action.generic", "verbs": [],
                 "label_zh": "完成「导出引文」的操作", "anchor": "导出引文"},
                {"capability": "action.generic", "verbs": [],
                 "label_zh": "完成「发邮件」的操作", "anchor": "发邮件"}]
        unsettled = AX._capabilities_unsettled(caps, [], "导出引文本环做不到")
        assert [u["capability"] for u in unsettled] == ["action.generic"]
        assert unsettled[0]["reason"] == "generic_unaddressed"
        assert AX._capabilities_unsettled(caps[:1], [], "导出引文本环做不到") == []


class TestChecklistRankSettlement:
    """清单 expect=rank 的核销口径（修复1①：检索半可核销，否则混合句永远缺项）。"""

    def test_rank_settled_by_settle_verbs(self):
        task = {"task_id": "t1", "text": "找乳腺癌数据", "expect_verb": "rank", "sources": []}
        for verb in ("rank", "rerank", "search.rerun"):
            assert AX._task_settled_by(task, [_step(verb)]) is True
        assert AX._task_settled_by(task, [_step("rank", ok=False)]) is False
        assert AX._task_settled_by(task, []) is False

    def test_rank_ignores_sources(self):
        """检索覆盖全库——条目点了来源也不要求来源级对账。"""
        task = {"task_id": "t1", "text": "找乳腺癌数据", "expect_verb": "rank",
                "sources": ["10x"]}
        assert AX._task_settled_by(task, [_step("rerank")]) is True

    def test_checklist_unsettled_rank_roundtrip(self):
        checklist = [{"task_id": "t1", "text": "找乳腺癌数据", "expect_verb": "rank",
                      "sources": []}]
        assert AX._checklist_unsettled(checklist, [_step("search.rerun")]) == []
        assert AX._checklist_unsettled(checklist, [])[0]["reason"] == "step_missing"
        states = AX._checklist_item_states(checklist, [_step("rerank")])
        assert states[0]["status"] == "done" and states[0]["step_no"] == 1

    def test_rank_in_controlled_vocab_and_prompt(self):
        assert "rank" in AX._CHECKLIST_VERBS
        assert "rank（在本地库检索数据）" in AX._CHECKLIST_PROMPT_ZH


# ---------------------------------------------------------------- C. finish 聚合否决的第五路

class TestFinishVetoCapabilities:
    """`_finish_veto_all` 的能力账路：旧 7 参调用零变化；带 capabilities 缺项拒收。"""

    CAPS = AX._hybrid_required_capabilities("检查更新，有新的就入库，再帮我找乳腺癌数据")

    def test_legacy_seven_arg_call_unchanged(self):
        """无 capabilities 的旧调用（既有测试的 7 参形态）不产 capability 缺口。"""
        out = AX._finish_veto_all("都办完了", 1, [], [], [_step("rank")], "", "找乳腺癌数据")
        assert all(code != "capability_unsettled" for _t, code in out)

    def test_missing_capability_vetoed(self):
        """混合句只做了动作半（check 零新增 → import 豁免），检索半没做 → 拒收。"""
        steps = [_step("curate.check_updates", result=_CHECK_ZERO)]
        out = AX._finish_veto_all("都办完了", len(steps), [], [], steps, "",
                                  "检查更新，有新的就入库，再帮我找乳腺癌数据",
                                  capabilities=list(self.CAPS))
        cap_vetoes = [(t, c) for t, c in out if c == "capability_unsettled"]
        assert len(cap_vetoes) == 1 and "在本地库检索数据" in cap_vetoes[0][0]
        assert "没有对应的成功步骤" in cap_vetoes[0][0]

    def test_generic_capability_vetoed_when_unaddressed(self):
        caps = AX._hybrid_required_capabilities("帮我找肺癌数据，再导出引文")
        out = AX._finish_veto_all("都办完了", 1, [], [], [_step("rank")], "",
                                  "帮我找肺癌数据，再导出引文", capabilities=caps)
        cap_vetoes = [(t, c) for t, c in out if c == "capability_unsettled"]
        assert len(cap_vetoes) == 1 and "没有明确交代" in cap_vetoes[0][0]
        # declined_zh 交代后放行
        out2 = AX._finish_veto_all("都办完了，导出引文做不到", 1, [], [], [_step("rank")],
                                   "导出引文本环做不到", "帮我找肺癌数据，再导出引文",
                                   capabilities=caps)
        assert all(code != "capability_unsettled" for _t, code in out2)

    def test_all_settled_no_veto(self):
        steps = [_step("curate.check_updates", result=_CHECK_ZERO), _step("rank")]
        out = AX._finish_veto_all("都办完了", len(steps), [], [], steps, "",
                                  "检查更新，有新的就入库，再帮我找乳腺癌数据",
                                  capabilities=list(self.CAPS))
        assert all(code != "capability_unsettled" for _t, code in out)

    def test_teaching_suffix_registered(self):
        assert "capability_unsettled" in AX._VETO_TEACHING_SUFFIX


# ---------------------------------------------------------------- D. plan_action 保底通道的混合句弃权

def _counting_plan_action(monkeypatch):
    """plan_action 调用计数替身（弃权的本质 = 不调用 plan_action、不浪费 LLM 调用）。"""
    calls: list = []
    real = AP.plan_action

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(AP, "plan_action", spy)
    return calls


class _FakeMeta:
    resolution_status = "results"
    result_total = 5
    answer = "规则生成说明"
    pipeline = "rule-based"
    llm_attempted = False
    llm_succeeded = False
    llm_response_used = False
    llm_provider = ""
    llm_mode = "disabled"
    fallback = "rule-based formatting"
    fallback_reason = ""
    retrieved_data = []
    facets = []
    clarification = None
    coverage_caveats = []
    unused_query_terms = []
    or_handling = None
    active_filters = []
    interpretation = ""
    search_trace = []
    audit = None


def _fake_summary(*a, **k):
    holder = k.get("meta_out")
    if holder is not None:
        holder.append(_FakeMeta())
    return {"status": "results", "total": 5, "top_titles": ["样本"],
            "abstain_reason": "", "unresolved_terms": [], "note": ""}


@pytest.fixture
def _deterministic_turn(monkeypatch):
    """turn 级弃权测试的确定性环境：规则概览打桩（不起真实检索），llm_call 注入
    （agent_path=False，必走 plan_action 保底通道）。"""
    monkeypatch.setattr(turn, "rule_match_summary", _fake_summary)


_HYBRID_UTTERANCE = "检查10x更新，再帮我找人类肺数据"
_NONE_REPLY = json.dumps({"verb": "none", "confidence": "high"}, ensure_ascii=False)


@pytest.mark.usefixtures("_deterministic_turn")
class TestHybridAbstainFallback:
    """修复2：混合句在单次单动词保底通道明确弃权——route=none、plan_action 零调用、
    如实回音；对照组（纯动作句/纯检索句）仍进 plan_action 正常分流。"""

    def test_concurrent_path_abstains(self, monkeypatch):
        calls = _counting_plan_action(monkeypatch)
        llm_calls: list = []
        out = turn.route_turn(
            _HYBRID_UTTERANCE, config=LLMConfig(enable_llm=True, api_key="sk-t"),
            llm_call=lambda p: llm_calls.append(p) or _NONE_REPLY)
        assert calls == [], "混合句不许进 plan_action（单次单动词通道做不全整句）"
        assert llm_calls == [], "弃权先于一切 LLM 调用"
        assert out["route"] == "none"
        assert out["plan"]["hybrid_abstain"] is True
        assert out["plan"]["llm_status"] == "hybrid_abstain"
        assert "什么都没有执行" in out["echo_zh"]
        assert "AI 执行" in out["echo_zh"]
        assert out["needs_agent"] is False and out["suggestions"] == []

    def test_control_pure_action_still_plans(self, monkeypatch):
        """对照：纯动作句（无独立检索子句）不弃权——plan_action 照常分类。"""
        calls = _counting_plan_action(monkeypatch)
        out = turn.route_turn(
            "检查10x更新", config=LLMConfig(enable_llm=True, api_key="sk-t"),
            llm_call=lambda p: _NONE_REPLY)
        assert len(calls) == 1
        assert "hybrid_abstain" not in out["plan"]

    def test_control_pure_search_still_plans(self, monkeypatch):
        """对照：纯检索句（v1 误判族）不弃权——plan_action 照常分流到 search。"""
        calls = _counting_plan_action(monkeypatch)
        out = turn.route_turn(
            "帮我找可下载数据", config=LLMConfig(enable_llm=True, api_key="sk-t"),
            llm_call=lambda p: json.dumps(
                {"verb": "search.new", "quoted": "帮我找可下载数据",
                 "effective_query": "可下载数据", "confidence": "high"},
                ensure_ascii=False))
        assert len(calls) == 1
        assert out["route"] == "search"
        assert "hybrid_abstain" not in out["plan"]


@pytest.mark.usefixtures("_deterministic_turn")
class TestHybridAbstainAgentDown:
    """2026-08-30 bug3：agent 路径已启用但当轮大模型调用失败（401/超时等）时，
    混合句弃权回音必须说真实原因（大模型没接上、设置是开着的），
    绝不误导用户去「设置」里开已开着的「AI 执行」。"""

    def _agent_boom(self, monkeypatch):
        monkeypatch.setattr(AX, "agent_available", lambda: True)

        def boom(*a, **k):
            raise AX.AgentError("llm 401")

        monkeypatch.setattr(AX, "plan_with_agent", boom)
        monkeypatch.setattr(AX, "plan_with_agent_events", boom)
        # 并发路径的 plan_action 前端直派探测：探测失败按未知处理走 agent 图
        monkeypatch.setattr(AP, "plan_action",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("probe off")))

    def test_concurrent_agent_down_echo(self, monkeypatch):
        self._agent_boom(monkeypatch)
        out = turn.route_turn(
            _HYBRID_UTTERANCE, config=LLMConfig(enable_llm=True, api_key="sk-t"))
        assert out["route"] == "none"
        assert out["plan"]["hybrid_abstain"] is True
        assert out["plan"].get("agent_fallback") is True
        assert "什么都没有执行" in out["echo_zh"]
        assert "没能接得上" in out["echo_zh"]
        assert "是开着的，不用改设置" in out["echo_zh"]
        assert "确认「AI 执行」已开启" not in out["echo_zh"]
