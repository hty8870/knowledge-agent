# -*- coding: utf-8 -*-
"""corpus_curation.check_updates（curate.check_updates 能力本体）的确定性门。

钉的是设计约定的三条：
  1. **online 分支**：ArrayExpress 走 `_fetch_logged`（monkeypatch 注入假响应）；ENCODE/10x
     （扩展）、HCA（Azul 接入）与 GEO（E-utilities 接入）
     走 corpus_net 工具组（monkeypatch `encode_recent_items` / `tenx_dataset_items` /
     `hca_recent_items` / `geo_recent_items` 出口）——真在线语义（最新清单 vs 本地键集合差分）
     全离线可测；
  2. **snapshot 分支**（CELLxGENE/EBI SCEA/HuBMAP/SCP 离线快照源 + ENCODE/10x/HCA/GEO/Zenodo 拉不到时的
     如实降级）：如实给条数/快照日期（找不到就 null 并说明）+ 官网入口 + 「联网搜…」指路，
     **不伪造在线能力**；
  3. **只读、不落盘、不抛**：网络失败 → note_zh 如实写明；未注册来源名 → unknown 条目。
另钉 `POST /api/curate/check-updates` 端点契约（无 token、same-origin 闸、响应形状只增不减）。
"""
import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus import corpus_net
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


@pytest.fixture(autouse=True)
def _corpus_net_offline(monkeypatch):
    """ENCODE/10x/HCA/GEO/Zenodo/refine.bio 的在线通道默认钉成失败降级（确定性禁网）；测在线支的用例自己覆盖。"""
    monkeypatch.setattr(corpus_net, "encode_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（ENCODE）"})
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（10x）"})
    monkeypatch.setattr(corpus_net, "hca_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（HCA）"})
    monkeypatch.setattr(corpus_net, "geo_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（GEO）"})
    monkeypatch.setattr(corpus_net, "zenodo_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（Zenodo）"})
    monkeypatch.setattr(corpus_net, "refinebio_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（refine.bio）"})


def _write_external(root, name, payload):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_base(root, name, records):
    base = root / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    (base / name).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")


AE_SNAPSHOT = {
    "source": "ArrayExpress",
    "records": [
        {"dataset_uid": "ae:E-MTAB-100", "dataset_name": "已有的一",
         "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-100"},
        {"dataset_uid": "ae:E-MTAB-101", "dataset_name": "已有的二",
         "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-101"},
    ],
}


# ---------------------------------------------------------------- online 分支（monkeypatch _fetch_logged）

def test_online_check_compares_recent_hits_against_local_accessions(tmp_path, monkeypatch):
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    seen_urls: list[str] = []

    def fake_fetch_logged(url, **kwargs):
        seen_urls.append(url)
        return {"hits": [
            {"accession": "E-MTAB-200", "title": "新的肺数据"},
            {"accession": "E-MTAB-100", "title": "本地已有"},
            {"accession": "E-MTAB-201", "title": "新的脑数据"},
        ]}

    monkeypatch.setattr(cc, "_fetch_logged", fake_fetch_logged)
    res = cc.check_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "online"
    assert entry["online_recent"] == 3
    assert entry["local_count"] == 2
    assert entry["new_count"] == 2
    assert [c["accession"] for c in entry["new_candidates"]] == ["E-MTAB-200", "E-MTAB-201"]
    assert entry["new_candidates"][0]["title"] == "新的肺数据"
    assert "sortBy=release_date" in seen_urls[0], "在线比对必须按发布日期取最近条目"
    assert "目录里还没有" in entry["note_zh"]


def test_online_check_with_nothing_new_says_so(tmp_path, monkeypatch):
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-100", "title": "已有"},
    ]})
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert entry["new_candidates"] == []
    assert "都有了" in entry["note_zh"]


def test_online_check_network_failure_never_raises(tmp_path, monkeypatch):
    """网络失败不抛：note_zh 如实写明，local_count 照给，online_recent/new_candidates 置空。
     AE 支与 net 支统一降级语义——在线比对没完成一律 mode="snapshot"
    （此前 AE 支保留 mode="online"，同一语义两种 mode，下游按 mode 判断时行为不一）。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)

    def boom(url, **kwargs):
        raise cc.CurateError("network_error", "官方源请求失败：HTTP 503")

    monkeypatch.setattr(cc, "_fetch_logged", boom)
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["online_recent"] is None
    assert entry["new_candidates"] is None
    assert entry["local_count"] == 2
    assert "没能完成" in entry["note_zh"] and "503" in entry["note_zh"]


# ---------------------------------------------------------------- snapshot 分支（纯本地）

def test_snapshot_sources_report_local_facts_honestly(tmp_path):
    _write_external(tmp_path, "cellxgene.json", {
        "source": "CELLxGENE Discover", "record_count": 2,
        "records": [{"dataset_name": "甲"}, {"dataset_name": "乙"}],
    })
    _write_base(tmp_path, "10x-Visium.json", [{"dataset_name": "丙"}] * 3)
    res = cc.check_updates(["cellxgene", "10x"], project_root=tmp_path)
    by_source = {e["source"]: e for e in res["sources"]}
    cxg = by_source["cellxgene"]
    assert cxg["mode"] == "snapshot"
    assert cxg["local_count"] == 2
    assert cxg["snapshot_date"] is None, "文件里没有显式日期元信息就诚实缺省"
    assert "未在文件里标注" in cxg["note_zh"]
    assert "本地副本" in cxg["note_zh"] and "不能在线核对" in cxg["note_zh"]
    assert "联网搜" in cxg["note_zh"]
    assert cxg["site_url"] == "https://cellxgene.cziscience.com/"
    tenx = by_source["10x"]
    assert tenx["mode"] == "snapshot"
    assert tenx["local_count"] == 3, "10x 读 base 冻结基准（只读）做条数统计"
    assert tenx["label"] == "10x Genomics"


def test_snapshot_date_is_reported_when_explicitly_declared(tmp_path):
    """文件里显式声明了快照日期就给；来源是文件元信息，不是文件 mtime。"""
    _write_external(tmp_path, "hca.json", {
        "source": "Human Cell Atlas", "snapshot_date": "2026-07-20",
        "records": [{"dataset_name": "甲"}],
    })
    entry = cc.check_updates(["hca"], project_root=tmp_path)["sources"][0]
    assert entry["snapshot_date"] == "2026-07-20"
    assert "2026-07-20" in entry["note_zh"]


def test_snapshot_source_with_missing_file_says_so(tmp_path):
    entry = cc.check_updates(["encode"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["local_count"] == 0
    assert entry["snapshot_date"] is None
    assert "没有找到" in entry["note_zh"]
    assert entry["site_url"] == "https://www.encodeproject.org/"


# ---------------------------------------------------------------- 入口纪律：只读、不抛、逐源隔离

def test_unknown_source_name_gets_an_honest_unknown_entry(tmp_path):
    res = cc.check_updates(["figshare", "10x"], project_root=tmp_path)
    modes = {e["source"]: e["mode"] for e in res["sources"]}
    assert modes["figshare"] == "unknown"
    assert modes["10x"] == "snapshot", "一个名字认不出不许连累其余来源"
    assert "不认识来源" in res["sources"][0]["note_zh"]


def test_default_checks_every_registered_source(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": []})
    _write_base(tmp_path, "10x-Visium.json", [{"dataset_name": "丙"}])
    res = cc.check_updates(project_root=tmp_path)
    assert {e["source"] for e in res["sources"]} == set(cc.CHECK_UPDATE_SOURCES)
    assert res["checked_at"]
    assert "ArrayExpress" in res["hint_zh"], "hint 必须如实说在线比对只覆盖有适配器的源"


def test_check_updates_writes_nothing(tmp_path, monkeypatch):
    """只读闸：整个调用不往 project_root 写任何文件（请求账本由 _fetch_logged 真身写，
    这里 monkeypatch 掉了它，所以连账本都不该有）。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": []})
    before = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    cc.check_updates(project_root=tmp_path)
    after = {p.relative_to(tmp_path) for p in tmp_path.rglob("*")}
    assert before == after


# ---------------------------------------------------------------- 端点 POST /api/curate/check-updates

def test_endpoint_happy_path(monkeypatch):
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-999", "title": "新的"},
    ]})
    res = client.post("/api/curate/check-updates", json={"sources": ["ArrayExpress", "10x"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    result = body["result"]
    assert result["checked_at"]
    by_source = {e["source"]: e for e in result["sources"]}
    assert by_source["arrayexpress"]["mode"] == "online"
    assert by_source["arrayexpress"]["online_recent"] == 1
    assert by_source["10x"]["mode"] == "snapshot"
    assert by_source["10x"]["local_count"] > 0, "真仓库 base 基准的条数统计"
    assert "confirm_token" not in res.text, "只读端点不该出现两步确认的 token"


def test_endpoint_rejects_unknown_fields():
    res = client.post("/api/curate/check-updates", json={"sources": ["10x"], "limitt": 5})
    assert res.status_code == 422


def test_endpoint_rejects_cross_origin_posts():
    res = client.post("/api/curate/check-updates", json={"sources": ["10x"]},
                      headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


# ---------------------------------------------------------------- ENCODE 在线比对（corpus_net 出口 mock）

def _write_encode_snapshot(root):
    _write_external(root, "encode.json", {
        "source": "ENCODE",
        "snapshot_date": "2026-07-01",
        "records": [
            {"dataset_uid": "encode:ENCSR009WQK", "dataset_name": "已有的实验",
             "url": "https://www.encodeproject.org/experiments/ENCSR009WQK/"},
        ],
    })


def test_encode_online_check_diffs_recent_against_local(tmp_path, monkeypatch):
    _write_encode_snapshot(tmp_path)
    monkeypatch.setattr(corpus_net, "encode_recent_items", lambda **kw: {
        "ok": True,
        "items": [
            {"accession": "ENCSR999NEW", "title": "新的 ChIP-seq",
             "url": "https://www.encodeproject.org/experiments/ENCSR999NEW/"},
            {"accession": "ENCSR009WQK", "title": "本地已有",
             "url": "https://www.encodeproject.org/experiments/ENCSR009WQK/"},
        ],
    })
    entry = cc.check_updates(["encode"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online"
    assert entry["online_recent"] == 2
    assert entry["local_count"] == 1
    assert entry["new_count"] == 1
    assert entry["new_candidates"] == [{"accession": "ENCSR999NEW", "title": "新的 ChIP-seq"}]
    assert "目录里还没有" in entry["note_zh"]


def test_encode_online_failure_degrades_to_snapshot_honestly(tmp_path):
    """拉不到 → mode 如实降级 snapshot（二值契约内），note 写清原因 + 本地事实，不伪造比对完成。"""
    _write_encode_snapshot(tmp_path)  # autouse 夹具已把 encode_recent_items 钉成失败
    entry = cc.check_updates(["encode"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["online_recent"] is None and entry["new_candidates"] is None
    assert entry["local_count"] == 1
    assert entry["snapshot_date"] == "2026-07-01"
    assert "没能完成" in entry["note_zh"] and "改为报告本地副本的情况" in entry["note_zh"]
    assert "测试禁网" in entry["note_zh"], "降级原因要如实写进 note"
    assert "2026-07-01" in entry["note_zh"]


# ---------------------------------------------------------------- 10x 在线比对（官网清单 vs base 冻结基准，只读）

def test_tenx_online_check_diffs_site_listing_against_base(tmp_path, monkeypatch):
    _write_base(tmp_path, "10x-Visium.json", [
        {"dataset_uid": "visium-ff-mouse-brain", "dataset_name": "Visium FF Mouse Brain",
         "url": "https://www.10xgenomics.com/datasets/visium-ff-mouse-brain"},
    ])
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True,
        "items": [
            {"accession": "visium-ff-mouse-brain", "title": "Visium FF Mouse Brain",
             "url": "https://www.10xgenomics.com/datasets/visium-ff-mouse-brain"},
            {"accession": "visium-hd-human-lung", "title": "Visium HD Human Lung",
             "url": "https://www.10xgenomics.com/datasets/visium-hd-human-lung"},
        ],
    })
    entry = cc.check_updates(["10x"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online"
    assert entry["online_recent"] == 2
    assert entry["local_count"] == 1
    assert entry["new_count"] == 1
    assert entry["new_candidates"][0]["accession"] == "visium-hd-human-lung"
    assert "目录里还没有" in entry["note_zh"]


def test_tenx_online_check_matches_by_normalized_name_too(tmp_path, monkeypatch):
    """slug 对不上时归一化 dataset_name 也要能认亲（双通道键，防误报新增）。"""
    _write_base(tmp_path, "10x-Visium.json", [
        {"dataset_uid": "old-slug", "dataset_name": "5k Mouse Kidney, Chromium GEM-X"},
    ])
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "new-slug-xyz", "title": "5k Mouse Kidney, Chromium GEM-X",
                   "url": "https://www.10xgenomics.com/datasets/new-slug-xyz"}],
    })
    entry = cc.check_updates(["10x"], project_root=tmp_path)["sources"][0]
    assert entry["new_count"] == 0 and "都有了" in entry["note_zh"]


def test_tenx_structure_change_degrades_to_snapshot(tmp_path, monkeypatch):
    """10x 页面结构变了（parse_changed）→ 降级 snapshot + 如实说明，不炸链不伪造。"""
    _write_base(tmp_path, "10x-Visium.json", [{"dataset_name": "甲"}] * 4)
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": False, "items": [], "error": "parse_changed",
        "note_zh": "10x 数据集页结构疑似变化，未能解析出条目。"})
    entry = cc.check_updates(["10x"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["local_count"] == 4
    assert "没能完成" in entry["note_zh"] and "结构疑似变化" in entry["note_zh"]


# ---------------------------------------------------------------- 契约回归（只增不减 + mode 二值）

def test_check_updates_response_contract_fields_regression(tmp_path, monkeypatch):
    """响应形状红线：每个来源条目仍含既有全部字段，mode 仍是 online/snapshot/unknown 之一。"""
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": []})
    _write_base(tmp_path, "10x-Visium.json", [{"dataset_name": "丙"}])
    res = cc.check_updates(project_root=tmp_path)
    assert set(res) >= {"checked_at", "sources", "hint_zh"}
    for entry in res["sources"]:
        assert set(entry) >= {"source", "label", "local_count", "site_url", "mode", "note_zh"}, (
            f"{entry.get('source')} 缺既有契约字段：{sorted(entry)}")
        assert entry["mode"] in {"online", "snapshot", "unknown"}
    by_source = {e["source"]: e for e in res["sources"]}
    assert by_source["arrayexpress"]["mode"] == "online", "AE 在线支拿到空 hits 也是 online"
    assert by_source["encode"]["mode"] == "snapshot", "ENCODE 拉不到如实降级"
    assert by_source["10x"]["mode"] == "snapshot"


def test_endpoint_contract_with_new_online_sources(monkeypatch):
    """端点级回归：hint_zh 如实列出全部在线源；既有字段一个不少。"""
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-999", "title": "新的"},
    ]})
    res = client.post("/api/curate/check-updates", json={"sources": ["ArrayExpress", "ENCODE", "10x"]})
    assert res.status_code == 200, res.text
    result = res.json()["result"]
    assert "ENCODE" in result["hint_zh"] and "10x Genomics" in result["hint_zh"], (
        "hint 必须如实说明在线比对覆盖到 ENCODE/10x")
    by_source = {e["source"]: e for e in result["sources"]}
    assert by_source["arrayexpress"]["mode"] == "online"
    assert by_source["encode"]["mode"] == "snapshot", "autouse 禁网夹具 → 如实降级"
    assert by_source["10x"]["mode"] == "snapshot"
    for e in result["sources"]:
        assert set(e) >= {"source", "label", "local_count", "site_url", "mode", "note_zh"}


# ---------------------------------------------------------------- HCA 在线比对（Azul 最近条目 vs 本地快照）

def _write_hca_snapshot(root):
    _write_external(root, "hca.json", {
        "source": "Human Cell Atlas",
        "snapshot_date": "2026-07-20",
        "records": [
            {"dataset_uid": "hca:74b6d569-3b11-42ef-b6b1-a0454522b4a0", "dataset_name": "已有项目",
             "url": "https://data.humancellatlas.org/explore/projects/74b6d569-3b11-42ef-b6b1-a0454522b4a0"},
        ],
    })


def test_hca_online_check_diffs_recent_against_local(tmp_path, monkeypatch):
    _write_hca_snapshot(tmp_path)
    monkeypatch.setattr(corpus_net, "hca_recent_items", lambda **kw: {
        "ok": True,
        "items": [
            {"accession": "984ce0a2-682d-47a3-b80e-1354dfe51ca3", "title": "新的脑图谱",
             "url": "https://data.humancellatlas.org/explore/projects/984ce0a2-682d-47a3-b80e-1354dfe51ca3"},
            {"accession": "74b6d569-3b11-42ef-b6b1-a0454522b4a0", "title": "本地已有"},
        ],
    })
    entry = cc.check_updates(["hca"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online"
    assert entry["online_recent"] == 2
    assert entry["local_count"] == 1
    assert entry["new_count"] == 1
    assert entry["new_candidates"] == [
        {"accession": "984ce0a2-682d-47a3-b80e-1354dfe51ca3", "title": "新的脑图谱"}]
    assert "HCA 官方源最近" in entry["note_zh"] and "目录里还没有" in entry["note_zh"]


def test_hca_online_check_matches_by_url_tail_too(tmp_path, monkeypatch):
    """uid 前缀对不上时 URL 末段 uuid 也要能认亲（双通道键，防误报新增）。"""
    _write_external(tmp_path, "hca.json", {
        "source": "Human Cell Atlas",
        "records": [{"dataset_name": "无 uid 的老快照记录",
                     "url": "https://data.humancellatlas.org/explore/projects/74B6D569-3B11-42EF-B6B1-A0454522B4A0"}],
    })
    monkeypatch.setattr(corpus_net, "hca_recent_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "74b6d569-3b11-42ef-b6b1-a0454522b4a0", "title": "本地已有"}],
    })
    entry = cc.check_updates(["Human Cell Atlas"], project_root=tmp_path)["sources"][0]
    assert entry["new_count"] == 0 and "都有了" in entry["note_zh"]


def test_hca_online_failure_degrades_to_snapshot_honestly(tmp_path):
    """拉不到 → mode 如实降级 snapshot（二值契约内），note 写清原因 + 本地事实。"""
    _write_hca_snapshot(tmp_path)  # autouse 夹具已把 hca_recent_items 钉成失败
    entry = cc.check_updates(["hca"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["online_recent"] is None and entry["new_candidates"] is None
    assert entry["local_count"] == 1 and entry["snapshot_date"] == "2026-07-20"
    assert "没能完成" in entry["note_zh"] and "测试禁网（HCA）" in entry["note_zh"]
    assert "2026-07-20" in entry["note_zh"]


# ---------------------------------------------------------------- GEO 在线比对（E-utilities pdat 窗口最近条目 vs 本地 GSE 编号）

def _write_geo_snapshot(root):
    _write_external(root, "geo.json", {
        "source": "NCBI GEO",
        "snapshot_date": "2026-07-22",
        "records": [
            {"dataset_uid": "geo:GSE3642", "dataset_name": "已有的 Series",
             "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE3642"},
        ],
    })


def test_geo_online_check_diffs_recent_against_local(tmp_path, monkeypatch):
    _write_geo_snapshot(tmp_path)
    monkeypatch.setattr(corpus_net, "geo_recent_items", lambda **kw: {
        "ok": True,
        "items": [
            {"accession": "GSE335566", "title": "新的肺 scRNA-seq",
             "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE335566",
             "date": "2026-08-04"},
            {"accession": "GSE3642", "title": "本地已有"},
        ],
    })
    entry = cc.check_updates(["geo"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online"
    assert entry["online_recent"] == 2
    assert entry["local_count"] == 1
    assert entry["new_count"] == 1
    assert entry["new_candidates"] == [{"accession": "GSE335566", "title": "新的肺 scRNA-seq"}]
    assert "GEO 官方源最近" in entry["note_zh"] and "目录里还没有" in entry["note_zh"]


def test_geo_online_check_matches_by_url_regex_too(tmp_path, monkeypatch):
    """uid 前缀对不上时 url/public_accession 里的 GSE 编号也要能认亲（双通道键，防误报新增）。"""
    _write_external(tmp_path, "geo.json", {
        "source": "NCBI GEO",
        "records": [{"dataset_name": "无 uid 的老快照记录",
                     "public_accession": "gse3642"}],
    })
    monkeypatch.setattr(corpus_net, "geo_recent_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "GSE3642", "title": "本地已有"}],
    })
    entry = cc.check_updates(["ncbi geo"], project_root=tmp_path)["sources"][0]
    assert entry["new_count"] == 0 and "都有了" in entry["note_zh"]


def test_geo_online_failure_degrades_to_snapshot_honestly(tmp_path):
    """拉不到（本机到 NCBI 的链路不可用是常态之一）→ mode 如实降级 snapshot（二值契约内），
    note 写清原因 + 本地事实，不伪造比对完成。"""
    _write_geo_snapshot(tmp_path)  # autouse 夹具已把 geo_recent_items 钉成失败
    entry = cc.check_updates(["GEO"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["online_recent"] is None and entry["new_candidates"] is None
    assert entry["local_count"] == 1 and entry["snapshot_date"] == "2026-07-22"
    assert "没能完成" in entry["note_zh"] and "测试禁网（GEO）" in entry["note_zh"]
    assert "2026-07-22" in entry["note_zh"]


# ---------------------------------------------------------------- 快照损坏诚实提示

def test_corrupt_snapshot_diff_carries_honest_warning(tmp_path, monkeypatch):
    """快照损坏 → accession 集合变空集 → diff 把远端全量当「新增」（运行时复现已确认虚报）。
    修复后：note 必须如实写明「新增数字可能虚报」，不再与真空快照同形。"""
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "arrayexpress.json").write_text(
        '{"records": [{"dataset_uid": "ae:E-MTAB-100"', encoding="utf-8")  # 半截 JSON
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-100", "title": "快照里其实已有"},
    ]})
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert "损坏" in entry["note_zh"] and "可能虚报" in entry["note_zh"], \
        "快照损坏必须如实提示，不许与「空库」同形"


def test_intact_snapshot_has_no_corrupt_warning(tmp_path, monkeypatch):
    """对照：快照完好时 note 不带损坏提示（不污染正常路径）。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-100", "title": "已有"},
    ]})
    entry = cc.check_updates(["arrayexpress"], project_root=tmp_path)["sources"][0]
    assert "损坏" not in entry["note_zh"]


def test_corrupt_snapshot_net_branch_warns(tmp_path, monkeypatch):
    """net 支：快照损坏同样如实提示（ENCODE 通道）。"""
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "encode.json").write_text("{ 半截 JSON", encoding="utf-8")
    monkeypatch.setattr(corpus_net, "encode_recent_items", lambda **kw: {
        "ok": True, "items": [{"accession": "ENCSR999ZZZ", "title": "x"}]})
    entry = cc.check_updates(["encode"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "online"
    assert "损坏" in entry["note_zh"] and "可能虚报" in entry["note_zh"]


def test_unknown_net_kind_degrades_honestly(tmp_path, monkeypatch):
    """守卫：未接线的 net_kind（拼错/新增源忘接线）不许静默按 10x 通道比对——
    如实降级 snapshot 并写明真实原因。"""
    _write_external(tmp_path, "encode.json", {"records": []})
    monkeypatch.setitem(cc.CHECK_UPDATE_SOURCES, "encode",
                        {**cc.CHECK_UPDATE_SOURCES["encode"], "net_kind": "bogus_kind"})
    entry = cc.check_updates(["encode"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert "bogus_kind" in entry["note_zh"] and "没有对应的在线比对通道" in entry["note_zh"]


# ---------------------------------------------------------------- 快照损坏诚实口径

def test_corrupt_offline_snapshot_not_reported_as_zero(tmp_path):
    """离线源快照损坏 → 不许报「本地副本 0 条」（假象）——note 如实说损坏、条数不可知，
    结构化字段带 snapshot_error。"""
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "cellxgene.json").write_text("{ 半截 JSON", encoding="utf-8")
    entry = cc.check_updates(["cellxgene"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert "损坏" in entry["note_zh"] and "不可知" in entry["note_zh"]
    assert "0 条" not in entry["note_zh"]
    assert entry["snapshot_error"]


def test_corrupt_snapshot_in_degrade_path_is_honest(tmp_path):
    """降级支：在线拉不到 + 本地快照损坏 → note 两件事都说，不再引用假象条数。"""
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "geo.json").write_text("{ 半截 JSON", encoding="utf-8")
    # autouse 夹具已把 geo_recent_items 钉成失败 → 走 _degrade_to_snapshot
    entry = cc.check_updates(["GEO"], project_root=tmp_path)["sources"][0]
    assert entry["mode"] == "snapshot"
    assert "没能完成" in entry["note_zh"] and "损坏" in entry["note_zh"]
    assert "本地 0 条" not in entry["note_zh"]
    assert entry["snapshot_error"]
