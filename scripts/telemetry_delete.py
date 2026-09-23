#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 install_id 删除遥测数据。

删除 ingest_packets 中 install_id 匹配的主包，并在同一事务内级联删除对应的
ingest_packet_receipts 与 ingest_event_receipts（幂等账本不留孤儿）。

**删除范围**：本脚本只动数据库三张表。
以下产物**不在其列**，按 install 追责/抹除时需另行处理：
- 每日导出产物（telemetry_export.py --out 目录，默认 /data/export 下的
  impressions/interactions/labels/mcp_calls 等 JSONL）——重跑导出覆盖，或按需
  清空该导出目录；
- 导出副产物 quarantine.jsonl / review.html（同目录）；
- 数据库/文件备份（由部署方策略决定）——随其保留策略处理，不等本脚本。

默认 dry-run 只打印将删计数；--yes 才真删（防误删，操作不可逆）。

用法：
  python scripts/telemetry_delete.py --dsn <dsn> --install-id <id> [--yes]
  --dsn          PG 连接串（postgresql+psycopg2://...）或 SQLite 文件路径；也可读 env BIODATA_TELEMETRY_DSN。
  --install-id   要删除数据的安装码（ingest_packets.install_id 精确匹配）。

表结构镜像自 services/telemetry-receiver/app.py 与 telemetry_idempotency.py（只读/删除消费）。
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from sqlalchemy import BigInteger, Column, DateTime, Integer, MetaData, Table, Text, create_engine, delete, select
from sqlalchemy.types import JSON

metadata = MetaData()

ingest_packets = Table(
    "ingest_packets",
    metadata,
    Column("id", BigInteger, primary_key=True),
    Column("received_at", DateTime(timezone=True)),
    Column("install_id", Text),
    Column("schema", Text),
    Column("ua", Text),
    Column("cache_generation", Text),
    Column("n_usage", Integer),
    Column("n_benchfb", Integer),
    Column("payload", JSON),
)

packet_receipts = Table(
    "ingest_packet_receipts",
    metadata,
    Column("packet_id", Text, primary_key=True),
    Column("received_at", DateTime(timezone=True)),
    Column("identity", Text),
    Column("row_id", BigInteger),
)

event_receipts = Table(
    "ingest_event_receipts",
    metadata,
    Column("event_key", Text, primary_key=True),
    Column("received_at", DateTime(timezone=True)),
    Column("packet_id", Text),
    Column("kind", Text),
)


def _make_engine(dsn: str):
    dsn = dsn.strip()
    if dsn.startswith(("sqlite", "postgresql")):
        url = dsn
    else:
        url = "sqlite:///" + dsn
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    return create_engine(url, **kwargs)


def plan_delete(engine, install_id: str) -> dict[str, int]:
    """只读统计将删行数：主包、packet receipts、event receipts。"""
    with engine.connect() as conn:
        rows = conn.execute(
            select(ingest_packets.c.id, ingest_packets.c.payload).where(
                ingest_packets.c.install_id == install_id
            )
        ).all()
        payload_packet_ids = {
            str(p.get("packet_id")) for _, p in rows if isinstance(p, dict) and p.get("packet_id")
        }
        # 兼容 identity 直接用 install_id 的 receipts（identity_of 的兜底分支）
        identity_matched = conn.execute(
            select(packet_receipts.c.packet_id).where(packet_receipts.c.identity == install_id)
        ).scalars().all()
        packet_ids = payload_packet_ids | {str(x) for x in identity_matched}
        n_packets = conn.execute(
            select(packet_receipts.c.packet_id).where(packet_receipts.c.packet_id.in_(packet_ids))
        ).scalars().all() if packet_ids else []
        n_events = conn.execute(
            select(event_receipts.c.event_key).where(event_receipts.c.packet_id.in_(packet_ids))
        ).scalars().all() if packet_ids else []
    return {"packets": len(rows), "packet_receipts": len(n_packets), "event_receipts": len(n_events)}


def execute_delete(engine, install_id: str) -> dict[str, int]:
    """事务删除主包 + 两张 receipts，返回删除计数。"""
    with engine.begin() as conn:
        rows = conn.execute(
            select(ingest_packets.c.id, ingest_packets.c.payload).where(
                ingest_packets.c.install_id == install_id
            )
        ).all()
        payload_packet_ids = {
            str(p.get("packet_id")) for _, p in rows if isinstance(p, dict) and p.get("packet_id")
        }
        identity_matched = conn.execute(
            select(packet_receipts.c.packet_id).where(packet_receipts.c.identity == install_id)
        ).scalars().all()
        packet_ids = payload_packet_ids | {str(x) for x in identity_matched}
        if packet_ids:
            result_e = conn.execute(delete(event_receipts).where(event_receipts.c.packet_id.in_(packet_ids)))
            result_p = conn.execute(delete(packet_receipts).where(packet_receipts.c.packet_id.in_(packet_ids)))
        else:
            result_e = result_p = None
        result = conn.execute(delete(ingest_packets).where(ingest_packets.c.install_id == install_id))
    return {
        "packets": result.rowcount,
        "packet_receipts": result_p.rowcount if result_p is not None else 0,
        "event_receipts": result_e.rowcount if result_e is not None else 0,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="按 install_id 删除遥测数据（默认 dry-run；--yes 真删）",
        epilog="删除范围：仅 PG/SQLite 三张表（ingest_packets + 两张 receipts）。每日导出产物"
               "（--out 目录）、quarantine.jsonl、review.html、/backup 备份不在其列，需另行清理"
               "（备份随 7 天保留期自动过期）。",
    )
    ap.add_argument("--dsn", default=None, help="PG 连接串或 SQLite 文件路径；缺省读 env BIODATA_TELEMETRY_DSN")
    ap.add_argument("--install-id", required=True, help="要删除数据的安装码")
    ap.add_argument("--yes", action="store_true", help="确认执行删除（默认只打印将删计数）")
    args = ap.parse_args(argv)

    dsn = args.dsn or os.environ.get("BIODATA_TELEMETRY_DSN")
    if not dsn:
        print("error: 需要 --dsn 或 env BIODATA_TELEMETRY_DSN", file=sys.stderr)
        return 2
    engine = _make_engine(dsn)
    try:
        counts = plan_delete(engine, args.install_id)
    except Exception as exc:  # noqa: BLE001
        print(f"error: 统计失败：{exc}", file=sys.stderr)
        return 1

    print(f"[telemetry-delete] install_id={args.install_id}：将删 主包 {counts['packets']}、"
          f"packet receipts {counts['packet_receipts']}、event receipts {counts['event_receipts']}。")
    if not args.yes:
        print("[telemetry-delete] dry-run（未删除）；加 --yes 执行。")
        return 0
    if not counts["packets"]:
        print("[telemetry-delete] 无匹配数据，无需删除。")
        return 0
    try:
        deleted = execute_delete(engine, args.install_id)
    except Exception as exc:  # noqa: BLE001
        print(f"error: 删除失败：{exc}", file=sys.stderr)
        return 1
    print(f"[telemetry-delete] 已删除 主包 {deleted['packets']}、packet receipts "
          f"{deleted['packet_receipts']}、event receipts {deleted['event_receipts']}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
