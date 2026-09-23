# -*- coding: utf-8 -*-
"""把 ArrayExpress（EBI BioStudies 托管的「欧洲版 GEO」）单细胞研究拉取为一份**离线快照**，
归一成本项目的记录 schema，写入 `database/external/arrayexpress.json`（外部平台库，opt-in）。

为什么加 ArrayExpress（与已有三库 CELLxGENE / HCA / EBI SCEA 的差异化价值）：
- **不同托管平台、不同数据形态**：ArrayExpress 是 EBI BioStudies 下的原始提交仓库（类比 NCBI GEO），
  收录大量「实验设计 + 样本表征」齐全的研究级记录，与偏「已处理矩阵」的 CELLxGENE、偏「项目级图谱」
  的 HCA、只给因子名不给取值的 EBI SCEA 互补；在浏览页并列为独立一库。
- **两段式抓取换取干净的 tissue/disease**：搜索接口只给自由文本 `content`；本脚本对每条命中再拉
  BioStudies 研究详情（`/api/v1/studies/<accession>`），递归遍历 `section.subsections` 找
  `Organism part`/`Tissue`/`Disease` 等结构化字段，取值比纯文本正则更干净。
- **物种归一**：复用 CELLxGENE 的 `map_species`，学名（Homo sapiens…）→ 词表通用名（Human…），
  否则 species 硬过滤会静默把它们全滤掉。详情拉取失败时，回退到对搜索命中 `content` 做正则匹配
  （复用 `cxg.ORGANISM_COMMON` 的学名键），保证至少给出粗粒度物种。

诚实的字段局限（写清楚，避免误导）：
- **count/unit 留空**：ArrayExpress 研究级记录没有可靠的「细胞数」字段（`files` 只是文件数，不是细胞数），
  宁可留空也不冒充。
- **has_raw_data 保守置 False**：不逐条列文件确认 FASTQ 存在，宁可漏标也不错标（HCA 已按
  `fileTypeSummaries` 据实覆盖「有 FASTQ」的一类，二者互补）。
- 详情接口偶发失败时优雅降级：species 回退正则、tissue/disease 留空，不中断整体抓取。

设计约束（与 ingest_cellxgene.py / ingest_hca.py 一致）：
- **不动 `database/base/`**：官方 54 题评测冻结在原始 767 条 10x 语料；外部平台库默认关（sources=None 只装
  基础语料）→ 确定性二元门不受影响。
- **运行时不联网**：一次性抓快照落地，查询期只读本地 JSON。
- 零第三方依赖（纯标准库 urllib）。复用 `ingest_cellxgene` 的物种/平台/清洗助手，保持单一真源。

数据许可：ArrayExpress/BioStudies 数据面向开放科研；BioStudies REST 为公开、面向程序化访问的官方接口，
仅抓元数据 + 门户直链。

用法：
    py -3 scripts/ingest_arrayexpress.py                # 抓全量 → database/external/arrayexpress.json
    py -3 scripts/ingest_arrayexpress.py --limit 30     # 只取前 N 条（快速自测）
    py -3 scripts/ingest_arrayexpress.py --detail 0     # 跳过详情富化，只用搜索命中 + content 正则（更快）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ingest_cellxgene as cxg  # 复用 map_species / platform_hint / _clean_join / ORGANISM_COMMON（单一真源）

SEARCH_API = "https://www.ebi.ac.uk/biostudies/api/v1/arrayexpress/search"
DETAIL_API = "https://www.ebi.ac.uk/biostudies/api/v1/studies"
STUDY_TMPL = "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/{accession}"
SOURCE_LABEL = "ArrayExpress"
QUERY = "single cell RNA sequencing"
PAGE_SIZE = 100
MAX_PAGES = 30  # 安全上限：30 页 * 100 = 3000，远超 ~1048 条命中
OUT_PATH = REPO_ROOT / "database" / "external" / "arrayexpress.json"

# 结构化字段名（小写后匹配）：BioStudies 的 Source Characteristics / Characteristics Table 节点里，
# 这些 name 携带我们要的组织 / 疾病取值。
_TISSUE_NAMES = {"organism part", "tissue"}
_DISEASE_NAMES = {"disease", "disease state", "clinical history", "clinical information"}
# 健康态标签（normal/healthy/control）＝ 源库**显式声明健康** → 统一写入规范值 `"normal"`。
# 口径：与 CELLxGENE / EBI SCEA 一致——「健康」是**已知事实**，
# 不是「没标注」。旧口径把健康态抹成空串，代价是诚实层的三个症状（实测在案）：
#   ① 搜「健康人的肺组织数据」对本库恒 0 命中（答案本来可知，是假阴性）；
#   ② `coverage_caveats` 把「已知健康」误报成「未标注、无法核验」；
#   ③ `lenient` 把已知健康当真·未标注放行 → 搜疾病时误纳真负。
# 新口径下 disease 为空**只**表示「源库确实没标注」。disease 分面会出现 "normal" 大项（预期行为，
# 与 CELLxGENE 1847 条 / SCEA 174 条同口径）。
_HEALTHY = {"normal", "healthy", "control"}
# NA 占位过滤（`_NONVALUE` / `_norm_token` / `_is_informative`）已上移到 `ingest_cellxgene`，
# 与 `_clean_join` 同处共享助手模块 → 四个适配器共用单一真源（SCEA 补字段时漏用它、写脏了快照）。
_norm_token = cxg._norm_token
_is_informative = cxg._is_informative


def _canonical_disease(v: str) -> str | None:
    """disease 原始取值 → 规范取值：非取值（NA 占位）→ None（丢弃）；健康态 → 规范 token
    `"normal"`；真实疾病 → 原文保留。首词判定健康态（catch 'normal tissue' 这类多词健康标记）。"""
    if not _is_informative(v):
        return None
    n = _norm_token(v)
    return "normal" if n.split(" ")[0] in _HEALTHY else v
# ---- 平台家族分类----------------------------------------------------
# 旧版只把 `title + content` 喂给 cxg.platform_hint，且只保留它识别出的 5 个家族 → 覆盖率实测 3.1%
# （1730/1786 留空、56 条 Chromium）。病根：AE 单细胞记录的**平台家族写在详情的 protocol
# `Description` 里**（"Libraries were constructed using the Smart-seq3xpress protocol" /
# "10x Genomics Chromium" / Software=cellranger），而旧版 platform 路径从不读详情自由文本
# （`_collect_characteristics` 读了详情却只留 tissue/disease）。修法：把详情里承载平台的自由文本
# 一并喂进一个**只认无歧义关键词**的分类器；识别不出就留空（诚实：不猜）。
#
# 诚实边界：这是关键词分类、不是策展平台字段。**按家族是否出现（二值）判定，不按别名命中频次**——
# 恰好一个家族出现才给标签，2+ 个出现（真并列 / 对照提及）一律留空（见 classify_platform 头，验证
# CONFIRMED major）。空间技术关键词高度特异、放最前；裸 "10x"（如 "10x magnification"）不计，
# 只认 chromium/cellranger/10x genomics / 10x+单细胞化学标记。

_PLATFORM_PATTERNS: "list[tuple[str, list[str]]]" = [
    # 空间技术（关键词特异，几乎不会被顺带提及）
    ("Visium", [r"visium"]),
    ("Xenium", [r"xenium"]),
    ("Slide-seq", [r"slide-?seq"]),
    ("MERFISH", [r"merfish"]),
    ("seqFISH", [r"seqfish"]),
    # 板式 / 液滴 单细胞文库化学
    ("Smart-seq", [r"smart-?seq"]),                 # Smart-seq2 / Smart-seq3 / Smart-seq3xpress
    ("Drop-seq", [r"drop-?seq"]),
    ("inDrop", [r"in-?drop"]),
    ("CEL-seq", [r"cel-?seq"]),                     # CEL-seq2
    ("MARS-seq", [r"mars-?seq"]),
    ("Seq-Well", [r"seq-?well"]),
    ("sci-RNA-seq", [r"sci-rna-?seq"]),
    ("BD Rhapsody", [r"\brhapsody\b"]),
    # "Fluidigm"（而非 "Fluidigm C1"）：Fluidigm/Standard BioTools 还产 CyTOF/Biomark 等非 scRNA
    # 仪器，只凭 "fluidigm" 断言具体 C1 化学是 overclaim；留家族名更诚实（验证）。
    ("Fluidigm", [r"fluidigm"]),
    # 单一模式同时覆盖 "STRT" 与 "STRT-seq"，避免两条正则对同一 token 双计（验证）。
    ("STRT-seq", [r"\bstrt(?:-?seq)?\b"]),
    ("Quartz-seq", [r"quartz-?seq"]),
    # 10x Chromium：无歧义 token，或 "10x" 紧跟单细胞化学标记（3'/5'/v2/v3/single/scRNA/GEX）
    ("Chromium", [r"chromium", r"cell\s?ranger",
                  r"10\s*x\s*genomics",
                  r"10\s*x[^a-z0-9]{0,4}(?:3['’]|5['’]|v[234]|single|scrna|gene\s*expression|gex)"]),
]
_PLATFORM_RE = [(fam, [re.compile(p, re.I) for p in pats]) for fam, pats in _PLATFORM_PATTERNS]

# 详情里承载平台家族的属性名（小写后匹配）：protocol 描述、软件、study type、标题、测序仪。
_PLATFORM_TEXT_NAMES = {
    "description", "study type", "software", "hardware", "title",
    "protocol", "library construction", "library construction protocol",
    "single cell isolation", "extract protocol", "nucleic acid sequencing protocol",
}

# publication DOI 匹配（关联论文的 DOI，可能是期刊或 preprint）。
# 字符类**不含 `;`**：否则 "10.x;10.y" 两个拼接 DOI 会被当成一个合并 token（验证）。
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._()/:A-Za-z0-9]+")


def classify_platform(*texts: str) -> str:
    """合并自由文本 → 单细胞平台家族短标签。只用无歧义关键词；识别不出返回 ""（不猜）。

    **按家族是否出现（二值）判定，不按别名命中频次**（实测复核：一个平台若用多个
    同义词写（如 "10x Genomics Chromium" 同时命中 `chromium` 与 `10x genomics` 两条别名）会在频次法下
    累积成更高计数、盖过一个真正**共提及**的单别名平台（如 "10x Genomics Chromium and Smart-seq2" →
    Chromium 2 / Smart-seq 1 → 错标成单一 Chromium），这正是要杀的 overclaim。改为数**家族**：
      - 恰好 1 个家族出现 → 该家族（同一家族的多个别名仍只算它出现一次，如纯 10x 多种写法 → Chromium）；
      - 0 个 或 2+ 个家族出现 → 留空（测不准、不谎报）。
    代价：真正共用多平台、以及「unlike Drop-seq, we used Smart-seq2」这类对照/否定提及，都留空
    （保守 underclaim，丢标签而非错标签，与全库「识别不出即空」的诚实默认一致）。
    """
    combined = " ".join(t for t in texts if t)
    if not combined:
        return ""
    present = [fam for fam, res in _PLATFORM_RE if any(r.search(combined) for r in res)]
    return present[0] if len(present) == 1 else ""


def _collect_platform_text(section: object) -> str:
    """递归收集详情 section 里承载平台家族的自由文本（protocol Description / Software / Study type /
    Title / Hardware…），供 classify_platform。与 `_collect_characteristics` 同样遍历，但取不同字段。"""
    bits: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if isinstance(a, dict):
                    name = str(a.get("name") or "").strip().lower()
                    val = str(a.get("value") or "").strip()
                    if val and name in _PLATFORM_TEXT_NAMES:
                        bits.append(val)
            if node.get("subsections") is not None:
                walk(node["subsections"])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    return " ".join(bits)


def extract_publication_doi(detail: object) -> str:
    """从 BioStudies 详情取 **publication DOI**（`DOI` 属性 + DOI 类型 link）。返回首个合法 DOI；无则 ""。
    语义是关联论文的 DOI（未发表子集本就无、留空），绝不编造；也绝不当作数据集自己的 DOI。"""
    if not isinstance(detail, dict):
        return ""
    section = detail.get("section")
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if isinstance(a, dict):
                    name = str(a.get("name") or "").strip().lower()
                    val = str(a.get("value") or "").strip()
                    if name == "doi" and val:
                        m = _DOI_RE.search(val)
                        if m:
                            found.append(m.group(0))
            url = node.get("url")
            if isinstance(url, str) and url:
                is_doi_link = any(
                    isinstance(x, dict)
                    and str(x.get("name") or "").strip().lower() == "type"
                    and str(x.get("value") or "").strip().lower() == "doi"
                    for x in (node.get("attributes") or [])
                )
                if is_doi_link:
                    m = _DOI_RE.search(url)
                    if m:
                        found.append(m.group(0))
            for key in ("subsections", "links"):
                if node.get(key) is not None:
                    walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    seen: list[str] = []
    for d in found:
        d = d.rstrip(".,;)")
        if d and d not in seen:
            seen.append(d)
    return seen[0] if seen else ""


def fetch(url: str, retries: int = 5, timeout: int = 60) -> dict:
    """GET url → JSON，带重试（5 次，指数退避）。本机网络慢且偶尔抖，靠这层扛过瞬时失败。"""
    last_exc: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "biodata-agent-ingest/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if i < retries - 1:
                time.sleep(1.5 * (i + 1))
    assert last_exc is not None
    raise last_exc


def fetch_detail(accession: str) -> dict | None:
    """拉单条研究详情；5 次重试仍失败 → 优雅降级返回 None（调用方回退正则 + 空 tissue/disease）。"""
    url = f"{DETAIL_API}/{accession}"
    try:
        return fetch(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  DETAIL_FAILED {accession}: {type(exc).__name__}: {exc} -> 降级（正则物种，tissue/disease 留空）")
        return None


def _attr(attrs: object, name: str) -> str:
    """attributes 列表（[{name, value}, ...]）里按 name 精确匹配取 value。"""
    if not isinstance(attrs, list):
        return ""
    for a in attrs:
        if isinstance(a, dict) and a.get("name") == name:
            return str(a.get("value") or "").strip()
    return ""


def _collect_characteristics(section: dict) -> tuple[list[str], list[str]]:
    """递归遍历 section.subsections（节点可能是 dict 或 list，元素可能再嵌 list），
    在每个节点的 attributes 里按 name（小写）收集 tissue / disease 取值。"""
    tissues: list[str] = []
    diseases: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for a in node.get("attributes") or []:
                if not isinstance(a, dict):
                    continue
                name = str(a.get("name") or "").strip().lower()
                val = str(a.get("value") or "").strip()
                if not val:
                    continue
                if name in _TISSUE_NAMES:
                    tissues.append(val)
                elif name in _DISEASE_NAMES:
                    diseases.append(val)
            sub = node.get("subsections")
            if sub is not None:
                walk(sub)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(section)
    return tissues, diseases


def _species_from_content(content: str) -> list[str]:
    """详情缺失时的回退：对搜索命中的自由文本 content 做正则，匹配 cxg.ORGANISM_COMMON 的学名键。"""
    text_l = content.lower()
    found: list[str] = []
    for latin in cxg.ORGANISM_COMMON:
        if re.search(r"\b" + re.escape(latin) + r"\b", text_l):
            found.append(latin)
    return found


def to_record(hit: dict, detail: dict | None) -> dict | None:
    """一条 ArrayExpress 搜索命中（+ 可选详情）→ 本项目记录 schema。无标题/无 accession 则丢弃。"""
    title = str(hit.get("title") or "").strip()
    accession = str(hit.get("accession") or "").strip()
    if not title or not accession:
        return None
    content = str(hit.get("content") or "")
    release_date = str(hit.get("release_date") or "").strip()
    published_date = release_date if re.match(r"^\d{4}-\d{2}-\d{2}$", release_date) else ""

    organism_raw = ""
    study_type = ""
    description_detail = ""
    tissues: list[str] = []
    diseases: list[str] = []
    section = detail.get("section") if isinstance(detail, dict) else None
    if isinstance(section, dict):
        attrs = section.get("attributes") or []
        organism_raw = _attr(attrs, "Organism")
        study_type = _attr(attrs, "Study type")
        description_detail = _attr(attrs, "Description")
        tissues, diseases = _collect_characteristics(section)

    if organism_raw:
        species_names = [p.strip() for p in organism_raw.split(";") if p.strip()]
    else:
        species_names = _species_from_content(content)  # 详情缺失/无 Organism → 回退正则

    tissue = cxg._clean_join([t for t in tissues if _is_informative(t)])
    # 健康态写入 "normal"（源库显式声明健康）；多个健康标签经 _clean_join 去重成一个。
    disease = cxg._clean_join([c for c in (_canonical_disease(d) for d in diseases) if c])

    desc = description_detail.replace("\n", " ").strip()
    if len(desc) > 400:
        desc = desc[:400] + "…"
    desc_bits = [b for b in (accession, study_type, desc) if b]
    page_url = STUDY_TMPL.format(accession=accession)

    # 平台家族：把详情 protocol Description / Software / Study type 一并喂进关键词分类器
    # （旧版只看 title+content，漏掉详情自由文本 → 覆盖 3.1%）。识别不出留空。
    platform = classify_platform(title, content, _collect_platform_text(section))
    # publication DOI（关联论文，发表子集才有；未发表留空、不编造）。
    collection_doi = extract_publication_doi(detail)

    return {
        "dataset_name": title,
        "species": cxg.map_species(species_names),
        "tissue": tissue,
        "disease": disease,
        "chemistry": study_type,                          # 详情 Study type（前端「技术方案」）；取不到留空
        "platform": platform,                             # 仅 5 个真·家族短标签或空
        "count": "",                                       # 研究级记录无可靠细胞数，不拿 files 数冒充
        "unit": "",
        "has_raw_data": False,                              # 保守：不逐条核实 FASTQ，宁可漏不可错报
        "url": page_url,
        "download_url": page_url,                           # BioStudies 经研究页下载（无单文件直链）
        "filesize": 0,
        "published_date": published_date,
        "description": " · ".join(desc_bits),
        "source": SOURCE_LABEL,
        "collection_doi": collection_doi,
        # 前缀 ae: → 绝不与 10x by_uid 直链表键碰撞（查表 miss → has_raw_data 保留本记录值 False）。
        "dataset_uid": f"ae:{accession}",
    }


def enrich_existing_snapshot(limit: int = 0, sleep: float = 0.2) -> int:
    """**Surgical enrichment**：只补 `platform` + `collection_doi` 两字段进现有快照，
    其余字段（tissue/disease/species/description…）**一字不动**——避免无关漂移，也不碰既定的
    disease 口径决策。

    单调保证：详情抓取失败、或新识别平台为空但旧有值 → **保留旧 platform**（绝不因抓取失败抹掉
    已有覆盖）。collection_doi 现全空、直接写新值。写盘前校验记录数 / UID 集与旧快照一致；不一致
    则**不写**并报错（守「不静默改数据」）。旧快照先备份到 `<OUT>.pre-n14.bak`。
    """
    if not OUT_PATH.exists():
        print(f"ENRICH_ABORT: 快照不存在 {OUT_PATH}")
        return 2
    snap = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    records = snap.get("records")
    if not isinstance(records, list) or not records:
        print("ENRICH_ABORT: 快照无 records")
        return 2
    old_uids = [r.get("dataset_uid") for r in records]

    backup = OUT_PATH.with_suffix(".pre-n14.bak")
    backup.write_text(OUT_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"backup -> {backup}")

    total = len(records)
    todo = records[:limit] if limit else records
    plat_before = sum(1 for r in records if str(r.get("platform") or "").strip())
    doi_before = sum(1 for r in records if str(r.get("collection_doi") or "").strip())
    n_detail_fail = 0
    n_plat_gained = 0
    n_doi_gained = 0
    multi_family = 0

    for i, rec in enumerate(todo, 1):
        uid = str(rec.get("dataset_uid") or "")
        acc = uid.split("ae:", 1)[-1] if uid.startswith("ae:") else uid
        if not acc:
            continue
        detail = fetch_detail(acc)
        if detail is None:
            n_detail_fail += 1
            time.sleep(sleep)
            continue
        section = detail.get("section") if isinstance(detail, dict) else None
        old_plat = str(rec.get("platform") or "").strip()
        new_plat = classify_platform(
            str(rec.get("dataset_name") or ""),
            str(rec.get("description") or ""),
            _collect_platform_text(section),
        )
        # 单调：识别不出但旧有值 → 保留旧值，绝不因本次抓取抹掉已有覆盖。
        if new_plat:
            if not old_plat:
                n_plat_gained += 1
            if "," in new_plat:
                multi_family += 1
            rec["platform"] = new_plat
        # collection_doi：单调（同 platform）——新识别为空但旧有值 → 保留旧值，绝不因二次运行的
        # 瞬时抓取失败/正则漏配抹掉已填 DOI（验证：re-runnable enrich 的 DOI 也要有单调保护）。
        new_doi = extract_publication_doi(detail)
        if new_doi:
            if not str(rec.get("collection_doi") or "").strip():
                n_doi_gained += 1
            rec["collection_doi"] = new_doi
        if i % 100 == 0 or i == len(todo):
            print(f"  enrich {i}/{len(todo)}: +platform={n_plat_gained} +doi={n_doi_gained} "
                  f"(detail_fail={n_detail_fail})")
        time.sleep(sleep)

    # ---- 写盘前校验：记录数 / UID 集不变（surgical，绝不增删记录） ----
    new_uids = [r.get("dataset_uid") for r in records]
    if len(records) != total or new_uids != old_uids:
        print(f"ENRICH_ABORT: 记录集变了（{total}→{len(records)}）——不写盘")
        return 3

    plat_after = sum(1 for r in records if str(r.get("platform") or "").strip())
    doi_after = sum(1 for r in records if str(r.get("collection_doi") or "").strip())
    snap["record_count"] = len(records)
    OUT_PATH.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} records -> {OUT_PATH}")
    print(f"platform: {plat_before}/{total} ({plat_before/total:.1%}) -> "
          f"{plat_after}/{total} ({plat_after/total:.1%}); multi-family={multi_family}")
    print(f"collection_doi: {doi_before}/{total} ({doi_before/total:.1%}) -> "
          f"{doi_after}/{total} ({doi_after/total:.1%})")
    print(f"detail failures: {n_detail_fail}/{len(todo)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（0=全量）")
    ap.add_argument("--detail", type=int, default=1, choices=(0, 1),
                     help="1=逐条拉详情富化 species/tissue/disease/chemistry（默认）；0=只用搜索命中+content 正则，快速自测")
    ap.add_argument("--enrich-existing", action="store_true",
                     help="surgical 模式：只补 platform + collection_doi 进现有快照，不重抓其余字段")
    args = ap.parse_args()
    if args.enrich_existing:
        return enrich_existing_snapshot(limit=args.limit)
    detail_enabled = bool(args.detail)

    # ---- 阶段一：搜索分页，收集命中（按 accession 去重） ----
    print(f"searching {SEARCH_API} query={QUERY!r} ...")
    hits: dict[str, dict] = {}
    total_hits = None
    page = 1
    while page <= MAX_PAGES:
        q = urllib.parse.quote(QUERY)
        url = f"{SEARCH_API}?query={q}&pageSize={PAGE_SIZE}&page={page}"
        try:
            payload = fetch(url)
        except Exception as exc:  # noqa: BLE001
            print(f"SEARCH_FAILED (page {page}): {type(exc).__name__}: {exc}")
            break
        if total_hits is None:
            total_hits = payload.get("totalHits")
            print(f"  totalHits={total_hits}")
        page_hits = payload.get("hits")
        if not isinstance(page_hits, list) or not page_hits:
            break
        for h in page_hits:
            if not isinstance(h, dict):
                continue
            acc = str(h.get("accession") or "").strip()
            if not acc or acc in hits:
                continue
            hits[acc] = h
        print(f"  page {page}: {len(hits)} unique hits so far")
        if args.limit and len(hits) >= args.limit:
            break
        if total_hits and len(hits) >= total_hits:
            break
        page += 1

    # ---- 阶段二：逐条详情富化（可选）→ 归一成记录 schema ----
    records: list[dict] = []
    seen: set[str] = set()
    n_detail_ok = 0
    n_detail_fail = 0
    total = len(hits)
    for i, (acc, hit) in enumerate(hits.items(), 1):
        detail = None
        if detail_enabled:
            detail = fetch_detail(acc)
            if detail is not None:
                n_detail_ok += 1
            else:
                n_detail_fail += 1
            time.sleep(0.2)  # 礼貌限速

        rec = to_record(hit, detail)
        if rec is None:
            continue
        uid = rec["dataset_uid"]
        if uid in seen:
            continue
        seen.add(uid)
        records.append(rec)

        if i % 50 == 0 or i == total:
            print(f"  detail {i}/{total}: {len(records)} records so far "
                  f"(detail ok={n_detail_ok} fail={n_detail_fail})")
        if args.limit and len(records) >= args.limit:
            break

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload_out = {
        "source": SOURCE_LABEL,
        "source_url": SEARCH_API,
        "note": "离线快照·外部平台库（opt-in）。不并入官方评测语料。"
                "has_raw_data 保守置 False；count 无研究级细胞数故留空。",
        "record_count": len(records),
        "records": records,
    }
    OUT_PATH.write_text(json.dumps(payload_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} records -> {OUT_PATH}")

    n_tissue = sum(1 for r in records if r["tissue"])
    specs: dict[str, int] = {}
    for r in records:
        for s in str(r["species"]).split(","):
            s = s.strip()
            if s:
                specs[s] = specs.get(s, 0) + 1
    print(f"detail ok={n_detail_ok} fail={n_detail_fail}")
    print(f"tissue non-empty: {n_tissue}/{len(records)}")
    print("species:", ", ".join(f"{k}={v}" for k, v in sorted(specs.items(), key=lambda kv: -kv[1])[:12]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
