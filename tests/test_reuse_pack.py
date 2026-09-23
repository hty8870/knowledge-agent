# -*- coding: utf-8 -*-
"""「复用数据出处」清单的回归网。

产物会被粘进论文 → 断言方向与检索器测试相反：宁可少说一句，绝不多说一句没凭据的话。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.corpus import provenance as P  # noqa: E402
from dataset_recommender.content import reuse_pack as RP  # noqa: E402


def _cxg(name: str = "CXG dataset") -> dict:
    return {
        "dataset_name": name,
        "source": "CELLxGENE Discover",
        "dataset_uid": "cxg:24921392-22ed-479a-9144-7d40adf148ae",
        "url": "https://cellxgene.cziscience.com/e/24921392-22ed-479a-9144-7d40adf148ae",
        "collection_doi": "10.1038/s41586-020-2157-4",
        "modality": "single-cell",
        "has_raw_data": False,
        "n_files": 0,
    }


def _ae(name: str = "AE study") -> dict:
    return {
        "dataset_name": name,
        "source": "ArrayExpress",
        "dataset_uid": "ae:E-MTAB-11814",
        "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-11814",
        "modality": "single-cell",
        "has_raw_data": False,   # 抓取脚本的保守猜测，不是来源声明
        "n_files": 0,
    }


def _tenx(name: str = "10x dataset") -> dict:
    return {
        "dataset_name": name,
        "source": "10x Genomics",
        "dataset_uid": "multiome-gemx-10k-human-brain",   # 真台账 uid → LISTED
        "url": "https://www.10xgenomics.com/datasets/multiome-gemx-10k-human-brain",
        "modality": "single-cell",
        "has_raw_data": True,
        "n_files": 12,
    }


# --------------------------------------------------------------- 入参

def test_sanitize_dedupes_and_preserves_order() -> None:
    # 保序：用户勾选的顺序就是他心里的顺序，产物里不该重排。
    assert RP.sanitize_uids([" b ", "a", "b", "", "  ", "a"]) == ["b", "a"]


def test_sanitize_rejects_junk() -> None:
    for bad, code in (
        (None, "bad_param"),
        ("cxg:x", "bad_param"),          # 字符串不是数组（易错：以为传一个就行）
        ({"uids": []}, "bad_param"),
        ([], "empty_input"),
        (["", "   "], "empty_input"),
        ([1, 2], "bad_param"),
        ([None], "bad_param"),
    ):
        with pytest.raises(RP.ReusePackError) as e:
            RP.sanitize_uids(bad)
        assert e.value.code == code, bad


def test_sanitize_caps_volume() -> None:
    with pytest.raises(RP.ReusePackError) as e:
        RP.sanitize_uids([f"uid-{i}" for i in range(201)])
    assert e.value.code == "too_many"


# --------------------------------------------------------------- 反编造

def test_paragraph_never_leaks_internal_prefixes_or_chinese() -> None:
    """段落会被粘进论文 → 不许出现内部主键前缀，也不许混入中文样板。"""
    pack = RP.build_reuse_pack([_cxg(), _ae(), _tenx()])
    para = pack["paragraph"]
    for bad in ("cxg:", "ae:", "hca:", "ebi:"):
        assert bad not in para, bad
    assert not [c for c in para if "一" <= c <= "鿿"], para


def test_identifier_never_calls_a_uuid_an_accession() -> None:
    rows = RP.build_reuse_pack([_cxg(), _ae(), _tenx()])["table"]
    by_source = {r["source"]: r for r in rows}

    cxg = by_source["CELLxGENE Discover"]
    assert cxg["identifier"] == "24921392-22ed-479a-9144-7d40adf148ae"
    assert cxg["identifier_kind"] == "platform_id"
    assert cxg["identifier_label_en"] == "Dataset ID"    # **不是** Accession

    ae = by_source["ArrayExpress"]
    assert ae["identifier"] == "E-MTAB-11814"            # 内部前缀已剥
    assert ae["identifier_kind"] == "accession"
    assert ae["identifier_label_en"] == "Accession"

    tenx = by_source["10x Genomics"]
    assert tenx["identifier"] is None                    # base 只有 URL slug → 不编
    assert tenx["identifier_kind"] is None


def test_paragraph_says_nothing_about_unchecked_raw_data() -> None:
    """没查过的（ArrayExpress）绝不能被段落里的任何一句覆盖。"""
    pack = RP.build_reuse_pack([_ae()])
    assert "FASTQ" not in pack["paragraph"]
    assert pack["raw_state_counts"] == {P.NOT_CHECKED: 1}
    # 但必须在表里显式标出 + 在待办里交代
    assert pack["table"][0]["raw_note_en"] == "Not determined"
    assert any("未核验" in g and "ArrayExpress" in g for g in pack["gaps"])


def test_paragraph_counts_only_verified_subsets() -> None:
    """3 个数据集：1 listed / 1 not_listed / 1 not_checked → 段落只覆盖前两个。"""
    pack = RP.build_reuse_pack([_tenx(), _cxg(), _ae()])
    para = pack["paragraph"]
    assert "Raw sequencing data (FASTQ) are listed in the corresponding source repositories for 1 of these datasets" in para
    assert "No FASTQ files are listed in the corresponding source repositories for 1 of these datasets" in para
    # not_checked 的那条既不算进 listed 也不算进 not_listed
    assert pack["raw_state_counts"] == {P.LISTED: 1, P.NOT_LISTED: 1, P.NOT_CHECKED: 1}


def test_all_n_wording_when_subset_is_everything() -> None:
    pack = RP.build_reuse_pack([_cxg("A"), _cxg("B")])
    assert "all 2 datasets" in pack["paragraph"]
    assert "2 of these datasets" not in pack["paragraph"]


def test_single_dataset_wording() -> None:
    pack = RP.build_reuse_pack([_cxg()])
    assert "This study reuses 1 publicly available dataset " in pack["paragraph"]
    assert "for this dataset" in pack["paragraph"]


# --------------------------------------------------------------- 墓碑

def test_unresolved_uids_are_tombstoned_not_dropped() -> None:
    """静默少一行 → 用户会以为那个数据集已经写进去了。必须显式喊出来。"""
    pack = RP.build_reuse_pack([_cxg()], unresolved=["ghost-1", "ghost-2"])
    assert pack["unresolved"] == ["ghost-1", "ghost-2"]
    assert pack["n_datasets"] == 1
    assert "未纳入本清单" in pack["gaps"][0]      # 且排在待办第一条
    assert "ghost-1" in pack["gaps"][0]


def test_build_pack_for_uids_tombstones_missing(monkeypatch) -> None:
    from types import SimpleNamespace

    record = SimpleNamespace(
        dataset_name="Real one", species="Human", tissue="Lung", disease="",
        platform_family="chromium", assay="scRNA", chemistry="v3", has_raw_data=False,
        count="1", unit="Cells", url="https://cellxgene.cziscience.com/e/abc",
        description="d", family_id="f", modality="single-cell",
        raw={"dataset_uid": "cxg:abc", "source": "CELLxGENE Discover", "n_files": 0},
    )
    pack = RP.build_pack_for_uids(["cxg:abc", "nope"], [record])
    assert pack["n_datasets"] == 1
    assert pack["unresolved"] == ["nope"]


# --------------------------------------------------------------- 体裁边界

def test_the_pack_never_ships_a_boundary_disclaimer_field() -> None:
    """体裁边界**不再**以一段中文置顶陈述（按产品要求删除）。

    这里钉的不是「删干净了」这种一次性清扫，而是**别再加回来**：
    边界靠 keys-only 入参保证（调用方压根没有传数据集内容的口子），不靠一段文案兜着。
    真正需要用户核实的事都在 `gaps` 里，那是逐条、有条数、可执行的；
    而那段话是在用户没问的时候辩解一件不会发生的事。
    """
    pack = RP.build_reuse_pack([_cxg()])
    assert "boundary" not in pack
    assert not hasattr(RP, "BOUNDARY_ZH")


def test_empty_items_degrade_without_crashing() -> None:
    pack = RP.build_reuse_pack([])
    assert pack["n_datasets"] == 0
    assert pack["paragraph"] == ""
    assert pack["table"] == []


# --------------------------------------------------------------- 文件数

def test_n_files_is_none_for_sources_without_a_ledger() -> None:
    """文件级台账只覆盖 10x 的 base 记录；其余写 0 就是把「不知道」说成「没有」。"""
    rows = {r["source"]: r for r in RP.build_reuse_pack([_cxg(), _ae(), _tenx()])["table"]}
    assert rows["CELLxGENE Discover"]["n_files"] is None
    assert rows["ArrayExpress"]["n_files"] is None
    assert rows["10x Genomics"]["n_files"] == 12


# --------------------------------------------------------------- Markdown

def test_markdown_escapes_pipes_so_the_table_survives() -> None:
    """数据集名里真的有 `|`，不转义会把整行表格截断。"""
    import re

    md = RP.to_markdown(RP.build_reuse_pack([_cxg("Weird | name")]))
    assert r"Weird \| name" in md
    # 表头 + 分隔 + 1 行数据 = 3 行以 | 开头
    body = [ln for ln in md.splitlines() if ln.startswith("|")]
    assert len(body) == 3
    # 只数**未转义**的竖线：`\|` 里的那个不是列分隔符（这正是转义的意义）。
    for line in body:
        assert len(re.findall(r"(?<!\\)\|", line)) == 6, line   # 5 列 → 6 个分隔符


def test_markdown_keeps_todos_out_of_the_manuscript() -> None:
    """待办是给用户的，不能随稿件提交 → 只能以 HTML 注释存在。"""
    md = RP.to_markdown(RP.build_reuse_pack([_ae()]))
    for line in md.splitlines():
        if "未核验" in line:
            assert line.strip().startswith("<!--"), line


def test_markdown_has_no_internal_prefixes_in_the_table() -> None:
    md = RP.to_markdown(RP.build_reuse_pack([_cxg(), _ae(), _tenx()]))
    table_part = md.split("<!--")[0]
    for bad in ("cxg:", "ae:", "hca:", "ebi:"):
        assert bad not in table_part, bad


def test_markdown_degrades_on_empty_pack() -> None:
    md = RP.to_markdown(RP.build_reuse_pack([]))
    assert "Reused public datasets" in md


# --------------------------------------------------------------- 面向用户的文案

def test_user_facing_strings_carry_no_markdown_emphasis() -> None:
    """运行期给用户看的字符串**不许**带 `**…**`。

    集成验证：前端一律 `escapeHtml` 后插入 DOM，于是字面星号原样显示成
    「本清单只覆盖你**复用的公开数据**」——面板里数出 8 对。MCP 侧同样按纯文本呈现。
    中文强调用本仓库既有的「」约定。**注意本条只管运行期字符串，不管 docstring/注释**
    （那是给维护者看的，markdown 强调在那里是对的）。
    """
    pack = RP.build_reuse_pack([_cxg(), _ae(), _tenx()], unresolved=["ghost"])
    blobs = [pack["paragraph"], *pack["gaps"]]
    blobs += [r["raw_note_zh"] for r in pack["table"]]
    for s in blobs:
        assert "**" not in s, s


def test_fair_user_facing_strings_carry_no_markdown_emphasis() -> None:
    """同一条约束覆盖 fair 的产物（它与本模块共用前端渲染约定）。"""
    from dataset_recommender.retrieval.fair import assess_fair, build_data_availability_statement

    item = _cxg()
    das = build_data_availability_statement(item)
    report = assess_fair(item)
    blobs = [das["notes"], *das["missing"], report["summary"]["statement"]]
    for c in report["checks"]:
        blobs += [c["evidence"], c["action"]]
    for s in blobs:
        assert "**" not in s, s


# --------------------------------------------------------------- 结构性隔离

def test_reuse_pack_is_outside_the_frozen_closure() -> None:
    """与 fair/provenance 同样的结构性隔离：冻结 784 评测路径永不 import 它。"""
    from test_provenance import _import_closure

    pkg = ROOT / "src" / "dataset_recommender"
    for rel in (
        "src/dataset_recommender/retrieval/retriever.py",
        "src/dataset_recommender/app/workflow.py",
        "scripts/evaluate_recommendation.py",
    ):
        assert "reuse_pack" not in _import_closure(ROOT / rel, pkg), rel
