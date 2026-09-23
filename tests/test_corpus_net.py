# -*- coding: utf-8 -*-
"""联网工具组 corpus_net.py的确定性门。**全程禁网**：fetch_text/fetch_json
接缝注入假响应（fixture HTML/JSON），账本/限速走真身断言。

钉的四条：
  1. **统一出口形态**：search_online_source 与各适配器一律 `{ok, items, note_zh?, error?}`，
     任何失败（网络/解析/空关键词）都是 ok=False 字典，**绝不抛异常炸链**；
  2. **DDG 通用搜索**：HTML fixture 解析（uddg 跳转壳剥壳/标签清洗/摘要配对）、≥1s 限速、
     ≤12s 超时参数、每次请求记 curate_net_ledger；
  3. **官方适配器**：ENCODE @graph 映射、10x 私有搜索 API（meta/results 形状校验 fail-closed，
     漂移如实 parse_changed 降级，不炸）、GEO E-utilities 两段式（"GSE"[Entry Type] 枚举 +
     [Organism] 物种过滤、≤3 req/s 限速、pdat 窗口最近条目本地倒序）；
  4. **账本**：成功/失败都追加一行（ts/endpoint/query/http_status/records，失败带 error）。
"""
import json
import re
import time
import urllib.parse

import pytest

from dataset_recommender.corpus import corpus_net as cn

# ---------------------------------------------------------------- fixtures

DDG_HTML = """
<html><body>
<div class="results">
  <div class="result results_links">
    <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.encodeproject.org%2Fexperiments%2FENCSR000AAA%2F&amp;rut=deadbeef">ENCODE Experiment &amp;quot;lung&amp;quot;</a>
    <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">Human lung single cell dataset, &lt;b&gt;ChIP-seq&lt;/b&gt;.</a>
  </div>
  <div class="result results_links">
    <a rel="nofollow" class="result__a" href="https://direct.example.org/page">Direct Link Title</a>
    <div class="result__snippet">Second snippet here.</div>
  </div>
</div>
</body></html>
"""

ENCODE_PAYLOAD = {
    "@graph": [
        {"accession": "ENCSR111AAA", "description": "Human lung ChIP-seq",
         "assay_title": "ChIP-seq", "date_created": "2026-07-30T10:00:00",
         "@id": "/experiments/ENCSR111AAA/", "lab": {"title": "Bradley Bernstein, Broad"}},
        {"accession": "ENCSR222BBB", "assay_title": "RNA-seq",
         "date_released": "2026-07-01", "@id": "/experiments/ENCSR222BBB/"},
        {"no_accession": True},
    ]
}

TENX_API_PAYLOAD = {
    "meta": {"count": 2, "limit": 1000, "offset": 0, "next": None, "prev": None},
    "results": [
        {"title": "1k PBMCs, Chromium 3' v3.1", "slug": "1k-pbmcs-chromium-3-v31",
         "path": "/datasets/1k-pbmcs-chromium-3-v31", "publishedAt": "1700000000",
         "species": ["Homo sapiens"], "body": "human pbmc"},
        {"title": "Visium Fresh Frozen Mouse Brain", "slug": "visium-ff-mouse-brain",
         "path": "/datasets/visium-ff-mouse-brain", "publishedAt": "1785774151",
         "species": ["Mus musculus"], "body": "mouse brain"},
    ],
}


def _ledger_lines(root):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- DDG 解析与搜索

def test_parse_ddg_html_extracts_title_url_snippet():
    items = cn.parse_ddg_html(DDG_HTML, limit=10)
    assert len(items) == 2
    first = items[0]
    assert first["url"] == "https://www.encodeproject.org/experiments/ENCSR000AAA/", "uddg 跳转壳必须剥掉"
    assert first["title"] == 'ENCODE Experiment "lung"', "HTML 实体要反转义、标签要清洗"
    assert "ChIP-seq" in first["snippet"] and "<b>" not in first["snippet"]
    assert items[1]["url"] == "https://direct.example.org/page", "直链原样保留"
    assert items[1]["snippet"] == "Second snippet here.", "div 形态的 snippet 也要认"


def test_parse_ddg_html_garbage_gives_empty_not_crash():
    assert cn.parse_ddg_html("<html>no results here</html>") == []


def test_search_duckduckgo_happy_path_writes_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (DDG_HTML, 200))
    res = cn.search_duckduckgo("human lung", species="Human", project_root=tmp_path)
    assert res["ok"] is True
    assert len(res["items"]) == 2
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 1, "每次联网必须记一行账本"
    entry = lines[0]
    assert entry["endpoint"] == cn.DDG_HTML_ENDPOINT
    assert "human lung" in entry["query"] and "Human" in entry["query"]
    assert entry["http_status"] == 200 and entry["records"] == 1
    assert entry["ts"]


def test_ddg_request_uses_12s_timeout_and_1s_interval(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (seen.append(kw) or (DDG_HTML, 200)))
    cn.search_duckduckgo("x", project_root=tmp_path)
    assert seen[0]["timeout"] <= 12, "任务红线：DDG 超时 ≤12s"
    assert seen[0]["min_interval"] >= 1.0, "任务红线：DDG 串行 + 间隔 ≥1s"


def test_ddg_rate_limit_sleeps_to_enforce_interval(monkeypatch):
    sleeps: list[float] = []
    clock = {"t": 1000.0}  # 假时钟：sleep 记表并推进时钟——死线循环（睡到死线为止，可多次 sleep）下必须可控
    monkeypatch.setattr(time, "sleep", lambda s: (sleeps.append(s), clock.__setitem__("t", clock["t"] + s)))
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    host = "test-host.example"  # 独立 host 键，不碰全局限速表里的真实 host
    cn._polite_wait(host, 1.0)
    assert sleeps == [], "首次请求不需要等"
    cn._polite_wait(host, 1.0)
    assert sleeps and abs(sum(sleeps) - 1.0) < 1e-6, "距上次 <1s 必须补睡到 1s 间隔（死线循环可拆多次 sleep，看总量）"


def test_search_duckduckgo_empty_query_never_hits_network(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_text",
                        lambda *a, **kw: pytest.fail("空关键词不该发请求"))
    res = cn.search_duckduckgo("", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "empty_query"
    assert _ledger_lines(tmp_path) == []


def test_search_duckduckgo_network_failure_is_fail_dict_and_logged(tmp_path, monkeypatch):
    def boom(url, **kw):
        raise cn._NetError("HTTP 503（https://html.duckduckgo.com/html/?q=x）")

    monkeypatch.setattr(cn, "fetch_text", boom)
    res = cn.search_duckduckgo("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "network_error"
    assert "失败" in res["note_zh"]
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 1, "失败也要记账本"
    assert lines[0]["http_status"] is None and "503" in lines[0]["error"]


# ---------------------------------------------------------------- ENCODE 适配器

def test_search_encode_maps_graph_items(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (ENCODE_PAYLOAD, 200))
    res = cn.search_encode("lung", species="Human", project_root=tmp_path)
    assert res["ok"] is True
    items = res["items"]
    assert len(items) == 2, "缺 accession 的节点要丢弃"
    first = items[0]
    assert first["accession"] == "ENCSR111AAA"
    assert first["title"] == "Human lung ChIP-seq"
    assert first["url"] == "https://www.encodeproject.org/experiments/ENCSR111AAA/"
    assert first["date"] == "2026-07-30"
    assert "ChIP-seq" in first["snippet"] and "Bernstein" in first["snippet"]
    assert items[1]["date"] == "2026-07-01", "date_created 缺失回退 date_released"
    lines = _ledger_lines(tmp_path)
    assert lines[0]["records"] == 3, "账本条数取 @graph 长度"


def test_search_encode_query_and_headers(tmp_path, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.update({"url": url, **kw}) or ({"@graph": []}, 200)))
    res = cn.search_encode("chip seq", species="human", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert "type=Experiment" in seen["url"] and "searchTerm=chip%20seq%20human" in seen["url"]
    assert seen["headers"]["Accept"] == "application/json"


def test_search_encode_failure_is_fail_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("URLError: boom")))
    res = cn.search_encode("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "network_error"


def test_search_encode_404_means_no_results_not_network_error(tmp_path, monkeypatch):
    """ENCODE 语义：searchTerm 无命中返回 404——如实映射 no_results，不谎报网络故障。"""
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("HTTP 404（…）")))
    res = cn.search_encode("xyzzyqwkjv", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert "没有命中条目" in res["note_zh"]


def test_encode_recent_items_hits_sort_by_date_created(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (ENCODE_PAYLOAD, 200)))
    res = cn.encode_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True and len(res["items"]) == 2
    assert "sort=-date_created" in seen[0], "check_updates 要的是按创建日期最新的条目"


# ---------------------------------------------------------------- 10x 适配器（官网私有搜索 API，形状校验 fail-closed）

def test_tenx_dataset_items_full_list_and_mapping(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (TENX_API_PAYLOAD, 200)))
    res = cn.tenx_dataset_items(project_root=tmp_path)
    assert res["ok"] is True and res["total"] == 2
    assert "sort=publishedAt%20DESC" in seen[0], "check_updates 要的是按发布时间最新的清单"
    by_acc = {it["accession"]: it for it in res["items"]}
    assert by_acc["1k-pbmcs-chromium-3-v31"]["url"] == \
        "https://www.10xgenomics.com/datasets/1k-pbmcs-chromium-3-v31"
    assert by_acc["visium-ff-mouse-brain"]["date"] == "2026-08-03"   # Unix 秒 → UTC 日期
    lines = _ledger_lines(tmp_path)
    assert lines[0]["records"] == 2, "meta/results 形态按 results 长度记账"


def test_tenx_dataset_items_paginates_until_total(tmp_path, monkeypatch):
    """meta.count 大于单页 → 按 offset 翻页补齐（offset=已攒条数）。"""
    page0 = {"meta": {"count": 2, "limit": 1, "offset": 0}, "results": [TENX_API_PAYLOAD["results"][0]]}
    page1 = {"meta": {"count": 2, "limit": 1, "offset": 1}, "results": [TENX_API_PAYLOAD["results"][1]]}
    seen: list[str] = []

    def _fake(url, **kw):
        seen.append(url)
        return (page1 if "offset=1" in url else page0), 200

    monkeypatch.setattr(cn, "_TENX_PAGE_LIMIT", 1)
    monkeypatch.setattr(cn, "fetch_json", _fake)
    res = cn.tenx_dataset_items(project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 2
    assert len(seen) == 2 and "offset=1" in seen[1]


def test_tenx_shape_drift_degrades_not_crash(tmp_path, monkeypatch):
    """私有接口无契约：缺 meta/count 漂移 → parse_changed 如实降级（写明人工核对入口），不炸链。"""
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"results": []}, 200))
    res = cn.tenx_dataset_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed"
    assert "私有接口" in res["note_zh"] and "人工核对" in res["note_zh"]


def test_tenx_network_failure_is_fail_dict(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("TimeoutError")))
    res = cn.tenx_dataset_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "network_error"


def test_search_10x_api_params_and_species_tag(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (TENX_API_PAYLOAD, 200)))
    res = cn.search_10x("pbmc", species="Human", project_root=tmp_path)
    assert res["ok"] is True
    assert "search=pbmc" in seen[0] and "tag%5Bspecies%5D=Human" in seen[0]
    # 词表外物种（zebrafish）不打服务端 tag → 本地子串过滤（两条都是人/鼠 → 滤空）
    empty = cn.search_10x("pbmc", species="zebrafish", project_root=tmp_path)
    assert empty["ok"] is False and empty["error"] == "no_results"
    assert "tag%5Bspecies%5D" not in seen[1]
    none_hit = cn.search_10x("zzz-no-such-thing", project_root=tmp_path)
    # 服务端 search= 由假出口照返两条（真实服务端会滤）——本地不再过滤标题，只钉出口形态
    assert none_hit["ok"] is True


# ---------------------------------------------------------------- HCA（Azul）轻量 items 搜索

_AZUL_PAGE = {
    "pagination": {"count": 2, "total": 2, "size": 75, "next": None, "previous": None, "pages": 1},
    "hits": [
        {"entryId": "aaa-brain", "projects": [{"projectTitle": "Human brain atlas",
                                               "projectDescription": "cortex\nsingle cell"}],
         "donorOrganisms": [{"genusSpecies": ["Homo sapiens"]}],
         "dates": [{"aggregateSubmissionDate": "2026-04-14T18:09:49.156048Z"}]},
        {"entryId": "bbb-lung", "projects": [{"projectTitle": "Mouse lung atlas",
                                              "projectDescription": "lung cells"}],
         "donorOrganisms": [{"genusSpecies": ["Mus musculus"]}], "dates": []},
    ],
}


def test_search_hca_local_keyword_filter_and_mapping(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (_AZUL_PAGE, 200)))
    res = cn.search_hca("brain", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 1       # 本地关键词匹配：lung 不命中 brain
    assert "size=75" in seen[0] and "filters=" not in seen[0]  # 无物种不打服务端 facet
    it = res["items"][0]
    assert it["accession"] == "aaa-brain"
    assert it["url"] == "https://data.humancellatlas.org/explore/projects/aaa-brain"
    assert it["date"] == "2026-04-14"
    assert it["snippet"] == "cortex single cell"              # 换行折叠
    lines = _ledger_lines(tmp_path)
    assert lines[0]["endpoint"] == cn.AZUL_PROJECTS_API and lines[0]["records"] == 2


def test_search_hca_species_facet_server_side(tmp_path, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (_AZUL_PAGE, 200)))
    res = cn.search_hca("atlas", species="Mouse", project_root=tmp_path)
    decoded = urllib.parse.unquote(seen[0])
    assert '"genusSpecies":{"is":["Mus musculus"]}' in decoded   # 服务端 facet 精确过滤
    assert res["ok"] is True                                     # 假出口不过滤，出口形态照钉
    # 词表外物种（Lepidoptera）不打 facet → genusSpecies 原文本地子串过滤（滤空 → no_results）
    empty = cn.search_hca("atlas", species="Lepidoptera", project_root=tmp_path)
    assert empty["ok"] is False and empty["error"] == "no_results"
    assert "filters=" not in seen[1]


def test_search_hca_pagination_and_foreign_next_stop(tmp_path, monkeypatch):
    page0 = {"pagination": {"next": cn.AZUL_PROJECTS_API + "?search_after=x"}, "hits": []}
    page1 = {"pagination": {"next": None}, "hits": [_AZUL_PAGE["hits"][1]]}
    seen: list[str] = []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (seen.append(url) or (page1 if "search_after" in url else page0), 200))
    res = cn.search_hca("lung", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 1
    assert len(seen) == 2 and "search_after=x" in seen[1]
    # next 指向外域 → 不跟随，如实零结果
    bad = {"pagination": {"next": "https://evil.example.com/x"}, "hits": []}
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (bad, 200))
    stop = cn.search_hca("lung", project_root=tmp_path)
    assert stop["ok"] is False and stop["error"] == "no_results"


def test_search_hca_shape_drift_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"pagination": {}}, 200))
    res = cn.search_hca("brain", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "parse_changed"
    assert "人工核对" in res["note_zh"]
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("TimeoutError")))
    res2 = cn.search_hca("brain", project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "network_error"


# ---------------------------------------------------------------- ArrayExpress 轻量 items

def test_search_arrayexpress_items_shape_and_species_filter(tmp_path, monkeypatch):
    payload = {"hits": [
        {"accession": "E-MTAB-1", "title": "Human lung atlas", "release_date": "2026-07-01",
         "content": "Homo sapiens single cell"},
        {"accession": "E-MTAB-2", "title": "Mouse brain", "release_date": "2026-06-01",
         "content": "Mus musculus"},
    ]}
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (payload, 200))
    res = cn.search_arrayexpress_items("single cell", species="human", project_root=tmp_path)
    assert res["ok"] is True and len(res["items"]) == 1, "species 本地子串过滤"
    it = res["items"][0]
    assert it["accession"] == "E-MTAB-1"
    assert it["url"] == "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-1"
    assert it["date"] == "2026-07-01"


# ---------------------------------------------------------------- NCBI GEO（E-utilities）适配器

_GEO_ESEARCH_PAYLOAD = {
    "header": {"type": "esearch", "version": "0.3"},
    "esearchresult": {"count": "2", "retmax": "2", "retstart": "0",
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
        },
        "200300001": {
            "uid": "200300001", "accession": "GSE300001", "title": "Mouse brain atlas",
            "summary": "mouse brain cells", "taxon": "Mus musculus",
            "gdstype": "Expression profiling by high throughput sequencing",
            "pdat": "2026/07/30", "n_samples": 8, "ftplink": "",
        },
    },
}


def _geo_fake_fetch(seen):
    """按 URL 分流 esearch/esummary 假响应；kw 一并记录（限速参数断言用）。"""

    def _fake(url, **kw):
        seen.append({"url": url, **kw})
        if "esearch.fcgi" in url:
            return _GEO_ESEARCH_PAYLOAD, 200
        return _GEO_ESUMMARY_PAYLOAD, 200

    return _fake


def test_search_geo_term_assembly_and_mapping(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _geo_fake_fetch(seen))
    res = cn.search_geo("lung", species="Mouse", project_root=tmp_path)
    assert res["ok"] is True
    decoded = urllib.parse.unquote(seen[0]["url"])
    assert '("GSE"[Entry Type])' in decoded or '"GSE"[Entry Type]' in decoded
    assert '"Mus musculus"[Organism]' in decoded, "词表内物种必须走服务端 [Organism] 过滤"
    assert "(lung)" in decoded and "retmax=20" in seen[0]["url"]
    assert "tool=biodata_agent" in seen[0]["url"], "NCBI 礼貌声明：带 tool 参数（无 email 可声明）"
    assert "db=gds" in seen[1]["url"] and "id=200335566,200300001" in seen[1]["url"]
    assert seen[0]["min_interval"] >= 0.34, "NCBI 无 key 红线 ≤3 req/s"
    assert seen[1]["min_interval"] >= 0.34
    it = res["items"][0]
    assert it["accession"] == "GSE335566" and it["title"] == "Human lung scRNA-seq"
    assert it["url"] == "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE335566"
    assert it["date"] == "2026-08-04", "pdat YYYY/MM/DD → ISO"
    assert it["snippet"] == "Single-cell RNA sequencing of human lung."
    assert it["taxon"] == "Homo sapiens"
    lines = _ledger_lines(tmp_path)
    assert len(lines) == 2 and lines[0]["endpoint"] == cn.GEO_ESEARCH_API
    assert lines[1]["endpoint"] == cn.GEO_ESUMMARY_API


def test_search_geo_species_outside_vocab_falls_back_to_local_filter(tmp_path, monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(cn, "fetch_json", _geo_fake_fetch(seen))
    res = cn.search_geo("atlas", species="Lepidoptera", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert "[Organism]" not in urllib.parse.unquote(seen[0]["url"]), "词表外物种不打服务端 [Organism]"


def test_search_geo_shape_drift_fail_closed(tmp_path, monkeypatch):
    """E-utilities 响应形状漂移 → 如实降级（写明人工核对入口），不硬解析。
    三通道降级编排：漂移先触发备用通道；备用通道也拿到畸形响应时
    同样过形状闸如实 parse_changed，最终落成 all_channels_failed。"""
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"esearchresult": {}}, 200))
    res = cn.search_geo("lung", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "all_channels_failed"
    assert "人工核对" in res["note_zh"]

    def _fake(url, **kw):
        if "esearch.fcgi" in url:
            return _GEO_ESEARCH_PAYLOAD, 200
        return {"result": {}}, 200

    monkeypatch.setattr(cn, "fetch_json", _fake)
    res2 = cn.search_geo("lung", project_root=tmp_path)
    assert res2["ok"] is False and res2["error"] == "all_channels_failed"


def test_search_geo_empty_query_and_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json",
                        lambda *a, **kw: pytest.fail("空关键词不该发请求"))
    res = cn.search_geo("", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "empty_query"
    assert _ledger_lines(tmp_path) == []
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: (_ for _ in ()).throw(cn._NetError("URLError: boom")))
    res2 = cn.search_geo("lung", project_root=tmp_path)
    # 三通道降级编排：主通道 network_error 不再直接收尾——E-GEOD/Europe PMC
    # 两条备用通道同样被这个全炸的 fake 打断后，才如实报 all_channels_failed。
    assert res2["ok"] is False and res2["error"] == "all_channels_failed"
    assert "三条联网通道" in res2["note_zh"]


def test_geo_recent_items_uses_pdat_window_and_sorts_desc(tmp_path, monkeypatch):
    seen: list[dict] = []
    # esummary 乱序（旧的前排）→ 出口必须按 pdat 倒序
    payload = {
        "result": {
            "uids": ["200300001", "200335566"],
            "200300001": _GEO_ESUMMARY_PAYLOAD["result"]["200300001"],
            "200335566": _GEO_ESUMMARY_PAYLOAD["result"]["200335566"],
        },
    }

    def _fake(url, **kw):
        seen.append({"url": url, **kw})
        if "esearch.fcgi" in url:
            return _GEO_ESEARCH_PAYLOAD, 200
        return payload, 200

    monkeypatch.setattr(cn, "fetch_json", _fake)
    res = cn.geo_recent_items(project_root=tmp_path, limit=10)
    assert res["ok"] is True
    assert "reldate=90" in seen[0]["url"] and "datetype=pdat" in seen[0]["url"]
    assert [it["accession"] for it in res["items"]] == ["GSE335566", "GSE300001"], \
        "check_updates 要的是按公开日期最新的条目"


def test_geo_recent_items_empty_window_is_honest_no_results(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_json",
                        lambda url, **kw: ({"esearchresult": {"count": "0", "idlist": []}}, 200))
    res = cn.geo_recent_items(project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"
    assert "90 天" in res["note_zh"]


# ---------------------------------------------------------------- 统一出口

def test_search_online_source_dispatch_and_aliases(tmp_path, monkeypatch):
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (DDG_HTML, 200))
    for alias in ("ddg", "web", "generic"):
        res = cn.search_online_source(alias, "lung", project_root=tmp_path)
        assert res["ok"] is True, f"别名 {alias} 应路由到 DDG 通用搜索"
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (TENX_API_PAYLOAD, 200))
    res = cn.search_online_source("10x Genomics", "pbmc", project_root=tmp_path)
    assert res["ok"] is True and res["items"][0]["accession"] == "1k-pbmcs-chromium-3-v31"
    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: (_AZUL_PAGE, 200))
    res = cn.search_online_source("Human Cell Atlas", "brain", project_root=tmp_path)
    assert res["ok"] is True and res["items"][0]["accession"] == "aaa-brain"
    res = cn.search_online_source("hca", "brain", project_root=tmp_path)
    assert res["ok"] is True
    monkeypatch.setattr(cn, "fetch_json", _geo_fake_fetch([]))
    res = cn.search_online_source("NCBI GEO", "lung", project_root=tmp_path)
    assert res["ok"] is True and res["items"][0]["accession"] == "GSE335566"
    res = cn.search_online_source("geo", "lung", project_root=tmp_path)
    assert res["ok"] is True


def test_search_online_source_unknown_source_is_honest_fail(tmp_path):
    res = cn.search_online_source("arxiv", "x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "unknown_source"
    assert "arxiv" in res["note_zh"]


def test_search_online_source_never_raises_on_adapter_bug(tmp_path, monkeypatch):
    """兜底防炸链：适配器内部出意外异常也落成 ok=False 字典。"""
    monkeypatch.setattr(cn, "search_encode",
                        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("意外")))
    res = cn.search_online_source("encode", "x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "unexpected_error"


# ---------------------------------------------------------------- anomaly 人机验证判定

def test_ddg_anomaly_bare_substring_is_not_blocked(tmp_path, monkeypatch):
    """页面只出现 "anomaly" 裸子串（如查询回显 "anomaly detection"）不得误报人机验证。"""
    page = "<html><body><h1>anomaly detection</h1><p>No results.</p></body></html>"
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (page, 200))
    res = cn.search_duckduckgo("anomaly detection", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results", \
        "裸子串命中不是人机验证——不得把「没结果」误报成 blocked"


def test_ddg_challenge_page_is_blocked(tmp_path, monkeypatch):
    """anomaly.js 挑战页脚本引用 + 无结果标记，两个特征同时成立才报 blocked。"""
    page = '<html><head><script src="/dist/anomaly.js"></script></head><body>prove you are human</body></html>'
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (page, 200))
    res = cn.search_duckduckgo("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "blocked"
    assert "人机验证" in res["note_zh"]


def test_ddg_anomaly_js_with_results_is_not_blocked(tmp_path, monkeypatch):
    """页面引用了 anomaly.js 但结果标记还在（解析却失败）→ 不是挑战页，报 no_results。"""
    page = '<html><head><script src="/dist/anomaly.js"></script></head><body><span class="result__a">x</span></body></html>'
    monkeypatch.setattr(cn, "fetch_text", lambda url, **kw: (page, 200))
    res = cn.search_duckduckgo("x", project_root=tmp_path)
    assert res["ok"] is False and res["error"] == "no_results"


# ---------------------------------------------------------------- 重试留痕 + JSON 不重试

def test_fetch_json_bad_json_fails_without_retry(tmp_path, monkeypatch):
    """JSON 解析失败是对端形状问题（确定性），不得当瞬时错误退避重试。"""
    calls: list[str] = []
    monkeypatch.setattr(cn, "fetch_text",
                        lambda url, **kw: (calls.append(url) or ("这不是合法 JSON", 200)))
    with pytest.raises(cn._NetError) as exc_info:
        cn.fetch_json("https://example.org/x")
    assert len(calls) == 1, "JSON 解析失败不得重试（此前会白打两次）"
    assert "不是合法 JSON" in str(exc_info.value) and "没有重试" in str(exc_info.value)


def test_raw_get_attempts_out_param_counts_retries(monkeypatch):
    """_raw_get 的 attempts 出参如实回填实际请求次数（429 重试两次后成功）。"""
    import urllib.error

    calls = {"n": 0}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n=-1):
            return b"{}"

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(req.full_url, 429, "too many", None, None)
        return _Resp()

    monkeypatch.setattr(cn.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cn.time, "sleep", lambda s: None)
    monkeypatch.setattr(cn, "_polite_wait", lambda host, interval: None)
    att = [1]
    body, status = cn._raw_get("https://example.org/x", timeout=1, min_interval=0,
                               headers={}, attempts=att)
    assert status == 200 and calls["n"] == 3
    assert att[0] == 3, "实际请求次数必须带出给账本"


def test_fetch_json_logged_writes_attempts_into_ledger(tmp_path, monkeypatch):
    """发生过重试的请求，账本条目带 attempts；一次就成的请求不污染条目形状。"""
    def fake_retried(url, **kw):
        kw["attempts"][0] = 3  # 模拟重试两次后成功
        return ({"hits": [1]}, 200)

    monkeypatch.setattr(cn, "fetch_json", fake_retried)
    cn.fetch_json_logged("https://example.org/x", project_root=tmp_path,
                         endpoint="https://example.org", query="q")
    lines = _ledger_lines(tmp_path)
    assert lines[0]["attempts"] == 3, "重试必须留痕：attempts 写进账本"

    monkeypatch.setattr(cn, "fetch_json", lambda url, **kw: ({"hits": []}, 200))
    cn.fetch_json_logged("https://example.org/y", project_root=tmp_path,
                         endpoint="https://example.org", query="q2")
    lines = _ledger_lines(tmp_path)
    assert "attempts" not in lines[1], "一次就成的请求不新增字段（账本形状只增于重试时）"
