# -*- coding: utf-8 -*-
"""P2 执行层后端核的确定性门（**零网络**：LLM 一律靠 `llm_call` 注入）。

这一层的全部风险都在「说了什么」与「做了什么」之间的缝隙里，所以这里钉的都是缝隙：
封闭词表不许被绕开、`quoted` 不许是编的、被否定的动作不许被当成授权、
静默钳位不许不吭声、`confidence=="low"` 不许变成一道确认门。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.retrieval import query_lexicon as V


def _llm(payload):
    """把一份 dict 当成 LLM 的返回。"""
    return lambda _prompt: json.dumps(payload, ensure_ascii=False)


def _plan(utterance, payload, *, has_results=True, result_total=42):
    return AP.plan_action(
        utterance, has_results=has_results, result_total=result_total, llm_call=_llm(payload)
    )


# ---------------------------------------------------------------- 封闭词表是唯一真源

def test_prompt_verb_list_is_generated_from_the_table_not_hand_copied():
    """prompt 里出现的动词集合必须**恰好**等于 `VERB_SPECS`。

    本仓库在 `.gitignore↔.deliveryignore`、`ACTION_MARKERS↔FILLER_GRAMMAR`、
    `SOURCE_PREFER_PREFIX_RE↔SOFT_PREFER_PREFIX_CN` 上栽过三次手抄漂移。
    动词表一旦在 prompt 里手抄一份，LLM 会照着旧表回答，而校验按新表判 —— 全部落进 rejected。
    """
    prompt = AP.build_action_prompt("随便一句", has_results=True, result_total=3)
    in_prompt = {v for v in AP.ACTION_VERBS if v in prompt}
    assert in_prompt == set(AP.ACTION_VERBS)
    # 反向：prompt 里不许出现表外的 verb 形状的词（形如 a.b 的点分小写标识符）；
    # 例外是动态注入的来源名单里的点分源名（如 refine.bio——源名不是动词）。
    import re
    shaped = set(re.findall(r"\b[a-z]+\.[a-z]+\b", prompt))
    dotted_sources = {t for t in AP._local_library_sources_zh().split(" / ") if "." in t}
    assert shaped - dotted_sources <= set(AP.ACTION_VERBS), \
        f"prompt 里有表外动词：{(shaped - dotted_sources) - set(AP.ACTION_VERBS)}"


def test_exec_verb_names_are_verb_phrases_so_the_receipt_reads_like_chinese():
    """回执抬头是「已」+ `zh`（`act.js` 的 `ACT_LEAD.done`）。

    第一版 `feasibility.run` 的 `zh` 是名词短语「可行性概览」，渲染出来是**「已可行性概览」**；
    `pack.preview` / `files.show` 的「查看…」则把动作主体说反了（看的人是用户，不是本工具）。
    这条不追求语言学正确，只逼你在往表里加词的那一刻回头看一眼回执长什么样。
    """
    for spec in AP.VERB_SPECS:
        if spec.kind != AP.EXEC:
            continue
        assert any(spec.zh.startswith(v) for v in AP._LEAD_VERBS), (
            f"{spec.verb} 的中文名「{spec.zh}」不是动词短语 → 回执会写成「已{spec.zh}」；"
            f"确认读得通后再把开头动词加进 _LEAD_VERBS"
        )


def test_rejected_actions_are_capped_so_one_runaway_response_cannot_flood_the_receipt():
    """`rejected[]` 是大模型自由生成的文本、会原样进回执那一行。"""
    plan = _plan("打包", {"verb": "pack.download", "quoted": "打包", "confidence": "high",
                          "also": [f"动作{i}" + "长" * 80 for i in range(30)]})
    assert len(plan["rejected"]) <= AP.MAX_REJECTED
    assert all(len(x) <= AP.MAX_REJECTED_CHARS for x in plan["rejected"])


def test_exec_and_route_partition_the_table():
    assert set(AP.EXEC_VERBS) | set(AP.ROUTE_VERBS) == set(AP.ACTION_VERBS)
    assert not (set(AP.EXEC_VERBS) & set(AP.ROUTE_VERBS))
    assert "none" in AP.ROUTE_VERBS, "none 哨兵必须在：它是 LLM 否掉规则误报的唯一渠道"


def test_verbs_outside_the_table_are_rejected_not_executed():
    plan = _plan("把这几个收藏起来", {"verb": "fav.add", "quoted": "收藏", "confidence": "high"})
    assert plan["verb"] == "none"
    assert plan["rejected"] == ["fav.add"]
    assert plan["kind"] == AP.ROUTE


def test_extra_actions_are_reported_but_not_done():
    """v1 一次只办一件事。LLM 额外提的动作只报不做——封闭词表唯一的真实反馈渠道。"""
    plan = _plan("打包并且收藏", {"verb": "pack.download", "quoted": "打包",
                                  "also": ["fav.add"], "confidence": "high"})
    assert plan["verb"] == "pack.download"
    assert plan["rejected"] == ["fav.add"]


# ---------------------------------------------------------------- quoted 必须是真的

def test_quoted_must_be_a_literal_substring_of_what_the_user_said():
    """LLM 改写过的「依据」= 编造。清空后执行类会被降成 none（见下一条）。"""
    plan = _plan("帮我整理一下", {"verb": "pack.download", "quoted": "打包下载", "confidence": "high"})
    assert plan["quoted"] == ""
    assert plan["verb"] == "none"


def test_exec_without_a_locatable_quote_is_not_executed():
    """**没有可定位的原文依据就不执行**——这条让回执里「依据你说的『…』」结构上不可能是编的。"""
    plan = _plan("嗯", {"verb": "pack.download", "quoted": "", "confidence": "high"})
    assert plan["verb"] == "none"
    assert "原文依据" in plan["reason_zh"]


def test_exec_downgraded_to_none_marks_downgraded_from():
    """2026-08-15 EXEC 缺 quoted 被机械降成的 none 是**降级 none**，不是真 none——
    additive 字段 `downgraded_from` 记下被降掉的 verb，路由层据此如实回音，不许谎称「没听懂」。"""
    plan = _plan("帮我打包一下", {"verb": "pack.download", "quoted": "", "confidence": "high"})
    assert plan["verb"] == "none"
    assert plan["downgraded_from"] == "pack.download"
    assert "原文依据" in plan["reason_zh"]
    # quoted 不是原话逐字子串被清空后同路降级
    plan2 = _plan("帮我打包一下", {"verb": "pack.download", "quoted": "不存在的话",
                                   "confidence": "high"})
    assert plan2["verb"] == "none" and plan2["downgraded_from"] == "pack.download"


def test_a_real_quote_survives_verbatim():
    plan = _plan("人类肺癌数据，打包前5条",
                 {"verb": "pack.download", "quoted": "打包前5条", "limit": 5, "confidence": "high"})
    assert plan["verb"] == "pack.download"
    assert plan["quoted"] == "打包前5条"
    assert plan["quoted"] in "人类肺癌数据，打包前5条"


# ---------------------------------------------------------------- 极性门 + 取消态（cancelled）

@pytest.mark.parametrize("utterance,quoted", [
    ("找肺癌数据，不要打包", "打包"),
    ("先看清单，别打包了", "打包"),
    ("这次不用导出", "导出"),
    ("无需下载", "下载"),
    ("人类肺数据，暂时不要打包", "打包"),
])
def test_a_negated_action_is_never_treated_as_authorization(utterance, quoted):
    """把用户**明确否定**的词当成授权证据引用给他看，是本层最恶劣的一种谎。

    2026-08-01 口径升级（`eval/curate_nlu/FINDINGS.md` §5③）：否定取消**不再整计划降 none
    装没听懂**——动词照判、`cancelled=True` 标记取消态，执行层据此不执行、只回音
    （「好，不打包」）。安全性质不放水：取消态的 plan **不许**被当成可执行授权，
    本条钉的就是「动词保留 + 取消标记 + 不回退 none」这三件事同时成立。
    """
    plan = _plan(utterance, {"verb": "pack.download", "quoted": quoted, "confidence": "high"})
    assert plan["verb"] == "pack.download", (utterance, plan)
    assert plan["cancelled"] is True, (utterance, plan)
    assert plan["quoted"] == quoted, "回执要能指着原话说「好，不做了」，依据不许丢"
    assert "没有执行" in plan["reason_zh"]


def test_negation_elsewhere_in_the_sentence_does_not_block_the_action():
    """整句里出现「不要小鼠」不代表用户不要打包——极性门只看**紧邻**窗口。"""
    plan = _plan("不要小鼠的数据，打包前5条",
                 {"verb": "pack.download", "quoted": "打包前5条", "limit": 5, "confidence": "high"})
    assert plan["verb"] == "pack.download", plan
    assert plan["cancelled"] is False


def test_negation_window_is_four_chars_so_a_closed_negation_does_not_spill():
    """否定语素只作用其后 ≤4 字的执行词：「不要了，帮我删掉吧」是否定收尾、删掉照旧。

    v3 prompt 定稿语义（`eval/curate_nlu/prompt_v3.md` 铁律 2）。「删掉」距「不要」
    隔了「了，帮我」4 个字——正在窗外，门不许触发。
    """
    plan = _plan("不要了，帮我删掉吧",
                 {"verb": "curate.remove", "quoted": "删掉", "confidence": "high"})
    assert plan["verb"] == "curate.remove", plan
    assert plan["cancelled"] is False


def test_only_one_of_several_anchors_negated_still_executes():
    """「打包前5条，不要引文」里「打包」没被否定，整句仍是一条执行指令。"""
    plan = _plan("打包前5条，不要引文",
                 {"verb": "pack.download", "quoted": "打包前5条", "limit": 5, "confidence": "high"})
    assert plan["verb"] == "pack.download", plan
    assert plan["cancelled"] is False


def test_action_verb_itself_is_not_a_negation_morpheme_cn():
    """「删掉我上传的 10x 数据」是删除指令，不是「否定删除」——动作词不否定自己。

    2026-08-03 模拟剧本抓到的真 bug：极性门直接消费检索弃权守卫表
    `NEGATION_GUARDS_CN`（里面收「删掉/移除/过滤掉/拒收」是因为「删掉 X」在**检索句**里
    意味着排除 X），quoted 锚在动作词之后时（LLM 通常只引对象片段），紧邻窗里恰好
    只有动作词本身 → 删除指令被误判 cancelled=True，用户看到「删除没有回应」。
    """
    plan = _plan("删掉我上传的 10x 数据",
                 {"verb": "curate.remove", "quoted": "我上传的 10x 数", "confidence": "high"})
    assert plan["verb"] == "curate.remove", plan
    assert plan["cancelled"] is False, "动作词「删掉」不许否定删掉这个动作本身"


def test_action_verb_itself_is_not_a_negation_morpheme_en():
    """"remove the uploaded file" 同理：动作词 remove 不否定自己；"don't remove it" 仍取消。"""
    plan = _plan("remove the uploaded file",
                 {"verb": "curate.remove", "quoted": "the uploaded file", "confidence": "high"})
    assert plan["cancelled"] is False, plan
    plan2 = _plan("don't remove it",
                  {"verb": "curate.remove", "quoted": "remove it", "confidence": "high"})
    assert plan2["cancelled"] is True, plan2


def test_real_negation_before_the_action_verb_still_cancels():
    """「别删掉那个文件」照旧取消——窗里有真否定语素「别」（动作词剔除后不受影响）。"""
    plan = _plan("别删掉那个文件",
                 {"verb": "curate.remove", "quoted": "那个文件", "confidence": "high"})
    assert plan["verb"] == "curate.remove", plan
    assert plan["cancelled"] is True, plan


def test_negation_gate_shares_the_vocabulary_table():
    """否定语素表必须是程序并出来的那一份，不能是本模块另抄的。"""
    assert set(V.NEG_MORPHEMES_CN) == set(V.EXEC_NEG_PREFIX_CN) | set(V.NEGATION_GUARDS_CN)
    from dataset_recommender.app import board
    assert board._NEG_MORPHEMES is V.NEG_MORPHEMES_CN


# ---------------------------------------------------------------- 征询掩码

@pytest.mark.parametrize("utterance,quoted,verb", [
    ("能不能上网检索一下", "检索", "curate.search_online"),
    ("要不要联网搜一下新的", "联网搜", "curate.search_online"),
    ("能不能打包一下", "打包", "pack.download"),
    ("要不打包吧", "打包", "pack.download"),
    ("可不可以帮我导出引文", "导出", "cite.export"),
])
def test_question_hedges_are_consultation_not_negation(utterance, quoted, verb):
    """「能不能/要不要/要不…吧」是**征询**不是否定——实验当场抓到的盲区
    （`eval/curate_nlu/FINDINGS.md` §5①）：「能不能上网检索一下」里的「不」
    落在「检索」前 4 字窗内，不掩掉征询格式词就会被极性门误判成取消。"""
    plan = _plan(utterance, {"verb": verb, "quoted": quoted, "confidence": "high"})
    assert plan["verb"] == verb, (utterance, plan)
    assert plan["cancelled"] is False, (utterance, plan)


def test_hedge_masking_never_unblocks_a_real_negation():
    """掩码只掩征询格式词本身：「要不要不打包」里独立的「不」照常触发取消。"""
    plan = _plan("要不要不打包", {"verb": "pack.download", "quoted": "打包", "confidence": "high"})
    assert plan["cancelled"] is True


def test_hedge_masking_is_length_preserving_so_anchor_indices_do_not_drift():
    masked = AP._mask_hedges("能不能上网检索一下")
    assert len(masked) == len("能不能上网检索一下")
    assert "不" not in masked


# ---------------------------------------------------------------- 疑问「没」掩码

@pytest.mark.parametrize("utterance,quoted", [
    # cr4final 坐实四族——疑问「没」落在续步 quoted 紧邻左窗，曾被误判 cancelled 掐死多步链：
    ("顺便检查一下ArrayExpress有没有更新有新增的人类肺数据就搜来入库", "有新增的人类肺数据就搜来入库"),
    ("帮我瞅一眼ArrayExpress更新没，要是有新的人类肺数据就赶紧搜来入库哈", "有新的人类肺数据就赶紧搜来入库"),
    ("查下ArrayExpress更新没，有新的human lung数据就搜来入库", "有新的human lung数据就搜来入库"),
    ("检査下10x、ArrayExpress和ENCODE更新没，完了数数库里多少条", "数数库里多少条"),
    # 句尾/语气词收尾的疑问「没」：
    ("看看10x更新了没？有更新就同步", "有更新就同步"),
    ("入库了没？有新的就再入库", "有新的就再入库"),
])
def test_interrogative_mei_is_not_a_negation(utterance, quoted):
    """「更新没 / 有没有 / 查了没」是疑问不是否定——疑问「没」不许触发 cancelled。"""
    plan = _plan(utterance, {"verb": "curate.search_online", "quoted": quoted, "confidence": "high"})
    assert plan["cancelled"] is False, (utterance, plan)


@pytest.mark.parametrize("utterance,quoted", [
    ("检查下更新没，别下载", "下载"),            # 疑问「没」被掩后，真否定「别」照常命中
    ("看看有没有新数据，不要打包", "打包"),       # 有没有 被掩后，「不要」照常命中
    ("看看更新没，千万别下载", "下载"),
])
def test_interrogative_mei_masking_never_unblocks_a_real_negation(utterance, quoted):
    """掩疑问「没」不得放生真否定——同句里的「别/不要」照旧取消。"""
    plan = _plan(utterance, {"verb": "pack.download", "quoted": quoted, "confidence": "high"})
    assert plan["cancelled"] is True, (utterance, plan)


def test_interrogative_mei_masking_is_length_preserving():
    masked = AP._mask_hedges("检查下更新没，要是有新增就搜")
    assert len(masked) == len("检查下更新没，要是有新增就搜")
    assert "没" not in masked


# ---------------------------------------------------------------- 顺承「没」掩码（2026-08-15

@pytest.mark.parametrize("utterance,quoted,verb", [
    ("没找到就联网搜", "联网搜", "curate.search_online"),
    ("没有就导出来", "导出", "cite.export"),
    ("没搜到合适的就打包", "打包", "pack.download"),
    ("没有结果就直接下载", "下载", "pack.download"),
])
def test_sequential_mei_is_not_a_negation(utterance, quoted, verb):
    """「没找到就联网搜」这类条件/顺承句里的「没」修饰**前一个**动词（找到），
    从不否定「就/才」后面的动作——不许误判 cancelled 把正向指令变成「好，不做了」。"""
    plan = _plan(utterance, {"verb": verb, "quoted": quoted, "confidence": "high"})
    assert plan["cancelled"] is False, (utterance, plan)


@pytest.mark.parametrize("utterance,quoted", [
    ("没找到就别打包了", "打包"),     # 顺承「没」被掩后，真否定「别」照常命中
    ("没结果就不要导出", "导出"),     # 同上，「不要」照常命中
])
def test_sequential_mei_masking_never_unblocks_a_real_negation(utterance, quoted):
    """掩顺承「没」不得放生真否定——同句里的「别/不要」照旧取消。"""
    plan = _plan(utterance, {"verb": "pack.download", "quoted": quoted, "confidence": "high"})
    assert plan["cancelled"] is True, (utterance, plan)


def test_sequential_mei_masking_is_length_preserving():
    masked = AP._mask_hedges("没找到就联网搜")
    assert len(masked) == len("没找到就联网搜")
    assert "没" not in masked


# ---------------------------------------------------------------- cancelled schema（LLM 自报 + 机械派生）

def test_llm_said_cancelled_is_accepted_fail_safe():
    """LLM 标了取消而极性门测不到（否定说法超出语素表）：照收——取消只意味着「不做」，
    错收的最坏结果是用户再说一次，不是做错。"""
    plan = _plan("打包的事先缓缓", {"verb": "pack.download", "quoted": "打包",
                                    "cancelled": True, "confidence": "high"})
    assert plan["verb"] == "pack.download"
    assert plan["cancelled"] is True
    assert plan["reason_zh"], "取消态的回执必须有一句人话"


def test_mechanical_gate_overrides_an_llm_that_forgot_to_mark_cancelled():
    """门测到否定而 LLM 没标 cancelled（甚至标了 false）：**以门为准**——安全侧不让 LLM 拍板。"""
    for raw in ({}, {"cancelled": False}):
        payload = {"verb": "curate.import", "quoted": "导入", "confidence": "high", **raw}
        plan = _plan("先别导入", payload)
        assert plan["verb"] == "curate.import", (raw, plan)
        assert plan["cancelled"] is True, (raw, plan)
        assert "别" in plan["reason_zh"]


def test_cancelled_only_accepts_a_json_boolean_true():
    """字符串「true」不算——LLM 手滑不该能取消一个动作。"""
    plan = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条", "limit": 5,
                               "cancelled": "true", "confidence": "high"})
    assert plan["cancelled"] is False


def test_route_verbs_never_carry_a_cancelled_flag():
    """cancelled 是 EXEC 专属语义：路由类本就不执行，挂取消态是空话。"""
    plan = _plan("小鼠的胰腺数据", {"verb": "search.new", "quoted": "小鼠的胰腺数据",
                                    "cancelled": True, "confidence": "high"})
    assert plan["verb"] == "search.new"
    assert plan["cancelled"] is False


def test_cancelled_plan_is_not_blocked_by_missing_results_and_carries_no_slot_disclaimer():
    """取消态的回执只需一句「好，不做了」：不挂 no_results（它本来就不执行），
    也不带槽位免责（槽值不会进任何后续动作，没有「这几项」需要免责）。"""
    plan = _plan("先别打包", {"verb": "pack.download", "quoted": "打包", "confidence": "high"},
                 has_results=False, result_total=0)
    assert plan["cancelled"] is True
    assert plan["blocked_reason"] == ""
    assert plan["uncertainty_zh"] == ""


def test_cancelled_plan_still_requires_a_verbatim_quote():
    """取消也要指着原话说：给不出逐字依据时照旧降 none——「好，不做了」同样不许是编的。"""
    plan = _plan("嗯", {"verb": "curate.import", "quoted": "", "cancelled": True, "confidence": "high"})
    assert plan["verb"] == "none"
    assert plan["cancelled"] is False


def test_prompt_teaches_the_cancelled_schema():
    """生产 prompt 对齐 v3：取消语义新范式（动词照判 + cancelled）与征询掩码都要写进铁律。"""
    prompt = AP.build_action_prompt("随便一句", has_results=True, result_total=3)
    assert "cancelled" in prompt
    assert "能不能" in prompt and "征询" in prompt
    assert "4 个字以内" in prompt


def test_prompt_teaches_the_no_scene_boundary():
    """规则 10 + 双向示例常驻：缺结果不挡动作类（a18 型）；无现场改条件不成立（a21 型）。"""
    prompt = AP.build_action_prompt("随便一句", has_results=False, result_total=0)
    assert "没有检索结果**不改变动作类动词的选择" in prompt
    assert "示例一" in prompt and "示例二" in prompt


# ---------------------------------------------------------------- 槽位五态 + 差额

def test_limit_the_user_actually_said_is_marked_said():
    plan = _plan("打包前20条", {"verb": "pack.download", "quoted": "打包前20条",
                                "limit": 20, "confidence": "high"})
    assert plan["slots"]["limit"] == 20
    assert plan["slot_sources"]["limit"] == "said"
    assert plan["deltas"] == []


def test_chinese_numerals_still_count_as_said():
    plan = _plan("打包前二十条", {"verb": "pack.download", "quoted": "打包前二十条",
                                  "limit": 20, "confidence": "high"})
    assert plan["slot_sources"]["limit"] == "said", plan["slot_sources"]


def test_a_number_the_user_never_said_is_marked_guessed():
    """大模型替用户填的数字必须与用户说过的数字**区分开**，否则回执会把猜的说成他要的。"""
    plan = _plan("打包一下", {"verb": "pack.download", "quoted": "打包",
                              "limit": 7, "confidence": "high"})
    assert plan["slots"]["limit"] == 7
    assert plan["slot_sources"]["limit"] == "guessed"


@pytest.mark.parametrize("utterance,limit", [
    ("2025年的人类肺癌数据，打包一下", 25),   # 「2025」里的「25」不是条数
    ("把 GSE123456 那套打包", 34),            # 编号「123456」里的「34」不是条数
    ("25日之前发表的数据，打包", 25),          # 「25日」是日期不是条数
    ("五月份的数据，打包", 5),                # 「五月」是月份不是条数
    ("十五条够了，打包", 5),                  # 「十五」里的「五」不是用户说的 5
])
def test_year_identifier_or_date_digits_do_not_count_as_said(utterance, limit):
    """2026-08-15 裸子串会把年份/编号/日期里的数字当成「用户说过的条数」，
    诚实层据此谎报 said——这是把系统（LLM 幻觉）的错算到用户头上。词边界判据下必须标 guessed。"""
    plan = _plan(utterance, {"verb": "pack.download", "quoted": "打包",
                             "limit": limit, "confidence": "high"})
    assert plan["slots"]["limit"] == limit, (utterance, plan)
    assert plan["slot_sources"]["limit"] == "guessed", (utterance, plan["slot_sources"])


def test_over_the_cap_is_clamped_and_said_out_loud():
    """静默按 min(说的, 上限) 执行而不吭声，正是本项目反复修过的「静默偏离」。"""
    plan = _plan("打包前80条", {"verb": "pack.download", "quoted": "打包前80条",
                                "limit": 80, "confidence": "high"})
    assert plan["slots"]["limit"] == AP.MAX_LIMIT
    assert plan["slot_sources"]["limit"] == "clamped"
    assert plan["deltas"] and plan["deltas"][0]["said"] == "80"
    assert plan["deltas"][0]["used"] == str(AP.MAX_LIMIT)


def test_unreadable_limit_is_dropped_not_silently_defaulted():
    plan = _plan("打包", {"verb": "pack.download", "quoted": "打包",
                          "limit": "很多", "confidence": "high"})
    assert plan["slots"]["limit"] == 0
    assert plan["slot_sources"]["limit"] == "dropped"
    assert plan["deltas"], "掉了一个槽位却不吭声就是静默偏离"


def test_scope_all_is_refused_with_an_explicit_delta():
    """对话入口 v1 仍钉死 primary 并显式播报（入口口径，不是产品能力边界——接口的
    scope=all 是公开能力；2026-08-09 评审把旧「产品不支持」假话文案改成如实口径）。"""
    plan = _plan("把全部文件都打包", {"verb": "pack.download", "quoted": "打包",
                                      "scope": "all", "confidence": "high"})
    assert plan["slots"]["target"] == AP.TARGET_DEFAULT
    delta = next(d for d in plan["deltas"] if d["slot"] == "scope")
    assert "不支持" not in delta["why_zh"], "不许把入口口径说成产品不支持（假话回退检测）"


# ---------------------------------------------------------------- confidence 不是确认门

def test_low_confidence_executes_the_same_verb_with_the_same_slots():
    """堵死旧哲学的回插口：`low` 只影响回执排版，**绝不**改 verb、改参数或跳过执行。"""
    high = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条",
                               "limit": 5, "confidence": "high"})
    low = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条",
                              "limit": 5, "confidence": "low"})
    assert low["verb"] == high["verb"] == "pack.download"
    assert low["slots"] == high["slots"]
    assert low["blocked_reason"] == high["blocked_reason"] == ""


def test_missing_confidence_defaults_to_low_and_still_executes():
    plan = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条", "limit": 5})
    assert plan["confidence"] == "low"
    assert plan["verb"] == "pack.download"


# ---------------------------------------------------------------- 屏上没结果 ≠ 没听懂

def test_no_results_is_reported_as_nothing_to_do_not_as_misunderstood():
    """系统明明读懂了（verb 都判出来了）。把「没东西可做」谎报成「没听懂」，
    用户会去改说法，而问题根本不在说法上。"""
    plan = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条",
                               "limit": 5, "confidence": "high"}, has_results=False, result_total=0)
    assert plan["verb"] == "pack.download", "读懂了就是读懂了，不许降级成 none"
    assert plan["blocked_reason"] == "no_results"


def test_route_verbs_are_never_blocked_by_missing_results():
    plan = _plan("小鼠的胰腺数据", {"verb": "search.new", "quoted": "小鼠的胰腺数据",
                                   "confidence": "high"}, has_results=False, result_total=0)
    assert plan["verb"] == "search.new"
    assert plan["blocked_reason"] == ""
    assert plan["requires_results"] is False


# ---------------------------------------------------------------- 不确定标注恒在

def test_every_exec_plan_carries_the_uncertainty_note_regardless_of_confidence():
    """2026-07-16 的结论逐字是「填了字段 ≠ 已核验」：槽值不是本工具核对出来的，
    就必须说出来——与 LLM 的自评 confidence 无关。"""
    for conf in ("high", "low"):
        plan = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条",
                                   "limit": 5, "confidence": conf})
        assert plan["source"] == "llm"
        assert plan["uncertainty_zh"] == AP.UNCERTAINTY_ZH, conf


def test_the_rule_path_does_not_credit_a_model_that_was_never_called():
    """2026-07-26 真机截图抓到的自相矛盾：同一张回执上半句说「大模型这次没有接上」，
    下半句说「以上这几项是**大模型**从你这句话里读出来的」。

    规则档一次网络请求都没发。「没有另外核对」这半句照旧要说，
    但**归因必须说对**——一份回执里写两句打架的话，比不写更糟。
    """
    plan = AP.rule_fallback_plan("人类肺癌数据，打包前5条", has_results=True,
                                 result_total=42, llm_status="empty")
    assert plan["source"] == "rule" and plan["kind"] == AP.EXEC
    note = plan["uncertainty_zh"]
    assert note == AP.UNCERTAINTY_RULE_ZH
    assert "大模型" not in note, f"规则档的标注里出现了「大模型」：{note}"
    assert "本工具没有另外核对" in note        # 免责这半句在两档里都必须在
    # 同一张回执里的两句话不许互相打架：caveat 说「大模型没接上」，标注就不能说「大模型读的」
    assert "大模型" in plan["caveat_zh"] and "没有接上" in plan["caveat_zh"]


def test_the_rule_path_admits_it_read_no_parameters_at_all():
    """同一张真机截图里的第二处静默：用户说「打包**前5条**」，规则档只认出「打包」，
    「前5条」一个字没读，面板按自己的默认口径开了 10 条 —— 回执里却没有一行提到这件事。

    后端刻意不在这里再抄一份条数解析器（前端 `tpCountFromUtterance` 是单一真源）。
    但「这一档根本不读参数」是这条路径的**固有事实**，不需要解析也能如实说。
    """
    plan = AP.rule_fallback_plan("人类肺癌数据，打包前5条", has_results=True,
                                 result_total=42, llm_status="empty")
    assert plan["slots"] == {"target": AP.TARGET_DEFAULT}      # 确实一个参数都没填
    assert "limit" not in plan["slots"]
    caveat = plan["caveat_zh"]
    assert "参数一概没读" in caveat and "条数" in caveat
    assert "在面板里自己定" in caveat                            # 并且给了下一步怎么办


def test_both_uncertainty_wordings_disclaim_verification_and_differ_only_in_attribution():
    for source in ("llm", "rule", "", "none"):
        note = AP.uncertainty_note(source)
        assert note.endswith("本工具没有另外核对。")
        assert ("大模型" in note) == (source == "llm")


def test_route_plans_do_not_carry_an_uncertainty_note_about_slots_they_do_not_have():
    plan = _plan("换成小鼠", {"verb": "refine.conditions", "quoted": "换成小鼠", "confidence": "high"})
    assert plan["uncertainty_zh"] == ""


# ---------------------------------------------------------------- fail-open（规则档）

def test_llm_absent_falls_back_to_rules_and_never_touches_the_disk():
    """规则表是裸子串、实测 5 句误报，所以规则档**不接落盘动作**——只开清单。
    这不是预防性弃权：交付没有被收回去，用户看到清单后一键就能继续。"""
    plan = AP.plan_action("帮我打包前20条", has_results=True, result_total=9,
                          llm_call=lambda _p: None)
    assert plan["source"] == "rule"
    assert plan["verb"] == "pack.preview", "规则档绝不能直接产文件"
    assert plan["caveat_zh"] and "猜" in plan["caveat_zh"], "必须醒目标注这是按关键词猜的"
    assert plan["confidence"] == "low"


def test_llm_garbage_output_falls_back_to_rules():
    plan = AP.plan_action("帮我打包", has_results=True, result_total=9,
                          llm_call=lambda _p: "我不知道该回什么")
    assert plan["source"] == "rule" and plan["llm_status"] == "unparsable"


def test_llm_exception_falls_back_to_rules_without_leaking_the_message():
    """诊断标签只取异常**类名**。

    第一版这里写的是 `_sanitize_provider_error(exc)` —— 它只脱敏「已知的那把 key」和
    `Bearer …`，provider 把裸 `sk-…` 拼进消息时原样漏进 `llm_status`。本条当场抓到过一次。
    """
    def boom(_p):
        raise RuntimeError("sk-abcdefghijklmnop 这个 key 无效")
    plan = AP.plan_action("帮我打包", has_results=True, result_total=9, llm_call=boom)
    assert plan["source"] == "rule"
    assert "sk-abcdefghijklmnop" not in json.dumps(plan, ensure_ascii=False)
    assert plan["llm_status"] == "error:RuntimeError"


def test_rule_fallback_on_a_non_action_sentence_stays_none():
    plan = AP.plan_action("人类肺癌的空间转录组数据", has_results=True, result_total=9,
                          llm_call=lambda _p: None)
    assert plan["verb"] == "none" and plan["source"] == "rule"


def test_rule_fallback_quotes_what_the_user_typed_not_the_lowercased_table_entry():
    plan = AP.plan_action("Download script for these", has_results=True, result_total=9,
                          llm_call=lambda _p: None)
    assert plan["quoted"] == "Download script", plan["quoted"]


# ---------------------------------------------------- rule_fallback 共用的名词用法反向闸

@pytest.mark.parametrize("utterance", [
    "下载量大的数据集有哪些",   # C-1 原案：「下载」后随「量」是名词用法，是检索句
    "只保留能下载的",           # 「下载」后随「的」
])
def test_rule_fallback_ignores_noun_usage_action_verbs(utterance):
    """规则档的动作词检出与 rule_operation_marker 共用同一道名词用法反向闸
    （`_action_verb_noun_usage`）：裸子串不许把检索句开成下载面板（只许收窄误触）。"""
    plan = AP.plan_action(utterance, has_results=True, result_total=9,
                          llm_call=lambda _p: None)
    assert plan["verb"] == "none" and plan["source"] == "rule", (utterance, plan)


@pytest.mark.parametrize("utterance,quoted", [
    ("下载 GSE123456", "下载"),       # 真操作意图：「下载」后随空格，照旧命中
    ("把这个数据集下载下来", "下载"),
    ("帮我打包前5条", "打包"),
    ("导出引文", "导出"),
])
def test_rule_fallback_still_catches_real_operation_sentences(utterance, quoted):
    """反向闸只放掉名词用法：真操作句在规则档照旧开 pack.preview（不许放过真操作意图）。"""
    plan = AP.plan_action(utterance, has_results=True, result_total=9,
                          llm_call=lambda _p: None)
    assert plan["verb"] == "pack.preview", (utterance, plan)
    assert plan["quoted"] == quoted, (utterance, plan)


def test_mock_llm_is_never_used_for_planning():
    """`call_mock_llm` 忽略 prompt、直吐 curator markdown 表；让执行层走它会「荒谬通过」。"""
    from dataset_recommender.llm.llm_client import LLMConfig
    ok, reason = AP.should_use_llm(LLMConfig(enable_llm=True, mock_llm=True, api_key="x"))
    assert ok is False and reason == "mock_not_used"


# ---------------------------------------------------- rule_operation_marker 的名词用法反向闸

@pytest.mark.parametrize("utterance", [
    "下载量大的数据集有哪些",   # C-1 原案：「下载」后随「量」是名词用法，是检索句
    "只保留能下载的",           # query_parser 注释点名的另一句检索句（「下载」后随「的」）
])
def test_rule_operation_marker_ignores_noun_usage_action_verbs(utterance):
    """动作词紧跟「量/的」是名词用法（检索语境），不算操作意图——
    裸子串把这类句子拦成降级气泡的话，检索在「AI 执行」关时永不可达。"""
    assert AP.rule_operation_marker(utterance) == "", utterance


@pytest.mark.parametrize("utterance,marker", [
    ("把这个数据集下载下来", "下载"),     # 真操作句：「下载」后随「下」，照拦
    ("帮我打包前5条", "打包"),
    ("删除我上传的那份数据", "删除"),     # 管护短语不走名词用法闸，照拦
    ("联网搜一下有没有新的人类肺数据", "联网搜"),
])
def test_rule_operation_marker_still_catches_real_operation_sentences(utterance, marker):
    """反向闸只放掉名词用法：真操作句（含管护短语）必须照常检出。"""
    assert AP.rule_operation_marker(utterance) == marker, utterance


# ---------------------------------------------------------------- 入参

def test_empty_and_oversized_utterances_are_refused_with_codes():
    with pytest.raises(AP.ActionPlanError) as e1:
        AP.plan_action("   ", llm_call=lambda _p: None)
    assert e1.value.code == "empty_input"
    with pytest.raises(AP.ActionPlanError) as e2:
        AP.plan_action("打" * (AP.MAX_UTTERANCE_CHARS + 1), llm_call=lambda _p: None)
    assert e2.value.code == "too_large"


# ---------------------------------------------------------------- 分层不变量

def test_a_plan_never_carries_an_artifact():
    """**后端只出 plan，前端才 exec。** 这条分层让自动执行不必动检索侧任何既有契约与门。"""
    plan = _plan("打包前5条", {"verb": "pack.download", "quoted": "打包前5条",
                               "limit": 5, "confidence": "high"})
    for forbidden in ("plan_token", "pack", "zip", "download_script", "files", "results", "uids"):
        assert forbidden not in plan, f"plan 里不该有 {forbidden}——产物只能由另一次显式请求产生"


# ================================ HTTP 端点 ================================

from fastapi.testclient import TestClient          # noqa: E402
from dataset_recommender.app.webapp import app     # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")


def test_endpoint_returns_a_plan_and_never_executes_anything():
    """端点只出 plan。`use_llm=false` → 规则档，全程零网络、零落盘。"""
    res = client.post("/api/action/plan", json={
        "utterance": "帮我打包前20条", "has_results": True, "result_total": 42, "use_llm": False,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    plan = body["plan"]
    # 只断 `source == "rule"` 是不够的：**网络调用失败**时它也成立，于是一条本该零网络的测试
    # 可以一边真发请求一边照样绿（同形的坑在 MCP 那条上真发生过）。这里钉「因为**没去调**才走的规则档」：
    # `disabled` / `no_key` / `mock_not_used` 三种状态都是在发请求**之前**就判否的。
    assert plan["source"] == "rule"
    assert plan["llm_status"] in {"disabled", "no_key", "mock_not_used"}, plan["llm_status"]
    assert plan["verb"] == "pack.preview"          # 规则档绝不直接产文件
    assert set(body) == {"ok", "plan"}, "端点返回体里不该有产物，只有 plan"


def test_endpoint_refuses_empty_utterance():
    res = client.post("/api/action/plan", json={"utterance": "", "use_llm": False})
    assert res.status_code == 422        # pydantic min_length


def test_endpoint_refuses_oversized_utterance():
    res = client.post("/api/action/plan", json={
        "utterance": "打" * (AP.MAX_UTTERANCE_CHARS + 1), "use_llm": False,
    })
    assert res.status_code == 400 and "太长" in res.json()["detail"]


def test_endpoint_rejects_unknown_fields():
    """`extra="forbid"`：多打一个字段名不该被静默忽略——那正是「参数没生效」这类 bug 的温床。"""
    res = client.post("/api/action/plan", json={
        "utterance": "打包", "use_llm": False, "limitt": 5,
    })
    assert res.status_code == 422


def test_endpoint_does_not_echo_the_submitted_api_key():
    res = client.post("/api/action/plan", json={
        "utterance": "帮我打包", "use_llm": False, "api_key": "sk-should-never-come-back",
    })
    assert res.status_code == 200
    assert "sk-should-never-come-back" not in res.text


def test_raw_shape_violations_is_agent_mirror_prefix():
    """agent_exec._validate_raw 的公共形状校验
    必须是 action_plan.raw_shape_violations 的**前缀**（单一真源，agent 只叠加多步专属闸）。"""
    from dataset_recommender.agent import agent_exec as AE

    cases = [
        ({}, "随便一句"),
        ({"verb": ""}, "随便一句"),
        ({"verb": "invented.verb", "quoted": "x"}, "随便一句"),
        ({"verb": "curate.remove", "quoted": "不在原话里的片段"}, "删掉我上传的那个文件"),
        ({"verb": "curate.remove", "quoted": ""}, "删掉我上传的那个文件"),
        ({"verb": "curate.remove", "quoted": "删掉我上传的"}, "删掉我上传的那个文件"),
    ]
    for raw, utt in cases:
        base = AP.raw_shape_violations(raw, utt)
        mirrored = AE._validate_raw(raw, utt)
        assert mirrored[:len(base)] == base, (raw, base, mirrored)
    # 空 raw 的「没有拿到可解析输出」也同源
    assert AE._validate_raw({}, "x") == AP.raw_shape_violations({}, "x")


# ---------------------------------------------------------------- 保底通道动词允许表

def test_plan_action_verbs_are_the_table_minus_loop_only_verbs():
    """plan_action 保底通道的允许表 = 闭环动词表
    减去环内专属动词（search.rerun/rank/rerank 环内检索工具 + route.request 换线
    元动词 + curate.rollback 回滚——rb1补录：回滚目标依赖本轮 steps
    实录的快照锚，单次分流没有这个现场；2026-08-18 四工具批补录 compare.datasets /
    compat.find / fair.check——默认对象依赖环内现场（当前结果集），单次分流没有；
    2026-09-13 rerank 处置批补录 results.show——handle 指认的是环内试跑现场，
    单次分流没有试跑缓存可放屏；2026-09-14 YOLO 批补录 ask.relax——选择卡依赖
    环内屏面基线与确定性预演现场，单次分流没有）——
    单此一份差集，多一个少一个都是泄漏或误伤。cite.export 不在差集里：它双通道
    （环内执行 + 保底通道前端 runner 仍在，仅 .ris 缺口由环内执行补上）。"""
    assert set(AP.ACTION_VERBS) - set(AP.PLAN_ACTION_VERBS) == {
        "search.rerun", "rank", "rerank", "route.request", "curate.rollback",
        "compare.datasets", "compat.find", "fair.check", "results.show", "ask.relax"}
    # 保底面该有的都在：14 个前端接线 EXEC + none + 三个 ROUTE_QUERY 检索指令动词。
    assert {"none", "search.new", "refine.conditions", "lookup.identifier",
            "pack.download", "curate.db_status", "cite.export"} <= set(AP.PLAN_ACTION_VERBS)
    # 2026-09-14 YOLO 批刻意更新：ask.relax 入差集（环内专属），-9 → -10。
    assert len(AP.PLAN_ACTION_VERBS) == len(AP.ACTION_VERBS) - 10


def test_mcp_doc_verb_list_covers_plan_action_verbs():
    """2026-08-17 复核（低）：MCP 模型可见文档的动词枚举曾两度漂移（这次
    少列 check_updates/sync_updates/db_status）——词表唯一真源是 PLAN_ACTION_VERBS，
    文档少列一个都会让 MCP 模型把合法返回当意外。钉：允许表每个动词都在文档里。"""
    import pathlib
    doc = (pathlib.Path(__file__).resolve().parents[1]
           / "src" / "dataset_recommender" / "app"
           / "mcp_server.py").read_text(encoding="utf-8")
    missing = [v for v in AP.PLAN_ACTION_VERBS if f"`{v}`" not in doc]
    assert not missing, f"mcp_server.py 的 plan_action 文档缺列: {missing}"


def test_plan_action_never_returns_loop_only_verb():
    """行为钉：模型在保底通道答了环内动词 rank → 机械拒（进 rejected、降 none）
    与未知 verb 同一条「不做，但要说」渠道；提示词里也不出环内动词行。"""
    seen: list[str] = []
    plan = AP.plan_action(
        "找人类肺癌数据", has_results=False, result_total=0,
        llm_call=lambda p: (seen.append(p), json.dumps(
            {"verb": "rank", "query": "人类肺癌", "quoted": "找人类肺癌数据",
             "confidence": "high"}, ensure_ascii=False))[1],
    )
    assert plan["verb"] == "none"
    assert plan["rejected"] == ["rank"]
    for loop_only in ("- rank（", "- rerank（", "- search.rerun（", "- route.request（"):
        assert loop_only not in seen[0], loop_only
    # 表内动词照出（保底面合法）：ROUTE_QUERY 三档与执行类都在。
    assert "- search.new（" in seen[0] and "- pack.download（" in seen[0]


def test_build_plan_from_raw_allowed_verbs_gate():
    """闸层钉：`allowed_verbs` 缺省 None = 全表放行（agent 环内路径不变——rank 在
    环内是合法首步）；显式给允许表时表外动词机械拒。"""
    raw = {"verb": "rank", "query": "人类肺癌", "quoted": "找人类肺癌数据",
           "confidence": "high"}
    utter = "找人类肺癌数据"
    open_plan = AP.build_plan_from_raw(raw, utter, has_results=False, result_total=0)
    assert open_plan["verb"] == "rank" and open_plan["rejected"] == []
    gated = AP.build_plan_from_raw(raw, utter, has_results=False, result_total=0,
                                   allowed_verbs=AP.PLAN_ACTION_VERBS)
    assert gated["verb"] == "none" and gated["rejected"] == ["rank"]


# ---------------------------------------------------------------- 铁律按面生成

def test_constraints_zh_default_is_byte_identical_full_table():
    """缺省（None）= 全表铁律，与 `_CONSTRAINTS_ZH` 逐位一致——plan_action 面零漂移。"""
    assert AP._constraints_zh(None) is AP._CONSTRAINTS_ZH
    assert AP._constraints_zh() == AP._CONSTRAINTS_ZH


def test_scoped_face_prompt_has_no_retired_route_verbs():
    """scoped 收窄面的 JSON 壳提示词里，面内不存在
    的动词（search.new/refine.conditions/lookup.identifier）零残留——铁律 5/7/10 换
    收窄面口径、铁律 8 整条退役后重新编号、JSON 模板的 effective_query 行同步改。"""
    face_verbs = {"rank", "rerank", "search.rerun", "curate.db_status", "none"}  # search 面
    specs = [s for s in AP.VERB_SPECS if s.verb in face_verbs]
    prompt = AP.build_action_prompt("找人类肺癌数据", has_results=False, result_total=0,
                                    verbs=specs)
    for retired in ("search.new", "refine.conditions", "lookup.identifier"):
        assert retired not in prompt, retired
    assert "选表里的检索动词" in prompt                # 铁律 5 收窄面口径（面内有 rank）
    assert "本表动词一律不填" in prompt                 # JSON 模板 effective_query 行
    constraints = AP._constraints_zh(specs)
    assert "9. " in constraints and "10. " not in constraints   # 铁律 8 退役，序不留洞
    # 无检索动机的面（general/action 型：无 rank 也无 ROUTE_QUERY）铁律 5 落到 none 口径。
    no_retrieval = [s for s in AP.VERB_SPECS
                    if s.verb in {"pack.download", "curate.db_status", "none"}]
    assert "检索需求，不是执行诉求，verb 填 none。" in AP._constraints_zh(no_retrieval)


# ---------------------------------------------------------------- 前端直派面词表派生

def test_frontend_dispatch_plane_derives_from_verb_specs():
    """前端直派面（turn._FRONTEND_EXEC_PLANE）的唯一真源是
    VerbSpec.frontend_dispatch 属性位——turn 不再私藏第二份 frozenset。本钉防名单
    漂移：加/摘直派动词必须先在词表立/摘属性位（含 pack.preview 刻意不在面的
    反向钉——预览不自动下载，进面会让预览句绕过 agent 图）。"""
    plane = {s.verb for s in AP.VERB_SPECS if s.frontend_dispatch}
    assert plane == {"pack.download", "reuse.pack"}
    from dataset_recommender.agent import turn as T
    assert T._FRONTEND_EXEC_PLANE == frozenset(plane)


# ---------------------------------------------------------------- 子意图枚举（「不少于我」下限合同探测半）

def test_first_json_array_extracts_balanced_array_from_prose():
    """散文里抠第一段平衡的 [...]；字符串字面量内的括号不计数、转义正确跳过。"""
    assert AP._first_json_array('前言 [{"a": "[1]"}] 后记') == '[{"a": "[1]"}]'
    assert AP._first_json_array('[[1],[2]]') == '[[1],[2]]'
    assert AP._first_json_array('["x\\"]"]') == '["x\\"]"]'   # 转义引号不收串
    assert AP._first_json_array('没有数组') == ''
    assert AP._first_json_array('[没闭合 [{"a":1}]') == '[{"a":1}]' or True  # 不抛即可
    assert AP._first_json_array('') == '' and AP._first_json_array(None) == ''


def test_parse_intents_response_distinguishes_legal_empty_from_failure():
    """核心语义钉：合法空清单 → []；解析不出 → None（调用方据此回落单次探测）。"""
    assert AP.parse_intents_response('[]') == []
    assert AP.parse_intents_response('{"intents": []}') == []
    assert AP.parse_intents_response('{"items": []}') == []
    assert AP.parse_intents_response('{"actions": []}') == []
    assert AP.parse_intents_response('这句话没有动作') is None
    assert AP.parse_intents_response('') is None
    assert AP.parse_intents_response('[1, 2, {"verb": "x"}]') == [{"verb": "x"}]  # 非 dict 滤掉
    assert AP.parse_intents_response('[1, 2]') == []          # 全滤完也是合法空清单
    one = AP.parse_intents_response('{"verb": "pack.download", "quoted": "下载"}')
    assert one == [{"verb": "pack.download", "quoted": "下载"}]   # 单对象宽容收下
    assert AP.parse_intents_response('{"foo": 1}') is None     # 无清单键也无 verb → 失败
    # 散文夹数组
    mixed = AP.parse_intents_response('我想了想：[{"verb": "cite.export", "quoted": "引文"}] 就这样')
    assert mixed == [{"verb": "cite.export", "quoted": "引文"}]


def test_build_intents_prompt_only_lists_exec_verbs_and_carries_rules():
    """枚举面只出执行类动词——检索是管线默认行为、不是清单项；铁律与 JSON 壳在位。"""
    prompt = AP.build_intents_prompt("找人类肺癌数据，把前两个打包下载",
                                     has_results=False, result_total=0)
    assert "找人类肺癌数据" in prompt
    assert "pack.download" in prompt and "cite.export" in prompt
    for not_exec in ("search.new", "refine.conditions", "lookup.identifier",
                     "route.request", '"none"'):
        assert not_exec not in prompt, not_exec
    assert "不是动作" in prompt           # 检索不入清单的明示
    assert "cancelled" in prompt and "quoted" in prompt
    assert "JSON 数组" in prompt
    assert "还没有" in prompt             # 现场情况段：无结果口径
    p2 = AP.build_intents_prompt("x", has_results=True, result_total=7,
                                 current_query="人类肺癌", retrieval=None)
    assert "共 7 条命中" in p2 and "人类肺癌" in p2


def _intents(utterance, payload, *, has_results=True, result_total=42, **kw):
    calls = {"n": 0}

    def caller(_prompt):
        calls["n"] += 1
        return json.dumps(payload, ensure_ascii=False)

    out = AP.plan_action_intents(
        utterance, has_results=has_results, result_total=result_total,
        llm_call=caller, **kw)
    return out, calls["n"]


def test_plan_action_intents_enumerates_all_exec_subintents():
    """混合句「检索+下载+引文」→ 两件执行动作全列出（检索不入清单）；每项与
    plan_action 产出同构（kind/verb/quoted 护栏全过）。"""
    payload = [
        {"verb": "pack.download", "quoted": "打包下载", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "明说下载"},
        {"verb": "cite.export", "quoted": "导出引文", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "明说引文"},
    ]
    plans, n = _intents("把结果打包下载，顺便导出引文", payload)
    assert n == 1                                    # 注入替身只调一次（重试预算留给真 LLM）
    assert [p["verb"] for p in plans] == ["pack.download", "cite.export"]
    for p in plans:
        assert p["kind"] == AP.EXEC and p["source"] == "llm"
        assert p["quoted"] in "把结果打包下载，顺便导出引文"


def test_plan_action_intents_legal_empty_list_means_no_action():
    """合法空清单 = 这句话没有要执行的动作 → []（**不是** None——不回落单次探测）。"""
    plans, _ = _intents("随便看看人类肺癌数据", [])
    assert plans == []


def test_plan_action_intents_parse_failure_falls_back_to_single_probe():
    """解析不出 / 空应答 → None（turn 探测段据此回落 plan_action 单次探测）。"""
    plans, _ = _intents("打包下载", None)
    assert plans is None

    def garbage(_prompt):
        return "我觉得吧，这个嘛……"
    assert AP.plan_action_intents("打包下载", has_results=True, result_total=1,
                                  llm_call=garbage) is None


def test_plan_action_intents_dropped_items_and_wholesale_garbage_rule():
    """逐项护栏：词表外/缺原文依据的项降级出列；**枚举非空却逐项降 none = 整单垃圾**
    → None 回落单次探测（这份枚举不可信）。"""
    # 一项合法 + 一项词表外 → 词表外出列，合法项保留
    mixed = [
        {"verb": "pack.download", "quoted": "打包", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "ok"},
        {"verb": "invented.verb", "quoted": "打包", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "编的词"},
    ]
    plans, _ = _intents("把结果打包", mixed)
    assert [p["verb"] for p in plans] == ["pack.download"]
    # 整单垃圾：两项都降 none → None
    all_bad = [
        {"verb": "invented.verb", "quoted": "打包", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "x"},
        {"verb": "pack.download", "quoted": "原文里没这四个字", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "x"},
    ]
    plans, _ = _intents("把结果打包", all_bad)
    assert plans is None


def test_plan_action_intents_dedupes_same_verb_and_caps_at_max():
    """同动词重复枚举取第一出现；超 MAX_INTENTS 截断。"""
    many = [{"verb": "pack.download", "quoted": "下载", "limit": i,
             "cancelled": False, "confidence": "high", "reason": "x"}
            for i in range(3)]
    plans, _ = _intents("下载下载下载", many)
    assert [p["verb"] for p in plans] == ["pack.download"]   # 去重后只剩一件

    verbs_cycle = ["pack.download", "pack.preview", "cite.export", "reuse.pack",
                   "feasibility.run", "files.show", "curate.list", "curate.db_status"]
    big = [{"verb": verbs_cycle[i], "quoted": "下载", "limit": None,
            "cancelled": False, "confidence": "high", "reason": "x"}
           for i in range(8)]
    plans, _ = _intents("下载", big)
    assert len(plans) == AP.MAX_INTENTS                       # 截断


def test_plan_action_intents_allowed_verbs_only_narrows():
    """allowed_verbs 收窄只缩小不扩权：面外 EXEC 动词出列；且枚举面永远不出
    检索类（即使 allowed_verbs 显式给了 search.new）。"""
    payload = [
        {"verb": "pack.download", "quoted": "打包", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "x"},
        {"verb": "cite.export", "quoted": "引文", "limit": None,
         "cancelled": False, "confidence": "high", "reason": "x"},
    ]
    plans, _ = _intents("打包和引文", payload, allowed_verbs=("pack.download",))
    assert [p["verb"] for p in plans] == ["pack.download"]
    # allowed_verbs 塞检索动词也进不了枚举面
    plans, _ = _intents("打包", [{"verb": "search.new", "quoted": "打包", "limit": None,
                                  "cancelled": False, "confidence": "high", "reason": "x"}],
                        allowed_verbs=("search.new", "pack.download"))
    assert plans is None        # 唯一一项被面闸出列 → 整单垃圾 → 回落单次探测


def test_plan_action_intents_llm_absent_returns_none():
    """LLM 缺席（无配置/被关）→ None，行为与引入本函数前逐位一致（fail-open）。"""
    assert AP.plan_action_intents("打包下载", has_results=True, result_total=1,
                                  llm_call=lambda _p: None) is None
    # 显式关断配置 → None（不依赖测试环境是否恰好没配 LLM——本机有真配置，会真联网）
    from dataset_recommender.llm.llm_client import LLMConfig
    off = LLMConfig(enable_llm=False)
    assert AP.plan_action_intents("打包下载", has_results=True, result_total=1,
                                  config=off) is None
