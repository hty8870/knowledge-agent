# -*- coding: utf-8 -*-
"""体裁路由的回归网。

守的是一条方法论教训：**能读到事实就别猜**。第一版我按字符串形状写正则猜体裁，
实测当场打脸（base 的 ingest 模板 618 条被判成 prose）。现在按 source 判定 ——
description 长什么样取决于哪个 ingest 脚本写的，那些脚本是可读的事实。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.content import summary_genre as SG  # noqa: E402


def _item(source: str, description: str, name: str = "N") -> dict:
    return {"source": source, "description": description, "dataset_name": name}


# --------------------------------------------------------------- 逐源体裁（依据 ingest 契约）

def test_arrayexpress_is_prose_after_stripping_its_prefix() -> None:
    """AE 的 description = `{accession} · {assay} · {study description}`。

    剥掉前缀才看得见那段真散文（残余中位 401 字符）。体裁不是整串的属性。
    """
    d = ("E-MTAB-11814 · RNA-seq of coding RNA from single cells · "
         "A novel approach to single cell analysis to reveal intrinsic differences in immune "
         "marker expression in unstimulated macrophages derived from healthy donors.")
    v = SG.classify(_item("ArrayExpress", d))
    assert v["genre"] == SG.GENRE_PROSE
    assert v["translatable"] is True
    assert v["residual"].startswith("A novel approach")
    assert "E-MTAB-11814" not in v["residual"]      # 编号是脚手架，不是叙述
    assert "RNA-seq of coding RNA" not in v["residual"]


def test_hca_is_prose_verbatim() -> None:
    d = ("A comprehensive cellular anatomy of normal human prostate is essential for solving "
         "the cellular origins of benign prostatic hyperplasia and prostate cancer.")
    v = SG.classify(_item("Human Cell Atlas", d))
    assert v["genre"] == SG.GENRE_PROSE
    assert v["residual"] == d


def test_cellxgene_is_a_paper_title_not_a_summary() -> None:
    """CELLxGENE 的 description = `{collection name} · DOI: x`，而 collection name 是**论文标题**。

    把标题当摘要，用户会以为那句话描述的是这个数据集的内容。
    """
    d = ("A single-cell eQTL atlas of the human cerebellum reveals vulnerability of "
         "oligodendrocytes in essential tremor · DOI: 10.1038/s41588-026-02544-8")
    v = SG.classify(_item("CELLxGENE Discover", d))
    assert v["genre"] == SG.GENRE_TITLE
    assert v["genre"] != SG.GENRE_PROSE
    assert "DOI:" not in v["residual"]
    assert v["residual"].startswith("A single-cell eQTL atlas")


def test_scea_is_a_factor_list_not_language() -> None:
    """SCEA 的「摘要」其实是 `因子: organism part, sampling site, …` —— 字段名清单。"""
    d = "E-ANND-1 · Baseline · 因子: organism part, sampling site, disease, inferred cell type"
    v = SG.classify(_item("EBI Single Cell Expression Atlas", d))
    assert v["genre"] == SG.GENRE_FACTOR_LIST
    assert v["translatable"] is False
    assert v["residual"] == ""       # 结构上不可能被喂进「翻译」


def test_base_template_is_scaffold_never_prose() -> None:
    """base 的 description 由索引器按字段拼装 —— 没有任何来源叙述。

    第一版正则把 618 条这样的模板判成了 prose。按 source 判定后结构上不可能。
    """
    d = ("5k Mouse Kidney, Chromium GEM-X Epi Multiome with Automated Cell Annotation, "
         "contains mouse material, from kidney tissue. Chemistry: Epi Multiome; "
         "preservation method: Fresh Frozen. Analysis 2.0.")
    v = SG.classify(_item("10x Genomics", d, name="5k Mouse Kidney"))
    assert v["genre"] == SG.GENRE_SCAFFOLD
    assert v["translatable"] is False
    assert v["residual"] == ""


def test_unknown_source_keeps_its_text_but_is_not_translatable() -> None:
    """未知来源：「不知道它是什么」≠「它什么都不是」。

    第一版我把 UNKNOWN 的残余也置空 → 用户上传的真实描述被整段丢弃、展示成
    「该数据集的公开字段显示：物种为 Human。」——丢真信息，比现状更糟。
    正解：原样保留供**展示**，但不拿去做「忠实翻译」（形状未知）。
    """
    text = "Some free text a user typed in here about something."
    v = SG.classify(_item("用户上传", text))
    assert v["genre"] == SG.GENRE_UNKNOWN
    assert v["residual"] == text        # 展示：保留
    assert v["translatable"] is False   # 翻译：不碰


def test_absent_description() -> None:
    v = SG.classify(_item("ArrayExpress", ""))
    assert v["genre"] == SG.GENRE_ABSENT
    assert v["translatable"] is False


def test_source_that_should_have_prose_but_does_not_degrades() -> None:
    """AE 的 `· TBC` —— 契约说该有叙述，这一条实际没有 → 降级，不硬凑。"""
    v = SG.classify(_item("ArrayExpress", "E-ERAD-230 · RNA-seq of coding RNA · TBC"))
    assert v["genre"] == SG.GENRE_SCAFFOLD
    assert v["residual"] == ""


# --------------------------------------------------------------- 截断（我踩过的坑）

def test_mid_string_truncation_is_detected() -> None:
    """**关键**：HCA 实测 313 条是中部截断，`endswith("…")` 对它们 100% 失明。

    把半句话喂给「忠实翻译」→ 半句中文，用户看不出被截断过。
    """
    mid = "Cortex and hippocampus were purchased… They were from 2 E18 mice dissected same day."
    assert SG.is_truncated(mid) is True
    assert mid.rstrip().endswith("…") is False        # 反重言式：证明 endswith 真的抓不到
    assert SG.classify(_item("Human Cell Atlas", mid))["truncated"] is True


def test_tail_truncation_is_detected() -> None:
    assert SG.is_truncated("Some long abstract that got cut off right here…") is True


def test_untruncated_text_is_not_flagged() -> None:
    assert SG.is_truncated("A complete sentence with no ellipsis at all.") is False
    assert SG.is_truncated("") is False


def test_translatable_genres_are_exactly_prose_and_title() -> None:
    assert set(SG.TRANSLATABLE) == {SG.GENRE_PROSE, SG.GENRE_TITLE}


# --------------------------------------------------------------- 结构不变量

def test_non_translatable_genres_always_have_empty_residual() -> None:
    """结构性保证：LLM 层只拿 residual → scaffold/factor_list/unknown 恒不可能进翻译。"""
    cases = [
        ("10x Genomics", "anything, contains x material, from y tissue."),
        ("EBI Single Cell Expression Atlas", "E-1 · Baseline · 因子: a, b"),
        ("ArrayExpress", ""),
    ]
    for source, d in cases:
        v = SG.classify(_item(source, d))
        assert v["residual"] == "", (source, v)
        assert v["translatable"] is False, (source, v)


def test_only_known_prose_sources_are_ever_translatable() -> None:
    """能进「忠实翻译」的，只有我们读过 ingest 契约、确知是散文/标题的来源。"""
    text = "A long enough narrative sentence about the biology of this dataset here."
    translatable = {
        s for s in ("ArrayExpress", "Human Cell Atlas", "CELLxGENE Discover",
                    "EBI Single Cell Expression Atlas", "10x Genomics", "用户上传", "",
                    "Zenodo", "refine.bio", "ENCODE", "HuBMAP")
        if SG.classify(_item(s, text))["translatable"]
    }
    assert translatable == {"ArrayExpress", "Human Cell Atlas", "CELLxGENE Discover",
                            "Zenodo", "refine.bio"}


def test_genre_is_outside_the_frozen_closure() -> None:
    from test_provenance import _import_closure

    pkg = ROOT / "src" / "dataset_recommender"
    for rel in (
        "src/dataset_recommender/retrieval/retriever.py",
        "src/dataset_recommender/app/workflow.py",
        "scripts/evaluate_recommendation.py",
    ):
        assert "summary_genre" not in _import_closure(ROOT / rel, pkg), rel


def test_encode_is_unknown_genre_and_not_translatable() -> None:
    """ENCODE（第六来源， 正式提升）：本模块尚未读过 encode ingest 契约，
    其 description 形状未知 → 钉死 unknown：文本原样保留供展示，绝不进「忠实翻译」。
    与 :154 的 translatable 精确集合断言互补——那条管「只有白名单可译」，这条管「ENCODE 明确不可译」。
    """
    text = "ENCSR000ABC · RNA-seq of human tissue · a narrative description follows here."
    v = SG.classify(_item("ENCODE", text))
    assert v["genre"] == SG.GENRE_UNKNOWN
    assert v["residual"] == text        # 展示：保留
    assert v["translatable"] is False   # 翻译：不碰


def test_missing_source_is_unknown_not_scaffold() -> None:
    """缺 source 的记录**不再是**默认 10x（scaffold）。

    修复前无 source 记录的真实描述被当 ingest 模板整段丢弃（residual 置空），
    展示层与 LLM 翻译双双失明且完全无声。钉：genre=UNKNOWN、原文保留供展示、
    不进「忠实翻译」（与「用户上传」等未知来源同口径）。
    """
    text = "A real narrative description from a record that lost its source field."
    v = SG.classify(_item("", text))
    assert v["genre"] == SG.GENRE_UNKNOWN
    assert v["genre"] != SG.GENRE_SCAFFOLD
    assert v["residual"] == text        # 展示：不吞真文案
    assert v["translatable"] is False   # 翻译：不碰
    # 显式 10x 来源的记录仍是 scaffold（回归：改的是「缺 source」，不是 10x 本体）
    d = ("5k Mouse Kidney, contains mouse material, from kidney tissue. "
         "Chemistry: Epi Multiome; preservation method: Fresh Frozen.")
    assert SG.classify(_item("10x Genomics", d))["genre"] == SG.GENRE_SCAFFOLD


def test_zenodo_and_refinebio_are_prose() -> None:
    """Zenodo / refine.bio 的 description 是来源真实叙述 = 真散文。

    依据（读过采集管线原文，非猜形状）：Zenodo 取官方 metadata.description 剥 HTML
    （corpus_curation._zenodo_to_record）；refine.bio 取上游实验描述原文
    （corpus_curation._refinebio_to_record）。修复前两源落 UNKNOWN → 真散文永远
    得不到翻译。
    """
    text = "A long enough narrative sentence about the biology of this dataset here."
    for source in ("Zenodo", "refine.bio"):
        v = SG.classify(_item(source, text))
        assert v["genre"] == SG.GENRE_PROSE, source
        assert v["residual"] == text, source
        assert v["translatable"] is True, source
