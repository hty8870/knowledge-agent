# -*- coding: utf-8 -*-
"""服务端真下载管理器：把一批数据集的文件**直接下到本机**（Web 后端进程内）。

## 与任务包 / executor 的分工

- `content/task_pack.py` 只产**文本任务包**（清单 + 脚本 + 引文），用户拿脚本自己跑；
- `corpus/download_executor.py` 是**单文件执行器**（`download_one` 流式 + md5 + .part 原子改名），
  本模块复用它逐文件执行，在它之上加三样它没有的东西：
  1. **job 注册表**：内存 {job_id: 状态 dict}，每 job 一个后台线程，同一时刻只允许一个 running job；
  2. **逐数据集子文件夹**：`<dir>/<safe_uid>__<标题前40字符>/`，直接解决「哪个文件属于哪个数据集」；
  3. **小白说明**：根目录随进度写 `README.txt` + 随完成追加 `manifest.tsv`。

## 来源覆盖（A 级，判定复用 download_plan 的四档，不另造一套）

- 10x 台账 774 条：`checksum_verifiable`（逐文件 md5）→ supported；
- CELLxGENE 2198 条：`size_only`（h5ad 直链 + filesize，`_synth_row` 合成行）→ supported；
- SCP/AE 有真直链且 filesize>0 的条目：`size_only` 合成行 → supported；
- 其余（GEO 等 page_only / direct_unsized / 语料查不到）：进 `unsupported` 并给中文 reason。
  **不新增任何网络抓取代码**：更多源的 API 接入不在本期范围。

## 安全红线（与 executor 同口径）

- 下载只放行 https + `build_plan` 派生的 `allowed_hosts` 白名单（单一真源是计划输出行本身）；
  生产下载走 executor 的 `_policy_opener`——**每一跳**重校验 scheme(https)+主机白名单+端口(443)
  +IP 解析闸（拒绝回环/私网/链路本地/保留/组播/云元数据 169.254.169.254）、限 3 跳、固定已校验
  IP 防 DNS rebinding；流式硬字节上限（声明×1.05 与全局 1 TiB 取小），超限中止并清理 `.part`。
- **用户上传记录代下开关**：env `BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED`
  默认关 = 允许（单机用户靠上传记录的直链直接下载）；置 1/true/yes/on 时，source 打标为
  「用户上传」的记录进 unsupported。该开关是策略便利，**不是安全边界**——真正的边界是上面的
  逐跳闸 + IP 闸 + 字节上限（对所有来源无条件生效）。
- 目标目录由本模块生成在 `Path.home()/Downloads/BioData数据-<时间戳>/`，不落仓库；
- 磁盘预检：`shutil.disk_usage` 剩余空间 < total_bytes×1.05 → 拒绝（与逐文件硬上限同系数）；
- 取消：`threading.Event`，chunk 间与文件间检查；取消后保留 `.part`（可续传语义）。

## 测试接缝

- `build_download_plan(uids, records=)`：`records` 可注入假记录（uid → 记录 dict），跳过语料装载；
- `start_job(..., out_dir=, opener=, sleep=)`：`out_dir` 落临时目录、`opener/sleep` 注入假网络，
  与 executor 的 opener 接缝同一纪律（禁真网）。不注入 opener 时走生产 `_policy_opener`——
  SSRF 传输层测试通过 monkeypatch `DE._resolve_host` / `DE._connect_pinned` 驱动（见
  tests/test_download_ssrf_guards.py）。
"""
from __future__ import annotations

import datetime
import os
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Sequence

from . import download_executor as DE
from . import download_plan as DP
from . import downloads, provenance

# ---------------------------------------------------------------- 状态词表

STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_CANCELLED = "cancelled"
STATE_ERROR = "error"
STATES = (STATE_RUNNING, STATE_DONE, STATE_CANCELLED, STATE_ERROR)

FILE_PENDING = "pending"
FILE_DOWNLOADING = "downloading"
FILE_OK = "ok"
FILE_SIZE_OK = "size_ok"
FILE_MD5_MISMATCH = "md5_mismatch"
FILE_ERROR = "error"
FILE_SKIPPED = "skipped"
FILE_CANCELLED = "cancelled"
FILE_STATUSES = (FILE_PENDING, FILE_DOWNLOADING, FILE_OK, FILE_SIZE_OK,
                 FILE_MD5_MISMATCH, FILE_ERROR, FILE_SKIPPED, FILE_CANCELLED)

MAX_JOBS = 20                 # 注册表最多保留的任务数（含已结束），超出驱逐最旧的
DISK_HEADROOM = 1.05          # 磁盘预检安全系数：需要 total×1.05 可用空间

#: executor 逐文件 verdict → 本模块文件状态。size_mismatch/unverified/unreachable/rejected
#: 一律收敛到 error 并带中文 error 文案（supported 档位下 unverified 理论上不会出现——
#: 10x 有 md5、CELLxGENE/SCP/AE 有 filesize，兜底映射保持诚实即可）。
_EXEC_TO_FILE = {
    DE.STATUS_OK: FILE_OK,
    DE.STATUS_SIZE_OK: FILE_SIZE_OK,
    DE.STATUS_MD5_MISMATCH: FILE_MD5_MISMATCH,
    DE.STATUS_SIZE_MISMATCH: FILE_ERROR,
    DE.STATUS_UNVERIFIED: FILE_ERROR,
    DE.STATUS_UNREACHABLE: FILE_ERROR,
    DE.STATUS_REJECTED: FILE_ERROR,
    DE.STATUS_SKIPPED_FLAGGED: FILE_SKIPPED,
}

#: 不支持档位的固定中文 reason（tier 词表在 download_plan，这里给「为什么本批下不了」的面向用户措辞）。
_UNSUPPORTED_ZH = {
    DP.TIER_PAGE: "该来源只有数据集页面地址，没有可核验的文件直链（如 GEO 需经官方页面解析）；"
                  "本批暂不支持直接下载，可改用任务包。",
    DP.TIER_DIRECT: "该来源给了一个未核验大小/校验和的直链，无法确认是完整文件；"
                    "本批暂不支持直接下载，可改用任务包。",
}

_MANIFEST_HEADER = ("dataset_uid\tdataset_title\tdir\tfilename\turl\texpected_bytes"
                    "\tdone_bytes\tstatus\terror\tmd5_actual")


class DownloadManagerError(ValueError):
    """start/cancel/update 入参或运行时拒绝。带 `code` 供 HTTP 端点映射稳定状态码。

    code: bad_param / no_downloadable / disk_space_insufficient / job_conflict /
          job_not_running / unknown_job
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------- 注册表

_REGISTRY_LOCK = threading.RLock()
_JOBS: "dict[str, dict]" = {}
_ACTIVITY_CALLBACK: "Callable[[bool], None] | None" = None


def bind_activity_callback(callback: "Callable[[bool], None] | None") -> None:
    """绑定一个壳层活动通知回调；浏览器模式留空即零副作用。

    依赖方向由 app/webview_shell 在建窗时把 `set_download_active` 注入本模块，
    corpus 层不反向 import app 层。绑定时立即同步当前真状态，覆盖“服务已开始
    下载、壳稍后建立”的竞态。
    """
    global _ACTIVITY_CALLBACK
    with _REGISTRY_LOCK:
        _ACTIVITY_CALLBACK = callback
    _sync_activity()


def _notify_activity(active: bool) -> None:
    callback = _ACTIVITY_CALLBACK
    if callback is None:
        return
    try:
        callback(bool(active))
    except Exception:
        # 壳层提示是保护增强，绝不能因 UI 回调失败破坏真实下载。
        pass


def _sync_activity() -> None:
    """从注册表真状态重算，避免旧 job 收尾把刚开始的新 job 误报为 false。"""
    with _REGISTRY_LOCK:
        active = any(job.get("state") == STATE_RUNNING for job in _JOBS.values())
    _notify_activity(active)


class _MultiCancel:
    """下载取消信号合成器：把「整 job 取消」与「单行被移除」两个 event 合成一个 `is_set()`。

    `download_one` 的 `cancel_event` 参数只调用 `.is_set()`，因此传任意带该方法的对象即可。
    用逐行 event 而**不**复用整 job 的 `_cancel_event`：移除某一行正在下载的文件时，只置那
    一行的 event（仅该文件中止），整 job 的 event 保持未置位——否则下一行 download_one
    一进就立刻看到置位而整体取消，移除一条变成取消全部。
    """

    def __init__(self, *events: "threading.Event") -> None:
        self._events = tuple(events)

    def is_set(self) -> bool:
        return any(e.is_set() for e in self._events)


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{int(value)} B" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def _err_summary(exc: BaseException) -> str:
    return type(exc).__name__


# ---------------------------------------------------------------- 记录 → item → 计划（纯函数）

def _block_user_uploaded() -> bool:
    """env 开关 `BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED`：置 1/true/yes/on 时，用户上传记录
    默认禁止服务端代下（进 unsupported 带中文 reason）。

    默认（未设置/0/false）= **允许**——单机用户场景要靠上传记录的直链直接下载。
    注意这只是一个策略便利：攻击者改记录 source 标签即可绕过，
    真正的安全边界是 executor 的逐跳闸 + IP 解析闸 + 字节硬上限（对所有来源无条件生效）。
    """
    return os.environ.get("BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED", "").strip().lower() \
        in ("1", "true", "yes", "on")


def _item_from_record(record: Any) -> dict:
    """归一化记录 / 原始 dict → build_plan 要求的最小 item 形状。

    `download_url` 取值链与 `item_view.build_item` 一致（primary_url(uid) → 记录 download_url →
    页面 url 兜底）；`filesize` 原样带过，由 `provenance.size_bytes_or_none` 收敛「0=未知」。
    """
    raw = (record.raw if isinstance(getattr(record, "raw", None), dict)
           else record if isinstance(record, dict) else {})
    uid = str(raw.get("dataset_uid") or getattr(record, "dataset_uid", "") or "").strip()
    page = str(getattr(record, "url", None) or raw.get("url") or "").strip()
    direct = downloads.primary_url(uid) or str(raw.get("download_url") or "").strip()
    name = str(getattr(record, "dataset_name", None) or raw.get("dataset_name") or "") or uid
    source = str(raw.get("source") or "").strip() or provenance.SOURCE_10X
    return {
        "dataset_uid": uid,
        "url": page,
        "download_url": direct or page,
        "filesize": raw.get("filesize"),
        "source": source,
        "dataset_name": name,
    }


def _records_by_uid(records: "dict | None" = None) -> dict:
    """uid → 记录。`records` 为 None 时装载真实语料（base + 全部 external，零网络）。

    惰性 import `corpus`：模块 import 期不拉语料，端点/测试按需才装。
    """
    if records is not None:
        return {str(uid): rec for uid, rec in records.items()}
    from .corpus import load_full_corpus  # 惰性
    root = Path(__file__).resolve().parents[3]
    loaded = load_full_corpus(root / "database" / "base", root)
    by_uid: dict = {}
    for record in loaded:
        raw = record.raw if isinstance(getattr(record, "raw", None), dict) else {}
        uid = str(raw.get("dataset_uid") or "").strip()
        if uid and uid not in by_uid:
            by_uid[uid] = record
    return by_uid


def build_download_plan(uids: Sequence[str], *, records: "dict | None" = None) -> dict:
    """uids → 完整下载计划。**纯函数、离线、零网络、不写盘、不起线程。**

    返回：
      items         list[{dataset_uid, dataset_title, source, tier, page_url, bytes, files:[{filename,url,bytes}]}]
      unsupported   list[{dataset_uid, title, reason}]（含语料查不到的编号）
      total_bytes   int（items 内全部文件字节合计；flagged 文件也计入——预检按保守口径多留空间）
      rows          build_plan 原始行（start_job 消费；含 flagged 行，执行时默认跳过）
      allowed_hosts build_plan 派生的 https+主机双闸白名单（单一真源）
      plan          build_plan 完整输出（诊断/测试用）

    判定完全复用 `download_plan.build_plan` 的四档，不另造一套。
    """
    by_uid = _records_by_uid(records)
    items: list[dict] = []
    unsupported: list[dict] = []
    seen: set[str] = set()
    block_uploaded = _block_user_uploaded()
    if block_uploaded:
        from .uploads import DEFAULT_UPLOAD_SOURCE  # 惰性：避免顶层拉 uploads→corpus 加载链
    for raw_uid in uids:
        uid = str(raw_uid or "").strip()
        if not uid or uid in seen:
            continue
        seen.add(uid)
        record = by_uid.get(uid)
        if record is None:
            unsupported.append({"dataset_uid": uid, "title": uid,
                                "reason": "本机语料中没有这个数据集编号，无法提供下载信息。"})
            continue
        item = _item_from_record(record)
        if block_uploaded and item["source"] == DEFAULT_UPLOAD_SOURCE:
            unsupported.append({
                "dataset_uid": uid,
                "title": item["dataset_name"] or uid,
                "reason": "已配置 BIODATA_DOWNLOAD_BLOCK_USER_UPLOADED=1：用户上传的记录默认"
                          "禁止服务端代下（请在调用方侧自行下载，或关闭该开关恢复代下）。",
            })
            continue
        items.append(item)
    plan = DP.build_plan(items)
    by_plan = {it["dataset_uid"]: it for it in plan["items"]}
    supported: list[dict] = []
    for item in items:
        uid = item["dataset_uid"]
        pit = by_plan[uid]
        if pit["rows_planned"] > 0:
            files = [{"filename": r["filename"], "url": r["download_url"],
                      "bytes": int(r.get("bytes") or 0)}
                     for r in plan["rows"] if r["dataset_uid"] == uid]
            supported.append({
                "dataset_uid": uid,
                "dataset_title": pit["dataset_name"] or uid,
                "source": pit["source"],
                "tier": pit["tier"],
                "page_url": pit["page_url"],
                "bytes": sum(f["bytes"] for f in files),
                "files": files,
            })
        else:
            unsupported.append({
                "dataset_uid": uid,
                "title": pit["dataset_name"] or uid,
                "reason": _UNSUPPORTED_ZH.get(pit["tier"]) or DP.tier_text(pit["tier"]),
            })
    return {
        "items": supported,
        "unsupported": unsupported,
        "total_bytes": sum(f["bytes"] for it in supported for f in it["files"]),
        "rows": [dict(r) for r in plan["rows"]],
        "allowed_hosts": list(plan.get("allowed_hosts") or []),
        "plan": plan,
    }


# ---------------------------------------------------------------- 目标目录与磁盘预检

def default_download_dir() -> Path:
    """本机下载根：`~/Downloads/<领域品牌前缀>-YYYYMMDD-HHMMSS/`（前缀随活动领域包，biodata=BioData数据）。"""
    from ..domain.registry import get_domain
    prefix = get_domain().branding.download_dir_prefix or "BioData数据"
    return (Path.home() / "Downloads"
            / f"{prefix}-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}")


def _ensure_disk_space(job_dir: Path, total_bytes: int) -> None:
    """磁盘预检：剩余空间 < total×1.05 → 拒绝（fail-closed，宁可不开始）。"""
    if total_bytes <= 0:
        return
    usage = shutil.disk_usage(str(job_dir))
    need = total_bytes * DISK_HEADROOM
    if usage.free < need:
        raise DownloadManagerError(
            "disk_space_insufficient",
            f"这批文件共约 {_human(total_bytes)}，按 1.05 倍预留给 {_human(need)}；"
            f"目标盘（{job_dir.drive or '/'}）当前可用 {_human(usage.free)}，空间不足。"
            "请清理磁盘后重试，或改用任务包。")


# ---------------------------------------------------------------- job 装配

def _subdir_for(item: dict, taken: "set[str]") -> str:
    """逐数据集子文件夹名：`<safe_uid>__<标题前40字符安全化>`。"""
    base = DP.safe_uid(item["dataset_uid"])
    title40 = DP.safe_name(str(item["dataset_title"] or "")[:40])
    candidate = f"{base}__{title40}"
    index = 2
    while candidate.lower() in taken:
        candidate = f"{base}__{title40}~{index}"
        index += 1
    taken.add(candidate.lower())
    return candidate


def _make_job(job_dir: Path, plan: dict) -> dict:
    job_id = (f"dl-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"
              f"-{uuid.uuid4().hex[:6]}")
    taken: set[str] = set()
    subdirs: dict[str, str] = {}
    titles: dict[str, str] = {}
    plan_items: list[dict] = []
    for it in plan["items"]:
        subdirs[it["dataset_uid"]] = _subdir_for(it, taken)
        titles[it["dataset_uid"]] = it["dataset_title"]
        plan_items.append({"dataset_uid": it["dataset_uid"], "dataset_title": it["dataset_title"],
                           "source": it["source"], "page_url": it["page_url"]})
    files: list[dict] = []
    for row in plan["rows"]:
        files.append({
            "dataset_uid": row["dataset_uid"],
            "dataset_title": titles.get(row["dataset_uid"], row["dataset_uid"]),
            "filename": row["filename"],
            "url": row["download_url"],
            "bytes": int(row.get("bytes") or 0),
            "done_bytes": 0,
            "status": FILE_PENDING,
            "error": row.get("flag_reason_zh") or "",
            # 完成时由 _finalize_file 填充；pending 期间也保持键存在（状态快照键集一致）。
            "saved_as": "",
            "md5_actual": "",
            "http_status": None,
        })
    return {
        "job_id": job_id,
        "state": STATE_RUNNING,
        "created_at": _utc_now(),
        "dir": str(job_dir),
        "total_bytes": plan["total_bytes"],
        "done_bytes": 0,
        "files": files,
        "unsupported": [dict(u) for u in plan["unsupported"]],
        "started_at": _utc_now(),
        "finished_at": "",
        "cancel_requested": False,
        "error": "",
        # ---- 私有（不进状态快照）----
        "_rows": list(plan["rows"]),
        "_allowed": list(plan["allowed_hosts"]),
        "_subdirs": subdirs,
        "_plan_items": plan_items,
        # 在途队列的权威 uid 列表（按进队顺序，随 add/remove 增删）。
        # 与状态快照的 files 不同——files 里被移除的数据集仍留有 skipped 条目，不能用于推导
        # 在途集（会把已移除的编号又当成在途）；_queue_uids 才是「下次差量」的基准。
        "_queue_uids": [it["dataset_uid"] for it in plan["items"]],
        "_cancel_event": threading.Event(),
        # （在途增删）：逐行取消信号 + 被用户移除的行下标集合。
        # _row_events 与 _rows/_files 按下标对齐（update add 会同步 append），
        # _removed_rows 供 _run_job 区分「整 job 取消」与「单行被移除」两种 DownloadCancelled。
        "_row_events": [threading.Event() for _ in plan["rows"]],
        "_removed_rows": set(),
        "_manifest_path": str(job_dir / "manifest.tsv"),
        "_readme_path": str(job_dir / "README.txt"),
        "_manifest_error": False,
    }


def _evict_old_jobs() -> None:
    """注册表只保留最近 MAX_JOBS 个；绝不驱逐仍在运行的任务（防御性检查）。"""
    while len(_JOBS) > MAX_JOBS:
        oldest = next(iter(_JOBS))
        if _JOBS[oldest].get("state") == STATE_RUNNING:
            break
        _JOBS.pop(oldest)


# ---------------------------------------------------------------- 快照 / 取消 / 状态

def _snapshot(job: dict) -> dict:
    return {
        "job_id": job["job_id"],
        "state": job["state"],
        "created_at": job["created_at"],
        "dir": job["dir"],
        "total_bytes": job["total_bytes"],
        "done_bytes": job["done_bytes"],
        "files": [dict(f) for f in job["files"]],
        "unsupported": [dict(u) for u in job["unsupported"]],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "cancel_requested": bool(job["cancel_requested"]),
        "error": job.get("error", ""),
    }


def get_status(job_id: str) -> "dict | None":
    """取任务状态快照；不存在返回 None（HTTP 层映射 404）。"""
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
        return _snapshot(job) if job else None


def cancel_job(job_id: str) -> "dict | None":
    """置取消标志并立刻把状态置为 cancelled（线程随后在 chunk/文件间停手）。

    running 任务取消后保留 .part（可续传语义）；对已结束的任务是幂等 no-op。
    返回状态快照；任务不存在返回 None。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return None
        job["cancel_requested"] = True
        job["_cancel_event"].set()
        if job["state"] == STATE_RUNNING:
            job["state"] = STATE_CANCELLED
        return _snapshot(job)


# ---------------------------------------------------------------- start_job

def start_job(uids: Sequence[str], *, records: "dict | None" = None,
              out_dir: "str | None" = None,
              opener: "Callable | None" = None,
              sleep: "Callable | None" = None) -> dict:
    """预检 + 建目录 + 起后台线程，返回 {job_id, dir, total_bytes}。

    失败以 `DownloadManagerError` 抛出：
      no_downloadable（400）   一批里没有任何可直接下载的文件
      disk_space_insufficient（507） 磁盘预检不通过
      job_conflict（409）      已有任务在运行（同一时刻只允许一个）
      bad_param（400）         入参形状非法（空列表等）

    `records/out_dir/opener/sleep` 仅供测试注入，端点不传。
    """
    if not uids:
        raise DownloadManagerError("bad_param", "uids 必须是非空数组（数据集编号列表）。")
    plan = build_download_plan(uids, records=records)
    if not plan["items"]:
        first = plan["unsupported"][0] if plan["unsupported"] else {}
        raise DownloadManagerError(
            "no_downloadable",
            f"这批数据集没有任何可直接下载的文件：{len(plan['unsupported'])} 条不支持"
            + (f"（例如 {first.get('dataset_uid', '')}：{first.get('reason', '')}）。"
               if first else "") + "可改用任务包，或换一批数据集。")
    base = Path(out_dir) if out_dir else default_download_dir()
    if not out_dir and base.exists():
        # 默认目录名只有秒级时间戳：同秒连续任务/多进程实例会撞目录并互相覆盖
        # README/manifest/同名文件——撞名时追加随机短后缀（与 job_id 的 uuid 后缀同式）。
        base = base.with_name(f"{base.name}-{uuid.uuid4().hex[:6]}")
    # **先预检、后建目录**（回归钉）：磁盘预检用「父目录」做（同卷任一路径的
    # disk_usage 结果相同），预检不通过就抛 507、**不创建任务目录**——此前先 mkdir 再预检，
    # 507 路径会在真实下载目录（~/Downloads/BioData数据-*）留下空目录（批留下 8 个的根源）。
    base.parent.mkdir(parents=True, exist_ok=True)
    _ensure_disk_space(base.parent, plan["total_bytes"])
    base.mkdir(parents=True, exist_ok=True)
    job_dir = base.resolve()
    with _REGISTRY_LOCK:
        running = [j for j in _JOBS.values() if j.get("state") == STATE_RUNNING]
        if running:
            raise DownloadManagerError(
                "job_conflict",
                f"已有下载任务 {running[0]['job_id']} 正在运行；"
                "请先等它结束，或在状态面板取消它，再开始新的。")
        job = _make_job(job_dir, plan)
        _JOBS[job["job_id"]] = job
        _evict_old_jobs()
    _write_readme(job)
    _write_manifest_header(job)
    thread = threading.Thread(
        target=_run_job, args=(job,), kwargs={"opener": opener, "sleep": sleep},
        name=f"biodata-download-{job['job_id']}", daemon=True)
    _sync_activity()
    try:
        thread.start()
    except Exception:
        with _REGISTRY_LOCK:
            job["state"] = STATE_ERROR
            job["error"] = "ThreadStartError"
            job["finished_at"] = _utc_now()
        _sync_activity()
        raise
    return {"job_id": job["job_id"], "dir": job["dir"], "total_bytes": plan["total_bytes"]}


# ---------------------------------------------------------------- 在途增删

#: 文件已落盘 → 该数据集不可移除（文件已在磁盘，用户需自行删除）。
_ON_DISK_STATUSES = {FILE_OK, FILE_SIZE_OK, FILE_MD5_MISMATCH}
_REMOVE_SKIPPED = "skipped"     # 移除排队中的条目（标记跳过，不再下载）
_REMOVE_ABORTED = "aborted"     # 移除正在下载的条目（中止当前文件，清理 .part/子目录，继续下一条）


def _find_running_job() -> "dict | None":
    with _REGISTRY_LOCK:
        for job in _JOBS.values():
            if job.get("state") == STATE_RUNNING:
                return job
    return None


def update_job(*, add: "Sequence[str]" = (), remove: "Sequence[str]" = (),
               records: "dict | None" = None) -> dict:
    """在当前**运行中的**下载任务上做增量增删。

    `remove`（按 uid，作用于当前 running job）：
      - 排队中的条目 → 标记跳过，不再下载（_REMOVE_SKIPPED）；
      - **正在下载**的条目 → 中止该数据集当前文件、清掉未完成部分（.part 与其子目录）、
        继续队列下一条（_REMOVE_ABORTED；只置该行取消信号，不动整 job 的取消事件）；
      - **已完成**（文件已落盘）的条目 → 拒绝并如实说明（文件已在磁盘，需自行删除）；
      - 不在队列里的 uid → not_in_job 标注，不报错。

    `add`（按 uid）：
      - 新条目追加到当前 job 队列尾部（与 start_job 同构的 plan 行构造）；已入队 → already_in_queue；
      - 不可下载的（unsupported）如实追加标注。

    无运行中任务 → 抛 `DownloadManagerError("job_not_running", …)`（HTTP 层映射 409）。

    线程安全：查找、状态写、列表 append 都在 `_REGISTRY_LOCK` 内；`build_download_plan` 读语料
    是离线纯函数，在锁外完成，避免持锁装载语料；锁内再次校验 job 仍为 running（查找与加锁之间
    线程可能已收尾）。
    """
    add_list = [str(u).strip() for u in (add or []) if str(u).strip()]
    remove_list = [str(u).strip() for u in (remove or []) if str(u).strip()]
    if not add_list and not remove_list:
        raise DownloadManagerError("bad_param", "add 或 remove 至少要有一个非空数组。")

    job = _find_running_job()
    if job is None:
        raise DownloadManagerError("job_not_running", "当前没有进行中的下载任务，没有可更新的队列。")
    # 锁外：读语料，纯函数零网络；records 仅供测试注入（与 start_job/端点不传同口径）
    plan = build_download_plan(add_list, records=records) if add_list else None

    with _REGISTRY_LOCK:
        if job["state"] != STATE_RUNNING:
            raise DownloadManagerError("job_not_running", "下载任务已结束，无法再增删条目。")

        # fail-closed 预检先于一切变更（含 remove）：磁盘预检若放在 remove 之后，
        # 预检失败时 remove 的跳过/中止信号已生效而端点却报错，调用方会以为整体失败。
        # 在途追加与 start_job 同一闸，防多次追加撑爆磁盘。
        new_items: list[dict] = []
        if plan:
            new_items = [it for it in plan["items"] if it["dataset_uid"] not in job["_subdirs"]]
            _ensure_disk_space(Path(job["dir"]).parent,
                               sum(int(it.get("bytes") or 0) for it in new_items))

        # ---------- remove ----------
        removed: list[dict] = []
        rejected: list[dict] = []
        not_in_job: list[dict] = []
        index_by_uid: dict[str, list[int]] = {}
        for i, row in enumerate(job["_rows"]):
            index_by_uid.setdefault(row["dataset_uid"], []).append(i)
        for uid in remove_list:
            rows_here = index_by_uid.get(uid)
            if not rows_here:
                not_in_job.append({"dataset_uid": uid})
                continue
            entries = [job["files"][i] for i in rows_here]
            title = entries[0]["dataset_title"]
            if any(f["status"] in _ON_DISK_STATUSES for f in entries):
                rejected.append({
                    "dataset_uid": uid,
                    "dataset_title": title,
                    "reason": "该数据集已有文件下载完成（文件已在磁盘），不能从队列移除；"
                              "如不需要请自行删除。",
                })
                continue
            # 全部未落盘 → 可移除。排队中的标 skipped；正在下载的置该行取消信号（abort）。
            outcome = _REMOVE_SKIPPED
            for i in rows_here:
                job["_removed_rows"].add(i)
                f = job["files"][i]
                if f["status"] == FILE_DOWNLOADING:
                    outcome = _REMOVE_ABORTED
                    job["_row_events"][i].set()
                if f["status"] == FILE_PENDING:
                    f["status"] = FILE_SKIPPED
                    f["error"] = "已从下载列表移除（未开始下载，不再下载）。"
                elif f["status"] == FILE_DOWNLOADING:
                    f["status"] = FILE_SKIPPED
                    f["error"] = "已从下载列表移除（下载中止，未完成部分已清理）。"
                # error / cancelled / skipped 类：本就结束/跳过，不再下载（status 保持，属终端）
            removed.append({"dataset_uid": uid, "dataset_title": title, "outcome": outcome})
            if uid in job["_queue_uids"]:
                job["_queue_uids"].remove(uid)

        # ---------- add ----------
        added: list[dict] = []
        added_unsupported: list[dict] = []
        if plan:
            # 磁盘预检已在 remove 之前完成（fail-closed）；此处只剩非失败性变更。
            # 追加批次的 allowed_hosts 并入任务白名单（plan 是白名单单一真源）——否则
            # 新主机的追加文件会被 worker 的 SSRF 闸全部拒绝（显示已加入、随后全失败）。
            job["_allowed"] = sorted(set(job["_allowed"]) | set(plan.get("allowed_hosts") or []))
            taken = {v.lower() for v in job["_subdirs"].values()}
            new_subdirs: dict[str, str] = {}
            new_titles: dict[str, str] = {}
            for it in plan["items"]:
                uid = it["dataset_uid"]
                if uid in job["_subdirs"]:
                    added.append({"dataset_uid": uid, "dataset_title": it["dataset_title"],
                                  "status": "already_in_queue"})
                    continue
                new_subdirs[uid] = _subdir_for(it, taken)
                new_titles[uid] = it["dataset_title"]
                job["_plan_items"].append({
                    "dataset_uid": uid, "dataset_title": it["dataset_title"],
                    "source": it["source"], "page_url": it["page_url"],
                })
                job["_queue_uids"].append(uid)
                added.append({"dataset_uid": uid, "dataset_title": it["dataset_title"],
                              "status": "added", "files": len(it["files"]), "bytes": it["bytes"]})
            for row in plan["rows"]:
                uid = row["dataset_uid"]
                if uid not in new_subdirs:
                    continue   # already_in_queue 的行不重建
                job["_rows"].append(row)
                job["files"].append({
                    "dataset_uid": uid,
                    "dataset_title": new_titles[uid],
                    "filename": row["filename"],
                    "url": row["download_url"],
                    "bytes": int(row.get("bytes") or 0),
                    "done_bytes": 0,
                    "status": FILE_PENDING,
                    "error": row.get("flag_reason_zh") or "",
                    "saved_as": "",
                    "md5_actual": "",
                    "http_status": None,
                })
                job["_row_events"].append(threading.Event())
            for uid, sub in new_subdirs.items():
                job["_subdirs"][uid] = sub
            for u in plan["unsupported"]:
                added_unsupported.append({"dataset_uid": u["dataset_uid"], "title": u["title"],
                                          "reason": u["reason"]})
                job["unsupported"].append({"dataset_uid": u["dataset_uid"], "title": u["title"],
                                           "reason": u["reason"]})
            job["total_bytes"] = sum(int(f.get("bytes") or 0) for f in job["files"])

    # 锁外重写 README（增删后数据集清单变化），再取快照返回
    _write_readme(job)
    with _REGISTRY_LOCK:
        snapshot = _snapshot(job)
    return {
        "job_id": job["job_id"],
        "added": added,
        "added_unsupported": added_unsupported,
        "removed": removed,
        "rejected": rejected,
        "not_in_job": not_in_job,
        "total_bytes": job["total_bytes"],
        "queue_uids": list(job["_queue_uids"]),
        "snapshot": snapshot,
    }


# ---------------------------------------------------------------- 任务线程

def _progress_cb(job: dict, entry: dict) -> "Callable[[int], None]":
    def _cb(delta: int) -> None:
        with _REGISTRY_LOCK:
            entry["done_bytes"] += delta
            job["done_bytes"] += delta
    return _cb


def _finalize_file(job: dict, entry: dict, result: DE.FileResult, row: dict) -> None:
    status = _EXEC_TO_FILE.get(result.status, FILE_ERROR)
    error = ""
    if status == FILE_SKIPPED:
        error = row.get("flag_reason_zh") or "来源巡检标记为有问题的文件，本次跳过。"
    elif result.status in (DE.STATUS_SIZE_MISMATCH, DE.STATUS_UNVERIFIED,
                           DE.STATUS_UNREACHABLE, DE.STATUS_REJECTED):
        if result.error and "硬上限" in result.error:
            # 超过单文件硬上限（服务器实际输出远超声明大小/未声明大小）——给专门文案，
            # 不套用「重试后仍未能下载完整文件」的通用措辞（那个会暗示值得重试）。
            error = "文件超过单文件硬上限（服务器实际输出远超声明大小），已中止并清理 .part。"
        else:
            error = {
                DE.STATUS_SIZE_MISMATCH: "文件字节数与来源声明不符；已改名 .corrupt 保留证据。",
                DE.STATUS_UNVERIFIED: "文件已下载，但来源既没给 md5 也没给大小，无法核对。",
                DE.STATUS_UNREACHABLE: "重试后仍未能下载完整文件（网络错误或服务器拒绝）。",
                DE.STATUS_REJECTED: "下载被安全规则拒绝（非 https、端口不在白名单、主机不在"
                                    "白名单，或解析到回环/私网/链路本地等禁止网段）。",
            }[result.status]
            if result.error:
                error += f"（{result.error}）"
    with _REGISTRY_LOCK:
        entry["done_bytes"] = int(result.bytes_downloaded or 0)
        entry["status"] = status
        entry["error"] = error
        entry["saved_as"] = result.saved_as or ""
        entry["md5_actual"] = result.md5_actual or ""


def _cleanup_removed_row(job: dict, entry: dict) -> None:
    """移除一个正在下载/排队中的数据集时，清掉它的未完成部分：`.part` 及其子目录。

    由于同一个数据集在**任一**文件已落盘（ok/size_ok/md5_mismatch）时移除会被拒绝
    （见 update_job），走到这里的子目录里必然没有已完成的归属文件，删子目录是安全的。
    正在写的 `.part` 在 `download_one` 的 finally 里因取消置位而保留（未删），
    这里在下载线程自己 abort 之后（文件句已关）rmtree，`ignore_errors` 兜底句柄残余竞态。
    """
    sub = job["_subdirs"].get(entry["dataset_uid"])
    if sub:
        shutil.rmtree(Path(job["dir"]) / sub, ignore_errors=True)


def _run_job(job: dict, *, opener: "Callable | None" = None,
             sleep: "Callable | None" = None) -> None:
    """job 后台线程：逐行调用 executor 下载，chunk 间/文件间检查取消。

    支持在途增删——`update_job` 移除正在下载的行时只置该行的 `_row_events[idx]`
    （复合取消，见 `_MultiCancel`），`download_one` 随即抛 `DownloadCancelled`，这里据此
    `continue` 走下一行（而不是把整个 job 标成 cancelled）；移除排队中的行进 status=skipped
    天然被 `if entry["status"] != FILE_PENDING: continue` 跳过。
    """
    rows = job["_rows"]
    allowed = job["_allowed"]
    cancel_event = job["_cancel_event"]
    cancelled = False
    try:
        for idx, row in enumerate(rows):
            if job["cancel_requested"]:
                cancelled = True
                break
            entry = job["files"][idx]
            if entry["status"] != FILE_PENDING:
                continue
            with _REGISTRY_LOCK:
                entry["status"] = FILE_DOWNLOADING
            # opener/sleep 只在注入时才传：显式传 None 会覆盖 download_one 的默认值
            # （_open_stream / time.sleep），真实路径会炸成 "TypeError: 'NoneType' object is not callable"
            # ——测试全部注入 opener，这个洞在集成上才现形（集成 sanity 抓到）。
            inject = {}
            if opener is not None:
                inject["opener"] = opener
            if sleep is not None:
                inject["sleep"] = sleep
            row_evt = job["_row_events"][idx] if idx < len(job["_row_events"]) else threading.Event()
            try:
                result = DE.download_one(
                    row, Path(job["dir"]), allowed,
                    subdir=job["_subdirs"][row["dataset_uid"]],
                    cancel_event=_MultiCancel(cancel_event, row_evt),
                    progress_cb=_progress_cb(job, entry),
                    **inject)
            except DE.DownloadCancelled:
                with _REGISTRY_LOCK:
                    removed = idx in job["_removed_rows"]
                    if removed:
                        # 用户移除了正在下载的这一行：abort 后清掉未完成部分，继续下一条。
                        entry["status"] = FILE_SKIPPED
                        entry["error"] = "已从下载列表移除（下载中止，未完成部分已清理）。"
                    else:
                        entry["status"] = FILE_CANCELLED
                        entry["error"] = "下载被取消；已保留 .part 文件（可续传语义）。"
                if removed:
                    _cleanup_removed_row(job, entry)
                    continue
                cancelled = True
                break
            with _REGISTRY_LOCK:
                # 竞态兜底：移除发生在下载完成前一刻，则「下载赢」——文件已落盘，按真实
                # 完成状态记录（_finalize_file），并把该行从「已移除」里撤下（后续移除会因
                # 文件已在磁盘而被诚实拒绝）。不再额外跳过，避免把真文件伪装成 skipped。
                if idx in job["_removed_rows"]:
                    job["_removed_rows"].discard(idx)
            _finalize_file(job, entry, result, row)
            _append_manifest(job, entry)
        with _REGISTRY_LOCK:
            job["state"] = STATE_CANCELLED if (cancelled or job["cancel_requested"]) else STATE_DONE
    except Exception as exc:  # 意外异常：如实记 error，已完成的文件状态保留
        with _REGISTRY_LOCK:
            job["state"] = STATE_ERROR
            job["error"] = _err_summary(exc)
    finally:
        with _REGISTRY_LOCK:
            job["finished_at"] = _utc_now()
        _write_readme(job, final=True)
        _sync_activity()


# ---------------------------------------------------------------- README / manifest

def _write_manifest_header(job: dict) -> None:
    try:
        Path(job["_manifest_path"]).write_text(_MANIFEST_HEADER + "\n", encoding="utf-8")
    except OSError:
        job["_manifest_error"] = True


def _append_manifest(job: dict, entry: dict) -> None:
    try:
        with open(job["_manifest_path"], "a", encoding="utf-8") as fh:
            fh.write("\t".join([
                entry["dataset_uid"],
                entry["dataset_title"],
                job["_subdirs"].get(entry["dataset_uid"], ""),
                entry["filename"],
                entry["url"],
                str(entry["bytes"]),
                str(entry["done_bytes"]),
                entry["status"],
                entry["error"],
                entry.get("md5_actual", ""),
            ]) + "\n")
    except OSError:
        job["_manifest_error"] = True


def _write_readme(job: dict, *, final: bool = False) -> None:
    """根目录 README.txt：开始写一份（数据集清单 + 校验结果含义），结束重写并附本次结果。"""
    try:
        lines = [
            "BioData 数据下载目录",
            "====================",
            f"任务编号：{job['job_id']}",
            f"开始时间：{job['started_at']}",
            f"状态：{job['state']}",
            "说明：本文件夹由 BioData Agent 直接下载到本机。",
            "每个子文件夹 = 一个数据集，文件夹名 = 数据集编号_标题（编号与标题见下方清单）。",
            "",
            "【数据集清单】",
        ]
        for i, it in enumerate(job["_plan_items"], 1):
            lines.append(f"{i}. {job['_subdirs'][it['dataset_uid']]} —— {it['dataset_title']}"
                         f"（来源：{it['source']}；页面：{it['page_url']}）")
        if job["unsupported"]:
            lines.append("")
            lines.append("【本批未下载的数据集】")
            for u in job["unsupported"]:
                lines.append(f"- {u['dataset_uid']}（{u['title']}）：{u['reason']}")
        lines += [
            "",
            "【校验结果含义】",
            "- ok：文件已下载，md5 校验和与来源声明一致。",
            "- size_ok：来源未提供 md5，已核对文件字节数与来源声明一致。",
            "- md5_mismatch：文件已下载，但 md5 与来源声明不符；文件已改名 .corrupt 保留作证据。",
            "- error：下载失败或校验失败（具体原因见 manifest.tsv 的 error 列）。",
            "- skipped：来源巡检标记为有问题的文件，本次跳过。",
            "- cancelled：下载被取消；已保留 .part 文件（可续传）。",
            "",
            "【逐文件结果】",
            "manifest.tsv（UTF-8，制表符分隔）逐文件记录下载结果，可用 Excel / 表格软件打开。",
        ]
        if final:
            counts = {s: 0 for s in FILE_STATUSES}
            for f in job["files"]:
                counts[f["status"]] = counts.get(f["status"], 0) + 1
            lines += [
                "",
                "【本次结果】",
                f"- 状态：{job['state']}",
                f"- 计划文件 {len(job['files'])} 个；完成时间：{job['finished_at']}",
                f"- 成功 {counts[FILE_OK] + counts[FILE_SIZE_OK]}（md5 一致 {counts[FILE_OK]}，"
                f"仅大小一致 {counts[FILE_SIZE_OK]}）；失败 {counts[FILE_ERROR]}；"
                f"跳过 {counts[FILE_SKIPPED]}；取消 {counts[FILE_CANCELLED]}。",
            ]
            if job.get("_manifest_error"):
                lines.append("- 注意：manifest.tsv 写入失败，逐文件结果仅见本 README 的状态行。")
        Path(job["_readme_path"]).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        job["_manifest_error"] = True
