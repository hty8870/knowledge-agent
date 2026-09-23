# -*- coding: utf-8 -*-
"""静默丢词诚实层专项。

缺口：用户输入了**结构上无对应筛选维度**的实义描述词（性别/年龄/受试者/功能类，如「免疫」「儿童」
「男性」），系统既不落维、又不入 free_text_terms（ASCII-only 正则抓不到中文）、也不触发 unresolved_term
弃权 → 原本**静默丢弃、零信号**（搜「肺癌免疫细胞」与「肺癌」逐位同结果，用户毫无察觉「免疫」被丢）。
修复：只读、additive 地回显 `unused_query_terms`「以下词未作为筛选维度」。

隔离保证（同 coverage_caveats 范式）：只读投影、不改解析/检索/弃权；`FILLER_TOKENS = FILLER_GRAMMAR +
FILLER_DOMAIN` 的**并集逐位不变** → `_residual_salient`/残差门/冻结 767 全部不变（另由
scripts/evaluate_recommendation.py 结构性守护）。本文件补四类行为断言：并集不变式、DOMAIN 成员钉死、
只报 DOMAIN 不报 facet-头词（否则撒谎）、排序输入与去掉描述词的查询逐位相同。
"""
from dataset_recommender.retrieval import query_lexicon as V
from dataset_recommender.llm.config import get_settings
from dataset_recommender.retrieval.query_parser import parse_query, _unused_domain_terms

CAT = get_settings().keyword_mapping


# ---------- 并集不变式：拆分不改 FILLER_TOKENS 成员（冻结门的结构性保证）----------
def test_filler_union_invariant():
    g, d = set(V.FILLER_GRAMMAR), set(V.FILLER_DOMAIN)
    assert g.isdisjoint(d)                                              # 两组互斥
    assert (g | d) == set(V.FILLER_TOKENS)                             # 并集 == 原表
    assert list(V.FILLER_TOKENS) == list(V.FILLER_GRAMMAR) + list(V.FILLER_DOMAIN)
    assert len(V.FILLER_TOKENS) == len(g | d)                         # 未引入重复


# ---------- DOMAIN 成员钉死：它决定「回显什么」，防漂移 ----------
# 扩表：原 21 个（性别/年龄/受试者/功能类）+ 新增 69 个（细胞类型/状态分期/规模/
# 取材保存/数据形态与工具）。新增的这批此前一律走 unresolved_term 整句弃权——用户写
# 「转移性乳腺癌数据」连乳腺癌都查不到。它们同样没有可过滤的字段，故同样是「不弃权、但必须回显」。
_DOMAIN_ORIGINAL = {
    "患者", "病人", "受试者", "供体", "捐赠者", "免疫",
    "成人", "成年", "儿童", "婴儿", "胎儿", "新生儿", "青少年", "老年", "年轻",
    "男性", "女性", "雄性", "雌性", "男", "女",
}
_DOMAIN_2026_07_22 = {
    "神经元", "巨噬", "巨噬细胞", "上皮", "内皮", "成纤维", "间质", "基质",
    "髓系", "淋巴", "树突", "小胶质", "星形胶质", "浆细胞", "祖细胞",
    "对照", "对照组", "野生型", "野生", "敲除", "过表达", "治疗前", "治疗后", "治疗",
    "用药", "化疗", "放疗", "复发", "转移", "转移性", "原发", "分期",
    "早期", "中期", "晚期", "进展期", "急性", "慢性",
    "高深度", "深度", "万级", "千级", "十万", "大规模", "小规模", "高分辨率", "低分辨率",
    "新鲜", "冷冻", "冻存", "石蜡", "ffpe", "活检", "穿刺", "尸检", "手术", "术后",
    "表达矩阵", "计数矩阵", "矩阵", "注释", "counts", "loom", "barcode", "matrix",
    "ranger", "cellranger", "seurat", "scanpy",
}
# 验证里仍在整句弃权、但结构上确实没有对应筛选维度的研究主题词与细胞类型。
# 「肿瘤微环境的单细胞数据」此前连「肿瘤」都查不到，就因为「微环境」不认识。
_DOMAIN_2026_07_22_NIGHT = {
    "微环境", "肿瘤微环境", "免疫微环境", "浸润", "免疫浸润",
    "发育", "发育时序", "时序", "拟时序", "轨迹", "分化", "再生", "衰老", "稳态",
    "谱系", "亚型", "异质性", "多样性", "可塑性", "极化", "激活",
    "类器官", "器官", "原代", "细胞系", "共培养", "培养",
    "毛细胞", "单核细胞", "干细胞", "免疫细胞", "肿瘤细胞", "基质细胞", "间充质",
    "成体", "胚系",
}
# 查询电池回归：病名**限定语**。系统只按病名落维，限定语没有对应字段。
# 「特发性肺纤维化的单细胞转录组」实测整句弃权，而「肺纤维化单细胞数据」有 19 条。
# 必须进 DOMAIN 而非 GRAMMAR：disease 落的是 Pulmonary Fibrosis（各种成因混在一起），
# 把 idiopathic 这个限定悄悄丢掉还不吭声就是静默丢词。
_DOMAIN_2026_07_22_BATTERY = {
    "特发性", "原发性", "继发性", "获得性", "遗传性", "家族性",
}

# 2026-09-11 OOD 批次：样本/分析语境领域词（无筛选维度承载，回显「未作为筛选维度」）。
# 源自 297 条真实语域 OOD 评测集的弃权普查（ffpe 已在既有批次，不重复）。
_DOMAIN_2026_09_11 = {
    "tme", "ctc", "ctdna",
}

_DOMAIN_BATCHES = (_DOMAIN_ORIGINAL, _DOMAIN_2026_07_22,
                   _DOMAIN_2026_07_22_NIGHT, _DOMAIN_2026_07_22_BATTERY,
                   _DOMAIN_2026_09_11)


def test_filler_domain_membership_pinned():
    expected = set()
    for batch in _DOMAIN_BATCHES:
        expected |= batch
    assert set(V.FILLER_DOMAIN) == expected
    # 各批互不重叠 → 上面的并集没有掩盖漏项
    for i, a in enumerate(_DOMAIN_BATCHES):
        for b in _DOMAIN_BATCHES[i + 1:]:
            assert a.isdisjoint(b), f"两批 DOMAIN 有重复成员：{a & b}"


# ---------- facet-头词必须留在 GRAMMAR、绝不进 DOMAIN（进了会「组织未筛选」撒谎）----------
def test_facet_heads_never_reported():
    g, d = set(V.FILLER_GRAMMAR), set(V.FILLER_DOMAIN)
    for head in ["组织", "平台", "细胞", "转录组", "图谱", "基因", "序列", "表达", "测序"]:
        assert head in g and head not in d


# ---------- 纯助手：只报 DOMAIN、保序、长词优先 ----------
def test_helper_reports_only_domain():
    # 「免疫细胞」整词进了 DOMAIN，长词优先 → 回显的是用户真正写下的那个词，
    # 而不是把它切成「免疫」再报。旧断言 ["免疫"] 是切碎后的产物，报全词更贴近用户输入。
    assert _unused_domain_terms("免疫细胞") == ["免疫细胞"]
    assert _unused_domain_terms("免疫浸润的肺癌") == ["免疫浸润"]      # 同理：不切成「免疫」+「浸润」
    assert _unused_domain_terms("儿童") == ["儿童"]
    assert _unused_domain_terms("成人男性") == ["成人", "男性"]        # 保序
    assert _unused_domain_terms("男性") == ["男性"]                    # 长词优先，不双报「男」


def test_helper_no_false_positive():
    assert _unused_domain_terms("推荐数据") == []                     # 纯语法噪声
    assert _unused_domain_terms("组织") == []                         # facet-头词
    assert _unused_domain_terms("") == []


# ---------- 端到端：executable 路径带 unused，且**不改排序输入** ----------
def test_parse_surfaces_unused_without_changing_ranking_inputs():
    a = parse_query("肺癌免疫细胞", CAT)
    b = parse_query("肺癌", CAT)
    assert a.parse_status == "executable"
    assert a.unused_query_terms == ["免疫细胞"]
    assert b.unused_query_terms == []
    # 关键：约束/排除/free_text 逐位相同 → 检索排序不变，只是丢词现在有信号
    assert dict(a.constraints) == dict(b.constraints)
    assert a.free_text_terms == b.free_text_terms
    assert dict(a.excluded_constraints) == dict(b.excluded_constraints)


def test_parse_no_lie_about_applied_dimension():
    # 「肺组织」里 tissue 已由「肺」落维；「组织」是残留头词，绝不能报成「未筛选」
    it = parse_query("人类肺组织", CAT)
    assert it.parse_status == "executable"
    assert it.unused_query_terms == []
    assert "tissue" in it.constraints


def test_demographic_query_reports_term():
    it = parse_query("儿童白血病", CAT)
    assert it.parse_status == "executable"
    assert it.unused_query_terms == ["儿童"]


# ---------- 非 executable（澄清）恒空 ----------
def test_unused_empty_on_non_executable():
    it = parse_query("不需要fastq", CAT)         # 歧义 → 澄清
    assert it.parse_status != "executable"
    assert it.unused_query_terms == []


# ---------- 工作流层：unused 透出 meta，且命中总数/名次与去掉描述词的查询逐位相同 ----------
def test_workflow_surfaces_unused_and_ranking_unchanged():
    from dataset_recommender.app.workflow import DatasetRecommendationWorkflow
    wf = DatasetRecommendationWorkflow()
    a = wf.run_with_meta(query="肺癌免疫细胞", use_llm=False)
    b = wf.run_with_meta(query="肺癌", use_llm=False)
    assert list(getattr(a, "unused_query_terms", [])) == ["免疫细胞"]
    assert list(getattr(b, "unused_query_terms", [])) == []
    # 排序真不变的强证据：命中总数逐位相同 + 前 10 名一致
    assert a.result_total == b.result_total
    assert a.retrieved_dataset_names[:10] == b.retrieved_dataset_names[:10]
