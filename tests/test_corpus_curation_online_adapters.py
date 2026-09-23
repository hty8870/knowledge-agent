# -*- coding: utf-8 -*-
"""curate.search_online 源适配器单测（**全 mock 禁网**：`_fetch` 接缝注入假响应，
镜像 tests/test_corpus_curation.py 的既有模式）。08-06 批三源（CELLxGENE / HuBMAP / SCP）+
08-08 批两源（HCA / 10x）+ 08-07 GEO（NCBI E-utilities 两段式）。

钉死约定：
- query → URL / 请求体映射（HuBMAP 的 POST ES 体：公共边界过滤 + 动态 allowlist terms + 全文子句；
  HCA 的 genusSpecies facet filters + pagination.next 跟随；10x 的 search=/tag[species]= 参数；
  GEO 的 "GSE"[Entry Type] 枚举 + [Organism] 物种过滤 + esearch→esummary id 接龙）；
- 字段映射移植正确性与空值诚实（端点不供 → None，不猜值；SCP 清单不可用 → 文件三簇 None）；
- species 过滤口径（SCP/HuBMAP 在 backfill 反标后本地子串过滤；HCA/10x/GEO 词表内走服务端、
  词表外回退本地子串）；HuBMAP species 过滤分页补齐；零候选 → no_candidates；
- 详情失败优雅降级 + warnings 如实告知；uid 前缀（cxg:/hubmap:/scp:/hca:/10x:/geo:）；
  plan→apply token 链路；
- 全量端点 TTL 缓存（命中不联网、不记账本；失败不缓存）；来源别名解析与报错文案；
- 10x 私有 API 形状漂移 fail-closed（字段缺失/类型漂移 → network_error 如实报错，不炸链）；
- GEO 的 NCBI 红线：限速 ≤3 req/s（min_interval ≥0.34 透传 _fetch）；
- 端点核实回归钉（curl 验证）：HuBMAP 必须走 /v3/、SCP 列表必须走 site/studies、
  CELLxGENE 必须走 curation/v1/datasets。
"""
import json
import urllib.parse
from pathlib import Path

import pytest

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus.corpus_curation import CurateError


@pytest.fixture(autouse=True)
def _clear_list_cache():
    """每个用例前后清进程内 TTL 缓存，防跨用例污染（缓存键不含 query）。"""
    cc._LIST_CACHE.clear()
    yield
    cc._LIST_CACHE.clear()


def _ledger_rows(root: Path):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ----------------------------------------------------------------------------------------------
# _fetch 的 POST 扩展（mock urllib 层，验证默认 GET 逐位不变 + method/body/headers 接线）
# ----------------------------------------------------------------------------------------------

class _FakeResp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    def read(self, n=-1):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_fetch_post_extension_default_get_unchanged(monkeypatch):
    seen = []

    def _fake_urlopen(req, timeout=0):
        seen.append(req)
        return _FakeResp({"ok": True})

    monkeypatch.setattr(cc.urllib.request, "urlopen", _fake_urlopen)
    payload, status = cc._fetch("https://example.org/a")
    assert payload == {"ok": True} and status == 200
    req = seen[0]
    assert req.get_method() == "GET" and req.data is None
    assert req.get_header("User-agent") == "biodata-agent-curate/1.0"

    payload2, _ = cc._fetch(
        "https://example.org/b", method="post", body=b'{"q": 1}',
        headers={"Content-Type": "application/json", "User-Agent": "biodata-agent-ingest/1.0"},
    )
    assert payload2 == {"ok": True}
    req2 = seen[1]
    assert req2.get_method() == "POST" and req2.data == b'{"q": 1}'
    assert req2.get_header("Content-type") == "application/json"
    assert req2.get_header("User-agent") == "biodata-agent-ingest/1.0"  # 逐键覆盖默认 UA


# ----------------------------------------------------------------------------------------------
# CELLxGENE：全量拉取（TTL 缓存）+ 本地关键词过滤
# ----------------------------------------------------------------------------------------------

_CXG_LIST = [
    {   # 命中「human lung」：title 含 lung、collection 含 human（多词 AND 走连接文本）
        "dataset_id": "cxg-aaa", "title": "Lung atlas", "collection_name": "Human cell collection",
        "collection_doi": "10.1234/coll", "published_at": "2024-05-06T00:00:00+00:00",
        "cell_count": 12345, "explorer_url": "https://cellxgene.cziscience.com/e/cxg-aaa",
        "organism": [{"label": "Homo sapiens"}], "tissue": [{"label": "lung"}],
        "disease": [{"label": "normal"}], "assay": [{"label": "10x 3' v3"}],
        "assets": [
            {"filetype": "RDS", "url": "https://x/a.rds", "filesize": 10},
            {"filetype": "H5AD", "url": "https://x/a.h5ad", "filesize": 20},
        ],
    },
    {   # 只含 human 不含 lung → 多词 AND 滤掉
        "dataset_id": "cxg-bbb", "title": "Human kidney", "collection_name": "C",
        "organism": [{"label": "Homo sapiens"}], "assay": [{"label": "Smart-seq2"}],
        "assets": [{"filetype": "H5AD", "url": "https://x/b.h5ad", "filesize": 30}],
        "cell_count": 100,
    },
    {"dataset_id": "cxg-ccc", "title": "Human lung tomb", "tombstone": True,
     "assets": [{"filetype": "H5AD", "url": "https://x/c.h5ad"}]},          # tombstone → 丢弃
    {"dataset_id": "cxg-ddd", "title": "Human lung noassets", "assets": []},  # 无有效资产 → 丢弃
    {   # mouse lung → species 过滤用
        "dataset_id": "cxg-eee", "title": "Mouse lung", "collection_name": "Mouse lung collection",
        "organism": [{"label": "Mus musculus"}], "assay": [{"label": "10x 5' v2"}],
        "assets": [{"filetype": "H5AD", "url": "https://x/e.h5ad", "filesize": 40}],
        "cell_count": 500,
    },
]


def _cxg_fetch(calls):
    def _fake(url, **kwargs):
        calls.append((url, kwargs))
        return _CXG_LIST, 200
    return _fake


def test_cellxgene_query_local_filter_and_mapping(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch(calls))
    plan = cc.plan_search_online("human lung", "cellxgene", project_root=tmp_path)
    assert plan["source"] == "cellxgene" and plan["source_label"] == "CELLxGENE Discover"
    assert plan["record_count"] == 1  # 多词 AND：cxg-bbb 只有 human 被滤；ccc/ddd 被映射丢弃
    # URL/请求映射：单次 GET 全量端点，UA 沿用 ingest 风格；不附 query 参数（本地过滤）
    assert len(calls) == 1 and calls[0][0] == cc.CXG_DATASETS_API
    assert calls[0][1].get("method", "GET") == "GET"
    assert calls[0][1]["headers"]["User-Agent"].startswith("biodata-agent-ingest")
    rec = plan["candidates"][0]
    assert rec["dataset_uid"] == "cxg:cxg-aaa"
    assert rec["source"] == "CELLxGENE Discover"
    assert rec["dataset_name"] == "Lung atlas"
    assert rec["species"] == "Human"                       # 学名 → 通用名
    assert rec["tissue"] == "lung" and rec["disease"] == "normal"
    assert rec["chemistry"] == "10x 3' v3" and rec["platform"] == "Chromium"
    assert rec["count"] == "12345" and rec["unit"] == "Cells"
    assert rec["has_raw_data"] is False                    # H5AD/RDS 处理后矩阵，非原始 reads
    assert rec["download_url"] == "https://x/a.h5ad"       # 资产优选 H5AD（非 RDS）
    assert rec["filesize"] == 20
    assert rec["url"] == "https://cellxgene.cziscience.com/e/cxg-aaa"
    assert rec["published_date"] == "2024-05-06"
    assert rec["collection_doi"] == "10.1234/coll"
    assert rec["description"] == "Human cell collection · DOI: 10.1234/coll"


def test_cellxgene_species_filter_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch([]))
    plan = cc.plan_search_online("lung", "cellxgene", species="Mouse", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["dataset_uid"] == "cxg:cxg-eee"
    # limit 截断：limit=1 时两命中（aaa/eee）只留第一条
    plan1 = cc.plan_search_online("lung", "cellxgene", limit=1, project_root=tmp_path)
    assert plan1["record_count"] == 1


def test_cellxgene_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch([]))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在的关键词zzz", "cellxgene", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_cellxgene_ttl_cache_and_ledger(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch(calls))
    cc.plan_search_online("lung", "cellxgene", project_root=tmp_path)
    cc.plan_search_online("kidney", "cellxgene", project_root=tmp_path)  # 不同 query 也命中缓存
    assert len(calls) == 1                       # 第二次搜索零联网
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1                        # 缓存命中 = 没有联网 → 不记账本
    assert rows[0]["endpoint"] == cc.CXG_DATASETS_API and rows[0]["records"] == 5  # 列表形态计数
    cc._LIST_CACHE.clear()
    cc.plan_search_online("kidney", "cellxgene", project_root=tmp_path)
    assert len(calls) == 2                       # 清缓存后重新真拉


def test_cellxgene_cache_failure_not_cached(tmp_path, monkeypatch):
    calls: list = []

    def _flaky(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise CurateError("network_error", "假网络故障：首次拉取失败。")
        return _CXG_LIST, 200

    monkeypatch.setattr(cc, "_fetch", _flaky)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", "cellxgene", project_root=tmp_path)
    assert ei.value.code == "network_error"
    plan = cc.plan_search_online("lung", "cellxgene", project_root=tmp_path)  # 失败不缓存 → 真重试
    assert plan["record_count"] == 2 and len(calls) == 2


def test_cellxgene_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch([]))
    plan = cc.plan_search_online("lung", "cellxgene", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["dry_run"] is False and res["record_count"] == 2
    assert res["sources"] == {"CELLxGENE Discover": 2}
    assert "curate_cellxgene" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert [r["dataset_uid"] for r in disk["records"]] == ["cxg:cxg-aaa", "cxg:cxg-eee"]
    tampered = json.loads(json.dumps(plan))
    tampered["candidates"][0]["dataset_name"] = "被调包"
    with pytest.raises(CurateError) as ei:
        cc.apply_search_online(tampered, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert ei.value.code == "token_mismatch"


# ----------------------------------------------------------------------------------------------
# HuBMAP：POST ES 查询（聚合推导 allowlist → terms + 全文子句）
# ----------------------------------------------------------------------------------------------

_HUBMAP_AGG = {
    "hits": {"total": {"relation": "eq", "value": 5272}, "hits": []},
    "aggregations": {"dataset_types": {"buckets": [
        {"key": "CODEX", "doc_count": 128},
        {"key": "CODEX [Cytokit + SPRM]", "doc_count": 172},
        {"key": "RNAseq [Salmon]", "doc_count": 462},   # generic RNAseq → allowlist 排除
        {"key": "Histology", "doc_count": 806},          # 非白名单 → 排除
    ]}},
}
_HUBMAP_HIT_U1 = {"_source": {
    "uuid": "u1", "hubmap_id": "HBM123.ABCD.456", "registered_doi": "10.35079/hbm123",
    "dataset_type": "CODEX [Cytokit + SPRM]", "title": "CODEX data from the lung of a human donor",
    "metadata": {"description": "CODEX imaging of human lung tissue.",
                 "acquisition_instrument_model": "Keyence BZ-X800"},
    "origin_samples": [{"organ": "LG"}, {"organ": "LG"}, {"organ": None}],
    "published_timestamp": 1700000000000,
    "entity_type": "Dataset", "status": "Published", "data_access_level": "public",
}}
_HUBMAP_HIT_U2 = {"_source": {
    "uuid": "u2", "hubmap_id": "HBM789.EFGH.012", "registered_doi": None,
    "dataset_type": "CODEX", "title": "CODEX data from the kidney of a mouse donor",
    "metadata": {"description": None}, "origin_samples": [], "published_timestamp": None,
}}


def _hubmap_fetch(bodies, *, agg=None, hits=None):
    def _fake(url, **kwargs):
        assert url == cc.HUBMAP_SEARCH_API
        assert kwargs.get("method") == "POST"                      # query → POST 映射
        body = json.loads(kwargs["body"])
        bodies.append(body)
        if "aggs" in body:
            return agg if agg is not None else _HUBMAP_AGG, 200
        payload = {"hits": {"total": {"relation": "eq", "value": len(hits or [])}, "hits": hits or []}}
        return payload, 200
    return _fake


def test_hubmap_request_body_and_mapping(tmp_path, monkeypatch):
    bodies: list = []
    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch(bodies, hits=[_HUBMAP_HIT_U1, _HUBMAP_HIT_U2]))
    plan = cc.plan_search_online("lung", "hubmap", project_root=tmp_path)
    assert plan["source"] == "hubmap" and plan["source_label"] == "HuBMAP"

    agg_body, search_body = bodies
    # 聚合体：公共边界过滤 + dataset_type.keyword 分布（allowlist 精确取值动态推导）
    assert agg_body["size"] == 0
    assert agg_body["query"]["bool"]["filter"] == cc._HUBMAP_PUBLIC_FILTERS
    terms_agg = agg_body["aggs"]["dataset_types"]["terms"]
    assert terms_agg["field"] == "dataset_type.keyword"
    # 搜索体：_source 白名单 + 公共边界 + 动态 allowlist terms（RNAseq/Histology 被排除）+ 全文子句
    assert search_body["size"] == 20
    assert search_body["_source"] == cc._HUBMAP_SOURCE_FIELDS
    filters = search_body["query"]["bool"]["filter"]
    assert filters[:3] == cc._HUBMAP_PUBLIC_FILTERS
    assert filters[3] == {"terms": {"dataset_type.keyword": ["CODEX", "CODEX [Cytokit + SPRM]"]}}
    must = search_body["query"]["bool"]["must"][0]
    assert must["bool"]["minimum_should_match"] == 1
    should_fields = [list(clause["match"])[0] for clause in must["bool"]["should"]]
    assert should_fields == ["title", "description", "metadata.description"]
    assert all(c["match"][f]["operator"] == "and" and c["match"][f]["query"] == "lung"
               for c, f in zip(must["bool"]["should"], should_fields))

    assert plan["record_count"] == 2
    r1, r2 = plan["candidates"]
    assert r1["dataset_uid"] == "hubmap:u1"
    assert r1["source"] == "HuBMAP"
    assert r1["public_accession"] == "HBM123.ABCD.456"
    assert r1["collection_doi"] == "10.35079/hbm123"
    assert r1["dataset_name"] == "CODEX data from the lung of a human donor"
    assert r1["tissue"] == "LG"                                # organ code 去重、None 跳过、本地不展开
    assert r1["chemistry"] == "CODEX"                          # 「 [pipeline]」后缀 → 规范家族
    assert r1["platform"] == "Keyence BZ-X800"
    assert r1["published_date"] == "2023-11-14"                # epoch 毫秒 → UTC 日期
    assert r1["description"] == "CODEX imaging of human lung tissue."
    assert r1["url"] == "https://portal.hubmapconsortium.org/browse/dataset/u1"
    # 空值诚实：端点不供/未查询 → None（不猜值）
    assert r1["has_raw_data"] is None and r1["filesize"] is None and r1["download_url"] is None
    assert r1["count"] is None and r1["unit"] is None
    assert r1["disease"] is None
    assert r1["species"] == "Human"                            # 端点不供 → backfill 反标（留痕）
    prov = r1["metadata_provenance"]
    assert prov["backfill"]["method"] == cc.corpus_enrich.BACKFILL_METHOD
    assert "species" in prov["backfill"]["dims"]
    r2 = plan["candidates"][1]
    assert r2["species"] == "Mouse" and r2["description"] is None
    assert r2["collection_doi"] is None
    assert r2["tissue"] == "Kidney"                          # 端点供 organ 时为空 → backfill 反标
    assert r2["metadata_provenance"]["complete"] is False    # 反标 tissue → 值集不穷尽第三态
    assert r2["published_date"] is None and r2["platform"] is None
    # warnings 如实告知：反标回填 + 文件清单未查询
    assert any("反标回填" in w for w in plan["warnings"])
    assert any("未查询文件清单" in w for w in plan["warnings"])
    # 账本：聚合（records=0，ES 形态 hits.hits 计数）+ 搜索（records=2）
    rows = _ledger_rows(tmp_path)
    assert [r["records"] for r in rows] == [0, 2]
    assert rows[0]["endpoint"] == cc.HUBMAP_SEARCH_API


def test_hubmap_species_filter_after_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch([], hits=[_HUBMAP_HIT_U1, _HUBMAP_HIT_U2]))
    plan = cc.plan_search_online("肿瘤", "hubmap", species="Mouse", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["dataset_uid"] == "hubmap:u2"


def test_hubmap_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch([], hits=[]))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在zzz", "hubmap", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_hubmap_observed_types_empty_fail_closed(tmp_path, monkeypatch):
    """官方分布里 allowlist 全认不出 → 零候选如实上报，且**绝不**发起正文搜索 POST。"""
    bad_agg = {"hits": {"total": {"relation": "eq", "value": 10}, "hits": []},
               "aggregations": {"dataset_types": {"buckets": [
                   {"key": "RNAseq [Salmon]", "doc_count": 9}, {"key": "__MISSING__", "doc_count": 1}]}}}

    def _fake(url, **kwargs):
        body = json.loads(kwargs["body"])
        if "aggs" not in body:
            raise AssertionError("allowlist 为空时不应发起正文搜索")
        return bad_agg, 200

    monkeypatch.setattr(cc, "_fetch", _fake)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", "hubmap", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_hubmap_agg_ttl_cache(tmp_path, monkeypatch):
    calls: list = []

    def _fake(url, **kwargs):
        body = json.loads(kwargs["body"])
        calls.append("agg" if "aggs" in body else "search")
        if "aggs" in body:
            return _HUBMAP_AGG, 200
        return {"hits": {"total": {"relation": "eq", "value": 1}, "hits": [_HUBMAP_HIT_U1]}}, 200

    monkeypatch.setattr(cc, "_fetch", _fake)
    cc.plan_search_online("lung", "hubmap", project_root=tmp_path)
    cc.plan_search_online("kidney", "hubmap", project_root=tmp_path)
    assert calls == ["agg", "search", "search"]   # 聚合走 TTL 缓存只问一次；正文每次都真查


def test_hubmap_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch([], hits=[_HUBMAP_HIT_U1]))
    plan = cc.plan_search_online("lung", "hubmap", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 1 and res["sources"] == {"HuBMAP": 1}
    assert "curate_hubmap" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert disk["records"][0]["dataset_uid"] == "hubmap:u1"


# ----------------------------------------------------------------------------------------------
# Broad Single Cell Portal：全量列表（缓存）+ 本地过滤 + 逐条详情富化
# ----------------------------------------------------------------------------------------------

_SCP_LIST = [
    {"accession": "SCP100", "name": "Mouse lung atlas", "public": True,
     "description": "single cell RNA-seq of mouse lung"},
    {"accession": "SCP101", "name": "Human lung single file", "public": True,
     "description": "human lung cells"},
    {"accession": "SCP105", "name": "Mouse lung manifest unavailable", "public": True,
     "description": "mouse lung cells"},
    {"accession": "SCP102", "name": "Mouse lung private", "public": False,   # 非 public → 排除
     "description": "mouse lung"},
    {"accession": "XX9", "name": "Mouse lung bad accession", "public": True,  # 非法 accession → 排除
     "description": "mouse lung"},
    {"accession": "SCP103", "name": "  ", "public": True, "description": "mouse lung"},  # 空标题 → 排除
    {"accession": "SCP104", "name": "Mouse brain", "public": True, "description": "mouse brain"},
]

_SCP_DETAIL_100 = {
    "accession": "SCP100",
    "full_description": "<p><strong>Mouse lung atlas.</strong></p><p>Single cell RNA-seq &amp; more.</p>",
    "description": "fallback desc", "cell_count": 1402,
    "study_files": [
        {"name": "reads.fastq.gz", "file_type": "Fastq", "upload_file_size": 100,
         "download_url": "https://x/reads"},
        {"name": "matrix.txt", "file_type": "Cluster", "upload_file_size": 50,
         "download_url": "https://x/matrix"},
    ],
    "publications": [{"title": "t", "url": "https://doi.org/10.1126/science.aad7038"}],
}
_SCP_DETAIL_101 = {
    "accession": "SCP101", "full_description": "", "description": "Human lung cells detail",
    "cell_count": 0,
    "study_files": [
        {"name": "only.fq.gz", "file_type": "Fastq", "upload_file_size": 70,
         "download_url": "https://x/only"},
    ],
    "publications": [],
}
_SCP_DETAIL_105 = {
    "accession": "SCP105", "description": "mouse lung", "cell_count": None,
    "study_files": "Unavailable (cannot load study workspace or bucket)",   # 占位串 → 不猜值
}


def _scp_fetch(calls, *, fail_details=()):
    def _fake(url, **kwargs):
        calls.append(url)
        assert kwargs["headers"]["Accept"] == "application/json"
        if url == cc.SCP_LIST_API:
            return _SCP_LIST, 200
        for acc in fail_details:
            if url.endswith(f"/{acc}"):
                raise CurateError("network_error", f"假网络故障：{acc} 详情拉取失败。")
        if url.endswith("/SCP100"):
            return _SCP_DETAIL_100, 200
        if url.endswith("/SCP101"):
            return _SCP_DETAIL_101, 200
        if url.endswith("/SCP105"):
            return _SCP_DETAIL_105, 200
        raise AssertionError(f"未预期的详情请求：{url}")
    return _fake


def test_scp_list_filter_detail_enrich_and_mapping(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _scp_fetch(calls))
    plan = cc.plan_search_online("lung", "single_cell_portal", project_root=tmp_path)
    assert plan["source"] == "single_cell_portal" and plan["source_label"] == "Broad Single Cell Portal"
    # 列表边界（public/合法 accession/非空标题）+ 关键词过滤 → 3 候选；逐个详情富化
    assert plan["record_count"] == 3
    assert calls[0] == cc.SCP_LIST_API
    assert sorted(calls[1:]) == sorted([
        f"{cc.SCP_LIST_API}/SCP100", f"{cc.SCP_LIST_API}/SCP101", f"{cc.SCP_LIST_API}/SCP105"])
    by_uid = {r["dataset_uid"]: r for r in plan["candidates"]}

    r100 = by_uid["scp:SCP100"]
    assert r100["source"] == "Broad Single Cell Portal"
    assert r100["public_accession"] == "SCP100"
    assert r100["description"] == "Mouse lung atlas. Single cell RNA-seq & more."  # 剥 HTML + 实体反转
    assert r100["count"] == 1402 and r100["unit"] == "cells"
    assert r100["has_raw_data"] is True                      # file_type==Fastq
    assert r100["filesize"] == 150                           # 全文件字节求和
    assert r100["download_url"] is None                      # 多文件 → 不给单直链
    assert r100["url"] == "https://singlecell.broadinstitute.org/single_cell/study/SCP100"
    assert r100["collection_doi"] is None                    # 论文 DOI 只报告、不冒充
    assert r100["species"] == "Mouse"                        # 端点不供 → backfill 反标
    assert r100["published_date"] is None                    # 端点不供 → None
    assert r100["metadata_provenance"]["backfill"]["method"] == cc.corpus_enrich.BACKFILL_METHOD

    r101 = by_uid["scp:SCP101"]
    assert r101["description"] == "Human lung cells detail"  # full_description 空 → 回退 description
    assert r101["count"] is None and r101["unit"] is None    # cell_count=0 → 不猜值
    assert r101["has_raw_data"] is True                      # 文件名 .fq.gz 命中
    assert r101["filesize"] == 70 and r101["download_url"] == "https://x/only"  # 单文件且字节已知
    assert r101["species"] == "Human"

    r105 = by_uid["scp:SCP105"]
    # 清单占位串「Unavailable…」→ 文件三簇留 None（清单不可用 ≠ 没有 FASTQ，不猜值）
    assert r105["has_raw_data"] is None and r105["filesize"] is None and r105["download_url"] is None

    # warnings 如实告知：反标回填 + 论文 DOI 只报告
    assert any("反标回填" in w for w in plan["warnings"])
    assert any("论文 DOI" in w and "未写入 collection_doi" in w for w in plan["warnings"])
    # 账本：列表 1 行（records=7，顶层数组计数）+ 详情 3 行（各 records=1）
    rows = _ledger_rows(tmp_path)
    assert [r["records"] for r in rows] == [7, 1, 1, 1]


def test_scp_detail_failure_graceful_degradation(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _scp_fetch([], fail_details=("SCP101",)))
    plan = cc.plan_search_online("lung", "single_cell_portal", project_root=tmp_path)
    assert plan["record_count"] == 3                           # 详情失败不中断整体搜索
    r101 = next(r for r in plan["candidates"] if r["dataset_uid"] == "scp:SCP101")
    assert r101["description"] == "human lung cells"           # 回退列表口径
    assert r101["has_raw_data"] is None and r101["filesize"] is None  # 留空不猜值
    assert any("详情拉取失败" in w for w in plan["warnings"])
    rows = _ledger_rows(tmp_path)
    fail = next(r for r in rows if r.get("error"))
    assert fail["http_status"] is None and fail["records"] == 0


def test_scp_species_filter_after_backfill(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _scp_fetch([]))
    plan = cc.plan_search_online("lung", "single_cell_portal", species="Mouse", project_root=tmp_path)
    assert plan["record_count"] == 2
    assert {r["dataset_uid"] for r in plan["candidates"]} == {"scp:SCP100", "scp:SCP105"}


def test_scp_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _scp_fetch([]))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在zzz", "single_cell_portal", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_scp_list_ttl_cache_details_not_cached(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _scp_fetch(calls))
    cc.plan_search_online("lung", "single_cell_portal", limit=1, project_root=tmp_path)
    cc.plan_search_online("lung", "single_cell_portal", limit=1, project_root=tmp_path)
    assert calls.count(cc.SCP_LIST_API) == 1                   # 列表走缓存
    assert sum(1 for c in calls if c.endswith("/SCP100")) == 2  # 详情不缓存、每次真查


def test_scp_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _scp_fetch([]))
    plan = cc.plan_search_online("lung", "single_cell_portal", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 3 and res["sources"] == {"Broad Single Cell Portal": 3}
    assert "curate_single_cell_portal" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert all(r["dataset_uid"].startswith("scp:") for r in disk["records"])


# ----------------------------------------------------------------------------------------------
# 来源别名解析与 source_not_registered 文案
# ----------------------------------------------------------------------------------------------

def test_source_alias_resolution(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _cxg_fetch([]))
    plan = cc.plan_search_online("lung", "cellxgene discover", project_root=tmp_path)  # label 匹配
    assert plan["source"] == "cellxgene"
    plan2 = cc.plan_search_online("lung", "CXG", project_root=tmp_path)                # 别名
    assert plan2["source"] == "cellxgene"

    monkeypatch.setattr(cc, "_fetch", _scp_fetch([]))
    plan3 = cc.plan_search_online("lung", "single cell portal", project_root=tmp_path)  # 口语别名
    assert plan3["source"] == "single_cell_portal"
    plan4 = cc.plan_search_online("lung", "scp", project_root=tmp_path)
    assert plan4["source"] == "single_cell_portal"

    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch([], hits=[_HUBMAP_HIT_U1]))
    plan5 = cc.plan_search_online("lung", "HuBMAP", project_root=tmp_path)             # label 大小写
    assert plan5["source"] == "hubmap"

    monkeypatch.setattr(cc, "_fetch", _hca_fetch([]))
    plan6 = cc.plan_search_online("brain", "human cell atlas", project_root=tmp_path)  # 口语别名
    assert plan6["source"] == "hca"

    monkeypatch.setattr(cc, "_fetch", _tenx_fetch([]))
    plan7 = cc.plan_search_online("liver", "10x Genomics", project_root=tmp_path)      # label 匹配
    assert plan7["source"] == "10x"
    plan8 = cc.plan_search_online("liver", "tenx", project_root=tmp_path)              # 别名
    assert plan8["source"] == "10x"

    monkeypatch.setattr(cc, "_fetch", _geo_fetch([]))
    plan9 = cc.plan_search_online("lung", "ncbi geo", project_root=tmp_path)           # 口语别名
    assert plan9["source"] == "geo"
    plan10 = cc.plan_search_online("lung", "NCBI GEO", project_root=tmp_path)          # label 匹配
    assert plan10["source"] == "geo"


def test_unregistered_source_hint_lists_new_adapters(tmp_path, monkeypatch):
    def _boom(url, **kw):
        raise AssertionError("未注册源不应发起任何联网")

    monkeypatch.setattr(cc, "_fetch", _boom)
    # encode 在 check_updates 注册表里、但不在 SOURCE_ADAPTERS（前本例钉的是 10x；
    # 10x/HCA 该批已接入，换成仍未接搜索的 encode 继续钉「认识但没接搜索」的 fail-closed）。
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("x", source="encode", project_root=tmp_path)
    assert ei.value.code == "source_not_registered"
    hint = ei.value.hint
    assert "暂不支持联网搜索来源" in hint
    for label in ("ArrayExpress", "CELLxGENE Discover", "HuBMAP", "Broad Single Cell Portal",
                  "Human Cell Atlas", "10x Genomics", "NCBI GEO"):
        assert label in hint                       # 能联网搜的来源如实全列出（含 08-08 两新源与 GEO）
    assert "ENCODE、EBI SCEA 等" in hint    # 剩余未接来源如实（不再含 10x/HCA/GEO）
    assert _ledger_rows(tmp_path) == []            # fail-closed：零联网、零账本


# ----------------------------------------------------------------------------------------------
# HCA（Azul /index/projects）：genusSpecies facet 物种过滤 + 分页拉取 + 客户端关键词匹配
# （接入；无服务端全文检索，全库 532 项本地匹配代价可忽略）
# ----------------------------------------------------------------------------------------------

_AZUL_HIT_HUMAN = {
    "entryId": "74b6d569-3b11-42ef-b6b1-a0454522b4a0",
    "projects": [{
        "projectTitle": "Human brain atlas",
        "projectDescription": "Cortex and hippocampus\nsingle cell.",
        "estimatedCellCount": 1330000,
        "publications": [{"doi": "10.1234/paper", "publicationTitle": "p"}],
    }],
    "donorOrganisms": [{"genusSpecies": ["Homo sapiens"], "disease": ["normal"], "id": ["donor-x"]}],
    "samples": [{"effectiveOrgan": ["brain"], "organ": ["brain"]}],
    "specimens": [{"disease": [None]}],
    "protocols": [{"libraryConstructionApproach": ["10x 3' v2"]}],
    "fileTypeSummaries": [{"format": "fastq", "totalSize": 100}, {"format": "h5ad", "totalSize": 50}],
    "dates": [{"aggregateSubmissionDate": "2026-04-14T18:09:49.156048Z"}],
}
_AZUL_HIT_MOUSE = {
    "entryId": "984ce0a2-682d-47a3-b80e-1354dfe51ca3",
    "projects": [{"projectTitle": "Mouse lung atlas", "projectDescription": "mouse lung cells",
                  "estimatedCellCount": None}],
    "donorOrganisms": [{"genusSpecies": ["Mus musculus"]}],
    "samples": [{"effectiveOrgan": ["lung"]}],
    "protocols": [],
    "fileTypeSummaries": [{"format": "h5ad", "totalSize": 10}],
    "dates": [{"aggregateSubmissionDate": "2025-01-01T00:00:00.000000Z"}],
}


def _azul_payload(hits, *, next_url=None):
    return {"pagination": {"count": len(hits), "total": len(hits), "size": 75,
                           "next": next_url, "previous": None, "pages": 1},
            "termFacets": {}, "hits": hits}


def _hca_fetch(calls, *, pages=None):
    """pages=None → 单页两命中；pages={URL 片段: payload} → 简易分页路由。"""
    def _fake(url, **kwargs):
        calls.append(url)
        assert url.startswith(cc.AZUL_PROJECTS_API)
        if pages is not None:
            for key, payload in pages.items():
                if key in url:
                    return payload, 200
            raise AssertionError(f"未预期的分页请求：{url}")
        return _azul_payload([_AZUL_HIT_HUMAN, _AZUL_HIT_MOUSE]), 200
    return _fake


def test_hca_client_keyword_filter_and_mapping(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _hca_fetch(calls))
    plan = cc.plan_search_online("brain", "hca", project_root=tmp_path)
    assert plan["source"] == "hca" and plan["source_label"] == "Human Cell Atlas"
    assert plan["record_count"] == 1                       # 客户端关键词匹配：mouse lung 不命中 brain
    url = calls[0]
    assert "size=75" in url and "filters=" not in url      # 无物种时不打服务端 facet
    rec = plan["candidates"][0]
    assert rec["dataset_uid"] == "hca:74b6d569-3b11-42ef-b6b1-a0454522b4a0"
    assert rec["source"] == "Human Cell Atlas"
    assert rec["public_accession"] == "74b6d569-3b11-42ef-b6b1-a0454522b4a0"
    assert rec["dataset_name"] == "Human brain atlas"
    assert rec["species"] == "Human"                       # 学名 → 通用名
    assert rec["tissue"] == "brain" and rec["disease"] == "normal"
    assert rec["chemistry"] == "10x 3' v2" and rec["platform"] == "Chromium"
    assert rec["count"] == "1330000" and rec["unit"] == "Cells"
    assert rec["has_raw_data"] is True                     # fileTypeSummaries 含 fastq
    assert rec["filesize"] == 150                          # 各档 totalSize 求和
    assert rec["published_date"] == "2026-04-14"
    assert rec["url"] == ("https://data.humancellatlas.org/explore/projects/"
                          "74b6d569-3b11-42ef-b6b1-a0454522b4a0")
    assert rec["description"] == "Cortex and hippocampus single cell."   # 换行折叠
    # 空值诚实：论文 DOI 不冒充数据集 DOI；单文件直链端点不供
    assert rec["collection_doi"] is None and rec["download_url"] is None
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["endpoint"] == cc.AZUL_PROJECTS_API
    assert rows[0]["records"] == 2                         # hits 列表形态计数


def test_hca_species_facet_server_side(tmp_path, monkeypatch):
    calls: list = []

    def _fake(url, **kwargs):
        calls.append(url)
        if "filters=" in url:
            decoded = urllib.parse.unquote(url)
            assert '"genusSpecies":{"is":["Mus musculus"]}' in decoded   # 服务端 facet 精确过滤
            return _azul_payload([_AZUL_HIT_MOUSE]), 200
        return _azul_payload([_AZUL_HIT_HUMAN, _AZUL_HIT_MOUSE]), 200

    monkeypatch.setattr(cc, "_fetch", _fake)
    plan = cc.plan_search_online("atlas", "hca", species="Mouse", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["dataset_uid"] == "hca:984ce0a2-682d-47a3-b80e-1354dfe51ca3"


def test_hca_species_outside_vocab_falls_back_to_local_filter(tmp_path, monkeypatch):
    """词表外物种（Lepidoptera 不在 ORGANISM_COMMON）：不打服务端 facet，映射后本地子串过滤
    （AE 同口径）；滤空 → no_candidates。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _hca_fetch(calls))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("atlas", "hca", species="Lepidoptera", project_root=tmp_path)
    assert ei.value.code == "no_candidates"
    assert "filters=" not in calls[0]


def test_hca_pagination_follows_next(tmp_path, monkeypatch):
    """pagination.next 是同服务绝对 URL 才跟随；关键词命中在第二页也必须拿到。"""
    calls: list = []
    pages = {
        "size=75": _azul_payload([], next_url=cc.AZUL_PROJECTS_API + "?search_after=abc"),
        "search_after=abc": _azul_payload([_AZUL_HIT_MOUSE]),
    }
    monkeypatch.setattr(cc, "_fetch", _hca_fetch(calls, pages=pages))
    plan = cc.plan_search_online("lung", "hca", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert len(calls) == 2 and "search_after=abc" in calls[1]


def test_hca_pagination_stops_on_foreign_next(tmp_path, monkeypatch):
    """next 指向外域 → 不跟随（防响应里混入奇怪链接），如实零候选。"""
    calls: list = []
    pages = {"size=75": _azul_payload([], next_url="https://evil.example.com/next")}
    monkeypatch.setattr(cc, "_fetch", _hca_fetch(calls, pages=pages))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", "hca", project_root=tmp_path)
    assert ei.value.code == "no_candidates"
    assert len(calls) == 1


def test_hca_shape_drift_fail_closed(tmp_path, monkeypatch):
    """Azul 无公开 API 文档：缺 hits 列表 → fail-closed 如实报错，不硬解析、零写入。"""
    monkeypatch.setattr(cc, "_fetch", lambda url, **kw: ({"pagination": {}}, 200))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("brain", "hca", project_root=tmp_path)
    assert ei.value.code == "network_error"
    assert "形状" in ei.value.hint and "没有拿回任何记录" in ei.value.hint
    assert not (tmp_path / "database").exists()


def test_hca_network_error_propagates(tmp_path, monkeypatch):
    def _fail(url, **kw):
        raise CurateError("network_error", "假网络故障：Azul 拉取失败。")

    monkeypatch.setattr(cc, "_fetch", _fail)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("brain", "hca", project_root=tmp_path)
    assert ei.value.code == "network_error"


def test_hca_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _hca_fetch([]))
    plan = cc.plan_search_online("brain", "hca", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 1 and res["sources"] == {"Human Cell Atlas": 1}
    assert "curate_hca" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert disk["records"][0]["dataset_uid"] == "hca:74b6d569-3b11-42ef-b6b1-a0454522b4a0"


# ----------------------------------------------------------------------------------------------
# 10x Genomics：官网前端私有搜索 API（接入；私有接口无契约，形状校验 fail-closed）
# ----------------------------------------------------------------------------------------------

_TENX_PAYLOAD = {
    "meta": {"count": 2, "limit": 200, "offset": 0, "next": None, "prev": None},
    "results": [
        {"title": "Visium HD Spatial Gene Expression Library, Rat Liver (FFPE)",
         "slug": "visium-hd-rat-liver", "path": "/datasets/visium-hd-rat-liver",
         "publishedAt": "1785774151", "species": ["Rattus norvegicus"],
         "speciesName": "Rattus norvegicus",
         "anatomicalEntities": ["Liver"], "preservationMethods": ["FFPE"],
         "diseaseStateNames": ["non-diseased"], "platformName": ["Visium"],
         "chemistries": ["v1 - Protocol 2.0"], "body": "**Biomaterials**\n\nA rat liver block."},
        {"title": "Chromium Universal 3' Human PBMC", "slug": "chromium-human-pbmc",
         "path": "/datasets/chromium-human-pbmc", "publishedAt": "1700000000",
         "species": ["Homo sapiens"], "diseaseStateNames": [], "platformName": ["Chromium"],
         "chemistries": [], "body": "human pbmc"},
    ],
}


def _tenx_fetch(calls, *, payload=None):
    def _fake(url, **kwargs):
        calls.append(url)
        assert url.startswith(cc.TENX_SEARCH_API)
        assert "document=dataset" in url
        return (payload if payload is not None else _TENX_PAYLOAD), 200
    return _fake


def test_tenx_query_params_and_mapping(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch(calls))
    plan = cc.plan_search_online("liver", "10x", species="Mouse", project_root=tmp_path)
    assert plan["source"] == "10x" and plan["source_label"] == "10x Genomics"
    url = calls[0]
    assert "search=liver" in url                          # 全文参数直达服务端
    assert "tag%5Bspecies%5D=Mouse" in url                # 词表内物种 → 服务端 facet
    rec = plan["candidates"][0]
    assert rec["dataset_uid"] == "10x:visium-hd-rat-liver"
    assert rec["source"] == "10x Genomics"
    assert rec["public_accession"] == "visium-hd-rat-liver"
    assert rec["species"] == "Rat"                        # 学名 → 通用名
    assert rec["tissue"] == "Liver"
    assert rec["disease"] == "normal"                     # 10x 词表 "non-diseased" 归一
    assert rec["platform"] == "Visium" and rec["chemistry"] == "v1 - Protocol 2.0"
    assert rec["published_date"] == "2026-08-03"          # Unix 秒 → UTC 日期
    assert rec["url"] == "https://www.10xgenomics.com/datasets/visium-hd-rat-liver"
    assert rec["description"] == "**Biomaterials** A rat liver block."   # 空白折叠
    # 空值诚实：私有接口不供文件清单/数据集 DOI/细胞数 → None，不猜值
    assert rec["has_raw_data"] is None and rec["filesize"] is None
    assert rec["download_url"] is None and rec["collection_doi"] is None and rec["count"] is None
    assert any("不提供文件清单" in w for w in plan["warnings"])
    rows = _ledger_rows(tmp_path)
    assert rows[0]["records"] == 2                        # meta/results 形态计数


def test_tenx_species_outside_vocab_falls_back_to_local_filter(tmp_path, monkeypatch):
    """词表外物种（Rat 不在实测服务端词表 Human/Mouse 内）：不打 tag，本地子串过滤映射后通用名。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch(calls))
    plan = cc.plan_search_online("liver", "10x", species="Rat", project_root=tmp_path)
    assert "tag%5Bspecies%5D" not in calls[0]
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["dataset_uid"] == "10x:visium-hd-rat-liver"


def test_tenx_slug_direct_lookup_for_sync(tmp_path, monkeypatch):
    """sync「按编号搜回」场景：query 形似 slug → 拉全量清单精确匹配（slug 不是全文词）。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch(calls))
    plan = cc.plan_search_online("visium-hd-rat-liver", "10x", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["dataset_name"].startswith("Visium HD")
    assert "limit=1000" in calls[0] and "search=" not in calls[0]


def test_tenx_slug_like_keyword_falls_back_to_search(tmp_path, monkeypatch):
    """带连字符的真关键词（single-cell）：slug 精确匹配不中 → 落回 search= 关键词流程。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch(calls))
    plan = cc.plan_search_online("single-cell", "10x", project_root=tmp_path)
    assert plan["record_count"] == 2
    assert len(calls) == 2 and "search=single-cell" in calls[1]


def test_tenx_shape_drift_fail_closed(tmp_path, monkeypatch):
    """私有接口无契约：count 类型漂移 / 条目缺 title → fail-closed 如实报错，零写入不炸链。"""
    for bad in ({"meta": {"count": "2"}, "results": []},          # count 类型漂移
                {"meta": {"count": 1}, "results": [{"slug": "x"}]},  # 条目缺 title
                {"results": []},):                                  # 缺 meta
        monkeypatch.setattr(cc, "_fetch", _tenx_fetch([], payload=bad))
        with pytest.raises(CurateError) as ei:
            cc.plan_search_online("liver", "10x", project_root=tmp_path)
        assert ei.value.code == "network_error"
        assert "形状变了" in ei.value.hint and "私有接口" in ei.value.hint
    assert not (tmp_path / "database").exists()


def test_tenx_no_candidates(tmp_path, monkeypatch):
    empty = {"meta": {"count": 0, "limit": 20, "offset": 0}, "results": []}
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch([], payload=empty))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在zzz", "10x", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_tenx_network_error_propagates(tmp_path, monkeypatch):
    def _fail(url, **kw):
        raise CurateError("network_error", "假网络故障：10x 接口失败。")

    monkeypatch.setattr(cc, "_fetch", _fail)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("liver", "10x", project_root=tmp_path)
    assert ei.value.code == "network_error"


def test_tenx_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _tenx_fetch([]))
    plan = cc.plan_search_online("liver", "10x", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 2 and res["sources"] == {"10x Genomics": 2}
    assert "curate_10x" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert all(r["dataset_uid"].startswith("10x:") for r in disk["records"])


# ----------------------------------------------------------------------------------------------
# HuBMAP 分页补齐（species 过滤在 limit 之后凑不足 → 按页拉取边滤边攒）
# ----------------------------------------------------------------------------------------------

_HUBMAP_HIT_U3 = {"_source": {
    "uuid": "u3", "hubmap_id": "HBM555.IJKL.345", "registered_doi": None,
    "dataset_type": "CODEX", "title": "CODEX data from the liver of a human donor",
    "metadata": {"description": "human liver CODEX"}, "origin_samples": [],
    "published_timestamp": None,
}}


def test_hubmap_species_filter_paginates_to_fill_limit(tmp_path, monkeypatch):
    """第一页只滤出 1 条 Human（u2 是 Mouse）→ 必须翻第二页凑足 limit（from 递增、凑足即停）。"""
    bodies: list = []

    def _fake(url, **kwargs):
        body = json.loads(kwargs["body"])
        bodies.append(body)
        if "aggs" in body:
            return _HUBMAP_AGG, 200
        frm = body.get("from", 0)
        page = {0: [_HUBMAP_HIT_U2, _HUBMAP_HIT_U1], 2: [_HUBMAP_HIT_U3]}.get(frm, [])
        return {"hits": {"total": {"relation": "eq", "value": 3}, "hits": page}}, 200

    monkeypatch.setattr(cc, "_fetch", _fake)
    plan = cc.plan_search_online("肿瘤", "hubmap", species="Human", limit=2, project_root=tmp_path)
    assert plan["record_count"] == 2
    assert [r["dataset_uid"] for r in plan["candidates"]] == ["hubmap:u1", "hubmap:u3"]
    search_froms = [b.get("from") for b in bodies if "aggs" not in b]
    assert search_froms == [0, 2]


def test_hubmap_no_species_single_page_unchanged(tmp_path, monkeypatch):
    """不带 species 时第一页即够：施工前后行为逐位一致（单个正文请求，from=0）。"""
    bodies: list = []
    monkeypatch.setattr(cc, "_fetch", _hubmap_fetch(bodies, hits=[_HUBMAP_HIT_U1]))
    plan = cc.plan_search_online("lung", "hubmap", project_root=tmp_path)
    assert plan["record_count"] == 1
    search_bodies = [b for b in bodies if "aggs" not in b]
    assert len(search_bodies) == 1 and search_bodies[0]["from"] == 0


# ----------------------------------------------------------------------------------------------
# 端点回归钉（curl 验证）：验证确认过的现行口径不许回退
# ----------------------------------------------------------------------------------------------

def test_existing_source_endpoints_regression():
    """ 验证：裸 POST search.api.hubmapconsortium.org/search 返回「migrate to /v3/」；
    SCP 的 /search/studies 实测 500 不存在、/site/studies 200；CELLxGENE 官方无 search 端点
    （404）、curation/v1/datasets 200（全量 ~9.6MB）。我们适配器已在现行配方上——钉死防回退。"""
    assert "/v3/" in cc.HUBMAP_SEARCH_API
    assert cc.SCP_LIST_API.endswith("/single_cell/api/v1/site/studies")
    assert "/search/studies" not in cc.SCP_LIST_API
    assert cc.CXG_DATASETS_API.endswith("/curation/v1/datasets")


# ----------------------------------------------------------------------------------------------
# NCBI GEO（E-utilities esearch→esummary 两段式， 接入；
# 实测配方："GSE"[Entry Type] 枚举 + [Organism] 物种过滤 +
# pdat/reldate 窗口；实验类型不走 Entry Type 短语（实测被忽略），gdstype 存 platform）
# ----------------------------------------------------------------------------------------------

_GEO_ESEARCH_PAYLOAD = {
    "header": {"type": "esearch", "version": "0.3"},
    "esearchresult": {"count": "2", "retmax": "20", "retstart": "0",
                      "idlist": ["200335566", "200300001"]},
}
_GEO_ESUMMARY_PAYLOAD = {
    "header": {"type": "esummary", "version": "0.3"},
    "result": {
        "uids": ["200335566", "200300001"],
        "200335566": {
            "uid": "200335566", "accession": "GSE335566", "title": "Human lung scRNA-seq",
            "summary": "Single-cell RNA\nsequencing of human lung.",
            "taxon": "Homo sapiens",
            "gdstype": "Expression profiling by high throughput sequencing",
            "pdat": "2026/08/04", "n_samples": 4,
            "ftplink": "ftp://ftp.ncbi.nlm.nih.gov/geo/series/GSE335nnn/GSE335566",
            "bioproject": "PRJNA1478899",
        },
        "200300001": {
            "uid": "200300001", "accession": "GSE300001", "title": "Mouse brain atlas",
            "summary": "mouse brain cells", "taxon": "Mus musculus",
            "gdstype": "Expression profiling by high throughput sequencing",
            "pdat": "2026/07/30", "n_samples": 8, "ftplink": "",
        },
    },
}


def _geo_fetch(calls, *, esearch=None, esummary=None):
    """按 URL 分流 esearch/esummary 假响应；kw 一并记录（min_interval 限速断言用）。"""

    def _fake(url, **kwargs):
        calls.append({"url": url, **kwargs})
        if "esearch.fcgi" in url:
            return (esearch if esearch is not None else _GEO_ESEARCH_PAYLOAD), 200
        return (esummary if esummary is not None else _GEO_ESUMMARY_PAYLOAD), 200

    return _fake


def test_geo_term_assembly_and_mapping(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _geo_fetch(calls))
    plan = cc.plan_search_online("lung", "geo", species="Mouse", project_root=tmp_path)
    assert plan["source"] == "geo" and plan["source_label"] == "NCBI GEO"
    decoded = urllib.parse.unquote(calls[0]["url"])
    assert decoded.startswith(cc.GEO_ESEARCH_API)
    assert '(lung) AND "Mus musculus"[Organism] AND "GSE"[Entry Type]' in decoded, \
        "term = (关键词) + 词表内物种 [Organism] 服务端过滤 + GSE Entry Type 枚举"
    assert "retmax=20" in calls[0]["url"] and "retmode=json" in calls[0]["url"]
    assert "tool=biodata_agent" in calls[0]["url"], "NCBI 礼貌声明：带 tool 参数（无 email 可声明）"
    assert calls[1]["url"].startswith(cc.GEO_ESUMMARY_API)
    assert "id=200335566,200300001" in calls[1]["url"], "esearch idlist 原样接龙进 esummary"
    assert calls[0]["min_interval"] >= 0.34 and calls[1]["min_interval"] >= 0.34, \
        "NCBI 无 key 红线 ≤3 req/s"
    rec = plan["candidates"][0]
    assert rec["dataset_uid"] == "geo:GSE335566"
    assert rec["source"] == "NCBI GEO" and rec["public_accession"] == "GSE335566"
    assert rec["species"] == "Human"                        # taxon 学名 → 通用名
    assert rec["platform"] == "Expression profiling by high throughput sequencing"  # gdstype
    assert rec["count"] == "4" and rec["unit"] == "Samples"
    assert rec["published_date"] == "2026-08-04"            # pdat YYYY/MM/DD → ISO
    assert rec["url"] == "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE335566"
    assert rec["download_url"].startswith("ftp://ftp.ncbi.nlm.nih.gov/")
    assert rec["description"] == "Single-cell RNA sequencing of human lung."
    # 空值诚实：esummary 不供组织/疾病/文件清单/数据集 DOI → None，不猜值
    assert rec["tissue"] is None and rec["disease"] is None and rec["chemistry"] is None
    assert rec["has_raw_data"] is None and rec["filesize"] is None and rec["collection_doi"] is None
    assert rec["metadata_provenance"]["geo_uid"] == "200335566"
    assert any("不提供组织/疾病/文件清单" in w for w in plan["warnings"])
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 2 and rows[0]["endpoint"] == cc.GEO_ESEARCH_API


def test_geo_species_outside_vocab_falls_back_to_local_filter(tmp_path, monkeypatch):
    """词表外物种（Lepidoptera）：不打 [Organism]，映射后本地子串过滤（人/鼠都滤掉 → no_candidates）。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _geo_fetch(calls))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("atlas", "geo", species="Lepidoptera", project_root=tmp_path)
    assert ei.value.code == "no_candidates"
    assert "[Organism]" not in urllib.parse.unquote(calls[0]["url"])


def test_geo_no_candidates_empty_idlist_short_circuits(tmp_path, monkeypatch):
    """esearch 零命中（空 idlist）→ 不发 esummary，直接 no_candidates。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _geo_fetch(
        calls, esearch={"esearchresult": {"count": "0", "idlist": []}}))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在zzz", "geo", project_root=tmp_path)
    assert ei.value.code == "no_candidates"
    assert len(calls) == 1, "空 idlist 不该再发 esummary"


def test_geo_accession_query_hits_same_path(tmp_path, monkeypatch):
    """sync「按编号搜回」场景：query=GSE 编号走同一 esearch 路径（accession 进全文索引），
    无需单独通道。"""
    calls: list = []
    monkeypatch.setattr(cc, "_fetch", _geo_fetch(calls))
    plan = cc.plan_search_online("GSE335566", "geo", project_root=tmp_path)
    assert plan["record_count"] == 2  # 假出口照返两条；真实服务端按 accession 只命中一条
    assert "(GSE335566)" in urllib.parse.unquote(calls[0]["url"])


def test_geo_shape_drift_fail_closed(tmp_path, monkeypatch):
    """E-utilities 响应形状漂移（esearch 缺 idlist / esummary 缺 uids）→ fail-closed 如实报错，
    零写入不炸链。"""
    monkeypatch.setattr(cc, "_fetch", _geo_fetch([], esearch={"esearchresult": {}}))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", "geo", project_root=tmp_path)
    assert ei.value.code == "network_error" and "形状变了" in ei.value.hint
    monkeypatch.setattr(cc, "_fetch", _geo_fetch([], esummary={"result": {}}))
    with pytest.raises(CurateError) as ei2:
        cc.plan_search_online("lung", "geo", project_root=tmp_path)
    assert ei2.value.code == "network_error" and "形状变了" in ei2.value.hint
    assert not (tmp_path / "database").exists()


def test_geo_network_error_propagates(tmp_path, monkeypatch):
    def _fail(url, **kw):
        raise CurateError("network_error", "假网络故障：E-utilities 失败。")

    monkeypatch.setattr(cc, "_fetch", _fail)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", "geo", project_root=tmp_path)
    assert ei.value.code == "network_error"


def test_geo_plan_apply_token_chain(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _geo_fetch([]))
    plan = cc.plan_search_online("lung", "geo", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 2 and res["sources"] == {"NCBI GEO": 2}
    assert "curate_geo" in res["filename"]
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert all(r["dataset_uid"].startswith("geo:") for r in disk["records"])


# ----------------------------------------------------------------------------------------------
# JSON 解析失败不重试 + 重试次数进账本
# ----------------------------------------------------------------------------------------------

def test_fetch_bad_json_fails_without_retry(monkeypatch):
    """JSON 解析失败是对端改了返回形状（确定性失败）——不得当瞬时错误退避重试。"""
    class _BadResp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, n=-1):
            return b"this is not json{{"

    calls = {"n": 0}

    def _fake_urlopen(req, timeout=0):
        calls["n"] += 1
        return _BadResp()

    monkeypatch.setattr(cc.urllib.request, "urlopen", _fake_urlopen)
    att = [1]
    with pytest.raises(CurateError) as exc_info:
        cc._fetch("https://example.org/bad", attempts=att)
    assert calls["n"] == 1 and att[0] == 1, "JSON 解析失败不得重试（此前会白打两次）"
    assert "不是合法 JSON" in exc_info.value.hint and "没有重试" in exc_info.value.hint


def test_fetch_retry_attempts_feed_ledger(tmp_path, monkeypatch):
    """503 重试两次后成功——attempts 计数如实写进账本条目（形状只增不减）。"""
    calls = {"n": 0}

    def _fake_urlopen(req, timeout=0):
        calls["n"] += 1
        if calls["n"] < 3:
            raise cc.urllib.error.HTTPError(req.full_url, 503, "unavailable", None, None)
        return _FakeResp({"hits": [{"accession": "x"}]})

    monkeypatch.setattr(cc.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(cc.time, "sleep", lambda s: None)
    payload = cc._fetch_logged("https://example.org/x", project_root=tmp_path,
                               endpoint="https://example.org", query="q")
    assert payload == {"hits": [{"accession": "x"}]}
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["attempts"] == 3, "重试必须留痕：attempts 写进账本"

    payload2 = cc._fetch_logged("https://example.org/x", project_root=tmp_path,
                                endpoint="https://example.org", query="q2")
    rows = _ledger_rows(tmp_path)
    assert "attempts" not in rows[1], "一次就成的请求不新增字段"
