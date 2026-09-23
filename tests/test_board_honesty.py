# -*- coding: utf-8 -*-
"""条件板的诚实性与文案门。

这一组测试不检查功能对不对，只检查**它有没有资格把话说出口**：
拒绝时必须真的什么都没改、每一条回执都得有话说、面向用户的字里不许有开发者黑话或空口断言。
"""
import re
from pathlib import Path

import pytest

from dataset_recommender.app import board
from dataset_recommender.retrieval.query_parser import active_filters, parse_query

Q_BASE = "找人类肺组织的单细胞数据"

#: 面向用户的文案里不该出现的内部说法。这些词在代码里天经地义，在界面上是黑话。
INTERNAL_JARGON = ("维度", "解析", "字段", "参数", "接口", "调用", "布尔", "序列化", "枚举")

#: 自夸禁语，与 tests/test_audience_copy_invariants.py 同源。
BRAG_WORDS = ("新颖", "首次", "没人做过", "填补空白", "业界领先", "novel", "unprecedented")


def af(query):
    return active_filters(parse_query(query))


def test_every_copy_line_is_plain_text():
    """面向用户的字符串一律按纯文本转义呈现——写了星号就会原样显示成两个星号。"""
    for key, value in board.COPY.items():
        assert "**" not in value, f"{key} 里有 markdown 强调"
        assert "`" not in value, f"{key} 里有反引号"


def test_no_internal_jargon_or_brag_in_user_copy():
    for key, value in board.COPY.items():
        for word in INTERNAL_JARGON:
            assert word not in value, f"{key} 里出现了内部说法「{word}」"
        for word in BRAG_WORDS:
            assert word.lower() not in value.lower(), f"{key} 里出现了自夸词「{word}」"


def test_mismatch_explanations_are_all_in_plain_chinese():
    for code, text in board._MISMATCH_ZH.items():
        assert text and text != code
        assert "**" not in text


@pytest.mark.parametrize("utterance,kwargs", [
    ("今天天气不错", {}),
    ("再加一条换成小鼠", {}),
    ("换成树鼩", {}),
    ("换成大鼠脑", {}),
    ("去掉平台限制", {}),
    ("放宽疾病", {"coverage_dims": []}),
    ("再加一条：小鼠", {}),
    ("换成 cellxgene", {}),
])
def test_non_applied_states_carry_a_message_and_change_nothing(utterance, kwargs):
    plan = board.plan_edit(Q_BASE, utterance, current_filters=af(Q_BASE), **kwargs)
    assert plan["status"] in (board.ST_UNKNOWN, board.ST_REJECTED, board.ST_CHOICE, board.ST_SUGGEST)
    assert plan["next_request"] is None, "没有执行就绝不能给出下一步请求"
    assert plan["message"].strip(), "拒绝也必须说清楚为什么"


def test_lenient_without_coverage_info_does_not_claim_there_is_no_gap():
    """「这一项现在没有没填的数据集」是一条**关于数据**的结论，不能建立在一个缺席的入参上。

    网页端每次都送 coverage_dims，MCP 调用方可以完全不送——合并成同一个空列表之后，
    同一步在两个入口一个照做、一个信誓旦旦地说「没有缺口」。第三态把「没告诉我」单独分出来：
    照做放宽（它只会放宽、不会更严），但一个字都不许声称缺口有多大。
    """
    told_empty = board.plan_edit(Q_BASE, "放宽组织", current_filters=af(Q_BASE), coverage_dims=[])
    assert told_empty["status"] == board.ST_UNKNOWN
    assert "没有没填的数据集" in told_empty["message"]

    not_told = board.plan_edit(Q_BASE, "放宽组织", current_filters=af(Q_BASE))
    assert not_told["status"] == board.ST_AUTO, "没拿到覆盖情况时照做放宽，不是拒绝"
    assert "没有没填的数据集" not in not_told["message"], "没拿到就不许下这个结论"
    assert "说不出这一步会多出多少" in not_told["message"]
    assert "tissue" in (not_told["next_request"] or {}).get("lenient_dims", [])


def test_every_response_has_every_key_regardless_of_status():
    """缺键会让消费者只能靠 try/except 判断状态——形状必须恒定。"""
    required = {
        "schema", "status", "op", "dim", "message", "detail", "next_request",
        "removed_text", "dropped_terms", "verify", "choices", "suggestions",
        "board_view", "echoed",
        # 这句话若不是「改条件」，它是什么（action / identifier / new_query / ""）。
        # 与其它键一样**在任何状态下都在场**，缺省 ""——前端据此把「对话记录」与「初次对话」
        # 接住同一批说法，而不是回一句听不懂。
        "route",
        # 这句话里出现的执行类说法（打包 / 下载脚本 / 导出引文…）。与 route **分开**给：
        # 「把 E-MTAB-1234 打包」的 route 是 identifier（编号优先，否则会在毫不相干的结果上开下载面板），
        # 但用户确实说了「打包」——只按 route 走会把这半句诉求静默吞掉。同样恒在场，缺省 []。
        "action_markers",
    }
    cases = [
        ("换成小鼠", {}),
        ("今天天气不错", {}),
        ("去掉组织限制", {}),
        ("放宽组织", {"coverage_dims": ["tissue"]}),
        ("再加一条：小鼠", {}),
    ]
    for utterance, kwargs in cases:
        plan = board.plan_edit(Q_BASE, utterance, current_filters=af(Q_BASE), **kwargs)
        assert set(plan) == required, f"{utterance} 的回执键集不对：{set(plan) ^ required}"
        assert plan["schema"] == board.BOARD_SCHEMA
        assert isinstance(plan["removed_text"], list)
        assert isinstance(plan["dropped_terms"], list)
        assert isinstance(plan["choices"], list)
        assert isinstance(plan["suggestions"], list)
        assert isinstance(plan["verify"], dict)


def test_choice_labels_never_say_both_are_required():
    plan = board.plan_edit(Q_BASE, "再加一条：小鼠", current_filters=af(Q_BASE))
    for choice in plan["choices"]:
        assert "都要" not in choice["label"]
        assert "同时满足" not in choice["label"]
    assert any("满足其一" in c["label"] for c in plan["choices"])


def test_lenient_footnote_states_the_third_case():
    """已放宽的说明必须覆盖第三种情况：来源只给了抽样、填得不全的也会被纳入。

    这不是措辞偏好——检索侧的宽容判定调的是「这一项填全了没有」，
    来源标注不完整的数据集即便写着别的取值也会被纳入。少说这一句，用户会以为只纳入了空白的那些。
    """
    root = Path(board.__file__).resolve().parents[3]
    copy_js = (root / "web/static/js/core/copy.js").read_text(encoding="utf-8")
    assert 'lenient: Object.freeze' in copy_js
    note = copy_js.split('lenient: Object.freeze', 1)[1].split('suppressed:', 1)[0]
    assert "没填" in note
    assert "填得不全" in note or "抽样" in note
    # 反过来，绝不能承诺一句代码并不成立的话。
    assert "已经标注成别的值的仍然排除" not in note
    assert "zone_lenient_note" not in board.COPY


def test_suppressed_rows_never_show_stale_values():
    view = board.board_view(af(Q_BASE), [], [], ["include:tissue", "include:species"], [])
    for row in view["rows"]:
        if row["zone"] == "suppressed":
            assert row["values"] == []


#: 「这一段不用大模型」这句承诺**真正出现在用户眼前**的地方。
#: 旧版这道门只断言 `board.COPY["offline_note"]` 里含这句话——而那个常量**从不进任何响应**
#: （`board_view` 只回 zone_query/facet/lenient/suppressed 四条 note），是个死常量。
#: 真正被用户读到的两份手抄在下面这两个文件里，此前不在任何门的扫描面内。
_OFFLINE_PROMISE_SURFACES = (
    ("web/static/index.html", "没有用到大模型"),
    ("web/static/js/core/onboarding.js", "不用大模型"),
)


def test_offline_promise_is_literally_true():
    """界面上写着「改条件完全在本地完成，不用大模型」——那就不许有任何联网或大模型的入口。

    守的是**用户看得见的那句话**，不是某个内部常量：承诺写在哪里，门就扫到哪里。
    日后若给条件板接上大模型（例如把 `/api/board/plan` 改成 LLM 参与），这道门会当场变红，
    提醒改的人先去把这句话改掉——而不是让界面上留着一句已经不成立的话。
    """
    root = Path(board.__file__).resolve().parents[3]
    text = open(board.__file__, encoding="utf-8").read()
    for forbidden in ("import requests", "import httpx", "urlopen", "call_llm", "openai"):
        assert forbidden not in text, f"board.py 里出现了 {forbidden}，与「不用大模型」的承诺矛盾"
    found = []
    for rel, promise in _OFFLINE_PROMISE_SURFACES:
        path = root / rel
        assert path.is_file(), f"承诺文案的落点不存在了：{rel}（改了位置就同步改这张表）"
        if promise in path.read_text(encoding="utf-8"):
            found.append(rel)
    assert found, (
        "界面上已经找不到「不用大模型」这句承诺了。若是有意去掉的，把 _OFFLINE_PROMISE_SURFACES "
        "一起改掉；若是漏改，说明用户看到的和代码做的已经对不上。"
    )


def test_plan_never_writes_anything_to_disk():
    text = open(board.__file__, encoding="utf-8").read()
    assert not re.search(r"\bopen\s*\(", text), "board.py 不该有文件读写"
    assert "Path(" not in text


def test_dim_labels_cover_every_filter_id_shape():
    """每一种筛选项编号都要能还原成一句中文条件名，否则已忽略区会出现英文内部名。"""
    shapes = ["include:species", "exclude:disease", "raw:required", "raw:forbidden", "date:range"]
    for fid in shapes:
        label = board._label_for_filter_id(fid)
        assert label and not re.search(r"[a-z_]{3,}", label), f"{fid} 还原出的名字不像中文：{label}"
