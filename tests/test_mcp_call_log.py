# -*- coding: utf-8 -*-
"""MCP 调用留痕（v1.29.0）测试：日志行 schema、脱敏、off 开关、写失败降级、汇总口径。

全程禁网。落盘文件由 tests/conftest.py 的 autouse 夹具重定向到 per-test tmp 路径
（`M._CALL_LOG_FILE`），绝不写真实 .userdata/mcp_calls.jsonl。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT), str(ROOT / "scripts")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.app import mcp_server as M  # noqa: E402
import summarize_mcp_calls as S  # noqa: E402


@pytest.fixture(autouse=True)
def _call_log_on(monkeypatch):
    """本文件专测留痕本身：显式打开（conftest 为防真实日志污染，对全局默认关停）。"""
    monkeypatch.setenv(M._CALL_LOG_ENV, "on")


def _read_log() -> list:
    path = Path(M._CALL_LOG_FILE)
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


# ---------------------------------------------------------------- 日志行 schema

def test_log_line_schema_and_query_verbatim():
    """正常调用落一行：schema 常量 / call_id 随机唯一 / ISO8601 UTC 时间戳 / 工具名 /
    query 原话 / 耗时 / ok / error=None。"""
    r = M.parse_constraints(query="人类肺癌单细胞，要有 FASTQ")
    assert r["ok"]
    lines = _read_log()
    assert len(lines) == 1
    rec = lines[0]
    assert rec["schema"] == M._CALL_LOG_SCHEMA == S.LOG_SCHEMA   # 写入侧与汇总脚本同一份常量
    assert rec["schema"] == "biodata-mcp-calls/v1"               # ：v0 → v1（additive 加 call_id）
    assert rec["tool"] == "parse_constraints"
    assert rec["params"]["query"] == "人类肺癌单细胞，要有 FASTQ"   # query 原话要记（需求分析核心证据）
    assert rec["ts"].endswith("Z") and "T" in rec["ts"]            # ISO8601 UTC
    assert isinstance(rec["duration_ms"], (int, float)) and rec["duration_ms"] >= 0
    assert rec["ok"] is True and rec["error"] is None
    assert set(rec.keys()) == {"schema", "call_id", "ts", "tool", "params", "duration_ms", "ok", "error"}
    assert re.fullmatch(r"[0-9a-f]{32}", rec["call_id"]), f"call_id 应为 32 位 hex：{rec['call_id']!r}"


def test_call_id_unique_per_call():
    """call_id 随机唯一（埋点中继/去重用）：同进程连续两次调用不得重复。"""
    M.parse_constraints(query="人类肺癌")
    M.parse_constraints(query="小鼠肝脏")
    ids = [rec["call_id"] for rec in _read_log()]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_call_log_path_aligned_with_webapp_userdata():
    """ 关键核实：MCP 留痕文件与 Web 遥测端点读的是**同一物理文件**。

    安装版 MCP exe 是独立进程，但两边都经 runtime_paths 单一真源解析 data_root
    （source/portable = 项目根；frozen = %LOCALAPPDATA%/BioDataAgent）。conftest 会把
    `M._CALL_LOG_FILE` 重定向到 per-test tmp（防真实日志污染），故这里直接对**定义式**做
    等价性断言：webapp 的路径 == mcp_server 同一公式的求值结果；再静态钉住 mcp_server
    的 `_CALL_LOG_FILE` 定义式（防将来改路径逻辑而不同步）。
    """
    from dataset_recommender.app import webapp as W
    from dataset_recommender.app.runtime_paths import get_app_paths, instance_data_dir_for

    mcp_formula = instance_data_dir_for(get_app_paths().data_root, ".userdata") / "mcp_calls.jsonl"
    assert mcp_formula.resolve() == W._mcp_calls_log_path().resolve()
    src = Path(M.__file__).read_text(encoding="utf-8")
    assert (
        '_CALL_LOG_FILE = instance_data_dir_for(get_app_paths().data_root, ".userdata") / "mcp_calls.jsonl"'
    ) in src


def test_error_call_logs_machine_code():
    """非法调用（坏 uid）也留痕：ok=false + 机器码（bad_uid），异常照常抛出。"""
    with pytest.raises(ToolError, match="bad_uid"):
        M.get_file_manifest("___definitely_not_a_uid___")
    rec = _read_log()[-1]
    assert rec["ok"] is False and rec["error"] == "bad_uid"
    assert rec["tool"] == "get_file_manifest"


# ---------------------------------------------------------------- 脱敏

def test_sensitive_params_never_written():
    """脱敏铁律：api_key/token/password 类参数名 → 值一律 <redacted>，绝不落盘；query 原话保留。

    现有工具签名本就不收密钥字段（extra=forbid 在协议层挡），这里用 @_logged 直接包一个
    带密钥形参的函数，验证装饰器这层真脱敏。"""
    @M._logged
    def fake_tool(query: str, api_key: str = "", llm_token: str = "", password: str = ""):
        return {"ok": True}

    fake_tool(query="小鼠肝脏", api_key="sk-SECRET-123", llm_token="tok-SECRET-456", password="pw-SECRET")
    rec = _read_log()[-1]
    assert rec["params"]["query"] == "小鼠肝脏"
    for k in ("api_key", "llm_token", "password"):
        assert rec["params"][k] == "<redacted>"
    raw = Path(M._CALL_LOG_FILE).read_text(encoding="utf-8")
    assert "SECRET" not in raw


def test_secret_values_inside_free_text_are_redacted():
    """（验证）：参数名不敏感、但**值**里误粘了真 key 形状——
    值级共享 redactor 兜住（与交付扫描/质量门同一真源，强锚定近零误报）。
    query 原话要记的契约不变：锚定模式不碰普通中文。"""
    fake_key = "sk-" + "A" * 24   # 逼真形状（拼接构造：源码里不出现可被交付扫描命中的完整字面）
    M.parse_constraints(query=f"把 {fake_key} 配到哪里")
    rec = _read_log()[-1]
    assert fake_key not in json.dumps(rec, ensure_ascii=False)
    assert "[REDACTED:openai-secret-key]" in rec["params"]["query"]
    assert rec["params"]["query"].startswith("把 ") and rec["params"]["query"].endswith(" 配到哪里")
    # 嵌套结构（dict/list 参数）同样过值级 redactor
    @M._logged
    def fake_tool2(payload: dict):
        return {"ok": True}

    fake_tool2(payload={"note": f"key 是 {fake_key}"})
    rec = _read_log()[-1]
    assert fake_key not in json.dumps(rec, ensure_ascii=False)
    # 良性 query 原文契约不变（值级 redactor 对普通中文零触碰）
    M.parse_constraints(query="人类肺癌单细胞，要有 FASTQ")
    assert _read_log()[-1]["params"]["query"] == "人类肺癌单细胞，要有 FASTQ"


def test_long_param_truncated():
    """超长参数值截断落盘（防日志膨胀），截断标记「…」。"""
    M.parse_constraints(query="肺" * 500)
    rec = _read_log()[-1]
    assert len(rec["params"]["query"]) == M._MAX_PARAM_CHARS + 1
    assert rec["params"]["query"].endswith("…")


# ---------------------------------------------------------------- off 开关 / 写失败降级

def test_env_off_disables_logging(monkeypatch):
    monkeypatch.setenv(M._CALL_LOG_ENV, "off")
    M.parse_constraints(query="人类肺癌")
    assert _read_log() == []
    monkeypatch.setenv(M._CALL_LOG_ENV, "OFF")   # 大小写不敏感
    M.parse_constraints(query="人类肺癌")
    assert _read_log() == []


def test_log_write_failure_never_breaks_tool(monkeypatch, tmp_path):
    """日志写失败（落盘路径撞上一个同名文件）→ 静默降级，工具调用本身完全不受影响。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(M, "_CALL_LOG_FILE", blocker / "mcp_calls.jsonl")  # 父路径是文件 → mkdir/open 必败
    r = M.parse_constraints(query="人类肺癌单细胞")
    assert r["ok"] and r["understood"]


# ---------------------------------------------------------------- 汇总脚本口径

def _write_jsonl(path: Path, rows: list) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                    encoding="utf-8")


def _row(tool, query=None, ok=True, error=None, ts="2026-08-01T10:00:00.000Z"):
    return {"schema": S.LOG_SCHEMA, "ts": ts, "tool": tool,
            "params": ({"query": query} if query is not None else {}),
            "duration_ms": 1.0, "ok": ok, "error": error}


def test_file_level_keyword_detection():
    assert S.has_file_level_constraint("人类肺癌单细胞，要有 FASTQ 原始数据")
    assert S.has_file_level_constraint("要 raw 的")
    assert S.has_file_level_constraint("带 filtered 矩阵的")
    assert S.has_file_level_constraint("给我文件类型说明")
    assert not S.has_file_level_constraint("人类肺癌单细胞数据")
    assert not S.has_file_level_constraint("")           # 空串不算
    assert not S.has_file_level_constraint("withdrawn dataset")  # 词边界：withdraw 不命中 raw


def test_summarize_counts_tools_timespan_and_file_level_share(tmp_path, capsys):
    """汇总口径：总数 / 工具分布 / 时间跨度 / 含文件级约束 query 占比 / 坏行跳过。"""
    log = tmp_path / "mcp_calls.jsonl"
    _write_jsonl(log, [
        _row("recommend_datasets", "人类肺癌单细胞，要有 FASTQ", ts="2026-08-01T09:00:00.000Z"),
        _row("recommend_datasets", "小鼠肝脏空间转录组", ts="2026-08-01T10:00:00.000Z"),
        _row("parse_constraints", "人类肺癌", ts="2026-08-01T11:00:00.000Z"),
        _row("get_file_manifest", ok=False, error="bad_uid", ts="2026-08-01T12:00:00.000Z"),
    ])
    log.write_text(log.read_text(encoding="utf-8") + "{ not json\n", encoding="utf-8")
    rc = S.main(["--path", str(log)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "调用总数：4" in out and "ok 3，isError 1" in out
    assert "recommend_datasets" in out and "bad_uid" in out
    assert "2026-08-01T09:00:00.000Z → 2026-08-01T12:00:00.000Z" in out
    assert "坏行：1" in out
    # 3 条含原话的调用里 1 条含文件级约束（FASTQ）→ 33.3%
    assert "含原话的调用：3" in out and "1（33.3%）" in out
    # --json 机器可读
    assert S.main(["--path", str(log), "--json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["total_calls"] == 4 and stats["queries_total"] == 3
    assert stats["queries_with_file_level"] == 1
    assert stats["tools"] == {"recommend_datasets": 2, "parse_constraints": 1, "get_file_manifest": 1}


def test_summarize_missing_log_returns_1(tmp_path, capsys):
    assert S.main(["--path", str(tmp_path / "nope.jsonl")]) == 1
    assert "没有找到调用日志" in capsys.readouterr().err
