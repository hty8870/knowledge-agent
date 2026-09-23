# -*- coding: utf-8 -*-
"""通用 SQLite 持久 job 镜像（默认部署形态下长任务状态的落盘真源）。

定位
----
语料同步（corpus-sync）等长任务的状态原先只活在进程内存里：进程一重启，「running」
就凭空消失，用户看到状态回落到 idle，既不知道那次同步是否还在跑、也不知道它已被打断。
本模块是**默认路径（单进程、无 Redis）的持久化镜像**：内存快照仍是运行期的事实源，
每次状态跃迁同步写一行 SQLite，重启后可由调用方读回真实终态。

通用机制，不绑业务
------------------
表只有「一类任务一行」（``kind`` 主键）——因为消费侧本来就是**单飞 job** 语义
（同一 kind 任何时刻只有一个当前 job），不需要历史表/多行排队。新增长任务只需给出
自己的 ``kind`` 字符串复用本模块，不新增表、不新增特例分支；任务语义（状态取值集合、
result 形状、错误文案）全部留在调用方，本模块只管**如实存取**。

与未来 RQ 后端的分工
--------------------
一旦启用消息队列后端（``BIODATA_JOB_BACKEND=rq``），job 状态存 Redis hash，由 RQ 侧
负责跨进程读写，**不写本表**；本表只在默认的进程内线程路径上生效。两条路径互斥，
不做双写，避免出现「两个真源不一致」的第三种状态。

错误契约
--------
``JobStoreError(code, message)`` 与 AccountError / McpTokenError 同构；Web 端点侧翻成
HTTPException、MCP 侧翻成 ToolError、agent 侧取 ``.code`` 落 step.error_code。
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .runtime_paths import assert_runtime_path, instance_data_dir_for

#: 显式覆盖 DB 文件的 env（缺省 = 实例 userdata 层；与 accounts/sessions 的覆盖套路一致）。
JOBS_DB_ENV = "BIODATA_JOBS_DB"
#: 短连接下的写锁等待上限（毫秒）：状态端点与 job 线程并发写同一文件时短暂互让，
#: 超时即如实抛错，不做无界等待。
_BUSY_TIMEOUT_MS = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    kind        TEXT PRIMARY KEY,
    status      TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    result_json TEXT,
    error       TEXT,
    updated_at  TEXT NOT NULL
)
"""


class JobStoreError(Exception):
    """机器码 + 人读提示；接口层翻成 4xx/5xx，不泄漏内部细节。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def default_db_path(project_root: Path) -> Path:
    """job 库默认路径 = 实例 userdata 层（source/portable = project_root/.userdata，
    frozen 布局 = data_root/.userdata）；``BIODATA_JOBS_DB`` 显式覆盖优先（过
    runtime_paths 路径守卫，与账户库/会话库同一条纪律：运行时状态绝不落 database/）。"""
    override = os.environ.get(JOBS_DB_ENV, "").strip()
    if override:
        return assert_runtime_path(Path(override), error_cls=JobStoreError)
    return instance_data_dir_for(Path(project_root), ".userdata") / "jobs.sqlite"


def _utc_now() -> str:
    """ISO UTC 秒级时间戳，与 webapp._corpus_sync_now 同形同口径（同一批状态值由内存快照
    与镜像两处呈现，格式必须逐字节一致，否则前端时间显示会出现两种风格）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """短连接：连上 → 建表（幂等）→ 交还连接 → commit → close。job 状态写频率极低
    （一次任务两三次），不值得维护长连接与线程本地状态；短连接顺带天然规避跨线程共享。"""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        conn.execute(_SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_running(kind: str, started_at: str, *, db_path: Path) -> None:
    """UPSERT 该 kind 为 running：清空上一轮的 finished_at/result/error（与内存快照
    `_corpus_sync_job_start` 的重置语义一致，不留上一轮残留）。"""
    now = _utc_now()
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (kind, status, started_at, finished_at, result_json, error, updated_at)
            VALUES (?, 'running', ?, NULL, NULL, NULL, ?)
            ON CONFLICT(kind) DO UPDATE SET
                status      = 'running',
                started_at  = excluded.started_at,
                finished_at = NULL,
                result_json = NULL,
                error       = NULL,
                updated_at  = excluded.updated_at
            """,
            (kind, started_at, now),
        )


def record_terminal(
    kind: str,
    status: str,
    finished_at: str,
    result: "dict[str, Any] | None",
    error: "str | None",
    *,
    db_path: Path,
) -> None:
    """UPSERT 终态（调用方传 'done'/'failed'；取值集合沿用内存快照，前端契约零改动）。

    ``result`` 非空 → 紧凑 JSON 存 ``result_json``（与落盘的其他 JSON 通道同风格）；
    与整行覆盖的 UPSERT 不同处：**保留原 started_at**——终态记录不该抹掉「这轮从何时开始」，
    行已存在且 started_at 非空时不动，行不存在（没经过 running 直接终态）则为 NULL。
    """
    now = _utc_now()
    payload = None if result is None else json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO jobs (kind, status, started_at, finished_at, result_json, error, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?)
            ON CONFLICT(kind) DO UPDATE SET
                status      = excluded.status,
                started_at  = COALESCE(jobs.started_at, excluded.started_at),
                finished_at = excluded.finished_at,
                result_json = excluded.result_json,
                error       = excluded.error,
                updated_at  = excluded.updated_at
            """,
            (kind, status, finished_at, payload, error, now),
        )


def load(kind: str, *, db_path: Path) -> "dict[str, Any] | None":
    """读回该 kind 的镜像，形状与内存快照逐字段同形：
    ``{"status","started_at","finished_at","result","error"}``。

    无 DB 文件或无该行 → None（调用方据此回落到内存快照，无需区分两种空）。
    ``result_json`` 解析失败 → result=None 且**不抛**：坏行只该退化成「结果读不出来」，
    不该让状态端点整条 500；status/error 仍如实呈现。"""
    path = Path(db_path)
    if not path.exists():
        return None
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT status, started_at, finished_at, result_json, error FROM jobs WHERE kind = ?",
            (kind,),
        ).fetchone()
    if row is None:
        return None
    status, started_at, finished_at, result_json, error = row
    result: "dict[str, Any] | None" = None
    if result_json is not None:
        try:
            result = json.loads(result_json)
        except (ValueError, TypeError):
            result = None
    return {
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "result": result,
        "error": error,
    }


def reconcile_interrupted(kind: str, message: str, *, db_path: Path) -> bool:
    """进程重启后的对账：镜像若停在 running，说明上一进程没来得及写终态就被打断——
    如实翻成 failed 并附 ``message``（中文，说明是重启中断而非任务本身失败），返回是否翻转。

    复用既有状态值集合（failed）而不是新造 'interrupted'：前端 watchSyncJobState /
    sync_button 的契约对状态取值封闭，新增取值会静默走到未知分支。"""
    path = Path(db_path)
    if not path.exists():
        return False
    now = _utc_now()
    with _connect(path) as conn:
        cursor = conn.execute(
            """
            UPDATE jobs
               SET status = 'failed', finished_at = ?, error = ?, updated_at = ?
             WHERE kind = ? AND status = 'running'
            """,
            (now, message, now, kind),
        )
        return cursor.rowcount > 0


__all__ = [
    "JOBS_DB_ENV",
    "JobStoreError",
    "default_db_path",
    "record_running",
    "record_terminal",
    "load",
    "reconcile_interrupted",
]
