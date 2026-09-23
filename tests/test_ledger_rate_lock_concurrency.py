# -*- coding: utf-8 -*-
"""账本并发写锁 + 限速锁 回归门（对抗评审第二轮转正：R2-3 P1-1、R2-8 P1-2/P1-3/P2-1）。

病形与验收口径：
  - `_append_jsonl`（corpus_net / corpus_curation 同构）：修复前 20 线程并发 append 丢行 7-13%
    且含撕裂行（Windows open("a") 的 seek-to-EOF+write 跨并发句柄非原子）。修复后持锁写，
    20 线程 × 50 行 = 1000 行必须零丢失、零撕裂（逐行 json.loads 可解析 + 唯一标记全到位）。
  - `corpus_curation._polite_wait`：修复前裸全局 check-then-set，8 线程 40 调用 35 次间隔违规
    （最小间隔 0.00ms）。修复后持锁 check-then-set，8 线程并发下完成时刻间隔必须 ≥0.2s。
  - `corpus_net._polite_wait`：修复前单次 sleep 在 Windows 上可提前返回（实测 187ms<200ms，
    单线程可复现）。修复后「睡到死线为止」循环，串行调用间隔也必须 ≥0.2s。

全程禁网、只写 tmp_path，不碰真实语料与 .userdata。
"""
from __future__ import annotations

import json
import threading
import time

from dataset_recommender.corpus import corpus_curation, corpus_net

# 量间隔留 5ms 余量吸收时间读取抖动：病形间隙是 0ms（无锁）或 187ms（sleep 提前返回），
# 修复后由锁内 monotonic 死线保证 ≥200ms，阈值 195ms 对两种病形都有决定性区分度。
_MIN_GAP_TOLERANCE = 0.005

# 并发变体专用余量（2026-08-30）：curation 并发用例的 stamp 在 `_polite_wait()` 返回
# （锁内死线已达）**之后**才 append——高负载 CI runner 上「返回→append」间的线程调度
# 延迟会吃进补号间距（windows-primary-full 实测 188ms < 200ms 误红）。该病形（无锁
# check-then-set）的间隙是 0.00ms，阈值放到 170ms 依然有决定性区分度；187ms 病形由
# corpus_net 串行用例以 5ms 余量单独看住，不在此处放宽。
_MIN_GAP_TOLERANCE_CONCURRENT = 0.03


def _hammer_append_jsonl(module, path, *, threads: int = 20, lines_per_thread: int = 50) -> None:
    def worker(tid: int) -> None:
        for seq in range(lines_per_thread):
            module._append_jsonl(path, {"tid": tid, "seq": seq})

    pool = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for th in pool:
        th.start()
    for th in pool:
        th.join()


def _assert_ledger_intact(path, *, expected: int) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == expected, f"丢行：应得 {expected} 行，实得 {len(lines)} 行"
    seen = set()
    for line in lines:
        entry = json.loads(line)  # 撕裂行会在这里直接打炸（病形之一）
        seen.add((entry["tid"], entry["seq"]))
    assert len(seen) == expected, "存在重复/覆盖行"


def test_append_jsonl_concurrent_zero_loss_corpus_net(tmp_path):
    """R2-3 P1-1 / R2-8 P1-3：corpus_net 账本 20 线程 × 50 行零丢失零撕裂。"""
    ledger = tmp_path / "ledger_net.jsonl"
    _hammer_append_jsonl(corpus_net, ledger)
    _assert_ledger_intact(ledger, expected=20 * 50)


def test_append_jsonl_concurrent_zero_loss_corpus_curation(tmp_path):
    """R2-3 P1-1 / R2-8 P1-3：corpus_curation 账本（含回收站 manifest 同函数）同款验收。"""
    ledger = tmp_path / "ledger_cc.jsonl"
    _hammer_append_jsonl(corpus_curation, ledger)
    _assert_ledger_intact(ledger, expected=20 * 50)


def test_curation_polite_wait_concurrent_zero_violation(monkeypatch):
    """R2-8 P1-2：8 线程并发 _polite_wait，完成时刻两两间隔不得 <0.2s（修复前最小 0.00ms）。"""
    # curation 已委托 corpus_net 的按 host 限速核；清共享状态而非重置退役的第二套时钟。
    monkeypatch.setattr(corpus_net, "_last_request_by_host", {})
    stamps: list[float] = []
    threads = 8
    calls_per_thread = 3

    def worker() -> None:
        for _ in range(calls_per_thread):
            corpus_curation._polite_wait()
            stamps.append(time.monotonic())  # list.append 原子；返回即记，贴近锁内记时

    pool = [threading.Thread(target=worker) for _ in range(threads)]
    for th in pool:
        th.start()
    for th in pool:
        th.join()

    assert len(stamps) == threads * calls_per_thread
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    worst = min(gaps)
    assert worst >= corpus_curation._MIN_REQUEST_INTERVAL - _MIN_GAP_TOLERANCE_CONCURRENT, (
        f"限速违规：最小间隔 {worst * 1000:.1f}ms < 200ms")


def test_corpus_net_polite_wait_sleeps_to_deadline(monkeypatch):
    """R2-8 P2-1：单线程串行 6 次 @0.2s，间隔必须睡到死线（修复前 Windows 提前返回 187ms）。"""
    monkeypatch.setattr(corpus_net, "_last_request_by_host", {})
    stamps = []
    for _ in range(6):
        corpus_net._polite_wait("regression-test-host", 0.2)
        stamps.append(time.monotonic())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    worst = min(gaps)
    assert worst >= 0.2 - _MIN_GAP_TOLERANCE, (
        f"sleep 提前返回欠隔：最小间隔 {worst * 1000:.1f}ms < 200ms")
