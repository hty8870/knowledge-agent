# -*- coding: utf-8 -*-
"""2026-08-31 两个 pill/下载通道 bug 的真机回归（用户截图指认）。

Bug1 混合轮：「小鼠空间转录组，并下载top5」（rank + pack.download 同轮）——
  回执气泡必须**同时**挂 检索结果 pill + 下载队列 pill（dlq 追加不顶替检索 pill）；
  且「你提到了下载——检索本身不包含这一步」指路条在动作真执行成后必须消失。
Bug2 引文导出：继续对话「把结果导出成 BibTeX 引文」——泛下载一视同仁走统一下载通道：
  回执气泡挂 dlq pill；下载队列里有「引文」类条目；点 pill 打开结果区下载面板。

安全：真实数据文件 GB 级——context.route 把一切**非本机**请求 abort（数据直下全是
跨域 URL），浏览器实际下载为零字节；本机 API/静态/ blob 照常。引文文件 KB 级走本机端点。
弹出的新标签页（target=_blank 直下锚点被拦后的残留）即开即关。

用法：先起服务（PORT=8317 BIODATA_SKIP_RECALL_WARM=1 ./.venv/Scripts/python.exe scripts/run_web.py），再
    ./.venv/Scripts/python.exe scripts/smoke_dlq_pills_playwright.py [base_url]
需要真实 LLM 配置（AI 执行开）；模型若没派发下载动作，脚本如实报 INCONCLUSIVE。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8317"
Q1 = "小鼠空间转录组，并下载top5"
Q2 = "把结果导出成 BibTeX 引文"


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []
    inconclusive: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        context = browser.new_context(viewport={"width": 1440, "height": 960},
                                      accept_downloads=True)

        # 安全闸：只放行本机与 blob/data；跨域数据文件直下全部 abort（零真实下载）。
        def _route(route):
            url = route.request.url
            if url.startswith(BASE) or url.startswith("blob:") or url.startswith("data:"):
                route.continue_()
            else:
                route.abort()

        context.route("**/*", _route)
        page = context.new_page()
        # target=_blank 残留弹窗即开即关（守卫：绝不关主页面自身）
        context.on("page", lambda p: p.close() if p is not page else None)
        page.on("download", lambda d: d.cancel())  # 双保险：任何真下载立即取消

        page.goto(BASE)
        page.wait_for_timeout(2500)
        try:
            page.click("text=跳过", timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass

        # 「AI 执行」必须开着（默认开；确认，不改设置）
        page.click("#settingsBtn")
        page.wait_for_timeout(900)
        agent_on = page.evaluate("() => document.getElementById('cfgAgentExec').checked")
        page.click("#settingsClose")
        page.wait_for_timeout(500)
        check("AI 执行已开启", agent_on)

        def maybe_consent():
            try:
                if page.evaluate("() => { const m = document.getElementById('consentModal');"
                                 " return m && !m.hidden; }"):
                    page.click("#consentAgreeBtn")
                    page.wait_for_timeout(500)
            except Exception:
                pass

        def settle(timeout_ms: int) -> bool:
            """等本轮处理收尾：提交按钮恢复可用（在途闸释放）且进度泡不在。"""
            try:
                page.wait_for_function(
                    "() => { const b = document.getElementById('submitBtn');"
                    " return b && !b.disabled; }", timeout=timeout_ms)
                page.wait_for_timeout(2500)
                return True
            except Exception:
                return False

        # ---------------- Bug1：混合轮（rank + pack.download） ----------------
        print(f"Q1: {Q1}")
        page.fill("#queryInput", Q1)
        page.click("#submitBtn")
        page.wait_for_timeout(1500)
        maybe_consent()
        if not settle(300000):
            page.screenshot(path=str(SHOTS / "dlq-pills-q1-timeout.png"))
            check("Q1 本轮收尾", False, "超时")
            browser.close()
            return 1

        dlq_count_q1 = page.locator("[data-dlq-pill]").count()
        if dlq_count_q1 == 0:
            page.screenshot(path=str(SHOTS / "dlq-pills-q1-nodlq.png"))
            inconclusive.append("Q1 模型没派发下载动作（无 dlq pill），Bug1 无法实锤")
            print("  [INCONCLUSIVE] Q1 没有下载 pill——模型这轮可能只检索没下载")
        else:
            # 同一颗气泡里：检索结果 pill（data-ft-pill 且非零命中标记）与下载 pill 并存
            merged = page.evaluate(
                "() => Array.from(document.querySelectorAll('.cbh-sys-bubble')).map("
                " b => ({ dlq: b.querySelectorAll('[data-dlq-pill]').length,"
                " res: b.querySelectorAll('[data-ft-pill]').length }))")
            both = [m for m in merged if m["dlq"] > 0 and m["res"] > 0]
            check("Q1 回执气泡同时挂 检索结果 pill + 下载 pill", len(both) > 0,
                  f"气泡 pill 分布: {merged}")
            hint = page.evaluate(
                "() => { const b = document.getElementById('actionHint');"
                " return b ? { hidden: b.hidden, empty: !b.innerHTML.trim() } : null; }")
            check("Q1 指路条（检索本身不包含这一步）已摘除",
                  bool(hint) and (hint["hidden"] or hint["empty"]), f"{hint}")
            # 点 dlq pill → 结果区下载面板展开、队列有「数据文件」行
            page.locator("[data-dlq-pill]").first.click()
            page.wait_for_timeout(2500)
            panel_open = page.evaluate(
                "() => { const p = document.getElementById('taskPackPanel');"
                " return p && !p.hidden; }")
            rows = page.locator(".dlq-row").all_inner_texts()
            check("Q1 点 pill 打开结果区下载面板", bool(panel_open))
            check("Q1 队列里有数据文件条目",
                  any("数据文件" in r for r in rows), f"{len(rows)} 行: {rows[:3]}")
            page.screenshot(path=str(SHOTS / "dlq-pills-q1.png"))

        # ---------------- Bug2：引文导出走统一下载通道 ----------------
        print(f"Q2: {Q2}")
        # 侧栏切到「继续对话」页签再发（微信式继续对话通道）
        try:
            page.click("text=继续对话", timeout=5000)
            page.wait_for_timeout(600)
        except Exception:
            pass
        page.fill("#chatInput", Q2)
        page.click("#chatSendBtn")
        page.wait_for_timeout(1500)
        maybe_consent()
        if not settle(300000):
            page.screenshot(path=str(SHOTS / "dlq-pills-q2-timeout.png"))
            check("Q2 本轮收尾", False, "超时")
            browser.close()
            return 1

        dlq_count_q2 = page.locator("[data-dlq-pill]").count()
        if dlq_count_q2 <= dlq_count_q1:
            page.screenshot(path=str(SHOTS / "dlq-pills-q2-nodlq.png"))
            if dlq_count_q1 == 0:
                check("Q2 引文导出回执挂 dlq pill", False, "本轮无 dlq pill（Q1 也没有，路径未验证）")
            else:
                inconclusive.append("Q2 模型没走引文导出（dlq pill 数未增），Bug2 无法实锤")
                print("  [INCONCLUSIVE] Q2 没有新增下载 pill——模型可能只答没导出")
        else:
            check("Q2 引文导出回执挂 dlq pill（总数增加）", True,
                  f"{dlq_count_q1} → {dlq_count_q2}")
            page.locator("[data-dlq-pill]").last.click()
            page.wait_for_timeout(2500)
            rows = page.locator(".dlq-row").all_inner_texts()
            check("Q2 队列里有引文条目", any("引文" in r for r in rows),
                  f"{len(rows)} 行: {rows[:4]}")
            cite_card = page.locator(".cbh-sys-extra .arx-card").all_inner_texts()
            print(f"  [info] 环内卡片: {len(cite_card)} 张"
                  + (f"（首张: {cite_card[-1][:60]}…）" if cite_card else "（plan-only 通道无卡，正常）"))
            page.screenshot(path=str(SHOTS / "dlq-pills-q2.png"))

        browser.close()

    print()
    for t in inconclusive:
        print(f"INCONCLUSIVE: {t}")
    if fails:
        print(f"FAIL: {len(fails)} 项 —— {fails}")
        return 1
    if inconclusive:
        print("PASS（含 inconclusive 项，需人工复查或重跑）")
        return 0
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
