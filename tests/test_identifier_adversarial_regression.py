# -*- coding: utf-8 -*-
"""对抗性验证已修病形的回归门。

转正自对抗性验证中被修复打掉用例的「应然」面（病形面随修复作废，不搬）：

- 裸标识符绕过形：尾随句读（.。，,/）/ 零宽字符 / 全角与引号包装 → 剥壳判等后
  仍算「裸贴」；中文包裹（「帮我查 <DOI> 这个数据集」）→ executable 空约束兜底闸
  fail-closed。两条都走 identifier_direct 诚实通道，绝不退化成全库 top-N 冒充结果。
- 标识符反查：共享 DOI（一篇论文挂多个数据集）命中 ≥2 条 → 与 locate_record 的
  409 消歧同口径如实列候选，不再静默取第一还用单数「已直达」；`v in doi` 子串宽松
  收紧为「去前缀 + casefold 后等值」，残片（10.1101/2021）不得假直达。
- locate_record 比较键 NFC + 去零宽 + casefold；uid 撞库与 name 同口径报歧义；
  `_RE_ENCSR` 大小写不敏感。
"""
import sys
import unicodedata
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.content import identifiers  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.corpus.corpus import load_full_corpus, locate_record  # noqa: E402
from dataset_recommender.content.identifier_patterns import classify  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

DOI_MISS = "10.1038/s41597-022-01234-5"   # 库里没有的 DOI（对照干净裸贴已 abstain）


def _rec(uid: str, name: str, url: str, source: str, doi: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="", count="", unit="",
        has_raw_data=None, url=url, source_file="", description="",
        raw={"dataset_uid": uid, "source": source, "collection_doi": doi},
    )


def _corpus():
    s = get_settings()
    return load_full_corpus(s.data_dir, s.project_root)


# ---------- 裸标识符尾随标点 / 零宽 / 全角包装 → identifier_direct 诚实通道 ----------

#: 真实世界最高频的粘贴形态：从句尾复制 DOI 带上的句读、斜杠、零宽空格、全角括号。
TRAILING_JUNK_FORMS = [".", "。", ",", "，", "/", "​", "）"]


@pytest.mark.parametrize("suffix", TRAILING_JUNK_FORMS)
def test_bare_doi_with_trailing_junk_abstains_identifier_direct(suffix):
    """尾随垃圾字符的裸 DOI 必须仍判「裸贴」→ fail-closed，不再 executable 空约束。"""
    p = parse_query(DOI_MISS + suffix)
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_direct"


@pytest.mark.parametrize("wrapped", [
    DOI_MISS + ".",
    DOI_MISS + "。",
    DOI_MISS + "，",
    "（" + DOI_MISS + "）",
    "「" + DOI_MISS + "」",
])
def test_bare_doi_wrapped_end_to_end_no_masquerade(wrapped):
    """端到端：这些形态一律 abstained + 0 结果 + 标识符条如实说未匹配（对照旧病形：
    resolution_status=results、空约束、774 条里切 top-5 冒充）。"""
    r = client.post("/api/recommend", json={
        "query": wrapped, "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained"
    assert r["results"] == []
    assert r["identifier_lookup"] and r["identifier_lookup"]["match"] is None


@pytest.mark.parametrize("q", [
    "帮我查 " + DOI_MISS + " 这个数据集",
    DOI_MISS + " 的数据",
])
def test_chinese_wrapped_identifier_empty_plan_fails_closed(q):
    """中文包裹（最日常的提问形态）：executable 空约束 + 剔除标识符后仍有实义残留
    → 兜底闸弃权 fail-closed，绝不拿全库冒充结果。"""
    p = parse_query(q)
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_direct"
    r = client.post("/api/recommend", json={
        "query": q, "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained" and r["results"] == []


def test_normal_search_sentence_unaffected_by_fallback_gate():
    """兜底闸零误伤：正常检索句照常 executable；句中**提到**编号的混合诉求不走 identifier_direct。"""
    normal = parse_query("推荐有 FASTQ 的人类乳腺癌数据")
    assert normal.parse_status == "executable" and any(normal.constraints.values())
    embedded = parse_query("把 E-MTAB-1234 打包")
    assert embedded.abstain_reason != "identifier_direct"


# ---------- 补刀：裸 DOI 残片（classify 不认的形态）也 fail-closed ----------

#: 「形似 DOI 但没写全」的输入：注册前缀 / 带孤零零斜杠 / 带尾随句读 / 全角包装 / 零宽。
DOI_FRAGMENT_FORMS = [
    "10.1038",
    "10.1038/",
    "10.1038。",
    "10.1038.",
    "（10.1038）",
    "10.1038​",
]


@pytest.mark.parametrize("q", DOI_FRAGMENT_FORMS)
def test_bare_doi_fragment_abstains_identifier_fragment(q):
    """裸 DOI 残片 → identifier_fragment 诚实弃权（文案点明补全），不再 executable 空约束冒充。"""
    p = parse_query(q)
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_fragment"
    assert "没写全" in p.abstain_detail and "全库检索" in p.abstain_detail


@pytest.mark.parametrize("q", DOI_FRAGMENT_FORMS[:4])
def test_bare_doi_fragment_end_to_end_no_masquerade(q):
    """端到端：残片一律 abstained + 0 结果（对照旧病形：results/774/top-5 且连标识符条都没有）。"""
    r = client.post("/api/recommend", json={
        "query": q, "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained" and r["results"] == []


def test_doi_like_but_complete_still_identifier_direct():
    """形态完整的 DOI（含 10.1101/2021 这类残片形但 regex 合法的）仍走 identifier_direct。"""
    for q in ("10.1101/2021", "10.1101/2021.", "10.5281/zenodo.1234567"):
        p = parse_query(q)
        assert p.parse_status == "abstained" and p.abstain_reason == "identifier_direct", q


def test_version_number_in_real_search_sentence_not_confiscated():
    """防误判：含版本号语义的检索句不受影响——约束照抽、不被残片闸没收。"""
    p = parse_query("cellranger 3.10.10380 处理的人类肺数据")
    assert p.parse_status == "executable" and any(p.constraints.values())
    assert p.abstain_reason != "identifier_fragment"


# ---------- 共享 DOI 如实列候选；残片不得假直达 ----------

def test_shared_doi_lists_all_candidates_never_silent_first():
    """同一 DOI 被 2 条记录共享 → match=None + candidates 全列出 + 文案说明条数，
    不再静默返回语料序第一条还用单数「已直达」对其余记录零披露。"""
    doi = "10.1101/2099.01.01.000001"
    recs = [
        _rec("uid-a", "数据集甲", "http://x/1", "ArrayExpress", doi=doi),
        _rec("uid-b", "数据集乙", "http://x/2", "ArrayExpress", doi=doi),
    ]
    out = identifiers.lookup(doi, lambda: recs)
    assert out["match"] is None
    assert {c["dataset_uid"] for c in out["candidates"]} == {"uid-a", "uid-b"}
    assert "2" in out["message"] and "直达" not in out["message"]


def test_shared_doi_real_corpus_same_caliber():
    """真实语料（246 个共享 DOI）：任取一个共享 DOI，反查必须如实列出全部候选。"""
    recs = _corpus()
    by_doi = {}
    for r in recs:
        d = str((r.raw or {}).get("collection_doi") or "").strip().casefold()
        if d:
            by_doi.setdefault(d, []).append(r)
    shared = next((d for d, rs in by_doi.items() if len(rs) >= 2), None)
    if shared is None:
        pytest.fail("真实语料里已没有任何共享 DOI——该口径的真实夹具消失，请重新评估本测试。")
    out = identifiers.lookup(shared, lambda: recs)
    assert out["match"] is None
    assert len(out["candidates"]) == len(by_doi[shared])


def test_doi_fragment_no_longer_false_direct_hit():
    """`v in doi` 子串已收紧为去前缀+casefold 后等值：残片 10.1101/2021 不得假直达。"""
    recs = _corpus()
    out = identifiers.lookup("10.1101/2021", lambda: recs)
    assert out["match"] is None and "candidates" not in out
    assert "未匹配" in out["message"]


def test_full_doi_and_prefixed_form_still_hit():
    """收紧不误伤：完整 DOI / 带 https://doi.org/ 前缀的形态仍正常直达（单条命中时）。"""
    doi = "10.1101/2099.01.01.000002"
    recs = [_rec("uid-a", "数据集甲", "http://x/1", "ArrayExpress",
                 doi="https://doi.org/" + doi)]   # 存储侧带前缀也要中等值
    for form in (doi, "https://doi.org/" + doi, doi.upper()):
        hits = identifiers._find_records("doi", form, recs)
        assert [str((h.raw or {}).get("dataset_uid")) for h in hits] == ["uid-a"]


# ---------- locate_record 比较键正规化 + uid 撞库消歧；ENCSR 大小写 ----------

def test_locate_cmp_key_nfc_zero_width_casefold():
    """比较键 NFC + 去零宽 + casefold（只动比较键，展示用原文）：
    NFD 粘贴形态 / 尾随零宽空格 / 大小写差异不再让「视觉相同」的键漏配。"""
    nfc = unicodedata.normalize("NFC", "café-单细胞")
    nfd = unicodedata.normalize("NFD", "café-单细胞")
    r1 = _rec("uid-abc", nfc, "http://x/1", "10x Genomics")
    rec, amb = locate_record([r1], name=nfd)
    assert rec is r1 and amb == []
    rec, _ = locate_record([r1], uid="uid-abc​")   # 尾随 U+200B
    assert rec is r1
    r2 = _rec("uid-1", "Mouse Brain", "http://x/9", "10x Genomics")
    rec, _ = locate_record([r2], name="mouse brain")
    assert rec is r2
    assert r1.dataset_name == nfc                  # 展示原文未被正规化改动


def test_locate_dup_uid_is_ambiguous_never_silent_first():
    """uid 撞库（用户上传/外部快照可注入）→ 与 name 消歧同口径：如实报候选，绝不静默取第一。"""
    first = _rec("uid-dup", "第一条", "http://x/1", "10x Genomics")
    second = _rec("uid-dup", "第二条", "http://x/2", "10x Genomics")
    record, ambiguous = locate_record([first, second], uid="uid-dup")
    assert record is None
    assert {c["dataset_uid"] for c in ambiguous} == {"uid-dup"}
    assert {c["dataset_name"] for c in ambiguous} == {"第一条", "第二条"}
    # source 能消歧就消（同 name 口径）
    r3 = _rec("uid-dup2", "丙", "http://x/3", "10x Genomics")
    r4 = _rec("uid-dup2", "丁", "http://x/4", "ENCODE")
    record, ambiguous = locate_record([r3, r4], uid="uid-dup2", source="ENCODE")
    assert record is r4 and ambiguous == []


def test_encsr_classify_case_insensitive():
    """_RE_ENCSR 与其它标识符正则一致带 IGNORECASE：小写 encsr 也认作 ENCODE 编号。"""
    hit = classify("encsr000aaa")
    assert hit and hit["kind"] == "encode_accession" and hit["indexed"] is True
    p = parse_query("encsr000aaa")
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_direct"
