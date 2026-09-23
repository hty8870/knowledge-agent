# -*- coding: utf-8 -*-
"""RQ 消息队列后端 —— corpus-sync 长任务的 opt-in 跨进程执行通道。

定位：默认形态是**进程内后台线程单飞 job**（`webapp._corpus_sync_job_*`，一行不动）；
只有 env `BIODATA_JOB_BACKEND=rq` 时才走到本模块。因此：

- **顶层不 import rq / redis**：最小安装（默认 `requirements.txt`）没有 rq 包，顶层引入会让
  整个应用起不来。`start()` 里才 `import rq`，缺包抛 `JobBackendError("job_backend_unavailable")`。
- **连接复用 `redis_store`**：本模块不解析 URL、不建连接池，一切走 `redis_store.get_client()`
  （唯一建连真源）。
- **状态形状与内存快照逐键一致**：对外始终是 `{"status","started_at","finished_at","result","error"}`，
  status 取值集合 `idle/running/done/failed` 与内存路径完全相同——前端 watchSyncJobState /
  sync_button 的契约对状态取值封闭，新增取值会静默走到未知分支。

跨进程缓存失效（本模块存在的核心理由）
--------------------------------------
RQ worker 跑在**独立进程**：worker 里调 `invalidate_external_cache()` /
`recall_api.invalidate_vectors()` 只能清掉 worker 自己的内存，对 web 进程无效。唯一通道是
worker 在状态 hash 里置 `needs_web_invalidate="1"`；web 进程每次读状态时调
`consume_web_invalidate_flag()`，读到旗标才在 **web 进程内**执行一次失效并清旗。
失效动作本身留在 webapp（它才持有那些缓存），本模块只负责如实记账旗标。

错误契约
--------
`JobBackendError(code, message)` 与 `AccountError` / `JobStoreError` 同构。与内存路径的
「快照永远读得出来」不同，RQ 路径下状态真源在 Redis：读不到就**如实 503**，不静默伪造
一个 idle（用户会误以为任务从没跑过）。翻译职责在接口层——Web 端点把 `JobBackendError`
翻成 HTTPException 503（响应体用 `message`，`code` 可进 `X-Error-Code`）。

连接双通道（RQ 二进制约束）
--------------------------
RQ 2.x 要求 job 数据以**二进制**写入 Redis hash（`Job.to_dict` 对序列化结果做 zlib 压缩），
decode_responses=True 的连接读它会抛 UnicodeDecodeError（真实 worker 取不到任务、job 停在
QUEUED）。因此：队列/Worker 走 `redis_store.get_rq_client()`（bytes 模式），本模块自己的
状态 hash（纯文本 JSON）仍走 `redis_store.get_client()`（decode 模式）。两个单例共用同一
URL/超时真源（见 redis_store 模块 docstring「两种解码模式」）。
"""
from __future__ import annotations

import datetime
import json
import os
import time
from typing import Any

from . import redis_store

#: 状态 hash（字段名与内存快照五键同名，另加两个 RQ 侧运维字段）。
_STATUS_KEY = "biodata:job:corpus_sync"
#: 队列名。web 进程入队、worker 进程消费，两端都取这里，不重复字面量。
_QUEUE_NAME = "biodata"

#: 后端开关。空 = 默认形态（进程内线程）；"rq" = 本模块；其他非空值 = 配置笔误，fail-closed。
_BACKEND_ENV = "BIODATA_JOB_BACKEND"
_BACKEND_VALUE = "rq"

#: worker 侧执行体路径（RQ 用字符串引用跨进程导入；写成常量避免 web/worker 两侧各写一份）。
_ENTRY_PATH = "dataset_recommender.app.rq_jobs.corpus_sync_entry"

#: worker 置位、web 进程消费后清零的跨进程失效旗标。
_FLAG_FIELD = "needs_web_invalidate"
#: RQ job id（运维线索，不对外暴露，不进快照形状）。
_JOB_ID_FIELD = "rq_job_id"

_STORE_UNAVAILABLE_MSG = "任务状态存储暂时不可用，请稍后重试。"


class JobBackendError(Exception):
    """机器码 + 人读提示；接口层翻成 4xx/5xx，不泄漏内部细节。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def backend_active() -> bool:
    """env `BIODATA_JOB_BACKEND` 是否点亮 RQ 后端。

    空（未设 / 全空白）→ False（默认形态）；"rq" → True；其他非空值 → 抛
    `JobBackendError("bad_config")`——拼错开关名时静默回落到内存后端会让「我明明开了
    消息队列」变成一个查不出来的错觉，宁可在启动路径上直接报错。
    """
    value = (os.environ.get(_BACKEND_ENV, "") or "").strip().lower()
    if not value:
        return False
    if value == _BACKEND_VALUE:
        return True
    raise JobBackendError(
        "bad_config",
        f"环境变量 {_BACKEND_ENV} 取值无法识别（当前 {value!r}）；"
        f"留空走默认形态，或设为 {_BACKEND_VALUE!r} 启用 RQ 消息队列后端。",
    )


def _now() -> str:
    """ISO UTC 秒级时间戳，与 `webapp._corpus_sync_now` / `durable_jobs._utc_now` 同形同口径
    （同一批状态值可能由内存快照、SQLite 镜像、Redis 三处呈现，格式必须逐字节一致）。"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _client() -> "Any":
    """共享 Redis 客户端（decode 模式，供状态 hash 读写）；缺包/建连失败翻成
    `JobBackendError`（code 原样透传）。"""
    try:
        return redis_store.get_client()
    except redis_store.RedisUnavailable as exc:
        raise JobBackendError(exc.code, exc.message) from exc


def _rq_client() -> "Any":
    """RQ 队列专用 bytes 模式客户端（RQ job 数据是 zlib 二进制，decode 模式读不了）。
    缺包/建连失败翻成 `JobBackendError`（code 原样透传）。"""
    try:
        return redis_store.get_rq_client()
    except redis_store.RedisUnavailable as exc:
        raise JobBackendError(exc.code, exc.message) from exc


def _as_text(value: "Any") -> str:
    """Redis 值 → str。`redis_store` 当前按 `decode_responses=True` 建连，这里仍兼容 bytes，
    避免连接模式变化时状态端点整体崩掉。"""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "replace")
    return str(value)


def _read_hash() -> "dict[str, str]":
    try:
        raw = _client().hgetall(_STATUS_KEY)
    except (redis_store.RedisUnavailable, *redis_store.redis_error_types()) as exc:
        raise JobBackendError("job_store_unavailable", _STORE_UNAVAILABLE_MSG) from exc
    return {_as_text(k): _as_text(v) for k, v in (raw or {}).items()}


def _write_hash(mapping: "dict[str, str]") -> None:
    """写状态字段（只覆盖给定字段，不整行替换——终态写不该抹掉 started_at）。失败抛错。"""
    try:
        _client().hset(_STATUS_KEY, mapping=mapping)
    except (redis_store.RedisUnavailable, *redis_store.redis_error_types()) as exc:
        raise JobBackendError("job_store_unavailable", _STORE_UNAVAILABLE_MSG) from exc


def _write_hash_quiet(mapping: "dict[str, str]") -> None:
    """尽力记账：失败静默吞掉。只用于「已经发生了更重要的错误」的路径（入队失败后的终态、
    以及 worker 进程内的状态写入）——在那些地方再抛一次只会掩盖原始故障，或让 RQ 把一个
    已经如实记账的 job 又标成 failed。"""
    try:
        _client().hset(_STATUS_KEY, mapping=mapping)
    except (redis_store.RedisUnavailable, *redis_store.redis_error_types()):
        pass


def _parse_result(raw: str) -> "Any":
    """result 以紧凑 JSON 存 hash（Redis 无嵌套结构）；空串/坏 JSON → None（坏行只该退化成
    「结果读不出来」，不该让状态端点整条 500）。"""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def snapshot() -> dict:
    """读状态快照，形状与 `webapp._corpus_sync_job_snapshot()` 逐键一致。

    空 hash（从未跑过）→ idle 形状（五个键全在，值为 None，与内存 idle 同形同值）。
    读失败 → `JobBackendError("job_store_unavailable")`：RQ 路径下状态真源就在 Redis，
    读不到只能如实报错，伪造 idle 会让用户以为任务不存在。
    """
    data = _read_hash()
    if not data.get("status"):
        return {"status": "idle", "started_at": None, "finished_at": None,
                "result": None, "error": None}
    return {
        "status": data["status"],
        "started_at": data.get("started_at") or None,
        "finished_at": data.get("finished_at") or None,
        "result": _parse_result(data.get("result", "")),
        "error": data.get("error") or None,
    }


def start(sources: "list[str] | None") -> dict:
    """启动或附着 corpus-sync job，返回最新快照（与内存路径单飞语义一致）。

    running 中重复触发 → 不新建、不抛 busy，直接返回现状（调用方看到的永远是同一个 job）。

    入队顺序：**先落 running 再 enqueue**。反过来写会把竞态变成 bug——worker 可能瞬间取走
    任务并写终态，随后 web 的 running 写入会把 done 覆盖成 running，状态永远停在 running。
    """
    snap = snapshot()
    if snap["status"] == "running":
        return snap

    try:
        import rq  # 惰性：顶层不加 rq 依赖边（最小安装没有 rq 包）
    except ImportError as exc:
        raise JobBackendError(
            "job_backend_unavailable",
            "未安装 RQ 消息队列依赖包，无法启用消息队列后端；请先执行 "
            "pip install -r requirements/requirements-mq.txt，"
            f"或去掉 {_BACKEND_ENV} 环境变量以回到默认形态。",
        ) from exc

    job_id = f"corpus-sync-{int(time.time())}"
    _write_hash({
        "status": "running",
        "started_at": _now(),
        "finished_at": "",
        "result": "",
        "error": "",
        _FLAG_FIELD: "0",
        _JOB_ID_FIELD: "",
    })
    # bytes 客户端在 try 外解析：缺包/建连失败要保留更精确的 code（redis_package_missing 等），
    # 不能被下面的入队兜底统一改写成 job_enqueue_failed。
    client = _rq_client()
    try:
        queue = rq.Queue(_QUEUE_NAME, connection=client)
        job = queue.enqueue(_ENTRY_PATH, sources, job_id=job_id)
    except Exception as exc:  # 入队失败：如实落 failed，别留一个永远 running 的幽灵状态
        hint = getattr(exc, "hint", "") or str(exc) or type(exc).__name__
        _write_hash_quiet({
            "status": "failed",
            "finished_at": _now(),
            "error": hint,
            _FLAG_FIELD: "1",
        })
        raise JobBackendError("job_enqueue_failed", f"任务入队失败：{hint}") from exc
    # rq_job_id 只是运维线索：写不进去不该让已经入队的任务报错给调用方。
    _write_hash_quiet({_JOB_ID_FIELD: job.id})
    return snapshot()


def corpus_sync_entry(sources: "list[str] | None") -> None:
    """worker 进程侧执行体：sync →（有新增）向量重建 → 写终态 + 置跨进程失效旗标。

    为什么 worker 不自己调 `invalidate_external_cache()`：那是 **web 进程**的内存缓存，
    worker 里调只清 worker 自己那份，等于没清。真正的失效由 web 进程读到
    `needs_web_invalidate="1"` 后在本进程内完成（见模块 docstring）。

    异常一律收口为 failed 状态，绝不漏出 worker：RQ 会把抛出的异常记进 failed registry，
    但用户看的是我们的状态端点——两处都标 failed 只会让「哪个才是真因」变模糊。
    """
    _write_hash_quiet({
        "status": "running",
        "started_at": _now(),
        "finished_at": "",
        "result": "",
        "error": "",
        _FLAG_FIELD: "0",
    })
    result: "Any" = None
    try:
        from . import webapp  # 惰性：worker 进程首次执行任务时才拉起 webapp 模块

        result = webapp._corpus_sync_execute(sources)
        vec_err = None
        if int(result.get("imported_total") or 0) > 0:
            vec_err = webapp._corpus_sync_rebuild_vectors()
        _write_hash_quiet({
            "status": "failed" if vec_err else "done",
            "finished_at": _now(),
            "result": "" if result is None else json.dumps(
                result, ensure_ascii=False, separators=(",", ":")),
            "error": vec_err or "",
            _FLAG_FIELD: "1",
        })
    except Exception as exc:  # 含 CurateError：如实把 hint 落进 error，供状态端点呈现
        hint = getattr(exc, "hint", "") or str(exc) or type(exc).__name__
        _write_hash_quiet({
            "status": "failed",
            "finished_at": _now(),
            "result": "" if result is None else json.dumps(
                result, ensure_ascii=False, separators=(",", ":")),
            "error": hint,
            _FLAG_FIELD: "1",
        })
    return None


def consume_web_invalidate_flag() -> bool:
    """读并清 `needs_web_invalidate` 旗标：为 "1" → 写 "0" 并返回 True，否则 False。

    调用方（web 进程）据此在本进程内执行一次外部库缓存失效 + 向量缓存失效。**读取失败
    fail-open 返回 False**：失效旗标是「锦上添花」的一次机会，读不到就留到下次轮询重试，
    不该让状态端点因为清旗失败再 503 一次（快照本身已经如实反映了存储状况）。
    """
    try:
        client = _client()
        flag = _as_text(client.hget(_STATUS_KEY, _FLAG_FIELD))
        if flag != "1":
            return False   # 无旗标时不做无谓写入，也不凭空创建 hash
        client.hset(_STATUS_KEY, _FLAG_FIELD, "0")
    except (redis_store.RedisUnavailable, *redis_store.redis_error_types()):
        return False
    return True


__all__ = [
    "JobBackendError",
    "backend_active",
    "snapshot",
    "start",
    "corpus_sync_entry",
    "consume_web_invalidate_flag",
]
