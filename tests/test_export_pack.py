# -*- coding: utf-8 -*-
"""课题导出中心（export_pack.py）：研究材料包的真行为测试。

盯的性质（每条坏了就给出「没法直接拿去用/含编造内容」的材料）：

1. **ZIP 内容完整性**：`render_files` 按 kind 只含该含的文件；`full` 全量；
   服务端零写盘（files_to_zip_bytes 内存拼包，可解包逐文件断言）。
2. **GB/T 7714-2015 格式**：`[DS/OL]` 类型标志、出版者=来源、出版年、[引用日期]=导出日、
   获取和访问路径；字段缺失**如实留位不编造**（缺出版年/URL 的条目里没有这两个字段，
   缺什么在「未填字段说明」里逐条列出）。
3. **方法草稿不编造**：只含 provenance/search_trace 里**在场**的内容；溯源里没有的
   样本数/步骤/日期，草稿里一个字都不出现，并如实声明「未记录」。
4. **溯源一致**：provenance.md 与 recipe.json 只含快照里的真实字段；目录版本来自语料快照。
5. **下载任务包复用 task_pack**：download/ 子目录含 00-START-HERE/file-list/manifest/脚本/todo。
6. **查不到的 uid 出显式墓碑**：不在本地语料的候选照列 manifest（标注「当前库中找不到」），
   不进下载/引文。
"""
import io
import json
import zipfile
from pathlib import Path

import pytest

from dataset_recommender.corpus import corpus
from dataset_recommender.content import export_pack, item_view

ROOT = Path(__file__).resolve().parents[1]

KIND_SUBSETS = {
    "download_list": ["README.md", "manifest.md", "manifest.tsv",
                      "download/00-START-HERE.txt", "download/README.md", "download/file-list.md",
                      "download/manifest.tsv", "download/manifest.json",
                      "download/download.sh", "download/download.ps1", "download/todo.md"],
    "citations": ["README.md", "manifest.md", "manifest.tsv",
                  "citations.ris", "citations.bib", "citations-gb7714.txt"],
    "screening_record": ["README.md", "manifest.md", "manifest.tsv", "candidates.md", "candidates.tsv",
                         "provenance.md", "method-draft.md", "recipe.json"],
}
FULL_FILES = list(dict.fromkeys(
    ["README.md", "manifest.md", "manifest.tsv", "candidates.md", "candidates.tsv",
     "citations.ris", "citations.bib", "citations-gb7714.txt"]
    + [f"download/{n}" for n in (
        "00-START-HERE.txt", "README.md", "file-list.md", "manifest.tsv", "manifest.json",
        "download.sh", "download.ps1", "todo.md")]
    + ["provenance.md", "method-draft.md", "recipe.json"]))


@pytest.fixture(scope="module")
def records():
    return corpus.load_full_corpus(ROOT / "database" / "base", ROOT)


@pytest.fixture(scope="module")
def sample_uids(records):
    """取策展产物 10x-Visium.json 前 3 条（有真实文件清单的输入语义），外加一个查不到的墓碑。"""
    visium = [r for r in records if r.source_file == "10x-Visium.json"]
    assert len(visium) >= 3, "测试前提：10x-Visium.json 至少 3 条"
    return [str(r.raw.get("dataset_uid")) for r in visium[:3]] + ["cxg:not-in-corpus"]


@pytest.fixture(scope="module")
def snapshot(records, sample_uids):
    """课题当前状态快照：3 条真实候选（已核验/已排除/待核验）+ 1 条墓碑 + 完整溯源。"""
    uids = sample_uids[:3]
    items = {str(it.get("dataset_uid")): it
             for it in [item_view.build_item(r, include_introduction=True) for r in records]}
    year = items[uids[0]].get("published_year") or 2020
    return {
        "project_id": "prj-test-1",
        "name": "人类肺组织单细胞课题",
        "goal": "比较健康与病变组织的细胞组成",
        "include_conditions": ["物种=人类", "组织=肺"],
        "exclude_conditions": ["含肿瘤细胞"],
        "candidates": [
            {"uid": uids[0], "status": "已核验", "reason": "样本量与目标一致", "verified_at": "2026-08-20T10:00:00Z", "added_at": "2026-08-19T09:00:00Z"},
            {"uid": uids[1], "status": "已排除", "reason": "技术平台不符", "verified_at": "2026-08-21T11:00:00Z", "added_at": "2026-08-19T09:00:00Z"},
            {"uid": uids[2], "status": "待核验", "reason": "", "verified_at": "", "added_at": "2026-08-22T08:00:00Z"},
            {"uid": "cxg:not-in-corpus", "status": "候选", "reason": "", "verified_at": "", "added_at": "2026-08-22T08:00:00Z"},
        ],
        "check_condition": {
            "display_query": "人类肺组织单细胞数据",
            "spec": {
                "spec_version": "v1", "query": "人类肺组织单细胞数据",
                "sources": ["10x Genomics"], "facet_filters": [],
                "suppressed_constraints": [], "lenient_dims": [], "date_from": "", "date_to": "",
            },
            "last_checked_at": "2026-08-22T09:00:00Z",
        },
        "provenance": {
            "query": "人类肺组织单细胞数据",
            "retrieval_params": {"query": "人类肺组织单细胞数据", "strategy": "fixed",
                                 "recall": "off", "rerank": "off", "sources": ["10x Genomics"]},
            "search_trace": {"version": 1, "steps": [
                {"id": "source_parse", "label": "数据来源", "status": "used",
                 "detail": "使用手动选择的数据来源。"},
                {"id": "hard_filter", "label": "必选条件筛选", "status": "used",
                 "detail": f"筛选后命中 3 条；后续排序不会加入不满足必选条件的数据。"},
            ]},
            "filters": {"active": [], "suppressed": [], "lenient": []},
            "corpus_digest": "abc123", "retrieved_at": "2026-08-22T08:30:00Z",
            "policy_id": "pol-test", "result": {"uids": uids, "truncated": False},
        },
    }


@pytest.fixture(scope="module")
def pack(snapshot, records):
    return export_pack.build_export_pack(snapshot, records, today="2026-08-22")


# ----------------------------------------------------------------- ZIP 内容完整性

@pytest.mark.parametrize("kind", ["download_list", "citations", "screening_record", "full"])
def test_zip_contains_exactly_the_kind_files(snapshot, records, kind):
    """每种 kind 的 ZIP 内容与约定一致（不多不少）；服务端零写盘（内存解包断言）。"""
    pack = export_pack.build_export_pack(snapshot, records, today="2026-08-22")
    files = export_pack.render_files(pack, kind)
    names = [f["path"] for f in files]
    expected = KIND_SUBSETS.get(kind, FULL_FILES)
    assert names == expected, f"kind={kind} 文件集不符：{names} != {expected}"
    blob = export_pack.files_to_zip_bytes(files)
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        assert sorted(zf.namelist()) == sorted(expected)


def test_full_kind_includes_everything(snapshot, records):
    pack = export_pack.build_export_pack(snapshot, records, today="2026-08-22")
    files = export_pack.render_files(pack, "full")
    names = set(f["path"] for f in files)
    assert names == set(FULL_FILES)
    # 下载脚本要可执行（macOS/Linux 用户不用先 chmod，task_pack 契约）
    sh = next(f for f in files if f["path"] == "download/download.sh")
    assert sh.get("executable") is True


def test_manifest_lists_stable_identifier_url_and_verified_time(pack):
    md = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "manifest.md")
    rows = pack["manifest_rows"]
    resolved = [r for r in rows if r["status"]]
    assert len(resolved) == 3 and len(pack["unresolved"]) == 1
    # 已核验/已排除带核验时间；待核验如实留空（"—"）
    verified = [r for r in resolved if r["status"] in ("已核验", "已排除")]
    assert all(r["verified_at"] for r in verified)
    assert any(r["status"] == "待核验" and not r["verified_at"] for r in resolved)
    # 墓碑照列、如实标注
    tomb = next(r for r in rows if r["uid"] == "cxg:not-in-corpus")
    assert tomb["name"] == "（当前库中找不到）"
    assert "当前库中找不到" in md


def test_download_task_pack_reuses_task_pack(snapshot, records):
    pack = export_pack.build_export_pack(snapshot, records, today="2026-08-22")
    files = {f["path"]: f for f in export_pack.render_files(pack, "full")}
    # 下载任务包：task_pack 的下载相关文件全部在场（目录隔离，不与顶层 manifest 撞名）
    for name in ("download/00-START-HERE.txt", "download/README.md", "download/file-list.md",
                 "download/manifest.tsv", "download/manifest.json",
                 "download/download.sh", "download/download.ps1", "download/todo.md"):
        assert name in files, f"缺 {name}"
    manifest = json.loads(files["download/manifest.json"]["text"])
    assert manifest.get("plan_token"), "下载任务包的 manifest 应含 plan_token（task_pack 契约）"
    # 墓碑 uid 不进下载任务包
    assert "cxg:not-in-corpus" not in files["download/manifest.tsv"]["text"]


# ----------------------------------------------------------------- GB/T 7714-2015

def test_gb7714_entry_format(snapshot, records):
    items, _, _ = export_pack._resolve_candidates(snapshot, records)
    it = items[0]
    entry = export_pack.gb7714_entry(it, "2026-08-22")
    assert "[DS/OL]" in entry, "文献类型标志 [DS/OL] 缺失"
    assert entry.startswith(str(it["dataset_name"]) + "[DS/OL]."), "题名应作首项（责任者缺省时国标允许）"
    assert str(it["source"]) in entry, "出版者=来源库缺失"
    year = it.get("published_year")
    if year:
        assert str(year) in entry, "出版年缺失"
    assert "[2026-08-22]" in entry, "[引用日期]=导出日缺失"
    assert "获取和访问路径" in entry and str(it["url"]) in entry, "访问路径缺失"
    # 字段缺失如实留位：无 URL/无年份的条目里不得出现这两个字段
    bare = export_pack.gb7714_entry({"dataset_name": "无字段数据集", "source": "", "published_year": None, "url": ""}, "2026-08-22")
    assert "获取和访问路径" not in bare and "2026" not in bare.replace("2026-08-22", ""), "缺失字段被编造"


def test_gb7714_gaps_list_what_is_missing():
    """缺字段必须在「未填字段说明」里逐条点名（留位不编造的可追溯性）。"""
    gaps = export_pack.gb7714_gaps([{"dataset_name": "缺字段记录", "published_year": None, "url": ""}])
    assert gaps, "应列出未填字段说明"
    assert "主要责任者" in gaps[0], "责任者缺省必须如实声明"
    assert any("缺字段记录" in g for g in gaps), "缺出版年/访问路径的记录未在 gaps 点名"


def test_gb7714_gaps_skip_complete_records(snapshot, records):
    """有 URL 和年份的记录不进 gaps 的缺字段清单（不虚报缺失）。"""
    items, _, _ = export_pack._resolve_candidates(snapshot, records)
    gaps = export_pack.gb7714_gaps(items)
    for it in items:
        if it.get("url") and it.get("published_year") is not None:
            name = str(it["dataset_name"])
            assert not any(name in g and ("未填" in g) for g in gaps), f"{name} 字段齐全却被 gaps 点名"


def test_gb7714_file_structure(pack):
    text = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "citations-gb7714.txt")
    assert "GB/T 7714-2015" in text and "[DS/OL]" in text
    assert "未填字段说明" in text, "缺字段说明缺失（留位不编造的配套清单）"
    assert "主要责任者" in text, "责任者缺省声明缺失"


def test_citations_ris_and_bibtex_reuse_reuse_pack(pack):
    files = {f["path"]: f["text"] for f in export_pack.render_files(pack, "full")}
    assert files["citations.ris"].count("TY  - DATA") == 3, "RIS 应为 TY-DATA（数据集，不是论文）"
    assert files["citations.bib"].count("@misc{") == 3, "BibTeX 应为 @misc（数据集，不是 @article）"
    assert "cxg:not-in-corpus" not in files["citations.ris"], "墓碑 uid 不得进引文"


# ----------------------------------------------------------------- 方法草稿不编造

def test_method_draft_only_uses_present_provenance_fields(snapshot, records):
    """草稿只含 provenance/search_trace 在场字段；溯源里没有的数字/步骤/日期一个字不出现。"""
    prov = snapshot["provenance"]
    pack = export_pack.build_export_pack(snapshot, records, today="2026-08-22")
    draft = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "method-draft.md")
    assert "草稿——请核对后使用" in draft, "草稿标注缺失"
    assert prov["query"] in draft, "检索语句应来自真实溯源"
    assert "筛选后命中 3 条" in draft, "search_trace 的真实步骤 detail 应在草稿里"
    assert "使用手动选择的数据来源。" in draft, "search_trace 步骤 detail 应在草稿里"
    # 不编造：provenance 里没有的样本数/日期/步骤不得出现
    for token in ("100000", "10,000 cells", "GEO 数据库", "访问于 2026-08-01"):
        assert token not in draft, f"草稿编造了溯源里没有的内容：{token}"
    # 未记录事项如实声明（样本数/访问日期等）
    assert "样本数" in draft and "未记录" in draft


def test_method_draft_without_trace_says_unrecorded(snapshot, records):
    no_trace = dict(snapshot)
    no_trace["provenance"] = dict(snapshot["provenance"], search_trace={}, query="")
    pack = export_pack.build_export_pack(no_trace, records, today="2026-08-22")
    draft = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "method-draft.md")
    assert "检索执行轨迹" in draft and "未记录。" in draft, "无 trace 时草稿应如实写「未记录」"
    assert "筛选后命中 3 条" not in draft, "空 trace 时不得出现任何步骤内容"


# ----------------------------------------------------------------- 溯源一致

def test_provenance_md_matches_snapshot(pack, snapshot):
    text = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "provenance.md")
    prov = snapshot["provenance"]
    assert prov["query"] in text
    assert prov["retrieved_at"] in text
    assert prov["corpus_digest"] in text
    assert prov["policy_id"] in text
    assert "3 个数据集" in text, "结果 uid 计数应来自溯源 result"
    assert "pol-not-exist" not in text and "2026-08-01" not in text, "溯源文件不得出现快照外的值"


def test_recipe_json_is_replayable(pack, snapshot):
    text = next(f["text"] for f in export_pack.render_files(pack, "full") if f["path"] == "recipe.json")
    recipe = json.loads(text)
    assert recipe["schema"] == export_pack.SCHEMA
    assert recipe["check_spec"]["query"] == "人类肺组织单细胞数据"
    assert recipe["provenance"]["retrieval_params"]["strategy"] == "fixed"
    assert recipe["provenance"]["search_trace"]["steps"][0]["label"] == "数据来源"
    assert recipe["corpus"]["snapshot_id"] == pack["corpus"]["snapshot_id"], "目录版本来自语料快照单一真源"
    assert recipe["candidates"][0]["status"] == "已核验"
    # 可重跑：check_spec 直接可喂 /api/watch/check（字段同构）
    assert {"spec_version", "query", "sources", "facet_filters"} <= set(recipe["check_spec"])


# ----------------------------------------------------------------- 入参归一

def test_sanitize_rejects_bad_inputs(snapshot):
    with pytest.raises(export_pack.ExportPackError):
        export_pack.sanitize_kind("nonsense")
    with pytest.raises(export_pack.ExportPackError):
        export_pack.sanitize_kind("")
    bad = dict(snapshot)
    bad["candidates"] = [{"uid": "x", "status": "随便"}]
    with pytest.raises(export_pack.ExportPackError):
        export_pack.sanitize_snapshot(bad)
    too_many = {"candidates": [{"uid": f"u{i}", "status": "待核验"} for i in range(201)]}
    with pytest.raises(export_pack.ExportPackError):
        export_pack.sanitize_snapshot(too_many)


def test_empty_candidates_still_exportable(snapshot, records):
    """课题可以没有候选（导出按钮在前端禁用）；后端如实产出不含数据集的包，不报错。"""
    empty = dict(snapshot, candidates=[])
    pack = export_pack.build_export_pack(empty, records, today="2026-08-22")
    files = export_pack.render_files(pack, "full")
    assert pack["manifest_rows"] == []
    md = next(f["text"] for f in files if f["path"] == "manifest.md")
    assert "数据集清单" in md
