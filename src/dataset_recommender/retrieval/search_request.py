# -*- coding: utf-8 -*-
"""共享查询入口解释：把数据来源专名从自然语言中安全解析为语料范围。

时间、生物实体与否定仍由 :mod:`query_parser` 负责；本模块只解决一个必须发生在装载语料之前的
问题：来源决定要读哪些库，因此不能等到 QueryIntent 创建后再处理。Web 与 MCP 都调用这里，避免
浏览器维护一套来源别名、MCP 又要求调用者手写精确来源名。

自动识别遵循保守原则：只认高辨识度专名；一旦句中出现排除/否定语义就完全不自动收窄；显式来源
池与查询点名冲突时尊重显式池且不删词，让后续 fail-closed 解析暴露矛盾，而不是静默改写意图。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

from . import query_lexicon as V
from ..domain.registry import domain_catalog


# alias 顺序有语义：`resolve_search_request` 与 `source_alias_spans` 都只认**声明顺序里第一个命中的**
# alias（命中即 break），所以长名必须排在短名前面，否则 "10x genomics" 会被裸 "10x" 抢先匹配，
# 去词时只抹掉 "10x"、把孤零零的 "genomics" 留在句子里再触发一次残差弃权。
#
# 2026-07-22：补上口语简称。此前只认 "10x genomics"/"10x 官方"，用户写「来自10x」时来源识别恒空——
# 而且不是弃权、是**静默不筛**（"10x" 里的 "x" 在残差门的 `[a-z]{2,}` 下取不到 2 字母词，
# 于是既没被当来源、也没触发弃权，用户以为按 10x 筛了、其实没有）。
# 简称的收录门槛：必须是这个来源在中文语境里**唯一**的常用简称，且 ASCII 简称有词边界保护
# （`_token_pattern` 的 `(?<![a-z0-9])…(?![a-z0-9])`，故 "110x"/"10xgenomics" 不会误命中）。
# 刻意**不收** "ae"（ArrayExpress）：两字母、与英文缩写和基因名大面积撞车，收了会把无关查询
# 静默改成只查 ArrayExpress——静默改检索池比认不出来严重得多。
SOURCE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CELLxGENE Discover", ("cellxgene discover", "cell x gene discover", "cellxgene", "cxg", "czi cell", "细胞图谱cellxgene")),
    ("Human Cell Atlas", ("human cell atlas", "人类细胞图谱", "人体细胞图谱", "hca")),
    ("ArrayExpress", ("arrayexpress", "array express", "array-express")),
    # 刻意**不收**裸 "encode"：它是普通英文动词（"factors that encode…"），收了会把无关查询
    # 静默改成只查 ENCODE——与上方不收 "ae" 同理：静默改检索池比认不出来严重得多。
    # 只收带 project/portal/数据库 等高辨识度语境的复合写法。
    ("ENCODE", ("encode project", "encodeproject", "encode portal", "encode 数据库", "encode 数据")),
    (
        "EBI Single Cell Expression Atlas",
        ("single cell expression atlas", "single-cell expression atlas", "ebi scea", "scea", "ebi 单细胞表达图谱"),
    ),
    # 2026-08-06 三源接入（HuBMAP / SCP / GEO）：别名沿用「高辨识度专名」门槛。
    # "scp" 在生物数据语境里唯一指向 Single Cell Portal（SCP2 基因写作 scp2，词边界保护不会误命中）；
    # "geo" 收裸形——它是中文生信语境的标准说法，且 GeoMx/geometry 这类都被词边界挡住；
    # GSM/SRA 等编号形态不走本表（走 identifier_patterns 的标识符路径）。
    ("HuBMAP", ("hubmap consortium", "hubmap")),
    (
        "Broad Single Cell Portal",
        ("broad single cell portal", "single cell portal", "single-cell portal", "scp"),
    ),
    ("NCBI GEO", ("ncbi geo", "geo 数据库", "geo 数据", "geo")),
    # 2026-08-14 Zenodo 首批入库（external/zenodo.json 非空）后登记检索侧别名：
    # "zenodo" 是高辨识度专名（通用开放仓储，无普通词义），词边界保护下不会误命中。
    ("Zenodo", ("zenodo 数据库", "zenodo 数据", "zenodo")),
    # 2026-08-14 refine.bio 首批入库（external/refinebio.json 非空，第 11 源）后登记检索侧别名：
    # "refine.bio"/"refinebio" 是高辨识度专名（GEO/SRA/AE 统一加工镜像，无普通词义）。
    ("refine.bio", ("refine.bio 数据库", "refine.bio 数据", "refine.bio", "refinebio", "refine bio")),
    ("10x Genomics", ("10x genomics", "tenx genomics", "10x 官方", "10x官方", "10x", "tenx")),
)

# 来源限定语境的否定/排除前缀（「除了 GEO」「other than 10x」）：词源**程序取自** vocabulary
# 否定语素锚点（交集，锚点本身按长度降序），不再手抄——本仓库在手抄词表上漂移过多次
# （见 vocabulary.NEG_MORPHEMES_CN 头注）。「除了/除去/不限于」是来源限定专用说法、不是
# 排除动作（「除了」在锚点仅以环缀开词存在），列在 EXTRA。
_SOURCE_NEG_VOCAB_CN = frozenset({"排除", "剔除", "去掉", "不含", "不包含", "不要", "不是", "不用", "非"})
_SOURCE_NEG_PREFIX_EXTRA_CN = ("除了", "除去", "不限于")
_SOURCE_NEG_VOCAB_EN = frozenset({"other than", "except", "exclude", "excluding", "without", "omit", "skip"})


def _source_negation_prefix_re() -> "re.Pattern[str]":
    cn = [m for m in V.NEG_MORPHEMES_CN if m in _SOURCE_NEG_VOCAB_CN] + list(_SOURCE_NEG_PREFIX_EXTRA_CN)
    en = [r"\s+".join(re.escape(p) for p in m.split())
          for m in V.NEG_MORPHEMES_EN if m in _SOURCE_NEG_VOCAB_EN]
    alts = sorted([re.escape(m) for m in cn] + en, key=len, reverse=True)  # 长词优先（同 _prefer_prefix_re 口径）
    return re.compile(r"(?:" + "|".join(alts) + r")\s*$", re.IGNORECASE)


SOURCE_NEGATION_PREFIX_RE = _source_negation_prefix_re()
SOURCE_NEGATION_SUFFIX_RE = re.compile(r"^\s*(?:以外|之外)", re.IGNORECASE)

# 「优先 10x」：这是**偏好**，不是范围收窄。若照常自动收窄，用户说「优先」却再也看不到别的来源，
# 等于把偏好偷换成筛选。命中时：不收窄来源池、把专名连同标记词一起去掉、把该来源记进
# preferred_sources 交给排序层加权（见 retriever.PREFERENCE_BOOST）。
# 词表**由 vocabulary 程序生成**，不再手抄。旧版是一行手写的 `(?:优先考虑|优先选择|优先|…)`，
# 与 `V.SOFT_PREFER_PREFIX_CN` 是两份手抄——当场已经漂了一处（虚字表少一个「带」）。
# 2026-07-25 起 hedge（最好 / 尽量 / 如果有…）也是软偏好标记，所以「最好是 10x 的人类肺数据」
# 必须和「优先 10x…」走同一条路；hedge 的虚字表刻意不含「的」（见 V.HEDGE_CONNECTOR_CHARS）。
def _prefer_prefix_re() -> "re.Pattern[str]":
    def group(markers: "Sequence[str]", conn: str) -> str:
        alts = "|".join(re.escape(m) for m in sorted(markers, key=len, reverse=True))
        return rf"(?:{alts})\s*[{re.escape(conn)}]?\s*$"
    return re.compile(group(V.SOFT_PREFER_PREFIX_CN, V.PREFER_CONNECTOR_CHARS)
                      + "|" + group(V.HEDGE_PREFER_PREFIX_CN, V.HEDGE_CONNECTOR_CHARS))


SOURCE_PREFER_PREFIX_RE = _prefer_prefix_re()


#: 「10x/tenx」裸简称专属的 assay 前瞻护栏词表缓存（见 _token_pattern）；
#: 按活动领域 catalog 对象身份键控——换域自动重建，不串词表（与 domain.vocab_tools 同纪律）。
_ASSAY_GUARD_WORDS: frozenset[str] | None = None
_ASSAY_GUARD_WORDS_CATALOG_ID: int | None = None


def _assay_guard_words() -> frozenset[str]:
    """「10X Multiome / 10X ATAC」这类**试剂盒名**的尾巴词：活动领域 catalog assay 维的
    ASCII 别名/target（单一真源，词表扩了自动跟随）。CJK 别名刻意不收——「10x 的多组学数据」
    里的「10x」仍按来源读（歧义句往「不静默改检索池」的保守方向倒）。"""
    global _ASSAY_GUARD_WORDS, _ASSAY_GUARD_WORDS_CATALOG_ID
    catalog = domain_catalog()
    if _ASSAY_GUARD_WORDS is None or _ASSAY_GUARD_WORDS_CATALOG_ID != id(catalog):
        _ASSAY_GUARD_WORDS = frozenset(
            str(alias).lower().strip()
            for entry in catalog.get("assay", [])
            for alias in list(entry.get("aliases", [])) + list(entry.get("targets", []))
            if re.fullmatch(r"[a-z0-9][a-z0-9-]{2,}", str(alias).lower().strip())
        )
        _ASSAY_GUARD_WORDS_CATALOG_ID = id(catalog)
    return _ASSAY_GUARD_WORDS


def _token_pattern(token: str) -> re.Pattern[str]:
    escaped = re.escape(token)
    if any(ord(ch) > 127 for ch in token):
        return re.compile(escaped, re.IGNORECASE)
    guard = ""
    if token.lower() in ("10x", "tenx"):
        # 2026-08-06 B6：「10X Multiome」「10X ATAC」是**试剂盒名（assay）**，不是 10x 数据来源——
        # 裸 "10x" 曾把它们误判成点名来源（实测：'10X Multiome 的人类肺癌数据' → 检索池收窄到
        # 10x，其它库的同 assay 数据全丢；agent 侧还被 _named_source_violation 错逼填 10x）。
        # 护栏 = 词边界后再加 assay 词前瞻：紧跟 assay 词时不算来源专名。做进 pattern（而非判后
        # 过滤），resolve_search_request 与 source_alias_spans 两个消费点自动逐位同构。
        assay = "|".join(sorted(_assay_guard_words(), key=len, reverse=True))
        if assay:
            guard = rf"(?!\s*-?\s*(?:{assay})(?![a-z0-9]))"
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])" + guard, re.IGNORECASE)


def _is_source_negated(query: str, pattern: re.Pattern[str]) -> bool:
    """只在否定词紧邻来源专名时触发守卫，避免“非小细胞肺癌”等合法实体误伤来源解析。"""
    match = pattern.search(query)
    if not match:
        return False
    before = query[max(0, match.start() - 24):match.start()]
    after = query[match.end():match.end() + 8]
    return bool(SOURCE_NEGATION_PREFIX_RE.search(before) or SOURCE_NEGATION_SUFFIX_RE.search(after))


def _preferred_source_spans(query: str, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
    """找出「<优先标记><来源专名>」的整段位置（含标记词）。空 = 该来源不是被「优先」修饰的。"""
    out: list[tuple[int, int]] = []
    for match in pattern.finditer(query):
        before = query[max(0, match.start() - 12):match.start()]
        m = SOURCE_PREFER_PREFIX_RE.search(before)
        if m:
            out.append((match.start() - (len(before) - m.start()), match.end()))
    return out


def source_alias_spans(query: str, known_sources: "Sequence[str] | None" = None) -> list[dict]:
    """只读：标出查询里每一段数据来源专名的位置。纯函数，不装载语料、不改任何解析行为。

    条件板（board.py）用它建立「来源保护区」：来源专名在 `resolve_search_request` 里是**先于**
    `parse_query` 被整段剥掉的（见本文件下方 `cleaned` 那段），所以任何在原句上做实体定位的模块
    如果不先把专名遮蔽，就会把「人类细胞图谱」里的「人类」当成物种约束——那既是凭空多出的条件，
    也会在改写时把专名打碎、静默换掉检索池。

    与 `resolve_search_request` 共用同一套 `_token_pattern`，保证两边认定的边界一致。
    `known_sources=None`（缺省）表示不按可用来源过滤：比服务端更宽，是 fail-closed 方向
    （宁可多保护一段，也不要漏保护）。

    返回 `[{source, alias, start, end}]`，按 start 升序、互不重叠（长 alias 优先占位，与正向解析的
    「长 alias 先消费」同构）。
    """
    text = str(query or "")
    known = set(known_sources) if known_sources is not None else None
    candidates: list[dict] = []
    for source, aliases in SOURCE_ALIASES:
        if known is not None and source not in known:
            continue
        # 与 resolve_search_request 逐位同构：每个来源**只认声明顺序里第一个命中的 alias**（它 break），
        # 然后把该 alias 的**全部**出现位置都算进去（它用 pattern.subn 全串替换）。
        # 若改成「所有 alias 里挑最长」，遮蔽范围会比服务端真正剥掉的更宽，
        # 自检时 masked_query 与 resolution.parsed_query 对不上，把本来能改的句子挡成 fail-closed。
        for alias in aliases:
            matches = list(_token_pattern(alias).finditer(text))
            if not matches:
                continue
            for match in matches:
                candidates.append(
                    {"source": source, "alias": alias, "start": match.start(), "end": match.end()}
                )
            break
    candidates.sort(key=lambda s: (s["end"] - s["start"], -s["start"]), reverse=True)
    occupied = [False] * (len(text) + 1)
    chosen: list[dict] = []
    for span in candidates:
        if any(occupied[span["start"]:span["end"]]):
            continue
        for pos in range(span["start"], span["end"]):
            occupied[pos] = True
        chosen.append(span)
    chosen.sort(key=lambda s: s["start"])
    # 「优先<来源>」在 resolve_search_request 里是**连同标记词一起**剪掉的。任何拿本函数
    # 重建保护区的模块（条件板的来源自检就是）如果只遮专名、留下「优先」两个字，
    # 两串永远对不上——自检会把「来源被改动了」当成拒绝理由，而来源其实一个字都没变。
    # 判定必须与 resolve_search_request 同为**全有或全无**：某来源只要有一处没写「优先」，
    # 那边就按普通来源收窄（不剪标记词），这边也不能把标记词算进遮蔽范围。
    prefix_len: dict[int, int] = {}
    for pos, span in enumerate(chosen):
        before = text[max(0, span["start"] - 12):span["start"]]
        m = SOURCE_PREFER_PREFIX_RE.search(before)
        prefix_len[pos] = (len(before) - m.start()) if m else 0
    by_source: dict[str, list[int]] = {}
    for pos, span in enumerate(chosen):
        by_source.setdefault(span["source"], []).append(pos)
    for positions in by_source.values():
        if all(prefix_len[p] > 0 for p in positions):
            for p in positions:
                chosen[p]["start"] -= prefix_len[p]
    return chosen


def mask_source_spans(query: str, spans: "Sequence[dict] | None" = None) -> str:
    """把来源专名整段替换成**等长**空格，索引与原句逐位对齐。

    等长遮蔽（而不是删字符）是有意的：后续实体定位、替换、贴回都按原句索引操作，删字符会让索引漂移，
    还可能让左右残字相邻拼出一个原本不存在的别名。
    """
    text = str(query or "")
    marks = list(spans) if spans is not None else source_alias_spans(text)
    if not marks:
        return text
    chars = list(text)
    for span in marks:
        for pos in range(max(0, int(span["start"])), min(len(chars), int(span["end"]))):
            chars[pos] = " "
    return "".join(chars)


@dataclass(frozen=True)
class SearchRequestResolution:
    original_query: str
    parsed_query: str
    sources: list[str] | None
    source_mode: str
    detected_sources: list[str] = field(default_factory=list)
    removed_terms: list[str] = field(default_factory=list)
    automatic_requested: bool = False
    automatic_skipped_reason: str | None = None
    # 「优先 <来源>」：只加权、不收窄。空 = 没有来源偏好。
    preferred_sources: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "original_query": self.original_query,
            "parsed_query": self.parsed_query,
            "sources": list(self.sources) if self.sources is not None else None,
            "source_mode": self.source_mode,
            "detected_sources": list(self.detected_sources),
            "removed_terms": list(self.removed_terms),
            "automatic_requested": self.automatic_requested,
            "automatic_skipped_reason": self.automatic_skipped_reason,
            "preferred_sources": list(self.preferred_sources),
        }


def resolve_search_request(
    query: str,
    sources: Sequence[str] | None,
    known_sources: Sequence[str],
    *,
    auto_parse_sources: bool = False,
) -> SearchRequestResolution:
    """解析来源专名并返回送入 query_parser 的查询。

    ``sources`` 是调用方允许的来源池。Web 自动模式传全部来源，识别到专名后在池内收窄；MCP
    默认传 None，未点名时仍保持基础语料默认，点名时才扩到对应公开来源。手动模式把
    ``auto_parse_sources`` 设为 False，完全尊重显式选择。
    """
    original = str(query or "")
    explicit = [str(x).strip() for x in (sources or []) if str(x).strip()] or None

    known = set(known_sources)
    matches: list[tuple[str, str, re.Pattern[str]]] = []
    for source, aliases in SOURCE_ALIASES:
        if source not in known:
            continue
        for alias in aliases:
            pattern = _token_pattern(alias)
            if pattern.search(original):
                matches.append((source, alias, pattern))
                break

    # 「优先 <来源>」先分流出去：这些来源只加权、不参与范围收窄。
    # 专名连同「优先」一起从查询里去掉，否则孤零零的「优先」会被下游残差门当成未识别词。
    preferred_sources: list[str] = []
    prefer_cuts: list[tuple[int, int]] = []
    remaining: list[tuple[str, str, re.Pattern[str]]] = []
    for source, alias, pattern in matches:
        cuts = _preferred_source_spans(original, pattern)
        if cuts and len(cuts) == len(pattern.findall(original)):
            preferred_sources.append(source)
            prefer_cuts.extend(cuts)
        else:
            remaining.append((source, alias, pattern))
    matches = remaining
    # 后续所有分支都以「已去掉偏好来源专名」的句子为准，避免某个分支忘了去词、
    # 把「优先」和专名一起漏给下游解析器（两份手抄必漂移，这里只留一份）。
    base_query = re.sub(r"\s+", " ", mask_source_spans(
        original, [{"start": a, "end": b} for a, b in prefer_cuts])).strip() if prefer_cuts else original

    # 手动来源模式**也要**认「优先<来源>」。它只影响加权与去词、从不收窄检索池，
    # 与「完全尊重显式选择」不冲突；放在早退之后的话，同一句话在自动/手动两种模式下语义会分叉，
    # 而且手动模式下专名会被残差门无声吞掉——一个字都不报，正是这轮在修的那类静默失败。
    if not auto_parse_sources:
        return SearchRequestResolution(
            original, base_query, explicit, "explicit" if explicit else "default",
            preferred_sources=preferred_sources,
        )

    detected = [source for source, _alias, _pattern in matches]
    if not detected:
        return SearchRequestResolution(
            original, base_query, explicit, "explicit" if explicit else "default",
            automatic_requested=True, preferred_sources=preferred_sources,
        )

    # 来源排除不能被反转成“只查该来源”。守卫仅绑定到已命中的来源专名，不能让“非小细胞肺癌”
    # 这类合法生物实体阻断 CELLxGENE 等来源的自动识别。
    if any(_is_source_negated(original, pattern) for _source, _alias, pattern in matches):
        return SearchRequestResolution(
            original,
            base_query,
            explicit,
            "explicit" if explicit else "default",
            detected_sources=detected,
            automatic_requested=True,
            automatic_skipped_reason="source_negation_guard",
            preferred_sources=preferred_sources,
        )

    effective = detected
    if explicit is not None:
        overlap = [source for source in detected if source in explicit]
        if not overlap:
            return SearchRequestResolution(
                original,
                base_query,
                explicit,
                "explicit_conflict",
                detected_sources=detected,
                automatic_requested=True,
                automatic_skipped_reason="detected_source_outside_explicit_pool",
                preferred_sources=preferred_sources,
            )
        effective = overlap

    # 去词只抹掉**命中的来源专名短语本身**（整条 alias），不碰句中其它位置单独点名的维度词
    # （"Human Cell Atlas 的小鼠脑" → 去掉专名后 mouse/brain 完整保留，见 test_search_request）。
    # 刻意**不**把专名内嵌的通用词（Human Cell Atlas→human、Single Cell Expression Atlas→single cell）
    # 提升成硬过滤约束：那是专名的组成部分、非用户约束。实证：HCA 532 条里 22 条 species 不含
    # 'human'（含少量非人参考数据）——若把内嵌 human 当 species=human，会静默漏掉这 22 条合法 HCA 记录。
    # 故对当前单物种/单模态来源，整词去掉即正确。（未来若新增名字含**该来源并不完全隐含**的通用维度词
    # 的来源，应在**添加该来源时**校验其 alias 不吞维度语义，而非在此运行期反向提升，避免上述回归。）
    cleaned = base_query
    removed: list[str] = []
    for source, alias, pattern in matches:
        if source not in effective:
            continue
        cleaned, n = pattern.subn(" ", cleaned)
        if n:
            removed.append(alias)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return SearchRequestResolution(
        original,
        cleaned,
        effective,
        "auto_detected",
        detected_sources=effective,
        removed_terms=removed,
        automatic_requested=True,
        preferred_sources=preferred_sources,
    )
