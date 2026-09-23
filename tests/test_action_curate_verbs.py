# -*- coding: utf-8 -*-
"""action_plan 管护动词（curate.list / curate.import / curate.search_online / curate.remove）的确定性门。

 删除类以**回收站式可逆删除**为前提纳入封闭动词表
（设计理由写进 VERB_SPECS 上方注释）。
本层依旧**只出 plan、不执行**：真正执行由调用方走 `/api/curate/*` / MCP `curate_datasets` /
CLI `scripts/curate_datasets.py`。

钉死：新动词进表（EXEC 类、program-generated 动词表同步进 prompt）；quoted 缺失降级 none；
极性门对「别删」标 cancelled（动词照留、执行层不执行）；词表外只报不做；管护动词不作用在
屏上结果上（requires_results=False、不带 target 槽、无结果不 blocked）。
"""
import json

import pytest

from dataset_recommender.agent import action_plan as AP

CURATE_VERBS = ("curate.list", "curate.import", "curate.search_online", "curate.remove",
                "curate.restore", "curate.check_updates", "curate.db_status", "curate.sync_updates")


def _llm(payload):
    return lambda _prompt: json.dumps(payload, ensure_ascii=False)


def _plan(utterance, payload, *, has_results=False, result_total=0):
    return AP.plan_action(
        utterance, has_results=has_results, result_total=result_total, llm_call=_llm(payload)
    )


# ---------------------------------------------------------------- 新动词进表

def test_curate_verbs_are_in_the_closed_table_as_exec():
    for verb in CURATE_VERBS:
        assert verb in AP.VERB_BY_NAME, verb
        spec = AP.VERB_BY_NAME[verb]
        assert spec.kind == AP.EXEC, verb
        assert spec.requires_results is False, "管护动词作用对象是外部库/回收站，不是屏上结果"
    assert set(CURATE_VERBS) <= set(AP.EXEC_VERBS)


def test_curate_verbs_reach_the_program_generated_prompt():
    """动词表由 VERB_SPECS 程序生成：新动词必须自动进 prompt（手抄必漂移）。"""
    prompt = AP.build_action_prompt("随便一句", has_results=False, result_total=0)
    for verb in CURATE_VERBS:
        assert verb in prompt, verb


def test_curate_exec_names_read_like_verb_phrases():
    """回执抬头是「已」+ zh：管护动词的中文名必须以 _LEAD_VERBS 里的动词开头。"""
    for verb in CURATE_VERBS:
        zh = AP.VERB_BY_NAME[verb].zh
        assert any(zh.startswith(v) for v in AP._LEAD_VERBS), f"已{zh} 读不通"


def test_curate_verbs_have_graduated_from_the_frontend_exemption_list():
    """curate.* 已全部从前端豁免清单**毕业**：前四动词已接线、restore
    随统一对话窗口接线、check_updates 亦随后接线
    （act.js `actRunCurateCheckUpdates`，只读无问卷），派发表闸恢复全量。
    豁免机制本身保留：常量仍在、必须是 EXEC 子集——将来新动词可凭独立执行入口加回。"""
    assert not (set(CURATE_VERBS) & set(AP.FRONTEND_UNWIRED_EXEC_VERBS)), (
        "curate.* 前端已接线，不该再留在 FRONTEND_UNWIRED_EXEC_VERBS 豁免清单里"
    )
    assert set(AP.FRONTEND_UNWIRED_EXEC_VERBS) <= set(AP.EXEC_VERBS)


# ---------------------------------------------------------------- 只出 plan 的语义

def test_curate_verbs_plan_without_results_and_without_target_slot():
    """管护动词不需要屏上有结果（不 blocked）、也不带 target="results" 槽。"""
    plan = _plan("把那个上传的文件删掉", {"verb": "curate.remove", "quoted": "删掉", "confidence": "high"})
    assert plan["verb"] == "curate.remove"
    assert plan["kind"] == AP.EXEC
    assert plan["blocked_reason"] == "", "管护动词不被 no_results 挡住"
    assert "target" not in plan["slots"], "管护动词不作用在屏上结果上，不带 target 槽"
    assert plan["uncertainty_zh"], "执行类恒带「没有另外核对」标注"


def test_curate_list_is_auto_able():
    """curate.list 是纯只读清点：授权语义上可自动执行（与需确认的写动作区分）。"""
    plan = _plan("看看我上传了哪些数据", {"verb": "curate.list", "quoted": "看看我上传了哪些",
                                         "confidence": "high"})
    assert plan["verb"] == "curate.list"
    assert plan["blocked_reason"] == ""


# ---------------------------------------------------------------- quoted 缺失降级

def test_curate_verb_without_quote_degrades_to_none():
    """执行类没有可定位的原文依据就不执行——管护动词同守这条铁律。"""
    plan = _plan("嗯", {"verb": "curate.remove", "quoted": "", "confidence": "high"})
    assert plan["verb"] == "none"
    assert "原文依据" in plan["reason_zh"]


def test_curate_verb_with_fabricated_quote_degrades_to_none():
    plan = _plan("帮我整理一下", {"verb": "curate.import", "quoted": "导入这个文件", "confidence": "high"})
    assert plan["quoted"] == ""
    assert plan["verb"] == "none"


# ---------------------------------------------------------------- 极性门 + 取消态

@pytest.mark.parametrize("utterance,quoted,verb", [
    ("别删那个文件", "删", "curate.remove"),
    ("那个上传的文件不要删", "删", "curate.remove"),
    ("先不用导入", "导入", "curate.import"),
    ("别联网搜了", "联网搜", "curate.search_online"),
])
def test_negated_curate_action_is_never_treated_as_authorization(utterance, quoted, verb):
    """「别删」里的「删」绝不能被当成删除授权引用给用户看。

     口径升级：动词照判 + `cancelled=True`，
    执行层据此不执行、只回音（「好，不删了」）——不再整计划降 none 装没听懂。
    """
    plan = _plan(utterance, {"verb": verb, "quoted": quoted, "confidence": "high"})
    assert plan["verb"] == verb, (utterance, plan)
    assert plan["cancelled"] is True, (utterance, plan)
    assert plan["quoted"] == quoted, "回执要能指着原话说「好，不做了」，依据不许丢"


def test_negation_elsewhere_does_not_block_curate_remove():
    """「不要小鼠的那条，删掉另一个上传」里「删掉」没被否定，仍是删除诉求。"""
    plan = _plan("不要小鼠那条，删掉另一个上传",
                 {"verb": "curate.remove", "quoted": "删掉另一个上传", "confidence": "high"})
    assert plan["verb"] == "curate.remove", plan
    assert plan["cancelled"] is False


# ---------------------------------------------------------------- curate.restore（补表：实验发现的真实缺口）

def test_curate_restore_is_in_the_closed_table_as_exec():
    spec = AP.VERB_BY_NAME.get("curate.restore")
    assert spec is not None, "curate.restore 不在封闭词表——回收站恢复诉求无处可路由"
    assert spec.kind == AP.EXEC
    assert spec.requires_results is False, "管护动词作用对象是回收站/外部库，不是屏上结果"
    prompt = AP.build_action_prompt("随便一句", has_results=False, result_total=0)
    assert "curate.restore" in prompt, "动词表程序生成 prompt：restore 必须自动进 prompt"


def test_curate_restore_plans_like_other_curate_verbs():
    plan = _plan("把刚删掉的上传文件恢复回来",
                 {"verb": "curate.restore", "quoted": "恢复", "confidence": "high"})
    assert plan["verb"] == "curate.restore"
    assert plan["kind"] == AP.EXEC
    assert plan["cancelled"] is False
    assert plan["blocked_reason"] == ""
    assert "target" not in plan["slots"]


def test_negated_restore_is_cancelled_not_executed():
    plan = _plan("先别恢复那个文件", {"verb": "curate.restore", "quoted": "恢复", "confidence": "high"})
    assert plan["verb"] == "curate.restore"
    assert plan["cancelled"] is True


def test_curate_restore_has_graduated_from_the_frontend_exemption_list():
    """restore 的前端 runner 已随统一对话窗口接线（act.js `actRunCurateRestore`：
    list 回收站 → 选择 → plan 预览 → 面板确认 → apply 回传 confirm_token）——按豁免机制的
    约定，接线完成时必须把它移出清单（`tests/test_act_frontend.py` 派发表闸已在盯）。
    豁免机制本身保留：常量仍在、必须是 EXEC 子集——将来新动词可凭独立执行入口加回。
    `curate.check_updates`亦已随同批前端改动接线毕业
    （act.js `actRunCurateCheckUpdates`，只读无问卷）。
     刻意更新：search.rerun 凭**独立执行入口**登记进
    豁免清单——独立入口 = langgraph 图内 LOOP_TOOLS 工具 + `/api/agent/search-rescue`
    端点。**永久豁免、不毕业**——前端只接步骤卡渲染
    （act.js actSearchRerunCardHtml），刻意不写 runner：本工具的采纳由后端机械择优闸
    裁定（改空/同集如实拒绝），前端 runner 直打 /api/recommend 会绕过择优闸换屏。
     钉字更新：curate.rollback（rollback 动词化批）凭同哲学入清单——
    回滚目标由机械闸从本轮 steps 实录里的快照锚现定，前端单步 runner 既没有这个现场、
    也没有快照锚可传，永久豁免；前端只走 act.js 通用兜底渲染，零新增卡片。
     钉字更新：results.show（2026-09-13 rerank 处置批）凭同哲学入清单——屏态唯一出口，
    handle 句柄指认环内试跑实录并重放确定性管线+指纹复核，前端没有这个现场，环内专属。
     钉字更新：ask.relax（2026-09-14 YOLO 批）凭同哲学入清单——「问用户放松哪个条件」
    选择卡由 utterance 响应的 ask_relax 键驱动，无前端 runner 独立入口，环内专属。"""
    assert "curate.restore" not in AP.FRONTEND_UNWIRED_EXEC_VERBS, (
        "curate.restore 前端已接线，不该再留在 FRONTEND_UNWIRED_EXEC_VERBS 豁免清单里"
    )
    assert AP.FRONTEND_UNWIRED_EXEC_VERBS == ("search.rerun", "rank", "rerank",
                                             "results.show", "curate.rollback",
                                             "compare.datasets", "compat.find",
                                             "fair.check", "ask.relax"), (
        "六个管护动词已全部毕业；清单应只含永久豁免的 search.rerun 与环内专属的 "
        "rank/rerank/results.show（均无前端 runner——results.show 是屏态唯一出口，"
        "句柄指认环内试跑实录，前端单步 runner 无此现场）以及 "
        "curate.rollback（目标靠本轮 steps 快照锚，前端无现场），"
        "以及 compare.datasets / compat.find / fair.check（"
        "默认对象靠环内当前结果集现场，前端单步 runner 无此现场），"
        "以及 ask.relax（选择卡依赖环内屏面基线与预演现场，无前端 runner 入口）"
    )
    assert set(AP.FRONTEND_UNWIRED_EXEC_VERBS) <= set(AP.EXEC_VERBS)


# ---------------------------------------------------------------- 词表外只报不做

def test_verbs_outside_the_table_are_rejected_not_executed():
    """表外动词（哪怕名字像管护动作）一律进 rejected、不执行。"""
    plan = _plan("把整个库清空", {"verb": "curate.delete_all", "quoted": "清空", "confidence": "high"})
    assert plan["verb"] == "none"
    assert plan["rejected"] == ["curate.delete_all"]


# ---------------------------------------------------------------- curate.check_updates

def test_check_updates_is_in_the_closed_table_as_exec():
    """「检查更新」从 search_online 剥出、各自专职（设计约定）。"""
    spec = AP.VERB_BY_NAME.get("curate.check_updates")
    assert spec is not None
    assert spec.kind == AP.EXEC
    assert spec.requires_results is False, "管护动词作用对象是来源/快照，不是屏上结果"
    assert "source" in spec.slots
    assert any(spec.zh.startswith(v) for v in AP._LEAD_VERBS), f"已{spec.zh} 读不通"
    prompt = AP.build_action_prompt("随便一句", has_results=False, result_total=0)
    assert "curate.check_updates" in prompt, "动词表程序生成 prompt：check_updates 必须自动进 prompt"


def test_search_online_no_longer_claims_the_check_updates_semantics():
    """问题修复的钉死：search_online 的 when_zh 不再把「检查某个库有没有更新」并进自己的语义
    （旧文案逐字片段不许回来；允许以「不是它」的排除形式提到 check_updates）。"""
    when = AP.VERB_BY_NAME["curate.search_online"].when_zh
    assert "检查某个库有没有更新" not in when
    assert "检查 10x 数据库有没有更新" not in when
    assert "check_updates" in when, "必须显式指路给专职动词，防语义再次被并回"


def test_check_updates_carries_the_source_slot():
    plan = _plan("检查10x是否有更新",
                 {"verb": "curate.check_updates", "quoted": "检查10x是否有更新",
                  "source": "10x", "confidence": "high"})
    assert plan["verb"] == "curate.check_updates"
    assert plan["kind"] == AP.EXEC
    assert plan["slots"]["source"] == "10x"
    assert plan["slot_sources"]["source"] == "said", "「10x」逐字出现在原话里，是用户说的"
    assert plan["blocked_reason"] == ""


def test_check_updates_negated_is_cancelled_not_executed():
    plan = _plan("先别检查更新了", {"verb": "curate.check_updates", "quoted": "检查更新",
                                   "confidence": "high"})
    assert plan["verb"] == "curate.check_updates"
    assert plan["cancelled"] is True
