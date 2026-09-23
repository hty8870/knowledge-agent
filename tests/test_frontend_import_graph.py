# -*- coding: utf-8 -*-
"""前端**全模块** import 图门。

既有 `test_frontend_boot_contract.py` 只完整检查 boot.js 一个文件；本门把同样双端断言
（specifier 可解析 ∧ import 的名字在目标模块 export 里真实存在）推广到全部一方 JS，
并额外钉两件事：

1. **import 环成员只许缩不许涨**。 切断 core→board 反向边后，前端静态环
   从 18 模块 SCC 降到 13+2 两个。「模块求值期互不触碰」此前全靠人肉纪律——环成员
   集合进 allowlist，任何新模块入环当场红（缩环不用改测试）。
2. core→board 反向边不得回潮（回潮即环重新长回 18 模块）。

无构建原生 ESM 的代价就是这类错位要等浏览器 evaluate 才炸；本门把它提前到 pytest。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "web" / "static"
JS_DIR = STATIC / "js"
PKG_IMPORTS: dict = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["imports"]

_IMPORT_RE = re.compile(r'import\s*\{([^}]*)\}\s*from\s*"(#[A-Za-z_0-9]+)"\s*;', re.S)
_EXPORT_DECL = re.compile(
    r"^\s*export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)", re.M)

#: 当前环成员 allowlist（首测：13 模块 SCC + core↔usage_log 2 模块 SCC；同日
#:  断 browse↔fav_folders 后复测：11 模块 SCC + cards↔fav_folders / core↔usage_log
#: 两个 2 模块 SCC）。只许缩小（成员减少不用改本表），任何**新**模块入环 = 红灯。
_CYCLE_ALLOWLIST = {
    "accounts", "act", "board", "browse", "cards", "core", "facets", "fav_folders",
    "interactions", "memory", "results", "search", "shell", "task_pack", "usage_log",
}


def _importmap_keys(page: str) -> dict:
    html = (STATIC / page).read_text(encoding="utf-8")
    m = re.search(r'<script type="importmap">(.*?)</script>', html, re.S)
    assert m, f"{page} 缺 importmap"
    return json.loads(m.group(1))["imports"]


def _graph() -> tuple[dict[str, set[str]], dict[str, Path]]:
    """返回（模块名 → 它 import 的模块名集合, 模块名 → 路径）。"""
    files = sorted(JS_DIR.glob("**/*.js"))
    by_stem = {p.stem: p for p in files}
    graph: dict[str, set[str]] = {}
    for p in files:
        deps = set()
        for m in _IMPORT_RE.finditer(p.read_text(encoding="utf-8")):
            spec = m.group(2)  # 含 # 前缀（与 package.json/importmap 键同形）
            assert spec in PKG_IMPORTS, f"{p.name}: specifier {spec} 不在 package.json imports"
            target = PKG_IMPORTS[spec]
            assert (ROOT / target).is_file(), f"{p.name}: {spec} 映射的 {target} 不存在"
            deps.add(Path(target).stem)
        graph[p.stem] = deps
    return graph, by_stem


def _sccs(graph: dict[str, set[str]]) -> list[set[str]]:
    """Tarjan SCC（迭代版，避免递归深度问题）。"""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    out: list[set[str]] = []
    counter = [0]

    for start in graph:
        if start in index:
            continue
        work = [(start, iter(sorted(graph[start])))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, it = work[-1]
            advanced = False
            for nxt in it:
                if nxt not in graph:
                    continue
                if nxt not in index:
                    index[nxt] = low[nxt] = counter[0]
                    counter[0] += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    work.append((nxt, iter(sorted(graph[nxt]))))
                    advanced = True
                    break
                if nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                scc = set()
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.add(w)
                    if w == node:
                        break
                if len(scc) > 1:
                    out.append(scc)
    return out


def test_every_import_name_exists_in_target_exports() -> None:
    """全部一方 JS 的具名 import 双端断言（boot 门的全模块推广）。"""
    _graph_checked = _graph()[0]
    for path in sorted(JS_DIR.glob("**/*.js")):
        text = path.read_text(encoding="utf-8")
        for m in _IMPORT_RE.finditer(text):
            spec = m.group(2)  # 含 # 前缀
            target = ROOT / PKG_IMPORTS[spec]
            exports = set(_EXPORT_DECL.findall(target.read_text(encoding="utf-8")))
            for raw in m.group(1).split(","):
                name = raw.strip()
                if not name:
                    continue
                name = name.split(" as ")[0].strip()
                assert name in exports, (
                    f"{path.relative_to(ROOT)} 从 #{spec} import 的「{name}」"
                    f"在 {target.relative_to(ROOT)} 的 export 里不存在——"
                    "浏览器 evaluate 期才炸的错位，被本门提前拦下")


def test_both_pages_importmap_cover_all_used_specifiers() -> None:
    """两页 importmap 必须覆盖全部用到的 specifier 且映射一致（dataset 页缺键=整页空白）。"""
    index_map = _importmap_keys("index.html")
    dataset_map = _importmap_keys("dataset.html")
    used = set()
    for path in sorted(JS_DIR.glob("**/*.js")):
        for m in _IMPORT_RE.finditer(path.read_text(encoding="utf-8")):
            used.add(m.group(2))
    for spec in sorted(used):
        assert spec in index_map, f"index.html importmap 缺 {spec}"
        assert spec in dataset_map, f"dataset.html importmap 缺 {spec}"
        assert index_map[spec].split("?")[0] == dataset_map[spec].split("?")[0], (
            f"{spec} 两页映射不一致：{index_map[spec]} vs {dataset_map[spec]}")


def test_import_cycles_only_shrink() -> None:
    """SCC 成员 ⊆ allowlist（只缩不涨）；core→board 反向边不得回潮。"""
    graph, _ = _graph()
    assert "board" not in graph.get("core", set()), "core→board 反向边回潮（应走 setHistHooks 注册反转）"
    assert "browse" not in graph.get("fav_folders", set()), "fav_folders→browse 反向边回潮（应走 setFavRerender 注册反转）"
    members = set().union(*_sccs(graph)) if _sccs(graph) else set()
    new_members = members - _CYCLE_ALLOWLIST
    assert not new_members, f"新模块进入 import 环：{sorted(new_members)}（环只许缩不许涨）"
