"""外部平台库 + 按来源自由勾选 + 确定性保护。

核心不变量：
- sources=None（官方评测/CLI 默认）→ 只装基础语料，绝不并入外部平台库 → 确定性不受影响。
- sources=["10x Genomics"] → 与基础语料逐位一致。
- sources 含外部来源 → 并入对应快照，且基础语料一条不少。
- 外部记录经硬过滤：物种归一到通用名后可被 species 约束命中；has_raw_data=False → 「要 FASTQ」正确排除。
"""
import sys
from pathlib import Path

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import (
    BASE_SOURCE,
    EXTERNAL_DIR_NAME,
    available_sources,
    load_full_corpus,
    load_normalized_corpus,
    source_of,
)
from dataset_recommender.corpus.data_loader import load_raw_records
from dataset_recommender.retrieval.normalizer import normalize_records
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import DatasetRetriever, passes_hard_filter

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

CXG = "CELLxGENE Discover"


def _base():
    s = get_settings()
    return normalize_records(load_raw_records(s.data_dir)), s


def test_none_and_10x_only_equal_base():
    """sources=None 与 sources=['10x Genomics'] 都与基础语料逐位一致（确定性）。"""
    base, s = _base()
    none = load_normalized_corpus(s.data_dir, s.project_root, None)
    only10x = load_normalized_corpus(s.data_dir, s.project_root, [BASE_SOURCE])
    base_names = [r.dataset_name for r in base]
    assert [r.dataset_name for r in none] == base_names
    assert [r.dataset_name for r in only10x] == base_names
    assert all(source_of(r) == BASE_SOURCE for r in none)


def test_external_only_and_both():
    """仅外部来源 → 全是该来源；base+单个外部 → 基础语料一条不少 + 恰好并入该来源。
    注：库中现有多个外部平台（CELLxGENE/HCA/EBI…），故 base+CXG 只是 full 的**子集**，
    full = base + 全部外部（见 test_full_corpus_is_base_plus_all_external）。
    运行时入库口径（钉更新）：curate sync 的 upload_*.json 住在外部目录、
    归属基础来源（source_of==10x Genomics）。外部目录只在「选择集含至少一个外部来源」
    时才装载（load_normalized_corpus 的既有结构），故这批记录只在 base+外部 的组合
    选择里并入；None 与 [10x] 单选保持冻结评测口径绝不并入（见
    test_none_and_10x_only_equal_base——该不变量优先级高于「10x 单选拿全 10x 数据」）。
    both 因此 = base + CXG + 基础来源运行时入库，期望值按外部目录实况动态算，不钉死数。"""
    base, s = _base()
    onlycxg = load_normalized_corpus(s.data_dir, s.project_root, [CXG])
    assert onlycxg, "应有 CELLxGENE 记录"
    assert all(source_of(r) == CXG for r in onlycxg)
    both = load_normalized_corpus(s.data_dir, s.project_root, [BASE_SOURCE, CXG])
    ext_dir = s.project_root / EXTERNAL_DIR_NAME
    ext_all = normalize_records(load_raw_records(ext_dir, lenient=True)) if ext_dir.exists() else []
    base_side_uploads = [r for r in ext_all if source_of(r) == BASE_SOURCE]
    assert len(both) == len(base) + len(onlycxg) + len(base_side_uploads)   # base + 单个外部 + 基础来源运行时入库：不多不少
    assert all(source_of(r) in {BASE_SOURCE, CXG} for r in both)
    assert {r.dataset_name for r in base} <= {r.dataset_name for r in both}


def test_full_corpus_is_base_plus_all_external():
    """load_full_corpus = 基础语料 + 全部外部平台库（浏览页「并列展示所有库」的后端保证）。
    随外部库增减而自适应：full 计数 == 各来源计数之和，且每个已声明来源都在 full 里出现。"""
    base, s = _base()
    full = load_full_corpus(s.data_dir, s.project_root)
    srcs = available_sources(s.data_dir, s.project_root)
    assert len(full) == sum(x["count"] for x in srcs)
    full_sources = {source_of(r) for r in full}
    assert {x["value"] for x in srcs} <= full_sources
    assert {r.dataset_name for r in base} <= {r.dataset_name for r in full}
    # 单选任一外部来源 → 与 full 里该来源的计数一致（成员资格按来路，不串库）
    for x in srcs:
        if x["value"] == BASE_SOURCE:
            continue
        only = load_normalized_corpus(s.data_dir, s.project_root, [x["value"]])
        assert len(only) == x["count"]
        assert all(source_of(r) == x["value"] for r in only)


def test_available_sources_lists_base_first():
    _, s = _base()
    srcs = available_sources(s.data_dir, s.project_root)
    assert srcs[0]["value"] == BASE_SOURCE
    values = {x["value"] for x in srcs}
    assert CXG in values
    assert all(x["count"] > 0 for x in srcs)


def test_external_record_passes_species_hard_filter():
    """物种归一：Homo sapiens→Human，故 species=human 约束能命中外部记录（否则会被静默滤掉）。"""
    _, s = _base()
    corpus = load_normalized_corpus(s.data_dir, s.project_root, [CXG])
    human_ext = [r for r in corpus if "human" in r.species.lower()]
    assert human_ext, "应存在物种归一为 Human 的外部记录"
    intent = parse_query("人类", s.keyword_mapping)
    assert passes_hard_filter(human_ext[0], intent)


def test_external_records_are_not_fastq():
    """CELLxGENE 资产是处理后矩阵（H5AD/RDS），非 FASTQ → 要 FASTQ 的查询必须排除它们。"""
    _, s = _base()
    ext = load_normalized_corpus(s.data_dir, s.project_root, [CXG])
    assert all(r.has_raw_data is not True for r in ext)
    intent = parse_query("要 FASTQ 的人类数据", s.keyword_mapping)
    if intent.has_raw_data_required is True:  # 词表解析出「要原始数据」约束时才有意义
        assert all(not passes_hard_filter(r, intent) for r in ext)


def test_retrieval_determinism_ignores_external_when_off():
    """同一查询：sources=None 的检索结果全部来自基础语料（外部库存在也不并入）。"""
    s = get_settings()
    r = DatasetRetriever(top_k=8)
    corpus_off = load_normalized_corpus(s.data_dir, s.project_root, None)
    intent = parse_query("人类乳腺癌", s.keyword_mapping)
    cands = r.retrieve(corpus_off, intent, top_k=8)
    assert cands, "基础语料应有人类乳腺癌结果"
    assert all(source_of(c.record) == BASE_SOURCE for c in cands)


def test_base_record_with_stray_source_labeled_by_load_path(tmp_path):
    """守 docstring 不变量：上传到 database/base/ 的记录即便自带 source 键，也按装载来路判定为 10x Genomics。
    否则 /api/sources 与 /api/datasets 计数会分歧、按来源筛选会张冠李戴。"""
    import json

    d = tmp_path / "database/base"
    d.mkdir(parents=True, exist_ok=True)
    (d / "x.json").write_text(
        json.dumps([{"dataset_name": "Stray Source Set", "species": "Human",
                     "source": "CELLxGENE Discover", "url": "https://example.org/x"}]),
        encoding="utf-8",
    )
    root = tmp_path  # 无 database/external → 外部为空

    corpus = load_normalized_corpus(d, root, [BASE_SOURCE])
    assert len(corpus) == 1
    assert source_of(corpus[0]) == BASE_SOURCE, "自带 source 键的基础记录必须被判为 10x Genomics"

    srcs = {s["value"]: s["count"] for s in available_sources(d, root)}
    assert srcs.get(BASE_SOURCE) == 1
    assert "CELLxGENE Discover" not in srcs, "基础语料不得凭内容凭空造出外部来源"

    # 选 CELLxGENE 时不应返回这条基础语料记录（成员资格按来路，不按内容）
    assert load_normalized_corpus(d, root, ["CELLxGENE Discover"]) == []


def test_ingest_organism_mapping():
    """锁定学名→通用名映射（回归守卫：改坏会让 species 硬过滤静默失效）。"""
    import ingest_cellxgene as ing

    assert ing.map_species(["Homo sapiens"]) == "Human"
    assert ing.map_species(["Mus musculus"]) == "Mouse"
    assert ing.map_species(["Danio rerio"]) == "Zebrafish"
    # 亚种三名法 → 退化到属+种再归一（否则 species 硬过滤把它们静默滤掉）
    assert ing.map_species(["Mus musculus musculus"]) == "Mouse"
    assert ing.map_species(["Mus musculus domesticus"]) == "Mouse"
    # 常见投稿拼写变体
    assert ing.map_species(["Homo Sapien"]) == "Human"
    # 受控三名法键仍按精确匹配（不被亚种回退误伤）
    assert ing.map_species(["Canis lupus familiaris"]) == "Dog"
    # 未知物种保留原名（保守：不误命中常见物种查询）
    assert ing.map_species(["Xenopus laevis"]) == "Xenopus laevis"
    assert ing.map_species(["Foo bar baz"]) == "Foo bar baz"
    assert ing.platform_hint(["10x 3' v3"]) == "Chromium"
    assert ing.platform_hint(["Visium Spatial Gene Expression V1"]) == "Visium"
