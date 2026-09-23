# -*- coding: utf-8 -*-
"""把 CELLxGENE Discover（CZI）公共单细胞数据集拉取为一份**离线快照**，
归一成本项目的记录 schema，写入 `database/external/cellxgene.json`（外部平台库，opt-in）。

为什么这么做（设计约束）：
- **不动 `database/base/`**：官方 54 题评测冻结在原始 767 条 10x 语料上；外部平台库是**独立、默认关**的
  附加语料，只有网页端显式勾选「包含外部平台库」时才并入 → 确定性二元门与评测基线不受任何影响。
- **运行时不联网**：与 `downloads.py` 一致——这里一次性抓快照落地，查询期只读本地 JSON。
- **物种归一**：硬过滤按子串匹配 `record.species`，而 CELLxGENE 存「Homo sapiens」。必须映射成词表
  认得的通用名（Human/Mouse…），否则 species 约束会静默把外部记录全部滤掉。
- **has_raw_data=False**：CELLxGENE 资产是处理后的 H5AD/RDS 矩阵，非 FASTQ 原始 reads。据实标 False，
  「要 FASTQ」的查询会正确排除它们（保住 0% 违规不变量）。

数据许可：CELLxGENE Discover 数据为 CC-BY，curation API 为公开、面向程序化访问的官方接口。
仅抓取元数据 + 官方资产直链，合规。零第三方依赖（纯标准库 urllib）。

用法：
    py -3 scripts/ingest_cellxgene.py            # 抓全量 → database/external/cellxgene.json
    py -3 scripts/ingest_cellxgene.py --limit 50 # 只取前 N 条（快速自测）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

API_URL = "https://api.cellxgene.cziscience.com/curation/v1/datasets"
SOURCE_LABEL = "CELLxGENE Discover"
REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = REPO_ROOT / "database" / "external" / "cellxgene.json"

# 学名 → 词表通用名（硬过滤按子串匹配 record.species，必须命中通用名）。
ORGANISM_COMMON: dict[str, str] = {
    "homo sapiens": "Human",
    "mus musculus": "Mouse",
    "rattus norvegicus": "Rat",
    "danio rerio": "Zebrafish",
    "drosophila melanogaster": "Drosophila",
    "macaca mulatta": "Macaque",
    "macaca fascicularis": "Macaque",
    "callithrix jacchus": "Marmoset",
    "pan troglodytes": "Chimpanzee",
    "gallus gallus": "Chicken",
    "sus scrofa": "Pig",
    "canis lupus familiaris": "Dog",
    "canis familiaris": "Dog",
    "oryctolagus cuniculus": "Rabbit",
    "bos taurus": "Cattle",
    "homo sapien": "Human",   # 常见投稿拼写变体（漏 s）；据实归一，否则被 species 硬过滤静默滤掉
}


def _labels(items: object) -> list[str]:
    """CELLxGENE 的 organism/tissue/disease/assay 均为 [{'label':...}] 结构 → 取 label 列表。"""
    out: list[str] = []
    if isinstance(items, list):
        for it in items:
            if isinstance(it, dict) and it.get("label"):
                out.append(str(it["label"]).strip())
            elif isinstance(it, str) and it.strip():
                out.append(it.strip())
    return out


def _clean_join(items: object, cap: int = 8) -> str:
    """label 列表 → 去重（拆 CELLxGENE 的 `||` 多标签）+ 截断的可读串。
    图谱类数据集常跨 20+ 组织/疾病；截断只影响**展示与靠后标签的匹配**，常见单组织/单病查询仍命中。"""
    seen: list[str] = []
    for raw in _labels(items):
        for part in str(raw).split("||"):
            part = part.strip().strip(",").strip()
            if part and part.lower() not in {s.lower() for s in seen}:
                seen.append(part)
    if len(seen) > cap:
        return ", ".join(seen[:cap]) + f" 等{len(seen)}项"
    return ", ".join(seen)


# NA 型占位/缺失标记 + 投稿备注 → 既非组织也非疾病，**任何适配器**都不该写进语义字段
# （否则卡片标签/浏览分面/自由词检索被脏值污染，且「not applicable」会被当成一个真组织参与匹配）。
# 归一后（去标点、小写、collapse 空格）比较。
# 单一真源：原在 `ingest_arrayexpress.py`， 上移到本共享模块——`ingest_ebi_scea.py` 补 tissue/disease
# 时漏用它，真的把 `spleen, not applicable, heart, testes` 写进了快照（E-MTAB-4617）。
_NONVALUE = {
    "", "na", "n a", "nan", "null", "none", "not applicable", "not available",
    "not specified", "not collected", "not provided", "not reported",
    "unknown", "undetermined", "missing",
}


def _norm_token(v: str) -> str:
    """归一：小写 + 非字母数字转空格 + collapse。用于把 'N/A' / 'not applicable.' 等对齐到受控集合。"""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(v).strip().lower())).strip()


def _is_informative(v: str) -> bool:
    """是否是真实取值（非空、非 NA 占位、非投稿备注）。tissue/disease 共用、跨适配器共用。"""
    n = _norm_token(v)
    return bool(n) and n not in _NONVALUE and "see processed file" not in n


def _map_one_species(name: str) -> str:
    """单个学名 → 通用名。两级回退：① 精确查表；② 亚种三名法（>2 词）退化到「属+种」二名再查
    （如 `Mus musculus domesticus` → `mus musculus` → Mouse）。都不中 → 保留原名（保守，不误命中）。
    注：三名法的受控键（如 `canis lupus familiaris`=Dog）在①已命中，不会走到②。"""
    key = name.strip().lower()
    if key in ORGANISM_COMMON:
        return ORGANISM_COMMON[key]
    toks = key.split()
    if len(toks) > 2:
        binom = " ".join(toks[:2])
        if binom in ORGANISM_COMMON:
            return ORGANISM_COMMON[binom]
    return name.strip()


def map_species(organisms: list[str]) -> str:
    """学名列表 → 通用名（去重保序）；未知物种保留原名（不会误命中常见物种查询，保守安全）。"""
    seen: list[str] = []
    for name in organisms:
        common = _map_one_species(name)
        if common and common not in seen:
            seen.append(common)
    return ", ".join(seen)


def platform_hint(assays: list[str]) -> str:
    """从 assay 归一出一个「平台家族」提示，供 derive_platform_family 识别 + 前端展示。
    10x 单细胞 → Chromium；空间/其他技术保留自身名（family 归一为空，仅作展示）。"""
    text = " ".join(assays).lower()
    if "visium" in text:
        return "Visium"
    if "xenium" in text:
        return "Xenium"
    if "slide-seq" in text or "slideseq" in text:
        return "Slide-seq"
    if "merfish" in text:
        return "MERFISH"
    if "10x" in text:
        return "Chromium"
    return assays[0] if assays else ""


def pick_asset(assets: object) -> tuple[str, int]:
    """选代表性下载资产：优先 H5AD，其次 RDS，再取首个有 url 的。返回 (url, filesize)。"""
    if not isinstance(assets, list):
        return "", 0
    valid = [a for a in assets if isinstance(a, dict) and a.get("url")]
    for ft in ("H5AD", "RDS"):
        for a in valid:
            if str(a.get("filetype", "")).upper() == ft:
                return str(a["url"]), int(a.get("filesize") or 0)
    if valid:
        return str(valid[0]["url"]), int(valid[0].get("filesize") or 0)
    return "", 0


def to_record(ds: dict) -> dict | None:
    """一条 CELLxGENE 数据集 → 本项目记录 schema（compact）。无有效下载资产则丢弃。"""
    if ds.get("tombstone"):
        return None
    assets = ds.get("assets")
    dl_url, filesize = pick_asset(assets)
    if not dl_url:
        return None

    assays = _labels(ds.get("assay"))
    organisms = _labels(ds.get("organism"))
    dataset_id = str(ds.get("dataset_id") or "").strip()
    title = str(ds.get("title") or "").strip()
    collection = str(ds.get("collection_name") or "").strip()
    doi = str(ds.get("collection_doi") or "").strip()
    published = str(ds.get("published_at") or "")[:10]
    cell_count = ds.get("cell_count")
    explorer = str(ds.get("explorer_url") or "").strip()

    if not (title or collection):
        return None

    desc_bits = [b for b in (collection, f"DOI: {doi}" if doi else "") if b]
    # dataset_uid 前缀 cxg: → 绝不与 10x by_uid 直链表键碰撞（查表 miss → has_raw_data 保留本记录值）。
    return {
        "dataset_name": title or collection,
        "species": map_species(organisms),
        "tissue": _clean_join(ds.get("tissue")),
        "disease": _clean_join(ds.get("disease")),
        "chemistry": ", ".join(dict.fromkeys(assays)),   # 精确 assay（前端「技术方案」）
        "platform": platform_hint(assays),               # 平台家族提示（derive_platform_family/facet）
        "count": str(cell_count) if cell_count else "",
        "unit": "Cells",
        "has_raw_data": False,                            # H5AD/RDS 处理后矩阵，非 FASTQ 原始 reads
        "url": explorer or dl_url,                        # 人读页面（explorer）
        "download_url": dl_url,                           # 官方资产直链（H5AD/RDS）
        "filesize": filesize,
        "published_date": published,
        "description": " · ".join(desc_bits),
        "source": SOURCE_LABEL,
        "collection_doi": doi,
        "dataset_uid": f"cxg:{dataset_id}" if dataset_id else "",
    }


def fetch(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "biodata-agent-ingest/1.0"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（0=全量）")
    args = ap.parse_args()

    print(f"fetching {API_URL} ...")
    try:
        raw = fetch(API_URL)
    except Exception as exc:  # noqa: BLE001
        print(f"FETCH_FAILED: {type(exc).__name__}: {exc}")
        return 2
    if not isinstance(raw, list):
        print("unexpected payload (not a list)")
        return 2
    print(f"got {len(raw)} datasets")

    records: list[dict] = []
    seen_ids: set[str] = set()
    for ds in raw:
        if not isinstance(ds, dict):
            continue
        did = str(ds.get("dataset_id") or "")
        if did and did in seen_ids:
            continue
        rec = to_record(ds)
        if rec is None:
            continue
        if did:
            seen_ids.add(did)
        records.append(rec)
        if args.limit and len(records) >= args.limit:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": SOURCE_LABEL,
        "source_url": API_URL,
        "note": "离线快照·外部平台库（opt-in）。不并入官方评测语料。",
        "record_count": len(records),
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} records -> {OUT_PATH}")

    # 平台多样性一览（证明「已拓展到 10x 以外」）
    plats: dict[str, int] = {}
    for r in records:
        for a in str(r["chemistry"]).split(","):
            a = a.strip()
            if a:
                plats[a] = plats.get(a, 0) + 1
    top = sorted(plats.items(), key=lambda kv: -kv[1])[:20]
    print(f"distinct assays: {len(plats)}")
    for name, n in top:
        print(f"  {n:5d}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
