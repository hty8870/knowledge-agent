# -*- coding: utf-8 -*-
"""Zenodo 第 10 源（接入）的确定性门。**全程禁网**：corpus_net 侧在 fetch_json
接缝注入假响应，corpus_curation 侧在 _fetch_logged 接缝注入假响应，账本/限速参数走真身断言。

钉的六条：
  1. **字段限定查询构造**：`(metadata.title:"kw" OR metadata.description:"kw") AND type=dataset`
     （不用裸自由词）；物种词表内→学名 AND 进查询，词表外→原词；
  2. **legacy / InvenioRDM 两版响应形状各自通过**；hits.hits 缺席、条目缺 id/标题等核心漂移
     → fail-closed（corpus_net 如实 parse_changed 降级 / corpus_curation 抛 CurateError）；
  3. **记录映射**：accession=数字 record id、url=/records/<id>、source=Zenodo、doi 两版取法、
     description 剥 HTML、species 从文本抠既有词表（抠不到留空不编）；
  4. **限速参数**：Zenodo 官方 30 req/min 红线，调用必须带 min_interval ≥ 3.0s（出口 20/min 余量）；
  5. **check 水位线**：有新增 / 无新增 / 快照缺失三种本地状态 + 网络失败如实降级 snapshot；
  6. **note 如实标注**：通用开放仓储局限（生物只占一部分、物种文本抠取不全、组织/疾病无字段）。
"""
import json
import urllib.parse

import pytest

from dataset_recommender.corpus import corpus_net as cn
from dataset_recommender.corpus import corpus_curation as cc

# ---------------------------------------------------------------- fixtures

#: legacy 形状（conceptrecid / access_right / 顶层 doi；resource_type 带 type 键）——调研实测形状。
ZENODO_LEGACY_PAYLOAD = {
    "hits": {
        "total": 2,
        "hits": [
            {"id": 6962483, "conceptrecid": "5720285", "conceptdoi": "10.5281/zenodo.5720285",
             "doi": "10.5281/zenodo.6962483", "created": "2026-07-25T10:00:00+00:00",
             "metadata": {"title": "Single-cell RNA-seq of human lung",
                          "description": "<p>scRNA-seq dataset from Homo sapiens lung tissue.</p>",
                          "publication_date": "2026-07-25",
                          "resource_type": {"title": "Dataset", "type": "dataset"},
                          "access_right": "open",
                          "creators": [{"name": "Doe, Jane"}]}},
            {"id": 7000001, "conceptrecid": "6000001", "doi": "10.5281/zenodo.7000001",
             "created": "2026-07-24T09:00:00+00:00",
             "metadata": {"title": "Dielectric spectroscopy measurements",
                          "description": "<p>Frequency sweep data.</p>",
                          "publication_date": "2026-07-24",
                          "resource_type": {"title": "Dataset", "type": "dataset"},
                          "access_right": "open"}},
        ],
    },
    "aggregations": {"resource_type": {"buckets": []}},
    "links": {"self": "https://zenodo.org/api/records?page=1"},
}

#: InvenioRDM 形状（pids / resource_type.id / 新式 links，无 conceptrecid/access_right）。
ZENODO_RDM_PAYLOAD = {
    "hits": {
        "total": 1,
        "hits": [
            {"id": 8123456, "created": "2026-08-01T09:00:00+00:00",
             "pids": {"doi": {"identifier": "10.5281/zenodo.8123456"}},
             "metadata": {"title": "Mouse brain single-nucleus RNA-seq",
                          "description": "<p>snRNA-seq of Mus musculus cortex.</p>",
                          "publication_date": "2026-08-01",
                          "resource_type": {"id": "dataset", "title": {"en": "Dataset"}},
                          "publisher": "Zenodo"},
             "links": {"self_html": "https://zenodo.org/records/8123456"}},
        ],
    },
    "links": {"self": "https://zenodo.org/api/records?page=1"},
}


def _net_ledger(root):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_zenodo_snapshot(root, record_ids):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    records = [
        {"dataset_uid": f"zenodo:{rid}", "dataset_name": f"record {rid}",
         "url": f"https://zenodo.org/records/{rid}", "source": "Zenodo"}
        for rid in record_ids
    ]
    (ext / "zenodo.json").write_text(
        json.dumps({"source": "Zenodo", "record_count": len(records), "records": records},
                   ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------- corpus_net：查询构造与限速

def test_search_zenodo_field_qualified_query_and_rate_limit(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or (ZENODO_LEGACY_PAYLOAD, 200)))
    res = cn.search_zenodo("single cell", project_root=tmp_path)
    assert res["ok"] is True
    decoded = urllib.parse.unquote(seen[0]["url"])
    assert 'metadata.title:"single cell"' in decoded, "关键词必须进 title 字段限定"
    assert 'metadata.description:"single cell"' in decoded, "关键词必须进 description 字段限定"
    assert "type=dataset" in seen[0]["url"]
    assert "size=20" in seen[0]["url"]
    assert seen[0]["min_interval"] >= 3.0, "官方红线 30 req/min，出口须 ≤20 req/min（≥3s 间隔）"
    lines = _net_ledger(tmp_path)
    assert len(lines) == 1 and lines[0]["endpoint"] == cn.ZENODO_API
    assert lines[0]["http_status"] == 200


def test_search_zenodo_species_goes_into_query(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (ZENODO_LEGACY_PAYLOAD, 200)))
    cn.search_zenodo("atlas", species="Mouse", project_root=tmp_path)
    decoded = urllib.parse.unquote(seen[0])
    assert '"Mus musculus"' in decoded, "词表内物种必须映射学名进查询词"
    cn.search_zenodo("atlas", species="Lepidoptera", project_root=tmp_path)
    decoded2 = urllib.parse.unquote(seen[1])
    assert '"Lepidoptera"' in decoded2, "词表外物种用原词进查询（服务端文本过滤）"
    assert "musculus" not in decoded2.lower()


def test_search_zenodo_quotes_in_keyword_never_break_lucene(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (ZENODO_LEGACY_PAYLOAD, 200)))
    cn.search_zenodo('single "cell', project_root=tmp_path)
    # 剥掉内层引号后，短语引号必须成对（防 Lucene 语法被顶断）
    assert urllib.parse.unquote(seen[0]).count('"') % 2 == 0


# ---------------------------------------------------------------- corpus_net：两版形状与 fail-closed

def test_search_zenodo_legacy_shape_maps_items(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (ZENODO_LEGACY_PAYLOAD, 200))
    res = cn.search_zenodo("single cell", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 2
    it = res["items"][0]
    assert it["accession"] == "6962483", "accession 用数字 record id"
    assert it["url"] == "https://zenodo.org/records/6962483"
    assert it["date"] == "2026-07-25"
    assert "<p>" not in it["snippet"], "description 是 HTML，必须剥标签"
    assert it["doi"] == "10.5281/zenodo.6962483"
    assert it["species"] == ["Homo sapiens"], "物种从文本抠既有词表"
    assert res["items"][1]["species"] == [], "抠不到物种 → 留空不编"
    assert "通用开放仓储" in res["note_zh"], "note 必须如实标注通用仓储局限"


def test_search_zenodo_rdm_shape_also_passes(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (ZENODO_RDM_PAYLOAD, 200))
    res = cn.search_zenodo("brain", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 1, "RDM 形状（resource_type.id/pids）也要过闸"
    it = res["items"][0]
    assert it["accession"] == "8123456"
    assert it["doi"] == "10.5281/zenodo.8123456", "RDM 形状 doi 从 pids.doi.identifier 取"
    assert it["species"] == ["Mus musculus"]


@pytest.mark.parametrize("bad", [
    {},                                                              # 顶层缺 hits
    {"hits": {"hits": "not-a-list"}},                                # hits.hits 非列表
    {"hits": {"hits": [{"conceptrecid": "1", "metadata": {"title": "x"}}]}},   # 缺 id
    {"hits": {"hits": [{"id": 1, "metadata": {"description": "x"}}]}},         # 缺 title
    {"hits": {"hits": [{"id": 1, "metadata": None}]}},               # metadata 非 dict
])
def test_search_zenodo_shape_drift_fail_closed(tmp_path, monkeypatch, bad):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (bad, 200))
    res = cn.search_zenodo("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed", f"漂移必须 fail-closed：{bad}"
    assert "人工核对" in res["note_zh"]


def test_search_zenodo_empty_query_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda *a, **kw: pytest.fail("空关键词不该发请求"))
    res = cn.search_zenodo("", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "empty_query"
    assert _net_ledger(tmp_path) == []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("HTTP 429（…）")))
    res2 = cn.search_zenodo("x", project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "network_error"
    assert len(_net_ledger(tmp_path)) == 1, "失败也要记账本"


def test_search_online_source_dispatches_zenodo(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (ZENODO_LEGACY_PAYLOAD, 200))
    res = cn.search_online_source("zenodo", "single cell", project_root=tmp_path)
    assert res["ok"] is True and res["items"][0]["accession"] == "6962483"


# ---------------------------------------------------------------- corpus_net：check_updates 最近条目出口

def test_zenodo_recent_items_uses_type_dataset_mostrecent(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or (ZENODO_LEGACY_PAYLOAD, 200)))
    res = cn.zenodo_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True and len(res["items"]) == 2
    assert "type=dataset" in seen[0]["url"] and "sort=mostrecent" in seen[0]["url"]
    assert seen[0]["min_interval"] >= 3.0
    assert "不限生物领域" in res["note_zh"], "全领域口径必须如实写明"


def test_zenodo_recent_items_drift_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"links": {}}, 200))
    res = cn.zenodo_recent_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed"
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("URLError: boom")))
    res2 = cn.zenodo_recent_items(project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "network_error"


# ---------------------------------------------------------------- corpus_curation：check 水位线

def _fake_recent(items):
    return {"ok": True, "items": items, "note_zh": "fake recent"}


_RECENT_ITEMS = [
    {"accession": "6962483", "title": "Single-cell RNA-seq of human lung"},
    {"accession": "7000001", "title": "Dielectric spectroscopy measurements"},
]


def test_check_updates_zenodo_new_candidates(tmp_path, monkeypatch):
    _write_zenodo_snapshot(tmp_path, ["6962483"])
    monkeypatch.setattr(cc.corpus_net, "zenodo_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    res = cc.check_updates(["zenodo"], project_root=tmp_path)
    ent = res["sources"][0]
    assert ent["mode"] == "online" and ent["local_count"] == 1
    assert ent["new_count"] == 1
    assert ent["new_candidates"] == [{"accession": "7000001",
                                      "title": "Dielectric spectroscopy measurements"}]
    assert "通用开放仓储" in ent["note_zh"], "全领域口径必须如实写进 note"


def test_check_updates_zenodo_nothing_new(tmp_path, monkeypatch):
    _write_zenodo_snapshot(tmp_path, ["6962483", "7000001"])
    monkeypatch.setattr(cc.corpus_net, "zenodo_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    ent = cc.check_updates(["zenodo"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "online" and ent["new_count"] == 0
    assert "都有了" in ent["note_zh"]


def test_check_updates_zenodo_snapshot_missing_all_new(tmp_path, monkeypatch):
    monkeypatch.setattr(cc.corpus_net, "zenodo_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    ent = cc.check_updates(["zenodo"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "online" and ent["local_count"] == 0
    assert ent["new_count"] == 2, "快照缺失 → 在线条目全是新增候选（如实报，不伪造水位线）"


def test_check_updates_zenodo_network_failure_degrades_to_snapshot(tmp_path, monkeypatch):
    _write_zenodo_snapshot(tmp_path, ["6962483"])
    monkeypatch.setattr(cc.corpus_net, "zenodo_recent_items",
                        lambda **kw: {"ok": False, "items": [], "error": "network_error",
                                      "note_zh": "Zenodo 官方 API 请求失败（boom）。"})
    ent = cc.check_updates(["zenodo"], project_root=tmp_path)["sources"][0]
    assert ent["mode"] == "snapshot", "在线拉不到 → 如实降级 snapshot，不假装比对完成"
    assert "没能完成" in ent["note_zh"] and ent["online_recent"] is None


# ---------------------------------------------------------------- corpus_curation：入库适配器（records 富化）

def test_search_zenodo_curation_adapter_maps_records(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cc, "_fetch_logged",
                        lambda url, **kw: (seen.append({"url": url, **kw}) or ZENODO_LEGACY_PAYLOAD))
    records, warnings = cc.SOURCE_ADAPTERS["zenodo"]["search"](
        "single cell", species="", limit=20, project_root=tmp_path)
    assert len(records) == 2
    decoded = urllib.parse.unquote(seen[0]["url"])
    assert 'metadata.title:"single cell"' in decoded and "type=dataset" in decoded
    assert seen[0]["min_interval"] >= 3.0
    rec = records[0]
    assert rec["dataset_uid"] == "zenodo:6962483"
    assert rec["source"] == "Zenodo"
    assert rec["public_accession"] == "6962483"
    assert rec["url"] == "https://zenodo.org/records/6962483"
    assert rec["collection_doi"] == "10.5281/zenodo.5720285", "conceptdoi 优先（指向最新版，更稳定）"
    assert rec["species"] == "Human", "文本抠到 Homo sapiens → 通用名 Human"
    assert rec["tissue"] is None and rec["disease"] is None, "组织/疾病槽位放弃（诚实缺省）"
    assert rec["published_date"] == "2026-07-25"
    assert "<p>" not in (rec["description"] or "")
    assert records[1]["species"] is None, "抠不到物种 → None（不编）"
    assert warnings and "通用开放仓储" in warnings[0], "warnings 必须如实标注局限"
    prov = rec["metadata_provenance"]["fields"]
    assert prov["tissue_disease"]["complete"] is False
    assert prov["species"]["complete"] is False


def test_search_zenodo_curation_id_lookup_for_sync(tmp_path, monkeypatch):
    """sync_updates「按编号搜回」：query 全数字 → GET /api/records/<id> 直查（数字 id 不是全文词）。"""
    seen: list[str] = []
    single = ZENODO_LEGACY_PAYLOAD["hits"]["hits"][0]
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: (seen.append(url) or single))
    records, warnings = cc.SOURCE_ADAPTERS["zenodo"]["search"](
        "6962483", species="", limit=5, project_root=tmp_path)
    assert seen[0].endswith("/api/records/6962483")
    assert len(records) == 1 and records[0]["dataset_uid"] == "zenodo:6962483"


def test_search_zenodo_curation_drift_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": {"hits": [{"id": "x"}]}})
    with pytest.raises(cc.CurateError):
        cc.SOURCE_ADAPTERS["zenodo"]["search"]("x", species="", limit=5, project_root=tmp_path)


def test_sync_updates_zenodo_closes_loop(tmp_path, monkeypatch):
    """sync 闭环：check 发现新增 → 按编号直查搜回 → 入外部库 upload_*（不落冻结基准）。"""
    _write_zenodo_snapshot(tmp_path, ["6962483"])
    monkeypatch.setattr(cc.corpus_net, "zenodo_recent_items",
                        lambda **kw: _fake_recent(_RECENT_ITEMS))
    single = ZENODO_LEGACY_PAYLOAD["hits"]["hits"][1]
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: single)
    res = cc.sync_updates(["zenodo"], project_root=tmp_path)
    ent = res["sources"][0]
    assert ent["imported_count"] == 1, "疑似新增应自动入库 1 条"
    assert ent["filename"] and ent["filename"].startswith("upload_")
    saved = json.loads((tmp_path / "database" / "external" / ent["filename"])
                       .read_text(encoding="utf-8"))
    assert saved["records"][0]["dataset_uid"] == "zenodo:7000001"
    assert not (tmp_path / "database" / "base").exists(), "冻结基准一个字节都不能动"
