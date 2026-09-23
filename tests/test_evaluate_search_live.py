# -*- coding: utf-8 -*-
"""search 档集成验证 harness（scripts/evaluate_search_live.py）的离线单测 + 门控集成入口。

离线部分（默认跑，绝不发 LLM 请求）：
- 用例集 eval/search_live_cases_v1.jsonl 启动自检全过；
- score_case 各维度双向合成断言（好样本全过 / 坏样本逐维记败）；
- extract_record 的票型/批次/Top1 提取口径（合成 route_turn 返回体）。

集成部分（默认 skip，`BIODATA_SEARCH_LIVE=1` 才跑——仓库没有真 LLM pytest 先例，
集成评测的既有约定是 scripts/ 下手跑；本用例只是给「想留在 pytest 里复跑」开的
显式门控入口）：单跑 s01 一例，断言共识落档 search、preliminary 已发、屏上有结果。
conftest 的全局 stub（route_consensus→general、清单空调用）是给 FakeModel 图内测试
的防错位垫片；集成入口用 import 期存的真引用恢复（与 tests/test_scoped_routing.py
test_json_only_model_walks_search_face_full_graph 同一做法）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import evaluate_search_live as esl  # noqa: E402

# import 期存真引用（conftest autouse stub 先于测试体执行，集成用例在测试体内恢复）
from dataset_recommender.agent import agent_exec as _ax  # noqa: E402

_REAL_RUN_ROUTE_CONSENSUS = _ax._run_route_consensus
_REAL_CHECKLIST_CALL = _ax._task_checklist_call


# ------------------------------------------------------------------ 离线：用例集自检

def test_cases_file_passes_startup_selfcheck():
    cases = esl.load_cases(_ROOT / "eval" / "search_live_cases_v1.jsonl")
    assert len(cases) >= 10
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    # 对照组必须在集内（分流鉴别力的证据一半在对照上）
    assert {"m01", "m02"} <= set(ids)


def test_load_cases_rejects_bad_sample(tmp_path):
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        '{"id": "x", "cat": "A良好表述", "utterance": "u", "expect": {"rank": "maybe"}}\n',
        encoding="utf-8")
    with pytest.raises(SystemExit):
        esl.load_cases(bad)


# ------------------------------------------------------------------ 离线：计分口径

_GOOD_REC = {
    "exc": "", "via": "agent",
    "votes": [{"ok": True, "route": "search", "temperature": 0.0},
              {"ok": True, "route": "search", "temperature": 0.8}],
    "consensus_route": "search",
    "preliminary_fired": True, "preliminary_total": 26,
    "batches": [{"kind": "preliminary"}, {"kind": "rank"}],
    "screen": True, "active_top1": "Human Lung Cancer FFPE",
    "active_top1_fields": {"dataset_name": "Human Lung Cancer FFPE", "species": "Human",
                           "tissue": "Lung", "disease": "lung cancer",
                           "raw": "✅ 包含 FASTQ", "platform": "visium"},
    "active_total": 26,
    "steps": [{"verb": "rank", "ok": True}],
}
_GOOD_CASE = {"id": "g", "expect": {
    "route": "search", "prelim": "yes", "rerank": "forbidden",
    "display_batch": "yes", "screen": "yes",
    "top1_contains": ["lung"],
    "must_match": {"disease": ["lung cancer"], "species": ["human"]},
    "must_not_match": {"species": ["mouse"]}, "min_total": 1}}


def _dims(case, rec):
    return {c["dim"]: c for c in esl.score_case(case, rec)}


def test_score_good_sample_all_pass():
    checks = esl.score_case(_GOOD_CASE, dict(_GOOD_REC))
    assert checks and all(c["ok"] for c in checks)


def test_score_route_mismatch_and_fallback():
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, consensus_route="general"))
    assert d["route"]["ok"] is False
    # agent 跌保底（无共识节点）：route 记败点名 via，votes 不适用不参评
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, votes=[], consensus_route="", via="llm"))
    assert d["route"]["ok"] is False and "via" in d["route"]["detail"]
    assert "votes" not in d


def test_score_display_discipline_and_forbidden_rerank():
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, batches=[{"kind": "preliminary"}]))
    assert d["display_batch"]["ok"] is False
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, steps=[{"verb": "rerank", "ok": True}]))
    assert d["rerank"]["ok"] is False


def test_score_top1_and_totals():
    """ 钉字：结构化字段决定正确性，标题子串只作辅助证据。"""
    bad_fields = dict(_GOOD_REC["active_top1_fields"], disease="healthy")
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, active_top1="Human Normal Lung Atlas",
                               active_top1_fields=bad_fields))
    assert d["top1:disease"]["ok"] is False, "标题含 lung 不能替正常肺冒充肺癌"
    assert d["title_evidence"]["ok"] is True and d["title_evidence"]["required"] is False
    bad_species = dict(_GOOD_REC["active_top1_fields"], species="Mouse")
    d2 = _dims(_GOOD_CASE, dict(_GOOD_REC, active_top1_fields=bad_species))
    assert d2["top1:species"]["ok"] is False
    d = _dims(_GOOD_CASE, dict(_GOOD_REC, active_total=0))
    assert d["min_total"]["ok"] is False
    zero_case = {"id": "z", "expect": {"route": "search", "prelim": "no",
                                       "final_total_max": 0}}
    rec = dict(_GOOD_REC, preliminary_fired=False, batches=[], screen=False,
               active_total=0, steps=[])
    assert all(c["ok"] for c in esl.score_case(zero_case, rec))
    d = _dims(zero_case, dict(rec, screen=True, active_total=2,
                              batches=[{"kind": "rank"}]))
    assert d["final_total_max"]["ok"] is False


def test_score_anyof_group():
    """ 钉字：任一命中组（受控同义类）——carcinoma 满足癌症类期望、healthy 不满足；
    must_not_match 同口径（Mouse 命中禁用组）。"""
    case = {"id": "a", "expect": {"route": "search", "prelim": "yes",
                                  "must_match": {"disease": [["cancer", "carcinoma"]]},
                                  "must_not_match": {"species": [["mouse", "rat"]]}}}
    ca = dict(_GOOD_REC["active_top1_fields"], disease="Ovarian Papillary Serous Carcinoma")
    d = _dims(case, dict(_GOOD_REC, active_top1_fields=ca))
    assert d["top1:disease"]["ok"] is True
    d = _dims(case, dict(_GOOD_REC, active_top1_fields=dict(ca, disease="healthy")))
    assert d["top1:disease"]["ok"] is False
    d = _dims(case, dict(_GOOD_REC, active_top1_fields=dict(ca, species="Mouse")))
    assert d["top1_not:species"]["ok"] is False


# ------------------------------------------------------------------ 离线：记录提取口径

def test_extract_record_pulls_votes_steps_and_top1():
    result = {
        "route": "tool", "via": "agent", "preliminary_final": False,
        "plan": {
            "verb": "rank", "steps": [
                {"verb": "rank", "ok": True,
                 "result": {"query": "肺癌", "total": 26, "displayed": True,
                            "batch": {"kind": "rank"}}},
            ],
            "trace": [
                {"node": "route_consensus", "ok": True,
                 "detail": "2 票（有效 2）→ 走「search」路线。",
                 "route_votes": [
                     {"temperature": 0.0, "bound": True, "ok": True,
                      "route": "search", "reason": "找数据", "raw": "{...}"},
                     {"temperature": 0.8, "bound": True, "ok": True,
                      "route": "search", "reason": "检索诉求", "raw": "{...}"}]},
                {"node": "understand", "ok": True},
            ],
        },
        "result_payload": {"results": [], "result_total": 26},
        "result_batches": [
            {"batch_id": "b1", "kind": "preliminary", "query_effective": "肺癌数据集",
             "payload": {"result_total": 26, "results": [
                 {"dataset_name": "Human Lung Cancer FFPE", "species": "Human",
                  "tissue": "Lung", "disease": "lung cancer",
                  "raw_data_status": "✅ 包含 FASTQ", "platform": "visium"}]}},
            {"batch_id": "b2", "kind": "rank", "query_effective": "肺癌",
             "payload": {"result_total": 26, "results": [
                 {"dataset_name": "Human Lung Cancer FFPE", "species": "Human",
                  "tissue": "Lung", "disease": "lung cancer",
                  "raw_data_status": "✅ 包含 FASTQ", "platform": "visium"}]}},
        ],
        "active_batch": "b2",
    }
    events = [{"kind": "preliminary", "entry": {"result_total": 26, "results": [
        {"dataset_name": "Human Lung Cancer FFPE", "species": "Human",
         "tissue": "Lung", "disease": "lung cancer",
         "raw_data_status": "✅ 包含 FASTQ", "platform": "visium"}]}}]
    rec = esl.extract_record({"id": "x", "cat": "A良好表述", "utterance": "肺癌数据集"},
                             result, events, 123)
    assert rec["consensus_route"] == "search"
    assert [v["route"] for v in rec["votes"]] == ["search", "search"]
    assert rec["preliminary_fired"] is True and rec["preliminary_total"] == 26
    assert rec["steps"][0]["displayed"] is True
    assert rec["active_batch"] == "b2"
    assert rec["active_top1"] == "Human Lung Cancer FFPE"
    assert rec["active_top1_fields"] == {
        "dataset_name": "Human Lung Cancer FFPE", "species": "Human",
        "tissue": "Lung", "disease": "lung cancer", "raw": "✅ 包含 FASTQ",
        "platform": "visium"}
    assert rec["batches"][1]["top1_fields"]["disease"] == "lung cancer"
    assert rec["active_total"] == 26
    assert rec["screen"] is True
    assert rec["nodes"][:2] == ["route_consensus", "understand"]


# ------------------------------------------------------------------ 集成入口（默认 skip）

@pytest.mark.skipif(os.environ.get("BIODATA_SEARCH_LIVE") != "1",
                    reason="真 LLM 复跑入口：BIODATA_SEARCH_LIVE=1 才跑（花 deepseek 额度）")
def test_live_search_route_end_to_end(tmp_path, monkeypatch):
    """真 deepseek 单例（s01 肺癌数据集）：共识落档 search + preliminary 已发 + 屏上有结果。

    恢复 conftest 全局 stub 的真引用（route_consensus / 清单调用）；.userdata 落 tmp_path。
    """
    from dataset_recommender.llm.llm_client import load_llm_config

    monkeypatch.setattr(_ax, "_run_route_consensus", _REAL_RUN_ROUTE_CONSENSUS)
    monkeypatch.setattr(_ax, "_task_checklist_call", _REAL_CHECKLIST_CALL)
    sandbox = esl._make_sandbox_root(tmp_path)
    monkeypatch.setattr(_ax, "_agent_project_root", lambda: sandbox)

    cfg = load_llm_config()
    case = next(c for c in esl.load_cases(_ROOT / "eval" / "search_live_cases_v1.jsonl")
                if c["id"] == "s01")
    rec = esl._execute_case(case, cfg, sandbox)
    assert not rec["exc"], rec["exc"]
    assert rec["consensus_route"] == "search", rec["votes"]
    assert rec["preliminary_fired"] is True
    assert rec["screen"] is True
