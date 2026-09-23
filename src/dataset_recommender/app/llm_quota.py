# -*- coding: utf-8 -*-
"""网页版账号护栏（T3，2026-08-25）：账号级 LLM 调用日配额。

定位：**只在部署护栏形态（``BIODATA_REQUIRE_ACCOUNT=1``）下被调用**；本机单机形态
完全不经过本模块（闸口在 webapp `_gate_llm_quota`，缺省关 = 逐字节不变）。

计数持久化到实例 userdata 层 ``llm_quota.json``（``BIODATA_LLM_QUOTA_FILE`` 可覆盖，
原子写 tmp+os.replace），按 **UTC 日**重置（北京时间每日 08:00 归零）。账本只服务
日配额，跨日旧账整段丢弃、不留历史。

纪律（T3 任务书 §2.3）：
- 计数存储故障 → **放行** + error 日志（可用性优先；provider 侧消费上限是最后防线）。
- 账本损坏 → 重建空账 + warning（与 sessions 库同为可再生状态，fail-open）。
- 只计「将真实消耗服务端 LLM」的请求：BYOK（请求自带 key）/ mock / 未启用 / 服务端
  无 key 一律不计——判定在调用方（webapp `_gate_llm_quota`），本模块只管账本。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

from .runtime_paths import atomic_write_json, instance_data_dir_for

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()


class QuotaExceeded(Exception):
    """超限：message 为中文人读文案（接口层翻 429，不泄漏内部细节）。"""


def default_quota_path(project_root: Path) -> Path:
    """配额账本默认路径 = 实例 userdata 层（`.userdata/llm_quota.json`）；
    `BIODATA_LLM_QUOTA_FILE` 显式覆盖优先（测试与特殊部署用）。"""
    override = os.environ.get("BIODATA_LLM_QUOTA_FILE", "").strip()
    if override:
        return Path(override)
    return instance_data_dir_for(Path(project_root), ".userdata") / "llm_quota.json"


def _utc_day(now: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(now if now is not None else time.time()))


def _load(path: Path) -> dict[str, Any]:
    """读账本。账本**可再生**（丢了 = 当日重新计数，不丢账户数据），fail-open：
    缺失/损坏/结构不符 → 空账 + warning，绝不阻断请求。"""
    try:
        raw = path.read_bytes()
    except (FileNotFoundError, OSError):
        return {"days": {}}
    if not raw.strip():
        return {"days": {}}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("llm_quota: 账本损坏，按空账重建（%s）", path)
        return {"days": {}}
    if not isinstance(data, dict) or not isinstance(data.get("days"), dict):
        logger.warning("llm_quota: 账本结构异常，按空账重建（%s）", path)
        return {"days": {}}
    return data


def _save(path: Path, data: dict[str, Any]) -> None:
    """原子写走 runtime_paths.atomic_write_json（紧凑格式，与历史字节一致）。"""
    atomic_write_json(path, data, indent=None)


def check_and_increment(
    project_root: Path,
    username: str,
    *,
    trial: bool,
    per_user_limit: int,
    global_limit: int,
    quota_path: Path | None = None,
) -> None:
    """当日计数 +1；超限则**不加计数**直接抛 `QuotaExceeded`（中文文案）。

    - `trial=True` 走独立的试用桶（`u:<name>:trial` / `global:trial`），与正式服务端
      key 的桶互不挤占——试用限轮与正式配额是两笔独立的账。
    - `per_user_limit` / `global_limit` ≤ 0 表示该维度不限（两道闸各自独立生效）。
    - 存储任何故障：放行 + error 日志（可用性优先，见模块 docstring）。
    """
    path = quota_path or default_quota_path(project_root)
    day = _utc_day()
    user_key = f"u:{username}:trial" if trial else f"u:{username}"
    global_key = "global:trial" if trial else "global"
    with _LOCK:
        try:
            data = _load(path)
            days = data["days"]
            # 只留当日：账本只为日配额服务，跨日旧账整段丢弃（防无界增长）。
            for old in [d for d in days if d != day]:
                days.pop(old, None)
            bucket = days.setdefault(day, {})
            used_user = int(bucket.get(user_key, 0) or 0)
            used_global = int(bucket.get(global_key, 0) or 0)
            if per_user_limit > 0 and used_user >= per_user_limit:
                if trial:
                    raise QuotaExceeded(
                        f"限量试用通道每日最多 {per_user_limit} 轮对话，今日已用完；"
                        "按 UTC 日重置（北京时间每日 8:00）。如需不限量使用，"
                        "请在「设置 → AI / API 配置」选择服务商并填入自己的密钥，或联系管理员。")
                raise QuotaExceeded(
                    f"您今日的 AI 使用次数已达上限（{per_user_limit} 次）；"
                    "按 UTC 日重置（北京时间每日 8:00）。如需更多额度请联系管理员。")
            if global_limit > 0 and used_global >= global_limit:
                raise QuotaExceeded(
                    "本站今日 AI 总用量已达上限，服务暂时熔断；"
                    "按 UTC 日重置（北京时间每日 8:00）。给您带来不便请联系管理员。")
            bucket[user_key] = used_user + 1
            bucket[global_key] = used_global + 1
            _save(path, data)
        except QuotaExceeded:
            raise
        except Exception:
            # 可用性优先：账本写不进绝不掀翻用户请求；provider 侧消费上限兜底。
            logger.error("llm_quota: 计数存储故障，本次放行", exc_info=True)


def usage_snapshot(project_root: Path, username: str, *, trial: bool,
                   quota_path: Path | None = None) -> int:
    """只读：该账号当日已用次数（前端展示「今日剩余」用；故障 → 0，绝不阻断）。"""
    path = quota_path or default_quota_path(project_root)
    user_key = f"u:{username}:trial" if trial else f"u:{username}"
    with _LOCK:
        try:
            bucket = _load(path)["days"].get(_utc_day(), {})
            return int(bucket.get(user_key, 0) or 0)
        except Exception:
            return 0
