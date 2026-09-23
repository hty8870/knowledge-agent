# -*- coding: utf-8 -*-
"""「初步结果先行 + 信息流升级」前端 Playwright 验证（async 版——
同步 route handler 里等待门闩会死锁，故全脚本走 async_playwright）。

手法（两段式打桩造观察窗）：page.route("**/api/utterance") 按请求体 stream 分流——
  · 流式请求：fulfill 一段含 preliminary + tool_start×2 + step（完成帧）+ 一个未知事件的
    SSE，**不发 final 直接断流**——前端 ubFetchStream 抛「没有 final 帧」，走 ubSubmit
    既有非流式重发 fallback（顺带验证这条既有回退路径）；
  · 非流式请求：handler 挂 gate.wait()，主协程趁机断言中间态（徽标在 / 先行结果在屏 /
    /api/recommend 零调用 / ubRouteBody 十参随流上行），再 gate.set() 放行 final。
  瞬态（思考泡换句、tool_start pending 行、完成帧改行）在断流后不可久留——改由
  MutationObserver 快照（window.__snaps）取证：帧处理与断流收尾在不同微任务，快照能捕到
  「全部流帧已上屏」那一刻。

场景（截图 .fix-shots/prelim_backend/，console 零报错才 PASS）：
  case1) final b 档（preliminary_final=true）→ 摘徽标、撤思考泡、先行结果留屏、/api/recommend 零调用；
      快照断言：思考泡换句出现过、tool_start 行出现过、完成帧按 label 改行（同行至多 1 行）、未知事件被忽略；
  case2) final a 档（result_payload 随 final 抵达）→ 换屏（prefetched，不再调 /api/recommend）+ sys 留痕；
  case3) final c 档（无新键）→ 现状 runRecommend，/api/recommend 恰好 1 次，落地摘徽标；
  case4) 旧流兼容（无 preliminary 只有 step + 流内 final）→ 徽标从未亮、思考句从未出现、行为与现状一致；
  case5) 观察窗内切交换态 → 徽标随 #resultsHead 在侧栏可见；b 档收尾后徽标摘、布局不回弹；
  case6) 真实 LLM 端到端（仅当项目 .env 有可用 key 且后端已随工作树起服务）：
      真流逐帧观察 preliminary→思考泡→final 三档之一，逐帧截图；后端判哪档不强求，如实记录。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7974），再
    .venv/Scripts/python.exe scripts/smoke_prelim_backend.py [base_url]
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots" / "prelim_backend"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7974"

HIT_QUERY = "人类乳腺癌 FASTQ 数据"
ALT_QUERY = "小鼠肝脏空间转录组数据"

BADGE_VISIBLE = ("() => { const b = document.getElementById('prelimBadge');"
                 " return !!(b && !b.hidden); }")
BTN_ENABLED = ("() => { const b = document.getElementById('submitBtn');"
               " return !!(b && !b.disabled); }")


def _load_real_llm() -> dict | None:
    """从项目 .env 读真实 LLM 配置（只读本机文件，不打印）。读不到可用 key → None（case6 跳过）。"""
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


def _sse(event: str, data) -> str:
    """一帧 SSE，与后端 _sse_line 同形：`data: {"event":…,"data":…}\\n\\n`。"""
    return "data: " + json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n"


async def main() -> int:
    SHOTS.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []
    js_errors: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    # 前置自检：打桩场景走流式路径（streamAgent = AI 执行开 ∧ agent 扩展在）——
    # 扩展缺席时前端根本不发流式请求，后面只会超时出谜语，这里先如实报。
    try:
        with urllib.request.urlopen(BASE + "/api/health", timeout=10) as resp:
            health = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL 前置自检：{BASE}/api/health 不可达 — {exc}")
        return 1
    agent_ext = not ((health.get("extensions") or {}).get("agent") is False)
    check("前置自检：agent 扩展在（流式路径的前提）", agent_ext, json.dumps(health.get("extensions") or {}))
    if not agent_ext:
        return 1

    async with async_playwright() as p:
        browser = await p.chromium.launch()

        async def fresh_page(provider: str | None = "mock"):
            page = await browser.new_page(viewport={"width": 1680, "height": 960})
            page.on("pageerror", lambda e: js_errors.append(f"pageerror: {e}"))
            page.on("console", lambda m: js_errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
            await page.goto(BASE, wait_until="networkidle")
            await page.evaluate("() => { localStorage.setItem('biodata_onboarding_v1', 'done');"
                                " localStorage.removeItem('biodata_sidebar_closed_v1'); }")
            await page.reload(wait_until="networkidle")
            await page.wait_for_timeout(1000)
            await page.evaluate(
                "() => { const r = document.getElementById('cfgRecall'); if (r) r.checked = false;"
                " const s = document.getElementById('cfgStrategy'); if (s) s.checked = false;"
                " const a = document.getElementById('cfgAgentExec'); if (a) a.checked = true; }")
            if provider:
                await page.evaluate(
                    "(v) => { const p = document.getElementById('cfgProvider');"
                    " p.value = v; p.dispatchEvent(new Event('change', { bubbles: true })); }", provider)
            await page.wait_for_timeout(300)
            return page

        async def fetch_payload(page, query: str) -> dict:
            """真实 /api/recommend 响应当 payload 底座（页内 fetch 取，形状零伪造）。"""
            return await page.evaluate(
                """async (q) => {
                    const res = await fetch('/api/recommend', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ query: q, provider: 'mock', use_llm: false,
                                               mock_llm: true, top_k: 3 })
                    });
                    return await res.json();
                }""", query)

        async def install_snaps(page) -> None:
            """MutationObserver 快照：捕断流前「全部流帧已上屏」的瞬态（思考泡换句/pending 行/改行）。"""
            await page.evaluate(
                """() => {
                    window.__snaps = [];
                    const hist = document.getElementById('cbHistory');
                    if (!hist) return;
                    let last = "";
                    new MutationObserver(() => {
                        const b = document.getElementById('prelimBadge');
                        const txt = hist.innerText || "";
                        const key = txt + "|" + (b && !b.hidden);
                        if (key === last) return;
                        last = key;
                        window.__snaps.push({ badge: !!(b && !b.hidden), txt });
                    }).observe(hist, { childList: true, subtree: true, characterData: true });
                }""")

        async def snaps(page) -> list:
            return await page.evaluate("() => window.__snaps || []")

        def make_stream_body(prelim_payload: dict | None, with_final: dict | None) -> str:
            body = ""
            if prelim_payload is not None:
                body += _sse("preliminary", prelim_payload)
            body += _sse("tool_start", {"label_zh": "理解用户意图", "verb": "understand"})
            body += _sse("tool_start", {"label_zh": "重写检索式", "verb": "rewrite"})
            body += _sse("step", {"node": "understand", "label_zh": "理解用户意图",
                                  "ok": True, "detail": "识别为检索"})
            body += _sse("mystery_future_event", {"foo": 1})   # 未知事件：必须被忽略（additive 兼容）
            if with_final is not None:
                body += _sse("final", with_final)
            return body

        async def submit_and_window(page, query: str):
            """发送 → 等观察窗（徽标亮=preliminary 已落地，fallback 正挂在门闩上）。"""
            await page.fill("#queryInput", query)
            await page.click("#submitBtn")
            await page.wait_for_function(BADGE_VISIBLE, timeout=15000)

        async def bubble_text(page) -> str:
            return await page.evaluate("() => (document.getElementById('cbHistory') || {}).innerText || ''")

        # ---------- case1) final b 档：preliminary_final=true ----------
        page = await fresh_page()
        prelim_payload = await fetch_payload(page, HIT_QUERY)
        rec_count = {"n": 0}
        page.on("request", lambda r: rec_count.__setitem__("n", rec_count["n"] + 1)
                if r.url.rstrip("/").endswith("/api/recommend") else None)
        await install_snaps(page)
        gate = asyncio.Event()
        seen = {"stream_body": None}
        final_b = {"ok": True, "route": "search", "query": HIT_QUERY,
                   "result_payload": None, "preliminary_final": True,
                   "suggestions": [], "agent": {"available": True, "used": True}}

        async def on_utterance_s1(route):
            body = route.request.post_data_json or {}
            if body.get("stream"):
                seen["stream_body"] = body
                await route.fulfill(status=200, content_type="text/event-stream",
                                    body=make_stream_body(prelim_payload, None))   # 无 final 断流
                return
            await gate.wait()
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(final_b, ensure_ascii=False))

        await page.route("**/api/utterance", on_utterance_s1)
        await submit_and_window(page, HIT_QUERY)
        await page.wait_for_timeout(600)   # 让断流收尾（fallback 挂门闩）与快照微任务落定
        # 观察窗断言（持久态）
        cards1 = await page.locator("#resultsGrid .card").count()
        check("case1：流式请求真发出且带 ubRouteBody 十参",
              bool(seen["stream_body"]) and all(k in (seen["stream_body"] or {}) for k in
                  ("top_k", "rerank", "recall", "strategy", "polish", "facet_filters",
                   "suppressed_constraints", "lenient_dims")),
              json.dumps(sorted((seen["stream_body"] or {}).keys()), ensure_ascii=False))
        check("case1：观察窗——先行结果上屏（结果卡在）", cards1 > 0, f"cards={cards1}")
        check("case1：观察窗——/api/recommend 零调用（先行结果来自环内，不再发检索）",
              rec_count["n"] == 0, str(rec_count))
        await page.screenshot(path=str(SHOTS / "case1-观察窗-preliminary徽标+先行结果.png"))
        # 快照断言（瞬态）
        sn = await snaps(page)
        check("case1：快照——思考泡换句「正在更深一步思考…」出现过",
              any("正在更深一步思考…" in s["txt"] for s in sn), f"snaps={len(sn)}")
        check("case1：快照——tool_start 的 pending running 行出现过（重写检索式）",
              any("重写检索式" in s["txt"] for s in sn))
        check("case1：快照——完成帧按 label 改行不落新行（「理解用户意图」任一时刻 ≤1 行）",
              all(s["txt"].count("理解用户意图") <= 1 for s in sn))
        gate.set()
        await page.wait_for_function(BTN_ENABLED, timeout=15000)
        await page.wait_for_timeout(800)
        badge_after = await page.evaluate(BADGE_VISIBLE)
        bt1 = await bubble_text(page)
        check("case1：b 档收尾——徽标摘", not badge_after)
        check("case1：b 档收尾——先行结果留屏（初步=最终，不换屏）",
              await page.locator("#resultsGrid .card").count() > 0)
        check("case1：b 档收尾——全程 /api/recommend 零调用", rec_count["n"] == 0, str(rec_count))
        check("case1：b 档收尾——思考泡撤、无 a 档留痕句",
              "正在更深一步思考…" not in bt1 and "深入思考后找到了更匹配的结果" not in bt1)
        await page.screenshot(path=str(SHOTS / "case1-b档收尾-徽标摘+结果留屏.png"))
        await page.unroute("**/api/utterance", on_utterance_s1)
        await page.close()

        # ---------- case2) final a 档：result_payload 随 final 抵达 → 换屏 ----------
        page = await fresh_page()
        prelim_payload2 = await fetch_payload(page, HIT_QUERY)
        alt_payload = await fetch_payload(page, ALT_QUERY)
        rec2 = {"n": 0}
        page.on("request", lambda r: rec2.__setitem__("n", rec2["n"] + 1)
                if r.url.rstrip("/").endswith("/api/recommend") else None)
        gate2 = asyncio.Event()
        final_a = {"ok": True, "route": "search", "query": HIT_QUERY,
                   "result_payload": alt_payload, "preliminary_final": False,
                   "suggestions": [], "agent": {"available": True, "used": True}}

        async def on_utterance_s2(route):
            body = route.request.post_data_json or {}
            if body.get("stream"):
                await route.fulfill(status=200, content_type="text/event-stream",
                                    body=make_stream_body(prelim_payload2, None))
                return
            await gate2.wait()
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(final_a, ensure_ascii=False))

        await page.route("**/api/utterance", on_utterance_s2)
        await submit_and_window(page, HIT_QUERY)
        await page.wait_for_timeout(600)
        grid_before = await page.evaluate(
            "() => (document.getElementById('resultsGrid') || {}).innerText || ''")
        gate2.set()
        await page.wait_for_function(BTN_ENABLED, timeout=15000)
        await page.wait_for_timeout(1000)
        grid_after = await page.evaluate(
            "() => (document.getElementById('resultsGrid') || {}).innerText || ''")
        bt2 = await bubble_text(page)
        check("case2：a 档——换屏（环内采纳 payload 替换先行结果）",
              grid_after != grid_before and len(grid_after.strip()) > 0)
        check("case2：a 档——全程 /api/recommend 零调用（prefetched 合流）", rec2["n"] == 0, str(rec2))
        check("case2：a 档——sys 留痕「深入思考后找到了更匹配的结果，已更新。」",
              "深入思考后找到了更匹配的结果，已更新。" in bt2)
        check("case2：a 档——徽标摘（换屏落地即摘）", not await page.evaluate(BADGE_VISIBLE))
        await page.screenshot(path=str(SHOTS / "case2-a档换屏+留痕.png"))
        await page.unroute("**/api/utterance", on_utterance_s2)
        await page.close()

        # ---------- case3) final c 档：无新键 → 现状 runRecommend ----------
        page = await fresh_page()
        prelim_payload3 = await fetch_payload(page, HIT_QUERY)
        rec3 = {"n": 0}
        page.on("request", lambda r: rec3.__setitem__("n", rec3["n"] + 1)
                if r.url.rstrip("/").endswith("/api/recommend") else None)
        gate3 = asyncio.Event()
        final_c = {"ok": True, "route": "search", "query": HIT_QUERY,
                   "suggestions": [], "agent": {"available": True, "used": True}}   # 无新键（旧 final 同形）

        async def on_utterance_s3(route):
            body = route.request.post_data_json or {}
            if body.get("stream"):
                await route.fulfill(status=200, content_type="text/event-stream",
                                    body=make_stream_body(prelim_payload3, None))
                return
            await gate3.wait()
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(final_c, ensure_ascii=False))

        await page.route("**/api/utterance", on_utterance_s3)
        await submit_and_window(page, HIT_QUERY)
        await page.wait_for_timeout(600)
        gate3.set()
        await page.wait_for_function(BTN_ENABLED, timeout=30000)
        await page.wait_for_timeout(1000)
        check("case3：c 档——现状 runRecommend，/api/recommend 恰好 1 次", rec3["n"] == 1, str(rec3))
        check("case3：c 档——落地摘徽标", not await page.evaluate(BADGE_VISIBLE))
        check("case3：c 档——结果在屏", await page.locator("#resultsGrid .card").count() > 0)
        await page.screenshot(path=str(SHOTS / "case3-c档-现状重检落地.png"))
        await page.unroute("**/api/utterance", on_utterance_s3)
        await page.close()

        # ---------- case4) 旧流兼容：无 preliminary，step + 流内 final ----------
        page = await fresh_page()
        rec4 = {"n": 0}
        page.on("request", lambda r: rec4.__setitem__("n", rec4["n"] + 1)
                if r.url.rstrip("/").endswith("/api/recommend") else None)
        await install_snaps(page)
        final_old = {"ok": True, "route": "search", "query": HIT_QUERY,
                     "suggestions": [], "agent": {"available": True, "used": True}}

        async def on_utterance_s4(route):
            body = route.request.post_data_json or {}
            if body.get("stream"):
                await route.fulfill(status=200, content_type="text/event-stream",
                                    body=make_stream_body(None, final_old))   # 完整旧式流
                return
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(final_old, ensure_ascii=False))

        await page.route("**/api/utterance", on_utterance_s4)
        await page.fill("#queryInput", HIT_QUERY)
        await page.click("#submitBtn")
        await page.wait_for_function(BTN_ENABLED, timeout=30000)
        await page.wait_for_timeout(1000)
        sn4 = await snaps(page)
        check("case4：旧流——徽标从未亮", not any(s["badge"] for s in sn4))
        check("case4：旧流——思考句「正在更深一步思考…」从未出现",
              not any("正在更深一步思考…" in s["txt"] for s in sn4))
        check("case4：旧流——step 帧照旧上屏（无 tool_start 时回落 append）",
              any("理解用户意图" in s["txt"] for s in sn4))
        check("case4：旧流——c 档照旧重检（/api/recommend 恰好 1 次）", rec4["n"] == 1, str(rec4))
        check("case4：旧流——结果在屏", await page.locator("#resultsGrid .card").count() > 0)
        await page.unroute("**/api/utterance", on_utterance_s4)
        await page.close()

        # ---------- case5) 观察窗内切交换态：徽标随 #resultsHead 在侧栏可见 ----------
        page = await fresh_page()
        prelim_payload5 = await fetch_payload(page, HIT_QUERY)
        gate5 = asyncio.Event()
        final_b5 = dict(final_b)

        async def on_utterance_s5(route):
            body = route.request.post_data_json or {}
            if body.get("stream"):
                await route.fulfill(status=200, content_type="text/event-stream",
                                    body=make_stream_body(prelim_payload5, None))
                return
            await gate5.wait()
            await route.fulfill(status=200, content_type="application/json",
                                body=json.dumps(final_b5, ensure_ascii=False))

        await page.route("**/api/utterance", on_utterance_s5)
        await submit_and_window(page, HIT_QUERY)
        await page.wait_for_timeout(600)
        await page.click("#swSwapBtn")
        await page.wait_for_selector("body.view-swapped", timeout=10000)
        await page.wait_for_timeout(500)
        badge_live = await page.evaluate(
            "() => { const b = document.getElementById('prelimBadge');"
            " return !!(b && !b.hidden && b.offsetParent); }")
        check("case5：交换态观察窗——徽标随结果头部件在侧栏可见", badge_live)
        await page.screenshot(path=str(SHOTS / "case5-交换态-侧栏徽标.png"))
        gate5.set()
        await page.wait_for_function(BTN_ENABLED, timeout=15000)
        await page.wait_for_timeout(800)
        st5 = await page.evaluate(
            "() => ({ badge: !document.getElementById('prelimBadge').hidden,"
            " swapped: document.body.classList.contains('view-swapped'),"
            " sideCards: document.querySelectorAll('#sideWork .card').length })")
        check("case5：b 档收尾——徽标摘", not st5["badge"], str(st5))
        check("case5：收尾后交换态保持（布局不回弹）且结果在侧栏", st5["swapped"] and st5["sideCards"] > 0, str(st5))
        await page.screenshot(path=str(SHOTS / "case5-交换态-b档收尾.png"))
        await page.unroute("**/api/utterance", on_utterance_s5)
        await page.close()

        # ---------- case6) 真实 LLM 端到端（.env 有 key 才跑） ----------
        real = _load_real_llm()
        if not real:
            print("SKIP case6：项目 .env 无可读 key——真实 LLM 端到端未跑（case1-case5 打桩已覆盖三档与兼容路径）")
        else:
            page = await fresh_page(provider=None)
            ui_provider = {"openai-compatible": "compatible"}.get(real["provider"], real["provider"])
            await page.evaluate(
                """(c) => {
                    const set = (id, v) => { const el = document.getElementById(id); if (!el) return;
                        el.value = v; el.dispatchEvent(new Event('change', { bubbles: true })); };
                    set('cfgProvider', c.provider); set('cfgBaseUrl', c.base_url);
                    set('cfgModel', c.model); set('cfgApiKey', c.api_key);
                }""", {"provider": ui_provider, "base_url": real["base_url"],
                       "model": real["model"], "api_key": real["api_key"]})
            await page.wait_for_timeout(300)
            rec6 = {"n": 0}
            page.on("request", lambda r: rec6.__setitem__("n", rec6["n"] + 1)
                    if r.url.rstrip("/").endswith("/api/recommend") else None)
            await install_snaps(page)
            await page.fill("#queryInput", HIT_QUERY)
            await page.click("#submitBtn")
            seen_badge = shot_badge = shot_think = False
            deadline = time.time() + 180
            done = False
            while time.time() < deadline:
                st = await page.evaluate(
                    "() => ({ btn: !!(document.getElementById('submitBtn')"
                    "     && !document.getElementById('submitBtn').disabled),"
                    " badge: (() => { const b = document.getElementById('prelimBadge');"
                    "     return !!(b && !b.hidden); })(),"
                    " txt: (document.getElementById('cbHistory') || {}).innerText || '' })")
                if st["badge"] and not shot_badge:
                    shot_badge = seen_badge = True
                    await page.screenshot(path=str(SHOTS / "case6-真机-preliminary徽标.png"))
                if "正在更深一步思考…" in st["txt"] and not shot_think:
                    shot_think = True
                    await page.screenshot(path=str(SHOTS / "case6-真机-思考泡+行动流.png"))
                if st["btn"]:
                    done = True
                    break
                await page.wait_for_timeout(400)
            bt6 = await bubble_text(page)
            cards6 = await page.locator("#resultsGrid .card").count()
            grade = ("a（环内采纳换屏）" if "深入思考后找到了更匹配的结果" in bt6
                     else "b（preliminary_final 免二次检索）" if rec6["n"] == 0 else "c（现状重检）")
            check("case6：真实端到端收尾（180s 内按钮解禁）", done)
            check("case6：真实端到端结果在屏", cards6 > 0, f"cards={cards6}")
            if seen_badge:
                check("case6：真流 preliminary 徽标亮过、收尾摘",
                      not await page.evaluate(BADGE_VISIBLE))
            else:
                print("INFO case6：本轮未见 preliminary 徽标（后端未发先行帧——可能是路由判定/机械闸未过），"
                      "三档分支以 case1-case5 打桩为准")
            print(f"INFO case6：final 档={grade}，/api/recommend 调用 {rec6['n']} 次")
            await page.screenshot(path=str(SHOTS / "case6-真机-收尾.png"))
            await page.close()

        await browser.close()

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
    sys.exit(asyncio.run(main()))
