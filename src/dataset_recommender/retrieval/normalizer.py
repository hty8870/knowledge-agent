from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from ..corpus.data_loader import RawRecord
from ..corpus.downloads import has_fastq as _fastq_actual
from ..corpus.sample_supplement import count_fill as _supp_count_fill, gene_count as _supp_gene_count
from ..domain.registry import get_domain as _get_domain


def _derive_vocab():
    """领域词表句柄（派生规则随活动领域包；biodata = `domains/biodata/vocabulary.py`）。

    缺对应函数的领域包优雅降级为空串——biodata 包三个函数齐全，行为逐位不变。
    """
    return _get_domain().vocab

# ---------------------------------------------------------------- 缺失哨兵（单一真源）

#: 数据源用来表达「本字段未标注」的字面量。**它们不是取值，是「没有取值」的另一种写法。**
#:
#: 单一真源（收敛）：此前同一概念在仓库里有**三套**互不相同的定义 ——
#: `introduction._MISSING_TEXT`（8 个英文词）、`retriever._FACET_SKIP_VALUES`（含中文「未说明/未知」）、
#: 以及 `retriever._dim_field_present`（**一个都不认，只判空字符串**）。第三套的代价是实测出来的：
#: 冻结 base 767 条里有 **298 条（38.9%）disease 写着 "unknown"/"n/a"**，诚实降级层把它们当成「已核验
#: 的真不匹配」→ 搜「肿瘤」时 caveat 恒空、点「也纳入未标注疾病的数据」一条都捞不回，而同一界面的
#: 卡片对同一条记录显示「疾病：未说明」。两个模块对同一条记录给出互相矛盾的结论。
#:
#: 收敛安全性（改前实测）：只被 `_MISSING_TEXT` 收、`_FACET_SKIP_VALUES` 漏的 4 个拼法
#: （`-` / `not provided` / `not specified` / `null`）在全部 5667 条记录的 6 个维度上**一次都没出现**
#: → 把分面口径并入本集合对现有数据是 no-op。
#:
#: 注意这里**不做**的事：不在归一阶段把哨兵抹成空字符串。`record.disease` 是展示字段、也进
#: `_field_contains` 的匹配文本，改它会动到冻结 767 的评分路径。哨兵语义只在**判定「该维是否已标注」**
#: 时生效，那条路径（`_dim_field_present`）结构上不被评测调用。
MISSING_VALUE_TOKENS: frozenset[str] = frozenset({
    "unknown",
    "not specified",
    "not provided",
    "n/a",
    "na",
    "none",
    "null",
    "-",
    "未说明",
    "未知",
})


def is_missing_value(text: object) -> bool:
    """该元数据文本是否等同于「未标注」（空、纯空白，或一个缺失哨兵）。

    只做 strip + casefold 后的**整串**比较：不做子串匹配，否则 `"unknown syndrome"`（一个真实病名）
    会被误判成未标注。
    """
    s = str(text or "").strip()
    return not s or s.lower() in MISSING_VALUE_TOKENS


@dataclass(slots=True)
class DatasetRecord:
    dataset_name: str
    species: str
    tissue: str
    disease: str
    chemistry: str
    count: str
    unit: str
    has_raw_data: bool | None
    url: str
    source_file: str
    description: str
    raw: dict[str, Any]
    # 派生字段（重构新增）
    platform_family: str = ""   # visium / xenium / chromium / atera —— 由 raw['platform'] 归一
    assay: str = ""             # atac / multiome / flex / cnv / gex ... —— 由 chemistry 归一
    modality: str = ""          # single-cell / spatial / "" —— 由 platform_family(+外部 chemistry) 派生
    family_id: str = ""         # 同一数据集家族的稳定标识（仅由标题根派生，与查询无关）
    gene_count: str = ""        # 检测基因数（10x 平台信息补充旁挂表；仅展示，不进检索/评分）
    # 通用分面容器（骨肉分离 additive）：非 biodata 领域包的维度取值放这里，
    # 领域包的 DimensionDescriptor 访问器按 key 读取；biodata 路径恒为空 dict、逐位不变。
    facets: dict[str, str] = field(default_factory=dict)

    @property
    def searchable_text(self) -> str:
        return " | ".join(
            [
                self.dataset_name,
                self.species,
                self.tissue,
                self.disease,
                self.chemistry,
                self.description,
            ]
        ).lower()


# 家族标识：只从标题根派生（剥离 section/replicate/sample/block 等切片后缀），
# 不掺入 species/tissue/disease 等查询相关属性——否则会把同一数据集拆成多个"家族"。
_FAMILY_SUFFIX_PATTERNS = [
    re.compile(r"\bblock\s+[a-z0-9]+\s+section\s+[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\bsection\s+[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\breplicate\s+[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\brep\s*[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\bsample\s+[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\bslide\s+[a-z0-9]+\b", re.IGNORECASE),
    re.compile(r"\b(rep|section|sample)[\s_-]*\d+\b", re.IGNORECASE),
]


def derive_family_id(dataset_name: str) -> str:
    name = (dataset_name or "").lower()
    for pattern in _FAMILY_SUFFIX_PATTERNS:
        name = pattern.sub(" ", name)
    name = re.sub(r"[^a-z0-9]+", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or (dataset_name or "").strip().lower()


FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "dataset_name": (
        "dataset_name",
        "name",
        "title",
        "dataset_title",
        "dataset",
    ),
    "species": ("species", "organism", "sample_species", "host"),
    "tissue": ("tissue", "organ", "tissue_type", "anatomical_site"),
    "disease": ("disease", "condition", "diagnosis", "phenotype", "disease_state"),
    "chemistry": (
        "chemistry",
        "technology",
        "assay",
        "platform",
        "library_prep",
        "sequencing_technology",
    ),
    "count": (
        "total_records",
        "count",
        "sample_size",
        "n_cells",
        "n_spots",
        "number_of_cells",
        "number_of_spots",
        "cells",
        "spots",
    ),
    "unit": ("unit", "count_unit", "measurement_unit"),
    "has_raw_data": ("has_raw_data", "raw_data", "raw_fastq", "has_fastq", "fastq_available"),
    "url": ("url", "download_url", "link", "dataset_url", "source_url"),
    "description": ("description", "summary", "desc", "abstract", "notes"),
}


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _flatten_values(payload: Any, parent_key: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            compound_key = f"{parent_key}.{key}" if parent_key else str(key)
            flattened[compound_key] = value
            flattened.update(_flatten_values(value, compound_key))
    elif isinstance(payload, list):
        if payload and all(not isinstance(item, (list, dict)) for item in payload):
            flattened[parent_key] = ", ".join(str(item) for item in payload)
    return flattened


def _extract_value(record: dict[str, Any], field_name: str) -> Any:
    candidates = FIELD_CANDIDATES[field_name]
    lower_record = {str(k).lower(): v for k, v in record.items()}
    for candidate in candidates:
        if candidate in lower_record:
            return lower_record[candidate]

    flattened = _flatten_values(record)
    normalized_map = {_normalize_key(k): v for k, v in flattened.items()}
    for candidate in candidates:
        normalized = _normalize_key(candidate)
        if normalized in normalized_map:
            return normalized_map[normalized]
    return None


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        stripped = value.strip()
        return "" if stripped.lower() in {"none", "null", "nan"} else stripped
    return str(value).strip()


def _normalize_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _normalize_unit(value: Any) -> str:
    text = _stringify(value).lower()
    if not text:
        return ""
    if text in {"cells", "cell"}:
        return "Cells"
    if text in {"spots", "spot"}:
        return "Spots"
    if text in {"nuclei", "nucleus"}:
        return "Nuclei"
    return text.title()


def _parse_count_from_name(name: str) -> str:
    """
    Parse shorthand counts from dataset names, such as 7.5k / 10k.
    """
    match = re.search(r"(\d+(?:\.\d+)?)\s*[kK]\b", name)
    if not match:
        return ""
    number = float(match.group(1)) * 1000
    return str(int(number))


def _normalize_count(value: Any, dataset_name: str) -> str:
    text = _stringify(value)
    if text:
        compact = text.replace(",", "")
        if compact.isdigit():
            return compact
        return text
    return _parse_count_from_name(dataset_name)


def normalize_dataset_record(raw_record: dict[str, Any], source_file: str) -> DatasetRecord:
    dataset_name = _stringify(_extract_value(raw_record, "dataset_name"))
    species = _stringify(_extract_value(raw_record, "species"))
    tissue = _stringify(_extract_value(raw_record, "tissue"))
    disease = _stringify(_extract_value(raw_record, "disease"))
    chemistry = _stringify(_extract_value(raw_record, "chemistry"))
    _raw_count = _extract_value(raw_record, "count")
    count = _normalize_count(_raw_count, dataset_name)
    unit = _normalize_unit(_extract_value(raw_record, "unit"))
    has_raw_data = _normalize_bool(_extract_value(raw_record, "has_raw_data"))
    # 阶段二实测更正：索引对 99 条（多为 Xenium 成像平台，物理上无 FASTQ）过度声称 has_raw_data。
    # 用更正信号 has_raw_data_actual 覆盖，令过滤/显示/评测口径统一为事实；下载数据缺失则保留索引值（降级）。
    _uid = raw_record.get("dataset_uid") if isinstance(raw_record, dict) else None
    _actual = _fastq_actual(_uid)
    if _actual is not None:
        has_raw_data = _actual
    url = _stringify(_extract_value(raw_record, "url"))
    # 10x 平台信息补充（2026-08 手工整理，sample_supplement 旁挂表）：只补缺、不覆盖——
    # ① count：base 显式值 > 补充表 > 标题缩写兜底。补充表（官方页数值）优先于标题 k 缩写猜测——
    #    实测 "Xenium Prime 5K" 会被标题兜底误读成 5000 Cells（那是产品名不是细胞数）；
    # ② gene_count 是 base 没有的新字段，纯展示（不进 searchable_text / 评分 / 冻结评测路径）。
    _supp_key = _uid or url
    if _supp_count_fill(_supp_key) and not _stringify(_raw_count):
        count = _supp_count_fill(_supp_key)
    gene_count = _supp_gene_count(_supp_key)
    description = _stringify(_extract_value(raw_record, "description"))

    platform_raw = _stringify(raw_record.get("platform")) if isinstance(raw_record, dict) else ""
    _v = _derive_vocab()
    platform_family = getattr(_v, "derive_platform_family", lambda _x: "")(platform_raw)
    assay = getattr(_v, "derive_assay", lambda _x: "")(chemistry)
    modality = getattr(_v, "derive_modality", lambda _p, _c: "")(platform_family, chemistry)
    family_id = derive_family_id(dataset_name)
    # additive（骨肉分离）：透传原始记录自带的 facets 字典（非 biodata 领域包的维度取值容器；
    # 形状非法一律保守为空 dict——biodata 语料无此键，行为逐位不变）。
    _raw_facets = raw_record.get("facets") if isinstance(raw_record, dict) else None
    facets = {str(k): str(v) for k, v in _raw_facets.items()} if isinstance(_raw_facets, dict) else {}

    return DatasetRecord(
        dataset_name=dataset_name,
        species=species,
        tissue=tissue,
        disease=disease,
        chemistry=chemistry,
        count=count,
        unit=unit,
        has_raw_data=has_raw_data,
        url=url,
        source_file=source_file,
        description=description,
        raw=raw_record,
        platform_family=platform_family,
        assay=assay,
        modality=modality,
        family_id=family_id,
        gene_count=gene_count,
        facets=facets,
    )


def normalize_records(raw_records: Iterable[RawRecord]) -> list[DatasetRecord]:
    normalized: list[DatasetRecord] = []
    for raw in raw_records:
        if not isinstance(raw.record, dict):
            continue
        normalized.append(normalize_dataset_record(raw.record, raw.source_file))
    return normalized

