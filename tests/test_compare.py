# -*- coding: utf-8 -*-
"""compare.datasets 的**确定性 diff 层**（`agent/compare.py`）独立单测——零 LLM、零语料、
零网络。钉四条：
  1. `diff_items` 逐字段产出结构化差异（same / different / only_a / only_b / both_missing），
     计数与 identical 语义正确——这是事实层，LLM 措辞只能基于它；
  2. 缺失字段如实记「未知」（only_a / only_b / both_missing），**绝不**把「不知道」说成「没有」；
  3. `render_deterministic` 兜底句与同一批事实一致（字段全同如实说没差异）；
  4. `introduces_foreign_numbers` 数字交叉核验：diff 事实里找不到出处的阿拉伯数字拦下
     （LLM 只负责措辞，数字必须有出处）。
"""
from dataset_recommender.agent import compare


def _item(**over):
    base = {
        "dataset_name": "Human Lung Cancer Visium",
        "source": "10x Genomics",
        "species": "Human",
        "tissue": "Lung",
        "disease": "Lung cancer",
        "platform": "Visium",
        "assay": "spatial",
        "chemistry": "3p v2",
        "modality": "spatial",
        "count": "12000",
        "unit": "cells",
        "published_date": "2021-03-15",
        "n_files": 5,
    }
    base.update(over)
    return base


def test_diff_items_reports_different_same_and_missing_fields():
    diff = compare.diff_items(_item(), _item(
        dataset_name="Mouse Brain Xenium", species="Mouse", tissue="Brain",
        disease="", count="8000", unit="cells", published_date="2022-07-01", n_files=3))
    assert diff["identical"] is False
    assert diff["n_diff"] >= 1 and diff["n_same"] >= 1
    by_field = {f["field"]: f for f in diff["fields"]}
    assert by_field["species"]["status"] == "different"
    assert by_field["disease"]["status"] == "only_a"   # b 缺失——如实记「只有 a 有」
    assert by_field["source"]["status"] == "same"
    assert by_field["dataset_name"]["status"] == "different"


def test_diff_items_missing_on_both_sides_is_unknown_not_same():
    diff = compare.diff_items(_item(disease=""), _item(disease=""))
    by_field = {f["field"]: f for f in diff["fields"]}
    assert by_field["disease"]["status"] == "both_missing"
    # 双缺失计入 n_unknown（那是「不知道」，不是「一致」）——n_same/n_diff 都不含它
    assert diff["n_unknown"] == sum(1 for f in diff["fields"] if f["status"] == "both_missing")
    assert diff["n_same"] == sum(1 for f in diff["fields"] if f["status"] == "same")
    assert diff["n_diff"] == sum(1 for f in diff["fields"] if f["status"] != "same"
                                 and f["status"] != "both_missing")


def test_diff_items_multi_value_field_ignores_order():
    """物种/组织/疾病按**集合**比较：顺序差异不算差异（与 compatibility 同思路）。"""
    diff = compare.diff_items(_item(species="Human, Mouse"), _item(species="Mouse, Human"))
    by_field = {f["field"]: f for f in diff["fields"]}
    assert by_field["species"]["status"] == "same"


def test_diff_items_identical_when_all_comparable_fields_equal():
    diff = compare.diff_items(_item(), _item())  # 同字段同值
    assert diff["identical"] is True
    assert diff["n_diff"] == 0 and diff["n_same"] > 0


def test_diff_items_sample_size_combines_count_and_unit():
    """sample_size 是合成键：count+unit 齐全才比较；缺一半 = 缺失（未知），不编造。"""
    diff = compare.diff_items(_item(count="12000", unit="cells"),
                              _item(count="12000", unit="cells"))
    by_field = {f["field"]: f for f in diff["fields"]}
    assert by_field["sample_size"]["a"] == "12000 cells"
    assert by_field["sample_size"]["status"] == "same"
    diff2 = compare.diff_items(_item(count="", unit="cells"), _item(count="", unit=""))
    by2 = {f["field"]: f for f in diff2["fields"]}
    assert by2["sample_size"]["status"] == "both_missing"


def test_field_value_treats_unknown_sentinels_as_missing():
    from dataset_recommender.retrieval.normalizer import MISSING_VALUE_TOKENS

    for sentinel in MISSING_VALUE_TOKENS:
        assert compare.field_value(_item(disease=sentinel), "disease") == ""
    assert compare.field_value(_item(disease="  "), "disease") == ""


def test_render_deterministic_honest_for_identical_and_different():
    same = compare.diff_items(_item(), _item())
    assert "完全相同" in compare.render_deterministic(same, "A", "B")
    assert "未发现差异" in compare.render_deterministic(same, "A", "B")
    diff = compare.diff_items(_item(species="Mouse"), _item())
    text = compare.render_deterministic(diff, "A", "B")
    assert "个字段一致" in text and "个字段不同" in text
    assert "物种" in text  # 差异逐项点名


def test_render_deterministic_never_invents_a_missing_value():
    diff = compare.diff_items(_item(disease=""), _item(disease="Alzheimer"))
    text = compare.render_deterministic(diff, "A", "B")
    assert "缺失/未标注" in text  # 缺的那侧如实写「未标注」，绝不写「没有」


# ---------------------------------------------------------------- 数字交叉核验（措辞层的机械健全性检查）

def _diff():
    return compare.diff_items(_item(), _item(species="Mouse", n_files=3))


def test_introduces_foreign_numbers_blocks_unfounded_digits():
    diff = _diff()
    # 「9999」在 diff 事实文本里找不到出处 → 拦下（LLM 凭空捏造的数字不得进结论）
    assert compare.introduces_foreign_numbers("A 有 9999 个细胞", diff, "A", "B") is True
    # 「5」在 n_files=5 的事实里 → 放行（合法引用事实数字）
    assert compare.introduces_foreign_numbers("A 有 5 个文件", diff, "A", "B") is False


def test_introduces_foreign_numbers_allows_digits_from_dates_and_names():
    diff = _diff()
    # 2021 出自 published_date=；3/15 以子串形式存在于事实文本 → 放行
    assert compare.introduces_foreign_numbers("A 发表于 2021 年", diff, "A", "B") is False
    assert compare.introduces_foreign_numbers("比较 10x 的两个数据集", diff, "A", "B") is False


def test_build_prompt_carries_structured_facts_only():
    diff = _diff()
    import json

    payload = json.loads(compare.build_prompt(diff, "A", "B"))
    assert payload["dataset_a"] == "A" and payload["dataset_b"] == "B"
    assert payload["same_fields"] == diff["n_same"]
    assert payload["different_fields"] == diff["n_diff"]
    assert payload["identical"] == diff["identical"]
    assert len(payload["fields"]) == len(diff["fields"])
