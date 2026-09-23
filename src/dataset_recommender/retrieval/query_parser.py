"""
查询理解：中文/英文自然语言 -> 结构化约束 + fail-closed。

设计（与 Codex 两轮对抗辩论收敛）：
- hard_filter 的 0% 违规保证 = 约束抽取必须"要么正确抽到、要么明确弃权/澄清"，绝不静默丢弃或**反向**用户约束。
- **正负极性**：正向 constraints（须含）+ 负向 excluded_constraints（须不含）+ raw 三态。
- **否定语法 = 小白名单执行 + 大兜底弃权**：只有整条负向 clause 完全落在白名单里才提交 exclusion；
  任何未被白名单覆盖的否定成分必然触发 guard/残差门弃权——结构性保证"绝不静默反向"。
- **无否定信号的查询原样走既有正向机器**（52 条冻结 + 现有测试逐位不变）；只有含否定形素的查询进新路径。
- parse_status 三态：executable / clarification_required（如"不需要fastq"歧义）/ abstained。
- OR / hedge / 未收录实义词 一律弃权（与历史一致）。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from . import query_lexicon as V
from ..content.identifier_patterns import classify as _classify_identifier, strip_doi_prefix as _strip_doi_prefix
from ..domain.registry import domain_catalog, domain_dimensions, domain_labels_zh


def __getattr__(name: str):
    """兼容 shim（PEP 562）：`DIMENSIONS` 与 `_DIM_LABEL_CN` 改由活动领域包供给，
    旧的 `from query_parser import DIMENSIONS` 写法惰性解析到当前领域，biodata 默认逐位不变。"""
    if name == "DIMENSIONS":
        return domain_dimensions()
    if name == "_DIM_LABEL_CN":
        return domain_labels_zh()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# parse_status 取值
PS_EXECUTABLE = "executable"
PS_CLARIFY = "clarification_required"
PS_ABSTAINED = "abstained"


@dataclass(slots=True)
class QueryIntent:
    original_query: str
    # 正向硬约束：dimension -> 规范 target 列表（对应字段须包含任一 target）
    constraints: dict[str, list[str]] = field(default_factory=dict)
    # 负向硬约束：dimension -> 规范 target 列表（对应字段命中任一 target 即淘汰）
    excluded_constraints: dict[str, list[str]] = field(default_factory=dict)
    has_raw_data_required: bool | None = None
    # 展示用：dimension -> 人类可读 display 名
    display_map: dict[str, list[str]] = field(default_factory=dict)
    excluded_display: dict[str, list[str]] = field(default_factory=dict)
    free_text_terms: list[str] = field(default_factory=list)
    # 解析状态（新真源）；abstain 为兼容字段，与 parse_status 同步
    parse_status: str = PS_EXECUTABLE
    abstain: bool = False
    abstain_reason: str = ""      # 机器可读
    abstain_detail: str = ""      # 人类可读
    # clarification 第三态（如"不需要fastq"）
    clarification_reason: str = ""
    clarification_detail: str = ""
    clarification_options: list[dict] = field(default_factory=list)
    # 发表时间范围（ISO YYYY-MM-DD，空串=不限）；硬过滤，仅作用于 published_date。
    date_from: str = ""
    date_to: str = ""
    # 诚实降级：被用户显式「宽容」的正向维度集合——这些维度上**字段为空（无法核验）视作通过**，
    # 但已知不同值仍被排除。默认空集＝no-op（官方评测/CLI 不传 → passes_hard_filter 默认分支逐位不变）。
    lenient_dims: set[str] = field(default_factory=set)
    # 软偏好（「优先 X」）：**不参与硬过滤**，只在排序里加权。dimension -> 规范 target 列表。
    # 与 constraints 的关键区别：constraints 决定「谁能活下来」，preferred 只决定「活下来的谁排前面」。
    # 空 dict = 无偏好 = 打分函数走原分支、逐位不变（官方评测/CLI 从不传偏好）。
    preferred_constraints: dict[str, list[str]] = field(default_factory=dict)
    preferred_display: dict[str, list[str]] = field(default_factory=dict)
    # 「优先有 FASTQ」：True=偏好有原始数据。None=无此偏好。**不**收 False——「优先没有 FASTQ」
    # 不是真实需求，且与 has_raw_data_required=False 的硬排除语义容易混淆。
    preferred_raw: bool | None = None
    # 「优先 2024 年」：偏好的发表时间区间（ISO YYYY-MM-DD，空串=无）。**不进 date_from/date_to**——
    # 那两个是硬过滤，把「优先 2024」写进去就等于把别的年份全筛掉了。
    preferred_date_from: str = ""
    preferred_date_to: str = ""
    # 「优先 10x」这类偏好某个数据来源。来源在 parse_query 之前就被 resolve_search_request 处理掉了，
    # 故这里由上层回填（parse_query 自己看不到来源专名）。存来源规范名，如 "10x Genomics"。
    preferred_sources: list[str] = field(default_factory=list)
    # N1 静默丢词诚实层（只读、additive）：用户输入了**结构上无对应筛选维度**的实义描述词
    # （性别/年龄/受试者/功能类，见 V.FILLER_DOMAIN），系统既不落维、又不入 free_text_terms、也不弃权
    # → 静默丢弃零信号。这里记下这些词供回显「未作为筛选维度」。不参与解析/检索/弃权；非 executable 恒空。
    unused_query_terms: list[str] = field(default_factory=list)
    # `unresolved_term` 弃权时，**卡住这句话的那几个词**（结构化版本；此前只以文字形式
    # 埋在 abstain_detail 里，上层想用只能去正则抠字符串）。
    # 用途：编排层据此自动降级——忽略这几个词、拿剩余条件继续检索并显著标注（见 workflow.run）。
    # 只在 abstain_reason == "unresolved_term" 时非空；其它状态恒空。
    unresolved_terms: list[str] = field(default_factory=list)
    # 「A 或 B」的**实际处理方式**（只读、additive；查询里没有「或」时恒为空 dict）。
    # 2026-07-25 之前这里是整句弃权，现在照做——但引擎能表达的「或」只有一种：**同一维度内多值**。
    # 于是必须如实说清这次落到了哪一档，否则就是静默偏离：
    #   {"marker": "或", "or_dims": ["species"], "and_dims": ["tissue"], "exact": True, "note_zh": "…"}
    # exact=True  → OR 的各项都落在同一维度（含只落在软偏好段的情形，见 _describe_or_handling），
    #               执行的语义**就是**用户说的「或」；
    # exact=False → 各项跨了不同维度，引擎只能按「同时满足」执行，比「或」更窄，note_zh 明说这件事。
    or_handling: dict = field(default_factory=dict)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        k = it.strip().lower()
        if k and k not in seen:
            seen.add(k)
            out.append(it.strip())
    return out


def _merged_catalog(keyword_mapping: dict[str, list[dict[str, Any]]] | None) -> dict:
    catalog = domain_catalog()
    if keyword_mapping:
        catalog = {k: list(v) for k, v in domain_catalog().items()}
        for dim, entries in keyword_mapping.items():
            if isinstance(entries, list):
                norm = []
                for e in entries:
                    if isinstance(e, dict):
                        e2 = dict(e)
                        if "aliases" not in e2 and "keywords" in e2:
                            e2["aliases"] = e2["keywords"]
                        norm.append(e2)
                catalog.setdefault(dim, [])
                catalog[dim].extend(norm)
    return catalog


_ASCII_ALNUM = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _alias_occurrences(text: str, alias: str) -> list[tuple[int, int]]:
    """找 alias 在 text 里的出现位置，**ASCII 端按词边界判定**。

    起因：alias 此前是裸子串匹配，于是英文单词内部的偶然包含会被当成真约束——
    `generated` / `integrated` / `celebrated` 里都含 `rat`（大鼠），查 `integrated human lung atlas`
    会静默解析出 species=[human, rat]。这不是弃权、是**悄悄多加了一个约束**，用户看不到任何信号。

    规则：alias 的首/尾字符是 ASCII 字母数字时，该侧不允许紧邻另一个 ASCII 字母数字；
    中文两端不判边界（中文本来就不分词）。右侧**容忍一个复数尾巴 `s`**（rats / pbmcs 仍算命中），
    但 `rate` / `rated` 这类词内包含不再命中。

    返回按出现顺序的 [start, end)，end 已包含被吞掉的复数 `s`（消费时要连它一起抹掉，
    否则残留的孤立 `s` 会变成新的残差）。
    """
    if not alias:
        return []
    left_guard = alias[0] in _ASCII_ALNUM
    right_guard = alias[-1] in _ASCII_ALNUM
    out: list[tuple[int, int]] = []
    start = 0
    n = len(text)
    while True:
        i = text.find(alias, start)
        if i < 0:
            break
        start = i + 1
        if left_guard and i > 0 and text[i - 1] in _ASCII_ALNUM:
            continue
        end = i + len(alias)
        if right_guard and end < n and text[end] in _ASCII_ALNUM:
            if text[end] == "s" and (end + 1 >= n or text[end + 1] not in _ASCII_ALNUM):
                end += 1          # 复数形式，算同一个词
            else:
                continue          # 词内包含 → 不是这个词
        out.append((i, end))
    return out


def _blank_spans(text: str, spans: list[tuple[int, int]]) -> str:
    """把若干区间替换成**等长**空格。等长是有意的：工作串还要被残差门/否定门按内容扫描，
    等长替换不会让左右残字意外相邻拼出原本不存在的词。"""
    if not spans:
        return text
    chars = list(text)
    for a, b in spans:
        for k in range(max(0, a), min(len(chars), b)):
            chars[k] = " "
    return "".join(chars)


def _scan_markers(query: str, table: "tuple[str, ...]") -> list[str]:
    """在 query 里扫一张标记表，按出现位置保序去重；长标记优先消费（「下载脚本」不被切成「下载」）。"""
    text = (query or "").lower()
    hits: list[str] = []
    consumed = [False] * len(text)
    # 排序键必须**全序**：只按 len 排的话，等长标记之间的先后取决于 set 的迭代顺序，
    # 而 str 的哈希在不同进程里是随机化的 → 同一句话在两次运行里可能给出不同结果，
    # 与本函数 docstring 承诺的「确定性」不符。加字典序做次级键即可全序。
    for marker in sorted({m.lower() for m in table}, key=lambda m: (-len(m), m)):
        start = 0
        while True:
            i = text.find(marker, start)
            if i < 0:
                break
            start = i + 1
            j = i + len(marker)
            if any(consumed[i:j]):
                continue
            for k in range(i, j):
                consumed[k] = True
            hits.append(marker)
    return _unique(sorted(hits, key=lambda m: text.find(m)))


def detect_action_markers(query: str) -> list[str]:
    """查询里出现的**执行类**说法（打包 / 下载脚本 / 导出引文…），按出现位置保序去重。

    定位：这些词不是检索条件，也不该炸掉检索（已收进 FILLER_GRAMMAR），但**不能装作没看见**——
    「人类肺数据，帮我打包前20条」里的「打包」是用户明确说出的诉求，静默吞掉是本项目反复修过的错。
    上层据此**如实回音**「你说了打包」。

    扫的是**并集**（动作 ∪ 对象），因为回音要完整。决定「这句话该不该被当成一条执行指令」用
    `detect_action_verbs`——那一问只能看动作词，见其 docstring。

    纯函数、只读、确定性；不参与解析/检索/弃权，非 executable 状态下上层也不会用它。
    """
    return _scan_markers(query, V.ACTION_MARKERS)


def detect_action_verbs(query: str) -> list[str]:
    """查询里出现的执行**动作**说法。这是「要不要把这句话交给执行那条路」的唯一判据。

    与 `detect_action_markers` 的区别是刻意的：对象词（清单 / 批量 / 脚本 / 引文…）是产物的名字，
    出现在句子里**不代表**用户在要求做这件事。拿它们路由的后果实测过——
    「去掉批量效应大的」被「批量」劫持成打开打包面板，用户那句改条件的话当场蒸发，
    而屏幕上还弹出了一个看起来「有反应」的面板。这比什么都不做更难被用户自己发现。
    """
    return _scan_markers(query, V.ACTION_VERBS)


def detect_operation_markers(query: str) -> list[str]:
    """「AI 执行」关闭时降级检测用的**操作意图**标记全集（2026-08-03 确定性、只读）。

    = 执行动作词（ACTION_VERBS）∪ 管护操作短语（CURATE_OP_MARKERS），同一套扫描口径
    （小写、长词优先消费、按出现位置保序去重）。与 `detect_action_verbs` 的分工是刻意的：
    那问的是「要不要交给执行那条路」（只许动作词，对象词都不行）；这问的是「这句是不是
    一句操作指令」（动作 ∪ 管护短语），只在「AI 执行」关时驱动降级气泡——绝不用于执行任何动作。
    """
    return _scan_markers(query, V.ACTION_VERBS + V.CURATE_OP_MARKERS)


def _protected_spans(text: str) -> list[tuple[int, int]]:
    """`V.ALIAS_PROTECTED_COMPOUNDS` 在 text 里的非重叠出现位置（长词优先）。

    为什么需要：中文别名是**裸子串**匹配（英文侧已按词边界收紧，中文本来不分词、没法照搬），
    所以短别名会被更长的**不同概念**词包含 —— 「单核细胞」(monocyte) 里的「单核」被当成
    单核测序、「皮质醇」里的「皮质」被当成组织。这类错筛不会报错、界面还会挂一个看起来
    很正常的筛选标签，比零返回更危险。做法与否定豁免复合词同源：在 alias 消费**之前**
    把整词屏蔽，之后按描述词如实回显（见 `_protected_terms`），不静默丢弃。
    """
    spans: list[tuple[int, int]] = []
    if not text:
        return spans
    occupied = [False] * len(text)
    for comp in sorted(V.ALIAS_PROTECTED_COMPOUNDS, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(comp, start)
            if i < 0:
                break
            start = i + 1
            j = i + len(comp)
            if any(occupied[i:j]):
                continue
            for k in range(i, j):
                occupied[k] = True
            spans.append((i, j))
    return spans


def _protected_terms(text: str) -> list[str]:
    """被 `_protected_spans` 屏蔽掉的复合词原文（按出现顺序去重），供回显「未作为筛选维度」。
    纯函数、只读；屏蔽本身在 `_match_all_consuming` 里做，这里只负责说清楚屏蔽了什么。"""
    return _unique([text[i:j] for i, j in sorted(_protected_spans(text))])


# 保护复合词分两类，判据不另立新表、直接取自 FILLER_DOMAIN 的成员资格（见 `_positive_core`）：
#   · 在 FILLER_DOMAIN 里（如「单核细胞」）＝有名有姓但系统没有对应维度 → 不弃权、回显「未作为筛选维度」；
#   · 不在（如「胰岛素 / 胸腺嘧啶 / 血管紧张素」）＝系统压根不认识 → 计入残差、照常弃权。
_FILLER_DOMAIN_SET = frozenset(V.FILLER_DOMAIN)


def _protected_echoable(text: str) -> list[str]:
    """只取「有名有姓、但系统没有这个维度」的那一类保护词——它们才该被回显成「未作为筛选维度」。
    系统不认识的那一类由残差门负责弃权，若也在这里回显，同一个词会既是「未收录」又是「无维度」。"""
    return [t for t in _protected_terms(text) if t in _FILLER_DOMAIN_SET]


def _match_all_consuming(query_lower: str, catalog: dict[str, list[dict[str, Any]]]):
    """跨维度**最长匹配 + 消费**（正向路径，历史行为，勿改语义）：先匹配长 alias 并从工作串抹除，
    避免子串重复命中。返回 (constraints, display_map, matched_aliases, residual_working)。"""
    alias_entries: list[tuple[str, str, dict[str, Any]]] = []
    for dim in domain_dimensions():
        for entry in catalog.get(dim, []):
            for a in entry.get("aliases", []):
                a = str(a).lower().strip()
                if a:
                    alias_entries.append((a, dim, entry))
    alias_entries.sort(key=lambda x: len(x[0]), reverse=True)

    # 先屏蔽保护复合词，再做 alias 消费——顺序不能反，反了就是屏蔽了个寂寞。
    working = _blank_spans(query_lower, _protected_spans(query_lower))
    constraints: dict[str, list[str]] = {}
    display_map: dict[str, list[str]] = {}
    matched_aliases: list[str] = []
    for alias, dim, entry in alias_entries:
        hits = _alias_occurrences(working, alias)
        if hits:
            working = _blank_spans(working, hits)
            constraints.setdefault(dim, [])
            constraints[dim].extend(str(t).lower() for t in entry.get("targets", []))
            disp = entry.get("display")
            if isinstance(disp, str) and disp:
                display_map.setdefault(dim, [])
                if disp not in display_map[dim]:
                    display_map[dim].append(disp)
            matched_aliases.append(alias)
    for dim in list(constraints):
        constraints[dim] = _unique(constraints[dim])
    return constraints, display_map, matched_aliases, working


#: 「打包前20条」这类**执行子句里的条数**。刻意写得很窄：数词必须带量词收尾（条/个/份/项），
#: 且整句必须先出现执行动作词。这两条一起，天然放过 10x、COVID-19、GSE123456、2020年、
#: 「20 个样本」（无执行词的句子完全不进这条路径）。
_ACTION_COUNT_RE = re.compile(r"(?:前|头|取|首|这|那)?\s*\d{1,3}\s*(?:条|个|份|项)")

#: 执行动作词与条数之间允许隔多远。「帮我打包前20条」是 0；留一点余量给「打包一下前 20 条」。
_ACTION_COUNT_WINDOW = 8


def strip_action_counts(query: str) -> str:
    """把执行子句里的**条数**从查询文字里等长抹掉（换成空格），其余一个字节不动。

    为什么必须抹：`_extract_free_text_terms` 会把句子里所有 ASCII 数字/字母串抽成自由文本词
    参与打分。于是「人类肺癌数据，打包前20条」里的 `20` 会当成一个检索词——它描述的是**要几条**，
    不是**要什么**。实测这会换掉候选池里的若干条，也就是说**进包的是哪几个数据集被这个 20 改变了**。

    等长替换（不是删除）是本仓库的既有纪律：删字符会让左右残字贴到一起，拼出原本不存在的词。
    """
    text = str(query or "")
    verbs = _scan_markers(text, V.ACTION_VERBS)
    if not verbs:
        return text
    low = text.lower()
    verb_spans = [(low.find(v), low.find(v) + len(v)) for v in verbs if low.find(v) >= 0]
    if not verb_spans:
        return text
    chars = list(text)
    for m in _ACTION_COUNT_RE.finditer(text):
        near = any(
            m.start() - end <= _ACTION_COUNT_WINDOW and start - m.end() <= _ACTION_COUNT_WINDOW
            for start, end in verb_spans
        )
        if not near:
            continue
        for k in range(m.start(), m.end()):
            chars[k] = " "
    return "".join(chars)


def _extract_free_text_terms(query: str) -> list[str]:
    # 先抹掉执行子句里的条数，再抽自由文本词——「打包前20条」的 20 是「要几条」，不是检索条件。
    terms = re.findall(r"[a-zA-Z0-9\+\-']+", strip_action_counts(query).lower())
    return _unique([t for t in terms if len(t) > 1])


#: 兼容别名：维度中文名唯一真源=活动领域包的维度描述符（经 `domain_labels_zh()` 下发）；
#: board.py 从本模块取 `_DIM_LABEL_CN`（历史入口，经顶部 __getattr__ shim 惰性解析），保留不删。


def active_filters(intent: "QueryIntent") -> list[dict]:
    """把「查询已命中的硬约束」拍平成可渲染 chip 列表——前端侧栏「本次查询命中」区与 MCP `understood`
    复用**同一真源**。每项含稳定极性 `filter_id`（include:<dim> / exclude:<dim> / raw:required / raw:forbidden /
    date:range）+ `polarity`（include|exclude）+ dim/label/values。

    **非可执行（弃权/澄清）→ 空**：不出可删命中 chip（删也解不了），理由另由无结果/澄清文案解释。
    """
    if intent.parse_status != PS_EXECUTABLE:
        return []
    out: list[dict] = []
    for dim in domain_dimensions():
        disp = intent.display_map.get(dim)
        if disp:
            out.append({"filter_id": f"include:{dim}", "polarity": "include",
                        "dim": dim, "label": domain_labels_zh().get(dim, dim), "values": list(disp)})
    for dim in domain_dimensions():
        disp = intent.excluded_display.get(dim)
        if disp:
            out.append({"filter_id": f"exclude:{dim}", "polarity": "exclude",
                        "dim": dim, "label": "排除·" + domain_labels_zh().get(dim, dim), "values": list(disp)})
    # 软偏好：**不是筛选条件**，只影响排序。极性单列成 prefer，标签写「优先·」，
    # 前端据此把它和硬条件区分开——混在一起会让用户以为结果都满足这一项，那就是骗人。
    for dim in domain_dimensions():
        disp = intent.preferred_display.get(dim)
        if disp:
            out.append({"filter_id": f"prefer:{dim}", "polarity": "prefer",
                        "dim": dim, "label": "优先·" + domain_labels_zh().get(dim, dim), "values": list(disp)})
    if intent.preferred_raw:
        out.append({"filter_id": "prefer:raw", "polarity": "prefer",
                    "dim": "has_raw_data", "label": "优先·原始数据", "values": ["有 FASTQ"]})
    if intent.preferred_sources:
        out.append({"filter_id": "prefer:source", "polarity": "prefer",
                    "dim": "source", "label": "优先·数据来源", "values": list(intent.preferred_sources)})
    if intent.preferred_date_from or intent.preferred_date_to:
        out.append({"filter_id": "prefer:date", "polarity": "prefer", "dim": "date",
                    "label": "优先·发表时间",
                    "values": [f"{intent.preferred_date_from or '不限'} ~ {intent.preferred_date_to or '不限'}"]})
    if intent.has_raw_data_required is not None:
        forbidden = intent.has_raw_data_required is False
        out.append({"filter_id": "raw:forbidden" if forbidden else "raw:required",
                    "polarity": "exclude" if forbidden else "include",
                    "dim": "has_raw_data", "label": "原始数据",
                    "values": ["不要 FASTQ" if forbidden else "需要 FASTQ"]})
    if intent.date_from or intent.date_to:
        lo, hi = (intent.date_from or "")[:10], (intent.date_to or "")[:10]
        rng = f"{lo} ~ {hi}" if lo and hi else (f"≥ {lo}" if lo else f"≤ {hi}")
        out.append({"filter_id": "date:range", "polarity": "include",
                    "dim": "date", "label": "发表时间", "values": [rng]})
    return out


def relax_key_to_suppressed(intent: "QueryIntent", key: object) -> list[str]:
    """把 `retriever.relaxation_options` 的放宽项 key 翻译成 suppressed_constraints 口径
    （filter_id 极性形式，与 `active_filters` 同词表——单锚点，agent 环内 ask.relax/rerank
    relax 槽与网页零命中放松卡共用）：dim:X→include:X / exclude:X→exclude:X /
    raw→按当前值 raw:required|raw:forbidden / date→date:range /
    only:X→删其余全部（其它正向维+全部排除维+FASTQ+时间范围）。
    只产出**本项放松量**，不与调用方既有 suppressed 合并（合并归装配侧）。
    非法/与 intent 不符的 key 抛 ValueError（如实拒绝，不静默忽略）。"""
    suppressed: list[str] = []
    k = str(key or "").strip()
    if k.startswith("dim:"):
        dim = k[4:]
        if dim not in domain_dimensions() or not intent.constraints.get(dim):
            raise ValueError(k)
        suppressed.append(f"include:{dim}")
    elif k.startswith("exclude:"):
        dim = k[8:]
        if dim not in domain_dimensions() or not intent.excluded_constraints.get(dim):
            raise ValueError(k)
        suppressed.append(f"exclude:{dim}")
    elif k == "raw":
        if intent.has_raw_data_required is None:
            raise ValueError(k)
        suppressed.append("raw:required" if intent.has_raw_data_required else "raw:forbidden")
    elif k == "date":
        if not (intent.date_from or intent.date_to):
            raise ValueError(k)
        suppressed.append("date:range")
    elif k.startswith("only:"):
        dim = k[5:]
        if dim not in domain_dimensions() or not intent.constraints.get(dim):
            raise ValueError(k)
        suppressed.extend(f"include:{d}" for d in domain_dimensions()
                          if d != dim and intent.constraints.get(d))
        suppressed.extend(f"exclude:{d}" for d in domain_dimensions()
                          if intent.excluded_constraints.get(d))
        if intent.has_raw_data_required is not None:
            suppressed.append("raw:required" if intent.has_raw_data_required
                              else "raw:forbidden")
        if intent.date_from or intent.date_to:
            suppressed.append("date:range")
    else:
        raise ValueError(k)
    out: list[str] = []
    for s in suppressed:
        if s not in out:
            out.append(s)
    return out


def _residual_salient(working: str) -> str:
    """在已消费 alias 的工作串上判断是否还有"未识别的实义词"。返回可疑片段（空=无残差）。

    **filler 必须换成分隔符，不能直接删掉**（2026-07-22 夜批量测试）：删掉会让被删词左右的残字
    紧挨在一起，拼出一个**用户从没打过**的幻影词，而这个词随后会被 `unresolved_terms` /
    弃权文案原样引述回去——「查询里有系统未收录的词：「白介素」」，可用户打的是「白细胞介素」。
    实测三例（全库 5665）：

        白细胞介素相关单细胞数据          「白细胞介素相关」-抠掉细胞/相关→「白介素」
        10x Genomics 的人类外周血单个核细胞数据  「单个核细胞数据」-抠掉数据/细胞/个→「单核」
        我需要一些多组学的人类数据集用来做整合分析  「数据集用来做整合分析」-抠掉…→「来整合」

    第一条是**诚实性缺陷**（引述用户没说过的话），第二条还顺带把 PBMC 这种最常见的说法打成弃权。
    换成分隔符后按空白切片、逐片判长度：残差只会**减少或持平**，不会凭空多出弃权
    （原来 ≥2 的幻影串拆开后每片可能 <2；反之不可能把 <2 变成 ≥2）。
    """
    text = working
    for a in sorted(V.RAW_REQUIRED_ALIASES + V.RAW_NOT_REQUIRED_ALIASES, key=len, reverse=True):
        text = text.replace(a.lower(), " ")
    filler = {f.lower() for f in V.FILLER_TOKENS}
    cn_filler = sorted((f for f in filler if re.fullmatch(r"[一-鿿]+", f)), key=len, reverse=True)
    salient: list[str] = []
    for run in re.findall(r"[一-鿿]+", text):
        r = run
        for f in cn_filler:
            r = r.replace(f, " ")
        for piece in r.split():
            if len(piece) >= 2:
                salient.append(piece)
    for w in re.findall(r"[a-z]{2,}", text):
        if len(w) >= 3 and w not in filler:
            salient.append(w)
    # 去重：同一个词在句子里出现两次，弃权文案不该说两遍，降级建议的 ignored_terms 也不该重复。
    # 实测 `人类肺数据 <script>alert(1)</script>` 的 ignored_terms 是 ['script','alert','script']。
    return " ".join(_unique(salient))


def _unused_domain_terms(working: str) -> list[str]:
    """N1 静默丢词诚实层（只读）：在**已消费 alias** 的工作串上，找出被当作 `V.FILLER_DOMAIN`
    （结构上无筛选维度的实义描述词：性别/年龄/受试者/功能类）而**静默丢弃**的词——它们不落维、不入
    free_text_terms（ASCII-only 抓不到中文）、也不触发 unresolved_term 弃权，用户毫无信号。

    只在 executable 路径调用（此时残差已判空，工作串仅剩 filler）；返回按出现位置保序去重的词表，供
    上层回显「以下词未作为筛选维度」。不改解析/检索/弃权，对冻结 767 零影响（此函数不在评测/CLI 路径）。
    只报 FILLER_DOMAIN、不报 FILLER_GRAMMAR：后者要么纯噪声、要么是已落维度的通用头（报了会撒谎）。
    """
    text = working.lower()
    # 先抹掉 raw alias（与 _residual_salient 同源），免得把「原始数据/fastq」等 raw 说法误当描述词。
    for a in sorted(V.RAW_REQUIRED_ALIASES + V.RAW_NOT_REQUIRED_ALIASES, key=len, reverse=True):
        text = text.replace(a.lower(), " ")
    # 长词优先消费，避免子串重复命中（「男性」先于「男」；命中即从工作串抹除）。
    hits: list[str] = []
    for tok in sorted({f.lower() for f in V.FILLER_DOMAIN}, key=len, reverse=True):
        if tok and tok in text:
            hits.append(tok)
            text = text.replace(tok, " ")
    return _unique(sorted(hits, key=lambda t: working.lower().find(t)))


_YEAR = r"(19\d\d|20\d\d)"

# ---- 相对时间（相对『今天』换算成绝对区间）----
# 只认**明确年数**的相对表达（近/最近/过去 N 年）+ 命名相对（今年/去年/前年）+ 年代（十年）。
# 年数不明确的（近几年/近年来）由 parse_query 的歧义门 fail-closed 弃权，绝不静默丢弃时间约束
# （历史 bug：中文数字『近三年』弃权、阿拉伯『近5年』却静默不筛——两种失败模式不一致）。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
_REL_N = r"(?P<n>\d{1,3}|[一二两三四五六七八九十]{1,3})"
_REL_YEARS_RE = re.compile(r"(?:最近|近|过去|过往)\s*" + _REL_N + r"\s*年(?:以来|以内|之内|内|来|间)?")
_NAMED_REL_RE = re.compile(r"今年|去年|前年")
_NAMED_REL_OFFSET = {"今年": 0, "去年": 1, "前年": 2}
_DECADE_RE = re.compile(_YEAR + r"\s*年代")
# 年数不明确 → 弃权（parse_query 用）。负向 lookahead 保证不误吞含明确 N 的『近3年 / 近三年』。
_AMBIGUOUS_REL_RE = re.compile(r"近几年|最近几年|近年来|近些年|这些年|前些年|近来|近年(?![\d一二两三四五六七八九十])")

# ---- 非法/歧义时间表达（反馈）：弃权而非静默放宽/丢弃 ----
# 近0年 / 近-1年：无意义的相对年数（此前 _parse_dates 因 N≤0 跳过相对分支 → 时间约束静默消失）。
_ZERO_NEG_REL_RE = re.compile(r"(?:最近|近|过去|过往)\s*(?:0+|-\s*\d+)\s*年")
# YYYY年MM月[DD日]：抓出月/日以校验是否合法日历日（如 13月、2月30日）；合法则照旧只用年份粒度。
_YMD_RE = re.compile(_YEAR + r"\s*年\s*(\d{1,2})\s*月(?:\s*(\d{1,2})\s*日)?")
# 并列年份（X年和/与/、Y年）：区间还是恰好这几年语义歧义（『到/至/-/~』区间不在此列，仍正常解析）。
_MULTI_YEAR_RE = re.compile(_YEAR + r"\s*年?\s*(?:和|与|、|及|以及|，|,)\s*" + _YEAR + r"\s*年")


def _cn_num(s: str) -> int:
    """小数量词 → int（阿拉伯：任意位；中文：一~九、十几、几十几，≤99）；无法解析返回 0。
    调用侧正则 `_REL_N` 已把阿拉伯限到 1-3 位，故不会收到超大数。"""
    s = (s or "").strip()
    if s.isdigit():
        return int(s)
    if not s or any(ch not in _CN_DIGITS for ch in s):
        return 0
    if s == "十":
        return 10
    if "十" in s:
        left, _, right = s.partition("十")
        tens = _CN_DIGITS.get(left, 1) if left else 1
        ones = _CN_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(s) == 1:
        return _CN_DIGITS.get(s, 0)
    return 0


def _shift_years(d: date, delta: int) -> date:
    """d 往前/后挪 delta 年；闰日（2/29）落到平年时收敛到 2/28。"""
    y = d.year + delta
    try:
        return date(y, d.month, d.day)
    except ValueError:
        return date(y, d.month, 28)


def _parse_dates(text: str, today: date | None = None) -> tuple[str, str, str]:
    """从中文/英文查询解析发表时间范围 → (date_from, date_to, 去掉日期表达后的文本)。
    支持：绝对年/区间/以后/以前（历史行为，逐位不变）+ 相对（近N年/今年/去年/前年）+ 年代（十年）。
    相对表达相对 `today`（默认 date.today()，测试可 pin）换算成绝对区间。"""
    if today is None:
        today = date.today()
    date_from = ""
    date_to = ""
    t = text
    # -- 相对：近/最近/过去 N 年 → [today-N, today]（N 明确才认；不明确交给 parse_query 弃权）--
    m = _REL_YEARS_RE.search(t)
    if m and _cn_num(m.group("n")) > 0:
        n = _cn_num(m.group("n"))
        return (_shift_years(today, -n).isoformat(), today.isoformat(),
                t[: m.start()] + " " + t[m.end():])
    # -- 命名相对：今年/去年/前年 → 对应整年 --
    m = _NAMED_REL_RE.search(t)
    if m:
        y = today.year - _NAMED_REL_OFFSET[m.group(0)]
        return f"{y}-01-01", f"{y}-12-31", t[: m.start()] + " " + t[m.end():]
    # -- 年代：YYYY年代 → 该十年整段（2010年代 = 2010-2019，此前被误当单年 2010）--
    m = _DECADE_RE.search(t)
    if m:
        start = (int(m.group(1)) // 10) * 10
        return f"{start}-01-01", f"{start + 9}-12-31", t[: m.start()] + " " + t[m.end():]
    m = re.search(_YEAR + r"\s*年?\s*[-~到至]\s*" + _YEAR + r"\s*年?", t)
    if m:
        lo, hi = sorted((int(m.group(1)), int(m.group(2))))
        return f"{lo}-01-01", f"{hi}-12-31", t[: m.start()] + " " + t[m.end():]
    m = re.search(_YEAR + r"\s*年?\s*(?:以后|之后|以来|往后|开始|起)", t)
    if m:
        date_from = f"{int(m.group(1))}-01-01"
        t = t[: m.start()] + " " + t[m.end():]
    m = re.search(_YEAR + r"\s*年?\s*(?:以前|之前)", t)
    if m:
        date_to = f"{int(m.group(1)) - 1}-12-31"
        t = t[: m.start()] + " " + t[m.end():]
    if not date_from:
        m = re.search(r"(?:after|since)\s+" + _YEAR, t)
        if m:
            date_from = f"{int(m.group(1))}-01-01"
            t = t[: m.start()] + " " + t[m.end():]
    if not date_to:
        m = re.search(r"before\s+" + _YEAR, t)
        if m:
            date_to = f"{int(m.group(1)) - 1}-12-31"
            t = t[: m.start()] + " " + t[m.end():]
    if not date_from and not date_to:
        m = re.search(_YEAR + r"\s*年", t)
        if m:
            y = int(m.group(1))
            date_from, date_to = f"{y}-01-01", f"{y}-12-31"
            t = t[: m.start()] + " " + t[m.end():]
    return date_from, date_to, t


# ============================================================================
# 否定 / 排除语法
# ============================================================================

# ---- span 词法：位置感知的实体匹配（仅用于否定绑定/保护，不改正向确定性）----
@dataclass(slots=True)
class _Span:
    start: int
    end: int
    kind: str          # "entity" | "raw"
    dim: str | None    # 结构化维度；raw 为 None
    targets: tuple[str, ...]
    display: str


def _entity_spans(q: str, catalog: dict) -> list[_Span]:
    """全查询最长-非重叠实体 span（含 raw 物理资产名）。用于判断否定作用域与保护实体内否定字。"""
    cand: list[_Span] = []
    for dim in domain_dimensions():
        for entry in catalog.get(dim, []):
            targets = tuple(str(t).lower() for t in entry.get("targets", []))
            disp = str(entry.get("display") or dim)
            for a in entry.get("aliases", []):
                a = str(a).lower().strip()
                if not a:
                    continue
                # 与正向消费同一套词边界判定（_alias_occurrences）：否则否定作用域会按
                # 「integrated 里的 rat」这种词内包含去圈实体，两条路径对同一句话看到的实体不一样。
                for i, j in _alias_occurrences(q, a):
                    cand.append(_Span(i, j, "entity", dim, targets, disp))
    for term in V.RAW_TERMS:
        for i, j in _alias_occurrences(q, term):
            cand.append(_Span(i, j, "raw", None, (term,), "FASTQ"))
    # 长优先、非重叠占位（镜像正向的"长 alias 先消费"）
    cand.sort(key=lambda s: (s.end - s.start), reverse=True)
    occ = [False] * (len(q) + 1)
    # 保护复合词先占位：与正向消费同源（`_match_all_consuming` 先 blank 再匹配），
    # 否则两条路径对同一句话看到的实体不一样——「单核细胞」在正向被保护、在否定作用域里
    # 却仍被当成 modality 实体，「不要单核细胞」的绑定就会跑偏。
    for a, b in _protected_spans(q):
        for k in range(a, min(b, len(q))):
            occ[k] = True
    chosen: list[_Span] = []
    for s in cand:
        if any(occ[s.start:s.end]):
            continue
        for k in range(s.start, s.end):
            occ[k] = True
        chosen.append(s)
    chosen.sort(key=lambda s: s.start)
    return chosen


def _date_spans(q: str) -> list[dict]:
    """把 `_parse_dates` 那套模式在**整句**上再扫一遍，只为拿到时间表达的位置。

    刻意镜像 `_parse_dates` 的分支顺序与优先级：前四种（相对年数 / 今年去年前年 / 年代 / 区间）任一命中
    即独占返回；否则收集「以后 / 以前 / after·since / before」，四者都没有时才认裸年份。
    只报位置、不报解析出的区间——区间的唯一真源仍是 `_parse_dates` 自己。
    """
    for regex in (_REL_YEARS_RE, _NAMED_REL_RE, _DECADE_RE):
        match = regex.search(q)
        if match:
            return [{"start": match.start(), "end": match.end()}]
    match = re.search(_YEAR + r"\s*年?\s*[-~到至]\s*" + _YEAR + r"\s*年?", q)
    if match:
        return [{"start": match.start(), "end": match.end()}]
    found: list[dict] = []
    has_from = has_to = False
    for regex, is_from in (
        (re.compile(_YEAR + r"\s*年?\s*(?:以后|之后|以来|往后|开始|起)"), True),
        (re.compile(_YEAR + r"\s*年?\s*(?:以前|之前)"), False),
        (re.compile(r"(?:after|since)\s+" + _YEAR), True),
        (re.compile(r"before\s+" + _YEAR), False),
    ):
        if (is_from and has_from) or (not is_from and has_to):
            continue
        match = regex.search(q)
        if match:
            found.append({"start": match.start(), "end": match.end()})
            has_from = has_from or is_from
            has_to = has_to or not is_from
    if found:
        return sorted(found, key=lambda s: s["start"])
    match = re.search(_YEAR + r"\s*年", q)
    return [{"start": match.start(), "end": match.end()}] if match else []


def annotate_query_spans(query: str, keyword_mapping: "dict[str, list[dict[str, Any]]] | None" = None) -> list[dict]:
    """只读：标出查询里每一段「能落到筛选维度上」的文字的位置。**纯函数，不参与解析、不改判定。**

    这是条件板（board.py）定位「用户要改的是哪一段」的唯一手段。复用否定路径已有的
    `_entity_spans`（长优先、非重叠），再叠加原始数据别名与时间表达，因此与真实解析同一套词表。

    与 `_match_all_consuming` 的差别是已知且刻意的：前者按长度降序逐个 `replace` 消费、允许同一 alias
    多处命中；本函数按位置占位、互不重叠。两者在「肺癌」这类嵌套 alias 上一致（都不会切出裸「肺」），
    差异形态由 `tests/test_board_span_parity.py` 量化并列入已知集——**绝不为了让两边一致去改 parse_query**。

    守卫：若 `query.lower()` 改变了字符串长度（少数 Unicode 大小写折叠会），索引不再可信 → 直接返回 `[]`，
    调用方据此 fail-closed，绝不在错位的索引上改写用户的句子。

    返回 `[{start, end, text, kind, dim, display, targets}]`，按 start 升序。
    `kind` ∈ entity / raw / date；`dim` 对应 DIMENSIONS 或 has_raw_data / date。
    """
    text = str(query or "")
    lowered = text.lower()
    if len(lowered) != len(text):
        return []
    q = _neutralize_existential(lowered)
    catalog = _merged_catalog(keyword_mapping)
    out: list[dict] = []
    occupied: list[bool] = [False] * (len(text) + 1)
    for span in _entity_spans(q, catalog):
        for pos in range(span.start, span.end):
            occupied[pos] = True
        out.append({
            "start": span.start, "end": span.end, "text": text[span.start:span.end],
            "kind": span.kind,
            "dim": span.dim if span.kind == "entity" else "has_raw_data",
            "display": span.display,
            "targets": list(span.targets),
        })
    # 时间表达最后叠加，且只在没被实体占位的区间上生效——实体词表优先，避免与含数字的别名抢位置。
    for span in _date_spans(q):
        if any(occupied[span["start"]:span["end"]]):
            continue
        out.append({
            "start": span["start"], "end": span["end"], "text": text[span["start"]:span["end"]],
            "kind": "date", "dim": "date", "display": "发表时间", "targets": [],
        })
    out.sort(key=lambda s: s["start"])
    return out


def _inside_span(pos: int, spans: list[_Span]) -> bool:
    return any(s.start <= pos < s.end for s in spans)


def _skip_ws(q: str, i: int) -> int:
    while i < len(q) and q[i] in " \t":
        i += 1
    return i


def _span_at(spans: list[_Span], i: int):
    for s in spans:
        if s.start == i:
            return s
    return None


def _startswith_any(q: str, i: int, tokens) -> str:
    for t in sorted(tokens, key=len, reverse=True):
        if q.startswith(t, i):
            return t
    return ""


# ---- 弃权 / 澄清消息 ----
_NEG_EXISTENTIAL = V.NEG_EXISTENTIAL_PHRASES
# 双重/嵌套否定检测词：能锚到 NEGATION_GUARDS_CN 的从锚点取（交集；消费方是 any() 命中判定，
# 顺序无关）；「不要不/没有不/无不/没不/not without」是双重否定连写复合形，锚点不收，列在下面。
_NESTED_FROM_GUARDS = frozenset({"不排除", "未排除", "不能排除", "不是", "并非", "并不是"})
_NESTED_EXTRA = ("不要不", "没有不", "无不", "not without", "没不")
_NESTED_MARKERS = tuple(m for m in V.NEGATION_GUARDS_CN if m in _NESTED_FROM_GUARDS) + _NESTED_EXTRA

_RAW_CLARIFY_DETAIL = ("『不需要 FASTQ』语义有歧义：可能是"
                       "『不把 FASTQ 作为筛选条件』，也可能是『排除含 FASTQ 的数据集』。请选择其一。")
_RAW_CLARIFY_OPTIONS = [
    {"id": "exclude_raw", "label": "排除含 FASTQ 的数据集", "rewrite": "不要 FASTQ"},
    {"id": "ignore_raw", "label": "FASTQ 不作为筛选条件", "rewrite": ""},
]


def _mk_abstain(query: str, reason: str, detail: str,
                unresolved: "list[str] | None" = None) -> QueryIntent:
    """弃权态也要如实回显被 `ALIAS_PROTECTED_COMPOUNDS` 整体屏蔽掉的词。

    2026-07-22 夜批量测试抓到：「胰岛素抵抗的单细胞数据」弃权时 `unused_query_terms` 是空的——
    可执行分支（1137/1260 行）都补了 `_protected_terms`，弃权分支没补。于是屏幕上只说
    「未收录的词：『抵抗』」，一个字都没提「胰岛素」也被整体屏蔽、没参与筛选；
    降级建议里的 `ignored_terms` 同样只有「抵抗」，用户会以为胰岛素还在生效。

    屏蔽在 `_match_all_consuming` 里是**无条件**发生的（不分支），所以在这里按原句重算是
    单一真源、不会误报：只要这几个字出现在句子里，它就一定被屏蔽了。
    """
    return QueryIntent(original_query=query, parse_status=PS_ABSTAINED,
                       abstain=True, abstain_reason=reason, abstain_detail=detail,
                       free_text_terms=_extract_free_text_terms(query),
                       unused_query_terms=_protected_echoable(str(query or "").lower()),
                       unresolved_terms=list(unresolved or []))


def _mk_clarify(query: str, reason: str, detail: str, options: list[dict]) -> QueryIntent:
    return QueryIntent(original_query=query, parse_status=PS_CLARIFY,
                       abstain=False, clarification_reason=reason,
                       clarification_detail=detail, clarification_options=list(options),
                       free_text_terms=_extract_free_text_terms(query))


def _intent_has_search_signal(intent: QueryIntent) -> bool:
    """intent 里有没有任何**可用来检索**的信号（物种/组织/疾病/技术/平台/原始数据/年份/偏好/非纯数字自由词）。

    注意：**来源专名（10x / ArrayExpress…）在 parse_query 之前就被剥掉了**，所以本函数看不到来源。
    因此「无信号」不等于「空查询」——「选自ArrayExpress的数据」在这里也无信号，但它有来源、是合法检索。
    故本函数只用来**配合执行词判定**「纯操作指令」，不单独据它弃权（见调用点）。
    纯数字 free-text（『20』）不算信号：它只会把一切含该数字的记录拉进来。
    """
    if any(intent.constraints.get(d) for d in domain_dimensions()):
        return True
    if any(intent.excluded_constraints.get(d) for d in domain_dimensions()):
        return True
    if intent.has_raw_data_required is not None:
        return True
    if intent.date_from or intent.date_to:
        return True
    if intent.preferred_constraints or intent.preferred_raw:
        return True
    if intent.preferred_date_from or intent.preferred_date_to or intent.preferred_sources:
        return True
    if any(not str(t).strip().isdigit() for t in intent.free_text_terms):
        return True
    return False


def _is_bare_action_command(query: str, intent: QueryIntent) -> bool:
    """这句话是不是一句**光秃秃的执行指令**——有「打包 / 下载脚本 / 导出引文」这类执行词，
    却没有任何可检索的条件（也没有来源，来源会让 `_intent_has_search_signal` 之外的路径救回）。

    只针对这一类弃权，是为了不误伤两种既有的、有意的设计：
      · 来源专名查询（「选自ArrayExpress的数据」）——来源被剥掉后这里无信号，但它是合法检索，不该弃权；
      · 宽泛查询（「我想要一些数据」）——按 MCP `_advisory` 的既定设计返回宽结果 + 提醒，也不弃权。
    「打包前20条」这类**只有指令、没有检索目标**的话才落这里：与其把全库前 10 条当结果倒出去，
    不如老实说「你说了打包，但没说要找什么」。
    """
    return bool(detect_action_markers(query)) and not _intent_has_search_signal(intent)


#: 纯执行指令弃权文案（正负两条路径共用单一真源）。
_ACTION_ONLY_DETAIL = (
    "这句话是一个操作指令（打包 / 下载脚本 / 导出引文），但没有说明要找什么数据。"
    "请先描述你要找的数据集，例如「有 FASTQ 的人类乳腺癌」，检索出结果后再打包 / 下载。"
)


def _contains_any(q: str, tokens) -> str:
    for t in tokens:
        if t in q:
            return t
    return ""


def _neutralize_existential(q: str) -> str:
    """把存在性问句短语（有没有/是否有…）替换成空白 → 其内部『没/无』不触发否定，正常正向解析。"""
    for ph in sorted(_NEG_EXISTENTIAL, key=len, reverse=True):
        q = q.replace(ph, " " * len(ph))
    return q


def _has_or(q: str) -> str:
    for m in V.OR_MARKERS:
        if m in q:
            return m
    return ""


def _unresolved_detail(shown: str) -> str:
    """`unresolved_term` 的用户可见文案。**单一真源**——此前正负两条路径各手抄一份。

    2026-07-25 产品哲学修正后重写了措辞。旧文案是「为避免返回违反你意图的结果，已弃权」，
    那是在把「弃权」当成一种美德讲。新措辞只说两件事实：哪个词没收录，以及**已经算好了**
    忽略它之后能搜到什么（编排层会自动降级检索并把结果与标注一并给出）。
    这一档之所以还保留「先不直接返回」，不是「宁可弃权」，而是因为**它本来就没让用户空手**：
    未收录词多半是真实约束（「霍格沃茨综合征」「胰岛素」），直接按剩下的条件返回几千条
    并宣称是答案才是撒谎；而降级选项把这几千条摆在一次点击之外，用户随时能拿。
    """
    # 注意：本文案里的「」**只许**用来引用用户原句里真实出现过的词。
    # `tests/test_unresolved_terms_are_real.py` 会把每一对角括号里的内容拿去和原句做子串核对——
    # 拿角括号当强调号用会被判成「引述了原句里没有的词」，那道门是对的：编造引文比措辞难看严重得多。
    # 2026-08-03 顺手项（用户：解释太啰嗦）——收敛成一句话：哪个词没收录、去掉它可能有结果。
    # 降级选项 chips（忽略它再搜）就排在下方，不需要文案再指路。
    return f"查询里有系统未收录的词：{shown}。把这些词去掉再搜，可能有结果。"


#: 维度中文名唯一真源=活动领域包的维度描述符（经 `domain_labels_zh()` 下发）；本模块不再另抄一份。
#: 裁决留痕：旧 `_DIM_ZH` 的键误写 "technology"（维度键实为
#: "assay"），OR 回执把英文维度键直出（「assay＝…」）；锚点化后输出中文标签属**修复**而非
#: 漂移，明确接受。逐字契约钉：tests/test_negation_contract.py::test_or_note_uses_chinese_dim_labels。
def _describe_or_handling(query: str, constraints: dict, display_map: dict,
                          excluded: dict | None = None,
                          excluded_display: dict | None = None,
                          preferred: dict | None = None,
                          preferred_display: dict | None = None) -> dict:
    """用户说了「或」时，如实描述系统**实际**是怎么执行的。查询里没有「或」→ 空 dict。

    引擎能表达的「或」只有一种：**同一维度内多值**。
      · 正向 `constraints[dim] = [A, B]`：`retriever.passes_hard_filter` 逐字写着
        「正向：须含任一 target」→ 就是 A 或 B；
      · 负向 `excluded_constraints[dim] = [A, B]`：「命中任一 forbidden 即淘汰」
        → ¬A ∧ ¬B，正是 ¬(A∨B)。所以「不要小鼠或大鼠」也**精确**成立；
      · 软偏好 `preferred[dim] = [A, B]`：命中任一都加权——「优先 A 或 B」正是用户说的
        「或」，只是机制是加权而非筛选（2026-08-15 此前不数这一份，
        「优先人或小鼠的脑数据」被谎报成「按同时满足执行、请分两次查」）。

    三档，全部机械可核实（只看「哪个维度收到了几个值」，不去猜「或」两边是哪两个词——
    那要重建 span 位置，猜错就是拿错的依据向用户解释）：
      exact    唯一一个维度拿到多值（含多值只落在软偏好里的情形）→ 执行语义就是用户说的「或」
      superset 多个维度各拿到多值 → 实际是交叉组合，比用户说的搭配更宽
      narrower 没有维度拿到多值 → 「或」跨了维度，只能按「同时满足」执行，比「或」更窄
    """
    marker = _has_or(query)
    if not marker:
        return {}
    excluded = excluded or {}
    excluded_display = excluded_display or {}
    preferred = preferred or {}
    preferred_display = preferred_display or {}

    # **数用户说了几个东西，不数展开出几个 target。**这一处真机实测栽过：
    # 「肺癌或 10x 的数据」里「肺癌」一个词就展开成 `['lung cancer', 'non-small cell lung']`
    # 两个 target，按 target 数判定会把它误判成「同维度多值＝精确的或」，
    # 而实际上「或」的另一半（10x）是**来源专名**、早在 parse_query 之前就被摘走了。
    # 那句话于是拿到一条「本次按『都算』检索：疾病＝Lung Cancer」的假回执——**编造依据**，比不报更糟。
    # `display_map` 存的是用户侧的规范展示名（一个词一条），正是需要的粒度。
    def _n(dim: str, neg: bool) -> int:
        src, disp = (excluded, excluded_display) if neg else (constraints, display_map)
        return len(disp.get(dim) or src.get(dim) or [])

    or_dims = [d for d in domain_dimensions() if _n(d, False) > 1]
    or_neg_dims = [d for d in domain_dimensions() if _n(d, True) > 1]
    and_dims = [d for d in domain_dimensions() if _n(d, False) == 1 or _n(d, True) == 1]
    # 软偏好段的多值也要数：_extract_preferences 已把偏好段从 constraints 抹掉，
    # 只数 constraints 会看不见「优先 A 或 B」里的「或」。
    pref_or_dims = [d for d in domain_dimensions()
                    if len(preferred_display.get(d) or preferred.get(d) or []) > 1]
    if not (or_dims or or_neg_dims or and_dims or pref_or_dims):
        return {}

    def _one(dim: str, neg: bool) -> str:
        src, disp = (excluded, excluded_display) if neg else (constraints, display_map)
        vals = " / ".join(disp.get(dim) or src.get(dim) or [])
        return f"{'不要' if neg else ''}{domain_labels_zh().get(dim, dim)}＝{vals}"

    def _join(dims: list[str], neg: bool | None = None, sep: str = "、") -> str:
        # neg=None → 自动：这个维度只有排除侧有值就按排除说，避免把「不要小鼠」说成「物种＝Mouse」。
        return sep.join(_one(d, (not (constraints.get(d) or [])) if neg is None else neg)
                        for d in dims)

    mk = marker.strip()
    hit = or_dims + or_neg_dims
    parts: list[str] = []
    if or_dims:
        parts.append(f"按「都算」检索：{_join(or_dims)}")
    if or_neg_dims:
        parts.append(f"按「都排除」筛掉：{_join(or_neg_dims, neg=True)}")
    if len(hit) == 1:
        fit = "exact"
        note = f"你写了「{mk}」，本次{parts[0]}。"
    elif len(hit) >= 2:
        # 「人类肺癌或小鼠肝癌」：两个维度各拿到两个值 → 实际检索的是**交叉组合**，
        # 比用户说的那两个搭配更宽（会混进人类肝癌）。宽比空好，但不能假装精确。
        fit = "superset"
        note = (f"你写了「{mk}」。有 {len(hit)} 个维度各拿到了多个值（{'；'.join(parts)}），"
                "系统是按**交叉组合**执行的——比你说的那几个搭配更宽，结果里会混进你没点名的组合。")
    else:
        if pref_or_dims:
            # 「或」的多值只落在软偏好里：两个值都已按「命中任一都加权」执行，
            # 这正是用户说的「或」（机制是加权不是筛选）。此前这里落 narrower 档，
            # 谎称「按同时满足执行、请分两次查」，与同屏的「优先·物种：Human / Mouse」
            # chip 自相矛盾（2026-08-15 触发点。
            fit = "exact"
            pref_text = "；".join(
                f"优先{domain_labels_zh().get(d, d)}：{' / '.join(preferred_display.get(d) or preferred.get(d) or [])}"
                for d in pref_or_dims)
            note = (f"你写了「{mk}」。优先条件收到了多个值（{pref_text}），"
                    "已按\"命中任一都加权\"执行——这就是\"或\"的效果，不需要分两次查。")
        else:
            # 每个维度都只有一个值 → 「或」的两侧落在不同维度，表达不出来，只能按「同时满足」办。
            fit = "narrower"
            note = (f"你写了「{mk}」，但没有任何一类条件收到两个值——"
                    f"要么这两样东西属于不同类别（本次在筛的是{_join(and_dims)}），"
                    "要么其中一个是数据来源专名（来源是单独的范围设定，不参与「或」）。"
                    "系统只能表达同一类里的「或」（例如物种同时要人和小鼠），"
                    "所以本次是按「同时满足」执行的，比你说的「或」更窄——想要真正的「或」，请分两次查。")
    if fit != "narrower" and and_dims:
        note += f"另外这些条件必须同时满足：{_join(and_dims)}。"
    return {"marker": mk, "or_dims": or_dims, "or_excluded_dims": or_neg_dims,
            "and_dims": and_dims, "fit": fit, "exact": fit == "exact", "note_zh": note}


# 「除非」是条件连词（→ 整句弃权）；但排除类动词「排除/剔除/去除/删除…」词尾的『除』紧跟正向『非X』实体
# （如 排除非霍奇金淋巴瘤 = 排除[非霍奇金淋巴瘤]）会拼出假的『除非』子串，误判成条件句而过度弃权。
# 用负向 lookbehind 排掉这些移除动词词尾的『除』，只把真正的条件『除非』当条件。
_CHUFEI_COND_RE = re.compile(r"(?<![排剔去删清切消解免])除非")


def _has_conditional(q: str) -> str:
    for m in V.NEG_CONDITIONAL_MARKERS:
        if m == "除非":
            if _CHUFEI_COND_RE.search(q):
                return m
        elif m in q:
            return m
    return ""


#: 英文否定语素检测正则：**程序生成自 `V.NEG_MORPHEMES_EN`，不手抄**——本表曾是手抄副本且已漂移
#: （漏 never / don't / do not / free of / other than 等 guard 里早就有的词，检测层与词表两张皮）。
#: 2026-08-17 起与词表同一份真源（`NEG_MORPHEMES_EN` 本身就是 EXEC ∪ GUARDS 的程序并）。
_EN_NEG_RE = re.compile(
    r"(?<![a-z0-9_])(" + "|".join(re.escape(w) for w in V.NEG_MORPHEMES_EN) + r")(?![a-z])")

#: 英文**可执行**否定前缀正则（4d 步）：同样程序生成自 `V.EXEC_NEG_PREFIX_EN`，防手抄漂移。
#: 词边界 lookbehind + 操作符后必须跟空白或连字符（"no-mouse" / "not lymphoma" / "free of X"）。
_EXEC_NEG_PREFIX_EN_RE = re.compile(
    r"(?<![a-z0-9_])(" + "|".join(re.escape(w) for w in V.EXEC_NEG_PREFIX_EN) + r")(?:\s+|-)")

#: 「why not X」是建议反问（含义≈「要不要试试 X」），不是排除——命中即整句弃权，绝不静默反向。
_WHY_NOT_RE = re.compile(r"(?<![a-z])why\s+not(?![a-z])")


def _leftover_negation(working: str) -> str:
    """在『已消费正向 alias 的工作串』上找**未被白名单消费的否定形素**。是否弃权由调用方按
    从句邻近实体判定（2026-09-11 起：同从句有实体→弃权防反向；无实体碎尾→改道降级通道）。
    先移除 filler + raw 物理名（保护 免疫/是 等含否定字的合法填充词与已处理 raw），再查否定形素。"""
    text = working
    # 否定豁免复合词（非编码RNA/non-coding…）：词首 非/non- 是词素、非排除操作符 → 扫描前先整词屏蔽，
    # 防止把正向术语误报成 unsupported_negation。长度降序先消费长词，避免「非编码」先屏蔽掉更长词的一部分。
    # 对不含豁免复合词的查询是 no-op（冻结门 767 逐位不变）；能映射维度的复合词另走 CATALOG alias 正向消费。
    for comp in sorted(V.NEGATION_EXEMPT_COMPOUNDS, key=len, reverse=True):
        if comp in text:
            text = text.replace(comp, " " * len(comp))
    # 英文否定形素先查一遍（filler 删除之前）：「free of / other than」这类多词形素横跨
    # filler 词（"of" 本身是 FILLER），先删 filler 就永远拼不回来；词边界正则保证不误伤
    # 词内子串。中文形素仍必须在删完 filler 后查（保护 免疫/是 等含否定字的合法填充词）。
    m0 = _EN_NEG_RE.search(text)
    if m0:
        return m0.group(1)
    for f in sorted(V.FILLER_TOKENS, key=len, reverse=True):
        fl = f.lower()
        # 纯 ASCII 字母 filler 必须按词边界删：朴素子串删除会把否定形素咬碎——"with" 把
        # "without" 咬成 "out"、"the" 咬碎 "neither"、"a" 咬碎 "avoid/lacking"，
        # 否定信号在入口检测阶段就丢了（英文否定盲区②）。中文 filler 无此问题
        # 保持原有子串语义不变。
        if fl.isascii() and fl.isalpha():
            text = re.sub(r"(?<![a-z])" + re.escape(fl) + r"(?![a-z])", " ", text)
        else:
            text = text.replace(fl, " ")
    for f in V.RAW_TERMS:
        text = text.replace(f, " ")
    cn_negs = sorted(set(V.EXEC_NEG_PREFIX_CN) | set(V.NEGATION_GUARDS_CN)
                     | {"除了", "除", "以外", "之外", "除外"}, key=len, reverse=True)
    for morph in cn_negs:
        if morph in text:
            return morph
    m = _EN_NEG_RE.search(text)
    if m:
        return m.group(1)
    return ""


def _date_ranges_disjoint(pref: tuple[str, str], hard: tuple[str, str]) -> bool:
    """两个发表时间区间是否完全没有交集（空串 = 该端不限）。

    「2019年之前的数据，优先2024年以后」两条并排挂在屏幕上，而偏好那条对存活集里
    **每一条**都不成立——这和「不要小鼠，优先小鼠」是同一种自相矛盾，只是维度不同。
    """
    pf, pt = pref
    hf, ht = hard
    if not (pf or pt) or not (hf or ht):
        return False
    if pf and ht and pf > ht:
        return True
    if hf and pt and hf > pt:
        return True
    return False


_SOFT_GAP_PUNCT = " \t，,、；;：:的了和与及"


def _skip_soft_gap(q: str, j: int) -> int:
    """跨过「优先」与实体之间那点无关紧要的东西：标点、连接词、以及**无对应筛选维度**的描述词。

    只跨这三类，且长度有上限——跨得太远就不是「紧邻」了，那种句子的作用域本来就不清楚，
    应该走弃权而不是猜。返回新的起点（没跨过任何东西时 = j）。
    """
    limit = min(len(q), j + 16)
    i = j
    moved = True
    while moved and i < limit:
        moved = False
        while i < limit and q[i] in _SOFT_GAP_PUNCT:
            i += 1
            moved = True
        for tok in sorted({t.lower() for t in V.FILLER_DOMAIN}, key=len, reverse=True):
            if tok and q.startswith(tok, i):
                i += len(tok)
                moved = True
                break
    return i


def _local_date_end(q: str, j: int, today: "date | None") -> int:
    """从 j 开始，找出最短的一段能被 `_parse_dates` 认出来的日期文字，返回它的结束下标（无则 = j）。

    存在的理由：`_date_spans` 是全句独占匹配，句中已经有别的日期表达时它不会在 j 处给出 span。
    只在「优先」紧跟位置做一次局部解析，不改全句日期语义。
    """
    limit = min(len(q), j + 24)
    for end in range(j + 2, limit + 1):
        df, dt, _rest = _parse_dates(q[j:end], today)
        if df or dt:
            return end
    return j


def _extract_preferences(q: str, catalog: dict, today: date | None = None):
    """抽出「优先 X」软偏好，并把命中的整段（标记词 + 紧邻实体）从查询里抹成等长空格。

    返回 `(剩余查询, preferred, preferred_display, preferred_raw, 孤立标记词)`。

    为什么抹掉：不抹的话紧邻实体会被后面的正向解析当成**硬约束**消费掉，
    「优先 Visium」就变成「只要 Visium」——把偏好偷换成筛选，比不支持这个词更糟。

    与否定语法的两点**有意的不同**：
    · 跨维度允许。否定要弃权是因为 NOT(A AND B) 有歧义；偏好没有这个问题，
      「优先人类肺」= 人类加权 + 肺加权，两者独立叠加，语义唯一。
    · 标记词后面没跟认识的实体时**不弃权**。硬约束丢了会返回违反意图的结果，必须 fail-closed；
      软偏好丢了只是没加权、结果集一模一样，弃权反而把整句话废掉。这种情况把标记词
      如实回显进「未作为筛选维度」（调用方处理），绝不静默吞掉。
    """
    spans = _entity_spans(q, catalog)
    dspans = _date_spans(q)
    excised = [False] * len(q)
    preferred: dict[str, list[str]] = {}
    preferred_disp: dict[str, list[str]] = {}
    pref_raw: bool | None = None
    pref_dates: tuple[str, str] = ("", "")
    orphan: list[str] = []
    neg_after_prefer = False
    ambiguous = False
    _hedge_set = set(V.HEDGE_PREFER_PREFIX_CN)
    for op in sorted(V.PREFER_PREFIXES_ALL, key=len, reverse=True):
        idx = 0
        while True:
            i = q.find(op, idx)
            if i < 0:
                break
            idx = i + 1
            if excised[i] or _inside_span(i, spans):
                continue
            # 「优先是 / 优先用 / 优先选 / 优先有」——标记词与实体之间允许一个单字虚词，再多就不猜了。
            # hedge 类的虚字表**不含「的」**：「最好的人类肺数据」里「人类 / 肺」是硬要求，
            # 允许「的」会把它读成「偏好人类」，把硬要求悄悄降级成加权（见 V.HEDGE_CONNECTOR_CHARS）。
            conn = V.HEDGE_CONNECTOR_CHARS if op in _hedge_set else V.PREFER_CONNECTOR_CHARS
            j = _skip_ws(q, i + len(op))
            # 「最好的人类肺数据」＝「最好的」当形容词，人类/肺都是**硬要求**。
            # 光把「的」从虚字表里去掉不够：下面 `_skip_soft_gap` 的 `_SOFT_GAP_PUNCT` 也含「的」，
            # 会绕过来把「人类」吃成偏好。所以 hedge 遇到紧跟的「的」直接跳过这次命中，
            # 让它落到虚词表（FILLER_GRAMMAR 里有「最好」等），整句照常做硬筛选。
            if op in _hedge_set and j < len(q) and q[j] == "的":
                continue
            if j < len(q) and q[j] in conn:
                j = _skip_ws(q, j + 1)
            # 「优先 2024 年」：日期不在实体 span 里，单独认一次。必须在这里就把日期文字抹掉——
            # 留着它，下游 _parse_dates 会把它变成**硬**时间过滤，用户说「优先」却被筛掉了别的年份，
            # 那正是把偏好偷换成筛选。
            # 不能只认全句 `_date_spans` 的结果：它是独占/首个匹配语义，句中先出现另一个日期时
            # （「2020年的数据，优先2024年」）这里的 span 对不上 j，整段年份就被**静默丢弃**——
            # 既没进偏好、也没进硬过滤、也不触发残差门，用户写的年份凭空消失。改成就地再解析一次。
            dsp = next((d for d in dspans if d["start"] == j), None)
            d_end = dsp["end"] if dsp is not None else _local_date_end(q, j, today)
            if d_end > j:
                df, dt, _rest = _parse_dates(q[j:d_end], today)
                if df or dt:
                    pref_dates = (df or pref_dates[0], dt or pref_dates[1])
                    _mark(excised, i, d_end)
                    continue
            # 「优先不要小鼠」= 软性排除。系统**表达不了**软排除：真去执行就成了硬排除，
            # 用户说的是「尽量别要」，拿到的却是「一条都不要」——把偏好偷换成筛选，正是本特性要防的事。
            # 既然做不到，就明说做不到（fail-closed），绝不悄悄按硬排除办。
            if _startswith_any(q, j, tuple(V.EXEC_NEG_PREFIX_CN) + tuple(V.NEGATION_GUARDS_CN)):
                neg_after_prefer = True
                idx = len(q)
                break
            items, end = _consume_entity_list(q, j, spans)
            if not items:
                # 标记词和实体之间常常隔着点东西：标点、连接词、或者一个「未作为筛选维度」的
                # 描述词（`优先转移性乳腺癌` 里的「转移性」）。跨过去再试一次。
                j2 = _skip_soft_gap(q, j)
                if j2 > j:
                    items, end = _consume_entity_list(q, j2, spans)
            if not items:
                # 仍然没接上，但右近邻确实有个实体——那这句话到底是「优先 X」还是「只要 X」，
                # 我判不出来。**绝不能**把它交给正向解析吃成硬约束：用户说「优先」，
                # 拿到「只要」，是这个特性文档里写明「比不支持更糟」的那件事。
                nxt = next((s for s in spans if j <= s.start < min(len(q), j + 16)), None)
                if nxt is not None and op not in _hedge_set:
                    ambiguous = True
                    idx = len(q)
                    break
                # hedge 类**不**走这条弃权：这里的「附近有实体」几乎总是
                # 「尽量是 10x 的人类肺数据」这种——10x 是来源专名（在 parse_query 之前就被
                # resolve_search_request 摘走了，不在 spans 里），于是 `nxt` 咬到的是后面本该做
                # 硬条件的「人类」。为此整句弃权，等于用户说了句客气话就什么都拿不到。
                # 「优先」保留弃权：那是用户**明确**要求加权，绑错对象会把偏好偷换成筛选；
                # hedge 落空只是没加权，剩下的硬条件照常执行，代价不对称。
                orphan.append(op)
                _mark(excised, i, i + len(op))     # 标记词本身抹掉，免得再触发残差门
                continue
            for it in items:
                if it.kind == "raw":
                    pref_raw = True
                    continue
                preferred.setdefault(it.dim, [])
                preferred[it.dim].extend(it.targets)
                preferred_disp.setdefault(it.dim, [])
                if it.display and it.display not in preferred_disp[it.dim]:
                    preferred_disp[it.dim].append(it.display)
            _mark(excised, i, end)
    for dim in list(preferred):
        preferred[dim] = _unique(preferred[dim])
    remainder = "".join(" " if excised[k] else ch for k, ch in enumerate(q))
    return (remainder, preferred, preferred_disp, pref_raw, pref_dates,
            _unique(orphan), neg_after_prefer, ambiguous)


def _positive_core(q: str, catalog: dict, today: date | None = None):
    """正向解析内核（历史行为）：返回 (date_from,date_to,working,constraints,display_map,has_raw_positive,residual)。"""
    date_from, date_to, q2 = _parse_dates(q, today)
    constraints, display_map, _matched, working = _match_all_consuming(q2, catalog)
    raw_req = any(a in q2 for a in V.RAW_REQUIRED_ALIASES)
    residual = _residual_salient(working)
    # 保护复合词在 `_match_all_consuming` 里是在 alias 消费**之前**被整体置空的，于是它对残差门
    # 也一并隐身了。对「单核细胞」这种**细胞类型**没问题（它同时在 FILLER_DOMAIN 里，本来就走
    # 「不筛但回显」那条诚实通道）；但对「胰岛素 / 胸腺嘧啶 / 血管紧张素」这种系统**压根不认识**
    # 的分子名，隐身的后果是从「诚实弃权」变成「返回全库 5665 条、零个筛选芯片」——
    # 对抗评审实测：
    #     胰岛素   → results / 5665 条 / active_filters=[] / 首屏全是 CRISPRi K562、PBMC
    #     皮质醇   → abstained（它不在保护表里）           ← 同一词类，两套互斥口径
    # 屏蔽的职责只有一个：别让短别名劫持长词。它不该顺手把「我不认识这个词」也一起抹掉。
    # 判据直接取自数据本身：**在 FILLER_DOMAIN 里 = 有名有姓的无维度描述词（回显即可）；
    # 不在 = 系统不认识（必须计入残差、照常弃权）**，不再另立一张表。
    unknown = [t for t in _protected_terms(q2) if t not in _FILLER_DOMAIN_SET]
    if unknown:
        residual = " ".join(_unique(([residual] if residual else []) + unknown))
    return date_from, date_to, working, constraints, display_map, raw_req, residual


def _consume_entity_list(q: str, start: int, spans: list[_Span]) -> tuple[list[_Span], int]:
    """从 start 起消费『同段紧邻的实体 span 列表』（仅允许连接词/空白分隔）。返回 (items, end)。"""
    items: list[_Span] = []
    i = _skip_ws(q, start)
    while True:
        sp = _span_at(spans, i)
        if sp is None:
            break
        items.append(sp)
        i = sp.end
        j = _skip_ws(q, i)
        conn = _startswith_any(q, j, V.LIST_CONNECTORS)
        if conn:
            i = j + len(conn)
            i = _skip_ws(q, i)
            continue
        # 无连接词但紧邻另一实体（如「小鼠脑」）→ 视为复合负向对象继续消费，
        # 交由 _neg_items_kind_dim 判跨维歧义（NOT(mouse AND brain) 无法表达 → 弃权）
        if _span_at(spans, j) is not None:
            i = j
            continue
        break
    return items, i


def _neg_items_kind_dim(items: list[_Span]) -> tuple[str, str, str]:
    """校验负向实体列表同维/同类。返回 (reason, detail, sole_dim)；reason 空=合法。"""
    kinds = {it.kind for it in items}
    dims = {it.dim for it in items if it.kind == "entity"}
    if len(kinds) > 1 or len(dims) > 1:
        return ("cross_dimension_negative_clause",
                "同一个『排除』里混了不同维度（如物种+组织，或结构化+原始数据），无法安全判定。"
                "请把不同维度拆成各自带『不要』的条件。", "")
    sole = next(iter(dims)) if dims else ""
    return ("", "", sole)


def _apply_exclude(items: list[_Span], excluded: dict, excluded_disp: dict) -> None:
    for it in items:
        if it.kind != "entity" or not it.dim:
            continue
        excluded.setdefault(it.dim, [])
        for t in it.targets:
            if t not in excluded[it.dim]:
                excluded[it.dim].append(t)
        excluded_disp.setdefault(it.dim, [])
        if it.display not in excluded_disp[it.dim]:
            excluded_disp[it.dim].append(it.display)


def _mark(excised: list[bool], a: int, b: int) -> None:
    for k in range(a, b):
        excised[k] = True


def _negation_parse(query: str, q: str, catalog: dict, today: date | None = None) -> QueryIntent:
    """白名单否定语法：只执行明确安全的负向结构，其余一律弃权/澄清。"""
    # 1. 全局危险结构先弃权
    if _contains_any(q, V.NEG_INTERROGATIVE_MARKERS):
        return _mk_abstain(query, "interrogative_negation", "检测到疑问句式，无法把『是否排除…』当作筛选命令，请改成明确要求。")
    if _has_conditional(q):
        return _mk_abstain(query, "conditional_negation", "检测到条件句式（如『除非/如果…不要…』），否定是否生效不明确，请改成无条件约束。")
    if any(ch in q for ch in ('"', "'", "「", "」", "“", "”")) or ("示例" in q) or ("例子" in q):
        return _mk_abstain(query, "quoted_or_metalinguistic_negation", "检测到引号/示例等元语言引用，未把引用内容当作真实约束。")
    if _contains_any(q, _NESTED_MARKERS):
        return _mk_abstain(query, "nested_negation", "检测到双重/嵌套否定（如『不排除』『不是不要』），无法可靠换算，请改写为明确的排除或需要。")
    # 「why not X」是英文建议反问（含义≈「要不要试试 X」），不是排除；「not」进可执行表后
    # 不设这道守卫会把反问静默反向成排除 X（2026-08-17 h41 修复的配套红线）。
    if _WHY_NOT_RE.search(q):
        return _mk_abstain(query, "interrogative_negation", "检测到疑问/建议句式（why not…），未把其中内容当作排除约束，请改成明确要求。")
    # 2026-07-25 产品哲学修正：这里曾对『或』与 hedge 整句弃权。
    #   · 『不要小鼠或大鼠』= ¬(A∨B) = ¬A∧¬B —— 排除侧「命中任一 forbidden 即淘汰」**精确**是这个语义，
    #     反而比一直照做的『不要小鼠和大鼠』（¬(A∧B)）更无歧义。连词本体已进 FILLER_GRAMMAR。
    #   · hedge 交给软偏好语法（`_extract_preferences` 在本函数之前就跑过了），到不了这里。

    # 2. raw 第三态（clarify / drop）优先于短否定操作符
    for pat, action in V.RAW_OPTIONAL_PATTERNS:
        if pat.search(q):
            if action == "clarify":
                return _mk_clarify(query, "ambiguous_raw_requirement", _RAW_CLARIFY_DETAIL, _RAW_CLARIFY_OPTIONS)
            q = pat.sub(lambda m: " " * (m.end() - m.start()), q)  # drop_constraint：抹掉、不设 raw 约束

    spans = _entity_spans(q, catalog)
    excised = [False] * len(q)
    staged_exclude: dict[str, list[str]] = {}
    staged_exclude_disp: dict[str, list[str]] = {}
    staged_raw: bool | None = None

    # 3. raw 硬排除
    for pat in V.RAW_FORBIDDEN_PATTERNS:
        for m in pat.finditer(q):
            staged_raw = False
            _mark(excised, m.start(), m.end())

    # 4. 结构化负向 clause：环缀 → 后缀 → 前缀（中/英）
    # 4a. 环缀 除了X以外
    for (op, close) in V.EXEC_NEG_CIRCUMFIX_CN:
        idx = 0
        while True:
            i = q.find(op, idx)
            if i < 0:
                break
            idx = i + 1
            if excised[i] or _inside_span(i, spans):
                continue
            items, end = _consume_entity_list(q, i + len(op), spans)
            j = _skip_ws(q, end)
            if not items or not q.startswith(close, j):
                continue
            reason, detail, _sole = _neg_items_kind_dim(items)
            if reason:
                return _mk_abstain(query, reason, detail)
            _apply_exclude(items, staged_exclude, staged_exclude_disp)
            _mark(excised, i, j + len(close))

    # 4b. 后缀 X 除外
    for suf in V.EXEC_NEG_SUFFIX_CN:
        idx = 0
        while True:
            k = q.find(suf, idx)
            if k < 0:
                break
            idx = k + 1
            if excised[k]:
                continue
            # 向左找紧邻的实体列表：以 suf 结尾，取紧邻 suf 左侧的实体链条，回溯首个实体的 start
            left = _skip_ws_left(q, k)
            chain = _entity_list_ending_at(q, left, spans)
            if not chain:
                continue
            reason, detail, _sole = _neg_items_kind_dim(chain)
            if reason:
                return _mk_abstain(query, reason, detail)
            _apply_exclude(chain, staged_exclude, staged_exclude_disp)
            _mark(excised, chain[0].start, k + len(suf))

    # 4c. 前缀（中文，长优先）
    for op in sorted(V.EXEC_NEG_PREFIX_CN, key=len, reverse=True):
        idx = 0
        while True:
            i = q.find(op, idx)
            if i < 0:
                break
            idx = i + 1
            if excised[i] or _inside_span(i, spans):
                continue
            items, end = _consume_entity_list(q, i + len(op), spans)
            if not items:
                continue
            reason, detail, sole = _neg_items_kind_dim(items)
            if reason:
                return _mk_abstain(query, reason, detail)
            # D2：负向列表后紧跟『的 + 同维异 target 实体』才允许开边界，否则弃权
            j = _skip_ws(q, end)
            clause_end = end
            if j < len(q) and q[j] == "的":
                p0 = _span_at(spans, _skip_ws(q, j + 1))
                # 『的』后**紧跟实体**才做边界判定：同维异 target=对照模板（adv03）；跨维/原始=歧义弃权。
                # 『的』后是 filler/未知（如「的数据」）→ 只是所有格，非边界问题，正常 excise 负向部分。
                if p0 is not None:
                    if p0.kind != "entity" or p0.dim != sole:
                        # 2026-08-07 产品评审裁决（遗留项 D2）：**维持弃权**——NOT（A∧B) 在
                        # 「跨维 AND + 维内 OR」的过滤模型里本就不可表达，放开任何一种解读
                        # 都是静默误读（把「不要小鼠的肺癌」读成排除全部小鼠 = 误伤小鼠非肺癌
                        # 数据）。提示语补手动筛选出口，不给假通路。
                        return _mk_abstain(query, "ambiguous_negation_scope",
                                           "无法判断否定修饰哪一部分（『的』后是跨维度或原始数据），"
                                           "请改写为明确的『排除 X；需要 Y』；或先正常检索，"
                                           "再用细化筛选手动去掉不想要的子集。")
                    neg_targets = {t for it in items for t in it.targets}
                    if neg_targets & set(p0.targets):
                        return _mk_abstain(query, "conflicting_polarity",
                                           "同一条件同时出现『排除』和『需要』，请只保留一个。")
                # OK：只 excise 负向部分，『的+后缀』留给正向解析
            _apply_exclude(items, staged_exclude, staged_exclude_disp)
            _mark(excised, i, clause_end)

    # 4d. 前缀（英文，词边界 + 紧邻 typed target；操作符表程序生成自 V.EXEC_NEG_PREFIX_EN）
    for m in _EXEC_NEG_PREFIX_EN_RE.finditer(q):
        i = m.start()
        if excised[i] or _inside_span(i, spans):
            continue
        items, end = _consume_entity_list(q, m.end(), spans)
        if not items:
            continue
        reason, detail, _sole = _neg_items_kind_dim(items)
        if reason:
            return _mk_abstain(query, reason, detail)
        _apply_exclude(items, staged_exclude, staged_exclude_disp)
        _mark(excised, i, end)

    # 5. 正向 remainder（excise 掉负向 clause）
    remainder = "".join(" " if excised[i] else ch for i, ch in enumerate(q))

    # 6. 兜底：remainder 里仍有未消费否定形素 → 弃权（防静默反向）
    df, dt, working, constraints, display_map, raw_pos, residual = _positive_core(remainder, catalog, today)
    stray = _leftover_negation(working)
    if stray:
        # 2026-09-11 产品哲学修正（「不替用户弃权」延伸到兜底否定形素，带极性保险丝）：
        # 形素与受控实体**不同从句**（口语碎尾「别笑/不要漏」——没有可否定对象）→ 改道
        # unresolved_term 自动降级通道：忽略该形素、剩余条件重搜并显著标注；硬闸不变。
        # 形素与实体**同从句**（「不是小鼠的」「not want mouse」——极性反转风险为真）→ 维持弃权。
        pos = working.find(stray)
        end = len(working)
        for ch in "，。！？；,.;!?":
            k = working.find(ch, pos + len(stray))
            if k >= 0:
                end = min(end, k)
        clause_has_entity = pos >= 0 and any(
            sp.kind == "entity" and pos + len(stray) <= sp.start < end for sp in spans)
        # 中文否定形素**直接粘连**实义汉字的（『非哺乳类』『别管正常对照』——紧跟 ≥2 个连写汉字）
        # 视同有真实排除对象（词表未收录不代表用户没在说东西），同样维持弃权；
        # 只有碎尾语气（『别笑』『不要漏』——形素后至多 1 个连写汉字）才进降级通道。
        j = pos + len(stray) if pos >= 0 else -1
        run = 0
        if j >= 0:
            while j < len(working) and working[j] in " 	":
                j += 1
            while j < len(working) and "一" <= working[j] <= "鿿":
                run += 1
                j += 1
        if clause_has_entity or run >= 2:
            return _mk_abstain(query, "unsupported_negation",
                               f"检测到排除含义『{stray}』，但当前写法无法安全执行，请改写为明确的『不要 X』或去掉该词。")
        stray_terms = [t for t in str(stray).split() if t] or [str(stray)]
        return _mk_abstain(query, "unresolved_term",
                           _unresolved_detail("、".join(f"「{t}」" for t in stray_terms)),
                           unresolved=stray_terms)

    # 7. 极性一致性
    if staged_raw is False and raw_pos:
        return _mk_abstain(query, "conflicting_polarity", "原始数据同时被要求『需要』和『排除』，请只保留一个。")
    for dim in domain_dimensions():
        if set(constraints.get(dim, [])) & set(staged_exclude.get(dim, [])):
            return _mk_abstain(query, "conflicting_polarity", "同一实体同时出现『需要』和『排除』，请只保留一个。")

    # 8. 残差实义词
    if residual:
        terms = [t for t in residual.split() if t]
        shown = "、".join(f"「{t}」" for t in terms) or f"「{residual}」"
        return _mk_abstain(query, "unresolved_term", _unresolved_detail(shown),
                           unresolved=terms or [residual])

    has_raw = False if staged_raw is False else (True if raw_pos else None)

    # 9. 否定信号在但什么都没抽到（如裸操作符无实体）→ 弃权
    if (not any(staged_exclude.get(d) for d in domain_dimensions())
            and has_raw is None
            and not any(constraints.get(d) for d in domain_dimensions())):
        return _mk_abstain(query, "unsupported_negation",
                           "检测到排除含义，但未能确定要排除什么，请改写为明确的『不要 X』。")

    return QueryIntent(
        original_query=query,
        constraints=constraints,
        excluded_constraints=staged_exclude,
        has_raw_data_required=has_raw,
        display_map=display_map,
        excluded_display=staged_exclude_disp,
        free_text_terms=_extract_free_text_terms(query),
        unused_query_terms=_unused_domain_terms(working) + _protected_echoable(q),
        parse_status=PS_EXECUTABLE,
        date_from=df,
        date_to=dt,
    )


def _skip_ws_left(q: str, i: int) -> int:
    while i > 0 and q[i - 1] in " \t":
        i -= 1
    return i


def _entity_list_ending_at(q: str, end: int, spans: list[_Span]) -> list[_Span]:
    """从 end（不含）向左取紧邻的实体列表（仅连接词/空白分隔）。返回按 start 升序的实体链（空=无）。"""
    items: list[_Span] = []
    i = end
    while True:
        # 找 end==i 的实体
        sp = next((s for s in spans if s.end == i), None)
        if sp is None:
            break
        items.append(sp)
        j = _skip_ws_left(q, sp.start)
        conn = ""
        for t in sorted(V.LIST_CONNECTORS, key=len, reverse=True):
            if j - len(t) >= 0 and q[j - len(t):j] == t:
                conn = t
                break
        if conn:
            i = _skip_ws_left(q, j - len(conn))
            continue
        break
    items.reverse()
    return items


def _is_bare_identifier(query: str) -> bool:
    """整句就是一个标识符（DOI / accession / UUID / GEO / SRA 编号）——不是检索句。

    刻意只认「裸贴」（标识符 == 整句去空白）：句子里**提到**编号的混合诉求
    （如「把 E-MTAB-1234 打包」）仍走正常解析，由上层指路执行动作。

    判等前先做轻量正规化：剥掉首尾句读 / 引号 / 全角括号包装与零宽字符，
    再剥 DOI 解析器前缀（https://doi.org/…、doi:…——复制 DOI 时最常一起带上的头，
    见 identifier_patterns.strip_doi_prefix）——真实世界最高频的粘贴形态是
    「从句尾复制 DOI 带着句点」，逐字判等会把这些放进词面解析，
    重演「数字残片静默丢弃 → executable 空约束 → 全库冒充」
    带前缀裸贴则会掉进 unresolved_term，指路文案教用户「把这些词去掉」——
    去掉后只剩纯数字残片，正好走回全库冒充通道（第二轮）。
    只做这层剥壳，不改动正常检索语义。
    """
    q = _strip_doi_prefix(_strip_bare_wrapper(query))
    hit = _classify_identifier(q)
    return bool(hit and hit["value"] == q)


#: 裸标识符判等前要剥掉的首尾包装：句读 / 斜杠 / 引号 / 全角与半角括号。
#: DOI、accession、UUID 本体都不会以这些字符开头或结尾，剥壳不会伤到标识符本身。
_BARE_WRAPPER_CHARS = (
    " \t\r\n"
    ".,;:!?/·~"
    "。，、；：！？"
    "\"'“”‘’「」『』"
    "()（）【】《》〈〉〔〕"
)

#: 零宽字符（粘贴常夹带）：U+200B/C/D 与 BOM。视觉不可见，但逐字判等会被它打穿。
_ZERO_WIDTH_RE = re.compile(r"[​‌‍﻿]")


def _strip_bare_wrapper(text: str) -> str:
    """剥裸标识符外壳：NFC 正规化 → 去零宽字符 → 剥首尾句读/引号/括号包装。"""
    t = unicodedata.normalize("NFC", text or "")
    t = _ZERO_WIDTH_RE.sub("", t)
    return t.strip(_BARE_WRAPPER_CHARS)


def _identifier_residue(query: str, value: str) -> str:
    """原始查询剔除标识符本体后的残留（剥掉包装字符）。非空 = 句子里除编号外还有实义内容。"""
    rest = (query or "").replace(value, " ")
    rest = _ZERO_WIDTH_RE.sub("", rest)
    return rest.strip(_BARE_WRAPPER_CHARS)


#: 裸 DOI 残片：注册前缀形态（10.XXXX，尾随斜杠已被剥壳字符吃掉）。`10.1101/2021` 这类
#: 形态完整的由 identifier_patterns.classify 认，不走这里；这里只收「形似 DOI 但没写全」的输入。
_BARE_DOI_FRAGMENT_RE = re.compile(r"^10\.\d{4,9}$")

#: 句中 DOI 残片搜索（兜底闸用）：`(?!\S)` 保证后面不接 DOI 后缀——完整 DOI 已由 classify 处理。
_DOI_FRAGMENT_IN_TEXT_RE = re.compile(r"\b10\.\d{4,9}\b(?!\S)")


def _bare_doi_fragment(query: str) -> bool:
    """剥壳（含 DOI 解析器前缀）后整串就是一个 DOI 残片（查询主体就是这个残片）。"""
    return bool(_BARE_DOI_FRAGMENT_RE.match(_strip_doi_prefix(_strip_bare_wrapper(query))))


def _doi_fragment_in_text(query: str) -> str | None:
    """句中找「形似 DOI 但没写全」的残片（兜底闸用）。返回残片文本（None=无）。

    版本号语境不算残片，两类排除（2026-08-04 第二轮的对照组）：
    - 残片是更长点分版本串的一段（前一字符是数字或点，如 cellranger 3.10.10380）——
      DOI 注册前缀永远是一个 token 的开头，不会接在「3.」后面；
    - 残片紧邻「版本」标记（10.10380 版本 / 版本 10.1234）——这个数字被用户明说为
      版本号，不是没写全的 DOI。把版本号句拦进 identifier_fragment 等于跟用户说
      「你没写全」，是没收正常输入。
    """
    q = query or ""
    for m in _DOI_FRAGMENT_IN_TEXT_RE.finditer(q):
        if m.start() > 0 and (q[m.start() - 1].isdigit() or q[m.start() - 1] == "."):
            continue
        if q[:m.start()].rstrip().endswith("版本") or q[m.end():].lstrip().startswith("版"):
            continue
        return m.group(0)
    return None


def _residue_has_substance(residue: str) -> bool:
    """剔除残片后的残留是否还含**实义**内容（实义 → 不拦，保守放行正常检索句）。

    判据单一真源 = vocabulary 的 FILLER 拆表，不另立 stopword 表：
    - FILLER_DOMAIN（性别/年龄/细胞类型/cellranger 这类系统结构上无对应维度的实义
      描述词）或未收录实义词 → 有实义；
    - 纯 FILLER_GRAMMAR（帮我查/这个/数据集/用…语法客套元词）→ 无实义——
      stopword 包裹的残片视同零残留，进 identifier_fragment 诚实通道。
    filler 一律换成分隔符而不是删除——删除会把左右残字拼成用户没打过的幻影词
    （同 _residual_salient 2026-07-22 的教训）。
    """
    text = (residue or "").lower()
    for tok in sorted({f.lower() for f in V.FILLER_DOMAIN}, key=len, reverse=True):
        if tok and tok in text:
            return True
    for tok in sorted({f.lower() for f in V.FILLER_GRAMMAR}, key=len, reverse=True):
        text = text.replace(tok, " ")
    if any(len(run) >= 2 for run in re.findall(r"[一-鿿]+", text)):
        return True
    return any(len(w) >= 3 for w in re.findall(r"[a-z]{2,}", text))


#: DOI 残片弃权文案（fail-closed，与 identifier_direct 同一条诚实通道：不检索、不倒全库）。
#: classify 不认残片（不构成可定位标识符），identifiers.lookup 也不会挂反查条——
#: 文案必须把「为什么什么结果都没有」说圆：看起来像没写全，补全再试。
_IDENTIFIER_FRAGMENT_DETAIL = (
    "看起来像没写全的 DOI 或编号（完整 DOI 形如 10.xxxx/xxxxx）——拿残片当查询没有可筛的条件，"
    "这次没有做全库检索，免得倒给你一堆不相干的数据集。请补全编号后再试；"
    "或改用物种、疾病、技术这类条件来描述你要找的数据。"
)


#: 裸标识符弃权文案（fail-closed）。**绝不退化成全库检索**：DOI 这类编号会被词面解析拆成
#: 纯数字残片静默丢弃 → executable 空约束 → 全库 5712 条冒充「满足基本检索条件」。
#: 与 GSE 编号的弃权同一条诚实通道：不检索、由 identifiers.lookup 的反查条如实应答。
_IDENTIFIER_DIRECT_DETAIL = (
    "这是一个数据集标识符（编号 / DOI），不是检索句——检索按条件匹配，拿它当查询没有可筛的条件。"
    "已改走标识符精确反查（结果见上方的标识符条）：库里查得到会直达该数据集；"
    "查不到会如实说明、并指路来源库，这次没有做全库检索。"
)


def parse_query(query: str, keyword_mapping: dict[str, list[dict[str, Any]]] | None = None,
                today: date | None = None) -> QueryIntent:
    """中文/英文 query → QueryIntent。keyword_mapping 保留以兼容旧调用（override 合并进 CATALOG）。
    today：相对日期（近N年/今年…）的换算基准，默认 date.today()（一次算好、贯穿整次解析；测试可 pin）。"""
    catalog = _merged_catalog(keyword_mapping)
    if today is None:
        today = date.today()
    q = _neutralize_existential(query.lower())

    # 裸标识符（整句就是一个 DOI/编号）→ fail-closed 弃权，与 GSE 同通道（见 _IDENTIFIER_DIRECT_DETAIL）。
    # 必须先于一切词面解析：DOI 会被拆成数字残片静默丢弃，走出「executable 空约束 → 全库冒充结果」。
    if _is_bare_identifier(query):
        return _mk_abstain(query, "identifier_direct", _IDENTIFIER_DIRECT_DETAIL)

    # 裸 DOI 残片（「10.1038」「10.1038/」这类形似 DOI 但没写全的输入）：classify 不认，
    # 词面解析同样会把数字静默丢弃 → executable 空约束 → 全库冒充。同通道 fail-closed，
    # 文案点明「补全编号再试」。只拦「查询主体就是残片」——正常检索句里的版本号不受影响。
    if _bare_doi_fragment(query):
        return _mk_abstain(query, "identifier_fragment", _IDENTIFIER_FRAGMENT_DETAIL)

    # 负向日期先于 _parse_dates 检测（否则日期文字先被摘掉，只剩会被残差门忽略的单字否定）
    if re.search(r"(?:不早于|不晚于|不迟于)\s*" + _YEAR, q) or \
       re.search(_YEAR + r"\s*年?\s*(?:除外|不算)", q) or \
       re.search(r"(?<![a-z])not\s+(?:before|after|since)\s+" + _YEAR, q):
        return _mk_abstain(query, "negative_date",
                           "暂不支持负向日期（如『不早于/不晚于/年份除外』），请改写为明确起止年份。")

    # 相对时间年数不明确（近几年/近年来/近些年）→ fail-closed 弃权，而不是静默丢弃时间约束。
    if _AMBIGUOUS_REL_RE.search(q):
        return _mk_abstain(query, "ambiguous_relative_date",
                           "相对时间（如『近几年 / 近年来』）年数不明确，无法换算成具体区间；"
                           "请给出明确年数（如『近3年』）或用绝对年份。")

    # 非法相对年数（近0年 / 近-1年）→ 弃权（此前时间约束被静默丢弃、仍返回普通结果）。
    if _ZERO_NEG_REL_RE.search(q):
        return _mk_abstain(query, "invalid_relative_date",
                           "相对时间年数必须为正整数（如『近3年』）；『近0年 / 近-1年』无意义，请修正后重试。")
    # 非法日历日期（月>12 / 该月不存在的日，如 2020年13月、2020年2月30日）→ 弃权（而非静默放宽成整年）。
    _mymd = _YMD_RE.search(q)
    if _mymd:
        _yy, _mm = int(_mymd.group(1)), int(_mymd.group(2))
        _dd = int(_mymd.group(3)) if _mymd.group(3) else 1
        try:
            date(_yy, _mm, _dd)
        except ValueError:
            return _mk_abstain(query, "invalid_date",
                               f"日期『{_mymd.group(0)}』不是合法日历日期；请检查月(1-12)/日，或只写年份。")
    # 并列年份（X年和/与/、Y年）：是年份区间还是恰好这几年？语义歧义 → 弃权（此前只取前一个年份、忽略其余）。
    _mmy = _MULTI_YEAR_RE.search(q)
    if _mmy and _mmy.group(1) != _mmy.group(2):
        return _mk_abstain(query, "ambiguous_multi_year",
                           f"检测到并列年份『{_mmy.group(0)}』，无法确定是年份区间还是恰好这几年；"
                           "请用『2020到2022年』表示区间，或分开查询。")

    # 软偏好（优先 X）先抽走：命中的整段从查询里抹成等长空格，后续所有路径（否定检测、正向解析、
    # 残差门）看到的都是同一个「已去偏好」的串。放在这里而不是各分支内部，是为了让正负两条路径
    # 对同一句话的理解不分叉——分叉过一次就是「同一份事实两处手抄」的老毛病。
    (q, _pref, _pref_disp, _pref_raw, _pref_dates,
     _pref_orphan, _pref_neg, _pref_ambig) = _extract_preferences(q, catalog, today)
    if _pref_neg:
        return _mk_abstain(query, "unsupported_soft_exclusion",
                           "「优先不要 X」这种说法暂不支持：系统只能做到「一条都不要 X」，"
                           "做不到「尽量少要 X」。请改成「不要 X」，或者去掉「优先」。")
    if _pref_ambig:
        return _mk_abstain(query, "ambiguous_preference_scope",
                           "没能确定「优先」是在说后面哪一项。请把它紧挨着要优先的那个词，"
                           "比如「人类肺数据，优先 Visium」。")

    # 检测否定信号：先做一次乐观正向解析，看工作串是否残留未消费否定形素；或命中 raw 特殊模式
    _df, _dt, working0, _c0, _d0, _r0, _res0 = _positive_core(q, catalog, today)
    neg_signal = bool(_leftover_negation(working0))
    if not neg_signal:
        for pat in V.RAW_FORBIDDEN_PATTERNS:
            if pat.search(q):
                neg_signal = True
                break
    if not neg_signal:
        for pat, _act in V.RAW_OPTIONAL_PATTERNS:
            if pat.search(q):
                neg_signal = True
                break

    if _date_ranges_disjoint(_pref_dates, (_df, _dt)):
        return _mk_abstain(query, "conflicting_polarity",
                           "要筛的发表时间和要优先的发表时间没有重叠，优先那一条对筛出来的结果"
                           "一条都不成立，请只保留一个。")

    if not neg_signal:
        # 无否定信号 → 既有正向路径（与历史逐位一致）
        intent = QueryIntent(
            original_query=query,
            constraints=_c0,
            has_raw_data_required=(True if _r0 else None),
            display_map=_d0,
            free_text_terms=_extract_free_text_terms(query),
            # `_protected_terms` 补进来的是被整体屏蔽掉的复合词（单核细胞/皮质醇…）：
            # 它们不落任何维度，若不回显就又是一次静默丢词。
            unused_query_terms=_unused_domain_terms(working0) + _protected_echoable(q) + _pref_orphan,
            preferred_constraints=_pref,
            preferred_display=_pref_disp,
            preferred_raw=_pref_raw,
            preferred_date_from=_pref_dates[0],
            preferred_date_to=_pref_dates[1],
            parse_status=PS_EXECUTABLE,
            date_from=_df,
            date_to=_dt,
        )
        # 2026-07-25 产品哲学修正：此处曾对「或」与 hedge 整句弃权（`unsupported_boolean_or` /
        # `unsupported_hedge`）。实测那两档给用户的是 **0 条结果 + 0 个放宽选项 + 0 个降级**，
        # 而能力其实一直都在：
        #     「优先 Xenium 的黑色素瘤数据」→ 55 条      「最好是 Xenium 的黑色素瘤数据」→ 0 条
        # 现在：hedge 由 `_extract_preferences` 当软偏好照做（到不了这里）；「或」按引擎的真实
        # 语义执行——同维度多值就是「或」——并把实际处理方式如实回显进 `or_handling`。
        intent.or_handling = _describe_or_handling(query, _c0, _d0,
                                                   preferred=_pref, preferred_display=_pref_disp)
        if _res0:
            terms = [t for t in _res0.split() if t]
            shown = "、".join(f"「{t}」" for t in terms) or f"「{_res0}」"
            return _mk_abstain(query, "unresolved_term", _unresolved_detail(shown),
                               unresolved=terms or [_res0])
        # 光秃秃的执行指令（「打包前20条」——有执行词、却没任何检索目标）→ 弃权，
        # 绝不把整个库当结果倒出去。宽泛/来源类查询不落这里（见 _is_bare_action_command）。
        if _is_bare_action_command(query, intent):
            return _mk_abstain(query, "action_only", _ACTION_ONLY_DETAIL)
        # 兜底闸（根因门）：executable 空约束 + 句中含标识符 + 剔除标识符后仍有实义残留
        # → 弃权 fail-closed。「帮我查 <DOI> 这个数据集」这类中文包裹不是裸贴，
        # _is_bare_identifier 的剥壳判等拦不住；词面解析把编号拆成数字残片静默丢弃后，
        # 空约束会拿全库 top-N 冒充结果。宁可如实走标识符
        # 诚实通道（由 identifiers.lookup 反查条应答），也绝不退化成全库检索。
        if (not any(_c0.get(d) for d in domain_dimensions())
                and not _r0 and not _df and not _dt):
            hit = _classify_identifier(query)
            if hit and _identifier_residue(query, hit["value"]):
                return _mk_abstain(query, "identifier_direct", _IDENTIFIER_DIRECT_DETAIL)
            # 残片同款根因：句中 DOI 残片 + 剔除后无实义残留（如「10.1038，10.1038」）→ 同通道弃权。
            # 残留判定看「实义」不看「非空」（2026-08-04 第二轮）：纯 stopword 包裹
            # （帮我查 10.1038 这个数据集 / 我用 10.1234 这个）视同零残留照样拦——否则词面解析
            # 把残片静默丢弃后，空约束拿全库 top-N 冒充结果；含实义描述词的不拦
            # （保守：实义词出现在句子里不该被没收检索），版本号语境由 _doi_fragment_in_text 豁免。
            frag = _doi_fragment_in_text(query)
            if frag and not _residue_has_substance(_identifier_residue(query, frag)):
                return _mk_abstain(query, "identifier_fragment", _IDENTIFIER_FRAGMENT_DETAIL)
        return intent

    # 有否定信号 → 白名单否定语法
    intent = _negation_parse(query, q, catalog, today)
    if intent.parse_status == PS_EXECUTABLE:
        # 「不要小鼠，优先小鼠」这种自相矛盾：偏好命中的记录已被硬排除筛掉，加权永远不会生效，
        # 屏幕上却会挂着一条「优先·物种：Mouse」——那是在给一个不可能发生的事背书。
        for dim, targets in _pref.items():
            if set(targets) & set(intent.excluded_constraints.get(dim, [])):
                return _mk_abstain(query, "conflicting_polarity",
                                   "同一个条件既被排除又被要求优先，无法同时成立，请只保留一个。")
        # raw 也要查。只查六个结构化维度的话，「不要 FASTQ 的肺数据，优先有 FASTQ」照样放行：
        # 屏幕上并排挂着「不要 FASTQ」和「优先有 FASTQ」，而后者对存活集里**每一条**都不成立。
        if _pref_raw and intent.has_raw_data_required is False:
            return _mk_abstain(query, "conflicting_polarity",
                               "原始数据既被要求排除又被要求优先，无法同时成立，请只保留一个。")
        intent.preferred_constraints = _pref
        intent.preferred_display = _pref_disp
        intent.preferred_raw = _pref_raw
        intent.preferred_date_from, intent.preferred_date_to = _pref_dates
        intent.unused_query_terms = list(intent.unused_query_terms) + _pref_orphan
        # 否定路径也要如实回显「或」怎么处理的。正负两侧**分开传**——
        # 「不要小鼠或大鼠」的多值落在 excluded_constraints，合并成一份会把「都排除」说成「都算」。
        intent.or_handling = _describe_or_handling(
            query, intent.constraints, intent.display_map,
            excluded=intent.excluded_constraints, excluded_display=intent.excluded_display,
            preferred=_pref, preferred_display=_pref_disp)
    # 否定路径同样兜底：一句只有执行指令、没有检索目标的话不该命中全库。
    if intent.parse_status == PS_EXECUTABLE and _is_bare_action_command(query, intent):
        return _mk_abstain(query, "action_only", _ACTION_ONLY_DETAIL)
    return intent
