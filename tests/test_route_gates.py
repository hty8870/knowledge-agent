# -*- coding: utf-8 -*-
"""分流三改（2026-09-19）钉：二元写闸 / L0 级联 / 初步检索执行闸 / read 套件。

conftest `_pin_legacy_route_defaults` 把测试会话钉在旧通道（three_class + L0 关 +
闸关）——既有套件继续全量覆盖兜底通道；本文件测新默认通道，各用例显式
setenv/delenv 启用（conftest fixture 先执行，测试体内 monkeypatch 后执行生效）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from dataset_recommender.agent import agent_exec as AX
from dataset_recommender.agent import l0_router as L0
from dataset_recommender.agent import turn as TURN

# conftest 把 _run_route_consensus 全局 stub 成 general（图内测试不预置投票应答）；
# 本文件部分用例测共识本体——import 期存真引用，文件级 fixture 恢复（与
# test_scoped_routing.py 同一模式；L0 直采用例再自行 setattr 覆盖）。
_REAL_RUN_ROUTE_CONSENSUS = AX._run_route_consensus


@pytest.fixture(autouse=True)
def _restore_real_route_consensus(monkeypatch):
    monkeypatch.setattr(AX, "_run_route_consensus", _REAL_RUN_ROUTE_CONSENSUS)


# ---------------------------------------------------------------- 二元写闸（任务1）

def test_binary_parse_vote_mapping():
    """票面 gate → 套件名：write → general（全集含写工具），read → read（去写套件）；
    非法票面记废票（不折算成任何一路）。"""
    assert AX._parse_route_vote('{"gate": "write", "reason": "要入库"}', mode="binary") == \
        ("general", "要入库", True)
    assert AX._parse_route_vote('{"gate": "read", "reason": "只找数据"}', mode="binary") == \
        ("read", "只找数据", True)
    assert AX._parse_route_vote('{"gate": "maybe"}', mode="binary") == ("", "", False)
    assert AX._parse_route_vote("不是 JSON", mode="binary") == ("", "", False)
    # 三分类票面在二元模式下同样作废票（口径不混）。
    assert AX._parse_route_vote('{"route": "search"}', mode="binary") == ("", "", False)


class _GateFakeModel:
    """按脚本出票的二元闸模型替身（与 test_scoped_routing 的 _RouteFakeModel 同形）。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self.seen_system: list[str] = []

    def bind(self, **kwargs):
        return self

    def invoke(self, messages):
        self.calls += 1
        self.seen_system.append(str(getattr(messages[0], "content", "")))
        content = self._script.pop(0) if self._script else ""
        return SimpleNamespace(content=content)


def test_binary_consensus_maps_gate_to_suite():
    """生产 `_run_route_consensus` mode="binary"：系统提示词走写闸真源，
    票面 gate 映射套件；分歧加投多数决；全废票机械兜底 general（安全地板）。"""
    model = _GateFakeModel(['{"gate": "write", "reason": "要下载"}',
                            '{"gate": "write", "reason": "要下载"}'])
    route, votes = AX._run_route_consensus(model, "把前 5 条打包下载", mode="binary")
    assert route == "general" and len(votes) == 2 and model.calls == 2
    assert "写盘工具闸" in model.seen_system[0]  # 系统提示词 = prompts/route_write_gate.md
    model = _GateFakeModel(['{"gate": "read", "reason": "只读"}', '{"gate": "read", "reason": "只读"}'])
    route, _ = AX._run_route_consensus(model, "找找人类肺癌数据", mode="binary")
    assert route == "read"
    model = _GateFakeModel(['{"gate": "write"}', '{"gate": "read"}', '{"gate": "read"}'])
    route, votes = AX._run_route_consensus(model, "x", mode="binary")
    assert route == "read" and len(votes) == 3  # 分歧加投多数决
    model = _GateFakeModel(["散文", "还是散文"])
    route, votes = AX._run_route_consensus(model, "x", mode="binary")
    assert route == "general" and all(not v["ok"] for v in votes)  # 机械兜底安全地板


def test_read_suite_composition():
    """read 套件 = general − 两个正向写工具；其余全集在（含 route.request 逃生口与
    回滚）；understand 首步面同口径剔写动词。"""
    read_loop = set(AX._SUITE_LOOP_VERBS["read"])
    assert AX._WRITE_LOOP_TOOLS.isdisjoint(read_loop)
    assert {"rank", "rerank", "results.show", "ask.relax", "search.rerun",
            "curate.db_status", "curate.check_updates", "curate.rollback",
            "compare.datasets", "cite.export", "compat.find", "fair.check",
            "route.request"} <= read_loop
    read_face = set(AX._SUITE_UNDERSTAND_VERBS["read"])
    assert "curate.search_online" not in read_face
    assert "curate.sync_updates" not in read_face
    assert "rank" in read_face
    names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS_BY_SUITE["read"]]
    assert "curate_search_online" not in names and "curate_sync_updates" not in names
    assert "route_request" in names and "finish" in names


def test_route_mode_default_and_invalid(monkeypatch):
    """默认 = binary（生产翻转后的新通道）；非法值 warn-once 回默认；显式 three_class 可切回。"""
    monkeypatch.delenv("BIODATA_ROUTE_MODE", raising=False)
    assert AX._route_mode() == "binary"
    monkeypatch.setenv("BIODATA_ROUTE_MODE", "three_class")
    assert AX._route_mode() == "three_class"
    monkeypatch.setenv("BIODATA_ROUTE_MODE", "banana")
    assert AX._route_mode() == "binary"


def test_write_gate_prompt_loads_from_file():
    prompt = AX._route_write_gate_prompt()
    assert "写盘工具闸" in prompt and '"gate"' in prompt  # 文件真源，非内置最小降级


# ---------------------------------------------------------------- L0 级联（任务2）

def test_l0_scorer_properties():
    """生产工件在场可载：打分性质钉（标签合法、概率归一、置信度 ∈ [0,1]）；
    缺席模型 fail-open 回 None。"""
    for name in (L0.ROUTE_THREE_CLASS, L0.ROUTE_BINARY, L0.PRELIM_GATE):
        assert L0.available(name), name
    hit = L0.score(L0.ROUTE_BINARY, "把前 5 条打包下载")
    assert hit is not None and hit["label"] in ("write", "read")
    assert abs(sum(hit["probs"].values()) - 1.0) < 1e-6
    assert 0.0 <= hit["confidence"] <= 1.0
    assert hit["label"] == "write"  # 明显写意图应判 write（方向钉，不钉概率值）
    assert L0.score("no_such_model", "x") is None


def test_l0_cascade_node_direct_high_confidence(monkeypatch):
    """L0 高置信直采：route_consensus 节点零 LLM 调用（`_run_route_consensus`
    被调即炸），route_scope 走 L0 判决，trace 如实记 route_l0 且 route_votes 留空。"""
    monkeypatch.setenv("BIODATA_ROUTE_MODE", "binary")
    monkeypatch.setenv("BIODATA_ROUTE_L0", "1")

    def _boom(*a, **k):
        raise AssertionError("L0 高置信时不许发起 LLM 共识")

    monkeypatch.setattr(AX, "_run_route_consensus", _boom)
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None))
    state = {"utterance": "把前 5 条打包下载", "has_results": True,
             "result_total": 5, "current_query": "", "current_filters": [],
             "retrieval": None}
    out = AX.route_consensus(state, runtime=runtime)
    assert out["route_scope"] == "general"  # write → general 套件
    entry = out["trace"][0]
    assert entry["route_votes"] == []
    assert entry["route_l0"]["label"] == "write"
    assert entry["route_l0"]["confidence"] >= entry["route_l0"]["tau"]
    assert out["usage_ledger"] == []


def test_l0_cascade_escalates_when_tau_unreachable(monkeypatch):
    """τ 调到不可达 → 全部升级 LLM 共识（兜底链完整）。单判官路径钉 τ 闸
    （BIODATA_DECIDER=off 钉 L0 单官；双判官一致闸本身无 τ，另行专测）。"""
    monkeypatch.setenv("BIODATA_ROUTE_MODE", "binary")
    monkeypatch.setenv("BIODATA_ROUTE_L0", "1")
    monkeypatch.setenv("BIODATA_DECIDER", "off")
    monkeypatch.setenv("BIODATA_ROUTE_L0_TAU", "2.0")  # 置信度 ≤1，永远升级
    assert AX._l0_route_verdict("把前 5 条打包下载", "binary") is None
    called = []
    monkeypatch.setattr(AX, "_run_route_consensus",
                        lambda *a, **k: (called.append(1), ("general", []))[1])
    runtime = SimpleNamespace(context=SimpleNamespace(
        chat_model=None, decide_model=None, on_progress=None))
    state = {"utterance": "把前 5 条打包下载", "has_results": True,
             "result_total": 5, "current_query": "", "current_filters": [],
             "retrieval": None}
    out = AX.route_consensus(state, runtime=runtime)
    assert called and out["route_scope"] == "general"


def test_dual_judge_agreement_gate(monkeypatch):
    """双判官一致闸（binary 默认通道）：两官一致即采、无 τ；分歧 → None 升级 LLM；
    单官可用退化为 τ 闸。判决实现如实记 impl=dual 与两官置信度。"""
    from dataset_recommender.agent import decider as D
    monkeypatch.setenv("BIODATA_ROUTE_L0", "1")
    monkeypatch.setenv("BIODATA_DECIDER", "auto")
    d_hit = {"label": "write", "confidence": 0.91, "probs": {"write": 0.91, "read": 0.09},
             "impl": "decider"}
    l_hit = {"label": "write", "confidence": 0.83, "probs": {"write": 0.83, "read": 0.17}}
    monkeypatch.setattr(D, "score", lambda task, text: dict(d_hit))
    monkeypatch.setattr(AX._l0, "score", lambda name, text: dict(l_hit))
    got = AX._l0_route_verdict("x", "binary")
    assert got is not None
    suite, info = got
    assert suite == "general" and info["impl"] == "dual"
    assert info["confidence"] == 0.83  # 取两官较低者
    assert info["dual"] == {"decider_conf": 0.91, "l0_conf": 0.83}
    # 分歧 → None（τ 再高/再低都无关）
    monkeypatch.setattr(AX._l0, "score",
                        lambda name, text: {"label": "read", "confidence": 0.99,
                                            "probs": {"write": 0.01, "read": 0.99}})
    assert AX._l0_route_verdict("x", "binary") is None
    # 单官（decider 缺席）→ τ 闸：conf 0.83 < decider/L0 工作点即升级
    monkeypatch.setattr(D, "score", lambda task, text: None)
    monkeypatch.setattr(AX._l0, "score", lambda name, text: dict(l_hit))
    monkeypatch.setenv("BIODATA_ROUTE_L0_TAU", "0.95")
    assert AX._l0_route_verdict("x", "binary") is None
    monkeypatch.setenv("BIODATA_ROUTE_L0_TAU", "0.5")
    got = AX._l0_route_verdict("x", "binary")
    assert got is not None and got[1]["impl"] == "l0"


def test_l0_off_env(monkeypatch):
    """显式关闭回纯 LLM 共识（BIODATA_ROUTE_L0=0）。"""
    monkeypatch.setenv("BIODATA_ROUTE_L0", "0")
    assert AX._l0_route_verdict("把前 5 条打包下载", "binary") is None


# ---------------------------------------------------------------- 初步检索执行闸（任务3）

def test_prelim_gate_decisions(monkeypatch):
    """闸方向钉：闲聊 → skip（不起 flight）；检索 → 放行；显式关 → 一律放行（现状）。"""
    monkeypatch.setenv("BIODATA_PRELIM_GATE", "1")
    assert TURN._prelim_gate_skip("你好，在吗") is True
    assert TURN._prelim_gate_skip("找找人类肺癌的单细胞数据") is False
    monkeypatch.setenv("BIODATA_PRELIM_GATE", "0")
    assert TURN._prelim_gate_skip("你好，在吗") is False


def test_prelim_gate_band_arbitration(monkeypatch):
    """灰带仲裁钉（2026-09-20 真·小 Jev 批）：L0 的 P(retrieve) 落在灰带内才升级
    decider 复核；带外 L0 直放（decider 不被调用）；BAND=0 / decider 缺席 /
    BIODATA_DECIDER=off / spec 无带沿键（空带）→ 纯 L0 行为逐位不变。"""
    monkeypatch.setenv("BIODATA_PRELIM_GATE", "1")
    monkeypatch.delenv("BIODATA_PRELIM_GATE_BAND", raising=False)
    calls = {"decider": 0}

    class _FakeDecider:
        def __init__(self, l0_p, d_p=None, band=(0.2, 0.9)):
            self.l0_p, self.d_p, self.band = l0_p, d_p, band

        def score_with_fallback(self, task, text, prefer=()):
            return {"label": "retrieve", "impl": "l0",
                    "probs": {"retrieve": self.l0_p, "skip": 1 - self.l0_p}}

        def tau_with_fallback(self, task, env_var, key, impl, default):
            return 0.1  # L0/仲裁共用 τ（测试内可控即可）

        def operating_tau(self, task, env_var, key, default):
            return {"band_lo": self.band[0], "band_hi": self.band[1]}.get(key, default)

        def score(self, task, text):
            calls["decider"] += 1
            if self.d_p is None:
                return None
            return {"label": "retrieve", "confidence": self.d_p,
                    "probs": {"retrieve": self.d_p, "skip": 1 - self.d_p}}

        def _env(self):
            return "auto"

    def use(fake):
        calls["decider"] = 0
        monkeypatch.setattr(TURN, "_decider", fake)

    # 带内 + decider 判 retrieve → 放行（decider 被调用一次）
    use(_FakeDecider(0.5, 0.8))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 1
    # 带内 + decider 判 skip → skip
    use(_FakeDecider(0.5, 0.05))
    assert TURN._prelim_gate_skip("x") is True and calls["decider"] == 1
    # 高于带沿：L0 直放，decider 不被调用（即使它会判 skip）
    use(_FakeDecider(0.95, 0.05))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 0
    # 低于 τ：L0 直判 skip，不仲裁
    use(_FakeDecider(0.05, 0.8))
    assert TURN._prelim_gate_skip("x") is True and calls["decider"] == 0
    # BAND=0：纯 L0（带内分也不仲裁）
    monkeypatch.setenv("BIODATA_PRELIM_GATE_BAND", "0")
    use(_FakeDecider(0.5, 0.05))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 0
    monkeypatch.delenv("BIODATA_PRELIM_GATE_BAND", raising=False)
    # decider 缺席（None）：回落 L0 规则
    use(_FakeDecider(0.5, None))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 1
    # spec 无带沿键（空带 1.1/1.1）：不仲裁——旧工件行为逐位不变
    use(_FakeDecider(0.5, 0.05, band=(1.1, 1.1)))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 0
    # BIODATA_DECIDER=off：不仲裁
    class _OffDecider(_FakeDecider):
        def _env(self):
            return "off"
    use(_OffDecider(0.5, 0.05))
    assert TURN._prelim_gate_skip("x") is False and calls["decider"] == 0


def test_shipped_spec_carries_band_edges():
    """随包 decider_spec 必须带灰带带沿（2026-09-20 真·小 Jev 批）：防未来工件
    置换/回退时静默丢失 prelim 灰带仲裁（空带=不仲裁，无报错、无报警）。
    同时钉带沿合法域与 τ 合法域（不钉具体值——工作点允许随新工件迭代）。"""
    import json as _json
    from pathlib import Path as _Path
    spec = _json.loads((_Path(__file__).resolve().parents[1] / "src" / "dataset_recommender"
                        / "agent" / "router_models" / "decider" / "decider_spec.json"
                        ).read_text(encoding="utf-8"))
    assert spec.get("format") == "decider-router-v1"
    op = (spec.get("operating_points") or {}).get("prelim") or {}
    lo, hi = op.get("band_lo"), op.get("band_hi")
    assert lo is not None and hi is not None, "随包 spec 缺 band_lo/band_hi（灰带仲裁会静默失效）"
    assert 0.0 < lo < hi <= 1.0
    assert 0.0 < float(op.get("tau_retrieve") or 0.0) <= 1.0


def test_verdict_hook_gate_rescue(monkeypatch):
    """verdict hook：闸误放（skip）+ LLM 判 search → lazy 补起兜底；判 general/action
    → 信任闸不补（省下的正是这笔）；marker 分支既有行为逐位不变（非 action 即补）。"""
    started = []
    monkeypatch.setattr(TURN, "_lazy_start_flight",
                        lambda holder: started.append(holder))

    holder = {"flight": None, "markers": [], "prelim_gated": True}
    TURN._make_route_verdict_hook(holder)("search")
    assert started == [holder]

    started.clear()
    holder = {"flight": None, "markers": [], "prelim_gated": True}
    TURN._make_route_verdict_hook(holder)("general")
    TURN._make_route_verdict_hook(holder)("action")
    assert started == []

    # marker 分支：非 action 判决即 lazy 补起（既有行为钉，闸不改动它）。
    started.clear()
    holder = {"flight": None, "markers": ["同步"], }
    TURN._make_route_verdict_hook(holder)("general")
    assert started == [holder]

    # flight 在场 + action：取消；非 action：不动。
    cancelled = []
    fake_flight = SimpleNamespace(cancel=lambda: cancelled.append(1))
    holder = {"flight": fake_flight, "markers": []}
    TURN._make_route_verdict_hook(holder)("action")
    assert cancelled == [1]
    started.clear()
    holder = {"flight": fake_flight, "markers": ["同步"]}
    TURN._make_route_verdict_hook(holder)("search")
    assert started == []  # flight 已在，不重复补起


# ---------------------------------------------------------------- decider 降级链（Jev 复刻批）

def test_decider_fallback_chain(monkeypatch):
    """降级链纪律：BIODATA_DECIDER=off → 直落 L0（impl 如实标注）；
    decider 工件缺席/依赖缺席 → 自动回落 L0 不炸；only 模式缺席 → None
    （调用方升级 LLM）。链上任何实现缺席都不得成为新单点。"""
    from dataset_recommender.agent import decider as D
    monkeypatch.setenv("BIODATA_DECIDER", "off")
    hit = D.score_with_fallback("binary", "把前 5 条打包下载")
    assert hit is not None and hit["impl"] == "l0" and hit["label"] == "write"
    # auto 模式：decider 不可用（本仓库默认无 GPU 工件/或可用）→ 链必出 L0 或 decider，
    # 绝不静默吞掉判决。
    monkeypatch.setenv("BIODATA_DECIDER", "auto")
    hit = D.score_with_fallback("binary", "把前 5 条打包下载")
    assert hit is not None and hit["impl"] in ("l0", "decider") and hit["label"] == "write"
    # only 模式 + decider 缺席 → None（仅当 decider 真的不可用时断言）
    if not D.available():
        monkeypatch.setenv("BIODATA_DECIDER", "only")
        assert D.score_with_fallback("binary", "把前 5 条打包下载") is None


def test_decider_tau_fallback(monkeypatch):
    """τ 按实现取：l0 实现回 L0 工件 τ；env 覆盖永远优先。"""
    from dataset_recommender.agent import decider as D
    monkeypatch.delenv("BIODATA_ROUTE_L0_TAU", raising=False)
    tau_l0 = D.tau_with_fallback("binary", "BIODATA_ROUTE_L0_TAU", "tau", "l0", 1.1)
    assert 0 < tau_l0 <= 1.0  # L0 工件内嵌级联 τ
    monkeypatch.setenv("BIODATA_ROUTE_L0_TAU", "0.77")
    assert D.tau_with_fallback("binary", "BIODATA_ROUTE_L0_TAU", "tau", "decider", 1.1) == 0.77
