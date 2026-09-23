# -*- coding: utf-8 -*-
"""检索类请求入参的跨端校验单一真源。

背景：Web（app/webapp.py）与 MCP（mcp_server.py）的入参校验曾是两份手写并已发生
行为级漂移——feasibility 缺来源校验与倒挂窗口检查、task-pack 缺倒挂检查、Web query
缺控制字符/纯符号闸、`_require_iso_date` 两份逐行同构靠注释互认「同口径」。本模块把
口径收到一处；两端只做各自的错误翻译（与 UploadError/CurateError 的三端翻译契约
同构，见 corpus/uploads.py docstring）：

- Web → ``HTTPException(400, detail=hint, headers={"X-Error-Code": code})``
- MCP → ``ToolError("code: hint")``

纪律：只做纯校验（无 IO、不 import webapp/mcp_server/workflow）；需要的语料真源
（``known_source_values`` 的结果集）由调用方解析后传入，保持 transport-neutral、
可被测试直接消费。校验语义逐位移植自 mcp_server 的 ``_require_query``/
``_require_iso_date``/``_validate_date_window``/``_validate_sources`` 与 webapp 的
``_require_iso_date``（严格档裁决），文案取两端的公共措辞——测试钉的
子串（「收录」「YYYY-MM-DD」「发表时间范围颠倒」）保持不变。
"""
from __future__ import annotations

import datetime
import re
import unicodedata

#: 单细胞检索查询通常几十字；超长（多为误粘贴/异常输入）→ 显式拒绝，不再无声吞下。
#: 原三处硬编码（webapp recommend/interpret 各一 + mcp_server `_MAX_QUERY_CHARS`）的唯一真源。
MAX_QUERY_CHARS = 2000


class ParamValidationError(ValueError):
    """检索入参非法：`code` 供调用方分类（empty_query/bad_query/bad_param/bad_source），
    `hint` 供人读中文。继承 ValueError；接口层各自翻译（见模块 docstring），语义与
    UploadError/CurateError 的机器码+人读提示同构。"""

    def __init__(self, code: str, hint: str) -> None:
        super().__init__(hint)
        self.code = code
        self.hint = hint


def validate_query(query: str) -> str:
    """query 四道闸（语义逐位同原 mcp `_require_query`，Web 侧由弱口径升级到此）：

    ① 空白 → empty_query；② 控制/不可见字符（Unicode Cc/Cf，制表换行回车除外）→ bad_query
    ——此前被静默带过，脏输入可无声穿过解析链、污染日志审计与字符串比较；③ 纯符号/标点/
    emoji（无任何字母/数字/汉字）→ bad_query——此前判 executable 返回与输入无关的通用
    结果，把「装作查过了」当推荐；④ 超长 → bad_query（防纯浪费）。返回原串不 strip：
    是否去首尾空白是各端口的既有行为，不借校验之名改变。
    """
    if not query or not query.strip():
        raise ParamValidationError("empty_query", "query 不能为空，例：「人类肺癌的单细胞数据，要有 FASTQ」。")
    ctrl = sorted({c for c in query if c not in "\t\n\r" and unicodedata.category(c) in ("Cc", "Cf")})
    if ctrl:
        codes = [f"U+{ord(c):04X}" for c in ctrl]
        raise ParamValidationError(
            "bad_query",
            f"query 含控制/不可见字符 {codes}（如 NUL、零宽空格、双向控制符）；请传纯文本查询。",
        )
    if not any(c.isalnum() for c in query):
        raise ParamValidationError(
            "bad_query",
            "query 无有效检索内容（纯符号/标点/emoji）；请用文字描述物种/组织/疾病/平台/技术等条件。",
        )
    if len(query) > MAX_QUERY_CHARS:
        raise ParamValidationError(
            "bad_query",
            f"query 过长（{len(query)} 字符，上限 {MAX_QUERY_CHARS}）；单细胞检索查询通常几十字即可。",
        )
    return query


def validate_iso_date(value: "str | None", *, name: str) -> str:
    """发表时间入参：空 → ""（不限）；否则必须是格式与日历都合法的 YYYY-MM-DD。

    为什么不能静默吞（旧行为：非「年份打头」一律当没传）：用户给了筛选条件、系统悄悄
    丢掉，结果和预期对不上却无任何提示；更糟的是 "" 这种不存在的日期曾被
    当作已生效条件回显上屏。诚实方向只有一个：给了就校验，不合法就明说。
    （严格档裁决；原 webapp 与 mcp 两份实现逐行同构，本函数是其唯一真源。）"""
    s = (str(value) if value is not None else "").strip()
    if not s:
        return ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        raise ParamValidationError("bad_param", f"{name} 需要 YYYY-MM-DD 格式的日期（收到 {s!r}）。")
    year, month, day = (int(part) for part in s.split("-"))
    try:
        datetime.date(year, month, day)
    except ValueError:
        raise ParamValidationError("bad_param", f"{name} 不是真实存在的日期（收到 {s!r}）。") from None
    return s


def validate_date_window(date_from: str, date_to: str) -> None:
    """倒挂窗口（from > to）→ bad_param 当场点名。

    此前被静默接受、恒零结果还冒充合法生效条件上屏——用户在替一个不可能的条件读
    「没数据」。feasibility 与 task-pack 曾缺这道闸（补齐，与 /api/recommend
    和 MCP 同口径）。"""
    if date_from and date_to and date_from > date_to:
        raise ParamValidationError(
            "bad_param",
            f"发表时间范围颠倒：date_from（{date_from}）晚于 date_to（{date_to}），这个窗口不可能成立。",
        )


def validate_sources(sources: "list[str] | tuple[str, ...] | str | None", *, known: "list[str] | set[str]") -> None:
    """来源校验（语义同原 mcp `_validate_sources` + webapp `_validate_pack_sources` 的形状闸）：

    None/[] → 放行（默认基础语料）；形状错（单个字符串等）→ bad_param——build 侧的
    retrieval_params 是自由 dict，不先判型会把字符串逐字符枚举报出滑稽文案；
    空/空白来源名 → bad_source——本想过滤却给了空串，此前被静默滤除后回退默认结果，
    无法与「真按某来源筛」区分；未知来源 → bad_source——此前被静默过滤成恒零结果，
    冒充「查过了没有」（corner 测试钉「不存在的来源XYZ」「收录」两个子串）。

    `known` 由调用方解析传入（webapp 用模块级 DATA_DIR/PROJECT_ROOT，MCP 用 _settings()
    惰性解析）——本模块不碰语料层。"""
    if not sources:
        return
    if isinstance(sources, str) or not isinstance(sources, (list, tuple)):
        raise ParamValidationError(
            "bad_param",
            "sources 需要是来源名数组（如 [\"10x Genomics\"]），不是单个字符串。",
        )
    if any(not str(x).strip() for x in sources):
        raise ParamValidationError(
            "bad_source",
            "sources 含空/空白来源名；请去掉空项，或整个省略 sources 用默认 10x 基础语料。",
        )
    known_seq = list(known)
    known_set = set(known_seq)
    unknown = [str(x) for x in sources if str(x) not in known_set]
    if unknown:
        raise ParamValidationError(
            "bad_source",
            f"未知来源：{'、'.join(unknown)}。当前收录的来源：{'、'.join(known_seq)}"
            "（大小写与空格需完全一致）。",
        )
