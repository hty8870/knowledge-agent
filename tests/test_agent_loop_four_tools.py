# -*- coding: utf-8 -*-
"""环内结果处理四工具的**包装层**单测——零 LLM（compare 措辞
model=None 自动退确定性兜底）、零网络、零真实语料（`_recent_result_records` /
`_full_corpus` 用合成记录替换）。钉四条：

  1. 返回契约模型（CompareResult / CiteExportResult / CompatFindResult / FairCheckResult）
     吃真形状、拒破形状（note_zh / comparison_zh 缺失即破——execute 的形状闸按此拦）；
  2. compare.datasets 的降级路径（无结果 / 找不到 / 歧义 / 同数据集 / 只有一条可比）
     全部如实产出，不假装对比成功、不编造差异；
  3. cite.export 落盘 RIS + BibTeX **双格式**并回执路径与字节数（前端旧路径只下 .ris
     的缺口在此补上）；
  4. compat.find / fair.check 恒带诚实边界（CAVEAT_ZH / 「非官方认证」），找不到对象如实降级。
"""
from pathlib import Path

import pytest

from dataset_recommender.agent import agent_exec, agent_schemas
from dataset_recommender.retrieval.normalizer import DatasetRecord


# ---------------------------------------------------------------- 合成记录

def _rec(uid, name, **over):
    n_files = over.pop("n_files", 5)
    published_date = over.pop("published_date", "2021-03-15")
    base = dict(
        species="Human", tissue="Lung", disease="Lung cancer",
        chemistry="3p v2", count="12000", unit="cells", has_raw_data=True,
        url=f"https://example.com/{uid}", source_file="", description="",
        raw={"dataset_uid": uid, "source": "10x Genomics", "n_files": n_files,
             "published_date": published_date, "filesize": 0, "collection_doi": ""},
        platform_family="Visium", assay="spatial", modality="spatial",
    )
    base.update(over)
    return DatasetRecord(
        dataset_name=name, species=base.pop("species"), tissue=base.pop("tissue"),
        disease=base.pop("disease"), chemistry=base.pop("chemistry"),
        count=base.pop("count"), unit=base.pop("unit"),
        has_raw_data=base.pop("has_raw_data"), url=base.pop("url"),
        source_file=base.pop("source_file"), description=base.pop("description"),
        raw=base.pop("raw"), **base,
    )


REC_A = _rec("fake-a", "Human Lung Cancer Visium")
REC_B = _rec("fake-b", "Mouse Brain Xenium", species="Mouse", tissue="Brain",
             disease="", count="8000", n_files=3, published_date="2022-07-01")


@pytest.fixture
def _stub_results(monkeypatch):
    """把两个合成记录钉成「最近一批检索结果」/「全量语料」——工具只消费记录列表，
    不碰真实语料（真实行为由集成验证覆盖）。"""
    records = [REC_A, REC_B]
    monkeypatch.setattr(agent_exec, "_recent_result_records",
                        lambda ctx, root: (records, None))
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: records)
    return records


# ---------------------------------------------------------------- 返回契约模型（形状闸）

def test_compare_result_model_accepts_real_shape_and_rejects_broken():
    ok = {
        "a": {"dataset_uid": "fake-a", "dataset_name": "A", "source": "10x"},
        "b": {"dataset_uid": "fake-b", "dataset_name": "B", "source": "10x"},
        "assumption_zh": "未指定对比对象，默认取当前结果的前两条进行对比。",
        "fields": [{"field": "species", "label_zh": "物种", "a": "Human", "b": "Mouse",
                    "status": "different"}],
        "n_same": 1, "n_diff": 1, "n_unknown": 0, "identical": False,
        "comparison_zh": "A 与 B 对比：1 个字段一致、1 个字段不同。",
        "wording_source": "deterministic", "degraded": False, "degrade_reason": "",
        "caveat_zh": "",
    }
    agent_schemas.CompareResult.model_validate(ok)
    broken = dict(ok)
    del broken["comparison_zh"]  # 用户可见结论恒在——缺了即形状破
    with pytest.raises(Exception):
        agent_schemas.CompareResult.model_validate(broken)


def test_cite_export_result_model_accepts_real_shape_and_rejects_broken():
    ok = {
        "n_datasets": 2,
        "uids": ["fake-a", "fake-b"],
        "files": [{"filename": "reused-public-datasets-20260818.ris", "format": "ris",
                   "bytes": 100},
                  {"filename": "reused-public-datasets-20260818.bib", "format": "bibtex",
                   "bytes": 120}],
        "out_dir": "C:/x/.userdata/citations",
        "note_zh": "已导出 2 个数据集的引文（RIS+BibTeX），文件在 …。",
    }
    agent_schemas.CiteExportResult.model_validate(ok)
    with pytest.raises(Exception):
        broken = dict(ok)
        del broken["note_zh"]  # 回执句键恒在——缺键即形状破
        agent_schemas.CiteExportResult.model_validate(broken)


def test_compat_find_result_model_accepts_real_shape_and_rejects_broken():
    ok = {
        "seed": {"dataset_uid": "fake-a", "dataset_name": "A", "source": "10x"},
        "criteria": {"species": ["human"], "chemistry": "3p v2", "platform_family": "Visium"},
        "total": 1,
        "compatible": [{"dataset_uid": "fake-b", "dataset_name": "B"}],
        "caveat": "「元数据兼容」只表示…",
        "note_zh": "已按「A」的元数据找到 1 个兼容数据集。",
        "degraded": False, "degrade_reason": "",
    }
    agent_schemas.CompatFindResult.model_validate(ok)
    with pytest.raises(Exception):
        broken = dict(ok)
        del broken["note_zh"]
        agent_schemas.CompatFindResult.model_validate(broken)


def test_fair_check_result_model_accepts_real_shape_and_rejects_broken():
    ok = {
        "dataset_name": "A", "source": "10x Genomics",
        "fair": {"checks": [], "summary": {"pass": 8, "partial": 3, "unknown": 2,
                                           "total": 13, "readiness_pct": 73},
                 "gaps": []},
        "data_availability": {"statement": "…", "missing": [], "notes": ""},
        "note_zh": "「A」的 FAIR 复用就绪度：73%…",
        "degraded": False, "degrade_reason": "",
    }
    agent_schemas.FairCheckResult.model_validate(ok)
    with pytest.raises(Exception):
        broken = dict(ok)
        del broken["note_zh"]
        agent_schemas.FairCheckResult.model_validate(broken)


# ---------------------------------------------------------------- compare.datasets 降级路径

def test_compare_degrades_honestly_without_results_or_identifiers(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_recent_result_records", lambda ctx, root: (None, None))
    out = agent_exec._loop_compare_datasets({}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "no_results"
    assert "没有可对比的检索结果" in out["comparison_zh"]


def test_compare_degrades_when_identifier_not_found(_stub_results, tmp_path):
    out = agent_exec._loop_compare_datasets({"a": "ghost-uid", "b": "ghost-uid2"},
                                            tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "not_found_a"
    assert "找不到" in out["comparison_zh"]


def test_compare_degrades_when_identifier_ambiguous(monkeypatch, tmp_path):
    dup = _rec("fake-dup", "Human Lung Cancer Visium")  # 与 REC_A 同名不同 uid
    monkeypatch.setattr(agent_exec, "_recent_result_records",
                        lambda ctx, root: (None, None))   # 无当前结果 → 走全量语料定位
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: [REC_A, dup])
    out = agent_exec._loop_compare_datasets({"a": "Human Lung Cancer Visium", "b": "fake-b"},
                                            tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "ambiguous_a"
    assert "命中多条同名" in out["comparison_zh"]


def test_compare_degrades_when_same_dataset(_stub_results, tmp_path):
    out = agent_exec._loop_compare_datasets({"a": "fake-a", "b": "fake-a"}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "same_dataset"
    assert "同一个数据集" in out["comparison_zh"]


def test_compare_degrades_when_only_one_result(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_recent_result_records",
                        lambda ctx, root: ([REC_A], None))
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: [REC_A])
    out = agent_exec._loop_compare_datasets({}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "only_one_result_b"
    assert "只有一条可比" in out["comparison_zh"]


# ---------------------------------------------------------------- compare.datasets 成功路径

def test_compare_success_defaults_to_first_two_and_states_the_assumption(_stub_results, tmp_path):
    out = agent_exec._loop_compare_datasets({}, tmp_path, {})
    assert out["degraded"] is False
    assert out["a"]["dataset_uid"] == "fake-a" and out["b"]["dataset_uid"] == "fake-b"
    assert "默认取当前结果的前两条" in out["assumption_zh"]
    # 确定性措辞（model=None 退兜底）：数字与事实与 diff 一致
    assert out["wording_source"] == "deterministic"
    assert str(out["n_diff"]) in out["comparison_zh"]
    # 事实层字段逐项在
    species_field = next(f for f in out["fields"] if f["field"] == "species")
    assert species_field["status"] == "different"
    assert species_field["a"] == "Human" and species_field["b"] == "Mouse"


def test_compare_uses_named_identifiers_without_assumption(_stub_results, tmp_path):
    out = agent_exec._loop_compare_datasets({"a": "fake-a", "b": "fake-b"}, tmp_path, {})
    assert out["degraded"] is False
    assert out["assumption_zh"] == ""


def test_compare_identical_fields_is_an_honest_conclusion(monkeypatch, tmp_path):
    same = _rec("fake-c", "Human Lung Cancer Visium")  # 与 REC_A 同名、全部字段同值（仅 uid 不同）
    monkeypatch.setattr(agent_exec, "_recent_result_records",
                        lambda ctx, root: ([REC_A, same], None))
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: [REC_A, same])
    out = agent_exec._loop_compare_datasets({}, tmp_path, {})
    assert out["degraded"] is False
    assert out["identical"] is True
    assert "完全相同" in out["comparison_zh"]


# ---------------------------------------------------------------- cite.export（双格式落盘 + 回执）

def test_cite_export_writes_ris_and_bibtex_and_receipt(_stub_results, tmp_path):
    out = agent_exec._loop_cite_export({}, tmp_path, {})
    assert out["n_datasets"] == 2
    assert {f["format"] for f in out["files"]} == {"ris", "bibtex"}
    assert all(f["bytes"] > 0 for f in out["files"])
    out_dir = Path(out["out_dir"])
    assert out_dir == tmp_path / ".userdata" / "citations"
    for f in out["files"]:
        target = out_dir / f["filename"]
        assert target.is_file() and target.stat().st_size == f["bytes"]
    assert "RIS 与 BibTeX" in out["note_zh"]
    # 内容抽查：RIS 是 TY-DATA、BibTeX 是 @misc（数据集条目，不是论文条目）
    ris_text = (out_dir / out["files"][0]["filename"]).read_text(encoding="utf-8")
    bib_text = (out_dir / out["files"][1]["filename"]).read_text(encoding="utf-8")
    assert "TY  - DATA" in ris_text
    assert "@misc{" in bib_text and "@article" not in bib_text


def test_cite_export_honest_when_no_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_recent_result_records", lambda ctx, root: (None, None))
    out = agent_exec._loop_cite_export({}, tmp_path, {})
    assert out["n_datasets"] == 0 and out["files"] == []
    assert "没有可导出的检索结果" in out["note_zh"]


def test_cite_export_respects_limit_slot(_stub_results, tmp_path):
    out = agent_exec._loop_cite_export({"limit": 1}, tmp_path, {})
    assert out["n_datasets"] == 1 and out["uids"] == ["fake-a"]


# ---------------------------------------------------------------- compat.find（caveat 恒带）

def test_compat_find_success_carries_caveat_and_degrades_cleanly(monkeypatch, tmp_path):
    REC_C = _rec("fake-c", "Another Human Lung")  # 与 REC_A 共享 Human + Visium/3p v2 → 兼容
    monkeypatch.setattr(agent_exec, "_recent_result_records",
                        lambda ctx, root: ([REC_A, REC_B, REC_C], None))
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: [REC_A, REC_B, REC_C])
    out = agent_exec._loop_compat_find({}, tmp_path, {})
    assert out["degraded"] is False
    assert out["total"] >= 1
    assert "必要非充分" in out["caveat"]            # 诚实边界恒带
    assert "已按" in out["note_zh"] and "兼容" in out["note_zh"]
    assert "默认取当前结果第一条" in out["note_zh"]   # 缺省假设如实说明
    # 每个兼容项都带凭据
    for c in out["compatible"]:
        assert c.get("_compat_basis")


def test_compat_find_degrades_when_no_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_recent_result_records", lambda ctx, root: (None, None))
    out = agent_exec._loop_compat_find({}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "no_results"
    assert "没有可用的检索结果" in out["note_zh"]
    assert out["caveat"]   # 降级也带边界句


def test_compat_find_degrades_when_identifier_not_found(_stub_results, tmp_path):
    out = agent_exec._loop_compat_find({"uid": "ghost-uid"}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "not_found"
    assert "找不到" in out["note_zh"]


# ---------------------------------------------------------------- fair.check（就绪度 + 边界句）

def test_fair_check_success_reports_readiness_with_boundary(_stub_results, tmp_path):
    out = agent_exec._loop_fair_check({}, tmp_path, {})
    assert out["degraded"] is False
    assert out["dataset_name"] == "Human Lung Cancer Visium"
    summary = out["fair"].get("summary") or {}
    assert summary.get("total") == 13
    # 边界句：复用者视角就绪度，不是官方 FAIR 认证
    assert "复用者视角" in out["note_zh"]
    assert "不是官方 FAIR 认证" in out["note_zh"]
    assert "默认取当前结果第一条" in out["note_zh"]
    # 数据可用性声明在
    assert out["data_availability"].get("statement")


def test_fair_check_degrades_when_no_results(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_recent_result_records", lambda ctx, root: (None, None))
    out = agent_exec._loop_fair_check({}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "no_results"
    assert "无法做 FAIR 自检" in out["note_zh"]


def test_fair_check_degrades_when_identifier_not_found(_stub_results, tmp_path):
    out = agent_exec._loop_fair_check({"uid": "ghost-uid"}, tmp_path, {})
    assert out["degraded"] is True and out["degrade_reason"] == "not_found"
    assert "找不到" in out["note_zh"]
