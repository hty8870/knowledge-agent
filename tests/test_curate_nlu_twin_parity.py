# -*- coding: utf-8 -*-
"""NLU 孪生差分门。

`eval/curate_nlu/rule_parser.py` 是 NLU 规则版对照实现，其极性门刻意复刻生产
`action_plan` 语义（「两处同步改」纪律）。纪律靠人肉已经失败过一次——hedges 表缺「要不」
且注释误称生产侧没有掩码层。本门把等价性机械化：两模块的掩码函数对任意输入必须逐字一致，
典型断言用例的极性判定必须同出同入。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "eval" / "curate_nlu"))

from dataset_recommender.agent import action_plan as AP  # noqa: E402
import rule_parser as RP  # noqa: E402


_MASK_BATTERY = [
    # 征询句式（掩掉）
    "能不能打包一下", "要不把 upload_x 删了吧", "要不要联网搜一下", "可不可以只留前5条",
    "该不该现在检查更新", "行不行啊", "好不好", "可否恢复那个文件",
    # 疑问/陈述「没」（掩掉）
    "检查下ArrayExpress更新没，有新增就搜来入库", "有没有更新", "更新了没", "有没新版本",
    "搜到了没？", "看看10x有没有新数据!",
    # 真否定（不许掩）
    "别打包了", "不要下载", "算了，取消导入", "不用检查了", "千万别删",
    # 顺承「没」（掩掉——「没」修饰前一动词，不否定「就/才」后动作）
    "没找到就联网搜", "没有就导出来", "没搜到合适的就打包",
    # 边界拼接
    "要不要不要打包", "要不", "能不能不搜", "没", "有没有",
    "把那个删掉吧，不要", "先别管10x，检查HCA更新没",
]


def test_mask_functions_byte_identical():
    """两模块 _mask_hedges 对同一输入必须逐字一致（等长掩码，位置不变）。"""
    for text in _MASK_BATTERY:
        assert AP._mask_hedges(text) == RP._mask_hedges(text), text


def test_hedge_tables_identical():
    """征询词表逐字一致（含顺序——长词先消费是语义的一部分）。"""
    assert tuple(AP._QUESTION_HEDGES) == tuple(RP._QUESTION_HEDGES)
    assert tuple(AP._INTERROGATIVE_MEI_FIXED) == tuple(RP._INTERROGATIVE_MEI_FIXED)
    assert AP._INTERROGATIVE_MEI_RE.pattern == RP._INTERROGATIVE_MEI_RE.pattern
    assert AP._SEQUENTIAL_MEI_RE.pattern == RP._SEQUENTIAL_MEI_RE.pattern


def test_polarity_outcomes_agree_on_typical_cases():
    """单锚点用例：生产 polarity_blocked 与验证 _all_blocked 同出同入。"""
    cases = [
        ("别打包了", "打包", True),
        ("不要下载那5条", "下载", True),
        ("能不能打包一下", "打包", False),
        ("要不把 upload_x 删了吧", "删", False),
        ("检查下ArrayExpress更新没，有新增就搜来入库", "搜来", False),
    ]
    for utt, anchor, expect_blocked in cases:
        pos = utt.index(anchor)
        rp = bool(RP._all_blocked(utt, [pos]))
        ap = bool(AP.polarity_blocked(utt, anchor))
        assert rp == expect_blocked, (utt, "rule_parser", rp)
        assert ap == expect_blocked, (utt, "action_plan", ap)
