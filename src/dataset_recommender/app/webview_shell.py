# -*- coding: utf-8 -*-
"""桌面窗口壳（pywebview 5.4，可选依赖）——把「开浏览器」升级成「开原生窗口」。

背景：产品要从「弹系统浏览器」进化成「真正的桌面应用」。选型：pywebview + 系统 WebView
（Windows=WebView2/EdgeChromium、macOS=WKWebView、Linux=WebKitGTK）——前端**零改动**
（还是同一份 importmap 原生 ESM、同一个 127.0.0.1 回环服务），浏览器开发通道**永久保留**
（截图/DevTools/Playwright 工作流不变）。本模块是启动器新增的**专用窗口 runner**
（`desktop_launcher.Launcher.window_runner` 注入点，独立于既有的 `browser` 回调——
后者仍被二次启动 attach 路径使用，不得被壳语义污染）。

设计约束：
- **懒导入**：pywebview 是可选依赖（requirements-webview.txt，>=5.4,<6），导入期不碰它；
  缺失/异常 → 回退 `webbrowser.open` 并记日志（logging，noconsole 构建下 print 会被
  `_NullStream` 吞掉，必须走启动器滚动日志），永不阻断启动。
- **单 GUI-loop**：Windows 最后窗口关闭时 5.4 后端即 `Application.Exit()`，关窗后再
  `start()` 无官方契约——模块级在途标记**锁内先置位**（并发双通过竞态已修），
  `start()` 异常锁内复位、成功关窗保持置位；重入直接按「窗口在跑」返回。
- **下载必须开**：5.4 默认 `ALLOW_DOWNLOADS=False`，而本项目前端大量
  fetch→blob→anchor 下载（reuse_pack/task_pack）——必须显式 True，否则保存框被禁。
- **持久化必须开**：5.4 默认 `private_mode=True`（不跨会话保存 cookies/localStorage），
  与项目「固定端口持久化 + localStorage origin」的设计直接冲突——必须
  `private_mode=False` + `storage_path=<data_root>/webview`。
- **新窗口按 origin 分流**：5.4 的 `NewWindowRequested` 不分同源，`OPEN_EXTERNAL_LINKS_IN_BROWSER`
  一刀切会把「数据集详情 ↗」这类同源 `target=_blank` 甩到系统浏览器（丢 localStorage handoff）。
  本模块经 `js_api` 桥 + 页面注入拦截器分流：同源开第二壳窗（共享 storage_path），异源才交
  系统浏览器；`OPEN_EXTERNAL_LINKS_IN_BROWSER` 仅作未拦截请求的兜底。
- **WebView2 预检**：干净 Win10/LTSC 可能缺 WebView2 Runtime，pywebview 会静默回退
  MSHTML 老引擎，importmap 原生 ESM 无法运行——注册表预检缺失即回退浏览器并日志说明，
  不让用户面对一个打不开的白窗口。
- **原生标题栏品牌一致**：页面 `background_color` 不控制 Windows caption；在
  `before_show` 的真实 HWND 上用 DWM 把 caption/text/border 对齐 CSS token，并把
  完整 favicon ICO 写入 WinForms `Form.Icon`（不支持 DWM 的系统保持原生配色）。
- **无窗口测试**：本模块绝不主动创建窗口；pytest monkeypatch 假 webview 只验证装配语义。
  真实窗口观感人工快验（scripts/run_app.py），不进 pytest。
- **下载中关窗（最近似方案）**：pywebview 5.4 无法从壳侧可靠拦截关窗
  （`events.closing` 仅通知、无取消返回值；`confirm_close` 是建窗期全局布尔、不可按下载
  状态动态切换），因此落地「下载中标记 + closing 事件检查提示」——生产者调用
  `set_download_active()`，壳在 `closing` 时告警并尽力弹原生警告框提示「关闭会中断下载、
  半成品不保留」。真正的二次确认需前端/后端提供下载状态并配合 `confirm_close`，属
  web/static 与 webapp 范围，暂不动。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

if os.name == "nt":
    from ctypes import wintypes
else:
    # 2026-08-24 W5 跨平台核查修复：wintypes 是 Windows 专有——此前顶层无条件导入，
    # 导致本模块在 macOS/Linux 导入即崩（pywebview 本身跨平台）。图标/DWM 等 wintypes
    # 使用点全在仅 Windows 才会进入的函数内（WM_SETICON/DwmSetWindowAttribute），
    # 非 Windows 恒不触达，置 None 即可。
    wintypes = None
from typing import Callable
from urllib.parse import urlsplit

_log = logging.getLogger("biodata.webview_shell")

# ── 窗口观感（单一真源）──────────────────────────────────────────────
WINDOW_TITLE = "BioData Agent"
# 与 web/static/css/app.css `:root` 的 --bg 同值：窗口底色先于页面渲染，同色防白闪。
# 改主题色时两处必须同步（css 是页面真源，这里是壳层兜底）。
WINDOW_BACKGROUND = "#f5f7fa"
# Windows 11 原生 caption/border/text 也要对齐页面 token；create_window 的
# background_color 只管客户区，不能改变系统标题栏。
WINDOW_CAPTION_TEXT = "#16212e"   # app.css --text
WINDOW_BORDER = "#e6eaf0"        # app.css --border
# 小屏研判（2026-08-21 安装版完善波次A）：1240×760 在 1366×768+任务栏的小屏上仍会超高。
# pywebview 5.4 的 ``webview.screens`` 只给整屏 Bounds（工作区在 Screen.frame，且是
# Windows/pythonnet 专属、访问还触发 initialize() 副作用），不是可靠的工作区 clamp 途径；
# 故保守固定 1180×720，宁可略小也不超出常见小屏工作区。
DEFAULT_WIDTH = 1180
DEFAULT_HEIGHT = 720
MIN_WIDTH = 900
MIN_HEIGHT = 600

# runner 回传语义（字符串常量，避免布尔三义）：
WINDOW_CLOSED = "window_closed"      # 窗口跑完消息循环=用户关窗 → 启动器干净关停
FALLBACK_BROWSER = "fallback"        # 壳不可用（缺依赖/缺 WebView2/建窗异常）→ 已回退
                                     # 系统浏览器，启动器恢复托盘维持服务
WindowRunner = Callable[[str], str]

# 模块级在途标记（进程单 GUI-loop）：**锁内置位/锁内复位**（修复「检查后放锁、置位在
# 建窗后」的双通过竞态与失败残留）。
_window_open = False
_lock = threading.Lock()
# WM_SETICON 不复制 HICON；保存到进程退出，避免 source 模式下被错误释放。应用是
# 单窗口/单 GUI-loop，最多两个句柄（small/big），不形成随使用增长的资源泄漏。
_icon_handles: "list[int]" = []
_icon_objects: "list[object]" = []

# 下载进行中标记（边缘修复第 6 项）：壳内 fetch→blob→anchor 下载（任务包/复用清单）无
# 续传，用户下载中途直接关窗会静默丢半成品。pywebview 5.4 从壳侧**无法可靠拦截关窗**
# ——`events.closing` 仅通知、无取消返回值；`confirm_close` 是建窗期全局布尔、不可按下载
# 状态动态切换（官方 API 只把它描述为「closing 前的确认对话框」，无按条件的开关）。故落地
# 「关窗前检查下载中标记 + 提示」的最近似方案：生产者（前端/后端下载状态，见遗留交接）
# 调用 set_download_active()，壳在 closing 事件里检查并告警（尽力弹原生警告框提示）。
_download_active = False
_download_lock = threading.Lock()


def set_download_active(active: bool) -> None:
    """标记壳内是否有进行中的下载任务（线程安全；由下载状态生产者调用）。"""
    global _download_active
    with _download_lock:
        _download_active = bool(active)


def is_download_active() -> bool:
    """返回当前是否有进行中的下载任务（线程安全）。"""
    with _download_lock:
        return _download_active

_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36

# 页面加载后注入的新窗口/外链 origin 分流拦截器（js_api 桥的 JS 侧）。
# 同源（当前服务 origin）的 target=_blank / window.open 交给 Python ``open_link`` 开第二壳窗；
# 异源同样交 ``open_link`` 走系统浏览器。带 ``download`` 的 <a> 不拦——下载必须留在壳内
# 走 ALLOW_DOWNLOADS 的保存框（WebView2 DownloadStarting），拦了会破坏复用清单/任务包下载。
_NEW_WINDOW_INTERCEPTOR_JS = r"""
(function () {
  if (window.__biodataLinkRouter) { return; }
  window.__biodataLinkRouter = true;

  function route(url) {
    try {
      var absolute = new URL(url, window.location.href).href;
      window.pywebview.api.open_link(absolute);
    } catch (err) {
      /* 无 bridge 时忽略：仍会落回 pywebview 的兜底（系统浏览器）。 */
    }
    return null;
  }

  // window.open → 交给 Python 按 origin 分流（同源开第二壳窗 / 异源系统浏览器）。
  window.open = function (url) { return route(url); };

  // target=_blank/_new 的 <a> 点击（capture 阶段兜住动态节点）；带 download 的不拦。
  document.addEventListener('click', function (event) {
    var anchor = event.target && event.target.closest ? event.target.closest('a[target]') : null;
    if (!anchor) { return; }
    if (anchor.hasAttribute && anchor.hasAttribute('download')) { return; }
    var target = (anchor.getAttribute('target') || '').toLowerCase();
    if (target !== '_blank' && target !== '_new') { return; }
    event.preventDefault();
    route(anchor.href);
  }, true);
})();
"""


def _colorref(value: str) -> int:
    """``#rrggbb`` → Win32 COLORREF（0x00bbggrr）；非法配置响亮失败。"""
    raw = str(value).strip()
    if len(raw) != 7 or not raw.startswith("#"):
        raise ValueError(f"invalid color: {value!r}")
    try:
        red, green, blue = (int(raw[i:i + 2], 16) for i in (1, 3, 5))
    except ValueError as exc:
        raise ValueError(f"invalid color: {value!r}") from exc
    return red | (green << 8) | (blue << 16)


def _native_hwnd(win: object) -> int:
    """兼容 pythonnet ``System.IntPtr`` 与测试整数桩，取 pywebview WinForms HWND。"""
    handle = getattr(getattr(win, "native", None), "Handle", 0)
    for method in ("ToInt64", "ToInt32"):
        fn = getattr(handle, method, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:  # noqa: BLE001
                continue
    try:
        return int(handle or 0)
    except (TypeError, ValueError):
        return 0


def _release_window_slot() -> None:
    global _window_open
    with _lock:
        _window_open = False


def shell_requested(args: object) -> bool:
    """--window 参数或 BIODATA_SHELL=window 环境变量是否要求窗口模式（两处同口径）。"""
    if getattr(args, "window", False):
        return True
    return (os.getenv("BIODATA_SHELL", "") or "").strip().lower() == "window"


def _load_webview() -> "object | None":
    """惰性导入 pywebview（5.4）；缺失/失败返回 None（调用方回退浏览器，绝不阻断启动）。"""
    try:
        import webview  # noqa: PLC0415（可选依赖，绝不在导入期加载）
        return webview
    except Exception as exc:  # noqa: BLE001（可选依赖缺失属正常场景）
        _log.warning("pywebview 不可用（%s: %s），回退系统浏览器。安装：pip install -r requirements-webview.txt",
                     type(exc).__name__, exc)
        return None


def _webview2_installed() -> bool:
    """Windows 上 WebView2 Evergreen Runtime 是否已装（微软官方注册表检测法）。

    缺失时 pywebview 会静默降级到 MSHTML（IE 内核）——本项目前端是 importmap 原生 ESM，
    老引擎白屏。预检失败 → 走浏览器回退，并给用户明确原因。非 Windows 恒 True
    （macOS/Linux 用系统 WKWebView/WebKitGTK，无此问题）。"""
    if sys.platform != "win32":
        return True
    try:
        import winreg  # noqa: PLC0415
    except Exception:  # noqa: BLE001（预检失败按可用处理，让 pywebview 自己裁决）
        return True
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    ]
    for hive, key in candidates:
        try:
            with winreg.OpenKey(hive, key) as k:
                winreg.QueryValueEx(k, "pv")   # pv 存在即已装（含未更新到最新版）
            return True
        except OSError:
            continue
    return False


def icon_path_candidates() -> "list[Path]":
    """窗口图标候选（按优先级）：frozen 打包内 assets/ → 仓库 packaging/assets/。
    都找不到返回空列表（图标是锦上添花，绝不因它失败）。"""
    out: "list[Path]" = []
    try:
        from dataset_recommender.app.runtime_paths import get_app_paths
        paths = get_app_paths()
        # frozen 资源根语义（含 BIODATA_RESOURCE_ROOT 覆盖）以 runtime_paths 单一真源为准
        if paths.runtime_mode == "frozen":
            out.append(paths.resource_root / "assets" / "BioDataAgent.ico")
        out.append(paths.install_root / "packaging" / "assets" / "BioDataAgent.ico")
    except Exception:  # noqa: BLE001（路径解析失败只影响图标）
        pass
    return out


def _set_window_icon_win32(win: object, ico: Path) -> None:
    """把多尺寸品牌 .ico 挂到窗口标题栏（ctypes WM_SETICON，best-effort）。

    必须在窗口**原生句柄建立之后**调用——经 `events.before_show` 订阅（Codex 评审：
    start() 前 `win.native` 未建立，直接调用是 no-op）。frozen 下 exe 图标已由 PyInstaller
    内嵌且 WinForms 自动提取，这里是 source/开发模式与标题栏图标的兜底。

    WinForms 主路径直接改 ``Form.Icon``，避免 Show() 把 before_show 阶段的 WM_SETICON
    覆盖回 Python executable icon；非 WinForms 后端才走 WM_SETICON 兜底。ctypes 默认把
    未声明 API 返回值当 32-bit int，64 位 HICON 会被截断，兜底路径因此也显式声明
    HANDLE/LRESULT 签名，并分别按系统 small/big icon 尺寸从 ICO 取层。"""
    try:
        if sys.platform != "win32" or not ico.is_file():
            return
        hwnd = _native_hwnd(win)
        if not hwnd:
            return

        # pywebview 5.4 Windows backend 是 WinForms Form。优先改 Form.Icon 属性，
        # 这样 Show() 会携带品牌图标；只在 before_show 发 WM_SETICON 会被 WinForms
        # 随后的 Show 生命周期用其缓存的 Python executable icon 覆盖。
        native = getattr(win, "native", None)
        try:
            import clr  # type: ignore[import-not-found]  # noqa: PLC0415
            clr.AddReference("System.Drawing")
            from System.Drawing import Icon as DrawingIcon  # type: ignore[import-not-found]  # noqa: PLC0415
            branded = DrawingIcon(str(ico))
            native.Icon = branded
            _icon_objects.append(branded)  # Form 持有期间不 Dispose；进程单窗口，退出由 OS 回收。
            return
        except Exception:  # noqa: BLE001（非 WinForms 后端继续走通用 WM_SETICON）
            pass

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.SendMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
        )
        user32.SendMessageW.restype = ctypes.c_ssize_t
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int

        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        WM_SETICON = 0x0080
        # (WM_SETICON kind, SM_CX*, SM_CY*)
        layers = ((0, 49, 50), (1, 11, 12))  # small 16px / big 32px（随 DPI 由系统给值）
        loaded = 0
        for kind, metric_x, metric_y in layers:
            width = max(1, int(user32.GetSystemMetrics(metric_x)))
            height = max(1, int(user32.GetSystemMetrics(metric_y)))
            hicon = user32.LoadImageW(None, str(ico), IMAGE_ICON, width, height, LR_LOADFROMFILE)
            if hicon:
                raw = int(hicon)
                user32.SendMessageW(wintypes.HWND(hwnd), WM_SETICON, kind, raw)
                _icon_handles.append(raw)
                loaded += 1
        if not loaded:
            _log.warning("未能从 %s 加载窗口图标，保留可执行文件图标。", ico.name)
    except Exception:  # noqa: BLE001（图标失败不阻断应用，日志一次）
        _log.warning("设置窗口图标失败（%s），忽略。", ico.name)


def _set_titlebar_colors_win32(win: object) -> "dict[int, int]":
    """Windows 11 DWM 原生标题栏对齐页面色板；旧系统/不支持属性时安全 no-op。

    返回 ``{attribute: HRESULT}`` 供真机探针与单测观察。标题栏不是 WebView 客户区，
    pywebview 的 ``background_color`` 不会触及它；必须使用 DWM caption attributes。
    """
    if sys.platform != "win32":
        return {}
    hwnd = _native_hwnd(win)
    if not hwnd:
        return {}
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        dwmapi.DwmSetWindowAttribute.argtypes = (
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        )
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        values = {
            _DWMWA_BORDER_COLOR: WINDOW_BORDER,
            _DWMWA_CAPTION_COLOR: WINDOW_BACKGROUND,
            _DWMWA_TEXT_COLOR: WINDOW_CAPTION_TEXT,
        }
        results: "dict[int, int]" = {}
        for attribute, color in values.items():
            native_color = ctypes.c_uint32(_colorref(color))
            results[attribute] = int(dwmapi.DwmSetWindowAttribute(
                wintypes.HWND(hwnd), attribute,
                ctypes.byref(native_color), ctypes.sizeof(native_color),
            ))
        if results and not any(code == 0 for code in results.values()):
            _log.info("当前 Windows/DWM 不支持自定义标题栏颜色，保留系统配色（HRESULT=%s）。", results)
        return results
    except Exception as exc:  # noqa: BLE001（外观增强不阻断应用）
        _log.info("设置标题栏配色失败（%s: %s），保留系统配色。", type(exc).__name__, exc)
        return {}


def _apply_window_chrome_win32(win: object, ico: "Path | None") -> None:
    """在原生句柄就绪后一次应用 caption 色板与品牌图标。"""
    _set_titlebar_colors_win32(win)
    if ico is not None:
        _set_window_icon_win32(win, ico)


def _attach_window_chrome_on_show(win: object) -> None:
    """挂载原生标题栏：before_show 写 Form.Icon，shown 后重申 DWM caption。

    WinForms ``Show()`` 会应用系统主题并可能覆盖此前 caption attributes，因此颜色必须
    在 ``shown`` 后再写一次；图标则须在 ``before_show`` 改 Form.Icon，才能让 Show
    从第一帧就使用品牌资产。
    """
    try:
        events = getattr(win, "events", None)
        if events is None:
            return
        icon = next((p for p in icon_path_candidates() if p.is_file()), None)
        events.before_show += lambda: _apply_window_chrome_win32(win, icon)  # noqa: B023
        shown = getattr(events, "shown", None)
        if shown is not None:
            def _restripe_titlebar() -> None:
                # pywebview Event.execute 把回调返回值塞进 set；_set_titlebar_colors_win32
                # 返回 dict（HRESULT 表），直接挂 lambda 会抛 unhashable type: 'dict'，包一层吞掉返回值。
                _set_titlebar_colors_win32(win)
            shown += _restripe_titlebar
            events.shown = shown
    except Exception:  # noqa: BLE001
        _log.warning("订阅窗口标题栏外观事件失败，忽略。")


def _shell_window_kwargs() -> dict:
    """主窗口 / 第二壳窗口共用的开窗参数钉（单一真源，防两处漂移）。

    x/y 留空：pywebview 自带 CenterScreen（Codex 评审：自算 GetSystemMetrics 不扣
    任务栏、不处理多显示器与 DPI，反而更差）。
    """
    return dict(
        width=DEFAULT_WIDTH,
        height=DEFAULT_HEIGHT,
        min_size=(MIN_WIDTH, MIN_HEIGHT),
        background_color=WINDOW_BACKGROUND,
        text_select=True,          # 结果文本可选中复制，更接近原生应用手感
        zoomable=True,             # Ctrl+滚轮缩放（可访问性意图；Edge 后端默认开）
        confirm_close=False,
    )


def _inject_new_window_router(win: object) -> None:
    """页面加载完成后注入「新窗口/外链按 origin 分流」拦截器（best-effort，失败不阻断）。"""
    try:
        events = getattr(win, "events", None)
        loaded = getattr(events, "loaded", None)
        if loaded is None:
            return

        def _inject() -> None:
            try:
                win.evaluate_js(_NEW_WINDOW_INTERCEPTOR_JS)
            except Exception:  # noqa: BLE001（拦截器是增强，失败不阻断应用）
                _log.warning("执行新窗口分流拦截器失败，忽略。")

        loaded += _inject
    except Exception:  # noqa: BLE001
        _log.warning("注入新窗口分流拦截器失败，忽略。")


def _notify_download_close() -> None:
    """下载进行中关窗的友好中文提示（委托 desktop_launcher_win32.message_box 单一通道；
    非 Windows/失败则忽略，绝不抛）。"""
    if sys.platform != "win32":
        return
    try:
        # 惰性 import：desktop_launcher_win32 模块级即加载 user32，非 Windows 上导入即失败
        from dataset_recommender.app import desktop_launcher_win32
        desktop_launcher_win32.message_box(
            WINDOW_TITLE,
            "检测到下载仍在进行。\n关闭窗口会中断下载，且不会保留已下载的半成品。\n"
            "请先等待下载完成，或取消下载后再关闭窗口。",
            icon=desktop_launcher_win32.MB_ICONWARNING,
        )
    except Exception:  # noqa: BLE001
        _log.warning("下载中关窗提示弹出失败，忽略。")


def _on_window_closing() -> None:
    """closing 事件钩子（fire-only、无法取消，见模块 docstring 证据）：下载进行中则告警 + 提示。"""
    if not is_download_active():
        return
    _log.warning("下载进行中用户关闭窗口：下载将中断、半成品不保留（无续传）。")
    _notify_download_close()


def _subscribe_close_download_guard(win: object) -> None:
    """订阅 closing 事件做「下载中关窗检查」（第 6 项最近似方案；事件缺失/失败绝不抛）。"""
    try:
        closing = getattr(getattr(win, "events", None), "closing", None)
        if closing is None:
            return
        closing += _on_window_closing
    except Exception:  # noqa: BLE001
        _log.warning("订阅关窗下载检查失败，忽略。")


def _bind_download_activity() -> None:
    """把真实下载管理器的 running 边沿接到壳层关窗提示。

    惰性 import 避免浏览器模式/模块导入期拉起语料或桌面依赖；绑定失败只失去
    提示，不阻断窗口和下载主功能。
    """
    try:
        from dataset_recommender.corpus import download_manager
        download_manager.bind_activity_callback(set_download_active)
    except Exception as exc:  # noqa: BLE001
        _log.warning("连接下载中关窗提示失败（%s），忽略。", type(exc).__name__)


def make_desktop_opener(debug: bool = False) -> WindowRunner:
    """构造窗口 runner：URL → 原生窗口（阻塞至关窗）；失败 → 回退浏览器。

    返回 WINDOW_CLOSED / FALLBACK_BROWSER（语义见模块常量）。debug=BIODATA_SHELL_DEBUG=1
    时开 pywebview 调试（右键 DevTools，供壳内排障）。
    """
    debug_flag = bool(debug)

    def _run(url: str) -> str:
        global _window_open
        with _lock:
            if _window_open:
                # 进程单 GUI-loop：窗口已开时重入（如托盘重复请求）直接按「在跑」返回。
                return WINDOW_CLOSED
            # 在任何导入/预检/建窗之前锁内占位。旧实现到 create_window 之后才置位，
            # 两线程可同时通过开头检查，与模块注释声称的「锁内先置位」不一致。
            _window_open = True
        webview = _load_webview()
        if webview is None:
            _release_window_slot()
            _fallback_browser(url, "pywebview 未安装/不可用")
            return FALLBACK_BROWSER
        if not _webview2_installed():
            _release_window_slot()
            _fallback_browser(url, "未检测到 WebView2 Runtime（Windows 干净系统可能缺失）")
            return FALLBACK_BROWSER
        try:
            _bind_download_activity()
            _configure_settings(webview)
            router = _LinkRouter(webview, url, debug_flag)
            win = webview.create_window(
                WINDOW_TITLE, url, js_api=router, **_shell_window_kwargs()
            )
            _attach_window_chrome_on_show(win)
            _inject_new_window_router(win)
            _subscribe_close_download_guard(win)
            try:
                # private_mode=False + storage_path：localStorage/cookies 跨会话持久化
                #（项目「固定端口+origin 持久化」契约依赖）；storage 落数据根，卸载/迁移随数据走。
                from dataset_recommender.app.runtime_paths import get_app_paths
                storage = str(get_app_paths().data_root / "webview")
                webview.start(debug=debug_flag, private_mode=False, storage_path=storage)
            except Exception:
                # 循环未建立/半途崩掉：复位允许再次尝试（否则标志永久残留）；成功关窗则
                # **保持置位**——GUI 循环结束后二次 start() 无契约保障（进程单 GUI-loop）。
                _release_window_slot()
                raise
            return WINDOW_CLOSED            # 消息循环结束 = 用户关窗 → 启动器干净关停
        except Exception as exc:  # noqa: BLE001（壳失败绝不阻断：回退浏览器并留痕）
            _release_window_slot()
            _log.warning("桌面窗口启动失败（%s: %s），回退系统浏览器。", type(exc).__name__, exc)
            _fallback_browser(url, "窗口创建失败")
            return FALLBACK_BROWSER

    return _run


def _configure_settings(webview: object) -> None:
    """壳层 settings（5.4 键；缺键/旧版静默跳过）：
    - OPEN_EXTERNAL_LINKS_IN_BROWSER=True：**兜底**——同源/异源新窗口主要由本模块的
      ``js_api`` 桥按 origin 分流（同源开第二壳窗、异源系统浏览器）；此键保留 True，
      让未拦截到的新窗口请求（如内嵌 iframe）安全落回系统浏览器，绝不把主窗口导航走。
    - ALLOW_DOWNLOADS=True：**必须**——前端 blob anchor 下载（复用清单/任务包）走
      WebView2 保存对话框；默认 False 会直接禁掉下载。"""
    try:
        settings = getattr(webview, "settings", None)
        if settings is None:
            return
        for key in ("OPEN_EXTERNAL_LINKS_IN_BROWSER", "ALLOW_DOWNLOADS"):
            if key in settings:
                settings[key] = True
    except Exception:  # noqa: BLE001
        pass


def _fallback_browser(url: str, reason: str) -> None:
    """回退：开系统浏览器（非阻塞），原因进滚动日志（noconsole 构建下 print 不可见）。"""
    _log.warning("%s：打开系统浏览器访问 %s", reason, url)
    try:
        webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001
        _log.warning("系统浏览器也未能打开：%s", url)


def _origin_key(url: str) -> "tuple[str, str, int] | None":
    """取 URL 的 ``(scheme, hostname, port)``；缺省端口归一为 80/443。非法/相对/非 http(s) → None。"""
    try:
        parts = urlsplit(str(url))
        scheme = (parts.scheme or "").lower()
        if scheme not in ("http", "https") or not parts.hostname:
            return None
        port = parts.port  # 无端口返回 None；非法端口抛 ValueError
    except (ValueError, AttributeError):
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, parts.hostname.lower(), int(port))


def is_same_origin(target_url: str, base_url: str) -> bool:
    """同源判定：scheme + host + port 全等。无法判定（相对/非法 URL）→ False（走系统浏览器，安全侧）。

    注意：``localhost`` 与 ``127.0.0.1`` 是不同 host（浏览器同源语义亦如此）；本项目回环
    服务恒用 ``127.0.0.1``，故按严格 host 比对即可覆盖「当前服务端口变化」的判定。
    """
    target = _origin_key(target_url)
    base = _origin_key(base_url)
    return target is not None and target == base


class _LinkRouter:
    """pywebview ``js_api`` 桥：页面 JS 把「新窗口 / 外链」交给 Python 按 origin 分流。

    同源（当前服务 origin）→ ``webview.create_window`` 开第二个壳窗口（共享 storage_path，
    localStorage handoff 在壳内同源存储里生效）；异源 → 系统浏览器。桥方法由
    ``js_bridge_call`` 在独立线程执行，非主线程调 ``create_window`` 会走 pywebview 的
    ``Invoke`` 封送到 UI 线程，线程安全。
    """

    def __init__(self, webview: "object", base_url: str, debug: bool = False):
        self._webview = webview
        self._base_url = base_url
        self._debug = bool(debug)

    def open_link(self, url: str) -> None:
        target = str(url or "")
        if not target:
            return
        if is_same_origin(target, self._base_url):
            self._open_shell_window(target)
        else:
            webbrowser.open(target, new=2)

    def _open_shell_window(self, url: str) -> None:
        """开第二个壳窗口；失败不阻断主窗口，回退系统浏览器。"""
        try:
            win = self._webview.create_window(
                WINDOW_TITLE, url, js_api=self, **_shell_window_kwargs()
            )
            _attach_window_chrome_on_show(win)
            _inject_new_window_router(win)
        except Exception:  # noqa: BLE001（第二窗口是增强，失败不影响主窗口）
            _log.warning("同源新窗口创建失败，回退系统浏览器打开：%s", url)
            try:
                webbrowser.open(url, new=2)
            except Exception:  # noqa: BLE001
                _log.warning("系统浏览器也未能打开：%s", url)
