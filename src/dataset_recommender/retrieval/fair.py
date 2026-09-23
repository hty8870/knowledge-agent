# -*- coding: utf-8 -*-
"""复用就绪度检查 + 「复用公开数据」声明生成 —— 确定性、离线、只读。

## 读者是**复用者**，不是数据产出者（受众修正，本模块的组织原则）

本项目语料**按定义 100% 是别人已公开的数据**，用户是**复用者**。这条事实决定了本模块
每一句话该怎么说，也是 修掉的一批 bug 的**共同根因**：

本模块的 FAIR 框架源自「面向数据产出者」的 FAIR 自检传统，落地时**把受众一起搬了过来**，
于是 13 项检查里有 12 项在给用户下达**结构上不可能执行**的指令（「向来源仓库登记稳定
accession」「附原始测序数据以支持复分析」——那是别人的数据集，用户改不了）。
`improve` 字段因此改名 `action`：只写**复用者真能做的事**（去哪核实、稿件里怎么写）。

同一根因还催生了三处**已上线的编造**（全部已在全库验证并修复）：
  - **F1** `if uid:` 无条件 PASS —— 每条记录都有 uid（那是本工具的**内部主键**），
    于是 5667/5667 假 PASS，还把内部主键当 accession 印进证据栏。
  - **R2** 把 `has_raw_data=False` 印成「来源明确标注无原始 FASTQ」—— 其中 1786 条
    是抓取脚本的**保守猜测**，来源从未标注过。
  - **A3** 把「我们没查过文件清单」印成「无文件级直链」—— 文件级台账**只覆盖 10x 的 base 记录**。
三者是同一个 bug 家族：**把「我们不知道」编码成「它没有」**，且产物会被粘进论文。
判定依据统一下沉到 `provenance.py`（「我们凭什么这么说」的单一真源）。

## 两个产物

1. **复用就绪度检查**（`assess_fair`）：按 Findable / Accessible / Interoperable /
   Reusable 四原则给 13 项检查，每项 `pass | partial | unknown` + 证据 + `action`。
   衡量的是「**这份公开元数据够不够我拿来引用/写方法学**」，**不是**官方 FAIR 认证，
   也不是对该数据集质量的评价。`unknown` 有两种成因（来源未标注 / 本工具未核验），
   **都不等于**不满足（与项目「诚实降级」一致——缺元数据不静默判负）。

2. **「复用公开数据」声明**（`build_data_availability_statement`）：拼一段可放进稿件
   「Data Availability」小节的**英文**段落。注意它**不是 DAS 主句**——主句讲的是你自己
   产出的数据，那句我们帮不上也不该帮（见该函数的体裁边界）。只用已有字段、缺失子句
   略去并列入 `missing`，绝不臆造。

设计边界（与 `introduction.py` 同）：只重排已有字段、不做领域推断、不调用 LLM、不联网；
缺失值保持显式。本模块**只被** `webapp.py` / `mcp_server.py` 调用，检索器 / 编排 / 冻结评测
从不 import 它 —— 结构性隔离，冻结基准不受影响。
"""
from __future__ import annotations

import re
from typing import Any

from ..corpus import provenance
from ..content.introduction import meaningful_metadata_text
from .search_request import SOURCE_ALIASES

# 状态常量
PASS = "pass"
PARTIAL = "partial"
UNKNOWN = "unknown"

# 被视为「公开可检索目录」的来源（F3）。**单一真源是 search_request.SOURCE_ALIASES**
# （检索侧登记来源的唯一清单）——此前这里另手抄 6 条正则，检索侧加到 11 源后
# ENCODE/HuBMAP/Broad SCP/Zenodo/refine.bio 的 F3 恒 PARTIAL，还附一句误导性怀疑
# 程序生成后检索侧每登记新源这里自动跟上；
# 用户上传 / 未标注来源不算公开目录（partial / unknown）。
# 匹配沿用检索侧的词边界纪律：ASCII 词两侧不许紧邻字母数字（防 "110x" 这类粘连误命中），
# 含非 ASCII 的词（中文别名）直接子串匹配，大小写不敏感。
def _source_pattern(token: str) -> "re.Pattern[str]":
    escaped = re.escape(token)
    if any(ord(ch) > 127 for ch in token):
        return re.compile(escaped, re.IGNORECASE)
    return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.IGNORECASE)


_RECOGNIZED_SOURCE_PATTERNS: "tuple[re.Pattern[str], ...]" = tuple(
    _source_pattern(token)
    for _canonical, _aliases in SOURCE_ALIASES
    for token in (_canonical, *_aliases)
)


def _text(item: dict[str, Any], *keys: str) -> str:
    """第一个「有意义」（非空、非 unknown 哨兵）的字段文本。"""
    for key in keys:
        value = meaningful_metadata_text(item.get(key))
        if value:
            return value
    return ""


def _is_recognized_public_source(source: str) -> bool:
    return any(p.search(source) for p in _RECOGNIZED_SOURCE_PATTERNS)


def _https(url: str) -> bool:
    return url.lower().startswith("https://")


def _http_only(url: str) -> bool:
    low = url.lower()
    return low.startswith("http://")


def _has_file_ledger(source: str) -> bool:
    """本工具是否**真的逐文件核验过**该来源的文件清单。

    只有 10x Genomics 的 base 记录有活台账（逐文件 Range-GET 实测）。
    其余四源的文件级可及性我们从没查过 —— 这个区分决定 A3 是「查过、没有」（PARTIAL）
    还是「没查过」（UNKNOWN）。把两者混为一谈，就是本轮要修的那个 bug。
    """
    return bool(source) and provenance.SOURCE_10X.lower() in source.lower()


def _n_files(item: dict[str, Any]) -> int:
    try:
        return max(0, int(item.get("n_files") or 0))
    except (TypeError, ValueError):
        return 0


def _check(principle: str, cid: str, label: str, status: str, evidence: str, action: str) -> dict[str, str]:
    """一项检查的结果。

    `action` 字段 由 `improve` 改名 —— 这不是措辞洁癖，是**受众修正**：
    「improve」预设读者能改进这份数据，而本工具的语料**按定义 100% 是别人的公开数据**。
    旧文案 13 项里有 12 项（如「向来源仓库登记稳定 accession」「附原始测序数据以支持复分析」）
    是对**数据产出者**说的，复用者结构上不可能执行。改名是为了让下一个维护者
    看到字段名就知道这里只能写「**你**能做的事」。
    """
    return {
        "principle": principle,
        "id": cid,
        "label": label,
        "status": status,
        "evidence": evidence,
        "action": action if status != PASS else "",
    }


def assess_fair(item: dict[str, Any]) -> dict[str, Any]:
    """对单个数据集元数据做 13 项 FAIR 检查，返回 {checks, summary, gaps}。

    **读者是复用者，不是产出者**（受众修正）。这不是措辞问题，它决定每一项
    检查该说什么：本工具语料 100% 是别人已公开的数据，用户不可能「补齐」它的元数据。
    所以每项检查回答的是「**这份公开元数据够不够我拿来引用/写方法学**」，
    `action` 给的是复用者**真能做**的事（去哪核实、稿件里怎么写），
    而不是「请把你的数据整改成 FAIR」。

    `unknown` 有两种成因，都**不等于不满足**：来源未标注，或**本工具未核验**。
    后者是 新增的诚实档 —— 此前「我们没查」被静默编码成「它没有」。
    """
    name = _text(item, "dataset_name")
    source = _text(item, "source")
    species = _text(item, "species")
    tissue = _text(item, "tissue")
    disease = _text(item, "disease")
    platform = _text(item, "platform", "platform_family")
    assay = _text(item, "assay")
    chemistry = _text(item, "chemistry")
    description = _text(item, "description")
    url = _text(item, "url")
    download_url = _text(item, "download_url")
    uid = _text(item, "dataset_uid")
    published = _text(item, "published_date")
    count = _text(item, "count")
    unit = _text(item, "unit")
    preservation = _text(item, "preservation_method")
    software = _text(item, "analysis_software")
    n_files = _n_files(item)
    any_url = url or download_url

    # 标识符的四种语义与「原始数据我们凭什么这么说」，统一走 provenance —— 与 DAS 同一真源，
    # 免得同一条记录在 FAIR 表里和在英文声明里说法不一致。
    accession = provenance.public_accession(item.get("dataset_uid"), source)
    platform_uuid = provenance.platform_id(item.get("dataset_uid"), source)
    doi = provenance.collection_doi(item)
    raw = provenance.raw_data_provenance(item)

    checks: list[dict[str, str]] = []

    # ---------- Findable ----------
    # F1 修复前：`if uid:` —— 而**每条记录都有 uid**（那是本工具的内部主键），
    # 于是全库 5667/5667 无条件 PASS，还把内部主键当 accession 印进证据栏。
    # 现在按标识符的四种真实语义分层，见 provenance.py 顶部。
    if accession:
        checks.append(_check("F", "F1", "持久标识符", PASS, f"来源登记的公开编号（accession）：{accession}", ""))
    elif platform_uuid:
        checks.append(_check("F", "F1", "持久标识符", PASS, f"{source or '来源平台'}的稳定公开定位符：{platform_uuid}", ""))
    elif doi:
        checks.append(_check("F", "F1", "持久标识符", PARTIAL, f"只有 collection 级 DOI（{doi}），无数据集级标识符",
                             "该 DOI 指向产出这批数据的论文、可能涵盖多个数据集；要精确指认你用的这一个，请在稿件里用来源页 URL"))
    elif any_url:
        checks.append(_check("F", "F1", "持久标识符", PARTIAL, "只有来源页 URL，来源未登记独立编号",
                             "该来源没给这个数据集编号，你无从补登记；稿件里用来源页 URL 指认，并注明访问日期（站点改版会让 URL 失效）"))
    else:
        checks.append(_check("F", "F1", "持久标识符", UNKNOWN, "本记录未见编号或 URL",
                             "投稿前须到来源确认这个数据集如何被公开指认，否则读者无法定位它"))

    descriptive = [x for x in (species, (tissue or disease), (platform or assay), description) if x]
    if len(descriptive) >= 3:
        checks.append(_check("F", "F2", "描述性元数据", PASS, f"已含 {len(descriptive)}/4 类描述字段", ""))
    elif descriptive:
        checks.append(_check("F", "F2", "描述性元数据", PARTIAL, f"来源只公开了 {len(descriptive)}/4 类描述字段",
                             "缺的字段来源没公开，本工具也无从得知；写方法学需要的那几项请到来源页面或关联论文核实"))
    else:
        checks.append(_check("F", "F2", "描述性元数据", UNKNOWN, "来源未公开物种/组织/疾病/平台/摘要",
                             "这条记录几乎没有描述字段；投稿前必须到来源核实它到底是什么数据"))

    if source and _is_recognized_public_source(source):
        checks.append(_check("F", "F3", "公开可检索来源", PASS, f"来源：{source}", ""))
    elif source:
        checks.append(_check("F", "F3", "公开可检索来源", PARTIAL, f"来源「{source}」不是本工具已识别的公开单细胞目录",
                             "投稿前确认它是否长期公开可访问；审稿人需要能自己取到这份数据"))
    else:
        checks.append(_check("F", "F3", "公开可检索来源", UNKNOWN, "本记录未标注来源",
                             "无法判断这份数据是否公开可检索，投稿前必须确认其出处"))

    # ---------- Accessible ----------
    if any_url:
        checks.append(_check("A", "A1", "可获取链接", PASS, any_url, ""))
    else:
        checks.append(_check("A", "A1", "可获取链接", UNKNOWN, "本记录未见访问链接",
                            "投稿前须到来源确认公开访问方式；审稿人取不到数据会直接质疑"))

    if _https(url) or _https(download_url):
        checks.append(_check("A", "A2", "标准协议(HTTPS)", PASS, "使用 https", ""))
    elif _http_only(url) or _http_only(download_url):
        checks.append(_check("A", "A2", "标准协议(HTTPS)", PARTIAL, "来源给的是明文 http 链接",
                             "不影响你引用；抄进稿件前可试试同址的 https 版本"))
    else:
        checks.append(_check("A", "A2", "标准协议(HTTPS)", UNKNOWN, "本记录无可判定协议的链接",
                             "投稿前确认来源的公开访问链接"))

    # A3 修复前：n_files==0 一律 PARTIAL + 证据「仅数据集页面链接，无文件级直链」——
    # 那是对 4900 条 external 的**假事实断言**。本工具的文件级台账**只覆盖 10x 的 base 记录**，
    # 其余四源有没有文件级直链，我们从没查过。「没查过」只能是 UNKNOWN。
    if n_files > 0:
        checks.append(_check("A", "A3", "文件级可下载", PASS, f"{n_files} 个可下载文件直链（本工具逐文件实测）", ""))
    elif _has_file_ledger(source):
        checks.append(_check("A", "A3", "文件级可下载", PARTIAL, "本工具的文件级台账在该数据集下未收录直链",
                             "到来源页面看是否需要登录/申请后才能拿到文件"))
    else:
        checks.append(_check("A", "A3", "文件级可下载", UNKNOWN, "本工具未核验该来源的文件级可及性（文件级台账只覆盖 10x Genomics）",
                             "如需在稿件里点名具体文件（矩阵/FASTQ），请到来源页面核对清单"))

    # ---------- Interoperable ----------
    bio_terms = [x for x in (species, tissue, disease) if x]
    if len(bio_terms) >= 2:
        checks.append(_check("I", "I1", "受控生物学词表", PASS, "、".join(bio_terms), ""))
    elif bio_terms:
        checks.append(_check("I", "I1", "受控生物学词表", PARTIAL, "来源只标注了：" + "、".join(bio_terms),
                             "缺的那几项来源没标，本工具也无从得知；写方法学要用就到来源页面核实"))
    else:
        checks.append(_check("I", "I1", "受控生物学词表", UNKNOWN, "来源未标注物种/组织/疾病",
                             "投稿前到来源页面或关联论文核实这三项"))

    tech = " / ".join(x for x in (platform, assay, chemistry) if x)
    if tech:
        checks.append(_check("I", "I2", "平台/技术标准化", PASS, tech, ""))
    else:
        checks.append(_check("I", "I2", "平台/技术标准化", UNKNOWN, "来源未标注平台/技术",
                             "这是方法学必需项，投稿前须到来源页面或关联论文核实"))

    # I3：目录记录本身已归一为结构化字段——真实成立，非占位。
    checks.append(_check("I", "I3", "结构化机器可读", PASS, "已归一为标准键的结构化 JSON 记录", ""))

    # ---------- Reusable ----------
    prov = [x for x in (software, preservation) if x]
    if len(prov) >= 2:
        checks.append(_check("R", "R1", "处理溯源", PASS, "、".join(prov), ""))
    elif prov:
        checks.append(_check("R", "R1", "处理溯源", PARTIAL, "来源只公开了：" + "、".join(prov),
                             "缺的那项来源没公开；如果你的方法学要写清上游处理流程，须到关联论文核实"))
    else:
        checks.append(_check("R", "R1", "处理溯源", UNKNOWN, "来源未公开分析软件/样本保存方式",
                             "这份数据经过了什么处理，来源没说；复用前建议到关联论文核实，否则难以解释批次效应"))

    # R2 修复前：直接读 `has_raw_data`，把 False 印成「来源明确标注无原始 FASTQ」。
    # 实测 4630 条 False 里有 1786 条（ArrayExpress）是抓取脚本的**保守猜测**
    # （`ingest_arrayexpress.py:19` 注释原文：「保守：不逐条核实 FASTQ，宁可漏不可错报」），
    # 来源从未标注过 —— 那句证据是**假的**。现在按证据等级三态判定。
    if raw["state"] == provenance.LISTED:
        checks.append(_check("R", "R2", "原始数据(FASTQ)可及", PASS, raw["evidence"], ""))
    elif raw["state"] == provenance.NOT_LISTED:
        checks.append(_check("R", "R2", "原始数据(FASTQ)可及", PARTIAL, raw["evidence"],
                             raw.get("elsewhere_hint") or "如需复分析原始 reads，请到来源页面或其它归档库核实"))
    else:
        # NOT_CHECKED → UNKNOWN。这会让 readiness_pct 下移：**那 0.5 分本来就是拿假 False 白拿的**。
        checks.append(_check("R", "R2", "原始数据(FASTQ)可及", UNKNOWN, raw["evidence"],
                             raw.get("elsewhere_hint") or "到来源页面核实是否提供原始 FASTQ"))

    scale = bool(count and unit)
    if scale and n_files > 0:
        checks.append(_check("R", "R3", "规模与文件级元数据", PASS, f"{count} {unit}；{n_files} 个文件", ""))
    elif scale or n_files > 0:
        checks.append(_check("R", "R3", "规模与文件级元数据", PARTIAL, (f"{count} {unit}" if scale else f"{n_files} 个文件"),
                             "另一半来源没公开；样本量/文件大小要写进稿件的话，请到来源页面核实"))
    else:
        checks.append(_check("R", "R3", "规模与文件级元数据", UNKNOWN, "来源未公开样本量与文件级信息",
                             "投稿前到来源页面核实规模，读者要靠它判断统计效力"))

    if published:
        checks.append(_check("R", "R4", "发表/发布时间", PASS, published, ""))
    else:
        checks.append(_check("R", "R4", "发表/发布时间", UNKNOWN, "来源未标注发布时间",
                             "到来源页面或关联论文核实；数据版本与日期影响可复现性"))

    n_pass = sum(1 for c in checks if c["status"] == PASS)
    n_partial = sum(1 for c in checks if c["status"] == PARTIAL)
    n_unknown = sum(1 for c in checks if c["status"] == UNKNOWN)
    total = len(checks)
    gaps = [
        {"id": c["id"], "label": c["label"], "action": c["action"]}
        for c in checks
        if c["status"] != PASS
    ]
    summary = {
        "pass": n_pass,
        "partial": n_partial,
        "unknown": n_unknown,
        "total": total,
        # 「复用就绪度」：pass 计 1、partial 计 0.5，四舍五入到整数百分比。
        # 它衡量的是**这份公开元数据够不够你拿来引用/写方法学**，不是这个数据集的质量，
        # 更不是官方 FAIR 评分。unknown 计 0 —— 「我们不知道」不该白拿分。
        "readiness_pct": round((n_pass + 0.5 * n_partial) / total * 100) if total else 0,
        "statement": (
            f"{total} 项复用就绪度检查：{n_pass} 项充分、{n_partial} 项部分、{n_unknown} 项未知"
            "（未知 = 来源未标注，或本工具未核验；都不等于不满足）。"
        ),
    }
    return {"checks": checks, "summary": summary, "gaps": gaps}


def _tech_phrase(item: dict[str, Any]) -> str:
    """投稿声明里对「什么数据」的一个保守英文描述子，只用已知字段。

    此前 `if assay: return "dataset"` 与兜底逐字节相同 —— 一个纯死枝，
    它让人以为 assay 参与了判断。真正的 bug 在上游：`webapp._web_item_from_record`
    从没把 `modality` 放进 item，于是全库 **5667/5667** 都走到兜底、一律印成泛泛的
    "dataset"。补上传参后：single-cell 3456 条（61.0%）、spatial 894 条（15.8%）
    拿回准确措辞，其余 1317 条**确实**没有 modality，"dataset" 是诚实的兜底。
    """
    modality = _text(item, "modality")
    if modality == "spatial":
        return "spatial transcriptomics dataset"
    if modality == "single-cell":
        return "single-cell dataset"
    return "dataset"


def build_data_availability_statement(item: dict[str, Any]) -> dict[str, Any]:
    """拼一段可直接投稿的**英文**「复用公开数据」说明；缺失子句略去并列入 missing。

    **体裁边界（务必读，这决定了它该说什么、不该说什么）**：
    真正的 Data Availability Statement 主句讲的是**作者自己产出**的数据去向
    （"The data generated in this study have been deposited in GEO under GSExxxxx"）。
    本项目语料**按定义 100% 是别人的公开数据**，用户是**复用者**。
    所以这里生成的**不是 DAS 主句**，而是 DAS 里「我们复用了哪些公开数据」那一段。
    「你自己产出的数据那一句」我们帮不上，也**永远不该**帮 —— 那需要吃进用户的未发表工作。
    这条边界不是外部规则，是产品定义：见 `reuse_pack.py` 顶部的「不做什么」。

    **标识符的四种语义**（修复；此前全部被拍平成 `under accession "{uid}"`）：
      - 有真 accession（AE/SCEA，2170 条） → `under accession E-MTAB-11814`
      - 只有平台 UUID（CELLxGENE/HCA，2730 条） → `in CELLxGENE Discover as dataset <UUID>`
      - 有 collection DOI（2155 条，全 CELLxGENE） → `associated with collection DOI ...`（**论文级，非数据集引用**）
      - 都没有（10x base 记录） → 只给 URL
    修复前实测：**5667/5667 = 100%** 的记录被印上编造的 accession，包括
    `under accession "cxg:24921392-22ed-479a-9144-7d40adf148ae"`（内部 UUID）与
    `under accession "ae:E-MTAB-11814"`（真编号被内部前缀污染）。
    """
    name = _text(item, "dataset_name")
    source = _text(item, "source")
    url = _text(item, "url")
    download_url = _text(item, "download_url")
    n_files = _n_files(item)
    any_url = url or download_url

    missing: list[str] = []

    subject = f'The {_tech_phrase(item)} "{name}"' if name else f"The {_tech_phrase(item)}"

    # 来源名走 english_source_name：五个公开来源本来就是 ASCII，但**用户上传的记录
    # 来源名可能是中文**（如「用户上传」），直拼会让这句英文变成中英混排。
    # 反之 `name`（数据集名）不过滤 —— 数据集真叫中文名时，声明里出现它是对的。
    source_en = provenance.english_source_name(source)
    if source and _is_recognized_public_source(source):
        avail = f"is publicly available from {source_en}"
    elif source:
        avail = f"is available from {source_en}"
    else:
        avail = "is described in the accompanying dataset metadata"
        missing.append("未标注公开数据仓库（source）")

    # ---- 标识符：按真实语义分层，没有就不写（不编）----
    accession = provenance.public_accession(item.get("dataset_uid"), source)
    pid = provenance.platform_id(item.get("dataset_uid"), source)
    doi = provenance.collection_doi(item)
    if accession:
        avail += f" under accession {accession}"
    elif pid:
        avail += f" as dataset {pid}"
    else:
        missing.append("该来源未提供可引用的 accession（本工具的内部编号不是 accession，不会替你写进声明）")

    if any_url:
        avail += f" at {any_url}"
    else:
        missing.append("无公开访问 URL")

    sentence = subject + " " + avail + "."

    if doi:
        # 措辞刻意用 "associated with collection DOI" 而非 "cite as"：
        # 它是 collection 级、指向产出该数据的论文，不是 dataset 级的数据引用（见 provenance.collection_doi）。
        sentence += f" The associated collection is described in https://doi.org/{doi}."

    if n_files > 0:
        sentence += f" The repository provides {n_files} downloadable file{'s' if n_files != 1 else ''}."
    elif any_url:
        # 修复前这里写「仅数据集页面链接，无文件级下载直链」—— 那是对 4900 条 external 的**假事实断言**：
        # 本工具的文件级台账只覆盖 10x 的 base 记录，从没查过其余四源有没有文件级直链。
        missing.append("本工具未核验该来源的文件级可及性（文件级台账只覆盖 10x Genomics 的 767 条）")

    # ---- 原始数据：带作用域、带证据、带日期的否定；未核验则**一个字都不写** ----
    raw = provenance.raw_data_provenance(item)
    # 注意用 `scope_en` 而非 `scope`：后者是给中文界面的。测试当场抓到过混血产物
    # `"...are listed in the 10x Genomics 官方下载页的文件清单."` —— 它会被原样粘进论文。
    if raw["state"] == provenance.LISTED:
        sentence += f" Raw sequencing data (FASTQ) are listed in {raw['scope_en']}."
    elif raw["state"] == provenance.NOT_LISTED:
        as_of = f" as captured on {raw['as_of']}" if raw.get("as_of") else ""
        # 关键：说的是「这份清单里没列出」，**不是**「这个数据集没有原始数据」。
        sentence += (
            f" No FASTQ files are listed for this dataset in {raw['scope_en']}{as_of};"
            " raw reads may be deposited in another archive."
        )
    else:
        # NOT_CHECKED：不作任何正面或负面结论。修复前这 1786 条 ArrayExpress 被印成
        # "Raw sequencing data (FASTQ) are not available for this dataset."——而 ingest 注释
        # 白纸黑字写着那个 False 是「保守占位、宁可漏不可错报」。
        missing.append(f"原始数据可及性：{raw['evidence']}")
    if raw.get("elsewhere_hint"):
        missing.append(f"原始数据线索：{raw['elsewhere_hint']}")

    # 面向用户的运行期字符串不带 markdown 强调：前端 escapeHtml 后字面星号会原样显示
    # （集成验证）。中文强调用本仓库既有的「」约定。
    notes = "英文段落由公开元数据自动拼接，只覆盖你「复用的公开数据」；投稿前请核对准确性" + (
        "。以下内容本工具无法提供，需你自行核实或补写：" + "；".join(missing) if missing else "。"
    )
    return {"statement": sentence, "missing": missing, "notes": notes}


def build_fair_report(item: dict[str, Any]) -> dict[str, Any]:
    """FAIR 自检 + DAS 合并入口（Web `/api/fair` 与 MCP `assess_dataset_fair` 共用单一真源）。"""
    return {
        "dataset_name": _text(item, "dataset_name"),
        "source": _text(item, "source") or "未说明",
        "fair": assess_fair(item),
        "data_availability": build_data_availability_statement(item),
    }


def build_fair_reports(items: "list[dict[str, Any]]") -> "list[dict[str, Any]]":
    """一批数据集的 FAIR 自检。逐条附上 `dataset_uid`，让调用方能把报告与清单对齐。

    薄循环，`build_fair_report` 本身一个字不改：任务包要能机械核对
    「结果清单 / 下载计划 / FAIR / 引文」四份产物覆盖的数据集完全相同，缺了 uid 就对不上。
    """
    out: "list[dict[str, Any]]" = []
    for item in items:
        report = build_fair_report(item)
        report["dataset_uid"] = str((item or {}).get("dataset_uid") or "")
        checks = report.get("fair", {}).get("checks", [])
        report["checks"] = checks
        out.append(report)
    return out
