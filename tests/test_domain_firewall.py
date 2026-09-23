# -*- coding: utf-8 -*-
"""骨肉分离防火墙门（AST 机械检查，自动枚举版）：

分类（每个 src 文件必居其一，新文件无处可逃）：
- **generic（骨架通用机制）**：不得 import 任何具体领域包（`dataset_recommender.domains.*`）。
  骨架拿领域数据的唯一通道是 `domain.registry`。
- **flesh-in-src（领域代码暂住骨架树，v2 迁出）**：corpus/、content/、retrieval/normalizer.py、
  retrieval/synonyms.py——显式豁免表管理，物理迁出后从豁免表删。
- **pack（domains/ 下的领域包）**：不得 import 骨架编排/接口层（app/agent/llm/corpus/content/retrieval），
  不得 import 其他领域包；只许 `dataset_recommender.domain.*` 与自身。

解析器覆盖 `import x`、`from x import y`（含 `from package import submodule` 形态）与
相对 import（按文件包位置还原绝对模块名），合成反例钉死「违规必被检出」。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_SRC = Path(__file__).resolve().parents[1] / "src" / "dataset_recommender"

FLESH_IN_SRC_DIRS = ("corpus/", "content/")
FLESH_IN_SRC_FILES = {"retrieval/normalizer.py", "retrieval/synonyms.py"}

_FORBIDDEN_PACK_PREFIXES = (
    "dataset_recommender.app",
    "dataset_recommender.agent",
    "dataset_recommender.llm",
    "dataset_recommender.corpus",
    "dataset_recommender.content",
    "dataset_recommender.retrieval",
)


def _all_src_py() -> list[Path]:
    return sorted(p for p in REPO_SRC.rglob("*.py") if "__pycache__" not in p.parts)


def _classify(path: Path) -> str:
    rel = path.relative_to(REPO_SRC).as_posix()
    if rel.startswith("domains/"):
        return "pack"
    if rel.startswith(FLESH_IN_SRC_DIRS) or rel in FLESH_IN_SRC_FILES:
        return "flesh"
    return "generic"


def _imports_from_text(text: str, *, pkg: str, is_init: bool) -> list[str]:
    """解析一段源码的 import 边（绝对模块名；相对 import 按包位置还原）。

    pkg = 文件自身模块名（__init__ 时为所在包名）。"""
    tree = ast.parse(text)
    out: list[str] = []
    anchor = pkg if is_init else pkg.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                base_module = node.module or ""
                # `from package import submodule` 形态：只取 `package.submodule`（一条 import 边；
                # base 包本身不单独算，避免与 `import package` 语义重复计数）。
                out.extend(f"{base_module}.{a.name}" for a in node.names if base_module)
                continue
            parts = anchor.split(".") if anchor else []
            if node.level - 1 > 0:
                parts = parts[: -(node.level - 1)] if len(parts) >= node.level - 1 else []
            base = ".".join(parts)
            if node.module:
                out.append(f"{base}.{node.module}" if base else node.module)
            else:
                out.extend(f"{base}.{a.name}" if base else a.name for a in node.names)
    return out


def _imports(path: Path) -> list[str]:
    rel = path.relative_to(REPO_SRC).with_suffix("")
    parts = ["dataset_recommender", *rel.parts]
    if parts[-1] == "__init__":
        parts.pop()
    return _imports_from_text(
        path.read_text(encoding="utf-8-sig"), pkg=".".join(parts), is_init=path.name == "__init__.py"
    )


def _generic_offenders(modules: list[str]) -> list[str]:
    return [m for m in modules
            if m == "dataset_recommender.domains" or m.startswith("dataset_recommender.domains.")]


def _pack_offenders(modules: list[str], own_pack: str) -> list[str]:
    out = [m for m in modules if m.startswith(_FORBIDDEN_PACK_PREFIXES)]
    for m in modules:
        if m.startswith("dataset_recommender.domains.") and not m.startswith(f"dataset_recommender.domains.{own_pack}"):
            out.append(m)
    return out


def test_every_src_file_is_classified():
    classes = {p: _classify(p) for p in _all_src_py()}
    assert classes, "src 树为空？"
    # 豁免表不许失效（文件不存在时立刻红，防「豁免了一个已迁走的文件」）
    for rel in FLESH_IN_SRC_FILES:
        assert (REPO_SRC / rel).is_file(), f"flesh 豁免表里的文件不存在: {rel}"


def test_skeleton_generic_files_do_not_import_domain_packs():
    offenders: list[str] = []
    for path in _all_src_py():
        if _classify(path) != "generic":
            continue
        hits = _generic_offenders(_imports(path))
        offenders.extend(f"{path.relative_to(REPO_SRC)} -> {m}" for m in hits)
    assert not offenders, "骨架通用文件 import 了具体领域包:\n" + "\n".join(offenders)


def test_domain_packs_do_not_import_orchestration_or_other_packs():
    offenders: list[str] = []
    for path in _all_src_py():
        if _classify(path) != "pack":
            continue
        own_pack = path.relative_to(REPO_SRC).parts[1]
        hits = _pack_offenders(_imports(path), own_pack)
        offenders.extend(f"{path.relative_to(REPO_SRC)} -> {m}" for m in hits)
    assert not offenders, "领域包 import 了骨架编排层或其他领域包:\n" + "\n".join(offenders)


_SYNTHETIC_GENERIC_CASES = [
    # (源码, 期望违规命中数) —— 违规写法必须被检出，合法写法必须为 0
    ("from dataset_recommender.domains.biodata import vocabulary\n", 1),
    ("import dataset_recommender.domains.biodata.vocabulary\n", 1),
    ("from ..domains.biodata import vocabulary\n", 1),
    ("from . import vocabulary\n", 0),
    ("from dataset_recommender import domains\n", 1),
    ("from dataset_recommender.domain.registry import get_domain\n", 0),
]

_SYNTHETIC_PACK_CASES = [
    ("from ...app import workflow\n", 1),
    ("from ...domain.pack import DomainPack\n", 0),
    ("from ...domains.course_kb import pack\n", 1),
    ("from . import vocabulary\n", 0),
    ("from dataset_recommender.domains.biodata import dimensions\n", 0),
]


@pytest.mark.parametrize(("src_text", "expected"), _SYNTHETIC_GENERIC_CASES)
def test_generic_rule_flags_violations(src_text: str, expected: int):
    """解析器+规则的反例钉（骨架侧，站在 retrieval/query_parser.py 位置）。"""
    mods = _imports_from_text(src_text, pkg="dataset_recommender.retrieval.query_parser", is_init=False)
    assert len(_generic_offenders(mods)) == expected


@pytest.mark.parametrize(("src_text", "expected"), _SYNTHETIC_PACK_CASES)
def test_pack_rule_flags_violations(src_text: str, expected: int):
    """解析器+规则的反例钉（领域包侧，站在 domains/biodata/pack.py 位置）。"""
    mods = _imports_from_text(src_text, pkg="dataset_recommender.domains.biodata.pack", is_init=False)
    assert len(_pack_offenders(mods, "biodata")) == expected


def test_relative_import_resolution_on_real_files():
    """真实文件的相对 import 还原钉。"""
    qp = REPO_SRC / "retrieval" / "query_parser.py"
    mods = _imports(qp)
    assert "dataset_recommender.retrieval.query_lexicon" in mods
    assert "dataset_recommender.domain.registry" in mods
    pack = REPO_SRC / "domains" / "biodata" / "pack.py"
    pack_mods = _imports(pack)
    assert "dataset_recommender.domain.pack" in pack_mods
    assert "dataset_recommender.domains.biodata.dimensions" in pack_mods
