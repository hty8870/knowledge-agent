# -*- coding: utf-8 -*-
from dataset_recommender.retrieval.search_request import resolve_search_request


KNOWN = [
    "10x Genomics",
    "CELLxGENE Discover",
    "Human Cell Atlas",
    "EBI Single Cell Expression Atlas",
    "ArrayExpress",
    "ENCODE",
]


def test_auto_detects_source_and_removes_only_source_term():
    r = resolve_search_request("近三年的 CELLxGENE 人类肝脏数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources == ["CELLxGENE Discover"]
    assert r.source_mode == "auto_detected"
    assert "CELLxGENE" not in r.parsed_query
    assert "近三年" in r.parsed_query and "人类肝脏" in r.parsed_query


def test_auto_without_mention_keeps_caller_pool():
    pool = ["10x Genomics", "CELLxGENE Discover"]
    r = resolve_search_request("人类肺癌", pool, KNOWN, auto_parse_sources=True)
    assert r.sources == pool and r.parsed_query == "人类肺癌"
    assert r.source_mode == "explicit"


def test_negation_guard_never_reverses_source_intent():
    r = resolve_search_request("除了 10x Genomics 以外的人类数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources is None and r.parsed_query == r.original_query
    assert r.automatic_skipped_reason == "source_negation_guard"


def test_legal_non_entity_does_not_block_source_detection():
    r = resolve_search_request(
        "CELLxGENE 的非小细胞肺癌单细胞数据", None, KNOWN, auto_parse_sources=True
    )
    assert r.sources == ["CELLxGENE Discover"]
    assert r.source_mode == "auto_detected"
    assert "非小细胞肺癌" in r.parsed_query


def test_explicit_pool_conflict_is_not_silently_rewritten():
    r = resolve_search_request(
        "CELLxGENE 人类数据", ["10x Genomics"], KNOWN, auto_parse_sources=True
    )
    assert r.sources == ["10x Genomics"]
    assert r.parsed_query == r.original_query
    assert r.source_mode == "explicit_conflict"


def test_ascii_aliases_use_token_boundaries():
    r = resolve_search_request("microarray expression study", None, KNOWN, auto_parse_sources=True)
    assert r.detected_sources == [] and r.parsed_query == r.original_query


def test_source_proper_noun_words_are_not_promoted_to_constraints():
    """来源专名内嵌的通用词（Human Cell Atlas→human、Single Cell Expression Atlas→single cell）
    随整条 alias 去掉，**不得**被提升成硬过滤维度约束。实证：HCA 语料 532 条里 22 条 species 不含
    'human'（HCA 有少量非人参考数据）；若把内嵌 human 当 species=human，会静默漏掉这 22 条合法记录。
    故去词后 parsed_query 不得残留会触发 species/modality 约束的词（验证结论：保持整词去掉）。"""
    r = resolve_search_request("Human Cell Atlas 的数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources == ["Human Cell Atlas"] and r.source_mode == "auto_detected"
    assert "human" not in r.parsed_query.lower() and "atlas" not in r.parsed_query.lower()

    r2 = resolve_search_request("EBI Single Cell Expression Atlas 数据", None, KNOWN, auto_parse_sources=True)
    assert r2.sources == ["EBI Single Cell Expression Atlas"]
    assert "single cell" not in r2.parsed_query.lower()


def test_separately_specified_dimension_survives_source_removal():
    """去掉来源专名不影响句中**另外**点名的维度词：'Human Cell Atlas 的小鼠脑数据' 去掉 HCA 专名后，
    '小鼠'/'脑' 完整保留 → 用户对该来源加的额外约束不被吞（防止未来把去词改成粗暴的按维度词删）。"""
    r = resolve_search_request("Human Cell Atlas 的小鼠脑数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources == ["Human Cell Atlas"]
    assert "小鼠" in r.parsed_query and "脑" in r.parsed_query


def test_manual_mode_is_exact_no_op():
    r = resolve_search_request(
        "CELLxGENE 人类数据", ["10x Genomics"], KNOWN, auto_parse_sources=False
    )
    assert r.sources == ["10x Genomics"] and r.parsed_query == r.original_query
    assert r.automatic_requested is False


def test_encode_project_alias_detects_and_removes_source_term():
    r = resolve_search_request("ENCODE project 的人类 ATAC-seq 数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources == ["ENCODE"] and r.source_mode == "auto_detected"
    assert "encode" not in r.parsed_query.lower()
    assert "人类" in r.parsed_query and "ATAC-seq" in r.parsed_query


def test_encodeproject_compact_alias_detects():
    r = resolve_search_request("encodeproject 的小鼠 RNA 数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources == ["ENCODE"]
    assert "encodeproject" not in r.parsed_query.lower()
    assert "小鼠" in r.parsed_query


def test_preferred_encode_project_weights_without_narrowing():
    r = resolve_search_request("优先 ENCODE project 的人类数据", None, KNOWN, auto_parse_sources=True)
    assert r.preferred_sources == ["ENCODE"]
    assert r.sources is None                       # 偏好不收窄来源池
    assert "encode" not in r.parsed_query.lower()  # 专名连同「优先」一起去掉


def test_encode_project_negation_guard_never_reverses_intent():
    r = resolve_search_request("除了 ENCODE project 的人类数据", None, KNOWN, auto_parse_sources=True)
    assert r.sources is None
    assert r.automatic_skipped_reason == "source_negation_guard"


def test_bare_encode_verb_is_not_a_source():
    """裸 "encode" 是普通英文动词——刻意不收，撞词查询不得检测到任何来源。"""
    r = resolve_search_request(
        "factors that encode chromatin regulators in human cells", None, KNOWN, auto_parse_sources=True
    )
    assert r.detected_sources == []
    assert r.sources is None                       # 不窄化
    assert r.parsed_query == r.original_query


def test_10x_followed_by_assay_word_is_not_a_source():
    """ 「10X Multiome」「10X ATAC」是试剂盒名（assay），不是 10x 数据来源——
    裸 "10x" 曾把它们误判成点名来源（检索池收窄到 10x，其它库的同 assay 数据全丢；
    agent 侧还被 _named_source_violation 错逼填 10x）。护栏：词边界后紧跟 vocabulary
    assay 维词时不算来源专名（做进 _token_pattern，两个消费点逐位同构）。"""
    for phrase in ("10X Multiome 的人类肺癌数据", "10X ATAC-seq 数据", "10x multiome 小鼠脑"):
        r = resolve_search_request(phrase, None, KNOWN, auto_parse_sources=True)
        assert r.detected_sources == [], phrase
        assert r.sources is None, phrase


def test_bare_10x_without_assay_word_still_names_the_source():
    """护栏不许误伤来源读法：裸「10x」（后接中文/虚字/句尾）仍是点名 10x 来源。"""
    for phrase in ("检查10x是否有更新", "来自10x的乳腺癌数据", "10x 的数据"):
        r = resolve_search_request(phrase, None, KNOWN, auto_parse_sources=True)
        assert r.detected_sources == ["10x Genomics"], phrase


def test_10x_cjk_assay_word_keeps_source_reading():
    """CJK assay 别名刻意不进护栏（「10x 的多组学数据」的「10x」仍按来源读）——
    歧义句往「不静默改检索池」的保守方向倒。"""
    r = resolve_search_request("10x 的多组学数据", None, KNOWN, auto_parse_sources=True)
    assert r.detected_sources == ["10x Genomics"]
