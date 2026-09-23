# -*- coding: utf-8 -*-
"""「已命中」合并 + 原始命中可删：suppressed_constraints 在检索前放宽被删维度。

背景：数据细化侧栏把「本次查询命中」（原始硬约束）与「已细化」（后加筛选）合并为「已命中」，
并让**原始命中**也能像细化项一样删除——删某维度即把它加入 suppressed_constraints，后端在
`parse_query` 之后、检索之前抹掉该维度的硬约束 → 检索真正放宽。

不变量：suppressed 缺省/空/全非法 → `apply_suppressed_constraints` 完全 no-op → 官方评测 / MCP /
CLI（不传此参）逐位一致、确定性零影响（本文件 test_empty_suppression_bit_identical 钉住这一点）。
"""
import json
import sys
from pathlib import Path

from starlette.requests import Request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.app.workflow import (  # noqa: E402
    SUPPRESSIBLE_DIMS,
    DatasetRecommendationWorkflow,
    apply_suppressed_constraints,
)

_KM = get_settings().keyword_mapping


def _run(q, sup=None):
    return DatasetRecommendationWorkflow().run_with_meta(query=q, use_llm=False, suppressed_constraints=sup)


def _dims(meta):
    return [g["dim"] for g in meta.active_filters]


# ---------- 单元：apply_suppressed_constraints 就地抹掉被抑制维度 ----------
def test_apply_suppression_unit():
    intent = parse_query("推荐有 FASTQ 的人类乳腺癌数据", _KM)
    assert "species" in intent.constraints and intent.has_raw_data_required is True
    apply_suppressed_constraints(intent, ["species", "has_raw_data"])
    assert "species" not in intent.constraints
    assert "species" not in intent.display_map
    assert intent.has_raw_data_required is None
    assert "disease" in intent.constraints          # 未抑制维度保留


def test_apply_suppression_date():
    intent = parse_query("小鼠大脑 2023 年", _KM)
    assert intent.date_from and intent.date_to
    apply_suppressed_constraints(intent, ["date"])
    assert intent.date_from == "" and intent.date_to == ""


def test_apply_suppression_noop_on_empty_or_bogus():
    intent = parse_query("推荐有 FASTQ 的人类乳腺癌数据", _KM)
    snap = (dict(intent.constraints), dict(intent.display_map),
            intent.has_raw_data_required, intent.date_from, intent.date_to)
    apply_suppressed_constraints(intent, None)
    apply_suppressed_constraints(intent, [])
    apply_suppressed_constraints(intent, ["bogus", "not_a_dim"])
    after = (dict(intent.constraints), dict(intent.display_map),
             intent.has_raw_data_required, intent.date_from, intent.date_to)
    assert snap == after


# ---------- workflow：抑制真实维度 → 放宽（total 不降 + 该维度从 active_filters 消失）----------
def test_suppress_raw_widens_and_drops_dim():
    q = "推荐有 FASTQ 的人类乳腺癌数据"
    base, sup = _run(q), _run(q, ["has_raw_data"])
    assert "has_raw_data" in _dims(base) and "has_raw_data" not in _dims(sup)
    assert sup.result_total > base.result_total       # 放宽 FASTQ 严格变宽
    assert "species" in _dims(sup) and "disease" in _dims(sup)   # 其余维度仍在


def test_suppress_species_widens():
    q = "有 FASTQ 的人类数据"
    base, sup = _run(q), _run(q, ["species"])
    assert "species" not in _dims(sup)
    assert sup.result_total > base.result_total       # 去物种严格变宽


# ---------- 空/缺省/全非法 suppressed → 与不传逐位一致（冻结门安全的直接证据）----------
def test_empty_suppression_bit_identical():
    q = "推荐有 FASTQ 的人类乳腺癌数据"
    a = _run(q)
    for x in (_run(q, []), _run(q, None), _run(q, ["bogus"])):
        assert x.answer == a.answer
        assert x.active_filters == a.active_filters
        assert x.result_total == a.result_total


def test_abstain_query_surfaces_no_chips():
    """弃权查询不出「已命中」chip：删也解不了弃权，避免误导。

     换了载荷：原来用「人类或小鼠数据」「最好是人类的乳腺癌」，而「或」与 hedge
    现在都照做了（前者＝同维度多值，后者＝软偏好），那两句已经不再弃权。
    改用仍然弃权的两档：未收录词、软性排除（后者是写明的永久红线）。
    """
    from dataset_recommender.retrieval.query_parser import active_filters, parse_query
    for q in ("霍格沃茨综合征的人类数据", "优先不要小鼠的肺数据"):
        it = parse_query(q, _KM)
        assert it.abstain, q
        assert active_filters(it) == [], q


def test_or_and_hedge_now_surface_chips_instead_of_nothing():
    """反过来钉一次：这两句不再弃权，且 chip 如实区分「筛选」与「优先」。

    「人类或小鼠数据」→ 物种一条 chip 两个值（同维度多值＝或）；
    「最好是人类的乳腺癌」→ 疾病是硬筛选、人类是**优先**（polarity=prefer），
    不能混成一条让用户以为已经按物种筛过了。
    """
    from dataset_recommender.retrieval.query_parser import active_filters, parse_query
    it = parse_query("人类或小鼠数据", _KM)
    assert not it.abstain, it.abstain_reason
    species = [f for f in active_filters(it) if f["dim"] == "species"]
    assert len(species) == 1 and len(species[0]["values"]) == 2, active_filters(it)
    assert it.or_handling.get("fit") == "exact", it.or_handling

    it2 = parse_query("最好是人类的乳腺癌", _KM)
    assert not it2.abstain, it2.abstain_reason
    pol = {f["dim"]: f["polarity"] for f in active_filters(it2)}
    assert pol.get("species") == "prefer", active_filters(it2)
    assert pol.get("disease") == "include", active_filters(it2)


def test_executable_negation_surfaces_polarity_chips():
    """可执行否定（负向语法落地）：『不要 FASTQ 的人类数据』= exclude raw + include human，
    出带极性 filter_id 的 chip，且检索结果全部无 FASTQ（不再弃权）。"""
    from dataset_recommender.retrieval.query_parser import active_filters, parse_query
    it = parse_query("不要 FASTQ 的人类数据", _KM)
    assert not it.abstain and it.has_raw_data_required is False
    af = active_filters(it)
    assert any(c.get("filter_id") == "raw:forbidden" and c.get("polarity") == "exclude" for c in af)
    assert any(c.get("filter_id") == "include:species" for c in af)
    run = _run("不要 FASTQ 的人类数据")
    assert run.resolution_status == "results" and run.active_filters
    for r in run.retrieved_data:
        assert r.get("raw_data_status", {}).get("code") != "has_fastq", r.get("dataset_name")


# ---------- sanitizer + API 端到端 ----------
def test_sanitize_and_api_end_to_end():
    from dataset_recommender.app.webapp import RecommendRequest, _sanitize_suppressed, api_recommend

    assert _sanitize_suppressed(["species", "bogus", "species", "date"]) == ["species", "date"]
    assert _sanitize_suppressed(None) == [] and _sanitize_suppressed("x") == []
    assert set(_sanitize_suppressed(list(SUPPRESSIBLE_DIMS))) == set(SUPPRESSIBLE_DIMS)

    q = "推荐有 FASTQ 的人类乳腺癌数据"
    request = Request({
        "type": "http",
        "headers": [(b"host", b"127.0.0.1")],
        "scheme": "http",
        "server": ("127.0.0.1", 80),
    })
    base = json.loads(api_recommend(RecommendRequest(query=q), request).body.decode("utf-8"))
    sup = json.loads(api_recommend(
        RecommendRequest(query=q, suppressed_constraints=["has_raw_data"]), request).body.decode("utf-8"))
    assert "has_raw_data" in {g["dim"] for g in base["query_constraints"]}
    assert "has_raw_data" not in {g["dim"] for g in sup["query_constraints"]}
    assert base["applied_suppressed"] == [] and sup["applied_suppressed"] == ["has_raw_data"]
    assert sup["result_total"] > base["result_total"]
