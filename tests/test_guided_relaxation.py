"""引导式放宽：0 结果（空交集）时逐个丢约束试算「放宽哪个能救回多少 + top-k 预览」。

钉死：
1) 空交集（非弃权）→ 回传放宽项，每项含 kind + 真实计数 + 预览，**同一档内**按救回数降序；弃权 → 无放宽项。
2) 放宽预览是**确定性** retrieve 出的、真满足放宽后约束（不引入违规）。
3) `/api/recommend` 把放宽项结构化返回（kind/label/count/results），供前端分组展示、一键切入。
4) 只在 0 结果路径触发；对官方评测/确定性零影响（评测走 sources=None、不经此路径）。
5) 两档策略：`drop`＝去掉一个条件（保守）、`only`＝只按一个条件搜（激进）。
   第二档只在放宽目标 ≥3 个时出现——恰好 2 个时它与第一档某项**逐字同义**，并排列两条一样的建议是误导。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.corpus.corpus import available_sources, load_full_corpus  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import DatasetRetriever, passes_hard_filter  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


def _corpus():
    s = get_settings()
    return load_full_corpus(s.data_dir, s.project_root)


def test_relaxation_options_on_empty_intersection():
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    intent = parse_query("拟南芥的乳腺癌数据")   # 各自有数据、交集为空
    assert not intent.abstain
    assert r.retrieve(recs, intent, top_k=5) == []       # 确实 0 结果
    opts = r.relaxation_options(recs, intent, top_k=5)
    keys = {o["key"] for o in opts}
    assert "dim:species" in keys and "dim:disease" in keys
    # 只有两个放宽目标（物种 + 疾病）→ 「只按一个条件搜」与「去掉另一个」同义，刻意不生成
    assert {o["kind"] for o in opts} == {"drop"}
    # 同一档内按救回数降序、每项计数>0、有预览
    counts = [o["count"] for o in opts if o["kind"] == "drop"]
    assert counts == sorted(counts, reverse=True)
    for o in opts:
        assert o["count"] > 0 and len(o["candidates"]) >= 1


def test_only_one_condition_tier_appears_when_there_are_enough_conditions():
    """条件 ≥3 个时给出第二档「只按一个条件搜」，且它确实比第一档放得更开。

    这一档存在的理由不是「多给几个按钮」：条件互相叠加得太死时，去掉**任何单独一个**仍然是 0 条，
    第一档会整档为空 —— 用户就只看到「无结果」而没有任何下一步。
    """
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    intent = parse_query("小鼠的乳腺癌 FASTQ 数据")   # 物种 + 疾病 + FASTQ ＝ 3 个放宽目标
    opts = r.relaxation_options(recs, intent, top_k=5)
    onlys = [o for o in opts if o["kind"] == "only"]
    drops = [o for o in opts if o["kind"] == "drop"]
    assert onlys, "三个条件时应给出「只按一个条件搜」这一档"
    assert all(o["key"].startswith("only:") for o in onlys)
    assert [o["count"] for o in onlys] == sorted([o["count"] for o in onlys], reverse=True)
    # 「只按物种搜」放开的东西严格多于「去掉疾病」，救回条数必然 ≥ 后者。
    only_species = next(o for o in onlys if o["key"] == "only:species")
    drop_disease = next((o for o in drops if o["key"] == "dim:disease"), None)
    if drop_disease:
        assert only_species["count"] >= drop_disease["count"]


def test_only_tier_really_drops_every_other_condition():
    """「只按 X 搜」的预览必须**只**按 X 筛——否则它就只是另一个「去掉一个」，名字在撒谎。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    intent = parse_query("小鼠的乳腺癌 FASTQ 数据")
    for o in r.relaxation_options(recs, intent, top_k=5):
        if o["kind"] != "only":
            continue
        dim = o["key"].split(":", 1)[1]
        kept = parse_query(intent.original_query)
        kept.constraints = {dim: intent.constraints[dim]}
        kept.excluded_constraints = {}
        kept.has_raw_data_required = None
        kept.date_from = kept.date_to = ""
        for c in o["candidates"]:
            assert passes_hard_filter(c.record, kept)


def test_relaxation_previews_are_valid_after_drop():
    """去掉某维度后的预览记录，必须真满足**其余**约束（不引入违规）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    intent = parse_query("小鼠的乳腺癌 FASTQ 数据")
    opts = [o for o in r.relaxation_options(recs, intent, top_k=5) if o["kind"] == "drop"]
    assert opts
    for o in opts:
        dropped = o["key"]
        relaxed = parse_query(intent.original_query)  # 重解析
        if dropped.startswith("dim:"):
            relaxed.constraints.pop(dropped.split(":", 1)[1], None)
        elif dropped == "raw":
            relaxed.has_raw_data_required = None
        for c in o["candidates"]:
            assert passes_hard_filter(c.record, relaxed)   # 预览项满足放宽后约束


def test_no_relaxation_on_abstain():
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    assert r.relaxation_options(recs, parse_query("翼龙的单细胞数据")) == []
    assert r.relaxation_options(recs, parse_query("不要小鼠的原始数据")) == []   # 跨维歧义否定→弃权，不放宽


def test_api_returns_relaxation_options():
    s = get_settings()
    all_sources = [x["value"] for x in available_sources(s.data_dir, s.project_root)]
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/api/recommend", json={
        "query": "拟南芥的乳腺癌数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []                       # 0 结果
    opts = data["relaxation_options"]
    assert len(opts) >= 2
    top = opts[0]
    assert top["count"] > 0 and top["label"] and len(top["results"]) >= 1
    # kind 必须真的传到 API 层：前端据它分组、且据它决定横幅说「去掉 X」还是「只按 X 搜」，
    # 后端算了前端拿不到，就会用「去掉」的模板去描述一次「只按」——说反了就是撒谎。
    assert all(o.get("kind") in {"drop", "only"} for o in opts)
    # 预览行是标准卡片字段（能被前端直接渲染）
    assert "dataset_name" in top["results"][0] and "source" in top["results"][0]


def test_api_no_relaxation_when_results_exist():
    s = get_settings()
    all_sources = [x["value"] for x in available_sources(s.data_dir, s.project_root)]
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/api/recommend", json={
        "query": "人类乳腺癌数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
    })
    data = resp.json()
    assert len(data["results"]) >= 1                   # 有结果
    assert data["relaxation_options"] == []            # 不显示放宽


def test_relaxation_options_carry_suppress_filter_ids():
    """每项带 suppress＝**本项**放松量的 filter_id 清单（`relax_key_to_suppressed`
    单锚点，仅本项、不含调用方既有忽略）——放松卡/agent 环内点选后原样下发为
    suppressed_constraints 即可零 LLM 重搜；缺这个字段，前端只能把 key→条件的
    映射再猜一遍（双份真源）。"""
    recs = _corpus()
    r = DatasetRetriever(top_k=5)
    intent = parse_query("拟南芥的乳腺癌数据")
    by_key = {o["key"]: o for o in r.relaxation_options(recs, intent, top_k=5)}
    assert by_key["dim:species"]["suppress"] == ["include:species"]
    assert by_key["dim:disease"]["suppress"] == ["include:disease"]

    intent3 = parse_query("小鼠的乳腺癌 FASTQ 数据")
    only = next(o for o in r.relaxation_options(recs, intent3, top_k=5)
                if o["key"] == "only:species")
    # 「只按物种搜」＝其余全部放松：疾病 + FASTQ（顺序即锚点产出顺序）
    assert only["suppress"] == ["include:disease", "raw:required"]


def test_api_relaxation_suppress_merges_request_level_suppressed():
    """/api/recommend 把请求级 suppressed_constraints 与本项放松量保序去重合并后下发
    （整组替换语义，2026-09-14 收编：与环内 ask.relax 卡同口径）——不合并的话，前端
    点选后原样回传会把用户已松开的条件偷偷拧回去。"""
    s = get_settings()
    all_sources = [x["value"] for x in available_sources(s.data_dir, s.project_root)]
    client = TestClient(app, base_url="http://127.0.0.1")
    resp = client.post("/api/recommend", json={
        "query": "拟南芥的乳腺癌 FASTQ 数据", "sources": all_sources,
        "use_llm": False, "mock_llm": True, "top_k": 5,
        "suppressed_constraints": ["raw:required"],     # 已忽略 FASTQ，仍零命中
    })
    assert resp.status_code == 200
    opts = resp.json()["relaxation_options"]
    by_key = {o["key"]: o for o in opts}
    # 既有忽略在前、本项放松量在后；已被忽略的条件不再出现在可放松项里
    assert by_key["dim:species"]["suppress"] == ["raw:required", "include:species"]
    assert by_key["dim:disease"]["suppress"] == ["raw:required", "include:disease"]
    assert not any(o["key"] == "raw" for o in opts)
