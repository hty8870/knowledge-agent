"""集中式受控同义词表（检索规则匹配层的产品级同义扩展）。

背景：评测侧（scripts/evaluate_search_live.py 的 must_match 嵌套列表）
早已有同义类判定，但只影响评分；产品检索层没有同义扩展——实测缺口在排序层：
硬约束维度词表（vocabulary.py 泛称组）的 targets 已含 cancer/carcinoma 双形，搜
任一形都出双 target、召回无损；但 `_rank_score` 的自由词打分只看 query 原词，
标题只写 carcinoma 的记录在搜 cancer 时少 +1.0 标题分（对照实测：Q=carcinoma 时
"3k Human Squamous Cell Lung Carcinoma DTCs" 排第 1，Q=cancer 时掉出 top10 前列）。

本表把同义扩展从评测侧上升为产品层，原则：
- **只放宽召回**：扩展只让「原本不命中的同义写法」命中，不改变原 query 的其他
  行为（不新增约束、不改 display、不改变已消费的 alias、不影响弃权判定）。
- **无关词一律不扩展**：不在本表里的词逐字原样，零行为变化。
- **误扩展零容忍**：每条词目必须人工审核后登记，收录标准见下。

维护规范（新增词目前必读）：
1. 一条词目 = 一个同义类，组内词互为同义写法（同一概念的英文/变体拼写），
   **不是**上位词/下位词关系（cancer↔carcinoma 同级成立；cancer↔tumor 是
   上下位近义、语义有差，不收——见 CONTROLLED_SYNONYMS 词目 1 的注释）。
2. 逐条审核三问：组内任意一词替换另一词，指向的记录集合是否基本重合？
   是否存在一词是另一词的子串或常见缩写碰撞（会连带误命中无关记录）？
   中文别名是否可能与既有 vocabulary alias 消费冲突（本表只在打分层用、
   不进 alias 消费，但仍避免引入歧义词形）？
3. 词目登记后必须补测试：扩展命中（双向）+ 无关词不扩展 + 不误扩展回归
   （tests/test_synonym_expansion.py）。
4. 改动本表必须跑冻结评测（scripts/evaluate_recommendation.py，784 条基线
   Top1/Top5 均 97.7%）：结果逐位不变或提升，变差即收窄。
5. 本表刻意**不**做成配置文件/外部数据：词目需要逐条人工验证与测试回归，
   放代码里让每次改动都走代码验证 + 质量门，而不是静默改配置。
"""
from __future__ import annotations

# 受控同义词表：{原词(小写): 同义扩展组(含原词自身，小写)}。
# 加载时经 _load 校验（组内不含空串/重复、键在自身组内、组内成员不跨组冲突），
# 校验失败直接抛异常——词表损坏宁可启动失败，不带病上线。
CONTROLLED_SYNONYMS: dict[str, tuple[str, ...]] = {
    # 词目 1：cancer ↔ carcinoma。
    # 依据：两者在库内 disease 字段高度共现（同一条常写 "lung cancer" 标题写
    # "…Lung Carcinoma"），是同一概念的两种写法而非上下位；评测侧 must_match
    # 同义类（[["cancer","carcinoma"]]）早已按同级处理。
    # 刻意不收 tumor/tumour/neoplasm：泛肿瘤概念外延更宽（含良性），会放宽到
    # 「用户搜癌症时冒出良性肿瘤记录」，违反误扩展零容忍。中文「癌/肿瘤」同理不收。
    "cancer": ("cancer", "carcinoma"),
    "carcinoma": ("cancer", "carcinoma"),
}


def _load(table: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """模块加载时自检：词表结构合法性（防手改出坏形状后静默半生效）。

    不变量：任一成员词在整表里只映射到**一个**扩展组。双向词目（如 cancer/carcinoma
    互为键）按「组内成员完全一致」表示同一个同义类，是合法且期望的形态；禁止的是
    同一成员出现在两个**不同**的组里（那意味着它有两种互相矛盾的扩展，行为不可预期）。
    """
    seen_groups: dict[str, frozenset[str]] = {}
    for key, group in table.items():
        key_l = key.lower()
        assert isinstance(group, tuple), f"同义词组必须是 tuple：{key!r}"
        members = [m.lower() for m in group]
        if key_l not in members:
            raise ValueError(f"同义词键 {key!r} 不在自己的组内：{group!r}")
        if len(set(members)) != len(members):
            raise ValueError(f"同义词组 {key!r} 内有重复：{group!r}")
        if any(not m for m in members):
            raise ValueError(f"同义词组 {key!r} 内有空串：{group!r}")
        this = frozenset(members)
        for m in members:
            prev = seen_groups.get(m)
            if prev is not None and prev != this:
                raise ValueError(
                    f"同义组成员 {m!r} 映射到两个不同的组：{sorted(prev)!r} 与 {sorted(this)!r}")
            seen_groups[m] = this
    # 键统一小写后重建，避免大小写不同的键悄悄分叉
    return {k.lower(): tuple(m.lower() for m in v) for k, v in table.items()}


# 加载即校验：import 本模块即触发，坏词表在进程启动时暴露
SYNONYM_TABLE: dict[str, tuple[str, ...]] = _load(CONTROLLED_SYNONYMS)


def expand_term(term: str) -> tuple[str, ...]:
    """受控同义扩展：命中词目返回同义组（含原词），未命中返回 `(term,)` 原样。

    幂等、无副作用；对未登记词零行为变化（调用方拿到单元素组等价于原词）。
    空串原样返回（调用方 `_any_text_hit` 对空串判否，与旧裸子串行为一致）。
    """
    if not term:
        return (term,)
    return SYNONYM_TABLE.get(term.lower(), (term,))


def is_registered(term: str) -> bool:
    """该词是否登记在受控同义词表中（测试/审计用）。"""
    return bool(term) and term.lower() in SYNONYM_TABLE
