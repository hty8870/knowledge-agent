# -*- coding: utf-8 -*-
"""相对时间解析（试用反馈驱动）：近N年/今年/去年/前年/年代 → 绝对区间；
年数不明确（近几年）fail-closed 弃权；绝对日期与无日期查询逐位不变（确定性）。

历史 bug：中文数字『近三年』弃权、阿拉伯『近5年』却静默不筛时间——两种失败模式不一致；
且『2010年代』被误当单年 2010。本文件把修正后的行为钉住，并用 pinned today 保证可复现。"""
from datetime import date

from dataset_recommender.retrieval.query_parser import parse_query

TODAY = date(2026, 7, 12)


def _p(q):
    return parse_query(q, None, today=TODAY)


def test_relative_years_cn_and_arabic_consistent():
    """近N年（中文/阿拉伯数字）都换算成 [today-N, today]，不再一个弃权一个静默丢弃。"""
    for q in ("近三年的人类数据", "最近三年人类数据", "过去三年的人类数据", "近3年人类数据"):
        i = _p(q)
        assert i.parse_status == "executable" and not i.abstain, q
        assert (i.date_from, i.date_to) == ("2023-07-12", "2026-07-12"), q
    a = _p("近5年人类数据")   # 此前 executable 但 date 全空（静默丢弃时间约束）
    assert (a.date_from, a.date_to) == ("2021-07-12", "2026-07-12")
    assert "human" in a.constraints.get("species", [])


def test_relative_decade_years():
    assert (_p("过去十年的人类数据").date_from, _p("过去十年的人类数据").date_to) == ("2016-07-12", "2026-07-12")
    assert (_p("近1年人类数据").date_from, _p("近1年人类数据").date_to) == ("2025-07-12", "2026-07-12")


def test_named_relative_years():
    assert (_p("今年的人类数据").date_from, _p("今年的人类数据").date_to) == ("2026-01-01", "2026-12-31")
    assert (_p("去年人类数据").date_from, _p("去年人类数据").date_to) == ("2025-01-01", "2025-12-31")
    assert (_p("前年人类数据").date_from, _p("前年人类数据").date_to) == ("2024-01-01", "2024-12-31")


def test_decade_is_ten_years_not_single_year():
    """『2010年代』= 2010–2019 整个十年（此前被误当单年 2010）。"""
    i = _p("2010年代的人类数据")
    assert (i.date_from, i.date_to) == ("2010-01-01", "2019-12-31")
    # 单年『2010年』仍是单年，不受 decade 影响
    j = _p("2010年的人类数据")
    assert (j.date_from, j.date_to) == ("2010-01-01", "2010-12-31")


def test_ambiguous_relative_date_abstains():
    """年数不明确（近几年/近年来/近些年）→ fail-closed 弃权，给明确原因（而非静默丢时间）。"""
    for q in ("近几年的人类数据", "近年来的人类数据", "近些年人类数据", "最近几年人类数据"):
        i = _p(q)
        assert i.parse_status == "abstained" and i.abstain, q
        assert i.abstain_reason == "ambiguous_relative_date", q


def test_invalid_relative_year_abstains():
    """近0年 / 近-1年：无意义相对年数（此前时间约束被静默丢弃、仍返回普通结果）→ 弃权（验证）。"""
    for q in ("近0年的人类数据", "近-1年人类数据", "过去-3年人类数据", "近00年人类数据"):
        i = _p(q)
        assert i.parse_status == "abstained" and i.abstain, q
        assert i.abstain_reason == "invalid_relative_date", q


def test_invalid_calendar_date_abstains():
    """非法日历日（月>12、该月不存在的日）此前被静默放宽成整年 → 弃权（验证）。"""
    for q in ("2020年13月的人类数据", "2020年2月30日人类数据", "2021年0月人类数据"):
        i = _p(q)
        assert i.parse_status == "abstained" and i.abstain, q
        assert i.abstain_reason == "invalid_date", q
    # 合法月/日不受影响：仍按年份粒度解析（回归护栏）
    ok = _p("2020年5月的人类数据")
    assert ok.parse_status == "executable" and (ok.date_from, ok.date_to) == ("2020-01-01", "2020-12-31")


def test_ambiguous_multi_year_abstains():
    """并列年份『2020年和2022年』语义歧义（区间还是恰好这几年）此前只取前一个 → 弃权（验证）。"""
    for q in ("2020年和2022年的人类数据", "2019年、2021年人类数据", "2018年与2020年数据"):
        i = _p(q)
        assert i.parse_status == "abstained" and i.abstain, q
        assert i.abstain_reason == "ambiguous_multi_year", q
    # 区间表达（到/至/-）不受影响，仍正常解析
    rng = _p("2020到2022年的人类数据")
    assert (rng.date_from, rng.date_to) == ("2020-01-01", "2022-12-31")


def test_negation_combined_with_relative_date():
    """否定 + 相对日期共存：排除小鼠 + 近3年时间窗都要落地。
    用**非系统日期**的 today钉住——若 today 未穿到否定路径而回落 date.today()，
    区间会变成真·今天、本断言当场失败（防「pinned==real today 的巧合通过」）。"""
    i = parse_query("不要小鼠 近3年", None, today=date(2019, 5, 20))
    assert i.parse_status == "executable"
    assert (i.date_from, i.date_to) == ("2016-05-20", "2019-05-20")
    assert "mouse" in i.excluded_constraints.get("species", [])


def test_today_threads_through_positive_path_not_system_clock():
    """正向路径同样必须用传入的 today，而非系统时钟（同上防巧合）。"""
    i = parse_query("近三年的人类数据", None, today=date(2019, 5, 20))
    assert (i.date_from, i.date_to) == ("2016-05-20", "2019-05-20")
    j = parse_query("今年的人类数据", None, today=date(2019, 5, 20))
    assert (j.date_from, j.date_to) == ("2019-01-01", "2019-12-31")


def test_absolute_and_no_date_are_deterministic_wrt_today():
    """确定性护栏：绝对日期/无日期查询的解析**与 today 无关**（不同 today 结果逐位一致）。
    这是冻结评测 767 不受相对日期改动影响的根据（无一含相对日期形素）。"""
    for q in ("2020年的人类数据", "2022年以后的人类数据", "2018-2020年的人类数据", "人类肺癌", "小鼠大脑"):
        a = parse_query(q, None, today=date(2020, 1, 1))
        b = parse_query(q, None, today=date(2030, 12, 31))
        assert (a.date_from, a.date_to, a.parse_status, a.constraints) == \
               (b.date_from, b.date_to, b.parse_status, b.constraints), q
