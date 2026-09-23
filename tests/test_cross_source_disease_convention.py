# -*- coding: utf-8 -*-
"""跨源 disease 口径不变量：四库统一「健康态写入 disease="normal"」。

背景：四库曾有两套相反口径——
CELLxGENE/SCEA 把源库显式声明的健康态写成 `disease="normal"`（已知事实），ArrayExpress/HCA 把
健康态抹成空串（假装不知道）。诚实层以「字段是否已标注」判定可核验性，于是同一根因产生三个症状：

    ① 搜「健康人的肺组织数据」：AE/HCA 恒 0 命中（答案本来可知，假阴性；HCA 正是健康参考图谱）；
    ② `coverage_caveats` 把「已知健康」误报成「未标注、无法核验」（把「知道」讲成「不知道」）；
    ③ `lenient`（也纳入未标注的）把已知健康当真·未标注放行 → 搜疾病时误纳真负。

方向 1 修复后：AE/HCA 适配器把健康态标签（normal/healthy/control）统一写成
规范 token `"normal"`；disease 为空**只**表示「源库确实没标注」。本文件钉死：
  - 适配器口径（合成数据，无网络）；
  - 跨四库口径不变量（真实快照）：健康态只以 "normal" 出现、且被诚实层视为「已标注」；
  - 三症状在真实快照上消解（修复前实测：AE 0 命中/捞回 9、HCA 0 命中/捞回 17）；
  - 「caveat 计数 == lenient 新增数」不变量在健康查询上成立。
"""
from __future__ import annotations

import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest  # noqa: E402

import ingest_arrayexpress as ae  # noqa: E402
import ingest_hca as hca  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.normalizer import (  # noqa: E402
    is_missing_value,
    normalize_dataset_record,
)
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.retrieval.retriever import (  # noqa: E402
    DatasetRetriever,
    _dim_field_present,
    _dim_value_set_complete,
    passes_hard_filter,
)

EXTERNAL = ROOT / "database" / "external"
SNAPSHOTS = {
    "CELLxGENE Discover": "cellxgene.json",
    "EBI Single Cell Expression Atlas": "ebi_scea.json",
    "ArrayExpress": "arrayexpress.json",
    "Human Cell Atlas": "hca.json",
}


def _load(name: str):
    path = EXTERNAL / name
    if not path.exists():
        pytest.skip(f"{name} 未生成")
    data = json.loads(path.read_text(encoding="utf-8"))
    return [normalize_dataset_record(r, name) for r in data["records"]]


def _healthy_query():
    intent = parse_query("健康人的肺组织数据", get_settings().keyword_mapping)
    if intent.abstain or intent.constraints.get("disease") != ["healthy", "normal"]:
        pytest.skip("词表未把「健康」解析成 disease=[healthy,normal] 约束 → 本断言前提不成立")
    return intent


# ------------------------------------------------------------ 适配器口径（合成，无网络）


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("normal", "normal"),          # 源库显式健康 → 规范 token
        ("Healthy", "normal"),         # 大小写/别名无关，统一成 normal
        ("control", "normal"),
        ("normal tissue", "normal"),   # 多词健康标记（首词判定）
        ("colorectal cancer", "colorectal cancer"),  # 真实疾病原文保留
        ("N/A", None),                 # NA 占位 → 丢弃（空 = 真未标注）
        ("not applicable", None),
        ("", None),
    ],
)
def test_ae_canonical_disease_mapping(raw, expected):
    assert ae._canonical_disease(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("normal", "normal"),
        ("healthy", "normal"),
        ("control", "normal"),
        ("COVID-19", "COVID-19"),
        ("none", None),   # 旧口径把 "none" 也当健康抹掉；新口径它是 NA 占位 → 丢弃，不冒充健康
        ("", None),
    ],
)
def test_hca_canonical_disease_mapping(raw, expected):
    assert hca._canonical_disease(raw) == expected


def test_ae_to_record_mixed_cohort_keeps_normal_and_disease():
    """混合队列（健康 + 疾病）两个事实都写入；多个健康标签去重成一个 normal。"""
    detail = {"section": {"attributes": [{"name": "Organism", "value": "Homo sapiens"}],
                          "subsections": [{"type": "Samples", "attributes": [
                              {"name": "Disease", "value": "normal"},
                              {"name": "Disease", "value": "healthy"},
                              {"name": "Disease", "value": "colorectal cancer"},
                          ]}]}}
    hit = {"title": "T", "accession": "E-XXXX-1", "content": "", "release_date": "2024-01-01"}
    assert ae.to_record(hit, detail)["disease"] == "normal, colorectal cancer"


def test_hca_to_record_healthy_only_writes_normal():
    hit = {"projects": [{"projectTitle": "P", "projectId": "p1"}],
           "samples": [{"disease": ["normal"], "organ": ["lung"]}],
           "donorOrganisms": [{"genusSpecies": ["Homo sapiens"], "disease": []}]}
    assert hca.to_record(hit)["disease"] == "normal"


# ------------------------------------------------------------ 跨四库口径不变量（真实快照）


@pytest.mark.parametrize("name", list(SNAPSHOTS.values()))
def test_healthy_state_only_appears_as_canonical_normal(name):
    """健康态在四库中**只**以规范 token "normal" 出现——不再有 healthy/control 字面量
    （否则分面碎裂、跨源口径再次分裂）。这是口径统一批点名的「当时没有任何测试覆盖跨源口径」的不变量。"""
    bad = []
    for r in _load(name):
        for part in (r.disease or "").split(","):
            tok = part.strip().lower().split(" ")[0] if part.strip() else ""
            if tok in ("healthy", "control"):
                bad.append((r.raw.get("dataset_uid"), part))
    assert not bad, f"{name} 含非规范健康态字面量: {bad[:3]}"


@pytest.mark.parametrize("name", list(SNAPSHOTS.values()))
def test_every_source_has_explicit_normal_records(name):
    """四库都有显式写入 "normal" 的记录（统一后 AE/HCA 不再是 0）。"""
    n = sum(1 for r in _load(name) if "normal" in (r.disease or "").lower())
    assert n > 0, f"{name} 没有 disease=normal 的记录——口径统一未生效？"


@pytest.mark.parametrize("name", list(SNAPSHOTS.values()))
def test_normal_is_treated_as_annotated_by_honesty_layer(name):
    """"normal" 必须被诚实层视为「已标注」（不是缺失哨兵）——否则 caveat 仍会把已知健康
    误报成「无法核验」（症状②回潮）。注意 SCEA 的第三态：抽样记录（complete=False）是
    「已标注但值集不完整」，`_dim_field_present` 按设计为 False——本断言只钉值集完整的情形。"""
    for r in _load(name):
        if "normal" in (r.disease or "").lower():
            assert not is_missing_value(r.disease), "normal 被误判成缺失哨兵"
            if _dim_value_set_complete(r, "disease"):
                assert _dim_field_present(r, "disease") is True
            return
    pytest.skip(f"{name} 无 normal 记录")


# ------------------------------------------------------------ 三症状在真实快照上消解


@pytest.mark.parametrize("name", ["arrayexpress.json", "hca.json"])
def test_symptom1_healthy_query_no_longer_constant_zero(name):
    """症状①：搜「健康人的肺组织数据」AE/HCA 不再恒 0（修复前实测均为 0；CELLxGENE=84、SCEA=10）。"""
    intent = _healthy_query()
    hits = [r for r in _load(name) if passes_hard_filter(r, intent)]
    assert hits, f"{name} 对健康查询仍 0 命中——已知健康仍被静默判负"
    assert all("normal" in (r.disease or "").lower() or "healthy" in (r.disease or "").lower()
               for r in hits), "命中应来自显式健康标注，不是别的原因漏入"


@pytest.mark.parametrize("name", ["arrayexpress.json", "hca.json"])
def test_symptom2_caveat_never_reports_known_healthy_as_unverifiable(name):
    """症状②：caveat 的「另有 N 条未标注疾病、无法核验」里**不得**含已知健康记录
    （disease 有值即已标注；修复前 AE=9 / HCA=17 条已知健康被混入）。"""
    intent = _healthy_query()
    records = _load(name)
    cav = DatasetRetriever().coverage_caveats(records, intent)
    dcav = next((c for c in cav if c["dim"] == "disease"), None)
    if dcav is None:
        return  # 无缺口可报，更好
    # 复算 caveat 集合的构成：被计数的记录必须 disease 缺失
    other = replace(intent, constraints={d: v for d, v in intent.constraints.items() if d != "disease"})
    counted = [r for r in records
               if passes_hard_filter(r, other) and not _dim_field_present(r, "disease")]
    assert dcav["count"] <= len(counted)
    assert all(is_missing_value(r.disease) for r in counted), (
        "caveat 把已知健康（disease 有值）误报成了无法核验")


@pytest.mark.parametrize("name", ["arrayexpress.json", "hca.json"])
def test_symptom3_lenient_admits_only_truly_unannotated(name):
    """症状③：lenient 放行的必须全部是 disease 缺失的记录；已知健康（含 normal）是真负、
    不得被「也纳入未标注的」捞回。同时钉住本层的核心不变量：caveat 计数 == lenient 新增数。"""
    intent = _healthy_query()
    records = _load(name)
    strict = [r for r in records if passes_hard_filter(r, intent)]
    lenient = [r for r in records if passes_hard_filter(r, replace(intent, lenient_dims={"disease"}))]
    added = [r for r in lenient if r not in strict]
    assert added, "修复后仍应存在真·未标注记录可被 lenient 捞回（否则 lenient 形同虚设）"
    assert all(is_missing_value(r.disease) for r in added), (
        "lenient 把已知健康（disease 有值）当真·未标注放行了")
    cav = DatasetRetriever().coverage_caveats(records, intent)
    dcav = next((c for c in cav if c["dim"] == "disease"), {"count": 0})
    assert dcav["count"] == len(added), (
        f"caveat 报 {dcav['count']}、点「也纳入」实增 {len(added)}——数字必须一致")


@pytest.mark.parametrize("name", ["arrayexpress.json", "hca.json"])
def test_symptom3_disease_query_known_healthy_stays_excluded_under_lenient(name):
    """症状③的反方向：搜真实疾病（肺癌）时，已知健康记录是**真负**——strict 排除、
    lenient 也绝不捞回（修复前它们 disease 为空、会被 lenient 误纳）。"""
    intent = parse_query("人的肺癌数据", get_settings().keyword_mapping)
    if intent.abstain or not intent.constraints.get("disease"):
        pytest.skip("词表未解析出 disease 约束 → 本断言前提不成立")
    records = _load(name)
    strict = [r for r in records if passes_hard_filter(r, intent)]
    lenient = [r for r in records if passes_hard_filter(r, replace(intent, lenient_dims={"disease"}))]
    added = [r for r in lenient if r not in strict]
    known_healthy_added = [r for r in added if not is_missing_value(r.disease)
                           and "normal" in r.disease.lower()]
    assert not known_healthy_added, (
        f"lenient 误纳 {len(known_healthy_added)} 条已知健康真负: "
        f"{[r.raw.get('dataset_uid') for r in known_healthy_added[:3]]}")
