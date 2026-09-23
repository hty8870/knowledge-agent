# -*- coding: utf-8 -*-
"""条件板前端的静态门。

三门都不执行 JS：`web_smoke_test.py` 只做字符串检查，`node --check` 只验语法。
所以「函数名打错了」这类问题在浏览器里表现为**静默什么都不发生**，没有任何红灯。
本仓库已经被同一种失效模式坑过至少两次（最近一次是拼错的来源函数让可行性概览只统计了基础库）。

这里对每一个跨文件的名字做**双端断言**：定义方有、调用方也有。名字一改就红。
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "web" / "static" / "js"
INDEX = ROOT / "web" / "static" / "index.html"


def _read(name: str) -> str:
    return (JS / name).read_text(encoding="utf-8")


def _strip_comments(source: str) -> str:
    """扫描代码里的坏模式之前先去掉注释。

    否则「这里刻意不用某个坏写法」这类说明本身会被当成违规命中——
    第一版就是这么误报的，而且一旦为了绕开它去改注释，反而丢掉了最该留下的那句话。
    """
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


BOARD = _read("panel/board.js")
BOARD_CORE = _read("panel/board_core.js")
SEARCH = _read("search/search.js")
RESULTS = _read("search/results.js")
BROWSE = _read("search/browse.js")
ACCOUNTS = _read("panel/accounts.js")
BOOT = _read("core/boot.js")
CORE = _read("core/core.js")
HTML = INDEX.read_text(encoding="utf-8")
CSS = (ROOT / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")


#: (符号, 定义所在文件内容, 调用方文件内容, 说明)
CROSS_MODULE_SYMBOLS = [
    ("renderCondBoard", BOARD, SEARCH, "每次检索落地后要重画条件板"),
    ("cbPushCurrent", BOARD, SEARCH, "推一帧进撤销栈"),
    ("cbClear", BOARD, RESULTS, "清空结果区时一并收起条件板"),
    ("cbRestoreConversation", BOARD, BROWSE, "从历史回看时把整条对话搬回来（帧栈 + 全部对话记录）"),
    ("initCondBoard", BOARD, BOOT, "启动时绑定条件板"),
    ("cbRowsFrom", BOARD_CORE, BOARD, "四分区归并"),
    ("cbSummary", BOARD_CORE, BOARD, "摘要句"),
    ("cbPushFrame", BOARD_CORE, BOARD, "帧栈推帧"),
    ("runRecommend", SEARCH, BOARD, "条件板改完之后要真的重新检索"),
    ("toggleQueryHit", _read("search/facets.js"), BOARD, "行内「不按这条筛」复用既有通道"),
    ("toggleLenient", RESULTS, BOARD, "行内「放宽」复用既有通道"),
    ("applyRecommendResult", SEARCH, BOARD, "回放历史帧"),
    ("resetSubmitButton", _read("core/progress.js"), BOARD, "撤销时复位检索按钮"),
    ("cbLogPush", BOARD, _read("search/facets.js"), "细化动作 / 说的话进「聊天+细化记录」"),
    ("cbLogClear", BOARD, SEARCH, "新查询＝新时间线，清空对话/细化记录"),
]


@pytest.mark.parametrize("symbol,defined_in,used_in,why", CROSS_MODULE_SYMBOLS,
                         ids=[s[0] for s in CROSS_MODULE_SYMBOLS])
def test_cross_module_symbol_exists_on_both_ends(symbol, defined_in, used_in, why):
    assert re.search(r"\b(?:function|let|const|var)\s+" + re.escape(symbol) + r"\b", defined_in), (
        f"{symbol} 没有定义（{why}）"
    )
    assert re.search(re.escape(symbol) + r"\s*\(", used_in), f"{symbol} 没有被调用（{why}）"


def test_hist_hooks_registration_contract():
    """历史打标的跨模块契约（2026-08-10 起由 import 改为注册反转——core→board 反向边切断）：
    board 定义并注册 cbConvId/cbLogForHistory，core 暴露 setHistHooks 且 pushHist 经钩子取值。"""
    assert re.search(r"export function cbConvId\(", BOARD), "board.js 缺 cbConvId 定义"
    assert re.search(r"export function cbLogForHistory\(", BOARD), "board.js 缺 cbLogForHistory 定义"
    assert "setHistHooks({ convId: cbConvId, logForHistory: cbLogForHistory })" in BOARD, (
        "board.js 未在 initCondBoard 注册历史打标钩子")
    assert re.search(r"export function setHistHooks\(", CORE), "core.js 缺 setHistHooks"
    assert "_histConvId" in CORE and "_histLogForHistory" in CORE, "core.js pushHist 未经钩子取值"


def test_or_handling_note_is_actually_wired_to_the_screen():
    """「或」的实际执行方式必须真的画到屏幕上——后端算了、前端不画，等于没做。

    2026-07-25 起「或」不再整句弃权，而是按引擎的真实能力执行（同维度多值＝或）。
    `fit` 的三档里有两档（`superset` / `narrower`）是**真实的语义偏离**，
    不播报就是静默钳位。本仓库栽过同型：后端算好 `coverage_caveats`、前端没接，全量门照旧全绿。

    三处一起钉（缺一处都会静默失效）：
      ① `results.js` 定义 `renderOrHandling`；
      ② 它被渲染主流程真的调用过（不是死函数）；
      ③ `index.html` 里有它要写入的 `#orHandling` 容器。
    """
    body = _strip_comments(RESULTS)
    assert "function renderOrHandling" in body, "results.js 里没有 renderOrHandling"
    assert body.count("renderOrHandling(") >= 2, "renderOrHandling 只有定义、没有调用点（死函数）"
    assert 'id="orHandling"' in HTML, "index.html 里缺 #orHandling 容器，回显无处可写"
    # 容器必须带 caution 样式类：这条提示的两档是「和你说的不一样」，不能长得像普通脚注。
    m = re.search(r'<div[^>]*id="orHandling"[^>]*>', HTML)
    assert m and "info-bar-caution" in m.group(0), m.group(0) if m else "no match"
    # **不许**在 JS 里重写 className：样式全靠 index.html 上那几个类承载，改 className 会把它们冲掉。
    assert not re.search(r"orHandling[\s\S]{0,400}?\.className\s*=", body), \
        "renderOrHandling 里出现了 className 赋值——会冲掉 index.html 上的样式类"


def test_board_never_guards_cross_module_calls_with_typeof():
    """typeof 守卫会把打错的函数名永久静默短路——这正是我们要靠上面那组断言暴露的问题。"""
    code = _strip_comments(BOARD)
    assert not re.search(r'typeof\s+[\w.]+\s*===\s*"function"', code), (
        "board.js 里出现了 typeof 守卫式跨模块调用"
    )


def test_board_core_stays_pure():
    for forbidden in ("localStorage", "sessionStorage", "document.", "fetch(", "Date.now("):
        assert forbidden not in BOARD_CORE, f"纯核里出现了 {forbidden}"


def test_board_only_persists_the_collapsed_preference_and_uses_the_account_namespace():
    """条件板唯一落盘的东西是「面板收没收起」这一个布尔值，而且必须走每账户的键。

    撤销栈里是完整的检索返回，落盘既会撑爆存储配额，也会把一个人的检索轨迹留给共用机器的下一个人。
    """
    # 允许落盘的只有**纯 UI 偏好**（不含查询/结果/撤销栈），且必须走每账户键。
    # 2026-07-23 加入 SIDE_MODE_KEY（侧栏看「数据细化」还是「对话记录」）——与收起偏好同性质。
    # 白名单**按名字列举**，不按个数：新增任何一个键都必须来这里显式说明它为什么不是用户数据。
    ALLOWED_KEYS = {"BOARD_COLLAPSED_KEY", "SIDE_MODE_KEY"}
    stores = re.findall(r"(localStorage|sessionStorage)\.(getItem|setItem|removeItem)\(([^)]*)\)", BOARD)
    assert stores, "至少应该持久化收起偏好"
    for _api, _method, args in stores:
        assert "nsKey(" in args, f"条件板的存储调用没有走每账户键：{args}"
        assert any(k in args for k in ALLOWED_KEYS), (
            f"条件板只允许持久化 UI 偏好（{sorted(ALLOWED_KEYS)}），却写了：{args}")
    assert "_cbStack" not in "".join(a for _x, _y, a in stores), "撤销栈绝不能落盘"


def test_push_frame_lives_in_run_recommend_not_in_apply_result():
    """推帧必须只在真正发起检索的那条路径上。

    applyRecommendResult 同时是「回到上一步」「从检索历史回看」「切账户重渲」三条路径的落点，
    把推帧放进去会造成「点两次上一步原地不动」和「三天前的一条历史被当成对话的下一步」。
    sr1（2026-08-16 检索工具化 Phase 2）后推帧随落地四件套收口进共享入口 landRecommendResult
    （runRecommend 两落地点与零命中救回换屏同走）——不变量不变：applyRecommendResult 仍不推帧。
    """
    body = re.search(r"function applyRecommendResult\([^)]*\)\s*\{(.*?)\n\}", SEARCH, re.S)
    assert body, "找不到 applyRecommendResult 的函数体"
    assert "cbPushCurrent(" not in body.group(1), "推帧不该写在 applyRecommendResult 里"
    run_body = re.search(r"async function runRecommend\([^)]*\)\s*\{(.*?)\n\}", SEARCH, re.S)
    assert run_body, "找不到 runRecommend 的函数体"
    assert run_body.group(1).count("landRecommendResult(") == 2, (
        "runRecommend 的两个落地点（缓存命中 / 网络返回）各走一次共享落地入口"
    )
    land_body = re.search(r"function landRecommendResult\([^)]*\)\s*\{(.*?)\n\}", SEARCH, re.S)
    assert land_body, "找不到 landRecommendResult 的函数体"
    assert land_body.group(1).count("cbPushCurrent(") == 1, "共享落地入口必须为每次落地推一帧"


def test_undo_cancels_inflight_request_and_resets_the_button():
    """撤销时这两步缺一不可：不作废在途请求，晚到的旧响应会顶掉刚恢复的帧；
    不复位按钮，检索按钮会永久转圈。"""
    body = re.search(r"function cbReplay\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 cbReplay 的函数体"
    assert "_recSeq" in body.group(1), "回放前必须让在途请求失效"
    assert "resetSubmitButton(" in body.group(1), "回放前必须复位检索按钮"


def test_applying_a_plan_invalidates_the_stale_hit_snapshot():
    """换了句子就必须清掉原始命中的旧快照，否则它会与新条件同屏给出互相矛盾的取值。"""
    body = re.search(r"function cbCommit\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 cbCommit 的函数体"
    # C2：四个分面状态的属主是 results.js（ESM），board.js 的重赋值一律经绞杀桥 setFacetState
    assert "setFacetState({ queryHits: [] })" in body.group(1), "写 queryInput 的分支里必须同时清掉命中快照"


def test_undo_restores_the_hit_snapshot_it_saved():
    """帧里必须存 `_queryHits`，回放时按帧还原。

    它是分面条那一整行「查询条件」的数据源，而 facets.js 只在「没有被忽略的条件」时才会重建它——
    回放到一个有忽略项的帧上，快照清空了就再也建不回来，那一整行凭空消失。
    """
    push = re.search(r"function cbPushCurrent\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    replay = re.search(r"function cbReplay\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert push and replay
    assert "queryHits:" in push.group(1), "推帧时必须把原始命中快照一起存进帧"
    assert "_queryHits = []" not in replay.group(1), "回放时不许把命中快照清成空"
    assert "frame.queryHits" in replay.group(1), "回放时必须从帧里还原命中快照"


def test_board_reads_its_conditions_from_its_own_frame_not_from_the_live_response():
    """`LAST_RECOMMEND_DATA` 会被主搜索框的每一次按键置成 null（那是给解释预览用的失效信号）。

    拿它当条件板的真源，就会出现「敲了个字 → 点条件板按钮 → 板上照旧显示旧条件，
    送去规划的 current_filters 却已经是空数组」。撤销栈的当前帧才是这批条件的出处。
    """
    body = re.search(r"function cbPlanBody\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 cbPlanBody 的函数体"
    assert "LAST_RECOMMEND_DATA" not in body.group(1), "规划请求不许直接读会被按键清空的那个全局"
    assert "cbFrameData(" in body.group(1) and "cbFrameQuery(" in body.group(1)


def test_failed_or_relaxed_screens_do_not_keep_a_stale_board_or_pack():
    """屏幕上已经是「检索失败」或「放宽预览」了，条件板和任务包还挂着上一次成功检索的口径，
    就是拿旧一次的条件给这一屏背书。分面条已经收了，这两个也必须收。"""
    catch = re.search(r"\}\s*catch \(err\) \{(.*?)\n\s*\} finally", SEARCH, re.S)
    assert catch, "找不到 runRecommend 的 catch 分支"
    assert 'condBoard' in catch.group(1) and "resetTaskPack(" in catch.group(1)
    # 零命中放松已收编为 ask.relax 卡单一通道（2026-09-14）：旧的空态卡 chips 通道
    # applyRelaxation 整条退役，不许在 results.js 复活。
    assert "function applyRelaxation" not in RESULTS, "applyRelaxation 已退役，不许复活"


def test_task_pack_plan_is_invalidated_on_every_new_result():
    """搜 A → 开面板 → 改搜 B → 再开面板，`_tpPlan` 还在就会画出 A 的清单并允许照它产包。"""
    task_pack_js = _read("act/task_pack.js")
    body = re.search(r"function syncTaskPackBar\([^)]*\)\s*\{(.*?)\n\}", task_pack_js, re.S)
    assert body, "找不到 syncTaskPackBar 的函数体"
    assert "resetTaskPack()" in body.group(1)
    assert "if (!hasResults) resetTaskPack" not in body.group(1), "只在没结果时作废是不够的"


def test_task_pack_panel_numbers_follow_the_selection():
    """以前只有按钮上的数字跟着勾选走，四档计数/装不了什么/主文件那句话全停在候选池口径。"""
    task_pack_js = _read("act/task_pack.js")
    body = re.search(r"function renderTaskPackPlan\([^)]*\)\s*\{(.*?)\n\}", task_pack_js, re.S)
    assert body, "找不到 renderTaskPackPlan 的函数体"
    code = body.group(1)
    assert "_tpChosen.has(m.dataset_uid)" in code, "「装不了什么」要按勾选过滤"
    assert "tierCount[" in code and "tierRows[" in code, "四档计数要按勾选重算"
    assert "primary_only_policy_zh" in code, "面板显示的必须是不含数字的政策句"
    assert "不含大模型重排" in code, "候选池的排序口径必须写明，别让用户以为就是屏幕上那几条"


def test_stale_pack_message_is_not_overwritten_by_the_finally_redraw(  ):
    """409 的「没有生成任何文件」渲染完，finally 里的重绘会立刻把它盖掉，用户什么都看不到。"""
    task_pack_js = _read("act/task_pack.js")
    body = re.search(r"async function buildTaskPack\([^)]*\)\s*\{(.*?)\n\}", task_pack_js, re.S)
    assert body, "找不到 buildTaskPack 的函数体"
    assert "replaced = true" in body.group(1)
    assert "if (_tpPlan && !replaced)" in body.group(1)


def test_board_markup_and_scripts_are_wired_in_index():
    for token in ('id="condBoard"', 'id="cbSummaryBar"', 'id="cbRows"',
                  'id="cbPreview"', 'id="cbStepHint"', 'id="cbHistory"',
                  # 数据细化「提交」暂存条（2026-07-24·点4）：board.js initCondBoard 不加守卫直接绑定，
                  # 名字打错会当场炸 boot——用 markup 门钉住这几个 id 存在，把「静默失效」变「立刻红」。
                  'id="facetStageBar"', 'id="facetStageSubmit"', 'id="facetStageCancel"'):
        assert token in HTML, f"index.html 缺少条件板节点 {token}"
    # 统一对话窗口：#cbInput / #cbSubmitBtn 已退役，唯一输入框是 #queryInput
    assert 'id="queryInput"' in HTML
    modules = [m.split("/")[-1] for m in re.findall(
        r'<script(?:\s+type="module")?\s+src="/static/js/([a-z_/]+)\.js', HTML)]
    assert modules[0] == "core" and modules[-1] == "boot", "加载顺序契约：core 最先、boot 最后"
    assert modules.index("board_core") < modules.index("board"), "纯核必须在界面层之前加载"
    assert modules.index("board") > modules.index("search"), "board.js 依赖 search.js 的全局"


def test_history_view_revert_handlers_are_defined_and_wired():
    """聊天记录里每条消息的「查看结果 / 回退至此」是新加的交互，三门都不执行 JS——
    定义方（cbViewFrame/cbRevertToFrame/cbHistoryClick）与调用方（#cbHistory 上的委托、
    cbHistoryClick 里对两个跳转函数的调用）都得钉住，否则改错名字会静默失效。"""
    for sym in ("cbViewFrame", "cbRevertToFrame", "cbHistoryClick"):
        assert re.search(r"\bfunction\s+" + sym + r"\b", BOARD), f"{sym} 没有定义"
    # 委托监听必须挂在 #cbHistory 上，且用的是 cbHistoryClick
    assert re.search(r'\$\("cbHistory"\)\.addEventListener\(\s*"click"\s*,\s*cbHistoryClick', BOARD), (
        "#cbHistory 的点击委托没有绑定到 cbHistoryClick"
    )
    # cbHistoryClick 必须真的调到两个跳转函数（否则按钮点了没反应）
    assert "cbViewFrame(" in BOARD and "cbRevertToFrame(" in BOARD


def test_history_reply_bubble_button_and_fork_bar():
    """点6/7（2026-08-03）：非当前帧的系统回复＝「查看历史回复」入口，点击只展示那一帧
    历史结果、不截断；查看历史位置时输入条变形成 #cbForkBar 三键——
    回到最新（cbToLatest 回栈顶）/ 从这里建立分支（cbBranchFromHere：**新开浏览器标签页**，
    ?fork=<convId>:<N> 落点由 browse.js 重建前 N 轮 + cbAdoptAsBranch 换新 convId）/
    回退至此（cbRevertHere 二段确认 → cbRevertToFrame：剪掉之后，不可撤销）。
    当前帧没有任何特殊标识（is-here 状态气泡/描环、hover 回退按钮、撤销/重做按钮全退役）。
    点1（2026-08-03 二轮）：两种「查看历史回复」（泡内 footer / 独立行）统一成**同一颗
    低调文本链接** `.cbh-view-link`——muted 小字、无箭头，气泡按钮形态退役。"""
    assert "查看历史回复" in BOARD, "非当前帧的入口文案缺席"
    assert "cbh-sys-bubble cbh-hist" not in BOARD, "独立气泡按钮形态已退役（统一为 .cbh-view-link 文本链接）"
    assert 'class="cbh-view-link"' in BOARD and "data-cbh-view" in BOARD
    assert ".cbh-hist" not in CSS, "退役气泡按钮的样式也要清掉"
    assert "data-cbh-revert" not in BOARD and "cbh-revert" not in BOARD, "hover 的「回退至此」应已退役"
    assert "cbh-revert" not in CSS, "退役按钮的样式也要清掉"
    # 点7：当前帧无特殊标识——状态气泡文案、is-here 描环双端都不许在
    assert "结果区正显示这句的结果" not in BOARD, "当前帧状态气泡应已退役（点7：取消特殊标识）"
    assert "is-here" not in BOARD and "is-here" not in CSS
    for token in ('id="cbForkBar"', 'id="cbTopBtn"', 'id="cbBranchBtn"', 'id="cbRevertBtn"'):
        assert token in HTML, f"index.html 缺少 {token}"
    for sym in ("cbComposerSync", "cbBranchFromHere", "cbRevertHere", "cbToLatest", "cbAdoptAsBranch"):
        assert re.search(r"\bfunction\s+" + sym + r"\b", BOARD) or re.search(r"export function\s+" + sym + r"\b", BOARD), f"{sym} 没有定义"
    assert "cbRevertToFrame(cur.id)" in BOARD, "回退键必须复用 cbRevertToFrame 的截断语义"
    assert "armed" in BOARD, "不可撤销的回退必须二段确认（armed 模式）"
    # 分支＝新开浏览器标签页（本标签页不变），落点 ?fork= 由 browse.js 接住；
    # 新标签页一律走 openPopup 单通道（被浏览器拦截时统一 toast 提醒），不许再裸调 window.open
    assert "openPopup(" in BOARD and "?fork=" in BOARD, "分支必须新开浏览器标签页（?fork=）"
    assert "window.open(" not in BOARD, "board.js 不许绕过 openPopup 裸调 window.open"
    assert 'params.get("fork")' in BROWSE and "cbAdoptAsBranch" in BROWSE, "新标签页的 ?fork= 落点没接"
    # 输入条变形的唯一同步口：cbRenderHistory 每次重画都调 cbComposerSync（游标不在栈顶＝三键）
    assert "cbComposerSync();" in BOARD


def test_board_plan_endpoint_is_declared_once_in_core():
    assert "boardPlan:" in CORE, "端点地址应集中声明在 core.js 的 API 常量里"
    assert BOARD.count('"/api/board/plan"') == 0, "board.js 里不该再手写一遍端点地址"
    assert "API.boardPlan" in BOARD


def test_user_visible_strings_in_board_carry_no_markdown_emphasis():
    """前端按纯文本转义呈现，写了星号就会原样显示成两个星号。"""
    code = _strip_comments(BOARD) + "\n" + _strip_comments(BOARD_CORE)
    # 限定在单行内配对，避免跨行把两段代码当成一个字符串
    for text in re.findall(r'"([^"\\\n]{4,})"', code):
        if any("一" <= ch <= "鿿" for ch in text):
            assert "**" not in text, f"面向用户的中文里有 markdown 强调：{text}"
            assert "`" not in text, f"面向用户的中文里有反引号：{text}"


def test_action_route_opens_pack_on_current_results_without_researching():
    """对话记录里说「打包前20条」曾被当成一句新检索送回主搜索框：这句话被词表当填充词剥光后
    退化成空查询，空查询命中全库，于是打包的是全库前 10 条、与用户看到的结果毫不相干
    （「跑起来了但全错」）。action 档必须在**当前结果**上打开任务包、不覆盖 queryInput、不重搜。

    三门都不执行 JS，改回老写法（先 input.value=text 再 runRecommend）不会有任何红灯，
    所以这里用位置断言把「action 必须先于查询覆盖处理并返回」钉死。"""
    body = re.search(r"function cbRouteAsFirstBox\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "找不到 cbRouteAsFirstBox 的函数体"
    code = body.group(1)
    assert 'route === "action"' in code, "cbRouteAsFirstBox 里必须单独处理 action 档"
    assert "previewTaskPack(" in code, "action 档必须在当前结果上打开任务包（previewTaskPack）"
    i_pack = code.index("previewTaskPack(")
    i_overwrite = code.index("input.value = text")
    assert i_pack < i_overwrite, "action 档必须在覆盖 queryInput 之前处理——否则又变回「打包全库」"
    assert "return true;" in code[i_pack:i_overwrite], "action 档处理完必须 return，不能继续走到重搜"
    # 「前20条」按条数开档：两端都钉住。
    # 2026-07-25 起由 tpCountFromUtterance 取代 tpLimitFromUtterance——后者取整句第一个数字且只认
    # 10/20/50 三档，「2020年后…打包前20条」会咬中「202」落回默认 10、「前5条」也静默变 10。
    task_pack_js = _read("act/task_pack.js")
    assert re.search(r"\bfunction\s+tpCountFromUtterance\b", task_pack_js), "tpCountFromUtterance 未定义"
    assert "tpCountFromUtterance(" in BOARD, "board.js 未调用 tpCountFromUtterance"
    assert not re.search(r"\bfunction\s+tpLimitFromUtterance\b", task_pack_js), (
        "tpLimitFromUtterance 已被取代，不该再定义——留着会有人接着用那套三档语义"
    )


def test_msg_feedback_action_bar_wired():
    """msgfb（2026-08-28）：每条系统回复气泡外下侧常驻 赞/倒赞/评论/分支 操作条。
    双端断言：渲染在 cbRenderHistory 的 sys 分支、事件在 cbHistoryClick 委托、
    三态纯逻辑在 board_core、埋点经 USAGE_KINDS.msgfb、评论走加密 feedback 通道
    （只许动态相对 import，不进静态图）、分支复用 ?fork= 新开标签页。"""
    # 渲染：sys 分支挂操作条，四键齐全
    assert "_cbMsgActBarHtml(e)" in BOARD, "sys 气泡下必须渲染操作条"
    for attr in ("data-cbh-up", "data-cbh-down", "data-cbh-comment", "data-cbh-fork"):
        assert attr in BOARD, f"操作条缺 {attr}"
    for attr in ("data-cbh-cmt-send", "data-cbh-cmt-cancel"):
        assert attr in BOARD, f"评论编辑器缺 {attr}"
    # 事件：委托处理四键 + 编辑器两键
    for sym in ("_cbMsgFbToggle", "_cbMsgCommentToggle", "_cbMsgCommentSend", "_cbMsgFork"):
        assert re.search(r"\bfunction\s+" + sym + r"\b", BOARD), f"{sym} 没有定义"
        assert sym + "(" in BOARD, f"{sym} 没有被调用"
    # 三态纯逻辑在 board_core（node 有真行为规格），board.js 必须经 import 取
    assert re.search(r"export function cbMsgFbNext\b", BOARD_CORE), "cbMsgFbNext 必须在 board_core"
    assert re.search(r"export function cbMsgCommentText\b", BOARD_CORE), "cbMsgCommentText 必须在 board_core"
    assert re.search(r"export function cbMsgForkable\b", BOARD_CORE), "cbMsgForkable 必须在 board_core"
    assert "cbMsgFbNext(" in BOARD and "cbMsgForkable(" in BOARD and "cbMsgCommentText(" in BOARD
    # 消息 id：cbLogPush 发 id、cbLogForHistory 落盘（i/f 字段）、恢复路径带回
    assert "id: _cbMsgIdNext()" in BOARD, "每条消息必须发唯一 id（反馈关联锚）"
    assert 'i: e.id || ""' in BOARD, "消息 id 必须随历史落盘"
    assert "_cbMsgIdSeen(" in BOARD, "恢复路径必须避让 id 碰撞"
    # 埋点：msgfb 事件经 usageLog，payload 无文本（conv/mid/v）
    assert "USAGE_KINDS.msgfb" in BOARD, "赞/倒赞必须打 msgfb 事件"
    usage_core = _read("core/usage_core.js")
    assert 'msgfb: "msgfb"' in usage_core, "USAGE_KINDS 必须登记 msgfb"
    # 评论：加密意见通道只许动态相对 import（不进静态图——import 图门只盯 # 静态边）
    assert 'import("../core/feedback_core.js")' in BOARD, "feedback_core 必须动态相对 import"
    assert 'import("../core/usage_upload.js")' in BOARD, "usage_upload 必须动态相对 import"
    assert 'sendFeedback(' in BOARD and "feedbackEnqueue(" in BOARD, "评论必须复用加密 feedback 管道"
    assert not re.search(r'import\s*\{[^}]*\}\s*from\s*"#[^"]*feedback[^"]*"', BOARD), (
        "feedback 模块不许经 # 静态 import 进 board.js（会牵动静态图）")
    # 分支：与输入条变形三键同一落点格式（?fork=<convId>:<N>，新开标签页）
    fork_body = re.search(r"function _cbMsgFork\([^)]*\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert fork_body and "?fork=" in fork_body.group(1) and "openPopup(" in fork_body.group(1)
    # 样式：操作条与评论编辑器的类都在 CSS 里（漏了就是一排裸按钮）
    for cls in (".cbh-actbar", ".cbh-act", ".cbh-cmt-ta", ".cbh-cmt-send"):
        assert cls in CSS, f"app.css 缺 {cls}"


def test_cb_preview_visible_host_follows_chat_log():
    """#cbPreview（ask.relax 选择卡 / cbShowMessage 追问卡共用）的可见宿主跟随（2026-09-15 修复）。

    根因：它的静态家在条件板 #cbBody 里，而条件板只在「有结果落地」时才被摘 hidden——
    环内 ask.relax 暂停回合恰恰不落地结果（chat-in-main），卡渲进隐藏宿主 0×0 不可见，
    用户看到「选完我会按你的选择自动重跑检索并继续」却无东西可点（yolo1 e2e 五连「卡未现」
    与此同因；探针三连「成功」是 query_selector 只验存在的假阳性）。
    三门不执行 JS，这里双端钉死：placeCbPreview 的定义、三个调用点（两个渲染口 +
    placeChatLog 布局链）、以及搬家后不失效的点击委托（绑在 #cbPreview 自身）。
    """
    assert re.search(r"\bfunction\s+placeCbPreview\b", BOARD), "placeCbPreview 没有定义"
    body = re.search(r"function placeCbPreview\(\)\s*\{(.*?)\n\}", BOARD, re.S)
    assert body, "placeCbPreview 函数体取不到"
    inner = body.group(1)
    assert "cbChatInMain()" in inner, "搬离判据必须是 chat-in-main（无结果有对话）"
    assert "log.nextSibling" in inner, "chat-in-main 时必须停到 #cbHistory 之后（输入框正上方）"
    assert '"cbBody"' in inner, "非 chat-in-main 时必须回静态家 #cbBody"
    # 两个渲染口落定后各调一次 + 布局链收口（placeChatLog 末尾）
    assert re.search(r"function askRelaxShow\([\s\S]*?placeCbPreview\(\);", BOARD), \
        "askRelaxShow 渲染后必须调 placeCbPreview"
    assert re.search(r"function cbShowMessage\([\s\S]*?placeCbPreview\(\);", BOARD), \
        "cbShowMessage 渲染后必须调 placeCbPreview"
    assert re.search(r'log\.classList\.toggle\("cbh-main"[^)]*\);\s*\n\s*placeCbPreview\(\);', BOARD), \
        "placeChatLog 链尾必须调 placeCbPreview（搬家同链收口）"
    # 点击委托：卡会被搬出 #condBoard，委托必须绑在卡自身；命中后止泡防 #condBoard 二次触发
    assert re.search(r'\$\("cbPreview"\)\.addEventListener\(\s*"click"\s*,\s*function', BOARD), (
        "#cbPreview 必须自带点击委托（搬出 #condBoard 后原委托够不到）")
    assert "event.stopPropagation();" in BOARD, "卡自身委托命中后必须止泡，防 #condBoard 重复触发"
