# -*- coding: utf-8 -*-
""" 死锁回归：vector_recall 模块级单飞锁自锁死锁。**全离线**。

背景：`load_cross_encoder`/`load_embedder` 持模块级 `_MODEL_LOCK`（threading.Lock，
**不可重入**）后锁内调 `_setup_determinism()`，而后者内部又 `with _MODEL_LOCK`——
冷启动（`_DETERMINISM_DONE=False`，run_web.py warm 路径首载）必自锁死锁（集成验证
CPU 0、>8 分钟无响应）。turn 请求路径因先无锁调 `_setup_determinism()` 才幸免，
所以既有测试没抓到。修复：两个 loader 先**无锁**调 `_setup_determinism()`（它自带
单飞锁），再持 `_MODEL_LOCK` 做 check-then-load——嵌套消失。

本文件断言：冷启动（清空 `_EMBEDDER_CACHE`/`_CROSS_CACHE`/确定性标志）在**超时守护**
下经 loader 完整走一遍不死锁；并发首载**单飞**（并发下只有一个真加载，其余命中缓存）。
负缓存纪律不变（只清正缓存与确定性标志，测试后原样恢复）。
"""
import sys
import threading
import types

import pytest

from dataset_recommender.retrieval import vector_recall as _vr


@pytest.fixture
def _cold_start(monkeypatch):
    """冷启动现场：清空正缓存 + 确定性标志；测试后原样恢复（含 _DETERMINISM_DONE）。"""
    saved = (dict(_vr._EMBEDDER_CACHE), dict(_vr._CROSS_CACHE), _vr._DETERMINISM_DONE)
    _vr._EMBEDDER_CACHE.clear()
    _vr._CROSS_CACHE.clear()
    _vr._DETERMINISM_DONE = False
    yield _vr
    _vr._EMBEDDER_CACHE.clear()
    _vr._EMBEDDER_CACHE.update(saved[0])
    _vr._CROSS_CACHE.clear()
    _vr._CROSS_CACHE.update(saved[1])
    _vr._DETERMINISM_DONE = saved[2]


@pytest.fixture
def _fake_st(monkeypatch):
    """假 sentence_transformers（sys.modules 注入，测试后还原）：让真代码路径走到
    loader 的锁区与 `_setup_determinism`（本次对象是锁结构，不是模型本体）。"""
    fake = types.ModuleType("sentence_transformers")
    created = {"cross": 0, "emb": 0}

    class _FakeCrossEncoder:
        def __init__(self, *a, **k):
            created["cross"] += 1

        def predict(self, pairs, **k):
            return [1.0] * len(pairs)

    class _FakeSentenceTransformer:
        def __init__(self, *a, **k):
            created["emb"] += 1

        def encode(self, texts, **k):
            return [[0.1] * 4 for _ in texts]

    fake.CrossEncoder = _FakeCrossEncoder
    fake.SentenceTransformer = _FakeSentenceTransformer
    monkeypatch.setitem(sys.modules, "sentence_transformers", fake)
    return created


def _assert_threads_finished(threads: list, what: str) -> None:
    for th in threads:
        th.join(timeout=10)
    alive = [th.name for th in threads if th.is_alive()]
    assert not alive, (
        f"{what} 死锁：线程 {alive} 10s 内未返回"
        "（_MODEL_LOCK 不可重入，锁内调 _setup_determinism 自锁）")


def test_cold_start_load_cross_encoder_no_deadlock(_cold_start, _fake_st, tmp_path):
    """ 回归：冷启动（清缓存+确定性标志）经 load_cross_encoder 完整走一遍——修复前
    单飞锁内调 `_setup_determinism` 自锁死锁（run_web.py warm 路径同款）；修复后不死锁，
    且确定性初始化确实执行（_DETERMINISM_DONE 置位）。"""
    model_dir = tmp_path / "ce"
    model_dir.mkdir()
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(_vr.load_cross_encoder(model_dir=model_dir)),
        daemon=True)
    t.start()
    _assert_threads_finished([t], "load_cross_encoder 冷启动")
    assert out and out[0] is not None, "假 CrossEncoder 加载应返回 scorer"
    assert _fake_st["cross"] == 1
    assert _vr._DETERMINISM_DONE is True, "_setup_determinism 应在冷启动首次加载时执行"


def test_cold_start_load_embedder_no_deadlock(_cold_start, _fake_st, tmp_path):
    """ 回归（embedder 侧）：冷启动经 load_embedder 完整走一遍不死锁（同款修复：
    无锁先调 `_setup_determinism` 再持锁 check-then-load）。"""
    model_dir = tmp_path / "emb"
    model_dir.mkdir()
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(_vr.load_embedder(model_dir=model_dir)),
        daemon=True)
    t.start()
    _assert_threads_finished([t], "load_embedder 冷启动")
    assert out and out[0] is not None, "假 SentenceTransformer 加载应返回 embedder"
    assert _fake_st["emb"] == 1
    assert _vr._DETERMINISM_DONE is True


def test_concurrent_first_load_cross_encoder_single_flight(_cold_start, _fake_st, tmp_path):
    """并发首载单飞：N 线程同时冷加载同一 cross-encoder → 只有一个真加载，其余命中
    同一缓存对象（单飞语义逐位不变，check-then-load 全程持锁）。"""
    model_dir = tmp_path / "ce"
    model_dir.mkdir()
    results: list = []

    def _load():
        results.append(_vr.load_cross_encoder(model_dir=model_dir))

    threads = [threading.Thread(target=_load, daemon=True, name=f"t{i}") for i in range(4)]
    for th in threads:
        th.start()
    _assert_threads_finished(threads, "并发首载 load_cross_encoder")
    assert _fake_st["cross"] == 1, "并发首载必须只有一个真加载（单飞）"
    assert len(results) == 4 and all(r is not None for r in results)
    assert len({id(r) for r in results}) == 1, "所有线程命中同一缓存 scorer"


def test_concurrent_first_load_embedder_single_flight(_cold_start, _fake_st, tmp_path):
    """并发首载单飞（embedder 侧）：同上，_EMBEDDER_CACHE 只有一个条目、一次真加载。"""
    model_dir = tmp_path / "emb"
    model_dir.mkdir()
    results: list = []

    def _load():
        results.append(_vr.load_embedder(model_dir=model_dir))

    threads = [threading.Thread(target=_load, daemon=True, name=f"t{i}") for i in range(4)]
    for th in threads:
        th.start()
    _assert_threads_finished(threads, "并发首载 load_embedder")
    assert _fake_st["emb"] == 1, "并发首载必须只有一个真加载（单飞）"
    assert len(results) == 4 and all(r is not None for r in results)
    assert len({id(r) for r in results}) == 1, "所有线程命中同一缓存 embedder"


def test_warmup_call_sequence_cold_start_no_deadlock(_cold_start, _fake_st, tmp_path):
    """warm 路径同款调用序：先 `_setup_determinism` 再 `load_cross_encoder`（run_web.py
    之外的另一 warm 调用方，turn 预热闭合）——冷启动同样不死锁、仍单飞。"""
    _vr._setup_determinism()  # turn 预热同款：先无锁 ensure（自带单飞锁）
    model_dir = tmp_path / "ce"
    model_dir.mkdir()
    out: list = []
    t = threading.Thread(
        target=lambda: out.append(_vr.load_cross_encoder(model_dir=model_dir)),
        daemon=True)
    t.start()
    _assert_threads_finished([t], "warm 调用序 load_cross_encoder")
    assert out and out[0] is not None
    assert _fake_st["cross"] == 1
