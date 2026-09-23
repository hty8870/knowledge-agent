# -*- coding: utf-8 -*-
""" Playwright 冒烟：执行侧 Agent 化后的前端契约。

覆盖：
1. 设置里 `#cfgAgentExec`（合并后的「AI 执行」开关）存在、默认勾选；
2. tool 路由 curate.check_updates → runner 自动执行 → 结果卡进对话流
   （/api/utterance 与 /api/curate/check-updates 均 route-stub，零网络、确定性）；
3. plan.trace 的后端真实步骤出现在行动流（「Agent 规划」标注）；
4. 截图存档到 .-shots/。

用法：先起服务（run_web.py 或 uvicorn，端口 7973），再
    .venv/Scripts/python.exe scripts/smoke_ui_playwright.py [base_url]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / ".-shots"
BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

UTTERANCE_RESP = {
    "ok": True,
    "route": "tool",
    "query": "",
    "plan": {
        "verb": "curate.check_updates",
        "verb_zh": "检查来源更新",
        "kind": "exec",
        "requires_results": False,
        "slots": {"source": "10x"},
        "quoted": "检查10x是否有更新",
        "confidence": "high",
        "source": "agent",
        "cancelled": False,
        "blocked_reason": "",
        "uncertainty_zh": "以上这几项是大模型从你这句话里读出来的，本工具没有另外核对。",
        "trace": [
            {"node": "understand", "label_zh": "理解意图",
             "detail": "工具调用模式，判为 curate.check_updates。", "ok": True, "ms": 812},
            {"node": "validate", "label_zh": "护栏校验",
             "detail": "通过：curate.check_updates（检查来源更新）。", "ok": True, "ms": 2},
            {"node": "narrate", "label_zh": "生成说明",
             "detail": "curate.check_updates（检查来源更新）", "ok": True, "ms": 1},
        ],
    },
    "echo_zh": "",
    "retrieval": {"status": "no_match", "total": 0, "top_titles": []},
    "via": "agent",
    "agent": {"available": True, "used": True},
}

CHECK_UPDATES_RESP = {
    "ok": True,
    "result": {
        "checked_at": "2026-08-03T12:00:00",
        "sources": [
            {
                "source": "10x",
                "mode": "snapshot",
                "snapshot_date": "2026-04-28",
                "local_count": 774,
                "site_url": "https://www.10xgenomics.com/datasets",
                "note_zh": "这是离线快照，本工具不能在线比对；可到官网核对，或说「联网搜…」找新数据。",
            }
        ],
        "hint_zh": "在线比对目前只覆盖 ArrayExpress；其余来源给本地快照信息。",
    },
}


def main() -> int:
    SHOTS.mkdir(exist_ok=True)
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1680, "height": 960})

        def _utterance_route(r):
            # 首次检索句放行给真后端（真实激活侧栏）；管护句回 stub（确定性、零网络）。
            try:
                said = json.loads(r.request.post_data or "{}").get("utterance", "")
            except Exception:
                said = ""
            if "检查10x" in str(said):
                r.fulfill(status=200, content_type="application/json",
                          body=json.dumps(UTTERANCE_RESP, ensure_ascii=False))
            else:
                r.fallback()

        page.route("**/api/utterance", _utterance_route)
        page.route("**/api/curate/check-updates", lambda r: r.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(CHECK_UPDATES_RESP, ensure_ascii=False)))

        page.goto(BASE + "/", wait_until="networkidle")
        # 新手教程若出现先关掉，避免遮挡
        try:
            page.click("text=跳过", timeout=2500)
        except Exception:
            pass

        # 0) 先用真检索激活侧栏对话窗口（统一输入框此时才可见）
        page.fill("#queryInput", "人类乳腺癌数据")
        page.click("#submitBtn")
        try:
            page.wait_for_selector("#chatInput", state="visible", timeout=25000)
        except Exception:
            page.screenshot(path=str(SHOTS / "0-no-chatinput.png"))
            check("首检后 #chatInput 可见", False)
        page.wait_for_timeout(800)

        # 1) AI 执行开关（合并旧 cfgAgent+cfgAutoAct）存在且默认勾选
        agent_box = page.locator("#cfgAgentExec")
        check("#cfgAgentExec 存在", agent_box.count() == 1)
        if agent_box.count():
            check("#cfgAgentExec 默认勾选", agent_box.is_checked())
            in_pipe = page.evaluate(
                "!!document.querySelector('#cfgAgentExec')?.closest('.pipe, #settings')")
            check("#cfgAgentExec 在设置面板内", in_pipe)

        # 2) 发送「检查10x是否有更新」→ runner 自动执行 → 结果卡
        page.fill("#chatInput", "检查10x是否有更新")
        page.click("#chatSendBtn")
        page.wait_for_timeout(3500)
        hist_text = page.inner_text("#cbHistory")
        # 行动流步骤在执行中上屏、完成后折进总结泡 <details class="arx-trace">——
        # 折叠内容 inner_text 不可见，用 textContent 断言契约，再展开截图供视觉核对。
        trace_text = page.eval_on_selector_all(
            "#cbHistory .arx-trace", "els => els.map(e => e.textContent).join('\\n')")
        check("行动流 trace 折叠块存在", "执行过程（" in trace_text, trace_text[:60])
        check("trace 含 Agent 规划步骤（理解意图）", "理解意图" in trace_text)
        check("trace 含护栏校验步骤", "护栏校验" in trace_text)
        check("结果卡：本地条数 774 上屏", "774" in hist_text)
        check("结果卡：快照说明上屏", "快照" in hist_text)
        check("结果卡：官网核对入口", "官网" in hist_text or "10xgenomics" in hist_text)
        page.evaluate("document.querySelectorAll('#cbHistory .arx-trace').forEach(d => d.open = true)")
        page.wait_for_timeout(300)
        page.screenshot(path=str(SHOTS / "1-check-updates-card.png"), full_page=False)

        # 对话窗口局部截图（看卡片细节）
        side = page.locator("#sideWork")
        if side.count():
            side.screenshot(path=str(SHOTS / "2-check-updates-card-zoom.png"))

        # 3) 设置面板里 cfgAgentExec 视觉位
        try:
            page.click("text=设置", timeout=2000)
            page.wait_for_timeout(600)
            page.screenshot(path=str(SHOTS / "3-settings-agent.png"))
        except Exception:
            pass

        browser.close()

    print("-" * 60)
    if fails:
        print(f"SMOKE FAILED: {len(fails)} 项 — {fails}")
        return 1
    print("SMOKE OK： 前端契约全过。截图在 .-shots/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
