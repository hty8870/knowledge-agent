# -*- coding: utf-8 -*-
"""领域包架构验收测试（骨肉分离的「假分离否决器」）。

判据：换领域不改骨架一行代码——外部 demo 领域包（课程知识库）经
`BIODATA_DOMAIN_DIR` 装载后，同一条 parse → hard filter → rank 管线照常工作，
且维度语义（子串/精确等值）完全来自领域包声明。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from dataset_recommender.domain import registry as domain_registry
from dataset_recommender.domain import vocab_tools
from dataset_recommender.retrieval.query_parser import parse_query
from dataset_recommender.retrieval.retriever import (
    DatasetRetriever,
    constraint_satisfied,
    facet_value,
    passes_hard_filter,
    record_passes_facets,
)

DEMO_DIR = Path(__file__).resolve().parent / "fixtures" / "domains" / "course_kb"


def _load_demo_module():
    """经注册表的外部装载通道拿 demo 包模块（与生产同一入口——包内相对导入必须工作）。"""
    domain_registry._load_external(str(DEMO_DIR))
    prefix = f"_biodata_external_domain_{DEMO_DIR.name}_"
    for name, module in sys.modules.items():
        if name.startswith(prefix) and name.endswith(".pack"):
            return module
    raise AssertionError("demo 包未装载到 sys.modules")


@pytest.fixture()
def demo_domain(monkeypatch):
    """把活动领域切到外部课程库包；测试结束复位（缓存与拼音索引都清，防串域）。"""
    monkeypatch.setenv(domain_registry.ENV_DOMAIN_DIR, str(DEMO_DIR))
    monkeypatch.setenv(domain_registry.ENV_DOMAIN, "course_kb")
    domain_registry.reset_domain_cache()
    vocab_tools.reset_vocab_caches()
    yield domain_registry.get_domain()
    domain_registry.reset_domain_cache()
    vocab_tools.reset_vocab_caches()


def _biodata_pack_available() -> bool:
    """内置 biodata 领域包是否可装载（骨架仓不含肉时恒 False——相关用例跳过）。"""
    try:
        import importlib

        return importlib.util.find_spec("dataset_recommender.domains.biodata.pack") is not None
    except (ImportError, ValueError):
        return False


requires_biodata_pack = pytest.mark.skipif(
    not _biodata_pack_available(), reason="骨架仓无内置 biodata 领域包（肉在装配仓）"
)


@requires_biodata_pack
def test_default_domain_is_biodata(monkeypatch):
    """不设环境变量时解析为内置 biodata 包（旧行为兜底）。环境隔离：显式清掉两项领域环境变量。"""
    monkeypatch.delenv(domain_registry.ENV_DOMAIN_DIR, raising=False)
    monkeypatch.delenv(domain_registry.ENV_DOMAIN, raising=False)
    domain_registry.reset_domain_cache()
    pack = domain_registry.get_domain()
    assert pack.domain_id == "biodata"
    assert pack.dimension_names() == ("species", "tissue", "disease", "platform", "assay", "modality")
    assert pack.base_source == "10x Genomics"
    assert "species" in pack.catalog and pack.catalog["species"]
    domain_registry.reset_domain_cache()


def test_external_pack_loads(demo_domain):
    pack = demo_domain
    assert pack.domain_id == "course_kb"
    assert pack.dimension_names() == ("course", "instructor", "semester")
    assert pack.dim_labels_zh["course"] == "课程"
    desc = pack.descriptor_for("semester")
    assert desc is not None and desc.match == "exact"


def test_external_pack_supports_intra_package_relative_imports(demo_domain):
    """外部包必须支持**包内**相对导入（course_kb 的 `from .vocab_data import`
    已由本夹具全部用例覆盖；这里显式钉子模块确经包上下文装载）。"""
    assert any(
        name.startswith(f"_biodata_external_domain_{DEMO_DIR.name}_") and name.endswith(".vocab_data")
        for name in sys.modules
    )


def test_same_named_external_packs_at_different_paths_are_isolated(tmp_path):
    """不同路径的同名外部包命名空间必须隔离，且重载不串旧子模块（A→B→A 三态探针）。"""
    import textwrap

    def _make_pack(parent: Path, marker: str) -> Path:
        pkg_dir = parent / "reviewpack"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "data.py").write_text(f'MARKER = "{marker}"\n', encoding="utf-8")
        (pkg_dir / "pack.py").write_text(
            textwrap.dedent(
                """\
                from dataset_recommender.domain.descriptors import DimensionDescriptor
                from dataset_recommender.domain.pack import DomainBranding, DomainPack
                from .data import MARKER

                def get_pack():
                    return DomainPack(
                        domain_id=f"reviewpack_{MARKER}",
                        name_zh="reviewpack",
                        tagline_zh="probe",
                        dimensions=(DimensionDescriptor("x", "X", lambda r: ""),),
                        catalog={"x": []},
                        dim_labels_zh={"x": "X"},
                        branding=DomainBranding(
                            product_name="reviewpack",
                            page_title="reviewpack",
                            dataset_page_title="reviewpack",
                            mcp_server_name="reviewpack",
                            download_dir_prefix="reviewpack",
                        ),
                    )
                """
            ),
            encoding="utf-8",
        )
        return pkg_dir

    dir_a = _make_pack(tmp_path / "parentA", "A")
    dir_b = _make_pack(tmp_path / "parentB", "B")
    first = domain_registry._load_external(str(dir_a))
    second = domain_registry._load_external(str(dir_b))
    third = domain_registry._load_external(str(dir_a))
    assert (first.domain_id, second.domain_id, third.domain_id) == (
        "reviewpack_A", "reviewpack_B", "reviewpack_A",
    ), "同名不同路径的外部包命名空间串了"


def test_parse_query_extracts_demo_dimensions(demo_domain):
    intent = parse_query("数据结构 王老师")
    assert intent.parse_status == "executable", intent.abstain_detail
    assert set(intent.constraints["course"]) >= {"数据结构"}
    assert set(intent.constraints["instructor"]) >= {"王老师"}
    # biodata 维度一个都不许出现（词表不串域）
    assert "species" not in intent.constraints


def test_parse_query_fail_closed_carries_over(demo_domain):
    """未收录实义词照样 fail-closed（解析器机制与领域无关）。"""
    intent = parse_query("量子力学")
    assert intent.parse_status != "executable"
    assert intent.unresolved_terms


def _demo_records():
    return _load_demo_module().demo_records()


def test_hard_filter_and_rank_on_demo_domain(demo_domain):
    records = _demo_records()
    intent = parse_query("王老师 数据结构")
    survivors = [r for r in records if passes_hard_filter(r, intent)]
    assert [r.dataset_name for r in survivors] == ["数据结构（2024秋·王老师）"]
    ranked = DatasetRetriever(top_k=5).retrieve(records, intent)
    assert ranked[0].record.dataset_name == "数据结构（2024秋·王老师）"


def test_semester_exact_match_semantics(demo_domain):
    records = _demo_records()
    fall = next(r for r in records if r.dataset_name.startswith("数据结构（2024秋"))
    # 精确等值维：term 全等才过；子串/错学期一律淘汰
    assert constraint_satisfied(fall, "semester", ["2024-fall"])
    assert not constraint_satisfied(fall, "semester", ["2025-spring"])
    assert not constraint_satisfied(fall, "semester", ["fall"])
    # 子串维：targets 任一出现即过（真实约束携词表全部 targets）
    assert constraint_satisfied(fall, "course", ["data structures", "数据结构"])
    assert constraint_satisfied(fall, "course", ["数据结构"])


def test_mixed_constraints_and_facets_on_demo_domain(demo_domain):
    records = _demo_records()
    intent = parse_query("机器学习 2025春")
    survivors = [r for r in records if passes_hard_filter(r, intent)]
    assert [r.dataset_name for r in survivors] == ["机器学习（2025春·赵老师）"]
    rec = survivors[0]
    assert facet_value("course", rec) == "机器学习"
    assert facet_value("semester", rec) == "2025-spring"
    assert facet_value("source", rec) == "校内课程库"
    assert facet_value("year", rec) == "2024"
    assert record_passes_facets(rec, [{"dim": "instructor", "value": "赵老师"}])
    assert not record_passes_facets(rec, [{"dim": "instructor", "value": "王老师"}])


def test_catalog_driven_vocab_tools_follow_domain(demo_domain):
    terms = vocab_tools.suggestable_terms("course")
    displays = {t["display"] for t in terms}
    assert "机器学习" in displays
    index = vocab_tools.pinyin_alias_index()
    assert any("shujujiegou" in k for k in index)


def test_normalizer_preserves_facets_for_foreign_domain(demo_domain):
    """课程 JSON 经归一化后 facets 不丢、可直接进硬过滤管线（外域记录的 facets 透传钉）。"""
    from dataset_recommender.retrieval.normalizer import normalize_dataset_record

    rec = normalize_dataset_record(
        {
            "dataset_name": "数据结构（2024秋·王老师）",
            "facets": {"course": "数据结构", "instructor": "王老师", "semester": "2024-fall"},
            "description": "线性表。",
            "source": "校内课程库",
        },
        "course_kb.json",
    )
    assert rec.facets == {"course": "数据结构", "instructor": "王老师", "semester": "2024-fall"}
    intent = parse_query("数据结构 王老师")
    assert passes_hard_filter(rec, intent)


def test_sanitize_facet_filters_follows_active_domain(demo_domain):
    """换域后合法分面筛选不得被静默丢弃（分面白名单随活动领域运行时供给，不是 import 时快照）。"""
    from dataset_recommender.app.workflow import sanitize_facet_filters

    assert sanitize_facet_filters([{"dim": "course", "value": "数据结构"}]) == [
        {"dim": "course", "value": "数据结构"}
    ]
    assert sanitize_facet_filters([{"dim": "species", "value": "human"}]) == []


@requires_biodata_pack
def test_biodata_restored_after_demo(demo_domain, monkeypatch):
    """环境清掉+缓存复位后默认领域回到 biodata（测试隔离）。"""
    monkeypatch.delenv(domain_registry.ENV_DOMAIN_DIR, raising=False)
    monkeypatch.delenv(domain_registry.ENV_DOMAIN, raising=False)
    domain_registry.reset_domain_cache()
    pack = domain_registry.get_domain()
    assert pack.domain_id == "biodata"
    intent = parse_query("人类肺癌单细胞数据")
    assert intent.parse_status == "executable"
    assert "species" in intent.constraints
    domain_registry.reset_domain_cache()


@requires_biodata_pack
def test_assay_guard_words_follow_domain_after_roundtrip(demo_domain, monkeypatch):
    """assay 护栏词表按领域 catalog 重建：课程域初始化缓存后复位 biodata，试剂盒识别不串。"""
    from dataset_recommender.retrieval.search_request import _assay_guard_words

    _ = _assay_guard_words()  # 先在课程域初始化缓存（课程 catalog 无 assay 维）
    monkeypatch.delenv(domain_registry.ENV_DOMAIN_DIR, raising=False)
    monkeypatch.delenv(domain_registry.ENV_DOMAIN, raising=False)
    domain_registry.reset_domain_cache()
    words = _assay_guard_words()
    assert "multiome" in words and "atac" in words
    domain_registry.reset_domain_cache()
