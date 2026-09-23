# -*- coding: utf-8 -*-
"""桌面启动器的 Win32 直调层（ctypes，零第三方 GUI 依赖）。

契约背景（`desktop_launcher.py` 的配套辅助模块）：windowed（noconsole）构建
没有控制台可写输出，也不能引入 pystray/Pillow/Tk/.NET——托盘、弹窗、剪贴板等
交互面全部用 ctypes 直调 kernel32/user32/shell32。本模块只提供**薄封装**：

- `open_mutex` / `Win32Mutex`   —— 命名 mutex（与 Inno AppMutex 同名，防双实例）
- `is_pid_alive`                —— OpenProcess + GetExitCodeProcess 存活探测
- `Win32Tray`                   —— Shell_NotifyIconW 托盘 + 隐藏窗口 + TrackPopupMenu 右键菜单
- `message_box`                 —— MessageBoxW（浏览器打开失败的简洁提示）
- `set_clipboard_text`          —— 复制访问地址
- `open_directory`              —— os.startfile 打开日志目录
- `TrayUnavailable`             —— 无交互桌面/服务会话下托盘不可用的可捕获异常

线程模型：托盘隐藏窗口与消息泵必须在**创建它们的线程**（启动器主线程）上运行，
菜单动作经回调回到启动器；服务端线程跑 uvicorn，不碰窗口。非桌面会话下
`Shell_NotifyIcon` 失败抛 `TrayUnavailable`，由调用方降级为无托盘轮询模式，
绝不阻断启动（契约 12：无桌面也必须能启动）。

菜单项与命令 ID：1=打开 BioData Agent / 2=打开日志目录 / 3=复制访问地址 / 4=退出。
托盘状态（正在启动/运行中/启动失败）经 NIF_TIP 悬浮提示展示。

Windows 验证依据（本机）：端口探测 socket 一律**不设 SO_REUSEADDR**——
双 REUSEADDR 时第二个 socket 可 bind+listen 已被监听的端口（探测误判）；无
REUSEADDR 时被占端口返回 WinError 10048、监听关闭后可立即重绑（退出即释放）。
"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

# ---------------------------------------------------------------------------
# Win32 常量
# ---------------------------------------------------------------------------
ERROR_ALREADY_EXISTS = 183
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259

#: LONG_PTR（wintypes 未导出；64 位下与指针等宽）
LRESULT = ctypes.c_ssize_t

WM_APP = 0x8000
WM_USER = 0x0400
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B
NIN_BALLOONUSERCLICK = WM_USER + 5

NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x1
NIF_ICON = 0x2
NIF_TIP = 0x4
NIF_INFO = 0x10
NIF_SHOWTIP = 0x80
NIIF_INFO = 0x1

WS_OVERLAPPED = 0
CW_USEDEFAULT = 0x80000000
IDI_APPLICATION = 32512

MF_STRING = 0
MF_SEPARATOR = 0x800
TPM_RETURNCMD = 0x0100
TPM_LEFTALIGN = 0x0
TPM_BOTTOMALIGN = 0x0020
TPM_NONOTIFY = 0x0080

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

MB_ICONINFORMATION = 0x40
MB_ICONWARNING = 0x30
MB_ICONERROR = 0x10

#: OpenInputDesktop 请求权限：允许切换到该桌面（交互会话探测用）
DESKTOP_SWITCHDESKTOP = 0x0100

#: 托盘菜单命令 ID（对外契约，`desktop_launcher.py` 不关心具体值，回调即用）
CMD_OPEN = 1
CMD_LOGS = 2
CMD_COPY = 3
CMD_EXIT = 4

# ---------------------------------------------------------------------------
# DLL 句柄与函数原型（显式 argtypes/restype，避免 x64 指针截断）
# ---------------------------------------------------------------------------
# 结构体必须先于原型定义（原型赋值在模块加载期引用它们）。
class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    """Vista+ 版 NOTIFYICONDATAW（含 guidItem/hBalloonIcon）。结构完整才能让
    cbSize 取到正确尺寸（968 @ x64）；szInfo 后 uTimeout 与 uVersion 是联合，
    ctypes 无法表达，两者同为 DWORD 布局等价，按 uVersion 声明。"""
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
        ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", ctypes.c_void_p),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32 = ctypes.WinDLL("user32", use_last_error=True)
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_kernel32.CreateMutexW.restype = wintypes.HANDLE
_kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetCurrentThreadId.restype = wintypes.DWORD
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_kernel32.GlobalLock.restype = wintypes.LPVOID
_kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalUnlock.restype = wintypes.BOOL
_kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_kernel32.GlobalFree.restype = wintypes.HGLOBAL
_kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

_user32.MessageBoxW.restype = ctypes.c_int
_user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
_user32.OpenClipboard.restype = wintypes.BOOL
_user32.OpenClipboard.argtypes = [wintypes.HWND]
_user32.EmptyClipboard.restype = wintypes.BOOL
_user32.SetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_user32.CloseClipboard.restype = wintypes.BOOL
_user32.LoadIconW.restype = wintypes.HICON
_user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
_user32.RegisterClassExW.restype = wintypes.ATOM
_user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
_user32.UnregisterClassW.restype = wintypes.BOOL
_user32.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_user32.DestroyWindow.restype = wintypes.BOOL
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.DefWindowProcW.restype = LRESULT
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.GetMessageW.restype = wintypes.BOOL
_user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
_user32.TranslateMessage.restype = wintypes.BOOL
_user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]
_user32.CreatePopupMenu.restype = wintypes.HMENU
_user32.AppendMenuW.restype = wintypes.BOOL
# wintypes 无 UINT_PTR：64 位下与指针等宽，用 c_size_t 声明（命令 ID 为 UINT 值）
_user32.AppendMenuW.argtypes = [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
_user32.TrackPopupMenu.restype = wintypes.BOOL
_user32.TrackPopupMenu.argtypes = [
    wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p,
]
_user32.DestroyMenu.restype = wintypes.BOOL
_user32.DestroyMenu.argtypes = [wintypes.HMENU]
_user32.GetCursorPos.restype = wintypes.BOOL
_user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
_user32.PostThreadMessageW.restype = wintypes.BOOL
_user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_user32.PostMessageW.restype = wintypes.BOOL
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

_shell32.Shell_NotifyIconW.restype = wintypes.BOOL
_shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]


class TrayUnavailable(RuntimeError):
    """托盘不可用（无交互桌面/服务会话/Shell_NotifyIcon 失败）。调用方降级，不阻断启动。"""


def _as_lpcstr(resource_id: int) -> wintypes.LPCWSTR:
    """MAKEINTRESOURCE：把小整数资源 ID 转成 LPCWSTR（LoadIconW 用）。"""
    return wintypes.LPCWSTR(resource_id)


# ---------------------------------------------------------------------------
# 命名 mutex（与 Inno AppMutex 同名，防双实例）
# ---------------------------------------------------------------------------
class Win32Mutex:
    """命名 mutex 句柄。`already_exists` 为 True 表示有另一个进程已持有该名。"""

    __slots__ = ("handle", "already_exists")

    def __init__(self, handle: int, already_exists: bool) -> None:
        self.handle = handle
        self.already_exists = already_exists

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)
            self.handle = 0


def open_mutex(name: str) -> Win32Mutex:
    """创建/打开命名 mutex。返回句柄与是否已存在。"""
    handle = _kernel32.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    return Win32Mutex(int(handle), ctypes.get_last_error() == ERROR_ALREADY_EXISTS)


# ---------------------------------------------------------------------------
# PID 存活探测（OpenProcess + GetExitCodeProcess；不依赖 psutil）
# ---------------------------------------------------------------------------
def is_pid_alive(pid: int) -> bool:
    """进程是否存活。句柄打开失败（进程不存在/权限不足）一律视为不存活。"""
    if pid <= 0:
        return False
    handle = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not _kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        _kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------
# 托盘（Shell_NotifyIconW + 隐藏窗口 + TrackPopupMenu）
# ---------------------------------------------------------------------------
_WM_TRAY_MSG = WM_APP + 1
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)


def _make_wnd_proc(tray: "Win32Tray") -> WNDPROC:
    """为托盘实例生成窗口过程（闭包持有实例引用；返回的 WNDPROC 必须由实例长期持有）。"""

    @WNDPROC
    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == WM_DESTROY:
            _user32.PostQuitMessage(0)
            return 0
        if msg == _WM_TRAY_MSG:
            tray._on_tray_notify(lparam)  # noqa: SLF001（同模块内部控制回调）
            return 0
        return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    return wnd_proc


class Win32Tray:
    """ctypes 系统托盘。消息泵必须在创建线程（启动器主线程）上运行。

    构造时接收菜单动作回调；`create` 注册隐藏窗口与图标；`update_status` 改
    悬浮提示（正在启动/运行中/启动失败）；`show_balloon` 气泡；`run_message_loop`
    阻塞直到 WM_QUIT（由 `quit_message_loop` 从任意线程投递）。
    """

    _TIP_MAX = 127

    def __init__(self, *, on_open=None, on_open_logs=None, on_copy=None, on_quit=None) -> None:
        self._on_open = on_open or (lambda: None)
        self._on_open_logs = on_open_logs or (lambda: None)
        self._on_copy = on_copy or (lambda: None)
        self._on_quit = on_quit or (lambda: None)
        self._hwnd = None
        self._class_atom = None
        self._class_name = None
        self._hicon = None
        self._wnd_proc = None
        self._icon_created = False
        self._owner_thread = 0

    # -- 生命周期 ---------------------------------------------------------
    def create(self, url: str, status: str) -> None:
        """注册隐藏窗口 + 添加托盘图标。失败抛 TrayUnavailable（调用方降级）。"""
        hinst = _kernel32.GetModuleHandleW(None)
        self._class_name = f"BioDataAgentTray{os.getpid()}"
        self._wnd_proc = _make_wnd_proc(self)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = ctypes.cast(self._wnd_proc, ctypes.c_void_p)
        wc.hInstance = hinst
        wc.lpszClassName = self._class_name
        atom = _user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            raise TrayUnavailable(f"RegisterClassExW 失败: {ctypes.WinError(ctypes.get_last_error())}")
        self._class_atom = atom
        hwnd = _user32.CreateWindowExW(
            0, self._class_name, "BioDataAgent", WS_OVERLAPPED,
            CW_USEDEFAULT, CW_USEDEFAULT, 0, 0, None, None, hinst, None,
        )
        if not hwnd:
            raise TrayUnavailable(f"CreateWindowExW 失败: {ctypes.WinError(ctypes.get_last_error())}")
        self._hwnd = hwnd
        self._owner_thread = _kernel32.GetCurrentThreadId()
        self._hicon = _user32.LoadIconW(None, _as_lpcstr(IDI_APPLICATION))
        nid = self._make_nid(tip=status)
        if not _shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
            raise TrayUnavailable(f"Shell_NotifyIconW(NIM_ADD) 失败: {ctypes.WinError(ctypes.get_last_error())}")
        self._icon_created = True

    def update_status(self, status: str) -> None:
        """更新托盘悬浮提示（NIM_MODIFY + NIF_TIP）。"""
        if not self._icon_created:
            return
        nid = self._make_nid(tip=status)
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def show_balloon(self, title: str, text: str) -> None:
        """托盘气泡提示（NIF_INFO）。"""
        if not self._icon_created:
            return
        nid = self._make_nid()
        nid.uFlags |= NIF_INFO
        nid.szInfo = text[:255]
        nid.szInfoTitle = title[:63]
        nid.dwInfoFlags = NIIF_INFO
        _shell32.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(nid))

    def run_message_loop(self) -> None:
        """阻塞消息泵；收到 WM_QUIT（`quit_message_loop`）后返回。"""
        msg = MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            _user32.TranslateMessage(ctypes.byref(msg))
            _user32.DispatchMessageW(ctypes.byref(msg))

    def quit_message_loop(self) -> None:
        """从任意线程投递 WM_QUIT 到消息泵线程。失败则退化为窗口 WM_CLOSE。"""
        if not self._owner_thread:
            return
        if not _user32.PostThreadMessageW(self._owner_thread, WM_QUIT, 0, 0) and self._hwnd:
            _user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)

    def destroy(self) -> None:
        """删除图标 + 销毁隐藏窗口。幂等。"""
        if self._icon_created:
            nid = self._make_nid()
            _shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid))
            self._icon_created = False
        if self._hwnd:
            _user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_atom and self._class_name:
            _user32.UnregisterClassW(self._class_name, _kernel32.GetModuleHandleW(None))
            self._class_atom = None

    # -- 内部 -------------------------------------------------------------
    def _make_nid(self, tip: str | None = None) -> NOTIFYICONDATAW:
        nid = NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        nid.hWnd = self._hwnd
        nid.uID = 1
        nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_SHOWTIP
        nid.uCallbackMessage = _WM_TRAY_MSG
        nid.hIcon = self._hicon
        if tip:
            nid.uFlags |= NIF_TIP
            nid.szTip = tip[: self._TIP_MAX]
        return nid

    def _on_tray_notify(self, lparam: int) -> None:
        if lparam in (WM_RBUTTONUP, WM_CONTEXTMENU):
            self._show_menu()
        elif lparam in (WM_LBUTTONDBLCLK, NIN_BALLOONUSERCLICK):
            self._on_open()

    def _show_menu(self) -> None:
        hmenu = _user32.CreatePopupMenu()
        if not hmenu:
            return
        try:
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_OPEN, "打开 BioData Agent")
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_LOGS, "打开日志目录")
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_COPY, "复制访问地址")
            _user32.AppendMenuW(hmenu, MF_SEPARATOR, 0, None)
            _user32.AppendMenuW(hmenu, MF_STRING, CMD_EXIT, "退出")
            pt = wintypes.POINT()
            _user32.GetCursorPos(ctypes.byref(pt))
            flags = TPM_RETURNCMD | TPM_NONOTIFY | TPM_LEFTALIGN | TPM_BOTTOMALIGN
            cmd = _user32.TrackPopupMenu(hmenu, flags, pt.x, pt.y, 0, self._hwnd, None)
        finally:
            _user32.DestroyMenu(hmenu)
        if cmd == CMD_OPEN:
            self._on_open()
        elif cmd == CMD_LOGS:
            self._on_open_logs()
        elif cmd == CMD_COPY:
            self._on_copy()
        elif cmd == CMD_EXIT:
            self._on_quit()


# ---------------------------------------------------------------------------
# MessageBox / 剪贴板 / 打开目录 / 交互桌面探测
# ---------------------------------------------------------------------------
def is_interactive() -> bool:
    """当前会话是否有可用的交互桌面（OpenInputDesktop 返回句柄 = 交互会话）。

    无交互桌面（服务会话 0 / 无头 / SSH 远程会话）下 MessageBoxW、托盘会失败或
    挂起——启动器 `_default_notify` 据此把弹框降级为仅日志，绝不阻断。
    失败一律保守判为「非交互」（fail-closed：宁可只记日志也不弹框）。"""
    try:
        hdesk = _user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
    except Exception:  # noqa: BLE001（探测失败保守判非交互）
        return False
    if not hdesk:
        return False
    _user32.CloseDesktop(hdesk)
    return True


def message_box(title: str, text: str, *, icon: int = MB_ICONINFORMATION) -> None:
    """模态提示框（windowed 下浏览器打开失败等场景）。调用线程阻塞至用户确认。"""
    _user32.MessageBoxW(None, text, title, icon | 0)


def set_clipboard_text(text: str) -> bool:
    """把文本写入系统剪贴板（UTF-16LE）。返回是否成功（不抛异常）。"""
    try:
        if not _user32.OpenClipboard(None):
            return False
        try:
            _user32.EmptyClipboard()
            data = text.encode("utf-16-le") + b"\x00\x00"
            handle = _kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
            if not handle:
                return False
            try:
                dst = _kernel32.GlobalLock(handle)
                if not dst:
                    return False
                ctypes.memmove(dst, data, len(data))
                _kernel32.GlobalUnlock(handle)
                if not _user32.SetClipboardData(CF_UNICODETEXT, handle):
                    return False
                handle = None  # 所有权已移交系统
            finally:
                if handle:
                    _kernel32.GlobalFree(handle)
            return True
        finally:
            _user32.CloseClipboard()
    except OSError:
        return False


def open_directory(path: str) -> bool:
    """用资源管理器打开目录（windowed 无控制台，os.startfile 最简）。"""
    try:
        os.startfile(path)  # noqa: S606（显式用户动作：打开日志目录）
        return True
    except OSError:
        return False


__all__ = [
    "CMD_COPY", "CMD_EXIT", "CMD_LOGS", "CMD_OPEN",
    "TrayUnavailable", "Win32Mutex", "Win32Tray",
    "is_interactive", "is_pid_alive", "message_box", "open_directory",
    "open_mutex", "set_clipboard_text",
]
