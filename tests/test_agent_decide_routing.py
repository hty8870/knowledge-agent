# -*- coding: utf-8 -*-
"""复杂度路由的确定性门。
**全离线**：

- decide_lane 定标钉：K/L 断链族进 complex；克制类（h01 型）与单步（a 型）守 simple；
  贴边界两例（k08=2 分进 / b08=1 分守）逐字钉死——词表或阈值一动，这里立刻红。
- decide 接线钉：ctx.decide_model 非 None 时 decide 只用它（首答/重问/回灌同档不换脑），
  trace 留「长链档」档标；None 时回退 chat_model。
- 构建钉：env 未配置 → 恰建一个 client（路由关闭 = 现状）；env 配置 + complex 车道 →
  恰建两个且 decide 走第二 client；注入 chat_model 的测试路径**永不**自建第二 client。
"""
import json

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：路由测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec
from dataset_recommender.llm import llm_client  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-route-test")

# 清单函数的真引用（import 期存根——autouse fixture 会 stub 掉模块属性
# 需要真函数的钉用这份存根后执行 setattr 恢复）。
_REAL_CHECKLIST_CALL = agent_exec._task_checklist_call


class _FakeModel:
    """bind_tools 返回自身；invoke 依次弹预置 AIMessage（用尽后 pop 抛 IndexError →
    decide/narrate 按「LLM 缺席」fail-safe 处理——与 test_agent_exec_loop 同纪律）。"""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.invocations = []

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        self.invocations.append(messages)
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[{"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


@pytest.fixture(autouse=True)
def _tmp_project_root(monkeypatch, tmp_path):
    """项目根重定向到 tmp——审计账本不碰真实库。"""
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    # V4 旋钮 env 默认清零：各测试要配自己 setenv，
    # 外部环境里的真实配置不许串进离线钉。
    monkeypatch.delenv("LLM_COMPLEX_THINKING", raising=False)
    monkeypatch.delenv("LLM_COMPLEX_EFFORT", raising=False)
    # 清单调用统一 stub（本文件多数钉钉路由/循环行为；清单自身的钉在测试体内
    # 用后执行的 setattr 覆盖本 stub）。
    monkeypatch.setattr(agent_exec, "_task_checklist_call",
                        lambda *a, **k: ([], 0, ""))
    return tmp_path


@pytest.fixture(autouse=True)
def _stub_loop_tools(monkeypatch):
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", {
        "curate.db_status": {
            "run": lambda slots, root: {"total_records": 0, "sources": [],
                                        "external_files": [], "recycle": [], "ledger": {}},
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True,
        },
        "curate.check_updates": {
            "run": lambda slots, root: {
                "checked_at": "2026-08-07T00:00:00+08:00",
                "sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                             "local_count": 12, "online_recent": 12, "new_count": 0}],
                "hint_zh": "",
            },
            "label_zh": "检查来源更新", "card_kind": "check_updates", "readonly": True,
        },
    })
    monkeypatch.setattr(agent_exec, "_audit_loop_tool", lambda *a, **k: None)


class _Runtime:
    """decide 节点直调用替身：节点只读 `runtime.context`。"""

    def __init__(self, ctx):
        self.context = ctx


#: 一条 complex 车道长链原话（k01 型；score=4）。
K01_LIKE = ("检查ArrayExpress有没有更新，有新的人类肺数据就搜来入库，"
            "然后检查下ENCODE，最后告诉我库里多少条")


# ---------------------------------------------------------------- decide_lane 定标钉

@pytest.mark.parametrize("utterance,expected", [
    # K/L 断链族（v11 实测 decide 断链）必须进 complex：
    (K01_LIKE, "complex"),                       # k01/k02：2 连接 + 1 条件 + 2 来源 = 4
    ("检查ArrayExpress更新，有新的人类肺数据就搜来入库；mouse brain 也顺便搜了入库；最后告诉我库里多少条",
     "complex"),                                  # k10：4 连接 + 1 条件 = 5
    ("看看ArrayExpress有没有新数据，没有的话就把ENCODE和10x都检查一遍",
     "complex"),                                  # k06：1 条件 + 3 来源 = 3
    ("检查10x、ArrayExpress和ENCODE有没有更新，完了看看库里多少条",
     "complex"),                                  # k09：1 连接 + 3 来源 = 3
    ("联网搜 human lung 数据入库，然后检查ENCODE更新，再告诉我库里多少条",
     "complex"),                                  # k08：恰 2 连接 = 贴阈值下界（阈值抬 1 就漏它）
    # 克制类 / 短链 / 单步必须守 simple：
    ("检查10x是否有更新，若有则下载下来", "simple"),  # h01：条件短链——reasoner 过动回落区
    ("库里现在有多少条数据", "simple"),               # 库容独句 = 单事项
    ("只看小鼠的", "simple"),
    ("把这批结果的引用格式导出来", "simple"),
    ("帮我看看 ENCODE 最近有没有更新", "simple"),
    ("把 upload_20260801_test.json 这个文件删了", "simple"),
])
def test_decide_lane_calibration(utterance, expected):
    assert agent_exec.decide_lane(utterance) == expected


@pytest.mark.parametrize("utterance,expected", [
    # 库容加档（坐实：chat 3/3 断链、reasoner 3/3 治愈）：库容问句 + 另一事项信号 → complex。
    ("检查一下ENCODE有没有更新，顺便看看库里多少条", "complex"),   # b08 本例
    ("同步一下ArrayExpress的更新，完了告诉我库里多少条", "complex"),  # b13 同族
    ("看看库里现在多少条，再检查下ENCODE有没有更新", "complex"),    # i08 同族
    # 克制守卫优先于库容加档：
    ("别查更新了，就告诉我库里多少条", "simple"),
])
def test_decide_lane_count_query_boost(utterance, expected):
    assert agent_exec.decide_lane(utterance) == expected


def test_decide_lane_empty_and_garbage_are_simple():
    assert agent_exec.decide_lane("") == "simple"
    assert agent_exec.decide_lane("嗯") == "simple"


@pytest.mark.parametrize("utterance,expected", [
    # 克制守卫：词面 4 分（+如果+再搜+最后）但全句是叫停语义 → 一票留 simple。
    ("检查一下 ArrayExpress；如果没新增就不用再搜，最后只告诉我没有更新", "simple"),
    ("不用下载了，就检查一下ArrayExpress有没有更新就行", "simple"),       # c05 型
    # 「分别」是多事项标记不是叫停（(?<!分)别 豁免）：分别 + 2 点名来源 = 2 分 → complex。
    ("麻烦分别查一下 10x 和 GEO 的更新", "complex"),
    # 词表增补（另外/以及/并且）能被计分；l04 型两分句 score 1，但叠库容加档 → complex
    # （这正是加档要收的「检查 + 报库容」两分链；纯库容独句仍 simple，见上组钉）。
    ("检查下ArrayExpress有没有更新，另外帮我看看库里多少条", "complex"),
])
def test_decide_lane_review_amendments(utterance, expected):
    assert agent_exec.decide_lane(utterance) == expected


# ---------------------------------------------------------------- decide 节点接线

def test_decide_prefers_decide_model_when_present():
    """complex 档注入时 decide 只调 decide_model，chat_model 零调用；trace 留档标。"""
    chat_fake = _FakeModel()
    decide_fake = _FakeModel(_tool_call("curate.db_status", quoted="库里多少条"))
    ctx = agent_exec._AgentContext(
        chat_model=chat_fake, model_name="chat-x",
        decide_model=decide_fake, decide_model_name="complex-y", decide_lane="complex")
    out = agent_exec.decide(
        {"utterance": "检查一下ENCODE有没有更新，顺便看看库里多少条",
         "steps": [], "finish_vetoes": 0, "reask_writes": []},
        runtime=_Runtime(ctx))
    assert out["loop_next"] is True and out["raw"]["verb"] == "curate.db_status"
    assert len(decide_fake.invocations) == 1, "decide 首答必须走 decide_model"
    assert chat_fake.invocations == [], "decide 不许碰 chat_model"
    assert out["trace"][0]["detail"].startswith("长链档｜"), "complex 车道必须留档标"


def test_decide_falls_back_to_chat_model_without_decide_model():
    """未注入 decide_model（路由关闭/simple 车道）→ decide 走 chat_model，无档标。"""
    chat_fake = _FakeModel(_tool_call("curate.db_status", quoted="库里多少条"))
    ctx = agent_exec._AgentContext(chat_model=chat_fake, model_name="chat-x")
    out = agent_exec.decide(
        {"utterance": "检查一下ENCODE有没有更新，顺便看看库里多少条",
         "steps": [], "finish_vetoes": 0, "reask_writes": []},
        runtime=_Runtime(ctx))
    assert out["loop_next"] is True
    assert len(chat_fake.invocations) == 1
    assert "长链档" not in out["trace"][0]["detail"]


# ---------------------------------------------------------------- plan_with_agent 构建纪律

def _three_answer_fake():
    return _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),          # decide：done
        AIMessage(content="已检查，ArrayExpress 没有疑似新增。"),  # narrate
    )


def test_env_unset_means_single_client_even_on_complex_lane(monkeypatch):
    """路由默认关闭：LLM_MODEL_COMPLEX 未配置时，complex 原话也只建一个 client。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    built = []
    fake = _three_answer_fake()

    def _builder(config):
        built.append(config.model)
        return fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, trace = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [CFG.model], "未配置 LLM_MODEL_COMPLEX 时恰建一个 client（路由关闭=现状）"
    assert not any(t["node"] == "decide" and "长链档" in t["detail"] for t in trace)
    assert plan["verb"] == "curate.check_updates"


def test_env_armed_complex_lane_builds_and_uses_second_client(monkeypatch):
    """complex 车道 + env 配置 → 恰建两个 client；decide 走第二 client 并留档标。"""
    monkeypatch.setenv("LLM_MODEL_COMPLEX", "complex-model-x")
    built = []
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="已检查，ArrayExpress 没有疑似新增。"),  # narrate 恒走 chat
    )
    decide_fake = _FakeModel(AIMessage(content='{"done": true}'))

    def _builder(config):
        built.append(config.model)
        return chat_fake if len(built) == 1 else decide_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, trace = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [CFG.model, "complex-model-x"]
    assert len(decide_fake.invocations) == 1, "decide 必须走第二 client"
    assert any(t["node"] == "decide" and t["detail"].startswith("长链档｜") for t in trace)
    assert plan["verb"] == "curate.check_updates"


def test_env_armed_simple_lane_stays_single_client(monkeypatch):
    """simple 车道即使 env 配置了也不建第二 client（simple 请求零额外开销）。"""
    monkeypatch.setenv("LLM_MODEL_COMPLEX", "complex-model-x")
    built = []
    fake = _FakeModel(_tool_call("none", quoted="", confidence="high"))

    def _builder(config):
        built.append(config.model)
        return fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, _ = agent_exec.plan_with_agent(
        "今天天气怎么样", has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [CFG.model]
    assert plan["verb"] == "none"


def test_injected_chat_model_never_builds_second_client(monkeypatch):
    """注入路径（测试/调用方全权）永不自建第二 client——complex 原话 + env 配置也不例外。"""
    monkeypatch.setenv("LLM_MODEL_COMPLEX", "complex-model-x")

    def _boom(config):
        raise AssertionError("注入路径不许自建 client")

    monkeypatch.setattr(agent_exec, "_build_chat_model", _boom)
    fake = _three_answer_fake()
    plan, trace = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None, chat_model=fake)
    assert plan["verb"] == "curate.check_updates"
    assert not any(t["node"] == "decide" and "长链档" in t["detail"] for t in trace)


def test_explicit_decide_model_seam_is_used(monkeypatch):
    """显式 decide_model 注入缝：给了就用它，即使 chat_model 也是注入的。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)  # env 不配置也必须生效
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="已检查，ArrayExpress 没有疑似新增。"),  # narrate 恒走 chat
    )
    decide_fake = _FakeModel(AIMessage(content='{"done": true}'))
    plan, trace = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None,
        chat_model=chat_fake, decide_model=decide_fake)
    assert len(decide_fake.invocations) == 1, "decide 必须走显式注入的 decide_model"
    assert any(t["node"] == "decide" and t["detail"].startswith("长链档｜") for t in trace)
    assert plan["verb"] == "curate.check_updates"


# ---------------------------------------------------------------- env 读取

def test_complex_model_name_reads_env(monkeypatch):
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    assert llm_client.complex_model_name() == ""
    monkeypatch.setenv("LLM_MODEL_COMPLEX", "  x-y  ")
    assert llm_client.complex_model_name() == "x-y"


# ---------------------------------------------------------------- V4 thinking 旋钮

def test_complex_thinking_env_parsing(monkeypatch):
    """env 解析矩阵：未配→(None,None)；on/off 大小写宽容；effort 非法值钳 None。"""
    assert agent_exec._complex_thinking_env() == (None, None)
    monkeypatch.setenv("LLM_COMPLEX_THINKING", "ON")
    assert agent_exec._complex_thinking_env() == (True, None)
    monkeypatch.setenv("LLM_COMPLEX_EFFORT", "low")
    assert agent_exec._complex_thinking_env() == (True, "low")
    monkeypatch.setenv("LLM_COMPLEX_EFFORT", "bogus")
    assert agent_exec._complex_thinking_env() == (True, None), "非法 effort 钳 None（官方默认档）"
    monkeypatch.setenv("LLM_COMPLEX_THINKING", "off")
    assert agent_exec._complex_thinking_env() == (False, None)


def test_build_chat_model_extra_body():
    """thinking/effort → extra_body 注入；缺省 → 不带 extra_body（请求体逐位不变铁律）。"""
    m = agent_exec._build_chat_model(LLMConfig(api_key="sk-t", model="deepseek-chat",
                                               thinking=True, reasoning_effort="high"))
    assert m.extra_body == {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}
    m2 = agent_exec._build_chat_model(LLMConfig(api_key="sk-t", model="deepseek-chat",
                                                thinking=False))
    assert m2.extra_body == {"thinking": {"type": "disabled"}}
    m3 = agent_exec._build_chat_model(LLMConfig(api_key="sk-t", model="deepseek-chat"))
    assert not m3.extra_body
    m4 = agent_exec._build_chat_model(LLMConfig(api_key="sk-t", model="deepseek-chat",
                                                reasoning_effort="high"))
    assert not m4.extra_body, "effort 不许脱离 thinking 单独发送"


def test_thinking_only_env_builds_second_client(monkeypatch):
    """V4 旋钮目标形态：无 LLM_MODEL_COMPLEX、只开 LLM_COMPLEX_THINKING=on → complex
    车道也建第二 client（同模型名 + thinking 参数），decide 走它。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    monkeypatch.setenv("LLM_COMPLEX_THINKING", "on")
    monkeypatch.setenv("LLM_COMPLEX_EFFORT", "low")
    built = []
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="已检查，ArrayExpress 没有疑似新增。"),  # narrate 恒走 chat
    )
    decide_fake = _FakeModel(AIMessage(content='{"done": true}'))

    def _builder(config):
        built.append((config.model, config.thinking, config.reasoning_effort))
        return chat_fake if len(built) == 1 else decide_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, trace = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [(CFG.model, None, None), (CFG.model, True, "low")]
    assert len(decide_fake.invocations) == 1, "decide 必须走 thinking 第二 client"
    assert plan["verb"] == "curate.check_updates"


def test_thinking_off_without_model_name_stays_single_client(monkeypatch):
    """thinking=off 且不配模型名 → 不建第二 client（off 语义 chat_model 已满足，
    再建只是多发 disabled 参数、白换缓存键）。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    monkeypatch.setenv("LLM_COMPLEX_THINKING", "off")
    built = []
    fake = _three_answer_fake()

    def _builder(config):
        built.append((config.model, config.thinking))
        return fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, _ = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [(CFG.model, None)]
    assert plan["verb"] == "curate.check_updates"


def test_model_name_plus_thinking_off_sends_disabled(monkeypatch):
    """模型名 + thinking=off → 建第二 client 且显式发 disabled（关别名自带思考，
    如 deepseek-reasoner 别名默认思考开——2026-08-08 实测）。"""
    monkeypatch.setenv("LLM_MODEL_COMPLEX", "deepseek-reasoner")
    monkeypatch.setenv("LLM_COMPLEX_THINKING", "off")
    built = []
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content="已检查。"),
    )
    decide_fake = _FakeModel(AIMessage(content='{"done": true}'))

    def _builder(config):
        built.append((config.model, config.thinking))
        return chat_fake if len(built) == 1 else decide_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, _ = agent_exec.plan_with_agent(
        K01_LIKE, has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert built == [(CFG.model, None), ("deepseek-reasoner", False)]
    assert plan["verb"] == "curate.check_updates"


# ---------------------------------------------------------------- 缓存埋点

def _used_call(verb, *, input_t=100, cache_read=64, output_t=8, **args):
    """带 usage_metadata 的 tool_call 应答（模拟 langchain_openai 对 DeepSeek 用量的透传）。"""
    m = _tool_call(verb, **args)
    m.usage_metadata = {"input_tokens": input_t, "output_tokens": output_t,
                        "input_token_details": {"cache_read": cache_read}}
    return m


def test_usage_record_extracts_and_tolerates():
    m = AIMessage(content="x")
    m.usage_metadata = {"input_tokens": 850, "output_tokens": 3,
                        "input_token_details": {"cache_read": 768}}
    rec = agent_exec._usage_record(m, "decide")
    assert rec == {"node": "decide", "input": 850, "cache_read": 768, "output": 3}
    # FakeModel 路径（无 usage_metadata）→ None（调用方跳过，台账保持缺席）
    assert agent_exec._usage_record(AIMessage(content="x"), "decide") is None
    assert agent_exec._usage_record(AIMessage(content="x", usage_metadata=None), "n") is None


def test_llm_usage_summary_present_when_models_report(monkeypatch):
    """端到端：模型带 usage → plan.llm_usage 汇总（calls/input_total/cache_read_total/hit_rate）。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    done_msg = AIMessage(content='{"done": true}')
    done_msg.usage_metadata = {"input_tokens": 100, "output_tokens": 4,
                               "input_token_details": {"cache_read": 64}}
    narrate_msg = AIMessage(content="检查完成，没有疑似新增。")
    narrate_msg.usage_metadata = {"input_tokens": 150, "output_tokens": 9,
                                  "input_token_details": {"cache_read": 0}}
    chat_fake = _FakeModel(
        _used_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新",
                   input_t=200, cache_read=128),
        done_msg,
        narrate_msg,
    )

    def _builder(config):
        return chat_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, _ = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新", has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    usage = plan.get("llm_usage")
    assert usage, "模型报了用量时 plan.llm_usage 必须在"
    assert usage["input_total"] == 450 and usage["cache_read_total"] == 192
    assert usage["cache_hit_rate"] == round(192 / 450, 4)
    assert {c["node"] for c in usage["calls"]} == {"understand", "decide", "narrate"}


def test_llm_usage_absent_when_models_silent(monkeypatch):
    """铁律：模型不报用量（FakeModel 常态）→ plan 无 llm_usage 键（离线钉键集逐位不变）。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),
    )

    def _builder(config):
        return chat_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, _ = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新", has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert "llm_usage" not in plan


# ---------------------------------------------------------------- 截断两段式续写

def _length_cut(content):
    """finish_reason=length 的截断应答。"""
    m = AIMessage(content=content)
    m.response_metadata["finish_reason"] = "length"
    return m


def test_continuation_stitches_truncated_text():
    """截断 → 自动续写一次 → 拼接全文返回。"""
    fake = _FakeModel(_length_cut('{"verb": "curate.db_sta'), AIMessage(content='tus"}'))
    out = agent_exec._invoke_text_with_continuation(fake, [AIMessage(content="prompt")])
    assert json.loads(agent_exec._message_text(out)) == {"verb": "curate.db_status"}
    assert len(fake.invocations) == 2, "截断后恰好多发一次续写"
    # 续写消息把前半段回贴了（模型能接着写的前提是看到已写部分）
    assert any("接着刚才的继续" in str(getattr(m, "content", "")) for m in fake.invocations[1])


def test_continuation_skips_tool_calls_and_bounds_retries():
    """tool_calls 截断不可续（结构化 JSON 无法拼接）；续写后再截断也不无限续。"""
    tc = AIMessage(content="", tool_calls=[{"name": "x", "args": {}, "id": "1"}])
    tc.response_metadata["finish_reason"] = "length"
    fake = _FakeModel(tc)
    out = agent_exec._invoke_text_with_continuation(fake, [AIMessage(content="p")])
    assert out is tc and len(fake.invocations) == 1, "带 tool_calls 的截断不许续写"
    # 续写后仍 length → 返回已有拼接，总调用 ≤2
    fake2 = _FakeModel(_length_cut("前半"), _length_cut("后半仍截断"))
    out2 = agent_exec._invoke_text_with_continuation(fake2, [AIMessage(content="p")])
    assert agent_exec._message_text(out2) == "前半后半仍截断"
    assert len(fake2.invocations) == 2


def test_continuation_not_triggered_on_normal_stop():
    """正常 stop 不触发续写（零额外调用——铁律）。"""
    fake = _FakeModel(AIMessage(content="完整回答"))
    out = agent_exec._invoke_text_with_continuation(fake, [AIMessage(content="p")])
    assert agent_exec._message_text(out) == "完整回答"
    assert len(fake.invocations) == 1


# ---------------------------------------------------------------- 任务清单核销

def test_parse_checklist_matrix():
    utt = "检查ArrayExpress有没有更新，有新的人类肺数据就搜来入库，最后告诉我库里多少条"
    good = [
        {"text": "检查ArrayExpress更新", "anchor": "检查ArrayExpress有没有更新",
         "expect_verb": "curate.check_updates"},
        {"text": "新增就搜来入库", "anchor": "有新的人类肺数据就搜来入库",
         "expect_verb": "curate.search_online"},
    ]
    tasks, dropped = agent_exec._parse_checklist(good, utt)
    assert [t["task_id"] for t in tasks] == ["t1", "t2"] and dropped == 0
    assert tasks[0]["sources"] == ["ArrayExpress"]
    # 幻觉锚点（非原话子串）剔除；非法动词剔除；过短锚点剔除
    bad = good + [
        {"text": "幻觉", "anchor": "这句话不在原话里", "expect_verb": "curate.db_status"},
        {"text": "非法动词", "anchor": "库里多少条", "expect_verb": "curate.drop_db"},
        {"text": "短锚", "anchor": "检查", "expect_verb": "curate.db_status"},
    ]
    tasks2, dropped2 = agent_exec._parse_checklist(bad, utt)
    assert len(tasks2) == 2 and dropped2 == 3
    # 全半角归一：模型把全角逗号抄成半角不算幻觉
    utt_fw = "检查ArrayExpress有没有更新，完了告诉我库里多少条"
    tasks3, dropped3 = agent_exec._parse_checklist(
        [{"text": "告诉库容", "anchor": "完了告诉我库里多少条", "expect_verb": "curate.db_status"}],
        utt_fw)
    assert len(tasks3) == 1 and dropped3 == 0
    # 非 list 形态（dict 包 tasks 键也收；其余返回空）
    tasks4, _ = agent_exec._parse_checklist({"tasks": good}, utt)
    assert len(tasks4) == 2
    assert agent_exec._parse_checklist("垃圾", utt) == ([], 0)


def _step(verb, ok=True, source="", result=None):
    slots = {"source": source} if source else {}
    return {"verb": verb, "ok": ok, "slots": slots, "result": result or {}}


def test_checklist_unsettled_matrix():
    # 一步成一事 / 一步不成 → step_missing
    cl = [{"task_id": "t1", "text": "检查AE更新", "anchor": "x",
           "expect_verb": "curate.check_updates", "sources": ["ArrayExpress"]},
          {"task_id": "t2", "text": "告诉库容", "anchor": "y",
           "expect_verb": "curate.db_status", "sources": []}]
    assert agent_exec._checklist_unsettled(cl, []) == [
        {"task_id": "t1", "text": "检查AE更新", "reason": "step_missing"},
        {"task_id": "t2", "text": "告诉库容", "reason": "step_missing"}]
    # 来源不匹配不算数
    steps_wrong_src = [_step("curate.check_updates", source="ENCODE")]
    assert len(agent_exec._checklist_unsettled(cl, steps_wrong_src)) == 2
    # 来源匹配核销一条；失败步不算
    steps_half = [_step("curate.check_updates", source="ArrayExpress"),
                  _step("curate.db_status", ok=False)]
    rest = agent_exec._checklist_unsettled(cl, steps_half)
    assert [r["task_id"] for r in rest] == ["t2"]
    # 全核销
    steps_full = steps_half + [_step("curate.db_status")]
    assert agent_exec._checklist_unsettled(cl, steps_full) == []


def test_checklist_conditional_exemption():
    """条件豁免：search 条目被「同来源 ok check 且零新增证据集」豁免；失败 check 不出具。"""
    cl = [{"task_id": "t1", "text": "有新增就搜来入库", "anchor": "x",
           "expect_verb": "curate.search_online", "sources": ["ArrayExpress"]}]
    zero_result = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                                "mode": "online", "new_count": 0, "new_candidates": []}]}
    ok_check = _step("curate.check_updates", source="ArrayExpress", result=zero_result)
    assert agent_exec._checklist_unsettled(cl, [ok_check]) == []
    # check 失败 → 豁免不成立
    bad_check = _step("curate.check_updates", ok=False, source="ArrayExpress",
                      result=zero_result)
    assert len(agent_exec._checklist_unsettled(cl, [bad_check])) == 1
    # 有新增（new_count>0）→ 条件成立不豁免，必须有 search 步
    nonzero = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                            "mode": "online", "new_count": 2,
                            "new_candidates": [{"accession": "X", "title": "y"}]}]}
    assert len(agent_exec._checklist_unsettled(
        cl, [_step("curate.check_updates", source="ArrayExpress", result=nonzero)])) == 1
    # unsupported 条目：decide 婉拒过才豁免
    cl2 = [{"task_id": "t9", "text": "打包下载", "anchor": "z",
            "expect_verb": "unsupported", "sources": []}]
    assert agent_exec._checklist_unsettled(cl2, [])[0]["reason"] == "unsupported_unaddressed"
    assert agent_exec._checklist_unsettled(cl2, [], declined_zh="打包下载没做") == []


def test_finish_veto_all_aggregates():
    cl = [{"task_id": "t1", "text": "告诉库容", "anchor": "y",
           "expect_verb": "curate.db_status", "sources": []}]
    # 清单未决 + 库容 pending 硬闸（去重：同一事实只报一次——清单与 pending 文案不同算两条）
    out = agent_exec._finish_veto_all("都做完了", 1, [], cl, [], "", "告诉我库里多少条")
    codes = [c for _t, c in out]
    assert "checklist_unsettled" in codes and "pending_count_query" in codes
    # 全核销 + 无 pending → 空
    steps = [_step("curate.db_status")]
    assert agent_exec._finish_veto_all("都做了", 1, [], cl, steps, "", "随便一句") == []
    # 旧形态单条保持（文本闸）
    out2 = agent_exec._finish_veto_all("检索：还没做", 1, [], [], steps, "", "随便一句")
    assert out2 and out2[0][1] == "unfinished"


def test_task_checklist_call_end_to_end(monkeypatch):
    """端到端：清单缺核销 → finish 否决回灌 → 补齐 db_status → 二次 finish 通过。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    checklist_json = json.dumps([
        {"text": "检查ArrayExpress更新", "anchor": "检查ArrayExpress有没有更新",
         "expect_verb": "curate.check_updates"},
        {"text": "告诉库里多少条", "anchor": "告诉我库里多少条", "expect_verb": "curate.db_status"},
    ], ensure_ascii=False)
    finish_bad = AIMessage(content="", tool_calls=[{"name": "finish", "id": "f1",
        "args": {"completion_report": "检查更新：已做（第1步）；告诉库容：已做（第1步）"}}])
    db_next = _tool_call("curate.db_status", quoted="告诉我库里多少条")
    finish_good = AIMessage(content="", tool_calls=[{"name": "finish", "id": "f2",
        "args": {"completion_report": "检查更新：已做（第1步）；告诉库容：已做（第2步）"}}])
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新，完了告诉我库里多少条",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content=checklist_json),   # 清单产出（真函数，stub 被下行覆盖）
        finish_bad,                           # decide 首答：finish 虚报 → 对账拦
        db_next,                              # 否决回灌 → 补 db_status
        finish_good,                          # 二次 finish：对账全核销 → 过
        AIMessage(content="检查无新增；库里 0 条。"),  # narrate
    )
    # 用后执行的 setattr 覆盖 autouse stub，恢复真函数（REAL 在 import 期存真引用）
    monkeypatch.setattr(agent_exec, "_task_checklist_call", _REAL_CHECKLIST_CALL)

    def _builder(config):
        return chat_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新，完了告诉我库里多少条",
        has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert plan["verb"] == "curate.check_updates"
    verbs = [s.get("verb") for s in plan.get("steps") or []]
    assert verbs == ["curate.check_updates", "curate.db_status"], (
        "虚报 finish 必须被清单对账拦下并回灌补齐 db_status")
    assert any("拒收收尾" in str(t.get("detail") or "") for t in trace), "否决留痕在 trace"


def test_checklist_unavailable_falls_back_without_blocking(monkeypatch):
    """清单产出两次都败 → checklist_unavailable 落 state 语义（降级不阻断：
    循环照常跑完、按旧文本闸收尾）。"""
    monkeypatch.delenv("LLM_MODEL_COMPLEX", raising=False)
    monkeypatch.setattr(agent_exec, "_task_checklist_call",
                        lambda *a, **k: ([], 0, "BoomError"))  # 覆盖 autouse stub
    chat_fake = _FakeModel(
        _tool_call("curate.check_updates", quoted="检查ArrayExpress有没有更新，完了告诉我库里多少条",
                   source="ArrayExpress", confidence="high", reason="查更新"),
        AIMessage(content='{"done": true}'),          # decide：done（无清单→旧闸口径）
        AIMessage(content="检查无新增。"),            # narrate
    )

    def _builder(config):
        return chat_fake

    monkeypatch.setattr(agent_exec, "_build_chat_model", _builder)
    plan, trace = agent_exec.plan_with_agent(
        "检查ArrayExpress有没有更新，完了告诉我库里多少条",
        has_results=False, result_total=0, config=CFG,
        retrieval=None, current_query="", current_filters=None)
    assert plan["verb"] == "curate.check_updates", "清单不可用不许阻断正常循环"


def test_pending_hard_gate_search_word_narrowed():
    """pending 升硬闸的入库诉求词表剔「搜」（裁决：「搜搜有没有」是检索语境，
    闸层误报=误杀）；「入库/拿回/下载」照常升闸。"""
    new_result = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                               "mode": "online", "new_count": 2,
                               "new_candidates": [{"accession": "X", "title": "y"}]}]}
    steps = [_step("curate.check_updates", source="ArrayExpress", result=new_result)]
    # 「搜搜有没有新数据」不触发入库硬闸
    assert not any(c == "pending_new_not_imported"
                   for _t, c in agent_exec._pending_violations(
                       "帮我搜搜ArrayExpress有没有新的人类肺数据", steps))
    # 「有新增就搜来入库」触发
    assert any(c == "pending_new_not_imported"
               for _t, c in agent_exec._pending_violations(
                   "检查ArrayExpress有没有更新，有新的人类肺数据就搜来入库", steps))
    # 已入库（ok search 步）不触发
    steps2 = steps + [_step("curate.search_online", source="ArrayExpress")]
    assert not any(c == "pending_new_not_imported"
                   for _t, c in agent_exec._pending_violations(
                       "检查ArrayExpress有没有更新，有新的就搜来入库", steps2))


# -------------------------------------------- 清单核销验证修复钉

def test_width_fold_punctuation_pairs():
    """全半角映射逐对钉（初版曾有 。→; 错配）：。→. ；→;。"""
    assert agent_exec._fold_width("检查GEO。") == "检查GEO."
    assert agent_exec._fold_width("检查A；同步B") == "检查A;同步B"
    assert agent_exec._fold_width("，！？（）【】：") == ",!?()[]:".replace(";", ";").replace(",", ",", 1) \
        .replace("!", "!").replace("?", "?")  # 明示逐对
    assert agent_exec._fold_width("，！？（）【】：") == ",!?()[]:"


def test_checklist_all_dropped_triggers_error(monkeypatch):
    """全剔除 = 失败（触发 repair/unavailable），不是合法空清单。"""
    out = agent_exec._parse_checklist(
        [{"text": "幻觉条目", "anchor": "原话里根本没有这句话啊", "expect_verb": "curate.db_status"}],
        "检查ArrayExpress有没有更新")
    assert out[0] == [] and out[1] == 1
    # _task_checklist_call 对全剔除应答返回非空 err（conftest 全局 stub 在先，
    # 这里用后执行的 setattr 恢复真函数）
    monkeypatch.setattr(agent_exec, "_task_checklist_call", _REAL_CHECKLIST_CALL)

    class _M:
        def invoke(self, msgs):
            return AIMessage(content='[{"text": "幻觉条目", "anchor": "根本没有这句", '
                                     '"expect_verb": "curate.db_status"}]')
    tasks, dropped, err = agent_exec._task_checklist_call(_M(), "检查ArrayExpress有没有更新")
    assert tasks == [] and err == "all_dropped"


def test_task_settled_multi_source_coverage():
    """：多来源条目按覆盖核销——任一来源步不再整体核销。"""
    cl = [{"task_id": "t1", "text": "检查AE和ENCODE", "anchor": "x",
           "expect_verb": "curate.check_updates", "sources": ["ArrayExpress", "ENCODE"]}]
    only_ae = [_step("curate.check_updates", source="ArrayExpress")]
    assert len(agent_exec._checklist_unsettled(cl, only_ae)) == 1, "只查 AE 不许核销 AE+ENCODE"
    both = only_ae + [_step("curate.check_updates", source="ENCODE")]
    assert agent_exec._checklist_unsettled(cl, both) == []
    # 全来源步（空 source）按 result.sources 还原覆盖
    full = [{"verb": "curate.check_updates", "ok": True, "slots": {},
             "result": {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                                     "mode": "online", "new_count": 0, "new_candidates": []},
                                    {"source": "encode", "label": "ENCODE",
                                     "mode": "online", "new_count": 0, "new_candidates": []}]}}]
    assert agent_exec._checklist_unsettled(cl, full) == [], "全来源步须按结果覆盖集核销"


def test_conditional_exemption_strict_and():
    """条件豁免严格且：new_candidates 空列表 AND new_count 严格 0；多来源要全零。"""
    cl = [{"task_id": "t1", "text": "AE和ENCODE有新增就搜", "anchor": "x",
           "expect_verb": "curate.search_online", "sources": ["ArrayExpress", "ENCODE"]}]
    zero_ae = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                            "new_count": 0, "new_candidates": []}]}
    one_encode = {"sources": [{"source": "encode", "label": "ENCODE", "mode": "online",
                               "new_count": 1, "new_candidates": [{"accession": "X"}]}]}
    steps = [_step("curate.check_updates", source="ArrayExpress", result=zero_ae),
             _step("curate.check_updates", source="ENCODE", result=one_encode)]
    assert len(agent_exec._checklist_unsettled(cl, steps)) == 1, "ENCODE 有新增不许豁免"
    # cands 空但 count=2（矛盾数据）不豁免
    weird = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                          "new_count": 2, "new_candidates": []}]}
    cl2 = [{"task_id": "t1", "text": "AE有新增就搜", "anchor": "x",
            "expect_verb": "curate.search_online", "sources": ["ArrayExpress"]}]
    assert len(agent_exec._checklist_unsettled(
        cl2, [_step("curate.check_updates", source="ArrayExpress", result=weird)])) == 1


def test_unsupported_items_exempt_one_per_declined():
    """ 修订：一次婉拒只豁免一条 unsupported；第二条仍未决。"""
    cl = [{"task_id": "t1", "text": "打包下载", "anchor": "x",
           "expect_verb": "unsupported", "sources": []},
          {"task_id": "t2", "text": "发邮件", "anchor": "y",
           "expect_verb": "unsupported", "sources": []}]
    out = agent_exec._checklist_unsettled(cl, [], declined_zh="你要的「打包下载」这一步没有做")
    assert [u["task_id"] for u in out] == ["t2"]


def test_sync_named_source_half_gate():
    """2026-08-15：sync_updates 纳入点名源一致性**半闸**——
    填了 source 就必须是用户点名源之一（写操作不依赖模型自觉）；不填 = 同步全部（覆盖
    点名源），合法放行。全闸动词（check/search）的空槽必填语义不变。"""
    utter = "检查ArrayExpress和ENCODE有没有更新，有新增就同步入库"
    # 填错来源 → 拦
    v = agent_exec._named_source_violation(
        "curate.sync_updates", {"source": "10x"}, utter)
    assert v and "用户点名的是" in v
    # 填对（别名归一）→ 放
    assert agent_exec._named_source_violation(
        "curate.sync_updates", {"source": "ArrayExpress"}, utter) is None
    # 不填（同步全部）→ 放（半闸与全闸的唯一差别）
    assert agent_exec._named_source_violation(
        "curate.sync_updates", {}, utter) is None
    # 全闸动词空槽照旧拦
    assert agent_exec._named_source_violation(
        "curate.check_updates", {}, utter) is not None
    # 原话没点名 → 填什么都不越界
    assert agent_exec._named_source_violation(
        "curate.sync_updates", {"source": "10x"}, "有新增就同步入库") is None


def test_sync_empty_source_named_scope_helper():
    """2026-08-15 （exec-gates M5）：点名单源 + sync 空槽 = 按全部在线源同步——
    半闸放行语义不变，但 `_sync_all_online_named` 必须把「写面超出点名范围」供给
    validate/decide trace 与 narrate 汇报留痕。非 sync / 已填 source / 未点名 → 不留痕。"""
    utter = "检查GEO有没有更新，有新增就同步"
    # 点名单源 + 空槽 → 返回点名录（留痕依据）
    assert agent_exec._sync_all_online_named("curate.sync_updates", {}, utter) == "NCBI GEO"
    # source 已填 → 写面收敛在点名源内，无需留痕
    assert agent_exec._sync_all_online_named(
        "curate.sync_updates", {"source": "NCBI GEO"}, utter) == ""
    # 非 sync 动词 / 原话没点名 → 空
    assert agent_exec._sync_all_online_named("curate.check_updates", {}, utter) == ""
    assert agent_exec._sync_all_online_named(
        "curate.sync_updates", {}, "有新增就同步入库") == ""


def test_curate_extra_named_sources():
    """：curate 侧补充点名表（Zenodo 等）；GEO 裸词命中是检索词表既有行为（第一趟）
    非本趟引入——本趟只管补充表里原 6 源外的 curate 可检源。"""
    assert "Zenodo" in agent_exec._named_sources_in("检查Zenodo有没有新数据集")
    assert "NCBI GEO" in agent_exec._named_sources_in("顺便看下 GEO 有没有更新")
    # 补充表不误伤：无来源原话不出补充表条目
    assert "Zenodo" not in agent_exec._named_sources_in("告诉我库里多少条")


def test_canonical_source_knows_curate_only_sources():
    """2026-08-09 自对抗复查：`_canonical_source` 此前只认检索 SOURCE_ALIASES（9 源），
    curate-only 的 Zenodo canon=None → Zenodo 检查步清单核销永远落空、
    `_step_covered_sources` 连 result.sources 条目回退也失灵。钉死第三趟。"""
    assert agent_exec._canonical_source("Zenodo") == "Zenodo"
    assert agent_exec._canonical_source("zenodo") == "Zenodo"
    assert agent_exec._canonical_source("NCBI GEO") == "NCBI GEO"
    checklist = [{"task_id": "t1", "text": "检查Zenodo有没有更新", "anchor": "检查Zenodo有没有更新",
                  "expect_verb": "curate.check_updates",
                  "sources": agent_exec._named_sources_in("检查Zenodo有没有更新")}]
    steps = [{"verb": "curate.check_updates", "ok": True, "slots": {"source": "Zenodo"},
              "result": {"checked_at": "t", "sources": [
                  {"source": "zenodo", "mode": "online", "new_count": 0, "new_candidates": []}],
               "hint_zh": ""}}]
    assert agent_exec._step_source(steps[0]) == "Zenodo"
    assert agent_exec._step_covered_sources(steps[0]) == {"Zenodo"}
    assert agent_exec._checklist_unsettled(checklist, steps, "") == [], "Zenodo 清单核销不得落空"


def test_pending_import_per_source_diff():
    """ 附带：检出未入库按来源差集——AE 检出、只搜了 10x → AE 仍未入库。"""
    new_ae = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress", "mode": "online",
                           "new_count": 2, "new_candidates": [{"accession": "X"}]}]}
    steps = [_step("curate.check_updates", source="ArrayExpress", result=new_ae),
             _step("curate.search_online", source="10x Genomics")]
    out = agent_exec._pending_violations("检查ArrayExpress更新，有新的就搜来入库", steps)
    assert any(c == "pending_new_not_imported" and "ArrayExpress" in t for t, c in out)


def test_pending_hard_gate_import_denial_polarity():
    """pending 硬闸规则 3 补否定极性——「有新增也不要入库」是
    **拒绝**入库，不许命中「入库」子串升硬闸（finish 误拒 + 回灌谎称"原话要求入库" +
    二次否决强推 search_online，最坏真执行用户拒绝的入库）；肯定诉求照常触发。"""
    new_result = {"sources": [{"source": "arrayexpress", "label": "ArrayExpress",
                               "mode": "online", "new_count": 2,
                               "new_candidates": [{"accession": "X", "title": "y"}]}]}
    steps = [_step("curate.check_updates", source="ArrayExpress", result=new_result)]
    # 否定极性：不触发硬闸（同小句前向窗口内的否定语素豁免该命中）
    for denial in ("检查ArrayExpress有没有更新，有新增也不要入库，就告诉我有没有",
                   "检查ArrayExpress有没有更新，别下载，告诉我要不要就行",
                   "检查ArrayExpress有没有更新，有新增也先不入库"):
        assert not any(c == "pending_new_not_imported"
                       for _t, c in agent_exec._pending_violations(denial, steps)), denial
    # 肯定诉求照常触发
    assert any(c == "pending_new_not_imported"
               for _t, c in agent_exec._pending_violations(
                   "检查ArrayExpress有没有更新，有新增就入库", steps))
    # 混合句逐命中判定：「别重复入库」被豁免，但「有新增就下载」是真实诉求 → 仍触发
    assert any(c == "pending_new_not_imported"
               for _t, c in agent_exec._pending_violations(
                   "检查ArrayExpress有没有更新，别重复入库，有新增就下载", steps))


def test_pending_count_query_jitiao_boundary():
    """「十几条/这几条/好几条」不是库容问句——裸子串「几条」
    曾误触发 pending_count_query 硬闸（finish 被误拒、二次否决强跑没人问的 db_status），
    同一词表还复用于 decide_lane 加档（误升 complex 车道）；加左侧边界后两口径同修。"""
    for false_hit in ("把搜到的这几条数据都下载入库", "库里有十几条人类肺数据",
                      "那好几条记录都看看", "只有少数几条需要补"):
        assert not agent_exec._PENDING_COUNT_RE.search(false_hit), false_hit
    # 真库容问句照常命中
    for true_hit in ("库里还有几条数据？", "数数库里多少条", "现在库容多大"):
        assert agent_exec._PENDING_COUNT_RE.search(true_hit), true_hit
    # 硬闸面：「这几条都入库」不许再逼出 pending_count_query
    assert not any(c == "pending_count_query"
                   for _t, c in agent_exec._pending_violations(
                       "把搜到的这几条数据都下载入库", []))
    # 车道路由面（同一词表真源）：「十几条」不再给 simple 请求误加档
    assert agent_exec.decide_lane(
        "检查一下ArrayExpress有没有更新，顺便说说那十几条都是什么") == "simple"


# ---------------------------------------------------------------- 「不少于我」句内动作清单（2026-09-01）

def _icl(verb, verb_zh, quoted, plane="inloop"):
    return {"verb": verb, "verb_zh": verb_zh, "quoted": quoted, "plane": plane}


def test_intent_checklist_item_states_settlement_channels():
    """归宿两通道：ok 步（带步骤号）或核销报告点名（verb_zh/quoted 字面子串，全角折叠口径）；
    plane=frontend 恒 delegated（环内免核销）。"""
    cl = [_icl("cite.export", "导出引文", "导出成 BibTeX 引文"),
          _icl("pack.download", "打包下载", "打包下载", plane="frontend")]
    states = agent_exec._intent_checklist_item_states(cl, [_step("cite.export")])
    assert states[0]["status"] == "done" and states[0]["step_no"] == 1
    assert states[1]["status"] == "delegated"
    # 报告点名（verb_zh 或 quoted 任一子串）→ accounted
    st = agent_exec._intent_checklist_item_states(cl[:1], [], "引文没导：导出引文这步做不了")
    assert st[0]["status"] == "accounted"
    st2 = agent_exec._intent_checklist_item_states(cl[:1], [], "报告里含：导出成 BibTeX 引文，因缺结果未做")
    assert st2[0]["status"] == "accounted"
    # 全角标点折叠后仍算点名
    st3 = agent_exec._intent_checklist_item_states(
        [_icl("fair.check", "检查 FAIR", "检查 FAIR 就绪度")], [],
        "检查ＦＡＩＲ这步没做成")  # 全角 ＦＡＩＲ 不折叠字母，换半角场景：
    assert st3[0]["status"] in ("missing", "accounted")   # 不抛即可（字母不折叠）
    # 两皆无 → missing
    st4 = agent_exec._intent_checklist_item_states(cl[:1], [], "别的都说完了")
    assert st4[0]["status"] == "missing"


def test_intent_checklist_unsettled_only_missing():
    cl = [_icl("cite.export", "导出引文", "导出引文"),
          _icl("pack.download", "打包下载", "打包下载", plane="frontend")]
    assert agent_exec._intent_checklist_unsettled(cl, [_step("cite.export")]) == []
    assert agent_exec._intent_checklist_unsettled(cl, [], "导出引文已交代") == []
    missing = agent_exec._intent_checklist_unsettled(cl, [], "")
    assert [m["verb"] for m in missing] == ["cite.export"]   # frontend 项不算未决


def test_finish_veto_all_intent_checklist_leg():
    """第六腿：句内动作未核销 → intent_unsettled 进聚合；核销（步骤/点名）后空。"""
    cl = [_icl("cite.export", "导出引文", "导出引文")]
    out = agent_exec._finish_veto_all("都做完了", 0, [], [], [], "", "随便一句",
                                      intent_checklist=cl)
    codes = [c for _t, c in out]
    assert "intent_unsettled" in codes
    assert agent_exec._finish_veto_all("都做完了", 1, [], [], [_step("cite.export")], "",
                                       "随便一句", intent_checklist=cl) == []
    assert agent_exec._finish_veto_all("导出引文做不到：没有可导的结果", 0, [], [], [], "",
                                       "随便一句", intent_checklist=cl) == []
    # 缺省 intent_checklist=() → 旧调用零变化
    assert agent_exec._finish_veto_all("都做完了", 0, [], [], [], "", "随便一句") == []


def test_intent_unsettled_has_teaching_suffix():
    assert "intent_unsettled" in agent_exec._VETO_TEACHING_SUFFIX
