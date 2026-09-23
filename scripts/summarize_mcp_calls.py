# -*- coding: utf-8 -*-
"""汇总 MCP 调用留痕（.userdata/mcp_calls.jsonl）→ 需求分析证据。

读 `mcp_server.py` 全部工具入口 append 的调用日志（schema `biodata-mcp-calls/v1`，常量
`_CALL_LOG_SCHEMA` 为单一真源；v0→v1 为 批 additive 升级，每行新增 call_id，本脚本
按 dict 容错读取、不挑版本），输出：

- 调用总数、ok/isError 分布、错误码分布；
- 工具分布（哪个工具被真实任务用得最多）；
- 时间跨度（首条 → 末条，ISO8601 UTC）；
- **含文件级约束的 query 占比**——kill-criteria「20 个真实任务 <5 个需要文件级约束」的判定
  证据：从 query/utterance 原话里数有多少条提到 FASTQ / raw / filtered / 文件类型 等文件级
  关键词（词表见 `FILE_LEVEL_KEYWORDS` 常量，改词表只改这里）。

纯 stdlib、零网络；只读日志文件，不写任何东西。日志脱敏由写入侧（mcp_server._logged）保证，
本脚本不输出 query 原文，只输出计数与占比。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

LOG_SCHEMA = "biodata-mcp-calls/v1"   # 与 mcp_server._CALL_LOG_SCHEMA 对齐（单一真源在写入侧）

# 文件级约束关键词（判定「这个任务需要文件级信息」的启发词表；唯一真源）。
# ASCII 词按词边界匹配（防 withdraw 误中 raw），中文词按子串匹配。
FILE_LEVEL_KEYWORDS_ASCII = ("fastq", "raw", "filtered", "unfiltered", "h5ad", "mtx",
                             "loom", "rds", "bam", "10x 文件", "fileset")
FILE_LEVEL_KEYWORDS_ZH = ("原始数据", "原始文件", "文件类型", "文件级", "文件清单",
                          "矩阵文件", "表达矩阵", "下载文件")
_ASCII_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in FILE_LEVEL_KEYWORDS_ASCII
                                         if k.isascii()) + r")\b", re.I)

# 哪些参数名算「用户原话」（query 是需求分析核心证据；utterance 是 plan_* 工具的原话槽）
_QUERY_KEYS = ("query", "utterance")


def default_log_path() -> Path:
    return Path(__file__).resolve().parent.parent / ".userdata" / "mcp_calls.jsonl"


def has_file_level_constraint(text: str) -> bool:
    """一条原话是否提到文件级约束（FASTQ/raw/filtered/文件类型…）。纯函数，便于单测。"""
    if not text:
        return False
    if _ASCII_RE.search(text):
        return True
    return any(k in text for k in FILE_LEVEL_KEYWORDS_ZH)


def _queries_of(record: dict) -> list:
    params = record.get("params")
    if not isinstance(params, dict):
        return []
    return [str(params[k]) for k in _QUERY_KEYS
            if isinstance(params.get(k), str) and params[k].strip()]


def summarize(records: list) -> dict:
    """records（已解析的日志行 dict 列表）→ 统计 dict。纯函数，便于单测。"""
    tool_counter: Counter = Counter()
    error_counter: Counter = Counter()
    n_ok = 0
    n_err = 0
    first_ts = None
    last_ts = None
    n_queries = 0
    n_file_level = 0
    for rec in records:
        tool_counter[str(rec.get("tool") or "?")] += 1
        if rec.get("ok"):
            n_ok += 1
        else:
            n_err += 1
            error_counter[str(rec.get("error") or "?")] += 1
        ts = rec.get("ts")
        if isinstance(ts, str) and ts:
            first_ts = ts if first_ts is None or ts < first_ts else first_ts
            last_ts = ts if last_ts is None or ts > last_ts else last_ts
        for q in _queries_of(rec):
            n_queries += 1
            if has_file_level_constraint(q):
                n_file_level += 1
                break   # 同一条调用多个原话槽只算一次任务
    share = (n_file_level / n_queries) if n_queries else 0.0
    return {
        "schema": LOG_SCHEMA,
        "total_calls": len(records),
        "ok": n_ok,
        "is_error": n_err,
        "tools": dict(tool_counter.most_common()),
        "errors": dict(error_counter.most_common()),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "queries_total": n_queries,
        "queries_with_file_level": n_file_level,
        "file_level_share": round(share, 4),
        "kill_criteria": {
            "rule": "20 个真实任务 <5 个需要文件级约束",
            "verdict": ("pass" if n_queries >= 1 and n_file_level < max(1, round(n_queries * 0.25))
                        else "watch"),
            "note": "占比参照线 25%（5/20）；样本不足 20 条时仅供参考。",
        },
    }


def load_records(path: Path) -> "tuple[list, int]":
    """读 jsonl → (记录列表, 跳过的坏行数)。坏行静默跳过并计数，不让一行脏数据掀翻整个汇总。"""
    records: list = []
    skipped = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(rec, dict):
                records.append(rec)
            else:
                skipped += 1
    return records, skipped


def render_zh(stats: dict, skipped: int) -> str:
    lines = [
        "MCP 调用留痕汇总（本地 .userdata/mcp_calls.jsonl，零网络、只读）",
        f"  调用总数：{stats['total_calls']}（ok {stats['ok']}，isError {stats['is_error']}）",
    ]
    if skipped:
        lines.append(f"  跳过无法解析的坏行：{skipped}")
    if stats["first_ts"]:
        lines.append(f"  时间跨度：{stats['first_ts']} → {stats['last_ts']}（UTC）")
    if stats["tools"]:
        lines.append("  工具分布：")
        for name, n in stats["tools"].items():
            lines.append(f"    {name:28s} {n}")
    if stats["errors"]:
        lines.append("  错误码分布：")
        for code, n in stats["errors"].items():
            lines.append(f"    {code:28s} {n}")
    lines += [
        f"  含原话的调用：{stats['queries_total']}；其中含文件级约束（FASTQ/raw/filtered/文件类型…）："
        f"{stats['queries_with_file_level']}（{stats['file_level_share'] * 100:.1f}%）",
        f"  kill-criteria 参照（{stats['kill_criteria']['rule']}）：{stats['kill_criteria']['verdict']}"
        f"——{stats['kill_criteria']['note']}",
    ]
    return "\n".join(lines)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--path", default="", help="日志文件路径（默认 <仓库>/.userdata/mcp_calls.jsonl）")
    ap.add_argument("--json", action="store_true", help="只输出机器可读 JSON")
    args = ap.parse_args(argv)

    path = Path(args.path).expanduser() if args.path else default_log_path()
    if not path.is_file():
        print(f"没有找到调用日志：{path}\n"
              "（MCP 工具每次调用都会自动留痕；确认服务器跑过、且未设 BIODATA_MCP_CALL_LOG=off。）",
              file=sys.stderr)
        return 1
    records, skipped = load_records(path)
    stats = summarize(records)
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=1))
    else:
        print(render_zh(stats, skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
