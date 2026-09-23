# -*- coding: utf-8 -*-
"""SCEA tissue/disease 回填（design 端点取样）的行为与诚实性守卫。

背景（真缺陷， 修复）：`ingest_ebi_scea.py` 原先把「汇总接口不给因子取值」当成「拿不到」，
384 条记录 tissue/disease 恒空 → 带组织/疾病硬约束的查询把它们**静默排除**（`E-ANND-1` = Human Lung
Cell Atlas 搜「人类肺组织」搜不到，而源库明写 organism part = "lower lobe of left lung"）。

本测试全部**离线**（monkeypatch `_http_get`）：质量门设网络 tripwire，测试绝不可真联网。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import ingest_ebi_scea as ing  # noqa: E402

SNAPSHOT = ROOT / "database" / "external" / "ebi_scea.json"

T_COL = ing.TISSUE_COL
D_COL = ing.DISEASE_COL


def _tsv(header_cols, rows):
    """构造带引号的 TSV 字节（与 SCEA 真实 design 文件同形状）。"""
    def line(vals):
        return "\t".join('"%s"' % v if v else "" for v in vals)
    out = [line(header_cols)] + [line(r) for r in rows]
    return ("\n".join(out) + "\n").encode("utf-8")


# ----------------------------------------------------------------- 纯解析层


def test_parse_tsv_line_handles_quotes_and_empty_fields():
    row = ing._parse_tsv_line('"a"\t\t"c d"')
    assert row == ["a", "", "c d"]


def test_complete_rows_drops_partial_head_and_tail():
    """Range 片段两端必然是残行：非首片丢首行，任何片丢末行——否则会把半个值当成取值。"""
    blob = b"XXpartial\nfull1\nfull2\ntrailing-partial"
    assert ing._complete_rows(blob, is_first=False) == ["full1", "full2"]
    # 首片的第 0 行是表头（调用方另取），这里只验不丢首行
    assert ing._complete_rows(blob, is_first=True) == ["XXpartial", "full1", "full2"]


def test_complete_rows_normalizes_crlf():
    blob = b"head\r\nfull1\r\nfull2\r\ntail"
    assert ing._complete_rows(blob, is_first=False) == ["full1", "full2"]


# ------------------------------------------------------- fetch_design_facets


def _install_fake(monkeypatch, *, total, chunk_for):
    """chunk_for(start) -> bytes；返回 206 + Content-Range 以模拟真实 Range 行为。"""
    calls = []

    def fake(url, byte_range=None, tries=3):
        if byte_range is None:
            calls.append(("full", None))
            return 200, {"Content-Length": str(total)}, chunk_for(None)
        start, end = byte_range
        calls.append(("range", start))
        return 206, {"Content-Range": "bytes %d-%d/%d" % (start, end, total)}, chunk_for(start)

    monkeypatch.setattr(ing, "_http_get", fake)
    return calls


def test_small_file_is_read_whole_and_marked_complete(monkeypatch):
    """≤FULL_READ_LIMIT → 整读 → complete=True（取值穷尽，可据此声称完整）。"""
    blob = _tsv([T_COL, D_COL], [["kidney", "renal cell carcinoma"]])
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)

    tis, dis, prov = ing.fetch_design_facets("E-CURD-10")
    assert tis == ["kidney"]
    assert dis == ["renal cell carcinoma"]
    assert prov["complete"] is True
    assert prov["origin"] == "declared"


def test_large_file_samples_multiple_offsets_and_is_marked_incomplete(monkeypatch):
    """>FULL_READ_LIMIT → 多点取样，且取样点必须**跨全文分布**。

    这里断言的是**分布性质**，不是 `SAMPLE_POINTS` 常量本身——验证证明旧写法
    （`len(offsets) == 1 + len(ing.SAMPLE_POINTS)`）是同义反复：把 SAMPLE_POINTS 变异成
    `(0.9,)` 或全挤在末 5%（即本测试自称要防的「排序偏差」最坏形态），16 项测试**依旧全绿**。
    """
    total = 400 * 1024 * 1024
    head = _tsv([T_COL, D_COL], [["lower lobe of left lung", "normal"]])

    def chunk_for(start):
        if start == 0:
            return head
        # 非首片：前后各加残行，模拟 Range 切断
        return b'PARTIAL\n"trachea"\t"cancer"\nPARTIAL-TAIL'

    calls = _install_fake(monkeypatch, total=total, chunk_for=chunk_for)
    tis, dis, prov = ing.fetch_design_facets("E-ANND-1")

    offsets = [s for kind, s in calls if kind == "range"]
    assert offsets[0] == 0, "第一片必须是表头"
    assert len(set(offsets)) == len(offsets), "取样点不得重复（否则覆盖率是假的）"

    # **分布**断言：把文件四等分，除表头所在的第一份外，后三份每份至少一个取样点。
    # 写死预期行为，不引用实现常量 → 把取样点挪到一处/砍成一个都会红。
    buckets = {i: 0 for i in range(4)}
    for off in offsets:
        buckets[min(3, int(4 * off / total))] += 1
    assert all(buckets[i] >= 1 for i in (1, 2, 3)), (
        f"取样点未跨全文分布（四分桶命中={buckets}）——只读头部会被 design 的样本聚簇排序骗")

    assert "lower lobe of left lung" in tis and "trachea" in tis
    assert prov["complete"] is False, "取样未整读 → 绝不可标 complete"
    assert prov["sampled_bytes"] < prov["total_bytes"]


def test_only_mid_file_value_is_still_collected(monkeypatch):
    """真行为断言：某取值**只**出现在文件中段 → 仍必须被采到。
    这是「跨全文取样」的**因果**证明；任何把取样点挪走/砍掉的改动都会让它红
    （旧断言全是对着实现常量写的，砍到只剩 (0.9,) 也不红）。"""
    total = 400 * 1024 * 1024
    mid_lo, mid_hi = int(total * 0.25), int(total * 0.55)

    def chunk_for(start):
        if start == 0:
            return _tsv([T_COL, D_COL], [["lung", "normal"]])
        if mid_lo <= start <= mid_hi:
            return b'PARTIAL\n"MIDDLE-ONLY-TISSUE"\t"normal"\nTAIL'
        return b'PARTIAL\n"lung"\t"normal"\nTAIL'

    _install_fake(monkeypatch, total=total, chunk_for=chunk_for)
    tis, _dis, _prov = ing.fetch_design_facets("X")
    assert "MIDDLE-ONLY-TISSUE" in tis, "只在中段出现的取值没被采到 → 取样未覆盖中段"


def test_server_ignoring_range_never_yields_impossible_coverage(monkeypatch):
    """Range 是建议性的（RFC 9110）。服务端改回 200 整发时，绝不可按切片语义继续：
    验证集成复现——4.8MB 文件被拉 7 遍共 33.6MB，provenance 变成
    sampled(33.6MB) > total(4.8MB) = 700% 覆盖率 + rows_parsed 7 倍虚报，且 complete=False
    掩盖了「其实已整读」。换算到 E-ANND-1 是 3.1GB 下载 + 极可能 MemoryError。"""
    # 真实的「忽略 Range」＝服务端把**全文**发来（而不是切片）。故 body 长度必须等于 total，
    # 并把 FULL_READ_LIMIT 压到极小，让这份小文件走「本该取样」的那条分支。
    whole = _tsv([T_COL, D_COL], [["spleen", "normal"], ["heart", "cancer"]])
    total = len(whole)
    monkeypatch.setattr(ing, "FULL_READ_LIMIT", 8)   # total > 8 → 本该走多点取样
    calls = []

    def fake(url, byte_range=None, tries=3):
        calls.append(byte_range)
        return 200, {"Content-Length": str(total)}, whole   # 一律 200 + 完整实体

    monkeypatch.setattr(ing, "_http_get", fake)
    tis, dis, prov = ing.fetch_design_facets("E-ANND-1")

    assert len(calls) == 1, f"服务端已给全文，不得再逐点重拉（实际发了 {len(calls)} 个请求）"
    assert prov["sampled_bytes"] <= prov["total_bytes"], "覆盖率不可能 >100%"
    assert prov["complete"] is True, "已拿到全文 → 取值其实穷尽，不该谎称可能不全"
    assert tis == ["spleen", "heart"]


def test_sampled_bytes_never_exceeds_total(monkeypatch):
    """兜底不变量：任何路径下 sampled_bytes 都不得超过 total_bytes；宁可抛错也不发布假覆盖率。
    构造一个「声称 206、却每片都回超量数据」的异常服务端（片段仍带合法表头，确保不会先被
    no_header 早退掉，否则这条断言就成了空跑）。"""
    chunk = _tsv([T_COL, D_COL], [["spleen", "normal"]])
    total = len(chunk) + 10          # 每片就已接近全长 → 累计几片必然超
    monkeypatch.setattr(ing, "FULL_READ_LIMIT", 8)

    def fake(url, byte_range=None, tries=3):
        return 206, {"Content-Range": "bytes 0-99/%d" % total}, chunk

    monkeypatch.setattr(ing, "_http_get", fake)
    with pytest.raises(RuntimeError, match="Range 语义被破坏"):
        ing.fetch_design_facets("X")


def test_values_are_deduplicated_case_insensitively(monkeypatch):
    blob = _tsv([T_COL, D_COL], [["Lung", "normal"], ["lung", "Normal"], ["lung", "normal"]])
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)
    tis, dis, _ = ing.fetch_design_facets("X")
    assert tis == ["Lung"] and dis == ["normal"], "同值不同大小写必须归并成一项"


def test_sampling_site_is_never_merged_into_tissue(monkeypatch):
    """语义守卫：organism part=kidney / sampling site=neoplasm（E-CURD-10 真实形状）。
    并入会把「肿瘤」写成组织＝比缺失更糟的假事实。"""
    blob = _tsv([T_COL, "Sample Characteristic[sampling site]", D_COL],
                [["kidney", "neoplasm", "renal cell carcinoma"]])
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)
    tis, _dis, _ = ing.fetch_design_facets("E-CURD-10")
    assert tis == ["kidney"]
    assert "neoplasm" not in tis


def test_na_placeholders_are_never_written_as_values(monkeypatch):
    """SCEA 真的用 NA 占位填 organism part（E-MTAB-4617 → `spleen, not applicable, heart, testes`）。
    必须复用共享 `cxg._is_informative` 过滤，不能只判「非空」——否则 "not applicable" 会变成一个
    可被匹配、可进分面的假组织。"""
    blob = _tsv([T_COL, D_COL], [
        ["spleen", "normal"],
        ["not applicable", "N/A"],
        ["heart", "unknown"],
        ["  ", "none"],
    ])
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)
    tis, dis, _ = ing.fetch_design_facets("E-MTAB-4617")
    assert tis == ["spleen", "heart"]
    assert dis == ["normal"]
    for bad in ("not applicable", "n/a", "unknown", "none"):
        assert bad not in [t.lower() for t in tis]
        assert bad not in [d.lower() for d in dis]


def test_missing_target_columns_yield_empty_without_guessing(monkeypatch):
    """该实验没标这两维 → 如实留空，绝不猜（不得回退成从标题抽词）。"""
    blob = _tsv(["Sample Characteristic[organism]"], [["Homo sapiens"]])
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)
    tis, dis, prov = ing.fetch_design_facets("X")
    assert tis == [] and dis == []
    assert prov["rows_parsed"] == 0


# --------------------------------------------------------------- to_record

_EXP = {"experimentAccession": "E-TEST-1", "experimentDescription": "T",
        "species": "Homo sapiens", "technologyType": ["10xv3"], "numberOfAssays": 10,
        "loadDate": "04-09-2024", "experimentType": "Baseline", "experimentalFactors": []}


def test_no_enrich_leaves_fields_empty_and_says_so():
    """--no-enrich → 字段空 + origin=skipped。空字段必须能与「取过但源库没标」区分。"""
    rec = ing.to_record(dict(_EXP), enrich=False)
    assert rec["tissue"] == "" and rec["disease"] == ""
    assert rec["metadata_provenance"]["origin"] == "skipped"


def test_enrich_failure_never_masquerades_as_source_having_no_data(monkeypatch):
    """取样抛错 → 字段留空但 provenance 记 error。绝不把「没取到」伪装成「源库没有」——
    那正是本次修复的老毛病。"""
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ing, "fetch_design_facets", boom)
    rec = ing.to_record(dict(_EXP), enrich=True)
    assert rec["tissue"] == ""
    assert rec["metadata_provenance"]["error"] == "RuntimeError"
    assert rec["metadata_provenance"]["origin"] != "skipped"


def test_multi_value_join_reuses_shared_convention(monkeypatch):
    """多值拼接必须复用 cxg._clean_join（与 CELLxGENE/HCA/ArrayExpress 三库同一约定），
    不得另立格式——否则分面面板与硬过滤口径分裂。
    本路径整读（complete=True，截断的是完整策展集）→ cap=8 约定维持不变。"""
    many = [["t%d" % i, ""] for i in range(12)]
    blob = _tsv([T_COL, D_COL], many)
    _install_fake(monkeypatch, total=len(blob), chunk_for=lambda s: blob)
    rec = ing.to_record(dict(_EXP), enrich=True)
    assert rec["metadata_provenance"]["complete"] is True
    assert "等12项" in rec["tissue"], "complete=True 记录超 8 项须维持「等N项」截断"


def test_sampled_record_keeps_all_retrieved_values_without_truncation(monkeypatch):
    """scea-sampled-cap8-truncation 修正在案，验收条件①：complete=False 的记录，cap=8 会把
    已付网络代价取回的取值再丢掉（实测 E-ANND-1 抽样拿到 12 个肺区取值只写 8 个 + 「等12项」）——
    抽样记录必须保留全部已取回取值，不得截断。complete=True 记录维持 cap=8 见上一条。"""
    total = 400 * 1024 * 1024
    head = _tsv([T_COL, D_COL], [["t00", "normal"]])
    seen_chunks = {"n": 0}

    def chunk_for(start):
        if start == 0:
            return head
        seen_chunks["n"] += 1
        a = "t%02d" % (seen_chunks["n"] * 2 - 1)
        b = "t%02d" % (seen_chunks["n"] * 2)
        return ('PARTIAL\n"%s"\t"normal"\n"%s"\t"normal"\nTAIL' % (a, b)).encode("utf-8")

    _install_fake(monkeypatch, total=total, chunk_for=chunk_for)
    rec = ing.to_record(dict(_EXP), enrich=True)

    assert rec["metadata_provenance"]["complete"] is False, "大文件多点取样 → 不得标 complete"
    assert "等" not in rec["tissue"], "complete=False 记录不得出现「等N项」截断尾巴"
    parts = [p.strip() for p in rec["tissue"].split(",")]
    expected = {"t00"} | {"t%02d" % i for i in range(1, 2 * len(ing.SAMPLE_POINTS) + 1)}
    assert set(parts) == expected, "抽样取回的全部取值必须保留"
    assert len(parts) > 8, "本测试必须构造超 8 项取值才能证明未截断"


# ------------------------------------------------- 真快照回归 + 隔离守卫


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_tissue_is_no_longer_universally_empty():
    """回归门：修复前 tissue 填充 0/384。若再次归零＝适配器又漏读了 design 端点。"""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    recs = data["records"]
    filled = sum(1 for r in recs if (r.get("tissue") or "").strip())
    assert filled > 0.9 * len(recs), f"tissue 填充率过低: {filled}/{len(recs)}"


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_lung_atlas_is_findable_by_lung():
    """标志案例：`E-ANND-1` = Human Lung Cell Atlas。修复前 tissue='' → 搜「肺」被硬过滤无声排除。
    retriever 的 tissue 判定是子串包含（`_field_contains`），故拼接串含 'lung' 即可命中。"""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    hit = [r for r in data["records"] if r["dataset_uid"] == "ebi:E-ANND-1"]
    assert hit, "快照缺少 E-ANND-1"
    assert "lung" in (hit[0]["tissue"] or "").lower()


def _dirty_parts(path):
    """扫一份快照里所有非 informative 的 tissue/disease 取值。
    `_clean_join` 的截断尾巴形如 `bronchus 等12项` → `_norm_token` 归一成 `bronchus`，仍判 informative，
    故无假阳性。"""
    import ingest_cellxgene as cxg
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for r in data.get("records", []):
        for dim in ("tissue", "disease"):
            for part in (r.get(dim) or "").split(","):
                part = part.strip()
                if part and not cxg._is_informative(part):
                    out.append((r.get("dataset_uid"), dim, part))
    return out


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_has_no_na_placeholders():
    """**产物**必须与过滤器口径一致，不只是**函数**能过滤。

    验证 ：曾有一版快照带 7 处 NA 占位（`ebi:E-MTAB-4617` → `spleen, not applicable, heart,
    testes`），因为它生成于过滤器接入之前——而当时 `pytest` 16 passed 全绿，脏值就躺在被断言的同一
    个文件里。只 monkeypatch 测函数的测试**结构上**抓不到「代码与数据脱节」，这条才抓得到。
    脏值会一路污染到卡片 tag、分面侧栏可点项、浏览页自由词检索。
    """
    dirty = _dirty_parts(SNAPSHOT)
    assert not dirty, f"快照含 NA 占位（产物早于过滤器？请重跑 ingest_ebi_scea.py）: {dirty[:5]}"


@pytest.mark.parametrize("name", ["cellxgene.json", "hca.json", "arrayexpress.json"])
def test_other_external_snapshots_also_have_no_na_placeholders(name):
    """把同一条口径推广成**跨四库**守卫——口径统一批自己点名的
    缺口就是「当时没有任何测试覆盖跨源口径」。三库实测现已 0 违规，可直接纳入而无需重抓。"""
    p = SNAPSHOT.parent / name
    if not p.exists():
        pytest.skip(f"{name} 未生成")
    dirty = _dirty_parts(p)
    assert not dirty, f"{name} 含 NA 占位: {dirty[:5]}"


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_every_record_declares_provenance_honestly():
    """诚实层：每条都必须带 provenance；非整读的绝不可标 complete=True。"""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    for r in data["records"]:
        p = r.get("metadata_provenance")
        assert isinstance(p, dict), f"{r['dataset_uid']} 缺 provenance"
        assert "complete" in p
        if p.get("complete") is True:
            assert p.get("sampled_bytes") >= p.get("total_bytes", 0), (
                f"{r['dataset_uid']} 标了 complete 但未整读——诚实层不得撒谎")


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_sampled_records_have_no_truncation_tail():
    """验收条件①（产物级）：complete=False 的 SCEA 记录 tissue/disease 不得出现「等N项」
    截断尾巴。只测函数 monkeypatch 抓不到「代码改了但快照没重抓」的脱节（见本文件 NA 守卫的教训）。"""
    import re
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    bad = []
    for r in data["records"]:
        p = r.get("metadata_provenance") or {}
        if p.get("complete") is False:
            for dim in ("tissue", "disease"):
                if re.search(r"等\d+项", r.get(dim) or ""):
                    bad.append((r["dataset_uid"], dim))
    assert not bad, f"抽样记录仍被 cap=8 截断（快照未按新口径重抓？）: {bad}"


@pytest.mark.skipif(not SNAPSHOT.exists(), reason="外部快照未生成")
def test_real_snapshot_complete_records_truncation_unchanged():
    """验收条件②（产物级）：complete=True 记录维持 cap=8 逐位不变——快照里曾截断的
    complete=True 记录（E-GEOD-71585，等12项）必须仍然带截断尾巴，证明共享约定没被顺手放宽。"""
    data = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    hit = [r for r in data["records"] if r["dataset_uid"] == "ebi:E-GEOD-71585"]
    assert hit, "快照缺少 ebi:E-GEOD-71585（complete=True 截断对照样本）"
    p = hit[0].get("metadata_provenance") or {}
    assert p.get("complete") is True
    assert "等12项" in (hit[0]["tissue"] or ""), "complete=True 记录的 cap=8 截断必须逐位维持"


def test_snapshot_never_written_into_frozen_base():
    """冻结隔离（结构性）：产物只能落 database/external/，绝不进 database/base/。"""
    assert "external" in ing.OUT_PATH.parts
    assert "base" not in ing.OUT_PATH.parts
