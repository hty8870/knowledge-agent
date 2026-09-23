"""结构化上移 + units.py DRY 的回归测试。

三块：
1. **金样等价**：代表性候选（覆盖 has_raw_data True/False/None、unit cells/spots/nuclei/未知/空）下，
   `format_candidates_markdown` 输出必须与**重构前**逐字节相等（证明确定性没漂）。
   EXPECTED 串取自重构**前**运行同一组候选的实际输出。
2. **结构化字段**：`_serialize_retrieved_data` 新增的 `raw_data_status` / `sample_size` 各字段值正确。
3. **units**：`format_sample_size` 四分支 + `explain_unit` 命中/未知(None)。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.llm.llm_client import _sample_size as _llm_sample_size  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402
from dataset_recommender.retrieval.retriever import RetrievedCandidate  # noqa: E402
from dataset_recommender.retrieval.units import (  # noqa: E402
    UNIT_EXPLANATIONS,
    explain_unit,
    format_sample_size,
)
# webapp 的私有 `_format_sample_size` 已删——web 渲染路径
# （`rows_from_retrieved`）收进 app/recommend_rows.py，样本量格式化走 units 真源的转口，
# 别名随之改指。
from dataset_recommender.app.recommend_rows import format_sample_size as _web_sample_size  # noqa: E402
from dataset_recommender.app.workflow import (  # noqa: E402
    RAW_WARNING,
    _serialize_retrieved_data,
    format_candidates_markdown,
)


def _cand(name, count, unit, has_raw, url, reason, raw=None):
    rec = DatasetRecord(
        dataset_name=name, species="", tissue="", disease="", chemistry="",
        count=count, unit=unit, has_raw_data=has_raw, url=url,
        source_file="x", description="", raw=raw or {},
    )
    return RetrievedCandidate(record=rec, score=1.0, reason=reason)


# 代表性候选：覆盖 raw True/False/None、unit cells/spots/nuclei/未知/空、count 有/无、以及
# 单位大小写与出现顺序（Nuclei 大写、且排在未知单位之前，用于证明解释块按规范顺序而非出现顺序）。
# 只翻 ArrayExpress 猜测档（见 test_guessed_false_from_arrayexpress_shows_unconfirmed）；
# 其余 False 维持 ❌——Zeta 带 CELLxGENE 来源（库级事实，有据 False），Beta 无来源信息（非 AE 猜测档，不动）。
def _candidates():
    return [
        _cand("Alpha 乳腺癌", "5000", "cells", True, "https://example.org/a", "匹配 species。"),
        _cand("Beta 脑", "", "spots", False, "https://example.org/b", "匹配 tissue。"),
        _cand("Gamma", "1000", "", None, "https://example.org/c", "满足基本检索条件。"),
        _cand("Delta", "", "", True, "https://example.org/d", ""),
        _cand("Epsilon", "7500", "Nuclei", None, "https://example.org/e", "匹配 disease。"),
        _cand("Zeta", "200", "unknownunit", False, "https://example.org/f", "匹配 chemistry。",
              raw={"source": "CELLxGENE Discover", "dataset_uid": "cxg:zeta"}),
    ]


# 重构前实际输出（逐字节金样）。重构后必须完全一致。
EXPECTED_MARKDOWN = (
    "| 数据集名称 | 物种 | 组织 | 疾病 | 技术方案 | 样本量 | 原始数据状态 | 下载链接 |\n"
    "|---|---|---|---|---|---|---|---|\n"
    "| Alpha 乳腺癌 | - | - | - | - | 5000 cells | ✅ 包含 FASTQ | [点击下载](https://example.org/a) |\n"
    "| Beta 脑 | - | - | - | - | 未说明 spots | ❌ 无 FASTQ | [点击下载](https://example.org/b) |\n"
    "| Gamma | - | - | - | - | 1000 | ⚪ 未说明 | [点击下载](https://example.org/c) |\n"
    "| Delta | - | - | - | - | 未说明 | ✅ 包含 FASTQ | [点击下载](https://example.org/d) |\n"
    "| Epsilon | - | - | - | - | 7500 Nuclei | ⚪ 未说明 | [点击下载](https://example.org/e) |\n"
    "| Zeta | - | - | - | - | 200 unknownunit | ❌ 无 FASTQ | [点击下载](https://example.org/f) |\n"
    "\n"
    "**提示：标有 ❌ 无 FASTQ 的数据集仅包含分析结果或处理后文件，不支持重新从 FASTQ 跑完整流程。**\n"
    "\n"
    "**推荐理由：**\n"
    "- **Alpha 乳腺癌**：匹配 species。\n"
    "- **Beta 脑**：匹配 tissue。\n"
    "- **Gamma**：满足基本检索条件。\n"
    "- **Epsilon**：匹配 disease。\n"
    "- **Zeta**：匹配 chemistry。\n"
    "\n"
    "“Cells” 指单细胞测序中捕获并测序的细胞数量。\n"
    "“Spots” 多用于空间转录组技术，表示组织切片上的空间检测位点，每个位点可能包含多个细胞。\n"
    "“Nuclei” 指单核测序或细胞核层面的计数单位，通常表示被捕获并测序的细胞核数量。"
)


def test_format_candidates_markdown_byte_identical():
    """确定性没漂：重构后逐字节等于重构前金样。"""
    assert format_candidates_markdown(_candidates()) == EXPECTED_MARKDOWN


def test_serialize_raw_data_status_struct():
    payload = _serialize_retrieved_data(_candidates())[1]
    by_name = {item["dataset_name"]: item for item in payload}

    # has_raw_data=True → has_fastq
    alpha = by_name["Alpha 乳腺癌"]["raw_data_status"]
    assert alpha["code"] == "has_fastq"
    assert alpha["label"] == "✅ 包含 FASTQ"
    assert alpha["warning"] is None
    assert alpha["authoritative"] is False
    assert "get_file_manifest" in alpha["note"]

    # has_raw_data=False 且无来源信息（非 AE 猜测档）→ no_fastq，warning=RAW_WARNING（口径不变）
    beta = by_name["Beta 脑"]["raw_data_status"]
    assert beta["code"] == "no_fastq"
    assert beta["label"] == "❌ 无 FASTQ"
    assert beta["warning"] == RAW_WARNING
    assert beta["authoritative"] is False

    # has_raw_data=None → unknown
    gamma = by_name["Gamma"]["raw_data_status"]
    assert gamma["code"] == "unknown"
    assert gamma["label"] == "⚪ 未说明"
    assert gamma["warning"] is None

    # 有据 False（CELLxGENE 库级事实 = NOT_LISTED）维持 no_fastq + RAW_WARNING（只翻猜测档）
    zeta = by_name["Zeta"]["raw_data_status"]
    assert zeta["code"] == "no_fastq"
    assert zeta["label"] == "❌ 无 FASTQ"
    assert zeta["warning"] == RAW_WARNING


def test_guessed_false_from_arrayexpress_shows_unconfirmed():
    """ArrayExpress 的 has_raw_data=False 是抓取时的保守占位
    （来源从未标注）——用户侧表格与结构化字段都必须显示「未确认」，
    不许印成「无 FASTQ」（把「我们没查」编码成「它没有」）。"""
    c = _cand("AE 集", "", "", False, "https://example.org/ae", "",
              raw={"source": "ArrayExpress", "dataset_uid": "ae:E-MTAB-1"})
    md = format_candidates_markdown([c])
    assert "⚪ 未确认" in md and "❌ 无 FASTQ" not in md
    payload = _serialize_retrieved_data([c])[1]
    assert payload[0]["raw_data_status"]["code"] == "unknown"
    assert payload[0]["raw_data_status"]["label"] == "⚪ 未确认"
    assert payload[0]["raw_data_status"]["warning"] is None

    # 逐条核验过的 AE 记录（BioStudies 文件清单 complete）不算猜 → 维持 ❌
    verified = _cand("AE 已核验", "", "", False, "https://example.org/ae2", "",
                     raw={"source": "ArrayExpress", "dataset_uid": "ae:E-MTAB-2",
                          "metadata_provenance": {"complete": True, "fields": {
                              "files": {"complete": True}, "has_raw_data": {"complete": True}}}})
    assert "❌ 无 FASTQ" in format_candidates_markdown([verified])


def test_serialize_sample_size_struct():
    payload = _serialize_retrieved_data(_candidates())[1]
    by_name = {item["dataset_name"]: item for item in payload}

    # count + unit
    alpha = by_name["Alpha 乳腺癌"]["sample_size"]
    assert alpha == {
        "count": "5000",
        "unit": "cells",
        "display": "5000 cells",
        "unit_explanation": UNIT_EXPLANATIONS["cells"],
    }

    # 仅 unit
    beta = by_name["Beta 脑"]["sample_size"]
    assert beta["display"] == "未说明 spots"
    assert beta["unit_explanation"] == UNIT_EXPLANATIONS["spots"]

    # 仅 count → 单位解释 None
    gamma = by_name["Gamma"]["sample_size"]
    assert gamma["display"] == "1000"
    assert gamma["unit_explanation"] is None

    # 未知单位 → 解释 None（有 display）
    zeta = by_name["Zeta"]["sample_size"]
    assert zeta["display"] == "200 unknownunit"
    assert zeta["unit_explanation"] is None


def test_format_sample_size_four_branches():
    assert format_sample_size("5000", "cells") == "5000 cells"   # count + unit
    assert format_sample_size("", "spots") == "未说明 spots"       # 仅 unit
    assert format_sample_size("1000", "") == "1000"               # 仅 count（规范：不带「未说明单位」）
    assert format_sample_size("", "") == "未说明"                  # 都无


def test_explain_unit_hit_and_unknown():
    assert explain_unit("cells") == UNIT_EXPLANATIONS["cells"]
    assert explain_unit("Nuclei ") == UNIT_EXPLANATIONS["nuclei"]  # 大小写 + 去空白
    assert explain_unit("SPOTS") == UNIT_EXPLANATIONS["spots"]
    assert explain_unit("unknownunit") is None                     # 未知 → None，绝不臆造
    assert explain_unit("") is None


def test_sample_size_divergence_unified_to_units():
    """有意统一：llm_client / webapp 的「仅 count」旧输出 `{count} 未说明单位` → 规范 `{count}`。"""
    assert _llm_sample_size("325345", "") == "325345"
    assert _web_sample_size("325345", "") == "325345"
    # 三处一致
    assert _llm_sample_size("325345", "") == format_sample_size("325345", "")
    assert _web_sample_size("325345", "") == format_sample_size("325345", "")
