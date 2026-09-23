# -*- coding: utf-8 -*-
"""档案浮窗（历史记录 + 我的课题合并为单窗双 tab）的前端静态门。

钉的是这条定稿：历史与课题合成一个**弹出式浮窗**——与主界面同层（非模态）、可拖动、可缩放、
双 tab 切换；历史点行本体即在本标签页找回该对话；行尾动作＝新标签页打开（?conv=）/ 重新检索 /
删除（二段确认）。骨架（开合/拖动/缩放/落位/tab）唯一属主在 shell.js（initArchiveWin），
browse.js 与 projects.js 只经 setArchiveRenderer 注册各自 tab 的渲染器。
静态门不执行 JS，只钉「结构件与接线都在」，防的是改结构时悄悄丢掉某一环
（本仓库的惯例：有行为必有静态门，见 test_unified_box.py 头部说明）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"

HTML = (STATIC / "index.html").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
BROWSE = (STATIC / "js" / "search" / "browse.js").read_text(encoding="utf-8")
INTERACTIONS = (STATIC / "js" / "core" / "interactions.js").read_text(encoding="utf-8")
SHELL = (STATIC / "js" / "core" / "shell.js").read_text(encoding="utf-8")
BOOT = (STATIC / "js" / "core" / "boot.js").read_text(encoding="utf-8")
ACCOUNTS = (STATIC / "js" / "panel" / "accounts.js").read_text(encoding="utf-8")


def test_history_is_a_floating_window_not_a_view():
    """独立页面形态已移除：不再有 data-view="history" 或 data-view="favorites" 的 section；
    我的库浮窗（#libWin 双页签 追踪/收藏）+ 历史独立浮窗（#histWin 单页签）骨架都在。"""
    assert '<section class="view" data-view="history">' not in HTML, "历史记录独立页面还在"
    assert '<section class="view" data-view="favorites">' not in HTML, "我的收藏独立页面还在"
    # 我的库浮窗：双页签（追踪/收藏），#artifactsWinBody 保留作追踪页签挂点（追踪/导出/e2e 取节点）
    for token in ('id="libWin"', 'id="libWinHead"', 'id="libWinBody"',
                  'id="libWinResize"', 'id="libWinClose"', 'id="libTabTracks"', 'id="libTabFavs"',
                  'id="artifactsWinBody"', 'id="libPaneFavs"', 'data-lib-active="tracks"'):
        assert token in HTML, f"index.html 缺少 {token}"
    # 历史独立浮窗（单页签，无 .arc-tabs）
    for token in ('id="histWin"', 'id="histWinHead"', 'id="histWinBody"',
                  'id="histWinResize"', 'id="histWinClose"', 'id="histClear"', 'id="histList"', 'id="histEmpty"'):
        assert token in HTML, f"index.html 缺少 {token}"
    # 历史/库两窗都是非模态 fixed 层，走全局层叠令牌（不许裸写魔法 z-index）
    assert re.search(r"\.hist-win\s*\{[^}]*position:\s*fixed", CSS)
    assert "--z-floatwin" in CSS and "z-index: var(--z-floatwin)" in CSS


def test_nav_history_toggles_the_window_not_a_view():
    """导航「我的库」「历史记录」点击 → toggleLibWin / toggleHistWin，**不**走 showView；
    showView 里也不再有 history/archive/favorites 分支。"""
    nav = re.search(r'nav-item\[data-view\]"\)[^;]*?=> \{(.*?)\}\)\);', INTERACTIONS, re.S)
    assert nav and 'toggleLibWin()' in nav.group(1) and 'toggleHistWin()' in nav.group(1), "导航我的库/历史记录项没有接浮窗开关"
    assert 'n.dataset.view === "lib"' in nav.group(1), "导航我的库早退分支缺席"
    assert 'n.dataset.view === "history"' in nav.group(1), "导航历史记录早退分支缺席"
    assert 'showView("lib")' not in INTERACTIONS + SHELL
    assert 'showView("history")' not in INTERACTIONS + SHELL
    assert 'if (name === "history")' not in SHELL, "showView 还留着 history 视图分支"
    assert 'if (name === "lib")' not in SHELL, "showView 不该有 lib 视图分支"
    assert "initHistWin()" in BOOT, "boot 初始化没有挂 initHistWin（历史渲染器注册 + conv/fork 落点）"
    assert "initLibWin()" in BOOT, "boot 初始化没有挂 initLibWin（我的库浮窗骨架接线）"
    assert "initHistWinSkeleton()" in BOOT, "boot 初始化没有挂 initHistWinSkeleton（历史浮窗骨架接线）"


def test_window_is_draggable_and_resizable():
    """标题栏拖动 + 右下角缩放：指针事件接线都在（骨架属主在 shell.js initFloatingWin 工厂），
    且都有视口约束（不许拖丢/拉爆）。"""
    assert re.search(r'\$\("libWinHead"\)', SHELL)
    assert re.search(r'\$\("libWinResize"\)', SHELL)
    assert re.search(r'\$\("histWinHead"\)', SHELL)
    assert re.search(r'\$\("histWinResize"\)', SHELL)
    assert SHELL.count("pointerdown") >= 2 and SHELL.count("pointermove") >= 2
    assert "innerWidth" in SHELL and "innerHeight" in SHELL, "拖/缩都要有视口边界"
    assert ".hw-resize" in CSS and "nwse-resize" in CSS


def test_each_conversation_row_click_and_actions():
    """每条对话：**点行本体**＝在本标签页找回（hist-main → viewHistorySnapshot，
    「本标签页显示」按钮退役）；行尾动作＝新标签页打开（?conv=）+ 重新检索 + 删除（二段确认）。"""
    assert "hist-view" not in BROWSE, "「本标签页显示」按钮应已退役——点行本体即找回"
    assert "hist-main" in BROWSE and "viewHistorySnapshot(" in BROWSE
    assert "hist-newtab" in BROWSE and "openPopup(" in BROWSE
    assert "window.open(" not in BROWSE, "browse.js 不许绕过 openPopup 裸调 window.open"
    assert '?conv=' in BROWSE and "encodeURIComponent" in BROWSE
    # 删除：每条行尾都有 hist-del；二段确认（armed）；删除写**当前账户命名空间**键（写裸键是账户隔离漏洞）
    assert "hist-del" in BROWSE and "armed" in BROWSE and "deleteHistoryGroup" in BROWSE
    assert "nsKey(LS.hist)" in BROWSE
    # 新标签页一侧：initHistWin 读 conv 参数 → 找对话 → viewHistorySnapshot；读完擦掉参数（刷新不重复触发）
    m = re.search(r'params\.get\("conv"\)(.*?)\}\s*catch', BROWSE, re.S)
    assert m and "viewHistorySnapshot(" in m.group(1) and "replaceState" in m.group(1)


def test_account_switch_rerenders_the_open_window():
    """账户切换：我的库/历史浮窗开着就经 shell 的 libRefreshActive / histRefreshActive
    重调当前页签渲染器；accounts.js 不再直取历史渲染器。"""
    assert "libRefreshActive" in ACCOUNTS
    assert "histRefreshActive" in ACCOUNTS
    assert "histWinOpen()" not in ACCOUNTS


def _strip_comments(src: str) -> str:
    """只留代码（与 test_unified_box.py 同款去注释器）。"""
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", out)


def test_chat_only_rows_render_and_restore_honestly():
    """仅对话历史行：展示照实报「仅对话 · N 条消息」、不给「重新检索」按钮
    （没有可重跑的检索句）；恢复走 chatOnly 专支——退出结果态 + cbRestoreConversation + swSync
    （对话落回主区 #chatMain），不套「无快照回退重跑」那条路（那会把工具句当检索句发出去）。"""
    code = _strip_comments(BROWSE)
    assert "chatOnly" in code, "browse.js 不认识 chatOnly 标记"
    assert "仅对话" in code, "仅对话行的展示文案缺席"
    snap = re.search(r"function viewHistorySnapshot\([^)]*\)\s*\{(.*?)\n\}", BROWSE, re.S)
    assert snap, "找不到 viewHistorySnapshot"
    scode = _strip_comments(snap.group(1))
    i_chatonly = scode.index("h.chatOnly")
    i_legacy = scode.index("!h.snap")
    assert i_chatonly < i_legacy, "chatOnly 分支必须先于「无快照回退重跑」兜底"
    branch = scode[i_chatonly:]
    assert "exitResultsLayout()" in branch, "恢复仅对话前必须退出结果态（屏上旧结果照实收起）"
    assert "cbRestoreConversation(chron)" in branch, "仅对话也要整条对话搬回"
    assert "swSync()" in branch, "恢复后要重排落位（无结果 → 对话住主区 #chatMain）"
    # 恢复分支里绝不许出现 runRecommend（仅对话没有检索句可重跑）
    seg = branch[:branch.index("return;")]
    assert "runRecommend" not in seg, "仅对话恢复不许触发检索"


def test_resize_uses_real_drag_geometry():
    """D1：右下角缩放按真实拖拽几何——setPointerCapture（滑出把手不断拖）
    + 按下时记偏移（第一步不再跳缩 11px）+ 双向正确 + 最小/最大钳制保留。
    骨架属主迁到 shell.js（initArchiveWin），几何参数不变。"""
    rz = re.search(r'if \(rz\) rz\.addEventListener\("pointerdown", \(e\) => \{(.*?)\n    \}\);', SHELL, re.S)
    assert rz, "找不到缩放把手的 pointerdown 接线"
    code = _strip_comments(rz.group(1))
    assert "setPointerCapture" in code, "缺指针捕获（旧病：滑出把手即断拖）"
    assert re.search(r"offX\s*=\s*e\.clientX\s*-\s*r\.right", code), "必须记按下点距窗右缘的偏移"
    assert re.search(r"offY\s*=\s*e\.clientY\s*-\s*r\.bottom", code), "必须记按下点距窗下缘的偏移"
    assert "ev.clientX - offX - r.left" in code, "新宽必须按「指针位移量」换算（双向不反向）"
    assert "ev.clientY - offY - r.top" in code
    assert "Math.max(360" in code and "Math.max(260" in code, "最小尺寸钳制丢失"
    assert "innerWidth - r.left" in code and "innerHeight - r.top" in code, "最大尺寸钳制丢失"
    assert "pointercancel" in code, "指针取消（系统抢指针）也要收尾"


def test_first_placement_stays_inside_the_viewport_on_mobile():
    """落位的 +110 桌面右偏在 ≤780px 视口豁免（验证移动宽度右缘溢出 98px、
    按钮不可达）；任何宽度下右缘钳进视口。落位逻辑在 shell.js openArchiveWin。"""
    place = re.search(r"dataset\.placed = \"1\";(.*?)\n        \}", SHELL, re.S)
    assert place, "找不到首次落位块"
    code = _strip_comments(place.group(1))
    assert "window.innerWidth > 780" in code, "移动宽度豁免缺席"
    assert "Math.min(" in code, "右缘钳制缺席"
    assert "window.innerWidth - ww - 12" in code, "右缘钳制口径缺席"


def test_open_window_is_reclamped_on_resize_and_close_is_animated():
    """① 浮窗开着时缩窗要把它重钳回视口
    （钳制此前只在首开算一次，缩窗后关闭按钮出屏不可达）；② 关闭播退出动画（与入场
    histWinIn 对称），动画播完才 hidden；③ Esc 逐层链接管我的库/历史浮窗。
    骨架属主迁到 shell.js initFloatingWin 工厂（reclamp / close，单一关窗计时器每窗一份）。"""
    code = _strip_comments(SHELL)
    assert re.search(r"function reclamp\(\)", code), "resize 重钳函数缺席"
    assert 'addEventListener("resize", reclamp)' in code, "resize 监听未接重钳"
    close_fn = re.search(r"function close\(\)\s*\{(.*?)\n    \}", code, re.S)
    assert close_fn, "找不到 close（工厂关窗函数）"
    assert "hist-win-out" in close_fn.group(1) and "setTimeout" in close_fn.group(1), (
        "关闭必须先挂退出类、动画播完才 hidden"
    )
    assert "REDUCE_MOTION" in close_fn.group(1), "reduced-motion 必须直接 hidden（不播动画）"
    assert "histWinOut" in CSS and "hist-win-out" in CSS, "退出动画关键帧/类样式缺席"
    esc = re.search(r'document\.addEventListener\("keydown", \(e\) => \{(.*?)\n    \}\);', INTERACTIONS, re.S)
    assert esc and "libWinOpen()" in esc.group(1) and "closeLibWin()" in esc.group(1) and "histWinOpen()" in esc.group(1), (
        "Esc 逐层链没有接管我的库/历史浮窗"
    )
