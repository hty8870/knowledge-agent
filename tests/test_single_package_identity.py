# -*- coding: utf-8 -*-
"""包身份单一守卫。

历史问题：测试一半 `src.dataset_recommender.*`（repo 根在 sys.path 时的命名空间包）、
一半 `dataset_recommender.*`——同一物理文件被导入成**两个模块对象**，模块级单例
（LangGraph 编译单例、锁、缓存）与 monkeypatch 一式两份，曾造成「scoped 绿、全量红」，
conftest 被迫「双名同 stub」。现全仓统一为 `dataset_recommender.*`，本测试机械防回潮。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_no_src_package_imports_anywhere():
    """tests/ 与 scripts/ 的 .py 不得再出现旧包身份的 import 或模块名字符串。"""
    offenders = []
    patterns = (
        "from src" + ".dataset_recommender",
        "import src" + ".dataset_recommender",
        "\"src" + ".dataset_recommender",
        "'src" + ".dataset_recommender",
    )
    for folder in (ROOT / "tests", ROOT / "scripts"):
        for path in sorted(folder.glob("*.py")):
            text = path.read_text(encoding="utf-8-sig")
            for lineno, line in enumerate(text.splitlines(), 1):
                if any(p in line for p in patterns):
                    offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, "包身份回潮（改用正式包名）：\n" + "\n".join(offenders)


def test_single_module_identity_at_runtime():
    """运行时 sys.modules 里不得同时存在 src.* 与正式名两个身份。"""
    import dataset_recommender.agent.agent_exec as canonical

    legacy_prefix = "src" + ".dataset_recommender"
    for name, module in list(sys.modules.items()):
        if name.startswith(legacy_prefix):
            raise AssertionError(f"双模块身份回潮：{name} 与 {canonical.__name__} 并存")
