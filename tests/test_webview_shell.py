# -*- coding: utf-8 -*-
"""桌面窗口壳（pywebview）装配测试——全程打桩、**绝不创建真实窗口**。

覆盖（2026-08-21 壳批 zcode/desktop-shell + 安装版完善波次A，Codex 只读评审结论已并入）：
- webview_shell：--window/env 判定、开窗参数钉（标题/尺寸/底色/最小尺寸/zoomable）、
  settings 双键（OPEN_EXTERNAL_LINKS_IN_BROWSER 兜底 + **下载必须开**）、
  start(private_mode=False + storage_path)、新窗口按 origin 分流（js_api 桥 + 注入拦截器）、
  单 GUI-loop 在途标记（重入不重开、异常复位）、缺依赖/缺 WebView2/建窗异常回退
  （FALLBACK_BROWSER + 日志 + webbrowser）、图标 before_show 订阅、图标候选链；
- desktop_launcher 集成：--window 解析、main() 的 **window_runner** 装配（browser 保持
  默认——attach 路径复用浏览器语义不受污染）、_run_foreground 壳分支（WINDOW_CLOSED →
  关窗即干净关停；FALLBACK → **恢复托盘**维持服务，除非 --no-tray 走轮询）。
真实窗口观感/交互验收走人工快验（scripts/run_app.py），不进 pytest（CI/无头环境无窗口）。
"""
from __future__ import annotations

import ctypes
import logging
import sys
import threading
from types import ModuleType
from pathlib import Path
from types import SimpleNamespace

import pytest

from dataset_recommender.app import webview_shell
from dataset_recommender.app import desktop_launcher as dl


# ---------------------------------------------------------------- 判定与参数
def test_shell_requested_flag_and_env(monkeypatch):
    monkeypatch.delenv("BIODATA_SHELL", raising=False)
    assert webview_shell.shell_requested(SimpleNamespace(window=True)) is True
    assert webview_shell.shell_requested(SimpleNamespace(window=False)) is False
    monkeypatch.setenv("BIODATA_SHELL", "window")
    assert webview_shell.shell_requested(SimpleNamespace(window=False)) is True
    monkeypatch.setenv("BIODATA_SHELL", "browser")
    assert webview_shell.shell_requested(SimpleNamespace(window=False)) is False


def test_parse_args_window_flag():
    assert dl._parse_args(["--window"]).window is True
    assert dl._parse_args([]).window is False
    assert dl._parse_args(["--no-tray"]).window is False


# ---------------------------------------------------------------- 开窗装配（假 webview）
class _EventSlot(list):
    """pywebview 事件槽（支持 += 订阅）。"""

    def __iadd__(self, handler):
        self.append(handler)
        return self


class _FakeEvents:
    def __init__(self):
        self.before_show = _EventSlot()
        self.shown = _EventSlot()
        self.closing = _EventSlot()


class _FakeWin:
    def __init__(self):
        self.native = SimpleNamespace(Handle=0)   # hwnd=0 → 图标挂载静默跳过（best-effort）
        self.events = _FakeEvents()               # 每窗独立事件（真实 pywebview 语义）


class _FakeWebview:
    """记录 create_window/start 调用与 settings 装配的假 pywebview 模块。"""

    def __init__(self):
        self.created: "list[dict]" = []
        self.starts: "list[dict]" = []
        # 真实 pywebview 5.4 的 settings 是**预置键**的字典；生产代码只在键存在时赋值
        # （版本兼容守卫），假模块必须还原这一前提。
        self.settings: "dict[str, object]" = {
            "OPEN_EXTERNAL_LINKS_IN_BROWSER": False,
            "ALLOW_DOWNLOADS": False,
        }

    def create_window(self, title, url, **kwargs):
        self.created.append({"title": title, "url": url, **kwargs})
        return _FakeWin()

    def start(self, **kwargs):
        self.starts.append(kwargs)


@pytest.fixture
def fake_webview(monkeypatch):
    """把 webview_shell 的懒加载指向假模块；机器状态隔离：WebView2 预检恒真、在途标记复位。"""
    fake = _FakeWebview()
    monkeypatch.setattr(webview_shell, "_load_webview", lambda: fake)
    monkeypatch.setattr(webview_shell, "_webview2_installed", lambda: True)
    monkeypatch.setattr(webview_shell, "_window_open", False)
    return fake


def test_opener_creates_window_with_aesthetics(fake_webview):
    opener = webview_shell.make_desktop_opener()
    assert opener("http://127.0.0.1:7860") == webview_shell.WINDOW_CLOSED
    (kw,) = fake_webview.created
    assert kw["title"] == "BioData Agent"
    assert kw["url"] == "http://127.0.0.1:7860"
    assert kw["width"] == 1180 and kw["height"] == 720      # 小屏保守默认（无可靠工作区 clamp API）
    assert kw["min_size"] == (900, 600)
    assert kw["background_color"] == "#f5f7fa"     # 与 app.css --bg 同值，防白闪
    assert kw["text_select"] is True
    assert kw["zoomable"] is True
    assert kw["confirm_close"] is False
    assert "x" not in kw and "y" not in kw         # 交给 pywebview CenterScreen（不自算）
    # OPEN_EXTERNAL_LINKS_IN_BROWSER 仅作未拦截请求的兜底 + 下载必须开（前端 blob anchor 下载依赖）
    assert fake_webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] is True
    assert fake_webview.settings["ALLOW_DOWNLOADS"] is True
    # localStorage 持久化：private_mode=False + storage_path 落数据根（项目 origin 契约）
    (start,) = fake_webview.starts
    assert start["debug"] is False
    assert start["private_mode"] is False
    assert start["storage_path"].endswith("webview")


def test_native_titlebar_tokens_match_frontend_palette():
    css = (Path(__file__).resolve().parents[1] / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert "--bg: #f5f7fa" in css and webview_shell.WINDOW_BACKGROUND == "#f5f7fa"
    assert "--text: #16212e" in css and webview_shell.WINDOW_CAPTION_TEXT == "#16212e"
    assert "--border: #e6eaf0" in css and webview_shell.WINDOW_BORDER == "#e6eaf0"
    # COLORREF 是 0x00bbggrr，不是网页的 RGB 字节序。
    assert webview_shell._colorref("#f5f7fa") == 0x00FAF7F5
    assert webview_shell._colorref("#16212e") == 0x002E2116
    with pytest.raises(ValueError):
        webview_shell._colorref("f5f7fa")


def test_dwm_titlebar_sets_border_caption_and_text(monkeypatch):
    calls: "list[tuple[int, int]]" = []

    class FakeCall:
        argtypes = None
        restype = None

        def __call__(self, _hwnd, attribute, value_ptr, _size):
            value = ctypes.cast(value_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value
            calls.append((int(attribute), int(value)))
            return 0

    fake = SimpleNamespace(DwmSetWindowAttribute=FakeCall())
    monkeypatch.setattr(webview_shell.sys, "platform", "win32")
    monkeypatch.setattr(webview_shell.ctypes, "WinDLL", lambda *_a, **_kw: fake)
    result = webview_shell._set_titlebar_colors_win32(SimpleNamespace(native=SimpleNamespace(Handle=123)))
    assert result == {34: 0, 35: 0, 36: 0}
    assert calls == [
        (34, webview_shell._colorref("#e6eaf0")),
        (35, webview_shell._colorref("#f5f7fa")),
        (36, webview_shell._colorref("#16212e")),
    ]


def test_opener_debug_flag_passes_through(fake_webview):
    webview_shell.make_desktop_opener(debug=True)("http://127.0.0.1:7860")
    assert fake_webview.starts[-1]["debug"] is True


def test_opener_reentrant_returns_window_closed_without_second_start(fake_webview):
    opener = webview_shell.make_desktop_opener()
    assert opener("http://127.0.0.1:7860") == webview_shell.WINDOW_CLOSED
    # 成功关窗后在途标记保持置位（GUI 循环结束后二次 start 无契约）→ 重入不再开窗
    assert opener("http://127.0.0.1:7860") == webview_shell.WINDOW_CLOSED
    assert len(fake_webview.starts) == 1


def test_window_slot_is_claimed_before_slow_import(monkeypatch):
    """首个 runner 仍在懒导入时，第二个 runner 不能穿透并再建一扇窗。"""
    fake = _FakeWebview()
    entered = threading.Event()
    release = threading.Event()
    loads: "list[int]" = []

    def slow_load():
        loads.append(1)
        entered.set()
        assert release.wait(5)
        return fake

    monkeypatch.setattr(webview_shell, "_load_webview", slow_load)
    monkeypatch.setattr(webview_shell, "_webview2_installed", lambda: True)
    monkeypatch.setattr(webview_shell, "_window_open", False)
    first: "list[str]" = []
    thread = threading.Thread(target=lambda: first.append(webview_shell.make_desktop_opener()("http://127.0.0.1:7860")))
    thread.start()
    assert entered.wait(5)
    assert webview_shell.make_desktop_opener()("http://127.0.0.1:7860") == webview_shell.WINDOW_CLOSED
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert first == [webview_shell.WINDOW_CLOSED]
    assert len(loads) == 1 and len(fake.created) == 1 and len(fake.starts) == 1


def test_opener_resets_flag_when_start_raises(fake_webview, monkeypatch, caplog):
    def boom(**kw):
        raise RuntimeError("start 崩了")

    fake_webview.start = boom
    opened: "list[str]" = []
    monkeypatch.setattr(webview_shell.webbrowser, "open", lambda url, new=2: opened.append(url))
    with caplog.at_level(logging.WARNING, logger="biodata.webview_shell"):
        result = webview_shell.make_desktop_opener()("http://127.0.0.1:7860")
    assert result == webview_shell.FALLBACK_BROWSER
    assert opened == ["http://127.0.0.1:7860"]
    assert webview_shell._window_open is False          # 异常路径复位，允许回退后再试
    assert any("回退" in r.message for r in caplog.records)


def test_opener_falls_back_when_webview_missing(monkeypatch, caplog):
    opened: "list[str]" = []
    monkeypatch.setattr(webview_shell, "_load_webview", lambda: None)
    monkeypatch.setattr(webview_shell, "_window_open", False)
    monkeypatch.setattr(webview_shell.webbrowser, "open", lambda url, new=2: opened.append(url))
    with caplog.at_level(logging.WARNING, logger="biodata.webview_shell"):
        result = webview_shell.make_desktop_opener()("http://127.0.0.1:7860")
    assert result == webview_shell.FALLBACK_BROWSER
    assert opened == ["http://127.0.0.1:7860"]
    assert webview_shell._window_open is False
    assert any("未安装/不可用" in r.message for r in caplog.records)


def test_opener_falls_back_when_webview2_missing(fake_webview, monkeypatch, caplog):
    monkeypatch.setattr(webview_shell, "_webview2_installed", lambda: False)
    opened: "list[str]" = []
    monkeypatch.setattr(webview_shell.webbrowser, "open", lambda url, new=2: opened.append(url))
    with caplog.at_level(logging.WARNING, logger="biodata.webview_shell"):
        result = webview_shell.make_desktop_opener()("http://127.0.0.1:7860")
    assert result == webview_shell.FALLBACK_BROWSER
    assert opened == ["http://127.0.0.1:7860"]
    assert fake_webview.created == []                  # 预检失败 → 根本不建窗（不落 MSHTML 白屏）
    assert webview_shell._window_open is False
    assert any("WebView2" in r.message for r in caplog.records)


def test_icon_candidates_source_and_frozen(monkeypatch):
    from dataset_recommender.app.runtime_paths import reset_app_paths_cache

    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    cands = webview_shell.icon_path_candidates()
    assert cands, "source 模式至少给出 install_root/packaging/assets 候选"
    assert cands[-1].name == "BioDataAgent.ico"
    # frozen 语义以 runtime_paths 单一真源判定（sys.frozen + 缓存重算），不再自读 _MEIPASS
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/fake/bundle", raising=False)
    reset_app_paths_cache()
    try:
        cands = webview_shell.icon_path_candidates()
        assert cands[0] == Path("/fake/bundle") / "assets" / "BioDataAgent.ico"   # frozen 候选优先
    finally:
        reset_app_paths_cache()


def test_attach_window_chrome_subscribes_before_show(fake_webview, monkeypatch):
    """原生句柄建立后再统一应用 caption + 图标；无图标仍必须设置 caption。"""
    win = _FakeWin()
    ico = Path(__file__).resolve().parents[1] / "packaging" / "assets" / "BioDataAgent.ico"
    monkeypatch.setattr(webview_shell, "icon_path_candidates", lambda: [ico])
    applied: "list[tuple[object, Path | None]]" = []
    monkeypatch.setattr(webview_shell, "_apply_window_chrome_win32", lambda w, p: applied.append((w, p)))
    webview_shell._attach_window_chrome_on_show(win)
    assert len(win.events.before_show) == 1
    assert len(win.events.shown) == 1
    win.events.before_show[0]()
    assert applied == [(win, ico)]
    colors: "list[object]" = []
    monkeypatch.setattr(webview_shell, "_set_titlebar_colors_win32", lambda w: colors.append(w))
    win.events.shown[0]()
    assert colors == [win]
    no_icon = _FakeWin()
    monkeypatch.setattr(webview_shell, "icon_path_candidates", lambda: [])
    webview_shell._attach_window_chrome_on_show(no_icon)
    assert len(no_icon.events.before_show) == 1
    assert len(no_icon.events.shown) == 1
    no_icon.events.before_show[0]()
    assert applied[-1] == (no_icon, None)


def test_shown_callback_discards_titlebar_result_dict(fake_webview, monkeypatch):
    """pywebview Event.execute 把回调返回值塞进 set——_set_titlebar_colors_win32 返回 dict，
    shown 回调必须吞掉返回值返回 None，否则真实壳抛 unhashable type: 'dict'。"""
    win = _FakeWin()
    monkeypatch.setattr(webview_shell, "icon_path_candidates", lambda: [])
    monkeypatch.setattr(webview_shell, "_apply_window_chrome_win32", lambda w, p: None)
    monkeypatch.setattr(webview_shell, "_set_titlebar_colors_win32", lambda w: {20: 0})
    webview_shell._attach_window_chrome_on_show(win)
    assert win.events.shown[0]() is None


# ---------------------------------------------------------------- 下载中关窗（第 6 项）
def test_download_active_flag_defaults_false_and_settable(monkeypatch):
    monkeypatch.setattr(webview_shell, "_download_active", False)
    assert webview_shell.is_download_active() is False
    webview_shell.set_download_active(True)
    assert webview_shell.is_download_active() is True
    webview_shell.set_download_active(False)
    assert webview_shell.is_download_active() is False


def test_subscribe_close_download_guard_wires_closing_event():
    win = _FakeWin()
    webview_shell._subscribe_close_download_guard(win)
    assert len(win.events.closing) == 1
    assert win.events.closing[0] is webview_shell._on_window_closing


def test_bind_download_activity_connects_manager_to_shell(monkeypatch):
    from dataset_recommender.corpus import download_manager

    bound = []
    monkeypatch.setattr(download_manager, "bind_activity_callback", bound.append)
    webview_shell._bind_download_activity()
    assert bound == [webview_shell.set_download_active]


def test_on_window_closing_warns_and_notifies_when_download_active(monkeypatch, caplog):
    monkeypatch.setattr(webview_shell, "_download_active", True)
    notified: "list[int]" = []
    monkeypatch.setattr(webview_shell, "_notify_download_close", lambda: notified.append(1))
    with caplog.at_level(logging.WARNING, logger="biodata.webview_shell"):
        webview_shell._on_window_closing()
    assert "下载将中断" in caplog.text
    assert notified == [1]


def test_on_window_closing_silent_when_no_download(monkeypatch, caplog):
    monkeypatch.setattr(webview_shell, "_download_active", False)
    notified: "list[int]" = []
    monkeypatch.setattr(webview_shell, "_notify_download_close", lambda: notified.append(1))
    with caplog.at_level(logging.WARNING, logger="biodata.webview_shell"):
        webview_shell._on_window_closing()
    assert "下载将中断" not in caplog.text
    assert notified == []


def test_set_window_icon_noop_on_missing_file_and_zero_hwnd(tmp_path):
    win = _FakeWin()
    webview_shell._set_window_icon_win32(win, tmp_path / "不存在.ico")   # 文件缺失 → 静默
    webview_shell._set_window_icon_win32(win, Path("unused"))            # hwnd=0 → 静默返回
    # 均不抛异常即通过（图标是 best-effort 锦上添花）


def test_set_window_icon_updates_winforms_property_before_show(monkeypatch, tmp_path):
    """WinForms Show 会重放 Form.Icon；生产路径必须改属性，不能只发 WM_SETICON。"""
    ico = tmp_path / "brand.ico"
    ico.write_bytes(b"fake-ico")
    native = SimpleNamespace(Handle=123, Icon=None)
    win = SimpleNamespace(native=native)

    class FakeIcon:
        def __init__(self, path):
            self.path = path

    fake_clr = ModuleType("clr")
    fake_clr.AddReference = lambda _name: None  # type: ignore[attr-defined]
    fake_drawing = ModuleType("System.Drawing")
    fake_drawing.Icon = FakeIcon  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "clr", fake_clr)
    monkeypatch.setitem(sys.modules, "System", ModuleType("System"))
    monkeypatch.setitem(sys.modules, "System.Drawing", fake_drawing)
    monkeypatch.setattr(webview_shell.sys, "platform", "win32")
    monkeypatch.setattr(webview_shell, "_icon_objects", [])

    webview_shell._set_window_icon_win32(win, ico)
    assert isinstance(native.Icon, FakeIcon)
    assert native.Icon.path == str(ico)
    assert webview_shell._icon_objects == [native.Icon]


# ---------------------------------------------------------------- origin 分流（H1）
def test_is_same_origin_compares_scheme_host_port():
    base = "http://127.0.0.1:7860"
    assert webview_shell.is_same_origin("http://127.0.0.1:7860/dataset?uid=x", base) is True
    assert webview_shell.is_same_origin("http://127.0.0.1:7860", base) is True
    # 端口变化：以 base_url（当前服务 origin）为准，动态判定
    assert webview_shell.is_same_origin("http://127.0.0.1:7861/x", base) is False
    assert webview_shell.is_same_origin("http://127.0.0.1:7861/x", "http://127.0.0.1:7861") is True
    assert webview_shell.is_same_origin("https://127.0.0.1:7860/x", base) is False   # scheme 变化
    assert webview_shell.is_same_origin("http://localhost:7860/x", base) is False    # host 变化
    assert webview_shell.is_same_origin("http://127.0.0.1/x", "http://127.0.0.1:80") is True  # 缺省端口归一
    assert webview_shell.is_same_origin("/relative", base) is False                  # 相对 URL 无法判定
    assert webview_shell.is_same_origin("not a url", base) is False


def test_link_router_same_origin_opens_second_shell_window(monkeypatch):
    created: "list[dict]" = []

    class _FakeWv:
        def create_window(self, title, url, **kwargs):
            created.append({"title": title, "url": url, **kwargs})
            return SimpleNamespace()

    opened: "list[str]" = []
    monkeypatch.setattr(webview_shell, "_attach_window_chrome_on_show", lambda win: None)
    monkeypatch.setattr(webview_shell, "_inject_new_window_router", lambda win: None)
    monkeypatch.setattr(webview_shell.webbrowser, "open", lambda url, new=2: opened.append(url))

    router = webview_shell._LinkRouter(_FakeWv(), "http://127.0.0.1:7860")
    router.open_link("http://127.0.0.1:7860/dataset?uid=1")
    assert len(created) == 1
    assert created[0]["title"] == "BioData Agent"
    assert created[0]["url"] == "http://127.0.0.1:7860/dataset?uid=1"
    assert created[0]["js_api"] is router          # 第二窗也挂同一 router，可继续分流
    assert created[0]["width"] == 1180 and created[0]["height"] == 720
    assert opened == []


def test_link_router_cross_origin_opens_system_browser(monkeypatch):
    created: "list[str]" = []

    class _FakeWv:
        def create_window(self, title, url, **kwargs):
            created.append(url)
            return SimpleNamespace()

    opened: "list[str]" = []
    monkeypatch.setattr(webview_shell, "_attach_window_chrome_on_show", lambda win: None)
    monkeypatch.setattr(webview_shell, "_inject_new_window_router", lambda win: None)
    monkeypatch.setattr(webview_shell.webbrowser, "open", lambda url, new=2: opened.append(url))

    router = webview_shell._LinkRouter(_FakeWv(), "http://127.0.0.1:7860")
    router.open_link("https://example.com/x")
    assert created == []
    assert opened == ["https://example.com/x"]


def test_opener_wires_origin_router_as_js_api(fake_webview):
    webview_shell.make_desktop_opener()("http://127.0.0.1:7860")
    (kw,) = fake_webview.created
    router = kw["js_api"]
    assert isinstance(router, webview_shell._LinkRouter)
    assert router._base_url == "http://127.0.0.1:7860"


def test_inject_new_window_router_subscribes_loaded(monkeypatch):
    evaluated: "list[str]" = []

    class _Win:
        def __init__(self):
            self.events = SimpleNamespace(loaded=_EventSlot())

        def evaluate_js(self, script):
            evaluated.append(script)

    win = _Win()
    webview_shell._inject_new_window_router(win)
    assert len(win.events.loaded) == 1
    win.events.loaded[0]()
    assert evaluated == [webview_shell._NEW_WINDOW_INTERCEPTOR_JS]


# ---------------------------------------------------------------- launcher 集成
class _FakeLauncherRunner:
    """捕获 main() 构造 Launcher 的参数（run 空转返回 0）。"""

    def __init__(self, **kwargs):
        self.kwargs = {"shell_mode": False, "window_runner": None, **kwargs}

    def run(self, argv):
        self.argv = argv
        return 0


def test_main_wires_window_runner_and_mode(monkeypatch):
    monkeypatch.delenv("BIODATA_SHELL", raising=False)
    made: "list[dict]" = []

    def fake_launcher(**kwargs):
        made.append({"shell_mode": False, "window_runner": None, **kwargs})
        return _FakeLauncherRunner(**kwargs)

    monkeypatch.setattr(dl, "Launcher", fake_launcher)
    assert dl.main(["--window"]) == 0
    assert made[-1]["shell_mode"] is True
    assert made[-1]["window_runner"] is not None    # 注入专用 runner（browser 保持默认→attach 不受污染）
    assert dl.main([]) == 0
    assert made[-1]["shell_mode"] is False
    assert made[-1]["window_runner"] is None


def test_main_wires_shell_via_env(monkeypatch):
    made: "list[dict]" = []
    monkeypatch.setattr(dl, "Launcher", lambda **kw: made.append(kw) or _FakeLauncherRunner(**kw))
    monkeypatch.setenv("BIODATA_SHELL", "window")
    assert dl.main([]) == 0
    assert made[-1]["shell_mode"] is True


class _FakeTray:
    def __init__(self):
        self.looped = False

    def run_message_loop(self):
        self.looped = True
        return None


def test_run_foreground_shell_quits_on_window_close(monkeypatch):
    """壳分支：WINDOW_CLOSED（用户关窗）→ 立即干净关停（exit_code=0），不建托盘。"""
    launcher = dl.Launcher(
        shell_mode=True,
        window_runner=lambda url: webview_shell.WINDOW_CLOSED,
    )
    shutdown: "list[int]" = []
    tray_made: "list[bool]" = []
    monkeypatch.setattr(launcher, "_wait_server_started", lambda server, thread: True)
    monkeypatch.setattr(launcher, "_shutdown",
                        lambda *a, exit_code=0: (shutdown.append(exit_code) or exit_code))
    monkeypatch.setattr(launcher, "_make_tray", lambda port, no_tray: tray_made.append(no_tray))
    monkeypatch.delenv("BIODATA_NO_BROWSER", raising=False)
    server = SimpleNamespace(should_exit=False)
    rc = launcher._run_foreground(server, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                                  None, port=7860)
    assert rc == 0 and shutdown == [0] and tray_made == []   # 全程无托盘


def test_run_foreground_shell_fallback_restores_tray(monkeypatch):
    """壳分支：FALLBACK_BROWSER → 恢复托盘并进托盘循环（浏览器模式语义）。"""
    launcher = dl.Launcher(
        shell_mode=True,
        window_runner=lambda url: webview_shell.FALLBACK_BROWSER,
    )
    shutdown: "list[int]" = []
    tray = _FakeTray()
    monkeypatch.setattr(launcher, "_wait_server_started", lambda server, thread: True)
    monkeypatch.setattr(launcher, "_shutdown",
                        lambda *a, exit_code=0: (shutdown.append(exit_code) or exit_code))
    monkeypatch.setattr(launcher, "_make_tray", lambda port, no_tray: tray)
    monkeypatch.setattr(launcher, "_notify", lambda title, text: None)
    monkeypatch.delenv("BIODATA_NO_BROWSER", raising=False)
    server = SimpleNamespace(should_exit=False)
    rc = launcher._run_foreground(server, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                                  None, port=7860)
    assert rc == 0 and tray.looped is True and shutdown == [0]


def test_run_foreground_shell_fallback_no_tray_polls(monkeypatch):
    """壳分支：FALLBACK + --no-tray → 轮询路径（一拍后退出，走关停）。"""
    launcher = dl.Launcher(
        shell_mode=True,
        window_runner=lambda url: webview_shell.FALLBACK_BROWSER,
    )
    fake_thread = SimpleNamespace(alive=True)

    def is_alive():
        if fake_thread.alive:                      # 第一次 True → 第二次 False，循环跑一拍即出
            fake_thread.alive = False
            return True
        return False

    fake_thread.is_alive = is_alive
    shutdown: "list[int]" = []
    monkeypatch.setattr(launcher, "_wait_server_started", lambda server, thread: True)
    monkeypatch.setattr(launcher, "_shutdown",
                        lambda *a, exit_code=0: (shutdown.append(exit_code) or exit_code))
    monkeypatch.setattr(launcher, "_server_thread", fake_thread)
    monkeypatch.setattr(dl.time, "sleep", lambda s: None)
    monkeypatch.setattr(launcher, "_notify", lambda title, text: None)
    monkeypatch.delenv("BIODATA_NO_BROWSER", raising=False)
    server = SimpleNamespace(should_exit=False)
    launcher._run_foreground(server, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
                             None, port=7860, no_tray=True)   # --no-tray → _make_tray 返回 None → 轮询
    assert shutdown == [0]
