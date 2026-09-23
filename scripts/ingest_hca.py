# -*- coding: utf-8 -*-
"""把 Human Cell Atlas（HCA）DCP 数据门户的单细胞 project 拉取为一份**离线快照**，
归一成本项目的记录 schema，写入 `database/external/hca.json`（外部平台库，opt-in）。

为什么加 HCA（与 CELLxGENE 的差异化价值）：
- **不同平台、不同来源**：HCA 是国际人类细胞图谱的一级数据协调门户（DCP/Azul 索引），与 CELLxGENE 各成一库、
  在浏览页并列展示；智能查询可按来源自由勾选。
- **补上「有 FASTQ 原始 reads」的一类**：CELLxGENE 资产是处理后矩阵（has_raw_data 全 False）；HCA 的
  `fileTypeSummaries` 会声明 `fastq`/`fastq.gz` 文件 → 据实标 `has_raw_data=True`，让「要 FASTQ / 要原始数据」
  的查询真正能命中外部库（这是 CELLxGENE 做不到的）。
- **物种归一**：硬过滤按子串匹配 `record.species`，HCA 存学名（Homo sapiens…）→ 复用 CELLxGENE 的
  `map_species` 映射成词表通用名（Human/Mouse…），否则 species 约束会静默把它们全滤掉。

设计约束（与 ingest_cellxgene.py 一致）：
- **不动 `database/base/`**：官方 54 题评测冻结在原始 767 条 10x 语料；外部库默认关（sources=None 只装基础语料）→
  确定性二元门不受影响。
- **运行时不联网**：一次性抓快照落地，查询期只读本地 JSON。
- 零第三方依赖（纯标准库 urllib）。复用 `ingest_cellxgene` 的物种/平台/清洗助手，保持单一真源。

数据许可：HCA DCP 数据面向开放科研；Azul 索引为公开、面向程序化访问的官方 REST 接口，仅抓元数据 + 门户直链。

用法：
    py -3 scripts/ingest_hca.py            # 抓全量 → database/external/hca.json
    py -3 scripts/ingest_hca.py --limit 50 # 只取前 N 条（快速自测）
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ingest_cellxgene as cxg  # 复用 map_species / platform_hint / _clean_join（单一真源）

API_BASE = "https://service.azul.data.humancellatlas.org/index/projects"
SOURCE_LABEL = "Human Cell Atlas"
EXPLORE_TMPL = "https://data.humancellatlas.org/explore/projects/{pid}"
OUT_PATH = REPO_ROOT / "database" / "external" / "hca.json"

# 健康态标签（normal/healthy/control）＝ 源库**显式声明健康** → 统一写入规范值 `"normal"`。
# 口径：与 CELLxGENE / EBI SCEA / ArrayExpress 一致——「健康」
# 是**已知事实**，不是「没标注」。旧口径抹成空串的代价（实测在案）：搜「健康」对 HCA 恒 0
# （HCA 正是健康参考图谱，假阴性直击核心用途）、caveat 误报「无法核验」、lenient 误纳真负。
# 新口径下 disease 为空**只**表示「源库确实没标注」；"none" 等 NA 占位仍不写入（缺失≠健康）。
_HEALTHY = {"normal", "healthy", "control"}


def _canonical_disease(v: str) -> str | None:
    """disease 原始取值 → 规范取值：NA 占位（含旧口径的 "none"/""）→ None（丢弃）；
    健康态 → 规范 token `"normal"`；真实疾病 → 原文保留。NA 判定复用 cxg._is_informative
    （单一真源，与跨库 NA 守卫测试同口径）。"""
    s = v.strip()
    if not cxg._is_informative(s):
        return None
    return "normal" if s.lower() in _HEALTHY else s


def _collect(hit: dict, section: str, key: str) -> list[str]:
    """从 hit[section]（list[dict]）里汇总 key 对应的字符串标签（拍平 + 去 None）。"""
    out: list[str] = []
    for item in hit.get(section) or []:
        if not isinstance(item, dict):
            continue
        for v in item.get(key) or []:
            if v is None:
                continue
            s = str(v).strip()
            if s:
                out.append(s)
    return out


def _has_fastq(hit: dict) -> bool:
    """fileTypeSummaries 里出现 fastq/fastq.gz → 该 project 托管 FASTQ 原始 reads。"""
    for f in hit.get("fileTypeSummaries") or []:
        if isinstance(f, dict) and str(f.get("format", "")).lower().startswith("fastq"):
            return True
    return False


def _submission_date(hit: dict) -> str:
    """dates[0].submissionDate（ISO 带 T）→ 取日期部分 YYYY-MM-DD。"""
    dates = hit.get("dates")
    if isinstance(dates, list) and dates and isinstance(dates[0], dict):
        for k in ("submissionDate", "aggregateSubmissionDate", "updateDate"):
            v = str(dates[0].get(k) or "")
            if v:
                return v[:10]
    return ""


def to_record(hit: dict) -> dict | None:
    """一个 HCA project → 本项目记录 schema。无标题则丢弃。"""
    proj = (hit.get("projects") or [{}])[0]
    if not isinstance(proj, dict):
        return None
    title = str(proj.get("projectTitle") or "").strip()
    if not title:
        return None
    pid = str(proj.get("projectId") or "").strip()

    species = cxg.map_species(_collect(hit, "donorOrganisms", "genusSpecies"))
    # 组织：samples/specimens 的 organ；疾病：samples + donorOrganisms 的 disease（健康态写 "normal"，见 _canonical_disease）
    tissue = cxg._clean_join(_collect(hit, "samples", "organ") + _collect(hit, "specimens", "organ"))
    diseases = _collect(hit, "samples", "disease") + _collect(hit, "donorOrganisms", "disease")
    # 健康态写入 "normal"（源库显式声明健康），不再抹掉；NA 占位仍丢弃。
    disease = cxg._clean_join([c for c in (_canonical_disease(d) for d in diseases) if c])
    libs = _collect(hit, "protocols", "libraryConstructionApproach")

    cell_count = proj.get("estimatedCellCount")
    if not cell_count:  # 回退：cellSuspensions.totalCells 求和
        tot = 0
        for cs in hit.get("cellSuspensions") or []:
            if isinstance(cs, dict) and isinstance(cs.get("totalCells"), (int, float)):
                tot += int(cs["totalCells"])
        cell_count = tot or None

    lab = ", ".join(dict.fromkeys(str(x) for x in (proj.get("laboratory") or []) if x))
    desc = str(proj.get("projectDescription") or "").strip().replace("\n", " ")
    if len(desc) > 400:
        desc = desc[:400] + "…"

    explore = EXPLORE_TMPL.format(pid=pid) if pid else "https://data.humancellatlas.org/explore/projects"
    return {
        "dataset_name": title,
        "species": species,
        "tissue": tissue,
        "disease": disease,
        "chemistry": ", ".join(dict.fromkeys(libs)),     # libraryConstructionApproach（前端「技术方案」）
        "platform": cxg.platform_hint(libs),             # 平台家族提示（derive_platform_family/facet）
        "count": str(int(cell_count)) if cell_count else "",
        "unit": "Cells",
        "has_raw_data": _has_fastq(hit),                 # 据实：托管 FASTQ → True
        "url": explore,                                  # 人读门户页
        "download_url": explore,                         # HCA 经门户下载（无单文件直链）→ 指向 project 页
        "filesize": 0,
        "published_date": _submission_date(hit),
        "description": " · ".join(b for b in (desc, lab) if b),
        "source": SOURCE_LABEL,
        "collection_doi": "",
        # 前缀 hca: → 绝不与 10x by_uid 直链表键碰撞（查表 miss → has_raw_data 保留本记录值）。
        "dataset_uid": f"hca:{pid}" if pid else "",
    }


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "biodata-agent-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（0=全量）")
    args = ap.parse_args()

    url = f"{API_BASE}?size=75"   # Azul 上限 size<=75
    records: list[dict] = []
    seen: set[str] = set()
    page = 0
    print(f"fetching {API_BASE} ...")
    while url and page < 50:  # 安全上限：50 页（每页 75）远超 ~532
        page += 1
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"FETCH_FAILED (page {page}): {type(exc).__name__}: {exc}")
            return 2
        hits = payload.get("hits") if isinstance(payload, dict) else None
        if not isinstance(hits, list):
            print("unexpected payload (no hits list)")
            return 2
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            rec = to_record(hit)
            if rec is None:
                continue
            uid = rec.get("dataset_uid") or rec["dataset_name"]
            if uid in seen:
                continue
            seen.add(uid)
            records.append(rec)
            if args.limit and len(records) >= args.limit:
                url = None
                break
        else:
            url = (payload.get("pagination") or {}).get("next")
        print(f"  page {page}: {len(records)} records so far")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload_out = {
        "source": SOURCE_LABEL,
        "source_url": API_BASE,
        "note": "离线快照·外部平台库（opt-in）。不并入官方评测语料。",
        "record_count": len(records),
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} records -> {OUT_PATH}")

    n_fastq = sum(1 for r in records if r["has_raw_data"])
    specs: dict[str, int] = {}
    for r in records:
        for s in str(r["species"]).split(","):
            s = s.strip()
            if s:
                specs[s] = specs.get(s, 0) + 1
    print(f"has_raw_data (FASTQ) True: {n_fastq}/{len(records)}")
    print("species:", ", ".join(f"{k}={v}" for k, v in sorted(specs.items(), key=lambda kv: -kv[1])[:10]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
