# -*- coding: utf-8 -*-
"""条件板规划器的行为测试。

重点不是「函数跑通了」，而是几条**只要坏了就会骗到用户**的性质：
换条件之后旧值必须真的消失（不能静默变成「或」）、筛选项编号必须取自真实返回、
来源专名必须逐字幸存、听不懂时必须一个字节都不改。
"""
import copy

import pytest

from dataset_recommender.app import board
from dataset_recommender.retrieval import search_request as SR
from dataset_recommender.retrieval.query_parser import (
    DIMENSIONS,
    active_filters,
    parse_query,
)
from dataset_recommender.app.workflow import (
    SUPPRESSIBLE_FILTER_IDS,
    apply_suppressed_constraints,
    sanitize_suppressed,
)

KNOWN_SOURCES = [
    "10x Genomics", "CELLxGENE Discover", "Human Cell Atlas",
    "EBI Single Cell Expression Atlas", "ArrayExpress",
]

Q_BASE = "找人类肺组织的单细胞数据"


def af(query, date_from="", date_to=""):
    """构造「上一次检索返回的条件」——这是条件板唯一的记忆来源。"""
    intent = parse_query(query)
    if date_from:
        intent.date_from = date_from
    if date_to:
        intent.date_to = date_to
    return active_filters(intent)


def species_of(query):
    return set(parse_query(query).constraints.get("species", []))


# ----------------------------------------------------------------- 主线四轮

def test_main_line_four_rounds():
    plan1 = board.plan_edit(Q_BASE, "再加一条：只要含 FASTQ 的", current_filters=af(Q_BASE))
    assert plan1["status"] == board.ST_CONFIRM and plan1["op"] == "add"
    assert plan1["dim"] == "has_raw_data"
    q1 = plan1["next_request"]["query"]
    assert parse_query(q1).has_raw_data_required is True

    plan2 = board.plan_edit(q1, "换成小鼠", current_filters=af(q1))
    assert plan2["status"] == board.ST_CONFIRM and plan2["op"] == "replace"
    q2 = plan2["next_request"]["query"]
    # 看门狗：换条件之后旧值必须**真的消失**。追加一个别名而不去掉旧的，
    # 在检索侧就是「满足其一即可」，用户全程收不到任何信号。
    assert species_of(q2) == {"mouse"}
    after = parse_query(q2)
    assert after.constraints.get("tissue") == parse_query(q1).constraints.get("tissue")
    assert after.has_raw_data_required is True

    plan3 = board.plan_edit(q2, "去掉组织限制", current_filters=af(q2))
    assert plan3["status"] == board.ST_AUTO and plan3["op"] == "remove"
    assert "include:tissue" in plan3["next_request"]["suppressed_constraints"]
    # 只动开关的编辑不许碰句子里的字。
    assert plan3["next_request"]["query"] == board._norm_ws(q2)


def test_replace_preview_names_both_sides():
    plan = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE))
    assert "Human" in plan["message"] and "Mouse" in plan["message"]
    assert plan["removed_text"] and plan["removed_text"][0]["text"] == "人类"
    # 单字别名被中和时，光看「被去掉的原文：人」用户判断不出发生了什么，必须带上下文。
    assert "人类" in plan["removed_text"][0]["context"]


# ----------------------------------------------------------------- 筛选项编号必须取自真实返回

@pytest.mark.parametrize("query,utterance,expect_fid", [
    ("找人类肺组织数据，不要肿瘤", "去掉疾病限制", "exclude:disease"),
    ("需要有 FASTQ 的人类乳腺数据", "去掉 FASTQ 限制", "raw:required"),
    ("找人类肺组织的单细胞数据", "去掉组织限制", "include:tissue"),
])
def test_remove_uses_real_filter_id(query, utterance, expect_fid):
    plan = board.plan_edit(query, utterance, current_filters=af(query))
    assert plan["status"] == board.ST_AUTO, plan["message"]
    assert expect_fid in plan["next_request"]["suppressed_constraints"]


def test_remove_date_range_from_year_dropdown():
    """时间条件来自网页上的年份下拉框，句子里一个时间词都没有——照样要能去掉。"""
    query = "人类心脏数据"
    plan = board.plan_edit(query, "去掉时间限制",
                           current_filters=af(query, date_from="2020-01-01"),
                           date_from="2020-01-01")
    assert plan["status"] == board.ST_AUTO
    assert "date:range" in plan["next_request"]["suppressed_constraints"]


def test_every_active_filter_id_is_really_suppressible():
    """板说「已经不按它筛了」与「真的没按它筛」必须是同一件事。

    遍历 active_filters 能产出的每一种编号，逐条证明它能被白名单保留、
    并且在忽略生效之后真的从条件清单里消失。这条断言把「说了」和「做了」钉在一起。
    """
    samples = {
        "include:species": "人类数据",
        "exclude:species": "人类数据，不要小鼠",
        "include:tissue": "肺组织数据",
        "include:disease": "肺癌数据",
        "include:platform": "Visium 数据",
        "include:assay": "ATAC 数据",
        "include:modality": "单细胞数据",
        "raw:required": "需要有 FASTQ 的数据",
    }
    for fid, query in samples.items():
        intent = parse_query(query)
        ids = {f["filter_id"] for f in active_filters(intent)}
        assert fid in ids, f"{query!r} 没有产出 {fid}"
        assert sanitize_suppressed([fid]) == [fid], f"{fid} 被白名单丢掉了"
        apply_suppressed_constraints(intent, [fid])
        assert fid not in {f["filter_id"] for f in active_filters(intent)}
    assert "date:range" in SUPPRESSIBLE_FILTER_IDS


# ----------------------------------------------------------------- 来源保护

def test_source_name_survives_every_applied_edit():
    query = "人类细胞图谱的肺组织数据"
    resolution = SR.resolve_search_request(query, None, KNOWN_SOURCES, auto_parse_sources=True).as_dict()
    current = af(resolution["parsed_query"])

    # 板上根本没有物种这一行（「人类细胞图谱」是来源专名，不是物种条件）→ 不许说要改物种。
    plan = board.plan_edit(query, "换成小鼠", current_filters=current, resolution=resolution)
    assert plan["status"] == board.ST_UNKNOWN
    assert plan["next_request"] is None

    # 能做的编辑做完之后，专名必须逐字幸存。
    plan = board.plan_edit(query, "再加一条：小鼠", current_filters=current, resolution=resolution)
    assert plan["status"] == board.ST_CONFIRM
    assert "人类细胞图谱" in plan["next_request"]["query"]


def test_source_scope_change_is_rejected():
    """用户在预览框里手改时把来源专名改坏了：必须被挡下，且明说原因。

    走的是真实路径——先有一步能成立的规划（才会出现可编辑的预览框），再在预览框里手改。
    """
    query = "人类细胞图谱的肺组织数据"
    resolution = SR.resolve_search_request(query, None, KNOWN_SOURCES, auto_parse_sources=True).as_dict()
    plan = board.plan_edit(query, "再加一条：小鼠", candidate_override="人类细胞的肺组织数据 小鼠",
                           current_filters=af(resolution["parsed_query"]), resolution=resolution)
    assert plan["status"] == board.ST_REJECTED
    assert plan["next_request"] is None
    assert any(m["code"] == "source_scope_changed" for m in plan["verify"]["mismatches"])


def test_mentioning_a_source_in_the_utterance_is_refused_not_guessed():
    plan = board.plan_edit(Q_BASE, "换成 cellxgene", current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_UNKNOWN
    assert "数据来源" in plan["message"]
    assert plan["next_request"] is None


def test_text_edits_fail_closed_when_source_selfcheck_disagrees():
    """遮蔽结果与服务端拿去解析的那句对不上 → 不碰句子里的字。"""
    plan = board.plan_edit("cellxgene 的肺组织数据", "换成小鼠",
                           current_filters=af("的肺组织数据"),
                           resolution={"parsed_query": "完全不一样的句子"})
    assert plan["status"] == board.ST_REJECTED
    assert plan["next_request"] is None


# ----------------------------------------------------------------- 冲突与放宽

def test_same_dim_conflict_stops_and_asks_with_three_named_choices():
    plan = board.plan_edit(Q_BASE, "再加一条：小鼠", current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_CHOICE
    assert plan["next_request"] is None
    assert len(plan["choices"]) == 3
    ids = [c["id"] for c in plan["choices"]]
    assert ids == ["replace", "widen", "restart"]
    for choice in plan["choices"]:
        # 中文里「两个都要」按字面就是「同时满足」，正好制造这个功能想消灭的那种误解。
        assert "都要" not in choice["label"]
        assert "同时满足" not in choice["label"]


def test_widen_is_reachable_and_produces_a_real_union():
    """冲突三选一里「都算」这个选项必须真的能点动——通用校验会把它误判成异常。"""
    plan = board.plan_edit(Q_BASE, "小鼠", forced_op="widen", dim="species", current_filters=af(Q_BASE))
    assert plan["status"] != board.ST_REJECTED, plan["verify"]
    assert plan["status"] == board.ST_CONFIRM
    assert species_of(plan["next_request"]["query"]) == {"human", "mouse"}


def test_restart_clears_every_knob_but_keeps_the_year_dropdown():
    plan = board.plan_edit(Q_BASE, "小鼠肾脏", forced_op="restart",
                           current_filters=af(Q_BASE),
                           suppressed_constraints=["include:tissue"],
                           lenient_dims=["disease"],
                           facet_filters=[{"dim": "species", "value": "homo sapiens"}],
                           date_from="2020-01-01", date_to="2023-12-31")
    assert plan["status"] == board.ST_CONFIRM and plan["op"] == "restart"
    request = plan["next_request"]
    assert request["suppressed_constraints"] == []
    assert request["lenient_dims"] == []
    assert request["facet_filters"] == []
    # 年份下拉框是独立控件，不归这句话管。
    assert request["date_from"] == "2020-01-01" and request["date_to"] == "2023-12-31"


def test_lenient_and_remove_are_different_promises():
    plan_lenient = board.plan_edit(Q_BASE, "放宽组织", current_filters=af(Q_BASE), coverage_dims=["tissue"])
    assert plan_lenient["op"] == "lenient"
    assert plan_lenient["next_request"]["lenient_dims"] == ["tissue"]
    assert plan_lenient["next_request"]["suppressed_constraints"] == []
    assert plan_lenient["next_request"]["query"] == board._norm_ws(Q_BASE)

    plan_remove = board.plan_edit(Q_BASE, "去掉组织限制", current_filters=af(Q_BASE))
    assert plan_remove["op"] == "remove"
    assert plan_remove["next_request"]["lenient_dims"] == []
    assert "include:tissue" in plan_remove["next_request"]["suppressed_constraints"]


def test_leading_head_does_not_break_recognition():
    """「把组织放宽」与「放宽组织」必须等价——只认句首会让前一种永远听不懂。"""
    with_head = board.plan_edit(Q_BASE, "把组织放宽", current_filters=af(Q_BASE), coverage_dims=["tissue"])
    without = board.plan_edit(Q_BASE, "放宽组织", current_filters=af(Q_BASE), coverage_dims=["tissue"])
    assert with_head["status"] == without["status"] == board.ST_AUTO
    assert with_head["next_request"] == without["next_request"]


def test_lenient_without_a_real_gap_says_so_instead_of_pretending():
    plan = board.plan_edit(Q_BASE, "放宽疾病", current_filters=af(Q_BASE), coverage_dims=[])
    assert plan["status"] == board.ST_UNKNOWN
    assert plan["next_request"] is None
    assert "不会多出结果" in plan["message"]


# ----------------------------------------------------------------- 否定

def test_negated_addition_never_flips_into_a_positive_filter():
    """把「不要小鼠」蒸馏成「小鼠」是最严重的静默反向，这条是它的看门狗。"""
    plan = board.plan_edit(Q_BASE, "再加一条：不要小鼠", current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_CONFIRM
    query = plan["next_request"]["query"]
    assert "不要" in query
    after = parse_query(query)
    assert "mouse" in after.excluded_constraints.get("species", [])
    assert "mouse" not in after.constraints.get("species", [])
    assert "排除" in plan["message"]


def test_negated_addition_is_not_treated_as_a_same_dim_conflict():
    """「物种＝人类」与「排除小鼠」并不冲突，不该弹三选一。"""
    plan = board.plan_edit(Q_BASE, "再加一条：不要小鼠", current_filters=af(Q_BASE))
    assert plan["status"] != board.ST_CHOICE


# ----------------------------------------------------------------- 手改整句

def test_candidate_override_actually_takes_effect():
    planned = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE))
    overridden = board.plan_edit(Q_BASE, "换成小鼠", candidate_override="找小鼠肾脏的单细胞数据",
                                 current_filters=af(Q_BASE))
    assert overridden["status"] == board.ST_CONFIRM
    assert overridden["next_request"]["query"] != planned["next_request"]["query"]
    after = parse_query(overridden["next_request"]["query"])
    assert after.constraints.get("tissue") == ["kidney"]
    # 手改会连带改别的条件是正常的——但必须**如实列出来**，不能默默执行。
    assert "组织" in overridden["detail"]


def test_candidate_override_that_cannot_be_executed_is_refused():
    """载荷刻意选「优先不要 X」这一族：它是**永久**fail-closed 的。

     之前这里用的是「人类或者小鼠的肺数据」——那句当时不可执行只是因为「或」被整句弃权，
    而「或」现在照做了（同维度多值本来就是「或」），载荷失效、这条门变成空转。
    换成软性排除：系统表达不了「尽量少要 X」，真去执行就成了「一条都不要 X」，
    这条 fail-closed 是 `query_parser` 里写明的红线，不会随哲学修正而变。
    """
    plan = board.plan_edit(Q_BASE, "换成小鼠", candidate_override="优先不要小鼠的肺数据",
                           current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_REJECTED
    assert plan["next_request"] is None


# ----------------------------------------------------------------- 听不懂的五种

@pytest.mark.parametrize("utterance,expect_in_message", [
    ("再加一条换成小鼠", "我没有听懂"),
    ("换成树鼩", "树鼩"),
    ("换成大鼠脑", "你是想改哪一个"),
    ("去掉平台限制", "没有按「平台」筛"),
    ("今天天气不错", "我没有听懂"),
])
def test_not_understood_paths_change_nothing(utterance, expect_in_message):
    current = af(Q_BASE)
    snapshot = copy.deepcopy(current)
    plan = board.plan_edit(Q_BASE, utterance, current_filters=current,
                           suppressed_constraints=["include:tissue"], lenient_dims=["disease"])
    assert plan["status"] in (board.ST_UNKNOWN, board.ST_REJECTED)
    assert plan["next_request"] is None
    assert expect_in_message in plan["message"] or expect_in_message in plan["detail"]
    # 入参一个字节都没被改动过。
    assert current == snapshot


def test_conflicting_operators_name_both_of_them():
    plan = board.plan_edit(Q_BASE, "再加一条换成小鼠", current_filters=af(Q_BASE))
    assert "再加" in plan["detail"] and "换成" in plan["detail"]


def test_unknown_term_offers_terms_we_actually_know():
    plan = board.plan_edit(Q_BASE, "换成树鼩", forced_op="replace", dim="species",
                           current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_UNKNOWN
    assert plan["suggestions"]
    for item in plan["suggestions"]:
        assert item["alias"] and item["display"]


# ----------------------------------------------------------------- 入参与类型

def test_plan_edit_never_mutates_its_inputs():
    current = af(Q_BASE)
    suppressed = ["include:tissue"]
    lenient = ["disease"]
    facets = [{"dim": "species", "value": "homo sapiens"}]
    before = copy.deepcopy((current, suppressed, lenient, facets))
    board.plan_edit(Q_BASE, "换成小鼠", current_filters=current,
                    suppressed_constraints=suppressed, lenient_dims=lenient, facet_filters=facets)
    assert (current, suppressed, lenient, facets) == before


def test_response_is_json_serializable_and_lenient_dims_is_a_list():
    import json
    plan = board.plan_edit(Q_BASE, "放宽组织", current_filters=af(Q_BASE), coverage_dims=["tissue"])
    assert isinstance(plan["next_request"]["lenient_dims"], list)
    assert isinstance(plan["echoed"]["lenient_dims"], list)
    json.dumps(plan, ensure_ascii=False)


@pytest.mark.parametrize("kwargs,code", [
    ({"query": Q_BASE, "utterance": ""}, "empty_input"),
    ({"query": Q_BASE, "utterance": "x" * (board.MAX_UTTERANCE_CHARS + 1)}, "too_large"),
    ({"query": "x" * (board.MAX_QUERY_CHARS + 1), "utterance": "换成小鼠"}, "too_large"),
    ({"query": Q_BASE, "utterance": "换成小鼠", "forced_op": "nonsense"}, "bad_param"),
])
def test_bad_input_raises_a_typed_error(kwargs, code):
    with pytest.raises(board.BoardError) as excinfo:
        board.plan_edit(**kwargs)
    assert excinfo.value.code == code


def test_facets_are_kept_for_widening_edits_and_cleared_for_narrowing_ones():
    facets = [{"dim": "species", "value": "homo sapiens"}]
    kept = board.plan_edit(Q_BASE, "去掉组织限制", current_filters=af(Q_BASE), facet_filters=facets)
    assert kept["next_request"]["facet_filters"] == facets

    cleared = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE), facet_filters=facets)
    assert cleared["next_request"]["facet_filters"] == []
    assert "再缩小" in cleared["detail"]


# ----------------------------------------------------------------- 条件板视图

def test_board_view_splits_four_zones_and_hides_stale_values():
    view = board.board_view(
        af(Q_BASE),
        [{"dim": "species", "value": "homo sapiens", "display": "Homo sapiens"}],
        ["disease"],
        ["include:tissue"],
        ["tissue"],
    )
    zones = [row["zone"] for row in view["rows"]]
    assert zones.index("query") < zones.index("facet") < zones.index("lenient") < zones.index("suppressed")
    suppressed_row = [r for r in view["rows"] if r["zone"] == "suppressed"][0]
    # 取值在被忽略之后已经不在返回里了，显示旧值就是拿过期快照当事实。
    assert suppressed_row["values"] == []
    assert suppressed_row["label"] == "组织"
    tissue_row = [r for r in view["rows"] if r["zone"] == "query" and r["dim"] == "tissue"][0]
    assert tissue_row["lenientable"] is True
    species_row = [r for r in view["rows"] if r["zone"] == "query" and r["dim"] == "species"][0]
    assert species_row["lenientable"] is False
    assert {n["zone"] for n in view["notes"]} == {"query", "facet", "lenient", "suppressed"}


def test_board_view_labels_negative_rows_as_exclusions():
    view = board.board_view(af("找人类肺组织数据，不要肿瘤"), [], [], ["exclude:disease"], [])
    suppressed_row = [r for r in view["rows"] if r["zone"] == "suppressed"][0]
    assert suppressed_row["label"] == "排除·疾病"


# ----------------------------------------------------------------- 反重言式：证明校验真的会失败

def test_verify_is_not_vacuously_true(monkeypatch):
    """构造一个必然被判红的候选，证明校验真会失败——否则上面所有「校验通过」都不值钱。

     换了载荷：原来是「找小鼠肾脏的肺癌数据 或者 人类」，靠「或」整句弃权来触发红灯；
    「或」现在照做了，那句成了合法查询，这条门会变成空转（比失败更糟：它会一直绿）。
    改用**来源范围守卫**：原句里一个来源专名都没有，候选句凭空多出「10x」——
    检索范围会跟着变，这是 `plan_edit` 里独立于可执行性的另一道硬守卫。
    """
    plan = board.plan_edit(Q_BASE, "换成小鼠",
                           candidate_override="找 10x 的小鼠肾脏数据",
                           current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_REJECTED
    assert plan["verify"]["ok"] is False and plan["verify"]["mismatches"]
    assert plan["verify"]["mismatches"][0]["code"] == "source_scope_changed", plan["verify"]

    ok = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE))
    assert ok["status"] == board.ST_CONFIRM and ok["verify"]["ok"] is True


def test_echoed_is_normalized_not_a_verbatim_copy():
    """回声是**规范化后**的入参，不是原样复读——两者必须能区分开，否则这个字段等于没有。"""
    plan = board.plan_edit(Q_BASE, "换成小鼠",
                           current_filters=af(Q_BASE),
                           facet_filters=[{"dim": "species", "value": "Homo Sapiens"},
                                          {"dim": "不存在的维度", "value": "x"}],
                           suppressed_constraints=["include:tissue", "乱写的"],
                           lenient_dims=["disease", "乱写"])
    assert plan["echoed"]["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]
    assert plan["echoed"]["suppressed_constraints"] == ["include:tissue"]
    assert plan["echoed"]["lenient_dims"] == ["disease"]
    # 规范化是幂等的。
    again = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE),
                            facet_filters=plan["echoed"]["facet_filters"],
                            suppressed_constraints=plan["echoed"]["suppressed_constraints"],
                            lenient_dims=plan["echoed"]["lenient_dims"])
    assert again["echoed"]["facet_filters"] == plan["echoed"]["facet_filters"]
    assert again["echoed"]["suppressed_constraints"] == plan["echoed"]["suppressed_constraints"]
    assert again["echoed"]["lenient_dims"] == plan["echoed"]["lenient_dims"]


def test_applied_plan_differs_from_the_echo():
    """姊妹断言：真的改了的时候，下一步请求必须与回声不同——证明上面那条比较能失败。"""
    plan = board.plan_edit(Q_BASE, "去掉组织限制", current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_AUTO
    assert plan["next_request"]["suppressed_constraints"] != plan["echoed"]["suppressed_constraints"]


# ----------------------------------------------------------------- 结构性隔离

def test_planning_never_loads_the_dataset_catalog(monkeypatch):
    """规划一步改动不该读磁盘上的数据集目录。用「读了就炸」来证明，而不是数名字。"""
    from dataset_recommender.corpus import corpus

    def explode(*args, **kwargs):  # pragma: no cover - 只在回归时触发
        raise AssertionError("条件板规划过程中装载了数据集目录")

    monkeypatch.setattr(corpus, "load_normalized_corpus", explode)
    monkeypatch.setattr(corpus, "_load_base", explode)
    monkeypatch.setattr(corpus, "known_source_values", explode)

    plan = board.plan_edit(Q_BASE, "换成小鼠", current_filters=af(Q_BASE))
    assert plan["status"] == board.ST_CONFIRM


def test_dimensions_stay_in_sync_with_the_parser():
    for dim in DIMENSIONS:
        assert board._dim_label(dim)
    assert board._dim_label("has_raw_data") == "原始数据"
    assert board._dim_label("date") == "发表时间"
