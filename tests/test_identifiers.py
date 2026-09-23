# -*- coding: utf-8 -*-
"""标识符精确反查 + 诚实 fail-closed。"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.content import identifiers  # noqa: E402
from dataset_recommender.corpus.corpus import load_full_corpus  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


def _corpus():
    s = get_settings()
    return load_full_corpus(s.data_dir, s.project_root)


def test_classify_kinds():
    assert identifiers.classify("GSE123456")["kind"] == "geo"
    assert identifiers.classify("GSM99999")["kind"] == "geo_sample"
    assert identifiers.classify("SRR1234567")["kind"] == "sra"
    assert identifiers.classify("PRJNA555")["kind"] == "sra"
    assert identifiers.classify("HBM626.KXRZ.238")["kind"] == "hubmap_accession"
    assert identifiers.classify("SCP128")["kind"] == "scp_accession"
    assert identifiers.classify("E-MTAB-11452")["kind"] == "arrayexpress_accession"
    assert identifiers.classify("abcdef12-3456-7890-abcd-ef1234567890")["kind"] == "cellxgene_uuid"
    assert identifiers.classify("10.1038/s41586-020-2157-4")["kind"] == "doi"
    assert identifiers.classify("人类肺癌免疫细胞") is None      # 普通查询不是标识符
    assert identifiers.classify("") is None


def test_classify_encode_accession():
    hit = identifiers.classify("ENCSR009WQK")
    assert hit["kind"] == "encode_accession" and hit["indexed"] is True
    # 5 位尾段不是合法 ENCODE 实验编号（ENCSR + 6 位 [0-9A-Z]）
    assert (identifiers.classify("ENCSR009WQ") or {}).get("kind") != "encode_accession"


def test_lookup_encode_accession_hits_record():
    corpus = _corpus()
    acc = None
    for rec in corpus:
        uid = str((rec.raw or {}).get("dataset_uid") or "")
        m = re.search(r"ENCSR[0-9A-Z]{6}", uid)
        if m:
            acc = m.group(0)
            break
    if not acc:
        import pytest
        pytest.skip("语料里没有 ENCSR accession 可测")
    res = identifiers.lookup(acc, lambda: corpus)
    assert res["is_identifier"] and res["indexed"] is True
    assert res["kind"] == "encode_accession"
    assert res["match"] is not None and res["match"].get("dataset_uid")


def test_geo_and_indexed_flags():
    # 三源接入：GEO Series（GSE）随 geo.json 入库转为 indexed；GSM（Sample 级）仍结构性不索引。
    assert identifiers.classify("GSE123")["indexed"] is True
    assert identifiers.classify("GSM123")["indexed"] is False
    assert identifiers.classify("HBM626.KXRZ.238")["indexed"] is True
    assert identifiers.classify("SCP128")["indexed"] is True
    assert identifiers.classify("E-MTAB-1")["indexed"] is True    # AE 在本目录


def test_lookup_geo_sample_is_fail_closed_not_silent():
    res = identifiers.lookup("GSM12345", lambda: (_ for _ in ()).throw(AssertionError("不该装载语料")))
    # GSM 无需装载语料即可 fail-closed（惰性证明：load_records 一旦被调用就抛错）
    assert res["is_identifier"] and res["indexed"] is False and res["match"] is None
    assert "GEO" in res["message"] and "Series" in res["message"]
    assert res["external_url"].endswith("GSM12345")


def test_lookup_gse_indexed_but_unlisted_is_honest():
    # GSE 已入库（试点切片 60 条）：不在切片里的 GSE 如实说「未匹配」，不再冒充「结构性不索引 GEO」。
    res = identifiers.lookup("GSE12345", _corpus)
    assert res["is_identifier"] and res["indexed"] is True and res["match"] is None
    assert "未匹配本目录任何数据集" in res["message"]


def test_scp_accession_boundary_not_prefix_swallowed():
    """「SCP1」必须精确命中 1 条，不被 scp10/scp101/… 前缀吞并（集成抓获：曾返 241 条候选）。"""
    corpus = _corpus()
    if not any(str((r.raw or {}).get("dataset_uid") or "").startswith("scp:") for r in corpus):
        import pytest
        pytest.skip("语料无 SCP（未提升时跳过）")
    res = identifiers.lookup("SCP1", lambda: corpus)
    assert res["indexed"] is True and res["match"] is not None
    assert res["match"]["dataset_uid"] == "scp:SCP1"
    # 边界是双向的：SCP10 也不能被 SCP1 的查询捎带
    res10 = identifiers.lookup("SCP10", lambda: corpus)
    if res10["match"] is not None:
        assert res10["match"]["dataset_uid"] == "scp:SCP10"


def test_lookup_new_source_accessions_hit_records():
    """三源直达：geo:GSE / scp:SCP / hubmap:HBM 编号（HBM 走 public_accession 等值匹配）。"""
    corpus = _corpus()
    by_uid = {}
    for rec in corpus:
        uid = str((rec.raw or {}).get("dataset_uid") or "")
        for prefix in ("geo:", "scp:", "hubmap:"):
            if uid.startswith(prefix):
                by_uid.setdefault(prefix, rec)
    if len(by_uid) < 3:
        import pytest
        pytest.skip("语料里三源不全（未提升时跳过）")
    geo_acc = str((by_uid["geo:"].raw or {}).get("public_accession"))
    res = identifiers.lookup(geo_acc, lambda: corpus)
    assert res["indexed"] is True and res["match"] is not None
    scp_acc = str((by_uid["scp:"].raw or {}).get("public_accession"))
    res = identifiers.lookup(scp_acc, lambda: corpus)
    assert res["indexed"] is True and res["match"] is not None
    hbm_acc = str((by_uid["hubmap:"].raw or {}).get("public_accession"))
    res = identifiers.lookup(hbm_acc, lambda: corpus)
    assert res["indexed"] is True and res["match"] is not None


def test_lookup_refinebio_alternate_gse_hits_mirror_record():
    """refine.bio 镜像条目（第 11 源）的 GSE 副号直达：
    主号是 SRP（sra 类编号结构性不索引，fail-closed 语义不动），副号 GSE 经
    `alternate_accession` 等值匹配直达同一条目。"""
    corpus = _corpus()
    rb = [rec for rec in corpus
          if str((rec.raw or {}).get("dataset_uid") or "").startswith("refinebio:")
          and str((rec.raw or {}).get("alternate_accession") or "").startswith("GSE")]
    if not rb:
        import pytest
        pytest.skip("语料里无 refine.bio 切片（未提升时跳过）")
    alt = str((rb[0].raw or {}).get("alternate_accession"))
    res = identifiers.lookup(alt, lambda: corpus)
    assert res["indexed"] is True and res["match"] is not None
    assert res["match"]["dataset_uid"] == str((rb[0].raw or {}).get("dataset_uid"))
    assert res["match"]["source"] == "refine.bio"


def test_lookup_non_identifier_returns_none():
    assert identifiers.lookup("人类肺组织", _corpus) is None
def test_lookup_real_accession_hits_record():
    corpus = _corpus()
    acc = None
    for rec in corpus:
        uid = str((rec.raw or {}).get("dataset_uid") or "")
        m = re.search(r"E-[A-Z]{4}-\d+", uid)
        if m:
            acc = m.group(0)
            break
    if not acc:
        import pytest
        pytest.skip("语料里没有 E-XXXX-N accession 可测")
    res = identifiers.lookup(acc, lambda: corpus)
    assert res["is_identifier"] and res["indexed"] is True
    assert res["match"] is not None and res["match"].get("dataset_uid")


def test_recommend_carries_identifier_lookup():
    client = TestClient(app, base_url="http://127.0.0.1")
    # 普通查询 → null
    r = client.post("/api/recommend", json={"query": "人类肺癌数据", "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["identifier_lookup"] is None
    # GEO 号 → 非 null； GSE 已入库（indexed=True），未命中时如实说「未匹配」
    r2 = client.post("/api/recommend", json={"query": "GSE181919", "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r2["identifier_lookup"] and r2["identifier_lookup"]["kind"] == "geo"
    assert r2["identifier_lookup"]["indexed"] is True


def test_mcp_lookup_identifier_tool():
    from dataset_recommender.app import mcp_server as M

    out = M.lookup_identifier(identifier="GSE181919")
    assert out["ok"] and out["lookup"]["kind"] == "geo" and out["lookup"]["indexed"] is True
    # 非标识符 → lookup=None
    assert M.lookup_identifier(identifier="人类肺组织数据")["lookup"] is None
    # 空 → ToolError
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        M.lookup_identifier(identifier="   ")
