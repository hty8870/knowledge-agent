# -*- coding: utf-8 -*-
"""前端加载期跨模块引用门：boot.js 已是 ES Module（全应用唯一入口）。

旧判据（经典脚本时代沿用）：收集所有模块顶层定义名当作共享命名空间，断言 boot.js
`init()` 里每个 bare 调用都能在其中解析。经典脚本靠**运行时**在共享命名空间里解析跨模块函数，
`node --check` 不解析跨文件引用、web_smoke 不执行 JS，「改了 shell.js 的函数名却漏改 boot.js」
这类 producer/consumer 错位要等 DOMContentLoaded 才 ReferenceError、交互整片空白。

新判据（ESM）：同一个不变量提前到**模块加载期**——import 一个目标模块不存在的名字，
浏览器在 evaluate 阶段就直接抛 SyntaxError，根本到不了 DOMContentLoaded。本门静态钉死它：
  ① boot.js 的每个 `import { … } from "#xxx"` 的 specifier 必须在根 package.json 的
     `imports` 表（与 index.html importmap 同键）里解析到一个真实存在的文件；
  ② 每个 import 的名字必须在目标模块的 `export` 声明里真的存在（双端断言，改名即红）；
  ③ `init()` 函数体里每个 bare 调用都必须来自这些 import（防止「忘了 import 就调」）。
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS_DIR = ROOT / "web" / "static" / "js"
BOOT = JS_DIR / "core" / "boot.js"
PKG_IMPORTS: dict = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["imports"]

# 宿主/第三方全局：init() 当前不直接调这些，保留作为将来 init 扩展的兜底 allowlist。
HOST_GLOBALS = {
    "window", "document", "console", "fetch", "alert", "confirm", "prompt",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "matchMedia", "structuredClone",
    "gsap", "ScrollTrigger", "$",
    "Array", "Object", "JSON", "Math", "Number", "String", "Boolean", "Date", "RegExp",
    "Set", "Map", "Promise", "URLSearchParams", "parseInt", "parseFloat", "isNaN",
    "encodeURIComponent", "decodeURIComponent",
}

# import { a, b as c } from "#xxx";（跨行也认）
_IMPORT_RE = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"(#[A-Za-z_]+)"\s*;', re.S)
# export function/foo、export async function、export const/let/var/class 的声明名
_EXPORT_DECL = re.compile(r"^\s*export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)
# export { a, b as c } 的列表式导出
_EXPORT_LIST = re.compile(r"^\s*export\s*\{([^}]*)\}\s*;?", re.M)
# bare 调用：标识符后紧跟 (，且前面不是 . 或标识符字符（排除 obj.method()）
_CALL = re.compile(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(")


def _import_pairs(boot_text: str) -> list[tuple[str, str, str]]:
    """boot.js 的 import 明细：[(specifier, 目标模块导出名, boot 内的本地名), …]。"""
    pairs = []
    for names_blob, spec in _IMPORT_RE.findall(boot_text):
        for piece in names_blob.split(","):
            piece = piece.strip()
            if not piece:
                continue
            m = re.match(r"^([A-Za-z_$][\w$]*)(?:\s+as\s+([A-Za-z_$][\w$]*))?$", piece)
            assert m, f"boot.js import 子句解析失败：{piece!r}"
            pairs.append((spec, m.group(1), m.group(2) or m.group(1)))
    return pairs


def _exports_of(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    names = set(_EXPORT_DECL.findall(text))
    for blob in _EXPORT_LIST.findall(text):
        for piece in blob.split(","):
            piece = piece.strip()
            if not piece:
                continue
            # `a as c` 对外暴露的是 c
            names.add(piece.split(" as ")[-1].strip())
    return names


def _init_body(boot_text: str) -> str:
    m = re.search(r"function\s+init\s*\(\)\s*\{(.*?)\}", boot_text, re.S)
    assert m, "boot.js 未找到 init() 函数体"
    return m.group(1)


def test_boot_import_specifiers_resolve_via_package_imports() -> None:
    pairs = _import_pairs(BOOT.read_text(encoding="utf-8"))
    assert pairs, "boot.js 没有解析出任何 import（它必须是纯 ESM 入口——解析失真？）"
    for spec, _exported, _local in pairs:
        assert spec in PKG_IMPORTS, (
            f"boot.js import 了 {spec}，但根 package.json 的 imports 表没有它"
            "（importmap ↔ package.json 同键，漏了一端）"
        )
        target = (ROOT / PKG_IMPORTS[spec]).resolve()
        assert target.is_file(), f"{spec} 在 package.json 里指向 {PKG_IMPORTS[spec]}，文件不存在"


def test_every_boot_import_name_exists_in_the_target_modules_exports() -> None:
    pairs = _import_pairs(BOOT.read_text(encoding="utf-8"))
    missing = []
    for spec, exported, _local in pairs:
        target = (ROOT / PKG_IMPORTS[spec]).resolve()
        if not target.is_file():
            continue   # 上一条门负责报这个
        if exported not in _exports_of(target):
            missing.append(f"{exported}（来自 {spec} → {target.name}）")
    assert not missing, (
        f"boot.js import 了这些目标模块没有导出的名字：{missing}。"
        "浏览器会在模块加载期直接抛 SyntaxError、整页交互空白——这正是本门要钉死的错位。"
    )


def test_init_calls_are_all_imported() -> None:
    boot_text = BOOT.read_text(encoding="utf-8")
    imported = {local for _spec, _exported, local in _import_pairs(boot_text)}
    called = set(_CALL.findall(_init_body(boot_text)))
    assert called, "boot.js init() 未解析出任何函数调用（解析失真？）"
    unresolved = sorted(c for c in called if c not in imported and c not in HOST_GLOBALS)
    assert not unresolved, (
        f"boot.js init() 调了这些没有 import 的标识符：{unresolved}。"
        "boot 是纯 ESM：忘了 import 的名字在浏览器里是 ReferenceError（ESM 不读 window 桥）。"
    )
