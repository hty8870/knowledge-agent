# -*- coding: utf-8 -*-
"""数据集介绍详情页（/）静态契约钉。

「查看介绍」从模态弹窗改为**独立浏览器标签页** /dataset（dataset.html + dataset_page.js），六子标签页，
元数据兼容卡对齐真正的检索结果卡；「数据集对比」= **全屏并排**（两个完整 /dataset iframe，embed 态）+
可关掉一侧保留另一侧。三门（node --check / web_smoke / pytest 后端）都不执行前端 JS，
「子标签顺序被改」「兼容卡退回 compact」「引文串在 JS 里拼」「对比空态没了诚实提示」「对比退回窄栏 / 丢了关闭单侧」
这类回归只能静态钉。（取代已退役的 test_intro_tabs_static.py。）
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_backend_serves_dataset_route() -> None:
    """webapp.py 提供 GET/HEAD /dataset → dataset.html（只读静态，不接受写入）。"""
    webapp = _read("src/dataset_recommender/app/webapp.py")
    assert '@app.api_route("/dataset"' in webapp
    assert 'FileResponse(STATIC_DIR / "dataset.html", headers={"Cache-Control": "no-cache"})' in webapp


def test_dataset_html_skeleton_and_script_order() -> None:
    """dataset.html 骨架：页头 + 六子标签面板挂点 + 文件弹窗；脚本序 core→cards→fav_folders→dataset_page。"""
    html = _read("web/static/dataset.html")
    for token in ('id="dsTitle"', 'id="dsFav"', 'id="dsBadges"', 'id="dsSub"', 'id="dsActions"',
                  'id="dsTabs"', 'id="dsPanel-intro"', 'id="dsPanel-files"', 'id="dsPanel-compat"',
                  'id="dsPanel-fair"', 'id="dsPanel-cite"', 'id="dsPanel-compare"',
                  'id="filesModal"', 'id="filesModalBody"'):
        assert token in html, f"dataset.html 缺骨架：{token}"
    scripts = [s.split("/")[-1] for s in re.findall(
        r'<script(?:\s+type="module")?\s+src="/static/js/([A-Za-z0-9_/-]+)\.js(?:\?[^"]*)?"', html)]
    assert scripts and scripts[0] == "core", "core.js 必须最先加载"
    for m in ("cards", "fav_folders", "dataset_page"):
        assert m in scripts, f"dataset.html 未加载 {m}.js"
    assert scripts.index("core") < scripts.index("cards") < scripts.index("dataset_page")
    # 独立页不加载首页大 bundle（search/browse/facets 等），保持轻量
    for heavy in ("search", "browse", "facets", "interactions", "boot"):
        assert heavy not in scripts, f"数据集详情页不应加载首页模块 {heavy}.js"


def test_subtabs_order_and_labels() -> None:
    """子标签顺序固定：介绍 / 全部文件 / 元数据兼容 / 投稿可用性与 FAIR / 导出引文 / 数据集对比。"""
    page = _read("web/static/js/search/dataset_page.js")
    m = re.search(r"function dsTabDefs\(it\)\s*\{(.*?)\n\}", page, re.DOTALL)
    assert m, "dataset_page.js 缺 dsTabDefs"
    body = m.group(1)
    order = ["介绍", "全部文件", "元数据兼容的数据集", "投稿可用性与 FAIR 自检", "导出引文 RIS/BibTeX", "数据集对比"]
    pos = []
    for token in order:
        i = body.find(token)
        assert i >= 0, f"子标签缺文案：{token}"
        pos.append(i)
    assert pos == sorted(pos), f"子标签顺序不符规格：{list(zip(order, pos))}"


def test_intro_link_opens_new_tab_with_handoff() -> None:
    """卡片「数据集详情」= <a target=_blank> 指向 /dataset，点击时把完整记录写进 localStorage 供新标签页取回。"""
    cards = _read("web/static/js/search/cards.js")
    # 同源相对路径直插（非整值插值），过 tests/test_frontend_url_safety.py 的 isHttp sink 门
    assert 'href="/dataset?uid=' in cards and 'target="_blank" rel="noopener"' in cards
    assert "数据集详情" in cards
    assert 'localStorage.setItem("biodata_dataset_view_v1"' in cards, "介绍链接点击未写完整记录 handoff"
    # 详情页按 uid/url 取回 handoff，取不到则用 URL 参数搭最小记录（不臆造）
    page = _read("web/static/js/search/dataset_page.js")
    assert '"biodata_dataset_view_v1"' in page and "dsResolveItem" in page


def test_compat_cards_are_full_result_cards() -> None:
    """元数据兼容卡=全检索卡：renderCompatCards 用缺省 buildCard(c)（非 compact），容器用标准 .grid。"""
    cards = _read("web/static/js/search/cards.js")
    m = re.search(r"function renderCompatCards\(wrap, data\)\s*\{(.*?)\n\}", cards, re.DOTALL)
    assert m, "cards.js 缺 renderCompatCards"
    body = m.group(1)
    assert "buildCard(c)" in body, "兼容卡应为全检索卡 buildCard(c)"
    assert "compact: true" not in body, "兼容卡不应再用 compact 精简模式"
    assert "compat-grid grid" in body, "兼容卡容器应叠加标准 .grid 布局（对齐检索结果网格）"


def test_compare_pool_written_and_read() -> None:
    """对比池：results.js 每次结果写 localStorage，详情页对比子页读它（跨同源标签共享）。"""
    results = _read("web/static/js/search/results.js")
    assert "writeComparePool" in results and 'localStorage.setItem("biodata_compare_pool_v1"' in results
    page = _read("web/static/js/search/dataset_page.js")
    assert '"biodata_compare_pool_v1"' in page and "dsReadComparePool" in page


def test_honesty_redlines_kept() -> None:
    """诚实红线：compat「必要非充分」caveat 逐字、引文串不在 JS 拼、对比空态如实提示、FAIR/引文渲染复用。"""
    cards = _read("web/static/js/search/cards.js")
    assert "escapeHtmlStrong(data.caveat" in cards, "兼容 caveat 不再逐字渲染"
    # 引文串绝不在 JS 拼：下载/复制的内容原样取自后端响应 data.ris / data.bibtex（renderCiteInto 不构造引用串，
    # 只把后端 to_ris/to_bibtex 的结果落盘/复制；正文里出现的 "TY - DATA/@misc" 是给用户看的类型说明、非拼串）。
    assert "data.ris" in cards and "data.bibtex" in cards, "引文应原样用后端 data.ris/data.bibtex，不在 JS 构造"
    assert "cite.ris" in cards and "cite.bibtex" in cards
    assert "loadCiteInto" in cards and "loadFairInto" in cards and "loadCompatInto" in cards
    page = _read("web/static/js/search/dataset_page.js")
    assert "先在检索页做一次检索" in page, "对比空态未如实提示需先检索"


def test_compare_is_fullscreen_split_with_close() -> None:
    """：数据集对比 = 全屏并排（两个完整 /dataset iframe，embed 态）+ 可关掉一侧保留另一侧。

    用户澄清「分屏」是**全屏意义**的分屏、右侧是被对比数据集的**完整**介绍页——非 子标签内的两个 bare 正文窄栏。
    这些行为三门都执行不到（不跑 JS），只能静态钉：启动器 → 浮层、split handoff、embed 防嵌套、关闭单侧、池存完整记录。
    """
    page = _read("web/static/js/search/dataset_page.js")
    # 对比子标签是启动器 → 全屏浮层（两个 embed 态完整 /dataset iframe）
    assert "dsOpenSplit" in page, "缺全屏并排启动函数 dsOpenSplit"
    assert '"biodata_split_handoff_v1"' in page and "dsReadSplitHandoff" in page, "缺 split handoff（按 uid 把完整记录交给 iframe）"
    assert "embed=1" in page and "ds-split-pane" in page, "对比两侧应是 embed 态的完整 /dataset iframe 分屏"
    # 关掉一侧 = 直接退出对比、落在保留者（keepOnly），不再停在单侧撑满态（solo）
    assert "ds-split-x" in page and "keepOnly" in page, "缺关闭单侧 → 退出对比落在保留者（keepOnly）"
    assert '"solo"' not in page, "全屏对比关闭改为直接退出，不应再有 solo 撑满逻辑"
    assert "dsSplitExit" in page and "退出对比" in page, "缺退出对比（回当前数据集）"
    # 「返回 BioData Agent」在对比态只出现一次——顶栏
    assert "ds-split-home" in page, "缺顶栏「返回 BioData Agent」一次入口"
    # embed 态：不在对比里再开对比（防嵌套分屏）
    assert "DS_EMBED" in page and '!== "compare"' in page, "embed 态未丢弃「数据集对比」子标签（防嵌套分屏）"
    # 对比池改存完整记录（否则对比侧页头退回「暂无下载」降级）
    results = _read("web/static/js/search/results.js")
    m = re.search(r"function writeComparePool\(items\)\s*\{(.*?)\n\}", results, re.DOTALL)
    assert m, "results.js 缺 writeComparePool"
    body = m.group(1)
    assert "normalizeItem(it)" in body, "对比池应存完整归一化记录"
    assert "dataset_uid: n.dataset_uid" not in body, "对比池不应再只存轻量五字段（否则对比侧页头退回降级）"
    # 浮层 / 嵌入态 / 顶栏返回样式在 app.css
    css = _read("web/static/css/app.css")
    assert ".ds-split {" in css and "body.ds-split-open" in css and ".ds-embedded .ds-topbar" in css, "缺全屏并排浮层样式"
    assert ".ds-split-home" in css, "缺顶栏「返回 BioData Agent」样式"
    # 详情页 body 不吃侧栏留白——用排除法把留白规则本身排除详情页（确定性匹配，绕开本环境层叠反常），
    # 否则 /dataset 内容整体右移一个侧栏宽（既存 bug）。
    assert "body:not(.dataset-page-body)" in css, "侧栏留白规则未排除详情页（body:not(.dataset-page-body)），/dataset 内容会右移一个侧栏宽"


def test_intro_modal_chrome_removed() -> None:
    """介绍模态弹窗骨架/脚本/几何样式已退役：index.html 无 #introModal/intro_tabs.js，app.css 无 .intro-modal 几何。"""
    html = _read("web/static/index.html")
    assert 'id="introModal"' not in html and "intro_tabs.js" not in html
    assert not (ROOT / "web/static/js/intro_tabs.js").exists(), "intro_tabs.js 应已删除"
    css = _read("web/static/css/app.css")
    # 内容样式保留、模态几何 chrome 删除
    assert ".intro-summary" in css and ".compat-basis-line" in css and ".fair-box" in css
    assert ".intro-tabbar {" not in css and ".intro-pane {" not in css and ".intro-split-on" not in css
    assert ".ds-page" in css and ".ds-tab" in css, "数据集详情页样式缺失"
