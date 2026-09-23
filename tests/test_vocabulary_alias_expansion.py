"""受控词表 alias 覆盖扩展（tissue+species）：本体/常见同义词现在能被正确解析，不再 fail-closed 弃权。

钉死两面：
1) 价值——一批过去会 abstain(unresolved_term) 的说法（homo sapiens / mus musculus / 表皮 / 乳房 / 骨骼肌 …）
   现在解析到正确的规范 target；且 species 与 tissue 能正确分流（mus musculus 脑 → mouse + brain）。
2) 护栏——扩 alias 不得破坏 fail-closed：未知实体/否定/OR 仍必须弃权。
   （0% 硬违规由 hard_filter 结构性保证 + 冻结门回归，不在本文件重复。）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402


def test_new_species_synonyms_resolve():
    cases = {
        "homo sapiens 的数据": "human",
        "mus musculus 数据集": "mouse",
        "danio rerio 相关数据": "zebrafish",
        "canis familiaris 数据": "dog",
        "rattus 的数据": "rat",
    }
    for q, target in cases.items():
        intent = parse_query(q)
        assert not intent.abstain, f"{q!r} 不应再弃权，got reason={intent.abstain_reason}"
        assert target in intent.constraints.get("species", []), f"{q!r} 应解析 species={target}, got {intent.constraints}"


def test_new_tissue_synonyms_resolve():
    cases = {
        "表皮的单细胞数据": "skin",
        "乳房数据": "breast",
        "骨骼肌数据": "muscle",
        "epidermis 数据": "skin",
        "prostatic 数据": "prostate",
    }
    for q, target in cases.items():
        intent = parse_query(q)
        assert not intent.abstain, f"{q!r} 不应再弃权，got reason={intent.abstain_reason}"
        assert target in intent.constraints.get("tissue", []), f"{q!r} 应解析 tissue={target}, got {intent.constraints}"


def test_species_and_tissue_disambiguate():
    # "musculus"(小鼠) 与 "muscle"/"骨骼肌"(肌肉) 不能互相串味
    m = parse_query("mus musculus 脑数据")
    assert not m.abstain
    assert "mouse" in m.constraints.get("species", [])
    assert "brain" in m.constraints.get("tissue", [])
    assert "muscle" not in m.constraints.get("tissue", [])  # musculus 不应误触发 muscle

    sk = parse_query("skeletal muscle 数据")
    assert not sk.abstain
    assert sk.constraints.get("tissue") == ["muscle"]
    assert "species" not in sk.constraints  # 不应误触发 mouse


def test_failclosed_guards_still_hold():
    # 扩 alias 不得破坏 fail-closed：未知实体仍弃权
    assert parse_query("翼龙的单细胞数据").abstain is True
    assert parse_query("霍格沃茨综合征的人类数据").abstain is True
    # 否定语法落地后：安全否定可执行，但跨维歧义否定仍必须弃权（fail-closed 未被削弱）
    assert parse_query("不要小鼠的原始数据").abstain is True
    # 「人或小鼠的脑数据」**不再**属于 fail-closed 范围——同维度多值就是「或」，
    # 引擎一直支持，弃权是白弃的。改成正面钉住它照做且语义精确。
    _or = parse_query("人或小鼠的脑数据")
    assert not _or.abstain, _or.abstain_reason
    assert sorted(_or.constraints.get("species") or []) == ["human", "mouse"]
    assert _or.constraints.get("tissue") == ["brain"]
