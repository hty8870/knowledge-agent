# -*- coding: utf-8 -*-
"""corpus_curation.sync_updates（curate.sync_updates 能力本体，「工作流即工具」）的确定性门。

钉的是复合流的四条设计契约：
  1. **步骤顺序写死在代码里**：先 check_updates 只读比对，再把「能在线比对且有入库适配器」
     来源的疑似新增逐编号搜回、合并成一个 sync 批次文件入库（upload_* 命名空间 + 账本 + 回收站可撤）；
  2. **不重复入库**：疑似新增只比对官方快照，本层再用外部库 upload_* 的 dataset_uid 集合拦截
     「以前 sync/联网搜/手动导入过」的条目——二次运行零写入；
  3. **闭不了环如实说**：无入库适配器的在线源（ENCODE）与离线快照源逐条写明哪段做不到，
     不伪造闭环；单编号搜回失败只进该源 note，不连累其余（10x/HCA、
      起 GEO 已接入入库适配器，闭环集 = ArrayExpress / 10x / HCA / GEO）；
  4. **端点契约**：`POST /api/curate/sync-updates` 无 token（原子调用无信任边界）、same-origin 闸、
     响应形状 {checked_at, sources[], imported_total, hint_zh}。
全离线：`_fetch_logged` / 适配器 search / corpus_net 全部 monkeypatch；落盘全在 tmp_path。
"""
import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus import corpus_net
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

AE_SNAPSHOT = {
    "source": "ArrayExpress",
    "records": [
        {"dataset_uid": "ae:E-MTAB-100", "dataset_name": "已有的一",
         "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-100"},
    ],
}


@pytest.fixture(autouse=True)
def _corpus_net_offline(monkeypatch):
    """ENCODE/10x/HCA/GEO 的在线通道默认钉成失败降级（确定性禁网）；测在线支的用例自己覆盖。"""
    monkeypatch.setattr(corpus_net, "encode_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（ENCODE）"})
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（10x）"})
    monkeypatch.setattr(corpus_net, "hca_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（HCA）"})
    monkeypatch.setattr(corpus_net, "geo_recent_items", lambda **kw: {
        "ok": False, "items": [], "error": "network_error", "note_zh": "测试禁网（GEO）"})


@pytest.fixture
def ae_online(monkeypatch):
    """ArrayExpress 在线比对假数据：最近 3 条里 E-MTAB-200/201 本地快照没有。"""
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-200", "title": "新的肺"},
        {"accession": "E-MTAB-100", "title": "本地已有"},
        {"accession": "E-MTAB-201", "title": "新的脑"},
    ]})


@pytest.fixture
def ae_search(monkeypatch):
    """适配器 search 假出口：按编号精确回一条记录（dataset_uid 后缀=编号）。"""
    calls: list[str] = []

    def fake_search(query, species="", limit=20, project_root=None):
        calls.append(query)
        if str(query).startswith("E-MTAB"):
            return ([{"dataset_uid": f"ae:{query}", "dataset_name": f"记录{query}",
                      "url": f"https://x/{query}"}], [])
        return ([], [])

    original = dict(cc.SOURCE_ADAPTERS["arrayexpress"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "arrayexpress", {**original, "search": fake_search})
    return calls


def _write_external(root, name, payload):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    (ext / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _external_files(root):
    ext = root / "database" / "external"
    return sorted(p.name for p in ext.glob("*.json")) if ext.is_dir() else []


# ---------------------------------------------------------------- 闭环：检查 → 自动入库

def test_sync_supply_no_longer_throttles_import_cap(tmp_path, monkeypatch, ae_search):
    """new_candidates 供给上限必须 ≥ 每源入库上限。

    修复前 `_NEW_CANDIDATES_SHOW=5` 把 `_SYNC_MAX_IMPORT=10` 卡死在 5——7 条疑似新增
    只能入库 5 条，note 还错写成「一次最多自动入库 10 条」。钉：7 条新增默认 cap 下
    7 条全入库；供给端回传的候选数也不再被截到 5。
    """
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": f"E-MTAB-2{i:02d}", "title": f"新条目{i}"} for i in range(7)
    ] + [{"accession": "E-MTAB-100", "title": "本地已有"}]})
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["new_count"] == 7
    assert entry["imported_count"] == 7, "供给端不得卡死每源 10 条的入库上限"
    assert res["imported_total"] == 7
    assert "没有自动入库" not in entry["note_zh"]
    # 供给上限常量本身也必须盖住入库上限（防再次错位）
    assert cc._NEW_CANDIDATES_SHOW >= cc._SYNC_MAX_IMPORT


def test_sync_skips_broken_external_file_and_reports(tmp_path, ae_online, ae_search):
    """external 里一个坏 JSON 不再炸掉整个 sync——

    跳过它、照常完成同步，并在结果的 skipped_files 里如实列出坏文件名（不静默失明）。
    """
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    ext = tmp_path / "database" / "external"
    (ext / "upload_broken.json").write_text("{ 这不是合法 JSON", encoding="utf-8")
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["imported_count"] == 2, "坏文件不得连累正常源的同步"
    assert res["skipped_files"] == ["upload_broken.json"]
    assert "upload_broken.json" in res["hint_zh"] and "损坏" in res["hint_zh"]


def test_sync_clean_external_reports_no_skipped_files(tmp_path, ae_online, ae_search):
    """对照组：没有坏文件时 skipped_files 是空清单、hint 不含损坏提示。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    assert res["skipped_files"] == []
    assert "损坏" not in res["hint_zh"]


def test_sync_imports_new_candidates_in_one_batch_file(tmp_path, ae_online, ae_search):
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "online"
    assert entry["new_count"] == 2
    assert entry["imported_count"] == 2
    assert res["imported_total"] == 2
    assert entry["filename"] and entry["filename"].startswith("upload_")
    assert "curate_sync_arrayexpress" in entry["filename"]
    assert entry["filename"] in _external_files(tmp_path)
    assert entry["imported_titles"] == ["记录E-MTAB-200", "记录E-MTAB-201"]
    # 两个编号合并进**一个**批次文件，不是一编号一个文件
    assert len([f for f in _external_files(tmp_path) if "curate_sync" in f]) == 1
    written = json.loads((tmp_path / "database" / "external" / entry["filename"]).read_text("utf-8"))
    assert {r["dataset_uid"] for r in written["records"]} == {"ae:E-MTAB-200", "ae:E-MTAB-201"}


def test_sync_second_run_does_not_reimport(tmp_path, ae_online, ae_search):
    """疑似新增只比对官方快照；本层必须拦「已在外部库」的条目——二次运行零写入。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    first = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    assert first["imported_total"] == 2
    calls_after_first = list(ae_search)
    second = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    assert second["imported_total"] == 0
    entry = second["sources"][0]
    assert "已经在外部库" in entry["note_zh"] or "已在外部库" in entry["note_zh"]
    # 已在外部库的条目直接跳过，连在线搜回都不发起
    assert ae_search == calls_after_first


def test_sync_no_new_writes_nothing(tmp_path, monkeypatch, ae_search):
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {
        "hits": [{"accession": "E-MTAB-100", "title": "本地已有"}]})
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    assert res["imported_total"] == 0
    assert not [f for f in _external_files(tmp_path) if "curate_sync" in f]
    assert ae_search == []


# ---------------------------------------------------------------- 闭不了环：如实写明哪段做不到

def test_sync_non_closable_source_says_so(tmp_path, monkeypatch):
    """ENCODE：能在线比对但无入库适配器 → 检到疑似新增也必须如实说「不能自动入库」，零写入。
    （前本例钉的是 10x；10x/HCA 该批已接适配器，换成仍闭不了环的 ENCODE 继续钉。）"""
    monkeypatch.setattr(corpus_net, "encode_recent_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "ENCSR999NEW", "title": "新的 ChIP-seq"}],
    })
    res = cc.sync_updates(["ENCODE"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["new_count"] == 1
    assert entry["imported_count"] == 0
    assert entry["filename"] is None
    assert "不能自动入库" in entry["note_zh"]
    assert res["imported_total"] == 0


def test_sync_tenx_closes_loop(tmp_path, monkeypatch):
    """ 起 10x 在闭环集里：在线比对检出疑似新增 → 适配器按 slug 搜回 → 自动入库。"""
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True, "total": 1,
        "items": [{"accession": "visium-hd-new-item", "title": "10x 新条目",
                   "url": "https://www.10xgenomics.com/datasets/visium-hd-new-item"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        if str(query) == "visium-hd-new-item":
            return ([{"dataset_uid": "10x:visium-hd-new-item", "dataset_name": "10x 新条目",
                      "url": "https://www.10xgenomics.com/datasets/visium-hd-new-item",
                      "source": "10x Genomics"}], [])
        return ([], [])

    original = dict(cc.SOURCE_ADAPTERS["10x"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "10x", {**original, "search": fake_search})
    res = cc.sync_updates(["10x"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "online" and entry["new_count"] == 1
    assert entry["imported_count"] == 1 and "curate_sync_10x" in entry["filename"]
    assert res["imported_total"] == 1
    assert entry["filename"] in _external_files(tmp_path)


def test_sync_partial_failure_keeps_other_sources_receipts(tmp_path, monkeypatch):
    """（验证）：第二源写入抛错只毁该源条目（如实记错误），
    第一源的入库回执（filename/imported_count/imported_total）一条不丢。"""
    monkeypatch.setattr(cc, "_SYNC_TOTAL_MAX_IMPORT", 30)
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-200", "title": "新的肺"},
        {"accession": "E-MTAB-100", "title": "本地已有"},
    ]})
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True, "total": 1,
        "items": [{"accession": "visium-hd-new-item", "title": "10x 新条目",
                   "url": "https://www.10xgenomics.com/datasets/visium-hd-new-item"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        q = str(query)
        if q.startswith("E-MTAB"):
            return ([{"dataset_uid": f"ae:{q}", "dataset_name": f"记录{q}", "url": f"https://x/{q}"}], [])
        raise RuntimeError("模拟 10x 适配器内部爆炸")

    for src in ("arrayexpress", "10x"):
        original = dict(cc.SOURCE_ADAPTERS[src])
        monkeypatch.setitem(cc.SOURCE_ADAPTERS, src, {**original, "search": fake_search})
    res = cc.sync_updates(["ArrayExpress", "10x"], project_root=tmp_path)
    ae_entry, ten_entry = res["sources"][0], res["sources"][1]
    assert ae_entry["imported_count"] == 1 and ae_entry["filename"], "第一源的回执必须完整保留"
    assert res["imported_total"] == 1
    assert ten_entry["imported_count"] == 0 and ten_entry["filename"] is None
    assert ten_entry.get("error") == "RuntimeError"
    assert "本来源没有写入" in ten_entry["note_zh"] and "其他来源不受影响" in ten_entry["note_zh"]


def test_sync_total_budget_stops_later_sources_with_honest_note(tmp_path, monkeypatch):
    """全请求总预算闸：AE 入满预算后，10x 的疑似新增一条不入，
    note 如实写「总预算已用完、再说一次即可续跑」，不写 10x 批次文件。"""
    monkeypatch.setattr(cc, "_SYNC_TOTAL_MAX_IMPORT", 2)
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-200", "title": "新的肺"},
        {"accession": "E-MTAB-100", "title": "本地已有"},
        {"accession": "E-MTAB-201", "title": "新的脑"},
    ]})
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True, "total": 1,
        "items": [{"accession": "visium-hd-new-item", "title": "10x 新条目",
                   "url": "https://www.10xgenomics.com/datasets/visium-hd-new-item"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        q = str(query)
        if q.startswith("E-MTAB"):
            return ([{"dataset_uid": f"ae:{q}", "dataset_name": f"记录{q}", "url": f"https://x/{q}"}], [])
        if q == "visium-hd-new-item":
            return ([{"dataset_uid": "10x:visium-hd-new-item", "dataset_name": "10x 新条目",
                      "url": "https://x/visium-hd-new-item", "source": "10x Genomics"}], [])
        return ([], [])

    for src in ("arrayexpress", "10x"):
        original = dict(cc.SOURCE_ADAPTERS[src])
        monkeypatch.setitem(cc.SOURCE_ADAPTERS, src, {**original, "search": fake_search})
    res = cc.sync_updates(["ArrayExpress", "10x"], project_root=tmp_path)
    assert res["imported_total"] == 2, "预算 2 条全给先到的 AE，总写入不得超预算"
    ten = res["sources"][1]
    assert ten["source"] == "10x" and ten["imported_count"] == 0
    assert "总预算" in ten["note_zh"] and "再说一次" in ten["note_zh"]
    assert not [f for f in _external_files(tmp_path) if "curate_sync_10x" in f]


def test_sync_total_budget_shrinks_per_source_cap_with_honest_note(tmp_path, monkeypatch):
    """预算余量收缩每源 cap：预算 3、AE 入 2 后 10x 只能再入 1——note 如实写「将用尽」。"""
    monkeypatch.setattr(cc, "_SYNC_TOTAL_MAX_IMPORT", 3)
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    monkeypatch.setattr(cc, "_fetch_logged", lambda url, **kw: {"hits": [
        {"accession": "E-MTAB-200", "title": "新的肺"},
        {"accession": "E-MTAB-100", "title": "本地已有"},
        {"accession": "E-MTAB-201", "title": "新的脑"},
    ]})
    monkeypatch.setattr(corpus_net, "tenx_dataset_items", lambda **kw: {
        "ok": True, "total": 2,
        "items": [{"accession": "visium-new-a", "title": "10x 新条目甲",
                   "url": "https://www.10xgenomics.com/datasets/visium-new-a"},
                  {"accession": "visium-new-b", "title": "10x 新条目乙",
                   "url": "https://www.10xgenomics.com/datasets/visium-new-b"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        q = str(query)
        if q.startswith("E-MTAB"):
            return ([{"dataset_uid": f"ae:{q}", "dataset_name": f"记录{q}", "url": f"https://x/{q}"}], [])
        if q.startswith("visium-new-"):
            return ([{"dataset_uid": f"10x:{q}", "dataset_name": f"10x 条目{q}",
                      "url": f"https://x/{q}", "source": "10x Genomics"}], [])
        return ([], [])

    for src in ("arrayexpress", "10x"):
        original = dict(cc.SOURCE_ADAPTERS[src])
        monkeypatch.setitem(cc.SOURCE_ADAPTERS, src, {**original, "search": fake_search})
    res = cc.sync_updates(["ArrayExpress", "10x"], project_root=tmp_path)
    assert res["imported_total"] == 3
    ten = res["sources"][1]
    assert ten["imported_count"] == 1, "余量只剩 1，10x 的两条疑似新增只能入一条"
    assert "将用尽" in ten["note_zh"]


def test_sync_hca_closes_loop(tmp_path, monkeypatch):
    """ 起 HCA 在闭环集里：uuid 候选经适配器搜回入库；hint 如实列出闭环三源。"""
    monkeypatch.setattr(corpus_net, "hca_recent_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "984ce0a2-682d-47a3-b80e-1354dfe51ca3", "title": "HCA 新项目"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        if str(query) == "984ce0a2-682d-47a3-b80e-1354dfe51ca3":
            return ([{"dataset_uid": "hca:984ce0a2-682d-47a3-b80e-1354dfe51ca3",
                      "dataset_name": "HCA 新项目", "source": "Human Cell Atlas"}], [])
        return ([], [])

    original = dict(cc.SOURCE_ADAPTERS["hca"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "hca", {**original, "search": fake_search})
    res = cc.sync_updates(["hca"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "online" and entry["new_count"] == 1
    assert entry["imported_count"] == 1 and "curate_sync_hca" in entry["filename"]
    for label in ("ArrayExpress", "10x Genomics", "Human Cell Atlas"):
        assert label in res["hint_zh"], "hint 必须如实列出全部闭环来源"


def test_sync_geo_closes_loop(tmp_path, monkeypatch):
    """ 起 GEO 在闭环集里：GSE 编号候选经适配器搜回入库；hint 如实列出 GEO。"""
    _write_external(tmp_path, "geo.json", {
        "source": "NCBI GEO",
        "records": [{"dataset_uid": "geo:GSE3642", "dataset_name": "已有的 Series",
                     "url": "https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc=GSE3642"}],
    })
    monkeypatch.setattr(corpus_net, "geo_recent_items", lambda **kw: {
        "ok": True,
        "items": [{"accession": "GSE335566", "title": "GEO 新 Series"},
                  {"accession": "GSE3642", "title": "本地已有"}],
    })

    def fake_search(query, species="", limit=20, project_root=None):
        if str(query) == "GSE335566":
            return ([{"dataset_uid": "geo:GSE335566", "dataset_name": "GEO 新 Series",
                      "source": "NCBI GEO"}], [])
        return ([], [])

    original = dict(cc.SOURCE_ADAPTERS["geo"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "geo", {**original, "search": fake_search})
    res = cc.sync_updates(["geo"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "online" and entry["new_count"] == 1
    assert entry["imported_count"] == 1 and "curate_sync_geo" in entry["filename"]
    assert entry["imported_titles"] == ["GEO 新 Series"]
    assert "NCBI GEO" in res["hint_zh"], "hint 必须如实列出 GEO 在闭环集里"


def test_sync_search_failure_is_honest_and_writes_nothing(tmp_path, ae_online, monkeypatch):
    """逐编号搜回全失败 → 零写入 + note 如实写明（不允许「检到了但悄悄没入库」）。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)

    def failing_search(query, species="", limit=20, project_root=None):
        raise cc.CurateError("network_error", "测试：联网查询失败")

    original = dict(cc.SOURCE_ADAPTERS["arrayexpress"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "arrayexpress", {**original, "search": failing_search})
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert res["imported_total"] == 0
    assert entry["filename"] is None
    assert "未写入任何内容" in entry["note_zh"]
    assert not [f for f in _external_files(tmp_path) if "curate_sync" in f]


def test_sync_snapshot_source_passes_through_honest_note(tmp_path, ae_online, ae_search):
    """离线快照源（CELLxGENE）：只有本地副本信息，sync 不伪造在线比对能力。"""
    res = cc.sync_updates(["CELLxGENE"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["mode"] == "snapshot"
    assert entry["imported_count"] == 0
    assert res["imported_total"] == 0


# ---------------------------------------------------------------- 端点契约

def test_sync_updates_endpoint_shape_and_origin_gate(tmp_path, ae_online, ae_search, monkeypatch):
    from dataset_recommender.app import webapp

    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    res = client.post("/api/curate/sync-updates", json={"sources": ["ArrayExpress"]},
                      headers={"Origin": "http://127.0.0.1"})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    result = body["result"]
    assert set(["checked_at", "sources", "imported_total", "hint_zh"]) <= set(result)
    assert result["imported_total"] == 2
    entry = result["sources"][0]
    for key in ("source", "mode", "imported_count", "filename", "imported_titles", "note_zh"):
        assert key in entry, key


def test_sync_updates_endpoint_rejects_foreign_origin(tmp_path, monkeypatch):
    from dataset_recommender.app import webapp

    monkeypatch.setattr(webapp, "PROJECT_ROOT", tmp_path)
    res = client.post("/api/curate/sync-updates", json={"sources": None},
                      headers={"Origin": "https://evil.example.com"})
    assert res.status_code in (403, 400)


# ---------------------------------------------------------------- 去重闸坏文件失明

def test_uid_suffix_collision_no_longer_misjudged(tmp_path, monkeypatch):
    """互为首尾缀的编号不得误判——probe="1234" 不命中 "zenodo:91234"（后缀匹配已改等值判定）。"""
    def fake_search(query, species="", limit=20, project_root=None):
        return ([{"dataset_uid": f"zenodo:{query}", "dataset_name": f"记录{query}",
                  "url": f"https://zenodo.org/records/{query}"}], [])

    original = dict(cc.SOURCE_ADAPTERS["zenodo"])
    monkeypatch.setitem(cc.SOURCE_ADAPTERS, "zenodo", {**original, "search": fake_search})
    records, warnings, skipped = cc._sync_collect_records(
        "zenodo", [{"accession": "1234", "title": "x"}], {"zenodo:91234"},
        max_import=5, root=tmp_path)
    assert skipped == 0 and len(records) == 1, "后缀撞车不得误判「已在库」（旧后缀匹配口径会被跳过）"
    records2, _, skipped2 = cc._sync_collect_records(
        "zenodo", [{"accession": "91234", "title": "x"}], {"zenodo:91234"},
        max_import=5, root=tmp_path)
    assert skipped2 == 1 and records2 == [], "真实等值（zenodo:91234 ↔ 91234）仍然判重"


def test_sync_identity_index_blindness_is_surfaced(tmp_path, ae_online, ae_search):
    """fix-corpus 遗留：_external_identity_index 对坏文件不再失明——
    坏文件名随去重闸扫描如实带进该源 note（与顶层 skipped_files 并存，双通道可见）。"""
    _write_external(tmp_path, "arrayexpress.json", AE_SNAPSHOT)
    ext = tmp_path / "database" / "external"
    (ext / "upload_broken.json").write_text("{ 坏文件", encoding="utf-8")
    res = cc.sync_updates(["ArrayExpress"], project_root=tmp_path)
    entry = res["sources"][0]
    assert entry["imported_count"] == 2, "坏文件不得连累正常入库"
    assert "upload_broken.json" in entry["note_zh"], "去重闸对坏文件失明必须用户可见"
    assert "upload_broken.json" in res["skipped_files"]
