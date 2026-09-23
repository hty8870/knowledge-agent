# -*- coding: utf-8 -*-
"""无控制台桌面启动器 `desktop_launcher.py` 的契约测试。

覆盖（对照契约测试清单）：
1. 端口分配与持久化：首启 7860 可用即用并保存；7860 被无关服务占用 → 7861-7869
   漂移并永久保存；固定端口被占 → 明确报错不换端口；env PORT 一次性覆盖不持久化。
2. socket 先绑后交：bind_socket 返回已绑 socket；真实 uvicorn 0.51 经
   `Server.serve(sockets=[sock])` 接过预绑定 socket 可服务（uvicorn 集成实测）。
3. instance.json 原子写与损坏恢复：无 .tmp 残留；损坏/缺字段/schema 不符 → None；
   delete_if_pid 只删自己；过期记录被覆盖。
4. 二次启动 attach：PID/版本/install_root/health 全匹配才连；损坏/过期回退
   固定端口 health 探测；轮询等待补写。
5. 退出端口释放：uvicorn 关停后 5s 内端口可重绑；_shutdown 清理 socket/instance/
   mutex/托盘。
6. 日志滚动与脱敏：≤5MiB×5 文件；密钥/密码/Token/Bearer 落盘前脱敏。
7. mutex 同名常量 + 真实机制（二次打开 already_exists）。
8. 流守卫：stdout/stderr None → 空实现替换，print/write 不崩。
9. 托盘/浏览器/MessageBox 注入：fake 托盘收到状态流转；浏览器失败 → notify 且服务
   继续跑；托盘不可用降级轮询模式。

互不干扰：端口分配类测试用 monkeypatch 把 7860-7869 抬高到 17860-17869，避免与
本机真实服务撞端口；日志全局状态用 fixture 前后 reset_logging。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import sys
import threading
import time
import types
import uuid
from pathlib import Path

import pytest
import uvicorn

from dataset_recommender.app import desktop_launcher as dl
from dataset_recommender.app import desktop_launcher_win32 as win32
from dataset_recommender.app.desktop_launcher import (
    EXPECTED_SERVICE,
    HealthProbe,
    HealthResult,
    InstanceRecord,
    InstanceStore,
    NoPortAvailableError,
    PortOccupiedByOtherError,
    PortDecision,
    RuntimeStore,
    TrayHandlers,
    bind_socket,
    redact,
    resolve_port,
    try_attach,
)
from dataset_recommender.app.runtime_paths import AppPaths

TEST_VERSION = "2.4.0"
MUTCHED = HealthResult(ok=True, service=EXPECTED_SERVICE, version=TEST_VERSION, install_root="")

# 高位测试端口段（避开本机真实 7860+ 服务）
P_BASE = 17860


def _make_paths(tmp_path: Path, runtime_mode: str = "portable") -> AppPaths:
    """以 tmp_path 为根的便携布局 AppPaths（data 层可写、不碰仓库）。

    `runtime_mode` 可覆盖为 "frozen"/"source" 以构造对应模式路径（收紧判据测试用）。"""
    return AppPaths(
        install_root=tmp_path / "install",
        resource_root=tmp_path / "install",
        data_root=tmp_path / "data",
        config_root=tmp_path / "data" / "config",
        shipped_base_dir=tmp_path / "install" / "database" / "base",
        shipped_external_dir=tmp_path / "install" / "database" / "external",
        user_external_dir=tmp_path / "data" / "database" / "external",
        userdata_dir=tmp_path / "data" / ".userdata",
        model_root=tmp_path / "data" / "models",
        log_root=tmp_path / "data" / "logs",
        trace_root=tmp_path / "data" / "database" / "trace",
        export_root=tmp_path / "data" / "exports",
        run_root=tmp_path / "data" / "run",
        runtime_mode=runtime_mode,
    )


@pytest.fixture(autouse=True)
def _isolate_logging():
    """每个测试前后清 install_logging 管理的全局日志状态（跨测试不泄漏）。"""
    dl.reset_logging()
    yield
    dl.reset_logging()


@pytest.fixture(autouse=True)
def _high_test_ports(monkeypatch):
    """把默认/漂移端口段抬到 17860-17869（防与真实服务撞端口）。"""
    monkeypatch.setattr(dl, "DEFAULT_PORT", P_BASE)
    monkeypatch.setattr(dl, "DRIFT_PORT_START", P_BASE + 1)
    monkeypatch.setattr(dl, "DRIFT_PORT_END", P_BASE + 9)


class FakeHealth(HealthProbe):
    """probe 按端口表返回（未命中取 default）。"""

    def __init__(self, results=None, default=None):
        super().__init__(timeout=0.05)
        self.results = results or {}
        self.default = default

    def probe(self, port: int):
        return self.results.get(port, self.default)


class FakeMutex:
    def __init__(self, already_exists: bool = False):
        self.already_exists = already_exists
        self.handle = 1
        self.closed = False

    def close(self):
        self.closed = True


class FakeWin32:
    """注入用 win32 替身：记录 mutex 打开/关闭、剪贴板、目录打开、弹窗。"""

    def __init__(self, already_exists: bool = False):
        self.already_exists = already_exists
        self.mutexes: list[FakeMutex] = []
        self.copied: list[str] = []
        self.opened_dirs: list[str] = []
        self.messages: list[tuple[str, str]] = []

    def open_mutex(self, name):
        mutex = FakeMutex(already_exists=self.already_exists)
        self.mutexes.append(mutex)
        return mutex

    def is_pid_alive(self, pid: int) -> bool:
        return True

    def set_clipboard_text(self, text: str) -> bool:
        self.copied.append(text)
        return True

    def open_directory(self, path: str) -> bool:
        self.opened_dirs.append(path)
        return True

    def message_box(self, title: str, text: str) -> None:
        self.messages.append((title, text))


class FakeTray:
    """注入用托盘替身：状态事件可断言；run_message_loop 阻塞至 quit。"""

    def __init__(self, handlers: TrayHandlers):
        self.handlers = handlers
        self.events: list[tuple] = []
        self._loop_exit = threading.Event()

    def create(self, url: str, status: str) -> None:
        self.events.append(("create", url, status))

    def update_status(self, status: str) -> None:
        self.events.append(("status", status))

    def show_balloon(self, title: str, text: str) -> None:
        self.events.append(("balloon", title, text))

    def run_message_loop(self) -> None:
        self._loop_exit.wait()

    def quit_message_loop(self) -> None:
        self._loop_exit.set()

    def destroy(self) -> None:
        self.events.append(("destroy",))


def _occupy(port: int) -> socket.socket:
    """占住一个端口（真实监听 socket，无 SO_REUSEADDR）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", port))
    sock.listen(1)
    return sock


def _free_port() -> int:
    """找一个当前空闲的端口（bind 0 → 关闭 → 返回；本地小竞态可接受）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _mini_app(install_root: str, version: str = TEST_VERSION):
    """极简 ASGI app：/api/health 形状与 webapp 一致（真 HealthProbe 可探通）。"""

    async def app(scope, receive, send):
        assert scope["type"] == "http"
        body = json.dumps({
            "ok": True, "service": EXPECTED_SERVICE, "version": version,
            "install_root": install_root,
        }).encode("utf-8")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": body})

    return app


def _matching(install_root: str, version: str = TEST_VERSION,
              runtime_mode: str = "") -> HealthResult:
    return HealthResult(ok=True, service=EXPECTED_SERVICE, version=version,
                        install_root=install_root, runtime_mode=runtime_mode)


def _run_bg(launcher: dl.Launcher, argv=None) -> tuple[threading.Thread, dict]:
    """后台线程跑 launcher.run；rc_box 收集返回值/异常。"""
    box: dict = {}

    def _target():
        try:
            box["rc"] = launcher.run(argv)
        except BaseException as exc:  # noqa: BLE001（测试线程兜底）
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread, box


def _wait_tray_ready(tray_holder: dict, box: dict, timeout: float = 15.0) -> FakeTray:
    """等待托盘完成 create + 运行中（含服务真正就绪）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        tray = tray_holder.get("tray")
        if tray is not None and ("status", "运行中") in tray.events:
            return tray
        if "error" in box:
            raise AssertionError(f"launcher 提前异常: {box['error']}")
        if box.get("rc") is not None:
            raise AssertionError(f"launcher 提前退出 rc={box['rc']}")
        time.sleep(0.05)
    raise AssertionError("托盘/服务未在时限内就绪")


# ===========================================================================
# 1. 端口分配与持久化（契约 4）
# ===========================================================================
def test_resolve_port_first_run_uses_default_and_persists(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    decision = resolve_port(runtime_store=store, health=FakeHealth(),
                            expected_version=TEST_VERSION,
                            expected_install_root=str(tmp_path))
    assert decision.port == P_BASE
    assert decision.attach_url is None
    assert decision.persisted is True
    assert decision.sock is not None
    assert decision.sock.getsockname()[1] == P_BASE
    decision.sock.close()
    assert store.read_port() == P_BASE, "首启成功后必须持久化（恒用该端口）"


def test_resolve_port_drifts_when_default_occupied_and_persists(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    occupied = _occupy(P_BASE)
    try:
        decision = resolve_port(runtime_store=store, health=FakeHealth(),
                                expected_version=TEST_VERSION,
                                expected_install_root=str(tmp_path))
    finally:
        occupied.close()
    assert decision.port == P_BASE + 1, "7860 被占且非本产品 → 取 7861-7869 首个可用"
    assert decision.persisted is True
    decision.sock.close()
    assert store.read_port() == P_BASE + 1


def test_resolve_port_drift_picks_first_available(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    held = [_occupy(P_BASE), _occupy(P_BASE + 1), _occupy(P_BASE + 2)]
    try:
        decision = resolve_port(runtime_store=store, health=FakeHealth(),
                                expected_version=TEST_VERSION,
                                expected_install_root=str(tmp_path))
    finally:
        for s in held:
            s.close()
    assert decision.port == P_BASE + 3
    decision.sock.close()


def test_resolve_port_all_drift_ports_occupied_raises(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    held = [_occupy(p) for p in range(P_BASE, P_BASE + 10)]
    try:
        with pytest.raises(NoPortAvailableError):
            resolve_port(runtime_store=store, health=FakeHealth(),
                         expected_version=TEST_VERSION,
                         expected_install_root=str(tmp_path))
    finally:
        for s in held:
            s.close()


def test_resolve_port_fixed_port_free_uses_without_rewrite(tmp_path):
    runtime = tmp_path / "runtime.json"
    store = RuntimeStore(runtime)
    store.save_port(P_BASE + 5)
    before = runtime.read_text(encoding="utf-8")
    decision = resolve_port(runtime_store=store, health=FakeHealth(),
                            expected_version=TEST_VERSION,
                            expected_install_root=str(tmp_path))
    assert decision.port == P_BASE + 5
    assert decision.persisted is False, "固定端口已被保存过，无需重写"
    decision.sock.close()
    assert runtime.read_text(encoding="utf-8") == before, "runtime.json 不得被无谓重写"


def test_resolve_port_fixed_occupied_by_unrelated_raises_explicitly(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    store.save_port(P_BASE + 5)
    occupied = _occupy(P_BASE + 5)
    try:
        with pytest.raises(PortOccupiedByOtherError) as exc_info:
            resolve_port(runtime_store=store, health=FakeHealth(),
                         expected_version=TEST_VERSION,
                         expected_install_root=str(tmp_path))
    finally:
        occupied.close()
    assert exc_info.value.port == P_BASE + 5
    assert "静默更换" in str(exc_info.value), "固定端口被无关服务占用必须明确报错"


def test_resolve_port_fixed_occupied_by_own_product_attaches(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    store.save_port(P_BASE + 5)
    occupied = _occupy(P_BASE + 5)
    try:
        decision = resolve_port(
            runtime_store=store,
            health=FakeHealth(results={P_BASE + 5: _matching(str(tmp_path))}),
            expected_version=TEST_VERSION, expected_install_root=str(tmp_path))
    finally:
        occupied.close()
    assert decision.attach_url == f"http://127.0.0.1:{P_BASE + 5}"
    assert decision.sock is None, "attach 分支不得 bind 新 socket"
    assert decision.warning is None, "同根实例 attach 无警告"


def test_resolve_port_default_occupied_by_other_install_drifts(tmp_path):
    """回归：同版本但 install_root 不同的另一份安装占默认端口时，
    resolve_port 不得静默 attach（此前 attach 并警告「另一份安装」）——应漂移绑新端口，
    启动自己的实例（attach_url=None），并把漂移端口持久化到 runtime.json。"""
    store = RuntimeStore(tmp_path / "runtime.json")
    occupied = _occupy(P_BASE)
    other_root = str(tmp_path / "another-install")
    try:
        decision = resolve_port(
            runtime_store=store,
            health=FakeHealth(results={P_BASE: _matching(other_root, runtime_mode="frozen")}),
            expected_version=TEST_VERSION, expected_install_root=str(tmp_path),
            expected_runtime_mode="frozen")
    finally:
        occupied.close()
    assert decision.attach_url is None, "不同 install_root 的实例不得被吸附"
    assert decision.port == P_BASE + 1, "应漂移到默认端口后的首个可用端口"
    assert decision.sock is not None
    decision.sock.close()
    assert store.read_port() == P_BASE + 1, "漂移端口应持久化（后续恒用）"


def test_resolve_port_env_override_not_persisted(tmp_path):
    store = RuntimeStore(tmp_path / "runtime.json")
    decision = resolve_port(runtime_store=store, health=FakeHealth(),
                            expected_version=TEST_VERSION,
                            expected_install_root=str(tmp_path),
                            preferred_port=P_BASE + 9, allow_persist=False)
    assert decision.port == P_BASE + 9
    assert decision.persisted is False
    decision.sock.close()
    assert store.read_port() is None, "env 一次性覆盖不得写入 runtime.json"


# ===========================================================================
# 2. socket 先绑后交（契约 5，真实 uvicorn 集成）
# ===========================================================================
def test_bind_socket_prebound_on_loopback():
    port = _free_port()
    sock = bind_socket(port)
    assert sock is not None
    assert sock.getsockname() == ("127.0.0.1", port)
    assert sock.getsockname()[1] == port
    # 占用期间其他 bind 必须失败（无 SO_REUSEADDR）
    assert bind_socket(port) is None
    sock.close()
    assert bind_socket(port) is not None, "关闭后立即重绑（退出即释放）"


def test_uvicorn_config_has_no_sock_kwarg_in_051():
    """文档化 venv uvicorn 0.51.0 行为：Config 不支持 sock（契约走 Server 通道）。"""
    with pytest.raises(TypeError):
        uvicorn.Config(_mini_app("x"), sock=socket.socket(socket.AF_INET, socket.SOCK_STREAM))


def test_prebound_socket_handed_to_uvicorn_serves_and_releases(tmp_path):
    """真实 uvicorn 经 Server.serve(sockets=[预绑定 sock]) 服务 + 退出 5s 内释放端口。"""
    port = _free_port()
    sock = bind_socket(port)
    assert sock is not None
    app = _mini_app(str(tmp_path / "install"))
    config = uvicorn.Config(app, host=dl.HOST, port=port, log_level="info",
                            log_config=None, timeout_graceful_shutdown=1.0)
    server = uvicorn.Server(config)

    def _serve():
        asyncio.run(server.serve(sockets=[sock]))

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not getattr(server, "started", False) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "uvicorn 未在预绑定 socket 上启动"
    hit = HealthProbe(timeout=1.0).probe(port)
    assert hit is not None and hit.ok and hit.service == EXPECTED_SERVICE, \
        "真实 urllib 健康探测应穿透预绑定 socket 通路"
    server.should_exit = True
    thread.join(timeout=10)
    assert not thread.is_alive()
    t0 = time.monotonic()
    rebind = None
    while time.monotonic() - t0 < 5.0:
        rebind = bind_socket(port)
        if rebind is not None:
            break
        time.sleep(0.05)
    assert rebind is not None, "退出后 5 秒内端口必须可重绑"
    rebind.close()


# ===========================================================================
# 3. instance.json 原子写与损坏恢复（契约 6）
# ===========================================================================
def _record(pid: int = 12345, port: int = P_BASE, version: str = TEST_VERSION,
            install_root: str = "C:\\install") -> InstanceRecord:
    return InstanceRecord(schema=dl.INSTANCE_SCHEMA, pid=pid, port=port, version=version,
                          install_root=install_root, started_at="2026-08-20T00:00:00+08:00")


def test_instance_store_atomic_write_and_read(tmp_path):
    store = InstanceStore(tmp_path / "instance.json")
    rec = _record(pid=os.getpid(), port=P_BASE + 1, install_root=str(tmp_path))
    store.write(rec)
    assert store.path.is_file()
    assert not store.path.with_name("instance.json.tmp").exists(), "临时文件必须被清理"
    parsed = json.loads(store.path.read_text(encoding="utf-8"))
    assert parsed["schema"] == dl.INSTANCE_SCHEMA
    assert parsed["pid"] == os.getpid() and parsed["port"] == P_BASE + 1
    loaded = store.read()
    assert loaded == rec


def test_instance_store_read_corrupt_and_invalid(tmp_path):
    store = InstanceStore(tmp_path / "instance.json")
    assert store.read() is None, "缺失 → None"
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.read() is None, "损坏 JSON → None"
    store.path.write_text(json.dumps({"schema": "bogus/1", "pid": 1}), encoding="utf-8")
    assert store.read() is None, "schema 不符 → None"
    store.path.write_text(json.dumps({"schema": dl.INSTANCE_SCHEMA, "pid": "x"}), encoding="utf-8")
    assert store.read() is None, "字段类型非法 → None"


def test_instance_store_delete_only_own_pid(tmp_path):
    store = InstanceStore(tmp_path / "instance.json")
    store.write(_record(pid=111))
    store.delete_if_pid(222)
    assert store.path.exists(), "其他 PID 的记录不得删除"
    store.delete_if_pid(111)
    assert not store.path.exists()


def test_instance_store_overwrite_replaces_stale(tmp_path):
    store = InstanceStore(tmp_path / "instance.json")
    store.write(_record(pid=999, port=P_BASE))
    store.write(_record(pid=os.getpid(), port=P_BASE + 2, install_root=str(tmp_path)))
    loaded = store.read()
    assert loaded.pid == os.getpid() and loaded.port == P_BASE + 2


# ===========================================================================
# 4. 二次启动 attach（契约 7：PID/health 验证 mock）
# ===========================================================================
def _attach_env(tmp_path, *, instance_rec=None, runtime_port=None, health=None,
                alive=True):
    paths = _make_paths(tmp_path)
    runtime = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
    if runtime_port is not None:
        runtime.save_port(runtime_port)
    instances = InstanceStore(paths.run_root / dl.INSTANCE_FILENAME)
    if instance_rec is not None:
        instances.write(instance_rec)
    health = health or FakeHealth(default=None)
    opened: list[str] = []
    return paths, runtime, instances, health, opened


def test_try_attach_valid_instance_opens_url(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    rec = _record(pid=4242, port=P_BASE + 4, install_root=str(paths.install_root))
    instances.write(rec)
    health = FakeHealth(results={P_BASE + 4: _matching(str(paths.install_root))})
    ok, url = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                         expected_version=TEST_VERSION,
                         expected_install_root=str(paths.install_root),
                         is_pid_alive=lambda pid: True, open_url=opened.append,
                         timeout=0.5, poll=0.02)
    assert ok is True and url == f"http://127.0.0.1:{P_BASE + 4}"
    assert opened == [url]


def test_try_attach_dead_pid_not_attached(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    instances.write(_record(pid=4242, port=P_BASE + 4, install_root=str(paths.install_root)))
    health = FakeHealth(results={P_BASE + 4: _matching(str(paths.install_root))})
    ok, _ = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                       expected_version=TEST_VERSION,
                       expected_install_root=str(paths.install_root),
                       is_pid_alive=lambda pid: False, open_url=opened.append,
                       timeout=0.3, poll=0.02)
    assert ok is False and opened == [], "PID 死亡 → 不盲信记录、不打开"


def test_try_attach_version_mismatch_not_attached(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    instances.write(_record(pid=4242, port=P_BASE + 4, install_root=str(paths.install_root),
                            version="9.9.9"))
    health = FakeHealth(results={P_BASE + 4: _matching(str(paths.install_root))})
    ok, _ = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                       expected_version=TEST_VERSION,
                       expected_install_root=str(paths.install_root),
                       is_pid_alive=lambda pid: True, open_url=opened.append,
                       timeout=0.3, poll=0.02)
    assert ok is False and opened == [], "版本不符 → 不吸附"


def test_try_attach_install_root_mismatch_not_attached(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    instances.write(_record(pid=4242, port=P_BASE + 4, install_root=str(tmp_path / "other")))
    health = FakeHealth(results={P_BASE + 4: _matching(str(paths.install_root))})
    ok, _ = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                       expected_version=TEST_VERSION,
                       expected_install_root=str(paths.install_root),
                       is_pid_alive=lambda pid: True, open_url=opened.append,
                       timeout=0.3, poll=0.02)
    assert ok is False and opened == [], "install_root 不符 → 不吸附（另一份安装）"


def test_try_attach_corrupt_record_recovers_via_fixed_port_health(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    instances.path.parent.mkdir(parents=True, exist_ok=True)
    instances.path.write_text("{ 损坏", encoding="utf-8")
    runtime.save_port(P_BASE + 6)
    health = FakeHealth(results={P_BASE + 6: _matching(str(paths.install_root))})
    ok, url = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                         expected_version=TEST_VERSION,
                         expected_install_root=str(paths.install_root),
                         is_pid_alive=lambda pid: True, open_url=opened.append,
                         timeout=0.5, poll=0.02)
    assert ok is True and url == f"http://127.0.0.1:{P_BASE + 6}"
    assert opened == [url], "损坏记录回退到固定端口 health 探测"


def test_try_attach_polls_for_late_instance_json(tmp_path):
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    health = FakeHealth(results={P_BASE + 4: _matching(str(paths.install_root))})

    def _write_late():
        time.sleep(0.2)
        instances.write(_record(pid=4242, port=P_BASE + 4, install_root=str(paths.install_root)))

    threading.Thread(target=_write_late, daemon=True).start()
    ok, url = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                         expected_version=TEST_VERSION,
                         expected_install_root=str(paths.install_root),
                         is_pid_alive=lambda pid: True, open_url=opened.append,
                         timeout=2.0, poll=0.02)
    assert ok is True and url == f"http://127.0.0.1:{P_BASE + 4}", "轮询等到补写的 instance.json"


# ===========================================================================
# 5. 退出端口释放与 _shutdown 清理（契约 9）
# ===========================================================================
def test_shutdown_cleans_socket_instance_mutex_tray(tmp_path):
    paths = _make_paths(tmp_path)
    port = _free_port()
    sock = bind_socket(port)
    assert sock is not None
    store = InstanceStore(paths.run_root / dl.INSTANCE_FILENAME)
    store.write(_record(pid=os.getpid(), port=port, install_root=str(paths.install_root)))
    fake_win = FakeWin32()
    tray = FakeTray(TrayHandlers(on_open=lambda: None, on_open_logs=lambda: None,
                                 on_copy=lambda: None, on_quit=lambda: None))
    launcher = dl.Launcher(paths=paths, win32=fake_win)
    server = types.SimpleNamespace(started=True, should_exit=False, force_exit=False)
    rc = launcher._shutdown(server, sock, store, FakeMutex(), tray, exit_code=0)
    assert rc == 0
    assert bind_socket(port) is not None, "_shutdown 后端口必须释放"
    assert not store.path.exists(), "本 PID 的 instance.json 必须删除"
    assert ("destroy",) in tray.events


def test_shutdown_keeps_foreign_instance_file(tmp_path):
    paths = _make_paths(tmp_path)
    store = InstanceStore(paths.run_root / dl.INSTANCE_FILENAME)
    store.write(_record(pid=99999, port=P_BASE, install_root=str(paths.install_root)))
    launcher = dl.Launcher(paths=paths, win32=FakeWin32())
    rc = launcher._shutdown(types.SimpleNamespace(should_exit=False), None, store,
                            FakeMutex(), None, exit_code=0)
    assert rc == 0
    assert store.path.exists(), "非本 PID 的 instance.json 不得删除"


# ===========================================================================
# 6. 日志滚动与脱敏（契约 2）
# ===========================================================================
def test_redact_secret_patterns():
    # 假 key 按 test_secret_scan 成例用拼接构造：源码不出现可被交付扫描命中的完整 sk-{20+} 字面
    FAKE_SK = "sk-" + "abcdefghijklmnopqrstuvwxyz123"
    cases = [
        ("LLM_API_KEY=sk-abc1234567890abc123", "sk-abc1234567890abc123"),
        ("api_key = sk-9999888877776666", "sk-9999888877776666"),
        ("password=hunter2", "hunter2"),
        ("Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abcdefgh", "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"),
        ("token=abcdef123456", "abcdef123456"),
        ("CLIENT_SECRET=xYz0123456789", "xYz0123456789"),
        ("裸 sk-token " + FAKE_SK, FAKE_SK),
    ]
    for line, secret in cases:
        out = redact(line)
        assert secret not in out, f"{line!r} 泄漏了 {secret!r}"
        assert "<redacted>" in out, f"{line!r} 未掩码"


def test_redact_keeps_innocent_text():
    line = "GET /api/recommend?dataset_id=123 HTTP/1.1 200 正常日志"
    assert redact(line) == line, "普通文本不得被误掩"


def test_install_logging_files_and_rotation_params(tmp_path):
    info = dl.install_logging(tmp_path)
    assert (tmp_path / dl.LAUNCHER_LOG_NAME).is_file()
    assert (tmp_path / dl.WEB_LOG_NAME).is_file()
    launcher_handler = logging.getLogger(dl.LAUNCHER_LOGGER).handlers[0]
    assert isinstance(launcher_handler, logging.handlers.RotatingFileHandler)
    assert launcher_handler.maxBytes == 5 * 1024 * 1024, "单文件 ≤5MiB"
    assert launcher_handler.backupCount == 4, "1+4 = 最多 5 个文件"


def test_log_rotation_keeps_at_most_five_files(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "LOG_MAX_BYTES", 32 * 1024)
    dl.install_logging(tmp_path)
    lg = logging.getLogger(dl.LAUNCHER_LOGGER)
    for i in range(300):  # ~2.1KB/条 × 300 → 多次滚动
        lg.info("%06d %s", i, "z" * 2000)
    dl.reset_logging()
    files = sorted(p for p in tmp_path.iterdir() if p.name.startswith(dl.LAUNCHER_LOG_NAME))
    assert len(files) <= 5, f"最多 5 个文件，实际 {len(files)}: {[f.name for f in files]}"
    assert (tmp_path / f"{dl.LAUNCHER_LOG_NAME}.1").exists(), "必须发生滚动"
    for f in files:
        assert f.stat().st_size <= 32 * 1024 + 4096, f"{f.name} 超出单文件上限"


def test_log_file_redaction(tmp_path):
    dl.install_logging(tmp_path)
    lg = logging.getLogger(dl.LAUNCHER_LOGGER)
    lg.info("配置完成 LLM_API_KEY=sk-super-secret-key-12345 与 password=topsecret")
    lg.info("Authorization: Bearer abcdEFGH0123ijkl4567")
    dl.reset_logging()
    content = (tmp_path / dl.LAUNCHER_LOG_NAME).read_text(encoding="utf-8")
    assert "sk-super-secret" not in content
    assert "topsecret" not in content
    assert "abcdEFGH0123ijkl4567" not in content
    assert content.count("<redacted>") >= 3, "三处敏感值都该被掩码"


def test_uvicorn_logs_land_in_web_log(tmp_path):
    dl.install_logging(tmp_path)
    lg = logging.getLogger("uvicorn.error")
    lg.info("uvicorn 测试行 %s", "WEB-ACCESS-MARKER")
    dl.reset_logging()
    content = (tmp_path / dl.WEB_LOG_NAME).read_text(encoding="utf-8")
    assert "WEB-ACCESS-MARKER" in content, "uvicorn 日志应进 web.log"


def test_webview_shell_logs_land_in_launcher_log(tmp_path):
    """biodata.webview_shell（桌面壳层）的 warning 必须落 launcher.log——装版回退
    原因日志（pywebview 不可用 / 未检测到 WebView2 / 桌面窗口启动失败）此前未接入，
    frozen windowed 下 propagate 到无 handler 的 root 而丢失，导致回退无原因行。"""
    dl.install_logging(tmp_path)
    lg = logging.getLogger("biodata.webview_shell")
    lg.warning("pywebview 不可用（ModuleNotFoundError: No module named 'webview'），"
               "回退系统浏览器。SHELL-FALLBACK-MARKER")
    lg.warning("桌面窗口启动失败，回退系统浏览器。")
    dl.reset_logging()
    content = (tmp_path / dl.LAUNCHER_LOG_NAME).read_text(encoding="utf-8")
    assert "SHELL-FALLBACK-MARKER" in content, "webview_shell 警告应进 launcher.log"
    assert "桌面窗口启动失败" in content, "webview_shell 多条警告都应落 launcher.log"


def test_webview_shell_logger_is_managed_and_cleared(tmp_path):
    """reset_logging 后 webview_shell 的 handler 清空、propagate 恢复（测试隔离语义）。"""
    dl.install_logging(tmp_path)
    lg = logging.getLogger("biodata.webview_shell")
    assert lg.handlers and isinstance(lg.handlers[0], logging.handlers.RotatingFileHandler)
    assert lg.propagate is False
    dl.reset_logging()
    assert lg.handlers == [], "reset_logging 必须清空 webview_shell 的 handler"
    assert lg.propagate is True


def test_webview_shell_logs_land_in_launcher_log_on_fallback(tmp_path, monkeypatch):
    """日志目录不可写时，webview_shell logger 与 launcher 一同降级到 stderr（不静默）。"""
    def deny_mkdir(self, *args, **kwargs):
        raise OSError("磁盘满")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    info = dl.install_logging(tmp_path / "logs")
    assert info["launcher_path"] == "" and info["web_path"] == ""
    lg = logging.getLogger("biodata.webview_shell")
    assert lg.handlers and isinstance(lg.handlers[0], logging.StreamHandler), \
        "降级后 webview_shell 必须仍有 handler（stderr），回退原因不得静默丢失"


# ── LOCALAPPDATA 不可写 / 磁盘满：不静默秒退（安装器边缘修复第 1 项）──
def test_install_logging_falls_back_to_stderr_when_dir_unwritable(tmp_path, monkeypatch):
    """日志目录建不出来 → 降级 stderr，返回空路径；不抛 OSError。"""
    real_mkdir = Path.mkdir

    def deny_mkdir(self, *args, **kwargs):
        if str(self).startswith(str(tmp_path)):
            raise OSError("磁盘满")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    info = dl.install_logging(tmp_path / "logs")
    assert info["launcher_path"] == "" and info["web_path"] == ""
    lg = logging.getLogger(dl.LAUNCHER_LOGGER)
    assert lg.handlers and isinstance(lg.handlers[0], logging.StreamHandler), \
        "降级后必须仍有 handler（stderr），不能静默无输出"


def test_install_logging_falls_back_to_stderr_on_handler_failure(tmp_path, monkeypatch):
    """RotatingFileHandler 建不出来（磁盘满）→ 降级 stderr，返回空路径；不抛。"""

    def boom(*args, **kwargs):
        raise OSError("磁盘满")

    monkeypatch.setattr(logging.handlers, "RotatingFileHandler", boom)
    info = dl.install_logging(tmp_path)
    assert info["launcher_path"] == "" and info["web_path"] == ""
    lg = logging.getLogger(dl.LAUNCHER_LOGGER)
    assert lg.handlers and isinstance(lg.handlers[0], logging.StreamHandler)


def test_atomic_write_json_swallows_oserror_and_warns(tmp_path, monkeypatch, capsys):
    """runtime/instance 写盘失败不抛：stderr 有提示，进程继续。"""
    target = tmp_path / "run" / "runtime.json"

    def deny_mkdir(self, *args, **kwargs):
        raise OSError("磁盘满")

    monkeypatch.setattr(Path, "mkdir", deny_mkdir)
    dl._atomic_write_json(target, {"port": 7860})   # 不抛即通过
    err = capsys.readouterr().err
    assert "无法写入" in err and "runtime.json" in err


def test_bootstrap_excepthook_writes_stderr_without_messagebox(monkeypatch, capsys):
    """最早 excepthook：非交互桌面 → 只写 stderr、不弹框、绝不抛。"""
    orig_hook = sys.excepthook
    monkeypatch.setattr(dl._win32, "is_interactive", lambda: False)
    boxes: list = []
    monkeypatch.setattr(dl._win32, "message_box", lambda t, x: boxes.append((t, x)))

    def boom():
        raise RuntimeError("引导期崩溃")

    dl._install_bootstrap_excepthook()
    try:
        boom()
    except RuntimeError:
        sys.excepthook(*sys.exc_info())
    finally:
        sys.excepthook = orig_hook
    err = capsys.readouterr().err
    assert "RuntimeError" in err and "引导期崩溃" in err
    assert boxes == []   # 非交互桌面绝不弹框（防无头挂起）


# ===========================================================================
# 7. mutex 同名常量 + 真实机制（契约 3）
# ===========================================================================
def test_mutex_constant_matches_inno_appmutex():
    assert dl.MUTEX_NAME == r"Local\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79"


def test_real_mutex_second_open_already_exists():
    name = rf"Local\BioDataAgent.Test.{uuid.uuid4().hex}"
    m1 = win32.open_mutex(name)
    m2 = win32.open_mutex(name)
    try:
        assert not m1.already_exists, "首开：mutex 新建"
        assert m2.already_exists, "二开：已被占用"
    finally:
        m1.close()
        m2.close()
    m3 = win32.open_mutex(name)
    try:
        assert not m3.already_exists, "关闭后释放，再次打开为新建"
    finally:
        m3.close()


# ===========================================================================
# 8. 流守卫（契约 11）
# ===========================================================================
def test_stream_guard_replaces_none_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    dl._guard_streams()
    assert sys.stdout is not None and sys.stderr is not None
    print("不可见但不应崩", file=sys.stdout)
    sys.stdout.write("x")
    sys.stdout.flush()
    assert sys.stdout.isatty() is False


def test_stream_guard_preserves_existing_streams(monkeypatch):
    original = sys.stdout
    dl._guard_streams()
    assert sys.stdout is original


# ===========================================================================
# 9. Launcher 端到端（可注入交互面 + 真实 uvicorn/socket）
# ===========================================================================
def _make_launcher(paths, *, fake_win=None, health=None, browser=None, notify=None,
                   version=TEST_VERSION, app=None, tray_factory=None, warm=None,
                   no_tray=False, argv=None, attach_timeout=0.5, **kwargs):
    fake_win = fake_win or FakeWin32()
    tray_holder: dict = {}
    opened: list[str] = []
    notified: list[tuple[str, str]] = []

    def _tray_factory(handlers):
        tray = tray_factory(handlers) if tray_factory else FakeTray(handlers)
        tray_holder["tray"] = tray
        return tray

    launcher = dl.Launcher(
        paths=paths, win32=fake_win,
        health=health or FakeHealth(default=None),
        browser=browser or (lambda url: opened.append(url) or True),
        notify=notify or (lambda t, x: notified.append((t, x))),
        version=version, app=app or _mini_app(str(paths.install_root), version),
        tray_factory=_tray_factory,
        warm=warm or (lambda log: "unavailable"),
        attach_timeout=attach_timeout,
        **kwargs,
    )
    return launcher, tray_holder, opened, notified


def test_launcher_end_to_end_clean_exit(tmp_path):
    """主实例全链路：端口先绑 → instance.json → 托盘状态流 → 浏览器 → 退出清理。"""
    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)
    try:
        fake_win = FakeWin32()
        launcher, tray_holder, opened, notified = _make_launcher(paths, fake_win=fake_win)
        thread, box = _run_bg(launcher, [])
        tray = _wait_tray_ready(tray_holder, box)
        assert opened == [f"http://127.0.0.1:{port}"], "浏览器应在服务就绪后打开"
        assert ("create", f"http://127.0.0.1:{port}", "正在启动") in tray.events
        assert ("status", "运行中") in tray.events
        # 服务真实可探（走预绑定 socket 通路）
        hit = HealthProbe(timeout=1.0).probe(port)
        assert hit is not None and hit.ok and hit.install_root == str(paths.install_root)
        assert launcher._server.config.workers == 1, "桌面实例必须显式锁定单 worker"
        rec = launcher._instance_store.read()
        assert rec is not None and rec.pid == os.getpid() and rec.port == port
        assert rec.install_root == str(paths.install_root)
        # 托盘「退出」
        tray.handlers.on_quit()
        thread.join(timeout=15)
        assert "error" not in box and box.get("rc") == 0, box
        assert not launcher._instance_store.path.exists(), "退出必须删除本 PID 的 instance.json"
        assert bind_socket(port) is not None, "退出后端口已释放"
        assert ("destroy",) in tray.events
        assert all(m.closed for m in fake_win.mutexes), "mutex 必须释放"
    finally:
        monkeypatch.undo()


def test_launcher_browser_failure_notifies_service_keeps_running(tmp_path):
    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)
    try:
        notified: list[tuple[str, str]] = []
        launcher, tray_holder, opened, _ = _make_launcher(
            paths, browser=lambda url: opened.append(url) or False,
            notify=lambda t, x: notified.append((t, x)))
        thread, box = _run_bg(launcher, [])
        tray = _wait_tray_ready(tray_holder, box)
        # 浏览器失败 → 简洁提示（含可复制 URL），服务继续跑
        assert opened and opened[0] == f"http://127.0.0.1:{port}"
        assert notified and notified[0][0] == "浏览器打开失败"
        assert f"http://127.0.0.1:{port}" in notified[0][1]
        hit = HealthProbe(timeout=1.0).probe(port)
        assert hit is not None and hit.ok, "浏览器失败后服务必须继续运行"
        tray.handlers.on_quit()
        thread.join(timeout=15)
        assert box.get("rc") == 0, box
    finally:
        monkeypatch.undo()


def test_launcher_no_tray_poll_mode_clean_exit(tmp_path):
    """托盘不可用（无桌面）→ 降级轮询模式，绝不阻断启动。"""
    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)
    try:
        def _boom(handlers):
            raise dl.TrayUnavailable("no desktop")

        launcher, _, _, _ = _make_launcher(paths, tray_factory=_boom)
        thread, box = _run_bg(launcher, [])
        deadline = time.monotonic() + 15
        while launcher._server is None or not getattr(launcher._server, "started", False):
            if "error" in box:
                raise AssertionError(box["error"])
            assert time.monotonic() < deadline, "无托盘模式服务未就绪"
            time.sleep(0.05)
        assert HealthProbe(timeout=1.0).probe(port) is not None
        launcher._server.should_exit = True  # 轮询路径的退出信号
        thread.join(timeout=15)
        assert box.get("rc") == 0, box
        assert bind_socket(port) is not None
    finally:
        monkeypatch.undo()


def test_launcher_startup_failure_notifies_and_cleans(tmp_path):
    """真正的服务启动失败（serve 线程抛异常退出）→ 明确提示 + 清理 + rc=1。

    warm 失败不再属于启动失败（第 5 项解耦后 warm 后台化），此处用假 uvicorn.Server
    让 serve 立即抛异常来构造真实的启动失败。"""
    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)

    class FakeServer:
        should_exit = False
        started = False
        force_exit = False

        async def serve(self, sockets=None):
            raise RuntimeError("serve boom")

    monkeypatch.setattr(dl.uvicorn, "Server", lambda config: FakeServer())
    try:
        launcher, tray_holder, _, notified = _make_launcher(paths, warm=lambda log: "unavailable")
        thread, box = _run_bg(launcher, [])
        thread.join(timeout=15)
        assert box.get("rc") == 1, box
        tray = tray_holder.get("tray")
        assert tray is not None and ("status", "启动失败") in tray.events
        assert notified and notified[0][0] == "BioData Agent 启动失败"
        assert not launcher._instance_store.path.exists(), "失败路径也要清理 instance.json"
        assert bind_socket(port) is not None, "失败路径端口已释放"
    finally:
        monkeypatch.undo()


def test_launcher_warm_failure_does_not_fail_startup(tmp_path):
    """第 5 项：预热在后台跑，warm 抛异常也不得导致启动失败——服务照常就绪。"""
    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)
    try:
        def _bad_warm(log):
            raise RuntimeError("model warm boom")

        launcher, tray_holder, _, _ = _make_launcher(paths, warm=_bad_warm)
        thread, box = _run_bg(launcher, [])
        tray = _wait_tray_ready(tray_holder, box)
        assert ("status", "运行中") in tray.events, "预热失败不得导致启动失败"
        assert HealthProbe(timeout=1.0).probe(port) is not None
        tray.handlers.on_quit()
        thread.join(timeout=15)
        assert box.get("rc") == 0, box
    finally:
        monkeypatch.undo()


def test_launcher_second_instance_attaches_existing(tmp_path):
    """契约 7：mutex 已被占 → instance.json+PID+health 全匹配（runtime_mode 同）→ 打开现有 URL 退出。"""
    paths = _make_paths(tmp_path)
    fake_win = FakeWin32(already_exists=True)
    store = InstanceStore(paths.run_root / dl.INSTANCE_FILENAME)
    store.write(_record(pid=4242, port=P_BASE + 7, install_root=str(paths.install_root)))
    health = FakeHealth(results={P_BASE + 7: _matching(str(paths.install_root), runtime_mode="portable")})
    opened: list[str] = []
    launcher = dl.Launcher(
        paths=paths, win32=fake_win, health=health,
        browser=lambda url: opened.append(url) or True, version=TEST_VERSION,
        app=_mini_app(str(paths.install_root)), warm=lambda log: "unavailable",
        attach_timeout=1.0, attach_poll=0.02,
        # 环境依赖根除：is_pid_alive/notify 缺省落真实系统——集成没有 PID
        # 4242 时走「PID 已死→attach 放弃→_default_notify 弹真模态框」，无人点击则全量
        # 套件挂死在本用例。注入后 attach 必走 FakeHealth 成功路径；万一失败也断言报错。
        is_pid_alive=lambda pid: True,
        notify=lambda t, x: (_ for _ in ()).throw(AssertionError(f"本用例不应触发通知: {t} {x}")),
    )
    rc = launcher.run([])
    assert rc == 0
    assert opened == [f"http://127.0.0.1:{P_BASE + 7}"], "应打开现有实例 URL"
    assert bind_socket(P_BASE + 7) is not None, "不得启动第二个服务（端口必须空闲）"


def test_launcher_second_instance_unlocatable_errors(tmp_path):
    """二次启动但 instance.json 损坏/过期且端口探不到 → 明确错误，不静默乱连。"""
    paths = _make_paths(tmp_path)
    fake_win = FakeWin32(already_exists=True)
    store = InstanceStore(paths.run_root / dl.INSTANCE_FILENAME)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ 损坏", encoding="utf-8")
    notified: list[tuple[str, str]] = []
    launcher = dl.Launcher(
        paths=paths, win32=fake_win, health=FakeHealth(default=None),
        browser=lambda url: True, version=TEST_VERSION,
        app=_mini_app(str(paths.install_root)), warm=lambda log: "unavailable",
        attach_timeout=0.3, attach_poll=0.02,
        notify=lambda t, x: notified.append((t, x)),
    )
    rc = launcher.run([])
    assert rc == 1
    assert notified and "已在运行" in notified[0][0], f"必须给出明确提示: {notified}"


def test_launcher_frozen_does_not_attach_source_on_default_port(tmp_path):
    """回归：默认端口 7860 上是**同版本 source 实例**（runtime_mode='source'，即本机 dev
    server）时，frozen 启动器无 PORT 首启不得 attach——此前 `HealthProbe.matches` 只比
    ok/service/version，把 source 服务误判为「本产品可 attach 实例」，随后在 attach 分支弹
    模态框卡死、runtime.json/instance.json 不写。收紧后应识破 mode 不同 → 漂移绑新端口，
    自己启动（rc=0、不开浏览器）。"""
    paths = _make_paths(tmp_path, runtime_mode="frozen")
    occupied = _occupy(P_BASE)
    try:
        launcher, tray_holder, opened, _ = _make_launcher(
            paths,
            health=FakeHealth(results={P_BASE: _matching(str(tmp_path / "dev-src"), runtime_mode="source")}),
        )
        thread, box = _run_bg(launcher, [])
        tray = _wait_tray_ready(tray_holder, box)
        # 启动器应在漂移端口上写了自己的一份 instance.json，并在该端口打开浏览器——
        # 绝不开 source 实例所在的默认端口（P_BASE）。
        rec = launcher._instance_store.read()
        assert rec is not None and rec.pid == os.getpid(), "启动器应写自己的 instance.json"
        assert rec.port > P_BASE, f"应漂移到默认端口之后的端口：{rec.port}"
        assert opened == [f"http://127.0.0.1:{rec.port}"], \
            f"不得 attach source 实例；应打开自启漂移端口地址：{opened}"
        assert ("status", "运行中") in tray.events
        tray.handlers.on_quit()
        thread.join(timeout=15)
        assert "error" not in box and box.get("rc") == 0, box
    finally:
        occupied.close()


def test_launcher_fixed_port_occupied_by_unrelated_errors(tmp_path):
    """固定端口后来被无关服务占用 → 明确错误，不静默换端口（Launcher 层）。"""
    paths = _make_paths(tmp_path)
    RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME).save_port(P_BASE + 5)
    occupied = _occupy(P_BASE + 5)
    try:
        notified: list[tuple[str, str]] = []
        launcher = dl.Launcher(
            paths=paths, win32=FakeWin32(), health=FakeHealth(default=None),
            browser=lambda url: True, version=TEST_VERSION,
            app=_mini_app(str(paths.install_root)), warm=lambda log: "unavailable",
            notify=lambda t, x: notified.append((t, x)),
        )
        rc = launcher.run([])
        assert rc == 1
        assert notified and "无法启动" in notified[0][0]
        assert "静默更换" in notified[0][1]
        # runtime.json 未被改写
        store = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
        assert store.read_port() == P_BASE + 5
    finally:
        occupied.close()


def test_launcher_migrate_from_args_parsed(tmp_path):
    """契约 13：--migrate-from / --include-models 参数解析。"""
    args = dl._parse_args(["--migrate-from", r"C:\old-install", "--include-models", "--no-tray"])
    assert args.migrate_from == r"C:\old-install"
    assert args.include_models is True
    assert args.no_tray is True
    args2 = dl._parse_args([])
    assert args2.migrate_from is None and args2.include_models is False


def test_launcher_local_model_installer_flag_is_standalone():
    args = dl._parse_args(["--install-local-model"])
    assert args.install_local_model is True
    assert args.window is False and args.migrate_from is None


def test_launcher_migrate_from_invalid_source_errors_before_start(tmp_path):
    """非法来源（目录不存在）→ 明确提示 + rc=1，**不启动服务**、不写 instance.json。"""
    paths = _make_paths(tmp_path)
    notified: list[tuple[str, str]] = []
    launcher = dl.Launcher(
        paths=paths, win32=FakeWin32(), health=FakeHealth(),
        browser=lambda url: True, version=TEST_VERSION,
        app=_mini_app(str(paths.install_root)), warm=lambda log: "unavailable",
        notify=lambda t, x: notified.append((t, x)),
    )
    rc = launcher.run(["--migrate-from", str(tmp_path / "no-such-portable")])
    assert rc == 1
    assert notified and "迁移" in notified[0][0] and "不存在" in notified[0][1]
    assert launcher._server is None, "迁移失败不得启动服务"
    assert not (paths.run_root / dl.INSTANCE_FILENAME).exists(), "不得写 instance.json"
    log_text = (paths.log_root / dl.LAUNCHER_LOG_NAME).read_text(encoding="utf-8")
    assert "来源不合法" in log_text


def test_launcher_migrate_from_valid_then_starts(tmp_path):
    """合法来源：迁移完成（.env 落位 config_root）→ 继续正常启动 → 干净退出。"""
    old = tmp_path / "old-portable"
    old.mkdir()
    (old / "start-web.bat").write_text("@echo off", encoding="utf-8")
    (old / "src" / "dataset_recommender").mkdir(parents=True)
    (old / "src" / "dataset_recommender" / "__init__.py").write_text("", encoding="utf-8")
    (old / ".env").write_text("LLM_API_KEY=sk-test-1234567890abcdef\n", encoding="utf-8")
    (old / ".userdata").mkdir()
    (old / ".userdata" / "accounts.json").write_text(
        '{"schema_version": 1, "users": {}}', encoding="utf-8")

    paths = _make_paths(tmp_path)
    port = _free_port()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(dl, "DEFAULT_PORT", port)
    try:
        launcher, tray_holder, opened, _ = _make_launcher(paths)
        thread, box = _run_bg(launcher, ["--migrate-from", str(old), "--no-tray"])
        deadline = time.monotonic() + 15
        while launcher._server is None or not getattr(launcher._server, "started", False):
            if "error" in box:
                raise AssertionError(box["error"])
            assert time.monotonic() < deadline
            time.sleep(0.05)
        # 迁移已落位（.env → config_root；.userdata/accounts.json → userdata_dir）
        assert (paths.config_root / ".env").read_text(encoding="utf-8") == \
            (old / ".env").read_text(encoding="utf-8")
        assert (paths.userdata_dir / "accounts.json").exists()
        launcher._server.should_exit = True
        thread.join(timeout=15)
        assert box.get("rc") == 0, box
        log_text = (paths.log_root / dl.LAUNCHER_LOG_NAME).read_text(encoding="utf-8")
        assert "迁移完成" in log_text
        assert "继续正常启动" in log_text
    finally:
        monkeypatch.undo()


def test_tray_menu_handlers_copy_and_open_logs(tmp_path):
    """托盘菜单回调接线：复制地址/打开日志目录/打开浏览器走注入的 win32。"""
    paths = _make_paths(tmp_path)
    fake_win = FakeWin32()
    launcher = dl.Launcher(paths=paths, win32=fake_win,
                           tray_factory=lambda handlers: FakeTray(handlers))
    launcher._log_root = paths.log_root
    launcher._url = f"http://127.0.0.1:{P_BASE}"
    tray = launcher._make_tray(P_BASE, no_tray=False)
    assert tray is not None
    tray.handlers.on_copy()
    assert fake_win.copied == [f"http://127.0.0.1:{P_BASE}"]
    tray.handlers.on_open_logs()
    assert fake_win.opened_dirs == [str(paths.log_root)]
    tray.handlers.on_quit()  # 不崩即可（无 server 时仅日志）


# ===========================================================================
# 11. Basic base64 / URL userinfo 脱敏
# ===========================================================================
def test_redact_basic_auth_and_url_userinfo():
    cases = [
        ("Authorization: Basic dXNlcjpwYXNzMTIz", "dXNlcjpwYXNzMTIz"),
        ("curl https://alice:s3cr3t@example.com/data -v", "alice:s3cr3t"),
        ("GET https://user:pass@10.0.0.1:7860/api/health HTTP/1.1", "user:pass"),
        ("Basic dXNlcjpwYXNz 夹在中间", "dXNlcjpwYXNz"),
    ]
    for line, secret in cases:
        out = redact(line)
        assert secret not in out, f"{line!r} 泄漏了 {secret!r}"
        assert "<redacted>" in out, f"{line!r} 未掩码"


def test_redact_url_userinfo_keeps_host_path():
    """URL userinfo 只掩 userinfo 段，保留主机与路径（邮件地址不误伤）。"""
    line = "请访问 https://example.com/data 或发邮件给 person@example.com"
    out = redact(line)
    assert "example.com/data" in out
    assert "person@example.com" in out


# ===========================================================================
# 12. runtime.json 损坏——告警 + 沿用原端口（不静默重排）
# ===========================================================================
def test_runtime_store_corrupt_state_and_recovery(tmp_path):
    """read_port_with_state 四态：missing / ok / 损坏可恢复 / 全损。"""
    store = RuntimeStore(tmp_path / "runtime.json")
    assert store.read_port_with_state() == (None, "missing")
    store.save_port(7891)
    assert store.read_port_with_state() == (7891, "ok")
    # 损坏但 port 字段仍在 → corrupt + 恢复端口
    store.path.write_text('{"schema": "biodata-launcher-runtime/1", "port": 7891, "corrupted',
                          encoding="utf-8")
    assert store.read_port_with_state() == (7891, "corrupt")
    # schema 不符 → corrupt
    store.path.write_text('{"schema": "other", "port": 7891}', encoding="utf-8")
    assert store.read_port_with_state() == (7891, "corrupt")
    # 全损 → corrupt + 无法恢复
    store.path.write_text("{\n", encoding="utf-8")
    assert store.read_port_with_state() == (None, "corrupt")
    # 保存新端口覆盖损坏文件（新端口生效，属预期收敛）
    store.save_port(7892)
    assert store.read_port_with_state() == (7892, "ok")


def test_resolve_port_corrupt_runtime_keeps_recovered_port(tmp_path, caplog):
    """runtime.json 损坏但可恢复端口 → 告警 + 沿用该端口（不静默重排）。

    恢复出的端口空闲 → resolve_port 直接绑定沿用（不重新分配默认/漂移端口）。"""
    paths = _make_paths(tmp_path)
    runtime = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
    runtime.path.parent.mkdir(parents=True, exist_ok=True)
    runtime.path.write_text(f'{{"port": {P_BASE + 2}, "broken', encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        decision = resolve_port(runtime_store=runtime, health=FakeHealth(),
                                expected_version=TEST_VERSION,
                                expected_install_root=str(paths.install_root))
    assert decision.port == P_BASE + 2
    assert decision.sock is not None, "恢复端口空闲 → 直接沿用（不换默认/漂移端口）"
    assert "runtime.json" in caplog.text and "损坏" in caplog.text
    assert "沿用该端口" in caplog.text
    decision.sock.close()


def test_resolve_port_corrupt_runtime_unrecoverable_reallocates(tmp_path, caplog):
    """损坏且无法恢复端口 → 明确告警 + 重新分配并覆盖保存（不静默）。"""
    paths = _make_paths(tmp_path)
    runtime = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
    runtime.path.parent.mkdir(parents=True, exist_ok=True)
    runtime.path.write_text("{\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        decision = resolve_port(runtime_store=runtime, health=FakeHealth(),
                                expected_version=TEST_VERSION,
                                expected_install_root=str(paths.install_root))
    assert decision.port == P_BASE  # 默认端口空闲 → 分配并保存
    assert decision.persisted is True
    assert runtime.read_port_with_state()[0] == P_BASE
    assert "无法恢复端口" in caplog.text


def test_try_attach_corrupt_runtime_warns_and_probes_recovered_port(tmp_path, caplog):
    """attach 探测失败路径也保留原端口——corrupt 用恢复端口探测而非 7860。"""
    paths, runtime, instances, health, opened = _attach_env(tmp_path)
    runtime.path.parent.mkdir(parents=True, exist_ok=True)
    runtime.path.write_text(f'{{"port": {P_BASE + 6}, broken', encoding="utf-8")
    health = FakeHealth(results={P_BASE + 6: _matching(str(paths.install_root))})
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        ok, url = try_attach(runtime_store=runtime, instance_store=instances, health=health,
                             expected_version=TEST_VERSION,
                             expected_install_root=str(paths.install_root),
                             is_pid_alive=lambda pid: True, open_url=opened.append,
                             timeout=0.5, poll=0.02)
    assert ok is True and url == f"http://127.0.0.1:{P_BASE + 6}"
    assert opened == [f"http://127.0.0.1:{P_BASE + 6}"], "用恢复端口命中，非默认 7860"
    assert "损坏" in caplog.text


# ===========================================================================
# 13. 固定端口被本产品异版本占用 —— 文案区分
# ===========================================================================
def test_resolve_port_fixed_occupied_by_other_product_version(tmp_path):
    """有健康响应但不匹配本安装（异版本/异安装）→ 文案点明「另一份 BioData Agent」。"""
    paths = _make_paths(tmp_path)
    runtime = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
    runtime.save_port(P_BASE + 1)
    occupied = _occupy(P_BASE + 1)
    try:
        health = FakeHealth(results={P_BASE + 1: _matching(str(paths.install_root), version="1.9.9")})
        with pytest.raises(PortOccupiedByOtherError) as ei:
            resolve_port(runtime_store=runtime, health=health,
                         expected_version=TEST_VERSION,
                         expected_install_root=str(paths.install_root))
        msg = str(ei.value)
        assert "另一份 BioData Agent" in msg
        assert "无关程序" not in msg
    finally:
        occupied.close()


def test_resolve_port_fixed_occupied_by_unrelated_message(tmp_path):
    """无健康响应（无关服务）→ 文案点明「无关程序」，不含异版本措辞。"""
    paths = _make_paths(tmp_path)
    runtime = RuntimeStore(paths.config_root / dl.RUNTIME_FILENAME)
    runtime.save_port(P_BASE + 1)
    occupied = _occupy(P_BASE + 1)
    try:
        with pytest.raises(PortOccupiedByOtherError) as ei:
            resolve_port(runtime_store=runtime, health=FakeHealth(default=None),
                         expected_version=TEST_VERSION,
                         expected_install_root=str(paths.install_root))
        msg = str(ei.value)
        assert "无关程序" in msg
        assert "另一份" not in msg
    finally:
        occupied.close()


# ===========================================================================
# 14. 无交互桌面下 notify 降级为仅日志
# ===========================================================================
def test_default_notify_degrades_to_log_without_interactive_session(monkeypatch, caplog):
    """非交互桌面 → 不弹 MessageBoxW，只写日志。"""
    calls: list[tuple[str, str]] = []

    class _NoDesktop:
        @staticmethod
        def is_interactive() -> bool:
            return False

        @staticmethod
        def message_box(title: str, text: str) -> None:
            calls.append((title, text))

    monkeypatch.setattr(dl, "_win32", _NoDesktop())
    with caplog.at_level(logging.INFO, logger=dl.LAUNCHER_LOGGER):
        dl._default_notify("标题", "正文")
    assert calls == [], "非交互桌面不得弹 MessageBoxW"
    assert "降级为日志" in caplog.text and "标题" in caplog.text


def test_default_notify_message_box_in_interactive_session(monkeypatch):
    """交互会话 → 正常弹 MessageBoxW。"""
    calls: list[tuple[str, str]] = []

    class _Desktop:
        @staticmethod
        def is_interactive() -> bool:
            return True

        @staticmethod
        def message_box(title: str, text: str) -> None:
            calls.append((title, text))

    monkeypatch.setattr(dl, "_win32", _Desktop())
    dl._default_notify("t", "x")
    assert calls == [("t", "x")]


# ===========================================================================
# 15. BIODATA_DATA_ROOT 重定向后旧根 .env 未随迁 —— 警告日志
# ===========================================================================
def test_warn_stale_root_env_warns_when_old_env_present_new_missing(tmp_path, monkeypatch, caplog):
    old_root = tmp_path / "old-root"
    (old_root / "config").mkdir(parents=True)
    (old_root / "config" / ".env").write_text("LLM_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.setattr(dl, "default_data_root_frozen", lambda: old_root)
    paths = AppPaths(
        install_root=tmp_path / "install", resource_root=tmp_path / "install",
        data_root=tmp_path / "new-data", config_root=tmp_path / "new-data" / "config",
        shipped_base_dir=tmp_path / "install" / "database" / "base",
        shipped_external_dir=tmp_path / "install" / "database" / "external",
        user_external_dir=tmp_path / "new-data" / "database" / "external",
        userdata_dir=tmp_path / "new-data" / ".userdata",
        model_root=tmp_path / "new-data" / "models",
        log_root=tmp_path / "new-data" / "logs",
        trace_root=tmp_path / "new-data" / "database" / "trace",
        export_root=tmp_path / "new-data" / "exports",
        run_root=tmp_path / "new-data" / "run",
        runtime_mode="frozen",
    )
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        dl._warn_stale_root_env(paths)
    assert "旧默认根" in caplog.text and ".env" in caplog.text
    assert str(old_root) in caplog.text


def test_warn_stale_root_env_silent_when_env_followed(tmp_path, monkeypatch, caplog):
    """新根 config/.env 已存在（配置已随迁）→ 不告警。"""
    old_root = tmp_path / "old-root"
    (old_root / "config").mkdir(parents=True)
    (old_root / "config" / ".env").write_text("LLM_API_KEY=sk-x\n", encoding="utf-8")
    monkeypatch.setattr(dl, "default_data_root_frozen", lambda: old_root)
    new_config = tmp_path / "new-data" / "config"
    new_config.mkdir(parents=True)
    (new_config / ".env").write_text("LLM_API_KEY=sk-y\n", encoding="utf-8")
    paths = AppPaths(
        install_root=tmp_path / "install", resource_root=tmp_path / "install",
        data_root=tmp_path / "new-data", config_root=new_config,
        shipped_base_dir=tmp_path / "install" / "database" / "base",
        shipped_external_dir=tmp_path / "install" / "database" / "external",
        user_external_dir=tmp_path / "new-data" / "database" / "external",
        userdata_dir=tmp_path / "new-data" / ".userdata",
        model_root=tmp_path / "new-data" / "models",
        log_root=tmp_path / "new-data" / "logs",
        trace_root=tmp_path / "new-data" / "database" / "trace",
        export_root=tmp_path / "new-data" / "exports",
        run_root=tmp_path / "new-data" / "run",
        runtime_mode="frozen",
    )
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        dl._warn_stale_root_env(paths)
    assert "旧默认根" not in caplog.text


def test_warn_stale_root_env_silent_for_source_mode(tmp_path, monkeypatch, caplog):
    """非 frozen 模式不适用该检查（source/portable 单根）。"""
    old_root = tmp_path / "old-root"
    monkeypatch.setattr(dl, "default_data_root_frozen", lambda: old_root)
    paths = _make_paths(tmp_path)  # portable 模式
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        dl._warn_stale_root_env(paths)
    assert "旧默认根" not in caplog.text


# ===========================================================================
# 预热与启动判活解耦（安装器边缘修复第 5 项）
# ===========================================================================
def test_start_background_warm_runs_in_separate_thread():
    done = threading.Event()
    names: list[str] = []

    def fake_warm(logger):
        names.append(threading.current_thread().name)
        done.set()
        return "warmed"

    dl.start_background_warm(fake_warm, logging.getLogger("test-bg-warm"))
    assert done.wait(5)
    assert names == ["biodata-warm"], "预热必须跑在独立 daemon 线程"


def test_serve_entry_starts_server_without_waiting_for_warm():
    """warm 仍阻塞时服务也必须已经 start——启动判活与预热解耦，不误判 60s 失败。"""
    warm_started = threading.Event()
    release_warm = threading.Event()
    serve_called = threading.Event()

    def slow_warm(logger):
        warm_started.set()
        release_warm.wait(5)
        return "warmed"

    class FakeServer:
        async def serve(self, sockets=None):
            serve_called.set()

    server = FakeServer()
    launcher = dl.Launcher(warm=slow_warm, logger=logging.getLogger("test-serve-warm"))
    t = threading.Thread(target=launcher._serve_entry, args=(server, object(), object(), 1))
    t.start()
    try:
        assert warm_started.wait(5)
        assert serve_called.wait(5), "warm 仍在跑，服务也必须已 start（解耦）"
    finally:
        release_warm.set()
        t.join(5)
    assert not t.is_alive()


# ===========================================================================
# 无账 upload_*.json 启动告警（安装器边缘修复第 7 项）
# ===========================================================================
def test_warn_orphaned_uploads_logs_warning(tmp_path, caplog):
    paths = _make_paths(tmp_path)
    paths.user_external_dir.mkdir(parents=True, exist_ok=True)
    (paths.user_external_dir / "upload_20260715_010203_000004_orphan.json").write_text(
        "{}", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        dl._warn_orphaned_uploads(paths, logging.getLogger(dl.LAUNCHER_LOGGER))
    assert "无对应 filename" in caplog.text
    assert "upload_20260715_010203_000004_orphan.json" in caplog.text


def test_warn_orphaned_uploads_silent_when_all_journaled(tmp_path, caplog):
    paths = _make_paths(tmp_path)
    paths.user_external_dir.mkdir(parents=True, exist_ok=True)
    (paths.user_external_dir / "upload_ok.json").write_text("{}", encoding="utf-8")
    paths.userdata_dir.mkdir(parents=True, exist_ok=True)
    (paths.userdata_dir / "uploads_journal.jsonl").write_text(
        json.dumps({"filename": "upload_ok.json"}) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger=dl.LAUNCHER_LOGGER):
        dl._warn_orphaned_uploads(paths, logging.getLogger(dl.LAUNCHER_LOGGER))
    assert "无对应 filename" not in caplog.text
