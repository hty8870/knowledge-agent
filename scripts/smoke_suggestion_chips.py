# -*- coding: utf-8 -*-
"""婉拒候选 chips 的浏览器级冒烟：

拦截 /api/utterance 返回「没听懂 + suggestions」罐头响应 → 断言 chips 渲染进对话流；
点击其中一颗 → 断言该 utterance 被当作用户消息重新入环（又发了一次 /api/utterance，
body 里的 utterance 就是 chip 那句）。

用法：先起服务（run_web.py，端口 7973），再
    .venv/Scripts/python.exe scripts/smoke_suggestion_chips.py [base_url]
"""
from __future__ import annotations

import json
import sys

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7973"

CANNED = {
    "ok": True, "route": "none", "query": "", "plan": {"verb": "none", "source": "llm"},
    "echo_zh": "这句话我没有听懂，什么都没有做。",
    "retrieval": None, "via": "llm", "needs_agent": False,
    "agent": {"available": True, "used": True},
    "suggestions": [
        {"label": "清点库里有什么", "utterance": "清点一下数据库里现在有什么"},
        {"label": "检查来源更新", "utterance": "检查数据库来源有没有更新"},
    ],
}


def main() -> int:
    seen_utterances: list[str] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.set_default_timeout(15000)

        def intercept(route):
            try:
                body = route.request.post_data_json or {}
                seen_utterances.append(str(body.get("utterance") or ""))
            except Exception:
                seen_utterances.append("")
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(CANNED, ensure_ascii=False))

        page.route("**/api/utterance", intercept)
        page.goto(BASE, wait_until="domcontentloaded")
        page.wait_for_selector("#queryInput", state="visible")

        # 首屏主框发一句（会被拦截成罐头 none 响应）
        page.fill("#queryInput", "今天天气怎么样")
        page.click("#submitBtn")
        # 等罐头回音 + chips 渲染进对话流
        page.wait_for_selector("[data-act-say]", state="visible")
        chips = page.eval_on_selector_all("[data-act-say]", "els => els.map(e => e.textContent.trim())")
        assert chips == ["清点库里有什么", "检查来源更新"], chips
        # 流式档会先试 SSE、拿到非流式罐头后按同一句回退重发一次（req_id 幂等设计）——
        # 故同一句话可能记 1~2 次；钉的是「发的就是用户那句」，次数随流式回退真相走。
        assert seen_utterances and set(seen_utterances) == {"今天天气怎么样"}, seen_utterances

        # 点第一颗 chip → 该句当作用户消息重新入环（后续 utterance 请求换成 chip 那句）
        before = len(seen_utterances)
        page.click("[data-act-say] >> nth=0")
        page.wait_for_function(
            f"document.querySelectorAll('[data-act-say]').length >= 2")  # 第二轮回音也带 chips
        assert len(seen_utterances) > before, seen_utterances
        assert seen_utterances[-1] == "清点一下数据库里现在有什么", seen_utterances
        browser.close()
    print("SUGGESTION CHIPS SMOKE PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
