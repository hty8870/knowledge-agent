# -*- coding: utf-8 -*-
"""对话式数据库管护核心 `corpus_curation.py` 单测（**全 mock 禁网**：`_fetch` 接缝注入假响应）。

钉死对话式数据库管护的核心约定：
- plan 零副作用（preview + confirm_token），apply 重算 token 比对、不一致零写入；
- import 内容 hash 去重（撞重拒绝 / force 通过），入库复用 uploads.ingest_dataset（只进 external）；
- search_online 候选先审后入（不落盘）、来源标签规范化、未注册源 fail-closed、network_error、账本追加；
- remove 回收站式移动 + manifest + 缓存失效，restore 往返；
- 管护对象限 upload_* 命名空间（官方快照 not_curatable），叶子文件名约束使 base 结构性不可达。
"""
import json
import re
from pathlib import Path

import pytest

from dataset_recommender.corpus import corpus, uploads
from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.corpus.corpus_curation import CurateError


# ----------------------------------------------------------------------------------------------
# 夹具与小助手
# ----------------------------------------------------------------------------------------------

def _payload_bytes(records, *, source=None):
    payload = {"records": records}
    if source:
        payload["source"] = source
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _write_external(root: Path, name: str, records, *, source="测试源"):
    ext = root / "database" / "external"
    ext.mkdir(parents=True, exist_ok=True)
    payload = {"source": source, "note": "测试落盘。", "record_count": len(records), "records": records}
    (ext / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _import_once(root: Path, records, *, source="测试源", filename="x.json"):
    """走完整 plan→apply 导入一份，返回 apply 结果。"""
    raw = _payload_bytes(records)
    plan = cc.plan_import(raw, filename, source, project_root=root)
    return cc.apply_import(raw, filename, source, confirm_token=plan["confirm_token"], project_root=root)


# BioStudies 假响应：搜索 2 条命中；第 1 条详情富化成功，第 2 条详情拉取失败（测优雅降级）。
_SEARCH_PAYLOAD = {
    "totalHits": 2,
    "hits": [
        {"accession": "E-MTAB-0001", "title": "Human lung single cell RNA-seq",
         "content": "single cell RNA sequencing of Homo sapiens lung", "release_date": "2023-05-01"},
        {"accession": "E-MTAB-0002", "title": "Mouse cortex single cell",
         "content": "Mus musculus cortex single cell atlas", "release_date": "2022-01-15"},
    ],
}
_DETAIL_1 = {
    "section": {
        "attributes": [
            {"name": "Organism", "value": "Homo sapiens"},
            {"name": "Study type", "value": "RNA-seq of coding RNA from single cells"},
            {"name": "Description", "value": "10x Genomics Chromium study of human lung."},
        ],
        "subsections": [
            {"attributes": [
                {"name": "Organism part", "value": "lung"},
                {"name": "Disease", "value": "normal"},
            ]},
        ],
    }
}


def _fake_fetch(url, **kwargs):
    """假 _fetch：搜索 URL → 命中 payload；E-MTAB-0001 详情 → 富化；E-MTAB-0002 详情 → 网络错误。"""
    if "arrayexpress/search" in url:
        return _SEARCH_PAYLOAD, 200
    if "E-MTAB-0001" in url:
        return _DETAIL_1, 200
    raise CurateError("network_error", "假网络故障：详情拉取失败。")


def _ledger_rows(root: Path):
    path = root / ".userdata" / "curate_net_ledger.jsonl"
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ----------------------------------------------------------------------------------------------
# curate.list
# ----------------------------------------------------------------------------------------------

def test_list_curations_enumerates_external_and_recycle(tmp_path):
    _write_external(tmp_path, "upload_20260101_000000_000001_a.json",
                    [{"dataset_name": "A", "source": "测试源"}, {"dataset_name": "B", "source": "测试源"}])
    _write_external(tmp_path, "arrayexpress.json",
                    [{"dataset_name": "官方", "source": "ArrayExpress"}], source="ArrayExpress")
    (tmp_path / "database" / "external" / "broken.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    # 回收站：一个已移除文件 + manifest 行
    rec_dir = tmp_path / ".userdata" / "recycle"
    rec_dir.mkdir(parents=True)
    (rec_dir / "20260701_010203_000004_upload_old.json").write_text(
        json.dumps({"source": "测试源", "records": [{"dataset_name": "旧", "source": "测试源"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (rec_dir / "manifest.jsonl").write_text(
        json.dumps({"ts": "2026-07-01T01:02:03+08:00", "action": "remove",
                    "original_path": "database/external/upload_old.json",
                    "recycle_name": "20260701_010203_000004_upload_old.json", "record_count": 1},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    out = cc.list_curations(project_root=tmp_path)
    assert out["action"] == "curate.list" and out["file_count"] == 3
    by_name = {f["filename"]: f for f in out["files"]}
    up = by_name["upload_20260101_000000_000001_a.json"]
    assert up["record_count"] == 2 and up["sources"] == {"测试源": 2}
    assert up["is_upload"] and up["curatable"]
    official = by_name["arrayexpress.json"]
    assert official["record_count"] == 1 and not official["is_upload"] and not official["curatable"]
    assert by_name["broken.json"]["record_count"] is None and "parse_error" in by_name["broken.json"]

    assert out["recycle_count"] == 1
    rec = out["recycle"][0]
    assert rec["recycle_name"] == "20260701_010203_000004_upload_old.json"
    assert rec["original_filename"] == "upload_old.json"
    assert rec["moved_at"] == "2026-07-01T01:02:03+08:00"
    assert rec["record_count"] == 1


def test_list_curations_empty_when_nothing(tmp_path):
    out = cc.list_curations(project_root=tmp_path)
    assert out["file_count"] == 0 and out["files"] == []
    assert out["recycle_count"] == 0 and out["recycle"] == []


# ----------------------------------------------------------------------------------------------
# curate.import
# ----------------------------------------------------------------------------------------------

def test_plan_import_preview_and_token_no_disk(tmp_path):
    raw = _payload_bytes([{"dataset_name": "A", "species": "Human"}, {"dataset_name": "B"}])
    plan = cc.plan_import(raw, "x.json", "测试源", project_root=tmp_path)
    assert plan["dry_run"] is True and plan["record_count"] == 2
    assert plan["sources"] == {"测试源": 2}
    assert plan["duplicate"]["is_duplicate"] is False
    assert re.fullmatch(r"[0-9a-f]{16}", plan["confirm_token"])
    assert re.fullmatch(r"[0-9a-f]{16}", plan["records_digest"])
    assert not (tmp_path / "database").exists()  # plan 零副作用


def test_apply_import_token_mismatch_writes_nothing(tmp_path):
    raw = _payload_bytes([{"dataset_name": "A"}])
    plan = cc.plan_import(raw, "x.json", None, project_root=tmp_path)
    # 情形一：回传错 token
    with pytest.raises(CurateError) as ei:
        cc.apply_import(raw, "x.json", None, confirm_token="deadbeefdeadbeef", project_root=tmp_path)
    assert ei.value.code == "token_mismatch"
    # 情形二：plan 后内容变了（重算指纹不一致）
    tampered = _payload_bytes([{"dataset_name": "A"}, {"dataset_name": "B"}])
    with pytest.raises(CurateError) as ei2:
        cc.apply_import(tampered, "x.json", None, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert ei2.value.code == "token_mismatch"
    assert not (tmp_path / "database").exists()  # 零写入


def test_apply_import_success_invalidates_and_retrievable(tmp_path, monkeypatch):
    called = {"n": 0}
    monkeypatch.setattr(uploads, "invalidate_external_cache",
                        lambda: called.__setitem__("n", called["n"] + 1))
    raw = _payload_bytes([{"dataset_name": "人肺单细胞", "species": "Human"}])
    plan = cc.plan_import(raw, "x.json", "测试源", project_root=tmp_path)
    res = cc.apply_import(raw, "x.json", "测试源", confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["dry_run"] is False and res["record_count"] == 1
    assert res["saved_to"].startswith("database/external/") and res["filename"].startswith("upload_")
    assert (tmp_path / res["saved_to"]).is_file()
    assert not (tmp_path / "database" / "base").exists()  # 绝不碰 base
    assert called["n"] == 1  # invalidate_external_cache 被调（即时可见）
    loaded = corpus.load_normalized_corpus(tmp_path / "database" / "base", tmp_path, sources=["测试源"])
    assert len(loaded) == 1  # 入库后可检索


def test_apply_import_duplicate_rejected_then_force_passes(tmp_path):
    records = [{"dataset_name": "A", "species": "Human"}, {"dataset_name": "B", "species": "Mouse"}]
    first = _import_once(tmp_path, records)
    raw = _payload_bytes(records)
    plan = cc.plan_import(raw, "x.json", "测试源", project_root=tmp_path)
    assert plan["duplicate"]["is_duplicate"] is True
    assert plan["duplicate"]["matched_files"] == [first["filename"]]
    with pytest.raises(CurateError) as ei:
        cc.apply_import(raw, "x.json", "测试源", confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert ei.value.code == "duplicate_content"
    files = list((tmp_path / "database" / "external").glob("*.json"))
    assert len(files) == 1  # 拒绝后零写入
    res = cc.apply_import(raw, "x.json", "测试源",
                          confirm_token=plan["confirm_token"], force=True, project_root=tmp_path)
    assert res["forced"] is True
    assert len(list((tmp_path / "database" / "external").glob("*.json"))) == 2


def test_import_bad_input_passthrough_codes(tmp_path):
    with pytest.raises(CurateError) as ei:
        cc.plan_import(b"{ not json", "x.json", None, project_root=tmp_path)
    assert ei.value.code == "invalid_json"
    with pytest.raises(CurateError) as ei2:
        cc.plan_import(b"[]", "x.json", None, project_root=tmp_path)
    assert ei2.value.code == "no_records"
    with pytest.raises(CurateError) as ei3:
        cc.plan_import(b"{}", "x.txt", None, project_root=tmp_path)
    assert ei3.value.code == "bad_file"


# ----------------------------------------------------------------------------------------------
# curate.search_online
# ----------------------------------------------------------------------------------------------

def test_plan_search_online_preview_no_disk_and_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["dry_run"] is True and plan["record_count"] == 2
    assert plan["source"] == "arrayexpress" and plan["source_label"] == "ArrayExpress"
    assert plan["sample_titles"][0] == "Human lung single cell RNA-seq"
    assert re.fullmatch(r"[0-9a-f]{16}", plan["confirm_token"])
    assert len(plan["candidates"]) == 2
    assert any("详情拉取失败" in w for w in plan["warnings"])  # 第 2 条详情失败的诚实降级提示
    assert not (tmp_path / "database").exists()  # preview 不落盘
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 3  # 1 次搜索 + 2 次详情（含失败）
    assert rows[0]["endpoint"] == cc.AE_SEARCH_API and rows[0]["http_status"] == 200 and rows[0]["records"] == 2
    assert rows[0]["query"] == "lung single cell"
    fail = rows[2]
    assert fail["endpoint"] == cc.AE_DETAIL_API and fail["http_status"] is None and fail["error"]


def test_apply_search_online_ingests_with_official_label_and_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["dry_run"] is False and res["record_count"] == 2
    assert res["sources"] == {"ArrayExpress": 2}  # 来源标签规范化为官方源名
    assert res["saved_to"].startswith("database/external/")
    assert res["filename"].startswith("upload_") and "curate_arrayexpress" in res["filename"]

    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert disk["source"] == "ArrayExpress"
    r1, r2 = disk["records"]
    # 字段映射移植正确性（ingest_arrayexpress.to_record 口径）
    assert r1["dataset_uid"] == "ae:E-MTAB-0001"
    assert r1["source"] == "ArrayExpress"
    assert r1["species"] == "Human"                # 学名 → 通用名
    assert r1["tissue"] == "lung"                  # 详情 characteristics 富化
    assert r1["disease"] == "normal"               # 健康态规范值（统一口径）
    assert r1["platform"] == "Chromium"            # 详情自由文本 → 平台家族
    assert r1["url"] == "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-0001"
    assert r1["has_raw_data"] is False and r1["count"] == ""
    # 第 2 条详情失败 → 优雅降级：species 回退 content 正则，tissue/disease 留空
    assert r2["species"] == "Mouse" and r2["tissue"] == "" and r2["disease"] == ""


def test_apply_search_online_token_mismatch_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    plan["candidates"][0]["dataset_name"] = "被调包的标题"
    with pytest.raises(CurateError) as ei:
        cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert ei.value.code == "token_mismatch"
    assert not (tmp_path / "database").exists()  # 零写入


def test_search_online_species_filter(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    plan = cc.plan_search_online("single cell", species="Human", project_root=tmp_path)
    assert plan["record_count"] == 1
    assert plan["candidates"][0]["species"] == "Human"


def test_search_online_unregistered_source_fail_closed(tmp_path, monkeypatch):
    def _boom(url, **kw):  # 不应被调用
        raise AssertionError("未注册源不应发起任何联网")
    monkeypatch.setattr(cc, "_fetch", _boom)
    # encode 在 check_updates 注册表里、但不在 SOURCE_ADAPTERS——「认识但没接搜索」也必须 fail-closed
    # （前本例用 hca；hca/10x 该批已接入，换成仍未接搜索的 encode）。
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("x", source="encode", project_root=tmp_path)
    assert ei.value.code == "source_not_registered"
    assert _ledger_rows(tmp_path) == []  # fail-closed：零联网、零账本


def test_search_online_network_error_and_ledger(tmp_path, monkeypatch):
    def _fail(url, **kw):
        raise CurateError("network_error", "假网络故障：搜索失败。")
    monkeypatch.setattr(cc, "_fetch", _fail)
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("lung", project_root=tmp_path)
    assert ei.value.code == "network_error"
    rows = _ledger_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["http_status"] is None and rows[0]["records"] == 0
    assert not (tmp_path / "database").exists()


def test_search_online_no_candidates(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", lambda url, **kw: ({"totalHits": 0, "hits": []}, 200))
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("不存在的稀奇关键词", project_root=tmp_path)
    assert ei.value.code == "no_candidates"


def test_search_online_bad_param(tmp_path):
    with pytest.raises(CurateError) as ei:
        cc.plan_search_online("  ", project_root=tmp_path)
    assert ei.value.code == "bad_param"
    with pytest.raises(CurateError) as ei2:
        cc.plan_search_online("x", limit=0, project_root=tmp_path)
    assert ei2.value.code == "bad_param"
    with pytest.raises(CurateError) as ei3:
        cc.apply_search_online({"candidates": []}, confirm_token="t", project_root=tmp_path)
    assert ei3.value.code == "bad_param"


# ----------------------------------------------------------------------------------------------
# 实体级去重（验证）：search_online 候选 vs 库中既有记录
# ----------------------------------------------------------------------------------------------

def test_plan_search_online_skips_entities_already_in_library(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    _write_external(tmp_path, "upload_seed.json",
                    [{"dataset_uid": "ae:E-MTAB-0001", "dataset_name": "旧入库", "source": "ArrayExpress"}],
                    source="ArrayExpress")
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["record_count"] == 1, "已在库中的实体不进候选"
    assert [r["dataset_uid"] for r in plan["candidates"]] == ["ae:E-MTAB-0002"]
    assert plan["skipped_existing_count"] == 1
    assert plan["skipped_existing"][0]["dataset_uid"] == "ae:E-MTAB-0001"
    assert any("已在库中" in w for w in plan["warnings"])


def test_plan_all_existing_then_apply_writes_nothing(tmp_path, monkeypatch):
    """零新候选是合法产出（非 no_candidates 错误）；apply 零写入并如实回报。"""
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    _write_external(tmp_path, "upload_seed.json",
                    [{"dataset_uid": "ae:E-MTAB-0001", "dataset_name": "旧1", "source": "ArrayExpress"},
                     {"dataset_uid": "ae:E-MTAB-0002", "dataset_name": "旧2", "source": "ArrayExpress"}],
                    source="ArrayExpress")
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["record_count"] == 0 and plan["candidates"] == []
    assert plan["skipped_existing_count"] == 2
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 0 and res["filename"] is None and res["saved_to"] is None
    assert res["skipped_existing_count"] == 2 and any("全部已在库中" in w for w in res["warnings"])
    assert {p.name for p in (tmp_path / "database" / "external").glob("*.json")} == {"upload_seed.json"}


def test_apply_search_online_rechecks_duplicates_at_write_time(tmp_path, monkeypatch):
    """TOCTOU：plan→apply 之间同一实体经其它通路入库 → 落盘前重检，只写真正的新候选。"""
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["record_count"] == 2
    _write_external(tmp_path, "upload_race.json",
                    [{"dataset_uid": "ae:E-MTAB-0001", "dataset_name": "竞态入库", "source": "ArrayExpress"}],
                    source="ArrayExpress")
    res = cc.apply_search_online(plan, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["record_count"] == 1 and res["skipped_existing_count"] == 1
    assert any("未重复写入" in w for w in res["warnings"])
    disk = json.loads((tmp_path / res["saved_to"]).read_text(encoding="utf-8"))
    assert [r["dataset_uid"] for r in disk["records"]] == ["ae:E-MTAB-0002"]


def test_identity_url_key_matches_across_source_labels(tmp_path, monkeypatch):
    """url 键不带来源：手动导入（用户上传、无编号）与官方源候选同页面链接 = 同一实体。
    尾斜杠差异也被归一化吸收。"""
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    _write_external(tmp_path, "upload_manual.json",
                    [{"dataset_name": "手动导入的同一份", "source": "用户上传",
                      "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-0001/"}],
                    source="用户上传")
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["record_count"] == 1
    # skipped 投影呈现的是**候选**侧信息（「这条候选已在库中」）；该候选在库里只有链接没有编号
    assert plan["skipped_existing"][0]["dataset_uid"] == "ae:E-MTAB-0001"


def test_identity_uid_normalization_casefold(tmp_path, monkeypatch):
    """编号归一化（NFC + 去零宽 + casefold，与 locate_record 同一真源）：大小写变体同指一条。"""
    monkeypatch.setattr(cc, "_fetch", _fake_fetch)
    _write_external(tmp_path, "upload_seed.json",
                    [{"dataset_uid": "AE:E-MTAB-0001", "dataset_name": "旧", "source": "ArrayExpress"}],
                    source="ArrayExpress")
    plan = cc.plan_search_online("lung single cell", project_root=tmp_path)
    assert plan["record_count"] == 1 and plan["skipped_existing_count"] == 1


def test_record_without_identity_keys_is_never_deduped(tmp_path):
    """无 uid 且无 url 的记录无法核验身份 → 不参与去重（宁可保留不错杀）。"""
    assert cc._record_identity_keys({"dataset_name": "只有名字"}) == set()
    assert cc._record_identity_keys({"dataset_uid": "x"}) == {"u||x"}   # 无来源时来源段为空


# ----------------------------------------------------------------------------------------------
# curate.remove / curate.restore
# ----------------------------------------------------------------------------------------------

def test_remove_moves_to_recycle_with_manifest_and_cache_invalidation(tmp_path, monkeypatch):
    _import_once(tmp_path, [{"dataset_name": "A"}, {"dataset_name": "B"}])
    ext = tmp_path / "database" / "external"
    name = next(p.name for p in ext.glob("*.json"))
    called = {"n": 0}
    monkeypatch.setattr(cc, "invalidate_external_cache",
                        lambda: called.__setitem__("n", called["n"] + 1))

    plan = cc.plan_remove(name, project_root=tmp_path)
    assert plan["record_count"] == 2 and plan["sources"] == {"测试源": 2}
    res = cc.apply_remove(name, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert res["dry_run"] is False and res["restorable"] is True
    assert not (ext / name).exists()  # 原位置已移走
    dest = tmp_path / res["moved_to"]
    assert dest.is_file() and dest.parent == tmp_path / ".userdata" / "recycle"
    assert re.match(r"^[0-9]{8}_[0-9]{6}_[0-9]{6}_" + re.escape(name) + r"$", dest.name)
    manifest = [json.loads(x) for x in (tmp_path / ".userdata" / "recycle" / "manifest.jsonl")
                .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert manifest[-1]["action"] == "remove"
    assert manifest[-1]["original_path"] == f"database/external/{name}"
    assert manifest[-1]["recycle_name"] == dest.name and manifest[-1]["record_count"] == 2
    assert called["n"] == 1  # 缓存失效 → 即时不可见


def test_apply_remove_token_mismatch_and_toctou(tmp_path):
    _import_once(tmp_path, [{"dataset_name": "A"}])
    ext = tmp_path / "database" / "external"
    name = next(p.name for p in ext.glob("*.json"))
    with pytest.raises(CurateError) as ei:
        cc.apply_remove(name, confirm_token="deadbeefdeadbeef", project_root=tmp_path)
    assert ei.value.code == "token_mismatch"
    assert (ext / name).is_file()  # 零写入

    # TOCTOU：plan 后文件被改 → 重算指纹不一致 → 拒绝
    plan = cc.plan_remove(name, project_root=tmp_path)
    (ext / name).write_text(json.dumps(
        {"source": "测试源", "records": [{"dataset_name": "被改过"}]}, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(CurateError) as ei2:
        cc.apply_remove(name, confirm_token=plan["confirm_token"], project_root=tmp_path)
    assert ei2.value.code == "token_mismatch"
    assert not (tmp_path / ".userdata" / "recycle").exists()


def test_restore_roundtrip(tmp_path, monkeypatch):
    _import_once(tmp_path, [{"dataset_name": "A"}])
    ext = tmp_path / "database" / "external"
    name = next(p.name for p in ext.glob("*.json"))
    called = {"n": 0}
    monkeypatch.setattr(cc, "invalidate_external_cache",
                        lambda: called.__setitem__("n", called["n"] + 1))
    plan_rm = cc.plan_remove(name, project_root=tmp_path)
    res_rm = cc.apply_remove(name, confirm_token=plan_rm["confirm_token"], project_root=tmp_path)
    recycle_name = Path(res_rm["moved_to"]).name

    plan_rs = cc.plan_restore(recycle_name, project_root=tmp_path)
    assert plan_rs["target_filename"] == name and plan_rs["will_conflict"] is False
    res_rs = cc.apply_restore(recycle_name, confirm_token=plan_rs["confirm_token"], project_root=tmp_path)
    assert res_rs["restored_to"] == f"database/external/{name}"
    assert (ext / name).is_file()  # 移回原文件名
    assert not (tmp_path / ".userdata" / "recycle" / recycle_name).exists()
    manifest = [json.loads(x) for x in (tmp_path / ".userdata" / "recycle" / "manifest.jsonl")
                .read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [m["action"] for m in manifest] == ["remove", "restore"]
    assert called["n"] == 2  # remove + restore 各失效一次


def test_restore_conflict_refused(tmp_path):
    _import_once(tmp_path, [{"dataset_name": "A"}])
    ext = tmp_path / "database" / "external"
    name = next(p.name for p in ext.glob("*.json"))
    plan_rm = cc.plan_remove(name, project_root=tmp_path)
    res_rm = cc.apply_remove(name, confirm_token=plan_rm["confirm_token"], project_root=tmp_path)
    recycle_name = Path(res_rm["moved_to"]).name
    # 人为在 external 放一个同名文件 → 恢复会覆盖，fail-closed 拒绝
    (ext / name).write_text(json.dumps({"records": [{"dataset_name": "占位"}]}), encoding="utf-8")
    plan_rs = cc.plan_restore(recycle_name, project_root=tmp_path)
    assert plan_rs["will_conflict"] is True
    with pytest.raises(CurateError) as ei:
        cc.apply_restore(recycle_name, confirm_token=plan_rs["confirm_token"], project_root=tmp_path)
    assert ei.value.code == "bad_param"
    assert (tmp_path / ".userdata" / "recycle" / recycle_name).is_file()  # 回收站文件未动


def test_remove_non_upload_file_not_curatable(tmp_path):
    _write_external(tmp_path, "arrayexpress.json", [{"dataset_name": "官方", "source": "ArrayExpress"}],
                    source="ArrayExpress")
    with pytest.raises(CurateError) as ei:
        cc.plan_remove("arrayexpress.json", project_root=tmp_path)
    assert ei.value.code == "not_curatable"


def test_remove_unknown_file(tmp_path):
    with pytest.raises(CurateError) as ei:
        cc.plan_remove("upload_missing.json", project_root=tmp_path)
    assert ei.value.code == "unknown_file"
    with pytest.raises(CurateError) as ei2:
        cc.plan_restore("upload_missing.json", project_root=tmp_path)
    assert ei2.value.code == "unknown_file"


def test_remove_rejects_path_escape_base_structurally_unreachable(tmp_path):
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True)
    sentinel = base / "sentinel.json"
    sentinel.write_text('{"records": [{"dataset_name": "冻结基准"}]}', encoding="utf-8")
    for bad in ("../base/sentinel.json", "database/base/sentinel.json", "..", ""):
        with pytest.raises(CurateError) as ei:
            cc.plan_remove(bad, project_root=tmp_path)
        assert ei.value.code == "bad_param"
    # 同名的 base 文件在 external 不存在 → unknown_file，够不到 base
    with pytest.raises(CurateError) as ei2:
        cc.plan_remove("sentinel.json", project_root=tmp_path)
    assert ei2.value.code == "unknown_file"
    assert sentinel.read_text(encoding="utf-8") == '{"records": [{"dataset_name": "冻结基准"}]}'  # base 未动


# ----------------------------------------------------------------------------------------------
# 动作校验 / 错误契约
# ----------------------------------------------------------------------------------------------

def test_require_action_unknown_bad_action():
    assert cc.require_action("list") == "list"
    with pytest.raises(CurateError) as ei:
        cc.require_action("nuke")
    assert ei.value.code == "bad_action"


def test_curate_error_is_value_error_with_code_and_hint():
    e = CurateError("some_code", "人读提示")
    assert isinstance(e, ValueError)
    assert e.code == "some_code" and e.hint == "人读提示"
    assert str(e) == "some_code: 人读提示"
