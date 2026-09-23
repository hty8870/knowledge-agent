# -*- coding: utf-8 -*-
"""OOV 词表闭环的确定性门：

1. `/api/recommend` 的未收录词弃权（unresolved_term）落结构化日志 `.userdata/oov_terms.jsonl`
   ——词表生长的真数据源（真实用户查询才记；日志失败绝不掀翻检索）；
2. `scripts/measure_entity_gap.py --oov-report` 把日志聚合成 vocabulary 候选报告
   （只产候选、不改词表；「仍 OOV」对照当前 CATALOG 如实标注）。
"""
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from dataset_recommender.app import webapp  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402
from dataset_recommender.corpus import corpus_curation  # noqa: E402
import measure_entity_gap as meg  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture
def oov_log(tmp_path, monkeypatch):
    """把账本目录重定向到沙盒：测试绝不写真实 .userdata。"""
    ledger = tmp_path / "curate_net_ledger.jsonl"
    monkeypatch.setattr(corpus_curation, "_net_ledger_path", lambda root: ledger)
    return tmp_path / "oov_terms.jsonl"


def test_unresolved_term_auto_degrade_is_logged(oov_log):
    """自动降级（2026-09-10 起）：被忽略的词同样落 OOV 日志（词表生长的真数据源不断流）。"""
    res = client.post("/api/recommend", json={"query": "人类膀胱造瘘的单细胞数据", "use_llm": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["resolution_status"] == "degraded"
    assert oov_log.exists(), "未收录词被自动忽略后仍必须落 OOV 日志"
    lines = [json.loads(x) for x in oov_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1
    assert lines[0]["terms"] == ["造瘘"]
    assert "造瘘" in lines[0]["query"]
    assert lines[0]["ts"]


def test_hard_gate_abstain_is_still_logged(oov_log):
    """硬闸（忽略后一个条件都不剩）→ 向量语义兜底（嵌入器不可用时回退弃权）；
    两态下未收录词的 OOV 日志都照落——兜底只是兜住沉默，词表缺口照样是生长数据。"""
    res = client.post("/api/recommend", json={"query": "霍格沃茨综合征的数据", "use_llm": False})
    assert res.status_code == 200, res.text
    assert res.json()["resolution_status"] in ("vector_fallback", "abstained")
    assert oov_log.exists(), "兜底/弃权路径的 OOV 日志不变"
    lines = [json.loads(x) for x in oov_log.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert lines[0]["terms"] == ["霍格沃茨综合征"]


def test_executable_query_is_not_logged(oov_log):
    res = client.post("/api/recommend", json={"query": "人类肺组织的单细胞数据", "use_llm": False})
    assert res.status_code == 200, res.text
    assert not oov_log.exists(), "可执行查询不许落 OOV 日志"


def test_other_abstain_reasons_are_not_logged(oov_log):
    # 并列年份弃权（非未收录词）——OOV 日志只收 unresolved_term 这一档。
    # 2026-09-12 起：这类弃权不再沉默，走向量语义兜底（vector_fallback）；
    # 嵌入器不可用的环境才回退 abstained。两态都不许落 OOV 日志。
    res = client.post("/api/recommend", json={"query": "2020年和2022年的人类肺数据", "use_llm": False})
    assert res.status_code == 200, res.text
    assert res.json()["resolution_status"] in ("abstained", "vector_fallback")
    assert not oov_log.exists()


def test_degraded_search_payload_exposes_ignored_terms(oov_log):
    """自动降级路径：被忽略的词在 degraded_search.ignored_terms 里（前端/日志的真源）。"""
    res = client.post("/api/recommend", json={"query": "人类膀胱造瘘的单细胞数据", "use_llm": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["degraded_search"]["ignored_terms"] == ["造瘘"]
    assert body["degraded_search"]["active_filters"]


def test_intent_projection_exposes_unresolved_terms_on_hard_gate(oov_log):
    """硬闸弃权路径：投影仍把卡住的原词带出来（additive）。"""
    res = client.post("/api/recommend", json={"query": "霍格沃茨综合征的数据", "use_llm": False})
    assert res.status_code == 200, res.text
    intent = res.json()["interpretation"]["intent"]
    assert intent["abstain_reason"] == "unresolved_term"
    assert intent["unresolved_terms"] == ["霍格沃茨综合征"]


def test_oov_log_failure_warns_but_does_not_break_search(oov_log, monkeypatch, capsys):
    """词表日志写盘失败绝不掀翻检索，但必须留一行 stderr（含异常类型）——
    全静默 = 词表生长机制悄悄停工无从发现。"""
    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(corpus_curation, "_append_jsonl", _boom)
    res = client.post("/api/recommend", json={"query": "人类膀胱造瘘的单细胞数据", "use_llm": False})
    assert res.status_code == 200, res.text
    assert res.json()["resolution_status"] == "degraded"
    err = capsys.readouterr().err
    assert "OOV 词表日志写入失败" in err
    assert "OSError" in err


# ---------------------------------------------------------------- 生长候选报告

def _write_log(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def test_oov_report_aggregates_and_marks_catalog_hits(tmp_path):
    log = tmp_path / "oov_terms.jsonl"
    _write_log(log, [
        {"ts": "t1", "query": "人类膀胱造瘘的单细胞数据", "terms": ["造瘘"]},
        {"ts": "t2", "query": "小鼠造瘘模型", "terms": ["造瘘"]},
        {"ts": "t3", "query": "人类肺数据", "terms": ["human"]},   # 已在词表（display/alias 命中）
        {"ts": "t4", "query": "坏行不算", "terms": []},
    ])
    (tmp_path / "bad.jsonl").write_text("not json\n", encoding="utf-8")  # 噪音文件不影响
    out_base = tmp_path / "report"
    assert meg.oov_vocabulary_report(log, out_base) == 0
    payload = json.loads(out_base.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["n_events"] == 3
    terms = {r["term"]: r for r in payload["terms"]}
    assert terms["造瘘"]["count"] == 2 and terms["造瘘"]["still_oov"] is True
    assert len(terms["造瘘"]["examples"]) == 2
    assert terms["human"]["still_oov"] is False, "已进词表的词必须如实标注「已收录」"
    md = out_base.with_suffix(".md").read_text(encoding="utf-8")
    assert "造瘘" in md and "只产候选、不改词表" in md


def test_oov_report_tolerates_missing_log(tmp_path):
    out_base = tmp_path / "report"
    assert meg.oov_vocabulary_report(tmp_path / "不存在.jsonl", out_base) == 0
    payload = json.loads(out_base.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["n_events"] == 0 and payload["terms"] == []


def test_oov_report_skips_broken_lines(tmp_path):
    log = tmp_path / "oov_terms.jsonl"
    log.write_text('{"ts":"t1","query":"小鼠造瘘","terms":["造瘘"]}\n这不是 JSON\n\n', encoding="utf-8")
    out_base = tmp_path / "report"
    assert meg.oov_vocabulary_report(log, out_base) == 0
    payload = json.loads(out_base.with_suffix(".json").read_text(encoding="utf-8"))
    assert payload["n_events"] == 1
    assert payload["terms"][0]["term"] == "造瘘"
