# -*- coding: utf-8 -*-
"""一次性交付工具：用 Playwright 把 HTML 打成 PDF（浏览器 Ctrl+P 会丢设计元素，page.pdf 不会）。

用法：./.venv/Scripts/python.exe scripts/print_pdf.py <输入.html> <输出.pdf>
"""
from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(src.resolve().as_uri(), wait_until="networkidle")
        page.pdf(path=str(dst), format="A4", print_background=True,
                 margin={"top": "14mm", "bottom": "14mm", "left": "12mm", "right": "12mm"})
        browser.close()
    print(f"PDF written: {dst} ({dst.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
