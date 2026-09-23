# -*- coding: utf-8 -*-
"""主区结果网格 ↔ 侧栏对话窗换位的静态测试钉。

背景：用户反馈「出结果后对话窝在左下角小窗」。在侧栏工作卡 .sw-switch 行内加
#swSwapBtn，交换 #resultsGrid 与对话套件（#cbHistory + #chatComposer）的显示位置——
对话进结果区的 #chatStage，结果网格以 grid-mini 紧凑卡进侧栏 #sideBoardScroll。
 二轮（同用户看截图后的 4 点）：① 头部件（标题行/摘要卡/诚实回显条）静态包进
#resultsHead（正常态 display:contents 透明），交换态随网格一起进侧栏；② 主区纯聊天
——.chat-stage 去白卡框、列宽 760→880px；③ grid-mini 恢复「数据集详情」小钮；
④ 两交付版本共享本 UI，缓存令牌 20260816- → 20260816-。
 三轮（同用户再看截图后的 2 点）：① 交换态「检索结果」页签隐藏常驻查询条件栏
#swHits（#sideWork[data-sw-mode] 标识 + 纯 CSS display:none，placeHitsBar 不受影响）；
② 输入条结构性钉底（交换态查询视图恰好一屏、页面不滚动，#chatStage flex:1 吃剩余高度，
替代 的 calc(100vh-56px) 视口硬算）；令牌 20260816- → 20260816-。
 四轮：交换态侧栏「检索结果」页签头部件紧凑化（约 350px → ≤150px，A/B 对比后
定稿方案 A=单行标题栏）——标题行并一行（按钮内双 span：.bt-full 完整文案 / .bt-short
「可复用/打包」短文案，纯 CSS 按上下文切换）；摘要卡 12px/2 行截断（title 兜全文）；
放宽提示折叠单行（「放宽方式 ▸」钮留在折叠行，:has(+ .cov-detail:not([hidden])) 解除截断，
max-height 过渡 + reduced-motion 门）；两卡视觉合并为一卡上下节（虚线分隔）。一切紧凑化
钉在 body.view-swapped + #sideWork[data-sw-mode="board"] 作用域，正常态像素级不变。

沿用项目既有的静态门范式（同 test_frontend_fixes.py / test_board_frontend_static.py）：
钉关键代码形态在场，防回退。运行态行为由内部 Playwright 冒烟脚本（未随公共仓发布）验证。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------- 静态结构（index.html）

def test_swap_button_in_sw_switch() -> None:
    """交换钮在 .sw-switch 行内（两页签右侧），aria-pressed 初态 false。"""
    html = _read("web/static/index.html")
    assert 'id="swSwapBtn"' in html, "缺 #swSwapBtn 交换钮"
    assert 'class="sw-swap" id="swSwapBtn" aria-pressed="false"' in html
    assert "把对话放大到主区，结果收进侧栏" in html, "交换钮 title 文案缺失"
    # 位置钉：必须在 swTabBoard 之后、sw-switch 闭合之前（行内第三格）
    assert html.index('id="swSwapBtn"') > html.index('id="swTabBoard"')


def test_chat_stage_static_home() -> None:
    """#chatStage 静态家在 #resultsWrap 内、#resultsGrid 之前，非交换态恒 hidden。"""
    html = _read("web/static/index.html")
    assert '<div class="chat-stage" id="chatStage" hidden>' in html
    assert 'id="chatStageLog"' in html and 'id="chatStageBar"' in html, "舞台上下槽缺失"
    assert html.index('id="chatStage"') > html.index('id="taskPackPanel"')
    assert html.index('id="chatStage"') < html.index('id="resultsGrid"'), (
        "#chatStage 必须在 #resultsGrid 之前（交换时网格搬去侧栏，舞台占它的位置）"
    )


def test_resultshead_static_wrap() -> None:
    """：头部件静态包进 #resultsHead——标题行起、identifierLookup 止；两展开面板不进容器。"""
    html = _read("web/static/index.html")
    assert '<div id="resultsHead">' in html, "缺 #resultsHead 包装容器"
    assert html.index('<div id="resultsHead">') < html.index('class="results-head"'), (
        "#resultsHead 必须包住标题行"
    )
    close = html.index("/#resultsHead")
    for nid in ('id="searchTrace"', 'id="unusedQueryTerms"', 'id="orHandling"',
                'id="actionHint"', 'id="identifierLookup"'):
        assert html.index(nid) < close, f"{nid} 必须在 #resultsHead 内（交换态随结果进侧栏）"
    for nid in ('id="feasibilityPanel"', 'id="taskPackPanel"'):
        assert html.index(nid) > close, f"{nid} 为主区宽度设计，必须留在 #resultsHead 外"
    # 容器内 id 一律不动（只搬节点不重建）：摘要卡与诚实条仍是原 id
    assert 'id="resultSummaryText"' in html and 'id="coverageCaveats"' in html


# ---------------------------------------------------------------- 搬家逻辑（board.js）

def test_view_swap_state_and_gate() -> None:
    """状态真源 = 旗标 + body.view-swapped；生效判据五要件缺一不可。"""
    board = _read("web/static/js/panel/board.js")
    assert "let _viewSwap = false;" in board, "交换旗标不落盘、初态恒假"
    assert "function viewSwapEffective()" in board
    for needle in ('"on-query"', '"has-results"', '"side-closed"', "window.innerWidth > 780", "_viewSwap"):
        assert needle in board, f"viewSwapEffective 缺判据要件 {needle}"
    assert '"view-swapped"' in board, "body.view-swapped 状态类缺失"


def test_place_chat_suite_is_the_single_mover() -> None:
    """搬家唯一收口 placeChatSuite 挂在 swApplyMode 链尾；交换文案/aria 同步。"""
    board = _read("web/static/js/panel/board.js")
    assert "function placeChatSuite()" in board
    assert "placeChatSuite();" in board, "placeChatSuite 未挂进 swApplyMode 链"
    assert 'grid.classList.add("grid-mini")' in board
    assert 'grid.classList.remove("grid-mini")' in board
    assert "检索结果（紧凑视图）" in board, "侧栏滚动槽 aria-label 交换文案缺失"
    assert 'tabLabel.textContent = "检索结果"' in board
    assert 'tabLabel.textContent = "继续对话"' in board, "退出交换必须恢复页签原文案"
    assert "把结果放回主区，对话收回侧栏" in board, "交换态按钮 title 文案缺失"
    assert "scopePopOpen(false)" in board, "交换前必须先关范围弹层（锚在 composer 上）"
    # placeChatLog 认识交换态：对话记录的主区家是 #chatStageLog
    assert '$("chatStageLog")' in board, "placeChatLog 缺交换态落点"
    # #resultsHead 随 grid 一起搬——进侧栏顶部（卡片列表之前）、出交换回 wrap 首子
    assert '$("resultsHead")' in board, "placeChatSuite 缺 #resultsHead 搬家"
    assert "scroll.appendChild(head)" in board, "交换态头部件必须随结果进侧栏"
    assert "wrap.insertBefore(head, wrap.firstChild)" in board, "退出交换头部件必须回静态原位（首子）"


def test_swap_button_wired_in_init_side_work() -> None:
    board = _read("web/static/js/panel/board.js")
    assert '$("swSwapBtn")' in board and "addEventListener" in board, "交换钮点击接线缺失"
    assert "_viewSwap = !_viewSwap;" in board


# ---------------------------------------------------------------- 样式（app.css）

def test_compact_grid_and_stage_styles() -> None:
    css = _read("web/static/css/app.css")
    assert "#resultsGrid.grid-mini" in css, "grid-mini 紧凑卡作用域缺失"
    assert ".grid-mini .card-why" in css, "紧凑卡必须隐去推荐理由等非必要块"
    assert ".grid-mini .files-open" in css
    assert ".chat-stage" in css and ".chat-stage-log" in css and ".chat-stage-bar" in css
    assert ".sw-swap" in css, "交换钮样式缺失"
    # 滑块宽度随三格布局重算（原 calc(50% - 4.5px) 的两等分前提已破）
    assert "width: calc(50% - 20px);" in css, ".sw-glide 宽度未按三格重算"


def test_resultshead_styles() -> None:
    """：#resultsHead 正常态 display:contents 透明；交换态侧栏紧凑规则在场
    （标题行/摘要卡的窄态形态 起由 body.view-swapped+#sideWork[data-sw-mode=board]
    作用域规则接替，见 test_compact_head_scoped_to_swap_board）。"""
    css = _read("web/static/css/app.css")
    assert "#resultsHead { display: contents; }" in css, "正常态必须对布局完全透明（视觉零变化）"
    assert ".view-swapped #sideBoardScroll .info-bar" in css, "交换态侧栏提示条紧凑规则缺失"
    assert ".view-swapped #sideBoardScroll .facetbar" in css, "交换态侧栏分面条规则缺失"


def test_chat_stage_no_card_frame() -> None:
    """：主区纯聊天——白卡框（背景/边框/圆角/阴影）已去，列宽 760 → 880px。"""
    css = _read("web/static/css/app.css")
    assert "max-width: 880px; margin-left: auto; margin-right: auto;" in css, "对话列宽未放宽到 880px"
    # 白卡框四件套的 形态必须不在（.chat-stage-log 现在是裸 flex 容器）
    assert (".chat-stage-log { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column;\n"
            "    background: var(--surface)") not in css, "白卡框背景/边框仍在（已去框）"


def test_grid_mini_detail_button_restored() -> None:
    """：紧凑卡恢复「数据集详情」小钮（不再隐藏 .btn-detail，压小号全宽行）。"""
    css = _read("web/static/css/app.css")
    assert ".grid-mini .btn-detail" in css, "grid-mini 缺 .btn-detail 小号规则"
    assert ".grid-mini .card-foot, .grid-mini .btn-detail," not in css, ".btn-detail 仍在隐藏清单里"


def test_sw_hits_hidden_only_on_swap_board_tab() -> None:
    """：交换态「检索结果」页签隐藏 #swHits——页签标识 data-sw-mode + 纯 CSS display:none。"""
    css = _read("web/static/css/app.css")
    board = _read("web/static/js/panel/board.js")
    assert 'body.view-swapped #sideWork[data-sw-mode="board"] #swHits { display: none; }' in css, (
        "缺组合选择器：交换态 + board 页签才隐藏 #swHits（facets 页签/正常态不命中）"
    )
    assert "cardEl.dataset.swMode = mode" in board, "swApplyMode 未给 #sideWork 落页签标识"
    # 只钉 display:none 形态（不动节点/hidden 属性），placeHitsBar 搬家逻辑零改动
    assert "function placeHitsBar()" in board


def test_structural_pin_bottom() -> None:
    """：输入条结构性钉底——交换态视图恰好一屏（overflow:hidden 页面不滚动）+
    resultsWrap flex 列 + chatStage flex:1 吃剩余高度； 的视口硬算已退役。"""
    css = _read("web/static/css/app.css")
    assert 'body.view-swapped .view.active[data-view="query"]' in css and "overflow: hidden;" in css, (
        "交换态查询视图必须恰好一屏且不滚动（无双滚动条）"
    )
    assert "body.view-swapped #resultsWrap { flex: 1 1 auto; min-height: 0; display: flex !important;" in css, (
        "resultsWrap 必须变 flex 列（!important 压 inline display:block）"
    )
    assert "calc(100vh - 56px)" not in css, "56px 视口硬算必须退役（别的视口会脱底）"
    assert ".chat-stage { display: flex; flex-direction: column; gap: 12px; margin-top: 6px;\n    flex: 1 1 auto; min-height: 0; }" in css, (
        "chatStage 必须 flex:1 吃剩余高度（输入条恒在视口底）"
    )


def test_sidebar_state_machine_alphabet_knows_view_swapped() -> None:
    """侧栏状态机冒烟的合法 body 类字母表必须收录 view-swapped（否则交换态被误判残留）。"""
    smoke = _read("scripts/smoke_sidebar_states.py")
    assert '"view-swapped"' in smoke


# ---------------------------------------------------------------- 头部件紧凑化（方案 A 定稿）

def test_dual_span_button_labels() -> None:
    """：两枚头部按钮改双 span 文案（.bt-full 完整 / .bt-short 短），id/行为不动。
    .bt-full 文案精简（「📊 可复用数据」「📦 下载这批数据」），title 兜完整文案不变。"""
    html = _read("web/static/index.html")
    for btn, full, short in (
        ('id="feasibilityBtn"', "📊 可复用数据", "📊 可复用"),
        ('id="taskPackBtn"', "📦 下载这批数据", "📦 下载"),
    ):
        seg = html[html.index(btn):html.index(btn) + 400]
        assert f'<span class="bt-full">{full}</span>' in seg, f"{btn} 缺 .bt-full 完整文案 span"
        assert f'<span class="bt-short">{short}</span>' in seg, f"{btn} 缺 .bt-short 短文案 span"
    css = _read("web/static/css/app.css")
    # 默认态只显完整文案（正常态像素级不变）
    assert ".results-head-acts .bt-short { display: none; }" in css
    # 交换态窄栏切换：藏完整、显短文案
    assert "body.view-swapped #sideWork[data-sw-mode=\"board\"] .results-head-acts .bt-full { display: none; }" in css
    assert "body.view-swapped #sideWork[data-sw-mode=\"board\"] .results-head-acts .bt-short { display: inline; }" in css


def test_compact_head_scoped_to_swap_board() -> None:
    """：紧凑化规则全部钉在 body.view-swapped + #sideWork[data-sw-mode=\"board\"] 作用域——
    正常态与「细化筛选」页签不命中；标题行并一行（按钮不再纵向满宽）。"""
    css = _read("web/static/css/app.css")
    scope = 'body.view-swapped #sideWork[data-sw-mode="board"]'
    # .rs-coverage 哑化上移为通用态（.res-overview .rs-coverage），交换态改钉 .res-overview 紧凑卡壳
    for needle in (".res-overview {", ".results-head {", ".rs-text {", ".cov-txt {", ".cov-expand {"):
        assert scope + " " + needle in css, f"缺作用域规则 {scope} {needle}"
    assert "flex-wrap: nowrap;" in css, "标题行必须并一行（不再纵向堆按钮）"
    # 摘要 2 行截断
    assert "-webkit-line-clamp: 2;" in css
    # 放宽提示折叠单行 + 展开解除（:has 相邻 detail 非 hidden）
    assert "white-space: nowrap; overflow: hidden; text-overflow: ellipsis;" in css
    assert ".cov-row:has(+ .cov-detail:not([hidden])) .cov-txt" in css, "展开态必须解除截断（完整来源分计数回流）"
    # reduced-motion 门关展开动画
    assert "@media (prefers-reduced-motion: reduce)" in css
    # 的侧栏纵向满宽按钮规则已退役（被 单行栏接替）
    assert ".view-swapped #sideBoardScroll .results-head-acts { width: 100%;" not in css


def test_summary_title_fallback() -> None:
    """：摘要截断的 title 兜全文——renderResultSummary 写 txt.title。"""
    js = _read("web/static/js/search/results.js")
    assert 'txt.title = txt.textContent || "";' in js, "摘要卡缺 title 兜全文（截断后全文不可触达）"
