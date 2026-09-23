# -*- coding: utf-8 -*-
"""benchmark 采集反馈的隐私/诚实性契约门。

与 tests/test_usage_telemetry_contract.py 同一道地基的延伸——采集版记的是**全量**
交互（完整响应、执行轨迹、评分），一旦泄漏或越界，比普通埋点更伤：

1. **零出网原语**：出网只经 `usage_upload.js`（脱敏自动上传）；
   本层代码里出现 fetch/XHR/… 即红，手动「导出反馈包」保留作兜底。
2. **后端零参与**：后端整树不得出现采集符号（服务端零改动是这个功能的设计前提）。
3. **脱敏是结构性的**：api_key 落盘前必经 benchfbStripRequest 整键删除——静态钉住
   调用链，行为由 node 规格逐条断言。
4. **与使用反馈同一开关**：采集闸 = usageEnabled（恒默认开），
   不开独立后门。
5. **展示收敛（设计约定）**：none/error 轮不再渲染评分卡（记录照攒，纯埋点）；
   记录补 `ms` 轮次耗时（设计约定）。
6. **双端登记**：importmap 两页同键 + package.json imports 同键（parity 门管集合相等，
   这里钉「这两个键真实存在」）。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"
CORE = JS_DIR / "core" / "benchfb_core.js"
LOG = JS_DIR / "core" / "benchfb.js"
INDEX = ROOT / "web" / "static" / "index.html"
DATASET = ROOT / "web" / "static" / "dataset.html"
PKG = ROOT / "package.json"
SPEC = ROOT / "tests" / "js" / "benchfb_core_spec.mjs"
BACKEND = ROOT / "src" / "dataset_recommender"


def _resolve_node() -> str | None:
    override = os.environ.get("BIODATA_NODE")
    if override and (shutil.which(override) or Path(override).exists()):
        return override
    for cand in ("node", "node.exe"):
        found = shutil.which(cand)
        if found:
            return found
    return None


def _strip_js_comments(text: str) -> str:
    """断言只看真代码（注释里的「不许 fetch」字样不该两头误判；同 usage 契约做法）。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ---------------------------------------------------------------- 无自动传输

def test_the_benchfb_layer_cannot_send_anything_anywhere() -> None:
    """采集代码里不许存在任何出网原语——「不会把你的数据传出去」是结构性事实。"""
    forbidden = ("fetch(", "XMLHttpRequest", "sendBeacon", "WebSocket", "EventSource", "navigator.connection")
    for path in (CORE, LOG):
        code = _strip_js_comments(path.read_text(encoding="utf-8"))
        for token in forbidden:
            assert token not in code, f"{path.name} 里出现了出网原语 {token!r} —— 采集层绝不许自动发送"


def test_the_backend_knows_nothing_about_benchfb() -> None:
    """后端全树不得出现采集符号：采集链路服务端零改动、零参与。"""
    hits = []
    for py in BACKEND.rglob("*.py"):
        text = py.read_text(encoding="utf-8", errors="ignore")
        if "benchfb" in text or "biodata_benchfb" in text:
            hits.append(py.relative_to(ROOT).as_posix())
    assert not hits, f"采集符号泄漏进后端：{hits}（采集必须是纯前端的）"


# ---------------------------------------------------------------- 脱敏

def test_every_search_capture_passes_through_the_stripper() -> None:
    """请求体落盘前必经 benchfbStripRequest（api_key 整键删、base_url 留主机）。

    行为断言在 node 规格（Key 影子搜不到、原对象不被改）；这里钉调用链本身——
    有人「优化」成直接存 reqBody 时当场红。"""
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    m = re.search(r"function benchfbTurnSearch[^{]*\{(?P<body>.*)", code, flags=re.S)
    assert m, "benchfbTurnSearch 不存在"
    assert "benchfbStripRequest(" in m.group("body"), (
        "benchfbTurnSearch 没有过 benchfbStripRequest —— api_key 会原样落盘"
    )
    core = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    fn = re.search(r"function benchfbStripRequest[^{]*\{(?P<body>.*?)return out;", core, flags=re.S)
    assert fn, "benchfbStripRequest 不存在"
    body = fn.group("body")
    assert 'k === "api_key"' in body and "benchfbEndpointHost(" in body, (
        "benchfbStripRequest 必须整键删 api_key、base_url 只留主机"
    )


def test_collection_gate_is_the_usage_switch() -> None:
    """采集不开独立后门：benchfbOn 就是 usageEnabled（benchfb 采集构建默认开、标准构建默认关，见 usage 契约门）。"""
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert re.search(r"function benchfbOn\(\)\s*\{\s*return usageEnabled\(\);\s*\}", code), (
        "benchfbOn 必须恒等于 usageEnabled() —— 采集与使用反馈同一个开关，不得另设默认开的路"
    )
    # 异步轮次固定开工 scope：begin/search 按该 scope 的开关，action 仍读当前 usage 开关。
    for fn_name in ("benchfbTurnBegin", "benchfbTurnSearch"):
        m = re.search(r"function " + fn_name + r"[^{]*\{(?P<body>.*?)\n\}", code, flags=re.S)
        assert m and "usageEnabledForScope(scope)" in m.group("body"), f"{fn_name} 必须按捕获 scope 短路"
    action = re.search(r"function benchfbTurnAction[^{]*\{\s*([^\n]+)", code)
    assert action and "benchfbOn()" in action.group(1), "benchfbTurnAction 第一道必须是开关短路"


# ---------------------------------------------------------------- 登记与界面

def test_none_and_error_turns_do_not_render_rating_cards() -> None:
    """none/error 轮**不再主动渲染完整评分卡**（记录照攒，
    纯埋点）；该判定连同「会话降频闸」收口进 `_collapsedRate`——none/error 轮、
    或被降频闸（主动卡每会话 ≤2 / 连续忽略 2 次）拦下的 search/tool 轮，默认都只给折叠「评价」按钮。

    结构性证据：`_collapsedRate` 必须含 kind 过滤与降频闸调用；`_renderMount`/`_renderHeroMount`
    必须经它分流；hero 槽位仍只接受 search/tool 轮。_lastSearchRecId 标注逻辑
    （_closeTurn 内 `kind === "search"` 才更新）不受影响。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    collapsed = re.search(r"function _collapsedRate[^{]*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert collapsed, "_collapsedRate 不存在"
    body = collapsed.group("body")
    assert 'rec.kind === "none"' in body and 'rec.kind === "error"' in body, (
        "折叠判定必须对 none/error 轮做 kind 过滤（这两类轮次不主动渲染完整评分卡）"
    )
    assert "benchfbProactiveAllowed(" in body, (
        "折叠判定必须接入会话降频闸 benchfbProactiveAllowed（主动卡 ≤2 / 连续忽略 2 次）"
    )
    mount = re.search(r"function _renderMount[^{]*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert mount and "_collapsedRate(rec)" in mount.group("body"), "对话轮挂载渲染必须经 _collapsedRate 分流"
    assert "_noteShown(rec)" in mount.group("body"), "主动完整卡上屏必须经 _noteShown 计会话配额"
    hero = re.search(r"function _renderHeroMount[^{]*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert hero, "_renderHeroMount 不存在"
    assert 'heroRec.kind !== "search"' in hero.group("body"), (
        "hero 槽位必须只接受 search/tool 轮（none/error 不出卡，防槽位被挂成空卡）"
    )
    assert "_collapsedRate(heroRec)" in hero.group("body"), "hero 槽位同样受会话降频闸约束"


def test_none_error_turns_expose_a_rate_toggle_button() -> None:
    """none/error 轮（纯埋点、不出完整卡）在系统回复气泡下方给一颗低视觉权重的
    小字「评价」按钮，点击原位展开完整评分卡（为完成度三选+原因 chips+评语；「标出有用」
    仅检索轮，复用 _fillCard canMark 判据），再点「收起」回到按钮。 起被会话
    降频闸拦下的 search/tool 轮复用同一折叠态。

    - 折叠态：_renderMount 的折叠分支渲染 `data-bf-rate-toggle` 按钮，而非直接留空；
    - 展开/收起状态存 `_expandedRates` 内存集合——重画后 _renderMount 照读（board cbRenderHistory
      尾部 benchfbAfterRender 重挂模式不变），状态不错乱；
    - 折叠档的「收起」＝删展开态回到按钮（不整卡消失）；search/tool 主动卡保持既有收起语义
      （整卡收起，未评分即收起经 benchfbNoteDismissed 计入连续忽略）；
    - hero 侧 none/error 轮同样有一颗系统回复泡（回音/检索失败）：_closeTurn 的绑定条件除 chat
      外还含 `kind === "none"` / `kind === "error"`——评价按钮要落在每一颗回复气泡下方。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert "data-bf-rate-toggle" in code, "none/error 轮必须渲染「评价」按钮（data-bf-rate-toggle）"
    assert "bf-rate-toggle" in code, "评价按钮样式类 bf-rate-toggle 必须存在"
    assert "_expandedRates" in code, "展开态集合 _expandedRates 必须存在"
    assert "_expandedRates.has(recId)" in code, "重画后 _renderMount 必须读展开态（状态不错乱）"
    assert "_expandedRates.add(recId)" in code, "点击「评价」必须写入展开态"
    assert "_expandedRates.delete(recId)" in code, "「收起」必须清除展开态（回到按钮）"
    close = re.search(r"function _closeTurn\(kind\)\s*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert close, "_closeTurn 不存在"
    assert '"none"' in close.group("body") and '"error"' in close.group("body"), (
        "hero 侧 none/error 轮也必须绑定到系统回复泡（评价按钮要落在每颗回复气泡下方）"
    )


def test_turn_records_carry_duration_ms() -> None:
    """设计约定：benchfb 轮次记录补 `ms`（turn begin→close 的时长）。"""
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    close = re.search(r"function _closeTurn\(kind\)\s*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert close, "_closeTurn 不存在"
    assert "ms:" in close.group("body") and "end: now" in close.group("body"), (
        "收尾时必须同时写 end 与 ms（轮次耗时）"
    )


def test_rating_card_is_tristate_not_stars_and_records_carry_tid() -> None:
    """ 评分结构改版 + 轮次 id 接线：

    - 界面：完成度三选（data-bf-comp）+ 可选原因 chips（data-bf-reason）取代星级——
      benchfb.js 不得再出现 data-bf-star，app.css 不得再留 .bf-star 死样式；
    - 记录形状：benchfbRate 走 BENCHFB_COMPLETIONS/BENCHFB_REASONS 白名单（行为断言在 node 规格）；
    - 轮次记录补 tid：benchfbTurnBegin 用 usage_core 的 usageActiveTurnId() 写入（与 usage
      事件同一轮次 id，ubSubmit 先 usageBeginTurn 再 benchfbTurnBegin）。
    """
    code = _strip_js_comments(LOG.read_text(encoding="utf-8"))
    assert "data-bf-star" not in code and "bf-star" not in code, "星级 UI 已退役（data-bf-star 不得残留）"
    assert "data-bf-comp" in code and "data-bf-reason" in code, "完成度三选与原因 chips 必须在评分卡上"
    begin = re.search(r"function benchfbTurnBegin[^{]*\{(?P<body>.*?)\n\}", code, flags=re.S)
    assert begin and "usageActiveTurnId()" in begin.group("body") and "tid:" in begin.group("body"), (
        "benchfbTurnBegin 必须用 usageActiveTurnId() 写入 record.tid"
    )
    core = _strip_js_comments(CORE.read_text(encoding="utf-8"))
    assert "BENCHFB_COMPLETIONS" in core and "BENCHFB_REASONS" in core, "三选与原因白名单常量必须在纯核"
    css = (ROOT / "web" / "static" / "css" / "app.css").read_text(encoding="utf-8")
    assert ".bf-star" not in css, "星级样式必须随 UI 一并退役"


def test_benchfb_keys_registered_everywhere() -> None:
    """importmap 两页同键 + package.json imports 同键（parity 门管集合相等，这里钉存在）。"""
    for path in (INDEX, DATASET):
        text = path.read_text(encoding="utf-8")
        for key in ('"#benchfb_core"', '"#benchfb"'):
            assert key in text, f"{path.name} 的 importmap 缺 {key}"
    pkg = PKG.read_text(encoding="utf-8")
    for key in ('"#benchfb_core"', '"#benchfb"'):
        assert key in pkg, f"package.json imports 缺 {key}（node 侧第二真源）"


def test_export_ui_is_present_and_off_by_default() -> None:
    html = INDEX.read_text(encoding="utf-8")
    for node_id in ("benchfbExportBtn", "benchfbModal", "benchfbDownloadBtn", "benchfbClearBtn", "bfMarkBar"):
        assert f'id="{node_id}"' in html, f"index.html 缺 #{node_id}"
    assert re.search(r'<button class="btn" id="benchfbExportBtn" type="button" disabled>', html), (
        "导出按钮 HTML 默认态必须是 disabled（没记录时不可点）"
    )


# ---------------------------------------------------------------- node 行为规格

def test_benchfb_core_behavior_spec() -> None:
    """纯核真行为规格：脱敏/评分/裁剪/导出逐条断言（node 跑，非零退出即红）。"""
    node = _resolve_node()
    if not node:
        import pytest
        pytest.skip("本机没有 node")
    proc = subprocess.run([node, str(SPEC)], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, f"benchfb_core 行为规格失败：\n{proc.stdout}\n{proc.stderr}"
