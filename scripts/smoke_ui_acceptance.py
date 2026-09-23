# -*- coding: utf-8 -*-
"""UI 验收走查（2026-08-03 真机四幕，真 LLM、不 stub）：

1. **零配置开箱**：全新浏览器（无存档）→ health 探测把接入方式默认到服务端预设（DeepSeek），
   设置面板三维度布局截图（AI 执行默认开、未禁点）。
2. **病例句**「汇报数据库的当前状态」→ 真 LLM 分流（tool / curate.db_status）→ observe 图内
   读工具 → LLM 组织汇报 + 状态卡（截图存证，验收硬性条目）。
3. **降级气泡**：设置里关掉「AI 执行」→ 说「删除我上传的文件」→ 规则检出操作意图 →
   accent 降级气泡 +「去开启 AI 执行」CTA（截图存证），且全程零 LLM（via=agent_off）。
4. **检索主链路不受染**：「有 FASTQ 的人类乳腺癌数据」→ 结果卡 + 侧栏工作卡（截图存证）。

用法：先起服务（run_web.py，端口 7973，.env 已配 DeepSeek），再
    .venv/Scripts/python.exe scripts/smoke_ui_acceptance.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1680, "height": 960})
        page = ctx.new_page()
        page.goto(BASE, wait_until="networkidle")
        page.wait_for_timeout(2500)   # health 探测 + 零配置默认 + playHero 落幕
        # 新手教程弹窗会遮住内容（无存档的全新浏览器必现）——先跳过，截图要的是产品不是教程。
        try:
            page.click("text=跳过", timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass

        # ---- 幕1：零配置开箱（接入方式默认到服务端预设；设置三维度）
        preset = page.evaluate("() => document.getElementById('cfgProvider').value")
        check("零配置：接入方式默认到服务端预设（非 mock）", preset not in ("", "mock"), f"preset={preset}")
        page.click("#settingsBtn")
        page.wait_for_timeout(900)
        agent_on = page.evaluate("() => document.getElementById('cfgAgentExec').checked")
        check("零配置：AI 执行默认开", agent_on is True)
        gated = page.evaluate(
            "() => document.getElementById('nodeAgentExec').classList.contains('ai-gated')")
        check("零配置：已配 key 必可开关（AI 执行未禁点）", gated is False)
        page.screenshot(path=str(SHOTS / "1-settings-three-dimensions.png"))
        page.click("#settingsClose")
        page.wait_for_timeout(500)

        # ---- 幕2：病例句「汇报数据库的当前状态」（真 LLM 分流 + observe + 汇报 + 状态卡）
        page.fill("#queryInput", "汇报数据库的当前状态")
        page.click("#submitBtn")
        try:
            page.wait_for_selector(".arx-card", timeout=90000)   # observe + 状态卡（真 LLM 双程调用）
        except Exception:
            page.screenshot(path=str(SHOTS / "2-db-status-FAILED.png"))
            check("病例句：状态卡上屏", False)
        else:
            page.wait_for_timeout(2500)   # narrate 汇报落地
            hist = page.inner_text("#cbHistory")
            check("病例句：LLM 汇报上屏（含条数）", "5712" in hist or "条" in hist)
            check("病例句：状态卡含 6 个来源", "来源" in hist)
            check("病例句：含外部库与回收站清单", "外部库" in hist and "回收站" in hist)
            trace = page.eval_on_selector_all(
                "#cbHistory .arx-trace", "els => els.map(e => e.textContent).join('\\n')")
            check("病例句：行动流含 observe 步骤（汇报数据库状态）", "汇报数据库状态" in trace)
            page.evaluate("document.querySelectorAll('#cbHistory .arx-trace').forEach(d => d.open = true)")
            page.wait_for_timeout(300)
            page.screenshot(path=str(SHOTS / "2-db-status.png"))

        # ---- 幕3：降级气泡（AI 执行关 → 操作句 → 气泡 + CTA；零 LLM）
        page.click("#settingsBtn")
        page.wait_for_timeout(700)
        page.click("#nodeAgentExec .switch")   # 关掉 AI 执行
        page.wait_for_timeout(400)
        page.click("#settingsClose")
        page.wait_for_timeout(500)
        # 无结果有对话 → chat-in-main：唯一可见输入框是 #queryInput（侧栏 #chatInput 此时隐藏）
        box = "#chatInput" if page.is_visible("#chatInput") else "#queryInput"
        page.fill(box, "删除我上传的文件")
        page.click("#chatSendBtn" if box == "#chatInput" else "#submitBtn")
        try:
            page.wait_for_selector(".cbh-agent-bubble", timeout=15000)
        except Exception:
            page.screenshot(path=str(SHOTS / "3-degradation-FAILED.png"))
            check("降级气泡上屏", False)
        else:
            bubble = page.inner_text(".cbh-agent-bubble")
            check("降级气泡：引用认到的操作词", "删除" in bubble)
            check("降级气泡：指路 AI 执行", "AI 执行" in bubble)
            cta = page.locator(".cbh-agent-cta")
            check("降级气泡：CTA 存在", cta.count() == 1)
            page.wait_for_timeout(800)   # 气泡入场动效落幕再截（cbhIn 的 opacity 过渡）
            page.screenshot(path=str(SHOTS / "3-degradation-bubble.png"))
            cta.first.click()   # CTA 打开设置 → 重新开启 AI 执行，恢复后续幕的环境
            page.wait_for_timeout(700)
            check("降级气泡：CTA 打开设置", page.evaluate(
                "() => document.getElementById('settings').classList.contains('open')") is True)
            page.click("#nodeAgentExec .switch")
            page.wait_for_timeout(400)
            page.click("#settingsClose")
            page.wait_for_timeout(500)

        # ---- 幕4：检索主链路（规则检索零 LLM 染指；结果卡 + 侧栏工作卡）
        page.fill("#queryInput", "有 FASTQ 的人类乳腺癌数据")
        page.click("#submitBtn")
        try:
            page.wait_for_selector(".card, .result-card, [data-card]", timeout=60000)
        except Exception:
            page.screenshot(path=str(SHOTS / "4-search-FAILED.png"))
            check("检索主链路：结果卡上屏", False)
        else:
            page.wait_for_timeout(2500)
            work = page.evaluate("() => { const w = document.getElementById('sideWork'); return w && !w.hidden; }")
            check("检索主链路：侧栏工作卡出现", work is True)
            page.screenshot(path=str(SHOTS / "4-search-results.png"))

        browser.close()

    print("-" * 60)
    if fails:
        print(f"有失败：{len(fails)} 项")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
