# -*- coding: utf-8 -*-
"""安装/升级/卸载 E2E harness（scripts/installer_e2e.py）。

两种模式（结果统一成 JSON 报告，供 pytest 驱动与人工 `--report` 检查）：

1. **frozen 模式**（真实 `BioDataAgent.exe`，PyInstaller 冻结产物）：把 frozen runtime 解包到隔离
   目录模拟安装布局（install_root 与 data_root 分离），用真实 exe 验证：
   - launch1 health/service/version/install_root/runtime_mode=frozen 全匹配
     （按 install_root 匹配扫描 7860-7869，避免误探本机其他 BioData Agent 实例）
   - launch2 固定端口跨重启不变（runtime.json 持久化；**启动器专用**）
   - launch3 二次启动 attach（第二个进程 attach 现有实例后快速退出 0；**启动器专用**）
   - launch4 退出后端口释放（进程退出 → 端口可立即重绑）
   **能力探测**：spec 入口已切到 desktop_launcher.main（entry_web.py 变薄转发）
   —— launch2/launch3 自动真跑（固定端口持久化/二次启动 attach）；能力探测仍保留兜底：
   若未来某产物缺 runtime.json/instance.json 则 launch2/launch3 自动 SKIP 并标注原因。
   隔离手段：`BIODATA_DATA_ROOT` 指向临时数据根、`BIODATA_NO_BROWSER=1`、
   `BROWSER` 指向可立即退出的解释器（防 attach 路径真开浏览器）、子进程环境加固
   （PATH 只留 System32，防 Git mingw64 DLL 污染——实测完整 PATH 下 exe 启动挂死）。
   运行前用与启动器同名的 mutex 探测本会话是否已有实例：有 → 整段 SKIP（不吸附）。
   托盘「退出」的优雅关停路径由启动器单测覆盖（FakeTray），此处验证进程退出即释放端口。

2. **安装器模式**（真实 Inno 产物，联调后启用）：安装器 exe 存在时跑
   `/VERYSILENT` 安装-升级-卸载矩阵 matrix1-matrix13。**真跑三重门**：① exe 存在；② 显式授权
   （`BIODATA_RUN_REAL_INSTALLER=1` 或 `--allow-real-installer`）；③ 安装器未被其他
   进程占用（防与其他实测进程撞锁）。真实安装会写 `%LOCALAPPDATA%`（Inno 的
   {localappdata} 由系统 API 解析、不吃进程 env 覆盖），属侵入性操作——未授权一律
   SKIP 并标注「待安装器联调」，绝不自动真跑；跑真矩阵还需要 .iss 提供数据根
   隔离钩子。

用法：
  python scripts/installer_e2e.py --report report.json   # 全量（frozen 可跑则跑）
  python scripts/installer_e2e.py --frozen-only
  python scripts/installer_e2e.py --installer-only
  python scripts/installer_e2e.py --list                 # 仅列矩阵与发现，不执行
环境变量：
  BIODATA_BUILD_OUT        构建产物根（默认 = 仓库父目录/build-out）
  BIODATA_INSTALLER_EXE    安装器 exe 绝对路径（优先于自动搜索）
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# sys.path 锚定：复用启动器的 MUTEX_NAME / HEALTH_PATH 与 win32 mutex 探测
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from dataset_recommender.app import desktop_launcher_win32 as _win32  # noqa: E402
from dataset_recommender.app.desktop_launcher import (  # noqa: E402
    EXPECTED_SERVICE,
    HEALTH_PATH,
    HOST,
    MUTEX_NAME,
)
from dataset_recommender.app.webapp import WEB_API_VERSION  # noqa: E402
from dataset_recommender.app.webview_shell import _webview2_installed  # noqa: E402

FROZEN_DIR_NAME = "BioDataAgent"
FROZEN_EXE_NAME = "BioDataAgent.exe"
INSTALLER_PREFIX = "BioData-Agent-Setup-"   # 契约：BioData-Agent-Setup-<version>-win-x64-unsigned-dev.exe
# build-out 与安装器构建 worktree 是仓库根的**兄弟目录**（与仓库同层），由文件位置派生，
# 不写死用户 home 路径（release 输入不得含私有路径）；其他机器用 env 覆盖。
_AGENTS_DIR = Path(__file__).resolve().parents[2]   # repo 的父目录
_REPO_ROOT = Path(__file__).resolve().parents[1]    # 仓库根
DEFAULT_BUILD_OUT = _AGENTS_DIR / "build-out"
DEFAULT_INSTALLER_WORKTREE = _AGENTS_DIR / "wt-installer-inno"
DRIFT_PORT_RANGE = range(7860, 7870)        # 启动器 7860 + 漂移段 7861-7869
HEALTH_POLL_S = 0.2
STARTUP_TIMEOUT_S = 30.0
EXIT_TIMEOUT_S = 15.0
ATTACH_TIMEOUT_S = 25.0
SHELL_OBSERVE_S = 8.0          # 窗口模式启动后观察 launcher.log 是否出现回退行的窗口


# ---------------------------------------------------------------------------
# 发现与守卫
# ---------------------------------------------------------------------------
def find_frozen_runtime(build_out: Path | None = None) -> Path | None:
    """定位 frozen runtime 根（含 BioDataAgent.exe 的目录）。env BIODATA_BUILD_OUT 优先。"""
    roots: list[Path] = []
    env = os.environ.get("BIODATA_BUILD_OUT", "").strip()
    if env:
        roots.append(Path(env).expanduser())
    if build_out is not None:
        roots.append(build_out)
    roots.append(DEFAULT_BUILD_OUT)
    for root in roots:
        cand = root / "dist" / FROZEN_DIR_NAME
        if (cand / FROZEN_EXE_NAME).is_file():
            return cand
    return None


def find_installer(build_out: Path | None = None) -> Path | None:
    """定位安装器：显式 exe 优先；自动发现必须优先当前 Web API 版本。

    旧实现遇到同目录多版本时按 ``os.walk`` 的偶然文件顺序返回第一项，真实矩阵曾因此拿
    2.4.0 去测已安装的 2.6.0，既制造假失败，又让后续场景装回旧 runtime。现在收集全部
    契约命名产物：先选 ``WEB_API_VERSION``，没有当前版才选最高 semver 作诊断性 fallback。
    """
    env = os.environ.get("BIODATA_INSTALLER_EXE", "").strip()
    if env and Path(env).is_file():
        return Path(env)
    roots: list[Path] = []
    env_out = os.environ.get("BIODATA_BUILD_OUT", "").strip()
    if env_out:
        roots.append(Path(env_out).expanduser())
    if build_out is not None:
        roots.append(build_out)
    roots.extend((DEFAULT_BUILD_OUT, DEFAULT_INSTALLER_WORKTREE))
    candidates: list[tuple[tuple[int, int, int], Path]] = []
    seen_roots: set[Path] = set()
    pattern = re.compile(r"^BioData-Agent-Setup-(\d+)\.(\d+)\.(\d+)-win-x64-.*\.exe$")
    for base in roots:
        base = base.resolve()
        if base in seen_roots:
            continue
        seen_roots.add(base)
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            depth = dirpath[len(str(base)):].count(os.sep)
            if depth >= 4:
                dirnames[:] = []
                continue
            for name in filenames:
                match = pattern.fullmatch(name)
                if match:
                    candidates.append((tuple(int(part) for part in match.groups()), Path(dirpath) / name))
    if not candidates:
        return None
    expected = tuple(int(part) for part in WEB_API_VERSION.split("."))
    exact = [item for item in candidates if item[0] == expected]
    pool = exact or candidates
    return max(pool, key=lambda item: (item[0], item[1].stat().st_mtime_ns, str(item[1])))[1]


def probe_mutex() -> tuple[bool, str]:
    """探测本会话是否已有运行中的 BioData Agent 实例（与启动器同名命名 mutex）。
    返回 (mutex 空闲, 说明)。异常 → 保守判为不空闲（绝不吸附真实实例）。"""
    try:
        mutex = _win32.open_mutex(MUTEX_NAME)
        try:
            if mutex.already_exists:
                return False, "检测到已运行的 BioData Agent 实例（mutex 被占），跳过真跑以免吸附"
            return True, "mutex 探测通过（无运行实例）"
        finally:
            mutex.close()
    except Exception as exc:  # noqa: BLE001（探测失败保守跳过）
        return False, f"mutex 探测异常（{type(exc).__name__}: {exc}），保守跳过真跑"


def drift_ports_available() -> bool:
    """7860-7869 中至少一个空闲即可跑（启动器先试 7860，被无关服务占则漂移取首个
    可用并持久化——harness 以 runtime.json 实际端口为准，不假设 7860）。全占 → False。"""
    for port in DRIFT_PORT_RANGE:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            continue
        finally:
            sock.close()
    return False


# ---------------------------------------------------------------------------
# 进程/健康/端口小助手
# ---------------------------------------------------------------------------
def _hardened_env() -> dict:
    """子进程环境加固（对齐 frozen 冒烟实测口径）：**PATH 只留 System32**——
    Git Bash 的完整 PATH 会把 mingw64 的 libssl/libcrypto 等 DLL 喂给 exe 导致
    启动期挂死（实测）；SYSTEMROOT/WINDIR 必须保留（缺失时 asyncio.windows_events
    导入抛 WinError 10106）；PYTHONNOUSERSITE=1 防用户站点干扰；代理指向不可用
    端口（离线）。"""
    return {
        "PATH": r"C:\Windows\System32;C:\Windows",
        "SYSTEMROOT": os.environ.get("SYSTEMROOT") or r"C:\Windows",
        "WINDIR": os.environ.get("WINDIR") or r"C:\Windows",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
        "HTTP_PROXY": "http://127.0.0.1:9",
        "HTTPS_PROXY": "http://127.0.0.1:9",
        "ALL_PROXY": "http://127.0.0.1:9",
        "NO_PROXY": "",
    }


def _browser_noop_env(env: dict) -> dict:
    """浏览器注入面：BIODATA_NO_BROWSER 关主实例自动开浏览器；BROWSER 指向可立即
    退出的解释器，使 attach 路径的 webbrowser.open 不弹真实浏览器（启动器把异常/失败
    都吞掉，attach 成功与否不依赖 open 返回值）。"""
    env = env.copy()
    env["BIODATA_NO_BROWSER"] = "1"
    env["BROWSER"] = f'"{sys.executable}" -c "raise SystemExit(0)"'
    return env


def _wait_health(port: int, timeout: float = STARTUP_TIMEOUT_S) -> dict | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://{HOST}:{port}{HEALTH_PATH}", timeout=1.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001（连接/超时/解析失败都继续轮询）
            pass
        time.sleep(HEALTH_POLL_S)
    return None


def _wait_port_free(port: int, timeout: float = EXIT_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((HOST, port))
            return True
        except OSError:
            time.sleep(HEALTH_POLL_S)
        finally:
            sock.close()
    return False


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _wait_for(fn, timeout: float, poll: float = 0.2,
              proc: subprocess.Popen | None = None) -> bool:
    """轮询 fn() 直到 True；超时返回 False。

    防空转：传 `proc` 时每轮先查进程存活——子进程已退出（poll() 非 None）
    → **立即返回 False**，不空转到超时（否则「等 runtime.json 落盘」会为一个已
    崩溃的进程白等 30s，再按空结果误判）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            return False
        if fn():
            return True
        time.sleep(poll)
    return False


# ---------------------------------------------------------------------------
# frozen 模式：真实 BioDataAgent.exe 全链路
# ---------------------------------------------------------------------------
def _scan_matching_health(install_root: Path, timeout: float = 90.0) -> tuple[int | None, dict | None]:
    """在 7860-7869 找**本安装**（install_root 匹配）的健康服务。

    必须校验 install_root：本机可能跑着别的 BioData Agent 实例（source/另一份安装），
    只凭 port+health 会误探到它（实测 7861 上有一份他人实例）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for port in DRIFT_PORT_RANGE:
            try:
                with urllib.request.urlopen(f"http://{HOST}:{port}{HEALTH_PATH}", timeout=0.6) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not isinstance(data, dict):
                    continue
                if Path(str(data.get("install_root", ""))).resolve() == install_root.resolve():
                    return port, data
            except Exception:  # noqa: BLE001（未监听/超时/解析失败都继续）
                pass
        time.sleep(1.0)
    return None, None


def _default_port_interfering(install_root: Path) -> str | None:
    """默认端口 7860 上是否已有**本产品但非本安装**的响应（source/另一份安装）。

    返回污染说明字符串；不污染返回 None。这种环境下无 PORT 首启的 exe 会被启动器
    的 attach 判定干扰（本机常驻的 source dev server 在 7860 即此情形）——launch2 固定
    端口 / launch3 二次 attach 无法干净真跑，应如实 SKIP，不谎报 pass/fail。"""
    try:
        with urllib.request.urlopen(
                f"http://{HOST}:{DRIFT_PORT_RANGE.start}{HEALTH_PATH}", timeout=1.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001（7860 无响应/超时/不可解析 → 不污染）
        return None
    if not isinstance(data, dict):
        return None
    if data.get("service") != EXPECTED_SERVICE or data.get("version") != WEB_API_VERSION:
        return None  # 非本产品版本 → 按无关程序处理，attach 判定不受影响
    if data.get("runtime_mode") == "frozen" and \
            Path(str(data.get("install_root", ""))).resolve() == install_root.resolve():
        return None  # 就是本安装的 frozen 实例 → 不污染，attach 可测
    return (
        f"本机 {DRIFT_PORT_RANGE.start} 存在同版本本产品响应（runtime_mode="
        f"{data.get('runtime_mode')!r} install_root={data.get('install_root')!r}），"
        f"非本安装（期望 runtime_mode='frozen'）；env 不净——无 PORT 首启将受 attach 判定"
        f"干扰，无法验证启动器 runtime.json/instance.json 的固定端口/二次 attach 语义，如实 SKIP")


def frozen_e2e(frozen_root: Path, workdir: Path) -> dict:
    """解包 frozen runtime 到隔离目录（install/data 分离），跑真实 exe 用例。

    能力探测：spec 入口已切到 desktop_launcher.main（entry_web.py 为薄转发，
    启动器行为：mutex/instance.json/固定端口持久化/attach/托盘全在）——launch2/launch3 真跑；
    兜底保留：若未来产物缺 runtime.json/instance.json 则 SKIP 并标注原因。
    首启用空闲端口 PORT 覆盖（entry_web 只认 7860 默认值；本机可能并存其他实例/
    安装器实测占用 7860-7869，install_root 匹配扫描保证只认本安装）。"""
    install_root = workdir / "install"
    data_root = workdir / "data"
    shutil.copytree(frozen_root, install_root)
    data_root.mkdir()
    exe = install_root / FROZEN_EXE_NAME
    base_env = _browser_noop_env(_hardened_env())
    base_env["BIODATA_DATA_ROOT"] = str(data_root)
    cases: list[dict] = []

    def _record(cid: str, name: str, status: str, detail: str) -> None:
        cases.append({"id": cid, "name": name, "status": status, "detail": detail})

    def _launch(extra: dict | None = None) -> subprocess.Popen:
        env = base_env.copy()
        if extra:
            env.update(extra)
        return subprocess.Popen([str(exe)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _free_drift_port() -> int | None:
        for port in DRIFT_PORT_RANGE:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.bind((HOST, port))
                return port
            except OSError:
                continue
            finally:
                sock.close()
        return None

    def _runtime_port() -> int | None:
        runtime = data_root / "config" / "runtime.json"
        data = _read_json(runtime)
        if not data or data.get("schema") != "biodata-launcher-runtime/1":
            return None
        port = data.get("port")
        return port if isinstance(port, int) and 1 <= port <= 65535 else None

    def _stop(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    # ---- 首启：PORT 覆盖到空闲端口，等本安装的健康服务（install_root 匹配）----------
    port1 = _free_drift_port()
    if port1 is None:
        _record("launch1", "health/service/version/install_root", "fail",
                "7860-7869 无空闲端口（可能被本机其他实例或安装器实测占用），无法启动隔离实例")
        return {"run": "failed", "reason": "无可用端口", "cases": cases}
    p1 = _launch({"PORT": str(port1)})
    health1 = None
    try:
        found_port, health1 = _scan_matching_health(install_root)
        if found_port is None:
            _record("launch1", "health/service/version/install_root", "fail",
                    f"首启 90s 内未发现本安装的健康服务（进程退出码 {p1.poll()}；"
                    "已按 install_root 匹配扫描 7860-7869，其他实例的响应会被正确忽略）")
            _stop(p1)
            return {"run": "failed", "reason": "首启未能就绪（可能冷启动过慢或 exe 不可用）", "cases": cases}
        ok = (health1.get("ok") is True
              and health1.get("service") == EXPECTED_SERVICE
              and health1.get("version") == WEB_API_VERSION
              and health1.get("runtime_mode") == "frozen")
        _record("launch1", "health/service/version/install_root", "pass" if ok else "fail",
                json.dumps({k: health1.get(k) for k in (
                    "ok", "service", "version", "install_root", "runtime_mode")},
                    ensure_ascii=False)
                + ("" if ok else f"（期望 version={WEB_API_VERSION} runtime_mode=frozen）"))
    finally:
        pass

    # ---- 能力探测：启动器行为是否存在（runtime.json / instance.json）------------------
    launcher_capable = False
    if port1 is not None:
        for _ in range(20):
            if (data_root / "config" / "runtime.json").is_file() \
                    or (data_root / "run" / "instance.json").is_file():
                launcher_capable = True
                break
            time.sleep(0.5)
    entry_note = ("当前 BioDataAgent.exe 入口缺失启动器行为（无 runtime.json/instance.json，"
                  "可能为旧入口产物或启动失败）；launch2/launch3 为启动器专用用例，已 SKIP")

    # ---- launch4：退出后端口释放（进程退出即释放；托盘优雅关停路径由启动器单测覆盖）--------
    _stop(p1)
    released = _wait_port_free(port1)
    _record("launch4", "退出后端口释放", "pass" if released else "fail",
            f"进程退出后 {port1} 可立即重绑（干净托盘退出路径由启动器单测覆盖，此处验证进程退出即释放）")

    if not launcher_capable:
        _record("launch2", "固定端口跨重启不变（runtime.json 持久化）", "skip",
                f"{entry_note}；本段为启动器专用行为")
        _record("launch3", "二次启动 attach 现有实例", "skip",
                f"{entry_note}；本段为启动器专用行为")
        failed = [c for c in cases if c["status"] == "fail"]
        return {"run": "passed" if not failed else "failed",
                "reason": "产物缺启动器能力（runtime.json/instance.json），launch2/launch3 已 SKIP"
                          if not failed else "存在失败用例",
                "cases": cases}

    # ---- 启动器能力就绪：launch2 固定端口跨重启 / launch3 attach ----------------------------
    # （PORT 覆盖不持久化，此路径必须无 PORT 走真实固定端口逻辑：首启绑定→runtime.json
    #  持久化→重启复用）
    # launch2/launch3 环境守卫：默认端口 7860 上若已有本产品但非本安装的响应（如本机常驻的
    # source dev server），无 PORT 首启会被 attach 判定吸附/卡死、且无法
    # 干净验证固定端口/二次 attach 语义——如实 SKIP，不谎报 pass/fail，也不卡死。
    taint = _default_port_interfering(install_root)
    if taint is not None:
        _record("launch2", "固定端口跨重启不变（runtime.json 持久化）", "skip", taint)
        _record("launch3", "二次启动 attach 现有实例", "skip", taint)
        return {"run": "passed",
                "reason": "env 不净（默认端口 7860 有非本安装的本产品响应），launch2/launch3 如实 SKIP",
                "cases": cases}
    p2 = _launch()
    port_a = None
    try:
        # 等 runtime.json 落盘时把进程存活纳入判定——进程死了立即失败（不空转）
        if _wait_for(lambda: _runtime_port() is not None, STARTUP_TIMEOUT_S, proc=p2):
            port_a = _runtime_port()
        health_a = _wait_health(port_a) if port_a is not None else None
        if port_a is None or health_a is None:
            _record("launch2", "固定端口跨重启不变", "fail",
                    f"启动器路径首启未就绪（port={port_a} health={'ok' if health_a else 'none'}，"
                    f"进程退出码 {p2.poll()}）")
        else:
            inst_a = _read_json(data_root / "run" / "instance.json")
            # 重启：固定端口应从 runtime.json 复用
            _stop(p2)
            p2 = _launch()
            port_b = None
            if _wait_for(lambda: _runtime_port() is not None, STARTUP_TIMEOUT_S, proc=p2):
                port_b = _runtime_port()
            health_b = _wait_health(port_b) if port_b is not None else None
            ok = (port_b is not None and port_b == port_a and health_b is not None
                  and health_b.get("version") == WEB_API_VERSION
                  and (inst_a is None or inst_a.get("pid") != p2.pid))
            _record("launch2", "固定端口跨重启不变", "pass" if ok else "fail",
                    f"首启 {port_a} → 重启后 {port_b}（runtime.json 持久化；服务可达={health_b is not None}）")
            # launch3：attach（第三个进程 attach 现有实例后快速退出 0）
            inst_before = _read_json(data_root / "run" / "instance.json")
            p3 = _launch()
            try:
                rc3 = p3.wait(timeout=ATTACH_TIMEOUT_S)
                health3 = _wait_health(port_b, timeout=5.0)
                inst_after = _read_json(data_root / "run" / "instance.json")
                pid_unchanged = (inst_before is not None and inst_after is not None
                                 and inst_before.get("pid") == inst_after.get("pid") == p2.pid)
                ok = (rc3 == 0 and health3 is not None and pid_unchanged)
                _record("launch3", "二次启动 attach 现有实例", "pass" if ok else "fail",
                        f"第二个进程退出码={rc3}（期望 0）；服务仍可达={health3 is not None}；"
                        f"实例 pid 未变={pid_unchanged}")
            except subprocess.TimeoutExpired:
                _record("launch3", "二次启动 attach 现有实例", "fail",
                        f"第二个进程 {ATTACH_TIMEOUT_S}s 未退出（attach 未发生或卡死）")
                _stop(p3)
    finally:
        _stop(p2)

    run = "passed" if cases and all(c["status"] in ("pass", "skip") for c in cases) else "failed"
    return {"run": run, "reason": "" if run == "passed" else "存在失败用例", "cases": cases}


# ---------------------------------------------------------------------------
# 安装器模式：真实 Inno 安装-升级-卸载矩阵（matrix1-matrix13；installer 缺失 → 全 SKIP）
# ---------------------------------------------------------------------------
def _innocmd(installer: Path, args: list[str], env: dict | None = None,
             timeout: int = 900) -> tuple[int, str]:
    """跑安装器/卸载器，返回 (returncode, 合并输出)。Inno 退出码：0=成功。

    timeout 超时（如 matrix7 运行中升级被拦时 Inno 弹对话框等待用户）→ 抛
    subprocess.TimeoutExpired，由调用方按「被拦/未完成」语义处理。"""
    result = subprocess.run([str(installer)] + args, env=env,
                            capture_output=True, text=True, timeout=timeout)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _default_install_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "BioData Agent"


def _redirected_data_root(workdir: Path) -> Path:
    """数据根隔离钩子——矩阵内安装器/卸载器进程统一注入 BIODATA_DATA_ROOT 指向
    workdir 隔离数据根（.iss 安装期把它持久化到卸载注册信息，卸载器按持久化值解析），
    使真实矩阵不读写用户真实 %LOCALAPPDATA%\\BioDataAgent。"""
    return workdir / "matrix-data"


def _installer_env(workdir: Path, extra: dict | None = None) -> dict:
    """安装器/卸载器子进程 env：加固 + 数据根隔离钩子 + 可选追加。"""
    env = _hardened_env()
    env["BIODATA_DATA_ROOT"] = str(_redirected_data_root(workdir))
    if extra:
        env.update(extra)
    return env


def _matrix_handlers(installer: Path, workdir: Path) -> list[dict]:
    """实现安装器矩阵各 handler（真实命令；installer 存在时才被调用）。

    联调收口要点：
    - 数据根隔离钩子：所有安装/卸载子进程带 BIODATA_DATA_ROOT（→ workdir/matrix-data），
      .iss 安装期持久化到卸载注册信息，matrix5/matrix6/matrix9/matrix10/matrix11 断言基于该隔离根；
    - matrix7：运行中升级按 .iss AppMutex 实际语义判定（被拦 = 安装器未在运行实例存在时
      完成安装）；
    - matrix8：真跑降级对——若 build-out 只有新版安装器，用 ISCC 以旧版本号（2.3.0）编译
      一个旧版安装器到 workdir，旧版覆盖新版 → 断言被拦（版本守卫）；同目录并存时
      直接用既有旧版；
    - matrix10：卸载器带 /DELETEDATA 开关非交互触发「同时删除本地数据」，断言只删精确
      隔离 data_root、兄弟目录不受影响。"""
    # matrix7/matrix12 需要启动已安装 exe：数据根一律隔离到 workdir，避免污染真实数据
    installed_exe = _default_install_dir() / FROZEN_EXE_NAME
    env_isolated = _browser_noop_env(_hardened_env())

    def _run_install(args: list[str], name: str, env: dict | None = None,
                     extra_detail: str = "", timeout: int = 300) -> dict:
        log = workdir / f"{name}.log"
        try:
            rc, _out = _innocmd(installer, args + [f"/LOG={log}"], env=env, timeout=timeout)
        except subprocess.TimeoutExpired:
            # 安装器超时未完成：多为运行实例存在时 Inno 弹对话框等待用户（AppMutex/
            # 版本守卫），静默安装被抑制 → 判定「未完成/被拦」，由调用方按语义处理。
            return {"status": "fail", "detail": f"timeout={timeout}s（安装器未在限时内完成，"
                                                f"疑似对话框等待）；LOG={log}" + (f"；{extra_detail}" if extra_detail else "")}
        detail = f"rc={rc}；LOG={log}" + (f"；{extra_detail}" if extra_detail else "")
        return {"status": "pass" if rc == 0 else "fail", "detail": detail}

    items: list[dict] = []
    workdir.mkdir(parents=True, exist_ok=True)
    matrix_env = _installer_env(workdir)   # 数据根隔离钩子（安装/卸载统一注入）

    # matrix1 静默安装（GUI 真人交互以 /VERYSILENT 等价路径代替并注明）
    res = _run_install(["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], "matrix1-install",
                       env=matrix_env)
    exe_ok = installed_exe.is_file()
    res.update({"id": "matrix1", "name": "静默安装（GUI 交互以 /SILENT 代替并注明）",
                "status": "fail" if res["status"] != "pass" or not exe_ok else "pass",
                "detail": res["detail"] + f"；BioDataAgent.exe 就位={exe_ok}（GUI 真人交互未执行，用 /VERYSILENT 等价路径代替）"})
    items.append(res)

    # matrix2 无网络安装（代理指向不可用端口 + 无外网依赖）
    netless = _installer_env(workdir, {
        "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
        "http_proxy": "http://127.0.0.1:1", "https_proxy": "http://127.0.0.1:1"})
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix2-nonet", env=netless,
                       extra_detail="代理指向不可用端口，安装器不得依赖外网")
    res.update({"id": "matrix2", "name": "无网络安装", "detail": res["detail"]})
    items.append(res)

    # matrix3 自定义目录迁移被拦（孤儿化守卫：已装默认目录 + 显式 /DIR=custom →
    # InitializeSetup 阻止，不覆盖卸载键孤儿化旧安装；静默只写日志不弹框，rc≠0）。
    # 注意：集成「首装到自定义目录」由 GUI 真人验证覆盖，此处矩阵语义
    # 是验证「换 /DIR 重装」被拦截而非成功安装。
    custom = workdir / "CustomInstall"
    res = _run_install(["/VERYSILENT", "/NORESTART", f"/DIR={custom}"], "matrix3-customdir",
                       env=matrix_env)
    old_ok = installed_exe.is_file()
    custom_ok = (custom / FROZEN_EXE_NAME).is_file()
    blocked = res["status"] != "pass" and old_ok and not custom_ok
    res.update({"id": "matrix3", "name": "自定义目录迁移被拦（孤儿化守卫）",
                "status": "pass" if blocked else "fail",
                "detail": res["detail"] + f"；旧安装仍在={old_ok}；custom 未落盘={not custom_ok}"
                          "（已装默认目录 + 显式 /DIR 不同 → InitializeSetup 拦截，不孤儿化旧安装）"})
    items.append(res)

    # matrix4 中文路径迁移被拦（孤儿化守卫：同上，目标目录为中文路径）
    zh = workdir / "中文安装目录"
    res = _run_install(["/VERYSILENT", "/NORESTART", f"/DIR={zh}"], "matrix4-zhpath",
                       env=matrix_env)
    old_ok = installed_exe.is_file()
    zh_ok = (zh / FROZEN_EXE_NAME).is_file()
    blocked = res["status"] != "pass" and old_ok and not zh_ok
    res.update({"id": "matrix4", "name": "中文路径迁移被拦（孤儿化守卫）",
                "status": "pass" if blocked else "fail",
                "detail": res["detail"] + f"；旧安装仍在={old_ok}；中文路径未落盘={not zh_ok}"
                          "（已装默认目录 + 显式 /DIR 不同 → InitializeSetup 拦截，不孤儿化旧安装）"})
    items.append(res)

    # matrix5 同版本修复安装（数据根标记保持；标记落在隔离 data_root）
    data_root = _redirected_data_root(workdir)
    data_root.mkdir(parents=True, exist_ok=True)
    marker = data_root / "e2e-marker.txt"
    marker.write_text("preserve-me", encoding="utf-8")
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix5-repair", env=matrix_env)
    marker_ok = marker.exists() and marker.read_text(encoding="utf-8") == "preserve-me"
    res.update({"id": "matrix5", "name": "同版本修复安装",
                "status": "fail" if res["status"] != "pass" or not marker_ok else "pass",
                "detail": res["detail"] + f"；隔离 data_root 标记保持={marker_ok}（{data_root}）"})
    items.append(res)

    # matrix6 升级保留 data_root（真版本升级需两版安装器；当前以同版重装近似并注明）
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix6-upgrade", env=matrix_env)
    marker_ok = marker.exists() and marker.read_text(encoding="utf-8") == "preserve-me"
    res.update({"id": "matrix6", "name": "升级保留 data_root",
                "status": "fail" if res["status"] != "pass" or not marker_ok else "pass",
                "detail": res["detail"] + f"；隔离 data_root 标记保持={marker_ok}（真版本升级需两版安装器，当前以同版重装近似）"})
    items.append(res)

    # matrix7 运行中升级被拦（AppMutex 契约：.iss AppMutex 与启动器同名 mutex 逐字一致；
    # 语义按安装器实际实现收口：运行实例存在时安装器不得静默完成安装）
    proc = None
    app_ready = False
    if installed_exe.is_file():
        iso_env = env_isolated.copy()
        matrix7_data = workdir / "matrix7-data"
        iso_env["BIODATA_DATA_ROOT"] = str(matrix7_data)
        proc = subprocess.Popen([str(installed_exe)], env=iso_env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # 等运行实例就绪（runtime.json + instance.json + health）——按 install_root 匹配，
        # 避免误探本机其他实例（本机 7860/7861 可能有既有实例）。
        # 防空转：等实例就绪也把进程存活纳入判定（proc 死了立即 fail 不白等）；
        # 进程提前退出 → app_ready=False → 本用例判 fail（实例未就绪，不得空转判 pass）。
        app_ready = _wait_for(
            lambda: _read_json(matrix7_data / "config" / "runtime.json") is not None
                    and _read_json(matrix7_data / "run" / "instance.json") is not None,
            STARTUP_TIMEOUT_S, proc=proc)
    try:
        # AppMutex 拦截在静默模式下表现为：安装器弹「正在运行」对话框等待用户确认
        # （Inno 静默安装不自动关应用），60s 内未能完成 → 判定为被拦。
        res = _run_install(["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"], "matrix7-blocked-running",
                           env=matrix_env, timeout=60)
    finally:
        running_at_install = proc is not None and proc.poll() is None
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
    timed_out = "timeout=" in res["detail"]
    if not app_ready:
        # 运行实例未就绪 = 用例无效，直接 fail（不得用「安装没被拦」反向判 pass）
        if timed_out:
            _kill_stray_installers()   # 超时被拦后清理残留 setup 对话框进程
        res.update({"id": "matrix7", "name": "运行中升级被拦", "status": "fail",
                    "detail": f"运行实例未能就绪（{STARTUP_TIMEOUT_S:.0f}s 内无 runtime.json/"
                              f"instance.json，进程退出码 {proc.poll() if proc else 'N/A'}）；"
                              "用例无效，不得判 pass" + (f"；{res['detail']}" if res["detail"] else "")})
        items.append(res)
    else:
        # 语义：AppMutex 拦截 = 运行实例存在时安装器不得静默完成（rc≠0 即被拦；
        # 超时=安装器被「正在运行」对话框卡住，同样是未静默完成 → 合规被拦；
        # rc=0 但实例已被优雅关闭且安装完成 → 按 CloseApplications=yes 语义也算合规）
        if timed_out:
            _kill_stray_installers()   # 超时被拦后清理残留 setup 对话框进程
        blocked = res["status"] != "pass" or not running_at_install
        res.update({"id": "matrix7", "name": "运行中升级被拦",
                    "status": "pass" if blocked else "fail",
                    "detail": res["detail"]
                              + f"；实例就绪={app_ready}；安装时实例运行中={running_at_install}"
                                + ("；安装器被 AppMutex 对话框卡住（超时未完成）=被拦"
                                   if timed_out else "")
                                + "（AppMutex 拦截语义按 .iss 实际实现判定：运行中不得静默完成）"})
        items.append(res)

    # matrix8 降级被拦（真跑）：同目录优先用既有旧版；仅新版时用 ISCC 编译 2.3.0 旧版
    # 到 workdir，旧版覆盖新版 → 断言被拦（InitializeSetup 版本守卫）。
    downgrade = _find_older_installer(installer, workdir)
    if downgrade is None:
        downgrade = _compile_older_installer(installer, workdir)
    if downgrade is None:
        items.append({"id": "matrix8", "name": "降级被拦", "status": "skip",
                      "detail": f"无法构造旧版安装器（ISCC 不可用：{_iscc_path()}）；无法真跑降级对"})
    else:
        try:
            rc, _out = _innocmd(downgrade, ["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                                env=matrix_env, timeout=120)
            timed_out = False
        except subprocess.TimeoutExpired:
            # 版本守卫 InitializeSetup 的 MsgBox 在静默模式被抑制前可能弹框等待
            # （「旧版覆盖新版被拦」实测：提示后确定退出）——超时同样视为被拦。
            rc, timed_out = -1, True
            _kill_stray_installers()   # 清理残留对话框进程
        still_installed = installed_exe.is_file()
        blocked = (rc != 0 or timed_out) and still_installed
        items.append({"id": "matrix8", "name": "降级被拦",
                      "status": "pass" if blocked else "fail",
                      "detail": f"旧版 {downgrade.name} 覆盖已装新版：rc={rc}；超时被拦={timed_out}；"
                                f"新版安装仍存在={still_installed}（版本守卫 InitializeSetup 拦截）"})

    # matrix9 默认卸载保留数据（数据根隔离 + 持久化值；不带 /DELETEDATA）
    uninstaller = installed_exe.parent / "unins000.exe"
    if not uninstaller.is_file():
        items.append({"id": "matrix9", "name": "默认卸载保留数据", "status": "skip",
                      "detail": f"未找到卸载器 {uninstaller}（可能 matrix7/matrix8 后安装目录状态异常）"})
    else:
        rc, _out = _innocmd(uninstaller, ["/VERYSILENT", "/NORESTART"], env=matrix_env)
        data_ok = marker.exists() and marker.read_text(encoding="utf-8") == "preserve-me"
        install_gone = not installed_exe.parent.exists() or not installed_exe.exists()
        ok = rc == 0 and data_ok and install_gone
        items.append({"id": "matrix9", "name": "默认卸载保留数据",
                      "status": "pass" if ok else "fail",
                      "detail": f"rc={rc}；安装目录已移除={install_gone}；隔离 data_root 标记保持={data_ok}"
                                "（卸载默认不勾删数据；.iss 按持久化 BioDataDataRoot 解析）"})

    # matrix10 显式删数据只删精确 data_root（/DELETEDATA 非交互开关）
    # 先重装（matrix9 已卸载）→ 造兄弟目录 → /DELETEDATA 卸载 → 断言只删精确 data_root
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix10-reinstall", env=matrix_env)
    sibling = workdir / "sibling-dir"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "keep.txt").write_text("keep", encoding="utf-8")
    marker.write_text("preserve-me", encoding="utf-8")
    uninstaller = installed_exe.parent / "unins000.exe"
    if res["status"] != "pass" or not uninstaller.is_file():
        items.append({"id": "matrix10", "name": "显式删数据只删精确 data_root", "status": "skip",
                      "detail": f"重装失败或卸载器缺失（install rc={res['status']}）；无法真跑"})
    else:
        rc, _out = _innocmd(uninstaller, ["/VERYSILENT", "/NORESTART", "/DELETEDATA"],
                            env=matrix_env)
        # DelTree 返回成功后文件系统释放有微小时序（实测卸载器进程退出后目录短暂残留），
        # 轮询等待删除落定再断言。
        _wait_for(lambda: not data_root.exists(), timeout=EXIT_TIMEOUT_S, poll=0.2)
        data_gone = not data_root.exists()
        marker_gone = not marker.exists()
        sibling_ok = sibling.exists() and (sibling / "keep.txt").read_text(encoding="utf-8") == "keep"
        install_gone = not installed_exe.parent.exists() or not installed_exe.exists()
        ok = rc == 0 and data_gone and marker_gone and sibling_ok and install_gone
        items.append({"id": "matrix10", "name": "显式删数据只删精确 data_root",
                      "status": "pass" if ok else "fail",
                      "detail": f"rc={rc}；data_root 已删={data_gone}；标记已删={marker_gone}；"
                                f"兄弟目录不受影响={sibling_ok}；安装目录已移除={install_gone}"
                                "（/DELETEDATA 非交互触发，精确路径校验仍生效）"})

    # matrix11 静默卸载（先重装；与 matrix9 同路径，卸载器 /VERYSILENT）
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix11-reinstall", env=matrix_env)
    uninstaller = installed_exe.parent / "unins000.exe"
    if res["status"] != "pass" or not uninstaller.is_file():
        items.append({"id": "matrix11", "name": "静默卸载", "status": "skip",
                      "detail": f"重装失败或卸载器缺失（install rc={res['status']}）；无法真跑"})
    else:
        rc, _out = _innocmd(uninstaller, ["/VERYSILENT", "/NORESTART"], env=matrix_env)
        gone = not installed_exe.parent.exists() or not installed_exe.exists()
        items.append({"id": "matrix11", "name": "静默卸载", "status": "pass" if rc == 0 and gone else "fail",
                      "detail": f"rc={rc}；安装目录已移除={gone}"})

    # matrix12 端口跨重启不变（已安装 exe，数据根隔离；先重装）
    res = _run_install(["/VERYSILENT", "/NORESTART"], "matrix12-reinstall", env=matrix_env)
    iso_data = workdir / "matrix12-data"
    ports: list[int] = []
    healths: list[dict | None] = []
    for i in range(2):
        env12 = _browser_noop_env(_hardened_env())
        env12["BIODATA_DATA_ROOT"] = str(iso_data)
        proc = subprocess.Popen([str(installed_exe)], env=env12,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            # 等 runtime.json 落盘纳入进程存活判定（进程死了立即失败，不空转）
            if not _wait_for(lambda: _read_json(iso_data / "config" / "runtime.json") is not None,
                             STARTUP_TIMEOUT_S, proc=proc):
                ports.append(None)
                healths.append(None)
                continue
            runtime = _read_json(iso_data / "config" / "runtime.json") or {}
            ports.append(runtime.get("port"))
            if ports[-1] is not None:
                # _wait_health 返回值必须纳入 ok 判定——健康不可达 = 服务未就绪
                #（即使 runtime.json 有端口，也不能算「启动成功且端口稳定」）
                healths.append(_wait_health(ports[-1], timeout=STARTUP_TIMEOUT_S))
            else:
                healths.append(None)
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=EXIT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                proc.kill()
    ok = (len(ports) == 2 and ports[0] == ports[1] and ports[0] is not None
          and all(h is not None for h in healths))
    items.append({"id": "matrix12", "name": "端口跨重启不变",
                  "status": "pass" if ok else "fail",
                  "detail": f"两次启动端口={ports}；健康可达={[h is not None for h in healths]}"
                            f"（runtime.json 持久化，期望一致；重装 rc={res['status']}）"})

    # matrix13 桌面壳未静默缺失：① `--shell-probe` 断言安装后的 frozen 包内 webview 随包
    #（windowed 无控制台，用退出码 + BIODATA_SHELL_PROBE_OUT 文件双通道，见 build_windows_runtime）；
    # ② 窗口模式（`--window`）启动后读隔离 data_root 的 launcher.log，断言**不含**
    #「桌面窗口不可用，已回退系统浏览器」——08-24 的 2.5.0 正是条缺壳 + 原因日志丢失静默漏出。
    # 前置：测试机有 WebView2（_webview2_installed 同款注册表检测）；无 WebView2 时 SKIP 并注明——
    # 缺 WebView2 时 pywebview 会静默降级 MSHTML（importmap ESM 白屏），无法区分「壳缺失」与
    # 「测试机没 WebView2」，此时窗口模式断言无判别力。
    if _webview2_installed():
        matrix13_ok = True
        matrix13_notes: list[str] = []
        matrix13_data = workdir / "matrix13-data"
        matrix13_data.mkdir(parents=True, exist_ok=True)
        SH_REQ_TIMEOUT = STARTUP_TIMEOUT_S
        # ① frozen 包内 webview 验证
        probe_file = workdir / "matrix13-shell-probe.txt"
        probe_env = _hardened_env()
        probe_env["BIODATA_DATA_ROOT"] = str(matrix13_data)
        probe_env["BIODATA_SHELL_PROBE_OUT"] = str(probe_file)
        if probe_file.exists():
            probe_file.unlink()
        try:
            prc = subprocess.run([str(installed_exe), "--shell-probe"], env=probe_env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=SH_REQ_TIMEOUT)
            probe_txt = probe_file.read_text(encoding="utf-8").strip() if probe_file.is_file() else ""
            probe_ok = prc.returncode == 0 and "SHELL_PROBE_OK" in probe_txt
            matrix13_notes.append(f"shell-probe: exit={prc.returncode} recorded={probe_txt or '<empty>'}")
            if not probe_ok:
                matrix13_ok = False
        except subprocess.TimeoutExpired:
            matrix13_ok = False
            matrix13_notes.append("shell-probe: timeout")
        # ② 窗口模式启动 → launcher.log 无「已回退」行（仅验证通过才有判别意义）
        if matrix13_ok:
            win_env = _hardened_env()
            win_env["BIODATA_DATA_ROOT"] = str(matrix13_data)
            launcher_log = matrix13_data / "logs" / "launcher.log"
            proc = subprocess.Popen([str(installed_exe), "--window"], env=win_env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            saw_fallback = False
            ready = False
            try:
                # 等 launcher.log 出现「服务已就绪」（服务已启动，随后进入窗口/回退分支）
                ready = _wait_for(
                    lambda: launcher_log.is_file() and "服务已就绪" in launcher_log.read_text(
                        encoding="utf-8", errors="replace"),
                    STARTUP_TIMEOUT_S, proc=proc)
                if ready:
                    # 壳成败在此刻后立刻见分晓：成功→阻塞在窗口消息循环；失败→马上写回退行。
                    deadline = time.monotonic() + SHELL_OBSERVE_S
                    while time.monotonic() < deadline and not saw_fallback:
                        if launcher_log.is_file():
                            text = launcher_log.read_text(encoding="utf-8", errors="replace")
                            if "桌面窗口不可用，已回退系统浏览器" in text:
                                saw_fallback = True
                                break
                        time.sleep(0.2)
            finally:
                proc.terminate()
                try:
                    proc.wait(timeout=EXIT_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    proc.kill()
            if not ready:
                matrix13_ok = False
                matrix13_notes.append("窗口模式启动未在限时内就绪（launcher.log 无「服务已就绪」）")
            elif saw_fallback:
                matrix13_ok = False
                matrix13_notes.append("launcher.log 出现「桌面窗口不可用，已回退系统浏览器」")
                # 把相关日志行打进输出便于定位
                try:
                    fallback_lines = [ln for ln in launcher_log.read_text(
                        encoding="utf-8", errors="replace").splitlines()
                        if "回退" in ln or "WebView2" in ln or "pywebview" in ln or "窗口" in ln]
                    matrix13_notes.append("相关日志: " + " | ".join(fallback_lines[-6:]))
                except OSError:
                    pass
            else:
                matrix13_notes.append("窗口模式启动无回退（launcher.log 无「已回退系统浏览器」）")
        items.append({"id": "matrix13", "name": "桌面壳未静默缺失（webview 随包 + 窗口模式不回退）",
                      "status": "pass" if matrix13_ok else "fail",
                      "detail": "; ".join(matrix13_notes) + "（前置：测试机有 WebView2）"})
    else:
        items.append({"id": "matrix13", "name": "桌面壳未静默缺失（前置缺失→跳过）",
                      "status": "skip",
                      "detail": "测试机未检测到 WebView2 Runtime（_webview2_installed 同款注册表检测），"
                                "无法在窗口模式区分「壳缺失」与「测试机无 WebView2」；"
                                "跳过窗口模式断言，待有 WebView2 的环境重跑"})
    return items


def _iscc_path() -> Path:
    return Path(os.environ.get("ISCC_PATH") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe")


def _find_older_installer(installer: Path, workdir: Path) -> Path | None:
    """同目录找旧版安装器（文件名版本号 < 当前，如 BioData-Agent-Setup-2.3.0-*）。"""
    import re
    cur = re.search(r"-(\d+\.\d+\.\d+)-win-", installer.name)
    if not cur:
        return None
    cur_v = tuple(int(x) for x in cur.group(1).split("."))
    for base in (installer.parent, DEFAULT_BUILD_OUT):
        if not base.is_dir():
            continue
        for p in base.glob(f"{INSTALLER_PREFIX}*-win-x64-*.exe"):
            m = re.search(r"-(\d+\.\d+\.\d+)-win-", p.name)
            if not m or p == installer:
                continue
            old_v = tuple(int(x) for x in m.group(1).split("."))
            if old_v < cur_v:
                return p
    return None


def _compile_older_installer(installer: Path, workdir: Path) -> Path | None:
    """用 ISCC 以旧版本号编译一个旧版安装器（版本守卫降级对的真实材料）。

    复用当前 .iss 与同 onedir runtime（build-out/dist/BioDataAgent），只注入
    /DAppVersion=2.3.0 使其 DisplayVersion=2.3.0 < 已装 2.4.0——InitializeSetup
    版本守卫会拦截。产物输出到 workdir（不进 build-out，防被 find_installer 误拾）。"""
    iscc = _iscc_path()
    if not iscc.is_file():
        return None
    runtime_dir = find_frozen_runtime()
    if runtime_dir is None:
        return None
    iss = _REPO_ROOT / "packaging" / "inno" / "biodata-agent.iss"
    out = workdir / "old-installer"
    out.mkdir(parents=True, exist_ok=True)
    base = "BioData-Agent-Setup-2.3.0-win-x64-unsigned-dev"
    cmd = [
        str(iscc),
        "/DAppVersion=2.3.0",
        f"/DRuntimeDir={runtime_dir}",
        f"/O{out}",
        f"/F{base}",
        str(iss),
    ]
    result = subprocess.run(cmd, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=600)
    target = out / f"{base}.exe"
    if result.returncode != 0 or not target.is_file():
        print(f"[e2e] 旧版安装器编译失败（exit={result.returncode}）：{result.stdout[-2000:]}")
        return None
    return target


MATRIX_IDS = [f"matrix{i}" for i in range(1, 14)]  # matrix1-matrix13（matrix13=桌面壳未静默缺失）


def _installer_in_use(installer: Path) -> bool:
    """安装器是否正被其他进程使用（并行实测时其安装进程会占用 exe 文件）。
    用映像名探测（tasklist），命中 → True。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {installer.stem}*", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
        return "No tasks" not in out and bool(out.strip())
    except Exception:  # noqa: BLE001（探测失败按未占用处理，由授权门兜底）
        return False


def _kill_stray_installers() -> None:
    """清理矩阵运行残留的安装器/卸载器进程（超时被拦后可能仍挂对话框等待）。
    仅杀本矩阵安装器映像名（含 2.3.0 旧版编译产物与 unins000），不影响其他进程。"""
    for pattern in ("BioData-Agent-Setup-*", "unins000.exe"):
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", pattern],
                capture_output=True, text=True, timeout=20,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:  # noqa: BLE001（清理尽力而为）
            pass


def installer_matrix(installer: Path | None, workdir: Path,
                     allow_real: bool | None = None) -> list[dict]:
    """矩阵入口。真跑条件（**全部满足**）：
    ① 安装器 exe 存在；② 显式授权 `BIODATA_RUN_REAL_INSTALLER=1` 或参数 allow_real=True；
    ③ 安装器未被其他进程占用（防与其他并行实测撞文件锁）。
    真实安装器会写 `%LOCALAPPDATA%`（Inno {localappdata} 由系统解析、不吃进程 env），
    属侵入性操作——未授权一律 SKIP 并标注「待安装器联调」，绝不自动真跑。"""
    if installer is None:
        return [{"id": iid, "name": "", "status": "skip",
                 "detail": "安装器 exe 未找到（待安装器联调：BioData-Agent-Setup-<version>-win-x64-unsigned-dev.exe）"}
                for iid in MATRIX_IDS]
    authorized = allow_real if allow_real is not None \
        else os.environ.get("BIODATA_RUN_REAL_INSTALLER", "").strip().lower() in ("1", "true", "yes", "on")
    if not authorized:
        return [{"id": iid, "name": "", "status": "skip",
                 "detail": f"安装器 {installer.name} 已就位但矩阵未授权真跑（需显式 "
                           "BIODATA_RUN_REAL_INSTALLER=1）：真实安装会写 %LOCALAPPDATA%，"
                           "且与构建/真实环境存在冲突面——待安装器联调窗口授权运行"}
                for iid in MATRIX_IDS]
    if _installer_in_use(installer):
        return [{"id": iid, "name": "", "status": "skip",
                 "detail": f"安装器 {installer.name} 正被其他进程使用（疑似另有实测在跑），"
                           "跳过真跑——待安装器联调（避免撞文件锁/重复安装）"}
                for iid in MATRIX_IDS]
    items = _matrix_handlers(installer, workdir)
    by_id = {it["id"]: it for it in items}
    return [by_id[iid] for iid in MATRIX_IDS]  # 保证顺序与数量


# ---------------------------------------------------------------------------
# 报告与 CLI
# ---------------------------------------------------------------------------
def run_e2e(*, workdir: Path | None = None, frozen: bool = True,
            installer: bool = True, allow_real_installer: bool | None = None) -> dict:
    """全量 E2E：返回 JSON 报告。frozen 真跑由「runtime 存在 + mutex 空闲 + 端口可用」
    门控；安装器矩阵由「exe 存在 + 显式授权 + 未被占用」门控（见 installer_matrix）。
    workdir 缺省用临时目录（不污染工作树）。"""
    workdir = Path(workdir) if workdir is not None else Path(tempfile.mkdtemp(prefix="biodata-e2e-"))
    frozen_root = find_frozen_runtime()
    installer_exe = find_installer()
    report: dict = {
        "environment": {
            "python": sys.executable,
            "cwd": str(Path.cwd()),
            "workdir": str(workdir),
            "frozen_root": str(frozen_root) if frozen_root else None,
            "installer": str(installer_exe) if installer_exe else None,
        },
        "frozen": {"enabled": bool(frozen), "run": "disabled", "reason": "", "cases": []},
        "installer_matrix": [],
    }
    if frozen:
        if frozen_root is None:
            report["frozen"].update(run="skipped", reason="frozen runtime 未找到（build-out/dist/BioDataAgent 缺失），待冻结运行时构建产物")
        else:
            mutex_free, mutex_note = probe_mutex()
            if not mutex_free:
                report["frozen"].update(run="skipped", reason=mutex_note)
            elif not drift_ports_available():
                report["frozen"].update(run="skipped", reason="7860-7869 全部被占用，启动器无可用端口，跳过真跑")
            else:
                try:
                    result = frozen_e2e(frozen_root, workdir / "frozen")
                    report["frozen"].update(result)
                except Exception as exc:  # noqa: BLE001（harness 自身异常如实记录）
                    report["frozen"].update(run="failed", reason=f"harness 异常：{type(exc).__name__}: {exc}")
    if installer:
        report["installer_matrix"] = installer_matrix(installer_exe, workdir / "installer",
                                                      allow_real=allow_real_installer)
    return report


def _inventory() -> dict:
    return {
        "frozen_runtime": str(find_frozen_runtime()) if find_frozen_runtime() else None,
        "installer": str(find_installer()) if find_installer() else None,
        "matrix": MATRIX_IDS,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="installer_e2e",
        description="BioData Agent 安装/升级/卸载 E2E harness",
    )
    parser.add_argument("--report", metavar="PATH", help="把 JSON 报告写到文件")
    parser.add_argument("--frozen-only", action="store_true", help="只跑 frozen 模式")
    parser.add_argument("--installer-only", action="store_true", help="只跑安装器矩阵")
    parser.add_argument("--allow-real-installer", action="store_true",
                        help="显式授权真跑安装器矩阵（侵入 %%LOCALAPPDATA%%；缺省需 env "
                             "BIODATA_RUN_REAL_INSTALLER=1）")
    parser.add_argument("--workdir", default=None, help="工作目录（默认临时目录）")
    parser.add_argument("--list", action="store_true", help="仅列出矩阵与发现，不执行")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps(_inventory(), ensure_ascii=False, indent=2))
        return 0
    # 修复：store_true 缺省为 False（≠ None），传给 installer_matrix 的
    # `authorized = allow_real if allow_real is not None else env` 会恒取 False，
    # 使 BIODATA_RUN_REAL_INSTALLER=1 经 CLI 入口失效（全 SKIP）。未显式给
    # --allow-real-installer 时应传 None 回落 env 判断，显式给旗标才强制 True。
    report = run_e2e(workdir=Path(args.workdir) if args.workdir else None,
                     frozen=not args.installer_only, installer=not args.frozen_only,
                     allow_real_installer=True if args.allow_real_installer else None)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        Path(args.report).write_text(text, encoding="utf-8")
        print(f"报告已写入 {args.report}")
    else:
        print(text)
    failed = report["frozen"].get("run") == "failed" or any(
        item.get("status") == "fail" for item in report["installer_matrix"])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
