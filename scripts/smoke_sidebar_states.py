# -*- coding: utf-8 -*-
"""侧边栏状态机 Playwright 回归（顽疾根治的守门员）。

钉两件事：

1. **内联样式零残留**（根因）：gsap autoAlpha 的 tween 只许 clearProps:"all"——
   `.sidebar` 在 boot 动画落幕后**绝不允许**带内联 opacity/visibility（内联优先级压过
   body.side-closed 的淡出规则，侧栏收起时以全不透明姿态滑出=「没有按常规折叠」）。
2. **合法状态枚举**：任意操作序列后，body 布局类与侧栏可见性必须落在合法集合里——
   桌面展开 / 桌面收起 / 移动抽屉开 / 移动抽屉关；`side-closed` ⇔ 侧栏不可见恒成立。

操作序列（每一步后都断言）：boot → 收起 → 展开 → 设置开/关 → 切视图（数据集浏览→回）
→ 缩窗过 780px 断点（移动抽屉规则）→ 拉回桌面 → 再收起/展开。

用法：先起服务（run_web.py 或 uvicorn，端口 7973），再
    .venv/Scripts/python.exe scripts/smoke_sidebar_states.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".sidebar-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

#: 允许出现在 body 上的布局类全集（状态机字母表；出现表外的类 = 类名残留，立刻 FAIL）。
LEGAL_BODY_CLASSES = {
    "side-closed", "side-resizing", "on-query", "facets-active", "scope-in-side",
    "chat-main-on", "has-results",
    "view-swapped",   # 视图交换（#swSwapBtn）：对话窗 ↔ 结果网格换位
}


def _body_classes(page) -> set[str]:
    return set(page.evaluate("() => Array.from(document.body.classList)"))


def _sidebar_state(page) -> dict:
    return page.evaluate(
        """() => {
            const el = document.querySelector('.sidebar');
            const cs = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return {
                inlineOpacity: el.style.opacity || "",
                inlineVisibility: el.style.visibility || "",
                closed: document.body.classList.contains('side-closed'),
                opacity: parseFloat(cs.opacity),
                right: r.right,          // 滑出屏外时 right <= 0
                mobile: window.innerWidth <= 780,
            };
        }"""
    )


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    def assert_legal(page, step: str) -> None:
        classes = _body_classes(page)
        extra = classes - LEGAL_BODY_CLASSES
        check(f"{step}：body 类全部合法", not extra, f"残留类 {sorted(extra)}" if extra else "")
        st = _sidebar_state(page)
        check(f"{step}：.sidebar 无内联 opacity/visibility 残留",
              st["inlineOpacity"] == "" and st["inlineVisibility"] == "",
              f"opacity='{st['inlineOpacity']}' visibility='{st['inlineVisibility']}'")
        # side-closed ⇔ 侧栏不可见（淡出 or 滑出屏外）；展开 ⇔ 可见。恒真式，两种形态都算合法。
        if st["closed"]:
            check(f"{step}：收起态侧栏不可见", st["opacity"] < 0.05 or st["right"] <= 0,
                  f"opacity={st['opacity']} right={st['right']}")
        else:
            check(f"{step}：展开态侧栏可见", st["opacity"] > 0.95 and st["right"] > 0,
                  f"opacity={st['opacity']} right={st['right']}")

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 960})
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(1200)   # playHero 时间线落幕（.sidebar tween 0.5s）
        assert_legal(page, "boot")
        page.screenshot(path=str(SHOTS / "01-boot.png"))

        # localStorage 的持久化偏好不污染状态机断言（上一轮收起态会被 initSidebar 读回）
        page.evaluate("() => localStorage.removeItem('biodata_sidebar_closed_v1')")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        assert_legal(page, "boot-clean")

        page.click("#sideCollapse")
        page.wait_for_timeout(700)    # 滑出过渡
        assert_legal(page, "收起")
        page.screenshot(path=str(SHOTS / "02-collapsed.png"))

        page.click("#sideFab")
        page.wait_for_timeout(700)
        assert_legal(page, "展开")
        page.screenshot(path=str(SHOTS / "03-expanded.png"))

        page.click("#settingsBtn")
        page.wait_for_timeout(500)
        check("设置开：drawer 打开", page.evaluate(
            "() => document.getElementById('settings').classList.contains('open')"))
        page.screenshot(path=str(SHOTS / "04-settings.png"))
        page.click("#settingsClose")
        page.wait_for_timeout(400)
        assert_legal(page, "设置关")

        page.click(".side-nav .nav-item[data-view='browse']")
        page.wait_for_timeout(800)
        assert_legal(page, "数据集浏览")
        page.screenshot(path=str(SHOTS / "05-browse.png"))
        page.click(".side-nav .nav-item[data-view='query']")
        page.wait_for_timeout(800)
        assert_legal(page, "回智能查询")

        # 过断点：桌面展开 → 移动端。**收起不是 resize 的责任**（设计如此：resize 不动 side-closed，
        # 抽屉开着过断点＝移动抽屉开，是合法态，遮罩可点关）；「移动端默认收起」是 boot 契约
        # （initSidebar），所以缩窗后重新加载一次再断言。
        page.set_viewport_size({"width": 640, "height": 900})
        page.wait_for_timeout(600)
        assert_legal(page, "缩窗过断点（抽屉保持开＝合法移动抽屉开）")
        page.screenshot(path=str(SHOTS / "06-mobile-open.png"))
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(900)
        st = _sidebar_state(page)
        check("移动端 boot：抽屉默认收起", st["closed"] is True, f"closed={st['closed']}")
        assert_legal(page, "移动端 boot")
        page.screenshot(path=str(SHOTS / "07-mobile-boot.png"))
        page.set_viewport_size({"width": 1680, "height": 960})
        page.wait_for_timeout(800)
        assert_legal(page, "拉回桌面（side-closed 随刷新持久，设计如此）")
        page.click("#sideFab")   # 移动端 boot 的收起态会带过断点——先展开再回到桌面主路径
        page.wait_for_timeout(700)
        assert_legal(page, "拉回桌面后展开")

        page.click("#sideCollapse")
        page.wait_for_timeout(700)
        assert_legal(page, "再收起")
        page.click("#sideFab")
        page.wait_for_timeout(700)
        assert_legal(page, "再展开")
        page.screenshot(path=str(SHOTS / "08-final.png"))

        browser.close()

    print(f"\n{'全部通过' if not fails else '有失败'}：{len(fails)} 项失败" if fails else "\n全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
