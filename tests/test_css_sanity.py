# -*- coding: utf-8 -*-
"""CSS 结构健全门：括号配平扫描（新增）。

事故背景：一次 heredoc 截断把 `app.css` 里 `.hp-meta` 规则的
`color: var(--muted);` 写成了 `color: var(--m`（括号未闭合）。CSS 解析器遇到
未闭合括号会**丢弃其后所有规则**——于是从断点到文件尾约 130 行样式（阶梯 chips、
coachmark、任务卡说明、wd 面板、pex 导出区）全部静默失效。这个 bug 类：
- `node --check` 管不着（不是 JS）；
- web_smoke 只查字符串在场，查不出「规则被解析器丢弃」；
- 视觉走查才能发现，但视觉走查不是每次都跑。

三次合并都没抓到它，说明缺一道门。本门做的事刻意保持极简（无第三方依赖）：
剥掉注释与字符串后，对 `web/static/css/*.css` 逐文件做 `{}`/`()`/`[]` 配平扫描，
报出第一个失衡点的行列号。它不验证语义（选择器是否写错、属性是否存在不归它管），
只钉死「解析器不会因为括号未闭合而静默丢弃尾部规则」这一底线。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CSS_DIR = ROOT / "web" / "static" / "css"

_PAIRS = {"}": "{", ")": "(", "]": "["}
_OPEN = set(_PAIRS.values())


def _strip_comments_and_strings(text: str) -> str:
    """把 /* ... */ 注释与引号字符串替换为等长空白（保行列号，便于定位）。"""
    out = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join("\n" if c == "\n" else " " for c in text[i:j]))
            i = j
        elif ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append("".join("\n" if c == "\n" else " " for c in text[i:j]))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _first_imbalance(text: str):
    """返回 (行, 列, 说明)；完全配平返回 None。行列均 1 起。"""
    stack = []  # (开括号, 行, 列)
    line, col = 1, 0
    for ch in _strip_comments_and_strings(text):
        col += 1
        if ch == "\n":
            line, col = line + 1, 0
            continue
        if ch in _OPEN:
            stack.append((ch, line, col))
        elif ch in _PAIRS:
            if not stack or stack[-1][0] != _PAIRS[ch]:
                return (line, col, f"闭合符 {ch!r} 没有匹配的开括号")
            stack.pop()
    if stack:
        ch, l, c = stack[-1]
        return (l, c, f"开括号 {ch!r} 直到文件尾未闭合（其后规则会被解析器静默丢弃）")
    return None


def test_css_files_exist():
    assert CSS_DIR.is_dir(), f"缺目录：{CSS_DIR}"
    assert list(CSS_DIR.glob("*.css")), "web/static/css/ 下没有任何 .css"


def test_css_brackets_balanced():
    failures = []
    for path in sorted(CSS_DIR.glob("*.css")):
        hit = _first_imbalance(path.read_text(encoding="utf-8"))
        if hit:
            line, col, msg = hit
            failures.append(f"{path.name}:{line}:{col} {msg}")
    assert not failures, "CSS 括号失衡（解析器会丢弃断点之后的全部规则）：\n" + "\n".join(failures)
