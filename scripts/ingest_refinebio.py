# -*- coding: utf-8 -*-
"""把 refine.bio（GEO/SRA/ArrayExpress 的统一加工镜像）中经三道闸甄别的单细胞/空间组学
实验拉取为一份**离线快照**，写入 `database/external/refinebio.json`（外部平台库，opt-in，
第 11 个来源）。

为什么是「服务端召回 + 本地三道闸」而不是全量或裸查询全收：
- refine.bio 有 6.25 万实验 / 204 万样本（实测 62,518），全量会淹没 7,712 条的
  现有语料；且它是 GEO/SRA/AE 的**镜像**——与库中 GEO（60 条）/ArrayExpress（1,784 条）/
  SCEA（384 条）存在实体级重复风险（本项目跨源去重最高风险点）。
- `/v1/search/` 的 `search=` 是 ElasticSearch **模糊 OR 匹配**（实测 "spatial transcriptomics"
  命中 19,388 条、"single cell transcriptomics" 23,382 条），服务端只起召回作用；
  真正的主题判定在本地内容级闸（逐条理由留档）。

切片定位（curated slice）：单细胞/单核/空间组学 + 人/小鼠 + 有可下载处理数据
（num_downloadable_samples>0——统一 Salmon 定量是 refine.bio 的核心价值，加工不了的条目
只是 GEO 镜像，不增量）。目标量级几百条：量级由数据说话，见 mapping.md 的漏斗计数。

三道闸（对齐 zenodo 先例）：
  1. API 侧过滤：technology=rna-seq + organism=HOMO_SAPIENS/MUS_MUSCULUS +
     num_downloadable_samples__gt=0，每组查询 relevance top 200；
  2. 内容级复核：标题+描述必须命中单细胞/空间信号词（机检 + 逐条理由留档，
     ATAC-only/甲基化/Hi-C 等非转录组剔除）；本闸是「机器初筛 + 规则可复跑」，
     每条剔除理由写进 staging candidates_review.tsv；
  3. 跨源去重：按 accession（GSE/SRP/ERP/DRP/PRJ*/E-MTAB/E-GEOD 等）对其余 external
     文件 + base 建索引，已在库的跳过并计数；再叠 `_record_identity_keys` 实体级去重兜底。

设计约束（与 ingest_zenodo.py 一致）：
- **不动 `database/base/`**：官方评测冻结在 774 条 base 语料；外部库默认关
  （sources=None 只装基础语料）→ 确定性二元门不受影响。
- **运行时不联网**：一次性抓快照落地，查询期只读本地 JSON。
- **不绕过管护管线**：抓取走 `corpus_curation` 的 refine.bio 适配器原语
  （`_fetch_logged` 账本 + ≤60 req/min 限速、`_refinebio_core_ok`/`_refinebio_validated_results`
  形状闸 fail-closed、`_refinebio_to_record` 字段映射、`_refinebio_apply_sample_annotations`
  样本页富化）。
- 原始响应摘要与审计留在仓库外的历史 staging 目录（历史管线产物，不随本仓分发），
  不进 external/。

用法：
    python scripts/ingest_refinebio.py            # 抓候选 → 三道闸 → database/external/refinebio.json
    python scripts/ingest_refinebio.py --dry-run  # 只抓取并打印复核漏斗，不写任何文件
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402

OUT_PATH = REPO_ROOT / "database" / "external" / "refinebio.json"
STAGING_DIR = REPO_ROOT / "research" / "staging" / "refinebio"
RAW_DIR = STAGING_DIR / "raw"
REVIEW_TSV = STAGING_DIR / "candidates_review.tsv"

#: 发现候选用的查询配方（与 zenodo 首批同主题，便于两源对照）。服务端过滤：
#: technology=rna-seq + organism=HOMO_SAPIENS/MUS_MUSCULUS + num_downloadable_samples__gt=0，
#: 每组 relevance top DISCOVERY_TOP_N。只起召回作用——主题判定在内容级闸。
DISCOVERY_QUERIES: tuple[str, ...] = (
    "single-cell RNA-seq",
    "scRNA-seq",
    "single-nucleus RNA-seq",
    "spatial transcriptomics",
    "single-cell ATAC-seq",
    "Visium",
    "single cell transcriptomics",
    "snRNA-seq",
)
DISCOVERY_TOP_N = 200

#: 入库条数上限（宁少勿滥：62.5k 实验全量会淹没 7,712 条语料；切片量级由漏斗数据说话，
#: 见 mapping.md）。按发现顺序（查询组序 × 组内 relevance 序）截断。
MAX_RECORDS = 300

#: 内容级闸（闸 2）：标题+描述必须命中「单细胞/单核/空间」信号。ES 是模糊 OR 匹配，
#: 不命中即弱相关召回（如 "spatial transcriptomics" 命中一切含 transcriptomics 的 bulk）。
_INCLUDE_RE = re.compile(
    r"single[- ]cell|single[- ]nuclei|single[- ]nucleus|\bsc[- ]?rna|\bsn[- ]?rna"
    r"|\bscrna|\bsnrna|spatial[- ](transcriptomics|transcriptome|omics|profiling|gene expression)"
    r"|\bvisium\b|slide[- ]?seq|merfish|seq[- ]?fish|stereo[- ]?seq|drop[- ]?seq|indrops?\b"
    r"|smart[- ]?seq|celseq|mars[- ]?seq|split[- ]?seq|sci[- ]?rna|chromium|10x genomics"
    r"|microwell|dnbc4|seekgene|cloup|单细胞|空间转录组",
    re.I,
)
#: 非转录组/非目标模态剔除（命中且不同时含转录组信号 → 剔除；随条记理由）。
_EXCLUDE_ONLY_RE = re.compile(
    r"\batac[- ]?seq|\bchip[- ]?seq|\bbisulfite|methylation|\bhi-?c\b|\bribo[- ]?seq"
    r"|\bclip[- ]?seq|\bcut&?(run|tag)|\bwgs\b|\bwes\b|whole genome|whole exome|genotyping",
    re.I,
)
_TRANSCRIPTOME_RE = re.compile(r"rna|transcriptom|基因表达|转录组", re.I)

#: 跨源去重（闸 3）用的 accession 形态（对 url/dataset_uid/public_accession 字段扫描）。
_ACC_RE = re.compile(
    r"(?:GSE|SRP|ERP|DRP|PRJNA|PRJEB|PRJDB)\d{3,}|E-(?:MTAB|GEOD|MEXP|TABM)-\d+")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _fetch_json(url: str, *, endpoint: str, query: str, cache: Path | None, use_cache: bool):
    """带缓存的 _fetch_logged 包装：raw 摘要缓存在 staging/raw/，复跑/干跑不重复联网。"""
    if use_cache and cache is not None and cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    payload = cc._fetch_logged(
        url, project_root=REPO_ROOT, endpoint=endpoint, query=query,
        min_interval=cc._REFINEBIO_MIN_INTERVAL,
    )
    if use_cache and cache is not None:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _discover(query: str, *, use_cache: bool) -> list[dict]:
    """一组发现查询：服务端过滤 + relevance top N → 已过形状闸的候选条目列表。"""
    q = urllib.parse.quote(query)
    url = (f"{cc.REFINEBIO_SEARCH_API}?search={q}&technology=rna-seq"
           f"&num_downloadable_samples__gt=0&organism=HOMO_SAPIENS&organism=MUS_MUSCULUS"
           f"&limit={DISCOVERY_TOP_N}")
    payload = _fetch_json(url, endpoint=cc.REFINEBIO_SEARCH_API, query=query,
                          cache=RAW_DIR / f"search_{_slug(query)}.json", use_cache=use_cache)
    return cc._refinebio_validated_results(payload)  # 形状闸 fail-closed


def _content_gate(hit: dict) -> str | None:
    """闸 2 内容级复核：通过 → None；剔除 → 理由（随条留档）。"""
    title = str(hit.get("title") or "")
    desc = str(hit.get("description") or "")
    text = f"{title}\n{desc}"
    if not _INCLUDE_RE.search(text):
        return "无单细胞/单核/空间信号（ES 模糊匹配的弱相关召回）"
    if _EXCLUDE_ONLY_RE.search(text) and not _TRANSCRIPTOME_RE.search(text):
        return "非转录组模态（ATAC/甲基化/Hi-C 等且无转录组信号）"
    return None


def _corpus_accession_index() -> set[str]:
    """其余 external 文件 + base 的全部 accession 键（闸 3 跨源去重；扫 url/uid/accession 字段）。"""
    index: set[str] = set()
    for folder in (REPO_ROOT / "database" / "external", REPO_ROOT / "database" / "base"):
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.json")):
            if p.name == OUT_PATH.name:
                continue
            try:
                records = cc._load_file_records(p)
            except cc.CurateError:
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                for field in ("dataset_uid", "url", "download_url", "public_accession"):
                    for m in _ACC_RE.finditer(str(record.get(field) or "")):
                        index.add(m.group(0).upper())
    return index


def _candidate_accessions(hit: dict) -> set[str]:
    """候选自身的 accession 键（主 + 副，大写）。"""
    accs = set()
    for field in ("accession_code", "alternate_accession_code"):
        v = str(hit.get(field) or "").strip().upper()
        if v:
            accs.add(v)
    return accs


def _enrich_from_samples(rec: dict, *, use_cache: bool) -> str:
    """闸后对入选候选拉 samples 端点一页（≤25 条）做 specimen_part/disease/has_raw 富化。
    返回富化状态串（留档用）；失败如实返回原因，记录保持诚实缺省。"""
    acc = rec["public_accession"]
    url = f"{cc.REFINEBIO_API}/samples/?experiment_accession_code={acc}&limit=25"
    try:
        payload = _fetch_json(url, endpoint=f"{cc.REFINEBIO_API}/samples/",
                              query=f"experiment:{acc}",
                              cache=RAW_DIR / f"samples_{acc}.json", use_cache=use_cache)
    except cc.CurateError as exc:
        return f"samples 拉取失败（{exc.hint[:60]}）→ tissue/disease 留空"
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return "samples 响应形状变了 → tissue/disease 留空"
    cc._refinebio_apply_sample_annotations(rec, results)
    got = [f for f in ("tissue", "disease", "has_raw_data") if rec.get(f) not in (None, "")]
    return f"samples 一页 {len(results)} 条 → 回填 {','.join(got) if got else '无有效值（留空）'}"


def _enrich_from_detail_annotations(rec: dict, *, use_cache: bool) -> str:
    """samples 端点不可用时的回退证据：experiment 详情的 annotations（SRA 样本键值对，
    实测每实验通常 1 条）聚合成 tissue/disease（证据等级低于 samples 聚合，provenance 如实写）。"""
    acc = rec["public_accession"]
    url = f"{cc.REFINEBIO_EXPERIMENTS_API}{acc}/"
    try:
        payload = _fetch_json(url, endpoint=cc.REFINEBIO_EXPERIMENTS_API,
                              query=f"detail:{acc}",
                              cache=RAW_DIR / f"detail_{acc}.json", use_cache=use_cache)
    except cc.CurateError as exc:
        return f"detail 拉取也失败（{exc.hint[:50]}）→ tissue/disease 留空"
    if not cc._refinebio_core_ok(payload):
        return "detail 响应形状变了 → tissue/disease 留空"
    before = (rec.get("tissue"), rec.get("disease"))
    cc._refinebio_apply_detail_annotations(rec, payload)
    after = (rec.get("tissue"), rec.get("disease"))
    return ("detail annotations 回填 tissue/disease" if after != before
            else "detail annotations 无组织/疾病键值（留空）")


def main() -> int:
    parser = argparse.ArgumentParser(description="refine.bio 单细胞/空间切片 → external 快照")
    parser.add_argument("--dry-run", action="store_true",
                        help="只抓取并打印复核漏斗，不写 refinebio.json / staging")
    parser.add_argument("--no-cache", action="store_true",
                        help="忽略 staging/raw/ 缓存，强制重新联网")
    parser.add_argument("--skip-enrich", action="store_true",
                        help="跳过 samples 端点富化（tissue/disease/has_raw 全留空，快）")
    parser.add_argument("--workers", type=int, default=2,
                        help="富化并发数（默认 2；聚合速率仍受 ≤60 req/min 全局限速锁约束）")
    args = parser.parse_args()
    use_cache = not args.no_cache

    # ---- 闸 1：服务端召回（8 组查询 × top 200，过滤已在 URL 里）----
    candidates: dict[str, dict] = {}   # accession → hit（保序去重；ES 索引实测有重复文档）
    for query in DISCOVERY_QUERIES:
        hits = _discover(query, use_cache=use_cache)
        n_new = 0
        for hit in hits:
            acc = str(hit.get("accession_code") or "").strip()
            if acc and acc not in candidates:
                candidates[acc] = hit
                n_new += 1
        print(f"发现查询 {query!r}: 服务端返回 {len(hits)} 条，新增候选 {n_new} 条")
    total = len(candidates)
    print(f"\n闸 1（API 侧过滤）后候选合计 {total} 条（8 组查询去重后）")

    # ---- 闸 2：内容级复核（逐条理由留档）----
    passed2: list[dict] = []
    dropped2: list[tuple[dict, str]] = []
    for hit in candidates.values():
        reason = _content_gate(hit)
        (dropped2.append((hit, reason)) if reason else passed2.append(hit))
    # 同题去重（归一化标题精确撞）：同一研究在 SRA 拆成多个主 accession（如 SRP059850/SRP059902
    # 同题）——与 zenodo 首批「同题重复不收」同口径，收先见的一条、其余留档。
    seen_titles: set[str] = set()
    deduped2: list[dict] = []
    for hit in passed2:
        norm_title = re.sub(r"\s+", " ", str(hit.get("title") or "").strip().lower())
        if norm_title and norm_title in seen_titles:
            dropped2.append((hit, "同题重复（同一研究多个主 accession，收先见的一条）"))
            continue
        seen_titles.add(norm_title)
        deduped2.append(hit)
    passed2 = deduped2
    print(f"闸 2（内容级复核 + 同题去重）：剔除 {len(dropped2)} 条，过 {len(passed2)} 条")

    # ---- 闸 3：跨源 accession 去重 + 实体级去重兜底 ----
    index = _corpus_accession_index()
    identity_index: set[str] = set()
    for p in sorted((REPO_ROOT / "database" / "external").glob("*.json")):
        if p.name == OUT_PATH.name:
            continue
        try:
            existing = cc._load_file_records(p)
        except cc.CurateError:
            continue
        for record in existing:
            identity_index |= cc._record_identity_keys(record)
    passed3: list[dict] = []
    dropped3: list[tuple[dict, str]] = []
    for hit in passed2:
        accs = _candidate_accessions(hit)
        dup = sorted(accs & index)
        if dup:
            dropped3.append((hit, f"跨源撞重（{'/'.join(dup)} 已在库）"))
            continue
        rec_probe = cc._refinebio_to_record(hit)
        if rec_probe and cc._record_identity_keys(rec_probe) & identity_index:
            dropped3.append((hit, "实体级撞重（同 url/同来源同 uid 已在库）"))
            continue
        passed3.append(hit)
    print(f"闸 3（跨源去重）：剔除 {len(dropped3)} 条，过 {len(passed3)} 条")

    # ---- 截断 + 映射 + 富化 ----
    selected = passed3[:MAX_RECORDS]
    overflow = len(passed3) - len(selected)
    if overflow:
        print(f"超上限 {MAX_RECORDS}：{overflow} 条合格候选未收（宁少勿滥，理由留档）")
    records: list[dict] = []
    for hit in selected:
        rec = cc._refinebio_to_record(hit)
        records.append(rec)
    enrich_notes: dict[str, str] = {}
    if not args.skip_enrich and not args.dry_run:
        # 富化链：samples 端点（优选证据，但实测很慢 10-30s/页且会网关超时）→ 详情 annotations
        # 回退（快，证据较弱）。samples 断路器：第一次真失败即判端点不可用，其余直接走回退，
        # 不在 504 端点上烧 3 次重试（验证并发触发持续 504 的教训）。
        from concurrent.futures import ThreadPoolExecutor

        state = {"samples_down": False}

        def _one(rec):
            acc = rec["public_accession"]
            cached = RAW_DIR / f"samples_{acc}.json"
            if use_cache and cached.is_file():
                return acc, _enrich_from_samples(rec, use_cache=True)
            if state["samples_down"]:
                return acc, _enrich_from_detail_annotations(rec, use_cache=use_cache)
            note = _enrich_from_samples(rec, use_cache=use_cache)
            if "拉取失败" in note:
                state["samples_down"] = True
                note = note + "；" + _enrich_from_detail_annotations(rec, use_cache=use_cache)
            return acc, note

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for idx, (acc, note) in enumerate(pool.map(_one, records), 1):
                enrich_notes[acc] = note
                if idx % 10 == 0 or idx == len(records):
                    print(f"  富化进度 {idx}/{len(records)}")
        for i, rec in enumerate(records, 1):
            print(f"[{i:>3}/{len(records)}] {rec['public_accession']}: "
                  f"{rec['dataset_name'][:60]} | {enrich_notes.get(rec['public_accession'], '')}")
    else:
        for i, rec in enumerate(records, 1):
            print(f"[{i:>3}/{len(records)}] {rec['public_accession']}: {rec['dataset_name'][:70]}")

    print(f"\n漏斗：服务端召回 {total} → 内容闸过 {len(passed2)} → 去重后合格 {len(passed3)} "
          f"→ 拟入库 {len(records)} 条")

    if args.dry_run:
        print("\n--dry-run：不写任何文件。剔除样本（每类前 5 条）：")
        for hit, why in dropped2[:5]:
            print(f"  [闸2] {hit.get('accession_code')}: {why} | {str(hit.get('title'))[:60]}")
        for hit, why in dropped3[:5]:
            print(f"  [闸3] {hit.get('accession_code')}: {why} | {str(hit.get('title'))[:60]}")
        return 0

    # ---- 留档：逐条复核表 ----
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    with REVIEW_TSV.open("w", encoding="utf-8", newline="") as fh:
        fh.write("accession\ttitle\tdecision\treason\n")
        for hit, why in dropped2:
            fh.write(f"{hit.get('accession_code')}\t{str(hit.get('title') or '')[:120]}\t剔除-闸2\t{why}\n")
        for hit, why in dropped3:
            fh.write(f"{hit.get('accession_code')}\t{str(hit.get('title') or '')[:120]}\t剔除-闸3\t{why}\n")
        for hit in passed3[MAX_RECORDS:]:
            fh.write(f"{hit.get('accession_code')}\t{str(hit.get('title') or '')[:120]}\t"
                     f"未收-超上限\t合格但超 MAX_RECORDS={MAX_RECORDS}（宁少勿滥）\n")
        for rec in records:
            fh.write(f"{rec['public_accession']}\t{rec['dataset_name'][:120]}\t入库\t"
                     f"{enrich_notes.get(rec['public_accession'], '未富化')}\n")

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    n_samples_enriched = sum(1 for n in enrich_notes.values() if n.startswith("samples 一页"))
    n_detail_enriched = sum(1 for n in enrich_notes.values() if "detail annotations 回填" in n)
    enrich_note = (
        f"tissue/disease 富化：samples 端点单页聚合 {n_samples_enriched} 条"
        f"（含 has_raw_data 正面证据）；详情 annotations 回退 {n_detail_enriched} 条"
        f"（证据较弱，provenance 逐条区分）；其余留空（诚实缺省）。"
    ) if enrich_notes else "未做富化（--skip-enrich/--dry-run）：tissue/disease/has_raw_data 全留空。"
    payload = {
        "source": "refine.bio",
        "source_url": "https://www.refine.bio/",
        "note": (
            "离线快照·外部平台库候选（opt-in），不并入官方评测语料。"
            f"首批 {len(records)} 条（{snapshot_date} 采集）：refine.bio 是 GEO/SRA/ArrayExpress 的"
            "统一加工镜像（Salmon 定量），本切片定位 = 单细胞/单核/空间组学 + 人/小鼠 + "
            "有可下载处理数据（num_downloadable_samples>0）。三道闸：① API 侧过滤"
            "（technology=rna-seq + 两物种 + 可下载门槛，8 组主题查询 × relevance top 200）；"
            "② 内容级复核（标题+描述须命中单细胞/空间信号，ES 模糊匹配的弱相关与同题重复"
            "逐条剔除留档）；"
            "③ 跨源 accession 去重（GSE/SRP/ERP/E-MTAB 等，对其余 external + base）。"
            "四槽位原生结构化：species/technology 入库即填；" + enrich_note +
            "collection_doi/download_url/filesize 端点不供"
            "（诚实 null，不猜值）。逐条复核表与漏斗见 "
            "database/workstream/staging/refinebio/mapping.md + candidates_review.tsv。"
            "check_updates 以本文件为水位线，与官方 /v1/search/ ordering=-source_first_published "
            "最新条目比对（全库口径，note 如实标注）。"
        ),
        "record_count": len(records),
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {OUT_PATH}（{len(records)} 条）；复核表 {REVIEW_TSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
