# -*- coding: utf-8 -*-
"""refine.bio 第 11 源（接入）的确定性门。**全程禁网**：corpus_net 侧在 fetch_json
接缝注入假响应，corpus_curation 侧在 _fetch_logged 接缝注入假响应，账本/限速参数走真身断言。

钉的七条：
  1. **查询构造**：关键词进 /v1/search/ 的 search= 参数；物种词表内 → organism=UPPER_SNAKE
     学名（Human→HOMO_SAPIENS），词表外 → 不加该参数（不乱猜映射）；
  2. **形状闸（fail-closed）**：缺 results 列表、条目缺 accession_code/标题 → corpus_net 如实
     parse_changed 降级 / corpus_curation 抛 CurateError；search 列表项与 experiment 详情
     两形状（platform_names/platforms、technology/technologies 变体）各自过闸；
  3. **记录映射**：uid=refinebio:<主 accession>、url=/experiments/<acc>、source=refine.bio、
     species 取原生 organism_names、count/unit=num_downloadable_samples 个 samples、
     alternate_accession 进顶层字段（标识符反查副号用）；组织/疾病/collection_doi/
     download_url 诚实 null + provenance 留痕（publication_doi 是论文 DOI 不冒充数据集 DOI）；
  4. **样本页富化**：specimen_part/disease 聚合回填、占位值过滤、has_raw 只认正面证据；
  5. **限速参数**：无官方限速文档，调用必须带 min_interval ≥ 1.0s（出口 ≤60/min）；
  6. **check 水位线**：有新增 / 无新增 / 快照缺失三种本地状态 + 网络失败如实降级 snapshot；
  7. **note 如实标注**：GEO/SRA/AE 镜像实体风险 + ES 模糊匹配弱相关 + 全库口径。
"""
import json
import urllib.parse

import pytest

from dataset_recommender.corpus import corpus_net as cn
from dataset_recommender.corpus import corpus_curation as cc

# ---------------------------------------------------------------- fixtures

#: /v1/search/ 列表项形状（验证字段）：扁平、technology 单数、platform_names。
REFINEBIO_SEARCH_PAYLOAD = {
    "count": 2,
    "next": None,
    "previous": None,
    "facets": {"technology": {"rna-seq": 2}},
    "results": [
        {"id": 41908, "title": "PyMINEr Finds Gene and Autocrine/Paracrine Networks from Human Islet scRNAseq",
         "publication_title": "PyMINEr Finds Gene and Autocrine-Paracrine Networks from Human Islet scRNA-Seq.",
         "description": "Single-cell RNA-seq of human pancreatic islets from Homo sapiens donors.",
         "technology": "RNA-SEQ", "accession_code": "SRP152577",
         "alternate_accession_code": "GSE116753", "submitter_institution": "",
         "has_publication": True, "publication_doi": "10.2337/db18-0742",
         "publication_authors": ["Tyler SR"], "pubmed_id": "30759402",
         "sample_metadata_fields": ["sex", "age", "specimen_part"],
         "platform_names": ["Illumina HiSeq 2500"],
         "platform_accession_codes": ["IlluminaHiSeq2500"],
         "organism_names": ["HOMO_SAPIENS"], "downloadable_organism_names": ["HOMO_SAPIENS"],
         "num_total_samples": 185, "num_processed_samples": 162, "num_downloadable_samples": 162,
         "source_first_published": "2019-01-26T00:00:00+00:00"},
        {"id": 41913, "title": "Defining the Transcriptional Landscape during Cytomegalovirus Latency with Single-Cell RNA Sequencing",
         "description": "MARS-seq and 10x single-cell RNA sequencing of hematopoietic cells.",
         "technology": "RNA-SEQ", "accession_code": "SRP111833",
         "alternate_accession_code": "GSE101341",
         "has_publication": True, "publication_doi": "",
         "sample_metadata_fields": ["specimen_part", "subject", "time"],
         "platform_names": ["NextSeq 500"], "organism_names": ["HOMO_SAPIENS"],
         "num_total_samples": 36, "num_processed_samples": 0, "num_downloadable_samples": 0,
         "source_first_published": "2018-02-20T00:00:00Z"},
        # ES 索引实测有重复文档（同一 accession 出现两次）→ 本地去重钉住
        {"id": 41913, "title": "Defining the Transcriptional Landscape during Cytomegalovirus Latency with Single-Cell RNA Sequencing",
         "description": "dup", "technology": "RNA-SEQ", "accession_code": "SRP111833",
         "organism_names": ["HOMO_SAPIENS"], "source_first_published": "2018-02-20T00:00:00Z"},
    ],
}

#: /v1/experiments/{accession}/ 详情形状（实测字段）：嵌套 samples/annotations、platforms/
#: technologies 复数键——与列表项的 platform_names/technology 是版本变体，映射须兼容。
REFINEBIO_DETAIL_PAYLOAD = {
    "id": 41913,
    "title": "Defining the Transcriptional Landscape during Cytomegalovirus Latency with Single-Cell RNA Sequencing",
    "description": "MARS-seq and 10x single-cell RNA sequencing of hematopoietic cells.",
    "annotations": [{"data": {"alias": "GSM2701539_r1"}}],
    "samples": [{"accession_code": "SRR5823243", "technology": "RNA-SEQ", "is_processed": False}],
    "accession_code": "SRP111833", "alternate_accession_code": "GSE101341",
    "source_database": "SRA", "source_url": "https://www.ebi.ac.uk/ena/data/view/SRP111833",
    "has_publication": True, "publication_doi": "", "pubmed_id": "29535194",
    "source_first_published": "2018-02-20T00:00:00Z",
    "organism_names": ["HOMO_SAPIENS"], "sample_metadata": ["specimen_part", "subject", "time"],
    "platforms": ["NextSeq 500"], "pretty_platforms": ["NextSeq 500 (NextSeq500)"],
    "technologies": ["RNA-SEQ"],
    "num_total_samples": 36, "num_processed_samples": 0, "num_downloadable_samples": 0,
}

#: samples 端点一页（富化用）：specimen_part/disease 原生取值 + has_raw 证据。
REFINEBIO_SAMPLES = [
    {"accession_code": "SRR7493776", "specimen_part": "pancreatic islet", "disease": "",
     "has_raw": True, "is_processed": True},
    {"accession_code": "SRR7493777", "specimen_part": "pancreatic islet", "disease": "N/A",
     "has_raw": True, "is_processed": True},
    {"accession_code": "SRR7493778", "specimen_part": "islet", "disease": "type 2 diabetes",
     "has_raw": False, "is_processed": True},
]


def _net_ledger(root):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_refinebio_snapshot(root, accessions):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    records = [
        {"dataset_uid": f"refinebio:{acc}", "dataset_name": f"experiment {acc}",
         "url": f"https://www.refine.bio/experiments/{acc}", "source": "refine.bio",
         "public_accession": acc}
        for acc in accessions
    ]
    (ext / "refinebio.json").write_text(
        json.dumps({"source": "refine.bio", "record_count": len(records), "records": records},
                   ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- corpus_net：查询构造与限速

def test_search_refinebio_query_construction_and_rate_limit(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or (REFINEBIO_SEARCH_PAYLOAD, 200)))
    res = cn.search_refinebio("single cell", project_root=tmp_path)
    assert res["ok"] is True
    url = seen[0]["url"]
    assert url.startswith(cn.REFINEBIO_SEARCH_API)
    assert "search=single%20cell" in url or "search=single+cell" in url
    assert "limit=20" in url
    assert seen[0]["min_interval"] >= 1.0, "无官方限速文档，出口须 ≤60 req/min（≥1s 间隔）"
    lines = _net_ledger(tmp_path)
    assert len(lines) == 1 and lines[0]["endpoint"] == cn.REFINEBIO_SEARCH_API
    assert lines[0]["http_status"] == 200


def test_search_refinebio_species_maps_to_organism_param(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (REFINEBIO_SEARCH_PAYLOAD, 200)))
    cn.search_refinebio("atlas", species="Mouse", project_root=tmp_path)
    assert "organism=MUS_MUSCULUS" in seen[0], "词表内物种必须映射 UPPER_SNAKE 学名进 organism 参数"
    cn.search_refinebio("atlas", species="Lepidoptera", project_root=tmp_path)
    assert "organism=" not in seen[1], "词表外物种不加 organism 参数（不乱猜映射）"


# ---------------------------------------------------------------- corpus_net：形状与 fail-closed

def test_search_refinebio_maps_items_and_dedupes(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (REFINEBIO_SEARCH_PAYLOAD, 200))
    res = cn.search_refinebio("single cell", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 2, "同一 accession 的重复 ES 文档必须去重"
    it = res["items"][0]
    assert it["accession"] == "SRP152577"
    assert it["url"] == "https://www.refine.bio/experiments/SRP152577"
    assert it["date"] == "2019-01-26"
    assert it["alternate_accession_code"] == "GSE116753"
    assert it["num_downloadable_samples"] == 162
    assert "镜像" in res["note_zh"] and "模糊匹配" in res["note_zh"], "note 必须如实标注镜像与弱相关"


@pytest.mark.parametrize("bad", [
    {},                                                            # 顶层缺 results
    {"results": "not-a-list"},                                     # results 非列表
    {"results": [{"title": "x"}]},                                 # 缺 accession_code
    {"results": [{"accession_code": "SRP1"}]},                     # 缺 title
    {"results": ["not-a-dict"]},                                   # 条目非 dict
])
def test_search_refinebio_shape_drift_fail_closed(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (bad, 200))
    res = cn.search_refinebio("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed", f"漂移必须 fail-closed：{bad}"
    assert "人工核对" in res["note_zh"]


def test_search_refinebio_empty_query_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda *a, **kw: pytest.fail("空关键词不该发请求"))
    res = cn.search_refinebio("", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "empty_query"
    assert _net_ledger(tmp_path) == []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("HTTP 503（…）")))
    res2 = cn.search_refinebio("x", project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "network_error"
    assert len(_net_ledger(tmp_path)) == 1, "失败也要记账本"


def test_search_online_source_dispatches_refinebio(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (REFINEBIO_SEARCH_PAYLOAD, 200))
    for alias in ("refinebio", "refine.bio", "refine bio"):
        res = cn.search_online_source(alias, "single cell", project_root=tmp_path)
        assert res["ok"] is True and res["items"][0]["accession"] == "SRP152577", alias


# ---------------------------------------------------------------- corpus_net：check_updates 最近条目出口

def test_refinebio_recent_items_uses_published_ordering(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or (REFINEBIO_SEARCH_PAYLOAD, 200)))
    res = cn.refinebio_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True and len(res["items"]) == 2
    assert "ordering=-source_first_published" in seen[0]["url"]
    assert seen[0]["min_interval"] >= 1.0
    assert "全库" in res["note_zh"], "全库口径必须如实写明"


def test_refinebio_recent_items_drift_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"facets": {}}, 200))
    res = cn.refinebio_recent_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed"
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("URLError: boom")))
    res2 = cn.refinebio_recent_items(project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "network_error"


# ---------------------------------------------------------------- corpus_curation：check 水位线

def _fake_recent(items):
    return {"ok": True, "items": items, "note_zh": "fake recent"}


_RECENT_ITEMS = [
    {"accession": "SRP152577", "title": "Human islet scRNAseq"},
    {"accession": "SRP111833", "title": "CMV latency single-cell"},
]


def test_check_updates_refinebio_new_candidates(tmp_path, monkeypatch):
    _write_refinebio_snapshot(tmp_path, ["SRP152577"])
    monkeypatch.setattr(cc.corpus_net, "refinebio_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    res = cc.check_updates(["refine.bio"], project_root=tmp_path)
    ent = res["sources"][0]
    assert ent["mode"] == "online" and ent["local_count"] == 1
    assert ent["new_count"] == 1
    assert ent["new_candidates"] == [{"accession": "SRP111833", "title": "CMV latency single-cell"}]
    assert "镜像" in ent["note_zh"] and "全库" in ent["note_zh"], "镜像与全库口径必须如实写进 note"


def test_check_updates_refinebio_nothing_new(tmp_path, monkeypatch):
    _write_refinebio_snapshot(tmp_path, ["SRP152577", "SRP111833"])
    monkeypatch.setattr(cc.corpus_net, "refinebio_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    ent = cc.check_updates(["refinebio"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "online" and ent["new_count"] == 0
    assert "都有了" in ent["note_zh"]


def test_check_updates_refinebio_snapshot_missing_all_new(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.corpus_net, "refinebio_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    ent = cc.check_updates(["refine.bio"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "online" and ent["local_count"] == 0
    assert ent["new_count"] == 2, "快照缺失 → 在线条目全是新增候选（如实报，不伪造水位线）"


def test_check_updates_refinebio_network_failure_degrades_to_snapshot(tmp_path, monkeypatch):
    _write_refinebio_snapshot(tmp_path, ["SRP152577"])
    monkeypatch.setattr(cc.corpus_net, "refinebio_recent_items",
                        lambda **kw: {"ok": False, "items": [], "error": "network_error",
                                      "note_zh": "refine.bio 官方 API 请求失败（boom）。"})
    ent = cc.check_updates(["refinebio"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "snapshot", "在线拉不到 → 如实降级 snapshot，不假装比对完成"
    assert "没能完成" in ent["note_zh"] and ent["online_recent"] is None


# ---------------------------------------------------------------- corpus_curation：入库适配器（records 富化）

def test_search_refinebio_curation_adapter_maps_records(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cc, "_fetch_logged",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or REFINEBIO_SEARCH_PAYLOAD))
    records, warnings = cc.SOURCE_ADAPTERS["refinebio"]["search"](
        "single cell", species="", limit=20, project_root=tmp_path)
    assert len(records) == 2, "重复 ES 文档去重后剩 2 条"
    assert seen[0]["min_interval"] >= 1.0
    rec = records[0]
    assert rec["dataset_uid"] == "refinebio:SRP152577"
    assert rec["source"] == "refine.bio"
    assert rec["public_accession"] == "SRP152577"
    assert rec["alternate_accession"] == "GSE116753", "副号进顶层字段（标识符反查用）"
    assert rec["url"] == "https://www.refine.bio/experiments/SRP152577"
    assert rec["species"] == "Human", "原生 organism_names HOMO_SAPIENS → Human"
    assert rec["count"] == "162" and rec["unit"] == "samples", "count=可下载处理样本数"
    assert rec["platform"] == "Illumina HiSeq 2500"
    assert rec["published_date"] == "2019-01-26"
    assert rec["collection_doi"] is None, "publication_doi 是论文 DOI，不冒充数据集 DOI"
    assert rec["tissue"] is None and rec["disease"] is None, "samples 端点未拉 → 诚实缺省"
    assert rec["download_url"] is None and rec["has_raw_data"] is None
    assert records[1]["count"] is None and records[1]["unit"] is None, "0 个可下载样本 → count 留空（未知非 0）"
    assert warnings and "镜像" in warnings[0] and "模糊匹配" in warnings[0]
    prov = rec["metadata_provenance"]["fields"]
    assert prov["tissue_disease"]["complete"] is False
    assert prov["collection_doi"]["complete"] is False


def test_search_refinebio_curation_accession_lookup_for_sync(tmp_path, monkeypatch):
    """sync_updates「按编号搜回」：query 形似 accession → GET /v1/experiments/<acc>/ 直查。"""
    seen: list[str] = []
    monkeypatch.setattr(cc, "_fetch_logged",
                        lambda url, **kw: (seen.append(url) or REFINEBIO_DETAIL_PAYLOAD))
    records, warnings = cc.SOURCE_ADAPTERS["refinebio"]["search"](
        "SRP111833", species="", limit=5, project_root=tmp_path)
    assert seen[0].endswith("/v1/experiments/SRP111833/")
    assert len(records) == 1
    rec = records[0]
    assert rec["dataset_uid"] == "refinebio:SRP111833"
    assert rec["platform"] == "NextSeq 500", "详情形状 platforms（复数键）变体也要映射"
    assert rec["species"] == "Human"


def test_search_refinebio_curation_accession_404_is_honest_empty(tmp_path, monkeypatch):
    """副号（GSE）直查 404 → 空结果 + 如实 warning（直达只认主 accession），不抛不炸。"""
    def fake_404(url, **kw):
        raise cc.CurateError("network_error", "官方来源请求失败（HTTP 404）。这个状态码不会自动重试。")
    monkeypatch.setattr(cc, "_fetch_logged", fake_404)
    records, warnings = cc.SOURCE_ADAPTERS["refinebio"]["search"](
        "GSE101341", species="", limit=5, project_root=tmp_path)
    assert records == [] and warnings and "主" in warnings[0] and "accession" in warnings[0]


def test_search_refinebio_curation_drift_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"results": [{"id": 1}]})
    with pytest.raises(cc.CurateError):
        cc.SOURCE_ADAPTERS["refinebio"]["search"]("x", species="", limit=5, project_root=tmp_path)


def test_refinebio_sample_annotations_enrichment():
    """samples 一页富化：specimen_part/disease 聚合 + 占位值过滤 + has_raw 只认正面证据。"""
    rec = cc._refinebio_to_record(REFINEBIO_SEARCH_PAYLOAD["results"][0])
    cc._refinebio_apply_sample_annotations(rec, REFINEBIO_SAMPLES)
    assert rec["tissue"] == "pancreatic islet; islet", "specimen_part 去重保序聚合（大小写敏感原样）"
    assert rec["disease"] == "type 2 diabetes", "N/A 占位值必须过滤"
    assert rec["has_raw_data"] is True, "本页有 has_raw=true → 正面证据回填 True"
    prov = rec["metadata_provenance"]["fields"]
    assert prov["tissue_disease"]["complete"] is False
    assert "positive evidence" in prov["has_raw_data"]["origin"]


def test_refinebio_sample_annotations_no_raw_evidence_stays_null():
    """本页全 has_raw=false → 仍 None（单页不是完整清单，「没见到」≠「没有」）。"""
    rec = cc._refinebio_to_record(REFINEBIO_SEARCH_PAYLOAD["results"][0])
    cc._refinebio_apply_sample_annotations(
        rec, [{"specimen_part": "", "disease": "", "has_raw": False}])
    assert rec["has_raw_data"] is None
    assert rec["tissue"] is None and rec["disease"] is None


def test_refinebio_detail_annotations_fallback():
    """详情 annotations 回退富化：sample_tissue/sample_disease 聚合；细胞类型/个体编号不冒充组织。"""
    rec = cc._refinebio_to_record(REFINEBIO_SEARCH_PAYLOAD["results"][0])
    detail = {"annotations": [
        {"data": {"sample_tissue": "Pancreatic islet", "sample_source_name": "Human 2",
                  "sample_cell_type": "beta cell", "sample_disease": "type 2 diabetes"}},
        {"data": {"sample_tissue": "N/A", "sample_title": "x"}},   # 占位值过滤
    ]}
    cc._refinebio_apply_detail_annotations(rec, detail)
    assert rec["tissue"] == "Pancreatic islet"
    assert rec["disease"] == "type 2 diabetes"
    assert "beta cell" not in (rec["tissue"] or ""), "细胞类型不冒充组织"
    prov = rec["metadata_provenance"]["fields"]
    assert "annotations" in prov["tissue_disease"]["origin"]
    # 无有效键值 → 留空，provenance 不改写
    rec2 = cc._refinebio_to_record(REFINEBIO_SEARCH_PAYLOAD["results"][1])
    cc._refinebio_apply_detail_annotations(rec2, {"annotations": [{"data": {"alias": "x"}}]})
    assert rec2["tissue"] is None and rec2["disease"] is None
    assert "samples-endpoint" in rec2["metadata_provenance"]["fields"]["tissue_disease"]["origin"] \
        or "samples endpoint" in rec2["metadata_provenance"]["fields"]["tissue_disease"]["origin"]


def test_sync_updates_refinebio_closes_loop(tmp_path, monkeypatch):
    """sync 闭环：check 发现新增 → 按主 accession 直查搜回 → 入外部库 upload_*（不落冻结基准）。"""
    _write_refinebio_snapshot(tmp_path, ["SRP152577"])
    monkeypatch.setattr(cc.corpus_net, "refinebio_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: REFINEBIO_DETAIL_PAYLOAD)
    res = cc.sync_updates(["refine.bio"], project_root=tmp_path)
    ent = res["sources"][0]
    assert ent["imported_count"] == 1, "疑似新增应自动入库 1 条"
    assert ent["filename"] and ent["filename"].startswith("upload_")
    saved = json.loads((tmp_path / "database" / "external" / ent["filename"])
                       .read_text(encoding="utf-8"))
    assert saved["records"][0]["dataset_uid"] == "refinebio:SRP111833"
    assert not (tmp_path / "database" / "base").exists(), "冻结基准一个字节都不能动"


# ---------------------------------------------------------------- 词表外物种两入口同口径、均如实提示

def test_net_outofvocab_species_is_noted(tmp_path, monkeypatch):
    """net 入口词表外物种不打 organism 过滤，但 note 必须如实写明（此前静默透过滤）。"""
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (REFINEBIO_SEARCH_PAYLOAD, 200)))
    res = cn.search_refinebio("atlas", species="white mouse", project_root=tmp_path)
    assert res["ok"] is True
    assert "organism=" not in seen[0]
    assert "white mouse" in res["note_zh"] and "没有按物种过滤" in res["note_zh"]


def test_net_outofvocab_species_no_results_also_noted(tmp_path, monkeypatch):
    """零结果时用户更要能区分「没有这个物种的数据」与「词没被认出来」。"""
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"results": []}, 200))
    res = cn.search_refinebio("atlas", species="鼠", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert "没有按物种过滤" in res["note_zh"]


def test_curation_outofvocab_species_not_fabricated(tmp_path, monkeypatch):
    """curation 入口不再把带空格的词表外词当二名法学名透传
    （"white mouse" → organism=WHITE_MOUSE 假过滤、零结果无提示）；两入口共用同一映射真源。"""
    assert cc._refinebio_organism_param("white mouse") == "", "词表外不得编造 organism 参数"
    assert cc._refinebio_organism_param("Mouse") == "MUS_MUSCULUS", "词表内映射不变"
    seen: list[str] = []
    monkeypatch.setattr(cc, "_fetch_logged",
                        lambda url, **kw: (seen.append(url) or REFINEBIO_SEARCH_PAYLOAD))
    records, warnings = cc.SOURCE_ADAPTERS["refinebio"]["search"](
        "single cell", species="white mouse", limit=20, project_root=tmp_path)
    assert "organism=" not in seen[0]
    assert any("white mouse" in w and "没有按物种过滤" in w for w in warnings), \
        "词表外物种必须在 warnings 里如实写明"
