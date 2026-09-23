# -*- coding: utf-8 -*-
"""BioData Agent 遥测接收端服务。

契约口径：同目录 `README.md`（上传协议、schema 收紧与接收端行为）。
前端脱敏后经 POST /v1/ingest 上传 usage / benchfb 数据，本服务校验、限流
并原样落库（jsonb 原样存，查询/物化留给后续分析）。

新增能力（additive）：
- 顶层 schema 白名单加可选键 mcp_records（元素 dict 且带字符串 call_id，条数上限
  MAX_MCP_RECORDS，超限/缺 call_id → 422）；事件幂等扩展 kind="mcp"
  （event_key=sha256(identity|"mcp"|call_id)，见 telemetry_idempotency.py）。
- 新增 GET /v1/stats：使用独立的服务端 STATS_TOKEN，返回轻量统计
  （packets_total / events_total=usage+benchfb+mcp / last_24h_packets /
  db_size_bytes / oldest_packet_at），不扫 payload 大字段。

安全收紧：
- schema 收紧：顶层 extra="forbid"、app 子对象白名单、明细元素形状（对象/键数/
  字符串长度/嵌套深度）与每数组条数上限——未知字段/超限 → 422（前端现行 6 字段包
  逐位兼容，只拒多余字段）。
- 配额：全局每日字节上限 + 每 IP 每日字节上限 + 每 profile 每日包数
  上限（超限 → 429）；保留期由服务启动时和后台定时任务自动执行，宿主 cron 仅作双保险。
- 限流器：有界 LRU + 定期惰性清理，消除每次请求全表扫描的 O(N²) 面。

幂等与净化（additive）：
- mcp 事件幂等键改用 install_id（其余两类仍用 identity）：MCP 调用属整机安装而非
  前端匿名账户，同一安装切换账户重传同一 call_id 不再重复入库。
- 落库前净化（防御纵深，与客户端 usage_core.js telemetryStrip 同规则）：秘密键整键
  剔除、base_url 只留 host、自由文本值级遮蔽（手机号/证件号/邮箱）。客户端已净化，
  本层兜「漏网/旧版本客户端/直接调 API」；响应 additive 新键 `sanitized` 报告本包
  处理处数（仅计数，任何日志/响应都不带回原始值）。

（自适应上传阈值，additive）：
- /v1/ingest 200 响应新增 `server_hint: {pressure, batch_threshold, min_interval_ms}`：
  压力 = max(在途请求/IN_FLIGHT_CAP, 限流窗口内请求/RATE_LIMIT_MAX, 今日已收字节/
  DAILY_BYTES_BUDGET)，按离散档映射（<0.3 → 2/30s；0.3–0.7 → 5/120s；≥0.7 →
  20/300s）。前端据此动态调整批量阈值与上传节奏：服务器空闲时几乎实时、压力大时
  攒批保护接收端。老客户端不读该字段无影响；限流器新增窗口内尝试计数（含被拒）。

（意见反馈通道后端，additive）：
- /v1/ingest 顶层新增可选键 `feedback_records[]`（严格 pydantic 模型，extra="forbid"）：
  每条 {feedback_id, identity, ephemeral_pubkey, nonce, ciphertext, with_diag}，条数上限
  MAX_FEEDBACK_RECORDS。自由文本经**开发者公钥加密**传输：客户端 WebCrypto
  ECDH(P-256)+HKDF-SHA256+AES-256-GCM，本服务用 FEEDBACK_DECRYPT_KEY 环境变量里的
  P-256 私钥解密；解密后明文过值级遮蔽（含 API Key 形态，追加在既有遮蔽之后）再入库；
  幂等 event_key=sha256(identity|"feedback"|feedback_id)（telemetry_idempotency.py 扩展
  kind="feedback"）。未配置私钥而请求带 feedback_records → 422 明确错误；无
  feedback_records 的既有 usage/benchfb/mcp 路径行为完全不变。隐私口径：意见正文
  明文不落日志、不随响应回显，只以遮蔽后形态入库。

- 生产：PostgreSQL 16（DATABASE_URL），docker compose 两容器（receiver + db）。
- 测试：SQLite 内存库，同一套表结构与插入逻辑（SQLAlchemy 抽象方言差异）。

运行：uvicorn app:app --host 0.0.0.0 --port 8471（env 必须提供 INGEST_TOKEN / DATABASE_URL）。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Deque
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from starlette.concurrency import run_in_threadpool
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    delete,
    func,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import JSON

# 意见反馈解密（ECDH+HKDF+AES-256-GCM；依赖 cryptography）。
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from telemetry_idempotency import (
    claim_new_events,
    claim_packet,
    complete_packet,
    ensure_tables as ensure_dedup_tables,
    event_receipts,
    identity_of,
    legacy_packet_id,
    packet_receipts,
)

logger = logging.getLogger(__name__)

# --- 常量 ---
MAX_BODY_BYTES = 2 * 1024 * 1024   # 兼容已发布的 1.9MB 客户端；日配额仍是主容量闸
BODY_READ_TIMEOUT = 10.0           # body 读取超时（秒）：慢连接/悬挂 → 408
RATE_LIMIT_MAX = 300               # 每 IP 每分钟粗粒度防洪；共享 NAT 不再把全体压成 30
PROFILE_RATE_LIMIT_MAX = 30        # 每匿名 profile 每分钟 30 次（解析 body 后执行）
RATE_LIMIT_WINDOW = 60.0           # 窗口秒数
RATE_LIMIT_MAX_KEYS = 10_000       # 限流表 key 总数上限（有界，防无限膨胀）
RATE_LIMIT_PURGE_INTERVAL = 300.0  # 全量惰性清理周期（秒）；到点或表满才扫，非每请求
TELEMETRY_SCHEMA_VALUE = "biodata-telemetry/1"   # schema 字段唯一合法值，否则 422

# --- schema 收紧常量 ---
MAX_USAGE_EVENTS = 1500            # 与客户端 FIFO 上限同源，超限 → 422
MAX_BENCHFB_RECORDS = 60           # 与客户端 per-profile 上限同源，超限 → 422
MAX_MCP_RECORDS = 200              # mcp_records 每包条数上限（与 usage/benchfb 同风格的条数上限）
MAX_FEEDBACK_RECORDS = 20          # feedback_records 每包条数上限（与客户端 per-profile 队列上限同源）
MAX_DETAIL_KEYS = 100              # 单个明细元素键数上限
MAX_DETAIL_KEY_LEN = 64            # 明细内键名字符数上限
MAX_DETAIL_DEPTH = 20              # 明细嵌套深度上限
MAX_DETAIL_STR_LEN = MAX_BODY_BYTES  # 明细内单字符串字符数上限。与 body 上限同值：body 层先拦，
                                     # 此处显式声明单字段守卫，防未来 body 上限放松时失控

# --- 配额与保留常量 ---
DAILY_BYTES_BUDGET = 100 * 1024 * 1024  # 全局每日入库字节上限（近似，见 ingest 注释），超限 → 429
PER_IP_DAILY_BYTES = 20 * 1024 * 1024   # 每 IP 每日原始 body 字节上限（IP 仅以 HMAC 桶入库）
PER_INSTALL_DAILY_PACKETS = 500         # 兼容旧 env 名；实际按 identity_of(profile 优先) 计数
RETENTION_DAYS = 90                     # DB 与导出文件统一保留期
RETENTION_INTERVAL_SECONDS = 24 * 60 * 60
DEFAULT_EXPORT_DIR = "/data/export"

# --- 自适应上传阈值（server_hint）---
# 压力 = max(在途请求/并发上限, 限流窗口内请求/限流上限, 今日已收字节/日字节预算)，
# 前端据此动态调整批量阈值与上传节奏：空闲时几乎实时，压力大时攒批保护服务器。
IN_FLIGHT_CAP = 16                       # 在途请求数上限（轻量计数器的分母）
HINT_BANDS = (                           # (pressure 下界, batch_threshold, min_interval_ms)，降序匹配
    (0.7, 20, 300_000),
    (0.3, 5, 120_000),
    (0.0, 2, 30_000),
)

# --- 意见反馈加密协议参数（客户端 feedback_core.js 同源，改动必须两端同步）---
FEEDBACK_HKDF_SALT = b"biodata-feedback-v1"     # HKDF salt（固定串，非秘密）
FEEDBACK_HKDF_INFO = b"biodata-feedback/1"      # HKDF info（协议版本）
FEEDBACK_AES_KEY_LEN = 32                       # AES-256-GCM 密钥字节数

# --- 表结构（ingest_packets + install_id/received_at 索引）---
metadata = MetaData()

ingest_packets = Table(
    "ingest_packets",
    metadata,
    # 生产 BIGSERIAL；测试（SQLite）退化为 INTEGER 主键以便 lastrowid 取 id
    Column("id", BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True),
    Column("received_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
    Column("install_id", Text, nullable=False),
    Column("schema", Text, nullable=False),
    Column("ua", Text, nullable=True),
    Column("cache_generation", Text, nullable=True),
    Column("n_usage", Integer, nullable=False, server_default=text("0")),
    Column("n_benchfb", Integer, nullable=False, server_default=text("0")),
    Column("raw_bytes", BigInteger, nullable=False, server_default=text("0")),
    Column("payload", JSONB().with_variant(JSON(), "sqlite"), nullable=False),
)
Index("ix_ingest_packets_install_id", ingest_packets.c.install_id)
Index("ix_ingest_packets_received_at", ingest_packets.c.received_at)

# 按日滚动的小计数表：避免每次 ingest 对当天全部 JSONB 重新 CAST/SUM；IP 只保存
# HMAC(day|ip) 桶，不保存原始地址。单 worker 内仍用数据库行锁保证线程池并发下配额原子。
daily_usage = Table(
    "ingest_daily_usage",
    metadata,
    Column("day_utc", Text, primary_key=True),
    Column("scope", Text, primary_key=True),
    Column("bucket", Text, primary_key=True),
    Column("raw_bytes", BigInteger, nullable=False, server_default=text("0")),
    Column("packet_count", BigInteger, nullable=False, server_default=text("0")),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)

service_state = Table(
    "telemetry_service_state",
    metadata,
    Column("key", Text, primary_key=True),
    Column("value", Text, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class Settings:
    """运行配置，全部来自环境变量；不硬编码任何秘密。配额/保留/限流参数可 env 覆盖（便于部署期调参）。"""

    def __init__(
        self,
        ingest_token: str,
        database_url: str,
        stats_token: str = "",
        body_read_timeout: float = BODY_READ_TIMEOUT,
        daily_bytes_budget: int = DAILY_BYTES_BUDGET,
        per_ip_daily_bytes: int = PER_IP_DAILY_BYTES,
        per_install_daily_packets: int = PER_INSTALL_DAILY_PACKETS,
        retention_days: int = RETENTION_DAYS,
        retention_interval_seconds: float = RETENTION_INTERVAL_SECONDS,
        export_dir: str = DEFAULT_EXPORT_DIR,
        rate_limit_max: int = RATE_LIMIT_MAX,
        profile_rate_limit_max: int = PROFILE_RATE_LIMIT_MAX,
        rate_limit_window: float = RATE_LIMIT_WINDOW,
        allowed_origins: tuple[str, ...] = (),
        allowed_origin_regex: str = r"^http://(?:127\.0\.0\.1|localhost)(?::\d+)?$",
        feedback_decrypt_key: str | None = None,
        db_pool_size: int = 5,
        db_max_overflow: int = 5,
        db_pool_timeout: float = 10.0,
    ) -> None:
        self.ingest_token = ingest_token
        self.stats_token = stats_token
        self.database_url = database_url
        self.body_read_timeout = body_read_timeout
        self.daily_bytes_budget = daily_bytes_budget
        self.per_ip_daily_bytes = per_ip_daily_bytes
        self.per_install_daily_packets = per_install_daily_packets
        self.retention_days = retention_days
        self.retention_interval_seconds = retention_interval_seconds
        self.export_dir = export_dir
        self.rate_limit_max = rate_limit_max
        self.profile_rate_limit_max = profile_rate_limit_max
        self.rate_limit_window = rate_limit_window
        self.allowed_origins = allowed_origins
        self.allowed_origin_regex = allowed_origin_regex
        # 意见反馈解密 P-256 私钥（PEM 或 base64 DER）。不配置时 feedback_records
        # 返回 422 明确错误，usage/benchfb/mcp 路径完全不受影响。
        self.feedback_decrypt_key = feedback_decrypt_key
        self.db_pool_size = max(1, int(db_pool_size))
        self.db_max_overflow = max(0, int(db_max_overflow))
        self.db_pool_timeout = max(0.1, float(db_pool_timeout))

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("INGEST_TOKEN")
        stats_token = os.environ.get("STATS_TOKEN")
        url = os.environ.get("DATABASE_URL")
        if not token or not stats_token or not url:
            raise RuntimeError("缺少环境变量 INGEST_TOKEN、STATS_TOKEN 或 DATABASE_URL，拒绝启动")
        if hmac.compare_digest(token.encode("utf-8"), stats_token.encode("utf-8")):
            raise RuntimeError("STATS_TOKEN 必须与 INGEST_TOKEN 不同，拒绝启动")

        def _int(name: str, default: int) -> int:
            v = os.environ.get(name)
            return int(v) if v else default

        def _float(name: str, default: float) -> float:
            v = os.environ.get(name)
            return float(v) if v else default

        return cls(
            ingest_token=token,
            stats_token=stats_token,
            database_url=url,
            body_read_timeout=_float("BODY_READ_TIMEOUT", BODY_READ_TIMEOUT),
            daily_bytes_budget=_int("DAILY_BYTES_BUDGET", DAILY_BYTES_BUDGET),
            per_ip_daily_bytes=_int("PER_IP_DAILY_BYTES", PER_IP_DAILY_BYTES),
            per_install_daily_packets=_int("PER_INSTALL_DAILY_PACKETS", PER_INSTALL_DAILY_PACKETS),
            retention_days=_int("RETENTION_DAYS", RETENTION_DAYS),
            retention_interval_seconds=_float("RETENTION_INTERVAL_SECONDS", RETENTION_INTERVAL_SECONDS),
            export_dir=os.environ.get("EXPORT_DIR") or DEFAULT_EXPORT_DIR,
            rate_limit_max=_int("RATE_LIMIT_MAX", RATE_LIMIT_MAX),
            profile_rate_limit_max=_int("PROFILE_RATE_LIMIT_MAX", PROFILE_RATE_LIMIT_MAX),
            rate_limit_window=_float("RATE_LIMIT_WINDOW", RATE_LIMIT_WINDOW),
            allowed_origins=tuple(x.strip() for x in os.environ.get("ALLOWED_ORIGINS", "").split(",") if x.strip()),
            allowed_origin_regex=(os.environ.get("ALLOWED_ORIGIN_REGEX")
                or r"^http://(?:127\.0\.0\.1|localhost)(?::\d+)?$"),
            feedback_decrypt_key=os.environ.get("FEEDBACK_DECRYPT_KEY"),
            db_pool_size=_int("DB_POOL_SIZE", 5),
            db_max_overflow=_int("DB_MAX_OVERFLOW", 5),
            db_pool_timeout=_float("DB_POOL_TIMEOUT", 10.0),
        )


def build_engine(database_url: str, *, pool_size: int = 5, max_overflow: int = 5,
                 pool_timeout: float = 10.0) -> Engine:
    """按 URL 构造引擎；SQLite 内存库必须单连接复用（StaticPool），否则每次请求都是空库。"""
    kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        kwargs["poolclass"] = StaticPool
    else:
        kwargs.update({
            "pool_pre_ping": True,
            "pool_size": max(1, int(pool_size)),
            "max_overflow": max(0, int(max_overflow)),
            "pool_timeout": max(0.1, float(pool_timeout)),
        })
    return create_engine(database_url, **kwargs)


def _insert_do_nothing(conn: Connection, table: Table, values: dict[str, Any]) -> None:
    """按方言做幂等 INSERT；只用于有主键的小型服务自用状态表。"""
    dialect = conn.engine.dialect.name
    if dialect == "postgresql":
        conn.execute(pg_insert(table).values(**values).on_conflict_do_nothing())
    elif dialect == "sqlite":
        conn.execute(sqlite_insert(table).values(**values).on_conflict_do_nothing())
    else:
        try:
            conn.execute(table.insert().values(**values))
        except IntegrityError:
            pass


def ensure_schema(engine: Engine) -> None:
    """幂等迁移当前 receiver schema；旧库补 raw_bytes，随后创建计数/状态表。

    `create_all` 不会给既有表加列，因此 raw_bytes 必须显式 ALTER。历史行以 payload
    文本的数据库字节/字符长度近似回填；新行一律记录真实 HTTP body 字节数。
    """
    metadata.create_all(engine)
    columns = {c["name"] for c in inspect(engine).get_columns("ingest_packets")}
    if "raw_bytes" not in columns:
        with engine.begin() as conn:
            conn.execute(text(
                "ALTER TABLE ingest_packets ADD COLUMN raw_bytes BIGINT NOT NULL DEFAULT 0"
            ))
            if conn.engine.dialect.name == "postgresql":
                conn.execute(text(
                    "UPDATE ingest_packets SET raw_bytes = octet_length(payload::text) WHERE raw_bytes = 0"
                ))
            else:
                conn.execute(text(
                    "UPDATE ingest_packets SET raw_bytes = length(CAST(payload AS TEXT)) WHERE raw_bytes = 0"
                ))

    # 首次升级只为当天全局桶做历史回填；旧版本没有保存 IP，不能也不猜 IP 桶。
    day_start = _utc_day_start()
    day_key = day_start.date().isoformat()
    with engine.begin() as conn:
        used = int(conn.execute(
            select(func.coalesce(func.sum(ingest_packets.c.raw_bytes), 0)).where(
                ingest_packets.c.received_at >= day_start
            )
        ).scalar_one())
        count = int(conn.execute(
            select(func.count()).select_from(ingest_packets).where(
                ingest_packets.c.received_at >= day_start
            )
        ).scalar_one())
        _insert_do_nothing(conn, daily_usage, {
            "day_utc": day_key, "scope": "global", "bucket": "all",
            "raw_bytes": used, "packet_count": count,
        })


def backfill_legacy_packet_receipts(engine: Engine) -> int:
    """为幂等账本上线前已成功落库的主包补 receipt，避免导出 join 漏掉历史包。

    packet_id 优先沿用载荷值；更老的载荷按与接收路径相同的 legacy_packet_id 生成。
    冲突时保留已完成 receipt，不把两个旧重复包伪装成两个独立包。
    """
    repaired = 0
    with engine.begin() as conn:
        rows = conn.execute(
            select(
                ingest_packets.c.id,
                ingest_packets.c.received_at,
                ingest_packets.c.install_id,
                ingest_packets.c.payload,
            )
            .outerjoin(packet_receipts, packet_receipts.c.row_id == ingest_packets.c.id)
            .where(packet_receipts.c.row_id.is_(None))
            .order_by(ingest_packets.c.id)
        ).mappings().all()
        for row in rows:
            payload = row["payload"] if isinstance(row["payload"], dict) else {}
            packet_id = str(payload.get("packet_id") or legacy_packet_id(payload))
            identity = identity_of(
                profile_id=payload.get("profile_id"),
                client_id=payload.get("client_id"),
                install_id=str(payload.get("install_id") or row["install_id"] or ""),
            )
            inserted = _insert_do_nothing(conn, packet_receipts, {
                "packet_id": packet_id,
                "identity": identity,
                "row_id": int(row["id"]),
                "received_at": row["received_at"],
            })
            if inserted:
                repaired += 1
                continue
            result = conn.execute(
                update(packet_receipts)
                .where(packet_receipts.c.packet_id == packet_id, packet_receipts.c.row_id.is_(None))
                .values(row_id=int(row["id"]), identity=identity)
            )
            repaired += max(0, int(result.rowcount or 0))
    return repaired


# --- 上传包请求模型（body 形状 + schema 收紧）---
class AppInfo(BaseModel):
    """app 子对象字段白名单：只收前端固定的三个键，多余字段 → 422。"""

    model_config = ConfigDict(extra="forbid")

    cache_generation: str | None = Field(default=None, max_length=64)
    ua: str | None = Field(default=None, max_length=512)
    lang: str | None = Field(default=None, max_length=32)


def _check_detail_shape(elems: list[dict[str, Any]], field_name: str) -> list[dict[str, Any]]:
    """明细元素形状校验：元素必须为对象，键数/键名长度/字符串长度/嵌套深度有上限。

    刻意用「对象 + 尺寸上限」而非枚举键白名单：usage/benchfb 事件的键由打点侧演进
    （USAGE_KINDS / benchfb 轮次记录），枚举会频繁误伤合法打点；收紧为形状与尺寸上限
    即可阻断「原样存任意对象」的攻击面，又不拒现行 payload。
    """
    def walk(v: Any, depth: int, path: str) -> None:
        if isinstance(v, str):
            if len(v) > MAX_DETAIL_STR_LEN:
                raise ValueError(f"{path}: string too long")
        elif isinstance(v, dict):
            if depth + 1 > MAX_DETAIL_DEPTH:
                raise ValueError(f"{path}: nesting too deep")
            if len(v) > MAX_DETAIL_KEYS:
                raise ValueError(f"{path}: too many keys")
            for k, val in v.items():
                if len(k) > MAX_DETAIL_KEY_LEN:
                    raise ValueError(f"{path}.{k[:16]}…: key name too long")
                walk(val, depth + 1, f"{path}.{k}")
        elif isinstance(v, list):
            for i, val in enumerate(v):
                walk(val, depth + 1, f"{path}[{i}]")

    for i, e in enumerate(elems):
        if not isinstance(e, dict):
            raise ValueError(f"{field_name}[{i}]: must be an object")
        walk(e, 0, f"{field_name}[{i}]")
    return elems


# --- 意见反馈记录模型（严格 pydantic，顶层 extra="forbid"）---
# 自由文本经开发者公钥加密：ephemeral_pubkey = ECDH 临时公钥（P-256 未压缩点，
# base64，65 字节）、nonce = AES-256-GCM 随机 nonce（base64，12 字节）、
# ciphertext = 密文（base64）。明文形状 = {feedback_id, authorized_at, text, diag?}，
# 由客户端 feedback_core.js 定义（两端同源）。长度上限按「2000 字正文 + 诊断快照 +
# GCM tag」留足余量：base64 密文 ≈ 1.37×明文字节，正文 UTF-8 上限 6000 字节 → 8.3KB。
class FeedbackRecord(BaseModel):
    """feedback_records 元素严格模型：未知字段 / 超限 → 422（与顶层同收紧口径）。"""

    model_config = ConfigDict(extra="forbid")

    feedback_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    identity: str = Field(min_length=1, max_length=128)   # 沿用 profile/install 标识语义（客户端填充）
    ephemeral_pubkey: str = Field(min_length=24, max_length=128)  # base64 P-256 未压缩点
    nonce: str = Field(min_length=8, max_length=64)               # base64 12 字节 nonce
    ciphertext: str = Field(min_length=16, max_length=20000)      # base64 AES-GCM 密文
    with_diag: bool = False                                       # 是否附诊断信息


class DropByQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    usage: int = Field(default=0, ge=0, le=10_000_000)
    benchfb: int = Field(default=0, ge=0, le=10_000_000)
    storage_error: int = Field(default=0, ge=0, le=10_000_000)


class DropReport(BaseModel):
    """客户端队列丢弃增量；只报计数，不带被丢内容。"""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1, le=2_147_483_647)
    dropped_count: int = Field(ge=1, le=10_000_000)
    by_queue: DropByQueue

    @model_validator(mode="after")
    def _sum_matches(self) -> "DropReport":
        total = self.by_queue.usage + self.by_queue.benchfb + self.by_queue.storage_error
        if total != self.dropped_count:
            raise ValueError("drop_report.dropped_count must equal by_queue sum")
        return self


class IngestPayload(BaseModel):
    """顶层 extra="forbid"；schema 字段强校验；明细只收对象列表且带条数/形状上限。"""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    schema_name: str = Field(alias="schema", max_length=32)  # 必须等于 TELEMETRY_SCHEMA_VALUE
    contract_version: int = Field(default=1, ge=1, le=2)      # 旧包缺省 v1；新包显式 v2
    packet_id: str | None = Field(default=None, min_length=8, max_length=96, pattern=r"^[A-Za-z0-9._:-]+$")
    install_id: str = Field(min_length=1, max_length=128)    # 必填；用于索引与归因
    client_id: str | None = Field(default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    profile_id: str | None = Field(default=None, min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    exported_at: str | None = Field(default=None, max_length=40)  # 前端 ISO 时间串，宽收不解析
    prompt_version: str | None = Field(default=None, max_length=128)
    experiment_id: str | None = Field(default=None, max_length=128)
    experiment_arm: str | None = Field(default=None, max_length=128)
    propensity: float | None = Field(default=None, gt=0, le=1)
    training_consent: bool = False
    drop_report: DropReport | None = None
    app: AppInfo | None = None
    usage_events: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_USAGE_EVENTS)
    benchfb_records: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_BENCHFB_RECORDS)
    mcp_records: list[dict[str, Any]] | None = Field(default=None, max_length=MAX_MCP_RECORDS)
    feedback_records: list[FeedbackRecord] | None = Field(default=None, max_length=MAX_FEEDBACK_RECORDS)

    @field_validator("schema_name")
    @classmethod
    def _schema_value_must_match(cls, v: str) -> str:
        if v != TELEMETRY_SCHEMA_VALUE:
            raise ValueError("unsupported schema value")
        return v

    @field_validator("usage_events")
    @classmethod
    def _usage_events_shape(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if v is None:
            return v
        return _check_detail_shape(v, "usage_events")

    @field_validator("benchfb_records")
    @classmethod
    def _benchfb_records_shape(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        if v is None:
            return v
        return _check_detail_shape(v, "benchfb_records")

    @field_validator("mcp_records")
    @classmethod
    def _mcp_records_shape(cls, v: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
        """mcp_records 元素必须为 dict 且带字符串 call_id（幂等键），其余形状约束对齐 usage/benchfb。"""
        if v is None:
            return v
        for i, e in enumerate(v):
            if not isinstance(e, dict):
                raise ValueError(f"mcp_records[{i}]: must be an object")
            if not isinstance(e.get("call_id"), str) or not e["call_id"]:
                raise ValueError(f"mcp_records[{i}]: missing string call_id")
        return _check_detail_shape(v, "mcp_records")


class IpRateLimiter:
    """有界滑动窗口限流（消除每次请求全表清理的 O(N²) 面）。

    - 滑动窗口语义不变：每 key（IP）每窗口最多 N 次（deque 记录窗口内命中时间）。
    - key 总数有界（RATE_LIMIT_MAX_KEYS）：表满时先做一次惰性过期清理，仍满按 LRU
      驱逐最久未使用的 key（OrderedDict move_to_end / popitem(last=False)）。
    - 全表清理只在「周期到点」或「表满」时触发（摊销 O(1)）；每次 allow 只裁剪
      当前 key 自己的过期条目，不再遍历全表。
    """

    def __init__(
        self,
        max_requests: int = RATE_LIMIT_MAX,
        window_seconds: float = RATE_LIMIT_WINDOW,
        max_keys: int = RATE_LIMIT_MAX_KEYS,
        purge_interval: float = RATE_LIMIT_PURGE_INTERVAL,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._max_keys = max_keys
        self._purge_interval = purge_interval
        self._hits: OrderedDict[str, Deque[float]] = OrderedDict()
        self._last_purge = time.monotonic()
        # 窗口内请求尝试计数（含被拒），供 server_hint 压力计算；窗口滑动即归零。
        # 复用 _last_purge 的时钟读数（同一次 monotonic 调用），不额外消耗时钟 tick。
        self._window_start = self._last_purge
        self._window_attempts = 0

    def _trim(self, q: Deque[float], now: float) -> None:
        while q and now - q[0] > self._window:
            q.popleft()

    def _purge_expired(self, now: float) -> None:
        for k in list(self._hits):
            q = self._hits[k]
            self._trim(q, now)
            if not q:
                del self._hits[k]

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        # 窗口内尝试计数：包含被拒的请求——被拒同样是服务器压力；
        # 窗口滑动即归零，避免计数无限增长。
        if now - self._window_start >= self._window:
            self._window_start = now
            self._window_attempts = 0
        self._window_attempts += 1
        # 周期到点才全量惰性清理（低流量期限流表也不会无限膨胀）
        if now - self._last_purge >= self._purge_interval:
            self._purge_expired(now)
            self._last_purge = now
        q = self._hits.get(key)
        if q is None:
            # 新 key：表满时先做一次惰性清理，仍满则 LRU 驱逐最久未使用的 key
            if len(self._hits) >= self._max_keys:
                self._purge_expired(now)
            while len(self._hits) >= self._max_keys:
                self._hits.popitem(last=False)
            q = deque()
            self._hits[key] = q
        else:
            self._hits.move_to_end(key)
            self._trim(q, now)
        if len(q) >= self._max:
            return False
        q.append(now)
        return True

    @property
    def window_attempts(self) -> int:
        """当前限流窗口内的请求尝试数（含被拒，窗口滑动即归零）；供 server_hint 压力计算。"""
        return self._window_attempts


def _utc_day_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


async def _read_body(request: Request, timeout: float) -> bytes:
    """限量流式读取请求体（慢连接/大 Content-Length 悬挂 DoS 加固）。

    - Content-Length 头预检：声明 >2MiB 直接 413，**不等 body**（堵「报 9 亿字节只发 2 字节」的悬挂）；
      头缺失/非数字/谎报（声明小、实发大）由下面的流式累计兜底。
    - 流式累计：实际读满 2MiB 即 413，不信任 CL 与实发字节一致。
    - 超时：整段 body 读取超过 timeout 秒 → 408（慢连接/悬挂）。
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "payload_too_large", "max_body_bytes": MAX_BODY_BYTES},
                )
        except ValueError:
            pass  # 非数字 CL：交给流式累计兜底

    async def _drain() -> bytes:
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_BODY_BYTES:
                raise HTTPException(
                    status_code=413,
                    detail={"code": "payload_too_large", "max_body_bytes": MAX_BODY_BYTES},
                )
            chunks.append(chunk)
        return b"".join(chunks)

    try:
        return await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=408, detail="request body read timeout")


def _today_used_bytes(conn: Connection) -> int:
    """从 O(1) 日汇总表读取今日真实 HTTP body 字节数。"""
    value = conn.execute(
        select(daily_usage.c.raw_bytes).where(
            daily_usage.c.day_utc == _utc_day_start().date().isoformat(),
            daily_usage.c.scope == "global",
            daily_usage.c.bucket == "all",
        )
    ).scalar_one_or_none()
    return int(value or 0)


def _check_profile_daily_quota(conn: Connection, identity: str, packet_id: str,
                               day_start: datetime, per_profile_daily_packets: int) -> None:
    """每 profile 每日包数上限；当前 packet receipt 不计入既有数。"""
    used_packets = conn.execute(
        select(func.count()).where(
            packet_receipts.c.identity == identity,
            packet_receipts.c.received_at >= day_start,
            packet_receipts.c.packet_id != packet_id,
        )
    ).scalar_one()
    if used_packets >= per_profile_daily_packets:
        raise HTTPException(status_code=429, detail="daily packet budget exceeded for profile")


def _ip_daily_bucket(stats_token: str, day_key: str, client_ip: str) -> str:
    """IP 仅以管理密钥 HMAC 后的日桶落库，不保存可读地址。"""
    return hmac.new(
        stats_token.encode("utf-8"), f"{day_key}|{client_ip}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _locked_daily_row(conn: Connection, day_key: str, scope: str, bucket: str) -> dict[str, Any]:
    _insert_do_nothing(conn, daily_usage, {
        "day_utc": day_key, "scope": scope, "bucket": bucket,
        "raw_bytes": 0, "packet_count": 0,
    })
    row = conn.execute(
        select(daily_usage).where(
            daily_usage.c.day_utc == day_key,
            daily_usage.c.scope == scope,
            daily_usage.c.bucket == bucket,
        ).with_for_update()
    ).mappings().one()
    return dict(row)


def _reserve_daily_bytes(conn: Connection, *, raw_bytes: int, client_ip: str,
                         settings: Settings) -> int:
    """原子预留全局与 IP 日字节额度；随 ingest 主事务失败自动回滚。"""
    day_key = _utc_day_start().date().isoformat()
    ip_bucket = _ip_daily_bucket(settings.stats_token, day_key, client_ip)
    global_row = _locked_daily_row(conn, day_key, "global", "all")
    ip_row = _locked_daily_row(conn, day_key, "ip", ip_bucket)
    global_after = int(global_row["raw_bytes"]) + raw_bytes
    ip_after = int(ip_row["raw_bytes"]) + raw_bytes
    if global_after > settings.daily_bytes_budget:
        raise HTTPException(status_code=429, detail="daily ingest byte budget exceeded")
    if ip_after > settings.per_ip_daily_bytes:
        raise HTTPException(status_code=429, detail="daily ingest byte budget exceeded for IP")
    now = datetime.now(timezone.utc)
    for scope, bucket in (("global", "all"), ("ip", ip_bucket)):
        conn.execute(
            update(daily_usage).where(
                daily_usage.c.day_utc == day_key,
                daily_usage.c.scope == scope,
                daily_usage.c.bucket == bucket,
            ).values(
                raw_bytes=daily_usage.c.raw_bytes + raw_bytes,
                packet_count=daily_usage.c.packet_count + 1,
                updated_at=now,
            )
        )
    return global_after


class InFlightGauge:
    """在途请求数轻量计数器（server_hint 的分量之一）。

    只在 async 事件循环线程内同步增减（不在 await 之间读写），GIL 原子即可，无需锁；
    run_in_threadpool 的 worker 不触碰本计数器，跨线程也不存在竞态。
    """

    def __init__(self) -> None:
        self._value = 0

    def incr(self) -> None:
        self._value += 1

    def decr(self) -> None:
        self._value -= 1

    @property
    def value(self) -> int:
        return self._value


def _server_pressure_hint(in_flight: int, window_attempts: int, rate_limit_max: int,
                          daily_used_bytes: int, daily_bytes_budget: int) -> dict[str, Any]:
    """把三个现成信号取最大压成 pressure∈[0,1]，再按离散档映射出上传节奏建议。

    三个信号：在途请求数/并发上限、限流窗口内请求数/限流上限、今日已收字节/日字节预算。
    分母取不到（≤0）时该信号按 0 处理（空闲视为无压力）；pressure 钳到 [0,1] 并
    四舍五入到 4 位小数（前端只消费 batch_threshold / min_interval_ms 两个整数档位）。
    """
    ratios = [
        in_flight / IN_FLIGHT_CAP if IN_FLIGHT_CAP > 0 else 0.0,
        window_attempts / rate_limit_max if rate_limit_max > 0 else 0.0,
        daily_used_bytes / daily_bytes_budget if daily_bytes_budget > 0 else 0.0,
    ]
    pressure = max(0.0, min(1.0, max(ratios)))
    for lo, batch, interval in HINT_BANDS:
        if pressure >= lo:
            return {
                "pressure": round(pressure, 4),
                "batch_threshold": batch,
                "min_interval_ms": interval,
                "max_body_bytes": MAX_BODY_BYTES,
            }
    return {  # 理论不可达兜底
        "pressure": round(pressure, 4),
        "batch_threshold": 2,
        "min_interval_ms": 30_000,
        "max_body_bytes": MAX_BODY_BYTES,
    }


# --- 落库前净化（与客户端 usage_core.js telemetryStrip 同规则，逐条对齐）---
# 客户端在上传前已做同一套净化；本层是防御纵深——兜旧版本客户端、打点漂移、直接调 API。
# 纪律：只数处数，任何日志/响应都绝不带回被处理的原始值。
_SECRET_KEY_RE = re.compile(
    r"^(api[_-]?key|password|passwd|username|accountusername|account[_-]?(?:name|id)"
    r"|token|secret|authorization|cookie|email)$",
    re.IGNORECASE,
)
# 值级遮蔽三条（与客户端同模式同顺序）：前导边界用捕获组保留下文再拼回（对齐 JS 无
# lookbehind 写法），误伤代价可接受、漏伤代价不可接受。
_VALUE_MASK_RULES = (
    (re.compile(r"(^|[^\d])1[3-9]\d{9}(?=[^\d]|$)"), "[手机号]"),
    (re.compile(r"(^|[^\dXx])\d{17}[\dXx](?=[^\dXx]|$)"), "[证件号]"),
    (re.compile(r"(^|[^A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[邮箱]"),
)
# API Key 形态遮蔽**追加在既有值级遮蔽之后**（usage 既有遮蔽语义不动）。
# 覆盖 sk-…（OpenAI 系，含 sk-ant-… 长串）、AKIA…（AWS）、Bearer token、ghp_…（GitHub PAT）
# 四种常见泄漏形态；规则与客户端 usage_core.js 追加的 _MASK_PATTERNS 逐字同源。
_API_KEY_MASK_RULES = (
    (re.compile(r"(^|[^A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}(?=[^A-Za-z0-9_-]|$)"), "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])AKIA[0-9A-Z]{16}(?=[^A-Za-z0-9]|$)"), "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])(?:[Bb]earer)\s+[A-Za-z0-9._~+/=-]{20,}"), "[API Key]"),
    (re.compile(r"(^|[^A-Za-z0-9])ghp_[A-Za-z0-9]{36}(?=[^A-Za-z0-9]|$)"), "[API Key]"),
)


def _mask_string_value(text: str, counter: list[int]) -> str:
    for pattern, tag in _VALUE_MASK_RULES + _API_KEY_MASK_RULES:
        text, n = pattern.subn(lambda m: m.group(1) + tag, text)
        counter[0] += n
    return text


def _endpoint_host_only(url: Any) -> str:
    """base_url 只留 host（对齐客户端 telemetryHost：保留端口，非法/空 → ""）。"""
    s = str(url or "").strip()
    if not s:
        return ""
    try:
        return urlsplit(s).netloc or ""
    except ValueError:
        return ""


def _sanitize_value(value: Any, counter: list[int]) -> Any:
    """递归净化：秘密键整键剔除（计数）；base_url 值改写为 host（值变了才计数）；
    字符串过值级遮蔽（命中计数）；其余标量原样。"""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if isinstance(k, str) and _SECRET_KEY_RE.match(k):
                counter[0] += 1
                continue
            if k == "base_url":
                host = _endpoint_host_only(v)
                if host != v:
                    counter[0] += 1
                out[k] = host
                continue
            out[k] = _sanitize_value(v, counter)
        return out
    if isinstance(value, list):
        return [_sanitize_value(v, counter) for v in value]
    if isinstance(value, str):
        return _mask_string_value(value, counter)
    return value


# --- 意见反馈解密（ECDH(P-256) + HKDF-SHA256 + AES-256-GCM）---
# 协议与客户端 feedback_core.js 逐字段同源：ephemeral 公钥（base64 未压缩点 65 字节）
# 与服务器私钥 ECDH 得共享密钥 → HKDF(salt=b"biodata-feedback-v1", info=b"biodata-feedback/1")
# 派生 32 字节 AES-256-GCM 密钥 → 用 12 字节 nonce 解密。私钥解析支持 PEM（PKCS8/SEC1）
# 与 base64 DER（先 PKCS8 后 SEC1）；解析失败或解密失败都是「服务端不可处理」→ 422。
_feedback_private_key_cache: tuple[str, ec.EllipticCurvePrivateKey] | None = None


def _load_feedback_private_key(encoded: str) -> ec.EllipticCurvePrivateKey:
    """按 FEEDBACK_DECRYPT_KEY 原始串解析 P-256 私钥；结果缓存（进程内只解析一次）。"""
    global _feedback_private_key_cache
    if _feedback_private_key_cache is not None and _feedback_private_key_cache[0] == encoded:
        return _feedback_private_key_cache[1]
    raw = encoded.strip()
    key: ec.EllipticCurvePrivateKey | None = None
    try:
        if "-----BEGIN" in raw:
            key = serialization.load_pem_private_key(
                raw.encode("utf-8"), password=None, backend=None)  # type: ignore[arg-type]
        else:
            # base64 DER：load_der_private_key 自动识别 PKCS8 与 SEC1（传统 OpenSSL 格式）
            key = serialization.load_der_private_key(
                base64.b64decode(raw, validate=True), password=None, backend=None)  # type: ignore[arg-type]
    except Exception:
        key = None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise HTTPException(status_code=422, detail="feedback decrypt key invalid")
    _feedback_private_key_cache = (encoded, key)
    return key


def _decrypt_feedback(record: FeedbackRecord, settings: Settings) -> dict[str, Any]:
    """解密一条 feedback 记录 → 明文 dict；任何一步失败 → 422 明确错误（不落库、不回显明文）。"""
    if not settings.feedback_decrypt_key:
        raise HTTPException(
            status_code=422,
            detail="feedback decrypt key not configured (FEEDBACK_DECRYPT_KEY)",
        )
    try:
        private_key = _load_feedback_private_key(settings.feedback_decrypt_key)
        peer_pub_raw = base64.b64decode(record.ephemeral_pubkey, validate=True)
        peer_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_pub_raw)
        shared = private_key.exchange(ec.ECDH(), peer_pub)
        aes_key = HKDF(
            algorithm=hashes.SHA256(),
            length=FEEDBACK_AES_KEY_LEN,
            salt=FEEDBACK_HKDF_SALT,
            info=FEEDBACK_HKDF_INFO,
        ).derive(shared)
        plaintext = AESGCM(aes_key).decrypt(
            base64.b64decode(record.nonce, validate=True),
            base64.b64decode(record.ciphertext, validate=True),
            None,
        )
    except (InvalidTag, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="feedback decrypt failed") from exc
    try:
        return json.loads(plaintext.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=422, detail="feedback plaintext invalid") from exc


def _sanitize_feedback_plain(plain: Any, counter: list[int]) -> dict[str, Any]:
    """解密后明文的落库形态：整对象过 _sanitize_value（键级剔除 + 值级遮蔽，
    含 追加的 API Key 形态——第二层防御，客户端加密前已过同一套）。

    返回 {feedback_id, authorized_at, text, diag}；text 非字符串时按空串处理
    （明文形状异常不炸库，但文本不是秘密）。"""
    sanitized = _sanitize_value(plain if isinstance(plain, dict) else {}, counter)
    text = sanitized.get("text")
    return {
        "feedback_id": str(sanitized.get("feedback_id") or ""),
        "authorized_at": sanitized.get("authorized_at") if isinstance(sanitized.get("authorized_at"), str) else None,
        "text": text if isinstance(text, str) else "",
        "diag": sanitized.get("diag"),
    }


def _store_ingest_packet(engine: Engine, payload: IngestPayload, settings: Settings,
                         raw_bytes: int, client_ip: str) -> dict[str, Any]:
    """同步 DB 工作单元；HTTP async 路由用 run_in_threadpool 调，绝不阻塞事件循环。

    packet receipt、event receipt 与主包 insert 在同一事务：重试幂等；重叠 batch 只存首次事件。
    旧客户端缺 packet/event id 时用规范 JSON 摘要兼容去重。
    三类明细**先落库前净化再 claim/存包**（normalized/packet_id 摘要均为净化后
    形态，重试同包净化结果确定，幂等不受影响）；mcp 事件幂等键用 install_id（整机口径）。
    feedback 明细**先解密再净化**（解密失败 → 422，包整体不落库、明文不回显）；
    幂等键 = record.identity（客户端按 profile/install 标识语义填充）。
    """
    counter = [0]
    if payload.usage_events is not None:
        payload.usage_events = [_sanitize_value(e, counter) for e in payload.usage_events]
    if payload.benchfb_records is not None:
        payload.benchfb_records = [_sanitize_value(e, counter) for e in payload.benchfb_records]
    if payload.mcp_records is not None:
        payload.mcp_records = [_sanitize_value(e, counter) for e in payload.mcp_records]
    # 解密发生在净化计数阶段（事务外、不占连接）；无私钥/解密失败在此抛 422。
    feedback_decrypted: list[dict[str, Any]] = []
    if payload.feedback_records:
        for rec in payload.feedback_records:
            plain = _decrypt_feedback(rec, settings)
            fb = _sanitize_feedback_plain(plain, counter)
            fb["identity"] = rec.identity
            fb["with_diag"] = rec.with_diag
            feedback_decrypted.append(fb)
    sanitized_n = counter[0]
    if sanitized_n:
        logger.info("落库前净化：本包处理 %d 处（仅计数，不含值）", sanitized_n)

    normalized = payload.model_dump(by_alias=True, exclude_none=True)
    # 新增默认字段不得改变旧无 packet_id 客户端的幂等摘要；legacy 材料只取请求实际给出的键。
    legacy_material = payload.model_dump(by_alias=True, exclude_none=True, exclude_unset=True)
    packet_id = payload.packet_id or legacy_packet_id(legacy_material)
    identity = identity_of(
        profile_id=payload.profile_id, client_id=payload.client_id, install_id=payload.install_id,
    )
    with engine.begin() as conn:
        packet = claim_packet(conn, packet_id=packet_id, identity=identity)
        if packet.duplicate:
            return {
                "ok": True, "id": packet.row_id, "packet_id": packet_id,
                "duplicate": True, "accepted_usage": 0, "accepted_benchfb": 0, "accepted_mcp": 0,
                "accepted_feedback": 0,  # ：重复包本次零写入（与其它三类同口径）
                "sanitized": 0,  # 重复包本次零写入，处数归零（净化结果与首次一致）
                "daily_used_bytes": _today_used_bytes(conn),  # 重复包也跳过配额检查，压力提示仍如实
            }
        _check_profile_daily_quota(
            conn, identity, packet_id, _utc_day_start(), settings.per_install_daily_packets,
        )

        usage_events = claim_new_events(
            conn, packet_id=packet_id, identity=identity, kind="usage",
            events=list(payload.usage_events or []),
        )
        benchfb_records = claim_new_events(
            conn, packet_id=packet_id, identity=identity, kind="benchfb",
            events=list(payload.benchfb_records or []),
        )
        mcp_records = claim_new_events(
            # mcp 幂等键用 install_id 而非 identity——MCP 调用属整机安装，
            # 同一安装切换匿名账户（profile 轮换）重传同一 call_id 不再重复入库。
            conn, packet_id=packet_id, identity=payload.install_id, kind="mcp",
            events=list(payload.mcp_records or []),
        )
        feedback_records = []
        if feedback_decrypted:
            # feedback 幂等键 = 记录内 identity（客户端按 profile/install 语义填，
            # 空值兜底用包级 identity_of），同 identity 重传同一 feedback_id 只入库首次。
            for fb in feedback_decrypted:
                feedback_records.extend(claim_new_events(
                    conn, packet_id=packet_id, identity=str(fb.get("identity") or identity),
                    kind="feedback", events=[fb],
                ))
        normalized["packet_id"] = packet_id
        normalized["usage_events"] = usage_events
        normalized["benchfb_records"] = benchfb_records
        normalized["mcp_records"] = mcp_records
        normalized["feedback_records"] = feedback_records

        # 一个新 packet 若所有事件都已由重叠 batch 收过，不再写空主包；receipt 仍完成为 0。
        had_events = bool(payload.usage_events or payload.benchfb_records
                          or payload.mcp_records or payload.feedback_records)
        if had_events and not usage_events and not benchfb_records and not mcp_records and not feedback_records:
            complete_packet(conn, packet_id=packet_id, row_id=0)
            return {
                "ok": True, "id": None, "packet_id": packet_id,
                "duplicate": False, "accepted_usage": 0, "accepted_benchfb": 0, "accepted_mcp": 0,
                "accepted_feedback": 0,
                "sanitized": sanitized_n,
                "daily_used_bytes": _today_used_bytes(conn),
            }

        used_bytes = _reserve_daily_bytes(
            conn, raw_bytes=raw_bytes, client_ip=client_ip, settings=settings,
        )
        result = conn.execute(
            ingest_packets.insert().values(
                install_id=payload.install_id,
                schema=payload.schema_name,
                ua=(payload.app.ua if payload.app else None),
                cache_generation=(payload.app.cache_generation if payload.app else None),
                n_usage=len(usage_events),
                n_benchfb=len(benchfb_records),
                raw_bytes=raw_bytes,
                payload=normalized,
            )
        )
        row_id = int(result.inserted_primary_key[0])
        complete_packet(conn, packet_id=packet_id, row_id=row_id)
        return {
            "ok": True, "id": row_id, "packet_id": packet_id,
            "duplicate": False, "accepted_usage": len(usage_events),
            "accepted_benchfb": len(benchfb_records),
            "accepted_mcp": len(mcp_records),
            "accepted_feedback": len(feedback_records),
            "sanitized": sanitized_n,
            "daily_used_bytes": used_bytes,
        }


def _db_size_bytes(engine: Engine, database_url: str) -> int | None:
    """库体积字节数：PG 用 pg_database_size(current_database())，SQLite 文件用文件大小；
    内存库/未知方言返回 None（统计端点轻量，不扫大字段）。"""
    if database_url.startswith("postgresql"):
        with engine.connect() as conn:
            return int(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
    if database_url.startswith("sqlite") and ":memory:" not in database_url:
        path = database_url.removeprefix("sqlite:///")
        try:
            return os.path.getsize(path)
        except OSError:
            return None
    return None


def _prune_export_files(export_dir: str, cutoff: datetime) -> int:
    """删除导出目录中早于 cutoff 的普通文件；不跟随目录或文件 symlink。"""
    root = Path(export_dir)
    if not root.exists():
        return 0
    if not root.is_dir() or root.is_symlink():
        raise RuntimeError("EXPORT_DIR must be a real directory, not a symlink")
    deleted = 0
    cutoff_ts = cutoff.timestamp()
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]
        for name in files:
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                deleted += 1
    return deleted


def _write_service_state(conn: Connection, key: str, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    _insert_do_nothing(conn, service_state, {"key": key, "value": encoded})
    conn.execute(
        update(service_state).where(service_state.c.key == key).values(
            value=encoded, updated_at=datetime.now(timezone.utc)
        )
    )


def _read_service_state(engine: Engine, key: str) -> dict[str, Any] | None:
    with engine.connect() as conn:
        value = conn.execute(
            select(service_state.c.value).where(service_state.c.key == key)
        ).scalar_one_or_none()
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def count_expired(engine: Engine, days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    with engine.connect() as conn:
        return int(conn.execute(
            select(func.count()).select_from(ingest_packets).where(
                ingest_packets.c.received_at < cutoff
            )
        ).scalar_one())


def run_retention_once(engine: Engine, settings: Settings) -> dict[str, Any]:
    """执行一次 DB + 导出物统一保留策略，并持久化可由 /v1/stats 查看的一次结果。"""
    if settings.retention_days <= 0:
        raise ValueError("retention_days must be positive")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=settings.retention_days)
    with engine.begin() as conn:
        deleted_events = conn.execute(
            delete(event_receipts).where(event_receipts.c.received_at < cutoff)
        ).rowcount
        deleted_receipts = conn.execute(
            delete(packet_receipts).where(packet_receipts.c.received_at < cutoff)
        ).rowcount
        deleted_packets = conn.execute(
            delete(ingest_packets).where(ingest_packets.c.received_at < cutoff)
        ).rowcount
        conn.execute(delete(daily_usage).where(daily_usage.c.day_utc < cutoff.date().isoformat()))

    export_error = None
    deleted_exports = 0
    try:
        deleted_exports = _prune_export_files(settings.export_dir, cutoff)
    except Exception as exc:
        export_error = type(exc).__name__
        logger.exception("导出物保留清理失败（不回显路径/内容）：%s", export_error)

    summary = {
        "completed_at": now.isoformat(),
        "retention_days": settings.retention_days,
        "cutoff": cutoff.isoformat(),
        "deleted_packets": max(0, int(deleted_packets or 0)),
        "deleted_packet_receipts": max(0, int(deleted_receipts or 0)),
        "deleted_event_receipts": max(0, int(deleted_events or 0)),
        "deleted_export_files": deleted_exports,
        "export_cleanup_ok": export_error is None,
    }
    with engine.begin() as conn:
        _write_service_state(conn, "last_retention", summary)
    return summary


def create_app(settings: Settings) -> FastAPI:
    """组装应用；测试注入内存 SQLite + 测试 token 即可。"""
    engine = build_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
    )
    ensure_dedup_tables(engine)
    ensure_schema(engine)
    repaired_receipts = backfill_legacy_packet_receipts(engine)
    if repaired_receipts:
        logger.info("旧遥测包幂等账本迁移完成：repaired=%s", repaired_receipts)

    async def _retention_safely() -> None:
        try:
            summary = await run_in_threadpool(run_retention_once, engine, settings)
            logger.info(
                "保留清理完成：days=%s packets=%s exports=%s export_ok=%s",
                summary["retention_days"], summary["deleted_packets"],
                summary["deleted_export_files"], summary["export_cleanup_ok"],
            )
        except Exception:
            logger.exception("保留清理失败；接收服务继续运行，下一周期重试")

    async def _retention_loop() -> None:
        while True:
            await asyncio.sleep(settings.retention_interval_seconds)
            await _retention_safely()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启动即跑一次，确保部署/重启后无需等待 24h；随后每天执行。定时触发由部署方按需配置。
        await _retention_safely()
        task = asyncio.create_task(_retention_loop(), name="telemetry-retention")
        _app.state.retention_task = task
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # 关闭交互式文档与 OpenAPI 暴露——本服务只有两个自用端点，无需可发现性
    app = FastAPI(
        title="BioData Agent Telemetry Receiver", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )
    app.state.engine = engine

    # CORS：默认只接受 loopback Web UI；公网部署显式配置 HTTPS origins/regex，绝不 `*`。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_origin_regex=settings.allowed_origin_regex or None,
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Ingest-Token"],
        allow_credentials=False,
    )
    ip_limiter = IpRateLimiter(max_requests=settings.rate_limit_max, window_seconds=settings.rate_limit_window)
    profile_limiter = IpRateLimiter(max_requests=settings.profile_rate_limit_max, window_seconds=settings.rate_limit_window)
    gauge = InFlightGauge()

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        """健康检查（含 DB 连通检查）。"""
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception:
            raise HTTPException(status_code=503, detail="db unavailable")
        return {"ok": True}

    @app.post("/v1/ingest")
    async def ingest(request: Request) -> dict[str, Any]:
        # 在途计数：进入处理器即计入，finally 归还；错误路径同样递减。
        gauge.incr()
        try:
            return await _ingest_impl(request, settings, engine, ip_limiter, profile_limiter, gauge)
        finally:
            gauge.decr()

    async def _ingest_impl(request: Request, settings: Settings, engine: Engine,
                           ip_limiter: IpRateLimiter, profile_limiter: IpRateLimiter,
                           gauge: InFlightGauge) -> dict[str, Any]:
        # 1. token 校验（防君子不防小人，配限流兜底）→ 401
        # 常数时间比较防时序侧信道；encode 成 bytes 是因为 compare_digest 对非 ASCII str
        # 会抛错而 token 约定 ASCII，encode 保证任意输入都不炸。
        given_token = request.headers.get("x-ingest-token") or ""
        if not hmac.compare_digest(given_token.encode("utf-8"), settings.ingest_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid ingest token")
        # 1b. Content-Type 必须是 application/json→ 415；在读取 body 之前判定
        ctype = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(status_code=415, detail="Content-Type must be application/json")
        # 2. 每 IP 简单限流（进程内有界滑动窗口）→ 429
        client_ip = request.client.host if request.client else "unknown"
        if not ip_limiter.allow(client_ip):
            raise HTTPException(status_code=429, detail="IP rate limit exceeded", headers={"Retry-After": "60"})
        # 3. body 限量读取：CL 预检 413 / 流式累计 413 / 读取超时 408
        raw = await _read_body(request, settings.body_read_timeout)
        # 4. JSON 解析 + schema 字段校验（缺/型错/值错/未知字段 → 422；深嵌套 RecursionError 一并兜住）
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):
            raise HTTPException(status_code=422, detail="invalid JSON body")
        try:
            payload = IngestPayload.model_validate(obj)
        except (ValidationError, RecursionError) as exc:
            # 422 detail 不回显输入：剔除 input/ctx（可能带用户原话/脱敏前原始值），只留位置与错误类型
            safe = [
                {k: v for k, v in e.items() if k not in ("input", "ctx")}
                for e in exc.errors()
            ]
            raise HTTPException(status_code=422, detail={"msg": "schema validation failed", "errors": safe})
        profile_key = identity_of(
            profile_id=payload.profile_id, client_id=payload.client_id, install_id=payload.install_id,
        )
        if not profile_limiter.allow(profile_key):
            raise HTTPException(status_code=429, detail="profile rate limit exceeded", headers={"Retry-After": "60"})
        # 4b/5. 配额 + packet/event 幂等 + 落库全部移出事件循环；慢 DB 不再卡住其它 body 读取。
        try:
            stored = await run_in_threadpool(
                _store_ingest_packet, engine, payload, settings, len(raw), client_ip
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail="db insert failed") from exc
        # 200 响应带 server_hint（additive，老客户端不读该字段无影响）——
        # 在途计数含本次请求（finally 归还前读取）；限流窗口尝试数含本次 allow()。
        stored["server_hint"] = _server_pressure_hint(
            in_flight=gauge.value,
            window_attempts=ip_limiter.window_attempts,
            rate_limit_max=settings.rate_limit_max,
            daily_used_bytes=int(stored.get("daily_used_bytes") or 0),
            daily_bytes_budget=settings.daily_bytes_budget,
        )
        return stored

    @app.get("/v1/stats")
    def stats(request: Request) -> dict[str, Any]:
        """管理统计摘要：使用只在服务器环境中的独立 STATS_TOKEN；
        全部轻量聚合，不扫 payload 大字段。

        - packets_total：主包数；events_total：event receipts 中 usage+benchfb+mcp+feedback
          四类事件去重总数。
        - last_24h_packets：24h 内主包数；oldest_packet_at：最早主包 received_at。
        - db_size_bytes：PG 用 pg_database_size；SQLite 文件用文件大小；内存库返回 null。
        """
        given_token = request.headers.get("x-stats-token") or ""
        if not settings.stats_token:
            raise HTTPException(status_code=503, detail="stats token not configured")
        if not hmac.compare_digest(given_token.encode("utf-8"), settings.stats_token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="invalid stats token")
        since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
        with engine.connect() as conn:
            packets_total = conn.execute(
                select(func.count()).select_from(ingest_packets)
            ).scalar_one()
            events_total = conn.execute(
                select(func.count()).select_from(event_receipts).where(
                    event_receipts.c.kind.in_(("usage", "benchfb", "mcp", "feedback"))
                )
            ).scalar_one()
            last_24h_packets = conn.execute(
                select(func.count()).select_from(ingest_packets).where(
                    ingest_packets.c.received_at >= since_24h
                )
            ).scalar_one()
            oldest_packet_at = conn.execute(
                select(func.min(ingest_packets.c.received_at))
            ).scalar_one()
        retention = _read_service_state(engine, "last_retention")
        return {
            "ok": True,
            "packets_total": int(packets_total),
            "events_total": int(events_total),
            "last_24h_packets": int(last_24h_packets),
            "db_size_bytes": _db_size_bytes(engine, settings.database_url),
            "oldest_packet_at": oldest_packet_at.isoformat() if oldest_packet_at is not None else None,
            "retention": retention,
        }

    return app


# uvicorn 入口；env 缺 INGEST_TOKEN / STATS_TOKEN / DATABASE_URL 时启动即失败（fail-fast）
app = create_app(Settings.from_env())
