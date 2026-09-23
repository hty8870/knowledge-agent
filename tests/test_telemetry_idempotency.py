# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, select
from sqlalchemy.pool import StaticPool


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "services" / "telemetry-receiver"
sys.path.insert(0, str(SERVICE))

from telemetry_idempotency import (  # noqa: E402
    claim_new_events,
    claim_packet,
    complete_packet,
    ensure_tables,
    event_receipts,
    legacy_packet_id,
    packet_receipts,
)


def _engine():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    ensure_tables(engine)
    return engine


def test_packet_retry_returns_original_row_id():
    engine = _engine()
    with engine.begin() as conn:
        first = claim_packet(conn, packet_id="pkt-a", identity="profile-a")
        assert first.duplicate is False
        complete_packet(conn, packet_id="pkt-a", row_id=42)
    with engine.begin() as conn:
        again = claim_packet(conn, packet_id="pkt-a", identity="profile-a")
        assert again.duplicate is True and again.row_id == 42
        assert conn.execute(select(packet_receipts.c.packet_id)).all() == [("pkt-a",)]


def test_receipt_operational_indexes_are_created_and_idempotent():
    engine = _engine()
    ensure_tables(engine)  # 生产重启/迁移重复执行不得报错
    inspector = inspect(engine)
    packet_indexes = {row["name"]: row["column_names"] for row in inspector.get_indexes("ingest_packet_receipts")}
    event_indexes = {row["name"]: row["column_names"] for row in inspector.get_indexes("ingest_event_receipts")}
    assert packet_indexes["ix_ingest_packet_receipts_identity_received_at"] == ["identity", "received_at"]
    assert packet_indexes["ix_ingest_packet_receipts_row_id"] == ["row_id"]
    assert event_indexes["ix_ingest_event_receipts_kind_received_at"] == ["kind", "received_at"]
    assert event_indexes["ix_ingest_event_receipts_received_at"] == ["received_at"]
    assert event_indexes["ix_ingest_event_receipts_packet_id"] == ["packet_id"]


def test_overlapping_batches_keep_each_event_once():
    engine = _engine()
    u1 = {"event_id": "u1", "t": 1, "k": "search"}
    u2 = {"event_id": "u2", "t": 2, "k": "search"}
    u3 = {"event_id": "u3", "t": 3, "k": "search"}
    with engine.begin() as conn:
        assert claim_new_events(conn, packet_id="p1", identity="profile", kind="usage", events=[u1, u2]) == [u1, u2]
    with engine.begin() as conn:
        assert claim_new_events(conn, packet_id="p2", identity="profile", kind="usage", events=[u2, u3]) == [u3]
        assert len(conn.execute(select(event_receipts.c.event_key)).all()) == 3


def test_same_event_id_is_independent_across_profiles_and_kinds():
    engine = _engine()
    event = {"event_id": "same", "id": "same", "t": 1, "k": "search"}
    with engine.begin() as conn:
        assert claim_new_events(conn, packet_id="p1", identity="a", kind="usage", events=[event])
        assert claim_new_events(conn, packet_id="p2", identity="b", kind="usage", events=[event])
        assert claim_new_events(conn, packet_id="p3", identity="a", kind="benchfb", events=[event])


def test_legacy_event_dedup_does_not_depend_on_batch_position():
    engine = _engine()
    same = {"t": 123, "k": "search", "q": "x"}
    with engine.begin() as conn:
        assert claim_new_events(conn, packet_id="p1", identity="a", kind="usage", events=[same]) == [same]
    with engine.begin() as conn:
        assert claim_new_events(conn, packet_id="p2", identity="a", kind="usage", events=[{"t": 0}, same]) == [{"t": 0}]


def test_legacy_packet_id_ignores_export_timestamp_but_not_content():
    a = {"schema": "biodata-telemetry/1", "install_id": "x", "exported_at": "t1", "usage_events": [{"t": 1}]}
    b = {**a, "exported_at": "t2"}
    c = {**a, "usage_events": [{"t": 2}]}
    assert legacy_packet_id(a) == legacy_packet_id(b)
    assert legacy_packet_id(a) != legacy_packet_id(c)
