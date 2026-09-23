# -*- coding: utf-8 -*-
"""数据库状态汇报（`curate.db_status`）的确定性门：

- `corpus_status.db_status`：只读离线、不抛（空项目根也如实降级）、形状稳定；
- `/api/curate/status`：same-origin 闸 + 同一份事实真源；
- agent execute 节点（取代 observe 只读节点；LOOP_TOOLS 注册表）：命中 →
  图内真跑工具 + plan.observation/report_zh + execute/decide trace 步骤；未命中 → 空过不伪造步骤；
- 动词注册：EXEC、不需结果、回执抬头读得通（_LEAD_VERBS）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langgraph", reason="langchain 扩展未安装：execute 节点测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec
from dataset_recommender.corpus import corpus_status  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")
CFG = LLMConfig(enable_llm=True, api_key="sk-status-test")


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch):
    """图内执行的审计落账改 noop——本文件的 db_status 跑的是真工具（只读），
    但审计行绝不许写进真实账本。"""
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)


# ---------------------------------------------------------------- corpus_status 能力本体

def test_db_status_shape_is_stable_and_offline():
    s = corpus_status.db_status()
    assert set(s) == {"generated_at", "sources", "total_records", "external_files", "recycle", "ledger"}
    assert s["total_records"] == sum(int(x["local_count"]) for x in s["sources"])
    assert len(s["sources"]) >= 6, "六源注册表（10x/CELLxGENE/ArrayExpress/HCA/EBI SCEA/ENCODE）"
    for x in s["sources"]:
        assert set(x) == {"source", "label", "local_count", "snapshot_date"}
        assert isinstance(x["local_count"], int)
    assert isinstance(s["external_files"], list) and isinstance(s["recycle"], list)
    assert set(s["ledger"]) == {"entries", "by_endpoint", "recent"}
    assert isinstance(s["ledger"]["entries"], int)


def test_db_status_empty_root_degrades_without_throwing(tmp_path: Path):
    """空项目根（没有快照/外部库/账本）→ 各部分如实空缺，绝不抛（边界：不掀翻整份汇报）。"""
    s = corpus_status.db_status(project_root=tmp_path)
    assert all(x["local_count"] == 0 for x in s["sources"])
    assert s["total_records"] == 0
    assert s["external_files"] == [] and s["recycle"] == []
    assert s["ledger"]["entries"] == 0 and s["ledger"]["recent"] == []


def test_ledger_summary_reads_tail_only(tmp_path: Path):
    """账本摘要：尾部窗口计数 + by_endpoint 聚合 + recent 紧凑回显；坏行跳过不毒化。"""
    ud = tmp_path / ".userdata"
    ud.mkdir()
    lines = [json.dumps({"ts": f"2026-08-03T10:{i:02d}:00", "endpoint": "ep-a", "query": f"q{i}", "records": i})
             for i in range(5)]
    lines.insert(2, "{bad json")
    (ud / "curate_net_ledger.jsonl").write_text("\n".join(lines), encoding="utf-8")
    s = corpus_status.db_status(project_root=tmp_path)
    assert s["ledger"]["entries"] == 5
    assert s["ledger"]["by_endpoint"] == {"ep-a": 5}
    assert len(s["ledger"]["recent"]) == 3
    assert s["ledger"]["recent"][-1]["records"] == 4


# ---------------------------------------------------------------- 端点

def test_status_endpoint_shape_and_same_origin():
    res = client.post("/api/curate/status")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert set(body["result"]) == {"generated_at", "sources", "total_records",
                                   "external_files", "recycle", "ledger"}
    res2 = client.post("/api/curate/status", headers={"Origin": "https://evil.example"})
    assert res2.status_code == 403


# ---------------------------------------------------------------- 动词注册

def test_db_status_verb_is_registered_as_readonly_exec():
    spec = AP.VERB_BY_NAME["curate.db_status"]
    assert spec.kind == AP.EXEC
    assert spec.requires_results is False, "作用对象是库状态，不是屏上结果"
    assert any(spec.zh.startswith(v) for v in AP._LEAD_VERBS), f"已{spec.zh} 读不通"
    assert "curate.db_status" in AP.build_action_prompt("随便一句", has_results=False, result_total=0)


# ---------------------------------------------------------------- agent execute 节点（LOOP_TOOLS）

class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（answers 用尽后 pop 抛 IndexError）。"""

    def __init__(self, *answers):
        self.answers = list(answers)

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


def _plan(utterance, model):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0,
        config=CFG, retrieval=None, current_query="", current_filters=None,
        chat_model=model,
    )


def test_execute_runs_the_loop_tool_and_llm_organizes_the_report():
    """db_status 句 → execute 图内真跑工具（observation 进 plan），decide 判断完成，
    narrate 由 LLM 组织汇报（report_zh 进 plan，**措辞与 READ_TOOLS 时代逐位一致**）。"""
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="汇报数据库的当前状态", confidence="high", reason="问库况"),
        AIMessage(content='{"done": true}'),   # decide：判断完成
        AIMessage(content="本地库共 5712 条、6 个来源；外部库 5 个文件，回收站 2 个。"),
    )
    plan, trace = _plan("汇报数据库的当前状态", model)
    assert plan["verb"] == "curate.db_status"
    assert plan["source"] == "agent"
    obs = plan.get("observation")
    assert obs and isinstance(obs.get("total_records"), int), "execute 必须把工具产出挂进 plan"
    assert plan.get("report_zh") == "本地库共 5712 条、6 个来源；外部库 5 个文件，回收站 2 个。"
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "execute", "decide", "narrate"]


def test_execute_falls_back_to_deterministic_report_when_llm_dry():
    """decide/narrate 的 LLM 都没回话（answers 用尽 → 调用即抛）→ decide 按完成停环、
    report_zh 回退确定性汇报（同一批事实）。"""
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="汇报数据库的当前状态", confidence="high", reason="问库况"),
    )
    plan, _ = _plan("汇报数据库的当前状态", model)
    report = plan.get("report_zh") or ""
    assert report.startswith("目录共收录 "), report
    assert "个来源" in report and "回收站" in report


def test_execute_passes_through_for_verbs_without_tools():
    """动词不在 LOOP_TOOLS → execute 空过、decide 不发起：无 observation、无伪造的步骤。"""
    model = _FakeModel(
        _tool_call("curate.list", quoted="看看我上传了哪些", confidence="high", reason="清点"),
    )
    plan, trace = _plan("看看我上传了哪些数据", model)
    assert plan["verb"] == "curate.list"
    assert "observation" not in plan
    assert "steps" not in plan
    assert [t["node"] for t in trace] == ["route_consensus", "understand", "validate", "narrate"]


# ---------------------------------------------------------------- 失败 ≠ 真空

def test_source_failure_carries_error_field(tmp_path: Path, monkeypatch):
    """单源统计失败不得呈现成「0 条」裸数字——带 error 字段与真空快照区分。"""
    def boom(path):
        raise OSError("simulated io error")

    monkeypatch.setattr(corpus_status, "_snapshot_local_info", boom)
    s = corpus_status.db_status(project_root=tmp_path)
    assert all(x["local_count"] == 0 for x in s["sources"])
    assert all(x.get("error") == "OSError" for x in s["sources"]), \
        "失败必须可见：error 字段如实区分「库坏了」与「库里没东西」"


def test_curations_failure_carries_error_field(tmp_path: Path, monkeypatch):
    """外部库/回收站清单失败不得呈现成真空清单——顶层带 curations_error。"""
    def boom(**kw):
        raise RuntimeError("simulated listing failure")

    monkeypatch.setattr(corpus_status, "list_curations", boom)
    s = corpus_status.db_status(project_root=tmp_path)
    assert s["external_files"] == [] and s["recycle"] == []
    assert s["curations_error"] == "RuntimeError"
