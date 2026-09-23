# -*- coding: utf-8 -*-
"""空查询不得命中全库：只剩填充词 / 执行词 / 纯数字的查询，
   解析器判 `abstained`（reason="empty_query"），检索器返回空——不把整个库当结果倒出去。
   真实检索查询（有物种 / 组织 / 疾病 / 技术 / 时间等）逐条仍 executable，冻结 767 不受影响。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.retrieval.query_parser import parse_query  # noqa: E402
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow  # noqa: E402


# ============================ 1. 空查询弃权（不命中全库） ============================

@pytest.mark.parametrize("q", [
    "打包前20条", "帮我打包", "下载脚本", "导出引文", "打包", "帮我打包前20条", "把这些打包",
])
def test_bare_action_command_abstains(q):
    """光秃秃的执行指令（有打包/下载/导出、没检索目标）→ 弃权，绝不命中全库。"""
    it = parse_query(q)
    assert it.abstain is True, f"{q!r} 应弃权、不命中全库"
    assert it.abstain_reason == "action_only", f"{q!r} 弃权原因应为 action_only，实为 {it.abstain_reason}"
    assert it.parse_status == "abstained"


@pytest.mark.parametrize("q", [
    # 真实检索意图
    "人类肺癌单细胞", "有FASTQ的人类乳腺癌", "小鼠脑", "2020年以来的数据", "肺癌免疫细胞",
    "Visium 人脑", "10x 的乳腺癌", "打包人类肺数据",
    # 宽泛/无建模词的查询：**不**落纯执行指令弃权——按既有设计返回宽结果 + 提醒（无执行词）。
    "我想要一些数据", "细胞", "免疫细胞", "T细胞", "衰老相关", "前20条", "帮我",
])
def test_non_action_queries_are_not_action_only_abstain(q):
    it = parse_query(q)
    assert not (it.abstain and it.abstain_reason == "action_only"), \
        f"{q!r} 不是纯执行指令，不该按 action_only 弃权（实为 {it.abstain_reason}）"


def test_empty_query_returns_no_results_not_whole_corpus():
    """端到端：空查询走 workflow → resolution_status=abstained、result_total=0（而非全库）。"""
    wf = DatasetRecommendationWorkflow()
    res = wf.run_with_meta(query="打包前20条", top_k=10, use_llm=False)
    assert res.resolution_status == "abstained"
    assert res.result_total == 0
    assert len(res.retrieved_data) == 0
    # 执行说法仍被认到（供 actionHint 指路），只是检索本身弃权。
    assert res.action_markers == ["打包"]
