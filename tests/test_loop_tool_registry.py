# -*- coding: utf-8 -*-
"""LOOP_TOOLS 注册表一致性闸（「工作流即工具」；蓝本 pydantic-ai 的
`require_parameter_descriptions`——把文档/注册表腐烂变成构建期错误，而不是运行时静默降级）。

钉四条：
  1. `LOOP_TOOLS` 与 `LOOP_RESULT_MODELS` 键集**锁步**——execute 的形状闸按 verb 查模型，
     少登记一个 = 该工具的返回形状无人把守（静默缺口）；
  2. 每个 LOOP_TOOLS 注册项字段齐整（label_zh / card_kind / decide_zh 非空、readonly 是 bool），
     且动词在封闭动词表里是 EXEC 类——decide 清单、卡片渲染、回执抬头全从注册表程序生成，
     字段缺了不会报错，只会悄悄渲染成半成品；
  3. `_DECIDE_VERB_ORDER` 与注册表同集合（decide prompt 的工具清单不漏不臆造）；
  4. 每个动词声明的槽位都有**专职**描述（`_SLOT_DESCRIPTIONS_ZH`；limit 为内置槽豁免）——
     回退泛模板是「问题温床」时期的兜底（问题：泛泛模板让 LLM 随手填
     唯一眼熟的源），新槽位没写专职描述 = 构建期就红。
  5. 非流式前端 fallback 的 `FLOW_VERB_LABEL` 与后端注册表中文名逐项相等；
     `route.request` 是路由元事件，不渲染工具行，故唯一排除。
"""
import re
from pathlib import Path

from dataset_recommender.agent import action_plan as AP
from dataset_recommender.agent import agent_exec
from dataset_recommender.agent import agent_schemas


ROOT = Path(__file__).resolve().parents[1]


def test_loop_tools_and_result_models_are_in_lockstep():
    assert set(agent_exec.LOOP_TOOLS) == set(agent_schemas.LOOP_RESULT_MODELS), (
        f"形状闸登记处与工具注册表漂移："
        f"无形状闸 {set(agent_exec.LOOP_TOOLS) - set(agent_schemas.LOOP_RESULT_MODELS)}，"
        f"无工具 {set(agent_schemas.LOOP_RESULT_MODELS) - set(agent_exec.LOOP_TOOLS)}"
    )


def test_every_loop_tool_entry_is_complete_and_exec():
    for verb, entry in agent_exec.LOOP_TOOLS.items():
        for field in ("label_zh", "card_kind", "decide_zh"):
            assert isinstance(entry.get(field), str) and entry[field].strip(), f"{verb}.{field} 为空"
        assert isinstance(entry.get("readonly"), bool), f"{verb}.readonly 必须是 bool"
        assert callable(entry.get("run")), f"{verb}.run 不可调用"
        spec = AP.VERB_BY_NAME.get(verb)
        assert spec is not None, f"{verb} 不在封闭动词表"
        # 转正：route.request 是注册表里**唯一**的 ROUTE 类成员（环内元动词——
        # execute 据它换线回写，不是真执行动作；显式豁免，其余仍必须 EXEC）。
        if verb == "route.request":
            assert spec.kind == AP.ROUTE, f"{verb} 是环内换线元动词（ROUTE 类）"
            continue
        assert spec.kind == AP.EXEC, f"{verb} 必须是 EXEC 类（图内真执行）"


def test_decide_verb_order_matches_registry():
    assert set(agent_exec._DECIDE_VERB_ORDER) == set(agent_exec.LOOP_TOOLS)
    assert len(agent_exec._DECIDE_VERB_ORDER) == len(set(agent_exec._DECIDE_VERB_ORDER)), "顺序表有重复"


def test_every_declared_slot_has_a_dedicated_description():
    for spec in AP.VERB_SPECS:
        for slot in spec.slots:
            if slot == "limit":
                continue  # 内置槽：上下界/描述在 _args_model_for 里内联（与裁决层同源）
            assert slot in agent_schemas._SLOT_DESCRIPTIONS_ZH, (
                f"{spec.verb} 的槽位 {slot} 没有专职描述——会回退泛模板（2026-08-03 的病灶温床）"
            )


def test_flow_trace_fallback_labels_match_loop_registry():
    text = (ROOT / "web/static/js/core/flow_trace.js").read_text(encoding="utf-8")
    match = re.search(r"const FLOW_VERB_LABEL = \{(.*?)\};", text, re.DOTALL)
    assert match, "flow_trace.js 缺少 FLOW_VERB_LABEL fallback 表"
    actual = dict(re.findall(r'"([^"]+)"\s*:\s*"([^"]+)"', match.group(1)))
    expected = {
        verb: entry["label_zh"]
        for verb, entry in agent_exec.LOOP_TOOLS.items()
        if verb != "route.request"
    }
    assert actual == expected
