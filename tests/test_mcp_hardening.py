# -*- coding: utf-8 -*-
"""MCP 入参与错误脱敏加固的回归门。

钉死的病形：
-   pack_failed/plan_failed ×3 把 {exc}（可含绝对路径）直送客户端且 stderr 无堆栈
        → 统一走 _internal_error：客户端只见类型名，堆栈留 stderr；
-   build_task_pack 日期校验与 recommend_datasets 同口径（严格档）：
        非法日期/倒挂窗口 → bad_param 点名，空分支回显实际生效的 retrieval_params；
-   build_reuse_pack 单条 uid 无长度上限 → 200 字符上限，超限 bad_param；
- 同族顺手收口：build 模式空指纹「if 给了才比」短路 → 缺一即 bad_param（Web 侧同型病，文档口径本就是 build 必须带全）。

打桩测试全程 tmp/假对象；真实语料只读（preview/build 均为只读工具），零写盘、零 LLM。
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from dataset_recommender.app import mcp_server as M


# ---------------------------------------------------------------- 三处脱敏漏网

def _assert_sanitized(func, capsys, *args, **kwargs):
    with pytest.raises(ToolError) as exc_info:
        func(*args, **kwargs)
    msg = str(exc_info.value)
    assert msg.startswith("internal_error: PermissionError"), msg
    assert "leak_canary" not in msg          # 异常正文（绝对路径）不进客户端
    assert "Traceback" in capsys.readouterr().err   # 完整堆栈留服务器侧 stderr


def test_build_task_pack_internal_error_is_sanitized(monkeypatch, capsys):
    def boom():
        raise PermissionError(13, "Permission denied", "Z:\\leak_canary\\corpus")

    monkeypatch.setattr(M, "_workflow", boom)
    _assert_sanitized(M.build_task_pack, capsys, mode="preview", query="human")


def test_plan_query_edit_internal_error_is_sanitized(monkeypatch, capsys):
    from dataset_recommender.app import board

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "Z:\\leak_canary\\board")

    monkeypatch.setattr(board, "plan_edit", boom)
    _assert_sanitized(M.plan_query_edit, capsys, query="human", utterance="换成小鼠")


def test_plan_action_internal_error_is_sanitized(monkeypatch, capsys):
    from dataset_recommender.agent import action_plan

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied", "Z:\\leak_canary\\action")

    monkeypatch.setattr(action_plan, "plan_action", boom)
    _assert_sanitized(M.plan_action, capsys, "把前 5 条打包下载")


# ---------------------------------------------------------------- 日期校验 + 空分支回显
# 两侧统一为严格档：非法 → bad_param，倒挂 → bad_param
# （旧「非年份打头 → 忽略=不限」宽松档已退役，与 Web `_require_iso_date` 同口径）。

class _FakeMeta:
    retrieved_data = []
    active_filters = []
    coverage_caveats = []
    unused_query_terms = []
    search_trace = {}
    result_total = 0
    resolution_status = "no_match"


@pytest.fixture
def fake_empty_retrieval(tmp_path, monkeypatch):
    """打桩 workflow + 空语料根：candidates 恒空 → 走「没有命中」分支，全程不碰真实语料。"""
    captured = {}

    class FakeWorkflow:
        def run_with_meta(self, p=None, **kwargs):
            # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
            captured.update(vars(p) if p is not None else kwargs)
            return _FakeMeta()

    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "_workflow", lambda: FakeWorkflow())
    monkeypatch.setattr(M, "_settings", lambda: SimpleNamespace(project_root=tmp_path, data_dir=base))
    return captured


def test_build_task_pack_garbage_date_is_rejected_not_literal_compared(fake_empty_retrieval):
    """病形：date_from='zzz' 曾直传检索层做字面比较（'zzz' 字典序大于一切 ISO 日期），
    把全库静默归零再谎称「没有命中」。拉齐为严格档：
    与 Web 同口径，垃圾日期 → bad_param 点名参数，既不字面比较、也不静默忽略。"""
    with pytest.raises(ToolError, match=r"bad_param: date_from 需要 YYYY-MM-DD 格式的日期"):
        M.build_task_pack(mode="preview", query="human", date_from="zzz")
    with pytest.raises(ToolError, match=r"bad_param: date_to 需要 YYYY-MM-DD 格式的日期"):
        M.build_task_pack(mode="preview", query="human", date_to="今天")


def test_build_task_pack_nonexistent_date_and_inverted_window_rejected(fake_empty_retrieval):
    """日历不存在的日期与 from>to 倒挂窗口 → bad_param（严格档）。"""
    with pytest.raises(ToolError, match=r"bad_param: date_from 不是真实存在的日期"):
        M.build_task_pack(mode="preview", query="human", date_from="2026-02-30")
    with pytest.raises(ToolError, match=r"bad_param: 发表时间范围颠倒"):
        M.build_task_pack(mode="preview", query="human", date_from="2025-01-01", date_to="2024-01-01")


def test_build_task_pack_valid_date_passes_and_is_echoed(fake_empty_retrieval):
    r = M.build_task_pack(mode="preview", query="human", date_from="2020-01-01")
    assert r["ok"] is True
    assert fake_empty_retrieval["date_from"] == "2020-01-01"
    assert r["retrieval_params"]["date_from"] == "2020-01-01"


def test_build_task_pack_date_boundary_equal_from_to_ok(fake_empty_retrieval):
    """边界：from == to 合法单天窗，透传并回显，不误伤。"""
    r = M.build_task_pack(mode="preview", query="human", date_from="2020-06-01", date_to="2020-06-01")
    assert r["ok"] is True
    assert fake_empty_retrieval["date_from"] == "2020-06-01"
    assert fake_empty_retrieval["date_to"] == "2020-06-01"
    assert r["retrieval_params"]["date_from"] == "2020-06-01"
    assert r["retrieval_params"]["date_to"] == "2020-06-01"


# ---------------------------------------------------------------- 同族收口：build 空指纹即拒（真实语料只读）

def test_build_task_pack_build_mode_requires_fingerprints():
    """空指纹三件套曾走「if 给了才比」短路、篡改条件也照产出。现在缺一即 bad_param。"""
    preview = M.build_task_pack(mode="preview", query="human", limit=10)
    assert preview["ok"] is True and preview["candidate_uids"]
    selected = preview["candidate_uids"][:2]
    with pytest.raises(ToolError, match="bad_param"):
        M.build_task_pack(mode="build", query="human", selected_uids=selected)
    # 合法全量回传不受误伤：预览指纹原样带回 → 正常产出
    built = M.build_task_pack(
        mode="build", query="human", selected_uids=selected,
        plan_token=preview["plan_token"],
        snapshot_id=preview["retrieval"]["snapshot_id"],
        content_digest=preview["retrieval"]["content_digest"],
        retrieval_date=preview["retrieval"]["date"],
    )
    assert built["ok"] is True and built["files"]


# ---------------------------------------------------------------- 单条 uid 长度上限

def test_reuse_pack_uid_length_cap():
    from dataset_recommender.content.reuse_pack import ReusePackError

    with pytest.raises(ReusePackError, match="过长"):
        M.sanitize_uids_shared(["x" * 201])
    assert M.sanitize_uids_shared(["x" * 200]) == ["x" * 200]   # 边界值不误伤
    with pytest.raises(ToolError, match="bad_param"):
        M.build_reuse_pack(["x" * 100_000])   # 100KB 输入不再被原样回显放大


# ---------------------------------------------------------------- 文案口径收口

def test_mcp_copy_uses_unified_terms():
    """「外部平台库」→「外部库」全库统一；工具描述不再谎称「与网页端一致，不因手滑报错」。
    非法日期两侧统一显式报错；
    这条钉字保留防回潮——旧宽松档话术（忽略=不限/不因手滑报错）不得回到源码。"""
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert "外部平台库" not in src
    assert "与网页端一致，不因手滑报错" not in src
