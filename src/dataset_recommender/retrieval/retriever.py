"""
检索：hard_filter（保证 0% 违规）→ 存活集排序 → family 去重（不越界）→ 终检 → 解释字段。
无匹配/弃权 → 结构化无结果诊断（缺失约束 + 放宽建议）。

架构约束（与 Codex 辩论收敛）：
- 硬约束只在结构化字段上判定，先过滤后排序；语义/embedding 若启用也只排存活集，永不引入违规。
- 硬约束是存活集的常量，不参与排序权重，只进解释字段。
- family 上限严格执行（宁可少返，绝不超额）。
- 终检：选出后对每条重新核验全部硬约束（不变量守卫）。
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass, field, replace

from .normalizer import DatasetRecord, is_missing_value
from .query_parser import QueryIntent, relax_key_to_suppressed
from .synonyms import expand_term
from ..domain.descriptors import MATCH_EXACT
from ..domain.registry import domain_dimensions, get_domain

# 注：历史兼容别名 FACET_ORDER / FACET_LABELS / _FACET_CASEFOLD 已退役——骨架内部一律调
# _facet_order()/_facet_labels()/_facet_casefold() 真函数（活动领域包运行时供给），不再留 shim。


@dataclass(slots=True)
class RetrievedCandidate:
    record: DatasetRecord
    score: float
    matched_fields: list[str] = field(default_factory=list)
    missing_fields: list[str] = field(default_factory=list)
    reason: str = ""


@dataclass(slots=True)
class NoResultDiagnosis:
    kind: str                       # "abstain" | "no_match" | "clarification"
    reason: str                     # 机器可读
    detail: str                     # 人类可读
    per_constraint_support: dict = field(default_factory=dict)  # {dim: 该单约束在库中支持数}
    relaxations: list[str] = field(default_factory=list)


def normalize_family_key(record: DatasetRecord) -> str:
    """家族标识（题根派生，与查询无关）。保留此函数名以兼容评测/旧调用。"""
    return record.family_id or record.dataset_name.strip().lower()


# ---------- 硬约束判定（结构化字段，唯一的 0% 违规来源）----------
def _field_contains(field_text: str, terms: list[str]) -> bool:
    t = (field_text or "").lower()
    return any(term in t for term in terms if term)


def _dim_field_text(record: DatasetRecord, dim: str) -> str:
    """某硬约束维度对应的原始字段文本（供「是否已标注」判定 + 诚实降级用）。

    维度 → 字段文本的映射由活动领域包的 DimensionDescriptor 声明（旧写死 if 链的
    数据化）；未知维度返回 ""（与旧 if 链末尾行为一致）。
    """
    desc = get_domain().descriptor_for(dim)
    if desc is None:
        return ""
    return desc.text_of(record)


def _partial_capable_dims() -> frozenset:
    """声明了「取值集合可能不完整」的维度名（领域包描述符驱动）。"""
    return frozenset(d.name for d in get_domain().dimensions if d.partial_capable)


def _completeness_text(record: DatasetRecord, name: str) -> str:
    """完整性 tie-breaker 的计分文本：维度名走领域包描述符访问器，否则按记录字段名取。"""
    desc = get_domain().descriptor_for(name)
    if desc is not None:
        return desc.text_of(record)
    return getattr(record, name, "") or ""


# 值集可能不完整的维度：适配器若声明该维取值取自**抽样**，则「字段有值但不含 X」**不构成**「不是 X」的
# 证据 → 该维仍属「无法核验」。哪些维度可能有此属性由领域包描述符的 `partial_capable` 声明
# （biodata 仅 tissue/disease）；其余维度恒视作完整。
def _dim_value_set_complete(record: DatasetRecord, dim: str) -> bool:
    """该维的取值集合是否**穷尽**。

    背景：`ingest_ebi_scea.py` 的 design 文件逐细胞、最大 453MB，只能按 Range 多点抽样（中位覆盖 2.5%、
    最差 0.1%），故在记录上写 `metadata_provenance.complete=False` 声明「取值可能不全」。

    没有 provenance 的记录（冻结 base/10x 与 cellxgene/hca/arrayexpress 三库）→ 沿用旧语义**视作完整**
    → 判定逐位不变。只有显式 `complete is False` 才算不完整（缺键/None/异常形状一律保守视作完整，
    避免因 provenance 形状漂移把全库降级成「无法核验」）。
    """
    if dim not in _partial_capable_dims():
        return True
    raw = record.raw if isinstance(record.raw, dict) else {}
    prov = raw.get("metadata_provenance")
    if not isinstance(prov, dict):
        return True
    return prov.get("complete") is not False


def _dim_field_present(record: DatasetRecord, dim: str) -> bool:
    """该维是否**已核验**：字段有真实取值 **且** 取值集合穷尽。否则＝无法核验该条件
    （诚实降级层据此把「不匹配」与「不知道」分开）。

    为什么不只看非空（2026-07-16 对抗评审证伪）：SCEA 补上抽样得来的 tissue 后，记录会从「不知道」
    静默升格成「已知」——实测 `tissue=alveolus` 查询下，人类肺图谱 `E-ANND-1` 命中 0、caveat 也不再
    报它、连 lenient 都捞不回；而修复前它至少还被算进「另有 160 条无法核验」。抽样值**命中**是可信
    证据（搜「肺」照常命中，走 `_field_contains`，不经本函数）；抽样值**不命中**不构成否证 → 必须
    仍报 caveat、仍可被 lenient 纳入。诚实层原本只有二态，这里补上第三态「已标注但值集不完整」。

    为什么不只看空字符串（2026-07-17 全盘审计）：判空曾是裸 `.strip()`，于是 `disease="unknown"`
    被当成「已核验的真取值」。冻结 base 767 条里这样的有 **298 条（38.9%，296 个 "unknown" + 2 个
    "n/a"）** → 搜「肿瘤」时 caveat 恒空、lenient 一条都捞不回，而同一界面的卡片对同一条记录显示
    「疾病：未说明」（introduction 认哨兵、这里不认）。**两个模块对同一条记录给出互相矛盾的结论**，
    且诚实降级层在它的旗舰语料 × disease 这一格上 100% 失效——正是它被造出来要消灭的静默判负。
    现改用 `normalizer.is_missing_value`（哨兵判定的单一真源，此前一个概念三套定义）。

    调用面（已 grep 核实，仅两处，均非默认路径）：`constraint_satisfied` 的 `lenient` 分支（opt-in，
    默认 False 时根本不调）与 `coverage_caveats`（官方评测/CLI 不调用）→ 默认检索与冻结 767 不受影响。
    """
    if is_missing_value(_dim_field_text(record, dim)):
        return False
    return _dim_value_set_complete(record, dim)


def constraint_satisfied(record: DatasetRecord, dim: str, terms: list[str], lenient: bool = False) -> bool:
    if not terms:
        return True
    # 诚实降级：用户显式请求「纳入未标注该维的数据」时，字段为空视作通过（无法核验≠不匹配）；
    # 已知不同值仍走下方判定被排除。lenient 默认 False → 默认/评测路径此分支从不进入、行为逐位不变。
    if lenient and not _dim_field_present(record, dim):
        return True
    desc = get_domain().descriptor_for(dim)
    if desc is None:
        # 旧 if 链末尾行为：未知维度恒真（调用方只遍历领域声明的维度，不会走到这里）。
        return True
    text = desc.text_of(record)
    if desc.match == MATCH_EXACT:
        # 精确等值维（biodata 的 platform/modality）：归一值小写后须等值于 targets 之一。
        return text.lower() in terms
    return _field_contains(text, terms)


def passes_hard_filter(record: DatasetRecord, intent: QueryIntent) -> bool:
    # 非可执行（弃权 / 需澄清）→ 扫描语料前即返回否；clarification 绝不落成"没有匹配"。
    if getattr(intent, "parse_status", "executable") != "executable":
        return False
    lenient_dims = getattr(intent, "lenient_dims", None) or set()
    for dim in domain_dimensions():
        # 正向：须含任一 target；负向：命中任一 forbidden target 即淘汰。始终遍历领域包声明的维度集，
        # 不遍历 intent.excluded_constraints.items()（未知维度会让 constraint_satisfied 恒真而误淘汰全部）。
        inc = intent.constraints.get(dim)
        # 诚实降级：仅**正向**约束支持宽容（负向「须不含」对空字段本就放行——空不算命中 forbidden——无需宽容）。
        if inc and not constraint_satisfied(record, dim, inc, lenient=(dim in lenient_dims)):
            return False
        exc = intent.excluded_constraints.get(dim)
        if exc and constraint_satisfied(record, dim, exc):
            return False
    # raw 三态精确：required is True→只留 True；is False→只留 False（None 两态都排，fail-closed：
    # None 既不能证明"有"也不能证明"没有"）。
    if intent.has_raw_data_required is not None and record.has_raw_data is not intent.has_raw_data_required:
        return False
    # 发表时间范围硬过滤（用户在前端限定；published_date 为 ISO YYYY-MM-DD，可字面比较）。
    # date_from/date_to 默认空串 → 官方评测/CLI 路径不传时整段为 no-op，确定性门不受影响。
    if intent.date_from or intent.date_to:
        pub = str(record.raw.get("published_date", "")) if isinstance(record.raw, dict) else ""
        if not pub:
            return False
        if intent.date_from and pub < intent.date_from:
            return False
        if intent.date_to and pub > intent.date_to:
            return False
    return True


# ---------- 分面细化（有结果时按未固定维度的取值分组，一键收窄）----------
# 设计要点（与硬约束、确定性不变量隔离）：
# - 分面**只在存活集上做**，取值取自记录真实字段 → 点击某分面值＝对存活集做**精确等值**后置过滤，
#   保证「点了 N 条就正好剩 N 条」（计数与过滤同源，不用 NL 猜测）。
# - facet_filters 缺省 None → retrieve/facets 整段 no-op → 官方评测（不传此参）逐位一致、确定性零影响。
# - 分面过滤与 query 硬约束正交：同维度多值 OR、跨维度 AND；只收窄存活集、绝不引入违规
#   （retrieve 第 4 步终检仍对每条重核硬约束）。
# 分面维度展示顺序与中文标签由活动领域包声明（biodata 顺序与旧常量逐位一致）；
# source/has_raw_data/year 是骨架记录特典（任何知识库记录都有来源/原始文件/发表年的概念）。
def _facet_order() -> list[str]:
    return list(get_domain().facet_order)


def _facet_labels() -> dict:
    return {
        **get_domain().dim_labels_zh,  # 维度中文名锚点在领域包，勿再抄一份
        "source": "数据来源", "has_raw_data": "原始数据", "year": "发表年份",
    }


def _facet_casefold() -> set:
    """按小写归并分面键的维度集合（领域包描述符 `casefold` 驱动；biodata = species/tissue/disease）。"""
    return {d.name for d in get_domain().dimensions if d.casefold}
# 缺失哨兵判定走 normalizer 的单一真源（此前这里另有一套、与 introduction 的那套互不相同，
# 而 _dim_field_present 干脆一套都不用 → 诚实层在 base 的 disease 维上恒失效）。
# 收敛是实测 no-op：两套的差集（`-` / not provided / not specified / null）在 5667 条 × 6 维里零出现。
# 自由文本维度：不同大小写（Blood/blood）本是同一取值，按小写归并成一个分面键，
# 避免分面面板出现重复项；显示名另取该键下**出现最多**的原始写法（保留 COVID-19 / Homo sapiens 的正确大小写）。


def _facet_source(record: DatasetRecord) -> str:
    raw = record.raw if isinstance(record.raw, dict) else {}
    # 缺来源的记录如实落「未标注来源」桶——此前静默默认 "10x Genomics"，会把无源记录
    # 计进 10x 的分面计数、caveat 的按来源分组，还在「优先 10x」时白吃来源加权
    # （把「不知道来源」当成「是 10x」，2026-08-15 触发点。
    return (str(raw.get("source") or "").strip()) or "未标注来源"


def _facet_year(record: DatasetRecord) -> str:
    raw = record.raw if isinstance(record.raw, dict) else {}
    # 先 strip 再切前 4 位：与其余维度的归一化一致，也防外部源（HCA/EBI/CELLxGENE）
    # published_date 带前导空白时切出 " 202" 这种坏桶、且与后端清洗（会 strip）口径不一而错配。
    return str(raw.get("published_date", "")).strip()[:4]


def _facet_raw(dim: str, record: DatasetRecord) -> str:
    """记录在某维度上的**原始**取值（未归一大小写）。空/占位返回 ""。"""
    if dim == "source":
        v = _facet_source(record)
    elif dim == "has_raw_data":
        _raw_yes, _raw_no = get_domain().raw_data_labels
        v = {True: _raw_yes, False: _raw_no}.get(record.has_raw_data, "")
    elif dim == "year":
        v = _facet_year(record)
    else:
        desc = get_domain().descriptor_for(dim)
        if desc is None:
            return ""
        v = desc.facet_of(record)
    return "" if is_missing_value(v) else v


def facet_value(dim: str, record: DatasetRecord) -> str:
    """记录在某分面维度上的**归一化键**（用于分组计数 + 精确等值过滤）。
    自由文本维度按小写归并；空/占位返回 ""（不作为分面）。
    计数与过滤都走此键 → 「点了 N 条就正好剩 N 条」的一致性由此保证。"""
    v = _facet_raw(dim, record)
    if v and dim in _facet_casefold():
        return v.lower()
    return v


def record_passes_facets(record: DatasetRecord, facet_filters: list[dict] | None) -> bool:
    """分面过滤谓词：跨维度 AND，同维度多值 OR。空/非法过滤项忽略。"""
    if not facet_filters:
        return True
    by_dim: dict[str, set[str]] = {}
    for f in facet_filters:
        if not isinstance(f, dict):
            continue
        dim, val = f.get("dim"), f.get("value")
        if dim in _facet_labels() and isinstance(val, str) and val.strip():
            by_dim.setdefault(dim, set()).add(val)
    for dim, vals in by_dim.items():
        if facet_value(dim, record) not in vals:
            return False
    return True


# 「优先 X」命中一个偏好维度时加的分。取值理由（不是拍脑袋，是对着本函数其余各项定的）：
# 本函数其余各项的量纲是——自由词命中标题 +1.0 / 命中描述 +0.3；约束词命中标题 +0.5；
# 完整度 tie-breaker 最多 +0.6；新鲜度最多 +0.4；样本量最多 +0.5。所有 tie-breaker 加起来最多 +1.5。
# 取 2.0 的含义：**一个偏好维度命中，压得过全部 tie-breaker 之和**（否则用户说了「优先」却看不出
# 变化，等于没做），但**压不过两个自由词都命中标题**（否则偏好会盖过词面相关性，让明显更贴题的
# 数据集掉下去，那就从「排序偏好」变成了事实上的硬过滤）。这个「压得过噪声、压不过相关性」的
# 夹缝就是软偏好该待的位置。
PREFERENCE_BOOST = 2.0

# 短 ASCII 自由词的词边界命中判定（见 `_score` 里的词面相关性段）。
# 编译结果缓存在模块级 dict 里：`_score` 在冻结评测热路径上，每条记录都要判几次。
_WORD_RE_CACHE: dict[str, "re.Pattern[str]"] = {}


def _word_hit(token: str, text: str) -> bool:
    """`token` 是否作为**独立词**出现在 `text` 里（两侧不能紧邻 ASCII 字母数字）。

    判据与 `query_parser._alias_occurrences` 同源：中文不分词、不判边界；这里只用于 ASCII 短词。
    """
    if not token or not text:
        return False
    pat = _WORD_RE_CACHE.get(token)
    if pat is None:
        pat = re.compile(r"(?<![0-9a-z])" + re.escape(token) + r"(?![0-9a-z])")
        _WORD_RE_CACHE[token] = pat
    return bool(pat.search(text))


def _term_hits_text(term: str, text: str) -> bool:
    """单个查询自由词对一段文本的命中判定（短 ASCII 词边界 / 长词子串）。

    拆出供 `_rank_score` 与受控同义扩展复用，判据与 2026-07-22 之前逐字相同。
    """
    if not term or not text:
        return False
    if len(term) <= 3 and term.isascii():
        return _word_hit(term, text)
    return term in text


def _any_synonym_hits(term: str, text: str) -> bool:
    """受控同义扩展命中判定：原词或任一登记同义词命中即真（见 synonyms.py 维护规范）。

    保守性：`expand_term` 对未登记词返回 `(term,)` 单元素组 → 判定退化为原词命中，
    逐字等价于扩展前的行为；只有登记词目才会额外认它的同义写法。词边界/子串的
    判据随 `term` 自身走（同义组各成员按自己的长度/字符集判，不混用）。
    """
    return any(_term_hits_text(v, text) for v in expand_term(term))


class DatasetRetriever:
    def __init__(self, top_k: int = 10) -> None:
        self.top_k = top_k

    # ---------- 存活集排序（硬约束是常量，只用相关性/tie-breaker）----------
    def _rank_score(self, record: DatasetRecord, intent: QueryIntent) -> float:
        score = 0.0
        name = record.dataset_name.lower()
        desc = record.description.lower()
        # 软偏好加权（「优先 X」）：只在这里生效，绝不进硬过滤。
        # intent.preferred_* 缺省全空 → 整段是 no-op，官方评测/CLI 走的就是这条空分支，
        # 打分与历史逐位相同（冻结 767 的结构性隔离靠这一点，不靠事后比对）。
        for dim, targets in (intent.preferred_constraints or {}).items():
            if targets and constraint_satisfied(record, dim, targets):
                score += PREFERENCE_BOOST
        if intent.preferred_raw and record.has_raw_data is True:
            score += PREFERENCE_BOOST
        if intent.preferred_sources:
            # 缺来源的记录不吃来源加权：先看 raw 里的真值，「未标注来源」哨兵不匹配任何偏好。
            raw_src = str(record.raw.get("source") or "").strip() if isinstance(record.raw, dict) else ""
            if raw_src and _facet_source(record) in set(intent.preferred_sources):
                score += PREFERENCE_BOOST
        if intent.preferred_date_from or intent.preferred_date_to:
            pub_pref = str(record.raw.get("published_date", "")) if isinstance(record.raw, dict) else ""
            pub_pref = pub_pref.strip()[:10]
            if pub_pref and (not intent.preferred_date_from or pub_pref >= intent.preferred_date_from) \
                    and (not intent.preferred_date_to or pub_pref <= intent.preferred_date_to):
                score += PREFERENCE_BOOST
        # 词面相关性：查询自由词命中标题(权重高)/描述
        #
        # **短 ASCII 自由词按词边界判定**（2026-07-22 夜场景覆盖测试）：此前是裸子串，于是
        # 查「AD」时 'ad' 命中了标题里的 **ad**ult —— 前 5 条全是 adult 肠/肝图谱、分数 2.5
        # 高出基线，排得整整齐齐、看不出任何异常，而库里真正的 37 条阿尔茨海默数据一条没露面。
        # 这与 alias 侧 2026-07-22 白天修的 `integrated` 里的 `rat` 是同一个病根（裸子串），
        # 只是 alias 那半修了、自由词打分这半没修。
        # 只对 **≤3 字符** 的 ASCII 词加词边界：短词才会被长单词偶然包含；长词（如 fibrosis、
        # pbmc、xenium）保持子串匹配，以免 'atlas' 匹配不到 'atlases' 这类正常的形态变化。
        # 2026-08-18 受控同义扩展（synonyms.py）：命中判定改为「原词或任一登记同义词」，
        # 只放宽召回（原本命中的仍命中、加分规则不变）；未登记词零行为变化。
        # 缺口来自实测：硬约束层 targets 双形无损，但标题只写 carcinoma 的记录在搜
        # cancer 时拿不到这 +1.0（自由词只认 query 原词），排不上来。
        for t in intent.free_text_terms:
            if not t:
                continue
            hit_name, hit_desc = _any_synonym_hits(t, name), _any_synonym_hits(t, desc)
            if hit_name:
                score += 1.0
            elif hit_desc:
                score += 0.3
        # 约束 display 词命中标题（更贴题的排前）
        for dim in domain_dimensions():
            for term in intent.constraints.get(dim, []):
                if term and term in name:
                    score += 0.5
        # tie-breaker：完整性。**刻意**保留这套局部二元判空（"" / "unknown"），不收敛到
        # is_missing_value（模块单一真源）：本函数在冻结评测热路径上，is_missing_value 多认的
        # 哨兵（n/a / not provided / - / 未说明…）会改变 filled 计数 → 动 score 与并列序 → 可能动
        # Top1/Top5 基线。差集在 base 上实测零出现，无功能差异；若日后做受控重基线再收敛。
        # 计分字段由活动领域包声明（`completeness_fields`；biodata = species/tissue/disease/chemistry，
        # 与旧写死四元组逐位一致；维度名走描述符访问器、记录字段名走 getattr）。
        # 抽样维不计（xdc1 追加发现，2026-07-19）：SCEA 的 tissue/disease 可能取自 0.1%~2.5%
        # 抽样（`metadata_provenance.complete=False`）——「抽样碰巧看到值」不等于「字段填得全」，
        # 与整读确认有值的记录拿同样的 +0.15 会让抽样记录系统性地排在真未标注之前。改为：
        # `_dim_value_set_complete(record, dim) is False` 的维不计入 filled。base/cellxgene/hca/ae
        # 无 provenance → 恒完整 → 计数逐位不变（冻结 767 结构性不受影响；实测改动前完整分布
        # SCEA complete=False 198/384，其中 190 条 tissue/disease 有值）。
        filled = sum(
            1 for name in get_domain().completeness_fields
            if (lambda v: bool(v) and v.strip().lower() not in ("", "unknown"))(
                _completeness_text(record, name))
            and _dim_value_set_complete(record, name)
        )
        score += 0.15 * filled
        # tie-breaker：新鲜度
        pub = str(record.raw.get("published_date", "")) if isinstance(record.raw, dict) else ""
        score += 0.4 if pub >= "2025" else (0.2 if pub >= "2024" else 0.0)
        # tie-breaker：样本量（对数封顶，避免大样本压倒相关性）
        try:
            n = int(str(record.count).replace(",", ""))
            if n > 0:
                score += min(0.5, n / 100000.0)
        except (ValueError, TypeError):
            pass
        return score

    def _explain(self, record: DatasetRecord, intent: QueryIntent) -> tuple[list[str], list[str], str]:
        matched: list[str] = []
        missing: list[str] = []
        for dim in domain_dimensions():
            terms = intent.constraints.get(dim)
            if not terms:
                continue
            (matched if constraint_satisfied(record, dim, terms) else missing).append(dim)
        if intent.has_raw_data_required is not None:
            ok = record.has_raw_data is intent.has_raw_data_required
            (matched if ok else missing).append("has_raw_data")
        # reason：用 display 名拼可读理由
        parts: list[str] = []
        disp = intent.display_map
        for dim in get_domain().explain_order:
            if dim in matched and disp.get(dim):
                parts.append("/".join(disp[dim]))
        reason = "匹配 " + "、".join(parts) if parts else "满足基本检索条件"
        # 负向：拼「排除 X」（存活集已保证不含，故只作解释）
        ex_parts: list[str] = []
        for dim in get_domain().explain_order:
            ed = intent.excluded_display.get(dim)
            if ed:
                ex_parts.append("/".join(ed))
        if ex_parts:
            reason += "；排除 " + "、".join(ex_parts)
        if "has_raw_data" in matched:
            reason += "；且含 FASTQ 原始数据" if intent.has_raw_data_required is True else "；且不含 FASTQ 原始数据"
        reason += "。"
        return matched, missing, reason

    def _apply_family_cap(self, scored: list[RetrievedCandidate], limit: int, cap: int) -> list[RetrievedCandidate]:
        selected: list[RetrievedCandidate] = []
        counts: dict[str, int] = {}
        for cand in scored:
            key = normalize_family_key(cand.record)
            if counts.get(key, 0) >= cap:
                continue
            selected.append(cand)
            counts[key] = counts.get(key, 0) + 1
            if len(selected) >= limit:
                break
        return selected

    def retrieve(
        self,
        records: list[DatasetRecord],
        intent: QueryIntent,
        top_k: int | None = None,
        rerank_backend: str = "off",
        rerank_top_n: int = 12,
        llm_config: object | None = None,
        recall_backend: str = "off",
        recall_alpha: float = 0.5,
        recall_fusion: "str | None" = None,
        embedder: object | None = None,
        cross_scorer: object | None = None,
        facet_filters: list[dict] | None = None,
        execution_trace: dict | None = None,
    ) -> list[RetrievedCandidate]:
        """rerank_backend / recall_backend 均为**显式参数**（默认 "off"），刻意不在此读环境变量：
        官方评测入口不传这些参数 → 结构性走确定性路径，残留 env 无法泄漏进二元门。

        facet_filters（默认 None）：分面细化的精确等值后置过滤，只在硬过滤后收窄存活集，
        官方评测不传 → 整段 no-op、确定性零影响。

        recall_fusion（缺省 None → 按后端取默认）：dense/vector 召回的融合法（"linear"=min-max+α /
        "rrf"=名次融合 k=60 / "ltr"=学习排序融合）；dense/vector 缺省 "ltr"（工件缺失时
        fail-soft 回退 dense→linear、vector→rrf，与旧默认逐位一致）。
        官方评测不传（recall off）→ 召回块整体跳过、确定性零影响。"""
        limit = max(1, top_k if top_k is not None else self.top_k)
        # 弃权 → 无结果（由 workflow 渲染诊断）
        if intent.abstain:
            return []
        # 1) 硬过滤（结构化字段）+ 分面细化的等值后置过滤（缺省无此过滤）
        survivors = [r for r in records if passes_hard_filter(r, intent)]
        if facet_filters:
            survivors = [r for r in survivors if record_passes_facets(r, facet_filters)]
        if not survivors:
            return []
        # 2) 排序（相关性 + tie-breaker），稳定：并列时按 family_id/name 确定序。
        #    _rank_score 是纯函数：先一次性算好每条分数，排序键与候选 score 都复用同一份，
        #    避免对每条存活记录重复计算两遍（宽查询存活集可达数千条，且此路径被官方评测直调）。
        scores = {id(r): self._rank_score(r, intent) for r in survivors}
        ranked = sorted(
            survivors,
            key=lambda r: (-scores[id(r)], r.family_id, r.dataset_name),
        )
        candidates: list[RetrievedCandidate] = []
        for r in ranked:
            matched, missing, reason = self._explain(r, intent)
            candidates.append(
                RetrievedCandidate(
                    record=r, score=round(scores[id(r)], 4),
                    matched_fields=matched, missing_fields=missing, reason=reason,
                )
            )
        # 2.4) 可选向量召回（默认关）：本地稠密嵌入对存活集按语义相关性重排（可与词面分融合）。
        #      顺序遵循既定方向「规则过滤 → 向量召回 → llm 重排」。同样只作用于存活集、
        #      输出恒为其排列 → 不引入违规记录；第 4 步终检再兜底一次。
        # recall_fusion 解析（唯一默认在此）：dense/vector 缺省 "ltr"（学习排序融合；
        # 工件缺失/约束解析不可用时由 recall_rerank 内 fail-soft 回退原默认 dense→linear、
        # vector→rrf，行为与旧默认逐位一致）；显式值原样下传。off 后端记录 "linear"
        # （与历史遥测同值，无行为差）。
        eff_recall_fusion = str(recall_fusion or "").strip().lower() or ("ltr" if recall_backend in ("dense", "vector") else "linear")
        if recall_backend and recall_backend != "off":
            from .vector_recall import recall_rerank  # 惰性导入：避免循环依赖 + 不装可选依赖也能导入本模块

            candidates = recall_rerank(
                query=intent.original_query,
                candidates=candidates,
                backend=recall_backend,
                alpha=recall_alpha,
                fusion=eff_recall_fusion,
                embedder=embedder,
                cross_scorer=cross_scorer,
                intent=intent,
                trace=(execution_trace.setdefault("recall", {}) if execution_trace is not None else None),
            )
        # 2.5) 可选 LLM 重排（默认关）：先把存活集**按家族去重成"多样候选池"**（每族只留当前排序下
        #      最靠前=最佳的一条），再让 LLM 只重排这个池——避免昂贵且窗口有限的 LLM 把 rerank_top_n
        #      名额浪费在同一数据集的近重复切片上（先去重后重排，而非先重排后去重）。
        #      池大小取 max(rerank_top_n, limit)，仍 > 最终输出，保留"把更靠后家族顶进 top_k"的空间；
        #      被去重挤下的同族样本作 tail 保留，供第 3 步 family cap 在不同家族不足 top_k 时放宽 2/family 兜底。
        #      只重排列/截断存活集，绝不引入违规；下面第 4 步终检再兜底一次。
        #      注：backend=="off"（默认 + 官方评测门）时整块跳过 → 与"先规则序/召回序、后 family cap"逐位一致。
        if rerank_backend and rerank_backend != "off":
            from .rerank import rerank_candidates  # 懒导入避免与 rerank.py 循环依赖

            pool_size = max(1, rerank_top_n, limit)
            pool = self._apply_family_cap(candidates, pool_size, cap=1)   # 每族一条 → 多样池
            pool_ids = {id(c) for c in pool}
            tail = [c for c in candidates if id(c) not in pool_ids]        # 被挤下的同族样本 + 池外家族
            reranked_pool = rerank_candidates(
                query=intent.original_query,
                candidates=pool,
                backend=rerank_backend,
                top_k=None,
                config=llm_config,
                trace=(execution_trace.setdefault("rerank", {}) if execution_trace is not None else None),
            )
            candidates = reranked_pool + tail
        # 遥测/离线评测所需的实际排序现场必须在 recall/rerank **之后**截取：否则实验臂虽然
        # 真执行了新排序，训练数据却仍记录旧规则序。保留完整候选 UID 顺序（轻量），并为前
        # 500 条保存规则分数与解释特征。完整 rich features 会挤爆 2MiB 上传包，故特征有界、
        # 全序由 UID 列表 + SHA-256 保真。只写显式 execution_trace，不改变排序或官方评测。
        if execution_trace is not None:
            ordered_uids = [str(c.record.raw.get("dataset_uid") or "") for c in candidates]
            canonical_order = json.dumps(ordered_uids, ensure_ascii=False, separators=(",", ":"))
            execution_trace["ranking_snapshot"] = {
                "schema": "biodata-ranking-snapshot/1",
                "feature_schema": "rule-rank-features/1",
                "parameters": {
                    "top_k": limit, "rerank_top_n": int(rerank_top_n),
                    "rerank_backend": str(rerank_backend or "off"),
                    "recall_backend": str(recall_backend or "off"),
                    "recall_alpha": float(recall_alpha), "recall_fusion": eff_recall_fusion,
                    "family_cap_primary": 1, "family_cap_fallback": 2,
                    "weights": {"preference": PREFERENCE_BOOST, "free_text_title": 1.0,
                                "free_text_description": 0.3, "constraint_title": 0.5,
                                "completeness_each": 0.15, "freshness_2025": 0.4,
                                "freshness_2024": 0.2, "sample_size_cap": 0.5},
                },
                "survivor_count": len(survivors), "candidate_count": len(candidates),
                "ordered_uids": ordered_uids,
                "ordered_uids_sha256": hashlib.sha256(canonical_order.encode("utf-8")).hexdigest(),
                "features": [
                    {"uid": str(c.record.raw.get("dataset_uid") or ""), "base_score": c.score,
                     "family_id": c.record.family_id, "matched_fields": c.matched_fields,
                     "missing_fields": c.missing_fields}
                    for c in candidates[:500]
                ],
                "features_truncated": len(candidates) > 500,
            }
        # 3) family 上限：先 1/family，不足再 2/family；严格不越界（宁可少返）
        selected = self._apply_family_cap(candidates, limit, cap=1)
        if len(selected) < limit:
            selected = self._apply_family_cap(candidates, limit, cap=2)
        # 4) 终检：不变量守卫——重核每条硬约束，丢弃任何违规者
        selected = [c for c in selected if passes_hard_filter(c.record, intent)]
        return selected

    # ---------- 无结果诊断 ----------
    def explain_empty(self, records: list[DatasetRecord], intent: QueryIntent) -> NoResultDiagnosis:
        if getattr(intent, "parse_status", "executable") == "clarification_required":
            return NoResultDiagnosis(
                kind="clarification", reason=intent.clarification_reason,
                detail=intent.clarification_detail or "查询含义需要确认。",
            )
        if intent.abstain:
            return NoResultDiagnosis(
                kind="abstain", reason=intent.abstain_reason,
                detail=intent.abstain_detail or "查询无法安全解析，已弃权。",
            )
        # 逐个单约束支持数：定位是哪个/哪些约束导致空集
        support: dict[str, int] = {}
        active: dict[str, list[str]] = {d: intent.constraints[d] for d in domain_dimensions() if intent.constraints.get(d)}
        for dim, terms in active.items():
            support[dim] = sum(1 for r in records if constraint_satisfied(r, dim, terms))
        if intent.has_raw_data_required is not None:
            want = intent.has_raw_data_required
            support["has_raw_data"] = sum(1 for r in records if r.has_raw_data is want)
        for dim in domain_dimensions():
            exc = intent.excluded_constraints.get(dim)
            if exc:
                support[f"exclude:{dim}"] = sum(1 for r in records if not constraint_satisfied(r, dim, exc))
        # 支持数为 0 的单约束 = 罪魁；否则是组合交集为空
        zero = [d for d, n in support.items() if n == 0]
        disp = intent.display_map
        def label(key: str) -> str:
            if key == "has_raw_data":
                return "需要 FASTQ" if intent.has_raw_data_required else "不要 FASTQ"
            if key.startswith("exclude:"):
                d = key.split(":", 1)[1]
                return "排除 " + "/".join(intent.excluded_display.get(d, [d]))
            return "/".join(disp.get(key, [key]))
        relaxations: list[str] = []
        if zero:
            detail = "库中不存在满足这些条件的数据：" + "、".join(label(d) for d in zero) + "。"
            for d in zero:
                relaxations.append(f"去掉「{label(d)}」这个条件")
        else:
            detail = "各单项条件都有数据，但同时满足全部条件的组合在库中不存在。"
            # 放宽建议：去掉支持数最少的约束
            for d, _n in sorted(support.items(), key=lambda x: x[1])[:2]:
                relaxations.append(f"放宽「{label(d)}」")
        return NoResultDiagnosis(
            kind="no_match", reason="empty_intersection",
            detail=detail, per_constraint_support=support, relaxations=relaxations,
        )

    # ---------- 引导式放宽（0 结果时的一键放宽项）----------
    def relaxation_options(
        self,
        records: list[DatasetRecord],
        intent: QueryIntent,
        top_k: int | None = None,
        max_options: int = 8,
        include_dead: bool = False,
    ) -> list[dict]:
        """空交集（no_match）时试算「若放宽某处能救回多少 + top-k 预览」，回传多种放宽策略供用户自选。

        **两档策略**（每项带 `kind`，前端据它分组）：

        - `kind="drop"` **去掉一个条件**：逐个丢掉**一个**硬约束（各约束维度 + FASTQ 要求 +
          排除项 + 发表时间范围）。保守档——你说的其余条件全都还在。

        include_dead=True 时放松后仍零存活的维度也保留（count=0、candidates=[]）——
        「放松也无救」对环内 rerank 的探测报告是信息量；缺省 False 维持既有 no_match
        行为逐位不变。
        - `kind="only"` **只按一个条件搜**：只保留**一个**维度的正向约束，其余（含 FASTQ /
          时间范围 / 排除项）全放开。激进档，但它是第一档**全军覆没**时唯一还救得回来的一档——
          条件互相叠加得太死时，去掉任何单独一个仍然是 0 条，用户就会看到「无结果且无任何建议」。

        第二档仅在第一档的放宽目标 **≥3 个**时生成：恰好 2 个目标时「只按 A 搜」逐字等于「去掉 B」，
        两条一模一样的建议并排列出只会让人以为它们不同。

        - **弃权（abstain）不在此列**：弃权是"查询无法安全解析"，不是"约束太严"，返回空（前端只提示原因）。
        - 每个放宽项都用确定性 retrieve（rerank/recall 默认关）出预览 → 不引入违规、可复现。
        - 每项带 `suppress`：本项放松量的 filter_id 清单（`relax_key_to_suppressed` 单锚点），
          供放松卡零 LLM 重搜直接下发为 suppressed_constraints（装配侧负责与既有忽略合并）。
        - 只读，不改 intent（用 dataclasses.replace 克隆）；官方评测/CLI 不调用此方法，确定性零影响。
        """
        if intent.abstain:
            return []
        limit = max(1, top_k if top_k is not None else self.top_k)

        targets: list[tuple[str, str, QueryIntent]] = []
        for dim in domain_dimensions():
            if intent.constraints.get(dim):
                label = "/".join(intent.display_map.get(dim, [dim])) or dim
                relaxed = replace(intent, constraints={d: v for d, v in intent.constraints.items() if d != dim})
                targets.append((f"dim:{dim}", label, relaxed))
        if intent.has_raw_data_required is not None:
            lbl = "FASTQ 原始数据" if intent.has_raw_data_required else "『不要 FASTQ』条件"
            targets.append(("raw", lbl, replace(intent, has_raw_data_required=None)))
        for dim in domain_dimensions():
            if intent.excluded_constraints.get(dim):
                lbl = "排除 " + ("/".join(intent.excluded_display.get(dim, [dim])) or dim)
                relaxed = replace(intent, excluded_constraints={d: v for d, v in intent.excluded_constraints.items() if d != dim})
                targets.append((f"exclude:{dim}", lbl, relaxed))
        if intent.date_from or intent.date_to:
            a, b = intent.date_from[:4], intent.date_to[:4]
            rng = (a + " 起") if a and not b else ("截至 " + b) if b and not a else (a + "–" + b)
            targets.append(("date", "发表时间(" + rng + ")", replace(intent, date_from="", date_to="")))

        def measure(key: str, label: str, kind: str, relaxed: QueryIntent) -> dict | None:
            survivors = sum(1 for r in records if passes_hard_filter(r, relaxed))
            if not survivors and not include_dead:
                return None
            cands = self.retrieve(records, relaxed, top_k=limit) if survivors else []
            if survivors and not cands:
                return None
            # suppress＝本项放松量的 filter_id 清单（单锚点翻译，key 与本函数 targets 同源恒合法）；
            # 网页零命中放松卡与环内 ask.relax 共用此真源，不再各自翻译。
            return {"key": key, "label": label, "kind": kind, "count": survivors,
                    "candidates": cands, "suppress": relax_key_to_suppressed(intent, key)}

        drops = [o for o in (measure(k, lb, "drop", rx) for k, lb, rx in targets) if o]
        drops.sort(key=lambda o: -o["count"])

        # 第二档「只按一个条件搜」：只留一个维度的正向约束，其余（其它维度 + 排除项 + FASTQ + 时间范围）全放开。
        # ≥3 个放宽目标才生成——恰好 2 个时「只按 A 搜」丢的正好也只有 B，与第一档某项逐字同义。
        onlys: list[dict] = []
        if len(targets) >= 3:
            for dim in domain_dimensions():
                if not intent.constraints.get(dim):
                    continue
                label = "/".join(intent.display_map.get(dim, [dim])) or dim
                kept = replace(
                    intent,
                    constraints={dim: intent.constraints[dim]},
                    excluded_constraints={},
                    has_raw_data_required=None,
                    date_from="",
                    date_to="",
                )
                opt = measure(f"only:{dim}", label, "only", kept)
                if opt:
                    onlys.append(opt)
            onlys.sort(key=lambda o: -o["count"])

        # 总额受 max_options 约束，但**给第二档留位**：条件一多，第一档就能把额度占满，
        # 而第二档恰恰是第一档全军覆没时唯一还救得回来的那一档，不能让它被挤掉。
        head = drops[: max(0, max_options - min(len(onlys), 3))]
        return head + onlys[: max_options - len(head)]

    # ---------- 诚实降级层（缺元数据 → 不静默判负，显式回显「无法核验」）----------
    def coverage_caveats(
        self, records: list[DatasetRecord], intent: QueryIntent, facet_filters: list[dict] | None = None
    ) -> list[dict]:
        """对每个**正向**硬约束维度 D，统计「满足其它全部约束、但 D 字段为空（无法核验）」的记录，
        按来源分组。语义＝『这些数据集满足你其它条件，但它们没标注 <D>，无法确认是否匹配』——本可能
        相关却被 fail-closed 静默判负的那批。让用户看见覆盖缺口、而非误以为「库里就这么点」。

        - 精确区分「不匹配」与「不知道」：记录 D 字段**有值但不同** → 属真实不匹配，不计入（诚实判负）；
          仅 D 字段**未标注/值集不全** → 计入（无法核验）。
        - **只数「你还没看见的」**：D 上抽样值已经命中、记录本就在结果列表里的，不得再计进「另有 N 条」
          （2026-07-17 修，见下方 `not constraint_satisfied` 守卫）。
        - **与当前存活集同口径**：若有活跃分面 `facet_filters`，unknown 集也套用 `record_passes_facets`
          （与 `matched_survivors` 同源）——保证 caveat 计数 == 点「也纳入」后真正新增数（前端重跑保留分面），
          不越过分面把用户已过滤掉的来源/记录算进「另有 N 条」。
        - 已被用户宽容（dim ∈ lenient_dims）的维度不再报 caveat（已纳入未标注项、缺口已消解）。
        - 只读：用 dataclasses.replace 克隆 intent，绝不改原 intent；官方评测/CLI 不调用此方法 →
          与 relaxation_options / facets 同为「结构性隔离冻结门」，确定性零影响。
        """
        if intent.abstain or getattr(intent, "parse_status", "executable") != "executable":
            return []
        lenient_dims = getattr(intent, "lenient_dims", None) or set()
        caveats: list[dict] = []
        for dim in domain_dimensions():
            terms = intent.constraints.get(dim)
            if not terms or dim in lenient_dims:
                continue
            # 去掉 D 的正向约束、其它约束/raw/date/宽容选择全保留 → 判「是否满足其它条件」
            other = replace(intent, constraints={d: v for d, v in intent.constraints.items() if d != dim})
            unknown = [
                r for r in records
                if passes_hard_filter(r, other) and not _dim_field_present(r, dim)
                # 「另有 N 条」必须只数**用户还没看见的**。第三态（抽样值集不全）出现后，一条记录可能
                # 同时「D 上抽样值命中 → 已在结果列表里」且「_dim_field_present=False → 值集不穷尽」，
                # 于是被重复计进「另有 N 条无法核验」。实测：勾 EBI 搜「人类肺组织」报 92、点「也纳入」
                # 实增 78，差额 14 条全在上方列表里；横幅还对它们说「未标注组织」——而 E-ANND-1 的
                # tissue 明明写着 "lower lobe of left lung"（正是上一轮拿来当标志案例的那条记录）。
                # caveat 计数 == lenient 新增数 是本层的核心不变量，也是它全部价值所在（数字不准 = 骗人）。
                and not constraint_satisfied(r, dim, terms)
                and (not facet_filters or record_passes_facets(r, facet_filters))
            ]
            if not unknown:
                continue
            by_source = Counter(_facet_source(r) for r in unknown)
            caveats.append({
                "dim": dim,
                # 缺口在于**该维度未标注**（不是某个值不匹配），故 label 用维度名（组织/疾病…）而非查询值（Lung）。
                "label": _facet_labels().get(dim, dim),
                "count": len(unknown),
                "by_source": [{"source": s, "count": c} for s, c in by_source.most_common()],
            })
        caveats.sort(key=lambda c: -c["count"])
        return caveats

    # ---------- 分面细化 ----------
    def matched_survivors(
        self,
        records: list[DatasetRecord],
        intent: QueryIntent,
        facet_filters: list[dict] | None = None,
    ) -> list[DatasetRecord]:
        """当前查询（含分面过滤）的**全部**命中记录（未截断 top-k）。弃权 → 空。
        与 retrieve 第 1 步同源，供「库中匹配总数 + 分面分组」统计。"""
        if intent.abstain:
            return []
        out = [r for r in records if passes_hard_filter(r, intent)]
        if facet_filters:
            out = [r for r in out if record_passes_facets(r, facet_filters)]
        return out

    def facets(
        self,
        records: list[DatasetRecord],
        intent: QueryIntent,
        facet_filters: list[dict] | None = None,
        max_values: int = 6,
        max_groups: int = 6,
    ) -> dict:
        """有结果时，按**未被查询固定、也未被分面选中**的维度，统计存活集里各取值的条数，
        回传 {"total": 命中总数, "groups": [{dim,label,values:[{value,display,count}]}]}。

        - 只保留「至少 2 个不同取值」的维度（单值无收窄意义）。
        - facet_filters 已选中的维度整组不再出现（该维度已被收窄，靠前端面包屑撤销）。
        - 弃权/命中<2 → groups 空（无可细化）；官方评测不调用此方法，确定性零影响。
        """
        survivors = self.matched_survivors(records, intent, facet_filters)
        total = len(survivors)
        if total < 2:
            return {"total": total, "groups": []}
        pinned = {d for d in domain_dimensions() if intent.constraints.get(d)}
        # raw 任一硬约束（True/False）都固定 has_raw_data 分面；结构化 exclusion **不**固定该维
        # （存活集里仍可能有其它取值，允许用户继续按该维收窄）。
        if intent.has_raw_data_required is not None:
            pinned.add("has_raw_data")
        if intent.date_from or intent.date_to:
            pinned.add("year")
        active = {f.get("dim") for f in (facet_filters or []) if isinstance(f, dict)}
        groups: list[dict] = []
        for dim in _facet_order():
            if dim in pinned or dim in active:
                continue
            counter: dict[str, int] = {}
            disp: dict[str, dict[str, int]] = {}   # 键 → {原始写法: 次数}，用于取最常见写法作显示名
            for r in survivors:
                v = facet_value(dim, r)
                if not v:
                    continue
                counter[v] = counter.get(v, 0) + 1
                raw = _facet_raw(dim, r) or v
                d = disp.setdefault(v, {})
                d[raw] = d.get(raw, 0) + 1
            if len(counter) < 2:
                continue
            if dim == "year":
                # 发表年份：按年份由新到旧排序（而非条数），截断后保留最近的 max_values 个年份
                top = sorted(counter.items(), key=lambda kv: kv[0], reverse=True)[:max_values]
            else:
                top = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:max_values]
            values = []
            for v, n in top:
                display = max(disp[v].items(), key=lambda kv: (kv[1], kv[0]))[0] if disp.get(v) else v
                values.append({"value": v, "display": display, "count": n})
            groups.append({"dim": dim, "label": _facet_labels()[dim], "values": values})
            if len(groups) >= max_groups:
                break
        return {"total": total, "groups": groups}
