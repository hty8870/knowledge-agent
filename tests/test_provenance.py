# -*- coding: utf-8 -*-
"""`provenance.py` 的三条不变量 + 五源判定的回归网。

这些测试守的是**产物会被粘进论文**的那条路径，所以断言方向和检索器测试相反：
检索器宁可漏召回，这里宁可少说一句，也绝不多说一句没凭据的话。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.corpus import provenance as P  # noqa: E402


# --------------------------------------------------------------- 不变量 1

def test_finalize_rejects_scopeless_negation() -> None:
    """否定断言没有作用域 → 直接炸，不给静默降级的口子。

    这是**结构性**闸门而非叮嘱：以后谁加新来源、复制粘贴一个 return 忘了写 scope，
    第一次调用就炸，而不是悄悄印一句「它没有原始数据」进别人的稿件。
    """
    for bad_scope in ("", "   ", None):
        with pytest.raises(ValueError, match="作用域"):
            P._finalize({"state": P.NOT_LISTED, "scope": bad_scope, "basis": P.BASIS_UNVERIFIED})
        with pytest.raises(ValueError, match="作用域"):
            P._finalize({"state": P.NOT_CHECKED, "scope": bad_scope, "basis": P.BASIS_UNVERIFIED})


def test_finalize_requires_scope_even_for_positive() -> None:
    """肯定断言也要作用域：LISTED 的措辞是 "listed in {scope}"，没作用域这句话不成立。

    统一要求比「只有否定才需要」少一个口子 —— 少一条规则，少一个漏网分支。
    """
    with pytest.raises(ValueError, match="作用域"):
        P._finalize({"state": P.LISTED, "scope": "", "scope_en": "somewhere"})
    ok = P._finalize({"state": P.LISTED, "scope": "某清单", "scope_en": "some listing"})
    assert ok["state"] == P.LISTED


def test_finalize_requires_english_scope() -> None:
    """`scope_en` 缺失 → 炸。它进的是**英文稿件**，中文漏进去用户可能到审稿才发现。"""
    with pytest.raises(ValueError, match="scope_en"):
        P._finalize({"state": P.LISTED, "scope": "中文清单", "scope_en": ""})


def test_finalize_rejects_non_ascii_english_scope() -> None:
    """图省事把中文塞进 scope_en 也要炸 —— 非空不等于是英文。"""
    with pytest.raises(ValueError, match="非 ASCII"):
        P._finalize({"state": P.LISTED, "scope": "中文清单", "scope_en": "10x 官方下载页"})


def test_english_source_name_degrades_non_ascii() -> None:
    """来源名进英文语境前必须过这道闸：用户上传的来源名可能是中文。"""
    assert P.english_source_name("10x Genomics") == "10x Genomics"
    assert P.english_source_name("CELLxGENE Discover") == "CELLxGENE Discover"
    assert P.english_source_name("用户上传") == "the source repository"
    assert P.english_source_name("") == "the source repository"
    assert P.english_source_name(None) == "the source repository"


def test_chinese_source_still_yields_a_usable_verdict() -> None:
    """中文来源名不能让出处判定崩掉（_finalize 的 ASCII 闸门就在这条路径上）。"""
    v = P.raw_data_provenance({"source": "用户上传", "dataset_uid": "x:y"})
    assert v["state"] == P.NOT_CHECKED
    assert "用户上传" in v["scope"]                    # 中文界面照常显示原名
    assert v["scope_en"].isascii()                     # 英文侧降级


def test_every_verdict_carries_both_languages() -> None:
    for source in (P.SOURCE_10X, P.SOURCE_CELLXGENE, P.SOURCE_ARRAYEXPRESS,
                   P.SOURCE_HCA, P.SOURCE_SCEA, "用户上传", ""):
        for has_raw in (True, False, None):
            v = P.raw_data_provenance({"source": source, "dataset_uid": "x:y", "has_raw_data": has_raw})
            assert v["scope"].strip() and v["scope_en"].strip(), (source, has_raw)
            # scope_en 必须真是英文 —— 不能图省事把中文塞进去
            assert not [c for c in v["scope_en"] if "一" <= c <= "鿿"], (source, v["scope_en"])


@pytest.mark.parametrize(
    "source",
    [P.SOURCE_10X, P.SOURCE_CELLXGENE, P.SOURCE_ARRAYEXPRESS, P.SOURCE_HCA, P.SOURCE_SCEA, "用户上传", ""],
)
def test_every_source_produces_scoped_verdict(source: str) -> None:
    for has_raw in (True, False, None):
        v = P.raw_data_provenance({"source": source, "dataset_uid": "x:y", "has_raw_data": has_raw})
        assert v["state"] in {P.LISTED, P.NOT_LISTED, P.NOT_CHECKED}
        if v["state"] != P.LISTED:
            assert v["scope"].strip(), (source, has_raw)
        assert v["basis"] in {
            P.BASIS_LEDGER_PROBE, P.BASIS_SOURCE_MANIFEST, P.BASIS_REPOSITORY_POLICY, P.BASIS_UNVERIFIED,
        }


# --------------------------------------------------------------- 不变量 2

def test_public_accession_strips_internal_prefix() -> None:
    assert P.public_accession("ae:E-MTAB-11814") == "E-MTAB-11814"
    assert P.public_accession("ebi:E-ANND-1") == "E-ANND-1"


def test_public_accession_refuses_to_invent() -> None:
    """没有公开编号就返回 None —— **不拿内部主键冒充**。

    修复前 `fair.py` 无条件写 `under accession "{uid}"`，全库 5667/5667 都被印上了
    一个编造的 accession。下面每一条都是真实出现过的编造。
    """
    assert P.public_accession("cxg:24921392-22ed-479a-9144-7d40adf148ae") is None  # 平台 UUID 不是 accession
    assert P.public_accession("hca:74b6d569-3b11-42ef-b6b1-a0454522b4a0") is None
    assert P.public_accession("multiome-gemx-5k-mouse-kidney") is None             # base 是 URL slug
    assert P.public_accession("") is None
    assert P.public_accession(None) is None
    assert P.public_accession("ae:") is None                                       # 空 bare 值不算


def test_platform_id_and_accession_are_mutually_exclusive() -> None:
    """一条记录不会既有 accession 又有 platform_id —— 否则 DAS 会写出两个标识符子句。"""
    for uid in ("ae:E-MTAB-11814", "ebi:E-ANND-1", "cxg:abc-123", "hca:def-456",
                "multiome-gemx-5k-mouse-kidney", "", "weird:", "no-colon"):
        assert not (P.public_accession(uid) and P.platform_id(uid)), uid


def test_platform_id_returns_uuid_for_uuid_sources() -> None:
    assert P.platform_id("cxg:24921392-22ed-479a-9144-7d40adf148ae") == "24921392-22ed-479a-9144-7d40adf148ae"
    assert P.platform_id("hca:74b6d569-3b11-42ef-b6b1-a0454522b4a0") == "74b6d569-3b11-42ef-b6b1-a0454522b4a0"
    assert P.platform_id("ae:E-MTAB-11814") is None
    assert P.platform_id("multiome-gemx-5k-mouse-kidney") is None


def test_collection_doi_filters_sentinels() -> None:
    assert P.collection_doi({"collection_doi": "10.1038/s41586-020-2157-4"}) == "10.1038/s41586-020-2157-4"
    for sentinel in ("", "unknown", "UNKNOWN", "n/a", "none", "null", "-", None):
        assert P.collection_doi({"collection_doi": sentinel}) is None, sentinel


# --------------------------------------------------------------- 不变量 3

def test_as_of_never_borrows_another_date() -> None:
    """`as_of` 只能来自真实抓取戳，**绝不拿 published_date 冒充**。

    published_date 是数据集的发表日，不是我们的核验日。把它当 as_of 印进稿件，
    等于宣称「我们在 2020 年核验过」——而工具那时还不存在。
    """
    item = {"source": P.SOURCE_CELLXGENE, "dataset_uid": "cxg:x", "published_date": "1999-01-01"}
    v = P.raw_data_provenance(item)
    assert v["as_of"] is None                      # 库级事实档没有核验戳
    assert "1999" not in str(v)                    # 那个日期一个字都不许漏进来


def test_unverified_basis_never_claims_a_state() -> None:
    """ArrayExpress 是本模块存在的首要理由：ingest 自陈「不逐条核实」→ 必须是 NOT_CHECKED。"""
    v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-11814",
                               "has_raw_data": False})
    assert v["state"] == P.NOT_CHECKED             # 那个 False 是猜的，不是来源的声明
    assert v["basis"] == P.BASIS_UNVERIFIED
    assert v["elsewhere_hint"] and "ENA" in v["elsewhere_hint"]  # 方向上原始数据大概率在 ENA


def test_repository_policy_sources_are_not_listed_with_hint() -> None:
    for source in (P.SOURCE_CELLXGENE, P.SOURCE_SCEA):
        v = P.raw_data_provenance({"source": source, "dataset_uid": "cxg:x", "has_raw_data": False})
        assert v["state"] == P.NOT_LISTED
        assert v["basis"] == P.BASIS_REPOSITORY_POLICY
        assert v["elsewhere_hint"], source          # 说了「这里没有」就必须说「可能在哪」


def test_hca_reads_source_manifest() -> None:
    base = {"source": P.SOURCE_HCA, "dataset_uid": "hca:x"}
    assert P.raw_data_provenance({**base, "has_raw_data": True})["state"] == P.LISTED
    assert P.raw_data_provenance({**base, "has_raw_data": False})["state"] == P.NOT_LISTED
    assert P.raw_data_provenance({**base, "has_raw_data": None})["state"] == P.NOT_CHECKED
    assert P.raw_data_provenance({**base, "has_raw_data": True})["basis"] == P.BASIS_SOURCE_MANIFEST


def test_10x_uses_live_ledger_and_stamps_as_of() -> None:
    """base 走活台账（本项目最强证据档），且**只有这一档**带核验戳。"""
    v = P.raw_data_provenance({"source": P.SOURCE_10X, "dataset_uid": "multiome-gemx-10k-human-brain"})
    assert v["state"] == P.LISTED
    assert v["basis"] == P.BASIS_LEDGER_PROBE
    assert v["as_of"] == P.snapshot_as_of() and v["as_of"]


def test_10x_unknown_uid_is_not_checked_not_negative() -> None:
    # 台账里没有这条 → 「没查过」，而不是「它没有」。
    v = P.raw_data_provenance({"source": P.SOURCE_10X, "dataset_uid": "definitely-not-in-the-ledger"})
    assert v["state"] == P.NOT_CHECKED


def test_negative_wording_never_claims_nonexistence() -> None:
    """所有否定档的措辞必须是「在 X 里没列出」，绝不能是「它不存在」。"""
    forbidden = ("does not exist", "不存在", "没有原始数据", "not available")
    for source in (P.SOURCE_CELLXGENE, P.SOURCE_SCEA, P.SOURCE_HCA, P.SOURCE_ARRAYEXPRESS, P.SOURCE_10X):
        v = P.raw_data_provenance({"source": source, "dataset_uid": "x:y", "has_raw_data": False})
        blob = f"{v['evidence']}{v['scope']}{v.get('elsewhere_hint') or ''}"
        for bad in forbidden:
            assert bad not in blob, (source, bad)


# --------------------------------------------------------------- 逐记录证据优先

def _ae_prov(retrieved_at: str = "2026-07-20T10:00:00+00:00") -> dict:
    return {
        "complete": True,
        "retrieved_at": retrieved_at,
        "fields": {
            "files": {"complete": True},
            "has_raw_data": {"complete": True},
        },
    }


def test_ae_per_record_listed_uses_source_manifest() -> None:
    v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-11814",
                               "has_raw_data": True, "metadata_provenance": _ae_prov()})
    assert v["state"] == P.LISTED
    assert v["basis"] == P.BASIS_SOURCE_MANIFEST
    assert v["as_of"] == "2026-07-20T10:00:00+00:00"
    assert v["elsewhere_hint"] is None


def test_ae_per_record_not_listed_points_to_ena() -> None:
    """完整逐记录证据 + has_raw_data 非 True（含 None）→ NOT_LISTED，且必须指路 ENA。"""
    for has_raw in (False, None):
        v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-11814",
                                   "has_raw_data": has_raw, "metadata_provenance": _ae_prov()})
        assert v["state"] == P.NOT_LISTED, has_raw
        assert v["basis"] == P.BASIS_SOURCE_MANIFEST
        assert "不代表原始数据不存在" in v["evidence"]
        assert v["elsewhere_hint"] and "ENA" in v["elsewhere_hint"]


def test_ae_per_record_retrieved_at_must_be_nonempty_str() -> None:
    """as_of 只认真实抓取戳：retrieved_at 不是非空 str 就留 None，不拿别的值冒充。"""
    prov = _ae_prov(retrieved_at="")
    v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-1",
                               "has_raw_data": True, "metadata_provenance": prov})
    assert v["state"] == P.LISTED and v["as_of"] is None
    prov2 = _ae_prov()
    prov2["retrieved_at"] = 12345
    v2 = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-1",
                                "has_raw_data": True, "metadata_provenance": prov2})
    assert v2["state"] == P.LISTED and v2["as_of"] is None


def test_ae_without_provenance_stays_not_checked() -> None:
    """回归：没有逐记录证据的旧 AE 记录仍是 NOT_CHECKED —— 那个 False 是猜的。"""
    v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-11814",
                               "has_raw_data": False})
    assert v["state"] == P.NOT_CHECKED and v["basis"] == P.BASIS_UNVERIFIED


def _scea_prov(file_complete: bool = True, retrieved_at: str = "2026-07-18T08:00:00+00:00") -> dict:
    return {
        "cross_source_enrichment": {
            "file_evidence": {"complete": file_complete},
            "biostudies_document": {"retrieved_at": retrieved_at},
        }
    }


def test_scea_per_record_file_evidence_overrides_repository_policy() -> None:
    """file_evidence.complete=True → 逐条核验档（source_manifest），盖过库级 repository_policy。"""
    for has_raw, state in ((True, P.LISTED), (False, P.NOT_LISTED), (None, P.NOT_LISTED)):
        v = P.raw_data_provenance({"source": P.SOURCE_SCEA, "dataset_uid": "ebi:E-ANND-1",
                                   "has_raw_data": has_raw, "metadata_provenance": _scea_prov()})
        assert v["state"] == state, (has_raw, v)
        assert v["basis"] == P.BASIS_SOURCE_MANIFEST, has_raw
        assert v["as_of"] == "2026-07-18T08:00:00+00:00"
    v_not = P.raw_data_provenance({"source": P.SOURCE_SCEA, "dataset_uid": "ebi:E-ANND-1",
                                   "has_raw_data": None, "metadata_provenance": _scea_prov()})
    assert "不代表原始数据不存在" in v_not["evidence"]


def test_scea_per_record_not_listed_hints_ena_study() -> None:
    v = P.raw_data_provenance({"source": P.SOURCE_SCEA, "dataset_uid": "ebi:E-ANND-1",
                               "has_raw_data": False, "ena_study_accessions": ["ERP129047"],
                               "metadata_provenance": _scea_prov()})
    assert v["state"] == P.NOT_LISTED
    assert v["elsewhere_hint"] and "ERP129047" in v["elsewhere_hint"] and "ENA" in v["elsewhere_hint"]


def test_scea_incomplete_file_evidence_falls_back() -> None:
    """回归：file_evidence.complete=False → 落回库级 repository_policy，语义不变。"""
    v = P.raw_data_provenance({"source": P.SOURCE_SCEA, "dataset_uid": "ebi:E-ANND-1",
                               "has_raw_data": False, "ena_study_accessions": ["ERP129047"],
                               "metadata_provenance": _scea_prov(file_complete=False)})
    assert v["state"] == P.NOT_LISTED
    assert v["basis"] == P.BASIS_REPOSITORY_POLICY
    assert v["elsewhere_hint"] and "ENA" in v["elsewhere_hint"]


def _hca_prov(raw_complete: bool, raw_value: object = None,
              retrieved_at: str = "2026-07-19T09:00:00+00:00") -> dict:
    return {
        "complete": True,
        "retrieved_at": retrieved_at,
        "raw_data": {"complete": raw_complete, "value": raw_value},
    }


def test_hca_per_record_listed_when_manifest_says_so() -> None:
    v = P.raw_data_provenance({"source": P.SOURCE_HCA, "dataset_uid": "hca:x",
                               "has_raw_data": True,
                               "metadata_provenance": _hca_prov(True, True)})
    assert v["state"] == P.LISTED
    assert v["basis"] == P.BASIS_SOURCE_MANIFEST
    assert v["as_of"] == "2026-07-19T09:00:00+00:00"


def test_hca_per_record_not_listed_when_manifest_says_no() -> None:
    v = P.raw_data_provenance({"source": P.SOURCE_HCA, "dataset_uid": "hca:x",
                               "has_raw_data": False,
                               "metadata_provenance": _hca_prov(True, False)})
    assert v["state"] == P.NOT_LISTED
    assert v["basis"] == P.BASIS_SOURCE_MANIFEST
    assert v["elsewhere_hint"]


def test_hca_incomplete_raw_data_falls_back() -> None:
    """回归：raw_data.complete=False（现有 20 条实况）→ 落回旧来源级分支。"""
    for has_raw, state in ((True, P.LISTED), (False, P.NOT_LISTED), (None, P.NOT_CHECKED)):
        v = P.raw_data_provenance({"source": P.SOURCE_HCA, "dataset_uid": "hca:x",
                                   "has_raw_data": has_raw,
                                   "metadata_provenance": _hca_prov(False, None)})
        assert v["state"] == state, (has_raw, v)


def test_hca_contradictory_raw_value_falls_back() -> None:
    """raw_data.value=True 但索引 has_raw_data 不是 True → 不一致组合不下结论，落回旧分支。"""
    v = P.raw_data_provenance({"source": P.SOURCE_HCA, "dataset_uid": "hca:x",
                               "has_raw_data": False,
                               "metadata_provenance": _hca_prov(True, True)})
    assert v["state"] == P.NOT_LISTED and v["scope_en"].endswith("file type summary")


def _encode_prov() -> dict:
    return {"complete": True, "files": {"complete": True}}


def test_encode_per_record_listed_without_as_of() -> None:
    """ENCODE 快照内无真实抓取戳 → as_of 必须诚实留 None。"""
    v = P.raw_data_provenance({"source": P.SOURCE_ENCODE, "dataset_uid": "encode:ENCSR009WQK",
                               "has_raw_data": True, "metadata_provenance": _encode_prov()})
    assert v["state"] == P.LISTED
    assert v["basis"] == P.BASIS_SOURCE_MANIFEST
    assert v["as_of"] is None
    assert v["elsewhere_hint"] is None


def test_encode_per_record_not_listed() -> None:
    v = P.raw_data_provenance({"source": P.SOURCE_ENCODE, "dataset_uid": "encode:ENCSR009WQK",
                               "has_raw_data": False, "metadata_provenance": _encode_prov()})
    assert v["state"] == P.NOT_LISTED
    assert v["basis"] == P.BASIS_SOURCE_MANIFEST
    assert "不代表原始数据不存在" in v["evidence"]


def test_encode_has_raw_none_falls_back_to_unchecked() -> None:
    """has_raw_data=None → 逐记录入口放弃，落回 _unchecked（NOT_CHECKED），不猜方向。"""
    v = P.raw_data_provenance({"source": P.SOURCE_ENCODE, "dataset_uid": "encode:ENCSR009WQK",
                               "has_raw_data": None, "metadata_provenance": _encode_prov()})
    assert v["state"] == P.NOT_CHECKED
    assert v["basis"] == P.BASIS_UNVERIFIED


# --------------------------------------------------------------- 逐记录入口的对抗面

def test_malformed_provenance_falls_back_without_crashing() -> None:
    """形状不认的 metadata_provenance 一律落回来源级默认，绝不拿不明字段当证据。"""
    cases = (
        # complete=True 但缺 fields 段
        (P.SOURCE_ARRAYEXPRESS, {"complete": True}, P.NOT_CHECKED),
        # complete=True、fields 缺 has_raw_data 子段
        (P.SOURCE_ARRAYEXPRESS, {"complete": True, "fields": {"files": {"complete": True}}}, P.NOT_CHECKED),
        # complete 不是 True
        (P.SOURCE_ENCODE, {"complete": "yes", "files": {"complete": True}}, P.NOT_CHECKED),
        # mp 根本不是 dict
        (P.SOURCE_ARRAYEXPRESS, "not a dict", P.NOT_CHECKED),
        (P.SOURCE_ENCODE, "not a dict", P.NOT_CHECKED),
        (P.SOURCE_HCA, ["not", "a", "dict"], P.NOT_LISTED),  # has_raw_data=False → 旧 HCA 分支
    )
    for source, mp, expected in cases:
        v = P.raw_data_provenance({"source": source, "dataset_uid": "x:y",
                                   "has_raw_data": False, "metadata_provenance": mp})
        assert v["state"] == expected, (source, mp, v)


def test_forged_complete_provenance_still_passes_finalize() -> None:
    """记录自称 complete=True 且 has_raw_data=True → 给 LISTED，但产物必须过 _finalize 闸：
    scope/scope_en 非空、scope_en 纯 ASCII —— 结构闸门对逐记录分支同样有效。"""
    v = P.raw_data_provenance({"source": P.SOURCE_ARRAYEXPRESS, "dataset_uid": "ae:E-MTAB-1",
                               "has_raw_data": True, "metadata_provenance": _ae_prov()})
    assert v["state"] == P.LISTED
    assert v["scope"].strip() and v["scope_en"].strip()
    assert v["scope_en"].isascii()


def test_encode_uid_is_public_accession_not_platform_id() -> None:
    """encode:ENCSR… 的 bare 值是公开 accession；与 platform_id 互斥。"""
    assert P.public_accession("encode:ENCSR009WQK") == "ENCSR009WQK"
    assert P.platform_id("encode:ENCSR009WQK") is None


def test_new_sources_prefix_tables() -> None:
    """geo/zenodo/refinebio/hubmap/scp 五源进前缀表。

    修复前前缀表只覆盖五源（ae/ebi/cxg/hca/encode），这五源的记录 reuse_pack
    Identifier 恒空、gap 误报「来源未登记可引用编号」。判定依据是各 ingest 脚本与
    快照实测：geo/scp/zenodo/refinebio 的 uid 后半段是公开 accession（zenodo 快照
    自身的 public_accession 字段就声明 record id）；hubmap 的 uid 后半段是平台 UUID，
    HBM 编号只在记录的 public_accession 字段上（不由 uid 前缀表承担）。
    """
    assert P.public_accession("geo:GSE3642") == "GSE3642"
    assert P.public_accession("zenodo:263694") == "263694"
    assert P.public_accession("refinebio:SRP059902") == "SRP059902"
    assert P.public_accession("scp:SCP1") == "SCP1"
    assert P.platform_id("hubmap:0008a49ac06f4afd886be81491a5a926") == "0008a49ac06f4afd886be81491a5a926"
    # accession 与 platform_id 互斥在新源上同样成立
    for uid in ("geo:GSE3642", "zenodo:263694", "refinebio:SRP059902", "scp:SCP1",
                "hubmap:0008a49ac06f4afd886be81491a5a926"):
        assert not (P.public_accession(uid) and P.platform_id(uid)), uid
    # 两张表键集一致、取值互补（手抄清单的结构性自检：加新源时两张表必须同步）
    assert set(P._PREFIX_IS_PUBLIC_ACCESSION) == set(P._PREFIX_IS_PLATFORM_ID)
    for prefix in P._PREFIX_IS_PUBLIC_ACCESSION:
        assert P._PREFIX_IS_PUBLIC_ACCESSION[prefix] != P._PREFIX_IS_PLATFORM_ID[prefix], prefix


def test_per_record_negative_wording_keeps_scoped_disclaimer() -> None:
    """新分支的否定措辞模式：必须带「在哪份清单里没列出」+「不代表不存在」的双重限定；
    去掉这句必需的免责后，其余文本不得含任何无作用域的否定措辞。"""
    forbidden = ("does not exist", "没有原始数据", "not available")
    cases = (
        (P.SOURCE_ARRAYEXPRESS, _ae_prov()),
        (P.SOURCE_SCEA, _scea_prov()),
        (P.SOURCE_HCA, _hca_prov(True, False)),
        (P.SOURCE_ENCODE, _encode_prov()),
    )
    for source, mp in cases:
        v = P.raw_data_provenance({"source": source, "dataset_uid": "x:y",
                                   "has_raw_data": False, "metadata_provenance": mp})
        assert v["state"] == P.NOT_LISTED, source
        blob = f"{v['evidence']}{v['scope']}{v.get('elsewhere_hint') or ''}"
        assert "不代表原始数据不存在" in blob or source in (P.SOURCE_HCA,), source
        cleaned = blob.replace("这不代表原始数据不存在。", "")
        for bad in forbidden:
            assert bad not in cleaned, (source, bad)
        assert "不存在" not in cleaned, (source, cleaned)


# --------------------------------------------------------------- size

def test_size_bytes_collapses_fake_zeros() -> None:
    """2702 条 external 把「未知」硬编码成 0 字节；任何直接渲染 filesize 的产物都在撒谎。"""
    assert P.size_bytes_or_none({"filesize": 1234}) == 1234
    for fake in (0, -1, None, "", "abc", {}):
        assert P.size_bytes_or_none({"filesize": fake}) is None, fake


# --------------------------------------------------------------- 结构性隔离

def _intra_package_imports(path: Path) -> set[str]:
    """一个模块直接 import 的**本包内**候选模块名（子包化：返回点形路径的每一段，
    由 `_import_closure` 的名字索引裁决——`from ..corpus.normalizer import x` 贡献
    corpus/normalizer 两候选。过度包含是安全的：闭包只用来抓泄漏，多算不算错）。"""
    # utf-8-sig：本仓库部分模块带 BOM（config.py 等），utf-8 会把它读成非法字符。
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from . import fair` / `from .fair import x` / `from ..corpus.normalizer import x`
            # / `from dataset_recommender.retrieval.fair import x`
            if node.level and node.module:
                out.update(node.module.split("."))
                out.update(a.name for a in node.names)
            elif node.level:
                out.update(a.name for a in node.names)
            elif node.module and node.module.startswith("dataset_recommender"):
                parts = node.module.split(".")
                if len(parts) > 1:
                    out.update(parts[1:])
                out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("dataset_recommender."):
                    out.update(a.name.split(".")[1:])
    return out


def _pkg_module_index(pkg_dir: Path) -> dict[str, Path]:
    """模块名 → 文件路径（子包化后模块不再平铺：rglob 递归建索引；模块名跨子包唯一）。"""
    return {p.stem: p for p in pkg_dir.rglob("*.py") if p.name != "__init__.py"}


def _import_closure(entry: Path, pkg_dir: Path) -> set[str]:
    """entry 的**传递** import 闭包（本包内，跨子包）。"""
    index = _pkg_module_index(pkg_dir)
    seen: set[str] = set()
    stack = [entry]
    while stack:
        cur = stack.pop()
        for name in _intra_package_imports(cur):
            if name in seen or name not in index:
                continue
            seen.add(name)
            stack.append(index[name])
    return seen


def test_frozen_path_never_reaches_manuscript_modules() -> None:
    """冻结 767 评测路径的**传递闭包**里不得出现任何稿件产物模块。

    旧测试只在 3 个文件里 substring 找 `import fair`——那是 **1 跳**检查：
    只要 retriever 引了某个中间模块、而那个模块引了 fair，旧测试照样绿。
    这里改成真正的传递闭包，隔离才是结构性的，而不是靠「今天没人这么写」。
    """
    pkg = ROOT / "src" / "dataset_recommender"
    manuscript_modules = {"fair", "provenance", "reuse_pack", "intro_zh"}
    for rel in (
        "src/dataset_recommender/retrieval/retriever.py",
        "src/dataset_recommender/app/workflow.py",
        "scripts/evaluate_recommendation.py",
    ):
        closure = _import_closure(ROOT / rel, pkg)
        leaked = closure & manuscript_modules
        assert not leaked, f"{rel} 的 import 闭包漏进了稿件产物模块：{leaked}"
        # 反真空钉（子包化教训：平铺解析失灵会让闭包悄悄变空、隔离测试假绿）：
        # 闭包必须真的走到了核心模块——retriever 的传递依赖里 normalizer/query_parser 必在。
        assert {"normalizer", "query_parser"} <= closure, \
            f"{rel} 的闭包异常小（{len(closure)} 项）——解析失灵，不是真隔离"


def test_closure_helper_actually_detects_a_transitive_leak(tmp_path: Path) -> None:
    """反重言式：证明上面那个闭包检查**真的能抓到多跳泄漏**，而不是恒真。

    审计教训：`test_partial_value_set_honesty` 曾把 bug 断言成正确行为。
    一个「永远绿」的隔离测试比没有测试更危险——它给人已被守护的错觉。
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "leaf.py").write_text("from . import fair\n", encoding="utf-8")
    (pkg / "middle.py").write_text("from . import leaf\n", encoding="utf-8")
    (pkg / "entry.py").write_text("from . import middle\n", encoding="utf-8")
    (pkg / "fair.py").write_text("x = 1\n", encoding="utf-8")
    closure = _import_closure(pkg / "entry.py", pkg)
    assert "fair" in closure          # 3 跳外的泄漏必须被抓到
    assert {"middle", "leaf"} <= closure
