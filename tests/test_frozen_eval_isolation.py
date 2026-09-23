# -*- coding: utf-8 -*-
"""把 MODULES.md 的一条隐性架构护栏显性化为门：官方冻结评测 `evaluate_recommendation`
**直调 `DatasetRetriever.retrieve`、且全模块绝不引用 `DatasetRecommendationWorkflow`**。

为什么重要：冻结评测的确定性地基就建立在这条不变量上——strategy 分类器 / auto / LLM 重排都挂在
workflow 层，只要 eval 不经 workflow，它们就**结构性够不到**冻结门，指标不会被非确定性成分污染。
此前没有任何测试钉住它：谁哪天把 eval 改成走 workflow，strategy/LLM 就泄进冻结门、确定性被抽掉。

判据用 **AST**（不是源码子串）：
  * 只看真实代码引用（ast.Name/Attribute/import alias），**不误伤注释/docstring/字符串字面里出现的 Workflow**；
  * 覆盖 `def` 与 `async def`（走 FunctionDef 与 AsyncFunctionDef）；
  * 负向断言作用于**整模块**而非仅 recommend() 函数体——这样即使 recommend() 保留一句 retriever.retrieve()
    再把重排委托给内部 helper，只要该 helper（或任何地方）import/引用了 workflow 类，模块级引用就会被抓到。
"""
import ast
from pathlib import Path

EVAL_PY = Path(__file__).resolve().parents[1] / "scripts" / "evaluate_recommendation.py"
WORKFLOW_CLASS = "DatasetRecommendationWorkflow"


def _module_and_recommend() -> tuple[ast.Module, ast.AST]:
    tree = ast.parse(EVAL_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "recommend":
            return tree, node
    raise AssertionError("scripts/evaluate_recommendation.py 未找到 recommend 函数（def 或 async def）")


def _code_identifiers(node: ast.AST) -> set[str]:
    """收集节点子树里的代码标识符（Name / Attribute 属性名 / import 名与别名）。不含注释/docstring/字符串字面。"""
    names: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, ast.alias):
            names.add(child.name.split(".")[-1])
            if child.asname:
                names.add(child.asname)
    return names


def test_frozen_eval_recommend_directly_calls_retriever() -> None:
    _tree, recommend = _module_and_recommend()
    ids = _code_identifiers(recommend)
    assert "DatasetRetriever" in ids and "retrieve" in ids, (
        "冻结评测 recommend() 必须直调 DatasetRetriever.retrieve（MODULES.md 确定性不变量）"
    )


def test_frozen_eval_module_never_references_workflow() -> None:
    tree, _recommend = _module_and_recommend()
    ids = _code_identifiers(tree)
    assert WORKFLOW_CLASS not in ids, (
        f"scripts/evaluate_recommendation.py 在代码里引用了 {WORKFLOW_CLASS} —— strategy/auto/LLM 会经 workflow "
        "泄进冻结门、抽掉确定性地基。官方评测必须绕开 workflow 直调 retriever（它调用的任何 helper 也不得引用 "
        "workflow 类）。"
    )
