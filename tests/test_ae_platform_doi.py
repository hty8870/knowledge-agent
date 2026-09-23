# -*- coding: utf-8 -*-
"""AE 适配器 platform / publication-DOI 抽取的**确定性**回归网（无网络）。

病根：AE 单细胞记录的平台家族**写在详情的 protocol `Description`** 里
（"Libraries were constructed using the Smart-seq3xpress protocol" / "10x Genomics Chromium" /
Software=cellranger），旧版 platform 路径只看 `title+content`、从不读详情自由文本 → 覆盖 3.1%。
publication DOI 写在 `DOI` 属性 / DOI 类型 link 里，旧版根本不抽 → 0%。

本测试用**贴合真实结构的合成详情**（section→subsections→attributes，字段名与真实 BioStudies 一致，
取值取自 probe 实测的真句子）钉死抽取逻辑：无网络即可证明修复正确。真数据全量复验在 enrichment
运行时打印覆盖率。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
for p in (str(SCRIPTS), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import ingest_arrayexpress as ae  # noqa: E402


def _detail(*protocol_descs: str, software: str = "", study_type: str = "",
            title: str = "", dois: "list[str] | None" = None,
            doi_links: "list[str] | None" = None) -> dict:
    """构造贴合真实 BioStudies 结构的详情 dict。"""
    proto_subs = []
    for d in protocol_descs:
        attrs = [{"name": "Description", "value": d}]
        if software:
            attrs.append({"name": "Software", "value": software})
        proto_subs.append({"type": "Protocol", "attributes": attrs})
    pub_subs = []
    for doi in (dois or []):
        pub_subs.append({"type": "Publication", "attributes": [
            {"name": "Title", "value": "Some paper"},
            {"name": "DOI", "value": doi},
        ]})
    section_attrs = []
    if title:
        section_attrs.append({"name": "Title", "value": title})
    if study_type:
        section_attrs.append({"name": "Study type", "value": study_type})
    links = []
    for u in (doi_links or []):
        links.append({"url": u, "attributes": [{"name": "Type", "value": "DOI"}]})
    # 再塞一个非 DOI link（ENA），确保不误采
    links.append({"url": "ERP138392", "attributes": [{"name": "Type", "value": "ENA"}]})
    return {"section": {"attributes": section_attrs,
                        "subsections": proto_subs + pub_subs,
                        "links": links}}


# ---------------------------------------------------------------- classify_platform

def test_platform_smartseq_from_protocol_description():
    """E-MTAB-11452 实况：平台只在 protocol Description 里（"Smart-seq3xpress protocol"）。"""
    d = _detail("Libraries were constructed using the Smart-seq3xpress protocol using either KAPA...",
                software="zUMIs", study_type="RNA-seq of coding RNA from single cells")
    text = ae._collect_platform_text(d["section"])
    assert "Smart-seq3xpress" in text
    assert ae.classify_platform("Single cell RNA sequencing of X", "", text) == "Smart-seq"


def test_platform_chromium_from_10x_and_cellranger():
    d = _detail("Single cells were captured using the 10x Genomics Chromium platform.",
                software="cellranger-6.0.0")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == "Chromium"


def test_platform_multi_family_empty_even_with_synonym_rich_family():
    """一个多别名家族（Chromium=chromium+10x genomics）**不得**靠别名冗余
    压过真正共提及的单别名平台。"10x Genomics Chromium and Smart-seq2" → 2 家族出现 → 留空（不 overclaim
    单一 Chromium）。旧频次法会算 Chromium 2 > Smart-seq 1 而错标 Chromium。"""
    d = _detail("Libraries were prepared using both 10x Genomics Chromium and the Smart-seq2 protocol.")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == ""


def test_platform_single_family_many_synonyms_still_labeled():
    """同一家族的多种写法仍只算它出现一次 → 纯 10x（chromium + 10x genomics + cellranger）→ Chromium。"""
    d = _detail("Single cells captured on the 10x Genomics Chromium and processed with cellranger.")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == "Chromium"


def test_platform_true_tie_returns_empty():
    """2+ 家族命中数并列最高 → **留空**（不 join、不谎报多平台）。keyword 计数区分不了真并列与对照提及。"""
    d = _detail("We profiled cells with Drop-seq and separately with 10x genomics.")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == ""


def test_platform_single_alias_incidental_no_overclaim():
    """单别名平台的对照提及："unlike Drop-seq, we used Smart-seq2" → 各 1 命中 → 留空，
    **绝不**误标成 "Smart-seq, Drop-seq"（验证：单别名板式平台的 1-1 平局 overclaim）。"""
    d = _detail("Unlike Drop-seq, we used the Smart-seq2 protocol for full-length coverage.")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == ""


def test_platform_strt_not_double_counted():
    """STRT-seq 单一模式：与一个共提及平台各 1 命中 → 平局留空（旧版两条正则会让 STRT 双计而错胜）。"""
    d = _detail("We compared STRT-seq with the Smart-seq2 protocol.")
    text = ae._collect_platform_text(d["section"])
    assert ae.classify_platform("", "", text) == ""


def test_platform_spatial_visium():
    d = _detail("Visium Spatial Gene Expression slides were used.")
    assert ae.classify_platform("", "", ae._collect_platform_text(d["section"])) == "Visium"


def test_platform_empty_when_no_keyword():
    """无任何平台关键词 → 空（诚实：不猜）。"10x magnification" 这类裸 10x 不算。"""
    d = _detail("Samples were imaged at 10x magnification and dissociated enzymatically.")
    assert ae.classify_platform("Study of tissue", "", ae._collect_platform_text(d["section"])) == ""


def test_platform_no_bare_10x_false_positive():
    assert ae.classify_platform("imaged at 10x and 40x magnification") == ""
    assert ae.classify_platform("10x coverage sequencing depth") == ""


def test_platform_classifier_pure_and_deterministic():
    """同输入同输出、无副作用。"""
    a = ae.classify_platform("smart-seq2 protocol")
    b = ae.classify_platform("smart-seq2 protocol")
    assert a == b == "Smart-seq"


# ---------------------------------------------------------------- extract_publication_doi

def test_doi_from_publication_attribute():
    d = _detail("10x Genomics Chromium.", dois=["10.1038/sdata.2018.13"])
    assert ae.extract_publication_doi(d) == "10.1038/sdata.2018.13"


def test_doi_from_doi_typed_link():
    d = _detail("Chromium.", doi_links=["10.1101/2021.04.04.438388"])
    assert ae.extract_publication_doi(d) == "10.1101/2021.04.04.438388"


def test_doi_empty_when_unpublished():
    """未发表子集：无 DOI 属性、无 DOI link（只有 ENA link）→ 空，不编造。"""
    d = _detail("Chromium single cell.")
    assert ae.extract_publication_doi(d) == ""


def test_doi_ignores_non_doi_links():
    """ENA/Biostudies link 的 url（ERP.../E-MTAB-...）不是 DOI，绝不误采。"""
    d = _detail("Chromium.")  # 只有 ENA link
    assert ae.extract_publication_doi(d) == ""


def test_doi_none_detail_safe():
    assert ae.extract_publication_doi(None) == ""
    assert ae.extract_publication_doi({}) == ""


def test_doi_dedup_attribute_and_link():
    """同一 DOI 既在属性又在 link → 去重返回一个。"""
    d = _detail("Chromium.", dois=["10.1101/x.2022"], doi_links=["10.1101/x.2022"])
    assert ae.extract_publication_doi(d) == "10.1101/x.2022"


def test_doi_does_not_merge_concatenated():
    """两个拼接 DOI（分号隔）→ 只取第一个合法 DOI，不合并成畸形 token（正则字符类不含 `;`）。"""
    d = _detail("Chromium.", dois=["10.1038/a;10.1101/b"])
    assert ae.extract_publication_doi(d) == "10.1038/a"


def test_doi_strips_trailing_period():
    d = _detail("Chromium.", dois=["10.1038/x."])
    assert ae.extract_publication_doi(d) == "10.1038/x"


# ---------------------------------------------------------------- surgical enrichment 流程

def test_enrich_is_surgical_and_monotonic(tmp_path, monkeypatch):
    """enrich_existing_snapshot：**只**改 platform + collection_doi，其余字段一字不动；
    抓取失败/新识别空但旧有值 → 保留旧 platform（单调）；记录数/UID 集不变。"""
    import json

    snap = {
        "source": "ArrayExpress", "record_count": 3,
        "records": [
            # r0: platform 空 → 应补上 Smart-seq + DOI
            {"dataset_uid": "ae:E-A", "dataset_name": "S", "platform": "", "collection_doi": "",
             "tissue": "lung", "disease": "", "species": "Human", "description": "orig-desc-A"},
            # r1: 旧有 Chromium，但本次详情抓取失败 → 必须保留 Chromium（单调）
            {"dataset_uid": "ae:E-B", "dataset_name": "S", "platform": "Chromium", "collection_doi": "",
             "tissue": "brain", "disease": "cancer", "species": "Mouse", "description": "orig-desc-B"},
            # 旧空、详情无平台关键词 → 仍空（不猜），无 DOI
            {"dataset_uid": "ae:E-C", "dataset_name": "S", "platform": "", "collection_doi": "",
             "tissue": "", "disease": "", "species": "Human", "description": "orig-desc-C"},
            # r3: 旧有 Chromium，详情**抓取成功但无平台关键词** → 新识别为空 → 必须保留 Chromium
            # （验证：这条单调红线原先只在抓取失败路径被覆盖，成功-但-空分类未测）。
            {"dataset_uid": "ae:E-D", "dataset_name": "S", "platform": "Chromium", "collection_doi": "10.9/old",
             "tissue": "gut", "disease": "", "species": "Human", "description": "orig-desc-D"},
        ],
    }
    out = tmp_path / "arrayexpress.json"
    out.write_text(json.dumps(snap, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(ae, "OUT_PATH", out)

    def fake_detail(acc):
        if acc == "E-A":
            return _detail("Smart-seq2 full-length protocol.", dois=["10.1000/aaa"])
        if acc == "E-B":
            return None  # 抓取失败
        if acc == "E-C":
            return _detail("Cells dissociated and imaged at 10x magnification.")  # 无平台
        if acc == "E-D":
            return _detail("Cells were dissociated and stained.")  # 抓取成功但无平台关键词
        return None
    monkeypatch.setattr(ae, "fetch_detail", fake_detail)

    rc = ae.enrich_existing_snapshot(sleep=0.0)
    assert rc == 0

    result = json.loads(out.read_text(encoding="utf-8"))
    recs = {r["dataset_uid"]: r for r in result["records"]}
    assert len(result["records"]) == 4                       # 记录数不变
    assert set(recs) == {"ae:E-A", "ae:E-B", "ae:E-C", "ae:E-D"}   # UID 集不变
    # r3: 抓取成功但新识别平台为空 → 保留旧 Chromium（单调，成功-但-空分类路径）
    assert recs["ae:E-D"]["platform"] == "Chromium"
    assert recs["ae:E-D"]["collection_doi"] == "10.9/old"    # DOI 单调：新空不抹旧值
    # r0: 补 platform + doi
    assert recs["ae:E-A"]["platform"] == "Smart-seq"
    assert recs["ae:E-A"]["collection_doi"] == "10.1000/aaa"
    assert recs["ae:E-A"]["tissue"] == "lung"                # 其余字段不动
    assert recs["ae:E-A"]["description"] == "orig-desc-A"
    # r1: 抓取失败 → 保留旧 Chromium（单调，绝不抹掉）
    assert recs["ae:E-B"]["platform"] == "Chromium"
    assert recs["ae:E-B"]["disease"] == "cancer"
    # 无平台关键词 → 仍空（不猜）
    assert recs["ae:E-C"]["platform"] == ""
    assert recs["ae:E-C"]["collection_doi"] == ""
    # 备份已生成
    assert out.with_suffix(".pre-n14.bak").exists()
