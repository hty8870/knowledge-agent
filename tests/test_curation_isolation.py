# -*- coding: utf-8 -*-
"""对话式数据库管护的隔离机械门（仿 test_frozen_eval_isolation.py 的 AST 写法）。

离线确定性闸：运行时检索/评测路径**不新增任何联网代码**——`corpus_curation` 是唯一带联网的管护模块，
必须不被检索/编排/查询解析/官方评测引用；反过来它自己也不得 import 检索三件套。
判据用 **AST import 语句**（不是源码子串），不误伤注释/docstring/字符串字面。

双向钉死：
  * `retriever.py` / `workflow.py` / `query_parser.py` / `scripts/evaluate_recommendation.py`
    不得 import `corpus_curation`（检索/评测热路径保持离线确定性，冻结基线结构性免疫）；
  * `corpus_curation.py` 不得 import `retriever` / `workflow` / `query_parser`。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "dataset_recommender"

HOT_PATH_FILES = [
    SRC / "retrieval" / "retriever.py",
    SRC / "app" / "workflow.py",
    SRC / "retrieval" / "query_parser.py",
    ROOT / "scripts" / "evaluate_recommendation.py",
]
CURATION_PY = SRC / "corpus" / "corpus_curation.py"
RETRIEVAL_MODULES = {"retriever", "workflow", "query_parser"}


def _imported_top_names(path: Path) -> set[str]:
    """收集一个模块全部 import 语句引用的（末段）模块名：Import 的每段与 ImportFrom 的模块名 + 别名。"""
    # utf-8-sig：兼容个别既有文件开头的 BOM（ast.parse 不接受 U+FEFF）。
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.update(part for part in alias.name.split(".") if part)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.update(part for part in node.module.split(".") if part)
            for alias in node.names:
                names.add(alias.name)
    return names


def test_hot_paths_never_import_corpus_curation() -> None:
    for path in HOT_PATH_FILES:
        names = _imported_top_names(path)
        assert "corpus_curation" not in names, (
            f"{path.relative_to(ROOT)} import 了 corpus_curation —— 联网管护代码会泄进检索/评测热路径，"
            "抽掉离线确定性地基（设计约定：运行时检索路径不新增任何联网代码）。"
        )


def test_corpus_curation_never_imports_retrieval_modules() -> None:
    names = _imported_top_names(CURATION_PY)
    leaked = RETRIEVAL_MODULES & names
    assert not leaked, (
        f"corpus_curation.py import 了检索模块 {sorted(leaked)} —— 管护模块必须独立于检索/编排/查询解析，"
        "保持纯函数、可单测、禁网可注入。"
    )
