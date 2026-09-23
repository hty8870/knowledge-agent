# -*- coding: utf-8 -*-
"""长程多步执行的**集成 Playwright** 验证（用户病例句）。

流程：开「AI 执行」→ 统一框发「检查10x是否有更新，若有则下载下来」→ 等步骤卡与总结 →
截图存档 .fix-shots/。断言（全部亲测亲印，成败如实）：
1. 图内已执行渲染通道生效：出现 step 卡（检查卡；若 decide 发起入库则另有错误/说明卡）
   + 一句总结 + 执行过程 trace（details 里）；
2. **绝不双执行**（硬性判据 = 页面网络请求）：本次会话**没有发出任何** `/api/curate/*`
   请求——若前端拿 plan.steps 又去跑 runner，必然 POST /api/curate/check-updates 等；
   外部库目录文件数前后不变。账本 `agent_exec:` 行增量只作参考打印——
   **账本与用户 7860 实例共享**（同 working tree，uvicorn reload 后那边的新代码也写它），
   不能当严格判据。

用法：先起服务（BIODATA_SKIP_RECALL_WARM=1 PORT=7973 ./.venv/Scripts/python.exe scripts/run_web.py），再
    ./.venv/Scripts/python.exe scripts/smoke_multi_step_playwright.py [base_url]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".fix-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"
UTTERANCE = "检查10x是否有更新，若有则下载下来"
EXT_DIR = ROOT / "database" / "external"
LEDGER = ROOT / ".userdata" / "curate_net_ledger.jsonl"


def _ext_files() -> list[str]:
    return sorted(p.name for p in EXT_DIR.glob("*.json"))


def _agent_exec_ledger_rows() -> list[dict]:
    if not LEDGER.is_file():
        return []
    rows = []
    for ln in LEDGER.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if str(obj.get("endpoint") or "").startswith("agent_exec:"):
            rows.append(obj)
    return rows


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'OK' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    before_files = _ext_files()
    before_rows = _agent_exec_ledger_rows()
    print(f"事前：外部库 {len(before_files)} 个文件；账本 agent_exec 行 {len(before_rows)} 行")

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        curate_calls: list[str] = []
        page.on("request", lambda req: curate_calls.append(req.url)
                if "/api/curate/" in req.url else None)
        page.goto(BASE)
        page.wait_for_timeout(2500)
        try:
            page.click("text=跳过", timeout=3000)
            page.wait_for_timeout(600)
        except Exception:
            pass

        # 「AI 执行」必须开着（默认开；确认一下，不替用户改设置以外的状态）
        page.click("#settingsBtn")
        page.wait_for_timeout(900)
        agent_on = page.evaluate("() => document.getElementById('cfgAgentExec').checked")
        page.click("#settingsClose")
        page.wait_for_timeout(500)
        check("AI 执行已开启", agent_on)

        page.fill("#queryInput", UTTERANCE)
        page.click("#submitBtn")
        # 真 LLM 多程调用 + 10x 在线比对：给足时间。先等第一张卡，再等总结落地。
        page.wait_for_selector(".arx-card", timeout=180000)
        page.wait_for_timeout(4000)

        cards = page.locator(".arx-card")
        n_cards = cards.count()
        check("出现 step 卡（≥1：检查卡；decide 发起入库则 2 张）", n_cards >= 1, f"{n_cards} 张")
        card_texts = [cards.nth(i).inner_text()[:120].replace("\n", " ") for i in range(n_cards)]
        for i, t in enumerate(card_texts):
            print(f"    卡{i + 1}: {t}")
        check("有检查来源更新卡", any("更新" in t for t in card_texts))

        # 总结泡正文（factual）：最后一条 sys 泡（cbh-sys-bubble；排除进度泡 cbh-prog）
        bubbles = page.locator(".cbh-sys-bubble:not(.cbh-prog)")
        summary_text = bubbles.last.inner_text() if bubbles.count() else ""
        print(f"    总结正文: {summary_text[:200]}")
        check("总结如实提到 10x", "10x" in summary_text)
        has_error_card = any("没有完成" in t or "未注册" in t or "暂未接入" in t for t in card_texts)
        print(f"    错误/说明卡在场: {has_error_card}")

        # 执行过程 trace 在总结泡的 details 里（只折不删）——展开再截一张
        page.screenshot(path=str(SHOTS / "multi-step-cards-summary.png"), full_page=True)
        try:
            details = page.locator(".cbh-item.sys details summary")
            for i in range(details.count()):
                details.nth(i).click()
            page.wait_for_timeout(400)
        except Exception:
            pass
        page.screenshot(path=str(SHOTS / "multi-step-trace-details.png"), full_page=True)
        browser.close()

    after_files = _ext_files()
    after_rows = _agent_exec_ledger_rows()
    delta_rows = after_rows[len(before_rows):]
    print(f"事后：外部库 {len(after_files)} 个文件（增量 {len(after_files) - len(before_files)}）；"
          f"账本 agent_exec 行增量 {len(delta_rows)} 行（参考值——账本与用户 7860 实例共享）："
          + json.dumps([{k: r.get(k) for k in ("endpoint", "ok")} for r in delta_rows],
                       ensure_ascii=False))
    check("外部库文件数前后不变（无意外写入）", after_files == before_files)
    check("页面没有发出任何 /api/curate/* 请求（双执行红线：图内已执行，runner 零调用）",
          curate_calls == [], f"{len(curate_calls)} 次：{curate_calls[:3]}")

    print("\n== " + ("全部通过" if not fails else f"有失败：{fails}") + f"；截图在 {SHOTS}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
