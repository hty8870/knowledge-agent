# -*- coding: utf-8 -*-
"""前端视觉回归基线：Playwright 截图基线（--record）+ 带容差比对（--check）。

为什么要有这个脚本：界面改版时有发生「没打算动的地方悄悄变了样」的事故，
既有 smoke 只断言 DOM/类名，看不见长相。这里给关键页面/状态钉一套截图基线，
以后每次改动能自动发现视觉意外变化。

范式与项目截图工具一致：每状态一个干净 browser context、
reduced_motion 推到动画终态、SETTLE_MS 等收尾、先起服务再跑本脚本（脚本**不会**自己起服务）。
浏览器用系统 Edge（channel="msedge"）。

零三方依赖：比对用的 PNG 解码/编码只用标准库（zlib/struct）——项目 venv 没有
numpy/Pillow，为了不往共享 venv 里塞包，自己写了解码器。先比原始文件字节
（完全一致直接 PASS，record 后立刻 check 的常见情形零解码成本），不一致才解码逐像素 diff。

动态内容 mask：列表计数、相对时间戳、「已攒下 N 条」这类数字每次跑都可能变，
mask 不掉就是假阳性。mask 用 JS 把匹配元素 visibility:hidden（不用 Playwright 的
mask= 参数：那要求元素可见取 bounding box，对 hidden 元素直接报错）。
每条 mask 的理由写在 MASKS 里，随 manifest.json 落盘。

比对判据（都做成 CLI 参数）：逐像素差异率 ≤ --tolerance（默认 0.5%）且
差异区域外接框面积占比 ≤ --max-diff-area（默认 10%）。像素级判异阈值
--channel-threshold（默认 12/255）吃掉抗锯齿噪声。

用法（先起服务，例如 PORT=7975 python scripts/run_web.py）：
    python scripts/visual_regression.py --record --base-url http://127.0.0.1:7975
    python scripts/visual_regression.py --check  --base-url http://127.0.0.1:7975
    python scripts/visual_regression.py --check --only home settings --tolerance 0.01
退出码：0 全过 / 1 有差异或拍摄失败 / 2 服务没起来或环境缺件。
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_DIR = ROOT / "tests" / "web" / "visual_baseline"
DIFF_DIR = ROOT / "outputs" / "visual_regression_diff"   # check 失败时的差异图（不入库）

DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}     # iPhone 12/13/14 竖屏：抽屉断点（≤780px）内侧
SETTLE_MS = 700                            # 入场动画 / 结果渲染收尾
SEARCH_Q = "人类乳腺癌"                     # 确定性本地库检索：冻结基准库，结果集稳定


# ---------------------------------------------------------------- mask 清单（理由随 manifest 落盘）
#: 每条 mask：为什么这块区域**允许**变而不算视觉回归。
MASK_HOME = {
    "#memorySuggestions": "记忆建议条随 localStorage 记忆内容出现/消失，属用户数据非界面",
    "#heroGreeting": "hero 轮播第一条是时段问候（早/中/晚文案随拍摄时刻变，interactions.js renderHeroGreeting）；reduced-motion 下轮播不启动、只显第一条，mask 它后首屏完全确定",
    "#toast": "页面加载时的瞬态提示（如「检测到服务端已配好 AI 密钥…」），~3s 后淡出，拍摄时刻是否在场随机（与 MASK_SETTINGS 同款）",
}
MASK_SETTINGS = {
    "#toast": "页面加载时的瞬态提示（如「检测到服务端已配好 AI 密钥…」），~3s 后淡出，拍摄时刻是否在场随机（2026-08-19 bl1 实测 settings 109.9↔102.5 KiB 全因此行）",
}
MASK_HISTORY = {
    ".hist-meta": "行内「N 条 · 相对时间戳 · 共 M 轮」：fmtTime 相对时间随拍摄时刻变（browse.js:405-407）",
}
MASK_BROWSE = {
    "#timelineUnknown": "未标注日期数据集的统计小字，从「正在统计…」异步刷新成计数，时序不可控",
}

MASK_SIDEBAR = {
    #: 行动流卡（Agent 规划 / 分流共识）：内容随 LLM 路由决策文本与执行步骤变化，属时序数据非界面结构
    ".arx-run": "行动流/Agent 规划卡：文本随 LLM 路由决策与执行步骤变化（时序数据，非界面结构）",
    #: 对话板「正在更深一步思考…」进度泡：agent 处理进行中才出现，拍摄时刻是否在场取决于搜索是否恰好在 shot 前完成
    ".cbh-prog": "对话板思考进度泡「正在更深一步思考…」：agent 处理中才出现，时序数据",
}

# ---------------------------------------------------------------- 逐状态取景

def _search(page, query: str = SEARCH_Q) -> None:
    page.fill("#queryInput", query)
    page.press("#queryInput", "Enter")
    # 结果区显形即可（0 结果时也渲染空态卡）。
    # 120s：本机检索含本地重排/向量召回模型推理，单次 utterance 可达 ~50s（bl1 验证）；
    # 60s 的旧值在模型冷加载 + 并发堆积时会把正常搜索误判为超时。
    page.wait_for_function("() => { const w = document.getElementById('resultsWrap');"
                           " return w && w.style.display !== 'none'; }", timeout=120_000)
    page.wait_for_timeout(SETTLE_MS)


def _open_settings(page) -> None:
    page.click("#settingsBtn")
    page.wait_for_selector('#settings[aria-hidden="false"]', timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_home(page) -> None:
    # chips 是 HTML 静态直出（不依赖异步来源清单），attached 即说明首屏骨架就位；
    # 问候文案/副标题同帧渲染。保留 SETTLE_MS settle 等首屏动效与字体落定，避免拍出过渡中间帧。
    page.wait_for_selector(".chips .chip", state="attached", timeout=15_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_results(page) -> None:
    _search(page)


def setup_sidebar_facets(page) -> None:
    _search(page)
    page.wait_for_selector("#sideWork:not([hidden])", timeout=10_000)
    # 分面渲染完再拍。不能用默认的 visible：placeFacetBar 会把 hidden 的 #facetBar
    # 整块搬进 #sideFacetBody，wait_for_selector 盯着第一个匹配（hidden）干等到超时。
    page.wait_for_selector("#sideFacetBody *", state="attached", timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_sidebar_board(page) -> None:
    _search(page)
    page.wait_for_selector("#sideWork:not([hidden])", timeout=10_000)
    page.click("#swTabBoard")            # 侧栏切到「继续对话」
    page.wait_for_timeout(400)
    page.wait_for_timeout(SETTLE_MS)


def setup_history_pop(page) -> None:
    _search(page)                        # 先造一条确定性的历史记录（同一句检索）
    page.click('#histNav')               # 历史记录是独立浮窗 #histWin（不再走 #archiveWin）
    page.wait_for_selector("#histWin:not([hidden])", timeout=10_000)
    page.wait_for_selector("#histList .hist-row", timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_settings(page) -> None:
    _open_settings(page)
    # 抽屉默认停在顶部（账户/API 区）；任务要求覆盖「用户记忆 / 使用反馈」区块，
    # 把它们滚进视口（a5 起卡片无动态数字行，无 mask 需求）。
    page.evaluate("document.querySelector('.usage-setting').scrollIntoView({block: 'center'})")
    page.wait_for_timeout(SETTLE_MS)


def setup_browse(page) -> None:
    page.click('.nav-item[data-view="browse"]')
    page.wait_for_selector("#browseGrid .card", timeout=60_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_favorites(page) -> None:
    # 「我的收藏」视图已删，收藏收进「我的库」浮窗 #libWin 的收藏页签（浮窗非视图）。
    page.click('#libNav')
    page.wait_for_selector("#libWin:not([hidden])", timeout=10_000)
    page.click("#libTabFavs")
    page.wait_for_selector("#libPaneFavs:not([hidden])", timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_help(page) -> None:
    page.click('.nav-item[data-view="help"]')
    page.wait_for_selector('.view[data-view="help"].active', timeout=10_000)
    page.wait_for_timeout(SETTLE_MS)


def setup_mobile_home(page) -> None:
    setup_home(page)         # 同 desktop：chips 静态直出，等 attached 即可


def setup_mobile_drawer(page) -> None:
    # 移动端 boot 默认收起（initSidebar 契约）；点悬浮 logo 开抽屉
    page.click("#sideFab")
    page.wait_for_function("() => !document.body.classList.contains('side-closed')", timeout=10_000)
    page.wait_for_timeout(700)           # 抽屉滑入过渡（同 smoke_sidebar_states 的 700ms）
    page.wait_for_timeout(SETTLE_MS)


#: name -> (viewport, clip_selector_or_None, setup, masks, 前置操作说明)
SHOTS = {
    "home":            (DESKTOP, None,       setup_home,           MASK_HOME,    "首页初始态：hero、推荐 chips、数据来源/发表时间两下拉"),
    "results":         (DESKTOP, None,       setup_results,        {},           f"检索「{SEARCH_Q}」结果渲染完"),
    "sidebar-facets":  (DESKTOP, "#sideWork", setup_sidebar_facets, MASK_SIDEBAR, f"检索「{SEARCH_Q}」后侧栏细化筛选页"),
    "sidebar-board":   (DESKTOP, "#sideWork", setup_sidebar_board,  MASK_SIDEBAR, f"检索「{SEARCH_Q}」后侧栏继续对话页（含输入条）"),
    "history-pop":     (DESKTOP, "#histWin", setup_history_pop,    MASK_HISTORY, "先检索一次造历史，再开历史记录浮窗 #histWin"),
    "settings":        (DESKTOP, None,       setup_settings,       MASK_SETTINGS, "打开设置抽屉（含用户记忆 / 使用反馈区块）"),
    "browse":          (DESKTOP, None,       setup_browse,         MASK_BROWSE,  "切数据集浏览视图"),
    "favorites":       (DESKTOP, "#libWin",  setup_favorites,      {},           "开「我的库」浮窗切收藏页签（干净上下文 = 空态）"),
    "help":            (DESKTOP, None,       setup_help,           {},           "切帮助视图"),
    "mobile-home":     (MOBILE,  None,       setup_mobile_home,    MASK_HOME,    "移动视口首页初始态"),
    "mobile-drawer":   (MOBILE,  None,       setup_mobile_drawer,  MASK_HOME,    "移动视口点开侧栏抽屉"),
}


# ---------------------------------------------------------------- 浏览器侧

def _health(base_url: str) -> str:
    # 30s：本机检索路径含本地重排/向量召回模型推理，忙时 health 也可能排队（bl1 验证）
    with urllib.request.urlopen(base_url.rstrip("/") + "/api/health", timeout=30) as resp:
        return resp.read().decode("utf-8")


def _new_page(browser, base_url: str, viewport):
    """每状态一个干净上下文：不继承上一状态的 localStorage / 设置 / 结果。"""
    ctx = browser.new_context(
        viewport=viewport,
        locale="zh-CN",
        reduced_motion="reduce",   # 动画一律推到终态再拍，拍到一半 = 假图
    )
    page = ctx.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    # 首次进入的轻量导览会盖住整屏 —— 先标记成看过，再进页面。
    # consent v2（usage_upload.js，键 `biodata_consent_v2`，默认采集开）：首次检索前会弹
    # #consentModal 阻断，必须先标记已同意，否则先检索的 shot（results/sidebar-*/history-pop）
    # 卡在 120s `_search` 超时。每 shot 新开干净 context、默认未登录 → currentAccountScope()=""，
    # nsKeyFor(base,"") 返回裸键 `biodata_consent_v2`（core.js:41-42）；写 "1" 即视为同意
    # （usage_log.js:282-291 任意非空非 "0" 值 = 同意）。
    page.add_init_script(
        "try { localStorage.setItem('biodata_onboarding_v1', '1'); } catch (e) {}"
        "try { localStorage.setItem('biodata_consent_v2', '1'); } catch (e) {}")
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector("#queryInput", timeout=30_000)
    page.wait_for_timeout(SETTLE_MS)
    return ctx, page, errors


def _apply_masks(page, masks: dict) -> None:
    """JS 侧 visibility:hidden（不用 Playwright mask= 参数：它对 hidden 元素取不到 bounding box 直接报错）。"""
    for sel in masks:
        page.evaluate(
            "(sel) => document.querySelectorAll(sel).forEach("
            "el => { el.style.visibility = 'hidden'; })", sel)


def _shoot(page, clip: str | None, masks: dict) -> bytes:
    _apply_masks(page, masks)
    if clip:
        loc = page.locator(clip)
        assert loc.count(), f"{clip} 不在页面上，没法出图"
        return loc.first.screenshot()
    return page.screenshot()


# ---------------------------------------------------------------- PNG 解码 / diff（纯标准库）

def _decode_png(data: bytes) -> tuple[int, int, bytes, int]:
    """返回 (width, height, pixels, bpp)。只支持 8-bit、非隔行、色型 2(RGB)/6(RGBA)——
    Chromium 截图只会出这两种；碰到别的直接报错而不是猜。"""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG")
    pos, idat = 8, bytearray()
    width = height = bit_depth = color_type = interlace = None
    while pos < len(data):
        length, = struct.unpack(">I", data[pos:pos + 4])
        ctype = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if ctype == b"IHDR":
            width, height, bit_depth, color_type, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif ctype == b"IDAT":
            idat += body
        elif ctype == b"IEND":
            break
        pos += 12 + length
    if bit_depth != 8 or interlace != 0 or color_type not in (2, 6):
        raise ValueError(f"不支持的 PNG 形态：bit_depth={bit_depth} color_type={color_type} interlace={interlace}")
    bpp = 3 if color_type == 2 else 4
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    out = bytearray(width * height * bpp)
    prev = bytearray(stride)
    src = memoryview(raw)
    p = 0
    for y in range(height):
        f = raw[p]; p += 1
        line = bytearray(src[p:p + stride]); p += stride
        if f == 1:      # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:    # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:    # Average
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:    # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pp = a + b - c
                pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return width, height, bytes(out), bpp


def _diff(base: bytes, shot: bytes, chan_thr: int) -> dict:
    """逐像素 diff。返回 diff_ratio / 差异外接框面积占比 / 尺寸是否一致 / diff 灰度图字节（供写 diff PNG）。"""
    if base == shot:
        return {"same_file": True}
    bw, bh, bpx, bbpp = _decode_png(base)
    sw, sh, spx, sbpp = _decode_png(shot)
    if (bw, bh) != (sw, sh):
        return {"same_file": False, "size_mismatch": (bw, bh, sw, sh)}
    n = bw * bh
    diff_count = 0
    minx, miny, maxx, maxy = bw, bh, -1, -1
    marks = bytearray(n)                     # 1 = 差异像素（写 diff 图用）
    step_b, step_s = bbpp, sbpp
    for y in range(bh):
        row_b = y * bw * step_b
        row_s = y * bw * step_s
        for x in range(bw):
            i_b, i_s = row_b + x * step_b, row_s + x * step_s
            if (abs(bpx[i_b] - spx[i_s]) > chan_thr
                    or abs(bpx[i_b + 1] - spx[i_s + 1]) > chan_thr
                    or abs(bpx[i_b + 2] - spx[i_s + 2]) > chan_thr):
                diff_count += 1
                marks[y * bw + x] = 1
                if x < minx: minx = x
                if x > maxx: maxx = x
                if y < miny: miny = y
                if y > maxy: maxy = y
    area_ratio = ((maxx - minx + 1) * (maxy - miny + 1) / n) if diff_count else 0.0
    return {
        "same_file": False,
        "size": (bw, bh, bbpp),
        "diff_ratio": diff_count / n,
        "area_ratio": area_ratio,
        "base_px": bpx,
        "marks": marks,
    }


def _encode_png(width: int, height: int, bpp: int, px: bytes) -> bytes:
    """filter 0 逐行写 PNG（diff 图用，标准库即可）。"""
    def chunk(ctype: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + ctype + body + struct.pack(">I", zlib.crc32(ctype + body))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2 if bpp == 3 else 6, 0, 0, 0)
    raw = bytearray()
    stride = width * bpp
    for y in range(height):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def _write_diff_png(info: dict, path: Path) -> None:
    """基线压暗作底、差异像素标红——一眼看出哪块变了。"""
    w, h, bpp = info["size"]
    base, marks = info["base_px"], info["marks"]
    out = bytearray(w * h * 3)
    for p in range(w * h):
        o, b = p * 3, p * bpp
        if marks[p]:
            out[o], out[o + 1], out[o + 2] = 255, 0, 0
        else:
            out[o] = base[b] // 3
            out[o + 1] = base[b + 1] // 3
            out[o + 2] = base[b + 2] // 3
    path.write_bytes(_encode_png(w, h, 3, bytes(out)))


# ---------------------------------------------------------------- record / check

def _manifest(wanted: list[str]) -> dict:
    return {
        "note": "前端视觉回归基线。masks 的每条理由见各状态；重录：python scripts/visual_regression.py --record",
        "shots": {
            name: {
                "file": f"{name}.png",
                "viewport": SHOTS[name][0],
                "clip": SHOTS[name][1],
                "masks": SHOTS[name][3],
                "setup": SHOTS[name][4],
            } for name in wanted
        },
    }


def _capture_all(base_url: str, wanted: list[str]) -> tuple[dict, list[str]]:
    """逐状态拍一遍，返回 {name: png_bytes} 与失败/报错清单。"""
    from playwright.sync_api import sync_playwright
    shots: dict[str, bytes] = {}
    failed: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        for name in wanted:
            viewport, clip, setup, masks, _ = SHOTS[name]
            ctx, page, errors = _new_page(browser, base_url, viewport)
            try:
                setup(page)
                shots[name] = _shoot(page, clip, masks)
                status = "OK " if not errors else "ERR"
                print(f"[{status}] {name:16} {len(shots[name]) / 1024:7.1f} KiB"
                      + (f"  console/page errors: {errors}" if errors else ""))
                if errors:
                    failed.append(f"{name}: console/page errors {errors}")
            except Exception as exc:
                print(f"[FAIL] {name:16} {type(exc).__name__}: {exc}")
                failed.append(f"{name}: {exc}")
            finally:
                ctx.close()
        browser.close()
    return shots, failed


def cmd_record(args, wanted: list[str]) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    shots, failed = _capture_all(args.base_url, wanted)
    for name, data in shots.items():
        (BASELINE_DIR / f"{name}.png").write_bytes(data)
    # --only 重录时与既有 manifest 合并，不全量覆盖——否则只重录一张会把其余状态的条目抹掉。
    manifest_path = BASELINE_DIR / "manifest.json"
    merged: dict = {"note": _manifest([])["note"], "shots": {}}
    if manifest_path.exists():
        try:
            merged = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    merged.setdefault("shots", {}).update(_manifest(wanted)["shots"])
    manifest_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    total = sum(p.stat().st_size for p in BASELINE_DIR.glob("*.png"))
    print(f"\n基线已写入 {BASELINE_DIR}（{len(shots)} 张，共 {total / 1024 / 1024:.2f} MiB）+ manifest.json")
    if failed:
        print("有状态没拍成 / 拍摄中有前端报错：")
        for line in failed:
            print("  -", line)
        return 1
    print("RECORD OK")
    return 0


def cmd_check(args, wanted: list[str]) -> int:
    shots, failed = _capture_all(args.base_url, wanted)
    if failed:                       # 拍都没拍成，比对无从谈起
        print("拍摄阶段失败：")
        for line in failed:
            print("  -", line)
        return 1
    bad: list[str] = []
    for name in wanted:
        base_path = BASELINE_DIR / f"{name}.png"
        if not base_path.exists():
            print(f"[FAIL] {name:16} 基线不存在：{base_path}（先 --record）")
            bad.append(name)
            continue
        info = _diff(base_path.read_bytes(), shots[name], args.channel_threshold)
        if info.get("same_file"):
            print(f"[PASS] {name:16} 字节一致")
            continue
        if "size_mismatch" in info:
            bw, bh, sw, sh = info["size_mismatch"]
            print(f"[FAIL] {name:16} 尺寸变了：基线 {bw}x{bh} → 现拍 {sw}x{sh}（布局高度变化）")
            bad.append(name)
            continue
        dr, ar = info["diff_ratio"], info["area_ratio"]
        ok = dr <= args.tolerance and ar <= args.max_diff_area
        print(f"[{'PASS' if ok else 'FAIL'}] {name:16} 差异像素 {dr * 100:.3f}%"
              f"（容差 {args.tolerance * 100:.3f}%），差异区域面积 {ar * 100:.2f}%"
              f"（上限 {args.max_diff_area * 100:.2f}%）")
        if not ok:
            bad.append(name)
            DIFF_DIR.mkdir(parents=True, exist_ok=True)
            diff_path = DIFF_DIR / f"{name}.diff.png"
            _write_diff_png(info, diff_path)
            (DIFF_DIR / f"{name}.actual.png").write_bytes(shots[name])
            print(f"       差异图：{diff_path}")
    if bad:
        print(f"\nCHECK FAIL：{len(bad)} 个状态超出容差：{', '.join(bad)}")
        return 1
    print("\nCHECK OK")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--record", action="store_true", help="录基线（覆盖 tests/web/visual_baseline/）")
    mode.add_argument("--check", action="store_true", help="与基线比对，退出码 0/1")
    ap.add_argument("--base-url", default="http://127.0.0.1:7860")
    ap.add_argument("--only", nargs="*", choices=sorted(SHOTS), default=None, help="只跑某几个状态")
    ap.add_argument("--tolerance", type=float, default=0.005,
                    help="允许的差异像素占比（默认 0.005 = 0.5%%）")
    ap.add_argument("--max-diff-area", type=float, default=0.10,
                    help="差异像素外接框面积占比上限（默认 0.10 = 10%%）")
    ap.add_argument("--channel-threshold", type=int, default=12,
                    help="单通道差超过该值才计为差异像素（默认 12/255，吃抗锯齿噪声）")
    args = ap.parse_args()

    try:
        print("health:", _health(args.base_url))
    except Exception as exc:
        print(f"服务没起来（{args.base_url}）：{exc}\n先跑 `python scripts/run_web.py`。", file=sys.stderr)
        return 2
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        print("缺 playwright：`pip install playwright`（浏览器用系统 Edge，无需 `playwright install`）", file=sys.stderr)
        return 2

    wanted = args.only or list(SHOTS)
    return cmd_record(args, wanted) if args.record else cmd_check(args, wanted)


if __name__ == "__main__":
    raise SystemExit(main())
