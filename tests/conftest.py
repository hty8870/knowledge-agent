# -*- coding: utf-8 -*-
"""pytest 全局夹具。

测试直接调用 MCP 工具是常态（test_mcp_server.py / test_mcp_provision.py / …）；
调用留痕默认开 → 不重定向的话整个测试套件会把成千上万条测试调用写进真实
`.userdata/mcp_calls.jsonl`，污染需求分析证据。这里把落盘文件统一重定向到
per-test 临时路径；test_mcp_call_log.py 再用自己的 monkeypatch 精细控制。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 包身份单一真源：历史上一半测试经
# `src.dataset_recommender.*`（repo 根在 sys.path 时的命名空间包）导入、另一半经
# `dataset_recommender.*`（src/ 在 sys.path）——同一物理文件成了**两个模块对象**，
# 单例/锁/LangGraph 编译实例/monkeypatch 一式两份（scoped 绿全量红的根因）。
# 现统一为 `dataset_recommender.*`；这里显式兜底两条路径（根留给历史根级模块兼容、
# src 给包； 一级目录整理后 mcp_server 也入包为
# `dataset_recommender.app.mcp_server`），不再依赖「从仓库根调 python -m pytest」的 cwd 巧合。
_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _redirect_mcp_call_log(tmp_path, monkeypatch):
    """双闸防污染（mcp_server 已导入时生效）：
    ① 直接调用：把调用日志落盘重定向到 per-test tmp 路径（不污染真实 .userdata/）；
    ② 真 stdio 子进程（test_mcp_validation.py 等 spawn 的协议测试）：子进程继承 env，
       文件重定向够不着 → 用 env 整体关停。test_mcp_call_log.py 自己再显式打开。"""
    module = sys.modules.get("dataset_recommender.app.mcp_server")
    if module is not None:
        monkeypatch.setattr(module, "_CALL_LOG_FILE", tmp_path / "mcp_calls.jsonl")
        monkeypatch.setenv("BIODATA_MCP_CALL_LOG", "off")


@pytest.fixture(autouse=True)
def _stub_checklist_call_globally(monkeypatch):
    """清单核销：understand 的清单轻量调用全局默认 stub。

    agent 图内测试的 FakeModel 应答序列普遍不预置清单应答——complex+EXEC 话术会触发
    清单调用、吃掉下一个预置应答造成全线错位。统一默认 stub 成「无清单」；
    要测清单本身的钉在测试体内用 import 期存的真引用后执行 setattr 覆盖（conftest
    fixture 先于文件级 fixture 与测试体执行，后执行生效）。

     起包身份统一为 `dataset_recommender.*` 单名（历史双名同 stub 补丁退役，
    见本文件顶部路径说明）；`tests/test_single_package_identity.py` 机械防回潮。"""
    import importlib
    module = sys.modules.get("dataset_recommender.agent.agent_exec")
    if module is None:
        try:
            module = importlib.import_module("dataset_recommender.agent.agent_exec")
        except ImportError:
            return
    monkeypatch.setattr(module, "_task_checklist_call", lambda *a, **k: ([], 0, ""))


@pytest.fixture(autouse=True)
def _stub_route_consensus_globally(monkeypatch):
    """ 转正（新逻辑摘除批）：route_consensus 恒为环首，全局默认 stub。

    agent 图内测试的 FakeModel 应答序列普遍不预置分流投票应答——共识投票会吃掉下一个
    预置应答造成全线错位（与上方清单 stub 同一问题）。统一默认 stub 成 general（全集
    安全地板 = 旧全量面的最近邻）；要测共识投票本身的钉（test_scoped_routing.py /
    test_trace_hooks.py）用 import 期存的真引用后执行 setattr 覆盖（conftest fixture
    先于文件级 fixture 与测试体执行，后执行生效）。

    掩盖风险（如实留痕）：本 stub 把所有图内测试钉在
    **general 面**——scoped 套件面（search/action）下的 understand/repair 提示词装配、
    JSON 兜底壳的按面铁律（action_plan._constraints_zh）在图内全无覆盖， 高2 的
    「scoped JSON 壳带全表铁律」就是被这层掩盖放过的。对冲钉：
    test_scoped_routing.py::test_json_only_model_walks_search_face_full_graph
    （真共识 + JSON-only 替身把 search 面全图走通）；改 scoped 提示词装配时先跑那条。"""
    import importlib
    module = sys.modules.get("dataset_recommender.agent.agent_exec")
    if module is None:
        try:
            module = importlib.import_module("dataset_recommender.agent.agent_exec")
        except ImportError:
            return
    monkeypatch.setattr(module, "_run_route_consensus", lambda *a, **k: ("general", []))


@pytest.fixture(autouse=True)
def _pin_legacy_route_defaults(monkeypatch):
    """分流三改（2026-09-19）环境钉：测试会话默认钉旧通道——三分类分流、
    L0 级联关、检索闸关（生产默认已是 binary + L0 开 + 闸开；套件里既有用例
    大量打在 FakeModel 应答序列/flight 起跑时序上，默认值翻转会造成全线错位，
    与上方 route_consensus stub 同一问题）。三分类作为文档化兜底通道由既有套件
    继续全量覆盖；新默认通道（binary/L0 级联/检索闸）的覆盖在
    tests/test_route_gates.py——各用例显式 setenv 启用（conftest 先执行，
    测试体内的 monkeypatch 后执行生效）。"""
    monkeypatch.setenv("BIODATA_ROUTE_MODE", "three_class")
    monkeypatch.setenv("BIODATA_ROUTE_L0", "0")
    monkeypatch.setenv("BIODATA_PRELIM_GATE", "0")
