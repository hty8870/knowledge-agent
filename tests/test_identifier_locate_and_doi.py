# -*- coding: utf-8 -*-
""" 普查 / 修复的回归门。

 「查看介绍」/ FAIR 自检张冠李戴：旧实现线性扫描语料、uid/url/name 任一命中即早退——
语料里同名两条（…-ff-ultima / …-ff-ultima-4）时，前端带全参的请求被靠前那条的 name 命中
截胡，介绍/FAIR 讲的是另一条。修复 = `corpus.locate_record` 单一真源：uid 精确 > url 精确 >
name 精确（source 消歧），name 多条消歧失败 → 409 / MCP ambiguous_name 如实报歧义。

 裸贴 DOI 未命中 → 全库 5712 条冒充结果：DOI 被词面解析拆成数字残片静默丢弃 →
executable 空约束 → 全库冒充「满足基本检索条件」。修复 = parse_query 裸标识符 fail-closed
（abstain reason=identifier_direct），与 GSE 编号同一条诚实通道：不做检索，由
identifiers.lookup 反查条如实应答。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.corpus.corpus import load_full_corpus, locate_record  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

#: 语料里的同名两条（天然测试夹具）：同名、同来源、不同 uid/url/发表时间/样本量。
UID_A = "visium-hd-cytassist-gene-expression-libraries-human-breast-cancer-ff-ultima"   # · 583104
UID_B = UID_A + "-4"                                                                     # · 38993


def _rec(uid: str, name: str, url: str, source: str) -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="", count="", unit="",
        has_raw_data=None, url=url, source_file="", description="",
        raw={"dataset_uid": uid, "source": source},
    )


# ---------- locate_record 优先级与消歧（纯单元，不依赖真实语料） ----------

def test_locate_uid_pass_beats_earlier_name_match():
    """uid 精确键必须全扫一遍：靠前的 name 命中不得截胡靠后的 uid 命中（=  病形）。"""
    earlier = _rec("uid-other", "同名", "http://x/1", "10x Genomics")   # name 命中、排靠前
    target = _rec("uid-want", "同名", "http://x/2", "10x Genomics")     # uid 命中、排靠后
    record, ambiguous = locate_record([earlier, target], uid="uid-want", name="同名", source="10x Genomics")
    assert record is target and ambiguous == []


def test_locate_url_pass_beats_earlier_name_match():
    earlier = _rec("uid-1", "同名", "http://x/1", "10x Genomics")
    target = _rec("uid-2", "同名", "http://x/want", "10x Genomics")
    record, ambiguous = locate_record([earlier, target], url="http://x/want", name="同名")
    assert record is target and ambiguous == []


def test_locate_name_only_duplicate_is_ambiguous_never_first_silent():
    """name 命中多条、无 source 可消歧 → 如实报歧义（候选带 uid），绝不静默任取第一条。"""
    r1 = _rec("uid-1", "同名", "http://x/1", "10x Genomics")
    r2 = _rec("uid-2", "同名", "http://x/2", "10x Genomics")
    record, ambiguous = locate_record([r1, r2], name="同名")
    assert record is None
    assert {c["dataset_uid"] for c in ambiguous} == {"uid-1", "uid-2"}


def test_locate_name_with_same_source_stays_ambiguous():
    """source 相同（同名同库）消不了歧 → 仍报歧义。"""
    r1 = _rec("uid-1", "同名", "http://x/1", "10x Genomics")
    r2 = _rec("uid-2", "同名", "http://x/2", "10x Genomics")
    record, ambiguous = locate_record([r1, r2], name="同名", source="10x Genomics")
    assert record is None and len(ambiguous) == 2


def test_locate_name_source_disambiguates_across_sources():
    r1 = _rec("uid-1", "同名", "http://x/1", "10x Genomics")
    r2 = _rec("uid-2", "同名", "http://x/2", "CELLxGENE Discover")
    record, ambiguous = locate_record([r1, r2], name="同名", source="CELLxGENE Discover")
    assert record is r2 and ambiguous == []


def test_locate_name_source_contradiction_is_not_found_not_silent_fallback():
    """source 把 name 命中滤空 → 查不到；不静默忽略调用方给的 source 去返回别来源的记录。"""
    r1 = _rec("uid-1", "同名", "http://x/1", "10x Genomics")
    record, ambiguous = locate_record([r1], name="同名", source="ENCODE")
    assert record is None and ambiguous == []


def test_locate_uid_miss_falls_through_to_name():
    """uid 给了但查不到 → 退化下一档（url > name），与「优先级」语义一致。"""
    r1 = _rec("uid-1", "独名", "http://x/1", "10x Genomics")
    record, ambiguous = locate_record([r1], uid="uid-不存在", name="独名")
    assert record is r1 and ambiguous == []


# ---------- API 级（真实语料同名两条 = 天然夹具） ----------

def _same_name_pair():
    records = client.get("/api/datasets").json()["records"]
    by_uid = {r["dataset_uid"]: r for r in records}
    if UID_A not in by_uid or UID_B not in by_uid:
        # 夹具缺席必须**炸出来**而不是静默 skip——否则 API/MCP 级回归门蒸发且无人察觉。
        # 语料更新掉了这对同名记录时，请换一对同名夹具并更新断言。
        missing = [u for u in (UID_A, UID_B) if u not in by_uid]
        pytest.fail(f" 同名夹具已不在语料里（缺 {missing}），本文件 6 条 API/MCP 级断言失去载体——"
                    "请从语料里另选一对同名同来源记录替换 UID_A/UID_B。")
    a, b = by_uid[UID_A], by_uid[UID_B]
    if a["dataset_name"] != b["dataset_name"] or a["source"] != b["source"]:
        pytest.fail(f"同名夹具漂移：{UID_A} 与 {UID_B} 的名称/来源已不一致"
                    f"（{a['dataset_name']!r}/{a['source']!r} vs {b['dataset_name']!r}/{b['source']!r}），"
                    "请换一对同名同来源记录。")
    return a, b


def test_api_introduction_full_params_belong_to_uid_record():
    """前端实际请求形态（uid+url+name+source 全参）：介绍必须属于 uid 那条，不得混入同名另一条。"""
    a, _b = _same_name_pair()
    full = {"uid": a["dataset_uid"], "url": a["url"], "name": a["dataset_name"], "source": a["source"]}
    i_full = client.get("/api/introduction", params=full).json()
    i_uid = client.get("/api/introduction", params={"uid": a["dataset_uid"]}).json()
    assert i_full == i_uid
    blob = json.dumps(i_full, ensure_ascii=False).replace(",", "")
    assert "583104" in blob          # 本卡（· 583104 Spots）
    assert "38993" not in blob       # 同名另一条（· 38993 Spots）不得混入
    assert "2025-07-24" not in json.dumps(i_full, ensure_ascii=False)


def test_api_fair_full_params_belong_to_uid_record():
    a, _b = _same_name_pair()
    full = {"uid": a["dataset_uid"], "url": a["url"], "name": a["dataset_name"], "source": a["source"]}
    f_full = client.get("/api/fair", params=full).json()
    blob = json.dumps(f_full, ensure_ascii=False).replace(",", "")
    assert "583104" in blob and "38993" not in blob
    assert "2025-02-20" in json.dumps(f_full) and "2025-07-24" not in json.dumps(f_full)


def test_api_introduction_name_only_is_409_with_candidates():
    a, _b = _same_name_pair()
    r = client.get("/api/introduction", params={"name": a["dataset_name"]})
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert detail["error"] == "ambiguous_name"
    assert {c["dataset_uid"] for c in detail["candidates"]} == {UID_A, UID_B}


def test_api_fair_name_and_same_source_is_409():
    a, _b = _same_name_pair()
    r = client.get("/api/fair", params={"name": a["dataset_name"], "source": a["source"]})
    assert r.status_code == 409 and r.json()["detail"]["error"] == "ambiguous_name"


def test_mcp_introduction_and_fair_same_disambiguation():
    """项目惯例「同一 bug Web 修好 MCP 依旧」的机械门：MCP 两工具走同一定位真源。"""
    from dataset_recommender.app import mcp_server as M
    from mcp.server.fastmcp.exceptions import ToolError

    a, _b = _same_name_pair()
    out = M.get_dataset_introduction(uid=UID_A, url=a["url"], name=a["dataset_name"], source=a["source"])
    assert out["matched"]["dataset_uid"] == UID_A
    fair = M.assess_dataset_fair(uid=UID_A, name=a["dataset_name"], source=a["source"])
    assert fair["matched"]["dataset_uid"] == UID_A
    with pytest.raises(ToolError, match="ambiguous_name"):
        M.get_dataset_introduction(name=a["dataset_name"])
    with pytest.raises(ToolError, match="ambiguous_name"):
        M.assess_dataset_fair(name=a["dataset_name"], source=a["source"])


# ---------- 裸标识符 fail-closed，绝不退化成全库检索 ----------

def test_parse_bare_identifier_abstains_identifier_direct():
    doi = parse_query("10.1038/s41597-022-01234-5")
    assert doi.parse_status == "abstained" and doi.abstain_reason == "identifier_direct"
    gse = parse_query("GSE180878")
    assert gse.parse_status == "abstained"      # 与 DOI 同一条诚实通道（reason 不锁死，锁行为）
    # 普通检索句不受影响
    normal = parse_query("推荐有 FASTQ 的人类乳腺癌数据")
    assert normal.parse_status == "executable" and normal.constraints
    # 句中**提到**编号的混合诉求不是「裸贴」，不走 identifier_direct
    embedded = parse_query("把 E-MTAB-1234 打包")
    assert embedded.abstain_reason != "identifier_direct"


def test_recommend_bare_doi_miss_is_honest_fail_closed_not_full_corpus():
    """ 病形：裸贴库里没有的 DOI 曾返回全库 5712 条「满足基本检索条件」。"""
    r = client.post("/api/recommend", json={
        "query": "10.1038/s41597-022-01234-5", "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained"
    assert r["results"] == []                    # 绝不拿全库冒充结果
    look = r["identifier_lookup"]
    assert look and look["kind"] == "doi" and look["indexed"] is True
    assert look["match"] is None and "未匹配" in look["message"]


def test_recommend_bare_doi_hit_is_direct_lookup_not_full_corpus():
    """库里有的 DOI：直达该数据集（identifier_lookup.match），同样不倒全库噪音。"""
    s = get_settings()
    by_doi = {}
    for rec in load_full_corpus(s.data_dir, s.project_root):
        d = str((rec.raw or {}).get("collection_doi") or "").strip()
        if d:
            by_doi.setdefault(d.casefold(), []).append(d)
    # 必须挑**独占** DOI：共享 DOI 如今如实列候选（match=None），那是如实列候选的新口径，不是直达。
    doi = next((rs[0] for rs in by_doi.values() if len(rs) == 1), "")
    if not doi:
        pytest.fail("语料里没有独占一条的 collection_doi 可测——夹具漂移，换一条独占 DOI 或补夹具")
    r = client.post("/api/recommend", json={
        "query": doi, "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained" and r["results"] == []
    assert r["identifier_lookup"]["match"] and r["identifier_lookup"]["match"]["dataset_uid"]


def test_recommend_bare_gse_same_honest_channel():
    """对照组：GSE 编号原有弃权通道不被改动破坏（GSE 已入库 indexed=True，
    不在 60 条试点切片里的 GSE 如实「未匹配」，依旧不倒全库噪音）。"""
    r = client.post("/api/recommend", json={
        "query": "GSE180878", "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["resolution_status"] == "abstained" and r["results"] == []
    assert r["identifier_lookup"]["indexed"] is True and r["identifier_lookup"]["match"] is None
    assert "未匹配" in r["identifier_lookup"]["message"]
