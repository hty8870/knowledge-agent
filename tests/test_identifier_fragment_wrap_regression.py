# -*- coding: utf-8 -*-
"""两条残片/前缀病形的回归门。

-  中文包裹 DOI 残片：「帮我查 10.1038 这个数据集」「我用 10.1234 这个」——
  修复前兜底闸的残留判定只看「剔除残片后非空」，stopword 包裹（帮我查/这个/数据集）
  被当成实义残留放行 → executable 空约束 → 全库 774 条冒充结果。修复后残留判定
  收紧为「剔除 stopword（vocabulary.FILLER 拆表，单一真源）后仍含实义描述词才不拦」。
  对照组必须零误伤：版本号句（10.10380 版本 / cellranger 3.10.10380）、
  含实义域词（FILLER_DOMAIN）的残留、正常检索句。
-  带前缀裸贴：「https://doi.org/10.xxxx/yyy」「DOI:10.xxxx/yyy」整句裸贴
  修复前掉 unresolved_term，主文案指路「把这些词去掉再搜」——照做只剩纯数字残片，
  正好走回 774 冒充通道。修复后剥前缀判等 → identifier_direct 诚实通道，
  指路文案不再教用户拆 DOI。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

DOI_MISS = "10.5281/zenodo.1234567"   # 库里没有的完整 DOI（反查条如实说未匹配）


# ---------- stopword 包裹的 DOI 残片 → identifier_fragment，绝不 774 冒充 ----------

#: 病形族：残片之外只剩语法/客套/元词（帮我查/这个/数据集/我用/一下/样本/库…）。
FRAGMENT_STOPWORD_WRAPS = [
    "帮我查 10.1038 这个数据集",
    "我用 10.1234 这个",
    "查一下 10.1101 这个数据",
    "麻烦查 10.5281 这个库",
    "10.1234 样本",
    "10.1038，10.1038",
]


@pytest.mark.parametrize("q", FRAGMENT_STOPWORD_WRAPS)
def test_stopword_wrapped_fragment_abstains_identifier_fragment(q):
    """中文包裹残片（残余病形）：视同零残留 → fail-closed，不再 executable 空约束。"""
    p = parse_query(q)
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_fragment"


@pytest.mark.parametrize("q", FRAGMENT_STOPWORD_WRAPS[:2])
def test_stopword_wrapped_fragment_end_to_end_no_774_masquerade(q):
    """端到端：abstained + 0 结果 + 不挂反查条（残片不构成可定位标识符）——
    对照旧病形：resolution_status=results、result_total=774、identifier_lookup=None。"""
    r = client.post("/api/recommend", json={
        "query": q, "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained"
    assert r["results"] == []
    assert r["result_total"] == 0


#: 对照组：版本号语境与含实义域词的残留——必须照常 executable，零误伤。
FRAGMENT_CONTROLS_EXECUTABLE = [
    "10.10380 版本",                 # 残片紧邻「版本」= 被明说为版本号
    "我用的是 10.1038 版本",
    "cellranger 3.10.10380",         # 残片是更长点分版本串的一段
    "10.1234 男性",                  # 残留含实义域词（FILLER_DOMAIN）
    "10.1234 版本的人肺数据",         # 正常检索句带版本号（有维度约束，本就到不了闸）
    "版本 10.1234 的人肺单细胞",
    "小鼠 10.12345 版本 scRNA-seq",
]


@pytest.mark.parametrize("q", FRAGMENT_CONTROLS_EXECUTABLE)
def test_version_context_and_substance_residue_not_blocked(q):
    """对照组零误伤：版本号句 / 实义残留句不被残片闸没收检索。"""
    p = parse_query(q)
    assert p.parse_status == "executable", f"{q!r} 被误拦成 {p.abstain_reason}"


def test_dimension_bearing_fragment_sentence_keeps_constraints():
    """带版本号的正常检索句不仅不被拦，维度约束还得照常落（闸根本没参与）。"""
    p = parse_query("10.1234 版本的人肺数据")
    assert p.parse_status == "executable" and any(p.constraints.values())


# ---------- 带 DOI 解析器前缀的整句裸贴 → identifier_direct ----------

PREFIXED_BARE_DOIS = [
    "https://doi.org/" + DOI_MISS,
    "http://doi.org/" + DOI_MISS,
    "https://dx.doi.org/" + DOI_MISS,
    "doi.org/" + DOI_MISS,
    "DOI:" + DOI_MISS,
    "doi: " + DOI_MISS,
    "HTTPS://DOI.ORG/" + DOI_MISS,          # 大小写变体
    "  https://doi.org/" + DOI_MISS + " 。",  # 带首尾空白与句读
]


@pytest.mark.parametrize("q", PREFIXED_BARE_DOIS)
def test_prefixed_bare_doi_abstains_identifier_direct(q):
    """剥前缀后判等：整句就是一个 DOI → identifier_direct 诚实通道。"""
    p = parse_query(q)
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_direct"


@pytest.mark.parametrize("q", PREFIXED_BARE_DOIS)
def test_prefixed_bare_doi_guidance_no_longer_tells_user_to_strip_words(q):
    """指路文案不得再引回 774 通道：旧病形走 unresolved_term，主文案教用户
    「把这些词去掉再搜」——照做只剩纯数字残片 → 静默丢弃 → 全库冒充。"""
    p = parse_query(q)
    assert "把这些词去掉" not in p.abstain_detail
    assert "标识符" in p.abstain_detail


def test_prefixed_bare_doi_end_to_end_lookup_bar_answers():
    """端到端：abstained + 0 结果；反查条照常工作（_norm_doi 本就不受前缀影响）。"""
    r = client.post("/api/recommend", json={
        "query": "https://doi.org/" + DOI_MISS,
        "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained"
    assert r["results"] == []
    assert r["identifier_lookup"] and r["identifier_lookup"]["match"] is None


def test_prefixed_bare_fragment_abstains_identifier_fragment():
    """带前缀的残片（https://doi.org/10.1038）同款：剥前缀后是残片 → fragment 通道，
    不再掉 unresolved_term 教用户拆词。"""
    p = parse_query("https://doi.org/10.1038")
    assert p.parse_status == "abstained" and p.abstain_reason == "identifier_fragment"
    assert "把这些词去掉" not in p.abstain_detail
