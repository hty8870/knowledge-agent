# -*- coding: utf-8 -*-
"""自动打包回执 chip 精简 Playwright 验证。

验证点（用户需求原话三点）：
  case1) 自动打包（「下载top5」）回执：#taskPackPanel **保持关闭**；chip 行只剩
      「打开下载面板自己挑」（pack 系无撤回）；「按原话重新检索」「以后别自动执行」
      两颗 chip 在整条对话流里都不存在；
  case2) 点「打开下载面板自己挑」→ 面板打开、清单可勾选；
  case3) 显式预览（「我自己挑，先给我看看清单」）→ pack.preview 履约：面板**会**打开；
  case4) 回归：结果区顶部「下载这批数据」钮照常开面板；
  case5) 回归：设置里 cfgAgentExec（AI 执行开关）不受删 chip 影响（off chip 原是它
      的一个入口，删掉后开关本身必须还在、可切）。
截图 .fix-shots/packchips/，console 零报错才 PASS。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7973 run_web.py），再
    .venv/Scripts/python.exe scripts/smoke_packchips.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots" / "packchips"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

QUERY = "推荐有 FASTQ 的人类乳腺癌数据"


def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []
    js_errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 960})
        page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
        page.on("console", lambda m: js_errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.goto(BASE, wait_until="networkidle")
        page.evaluate("() => { localStorage.setItem('biodata_onboarding_v1', 'done');"
                      " localStorage.removeItem('biodata_sidebar_closed_v1'); }")
        page.reload(wait_until="networkidle")
        page.wait_for_timeout(1200)
        page.evaluate(
            "() => { const r = document.getElementById('cfgRecall'); if (r) r.checked = false;"
            " const s = document.getElementById('cfgStrategy'); if (s) s.checked = false; }")

        def say(text: str) -> None:
            page.click("#chatInput")
            page.fill("#chatInput", text)
            page.press("#chatInput", "Enter")

        # ---- 造态：一次真实检索（有结果，侧栏对话窗可用） ----
        page.fill("#queryInput", QUERY)
        page.click("#submitBtn")
        page.wait_for_selector("#resultsGrid .card", timeout=30000)
        page.wait_for_function(
            "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }", timeout=30000)
        page.wait_for_timeout(800)

        # ---- case1) 自动打包「下载top5」：面板保持关闭 + chip 行只剩「打开下载面板自己挑」 ----
        say("下载top5")
        # 回执收尾 = chip 上屏（actFinish）；preview→build 链路异步，等 panel chip 出现
        page.wait_for_selector('#cbHistory [data-act-fix="panel"]', timeout=45000)
        page.wait_for_timeout(1500)   # buildTaskPack 回包 + 总结改写落幕
        case1 = page.evaluate(
            """() => {
                const chips = [...document.querySelectorAll('#cbHistory [data-act-fix]')].map((b) => b.dataset.actFix);
                const panel = document.getElementById('taskPackPanel');
                const txt = document.getElementById('cbHistory').innerText;
                return { chips, panelHidden: panel.hidden,
                         noResearch: !txt.includes('按原话重新检索'),
                         noOff: !txt.includes('以后别自动执行') };
            }"""
        )
        check("case1：自动打包时下载面板保持关闭", case1["panelHidden"], str(case1))
        check("case1：chip 行只剩「打开下载面板自己挑」（无 research/off/undo）",
              case1["chips"] == ["panel"], str(case1["chips"]))
        check("case1：对话流无「按原话重新检索」「以后别自动执行」", case1["noResearch"] and case1["noOff"])
        page.screenshot(path=str(SHOTS / "case1-自动打包回执-面板保持关闭.png"))
        page.locator("#cbHistory .act-fix").last.screenshot(path=str(SHOTS / "case1b-chip行特写.png"))

        # ---- case2) 点 chip → 面板打开、清单可勾选 ----
        page.click('#cbHistory [data-act-fix="panel"]')
        page.wait_for_selector("#taskPackPanel:not([hidden])", timeout=15000)
        page.wait_for_selector("#taskPackPanel .tp-loading", state="detached", timeout=15000)
        page.wait_for_timeout(400)
        case2 = page.evaluate(
            """() => {
                const boxes = document.querySelectorAll('#taskPackPanel input[type="checkbox"]');
                return { visible: !document.getElementById('taskPackPanel').hidden, nBoxes: boxes.length };
            }"""
        )
        check("case2：点 chip 面板打开、清单行带勾选框", case2["visible"] and case2["nBoxes"] > 0, str(case2))
        if case2["nBoxes"] > 1:
            page.locator('#taskPackPanel input[type="checkbox"]').nth(1).click()   # 勾掉一行验证可改
            page.wait_for_timeout(250)
        page.screenshot(path=str(SHOTS / "case2-点chip面板打开可勾选.png"))
        page.click("#taskPackCloseBtn")   # 关面板，为 case3 的「会打开」造对照
        page.wait_for_timeout(300)
        check("case2：面板可关闭", page.evaluate("() => document.getElementById('taskPackPanel').hidden"))

        # ---- case3) 显式预览「我自己挑，先给我看看清单」→ 面板会打开（pack.preview 履约） ----
        say("我自己挑，先给我看看清单")
        page.wait_for_selector("#taskPackPanel:not([hidden])", timeout=45000)
        page.wait_for_selector("#taskPackPanel .tp-loading", state="detached", timeout=15000)
        page.wait_for_timeout(400)
        case3 = page.evaluate(
            "() => ({ visible: !document.getElementById('taskPackPanel').hidden,"
            " rows: document.querySelectorAll('#taskPackPanel input[type=\"checkbox\"]').length })")
        check("case3：显式预览面板会打开（pack.preview 履约）", case3["visible"] and case3["rows"] > 0, str(case3))
        page.screenshot(path=str(SHOTS / "case3-显式预览面板打开.png"))
        page.click("#taskPackCloseBtn")
        page.wait_for_timeout(300)

        # ---- case4) 回归：结果区顶部「下载这批数据」钮照常开面板 ----
        page.click("#taskPackBtn")
        page.wait_for_selector("#taskPackPanel:not([hidden])", timeout=15000)
        page.wait_for_timeout(400)
        check("case4：顶部「下载这批数据」钮照常开面板",
              page.evaluate("() => !document.getElementById('taskPackPanel').hidden"))
        page.screenshot(path=str(SHOTS / "case4-顶部按钮开面板.png"))
        page.click("#taskPackBtn")   # 再点收起（toggle）
        page.wait_for_timeout(300)
        check("case4：再点可收起", page.evaluate("() => document.getElementById('taskPackPanel').hidden"))

        # ---- case5) 回归：cfgAgentExec 开关在场可切（off chip 删了，开关本体不受影响） ----
        cfg_probe = page.evaluate(
            """() => {
                const box = document.getElementById('cfgAgentExec');
                if (!box) return { ok: false };
                const before = box.checked;
                box.checked = !before; box.dispatchEvent(new Event('change', { bubbles: true }));
                const mid = box.checked;
                box.checked = before; box.dispatchEvent(new Event('change', { bubbles: true }));
                return { ok: mid === !before && box.checked === before, before };
            }"""
        )
        check("case5：cfgAgentExec 开关在场、可切换、可切回", cfg_probe["ok"], str(cfg_probe))

        browser.close()

    if js_errors:
        print("\n浏览器 JS 报错：")
        for e in js_errors:
            print("  " + e)
        fails.append("console 零报错")
    else:
        print("PASS 浏览器 console 零报错")

    print(f"\n{'全部通过' if not fails else '有失败'}：{len(fails)} 项失败" if fails else "\n全部通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
