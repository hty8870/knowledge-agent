# -*- coding: utf-8 -*-
"""sum1摘要卡方法层关键词行内高光 Playwright 取证。

三张图（.fix-shots/sumhl/）：
  a) 正常态摘要卡特写——真实检索（cfgRecall/cfgStrategy 关），一层「规则排序」高光；
  b) 交换态侧栏摘要卡特写——真实交换（#swSwapBtn），验证 300px 窄宽 + 两行截断下
     行内 mark 不破行、不溢出；
  c) 三层 + 润色短句——**DOM 注入取证**（mock 环境无法触发 local_semantic/llm 层真实 used），
     直接写 resultSummaryText.innerHTML 模拟三层 + 「推荐说明由 AI 润色。」，验证多 mark 连排观感。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7973），再
    .venv/Scripts/python.exe scripts/smoke_sumhl.py [base_url]
"""
from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots" / "sumhl"
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

        # ---- 造态：一次真实检索（只有「规则排序」一层 used） ----
        page.fill("#queryInput", QUERY)
        page.click("#submitBtn")
        page.wait_for_selector("#resultsGrid .card", timeout=30000)
        page.wait_for_function(
            "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }", timeout=30000)
        page.wait_for_timeout(800)

        # ---- a) 正常态摘要卡特写 ----
        a = page.evaluate(
            """() => {
                const txt = document.getElementById('resultSummaryText');
                const marks = [...txt.querySelectorAll('mark.sum-layer')].map((m) => m.textContent);
                return { marks, html: txt.innerHTML,
                         cardVisible: !document.getElementById('searchTrace').hidden };
            }"""
        )
        check("a：正常态摘要卡可见、一层「规则排序」上高光",
              a["cardVisible"] and a["marks"] == ["规则排序"], str(a["marks"]))
        # 本机服务 AI 润色实际开着（llm_response_used=true），附注在场是诚实回显——
        # 钉的是「在场必为 sum1 短句、旧长句不在」，不是「未开 AI 时无附注」。
        check("a：润色附注若在场必为短句（旧长句不在）",
              "推荐说明由 AI 润色。" not in a["html"] or "不改变结果与顺序" not in a["html"])
        page.locator("#searchTrace").screenshot(path=str(SHOTS / "a-正常态摘要卡-单层高光.png"))

        # ---- b) 交换态侧栏摘要卡特写 ----
        page.click("#swSwapBtn")
        page.wait_for_selector("body.view-swapped", timeout=10000)
        page.wait_for_timeout(600)
        b = page.evaluate(
            """() => {
                const txt = document.getElementById('resultSummaryText');
                const card = document.getElementById('searchTrace');
                const r = txt.getBoundingClientRect();
                const marks = txt.querySelectorAll('mark.sum-layer').length;
                // 破行/溢出检测：任一 mark 的左右边缘超出摘要文本盒即算溢出
                let overflow = false;
                for (const m of txt.querySelectorAll('mark.sum-layer')) {
                    const mr = m.getBoundingClientRect();
                    if (mr.left < r.left - 1 || mr.right > r.right + 1) overflow = true;
                }
                return { swapped: document.body.classList.contains('view-swapped'),
                         width: Math.round(r.width), marks, overflow,
                         cardVisible: !card.hidden };
            }"""
        )
        check("b：交换态摘要卡在侧栏可见、高光仍在", b["swapped"] and b["cardVisible"] and b["marks"] == 1, str(b))
        check("b：窄栏（≤300px 量级）下高光不溢出", b["width"] <= 360 and not b["overflow"], str(b))
        page.locator("#searchTrace").screenshot(path=str(SHOTS / "b-交换态侧栏摘要卡-窄宽高光.png"))
        page.click("#swSwapBtn")   # 还原，给 c 回正常态
        page.wait_for_function("() => !document.body.classList.contains('view-swapped')", timeout=10000)
        page.wait_for_timeout(400)

        # ---- c) 三层 + 润色短句（DOM 注入取证，非真实链路） ----
        page.evaluate(
            """() => {
                document.getElementById('resultSummaryText').innerHTML =
                    '通过<mark class="sum-layer">规则排序</mark>、<mark class="sum-layer">本地精准重排</mark>'
                    + ' 与 <mark class="sum-layer">AI 重排</mark>检索，库中共 <b>36</b> 条记录匹配；'
                    + '展示前 <b>10</b> 条。推荐说明由 AI 润色。';
            }"""
        )
        page.wait_for_timeout(300)
        c = page.evaluate(
            "() => [...document.querySelectorAll('#resultSummaryText mark.sum-layer')].map((m) => m.textContent)")
        check("c：注入三 mark 全部在册（注入取证）",
              c == ["规则排序", "本地精准重排", "AI 重排"], str(c))
        page.locator("#searchTrace").screenshot(path=str(SHOTS / "c-三层高光+润色短句-DOM注入.png"))

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
