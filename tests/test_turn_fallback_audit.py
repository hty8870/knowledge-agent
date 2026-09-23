""" `turn.route_turn` 的 agent 路径异常分诊钉。

改前：``except Exception: plan = None``——AgentError（预期内协议/通道失败）与代码 bug、
导入错误吞成同一种静默降级，长期故障完全不可见。
改后：AgentError 照静默降级；**其他异常留一行脱敏审计**（node="turn"，类型+截断消息，
不落密钥/正文）再走同一保底路径——「永不炸链」契约逐位不变。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.agent import turn as T  # noqa: E402


def _arm_agent(monkeypatch, exc):
    """让 agent 路径可用且在调用时抛指定异常；规则段检索不付真钱（mock provider）。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(agent_exec, "plan_with_agent", lambda *a, **k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(T, "rule_match_summary", lambda *a, **k: {"status": "results", "total": 0, "top_titles": []})


class _Cfg:
    """should_use_llm 过关的最小配置替身（真 provider + key + 非 mock）。"""

    enable_llm = True
    provider = "openai-compatible"
    api_key = "sk-test"
    base_url = "https://example.invalid"
    model = "m"
    mock_llm = False


def test_agent_error_stays_silent_no_audit(monkeypatch):
    """预期内失败（AgentError）安静降级：不落审计行，plan 回落保底路径。"""
    _arm_agent(monkeypatch, agent_exec.AgentError("channel down"))
    calls = []
    monkeypatch.setattr(agent_exec, "_audit_fallback", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(T._ap, "plan_action", lambda text, **k: {"kind": "route", "verb": "none", "source": "rule", "llm_status": "empty"})
    out = T.route_turn("随便一句话", config=_Cfg())
    assert out["route"] == "none"  # 保底路径照常
    assert calls == []


def test_agent_error_fallback_is_flagged_on_the_plan(monkeypatch):
    """AgentError 安静降级契约不变（不落审计行），但保底 plan 上
    additive 留痕 `agent_fallback: true`——否则一次请求付了 2~3 次 LLM 调用，
    via 却看不出 agent 路径刚才失败过，线上排查「为什么这么慢/这么贵」无据可查。"""
    _arm_agent(monkeypatch, agent_exec.AgentError("channel down"))
    calls = []
    monkeypatch.setattr(agent_exec, "_audit_fallback", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(T._ap, "plan_action", lambda text, **k: {"kind": "route", "verb": "none", "source": "rule", "llm_status": "empty"})
    out = T.route_turn("随便一句话", config=_Cfg())
    assert out["route"] == "none"
    assert calls == [], "静默降级契约不变：AgentError 不落审计行"
    assert out["plan"]["agent_fallback"] is True, "但 plan 上必须留痕"


def test_agent_path_success_does_not_carry_the_fallback_flag(monkeypatch):
    """反向钉：agent 路径成功时不许误挂 `agent_fallback`（留痕必须指向真失败）。"""
    monkeypatch.setattr(agent_exec, "agent_available", lambda: True)
    monkeypatch.setattr(
        agent_exec, "plan_with_agent",
        lambda *a, **k: ({"kind": "exec", "verb": "curate.check_updates", "source": "agent"}, []))
    monkeypatch.setattr(T, "rule_match_summary", lambda *a, **k: {"status": "results", "total": 0, "top_titles": []})
    out = T.route_turn("检查10x是否有更新", config=_Cfg())
    assert out["route"] == "tool"
    assert "agent_fallback" not in (out["plan"] or {})


def test_audit_fallback_write_failure_is_warned_not_silent(monkeypatch):
    """跨环节移交项（→ dispatch2）：turn 调 `_audit_fallback` 的那层
    except 不再静默 pass——`_warn_once` 留一行（脱敏），路由照常走完保底路径。"""
    _arm_agent(monkeypatch, RuntimeError("boom-internal-detail"))
    monkeypatch.setattr(agent_exec, "_audit_fallback",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    warns = []
    monkeypatch.setattr(agent_exec, "_warn_once", lambda *a, **k: warns.append(a))
    monkeypatch.setattr(T._ap, "plan_action", lambda text, **k: {"kind": "route", "verb": "none", "source": "rule", "llm_status": "empty"})
    out = T.route_turn("随便一句话", config=_Cfg())
    assert out["route"] == "none", "审计失败绝不掀翻路由"
    assert len(warns) == 1
    assert "OSError" in warns[0][0]


def test_unexpected_exception_audited_then_fallback(monkeypatch):
    """预期外异常（代码 bug 族）：留一行 node=turn 脱敏审计，再照走保底路径。"""
    _arm_agent(monkeypatch, RuntimeError("boom-internal-detail"))
    calls = []
    monkeypatch.setattr(agent_exec, "_audit_fallback", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(T._ap, "plan_action", lambda text, **k: {"kind": "route", "verb": "none", "source": "rule", "llm_status": "empty"})
    out = T.route_turn("随便一句话", config=_Cfg())
    assert out["route"] == "none"
    assert len(calls) == 1
    _root, node, reason, utterance, _model = calls[0]
    assert node == "turn"
    assert "RuntimeError" in reason and len(reason) <= 200
    assert utterance == "随便一句话"
