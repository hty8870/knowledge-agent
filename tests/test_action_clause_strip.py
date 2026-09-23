# -*- coding: utf-8 -*-
"""执行子句里的**条数**不得流进自由文本打分，且这条新路径在冻结基准上必须完全不可达。

背景：`_extract_free_text_terms` 把句子里所有 ASCII 数字/字母串抽成自由文本词参与打分。
于是「人类肺癌数据，打包前20条」里的 `20` 被当成一个**检索词**——它描述的是「要几条」，
不是「要什么」。后果不是排版问题：它会换掉候选池里的若干条，也就是**进包的是哪几个数据集变了**。

这里同时钉两件事：
  1. 行为：带执行子句与不带执行子句的同一句话，抽出的自由文本词与命中的前 N 条必须逐位相同；
  2. 隔离：冻结评测的 192 条查询（主集 54 + multisource 33 + held-out 50 + dev 55）里**一个执行动作词都没有**，
     因此 `strip_action_counts` 在基准上是恒等映射——不必等跑完全量评测就能先证明「这条改动碰不到冻结基准」。
"""
import json
from pathlib import Path

import pytest

from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import known_source_values
from dataset_recommender.retrieval.query_parser import (
    detect_action_verbs,
    parse_query,
    strip_action_counts,
)
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow

ROOT = Path(__file__).resolve().parents[1]
S = get_settings()
CAT = S.keyword_mapping
ALL_SOURCES = known_source_values(S.data_dir, S.project_root)


@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


@pytest.mark.parametrize("query,expect_gone", [
    ("人类肺癌数据，打包前20条", "20"),
    ("人类肺数据，帮我打包前 5 条", "5"),
    ("人类脑数据，导出这3份", "3"),
    ("人类肺数据，下载前50个", "50"),
])
def test_action_clause_counts_do_not_enter_free_text(query, expect_gone):
    intent = parse_query(query, CAT)
    assert intent.parse_status == "executable", (query, intent.abstain_reason)
    assert expect_gone not in intent.free_text_terms, (query, intent.free_text_terms)


@pytest.mark.parametrize("query", [
    # 没有执行动作词 → 整条路径不启动，数字原样保留（「20 个样本」是检索信息，不是条数指令）
    "20个样本的人类肺数据",
    # 版本号 / 编号 / 年份：既没有量词收尾，也不该被碰
    "10x Genomics 的人类肺数据",
    "COVID-19 的人类肺数据",
    "2020年之后的人类肺癌数据",
])
def test_strip_never_touches_search_bearing_numbers(query):
    assert strip_action_counts(query) == query, query


def test_action_clause_does_not_change_which_datasets_come_back(wf):
    """带/不带执行子句，前 20 条必须**逐位相同**——否则「说了打包」这件事本身改变了进包的内容。"""
    base = "人类肺癌的单细胞数据"
    withact = base + "，帮我打包前20条"
    a = wf.run_with_meta(query=base, use_llm=False, sources=ALL_SOURCES, top_k=20)
    b = wf.run_with_meta(query=withact, use_llm=False, sources=ALL_SOURCES, top_k=20)
    names_a = [r.get("dataset_name") for r in (a.retrieved_data or [])]
    names_b = [r.get("dataset_name") for r in (b.retrieved_data or [])]
    assert names_a == names_b, "执行子句改变了检索结果——那 20 条被当成检索词打分了"
    assert a.result_total == b.result_total


@pytest.mark.parametrize("rel", ["eval/eval_queries.json", "eval/multisource_queries.json", "eval/eval_queries_dev.json"])
def test_frozen_eval_queries_are_out_of_reach(rel):
    """冻结基准的每一条查询都不含执行动作词 → 新路径在基准上恒为恒等映射。

    这是「本次改动碰不到冻结 767」最便宜的机械证明：不必跑完整评测就能先红。
    dev 集（eval/eval_queries_dev.json）同挂此断言：它同样是评测基准，
    其查询也不许被执行子句路径改写。
    """
    data = json.loads((ROOT / rel).read_text(encoding="utf-8"))
    rows = data if isinstance(data, list) else data.get("queries", data)
    assert rows, rel
    for row in rows:
        q = row.get("query") if isinstance(row, dict) else str(row)
        if not q:
            continue
        assert detect_action_verbs(q) == [], f"{rel} 里出现了执行动作词：{q}"
        assert strip_action_counts(q) == q, f"{rel} 的这条被改写了：{q}"
