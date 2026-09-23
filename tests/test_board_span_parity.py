# -*- coding: utf-8 -*-
"""条件板的地基门：位置标注与真实解析必须说同一件事。

条件板要在用户原句上「把物种那一段换掉」，前提是它标出来的位置与 `parse_query` 真正落到维度上的
那一段是同一段。这两条路径在实现上不同构（正向解析按长度降序逐个 replace 消费，标注按位置占位），
所以一致性不能靠推理，只能靠量化。

本文件是施工序列的第 0 步（Go/No-Go）：一致率不达标就停下来重估设计，
**绝不为了让测试变绿去改 `parse_query` 或 `resolve_search_request`** —— 它们在冻结评测的热路径上。
"""
import re

from dataset_recommender.retrieval import search_request as SR
from dataset_recommender.retrieval.query_parser import (
    PS_EXECUTABLE,
    annotate_query_spans,
    parse_query,
)

DIMS = ("species", "tissue", "disease", "platform", "assay", "modality")

KNOWN_SOURCES = [
    "10x Genomics",
    "CELLxGENE Discover",
    "Human Cell Atlas",
    "EBI Single Cell Expression Atlas",
    "ArrayExpress",
]

# 覆盖：单维 / 多维 / 嵌套 alias（肺 vs 肺癌）/ 否定 / 原始数据 / 相对与绝对时间 / 平台 / 技术 /
# 单字 alias 被吃进普通词（成人的「人」）/ 英文 / 客套话
ENTITY_QUERIES = (
    "找人类肺组织的单细胞数据",
    "小鼠脑的单细胞转录组",
    "人类肺癌的单细胞数据",
    "成人肺组织单细胞",
    "近三年的小鼠肾脏数据",
    "2022年以后的人类心脏数据",
    "需要有 FASTQ 的人类乳腺数据",
    "不要肿瘤的人类肺数据",
    "Visium 空间转录组 小鼠",
    "大鼠脑单细胞",
    "斑马鱼胚胎单细胞",
    "ATAC 人类免疫细胞",
    "2020年到2022年的人类胰腺数据",
    "去年的小鼠皮肤数据",
    "人类血液单细胞",
    "Xenium 人类乳腺癌",
    "帮我找一些人类肠道的单细胞数据集",
    "猕猴视网膜单细胞",
    "果蝇胚胎数据",
    "human lung single cell",
)

SOURCE_QUERIES = (
    "人类细胞图谱的肺组织数据",
    "cellxgene 的小鼠肾脏",
    "CELLxGENE Discover 人类心脏",
    "ArrayExpress 里的人类皮肤数据",
    "10x Genomics 官方的乳腺数据",
    "EBI SCEA 拟南芥",
    "single cell expression atlas 水稻",
    "非小细胞肺癌的人类数据",
    "Human Cell Atlas 的小鼠脑",
    "细胞图谱cellxgene 免疫细胞",
    "tenx genomics 小鼠肝脏",
)


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def test_span_dims_match_parse_query_dims_on_every_executable_query():
    """实体口径：标注出来的维度集合 == parse_query 真正落到的维度集合。

    只在可执行查询上比（弃权句本来就没有条件可编辑）。这条断言就是 Go/No-Go 本身：
    不一致率上来了，说明「按位置改写」这个前提不成立，要停下来重估，而不是放宽断言。
    """
    checked = 0
    mismatches = []
    for query in ENTITY_QUERIES:
        intent = parse_query(query)
        if intent.parse_status != PS_EXECUTABLE:
            continue
        checked += 1
        span_dims = {s["dim"] for s in annotate_query_spans(query) if s["dim"] in DIMS}
        parse_dims = {d for d, v in intent.constraints.items() if v}
        parse_dims |= {d for d, v in intent.excluded_constraints.items() if v}
        if span_dims != parse_dims:
            mismatches.append((query, sorted(span_dims), sorted(parse_dims)))
    assert checked >= 15, f"可执行样本太少（{checked}），这条门就失去意义了"
    assert not mismatches, f"标注与解析的维度口径不一致：{mismatches}"


def test_span_display_names_match_parse_query_display_names():
    """连 display 名也要对得上——只对维度不对取值，预览里会写出「把物种从 X 换成 Y」里的错 X。"""
    for query in ENTITY_QUERIES:
        intent = parse_query(query)
        if intent.parse_status != PS_EXECUTABLE:
            continue
        by_dim: dict[str, set] = {}
        for span in annotate_query_spans(query):
            if span["dim"] in DIMS:
                by_dim.setdefault(span["dim"], set()).add(span["display"])
        for dim, displays in intent.display_map.items():
            if not displays:
                continue
            assert by_dim.get(dim, set()) >= set(displays), (
                f"{query!r} 的 {dim} 维 display 对不上：标注 {by_dim.get(dim)} vs 解析 {displays}"
            )


def test_masked_query_equals_server_parsed_query_for_every_source_alias():
    """来源口径：遮蔽掉来源专名之后的句子，必须与服务端真正拿去解析的那句一致。

    这条不一致，条件板就会在一份与服务端不同的句子上做定位与改写——最坏形态是把专名打碎、
    静默换掉检索范围，而 QueryIntent 里根本没有 sources 这一维，事后校验看不见。
    """
    mismatches = []
    for query in SOURCE_QUERIES:
        resolution = SR.resolve_search_request(query, None, KNOWN_SOURCES, auto_parse_sources=True)
        masked = SR.mask_source_spans(query)
        if _norm_ws(masked) != _norm_ws(resolution.parsed_query):
            mismatches.append((query, _norm_ws(masked), _norm_ws(resolution.parsed_query)))
    assert not mismatches, f"遮蔽结果与服务端 parsed_query 不一致：{mismatches}"


def test_masking_preserves_length_and_indices():
    """遮蔽必须等长——索引一旦漂移，后面所有「在第 N 个字上动手」的逻辑全部错位。"""
    for query in SOURCE_QUERIES:
        masked = SR.mask_source_spans(query)
        assert len(masked) == len(query)
        for span in SR.source_alias_spans(query):
            assert masked[span["start"]:span["end"]].strip() == ""


def test_source_alias_spans_does_not_fire_on_non_source_text():
    """「非小细胞肺癌」里没有任何来源专名——误命中会让一整段疾病名被当成来源保护区。"""
    assert SR.source_alias_spans("非小细胞肺癌的人类数据") == []
    assert SR.source_alias_spans("") == []


def test_source_alias_spans_respects_known_sources_filter():
    query = "cellxgene 的小鼠肾脏"
    assert SR.source_alias_spans(query, ["CELLxGENE Discover"])
    assert SR.source_alias_spans(query, ["10x Genomics"]) == []
