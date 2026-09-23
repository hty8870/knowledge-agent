# -*- coding: utf-8 -*-
"""否定/排除语法契约测试（Codex 两轮对抗辩论收敛的必测矩阵）。

覆盖：安全执行白名单 / 实体内否定字保护 / 必须弃权的危险语法 / 四个"当前就存在的静默反向"回归 /
不需要fastq 的 clarification 第三态 / raw 三态精确过滤 / 存活集全集无违规。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import passes_hard_filter  # noqa: E402
from dataset_recommender.corpus.corpus import load_normalized_corpus  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402

_S = get_settings()
_RECS = load_normalized_corpus(_S.data_dir, _S.project_root)


def _p(q):
    return parse_query(q)


# ---------- 安全执行白名单 ----------
def test_exclude_single_species():
    i = _p("不要小鼠")
    assert i.parse_status == "executable" and not i.abstain
    assert i.excluded_constraints.get("species") == ["mouse"]
    assert not i.constraints


def test_exclude_species_list_connectors():
    for q in ("不要小鼠和大鼠", "不要小鼠、大鼠数据", "不要小鼠与大鼠"):
        i = _p(q)
        assert i.parse_status == "executable", q
        assert "mouse" in i.excluded_constraints.get("species", []), q
        assert "rat" in i.excluded_constraints.get("species", []), q


def test_raw_forbidden_variants():
    """2026-08-18 钉字：英文可执行前缀扩到 not/free of 时，raw 专用分支必须同表扩展。

    否则通用 4d 会消费 raw span，却不会设置 ``has_raw_data_required=False``，把明确排除
    静默降成「不筛 FASTQ」。本矩阵同时覆盖 fastq 与 raw data 两种物理名。
    """
    for q in ("不要fastq", "不要原始数据", "无FASTQ数据", "没有 FASTQ 的数据",
              "without fastq", "not fastq", "free of fastq",
              "not raw data", "free of raw data"):
        i = _p(q)
        assert i.parse_status == "executable", q
        assert i.has_raw_data_required is False, q


def test_english_raw_negation_never_returns_fastq_records():
    """2026-08-18 行为钉：not/free of + raw 对象必须真排除，不能只把词面吃掉。"""
    for q in ("human data not fastq", "human data free of fastq"):
        i = _p(q)
        assert not i.abstain, (q, i.abstain_reason)
        assert i.has_raw_data_required is False, q
        survivors = [r for r in _RECS if passes_hard_filter(r, i)]
        assert survivors, q
        assert all(r.has_raw_data is False for r in survivors), q


def test_de_boundary_template_adv03():
    i = _p("不要小鼠的人类数据")
    assert i.parse_status == "executable"
    assert i.constraints.get("species") == ["human"]
    assert i.excluded_constraints.get("species") == ["mouse"]


def test_circumfix_adv04():
    i = _p("除了脑以外的人类组织数据")
    assert i.parse_status == "executable"
    assert i.constraints.get("species") == ["human"]
    assert i.excluded_constraints.get("tissue") == ["brain"]


def test_de_boundary_keeps_positive_suffix_entities():
    i = _p("不要小鼠的人脑数据")
    assert i.parse_status == "executable"
    assert "human" in i.constraints.get("species", [])
    assert "brain" in i.constraints.get("tissue", [])
    assert i.excluded_constraints.get("species") == ["mouse"]


def test_english_no_prefix():
    i = _p("no mouse data")
    assert i.parse_status == "executable"
    assert i.excluded_constraints.get("species") == ["mouse"]


# ---------- 英文否定改写盲区（h41：rerank 把中文否定句改写成英文措辞） ----------
def test_english_not_prefix_executes_exclusion():
    """2026-08-18 保护钉：修 raw 对象不能回退非 raw typed target 的既有执行语义。"""
    # h41 失手句型：环内 rerank 把「人的血液样本，淋巴瘤的不要」改写成英文否定，
    # 「not」此前只检测（guard）不执行 → unsupported_negation 弃权、排除约束丢失。
    i = _p("human blood sample not lymphoma")
    assert not i.abstain, i.abstain_reason
    assert i.excluded_constraints.get("disease") == ["lymphoma"]
    assert "human" in i.constraints.get("species", [])
    assert "blood" in i.constraints.get("tissue", [])
    assert "lymphoma" not in i.constraints.get("disease", [])


def test_english_without_not_eaten_by_filler_with():
    # 入口 neg_signal 检测的 filler 子串删除曾把 "without" 咬成 "out"（"with" 是 FILLER），
    # 信号丢失走正向路径以 unresolved_term 弃权——4d 执行步本体对 without 一直是可执行的。
    i = _p("human blood sample without lymphoma")
    assert not i.abstain, i.abstain_reason
    assert i.excluded_constraints.get("disease") == ["lymphoma"]
    assert "human" in i.constraints.get("species", [])


def test_english_free_of_prefix():
    """2026-08-18 保护钉：free of 的非 raw 对象仍走结构化疾病 exclusion。"""
    i = _p("human blood sample free of lymphoma")
    assert not i.abstain, i.abstain_reason
    assert i.excluded_constraints.get("disease") == ["lymphoma"]


def test_english_why_not_idiom_abstains():
    # 「why not X」是建议反问（含义≈想要 X），不是排除 → 宁可弃权绝不静默反向。
    i = _p("why not mouse data")
    assert i.abstain and i.abstain_reason == "interrogative_negation", i.abstain_reason


def test_english_not_without_entity_still_abstains():
    # 「not」后不是受控词表实体时不猜（同中文白名单口径）→ 诚实弃权。
    i = _p("i do not want mouse data")
    assert i.abstain and i.abstain_reason == "unsupported_negation", i.abstain_reason


def test_english_non_small_cell_lung_cancer_not_reversed():
    # 「非小细胞肺癌」英文同款红线：non- 是词素、整体是正向疾病实体，绝不当排除操作符。
    i = _p("non-small cell lung cancer data")
    assert not i.abstain, i.abstain_reason
    assert "non-small cell lung" in i.constraints.get("disease", [])
    assert not i.excluded_constraints


def test_english_free_suffix_abstains_honestly():
    # 「X-free」后缀否定暂无执行机制，但必须保持诚实弃权——绝不静默反向成正向包含。
    i = _p("lymphoma-free human blood sample")
    assert i.abstain, "X-free 未支持时必须弃权，不能静默吞掉否定"


# ---------- 四个"当前就存在的静默反向" bug 的回归 ----------
def test_regression_no_silent_reversal():
    # 无FASTQ 曾被误判成"要 fastq"（raw True）
    assert _p("无FASTQ数据").has_raw_data_required is False
    # 没有小鼠 / 没小鼠 曾被 filler 吞成正向 include mouse
    assert _p("没有小鼠的数据").excluded_constraints.get("species") == ["mouse"]
    assert "species" not in _p("没有小鼠的数据").constraints
    assert _p("没小鼠数据").excluded_constraints.get("species") == ["mouse"]
    # no-mouse 曾因英文两字母被忽略
    assert _p("no-mouse data").excluded_constraints.get("species") == ["mouse"]


# ---------- 实体内否定字保护（不能误判） ----------
def test_entity_internal_negation_protected():
    nsclc = _p("非小细胞肺癌的人类数据")
    assert not nsclc.abstain and "non-small cell lung" in nsclc.constraints.get("disease", [])
    xen = _p("非洲爪蟾的数据")
    assert not xen.abstain and "xenopus" in xen.constraints.get("species", [])
    # 免疫是 filler，不含否定语义，不应被单字「免」guard 误触发
    imm = _p("人类免疫细胞数据")
    assert not imm.abstain and imm.constraints.get("species") == ["human"]


def test_existential_question_is_positive():
    i = _p("有没有小鼠脑数据")
    assert not i.abstain
    assert "mouse" in i.constraints.get("species", [])
    assert "brain" in i.constraints.get("tissue", [])
    assert not i.excluded_constraints


# ---------- 必须弃权的危险语法 ----------
def test_must_abstain_cases():
    cases = {
        # 注：非霍奇金淋巴瘤 已由 non 复合术语修复映射到 lymphoma（见 test_negation_exempt.py）；
        # 这里换一个仍无对应实体的裸「非」case，保持「裸非+未知实体→unsupported_negation」覆盖。
        "非哺乳类的数据": "unsupported_negation",              # 哺乳类非 alias、非白名单，裸「非」
        "不要小鼠脑数据": "cross_dimension_negative_clause",   # 跨维复合 NOT(mouse AND brain)
        "不要小鼠的原始数据": "ambiguous_negation_scope",       # 的 后跨维
        # 2026-07-25：「不要小鼠或大鼠」**已从这张表移出**，改为照做（见下方专项测试）。
        # ¬(A∨B) = ¬A∧¬B，而排除侧的判据逐字就是「命中任一 forbidden 即淘汰」——精确成立。
        "不排除小鼠的数据": "nested_negation",                  # 双重否定（会反向）
        "不要人类的人类数据": "conflicting_polarity",           # 同 target 正负冲突
        "是否排除小鼠的数据": "interrogative_negation",         # 疑问
    }
    for q, reason in cases.items():
        i = _p(q)
        assert i.abstain, f"{q} 应弃权"
        assert i.abstain_reason == reason, f"{q}: 期望 {reason}, got {i.abstain_reason}"


def test_negated_or_is_executed_exactly_not_abstained():
    """「不要小鼠或大鼠」= ¬(A∨B) = ¬A∧¬B —— 排除侧本来就是这个语义，2026-07-25 起照做。

    这条弃权是白弃的：`passes_hard_filter` 的负向判据逐字是「命中任一 forbidden target 即淘汰」，
    也就是同维度多个 forbidden 值天然就是「或」。更荒唐的是语义**更含糊**的
    「不要小鼠和大鼠」（¬(A∧B)）因为「和」是虚词，一直在照做。
    """
    i = _p("不要小鼠或大鼠")
    assert not i.abstain, i.abstain_reason
    # 大鼠会展开成 rat + norvegicus（Rattus norvegicus），是既有的 alias 展开，不在本轮讨论范围。
    excluded = set(i.excluded_constraints.get("species") or [])
    assert {"mouse", "rat"} <= excluded, excluded
    assert not i.constraints.get("species"), "这是排除，不该变成正向包含"
    # 存活集里一条小鼠 / 大鼠都不许有——精确 ¬A∧¬B。
    surv = [r for r in _RECS if passes_hard_filter(r, i)]
    for r in surv:
        low = (r.species or "").lower()
        assert "mouse" not in low and "rat" not in low, r.species
    # 如实回显：这次的「或」落在同一个维度上，是精确档。
    assert i.or_handling.get("fit") == "exact", i.or_handling
    assert i.or_handling.get("or_excluded_dims") == ["species"], i.or_handling
    assert "都排除" in i.or_handling.get("note_zh", ""), i.or_handling


def test_or_fit_counts_what_the_user_said_not_alias_expansions():
    """`fit` 必须数「用户说了几个东西」，不能数「展开出几个 target」。

    真机实测抓到的编造：「肺癌或 10x 的数据」里「肺癌」一个词就展开成
    `['lung cancer', 'non-small cell lung']` 两个 target，按 target 数判定会给出
    「本次按『都算』检索：疾病＝Lung Cancer」这条**假回执**——「或」的另一半（10x）
    是来源专名，早在 parse_query 之前就被摘走了，根本没进任何维度。
    判据改用 `display_map`（用户侧规范展示名，一个词一条）。
    """
    one = _p("肺癌或 10x 的数据")
    assert not one.abstain, one.abstain_reason
    assert len(one.constraints.get("disease") or []) > 1, "前提：肺癌确实展开成多个 target"
    assert one.display_map.get("disease") == ["Lung Cancer"], one.display_map
    assert one.or_handling.get("fit") == "narrower", one.or_handling
    assert "都算" not in one.or_handling.get("note_zh", ""), one.or_handling

    # 对照组：真的写了两个病名 → 展示名两条 → 精确档。
    two = _p("肺癌或肝癌的数据")
    assert len(two.display_map.get("disease") or []) == 2, two.display_map
    assert two.or_handling.get("fit") == "exact", two.or_handling


def test_conditional_negation_abstains():
    i = _p("如果没有小鼠就推荐人类数据")
    assert i.abstain and i.abstain_reason == "conditional_negation"


# ---------- 不需要fastq → clarification 第三态 ----------
def test_clarification_not_required_raw():
    for q in ("不需要fastq的人类数据", "无需fastq"):
        i = _p(q)
        assert i.parse_status == "clarification_required", q
        assert not i.abstain
        assert i.has_raw_data_required is None            # 绝不提前猜成 False
        assert i.clarification_reason == "ambiguous_raw_requirement"
        assert len(i.clarification_options) == 2


# ---------- raw 三态精确过滤（retriever）----------
def test_raw_tristate_filter():
    forbid = _p("不要fastq")
    surv = [r for r in _RECS if passes_hard_filter(r, forbid)]
    assert surv and all(r.has_raw_data is False for r in surv)   # None/True 都排除
    require = _p("有 FASTQ 原始数据")
    surv2 = [r for r in _RECS if passes_hard_filter(r, require)]
    assert surv2 and all(r.has_raw_data is True for r in surv2)


# ---------- 存活集全集无违规 ----------
def test_survivor_set_no_violation():
    i = _p("不要小鼠的人类数据")
    surv = [r for r in _RECS if passes_hard_filter(r, i)]
    assert surv
    for r in surv:
        assert "human" in (r.species or "").lower()
        assert "mouse" not in (r.species or "").lower()


def test_clarification_returns_no_candidates():
    i = _p("不需要fastq")
    assert [r for r in _RECS if passes_hard_filter(r, i)] == []   # 澄清态检索前即空


def test_or_note_uses_chinese_dim_labels():
    """2026-08-31 裁决钉：OR 回执的维度名必须是中文标签
    （assay→「技术」、modality→「模态」），不许英文维度键直出。旧 `_DIM_ZH` 的键
    误写 "technology" 永远 miss，「assay＝…」直出是 bug 而非基线行为。"""
    from dataset_recommender.retrieval.query_parser import _describe_or_handling
    out = _describe_or_handling(
        "ATAC-seq 或 ChIP-seq 的数据",
        constraints={"assay": ["atac-seq", "chip-seq"]},
        display_map={"assay": ["ATAC-seq", "ChIP-seq"]},
    )
    note = out.get("note_zh", "")
    assert "技术＝ATAC-seq / ChIP-seq" in note, note
    assert "assay＝" not in note and "modality＝" not in note, note
    out2 = _describe_or_handling(
        "要 FASTQ 或表达矩阵",
        constraints={"modality": ["fastq", "matrix"]},
        display_map={"modality": ["FASTQ", "表达矩阵"]},
    )
    note2 = out2.get("note_zh", "")
    assert "模态＝FASTQ / 表达矩阵" in note2, note2
