# -*- coding: utf-8 -*-
"""corpus 包文件级共享基元：旁挂台账缓存装载、跨进程文件锁。唯一锚点，勿复制变体。"""
from __future__ import annotations

import contextlib
import json
import os
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

# ==================================================================================================
# by-uid 旁挂台账装载器（downloads / sample_supplement / inspection 三台账同一骨架）
# ==================================================================================================


def make_sidecar_loader(
    *,
    data_path: Callable[[], str],
    shape_gate: Callable[[Any], Any],
    missing: Any,
) -> Callable[[], Any]:
    """by-uid 旁挂台账装载器骨架（唯一锚点）：返回 `load()`，且带 `load.cache_clear` 失效仪式。

    各台账只喂差异内容：`data_path`（路径解析，通常「现读环境变量、未设置回落 import 期快照」）、
    `shape_gate`（校验并归一文件内容）、`missing`（文件缺失时的稳定空值）。纪律随骨架一并生效：

    - 路径含进缓存键：`data_path` 每次加载现调，长驻进程内改环境变量即时生效，
      换路径自然换缓存条目。
    - 失败不入缓存：文件不存在 → 返回 `missing`（稳定状态，缓存合法）；读了但失败
      （坏 JSON / IO / 形状闸抛出）→ 异常传出不入缓存，由调用方降级——故障消除后同进程
      下次调用自动重试，不会把失败缓存到进程结束。
    """
    @lru_cache(maxsize=4)
    def load_sidecar_cached(path: str):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return missing
        return shape_gate(data)

    def load():
        try:
            return load_sidecar_cached(data_path())
        except Exception:
            return missing

    load.cache_clear = load_sidecar_cached.cache_clear  # 测试既有失效仪式（cache_clear）不变
    return load


def by_uid_shape(label_zh: str) -> Callable[[Any], "tuple[dict, dict]"]:
    """形状闸：顶层 dict（损坏时 ValueError 文案以 `label_zh` 开头）→ 值过滤为 dict → 派生 by_url。

    只保留值为 dict 的记录：即便文件被部分损坏（某条记录值退化成标量/列表），
    下游 getter 的 r.get(...) 也不会 AttributeError（守住「永不崩→降级」合同）。"""
    def gate(data: Any) -> "tuple[dict, dict]":
        if not isinstance(data, dict):
            raise ValueError(f"{label_zh}顶层不是 dict（文件损坏）")
        by_uid = {k: v for k, v in data.items() if isinstance(v, dict)}
        by_url = {r.get("url"): r for r in by_uid.values() if r.get("url")}
        return by_uid, by_url
    return gate


# ==================================================================================================
# 跨进程互斥文件锁（uploads 摄取锁 / curate 同步锁同一平台分支）
# ==================================================================================================


class FileLockBusy(RuntimeError):
    """`acquire_file_lock` 非阻塞被占、或等待超时仍未获得锁时抛出；句柄已关闭。
    调用方把它翻译成各自的领域错误（如 UploadError(lock_busy) / CurateError(sync_busy)）。"""


def acquire_file_lock(
    lock_path: "str | Path",
    *,
    wait: bool = True,
    timeout: float = 60.0,
    poll_interval: float = 0.1,
):
    """获取跨进程互斥文件锁并持有至 `release_file_lock`（stdlib only：msvcrt on Windows /
    fcntl on POSIX 的唯一锚点），返回打开的句柄。

    - wait=True：非阻塞尝试 + `poll_interval` 秒退避重试，至 `timeout` 秒仍未得 →
      FileLockBusy。即使 timeout=0 也先尝试一次（「先试再判」的循环形状）。
    - wait=False：单次尝试，被占立即 FileLockBusy，不等待不重试。

    锁文件按需创建、只创建不删除（删除与重建之间有竞态）；进程崩溃遗留的锁文件不永久
    busy——OS 层文件锁随进程消亡自动释放。"""
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("a+b")
    deadline = time.monotonic() + timeout
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fh
        except OSError:
            if not wait or time.monotonic() >= deadline:
                fh.close()
                raise FileLockBusy(f"文件锁被占用：{lock_path.name}") from None
            time.sleep(poll_interval)


def release_file_lock(fh) -> None:
    """释放 `acquire_file_lock` 拿到的锁并关闭句柄（解锁与加锁锁同一字节位：先 seek(0)）。"""
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


@contextlib.contextmanager
def hold_file_lock(
    lock_path: "str | Path",
    *,
    wait: bool = True,
    timeout: float = 60.0,
    poll_interval: float = 0.1,
):
    """`acquire_file_lock`/`release_file_lock` 的 with 形态：进入持锁、退出必释放。"""
    fh = acquire_file_lock(lock_path, wait=wait, timeout=timeout, poll_interval=poll_interval)
    try:
        yield fh
    finally:
        release_file_lock(fh)
