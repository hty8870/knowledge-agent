# -*- coding: utf-8 -*-
r"""前瞻回归门：任何把数据派生值整值插入 href 的 JS（`href="${...}"`）都必须引用 `isHttp` 方案守卫。

现状（已核实安全）：`web/static/js/search/cards.js` 的每个 href 出口都 `isHttp(...)` 门控 + `escapeHtml`，
`core.js` 的 `isHttp=/^https?:\/\//i` 挡掉 `javascript:`/`data:` 等非 http 方案 → 存储型恶意 URL
无法变成可点击的危险链接，无现存漏洞。此门不是修漏，而是**钉死这个不变量**：将来别的 JS 文件新开
一个未经 isHttp 守卫的 href 出口时立刻红——否则三门全绿也发现不了（web_smoke 只做静态字符串在场
检查、从不执行 JS）。判据保守（只针对 `href="${` 这种**整值插值**的危险模式，`href="/path/${id}"`
这类同源相对路径插值不误伤）。
"""
import re
from pathlib import Path

JS_DIR = Path(__file__).resolve().parents[1] / "web" / "static" / "js"
# 只匹配「引号后紧跟 ${」的整值插值 —— 数据派生完整 URL 的危险写法；
# 相对路径插值（href="/api/x/${id}"）引号后是 `/` 不匹配，故不误伤同源链接。
HREF_SINK = re.compile(r"""href\s*=\s*["'`]\$\{""")
# 每个 sink 之前这么多字符内必须出现 isHttp( 守卫调用（覆盖 cards.js 的三元 `isHttp(x) ? `<a href=…`）。
# 用 per-sink 邻域判据而非 per-file「文件里有没有 isHttp」——后者会让已含 isHttp 的 cards.js 里新增一个
# 无守卫 sink 静默漏判。要求带括号的 isHttp( 而非裸 isHttp，避开 `// no isHttp needed` 之类注释规避。
GUARD_WINDOW = 240


def test_scheme_guard_is_http_only() -> None:
    core = (JS_DIR / "core" / "core.js").read_text(encoding="utf-8")
    assert "function isHttp" in core, "core.js 未定义 isHttp 方案守卫"
    assert re.search(r"\^https\?:", core), "isHttp 未限定 http/https 方案（应为 /^https?:\\/\\//）"


def _unguarded_sink_lines(content: str) -> list[int]:
    """返回内容里「前方 GUARD_WINDOW 字符内没有 isHttp( 守卫」的 href sink 行号。"""
    out: list[int] = []
    for match in HREF_SINK.finditer(content):
        window = content[max(0, match.start() - GUARD_WINDOW):match.start()]
        if "isHttp(" not in window:
            out.append(content.count("\n", 0, match.start()) + 1)
    return out


def test_dynamic_href_sinks_are_scheme_guarded_per_sink() -> None:
    offenders = []
    for js in sorted(JS_DIR.glob("*/*.js")):
        content = js.read_text(encoding="utf-8")
        offenders += [f"{js.name}:{line}" for line in _unguarded_sink_lines(content)]
    assert not offenders, (
        f"这些位置把数据派生值整值插进 href，但其前方 {GUARD_WINDOW} 字符内没有 isHttp( 方案守卫：{offenders}。"
        "任何由 url/download_url 等数据字段构造的 href 必须先过 isHttp（挡 javascript:/data: 等非 http 方案），"
        "否则存储型恶意 URL 可成为可点击危险链接。"
    )


def test_per_sink_guard_catches_unguarded_sink_even_when_isHttp_elsewhere() -> None:
    guarded = 'const a = isHttp(u) ? `<a href="${u}">ok</a>` : "";\n'
    unguarded = 'const b = `<a href="${it.download_url}">bad</a>`;\n'
    filler = "// padding line to push the new sink past the guard window\n" * 8
    assert _unguarded_sink_lines(guarded) == []                 # 有守卫 → 不判红
    assert _unguarded_sink_lines(unguarded)                     # 无守卫 → 判红
    assert _unguarded_sink_lines(guarded + filler + unguarded)  # 别处有 isHttp( 也不放过远处无守卫 sink
    assert _unguarded_sink_lines("// no isHttp needed\n" + unguarded)  # 注释里的 isHttp（无括号）不算守卫
