# -*- coding: utf-8 -*-
"""统一路由端点 `/api/utterance` 的确定性门（**零网络**：LLM 一律 monkeypatch 注入计数假实现）。

钉的是 定稿的 turn pipeline（`turn.route_turn` 单一真源）：
**「AI 执行」（agent 标志）开 → 规则匹配（一切指令都过；零命中/弃权 ≠ 无效）→ 100% LLM 分流
→ search / tool / none；「AI 执行」关 → 规则直达（LLM 分流器永不启动），操作句回降级气泡**。
  1. 响应契约：`{ok, route, query, plan, echo_zh, retrieval, via, needs_agent, agent}`；
     search 带完整检索句（LLM 改写的 effective_query，空则按原话），tool 带 EXEC plan，
     none 带如实回音；
  2. **无短路**（C 开）：除编号快速道外每句话都过 LLM——工具调用句关键词往往零命中，
     零命中阶段把它毙掉正是本模块存在的理由（「检查10x数据库是否有更新」一例）；
  3. **C 关零 LLM**：agent=false 时分流器永不启动（不拼装提示词、不发调用）——检索句
     原话直达（via=rule_direct）；规则检出操作意图的句子回降级气泡
     （route=none, needs_agent=true），绝不静默当检索处理；
  4. LLM 缺席/失败（C 开）→ 规则兜底：动作词 → tool 规则档（只开清单）；search_shaped → search；
     其余 → none（绝不静默全库检索）；
  5. 入参纪律与既有端点一致：extra=forbid、same-origin 闸、长度上限、不回显 key。
"""
import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

#: 把请求武装到「只要代码走到 LLM 配置链就一定会发调用」的程度：
#: 真 provider + use_llm + 请求级 key + **agent=True（「AI 执行」开，LLM 分流器的总闸）**。
#: 此时若分支设计漏了 LLM，计数器必然抓到。本文件钉的是 plan_action 单次分类保底路径
#: （langgraph agent 路径由 tests/test_agent_exec.py 专测）——autouse fixture 把
#: agent_available 恒关掉，agent 分流因此总是落到保底路径，注入缝（_default_llm_call）才够得着。
ARMED = {"provider": "openai-compatible", "use_llm": True, "api_key": "sk-utterance-test",
         "agent": True}


@pytest.fixture(autouse=True)
def _no_langgraph(monkeypatch):
    """本文件钉 plan_action 保底路径：langgraph agent 恒不可用（流式 agent 测试自行桩回来）。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: False)


@pytest.fixture
def counting_llm(monkeypatch):
    """注入一个计数的假 LLM：返回 None（→ 规则档），但调用次数可查。"""
    calls: list[str] = []

    def fake(prompt, config):
        calls.append(prompt)
        return None

    monkeypatch.setattr(AP, "_default_llm_call", fake)
    return calls


def _reply_llm(monkeypatch, payload: dict):
    """注入一个按固定 JSON 回答的假 LLM。"""
    calls: list[str] = []

    def fake(prompt, config):
        calls.append(prompt)
        return json.dumps(payload, ensure_ascii=False)

    monkeypatch.setattr(AP, "_default_llm_call", fake)
    return calls


# ---------------------------------------------------------------- turn pipeline 三档路由

def test_clean_search_still_reaches_the_llm_then_falls_back_to_search(counting_llm):
    """明确检索句**也要过 LLM**（定稿管线：无并行短路）；LLM 空回 → 规则兜底
    search_shaped → route="search"（query=原话）。工具调用句关键词
    零命中，短路会把管护诉求钉死在检索上，这正是旧设计被清除的原因。"""
    res = client.post("/api/utterance", json={"utterance": "人类肺癌的单细胞数据", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["route"] == "search"
    assert body["query"] == "人类肺癌的单细胞数据"
    assert body["via"] == "rule_fallback"
    assert 1 <= len(counting_llm) <= 2, "定稿管线：每句话都过 LLM，检索句没有捷径（失败允许重试一次）"


def test_llm_judged_search_new_returns_effective_query(monkeypatch):
    """LLM 真判 search.new → route="search"，query 用 LLM 的 effective_query（剥掉客套的整句）。"""
    calls = _reply_llm(monkeypatch, {"verb": "search.new", "quoted": "人类肺癌的单细胞数据",
                                     "effective_query": "人类肺癌单细胞数据", "confidence": "high"})
    res = client.post("/api/utterance", json={"utterance": "帮我找人类肺癌的单细胞数据", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search"
    assert body["query"] == "人类肺癌单细胞数据"
    assert body["via"] == "llm"
    assert body["plan"]["verb"] == "search.new" and body["plan"]["source"] == "llm"
    assert len(calls) == 1


def test_llm_judged_refine_merges_into_current_query(monkeypatch):
    """refine.conditions → route="search"，query 是 LLM 据当前查询+条件改写的整句。

    prompt 必须带上当前查询与当前条件（refine 改写的依据）——断言它们真的进了 prompt。"""
    calls = _reply_llm(monkeypatch, {"verb": "refine.conditions", "quoted": "去掉小鼠",
                                     "effective_query": "推荐有 FASTQ 的乳腺癌数据",
                                     "confidence": "high"})
    res = client.post("/api/utterance", json={
        "utterance": "去掉小鼠", "query": "推荐有 FASTQ 的小鼠乳腺癌数据",
        "has_results": True, "result_total": 34,
        "current_filters": [{"dim": "species", "label": "物种", "values": ["Mouse"]}],
        **ARMED,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search" and body["query"] == "推荐有 FASTQ 的乳腺癌数据"
    prompt = calls[0]
    assert "推荐有 FASTQ 的小鼠乳腺癌数据" in prompt, "当前查询必须进 prompt"
    assert "Mouse" in prompt, "当前生效条件必须进 prompt"
    assert len(calls) == 1


def test_llm_search_verb_without_effective_query_falls_back_to_original(monkeypatch):
    """LLM 判了检索动词却没给 effective_query → 按用户原话检索（fail-open 不丢句）。"""
    _reply_llm(monkeypatch, {"verb": "search.new", "quoted": "人类肺癌", "confidence": "low"})
    res = client.post("/api/utterance", json={"utterance": "人类肺癌", **ARMED})
    body = res.json()
    assert body["route"] == "search" and body["query"] == "人类肺癌"


def test_tool_call_sentence_with_zero_keyword_hits_reaches_tool(monkeypatch):
    """本次重写覆盖句：「检查10x数据库是否有更新」关键词阶段零命中/弃权，
    但 LLM 必须见到原话并判成管护动词——绝不落入检索路径。
     breaking：tool 路线 retrieval 由摘要 dict 变 None +
    additive retrieval_note（"discarded_action_route"——保底就地起 flight、结果被弃）。"""
    calls = _reply_llm(monkeypatch, {"verb": "curate.search_online",
                                     "quoted": "检查10x数据库是否有更新", "confidence": "high"})
    res = client.post("/api/utterance", json={"utterance": "检查10x数据库是否有更新",
                                              "has_results": True, "result_total": 34, **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool", body
    assert body["plan"]["verb"] == "curate.search_online" and body["plan"]["kind"] == "exec"
    # breaking 重钉：tool 路线不再带规则匹配概览摘要，改 None + retrieval_note。
    assert body["retrieval"] is None
    assert body["retrieval_note"] == "discarded_action_route"
    assert len(calls) == 1, "零命中也必须到得了 LLM"


def test_llm_judged_curate_intent_reaches_tool(monkeypatch):
    """「查找一个新的、与10x同定位的数据库」——残差句关键词阶段认不出，
    但必须在 LLM 阶段被识别成管护动词 curate.search_online。"""
    calls = _reply_llm(monkeypatch, {"verb": "curate.search_online", "quoted": "与10x同定位的数据库",
                                     "confidence": "high"})
    res = client.post("/api/utterance", json={"utterance": "查找一个新的、与10x同定位的数据库", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool", body
    assert body["plan"]["verb"] == "curate.search_online" and body["plan"]["kind"] == "exec"
    assert len(calls) == 1, "这句话必须真的见到 LLM"


def test_exec_verb_never_carries_effective_query(monkeypatch):
    """LLM 手滑给执行动词也填了 effective_query → 机械剥掉（它不属检索槽）。"""
    _reply_llm(monkeypatch, {"verb": "pack.download", "quoted": "打包前5条", "limit": 5,
                             "effective_query": "打包前5条", "confidence": "high"})
    res = client.post("/api/utterance", json={
        "utterance": "帮我打包前5条", "has_results": True, "result_total": 42, **ARMED,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool"
    plan = body["plan"]
    assert plan["verb"] == "pack.download" and plan["source"] == "llm"
    assert plan["cancelled"] is False
    assert plan["effective_query"] == "", "执行类动词不许带检索句"


def test_string_slot_given_as_list_is_flattened():
    """模型偶发把字符串槽给成列表
    （例如把 keywords 给成 ["..."]）——build_plan_from_raw 必须拍平成
    空格连接的干净字符串，不许让 Python 列表语法（引号、方括号）灌进联网搜索词。"""
    plan = AP.build_plan_from_raw(
        {"verb": "curate.search_online", "quoted": "联网搜人类肺数据",
         "source": "ArrayExpress", "keywords": ["human", "lung"]},
        "联网搜人类肺数据入库", has_results=False, result_total=0,
    )
    assert plan["slots"]["keywords"] == "human lung"
    assert "[" not in plan["slots"]["keywords"] and "'" not in plan["slots"]["keywords"]


@pytest.mark.parametrize("utterance,payload,verb,cancelled", [
    # 歧义①：带否定 → LLM；极性门机械派生 cancelled（动词照留）。
    ("先别导入了", {"verb": "curate.import", "quoted": "导入", "confidence": "high"},
     "curate.import", True),
    # 歧义②：否定式改条件 → LLM 判 refine.conditions（路由类恒不取消）。
    ("不要小鼠的了", {"verb": "refine.conditions", "quoted": "不要小鼠的了",
                      "effective_query": "人类肺癌数据", "confidence": "high"},
     "refine.conditions", False),
])
def test_ambiguous_negated_utterances_take_the_llm_path(monkeypatch, utterance, payload, verb, cancelled):
    calls = _reply_llm(monkeypatch, payload)
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    plan = body["plan"]
    assert plan["verb"] == verb, (utterance, body)
    assert plan["cancelled"] is cancelled, (utterance, body)
    assert len(calls) == 1, "歧义句必须且只须调一次 LLM"


def test_identifier_fast_path_routes_search_without_llm(counting_llm):
    """编号快速道：贴编号/直链 → route="search"（query=原话，via=identifier），零 LLM。

    编号必须排在执行词前面：「把 E-MTAB-1234 打包」要先查出那一条，才谈得上打包它。"""
    res = client.post("/api/utterance", json={"utterance": "GSE123456", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search" and body["query"] == "GSE123456"
    assert body["via"] == "identifier"
    assert counting_llm == []


# ---------------------------------------------------- 「AI 执行」关（agent=false）：规则直达 + 降级气泡
#
# 维度 C 是 LLM 分流器的总闸（定稿）：关闭时分流器**永不启动**——
# 不拼装分流提示词、不发调用（计数器恒空），一切输入按规则检索处理；唯一例外是
# 规则（非 LLM）检出操作意图的句子：不静默当检索处理，回降级气泡指路「设置 → AI 执行」。

def test_agent_off_search_sentence_goes_rule_direct_with_zero_llm(counting_llm):
    """C 关：检索句原话直达（via=rule_direct），LLM 分流器零调用；规则匹配概览也不重复付
    （retrieval=None——真正的规则匹配由 /api/recommend 侧做）。"""
    res = client.post("/api/utterance", json={"utterance": "人类肺癌的单细胞数据", **ARMED, "agent": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search" and body["query"] == "人类肺癌的单细胞数据"
    assert body["via"] == "rule_direct"
    assert body["needs_agent"] is False
    assert body["retrieval"] is None, "规则直达不付规则匹配概览（检索侧自会做）"
    assert counting_llm == [], "C 关时 LLM 分流器必须永不启动"


@pytest.mark.parametrize("utterance,marker", [
    ("删除我上传的那份数据", "删除"),
    ("帮我打包前5条", "打包"),
    ("汇报数据库的当前状态", "汇报"),
    ("检查10x是否有更新", "是否有更新"),
    ("联网搜一下 ArrayExpress 有没有新的人类肺数据", "联网搜"),
    ("把删掉的文件找回", "删掉"),   # 最靠左命中优先：「删掉」在「找回」之前
    ("把这个数据集下载下来", "下载"),   # 真操作句：「下载」后随「下」，名词用法闸不放行
])
def test_agent_off_operation_sentence_gets_degradation_bubble(counting_llm, utterance, marker):
    """C 关 + 规则检出操作意图 → 降级气泡（route=none, needs_agent=true）：
    不静默当检索处理，回音引用认到的操作词并指路「AI 执行」。全程零 LLM。"""
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED, "agent": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", (utterance, body)
    assert body["needs_agent"] is True, (utterance, body)
    assert marker in body["echo_zh"], (utterance, body["echo_zh"])
    assert "AI 执行" in body["echo_zh"]
    assert body["via"] == "agent_off"
    assert body["plan"]["llm_status"] == "agent_off"
    assert counting_llm == [], "C 关时连降级检测也不许碰 LLM（规则检出，确定性）"


def test_agent_off_identifier_still_takes_the_fast_lane(counting_llm):
    """C 关 + 贴编号：编号快速道排在最前，不受闸影响（先查出那一条才谈得上操作它）。"""
    res = client.post("/api/utterance", json={"utterance": "E-MTAB-1234", **ARMED, "agent": False})
    body = res.json()
    assert body["route"] == "search" and body["via"] == "identifier"
    assert counting_llm == []


# ---------------------------------------------------- 编号快速道的管护操作闸

def test_identifier_with_curate_op_agent_off_gets_the_bubble(counting_llm):
    """C 关 + 「把 GSE123456 从我上传的里删掉」：快速道不再吃掉操作意图——
    落入正常分流，agent_off 分支回降级气泡（原来会被静默当检索，连气泡都拿不到）。"""
    res = client.post("/api/utterance",
                      json={"utterance": "把 GSE123456 从我上传的里删掉", **ARMED, "agent": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", body
    assert body["needs_agent"] is True
    assert body["via"] == "agent_off"
    assert "AI 执行" in body["echo_zh"]
    assert counting_llm == [], "C 关时 LLM 分流器必须永不启动"


def test_identifier_with_curate_op_reaches_the_llm_when_agent_on(monkeypatch):
    """C 开 + 「GSE123456 那套有没有更新」：不走快速道，正常过 LLM 分流，
    管护意图（检查更新）有机会被判成对应动词，不再被静默当检索。"""
    calls = _reply_llm(monkeypatch, {"verb": "curate.check_updates",
                                     "quoted": "GSE123456 那套有没有更新", "confidence": "high"})
    res = client.post("/api/utterance", json={"utterance": "GSE123456 那套有没有更新", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool", body
    assert body["plan"]["verb"] == "curate.check_updates"
    assert len(calls) == 1


@pytest.mark.parametrize("utterance", [
    "下载 GSE123456",         # 纯动作词 + 编号：快速道照旧（先查出那条才谈得上操作）
    "把 E-MTAB-1234 打包",    # 模块 docstring 论证过的形态，不收窄
])
def test_identifier_with_plain_action_verb_keeps_the_fast_lane(counting_llm, utterance):
    """C-3 的闸只拦管护操作短语：「编号 + 纯动作词」维持编号快速道（既有文档承诺不变）。"""
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search" and body["via"] == "identifier", (utterance, body)
    assert counting_llm == []


@pytest.mark.parametrize("utterance", [
    "下载量大的数据集有哪些",   # 「下载」后随「量」是名词用法，是检索句
    "只保留能下载的",           # query_parser 注释点名的另一句检索句（「下载」后随「的」）
])
def test_agent_off_noun_usage_action_verb_still_reaches_search(counting_llm, utterance):
    """动作词名词用法的检索句：不许被裸子串拦成降级气泡——
    原话直达检索（via=rule_direct）；气泡只拦真操作句（见上面的 degradation_bubble 钉）。"""
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED, "agent": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search", (utterance, body)
    assert body["query"] == utterance
    assert body["via"] == "rule_direct"
    assert body["needs_agent"] is False
    assert counting_llm == [], "C 关时 LLM 分流器必须永不启动"


def test_agent_on_report_status_sentence_reaches_the_llm(counting_llm):
    """病例句：「汇报数据库的当前状态」在 C 开时**必须**过 LLM 分流——
    旧行为里它在关键词阶段被毙掉、永远到不了 LLM（规则兜底 fail-open 成检索）。"""
    res = client.post("/api/utterance", json={"utterance": "汇报数据库的当前状态", **ARMED})
    assert res.status_code == 200, res.text
    assert 1 <= len(counting_llm) <= 2, "C 开时所有消息 100% 过 LLM 分流（失败允许重试一次）"


# ---------------------------------------------------- LLM 缺席时的规则兜底（search_shaped 单一真源）
#
# 无短路 ≠ 无兜底：LLM 缺席/失败时，长一张检索的脸的句子照旧按检索落地
# （弃权诚实卡如实摆出未收录词，比一句「没听懂」信息多）；零检索信号的歧义句
# （「帮我处理一下数据库」）与真否定句（「没有小鼠的数据」）绝不 fail-open 成检索。

@pytest.mark.parametrize("utterance", ["帮我处理一下数据库", "今天天气怎么样"])
def test_zero_signal_ambiguity_reaches_the_llm(counting_llm, utterance):
    """零检索信号的歧义句 → 也要过 LLM 护栏（武装时必须真调一次）。"""
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED})
    assert res.status_code == 200, res.text
    assert 1 <= len(counting_llm) <= 2, "歧义句必须落入 LLM 护栏路径（失败允许重试一次）"


@pytest.mark.parametrize("utterance", ["帮我处理一下数据库", "今天天气怎么样"])
def test_zero_signal_without_llm_falls_back_to_none(counting_llm, utterance):
    """无 LLM 时：规则兜底回 none。回音必须如实说「大模型没有接上」——
    说「没听懂」是谎：这句可能本就是有效诉求，用户改说法治不好连接问题。"""
    res = client.post("/api/utterance", json={"utterance": utterance, "use_llm": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", (utterance, body)
    assert body["plan"]["verb"] == "none" and body["plan"]["source"] == "rule"
    assert "大模型这次没有接上" in body["echo_zh"]
    assert "没有听懂" not in body["echo_zh"]
    assert counting_llm == []


@pytest.mark.parametrize("utterance", [
    "人类肺组织数据",
    # 存在性问句：「有没有 / 是否包含」里的「没有/否」不是排除操作符，不许误伤。
    "库里有没有斑马鱼的",
    "库里是否包含小鼠数据",
    # 「只」不是改动操作（无结果上下文规则认不出 op），这句长检索脸（信号「人」）。
    "只要人的",
])
def test_search_shaped_falls_back_to_search_without_llm(counting_llm, utterance):
    """LLM 空回（武装但假实现返回 None）→ 规则兜底：search_shaped → route="search"。"""
    res = client.post("/api/utterance", json={"utterance": utterance, **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search", (utterance, body)
    assert body["query"] == utterance and body["via"] == "rule_fallback"
    assert 1 <= len(counting_llm) <= 2, "兜底前必须先问过 LLM（无短路；失败允许重试一次）"


def test_curate_phrase_sentence_without_llm_stays_in_honest_echo(counting_llm):
    """LLM 缺席 + 管护短语句（「联网搜」）即使长检索脸也不许
    fail-open 成本地关键词检索（那是静默降级、零提示）——留在「大模型没有接上」诚实回音档。"""
    res = client.post("/api/utterance",
                      json={"utterance": "联网搜一下有没有新的人类肺数据", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", body
    assert body["plan"]["verb"] == "none" and body["plan"]["source"] == "rule"
    assert body["via"] == "rule"
    assert "大模型这次没有接上" in body["echo_zh"]
    assert body["suggestions"] == []
    assert 1 <= len(counting_llm) <= 2, "兜底前必须先问过 LLM（无短路；失败允许重试一次）"


def test_llm_none_verdict_keeps_the_honest_not_understood_echo(monkeypatch):
    """LLM **真判**的 none 照旧说「没听懂」——这与「没接上」是两种事实，不许混。"""
    _reply_llm(monkeypatch, {"verb": "none", "quoted": "", "confidence": "high", "reason": "闲聊"})
    res = client.post("/api/utterance", json={"utterance": "今天天气怎么样", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none"
    assert body["plan"]["source"] == "llm"
    assert body["echo_zh"] == "这句话我没有听懂，什么都没有做。"


def test_exec_downgraded_none_echoes_the_real_reason(monkeypatch):
    """EXEC 缺 quoted 被机械降 none → echo 如实说真实原因
    （plan.reason_zh），不谎称「没听懂」——系统明明读懂了，用户改说法解决不了。"""
    _reply_llm(monkeypatch, {"verb": "pack.download", "quoted": "", "confidence": "high",
                             "reason": "要打包"})
    res = client.post("/api/utterance", json={"utterance": "帮我打包一下",
                                              "has_results": True, "result_total": 5, **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", body
    assert body["plan"]["verb"] == "none" and body["plan"]["source"] == "llm"
    assert body["plan"]["downgraded_from"] == "pack.download"
    assert "没有听懂" not in body["echo_zh"]
    assert "原文依据" in body["echo_zh"]


# ---------------------------------------------------------------- 婉拒候选 chips

def test_llm_none_carries_suggestion_chips(monkeypatch):
    """LLM 真判 none（死胡同）→ suggestions 非空：2~3 颗机械候选，每颗 {label, utterance}，
    utterance 都是封闭动词表够得着的真实动作（分流代替硬拒，零幻觉）。"""
    _reply_llm(monkeypatch, {"verb": "none", "quoted": "", "confidence": "high", "reason": "闲聊"})
    res = client.post("/api/utterance", json={"utterance": "今天天气怎么样", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    sug = body["suggestions"]
    assert 2 <= len(sug) <= 3
    for item in sug:
        assert item["label"] and item["utterance"]
    assert any("数据库" in s["utterance"] for s in sug)


def test_llm_none_suggestions_include_pack_when_results_on_screen(monkeypatch):
    """屏上有结果时，候选里多一颗「打包当前结果」（has_results 上下文的机械规则）。"""
    _reply_llm(monkeypatch, {"verb": "none", "quoted": "", "confidence": "high", "reason": "闲聊"})
    res = client.post("/api/utterance", json={
        "utterance": "今天天气怎么样", "has_results": True, "result_total": 12, **ARMED})
    assert res.status_code == 200, res.text
    sug = res.json()["suggestions"]
    assert len(sug) == 3
    assert any("打包" in s["label"] for s in sug)


def test_rule_fallback_none_has_no_suggestions(counting_llm):
    """LLM 缺席的规则兜底 none → suggestions 恒空：管护动词没有大模型到场判不了，
    给候选也是死路（如实，不用死路引导用户）。"""
    res = client.post("/api/utterance", json={"utterance": "今天天气怎么样", "use_llm": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none"
    assert body["suggestions"] == []


def test_needs_agent_degrade_has_no_suggestions():
    """「AI 执行」关的降级气泡 → suggestions 恒空（指路按钮已在，不再给候选）。"""
    res = client.post("/api/utterance", json={"utterance": "把结果打包下载", "agent": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["needs_agent"] is True
    assert body["suggestions"] == []


def test_search_and_tool_routes_have_empty_suggestions(monkeypatch):
    """search / tool 路由恒带空 suggestions（契约形状稳定，前端不用猜字段在不在）。"""
    _reply_llm(monkeypatch, {"verb": "search.new", "quoted": "人类肺癌的单细胞数据",
                             "effective_query": "人类肺癌的单细胞数据", "confidence": "high"})
    res = client.post("/api/utterance", json={"utterance": "人类肺癌的单细胞数据", **ARMED})
    assert res.status_code == 200, res.text
    assert res.json()["route"] == "search"
    assert res.json()["suggestions"] == []

    _reply_llm(monkeypatch, {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
                             "confidence": "high", "reason": "查官方源更新"})
    res = client.post("/api/utterance", json={"utterance": "检查10x是否有更新", **ARMED})
    assert res.status_code == 200, res.text
    assert res.json()["route"] == "tool"
    assert res.json()["suggestions"] == []



def test_transient_llm_failure_is_retried_once(monkeypatch):
    """第一次空回、第二次正常 → route 正常落地（llm_status=ok），且恰好调了两次：
    瞬时抖动不再直接掉规则档（执行侧「触发不稳定」的主因）。"""
    calls: list[str] = []

    def flaky(prompt, config):
        calls.append(prompt)
        if len(calls) == 1:
            return None
        return json.dumps({"verb": "curate.search_online", "quoted": "检查10x是否有更新",
                           "confidence": "high", "reason": "查官方源更新"}, ensure_ascii=False)

    monkeypatch.setattr(AP, "_default_llm_call", flaky)
    monkeypatch.setattr(AP, "_RETRY_BACKOFF_SECONDS", 0)   # 测试不为退避付真实等待
    res = client.post("/api/utterance", json={"utterance": "检查10x是否有更新", **ARMED})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool", body
    assert body["plan"]["verb"] == "curate.search_online"
    assert body["plan"]["llm_status"] == "ok"
    assert len(calls) == 2


def test_negated_search_without_llm_stays_none(counting_llm):
    """真否定（非存在性问句）→ 规则兜底 none：存在性豁免不许误伤真否定。"""
    res = client.post("/api/utterance", json={"utterance": "没有小鼠的数据", "use_llm": False})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "none", body
    assert body["plan"]["verb"] == "none"
    assert counting_llm == []


# ---------------------------------------------------------------- 兜底判据纯函数（单一真源 board.search_shaped）

def test_search_shaped_predicate_matrix():
    """兜底判据是纯函数、可单测：信号 / 否定 / 改动操作三条的逐格矩阵。"""
    from dataset_recommender.app import board

    for text in ("人类肺组织数据", "库里有没有斑马鱼的", "GSE131907", "只要人的",
                 "integrated human lung atlas"):
        assert board.search_shaped(text) is True, text
    for text in ("帮我处理一下数据库", "今天天气怎么样", "？？？", "",
                 "换成小鼠",            # 改动操作 → LLM 分流处理，绝不兜底成检索
                 "不要小鼠的了", "没有小鼠的数据"):   # 真否定 → 护栏回音，绝不兜底成检索
        assert board.search_shaped(text) is False, text


def test_search_shaped_honors_deployment_keyword_mapping():
    """部署方经 KEYWORD_MAPPING_PATH 注入的说法也是系统真的认识的词——不透传就误判成零信号。"""
    from dataset_recommender.app import board

    mapping = {"tissue": [{"aliases": ["普罗米修斯"], "targets": ["prometheus"], "display": "Prometheus"}]}
    assert board.search_shaped("来点普罗米修斯数据") is False
    assert board.search_shaped("来点普罗米修斯数据", keyword_mapping=mapping) is True


# ---------------------------------------------------------------- LLM 延迟控制

def test_use_llm_false_action_branch_is_rule_fallback_all_the_way(counting_llm):
    """`use_llm=false`：工具句走 fail-open 规则档（只开清单的 pack.preview），全程零网络。"""
    res = client.post("/api/utterance", json={
        "utterance": "帮我打包前5条", "has_results": True, "result_total": 42, "use_llm": False,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "tool"
    plan = body["plan"]
    assert plan["source"] == "rule"
    assert plan["llm_status"] in {"disabled", "no_key", "mock_not_used"}, plan["llm_status"]
    assert plan["verb"] == "pack.preview", "规则档绝不直接产文件"
    assert counting_llm == []


def test_llm_error_fails_open_to_rules(monkeypatch):
    """LLM 抛异常 → 规则兜底照现有（fail-open），端点不 5xx。"""
    def boom(prompt, config):
        raise RuntimeError("sk-should-not-leak 这个 key 无效")

    monkeypatch.setattr(AP, "_default_llm_call", boom)
    res = client.post("/api/utterance", json={"utterance": "帮我打包前5条", "has_results": True,
                                              "result_total": 3, **ARMED})
    assert res.status_code == 200, res.text
    plan = res.json()["plan"]
    assert plan["source"] == "rule" and plan["llm_status"] == "error:RuntimeError"
    assert "sk-should-not-leak" not in res.text


# ---------------------------------------------------------------- 入参纪律（与既有端点一致）

def test_endpoint_rejects_unknown_fields():
    res = client.post("/api/utterance", json={"utterance": "打包", "use_llm": False, "limitt": 5})
    assert res.status_code == 422


def test_endpoint_refuses_empty_and_oversized_utterance():
    assert client.post("/api/utterance", json={"utterance": "", "use_llm": False}).status_code == 422
    res = client.post("/api/utterance", json={
        "utterance": "打" * (AP.MAX_UTTERANCE_CHARS + 1), "use_llm": False,
    })
    assert res.status_code == 400 and "太长" in res.json()["detail"]


def test_endpoint_rejects_cross_origin_posts():
    res = client.post("/api/utterance", json={"utterance": "打包", "use_llm": False},
                      headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_endpoint_does_not_echo_the_submitted_api_key():
    res = client.post("/api/utterance", json={
        "utterance": "帮我打包", "use_llm": False, "api_key": "sk-should-never-come-back",
    })
    assert res.status_code == 200
    assert "sk-should-never-come-back" not in res.text


def test_existing_action_plan_endpoint_shape_is_unchanged():
    """`/api/action/plan`（外部/MCP 兼容保留）既有响应形状逐位不变。"""
    res = client.post("/api/action/plan", json={
        "utterance": "帮我打包前20条", "has_results": True, "result_total": 42, "use_llm": False,
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"ok", "plan"}
    assert body["plan"]["verb"] == "pack.preview"


# ---------------------------------------------------------------- 流式（SSE）

def _sse_events(res) -> list[dict]:
    """把 text/event-stream 解析成事件列表（每帧 `data: {json}`）。"""
    events: list[dict] = []
    for line in res.iter_lines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))
    return events


#: agent 路径的固定剧本：三个节点步骤 + 一份 EXEC plan（与 test_agent_exec 的 trace 契约同形）。
_STREAM_STEPS = [
    {"node": "understand", "label_zh": "理解意图", "detail": "工具调用模式，判为 curate.check_updates。",
     "ok": True, "ms": 1},
    {"node": "validate", "label_zh": "护栏校验", "detail": "通过：curate.check_updates（检查来源更新）。",
     "ok": True, "ms": 1},
    {"node": "narrate", "label_zh": "生成说明", "detail": "curate.check_updates（检查来源更新）",
     "ok": True, "ms": 1},
]


def _stub_agent_path(monkeypatch):
    """把 agent 路径桩成确定剧本（零网络）：available=True、events/薄封装共用同一份 plan。"""
    monkeypatch.setattr(webapp, "load_llm_config",
                        lambda *a, **k: LLMConfig(enable_llm=True, mock_llm=False,
                                                  provider="zhipuai", api_key="sk-req"))
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)

    def _make_plan(utterance):
        plan = AP.build_plan_from_raw(
            {"verb": "curate.check_updates", "quoted": "检查10x是否有更新", "source": "10x",
             "confidence": "high", "reason": "查更新"},
            utterance, has_results=False, result_total=0, llm_status="ok",
        )
        plan["source"] = "agent"
        plan["trace"] = [dict(s) for s in _STREAM_STEPS]
        return plan

    def fake_events(utterance, *, on_event=None, **kwargs):
        assert on_event is not None, "流式路径必须把 on_event 透传下来"
        for entry in _STREAM_STEPS:
            on_event("step", dict(entry))
        plan = _make_plan(utterance)
        return plan, plan["trace"]

    def fake_plain(utterance, **kwargs):
        plan = _make_plan(utterance)
        return plan, plan["trace"]

    monkeypatch.setattr(agent_exec, "plan_with_agent_events", fake_events)
    monkeypatch.setattr(agent_exec, "plan_with_agent", fake_plain)


def test_stream_agent_path_emits_steps_then_final(monkeypatch):
    """stream=true ∧ agent 在场：事件序 step* → final；final 体与非流式**逐位同形**。"""
    _stub_agent_path(monkeypatch)
    payload = {"utterance": "检查10x是否有更新", "stream": True,
               "provider": "zhipuai", "use_llm": True, "api_key": "sk-req"}
    with client.stream("POST", "/api/utterance", json=payload) as res:
        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(res)
    assert [e["event"] for e in events] == ["step", "step", "step", "final"]
    steps = [e["data"] for e in events[:-1]]
    assert [s["node"] for s in steps] == ["understand", "validate", "narrate"]
    assert [s["label_zh"] for s in steps] == ["理解意图", "护栏校验", "生成说明"]
    for entry in steps:
        assert set(entry) == {"node", "label_zh", "detail", "ok", "ms"}

    final = events[-1]["data"]
    assert set(final) == {"ok", "route", "query", "plan", "echo_zh", "retrieval", "via", "needs_agent", "agent", "suggestions",
                          # （additive）：环内采纳载荷 + 初步结果即终判旗标
                          "result_payload", "preliminary_final",
                          # （additive）：tool 路线 retrieval=None 时带
                          # retrieval_note（breaking 重钉契约形状）
                          "retrieval_note",
                          # trace turn id（additive；报障给号）
                          "trace_turn_id"}
    assert final["ok"] is True
    assert final["route"] == "tool"
    assert final["via"] == "agent"
    assert final["agent"] == {"available": True, "used": True}
    # 前端去重契约：final.plan.trace 与已播步骤逐条对应（流式播过的不再二次渲染）
    assert final["plan"]["trace"] == steps

    # 同请求走非流式（agent 同桩）：final 体与非流式响应逐位相等
    plain_payload = {k: v for k, v in payload.items() if k != "stream"}
    plain = client.post("/api/utterance", json=plain_payload)
    assert plain.status_code == 200, plain.text
    # trace_turn_id 每轮唯一（两请求各一份 trace）——同形比对前剥除。
    plain_body = plain.json()
    assert final["trace_turn_id"] and plain_body["trace_turn_id"]
    final.pop("trace_turn_id")
    plain_body.pop("trace_turn_id")
    assert final == plain_body


def test_stream_fallback_path_emits_only_final(monkeypatch):
    """agent 不可用：保底路径没有节点可播，只有 final——前端维持非流式的百分比画像。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: False)
    with client.stream("POST", "/api/utterance",
                       json={"utterance": "今天天气怎么样", "stream": True}) as res:
        assert res.status_code == 200, res.text
        assert res.headers["content-type"].startswith("text/event-stream")
        events = _sse_events(res)
    assert [e["event"] for e in events] == ["final"]
    final = events[0]["data"]
    assert final["ok"] is True
    assert final["route"] == "none"
    assert final["agent"]["available"] is False
    assert final["agent"]["used"] is False


def test_stream_still_enforces_same_origin_and_forbid(monkeypatch):
    """流式分支不绕开入参纪律：跨源 403、未知字段 422（extra=forbid 对 stream 字段以外仍生效）。"""
    res = client.post("/api/utterance",
                      json={"utterance": "今天天气怎么样", "stream": True},
                      headers={"Origin": "http://evil.example.com"})
    assert res.status_code == 403
    res2 = client.post("/api/utterance",
                       json={"utterance": "今天天气怎么样", "stream": True, "bogus": 1})
    assert res2.status_code == 422
