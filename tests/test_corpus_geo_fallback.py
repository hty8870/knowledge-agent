# -*- coding: utf-8 -*-
"""GEO 三通道降级（降级施工）的确定性门。**全程禁网**：fetch_json 接缝按 URL
分流注入假响应（NCBI esearch/esummary / BioStudies E-GEOD / Europe PMC），账本走真身断言。

钉的五条：
  1. **主通道通 → 不降级**：NCBI 应答正常时备用通道一个请求都不发，channel=ncbi_eutils；
  2. **主断 → E-GEOD**：note_zh/channel 如实写明走了镜像（含「只有 2016 年前老数据」年代局限），
     E-GEOD-{n} → GSE{n} 编号换算；主通道形状漂移（parse_changed）同样触发降级；
  3. **主 + E-GEOD 断 → Europe PMC 弱兜底**：文献维度如实写明（不是 GEO 数据集清单）；
  4. **全断 → 如实失败**：all_channels_failed，note 逐条列清三条通道的败因；
  5. **降级响应形状漂移 → fail-closed**：缺 hits / 缺 resultList.result 都如实 parse_changed
     继续降级或收尾，绝不硬解析充数。

另钉 check_updates 侧两处：GEO 降级通道如实写进比对 note；AE 版本监控小钉
（BioStudies 顶层键漂移 → note 附提示，不报错）。
"""
import json

import pytest

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus import corpus_net as cn

# ---------------------------------------------------------------- fixtures

_NCBI_ESEARCH = {"esearchresult": {"count": "1", "idlist": ["200335566"]}}
_NCBI_ESUMMARY = {
    "result": {
        "uids": ["200335566"],
        "200335566": {
            "uid": "200335566", "accession": "GSE335566", "title": "Human lung scRNA-seq",
            "summary": "single cell lung", "taxon": "Homo sapiens",
            "gdstype": "Expression profiling by high throughput sequencing",
            "pdat": "2026/08/04",
        },
    },
}

_EGEOD_PAYLOAD = {
    "totalHits": 2,
    "hits": [
        {"accession": "E-GEOD-44549", "title": "Human lung single cell RNA-seq compendium",
         "content": "Homo sapiens lung single cell expression", "release_date": "2014-06-30"},
        {"accession": "E-GEOD-68779", "title": "Mouse cortex single cell atlas",
         "content": "Mus musculus cortex single cell", "release_date": "2016-01-20"},
    ],
}

_EPMC_PAYLOAD = {
    "resultList": {"result": [
        {"id": "39111111", "source": "MED", "pubYear": "2026",
         "title": "Single-cell RNA-seq of human lung fibrosis",
         "abstractText": "Data are available at GEO under accession GSE335566 and GSE335567."},
        {"id": "39222222", "source": "MED", "pubYear": "2025",
         "title": "Another study reusing GSE335566",
         "abstractText": "We reused GSE335566 for benchmarking."},
    ]},
}


def _fake_dispatch(seen, *, esearch=None, esummary=None, egeod=None, epmc=None):
    """三通道 URL 分流假 fetch_json：通道给 payload 字典 → (payload, 200)；给 None → 抛
    _NetError（该通道网络断）。seen 记录每次请求的 url/kw（限速参数与通道分流断言用）。"""

    def _fake(url, **kw):
        seen.append({"url": url, **kw})
        if "esearch.fcgi" in url:
            payload = esearch
        elif "esummary.fcgi" in url:
            payload = esummary
        elif "europepmc" in url:
            payload = epmc
        else:  # BioStudies 通用搜索（E-GEOD 集合）
            payload = egeod
        if payload is None:
            raise cn._NetError("URLError: 测试通道断")
        return payload, 200

    return _fake


def _ledger_lines(root):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _write_external(root, name, payload):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------- 1. 主通道通 → 不降级

def test_primary_up_never_touches_fallback_channels(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, esearch=_NCBI_ESEARCH, esummary=_NCBI_ESUMMARY))  # egeod/epmc=None=断
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "ncbi_eutils"
    assert res["items"][0]["accession"] == "GSE335566"
    assert "镜像" not in res["note_zh"] and "文献" not in res["note_zh"], \
        "主通道的数据不许混入降级通道的字样"
    assert all("ebi.ac.uk" not in r["url"] for r in seen), "主通道通时备用通道一个请求都不许发"
    lines = _ledger_lines(tmp_path)
    assert [l["endpoint"] for l in lines] == [cn.GEO_ESEARCH_API, cn.GEO_ESUMMARY_API]


def test_primary_no_results_is_honest_answer_not_fallback(tmp_path, monkeypatch):
    """主通道真没结果（no_results）是诚实的完整答案，**不**触发降级。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, esearch={"esearchresult": {"count": "0", "idlist": []}}, egeod=_EGEOD_PAYLOAD))
    res = cn.search_geo("xyzzyqwkjv", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert all("ebi.ac.uk" not in r["url"] for r in seen)


# ---------------------------------------------------------------- 2. 主断/主漂移 → E-GEOD

def test_primary_network_down_falls_back_to_egeod_mirror(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen, egeod=_EGEOD_PAYLOAD))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "egeod_mirror"
    assert "NCBI 连不上" in res["note_zh"], "降级必须如实写明主通道没通"
    assert "2016 年前" in res["note_zh"], "E-GEOD 的年代局限（≤2016）必须写进 note"
    it = res["items"][0]
    assert it["accession"] == "GSE44549", "E-GEOD-{n} → GSE{n} 编号换算"
    assert it["egeod_accession"] == "E-GEOD-44549", "镜像原号要留档供人工核对"
    assert it["url"] == "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-GEOD-44549"
    assert it["date"] == "2014-06-30"
    assert all("europepmc" not in r["url"] for r in seen), "E-GEOD 通了就不该再碰弱兜底"
    lines = _ledger_lines(tmp_path)
    endpoints = [l["endpoint"] for l in lines]
    assert cn.GEO_ESEARCH_API in endpoints and "corpus_net:geo_egeod" in endpoints, \
        "账本 endpoint 立名要能区分通道"


def test_primary_shape_drift_also_falls_back(tmp_path, monkeypatch):
    """主通道响应形状漂移（形状闸拦 parse_changed）与网络断同等待遇：触发降级。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, esearch={"esearchresult": {}}, egeod=_EGEOD_PAYLOAD))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "egeod_mirror"


def test_egeod_fallback_species_filter_is_local_substring(tmp_path, monkeypatch):
    """E-GEOD 无 Organism facet：物种过滤回退本地子串（与 AE 轻量支同口径）。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen, egeod=_EGEOD_PAYLOAD))
    res = cn.search_geo("atlas", species="Mouse", project_root=tmp_path)
    assert res["ok"] is True
    assert [it["accession"] for it in res["items"]] == ["GSE68779"]


# ---------------------------------------------------------------- 3. 主+E-GEOD 断 → Europe PMC

def test_two_channels_down_falls_back_to_europepmc_literature(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen, epmc=_EPMC_PAYLOAD))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "europepmc_literature"
    assert "Europe PMC 文献兜底" in res["note_zh"]
    assert "不是 GEO 数据集清单" in res["note_zh"], "文献维度弱兜底必须如实写明"
    assert [it["accession"] for it in res["items"]] == ["GSE335566", "GSE335567"], \
        "从文献标题/摘要抠 GSE 号提及，跨文献去重"
    it = res["items"][0]
    assert it["url"] == "https://europepmc.org/abstract/MED/39111111"
    assert it["date"] == "2026" and it["mentioned_in"] == "Europe PMC 文献全文"
    endpoints = [l["endpoint"] for l in _ledger_lines(tmp_path)]
    assert "corpus_net:geo_egeod" in endpoints and "corpus_net:geo_europepmc" in endpoints


# ---------------------------------------------------------------- 4. 全断 → 如实失败

def test_all_channels_down_reports_honest_failure(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen))  # 三通道全断
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "all_channels_failed"
    assert res["channel"] == "none"
    note = res["note_zh"]
    assert "三条联网通道" in note
    assert "① NCBI E-utilities" in note and "②" in note and "③ Europe PMC" in note, \
        "全败要逐条列清每条通道的败因，不挑一条背锅"
    endpoints = [l["endpoint"] for l in _ledger_lines(tmp_path)]
    assert endpoints == [cn.GEO_ESEARCH_API, "corpus_net:geo_egeod", "corpus_net:geo_europepmc"]


# ---------------------------------------------------------------- 5. 降级响应形状漂移 → fail-closed

def test_egeod_shape_drift_continues_to_europepmc_not_hard_parse(tmp_path, monkeypatch):
    """E-GEOD 缺 hits 列表 → 如实 parse_changed 继续降级（不硬解析充数）；EPMC 正常则兜底成功。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, egeod={"no_hits_here": True}, epmc=_EPMC_PAYLOAD))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "europepmc_literature"


def test_both_fallbacks_shape_drift_is_honest_failure(tmp_path, monkeypatch):
    """两条备用通道都形状漂移 → 全败如实报，note 里两条败因都是形状闸拦。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, egeod={"no_hits_here": True}, epmc={"resultList": {}}))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "all_channels_failed"
    assert res["note_zh"].count("形状变了") == 2, "E-GEOD 与 Europe PMC 的形状漂移都要如实写"


def test_primary_shape_drift_plus_fallbacks_down_is_honest_failure(tmp_path, monkeypatch):
    """主通道形状漂移（不是网络断）+ 备用通道全断 → 同样全败如实报。"""
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, esearch={"esearchresult": {}}))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "all_channels_failed"
    assert "形状变了" in res["note_zh"], "主通道的败因（形状闸拦）要进全败清单"


# ---------------------------------------------------------------- geo_recent_items（check_updates 用）同口径降级

def test_geo_recent_items_falls_back_to_egeod_sorted_by_release_date(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen, egeod=_EGEOD_PAYLOAD))
    res = cn.geo_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True and res["channel"] == "egeod_mirror"
    assert "2016 年后的 GEO 新数据这次看不到" in res["note_zh"], \
        "镜像当「最近」用的年代盲区必须如实写明"
    egeod_reqs = [r for r in seen if "biostudies" in r["url"]]
    assert egeod_reqs and "sortBy=release_date" in egeod_reqs[0]["url"], \
        "镜像的「最近」口径是按 release_date 倒序"


def test_geo_recent_items_all_down_is_honest_failure(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen))
    res = cn.geo_recent_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "all_channels_failed"


def test_geo_recent_items_primary_up_no_fallback(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(
        seen, esearch=_NCBI_ESEARCH, esummary=_NCBI_ESUMMARY))
    res = cn.geo_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True and res["channel"] == "ncbi_eutils"
    assert all("ebi.ac.uk" not in r["url"] for r in seen)


# ---------------------------------------------------------------- 统一出口/search_online_source 透传

def test_search_online_source_geo_fallback_flows_through(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _fake_dispatch(seen, egeod=_EGEOD_PAYLOAD))
    res = cn.search_online_source("geo", "lung", project_root=tmp_path)
    assert res["ok"] is True and res["channel"] == "egeod_mirror"
    assert "E-GEOD" in res["note_zh"]


# ---------------------------------------------------------------- check_updates 侧：通道如实进 note

def test_check_updates_geo_egeod_channel_written_into_note(tmp_path, monkeypatch):
    """check_updates 的 GEO 在线比对：降级走的通道必须如实写进 note（不许假装主通道）。"""
    _write_external(tmp_path, "geo.json", {"records": [
        {"dataset_uid": "geo:GSE44549", "dataset_name": "已有镜像条目",
         "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE44549"},
    ]})
    monkeypatch.setattr(cc.corpus_net, "geo_recent_items", lambda **kw: {
        "ok": True, "channel": "egeod_mirror",
        "items": [
            {"accession": "GSE44549", "title": "本地已有"},
            {"accession": "GSE68779", "title": "目录里没有的镜像条目"},
        ],
    })
    entry = cc.check_updates(["geo"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online" and entry["new_count"] == 1
    assert "E-GEOD" in entry["note_zh"] and "2016 年后的 GEO 新数据这里看不到" in entry["note_zh"]
    assert "GEO 官方源" not in entry["note_zh"], "走了镜像就不许再说「GEO 官方源最近」"


def test_check_updates_geo_europepmc_channel_written_into_note(tmp_path, monkeypatch):
    _write_external(tmp_path, "geo.json", {"records": []})
    monkeypatch.setattr(cc.corpus_net, "geo_recent_items", lambda **kw: {
        "ok": True, "channel": "europepmc_literature",
        "items": [{"accession": "GSE335566", "title": "某文献"}],
    })
    entry = cc.check_updates(["geo"], project_root=tmp_path)["sources"][0]
    assert "Europe PMC 文献维度弱兜底" in entry["note_zh"]
    assert "不能代表 GEO 数据集的更新情况" in entry["note_zh"]


# ---------------------------------------------------------------- AE 版本监控小钉

def test_ae_drift_pin_notes_new_toplevel_field(tmp_path, monkeypatch):
    """BioStudies 响应冒出快照外新顶层字段 → note 附一句提示，比对照常（不报错）。"""
    _write_external(tmp_path, "arrayexpress.json", {"records": []})
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {
        "hits": [{"accession": "E-MTAB-999", "title": "新条目"}],
        "totalHits": 1, "apiVersion": "2.0",
    })
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online" and entry["new_count"] == 1, "漂移只提示，不影响比对结果"
    assert "新顶层字段" in entry["note_zh"] and "apiVersion" in entry["note_zh"]


def test_ae_drift_pin_notes_missing_hits_field(tmp_path, monkeypatch):
    """消费字段 hits 缺席 → note 如实提示疑似改版（不炸、不假装比对成功）。"""
    _write_external(tmp_path, "arrayexpress.json", {"records": []})
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"apiVersion": "2.0"})
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert "没有了 hits 字段" in entry["note_zh"]


def test_ae_drift_pin_silent_when_shape_matches_snapshot(tmp_path, monkeypatch):
    """顶层键集与快照一致 → 小钉静默，不污染 note。"""
    _write_external(tmp_path, "arrayexpress.json", {"records": []})
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"totalHits": 0, "hits": []})
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert "另注意" not in entry["note_zh"]
