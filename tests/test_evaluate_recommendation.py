# -*- coding: utf-8 -*-
"""评测外部裁判的 must_not_match 契约（受控重基线新增）。

裁判纯正向不足以看见 exclusion——加 must_not_match 后，Top1/Top5/support/违规率都进同一裁判，
否则"解析器彻底忽略 exclusion"仍可能全绿。"""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

# evaluate_recommendation.py 在 import 时会 `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`
# （CLI 中文编码用）；在 pytest 下这会夺走并关闭捕获缓冲、破坏整个会话。import 期间临时把 sys.stdout
# 换成无 `.buffer` 的 StringIO → 触发该脚本 try/except 跳过 swap；import 后恢复。
import io as _io  # noqa: E402
_saved_stdout = sys.stdout
sys.stdout = _io.StringIO()
try:
    import evaluate_recommendation as E  # noqa: E402
finally:
    sys.stdout = _saved_stdout


def _rec(species="", tissue="", disease="", chemistry="", has_raw_data=None, platform=""):
    return SimpleNamespace(species=species, tissue=tissue, disease=disease,
                           chemistry=chemistry, has_raw_data=has_raw_data,
                           raw={"platform": platform})


def test_satisfies_expected_positive_and_negative():
    human_mouse = _rec(species="Human, Mouse")
    human_only = _rec(species="Human")
    must = {"species": "human"}
    must_not = {"species": "mouse"}
    # 纯正向都满足；加负向后混合物种被判不满足
    assert E.satisfies_must(human_mouse, must) is True
    assert E.satisfies_expected(human_mouse, must, must_not) is False
    assert E.satisfies_expected(human_only, must, must_not) is True


def test_empty_must_not_equivalent_to_legacy():
    for r in (_rec(species="Human"), _rec(species="Mouse"), _rec(species="Human, Mouse")):
        must = {"species": "human"}
        assert E.satisfies_expected(r, must, {}) == E.satisfies_must(r, must)


def test_has_raw_data_bool_exact():
    assert E.constraint_satisfied(_rec(has_raw_data=True), "has_raw_data", True) is True
    assert E.constraint_satisfied(_rec(has_raw_data=False), "has_raw_data", True) is False
    assert E.constraint_satisfied(_rec(has_raw_data=None), "has_raw_data", True) is False
    # False 期望：只有 has_raw_data is False 满足
    assert E.constraint_satisfied(_rec(has_raw_data=False), "has_raw_data", False) is True
    assert E.constraint_satisfied(_rec(has_raw_data=None), "has_raw_data", False) is False


def test_unknown_expectation_key_raises():
    with pytest.raises(ValueError):
        E._validate_expectation_map("q.must_match", {"celltype": "T cell"})
    # 合法键不报错
    E._validate_expectation_map("q.must_match", {"species": "human"})
    E._validate_expectation_map("q.must_not_match", {"tissue": "brain"})
