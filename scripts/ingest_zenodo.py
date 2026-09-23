# -*- coding: utf-8 -*-
"""把 Zenodo（通用开放仓储）中经人工甄别的小组学数据集拉取为一份**离线快照**，
写入 `database/external/zenodo.json`（外部平台库，opt-in，第 10 个来源）。

为什么首批是「查询配方 + 人工甄别入选清单」而不是整查询结果全收：
- Zenodo 是**通用**仓储，生物数据集只占一部分；即便用 Lucene 字段限定查询
  （`metadata.title/description` 短语 OR + `type=dataset`），结果里仍混有软件/方法代码、
  教学/演示/测试数据、非组学记录和碎片化系列（同一出版物拆成十几个 "data set N"）。
- 本项目是供检索的正式语料，宁少勿滥：候选由 8 组高精准查询产出（每组 relevance top 25，
   验证共 192 条含跨查询重复），逐条标题级人工甄别 + 入库前描述级复核，
  剔除理由按类别记录在历史 staging 台账（历史管线产物，不随本仓分发）。

设计约束（与 ingest_hca.py / ingest_cellxgene.py 一致）：
- **不动 `database/base/`**：官方评测冻结在 774 条 base 语料；外部库默认关
  （sources=None 只装基础语料）→ 确定性二元门不受影响。
- **运行时不联网**：一次性抓快照落地，查询期只读本地 JSON。
- **不绕过管护管线**：抓取走 `corpus_curation` 的 Zenodo 适配器原语
  （`_fetch_logged` 账本 + 20 req/min 限速、`_zenodo_core_ok` 形状闸 fail-closed、
  `_zenodo_to_record` 字段映射），入库前过 `_record_identity_keys` 实体级去重
  （对其余 8 个 external 文件；zenodo.json 自身除外——本脚本每次整体重建该文件）。
- 原始响应摘要与审计留在仓库外的历史 staging 目录（历史管线产物，不随本仓分发），
  不进 external/。

用法：
    python scripts/ingest_zenodo.py            # 抓入选清单 → database/external/zenodo.json
    python scripts/ingest_zenodo.py --dry-run  # 只抓取并打印复核表，不写任何文件
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from dataset_recommender.corpus import corpus_curation as cc  # noqa: E402

OUT_PATH = REPO_ROOT / "database" / "external" / "zenodo.json"
STAGING_DIR = REPO_ROOT / "research" / "staging" / "zenodo"
RAW_DIR = STAGING_DIR / "raw"

#: 发现候选用的查询配方（验证，每组 relevance top 25 经同一适配器拉取）。
#: 只起文档与可复跑作用——入库以 CURATED_IDS 人工甄别清单为准。
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

#: 人工甄别后的入选 record id（按发现查询分组，组内按 id 升序）。
#: 甄别标准：真为单细胞/单核/空间组学数据集（原始或处理后矩阵均可）；
#: 剔除软件/方法代码、教学/演示/测试材料、非组学记录、碎片化 "data set N" 系列、
#: 同题重复与元数据异常（如未来发布日期）。逐类剔除理由见 staging mapping.md。
#: 入库前还有两道闸（见 main()）：① access_right != "open"（restricted/embargoed）一律
#: 不收——准入硬条件要求「可以明确区分公开与受限记录」；② MANUAL_EXCLUDE 内容级复核剔除。
CURATED_IDS: tuple[tuple[str, tuple[int, ...]], ...] = (
    ("single-cell RNA-seq", (
        1490475, 7338746, 13385122, 15789211, 20559080, 20734829, 21401004,
    )),
    ("scRNA-seq", (
        4072975, 5655850, 6311356, 6855499, 8336489, 10015169, 11180599, 12742819,
        14226700, 14842959, 16929491, 17937263, 18354537, 19600804, 19600808,
        20017381, 20738974, 21292944, 21539847, 4708300,
    )),
    ("single-nucleus RNA-seq", (
        6981677, 8242458, 10103722, 13905956, 16615279, 16777109, 17138062,
        17345526, 17376961, 17733765, 18371193, 18481930, 18498335, 18863971,
        19937391, 20078073, 20135166, 21481391, 21722584, 21821264,
    )),
    ("spatial transcriptomics", (
        7003789, 7584110, 8259942, 10782711, 11619309, 13883320, 14602110,
        15789780, 16505469, 17108059, 17664023, 17999961, 18000256, 18306028,
        18490685, 18932473, 19472447,
    )),
    ("single-cell ATAC-seq", (
        263694, 263695, 8294148, 8313962, 14999322, 18265276, 19937284, 19938020,
    )),
    ("Visium", (
        6921620, 10372917, 10863736, 10901217, 11402686, 14620362, 14624390,
        14859875, 15151517, 15209564, 15274014, 15361297, 15722536, 16995699,
        18380571,
    )),
    ("single cell transcriptomics", (
        4087586, 4387066, 6910635, 7199569, 7234828, 7966849, 10261849, 14802187,
        15795735, 18765353, 19002805, 20548618,
    )),
    ("snRNA-seq", (
        6578047, 7126364, 7425938, 7789478, 8190317, 8219741, 10634153, 13269263,
        17042145, 17214560, 17402344, 18237749, 18426113, 18794192, 20078069,
        20078306, 21149645,
    )),
)


#: 内容级复核剔除（raw 摘要逐条看过描述与文件清单后判定，理由随条记录）：
#: 与机检闸（access_right、形状闸）不同，这三条是「公开但不适合作检索语料」的人工判定。
MANUAL_EXCLUDE: dict[int, str] = {
    5655850: "10x 官方数据集镜像（desc 自述 Obtained from 10xgenomics.com/resources/datasets）"
             "——与冻结 base（10x 官方语料）实体重复，不收",
    15722536: "仅两个 Loupe Browser 浏览器文件（.cloupe）的衍生产物，无可复用表达矩阵，不收",
    7584110: "文件清单只有显微图像 zip（Krausgruber_microscopy.zip），无转录组矩阵，不收",
}


def _curated_id_list() -> list[int]:
    """拍平入选清单并校验组内/跨组无重复（清单笔误在抓取前就暴露）。"""
    ids = [rid for _query, group in CURATED_IDS for rid in group]
    dupes = sorted({rid for rid in ids if ids.count(rid) > 1})
    if dupes:
        raise SystemExit(f"CURATED_IDS 有重复 record id：{dupes}")
    return ids


def _fetch_record_payload(rid: int, *, project_root: Path, use_cache: bool) -> dict:
    """按 record id 直查（适配器原语：账本 + 限速 + 形状闸）。raw 摘要缓存在 staging/raw/，
    复跑/干跑不重复联网；缓存只在本脚本内使用，不进 external/。"""
    cache = RAW_DIR / f"{rid}.json"
    if use_cache and cache.is_file():
        return json.loads(cache.read_text(encoding="utf-8"))
    payload = cc._fetch_logged(
        f"{cc.ZENODO_API}/{rid}", project_root=project_root,
        endpoint=cc.ZENODO_API, query=f"id:{rid}",
        min_interval=cc._ZENODO_MIN_INTERVAL,
    )
    if not cc._zenodo_core_ok(payload):
        raise cc.CurateError(
            "network_error",
            f"Zenodo 记录 {rid} 的响应形状变了（缺 id/标题）。本条跳过前已按 fail-closed 中止；"
            "可到 https://zenodo.org/ 人工核对。",
        )
    if use_cache:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def _external_identity_index_without_zenodo(project_root: Path) -> set[str]:
    """其余 external 文件的实体身份键集合（zenodo.json 除外——本脚本整体重建它）。"""
    index: set[str] = set()
    ext_dir = project_root / "database" / "external"
    for p in sorted(ext_dir.glob("*.json")):
        if not p.is_file() or p.name == OUT_PATH.name:
            continue
        try:
            existing = cc._load_file_records(p)
        except cc.CurateError:
            continue
        for record in existing:
            index |= cc._record_identity_keys(record)
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description="Zenodo 首批经甄别数据集 → external 快照")
    parser.add_argument("--dry-run", action="store_true",
                        help="只抓取并打印复核表，不写 zenodo.json / staging")
    parser.add_argument("--no-cache", action="store_true",
                        help="忽略 staging/raw/ 缓存，强制重新联网")
    args = parser.parse_args()

    ids = _curated_id_list()
    print(f"入选清单 {len(ids)} 条（发现查询 {len(DISCOVERY_QUERIES)} 组）")
    if not args.dry_run:
        RAW_DIR.mkdir(parents=True, exist_ok=True)

    records: list[dict] = []
    failures: list[tuple[int, str]] = []
    for i, rid in enumerate(ids, 1):
        try:
            payload = _fetch_record_payload(rid, project_root=REPO_ROOT,
                                            use_cache=not args.no_cache and not args.dry_run)
        except Exception as exc:  # 单条失败不连累整批；如实记录后由人工复核决定是否剔出清单
            failures.append((rid, f"{type(exc).__name__}: {exc}"))
            print(f"[{i:>3}/{len(ids)}] {rid}: 抓取失败 {failures[-1][1]}")
            continue
        rec = cc._zenodo_to_record(payload)
        if rec is None:
            failures.append((rid, "resource_type 非 dataset（服务端过滤之外的混入）"))
            print(f"[{i:>3}/{len(ids)}] {rid}: 非 dataset，跳过")
            continue
        # 闸 1：公开性——restricted/embargoed 一律不收（准入硬条件「明确区分公开与受限」）。
        access = str((payload.get("metadata") or {}).get("access_right") or "").strip().lower()
        if access and access != "open":
            failures.append((rid, f"access_right={access}（非公开，不收）"))
            print(f"[{i:>3}/{len(ids)}] {rid}: access_right={access}，跳过")
            continue
        # 闸 2：内容级人工复核剔除（理由见 MANUAL_EXCLUDE）。
        if rid in MANUAL_EXCLUDE:
            failures.append((rid, f"人工复核剔除：{MANUAL_EXCLUDE[rid]}"))
            print(f"[{i:>3}/{len(ids)}] {rid}: 人工复核剔除，跳过")
            continue
        records.append(rec)
        print(f"[{i:>3}/{len(ids)}] {rid}: {rec['dataset_name'][:80]}")

    if not records:
        print("没有拿到任何记录，不写文件。")
        return 1

    # 实体级去重（对其余 8 个 external 文件；同 url / 同来源同 uid 即同一实体）
    index = _external_identity_index_without_zenodo(REPO_ROOT)
    kept: list[dict] = []
    deduped: list[dict] = []
    for rec in records:
        keys = cc._record_identity_keys(rec)
        if keys and keys & index:
            deduped.append(rec)
        else:
            kept.append(rec)
    kept.sort(key=lambda r: int(r["public_accession"]))

    print(f"\n抓取 {len(records)} 条；跨源去重剔除 {len(deduped)} 条；闸/失败剔除 {len(failures)} 条；"
          f"拟入库 {len(kept)} 条")
    for rec in deduped:
        print(f"  去重剔除: {rec['dataset_uid']} {rec['dataset_name'][:70]}")
    for rid, why in failures:
        print(f"  未入库: {rid} {why[:100]}")

    if args.dry_run:
        print("\n--dry-run：不写任何文件。")
        return 0

    snapshot_date = datetime.now(timezone.utc).date().isoformat()
    payload = {
        "source": "Zenodo",
        "source_url": "https://zenodo.org/",
        "note": (
            "离线快照·外部平台库候选（opt-in），不并入官方评测语料。"
            f"首批 {len(kept)} 条（{snapshot_date} 采集）：8 组 Lucene 字段限定查询"
            "（metadata.title/description 短语 OR + type=dataset，每组 relevance top 25）"
            "共产出候选 192 条（含跨查询重复），逐条人工甄别剔除软件/方法代码、教学/演示/测试材料、"
            "非组学记录、碎片化系列与重复；入库前再过公开性闸（access_right 非 open 不收）与"
            "内容级复核（镜像/纯图像/纯浏览器文件不收）；逐类剔除理由与复核表见 "
            "database/workstream/staging/zenodo/mapping.md。"
            "Zenodo 是通用开放仓储，生物数据集只占一部分；物种/组织/疾病无结构化字段，"
            "物种从标题/描述文本抠取（不全，抠不到留空），组织/疾病槽位放弃。"
            "check_updates 以本文件为水位线，与官方 API type=dataset&sort=mostrecent 最新条目比对。"
        ),
        "record_count": len(kept),
        "records": kept,
    }
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已写入 {OUT_PATH}（{len(kept)} 条）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
