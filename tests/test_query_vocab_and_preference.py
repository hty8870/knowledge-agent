# -*- coding: utf-8 -*-
"""查询解析：词表/词边界修复 +「优先」软偏好 专项。

起因是一句真实查询：`推荐有 FASTQ 的人类乳腺癌数据，来自10x` 整句弃权，
屏幕上只说「查询里有系统未收录的词：来自」。两处根因：

1. `来自` 是**介词**，却被残差门当成未识别的实义词 → 整句 fail-closed。
2. 裸 `10x` 不在 `SOURCE_ALIASES` 里 → 来源识别恒空。而且这一半不是弃权、是**静默不筛**：
   `10x` 在残差门的 `[a-z]{2,}` 下取不出 2 字母词，于是既没被当来源、也没触发弃权。

顺带查出第三处同类根因（更隐蔽）：alias 此前是**裸子串**匹配，
`integrated` / `generated` / `celebrated` 里都含 `rat`（大鼠），
于是这些普通英文词会**悄悄多加一个 species 约束**。

本文件用「一句话进、状态出」的真行为断言把这三类钉住。
断言的是**语义**（这句话该不该被理解），不是某个内部函数的返回值——
换实现不该让这些测试变红，除非用户看到的行为真的变了。
"""
import pytest

from dataset_recommender.retrieval import query_lexicon as V
from dataset_recommender.retrieval.query_parser import (
    _alias_occurrences,
    active_filters,
    parse_query,
)
from dataset_recommender.retrieval.retriever import PREFERENCE_BOOST
from dataset_recommender.retrieval.search_request import resolve_search_request

KNOWN_SOURCES = [
    "10x Genomics", "CELLxGENE Discover", "Human Cell Atlas",
    "ArrayExpress", "EBI Single Cell Expression Atlas",
]


def _resolve_and_parse(query: str, auto: bool = True):
    r = resolve_search_request(query, None, KNOWN_SOURCES, auto_parse_sources=auto)
    intent = parse_query(r.parsed_query)
    intent.preferred_sources = list(r.preferred_sources)
    return r, intent


# ---------------------------------------------------------------------------
# 1. 原始故障：整句可执行，且三个条件一个不少
# ---------------------------------------------------------------------------
def test_the_original_failing_query_now_parses():
    r, intent = _resolve_and_parse("推荐有 FASTQ 的人类乳腺癌数据，来自10x")
    assert intent.parse_status == "executable", intent.abstain_detail
    assert r.sources == ["10x Genomics"]                 # 来源被认出来了
    assert intent.constraints["species"] == ["human"]
    assert "breast cancer" in intent.constraints["disease"]
    assert intent.has_raw_data_required is True          # FASTQ 仍是硬要求


# ---------------------------------------------------------------------------
# 2. 出处类介词：整类不再弃权（不是只修「来自」一个词）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", [
    "来自10x的人类乳腺癌数据",
    "出自10x的肺数据",
    "源自10x的数据",
    "来源于10x的数据",
    "产自10x的数据",
    "取自CELLxGENE的肺数据",
    "选自ArrayExpress的数据",
    "10x官方的乳腺癌数据",
    "10x发布的乳腺癌数据",
    "人类细胞图谱收录的肺数据",
    "HCA里的小鼠脑数据",
    "human lung data from cellxgene",
])
def test_provenance_prepositions_do_not_abstain(query):
    _r, intent = _resolve_and_parse(query)
    assert intent.parse_status == "executable", f"{query} → {intent.abstain_reason}"


@pytest.mark.parametrize("query", [
    "针对乳腺癌的单细胞数据",
    "面向小鼠脑的数据",
    "基于10x平台的人类数据",
    "根据人类乳腺癌找数据",
    "按照人类肺筛选",
    "涉及人类肺的数据",
    "通过10x得到的人类数据",
    "跑出来的人类肺数据",
    "生成的人类肺数据",
    "产出的小鼠脑数据",
    "一批人类肺数据",
    "一套小鼠脑数据",
    "大概找些人类肺数据",
    "尽可能多的人类肺数据",
    "越多越好的人类肺数据",
    "human lung datasets generated using 10x",
    "datasets based on visium",
])
def test_function_words_do_not_abstain(query):
    _r, intent = _resolve_and_parse(query)
    assert intent.parse_status == "executable", f"{query} → {intent.abstain_reason}"


# ---------------------------------------------------------------------------
# 3. 实义描述词：不弃权，但**必须回显**（不弃权 ≠ 可以静默吞掉）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,dropped", [
    ("转移性乳腺癌数据", "转移性"),
    ("早期肺癌数据", "早期"),
    ("冷冻小鼠脑数据", "冷冻"),
    ("FFPE人类乳腺癌数据", "ffpe"),
    ("小鼠脑的神经元数据", "神经元"),
    ("对照组人类肺数据", "对照组"),
])
def test_domain_words_are_reported_not_swallowed(query, dropped):
    _r, intent = _resolve_and_parse(query)
    assert intent.parse_status == "executable", f"{query} → {intent.abstain_reason}"
    assert dropped in intent.unused_query_terms, (
        f"{query} 里的「{dropped}」既没落维、又没回显 —— 那就是静默丢词"
    )


def test_domain_words_still_keep_the_real_constraint():
    """回显不能是「把整句降级成模糊搜索」的遮羞布：该落的维度还得落。"""
    _r, intent = _resolve_and_parse("转移性乳腺癌数据")
    assert "breast cancer" in intent.constraints.get("disease", [])


# ---------------------------------------------------------------------------
# 4. 词边界：英文单词内部的偶然包含不再变成约束
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query", ["generated data", "integrated data", "celebrated data"])
def test_alias_inside_an_english_word_is_not_a_constraint(query):
    """`rat`（大鼠）在 generated/integrated/celebrated 里都出现过。

    这类失败最坏的地方是**没有任何信号**：不弃权、不报错，只是结果里悄悄少了一批数据。
    """
    _r, intent = _resolve_and_parse(query)
    assert "species" not in intent.constraints or not intent.constraints["species"]


def test_word_boundary_still_matches_the_real_word_and_its_plural():
    assert _alias_occurrences("rat brain", "rat") == [(0, 3)]
    assert _alias_occurrences("rats brain", "rat") == [(0, 4)]      # 复数尾巴一起吃掉
    assert _alias_occurrences("generated", "rat") == []
    assert _alias_occurrences("accelerate", "rat") == []
    # 中文不分词，两端都不判边界（否则「人类肺」里的「肺」会被判成词内包含）
    assert _alias_occurrences("人类肺组织", "肺") == [(2, 3)]


def test_prefixed_technique_names_are_explicit_aliases_now():
    """scATAC 过去靠「裸子串命中 atac」偶然生效。改词边界后必须显式登记，
    否则会从「偶然能用」直接变成「悄悄不认识」。"""
    _r, intent = _resolve_and_parse("scATAC的人类数据")
    assert intent.constraints.get("assay") == ["atac"]


# ---------------------------------------------------------------------------
# 5. 「优先」软偏好：只排序、绝不筛选
# ---------------------------------------------------------------------------
def test_preference_does_not_become_a_hard_filter():
    """这是整个特性的核心不变量。若 Visium 落进 constraints，
    用户说「优先 Visium」就再也看不到 Xenium 的数据——那不是偏好，是筛选。"""
    _r, intent = _resolve_and_parse("人类肺数据，优先Visium")
    assert intent.preferred_constraints == {"platform": ["visium"]}
    assert "platform" not in intent.constraints
    assert intent.constraints["species"] == ["human"]
    assert intent.constraints["tissue"] == ["lung"]


def test_preference_can_span_multiple_dimensions():
    """跨维度对偏好没有歧义（A 加权 + B 加权），不需要像否定那样弃权。"""
    _r, intent = _resolve_and_parse("优先人类肺")
    assert intent.preferred_constraints == {"species": ["human"], "tissue": ["lung"]}
    assert not any(intent.constraints.values())


def test_preference_on_raw_data_does_not_require_it():
    _r, intent = _resolve_and_parse("人类肺数据 优先有FASTQ")
    assert intent.preferred_raw is True
    assert intent.has_raw_data_required is None, "「优先有 FASTQ」不能把没有 FASTQ 的数据筛掉"


def test_preference_on_a_year_does_not_filter_by_year():
    _r, intent = _resolve_and_parse("人类数据，优先2024年")
    assert intent.preferred_date_from == "2024-01-01"
    assert intent.preferred_date_to == "2024-12-31"
    assert intent.date_from == "" and intent.date_to == "", "「优先 2024」不能把别的年份筛掉"


def test_preference_on_a_source_does_not_narrow_the_source_pool():
    r, intent = _resolve_and_parse("人类乳腺癌数据，优先10x")
    assert r.preferred_sources == ["10x Genomics"]
    assert r.sources is None, "「优先 10x」不能把检索池缩到只剩 10x"
    assert intent.parse_status == "executable"


def test_naming_a_source_without_prefer_still_narrows():
    """对照组：不写「优先」时，点名来源仍然是收窄（这是既有行为，不能被偏好特性带跑偏）。"""
    r, _intent = _resolve_and_parse("人类乳腺癌数据，来自10x")
    assert r.sources == ["10x Genomics"]
    assert r.preferred_sources == []


def test_preference_marker_without_a_known_target_does_not_kill_the_query():
    """软偏好丢了只是没加权、结果集一模一样，为它把整句弃权是过度 fail-closed。
    但也不能静默吞——必须回显。"""
    _r, intent = _resolve_and_parse("人类肺数据，优先")
    assert intent.parse_status == "executable"
    assert intent.constraints["tissue"] == ["lung"]
    assert "优先" in intent.unused_query_terms


def test_an_unknown_word_after_the_marker_still_abstains():
    """对照组：弃权与否取决于**那个词**认不认识，而不是取决于它前面有没有「优先」。
    「优先」放行的是标记词本身，不是它后面任意内容的免检通行证。"""
    _r, intent = _resolve_and_parse("人类肺数据，优先甲乙丙平台")
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "unresolved_term"


def test_preference_conflicting_with_an_exclusion_abstains():
    """「不要小鼠，优先小鼠」：偏好命中的记录已被硬排除筛光，加权永远不会生效，
    界面上却会挂着一条「优先·物种：Mouse」——那是给一件不可能发生的事背书。"""
    intent = parse_query("不要小鼠，优先小鼠")
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "conflicting_polarity"


@pytest.mark.parametrize("query", ["优先不要小鼠", "优先不含Visium的人类数据", "优先没有FASTQ的数据"])
def test_soft_exclusion_abstains_instead_of_hard_excluding(query):
    """「优先不要 X」是软性排除，系统**表达不了**——真去执行就变成「一条都不要 X」。
    悄悄按硬排除办，正是这个特性从头到尾要防的那件事（把偏好偷换成筛选）。做不到就明说做不到。"""
    intent = parse_query(query)
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "unsupported_soft_exclusion"
    assert not any(intent.excluded_constraints.values()), "弃权了就不能还留着硬排除"


def test_a_hedge_is_no_longer_silenced_nor_used_to_kill_the_whole_query():
    """「最好优先 Visium」：两个标记词叠在一起，最终只留一份软偏好，不弃权也不静默丢词。

     之前这句弃权在 `unsupported_hedge`。旧注释的担心是「Visium 被『优先』消费掉后
    句子里没有硬约束了，hedge 会被静默忽略」——现在 hedge 本身**就是**软偏好，
    不存在「被忽略」这回事：两个标记词指向同一个值，落一份偏好即可。
    """
    intent = parse_query("最好优先Visium")
    assert intent.parse_status == "executable", intent.abstain_reason
    assert intent.preferred_constraints.get("platform") == ["visium"], intent.preferred_constraints
    assert not intent.constraints.get("platform"), "偏好绝不能变成硬筛选"


@pytest.mark.parametrize("query,dim,value", [
    ("最好是 Xenium 的黑色素瘤数据", "platform", "xenium"),
    ("尽量是 Visium 的人类肺数据", "platform", "visium"),
    ("如果可以用 Xenium 的黑色素瘤数据", "platform", "xenium"),
])
def test_hedges_are_executed_as_soft_preferences(query, dim, value):
    """hedge 与「优先」同解：只加权、不筛掉任何数据。

    这是 2026-07-25 基线变更的落点。变更前验证：
        「优先 Xenium 的黑色素瘤数据」→ 55 条
        「最好是 Xenium 的黑色素瘤数据」→ **0 条，且放宽选项 0、降级 0**
    两句除了标记词逐字相同。而冻结评测 adv07 自己写着 `nice_to_have: {technology: xenium}`
    ——评测数据本来就把「最好」建模成软偏好，弃权反倒与它自相矛盾。
    """
    intent = parse_query(query)
    assert intent.parse_status == "executable", (query, intent.abstain_reason)
    assert intent.preferred_constraints.get(dim) == [value], intent.preferred_constraints
    assert not intent.constraints.get(dim), "hedge 必须是软偏好，不能悄悄变成硬筛选"


def test_hedge_does_not_downgrade_a_hard_requirement_via_de():
    """「最好的人类肺数据」里「人类 / 肺」是**硬要求**，「最好的」只是形容词。

    实现细节：hedge 的虚字表刻意不含「的」（`V.HEDGE_CONNECTOR_CHARS`）。
    允许「的」会把这句读成「偏好人类」，把硬要求悄悄降级成加权——那是反向的静默偏离，
    比不支持这个词更糟。
    """
    intent = parse_query("最好的人类肺数据")
    assert intent.parse_status == "executable", intent.abstain_reason
    assert intent.constraints.get("species") == ["human"], intent.constraints
    assert intent.constraints.get("tissue") == ["lung"], intent.constraints
    assert not intent.preferred_constraints, intent.preferred_constraints


def test_soft_exclusion_stays_fail_closed_even_for_hedges():
    """唯一保留 fail-closed 的一档：「尽量不要 X」。

    系统表达不了软性排除——真去执行就成了「一条都不要 X」，把偏好偷换成筛选、
    **筛掉了用户其实想看的数据**。这与「hedge 落空只是没加权」不对称，故照旧明说做不到。
    """
    intent = parse_query("尽量不要小鼠的肺数据")
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "unsupported_soft_exclusion"
    assert not any(intent.excluded_constraints.values()), "弃权了就不能还留着硬排除"


def test_hedge_markers_are_a_partition_not_a_second_transcript():
    """两族标记词必须**程序并**成一张表，不许出现第三份手抄。"""
    assert set(V.PREFER_PREFIXES_ALL) == set(V.SOFT_PREFER_PREFIX_CN) | set(V.HEDGE_PREFER_PREFIX_CN)
    assert not (set(V.SOFT_PREFER_PREFIX_CN) & set(V.HEDGE_PREFER_PREFIX_CN))
    # 「的」只许出现在「优先」那一族的虚字表里（理由见上一条测试）。
    assert "的" in V.PREFER_CONNECTOR_CHARS and "的" not in V.HEDGE_CONNECTOR_CHARS
    # 来源侧那条正则也必须是从同一张表生成的，不是手抄。
    from dataset_recommender.retrieval.search_request import SOURCE_PREFER_PREFIX_RE
    for marker in V.PREFER_PREFIXES_ALL:
        assert SOURCE_PREFER_PREFIX_RE.search(marker), marker


# ---------------------------------------------------------------------------
# 6. 契约：偏好在「已命中」里必须**看得出来不是筛选条件**
# ---------------------------------------------------------------------------
def test_preference_shows_up_as_its_own_polarity():
    _r, intent = _resolve_and_parse("人类肺数据，优先Visium")
    by_id = {f["filter_id"]: f for f in active_filters(intent)}
    assert "prefer:platform" in by_id
    row = by_id["prefer:platform"]
    assert row["polarity"] == "prefer"
    assert row["label"].startswith("优先·"), "标签不带「优先」就会被读成硬条件"
    # 硬条件仍是 include，两者不能混成一种极性
    assert by_id["include:tissue"]["polarity"] == "include"


def test_every_preference_filter_id_is_suppressible():
    """前端只要看到 filter_id 就会渲染「停用」按钮。若后端白名单漏了它，
    用户一点、请求被 sanitize 丢掉、界面却显示已停用 —— 失败长得像成功。"""
    from dataset_recommender.app.workflow import SUPPRESSIBLE_FILTER_IDS, sanitize_suppressed

    for query in ["人类肺数据，优先Visium", "人类肺数据 优先有FASTQ", "人类数据，优先2024年"]:
        _r, intent = _resolve_and_parse(query)
        for f in active_filters(intent):
            if f["polarity"] == "prefer":
                assert f["filter_id"] in SUPPRESSIBLE_FILTER_IDS
                assert sanitize_suppressed([f["filter_id"]]) == [f["filter_id"]]


def test_suppressing_a_preference_turns_off_only_the_ranking_boost():
    """`prefer:raw` 的 dim 段和硬约束 `raw:*` 重名。落错分支就会出现
    「点的是别按这条排序、结果把必须有 FASTQ 也去掉了」。"""
    from dataset_recommender.app.workflow import apply_suppressed_constraints

    _r, intent = _resolve_and_parse("有FASTQ的人类肺数据 优先有FASTQ")
    assert intent.has_raw_data_required is True and intent.preferred_raw is True
    apply_suppressed_constraints(intent, ["prefer:raw"])
    assert intent.preferred_raw is None
    assert intent.has_raw_data_required is True, "停用排序偏好不该动硬条件"


# ---------------------------------------------------------------------------
# 7. 打分：偏好压得过全部 tie-breaker，但压不过词面相关性
# ---------------------------------------------------------------------------
def test_preference_boost_sits_between_tiebreakers_and_relevance():
    """取值不是拍脑袋：小于 tie-breaker 之和 → 用户看不出「优先」有效果；
    大于两个自由词命中标题 → 偏好会盖过明显更贴题的结果，事实上变成硬过滤。"""
    max_tiebreakers = 0.15 * 4 + 0.4 + 0.5      # 完整度 + 新鲜度 + 样本量
    assert PREFERENCE_BOOST > max_tiebreakers
    assert PREFERENCE_BOOST <= 2 * 1.0          # 两个自由词命中标题


# ---------------------------------------------------------------------------
# 8. 验证确认的缺陷，逐条钉死
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("query,expect_pref", [
    ("优先转移性乳腺癌", {"disease"}),      # 「转移性」是 FILLER_DOMAIN 词，卡在标记词和实体中间
    ("优先冷冻的人类肺数据", {"species", "tissue"}),
    ("优先，人类肺数据", {"species", "tissue"}),
])
def test_a_gap_between_the_marker_and_the_entity_does_not_turn_it_into_a_filter(query, expect_pref):
    """标记词和实体之间隔个描述词或标点，原本会让「优先」被判成孤立词，
    紧跟其后的实体被正向解析吃成**硬约束**——用户说「优先 X」拿到「只要 X」。

    这是本轮两半改动的交互产物：新收的 69 个 FILLER_DOMAIN 词正好卡在中间。"""
    _r, intent = _resolve_and_parse(query)
    assert intent.parse_status == "executable", intent.abstain_detail
    assert set(intent.preferred_constraints) == expect_pref
    assert not any(intent.constraints.values()), "这几个词应该只加权，一个都不该变成筛选条件"


def test_an_unreachable_preference_scope_abstains_rather_than_guessing():
    """跨得太远就不是「紧邻」了，作用域本来就不清楚——猜错的后果是把偏好变成筛选，所以宁可弃权。"""
    _r, intent = _resolve_and_parse("优先。人类肺数据")
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "ambiguous_preference_scope"


def test_conflicting_raw_polarity_abstains():
    """冲突守卫原本只遍历六个结构化维度，raw 漏在外面：
    「不要 FASTQ + 优先有 FASTQ」两条并排挂着，而偏好对存活集里每一条都不成立。"""
    intent = parse_query("不要fastq的肺数据，优先有fastq")
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "conflicting_polarity"


@pytest.mark.parametrize("query", ["2020年的数据，优先2024年", "2019年之前的数据，优先2024年以后"])
def test_disjoint_date_ranges_abstain(query):
    """同理：要筛的时间区间与要优先的时间区间没有交集时，偏好那条恒不成立。
    顺带钉住另一半——第二个日期表达**不能被静默丢弃**（既不进偏好、也不进硬过滤、也不报）。"""
    intent = parse_query(query)
    assert intent.parse_status == "abstained"
    assert intent.abstain_reason == "conflicting_polarity"


def test_a_second_date_expression_is_not_silently_dropped():
    """`_date_spans` 是全句独占匹配，句中先有别的日期时偏好日期的 span 对不上，
    整段年份会凭空消失。就地再解析一次，保证它至少被认出来。"""
    intent = parse_query("人类数据，优先2024年")
    assert (intent.preferred_date_from, intent.preferred_date_to) == ("2024-01-01", "2024-12-31")
    assert intent.date_from == "" and intent.date_to == ""


def test_source_preference_is_recognised_in_manual_source_mode_too():
    """手动来源模式下原本在 `resolve_search_request` 第一行就早退，
    「优先<来源>」整段不被识别，专名随后被残差门无声吞掉——同一句话两种模式语义不同。"""
    auto = resolve_search_request("优先10x的人类数据", None, KNOWN_SOURCES, auto_parse_sources=True)
    manual = resolve_search_request("优先10x的人类数据", None, KNOWN_SOURCES, auto_parse_sources=False)
    assert auto.preferred_sources == manual.preferred_sources == ["10x Genomics"]
    assert auto.parsed_query == manual.parsed_query, "两种模式下送进解析器的句子必须一样"
    assert manual.sources is None, "识别偏好不等于替用户改检索范围"


def test_the_prefer_marker_is_cut_together_with_the_source_name():
    """条件板的来源自检要用 `source_alias_spans` 重建保护区。它只遮专名、留下「优先」两个字的话，
    两串永远对不上，改字类操作会 100% 被拒，理由还写成「来源对不上」——来源其实一个字都没变。"""
    from dataset_recommender.retrieval.search_request import mask_source_spans, source_alias_spans

    q = "人类肺数据，优先10x"
    masked = " ".join(mask_source_spans(q, source_alias_spans(q)).split())
    resolved = resolve_search_request(q, None, KNOWN_SOURCES, auto_parse_sources=True)
    assert masked == " ".join(resolved.parsed_query.split())
    assert "优先" not in masked


def test_no_preference_means_the_scoring_path_is_untouched():
    """默认空 = no-op。冻结 767 评测走的就是这条空分支，
    隔离靠「默认分支字面不变」，不靠事后比对指标。"""
    _r, intent = _resolve_and_parse("人类肺数据")
    assert intent.preferred_constraints == {}
    assert intent.preferred_raw is None
    assert intent.preferred_sources == []
    assert intent.preferred_date_from == "" and intent.preferred_date_to == ""
    assert all(f["polarity"] != "prefer" for f in active_filters(intent))


def test_or_inside_preference_segment_is_reported_honestly():
    """「优先人或小鼠的脑数据」的「或」落在软偏好段，
    实际执行是双值加权（preferred species=[human, mouse]），or_handling 此前却落
    narrower 档谎称「按同时满足执行、请分两次查」，与同屏两颗「优先·物种」chip 自相矛盾。
    现在必须如实说成「命中任一都加权」。"""
    intent = parse_query("优先人或小鼠的脑数据")
    assert intent.parse_status == "executable"
    assert [v.lower() for v in intent.preferred_constraints["species"]] == ["human", "mouse"]
    oh = intent.or_handling
    assert oh["fit"] == "exact"
    assert "加权" in oh["note_zh"]
    assert "请分两次查" not in oh["note_zh"]
    assert "同时满足" not in oh["note_zh"].split("另外")[0]


def test_or_handling_regressions_after_preference_fix():
    """反面钉：约束内的「或」与跨维度「或」的既有分档逐位不变。"""
    assert parse_query("人类或小鼠的脑数据").or_handling["fit"] == "exact"
    assert parse_query("人类肺癌或小鼠肝癌").or_handling["fit"] == "superset"
    narrower = parse_query("人类肺癌或 10x 的数据").or_handling
    assert narrower["fit"] == "narrower"
    assert "分两次查" in narrower["note_zh"]
    assert parse_query("人类的脑数据").or_handling == {}
