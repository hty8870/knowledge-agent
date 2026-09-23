# -*- coding: utf-8 -*-
"""执行类说法不该炸掉检索，也不该被静默吞掉（用户想法 3 的确定性半边）。

用户的原话：「一句话检索+下载」应该以 LLM 开启时为主力场景，因为执行相关的关键词很多变。
实测发现的第一性问题**不是**「LLM 该不该归一化执行语句」，而是——

    人类肺数据，帮我打包前20条    → 弃权：查询里有系统未收录的词：「打包前」
    人类肺癌数据，生成下载脚本     → 弃权：「下载脚本」
    人类肺数据，导出引文          → 弃权：「导出引文」

**每一句都整句弃权**，连人类肺数据都查不到。所以先把这个坑填了：执行词收进语法填充词
（不再阻断检索），同时用只读的 `action_markers` 如实回显「你说了打包」。

 2026-07-25 基线变更后，本文件的定位变了一处：**不再以「只指路、不代劳」为底线**
（该做就做、做了就报）。但 `/api/recommend` 只回意图、产物由另一次显式请求产生这条**分层**
仍然成立且更重要——自动执行落在独立端点与前端派发，因此不必改动检索侧任何既有契约。
"""
import pytest

from dataset_recommender.app import board
from dataset_recommender.llm.config import get_settings
from dataset_recommender.corpus.corpus import known_source_values
from dataset_recommender.retrieval.query_parser import (
    detect_action_markers,
    detect_action_verbs,
    parse_query,
)
from dataset_recommender.app.workflow import DatasetRecommendationWorkflow

CAT = get_settings().keyword_mapping
S = get_settings()
ALL_SOURCES = known_source_values(S.data_dir, S.project_root)


@pytest.fixture(scope="module")
def wf():
    return DatasetRecommendationWorkflow()


EXECUTION_QUERIES = [
    "人类肺数据，帮我打包前20条",
    "人类肺癌数据，生成下载脚本",
    "人类肺数据，导出引文",
    "把人类肺癌的数据打包给我",
    "人类肺数据，做成清单",
    "人类肺数据，批量下载",
]


@pytest.mark.parametrize("query", EXECUTION_QUERIES)
def test_execution_phrasing_no_longer_kills_the_search(query, wf):
    intent = parse_query(query, CAT)
    assert intent.parse_status == "executable", (query, intent.abstain_reason, intent.abstain_detail)
    res = wf.run_with_meta(query=query, use_llm=False, sources=ALL_SOURCES)
    assert res.result_total > 0, (query, res.resolution_status)


@pytest.mark.parametrize("query,expected", [
    ("人类肺数据，帮我打包前20条", "打包"),
    ("人类肺癌数据，生成下载脚本", "下载脚本"),
    ("人类肺数据，批量下载", "批量下载"),
])
def test_execution_intent_is_echoed_not_swallowed(query, expected, wf):
    """填坑不等于可以装作没看见——用户明确说出的诉求必须有回音。"""
    res = wf.run_with_meta(query=query, use_llm=False, sources=ALL_SOURCES)
    assert expected in res.action_markers, (query, res.action_markers)


def test_plain_search_reports_no_action():
    assert detect_action_markers("人类肺组织的单细胞数据") == []
    assert detect_action_markers("") == []


def test_longest_marker_wins():
    """「下载脚本」不该被切成「下载」——回显的是用户真正写下的说法。"""
    assert detect_action_markers("生成下载脚本") == ["下载脚本"]
    assert detect_action_markers("批量下载数据") == ["批量下载"]


def test_recommend_endpoint_only_reports_intent_never_ships_artifacts(wf):
    """**分层不变量**：检索端点只回「读出了什么诉求」，产物一律由另一次显式请求产生。

     2026-07-25 基线变更后，「绝不代劳」不再是本项目的底线（该做就做、做了就报）。
    但这条断言本身仍然成立、且更重要了：自动执行落在**独立端点 + 前端派发**，
    `/api/recommend` 的返回体里永远不该夹带任务包产物。
    有了这条分层，「一句话直接执行」不需要动检索侧任何既有契约与门。
    """
    res = wf.run_with_meta(query="人类肺数据，帮我打包前20条", use_llm=False, sources=ALL_SOURCES)
    assert res.action_markers == ["打包"]
    for attr in ("plan_token", "pack", "zip", "download_script"):
        assert not getattr(res, attr, None), f"检索返回体里不该有 {attr}"


@pytest.mark.parametrize("utterance,route", [
    # 编号必须赢过执行词：否则「把这条打包」会在**当前屏上那批毫不相干的结果**上开下载面板，
    # 用户点名的那一条根本不在里面（identifiers.classify 用的是 search，编号在句中任意位置都算）。
    ("把 E-MTAB-1234 打包", "identifier"),
    ("下载 GSE123456", "identifier"),
    ("10.1038/s41586-020-2157-4 导出引文", "identifier"),
    # 没有编号时执行词照旧赢
    ("帮我打包前20条", "action"),
    ("生成下载脚本", "action"),
    # 两者都没有 → 当作一整句新检索
    ("人类肺癌的空间转录组数据", "new_query"),
    ("？？？", ""),
    # 对象词不是动作：这几句都是**改条件**，被裸子串劫持成「打开下载面板」会让用户那半句诉求当场蒸发
    ("去掉批量效应大的", "new_query"),
    ("候选清单里去掉重复的", "new_query"),
    ("只要有脚本的那些", "new_query"),
])
def test_identifier_outranks_action_in_route(utterance, route):
    assert board.classify_utterance(utterance) == route, utterance


def test_object_words_still_echo_even_though_they_do_not_route():
    """拆表不等于把对象词吞掉：回音仍然完整，只是不再拿它路由。"""
    assert detect_action_markers("去掉批量效应大的") == ["批量"]
    assert detect_action_verbs("去掉批量效应大的") == []
    # 动作词两边都在
    assert detect_action_verbs("帮我打包前20条") == ["打包"]
    # 长标记优先在两张表上都成立
    assert detect_action_verbs("生成下载脚本") == ["下载脚本"]


def test_action_tables_are_a_partition_of_the_echo_table():
    """并集必须是程序并出来的，不能有第三份手抄。"""
    from dataset_recommender.retrieval import query_lexicon as V

    assert set(V.ACTION_MARKERS) == set(V.ACTION_VERBS) | set(V.ACTION_NOUNS)
    assert not (set(V.ACTION_VERBS) & set(V.ACTION_NOUNS)), "同一个词不能既当动作又当对象"


def test_action_markers_are_reported_even_when_route_is_identifier():
    """编号优先不等于可以把「打包」这半句吞掉——回执里必须有它，供上层如实告知。"""
    plan = board.plan_edit("人类肺癌数据", "把 E-MTAB-1234 打包", current_filters=[])
    assert plan["route"] == "identifier"
    assert plan["action_markers"] == ["打包"]
