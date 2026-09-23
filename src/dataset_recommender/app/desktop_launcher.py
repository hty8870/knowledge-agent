# -*- coding: utf-8 -*-
"""无控制台桌面启动器（安装器工程 W2）——PyInstaller windowed/noconsole 构建的进程编排层。

本模块**不面向控制台**：windowed 构建下 `sys.stdout`/`sys.stderr` 可能为 None
（入口流守卫见 `_guard_streams`），一切生产输出走滚动日志（launcher.log/web.log）。
路径一律经 W1 的 `runtime_paths.get_app_paths()` 单一真源解析；**不引入任何
第三方 GUI 依赖**，Win32 交互面（mutex/托盘/MessageBox/剪贴板/PID 探测）全部
ctypes 直调 `desktop_launcher_win32.py`。

契约逐条落点（对应安装器 W2 任务书 14 条）：
1. 入口 `main(argv=None) -> int` 供 W3 spec 直接引用；`if __name__ == "__main__"`
   兜底脚本式执行。
2. 启动第一行先配脱敏滚动日志：log_root/launcher.log 与 web.log，RotatingFileHandler
   各 ≤5MiB×5 文件；`RedactingFormatter` 掩掉 API Key/密码/Token/Authorization 等，
   不记请求体（uvicorn access log 本就只记 method/path/status）、不记完整环境变量。
3. 命名 mutex `Local\\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79`
   （与 Inno AppMutex 同名）防双实例。
4. 端口策略：首启读 config_root/runtime.json；无固定端口先试 7860；被无关服务占用
   则在 7861-7869 取首个可用并**永久保存**；后续恒用固定端口（保 localStorage
   origin）；固定端口后被无关服务占用 → 明确报错、不静默换端口。
5. 端口由启动器先 bind，已绑定 socket 经 `uvicorn.Server.serve(sockets=[sock])`
   交给 uvicorn（uvicorn 0.51.0 的 `Config` 无 `sock` 参数——实测走 Server 的
   Gunicorn-worker 通道），消除检查-启动竞态；host 恒 127.0.0.1。
6. run/instance.json：临时文件 + `os.replace` 原子写；schema/pid/port/version/
   install_root/started_at 六字段。
7. 二次启动：mutex 已存在 → 读 instance.json → 验证 PID 存活 + health 的
   service/version/runtime_mode/install_root 匹配 → 通过则打开现有 URL 退出；
   损坏/过期（PID 死亡、版本/模式/根不匹配、JSON 损坏）自动恢复、绝不盲信。
   同一「本产品」判据（service+version+runtime_mode+install_root 规范化全等）也
   用于 `resolve_port`——mode/root 不同的另一份安装或 source 实例不会再被误认
   为「本实例」而在无 PORT 首启时被静默吸附。
8. 系统托盘：ctypes 调 Shell_NotifyIconW/TrackPopupMenu；菜单「打开 BioData
   Agent / 打开日志目录 / 复制访问地址 / 退出」；状态「正在启动/运行中/启动失败」
   经悬浮提示展示。无交互桌面时抛 `TrayUnavailable` → 降级无托盘轮询模式，不阻断。
9. 干净退出：tray「退出」→ `server.should_exit = True` → uvicorn 优雅关停
   （shutdown() 自行关闭监听 socket，端口即时释放）→ 主线程关 socket 引用、
   删本 PID 的 instance.json、释放 mutex；`timeout_graceful_shutdown=3s` 兜底，
   保证退出后 5 秒内端口可重绑。
10. 浏览器打开失败 → 简洁提示（windowed 下 MessageBoxW）+ 可复制地址（托盘
    「复制访问地址」菜单），服务继续跑。
11. windowed 流守卫：stdout/stderr 为 None 时换入空实现；未捕获异常进日志。
12. 无模型不触发下载（warm 仅本地加载、`unavailable` 秒过）、不发现 Python、
    不碰 PATH、无外网依赖（urllib 只探 127.0.0.1）、不加防火墙规则。
13. `--migrate-from` 迁移旧便携版用户数据（实现）：源校验（start-web.bat + 产品
    源码标记）→ dry-run 计划 → staging+逐文件 SHA-256 校验；**staging 全成功才开写**
    目标（落位逐文件 os.replace，可重入幂等——不是全有或全无的事务性提交，落位阶段
    中断会留下部分已落位文件但可安全重入）；只复制白名单用户数据（.env/已知
    .userdata/回收站/引文/upload_* 上传/可选 models），冲突保留双方并出报告；不删旧
    目录、失败不破坏旧数据、可重入幂等。
14. 现有 start-web.bat / 打开前端.bat / launch_web.ps1 行为零变化（本模块不触碰）。

模块结构：常量 → 日志（install_logging/RedactingFormatter）→ 原子 JSON
（RuntimeStore/InstanceStore）→ 端口（bind_socket/resolve_port）→ health 探测 →
attach → 预热 → Launcher 编排 → main 入口。除 `main`/`Launcher` 外都是纯函数，
便于单测；托盘/浏览器/MessageBox 等交互面一律可注入（测试用 fake）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import logging.handlers
import os
import re
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import uvicorn

# W1 风格 sys.path 锚定（source/portable 直接以模块方式运行时可解析到真实源码；
# frozen 下随包导入，此段为空操作）。desktop_launcher.py 位于
# src/dataset_recommender/app/ → 仓库根 = parents[3]。
_SRC_ROOT = Path(__file__).resolve().parents[3] / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from dataset_recommender.app.runtime_paths import (  # noqa: E402
    AppPaths,
    default_data_root_frozen,
    get_app_paths,
)
from dataset_recommender import secret_patterns  # noqa: E402
from dataset_recommender.app import desktop_launcher_win32 as _win32  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
#: 与 Inno 安装器 AppMutex 同名的命名 mutex（契约 3）
MUTEX_NAME = r"Local\BioDataAgent.Desktop.E249D2BA84574B8AB2C9B7CFA234BA79"

HOST = "127.0.0.1"
DEFAULT_PORT = 7860
DRIFT_PORT_START = 7861
DRIFT_PORT_END = 7869
EXPECTED_SERVICE = "dataset-recommender-web"
HEALTH_PATH = "/api/health"

LOG_MAX_BYTES = 5 * 1024 * 1024   #: 单文件 ≤5MiB（契约 2）
LOG_BACKUP_COUNT = 4              #: 1 活动 + 4 备份 = 最多 5 个文件
LAUNCHER_LOG_NAME = "launcher.log"
WEB_LOG_NAME = "web.log"
LAUNCHER_LOGGER = "biodata.launcher"
#: 在 launcher 进程内运行的 biodata.* 层 logger（统一接 launcher.log 的同一 handler）。
#: 审计（2026-08-24 grep 'getLogger("biodata\\.' src/）：除 biodata.launcher 外只有
#: biodata.webview_shell（桌面壳层）。它此前未接入——frozen windowed 下无控制台，
#: 其回退原因日志（"pywebview 不可用"/"未检测到 WebView2"/"桌面窗口启动失败"）
#: propagate 到无 handler 的 root 而全部丢失，导致 launcher 只报「已回退系统浏览器」
#: 却无任何原因行。webapp 的 logger 用 __name__（dataset_recommender.app.webapp），
#: 属 web 服务层（与 uvicorn 同层，走 web.log），不在此列。
LAUNCHER_LOGGERS = (LAUNCHER_LOGGER, "biodata.webview_shell")
WEB_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

INSTANCE_SCHEMA = "biodata-launcher-instance/1"
RUNTIME_SCHEMA = "biodata-launcher-runtime/1"
RUNTIME_FILENAME = "runtime.json"
INSTANCE_FILENAME = "instance.json"

STARTUP_TIMEOUT_S = 60.0        #: 服务从线程启动到可服务的等待上限
GRACEFUL_SHUTDOWN_S = 3.0       #: uvicorn 优雅关停兜底（契约 9：退出后 5s 内释放端口）
SHUTDOWN_JOIN_S = 10.0
ATTACH_TIMEOUT_S = 60.0         #: 二次启动定位运行中实例的等待上限（A2-M4：与 STARTUP_TIMEOUT_S
                                #: 对齐——主实例可能仍在 warm 预热（本地重排模型加载可达分钟级），
                                #: 6s 等不到就报「无法确认可用地址」是误导；期间每轮验证 PID 存活，
                                #: 实例进程已退出立即失败，不空转到超时）
ATTACH_POLL_S = 0.25
HEALTH_TIMEOUT_S = 1.2

_logger = logging.getLogger(LAUNCHER_LOGGER)
_logger.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# 日志：脱敏 + 滚动
# ---------------------------------------------------------------------------
#: 键值对形态的敏感字段（key=value / key: value / Authorization: Bearer x）。
#: 与 secret_patterns.SECRET_VALUE_PATTERNS 的分工：那边是交付扫描级的**强锚定**
#: 服务商前缀（宁可漏、不可误报）；本层是日志脱敏，额外保留更宽的形态网
#: （key=value / Bearer / Basic / URL userinfo）兜住无知名前缀的凭据。
_SECRET_KEY_VALUE = re.compile(
    r"(?i)(api[_-]?key|apikey|access[_-]?key|client[_-]?secret|secret|password|passwd|pwd|"
    r"token|authorization|auth|cookie|session[_-]?id|private[_-]?key|llm[_-]?api[_-]?key)"
    r"\b[^\r\n=:]{0,24}?[=:][ \t]*[^\s,;\"']{4,}"
)
#: Bearer <token>（值含空格，键值模式匹配不全，先于键值模式处理）
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}")
#: HTTP Basic base64 凭据（Authorization: Basic <base64(user:pass)>；字符集仅
#: A-Za-z0-9+/=，长度 ≥8——过短词（如普通文本 "Basic mode"）不掩，避免误伤）
_BASIC_TOKEN = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}")
#: URL userinfo 形态（https://user:pass@host/path 的 userinfo 段；要求 http(s)://
#: 前缀，不误伤邮件地址）
_URL_USERINFO = re.compile(r"(?i)\b(https?://)[^/@\s]{1,128}@")


def redact(text: str) -> str:
    """把日志文本中的密钥/密码/Token 值掩成 `<redacted>`（保留键名，便于定位）。

    用「键名之后的第一个分隔符」定位值起点：`LLM_API_KEY=abc==` 只掩掉 `abc==`
    而不会把 base64 末尾的 `=` 误判成新分隔符。"""
    def _mask_value(match: "re.Match[str]") -> str:
        key = match.group(1)
        tail = match.group(0)[len(key):]
        cut = len(tail)
        for sep in ("=", ":"):
            idx = tail.find(sep)
            if idx != -1:
                cut = min(cut, idx)
        return key + tail[:cut] + " <redacted>"

    # 先走交付扫描同源的强锚定值模式（单一真源 secret_patterns），再走本层形态网。
    # 裸 sk-/pk-/rk- 短令牌不再单独立网：强锚定模式已覆盖真实服务商 key 形态。
    for _pattern_id, rx in secret_patterns.SECRET_VALUE_PATTERNS:
        text = rx.sub("<redacted>", text)
    text = _BEARER_TOKEN.sub("<redacted>", text)
    text = _BASIC_TOKEN.sub("<redacted>", text)
    text = _URL_USERINFO.sub(r"\1<redacted>@", text)
    text = _SECRET_KEY_VALUE.sub(_mask_value, text)
    return text


class RedactingFormatter(logging.Formatter):
    """最终落盘前统一脱敏的 Formatter（对格式化后的整条消息做 redact）。"""

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        return redact(message)


#: install_logging 管理过的 logger 与 handler（reset_logging 用；测试隔离）
_MANAGED_LOGGERS: list[str] = []
_MANAGED_HANDLERS: list[logging.Handler] = []


def install_logging(log_root: Path, *, launcher_name: str = LAUNCHER_LOG_NAME,
                    web_name: str = WEB_LOG_NAME) -> dict[str, Any]:
    """配置脱敏滚动日志（契约 2）。可重复调用（先清旧 handler 再装新）。

    LOCALAPPDATA 不可写/磁盘满时**不抛**：降级到 stderr（守卫）并返回空路径的 info，
    由调用方据此给出友好中文提示——绝不让日志目录可写性带崩主流程。

    返回 {"log_root", "launcher_path", "web_path"} 供断言/展示；降级时 launcher_path/
    web_path 为空串。"""
    log_root = Path(log_root)
    try:
        log_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _install_stderr_fallback_logging(f"日志目录不可写（{log_root}）：{exc}")
    reset_logging()

    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        launcher_handler = logging.handlers.RotatingFileHandler(
            log_root / launcher_name, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        web_handler = logging.handlers.RotatingFileHandler(
            log_root / web_name, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        reset_logging()
        return _install_stderr_fallback_logging(f"日志文件不可写（{log_root}）：{exc}")

    for handler in (launcher_handler, web_handler):
        handler.setFormatter(formatter)
        handler.setLevel(logging.INFO)
        _MANAGED_HANDLERS.append(handler)

    for name in LAUNCHER_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = [launcher_handler]
        lg.setLevel(logging.INFO)
        lg.propagate = False
        _MANAGED_LOGGERS.append(name)
    for name in WEB_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = [web_handler]
        lg.setLevel(logging.INFO)
        lg.propagate = False
        _MANAGED_LOGGERS.append(name)

    return {"log_root": str(log_root),
            "launcher_path": str(log_root / launcher_name),
            "web_path": str(log_root / web_name)}


def _install_stderr_fallback_logging(reason: str) -> dict[str, Any]:
    """日志目录/文件不可写时的降级：launcher 域 logger（biodata.launcher /
    biodata.webview_shell）指向 stderr（守卫）并记一条警告；web 域 logger 静默。

    返回 info 中 launcher_path/web_path 为空串，调用方据此识别「未落盘」并给出提示。"""
    reset_logging()
    stderr_handler = logging.StreamHandler()   # sys.stderr 已由 _guard_streams 守卫为非 None
    stderr_handler.setFormatter(RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    stderr_handler.setLevel(logging.INFO)
    _MANAGED_HANDLERS.append(stderr_handler)
    for name in LAUNCHER_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = [stderr_handler]
        lg.setLevel(logging.INFO)
        lg.propagate = False
        _MANAGED_LOGGERS.append(name)
    launcher_logger = logging.getLogger(LAUNCHER_LOGGER)
    for name in WEB_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = False
        _MANAGED_LOGGERS.append(name)
    launcher_logger.warning("日志目录不可写，降级为标准错误输出：%s", reason)
    return {"log_root": reason.split("（")[0], "launcher_path": "", "web_path": ""}


def reset_logging() -> None:
    """移除 install_logging 安装的 handler 并恢复 propagate（测试隔离用）。"""
    for name in _MANAGED_LOGGERS:
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    _MANAGED_LOGGERS[:] = []
    _MANAGED_HANDLERS[:] = []


class _NullStream(io.TextIOBase):
    """windowed 下 stdout/stderr 为 None 时的空实现（契约 11 流守卫）。"""

    encoding = "utf-8"

    def write(self, *args, **kwargs) -> int:  # noqa: D102
        return 0

    def flush(self) -> None:  # noqa: D102
        pass

    def isatty(self) -> bool:  # noqa: D102
        return False

    @property
    def closed(self) -> bool:
        return False


def _guard_streams() -> None:
    """入口流守卫：sys.stdout/stderr 为 None（windowed/noconsole）时换入空实现。"""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, _NullStream())


def _safe_stderr_write(text: str) -> None:
    """把诊断文本写到 stderr（windowed 下可能为 None，需守卫）；绝不抛。"""
    try:
        stream = getattr(sys, "stderr", None)
        if stream is None:
            return
        stream.write(text)
        stream.flush()
    except Exception:  # noqa: BLE001（兜底写盘失败时不因诊断本身再崩）
        pass


def _safe_message_box(title: str, text: str) -> None:
    """尽力弹一个友好中文 MessageBox（无交互桌面/失败则忽略）；绝不抛。"""
    try:
        if not _win32.is_interactive():
            return
        _win32.message_box(title, text)
    except Exception:  # noqa: BLE001
        pass


def _install_bootstrap_excepthook() -> None:
    """在任何写盘之前安装的最早全局 excepthook：不依赖 logger、不依赖日志目录。

    未捕获异常至少落到 stderr（守卫）+ 尽力 MessageBox——杜绝「LOCALAPPDATA 不可写
    /磁盘满 → 在 excepthook 生效前崩溃 → 静默秒退」。日志配置成功后由
    `_install_excepthook(logger)` 覆盖为滚动日志版（只记日志、不弹框）。"""
    def hook(exc_type, exc_value, exc_tb) -> None:
        text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        _safe_stderr_write(text)
        _safe_message_box(
            "BioData Agent 启动失败",
            "程序启动时遇到无法处理的错误，已尝试写入标准错误输出。\n\n"
            "常见原因：本地数据目录（%LOCALAPPDATA%\\BioDataAgent）不可写或磁盘已满。\n"
            "详情见标准错误输出 / 日志目录 launcher.log。",
        )
    sys.excepthook = hook


def _install_excepthook(logger: logging.Logger) -> None:
    """未捕获异常进日志（windowed 无控制台，默认 traceback 无处可见）。"""
    def hook(exc_type, exc_value, exc_tb) -> None:
        logger.critical("未捕获异常导致退出:\n%s", "".join(traceback.format_exception(exc_type, exc_value, exc_tb)))
    sys.excepthook = hook


# ---------------------------------------------------------------------------
# 原子 JSON 读写
# ---------------------------------------------------------------------------
def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """临时文件 + os.replace 原子写（契约 6）。同目录 .tmp 后缀避免跨盘替换。

    写失败（父目录不可建 / 磁盘满 / 文件不可写）**不抛**：警告到 stderr 与日志后
    返回（runtime.json/instance.json 均非运行必需，best-effort 不因写盘带崩主流程）。"""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        _safe_stderr_write(f"无法写入 {path}：{exc}\n")
        _logger.warning("原子写 %s 失败（best-effort 继续）：%s", path, exc)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def read_json(path: Path) -> Any | None:
    """读 JSON；缺失/损坏一律返回 None（调用方按「不存在」处理，不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------
# config/runtime.json（端口持久化，契约 4）
# ---------------------------------------------------------------------------
def _recover_port_from_raw(raw: str) -> int | None:
    """从（可能损坏的）runtime.json 原始文本里尽力恢复 port 字段。

    只做最小正则兜底（不解析 JSON）：`"port": <n>` 仍在时返回该值（1-65535），
    否则 None。供 corrupt 状态下「沿用原端口、不静默重排」使用。"""
    m = re.search(r'"port"\s*:\s*(\d+)', raw)
    if not m:
        return None
    port = int(m.group(1))
    return port if 1 <= port <= 65535 else None


class RuntimeStore:
    """runtime.json 端口持久化：唯一「固定端口」真源。

    语义：文件缺失或 schema/port 非法 → 无固定端口；否则恒用该端口。
    A2-M5：缺失（首次启动）与损坏（存在但读不出合法端口）区分对待——损坏时
    `read_port_with_state` 尽力恢复原 port 并标记 corrupt，调用方告警并沿用，
    避免静默换端口导致 localStorage origin 漂移。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read_port(self) -> int | None:
        port, _state = self.read_port_with_state()
        return port

    def read_port_with_state(self) -> tuple[int | None, str]:
        """返回 `(port, state)`；state ∈ {"ok", "missing", "corrupt"}。

        - missing：文件不存在/读不出 → 首次启动，允许重新分配并保存；
        - ok：schema 与 port 均合法 → 恒用该端口；
        - corrupt：文件存在但 JSON 损坏/schema 不符/port 非法 → 尽力恢复 port
          （若字段还在），**不静默重排**：调用方应告警并沿用恢复出的端口；
          恢复不出才视同无固定端口。
        """
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError:
            return None, "missing"
        try:
            data = json.loads(raw)
        except ValueError:
            return _recover_port_from_raw(raw), "corrupt"
        if not isinstance(data, dict) or data.get("schema") != RUNTIME_SCHEMA:
            return _recover_port_from_raw(raw), "corrupt"
        port = data.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            return port, "ok"
        return _recover_port_from_raw(raw), "corrupt"

    def save_port(self, port: int) -> None:
        _atomic_write_json(self.path, {
            "schema": RUNTIME_SCHEMA,
            "port": port,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        })


# ---------------------------------------------------------------------------
# run/instance.json（运行实例记录，契约 6/7）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class InstanceRecord:
    schema: str
    pid: int
    port: int
    version: str
    install_root: str
    started_at: str


class InstanceStore:
    """instance.json 读写。write 原子；read 对缺失/损坏/非法 schema 返回 None。"""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def read(self) -> InstanceRecord | None:
        data = read_json(self.path)
        if not isinstance(data, dict) or data.get("schema") != INSTANCE_SCHEMA:
            return None
        try:
            return InstanceRecord(
                schema=str(data["schema"]),
                pid=int(data["pid"]),
                port=int(data["port"]),
                version=str(data["version"]),
                install_root=str(data["install_root"]),
                started_at=str(data["started_at"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def write(self, record: InstanceRecord) -> None:
        _atomic_write_json(self.path, {
            "schema": record.schema,
            "pid": record.pid,
            "port": record.port,
            "version": record.version,
            "install_root": record.install_root,
            "started_at": record.started_at,
        })

    def delete_if_pid(self, pid: int) -> None:
        """仅当记录仍属于给定 PID 时删除（不误删后续实例的写入，契约 9）。"""
        record = self.read()
        if record is not None and record.pid == pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# health 探测（127.0.0.1 环回；无任何外网依赖，契约 12）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HealthResult:
    ok: bool
    service: str
    version: str
    install_root: str
    runtime_mode: str = ""


class HealthProbe:
    """GET /api/health 的薄封装。`probe` 任何失败（连接/超时/解析）返回 None。

    探测对象恒为 loopback（127.0.0.1）——显式 bypass HTTP 代理：用户/加固环境里的
    HTTP(S)_PROXY 若指向不可用代理，会让 attach/端口裁决的健康探测失败（实测
    frozen E2E f03 attach 因此卡死）。契约 12「无外网依赖（urllib 只探 127.0.0.1）」
    本就不应走代理。"""

    def __init__(self, timeout: float = HEALTH_TIMEOUT_S) -> None:
        self.timeout = timeout
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def probe(self, port: int) -> HealthResult | None:
        url = f"http://{HOST}:{port}{HEALTH_PATH}"
        try:
            with self._opener.open(url, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        return HealthResult(
            ok=bool(data.get("ok")),
            service=str(data.get("service", "")),
            version=str(data.get("version", "")),
            install_root=str(data.get("install_root", "")),
            runtime_mode=str(data.get("runtime_mode", "")),
        )

    @staticmethod
    def matches(h: HealthResult, expected_version: str,
                expected_runtime_mode: str = "", expected_install_root: str = "") -> bool:
        """是否「本产品的同一实例」（可 attach）。

        判据除 ok/service/version 外，还必须 `runtime_mode` 与 `install_root`（规范化后）
        都与本安装全等——否则只是**另一份安装**或更早的 instance/source 模式服务，
        不得静默吸附（否则无 PORT 首启会误接被本机其他实例占用，或启动器卡在弹框）。
        mode/root 缺失（空串）时按不匹配处理（健壮兜底，绝不误吸）。"""
        return (h.ok
                and h.service == EXPECTED_SERVICE
                and h.version == expected_version
                and h.runtime_mode == expected_runtime_mode
                and _same_root(h.install_root, expected_install_root))


def _same_root(a: str, b: str) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


# ---------------------------------------------------------------------------
# 端口：先绑后交（契约 4/5）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PortDecision:
    """resolve_port 的裁决结果。二选一：要么自己 bind 出 socket 启动服务，
    要么探测到已有本产品实例（attach_url 非空）——打开现有 URL 后退出。"""
    port: int | None
    sock: socket.socket | None
    attach_url: str | None
    persisted: bool
    warning: str | None


class PortError(RuntimeError):
    """端口策略错误（明确报错、不静默换端口）。"""

    def __init__(self, message: str, port: int | None = None) -> None:
        super().__init__(message)
        self.port = port


class PortOccupiedByOtherError(PortError):
    """固定端口被无关服务占用（契约 4：明确错误，不静默换端口）。"""


class NoPortAvailableError(PortError):
    """7860-7869 全部被无关服务占用。"""


def bind_socket(port: int, host: str = HOST) -> socket.socket | None:
    """探测并占住端口：成功返回已 bind（未 listen）的 socket，失败返回 None。

    **绝不设 SO_REUSEADDR**（实测 2026-08-20：Windows 上双 REUSEADDR 时第二个
    socket 可 bind+listen 已被监听的端口，会误判「空闲」）；无 REUSEADDR 时被占
    端口抛 WinError 10048，监听关闭后可立即重绑（退出即释放）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((host, port))
        return sock
    except OSError:
        sock.close()
        return None


def _url_for(port: int) -> str:
    return f"http://{HOST}:{port}"


def _warn_corrupt_runtime(logger: logging.Logger, path: Path, port: int | None) -> None:
    """runtime.json 损坏/非法时的明确警告（A2-M5）：提示 localStorage origin 可能漂移。

    能恢复原端口 → 本次继续沿用（不静默重排）；恢复不出 → 重新分配，明确提示。"""
    if port is not None:
        logger.warning(
            "runtime.json 已损坏/非法但可恢复原固定端口 %s——本次沿用该端口（不静默重排）。"
            "若此前曾因别的端口访问过页面，localStorage 数据仍按地址（origin）隔离，"
            "更换端口会看不到旧数据。文件：%s", port, path)
    else:
        logger.warning(
            "runtime.json 已损坏/非法且无法恢复端口（%s）——本次将重新分配端口并覆盖该文件；"
            "localStorage 按访问地址（origin）隔离，端口变化后旧页面数据不可见，请以新地址为准。",
            path)


def resolve_port(*, runtime_store: RuntimeStore, health: HealthProbe,
                 expected_version: str, expected_install_root: str,
                 expected_runtime_mode: str = "",
                 preferred_port: int | None = None,
                 allow_persist: bool = True,
                 logger: logging.Logger | None = None) -> PortDecision:
    """契约 4 的端口分配主逻辑。`preferred_port`（env PORT）为一次性调试覆盖，
    不参与持久化；其余情况下固定端口优先、未固定则 7860 → 7861-7869 漂移并保存。

    A2-M5：runtime.json 损坏（corrupt）与缺失（missing）区分对待——损坏时告警并
    沿用恢复出的原端口（不静默重排）；恢复不出才重新分配。"""
    log = logger or _logger
    if preferred_port is not None:
        fixed = None
    else:
        fixed, state = runtime_store.read_port_with_state()
        if state == "corrupt":
            _warn_corrupt_runtime(log, runtime_store.path, fixed)
    if fixed is not None:
        sock = bind_socket(fixed)
        if sock is not None:
            return PortDecision(fixed, sock, None, False, None)
        hit = health.probe(fixed)
        if hit is not None and health.matches(hit, expected_version,
                                              expected_runtime_mode, expected_install_root):
            return PortDecision(fixed, None, _url_for(fixed), False, None)
        if hit is not None:
            # 端口上有健康响应但不匹配本安装 → 本产品异版本/异安装（A2-L1 文案区分）
            raise PortOccupiedByOtherError(
                f"固定端口 {fixed} 上运行着另一份 BioData Agent"
                f"（service={hit.service} version={hit.version} install_root={hit.install_root}）。"
                "请先关闭该实例再重试（启动器不会静默更换端口）。", port=fixed)
        raise PortOccupiedByOtherError(
            f"固定端口 {fixed} 已被无关程序占用且不是 BioData Agent。"
            f"请关闭占用 {fixed} 端口的程序后重试（启动器不会静默更换端口）。", port=fixed)

    probe_first = preferred_port if preferred_port is not None else DEFAULT_PORT
    sock = bind_socket(probe_first)
    if sock is not None:
        if allow_persist:
            # 保存会覆盖（可能损坏的）runtime.json——新端口已生效，属预期收敛
            runtime_store.save_port(probe_first)  # 恒用该端口，保 localStorage origin
        return PortDecision(probe_first, sock, None, allow_persist, None)
    hit = health.probe(probe_first)
    if hit is not None and health.matches(hit, expected_version,
                                          expected_runtime_mode, expected_install_root):
        return PortDecision(probe_first, None, _url_for(probe_first), False, None)

    # 端口上有本产品响应但 mode/root/version 不匹配（另一份安装或 source 实例）——
    # 不静默吸附，继续漂移绑新端口；记一条日志供诊断。
    if hit is not None and hit.service == EXPECTED_SERVICE:
        log.warning(
            "端口 %s 上有本产品响应但不匹配本安装（service=%s version=%s "
            "runtime_mode=%s install_root=%s），不吸附，改绑漂移端口", probe_first,
            hit.service, hit.version, hit.runtime_mode, hit.install_root)

    for candidate in range(DRIFT_PORT_START, DRIFT_PORT_END + 1):
        sock = bind_socket(candidate)
        if sock is not None:
            if allow_persist:
                runtime_store.save_port(candidate)
            return PortDecision(candidate, sock, None, allow_persist, None)
    raise NoPortAvailableError(
        f"端口 {probe_first} 与 {DRIFT_PORT_START}-{DRIFT_PORT_END} 均被其他服务占用。"
        "请关闭占用这些端口的程序后重试。")


# ---------------------------------------------------------------------------
# 二次启动 attach（契约 7）：mutex 已被占 → 验证 instance.json + health，不盲信
# ---------------------------------------------------------------------------
def try_attach(*, runtime_store: RuntimeStore, instance_store: InstanceStore,
               health: HealthProbe, expected_version: str, expected_install_root: str,
               expected_runtime_mode: str = "",
               is_pid_alive: Callable[[int], bool], open_url: Callable[[str], None],
               timeout: float = ATTACH_TIMEOUT_S, poll: float = ATTACH_POLL_S,
               logger: logging.Logger | None = None) -> tuple[bool, str]:
    """尝试连接运行中的实例：成功（open_url 已调用）返回 (True, url)。

    验证链（每步都独立校验，任一失败不盲信）：instance.json 可读且 schema 合法 →
    PID 存活 → 记录 port 的 health 的 service/version/install_root 全匹配。
    记录缺失/损坏/过期时回退到固定端口（或 7860）的 health 探测（覆盖「实例刚
    启动、instance.json 尚未落盘」的窗口），并轮询等待 instance.json 补写。

    A2-M4：`timeout` 对齐 warm 预热耗时（默认 60s）；每轮验证 PID——记录里的实例
    进程已退出时**立即失败**，不空转到超时。A2-M5：runtime.json 损坏时告警并用
    恢复出的原端口探测（不静默改探 7860）。"""
    log = logger or _logger
    deadline = time.monotonic() + timeout

    def _health_matches_port(port: int) -> HealthResult | None:
        hit = health.probe(port)
        if hit is None or not health.matches(hit, expected_version,
                                             expected_runtime_mode, expected_install_root):
            return None
        return hit

    probed_fixed = False
    while time.monotonic() < deadline:
        record = instance_store.read()
        if record is not None:
            if not is_pid_alive(record.pid):
                log.warning("instance.json 记录的 PID %s 已不存在（实例已退出），attach 立即放弃",
                            record.pid)
                return False, ""
            elif not _same_root(record.install_root, expected_install_root):
                log.warning("instance.json 的 install_root 与本安装不一致（%s），不吸附", record.install_root)
            elif record.version != expected_version:
                log.warning("instance.json 的版本 %s ≠ 本安装 %s，不吸附", record.version, expected_version)
            elif _health_matches_port(record.port) is not None:
                url = _url_for(record.port)
                open_url(url)
                return True, url
        if not probed_fixed:
            probed_fixed = True
            probe_port, state = runtime_store.read_port_with_state()
            if state == "corrupt":
                _warn_corrupt_runtime(log, runtime_store.path, probe_port)
            if probe_port is None:
                probe_port = DEFAULT_PORT
            hit = _health_matches_port(probe_port)
            if hit is not None:
                url = _url_for(probe_port)
                open_url(url)
                return True, url
        time.sleep(poll)
    return False, ""


def _default_is_pid_alive(pid: int) -> bool:
    return _win32.is_pid_alive(pid)


# ---------------------------------------------------------------------------
# 语义重排模型预热（镜像 scripts/run_web.py 语义；绝不下载，契约 12）
# ---------------------------------------------------------------------------
def warm_recall_if_available(logger: logging.Logger) -> str:
    """启动期预热本地语义重排模型。与 run_web.warm_web_recall 同语义（本地加载、
    无模型秒过、绝不下载、绝不阻断开服），但内联在启动器内，frozen 无需打包 scripts/。

    返回 "disabled" | "unavailable" | "warmed" | "failed"。"""
    if os.getenv("BIODATA_SKIP_RECALL_WARM", "").strip().lower() in ("1", "true", "yes", "on"):
        return "disabled"
    try:
        from dataset_recommender.retrieval.vector_recall import recall_backend_local_available, warm_recall_backend
    except Exception:
        return "unavailable"
    if not recall_backend_local_available("cross_encoder"):
        return "unavailable"
    logger.info("预热本地语义重排模型（cross_encoder）… 首次加载较慢，请稍候。")
    started = time.perf_counter()
    try:
        ok = warm_recall_backend("cross_encoder")
    except Exception as exc:  # 防御：warm 内部已自吞，这里再兜一层
        logger.warning("语义模型预热异常（%.1fs）：%s: %s", time.perf_counter() - started,
                       type(exc).__name__, exc)
        return "failed"
    if ok:
        logger.info("语义模型预热完成（%.1fs）", time.perf_counter() - started)
        return "warmed"
    logger.info("语义模型预热未成功（%.1fs）——本次运行 auto 回退确定性排序", time.perf_counter() - started)
    return "failed"


def _warm_worker(warm: Callable[[logging.Logger], str], logger: logging.Logger) -> None:
    """后台预热线程体：预热结果只进日志，绝不抛、绝不影响服务线程。"""
    try:
        result = warm(logger)
        logger.info("后台预热完成（result=%s）", result)
    except Exception as exc:  # noqa: BLE001（warm 自身已自吞，这里再兜一层）
        logger.warning("后台预热异常：%s: %s", type(exc).__name__, exc)


def start_background_warm(warm: Callable[[logging.Logger], str], logger: logging.Logger) -> None:
    """把 warm 预热放到独立 daemon 线程，与启动判活/服务启动解耦（边缘修复第 5 项）。

    本地模型引导落地后首次预热（cross_encoder 加载）可能超过 STARTUP_TIMEOUT_S=60s；
    若仍同步跑在 `_serve_entry` 里，`_wait_server_started` 会把它误判为「启动失败」。
    预热结果只用于日志与后续请求的 auto 回退，不影响服务是否就绪——因此必须后台化。
    load_cross_encoder 用 _MODEL_LOCK 单飞 + 失败不缓存，与请求内首次加载并发安全。"""
    threading.Thread(
        target=_warm_worker, args=(warm, logger),
        name="biodata-warm", daemon=True,
    ).start()


def _warn_stale_root_env(paths: AppPaths) -> None:
    """frozen 下 BIODATA_DATA_ROOT 重定向后，旧默认根的 .env 不会自动跟随（A2-L5）。

    最小动作：仅检测「旧默认根 config/.env 存在而新根 config/.env 缺失」→ 警告日志
    （提示原 LLM 配置未随重定向生效）。不做自动复制——不改变用户意图。"""
    if paths.runtime_mode != "frozen":
        return
    try:
        default_root = default_data_root_frozen()
    except OSError:
        return
    if Path(paths.data_root).resolve() == default_root.resolve():
        return
    old_env = default_root / "config" / ".env"
    new_env = Path(paths.config_root) / ".env"
    if old_env.is_file() and not new_env.is_file():
        _logger.warning(
            "BIODATA_DATA_ROOT 指向 %s（非默认根）；旧默认根 %s 存在 config/.env，"
            "但新根 %s/config/.env 不存在——.env 不会自动跟随重定向，原 LLM 配置未生效。"
            "如需沿用，请把 .env 复制到 %s。", paths.data_root, default_root,
            paths.config_root, new_env)


def _warn_orphaned_uploads(paths: AppPaths, logger: logging.Logger) -> None:
    """启动时（装载外部库前）扫描 user_external_dir 的 upload_*.json，凡在
    .userdata/uploads_journal.jsonl 无对应 filename 的记一条告警（边缘修复第 7 项）。

    只告警、不阻塞、不自动删；扫描函数住在 uploads.py（与账本格式同源），这里只做
    轻量挂钩。异常（目录/账本不可读等）一律降级为一条日志，绝不阻断启动。"""
    try:
        from dataset_recommender.corpus.uploads import find_orphaned_uploads
        orphans = find_orphaned_uploads(
            paths.user_external_dir,
            paths.userdata_dir / "uploads_journal.jsonl",
        )
    except Exception as exc:  # noqa: BLE001（扫描失败不阻塞启动）
        logger.warning("扫描无账上传文件失败（不阻塞启动）：%s", exc)
        return
    if orphans:
        logger.warning(
            "发现 %d 个 upload_*.json 在 uploads_journal.jsonl 中无对应 filename（不自动删除）：%s",
            len(orphans), ", ".join(orphans[:20]),
        )


# ---------------------------------------------------------------------------
# 浏览器 / 提示（可注入的薄封装，契约 10）
# ---------------------------------------------------------------------------
def _default_open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _default_notify(title: str, text: str) -> None:
    """windowed 下用 MessageBoxW；无交互桌面或调用失败 → 仅日志，绝不阻断。

    A2-L2：无交互桌面（服务会话/无头环境）下 MessageBoxW 会失败或挂起——先探测
    交互会话，非交互直接降级为仅日志，不尝试弹框。"""
    try:
        if not _win32.is_interactive():
            _logger.info("非交互桌面，提示降级为日志 [%s]：%s", title, text)
            return
        _win32.message_box(title, text)
    except Exception:
        _logger.error("提示框失败（%s）：%s", title, text)


# ---------------------------------------------------------------------------
# 托盘交互面（可注入，测试用 fake；契约 8）
# ---------------------------------------------------------------------------
@dataclass
class TrayHandlers:
    """托盘菜单动作回调（由 Launcher 绑定真实行为，Win32Tray/Fake 统一消费）。"""
    on_open: Callable[[], None]
    on_open_logs: Callable[[], None]
    on_copy: Callable[[], None]
    on_quit: Callable[[], None]


def _default_tray_factory(handlers: TrayHandlers) -> Any:
    return _win32.Win32Tray(on_open=handlers.on_open, on_open_logs=handlers.on_open_logs,
                            on_copy=handlers.on_copy, on_quit=handlers.on_quit)


# ---------------------------------------------------------------------------
# 启动器编排
# ---------------------------------------------------------------------------
class Launcher:
    """桌面启动器编排。全部依赖可注入（tests 用 fake 替换托盘/浏览器/提示/PID）。

    生命周期：guard 流 → 配日志 → mutex → （已存在 → attach / 否则主实例）
    → 端口先绑后交 → 写 instance.json → 托盘「正在启动」→ 服务线程
    → 「运行中」→ 开浏览器 → 消息泵/轮询 → 退出（should_exit → 等线程 → 关
    socket → 删 instance.json → 释放 mutex）。"""

    def __init__(self, *, paths: AppPaths | None = None,
                 win32: Any = None, health: HealthProbe | None = None,
                 browser: Callable[[str], bool] | None = None,
                 window_runner: Callable[[str], str] | None = None,
                 notify: Callable[[str, str], None] | None = None,
                 version: str | None = None, app: Any = None,
                 tray_factory: Callable[[TrayHandlers], Any] | None = None,
                 is_pid_alive: Callable[[int], bool] | None = None,
                 warm: Callable[[logging.Logger], str] | None = None,
                 attach_timeout: float | None = None, attach_poll: float | None = None,
                 logger: logging.Logger | None = None,
                 shell_mode: bool = False) -> None:
        self._paths = paths
        self._win32 = win32 or _win32
        self._health = health or HealthProbe()
        self._browser = browser or _default_open_browser
        # 桌面壳专用 runner（2026-08-21 壳批）：独立于 browser——browser 仍被二次启动
        # attach 路径复用（浏览器语义），window_runner 只服务主实例就绪后的窗口生命周期
        # （阻塞至关窗）。返回字符串语义见 app/webview_shell.py（WINDOW_CLOSED/FALLBACK_BROWSER）。
        self._window_runner = window_runner
        self._notify = notify or _default_notify
        self._version = version
        self._app = app
        self._tray_factory = tray_factory or _default_tray_factory
        self._is_pid_alive = is_pid_alive or _default_is_pid_alive
        self._warm = warm or warm_recall_if_available
        self._attach_timeout = attach_timeout if attach_timeout is not None else ATTACH_TIMEOUT_S
        self._attach_poll = attach_poll if attach_poll is not None else ATTACH_POLL_S
        self._log = logger or _logger
        self._shell_mode = bool(shell_mode)
        self._server: Any = None
        self._server_thread: threading.Thread | None = None
        self._url = ""
        self._log_root: Path | None = None
        self._tray: Any = None
        self._runtime_store: RuntimeStore | None = None
        self._instance_store: InstanceStore | None = None

    # -- 公共入口 ----------------------------------------------------------
    def run(self, argv: list[str] | None = None) -> int:
        _guard_streams()
        # 最早安装全局 excepthook（任何写盘之前）：LOCALAPPDATA 不可写/磁盘满时不再
        # 静默秒退，而是 stderr + 友好 MessageBox；日志就绪后 _install_excepthook 覆盖。
        _install_bootstrap_excepthook()
        args = _parse_args(argv)
        # BIODATA_SHELL=window：安装包快捷方式不便传参数时的环境变量通道（与 --window 同义）。
        if os.getenv("BIODATA_SHELL", "").strip().lower() == "window":
            args.window = True
        paths = self._paths or get_app_paths()
        # 契约 2：启动第一行先配脱敏滚动日志（windowed 无控制台，之后一切输出走日志）。
        self._log_root = paths.log_root
        info = install_logging(paths.log_root)
        _install_excepthook(self._log)
        if not info.get("launcher_path"):
            self._notify(
                "BioData Agent 日志不可用",
                f"无法写入日志目录：{paths.log_root}\n"
                "（常见原因：磁盘已满或本地数据目录不可写）。\n"
                "本次运行不记录日志，程序继续启动。",
            )
        _warn_stale_root_env(paths)
        if args.migrate_from is not None:
            self._log.info("--migrate-from 迁移开始（来源：%r，include_models=%s）",
                           args.migrate_from, args.include_models)
            rc = self._migrate_from(args.migrate_from, paths, args.include_models)
            if rc != 0:
                return rc
            self._log.info("--migrate-from 迁移完成，继续正常启动")
        self._log.info("BioData Agent 桌面启动器开始（mode=%s install_root=%s data_root=%s log=%s）",
                       paths.runtime_mode, paths.install_root, paths.data_root, paths.log_root)
        mutex = self._win32.open_mutex(MUTEX_NAME)
        try:
            if mutex.already_exists:
                self._log.info("检测到已有 BioData Agent 实例在运行（mutex %s 已被占）", MUTEX_NAME)
                return self._second_instance(paths, mutex)
            self._log.info("mutex 获取成功，作为主实例启动")
            return self._primary_instance(paths, mutex, args)
        finally:
            mutex.close()

    # -- 数据迁移（契约 13，W5）-----------------------------------------------
    def _migrate_from(self, source: str, paths: AppPaths, include_models: bool) -> int:
        """执行 --migrate-from：先 plan（dry-run 零写入，含冲突判定与链接穿透拒绝）→
        日志留痕 → run_migration（staging 全成功才开写 + 逐文件落位，可重入）。
        失败 → 明确提示 + 返回非零，不启动服务。"""
        try:
            plan = plan_migration(source, paths, include_models=include_models)
        except OSError as exc:
            self._log.error("迁移扫描失败：%s", exc)
            self._notify("BioData Agent 数据迁移失败", f"无法扫描来源目录：{exc}")
            return 1
        if not plan.valid:
            self._log.error("--migrate-from 来源不合法：%s", plan.reason)
            self._notify("BioData Agent 数据迁移失败", plan.reason)
            return 1
        self._log.info("迁移计划（dry-run）：%d 项 / %d 字节；不迁移 %d 项",
                       len(plan.items), plan.total_bytes, len(plan.rejected))
        for item in plan.items:
            note = ("冲突保留双方" if item.conflict
                    else ("幂等跳过（已迁移）" if item.identical else "新增"))
            self._log.info("  迁移 %s：%s（%d 字节，%s）", item.kind, item.rel, item.size, note)
        for rej in plan.rejected:
            self._log.info("  不迁移 %s：%s", rej["rel"], rej["reason"])
        try:
            report = run_migration(source, paths, include_models=include_models,
                                   plan=plan, logger=self._log)
        except MigrationError as exc:
            self._log.error("迁移失败：%s", exc.message)
            self._notify("BioData Agent 数据迁移失败", exc.message)
            return 1
        self._log.info("迁移报告：%s", report["summary"])
        return 0

    # -- 二次启动（契约 7）-------------------------------------------------
    def _second_instance(self, paths: AppPaths, mutex: Any) -> int:
        runtime_store = RuntimeStore(paths.config_root / RUNTIME_FILENAME)
        instance_store = InstanceStore(paths.run_root / INSTANCE_FILENAME)
        version = self._version or _default_version()
        install_root = str(paths.install_root)
        ok, url = try_attach(
            runtime_store=runtime_store, instance_store=instance_store,
            health=self._health, expected_version=version, expected_install_root=install_root,
            expected_runtime_mode=paths.runtime_mode,
            is_pid_alive=self._is_pid_alive, open_url=self._open_browser,
            timeout=self._attach_timeout, poll=self._attach_poll,
            logger=self._log,
        )
        if ok:
            self._log.info("已连接运行中的实例：%s", url)
            return 0
        self._notify(
            "BioData Agent 已在运行",
            "另一个实例正在运行，但无法确认其可用地址（实例记录缺失、损坏或验证未通过）。\n"
            "请关闭已运行的 BioData Agent 窗口后重试。",
        )
        self._log.warning("二次启动未能定位运行中的实例（attach 失败）")
        return 1

    # -- 主实例 ------------------------------------------------------------
    def _primary_instance(self, paths: AppPaths, mutex: Any, args: argparse.Namespace) -> int:
        # 装载外部库前做一次「无账 upload_*.json」告警（只告警、不阻塞、不自动删）。
        _warn_orphaned_uploads(paths, self._log)
        runtime_store = RuntimeStore(paths.config_root / RUNTIME_FILENAME)
        instance_store = InstanceStore(paths.run_root / INSTANCE_FILENAME)
        self._runtime_store = runtime_store
        self._instance_store = instance_store
        version = self._version or _default_version()
        install_root = str(paths.install_root)
        preferred_port = _env_preferred_port()
        try:
            decision = resolve_port(
                runtime_store=runtime_store, health=self._health,
                expected_version=version, expected_install_root=install_root,
                expected_runtime_mode=paths.runtime_mode,
                preferred_port=preferred_port,
                allow_persist=preferred_port is None,
            )
        except PortError as exc:
            self._notify("BioData Agent 无法启动", str(exc))
            self._log.error("端口裁决失败：%s", exc)
            return 1
        if decision.attach_url is not None:
            # attach 分支绝不弹模态通知：warning 只走日志，随后照常打开现有地址并退出。
            # （模态 MessageBoxW 在交互会话会阻塞到用户点确定——无 PORT 首启误 attach 或
            #  真正二次启动时都不应把启动器卡死在该弹框上。）
            if decision.warning:
                self._log.warning("%s；%s", decision.warning, decision.attach_url)
            else:
                self._log.info("检测到已运行实例，直接打开：%s", decision.attach_url)
            self._open_browser(decision.attach_url)
            return 0
        assert decision.sock is not None and decision.port is not None
        if decision.persisted:
            self._log.info("端口 %s 已持久化到 runtime.json（后续启动恒用该端口）", decision.port)

        stale = instance_store.read()
        if stale is not None:
            self._log.info("发现遗留实例记录（PID %s），验证后覆盖", stale.pid)
        record = InstanceRecord(
            schema=INSTANCE_SCHEMA, pid=os.getpid(), port=decision.port,
            version=version, install_root=install_root,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        instance_store.write(record)
        self._log.info("instance.json 已写入（port=%s pid=%s）", decision.port, os.getpid())

        app = self._app or _default_app()
        config = uvicorn.Config(
            app, host=HOST, port=decision.port, log_level="info", log_config=None,
            access_log=True, timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S, workers=1,
        )
        if config.workers != 1:
            # 内存 job/缓存/壳层状态均以单进程为设计边界。未来改启动参数时必须先完成
            # 状态外置，不能静默把同一桌面实例拆成多个彼此不一致的 worker。
            raise RuntimeError("BioData Agent 当前只支持单 worker 运行。")
        server = uvicorn.Server(config)
        self._server = server
        sock = decision.sock
        thread = threading.Thread(
            target=self._serve_entry, args=(server, sock, app, decision.port),
            name="biodata-web-server", daemon=True,
        )
        self._server_thread = thread
        thread.start()

        # 壳模式：托盘**延后**——壳成功则全程无托盘（窗口即界面）；壳不可用
        # 回退浏览器时由 _run_foreground 按需恢复托盘（Codex 评审：提前抑制会让回退路径
        # 无托盘永久驻留，只能结束进程）。
        shell = self._shell_mode and self._window_runner is not None
        tray = None if shell else self._make_tray(decision.port, args.no_tray)
        try:
            return self._run_foreground(server, sock, instance_store, mutex, tray, decision.port,
                                        no_tray=args.no_tray)
        except KeyboardInterrupt:
            # 控制台调试模式 Ctrl+C：干净退出（windowed 无控制台，走托盘「退出」）。
            self._log.warning("收到中断信号，干净退出…")
            return self._shutdown(server, sock, instance_store, mutex, tray, exit_code=130)

    def _run_foreground(self, server: Any, sock: socket.socket,
                        instance_store: InstanceStore, mutex: Any, tray: Any,
                        port: int, no_tray: bool = False) -> int:
        """服务启动后：等就绪 → 托盘「运行中」→ 开浏览器 → 消息泵/轮询 → 干净退出。"""
        started = self._wait_server_started(server, self._server_thread)
        if not started:
            self._log.error("服务启动失败（启动超时或线程提前退出）")
            if tray is not None:
                tray.update_status("启动失败")
            self._notify("BioData Agent 启动失败",
                         "Web 服务未能启动，详见日志目录 launcher.log。\n"
                         "（请用托盘/资源管理器打开日志目录查看原因）")
            return self._shutdown(server, sock, instance_store, mutex, tray, exit_code=1)
        if tray is not None:
            tray.update_status("运行中")
        self._url = _url_for(port)
        self._log.info("服务已就绪：%s", self._url)
        if os.getenv("BIODATA_NO_BROWSER", "").strip() != "1":
            if self._shell_mode and self._window_runner is not None:
                # 桌面窗口模式（2026-08-21 壳批，Codex 评审修订）：专用 window_runner 阻塞
                # 至关窗——WINDOW_CLOSED=关窗即退出（干净关停走既有 _shutdown）；
                # FALLBACK_BROWSER=壳不可用（缺依赖/缺 WebView2/建窗失败，已开系统浏览器）
                # → **此时恢复托盘**维持服务（除非 --no-tray），与浏览器模式一致。
                # 条件与 run() 的托盘预判同口径（shell=True 且 runner 缺失时按浏览器路径走，
                # 避免预判不建托盘而此处不进壳分支的双托盘/无托盘分歧）。
                from dataset_recommender.app.webview_shell import WINDOW_CLOSED
                result = self._window_runner(self._url)
                if result == WINDOW_CLOSED:
                    self._log.info("桌面窗口已关闭，应用退出。")
                    return self._shutdown(server, sock, instance_store, mutex, tray, exit_code=0)
                self._log.warning("桌面窗口不可用，已回退系统浏览器（见上方日志原因）。")
                self._notify("桌面窗口模式不可用",
                             f"已改为在系统浏览器中打开：{self._url}\n（原因详见日志目录 launcher.log）")
                tray = self._make_tray(port, no_tray)
            elif not self._open_browser(self._url):
                self._notify(
                    "浏览器打开失败",
                    f"访问地址：{self._url}\n（托盘菜单「复制访问地址」可复制；服务仍在运行）",
                )
        if tray is not None:
            tray.run_message_loop()  # 阻塞至「退出」回调触发 quit_message_loop
        else:
            while not server.should_exit and self._server_thread is not None \
                    and self._server_thread.is_alive():
                time.sleep(0.25)
        server.should_exit = True  # 保险：无托盘轮询路径退出
        return self._shutdown(server, sock, instance_store, mutex, tray, exit_code=0)

    # -- 服务线程 / 等待 / 托盘 / 清理 -------------------------------------
    def _serve_entry(self, server: Any, sock: socket.socket, app: Any, port: int) -> None:
        """服务线程：后台预热（独立 daemon 线程，不阻塞开服）→ asyncio 跑 uvicorn。

        预热与启动判活解耦（边缘修复第 5 项）：首次预热可能超 60s，若同步跑在这里，
        `_wait_server_started` 会误判「启动失败」。服务立即 start，`server.started`
        尽快置位，浏览器/窗口按既有口径在 health 就绪后再打开。"""
        try:
            start_background_warm(self._warm, self._log)
            asyncio.run(server.serve(sockets=[sock]))
        except BaseException as exc:  # noqa: BLE001（线程兜底：异常进日志，主线程据 started 判定失败）
            self._log.error("Web 服务线程异常退出（port=%s）：%s", port, exc)

    def _wait_server_started(self, server: Any, thread: threading.Thread) -> bool:
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if getattr(server, "started", False):
                return True
            if not thread.is_alive():
                return False
            time.sleep(0.05)
        return False

    def _make_tray(self, port: int, no_tray: bool) -> Any:
        if no_tray:
            self._log.info("无托盘模式（--no-tray 或窗口模式）")
            return None
        url = _url_for(port)
        log_root = str(self._log_root or "")
        handlers = TrayHandlers(
            on_open=lambda: self._open_browser(url),
            on_open_logs=lambda: self._win32.open_directory(log_root),
            on_copy=lambda: self._win32.set_clipboard_text(url),
            on_quit=self._request_quit,
        )
        try:
            tray = self._tray_factory(handlers)
            tray.create(url=url, status="正在启动")
            self._tray = tray
            return tray
        except Exception as exc:  # 无交互桌面/托盘失败 → 降级轮询，绝不阻断（契约 8/12）
            self._tray = None
            self._log.warning("托盘不可用，降级为无托盘模式：%s", exc)
            return None

    def _request_quit(self) -> None:
        """托盘「退出」回调：置 should_exit 让服务线程优雅关停，并结束消息泵。

        消息泵退出后主线程进入 `_shutdown`：join 服务线程（等服务完成
        uvicorn 关停并释放端口）→ 关 socket → 删 instance.json → 释放 mutex。"""
        self._log.info("收到退出请求")
        if self._server is not None:
            self._server.should_exit = True
        if self._tray is not None:
            self._tray.quit_message_loop()

    def _shutdown(self, server: Any, sock: socket.socket | None,
                  instance_store: InstanceStore, mutex: Any, tray: Any,
                  exit_code: int) -> int:
        """契约 9 的干净退出：等线程 → 关 socket → 删本 PID 的 instance.json → 释放 mutex。"""
        server.should_exit = True
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=SHUTDOWN_JOIN_S)
            if thread.is_alive():
                self._log.warning("服务线程未按时退出，强制结束")
                try:
                    server.force_exit = True
                except Exception:  # noqa: BLE001
                    pass
                thread.join(timeout=2.0)
        if sock is not None:
            try:
                sock.close()  # 幂等；uvicorn shutdown 已关底层 fd
            except OSError:
                pass
        try:
            instance_store.delete_if_pid(os.getpid())
        except OSError as exc:
            self._log.warning("删除 instance.json 失败：%s", exc)
        mutex.close()
        if tray is not None:
            try:
                tray.destroy()
            except Exception as exc:  # noqa: BLE001
                self._log.warning("托盘销毁失败：%s", exc)
        self._tray = None
        self._log.info("已退出（exit_code=%s）", exit_code)
        return exit_code

    def _open_browser(self, url: str) -> bool:
        return self._browser(url)


# ---------------------------------------------------------------------------
# 数据迁移：--migrate-from（契约 13，W5 实现）
# ---------------------------------------------------------------------------
#: 旧便携版根必须同时具备的标识：启动脚本 + 产品源码标记。
MIGRATE_REQUIRED_FILES = ("start-web.bat", "src/dataset_recommender/__init__.py")
#: 已知 .userdata 运行产物（单文件白名单）：账户/会话/账本/引文/回收站清单全部随迁移
#: 走；**未知** .userdata 文件一律不迁移（检测并报告，防把运行时垃圾带进新实例）。
MIGRATE_USERDATA_FILES = (
    "accounts.json",
    "sessions.json",
    "agent_fallbacks.jsonl",
    "curate_example_candidates.jsonl",
    "curate_examples.jsonl",
    "curate_net_ledger.jsonl",
    "oov_terms.jsonl",
    "uploads_journal.jsonl",
)
#: .userdata 下整目录迁移项（回收站、引文导出——目录内逐文件随迁）。
MIGRATE_USERDATA_DIRS = ("recycle", "citations")
#: database/external 下用户数据文件前缀。上传全部经 `new_upload_name` 落
#: `upload_<时间戳>_<原名>.json`（upload_/curate_/curate_sync_* 命名空间统一带此前缀）；
#: 官方快照（geo.json 等）不带此前缀 → 天然区分、不迁移。
MIGRATE_UPLOAD_PREFIX = "upload_"
#: 顶层明确不迁移的已知项（存在才进报告，便于用户核对；不复制）。
#: 2026-08-27 迁移批：研究/采集流水线已整体迁至顶层 `research/`（同样非用户数据）；
#: 原 database 下的 workstream 条目保留在清单里只为识别存量安装目录（可能尚未迁移）。
MIGRATE_KNOWN_EXCLUDED = (
    "src", "scripts", "web", "tests", "docs", "eval", "prompts", "automation",
    "research",
    # 旧安装目录迁移识别兼容：存量安装的流水线可能还留在 database 下（2026-08-27 起
    # 顶层已是 research/），两个都认，存在才进报告。
    "database/base", "database/trace", "database/workstream",
    "logs", "run", "outputs", "exports", "services",
    ".venv", ".git", "__pycache__", "开发日志归档", "协同", "使用教程",
)


class MigrationError(RuntimeError):
    """迁移错误（校验/执行失败）。携带 code 供机器识别，message 供日志/提示。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MigrationItem:
    """迁移计划单条。`rel` 为相对源根的 posix 路径（staging 键）；`dest` 为最终落位
    （冲突时带 `.migrated` 后缀的路径）；`conflict=True` 表示保留双方（既有文件不动）。"""
    rel: str
    kind: str
    src: Path
    dest: Path
    size: int
    conflict: bool = False
    identical: bool = False


@dataclass(frozen=True)
class MigrationPlan:
    """dry-run 结果（纯只读、零写入）。valid=False 时 reason 给出拒绝原因。"""
    valid: bool
    reason: str = ""
    items: tuple[MigrationItem, ...] = ()
    rejected: tuple[dict[str, str], ...] = ()
    total_bytes: int = 0


def _sha256_file(path: Path, chunk: int = 1 << 16) -> str:
    """分块 SHA-256（models 可能达数百 MB，不整读进内存）。"""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _same_bytes(a: Path, b: Path) -> bool:
    """逐字节比较（尺寸不同直接短路）。"""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
    except OSError:
        return False
    return _sha256_file(a) == _sha256_file(b)


def _link_escape_reason(candidate: Path, source_root: Path) -> str | None:
    """源条目 resolve() 后必须仍位于源根之内（A1-M2 防 symlink/junction 穿透迁出）。

    条目本身是链接（symlink/junction）**允许**，但链接解析目标必须在源根内；指向源根
    外的链接会随迁移把外部数据带进本实例（或跟随复制意外内容）→ 拒绝。staging 前对
    每个条目执行；返回 None 表示放行，否则返回拒绝原因。"""
    try:
        src_root = source_root.resolve()
        resolved = candidate.resolve()
    except OSError as exc:
        return f"无法解析（{candidate}）：{exc}"
    try:
        resolved.relative_to(src_root)
    except ValueError:
        return f"解析后位于源目录之外（{resolved}），拒绝迁移（防链接穿透）"
    return None


def _conflict_path(dest: Path) -> Path:
    """冲突副本路径：`<name>.<YYYYmmdd_HHMMSS_ffffff>.migrated`（绝不复用既有文件名）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    return dest.with_name(f"{dest.name}.{stamp}.migrated")


def _dir_size(directory: Path) -> int:
    return sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())


def _dir_identical(src_dir: Path, dest_dir: Path) -> bool:
    """整目录逐文件比对（同一文件集合且逐字节相同）→ 可幂等跳过。"""
    if not dest_dir.is_dir():
        return False
    try:
        src_files = {p.relative_to(src_dir): p for p in src_dir.rglob("*") if p.is_file()}
        dest_files = {p.relative_to(dest_dir): p for p in dest_dir.rglob("*") if p.is_file()}
    except OSError:
        return False
    if set(src_files) != set(dest_files):
        return False
    return all(_same_bytes(s, dest_files[rel]) for rel, s in src_files.items())


def _find_matching_migrated(dest: Path, src: Path) -> Path | None:
    """查找目标旁已有的 `<name>.<stamp>.migrated` 文件中与源同字节者（重入幂等：
    上次冲突已保留的副本再次出现时直接跳过，不产生新副本）。"""
    for cand in dest.parent.glob(f"{dest.name}.*.migrated"):
        if cand.is_file() and _same_bytes(src, cand):
            return cand
    return None


def _find_matching_migrated_dir(dest: Path, src_dir: Path) -> Path | None:
    """目录版 `_find_matching_migrated`。"""
    for cand in dest.parent.glob(f"{dest.name}.*.migrated"):
        if cand.is_dir() and _dir_identical(src_dir, cand):
            return cand
    return None


def plan_migration(source: Path | str, paths: AppPaths, *, include_models: bool = False) -> MigrationPlan:
    """扫描旧便携版根，产出迁移计划（**纯只读 dry-run，零写入**）。

    校验：目录必须同时含 `start-web.bat` 与产品源码标记 `src/dataset_recommender/`；
    源不得等于/包含/被包含于本实例 data_root；每个允许项 resolve() 后必须仍位于
    源根之内（A1-M2：symlink/junction 指向源根外 → 拒绝进 rejected，防穿透迁出）。
    只允许白名单内的用户数据项；检测到但不在白名单内的项一律进 `rejected`
    （代码/官方快照/tests/日志缓存等），不复制。冲突判定：目标已存在 → 同字节视为
    「已迁移（幂等跳过）」；不同字节 → 保留双方，迁移副本带 `.migrated` 后缀。"""
    src = Path(source).expanduser()
    if not src.is_dir():
        return MigrationPlan(False, reason=f"来源目录不存在：{src}")
    # 先做数据根包含关系守卫（防递归嵌套/自迁移），再做产品目录标记校验
    try:
        src_resolved = src.resolve()
    except OSError:
        return MigrationPlan(False, reason=f"来源目录无法解析：{src}")
    data_root = paths.data_root.resolve()
    if src_resolved == data_root:
        return MigrationPlan(False, reason="来源目录就是本实例的数据根，无需迁移")
    try:
        src_resolved.relative_to(data_root)
        return MigrationPlan(False, reason="来源目录位于本实例数据根内，拒绝迁移（防止递归嵌套）")
    except ValueError:
        pass
    try:
        data_root.relative_to(src_resolved)
        return MigrationPlan(False, reason="本实例数据根位于来源目录内，拒绝迁移")
    except ValueError:
        pass
    missing = [rel for rel in MIGRATE_REQUIRED_FILES if not (src / rel).is_file()]
    if missing:
        return MigrationPlan(False, reason=(
            f"不是可识别的 BioData Agent 便携版目录（缺少：{', '.join(missing)}）；"
            "只接受含 start-web.bat 与产品源码标记的目录"))

    items: list[MigrationItem] = []
    rejected: list[dict[str, str]] = []
    total = 0

    def _add_file(rel: str, kind: str, src_file: Path, dest: Path) -> None:
        nonlocal total
        reason = _link_escape_reason(src_file, src)
        if reason is not None:
            rejected.append({"rel": rel, "kind": kind, "reason": reason})
            return
        size = src_file.stat().st_size
        if dest.exists():
            if _same_bytes(src_file, dest):
                items.append(MigrationItem(rel, kind, src_file, dest, size, identical=True))
                return
            sibling = _find_matching_migrated(dest, src_file)
            if sibling is not None:
                # 上次冲突已保留的双方副本：重入跳过，不产生新副本
                items.append(MigrationItem(rel, kind, src_file, sibling, size, conflict=True, identical=True))
                return
            items.append(MigrationItem(rel, kind, src_file, _conflict_path(dest), size, conflict=True))
        else:
            items.append(MigrationItem(rel, kind, src_file, dest, size))
        total += size

    def _add_dir(rel: str, kind: str, src_dir: Path, dest: Path) -> None:
        nonlocal total
        reason = _link_escape_reason(src_dir, src)
        if reason is not None:
            rejected.append({"rel": rel, "kind": kind, "reason": reason})
            return
        size = _dir_size(src_dir)
        if _dir_identical(src_dir, dest):
            items.append(MigrationItem(rel, kind, src_dir, dest, size, identical=True))
            return
        sibling = _find_matching_migrated_dir(dest, src_dir)
        if sibling is not None:
            items.append(MigrationItem(rel, kind, src_dir, sibling, size, conflict=True, identical=True))
            return
        if dest.exists():
            items.append(MigrationItem(rel, kind, src_dir, _conflict_path(dest), size, conflict=True))
        else:
            items.append(MigrationItem(rel, kind, src_dir, dest, size))
        total += size

    # .env（LLM 配置）→ config_root/.env；.env.zhipu 契约只迁移 .env，检测提示人工
    env_src = src / ".env"
    if env_src.is_file():
        _add_file(".env", "env", env_src, paths.config_root / ".env")
    else:
        rejected.append({"rel": ".env", "kind": "env", "reason": "源目录无 .env（可选配置，跳过）"})
    if (src / ".env.zhipu").is_file():
        rejected.append({"rel": ".env.zhipu", "kind": "env", "reason": "契约只迁移 .env；如需 zhipu 配置请手工复制"})

    # 已知 .userdata 运行产物
    ud = src / ".userdata"
    for name in MIGRATE_USERDATA_FILES:
        p = ud / name
        if p.is_file():
            _add_file(f".userdata/{name}", "userdata-file", p, paths.userdata_dir / name)
        else:
            rejected.append({"rel": f".userdata/{name}", "kind": "userdata-file",
                             "reason": "源无此文件（可选，跳过）"})
    for name in MIGRATE_USERDATA_DIRS:
        d = ud / name
        if d.is_dir():
            _add_dir(f".userdata/{name}", "userdata-dir", d, paths.userdata_dir / name)
    # 未知 .userdata 文件 → 拒绝并报告（不复制运行时垃圾）
    if ud.is_dir():
        allowed = set(MIGRATE_USERDATA_FILES) | set(MIGRATE_USERDATA_DIRS)
        for p in sorted(ud.rglob("*")):
            if p.is_file() and p.relative_to(ud).parts[0] not in allowed:
                rejected.append({"rel": f".userdata/{p.relative_to(ud).as_posix()}",
                                 "kind": "userdata-unknown", "reason": "非已知 .userdata 文件，不迁移"})

    # database/external 用户上传（upload_* 命名空间；官方快照不带此前缀）
    ext = src / "database" / "external"
    if ext.is_dir():
        for p in sorted(ext.glob("*.json")):
            if not p.is_file():
                continue
            rel = f"database/external/{p.name}"
            if p.name.startswith(MIGRATE_UPLOAD_PREFIX):
                _add_file(rel, "upload", p, paths.user_external_dir / p.name)
            else:
                rejected.append({"rel": rel, "kind": "official-snapshot", "reason": "官方快照，不迁移"})

    # models（仅 --include-models）
    models_dir = src / "models"
    if models_dir.is_dir():
        if include_models:
            _add_dir("models", "models", models_dir, paths.model_root)
        else:
            rejected.append({"rel": "models", "kind": "models",
                             "reason": "未指定 --include-models，不迁移（可手工复制）"})

    # 已知不迁移的顶层项（存在才报告）
    for rel in MIGRATE_KNOWN_EXCLUDED:
        if (src / rel).exists():
            rejected.append({"rel": rel, "kind": "excluded",
                             "reason": "非用户数据（代码/官方快照/测试/日志缓存等），不迁移"})
    # 顶层未知项兜底（既不在白名单也不在已知排除清单）
    known_toplevel = {".env", ".env.zhipu", ".userdata", "database", "models", "start-web.bat",
                      "打开前端.bat", "创建桌面快捷方式.bat", "回滚-恢复GBK代码页.reg",
                      "README.md", "package.json", "mcp_server.py", "AGENTS.md", "CLAUDE.md",
                      "DEVELOPMENT.md", "MODULES.md", "PRODUCT.md", "SECURITY.md", "测试说明.txt"}
    for p in sorted(src.iterdir()):
        if p.name in known_toplevel or p.name in MIGRATE_KNOWN_EXCLUDED:
            continue
        rejected.append({"rel": p.name, "kind": "unknown", "reason": "未知顶层项，不迁移"})

    return MigrationPlan(True, reason="", items=tuple(items), rejected=tuple(rejected), total_bytes=total)


def _stage_file(src: Path, dst: Path) -> None:
    """复制到 staging 并做 SHA-256 校验 + fsync（staging 内任一步失败都不碰目标）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    if _sha256_file(src) != _sha256_file(dst):
        raise MigrationError("hash_mismatch", f"staging 哈希校验失败：{src}")
    with open(dst, "rb+") as fh:
        os.fsync(fh.fileno())


def run_migration(source: Path | str, paths: AppPaths, *, include_models: bool = False,
                  plan: MigrationPlan | None = None,
                  logger: logging.Logger | None = None) -> dict[str, Any]:
    """执行迁移：staging 全成功才开写目标，逐文件 os.replace，可重入。

    流程：① 全部允许项先复制进 `data_root/run/migrate/staging-<ts>/` 并逐文件哈希校验
    （此阶段不碰目标；staging 前对每个源条目再校验 resolve() 仍在源根内——A1-M2
    防御 plan 与执行之间的穿透）；② **staging 全部成功后才开始落位**（os.replace，
    同卷单文件原子）；③ 清理 staging。失败 → 抛 MigrationError，staging 保留供诊断，
    **旧数据未受影响**，可重入。

    A1-L3 表述降级：这不是全有或全无的事务性「原子提交」——落位阶段中途失败会留下
    部分已落位文件（已落位文件与源同字节，重入时按 identical 幂等跳过，不重复复制），
    staging 未落位部分重新 staging。可重入是恢复手段，不等于整体原子性。

    冲突项落位为新文件名（`.migrated` 后缀），绝不覆盖/删除既有文件；不删旧目录。
    返回报告：{copied, identical_skipped, conflicts, summary, staging}。"""
    log = logger or _logger
    plan = plan or plan_migration(source, paths, include_models=include_models)
    if not plan.valid:
        raise MigrationError("invalid_source", plan.reason)
    source_root = Path(source).expanduser()
    staging = paths.run_root / "migrate" / f"staging-{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    copied: list[str] = []
    identical: list[str] = []
    conflicts: list[str] = []
    try:
        staging.mkdir(parents=True, exist_ok=True)
        # 阶段一：全量 staging + 哈希校验（此阶段不碰目标）
        for item in plan.items:
            if item.identical:
                identical.append(item.rel)
                continue
            reason = _link_escape_reason(item.src, source_root)
            if reason is not None:
                raise MigrationError("link_escape", f"{item.rel}：{reason}")
            staged = staging / item.rel
            if item.src.is_dir():
                for f in sorted(item.src.rglob("*")):
                    if f.is_file():
                        _stage_file(f, staged / f.relative_to(item.src))
            else:
                _stage_file(item.src, staged)
            copied.append(item.rel)
        # 阶段二：staging 全成功后才逐文件落位（os.replace 同卷单文件原子；冲突副本
        # 是新文件名，绝不覆盖既有文件；此阶段中断 → 可重入恢复，见 docstring）
        for item in plan.items:
            if item.identical:
                continue
            staged = staging / item.rel
            if item.src.is_dir():
                dest_base = item.dest
                for f in sorted(staged.rglob("*")):
                    if f.is_file():
                        dest = dest_base / f.relative_to(staged)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(f, dest)
            else:
                item.dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, item.dest)
            if item.conflict:
                conflicts.append(item.rel)
        # 阶段三：清理 staging
        shutil.rmtree(staging, ignore_errors=True)
    except MigrationError:
        raise
    except OSError as exc:
        raise MigrationError(
            "migrate_failed",
            f"迁移执行失败（staging 保留在 {staging}，旧数据未受影响，可重入）：{exc}") from exc
    summary = (f"复制 {len(copied)} 项 / 跳过相同 {len(identical)} 项"
               + (f" / 冲突保留双方 {len(conflicts)} 项" if conflicts else ""))
    log.info("迁移完成：%s", summary)
    return {"copied": copied, "identical_skipped": identical, "conflicts": conflicts,
            "summary": summary, "staging": str(staging)}


# ---------------------------------------------------------------------------
# 入口（契约 1/13）
# ---------------------------------------------------------------------------
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="biodata-agent-desktop",
        description="BioData Agent 无控制台桌面启动器（windowed/noconsole 构建入口）",
    )
    parser.add_argument("--migrate-from", metavar="PATH", default=None,
                        help="迁移旧便携版（含 start-web.bat 与产品源码标记的目录）的用户数据"
                             "到本实例数据根；迁移完成后继续正常启动")
    parser.add_argument("--include-models", action="store_true",
                        help="与 --migrate-from 同用：同时迁移旧便携版的 models/ 本地模型目录")
    parser.add_argument("--no-tray", action="store_true",
                        help="禁用系统托盘（无桌面环境/调试场景），改轮询模式")
    parser.add_argument("--window", action="store_true",
                        help="桌面窗口模式（pywebview 内嵌窗口；缺依赖/失败自动回退开浏览器）。"
                             "等价环境变量 BIODATA_SHELL=window（安装包快捷方式可用）")
    parser.add_argument("--tray-selfcheck", action="store_true",
                        help="托盘自检：快速创建/销毁真实 ctypes 托盘后退出（验证用）")
    parser.add_argument("--shell-probe", action="store_true",
                        help="桌面壳依赖探针（构建期 fail-closed 用）：只 import webview，"
                             "成功退出码 0 并把 'SHELL_PROBE_OK' 写入 env BIODATA_SHELL_PROBE_OUT "
                             "指向的文件；失败退出码 1 并写入缺失模块名。windowed 无控制台，"
                             "stdout/stderr 不可捕获，故用退出码 + 临时文件表达结果")
    parser.add_argument("--install-local-model", action="store_true",
                        help="在线安装可选本地精准重排运行环境与固定模型后退出；失败不启动服务")
    return parser.parse_args(argv)


def _env_preferred_port() -> int | None:
    """env PORT 一次性调试覆盖（与 launch_web.ps1 的 PORT 语义一致；不持久化）。"""
    raw = os.getenv("PORT", "").strip()
    if not raw:
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 1 <= port <= 65526 else None


def _default_version() -> str:
    from dataset_recommender.app.webapp import WEB_API_VERSION
    return WEB_API_VERSION


def _default_app() -> Any:
    from dataset_recommender.app.webapp import app
    return app


def tray_selfcheck(*, win32: Any = None, hold_seconds: float = 1.5) -> int:
    """真实 ctypes 托盘自检（`--tray-selfcheck`）：创建 → 改状态 → 短暂消息泵 → 销毁。

    返回 0=成功；1=托盘不可用（无交互桌面/Shell_NotifyIcon 失败）。"""
    win32 = win32 or _win32
    try:
        tray = win32.Win32Tray()
        tray.create(url=_url_for(DEFAULT_PORT), status="正在启动")
        tray.update_status("运行中")
        tray.show_balloon("BioData Agent 托盘自检", "自检通过后将自动消失。")
    except Exception as exc:
        print(f"TRAY_SELFCHECK FAILED: {type(exc).__name__}: {exc}")
        return 1

    def _quit_later() -> None:
        time.sleep(hold_seconds)
        tray.quit_message_loop()

    threading.Thread(target=_quit_later, daemon=True).start()
    tray.run_message_loop()
    tray.destroy()
    print("TRAY_SELFCHECK OK")
    return 0


def _shell_probe() -> int:
    """桌面壳依赖探针（构建期 fail-closed，配合 scripts/build_windows_runtime.py）。

    只做 `import webview`（pywebview 5.4 必须在 frozen 内可导入——modulegraph 只收代码，
    数据/二进制靠 spec 的 collect_all + hooks；本探针是最终判据）。windowed 构建下
    `sys.stdout`/`sys.stderr` 为 None（`_guard_streams` 换 `_NullStream`），打印不可捕获，
    故：真成败由**退出码**表达（0=可导入，1=缺依赖或导入异常），同时把结果写入
    env `BIODATA_SHELL_PROBE_OUT` 指向的文件（'SHELL_PROBE_OK' 或 'SHELL_PROBE_FAIL: <原因>'）
    供构建脚本读取确认，不依赖 shell 管道。"""
    result_path = os.environ.get("BIODATA_SHELL_PROBE_OUT", "")
    try:
        import webview  # noqa: PLC0415（探针只测壳依赖，与 webview_shell 同懒导入语义）
        msg = "SHELL_PROBE_OK\n"
        rc = 0
    except Exception as exc:  # noqa: BLE001（缺模块/导入异常都算壳不可用，fail-closed）
        msg = f"SHELL_PROBE_FAIL: {type(exc).__name__}: {exc}\n"
        rc = 1
    if result_path:
        try:
            Path(result_path).write_text(msg, encoding="utf-8")
        except OSError:
            pass
    return rc


def main(argv: list[str] | None = None) -> int:
    """spec 可直接引用的入口函数（契约 1）。windowed 下以 `if __name__ == "__main__"`
    脚本方式执行时同样有效。--window / BIODATA_SHELL=window → 桌面窗口模式（pywebview
    壳经独立 window_runner 注入点接入，browser 回调仍属 attach/浏览器路径不污染；
    缺依赖自动回退浏览器，见 app/webview_shell.py）。"""
    _guard_streams()
    _install_bootstrap_excepthook()
    args = _parse_args(argv)
    if args.shell_probe:
        return _shell_probe()
    if args.tray_selfcheck:
        return tray_selfcheck()
    if args.install_local_model:
        paths = get_app_paths()
        install_logging(paths.log_root)
        from dataset_recommender.app.model_installer import cli_install_local_model
        return cli_install_local_model(paths)
    from dataset_recommender.app import webview_shell
    if webview_shell.shell_requested(args):
        debug = os.getenv("BIODATA_SHELL_DEBUG", "").strip().lower() in ("1", "true", "yes", "on")
        launcher = Launcher(window_runner=webview_shell.make_desktop_opener(debug=debug),
                            shell_mode=True)
    else:
        launcher = Launcher()
    return launcher.run(argv)


if __name__ == "__main__":
    raise SystemExit(main())
