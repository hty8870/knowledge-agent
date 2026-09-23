# -*- coding: utf-8 -*-
"""遥测 packet/event 幂等账本（PostgreSQL + SQLite 测试双方言）。

主包仍落 ``ingest_packets``；本模块只负责在同一数据库事务内：
1) claim packet_id（重复重试直接拿原 row_id）；
2) claim identity+kind+event_id（重叠 batch 只保留首次事件）。identity 由调用方定：
   usage/benchfb 用 ``identity_of``（profile/client/install 匿名账户口径）；**mcp 用
   install_id**（——MCP 调用属整机安装，同一安装切换匿名账户重传同一
   call_id 不再重复入库）；**feedback 用记录内 identity 字段**（——客户端按
   profile/install 标识语义填充，见 app.py）；
3) 主包插入后把 row_id 回填 receipt。

两张新表由 ``create_all`` additive 创建，不需要改已有 ingest_packets 表，现有生产库可原位升级。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Iterable

from sqlalchemy import BigInteger, Column, DateTime, Index, MetaData, Table, Text, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine


dedup_metadata = MetaData()

packet_receipts = Table(
    "ingest_packet_receipts",
    dedup_metadata,
    Column("packet_id", Text, primary_key=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("identity", Text, nullable=False),
    Column("row_id", BigInteger, nullable=True),
)

event_receipts = Table(
    "ingest_event_receipts",
    dedup_metadata,
    Column("event_key", Text, primary_key=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("packet_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
)

# 常用运维/清理路径：profile 日配额按 identity+received_at 查；保留清理按 kind/time 扫。
# 显式命名便于已有生产表原位补建，不依赖 ORM 自动生成名。
IX_PACKET_IDENTITY_RECEIVED = Index(
    "ix_ingest_packet_receipts_identity_received_at",
    packet_receipts.c.identity, packet_receipts.c.received_at,
)
IX_PACKET_ROW_ID = Index(
    "ix_ingest_packet_receipts_row_id",
    packet_receipts.c.row_id,
)
IX_EVENT_KIND_RECEIVED = Index(
    "ix_ingest_event_receipts_kind_received_at",
    event_receipts.c.kind, event_receipts.c.received_at,
)
IX_EVENT_RECEIVED = Index(
    "ix_ingest_event_receipts_received_at",
    event_receipts.c.received_at,
)
IX_EVENT_PACKET = Index(
    "ix_ingest_event_receipts_packet_id",
    event_receipts.c.packet_id,
)


@dataclass(frozen=True)
class PacketClaim:
    duplicate: bool
    row_id: int | None = None


def ensure_tables(engine: Engine) -> None:
    dedup_metadata.create_all(engine)
    # create_all 遇到已存在表时不会保证新 Index 一定补齐；逐索引 checkfirst 才能迁移生产存量库。
    IX_PACKET_IDENTITY_RECEIVED.create(bind=engine, checkfirst=True)
    IX_PACKET_ROW_ID.create(bind=engine, checkfirst=True)
    IX_EVENT_KIND_RECEIVED.create(bind=engine, checkfirst=True)
    IX_EVENT_RECEIVED.create(bind=engine, checkfirst=True)
    IX_EVENT_PACKET.create(bind=engine, checkfirst=True)


def identity_of(*, profile_id: str | None, client_id: str | None, install_id: str) -> str:
    return str(profile_id or client_id or install_id)


def legacy_packet_id(obj: dict[str, Any]) -> str:
    """旧客户端无 packet_id：排除每次变化的 exported_at，对同一快照给稳定摘要。"""
    stable = {k: v for k, v in obj.items() if k != "exported_at"}
    blob = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "legacy-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _insert_ignore(conn: Connection, table: Table, values: dict[str, Any]) -> bool:
    dialect = conn.dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(table).values(**values).on_conflict_do_nothing(index_elements=[next(iter(table.primary_key.columns))])
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(**values).on_conflict_do_nothing(index_elements=[next(iter(table.primary_key.columns))])
    else:  # 生产/测试只支持上述两种；其它方言 fail-closed，避免无幂等静默运行。
        raise RuntimeError(f"unsupported telemetry dedup dialect: {dialect}")
    result = conn.execute(stmt)
    return bool(result.rowcount)


def claim_packet(conn: Connection, *, packet_id: str, identity: str) -> PacketClaim:
    inserted = _insert_ignore(conn, packet_receipts, {"packet_id": packet_id, "identity": identity})
    if inserted:
        return PacketClaim(duplicate=False)
    row_id = conn.execute(
        select(packet_receipts.c.row_id).where(packet_receipts.c.packet_id == packet_id)
    ).scalar_one_or_none()
    return PacketClaim(duplicate=True, row_id=int(row_id) if row_id is not None else None)


def complete_packet(conn: Connection, *, packet_id: str, row_id: int) -> None:
    conn.execute(update(packet_receipts).where(packet_receipts.c.packet_id == packet_id).values(row_id=int(row_id)))


def _event_id(event: dict[str, Any], *, kind: str, index: int) -> str:
    """每种 kind 的稳定事件 id：usage→event_id、benchfb→id、mcp→call_id、
    feedback→feedback_id；缺省时退化为规范 JSON 摘要
    （不含 batch 位置，重叠窗口中同一事件换位置仍可去重）。"""
    if kind == "usage":
        explicit = event.get("event_id")
    elif kind == "benchfb":
        explicit = event.get("id")
    elif kind == "mcp":
        explicit = event.get("call_id")
    elif kind == "feedback":
        explicit = event.get("feedback_id")
    else:
        explicit = None
    if explicit:
        return str(explicit)
    blob = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    _ = index  # 旧签名保留；摘要不含 batch 位置，重叠窗口中同一事件换位置仍可去重。
    return "legacy-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def claim_new_events(
    conn: Connection,
    *,
    packet_id: str,
    identity: str,
    kind: str,
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """返回本事务首次见到的事件；重叠 batch 已见事件不再进入 payload/计数。

    event_key = sha256(identity|kind|event_id)：usage/benchfb 的 identity 是匿名账户
    （identity_of），mcp 由调用方传 install_id（防账户切换重复入库），
    feedback 由调用方传记录内 identity（客户端按 profile/install 标识语义填）。"""
    out: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        eid = _event_id(event, kind=kind, index=index)
        digest = hashlib.sha256(f"{identity}|{kind}|{eid}".encode("utf-8")).hexdigest()
        if _insert_ignore(conn, event_receipts, {
            "event_key": digest, "packet_id": packet_id, "kind": kind,
        }):
            out.append(event)
    return out
