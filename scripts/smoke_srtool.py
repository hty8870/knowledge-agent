# -*- coding: utf-8 -*-
"""零命中救回 Playwright 验证。

场景（截图 .fix-shots/srtool/，console 零报错才 PASS）：
  case1) mock 档**结构性不发**救回：网络监听零 /api/agent/search-rescue 请求、无在途 sys；
  case2) 打桩 adopted → 换屏（结果卡上屏）+ audit 横幅（renderStatus 照旧出，零新代码）+ sys 留痕；
  case3) 打桩 no_rewrite → 空态不动 + 一条诚实 sys（人话，不甩枚举名）；
  case4) 打桩 attempted=false（agent_unavailable）→ **静默**（只在途那句，不追加基础设施借口）；
  case5) 连发两次同查询只救一次（指纹记忆）；
  case6) 交换态（viewswap）下打桩 adopted（延迟回包，先交换后落地）→ 侧栏换屏正常；
  case7) 真实 LLM 端到端（仅当项目 .env 有可用 key）：真打 /api/agent/search-rescue，
      断言「真发了救回 + 留痕或换屏」——LLM 判什么不强求，跑不了如实记取证边界。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7974），再
    .venv/Scripts/python.exe scripts/smoke_srtool.py [base_url]
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots" / "srtool"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7974"

HIT_QUERY = "人类乳腺癌 FASTQ 数据"
ZERO_QUERY = "人类火星殖民地单细胞转录组数据集"
REWRITE = "人类乳腺癌单细胞转录组"


def _load_real_llm() -> dict | None:
    """从项目 .env 读真实 LLM 配置（只读本机文件，不打印）。读不到可用 key → None（case7 跳过）。"""
    env = {}
    for name in (".env", ".env.local"):
        p = ROOT / name
        if not p.is_file():
            continue
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    provider = (env.get("LLM_PROVIDER") or "openai-compatible").strip()
    key = (env.get("OPENAI_API_KEY") or env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY") or "").strip()
    if not key or "your_" in key:
        return None
    return {
        "provider": provider,
        "api_key": key,
        "base_url": (env.get("LLM_BASE_URL") or env.get("OPENAI_BASE_URL") or "").strip(),
        "model": (env.get("LLM_MODEL") or env.get("OPENAI_MODEL") or "").strip(),
    }


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

        def fresh_page(provider: str | None = "zhipuai"):
            page = browser.new_page(viewport={"width": 1680, "height": 960})
            page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: js_errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            page.goto(BASE, wait_until="networkidle")
            page.evaluate("() => { localStorage.setItem('biodata_onboarding_v1', 'done');"
                          " localStorage.removeItem('biodata_sidebar_closed_v1'); }")
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(1000)
            page.evaluate(
                "() => { const r = document.getElementById('cfgRecall'); if (r) r.checked = false;"
                " const s = document.getElementById('cfgStrategy'); if (s) s.checked = false;"
                " const a = document.getElementById('cfgAgentExec'); if (a) a.checked = true; }")
            if provider:
                page.evaluate(
                    "(v) => { const p = document.getElementById('cfgProvider');"
                    " p.value = v; p.dispatchEvent(new Event('change', { bubbles: true })); }", provider)
            page.wait_for_timeout(300)
            return page

        def search(page, query: str) -> None:
            page.fill("#queryInput", query)
            page.click("#submitBtn")
            page.wait_for_function(
                "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }",
                timeout=30000)
            page.wait_for_timeout(600)

        def sys_texts(page) -> list[str]:
            return page.evaluate(
                "() => [...document.querySelectorAll('#cbHistory .cbh-sys, #cbHistory [class*=sys]')]"
                ".map((e) => e.textContent || '')")

        def bubble_text(page) -> str:
            return page.evaluate("() => document.getElementById('cbHistory').innerText")

        # ---------- case1) mock 档结构性不发 ----------
        page = fresh_page(provider="mock")
        reqs = []
        page.on("request", lambda r: reqs.append(r.url) if "search-rescue" in r.url else None)
        search(page, ZERO_QUERY)
        page.wait_for_timeout(3000)
        check("case1：mock 档零命中→零 search-rescue 请求（结构性不调）", len(reqs) == 0, str(reqs))
        check("case1：mock 档无在途 sys", "换个说法再查一次" not in bubble_text(page))
        page.close()

        # ---------- case2) 打桩 adopted：换屏 + audit 横幅 + sys 留痕 ----------
        page = fresh_page()
        # 真实 /api/recommend 响应当 payload 底座（页内 fetch 取，形状零伪造；
        # 不走 UI 首查——结果态主检索框隐藏，本页只做零命中那一次 UI 检索）
        payload = page.evaluate(
            """async (q) => {
                const res = await fetch('/api/recommend', {
                    method: 'POST', headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ query: q, provider: 'mock', use_llm: false,
                                           mock_llm: true, top_k: 10 })
                });
                return await res.json();
            }""", HIT_QUERY)
        payload["audit"] = {"triggered": True, "verdict": False, "rewritten_query": REWRITE,
                            "used": True, "reason": "rewritten", "mode": "rerank",
                            "n_before": 0, "n_after": payload.get("result_total") or len(payload.get("results") or []),
                            "was_no_result": True}
        adopted_body = {"ok": True, "attempted": True, "reason": "adopted", "adopted": True,
                        "query": ZERO_QUERY, "rewrite": REWRITE, "n_before": 0,
                        "n_after": payload["audit"]["n_after"], "payload": payload,
                        "report_zh": f"已换用「{REWRITE}」重新检索，结果区已更新。",
                        "trace": [], "agent": {"available": True, "used": True}}
        hits = {"n": 0}

        def route_adopted(route):
            hits["n"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps(adopted_body, ensure_ascii=False))

        page.route("**/api/agent/search-rescue", route_adopted)
        search(page, ZERO_QUERY)
        page.wait_for_selector("#resultsGrid .card", timeout=15000)   # 换屏：结果卡上屏
        page.wait_for_timeout(800)
        banner = page.evaluate("() => { const b = document.getElementById('auditBanner');"
                               " return { hidden: b.hidden, text: b.textContent }; }")
        bt = bubble_text(page)
        check("case2：adopted → 救回请求恰好 1 次", hits["n"] == 1, str(hits))
        check("case2：adopted → 换屏出结果卡", page.locator("#resultsGrid .card").count() > 0)
        check("case2：audit 横幅照旧出（renderStatus 零新代码）",
              (not banner["hidden"]) and REWRITE in banner["text"], str(banner))
        check("case2：sys 留痕（在途句 + report_zh）",
              "换个说法再查一次" in bt and f"已换用「{REWRITE}」" in bt)
        page.screenshot(path=str(SHOTS / "case2-adopted换屏+audit横幅+sys留痕.png"))
        page.unroute("**/api/agent/search-rescue", route_adopted)
        page.close()

        # ---------- case3) 打桩 no_rewrite：空态不动 + 诚实 sys ----------
        page = fresh_page()
        hits3 = {"n": 0}

        def route_no_rewrite(route):
            hits3["n"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "attempted": True, "reason": "no_rewrite", "adopted": False,
                "query": ZERO_QUERY, "rewrite": "", "n_before": None, "n_after": None,
                "payload": None, "report_zh": "没有找到更合适的改写，当前结果保持不变。",
                "trace": [], "agent": {"available": True, "used": True}}, ensure_ascii=False))

        page.route("**/api/agent/search-rescue", route_no_rewrite)
        search(page, ZERO_QUERY)
        page.wait_for_timeout(2500)
        bt3 = bubble_text(page)
        check("case3：no_rewrite → 空态不动（无结果卡）", page.locator("#resultsGrid .card").count() == 0)
        check("case3：no_rewrite → 诚实 sys（不甩枚举名）",
              "也试着想过换个说法，但没有更合适的改写，当前结果保持不变。" in bt3
              and "no_rewrite" not in bt3)
        page.screenshot(path=str(SHOTS / "case3-no_rewrite空态不动+诚实sys.png"))
        page.unroute("**/api/agent/search-rescue", route_no_rewrite)
        page.close()

        # ---------- case4) 打桩 attempted=false：静默 ----------
        page = fresh_page()

        def route_silent(route):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "attempted": False, "reason": "agent_unavailable", "adopted": False,
                "query": ZERO_QUERY, "rewrite": "", "n_before": None, "n_after": None,
                "payload": None, "report_zh": "检索救回没有启动。", "trace": [],
                "agent": {"available": False, "used": False}}, ensure_ascii=False))

        page.route("**/api/agent/search-rescue", route_silent)
        search(page, ZERO_QUERY)
        page.wait_for_timeout(2500)
        bt4 = bubble_text(page)
        check("case4：attempted=false → 空态保持", page.locator("#resultsGrid .card").count() == 0)
        check("case4：attempted=false → 静默（只在途那句，无借口刷屏）",
              "换个说法再查一次" in bt4 and "没有启动" not in bt4
              and "没有完成" not in bt4 and "更好" not in bt4 and "agent_unavailable" not in bt4)
        page.unroute("**/api/agent/search-rescue", route_silent)
        page.close()

        # ---------- case5) 连发两次同查询只救一次 ----------
        page = fresh_page()
        hits5 = {"n": 0}

        def route_count(route):
            hits5["n"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "attempted": True, "reason": "no_rewrite", "adopted": False,
                "query": ZERO_QUERY, "rewrite": "", "n_before": None, "n_after": None,
                "payload": None, "report_zh": "", "trace": [],
                "agent": {"available": True, "used": True}}, ensure_ascii=False))

        page.route("**/api/agent/search-rescue", route_count)
        search(page, ZERO_QUERY)
        page.wait_for_timeout(2000)
        # 同查询再发一次：结果态主检索框隐藏，走统一对话窗（真实用户重发路径）；
        # 缓存命中或重取均不应再救（指纹记忆在 JS 会话内）。
        page.click("#chatInput")
        page.fill("#chatInput", ZERO_QUERY)
        page.press("#chatInput", "Enter")
        page.wait_for_function(
            "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }",
            timeout=45000)
        page.wait_for_timeout(2000)
        check("case5：同查询连发两次，救回只发 1 次（指纹记忆）", hits5["n"] == 1, str(hits5))
        page.unroute("**/api/agent/search-rescue", route_count)
        page.close()

        # ---------- case6) 交换态下 adopted 换屏 ----------
        page = fresh_page()
        search(page, HIT_QUERY)                      # 先有结果，交换钮可用
        page.wait_for_selector("#resultsGrid .card", timeout=15000)
        page.click("#swSwapBtn")
        page.wait_for_selector("body.view-swapped", timeout=10000)
        hits6 = {"n": 0}

        def route_adopted6(route):
            hits6["n"] += 1
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(adopted_body, ensure_ascii=False))

        page.route("**/api/agent/search-rescue", route_adopted6)
        # 交换态主检索框隐藏，走主区对话窗发零命中查询（统一路由 → runRecommend → 救回）
        page.click("#chatInput")
        page.fill("#chatInput", ZERO_QUERY)
        page.press("#chatInput", "Enter")
        page.wait_for_function(
            "() => { const b = document.getElementById('submitBtn'); return b && !b.disabled; }",
            timeout=45000)
        page.wait_for_timeout(2500)
        b6 = page.evaluate("() => ({ swapped: document.body.classList.contains('view-swapped'),"
                           " sideCards: document.querySelectorAll('#sideWork .card').length,"
                           " gridCards: document.querySelectorAll('#resultsGrid .card').length })")
        check("case6：交换态下零命中查询救回真发出", hits6["n"] == 1, str(hits6))
        check("case6：交换态下救回换屏出结果卡", b6["sideCards"] > 0 or b6["gridCards"] > 0, str(b6))
        check("case6：交换态保持（换屏没把布局打回）", b6["swapped"], str(b6))
        page.screenshot(path=str(SHOTS / "case6-交换态下救回换屏.png"))
        page.unroute("**/api/agent/search-rescue", route_adopted6)
        page.close()

        # ---------- case7) 真实 LLM 端到端（.env 有 key 才跑） ----------
        real = _load_real_llm()
        if not real:
            print("SKIP case7：项目 .env 无可读 key——真实 LLM 端到端未跑（取证边界：case2-case6 为打桩，"
                  "case1/case4 语义已覆盖结构性与静默路径；真实链路留给全量回归）")
        else:
            page = fresh_page(provider=None)
            # UI 选项值与后端 provider 名不同族（openai-compatible → compatible），先映射再写入
            ui_provider = {"openai-compatible": "compatible"}.get(real["provider"], real["provider"])
            page.evaluate(
                """(c) => {
                    const set = (id, v) => { const el = document.getElementById(id); if (!el) return;
                        el.value = v; el.dispatchEvent(new Event('change', { bubbles: true })); };
                    set('cfgProvider', c.provider); set('cfgBaseUrl', c.base_url);
                    set('cfgModel', c.model); set('cfgApiKey', c.api_key);
                }""", {"provider": ui_provider, "base_url": real["base_url"],
                       "model": real["model"], "api_key": real["api_key"]})
            page.wait_for_timeout(300)
            reqs7 = []
            page.on("request", lambda r: reqs7.append(r.url) if "search-rescue" in r.url else None)
            search(page, ZERO_QUERY)
            # 真实环：LLM decide + 重检，宽限 120s；等到任一救回落痕（换屏或诚实 sys）
            deadline = time.time() + 120
            landed = False
            while time.time() < deadline:
                bt7 = bubble_text(page)
                if ("也试着" in bt7) or page.locator("#resultsGrid .card").count() > 0:
                    landed = True
                    break
                page.wait_for_timeout(2000)
            bt7 = bubble_text(page)
            cards = page.locator("#resultsGrid .card").count()
            check("case7：真实 LLM 端到端——救回请求真发出", len(reqs7) == 1, str(len(reqs7)))
            check("case7：真实端到端有落痕（换屏或诚实 sys）", landed,
                  f"cards={cards}, sys尾部={bt7[-80:] if bt7 else ''}")
            page.screenshot(path=str(SHOTS / "case7-真实LLM端到端.png"))
            page.close()

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
