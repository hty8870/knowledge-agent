"""demo 领域包：校内课程知识库（骨肉分离的最小第二领域证明）。

这是一个**外部领域包**——不在骨架源码树里，经 `BIODATA_DOMAIN_DIR` 指向本目录
按文件位置装载（`domain.registry._load_external`）。它证明：换领域不改骨架一行
代码，同一条 parse → retrieve 管线照常工作。领域数据刻意放 `vocab_data.py` 并以
**包内相对导入**引用（外部装载必须支持多文件包）。

- 维度：course（课程，子串）/ instructor（教师，子串）/ semester（学期，精确等值）。
- 记录：直接构造骨架公共记录容器 DatasetRecord，维度取值放 `facets` 字典
  （biodata 的具名字段一条不用——证明记录容器与维度语义解耦）。
"""
from __future__ import annotations

from dataset_recommender.domain.descriptors import MATCH_EXACT, MATCH_SUBSTRING, DimensionDescriptor
from dataset_recommender.domain.pack import DomainBranding, DomainPack
from dataset_recommender.retrieval.normalizer import DatasetRecord

from .vocab_data import CATALOG, RECORD_ROWS

DIMENSIONS: tuple[DimensionDescriptor, ...] = (
    DimensionDescriptor(
        "course", "课程", lambda r: (r.facets or {}).get("course", ""), MATCH_SUBSTRING,
        facet_text=lambda r: (r.facets or {}).get("course", ""), casefold=True,
    ),
    DimensionDescriptor(
        "instructor", "教师", lambda r: (r.facets or {}).get("instructor", ""), MATCH_SUBSTRING,
        facet_text=lambda r: (r.facets or {}).get("instructor", ""), casefold=True,
    ),
    DimensionDescriptor(
        "semester", "学期", lambda r: (r.facets or {}).get("semester", ""), MATCH_EXACT,
        facet_text=lambda r: (r.facets or {}).get("semester", ""),
    ),
)


def _mk(name: str, facets: dict, desc: str = "") -> DatasetRecord:
    return DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="",
        count="", unit="", has_raw_data=None, url="", source_file="",
        description=desc, raw={"source": "校内课程库", "published_date": "2024-09-01"},
        facets=facets,
    )


def demo_records() -> list[DatasetRecord]:
    """demo 语料（6 条课程记录）。真实领域包会把这里换成自己的语料装载钩子（corpus_loader，v2）。"""
    return [_mk(name, facets, desc) for name, facets, desc in RECORD_ROWS]


def get_pack() -> DomainPack:
    return DomainPack(
        domain_id="course_kb",
        name_zh="CourseKB Agent",
        tagline_zh="校内课程知识库检索助手",
        dimensions=DIMENSIONS,
        catalog=CATALOG,
        dim_labels_zh={"course": "课程", "instructor": "教师", "semester": "学期"},
        branding=DomainBranding(
            product_name="CourseKB Agent",
            page_title="CourseKB Agent · 课程检索",
            dataset_page_title="课程详情 · CourseKB Agent",
            mcp_server_name="coursekb",
            download_dir_prefix="CourseKB资料",
            corpus_blurb_zh="",
        ),
        base_source="校内课程库",
        facet_order=("source", "course", "instructor", "semester", "year"),
        explain_order=("course", "instructor", "semester"),
        raw_data_labels=("有附件", "无附件"),
        completeness_fields=("course", "instructor", "semester"),
        vocab=None,
    )
