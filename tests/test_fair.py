from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.retrieval.fair import (  # noqa: E402
    PARTIAL,
    PASS,
    UNKNOWN,
    assess_fair,
    build_data_availability_statement,
    build_fair_report,
)


def _rich_item() -> dict:
    """一条**真实存在于 base 台账**的 10x 记录（uid 必须真，R2 要走活台账实测）。"""
    return {
        "dataset_name": "10k Human Brain Nuclei, Chromium GEM-X Epi Multiome",
        "source": "10x Genomics",
        "species": "Human",
        "tissue": "Brain",
        "disease": "unknown",
        "platform": "chromium",
        "assay": "multiome",
        "chemistry": "Epi Multiome",
        "description": "10k Human Brain Nuclei ...",
        "modality": "single-cell",
        "url": "https://www.10xgenomics.com/datasets/multiome-gemx-10k-human-brain",
        "download_url": "https://cf.10xgenomics.com/samples/x/y/file.tar.gz",
        "dataset_uid": "multiome-gemx-10k-human-brain",
        "published_date": "2026-04-20",
        "count": "10000",
        "unit": "Nuclei",
        "preservation_method": "Fresh Frozen",
        "analysis_software": "Cell Ranger ARC",
        "software_version": "2.2.0",
        "n_files": 12,
        "has_raw_data": True,
    }


def _cxg_item() -> dict:
    return {
        "dataset_name": "Some CELLxGENE collection dataset",
        "source": "CELLxGENE Discover",
        "species": "Human",
        "tissue": "Lung",
        "platform": "10x 3' v3",
        "modality": "single-cell",
        "url": "https://cellxgene.cziscience.com/e/24921392-22ed-479a-9144-7d40adf148ae",
        "dataset_uid": "cxg:24921392-22ed-479a-9144-7d40adf148ae",
        "collection_doi": "10.1038/s41586-020-2157-4",
        "has_raw_data": False,
        "n_files": 0,
    }


def _ae_item() -> dict:
    return {
        "dataset_name": "An ArrayExpress study",
        "source": "ArrayExpress",
        "species": "Human",
        "tissue": "Liver",
        "modality": "single-cell",
        "url": "https://www.ebi.ac.uk/biostudies/arrayexpress/studies/E-MTAB-11814",
        "dataset_uid": "ae:E-MTAB-11814",
        "has_raw_data": False,   # 抓取脚本的**保守猜测**，不是来源的声明
        "n_files": 0,
    }


def _status(report: dict, cid: str) -> str:
    return next(c["status"] for c in report["checks"] if c["id"] == cid)


def _check_of(report: dict, cid: str) -> dict:
    return next(c for c in report["checks"] if c["id"] == cid)


def test_summary_counts_are_consistent() -> None:
    report = assess_fair(_rich_item())
    s = report["summary"]
    assert s["pass"] + s["partial"] + s["unknown"] == s["total"] == len(report["checks"])
    assert 0 <= s["readiness_pct"] <= 100


def test_unknown_sentinel_is_not_counted_as_present() -> None:
    # disease == "unknown" must be treated as missing, not a real value.
    item = _rich_item()
    item["disease"] = "unknown"
    item["tissue"] = ""
    # only species remains among F2 bio terms -> I1 partial (1 of species/tissue/disease)
    report = assess_fair(item)
    assert _status(report, "I1") == PARTIAL


def test_sparse_record_degrades_without_crashing() -> None:
    item = {"dataset_name": "Mystery dataset"}
    report = assess_fair(item)
    assert _status(report, "F1") == UNKNOWN
    assert _status(report, "A1") == UNKNOWN
    assert _status(report, "R2") == UNKNOWN
    assert report["summary"]["pass"] < report["summary"]["total"]
    # I3 (structured record) is always a genuine pass.
    assert _status(report, "I3") == PASS


def test_source_recognition() -> None:
    base = _rich_item()
    assert _status(assess_fair({**base, "source": "CELLxGENE Discover"}), "F3") == PASS
    assert _status(assess_fair({**base, "source": "EBI SCEA"}), "F3") == PASS
    assert _status(assess_fair({**base, "source": "用户上传"}), "F3") == PARTIAL
    assert _status(assess_fair({**base, "source": ""}), "F3") == UNKNOWN


def test_protocol_check() -> None:
    base = _rich_item()
    assert _status(assess_fair({**base, "url": "http://x.org/d", "download_url": ""}), "A2") == PARTIAL
    assert _status(assess_fair({**base, "url": "", "download_url": ""}), "A2") == UNKNOWN


# ------------------------------------------------------- F1：标识符四层语义

def test_f1_is_not_a_free_pass_on_internal_uid() -> None:
    """修复前 F1 是 `if uid:` —— 每条记录都有 uid（内部主键），全库 5667/5667 假 PASS。

    base 的 uid 是 URL slug，不是持久标识符 → 只能 PARTIAL。
    """
    report = assess_fair(_rich_item())
    assert _status(report, "F1") == PARTIAL
    ev = _check_of(report, "F1")["evidence"]
    assert "multiome-gemx-10k-human-brain" not in ev   # 内部主键不许出现在证据栏
    assert "accession" not in ev


def test_f1_passes_on_real_accession_and_platform_uuid() -> None:
    ae = assess_fair(_ae_item())
    assert _status(ae, "F1") == PASS
    assert "E-MTAB-11814" in _check_of(ae, "F1")["evidence"]
    assert "ae:" not in _check_of(ae, "F1")["evidence"]   # 前缀污染

    cxg = assess_fair(_cxg_item())
    assert _status(cxg, "F1") == PASS
    assert "24921392-22ed-479a-9144-7d40adf148ae" in _check_of(cxg, "F1")["evidence"]
    # UUID 是稳定公开定位符，但**不是** accession —— 措辞不得混用
    assert "accession" not in _check_of(cxg, "F1")["evidence"]


# ------------------------------------------------------- R2：按证据等级三态

def test_r2_tri_state_follows_evidence_tier_not_the_raw_bool() -> None:
    """R2 的三态由**证据等级**决定，不再直接读 has_raw_data。

    ArrayExpress 的 False 是抓取脚本的保守猜测（`ingest_arrayexpress.py:19` 自陈
    「不逐条核实 FASTQ」）→ 必须是 unknown，不能白拿 partial 的 0.5 分。
    """
    assert _status(assess_fair(_rich_item()), "R2") == PASS       # 10x 活台账实测有
    assert _status(assess_fair(_cxg_item()), "R2") == PARTIAL     # 库级事实：只分发矩阵
    assert _status(assess_fair(_ae_item()), "R2") == UNKNOWN      # 没查过


def test_r2_evidence_no_longer_claims_the_source_said_so() -> None:
    """修复前 R2 证据栏写「来源明确标注无原始 FASTQ」—— 对 4476 条来说来源从未标注过。"""
    for item in (_cxg_item(), _ae_item()):
        ev = _check_of(assess_fair(item), "R2")["evidence"]
        assert "来源明确标注" not in ev


def test_r2_unknown_still_offers_a_reuser_action() -> None:
    action = _check_of(assess_fair(_ae_item()), "R2")["action"]
    assert action and "ENA" in action          # 方向上原始数据大概率在 ENA，必须说


# ------------------------------------------------------- A3：查过 vs 没查过

def test_a3_distinguishes_checked_from_unchecked() -> None:
    """文件级台账**只覆盖 10x 的 base 记录**；对其余四源说「无文件级直链」是假事实断言。"""
    assert _status(assess_fair(_rich_item()), "A3") == PASS        # 台账里有 12 个直链
    assert _status(assess_fair({**_rich_item(), "n_files": 0}), "A3") == PARTIAL  # 查过、没有

    cxg = assess_fair(_cxg_item())
    assert _status(cxg, "A3") == UNKNOWN                            # 没查过 → 不许断言
    assert "未核验" in _check_of(cxg, "A3")["evidence"]


# ------------------------------------------------------- action：受众

def test_action_field_replaced_improve_and_drops_producer_voice() -> None:
    """`improve` → `action`：13 项里曾有 12 项在给复用者下达不可能执行的指令。"""
    producer_voice = (
        "向来源仓库登记", "将数据存入", "附原始测序数据", "补齐核心描述字段",
        "为数据集登记", "提供公开可访问", "公开文件级下载直链", "标注并公开",
        "记录分析软件版本", "改用 https 提供", "使用受控本体标注",
    )
    for item in (_rich_item(), _cxg_item(), _ae_item(), {"dataset_name": "X"}):
        report = assess_fair(item)
        for c in report["checks"]:
            assert "improve" not in c                       # 旧键必须彻底消失
            assert "action" in c
            for bad in producer_voice:
                assert bad not in c["action"], (c["id"], bad)
        for g in report["gaps"]:
            assert set(g) == {"id", "label", "action"}


def test_pass_checks_carry_no_action() -> None:
    for c in assess_fair(_rich_item())["checks"]:
        if c["status"] == PASS:
            assert c["action"] == ""


def test_every_non_pass_check_gives_the_reuser_something_to_do() -> None:
    """诚实降级不等于摊手：每个非 PASS 项都必须给出复用者**真能执行**的下一步。"""
    for item in (_rich_item(), _cxg_item(), _ae_item(), {"dataset_name": "X"}):
        for c in assess_fair(item)["checks"]:
            if c["status"] != PASS:
                assert c["action"].strip(), c["id"]


# ------------------------------------------------------- DAS：反编造

def test_das_never_fabricates_an_accession() -> None:
    """修复前：`under accession "{uid}"` 无条件成立，全库 5667/5667 都被印上编造的编号。"""
    # base：只有 URL slug，不是 accession → 不写 accession 子句。
    # （slug 本身仍会作为 **URL 的一部分**出现，那是对的；错的是把它当编号。）
    stmt = build_data_availability_statement(_rich_item())["statement"]
    assert "accession" not in stmt
    assert "https://www.10xgenomics.com/datasets/multiome-gemx-10k-human-brain" in stmt

    # CELLxGENE：UUID 是平台定位符，不是 accession
    cxg = build_data_availability_statement(_cxg_item())["statement"]
    assert "under accession" not in cxg
    assert "as dataset 24921392-22ed-479a-9144-7d40adf148ae" in cxg
    assert "cxg:" not in cxg                                  # 内部前缀不得外泄

    # ArrayExpress：真编号，但必须剥掉内部前缀
    ae = build_data_availability_statement(_ae_item())["statement"]
    assert "under accession E-MTAB-11814" in ae
    assert "ae:" not in ae


def test_das_collection_doi_is_scoped_to_the_paper_not_the_dataset() -> None:
    """collection DOI 指向论文、可能涵盖几十个数据集 → 措辞不得让人以为它唯一指向这一个。"""
    stmt = build_data_availability_statement(_cxg_item())["statement"]
    assert "https://doi.org/10.1038/s41586-020-2157-4" in stmt
    assert "collection" in stmt
    assert "cite this dataset as" not in stmt.lower()


def test_das_raw_data_negation_is_always_scoped() -> None:
    """「这份清单里没列出」≠「它没有原始数据」。后者是审稿人一查即穿的假话。"""
    cxg = build_data_availability_statement(_cxg_item())["statement"]
    assert "No FASTQ files are listed for this dataset in CELLxGENE Discover" in cxg
    assert "may be deposited in another archive" in cxg
    # 绝不能出现无作用域的否定
    assert "Raw sequencing data (FASTQ) are not available" not in cxg


def test_das_says_nothing_when_it_has_not_checked() -> None:
    """ArrayExpress 1786 条：没查过 → 正文一个字都不写，只在 missing 里交代。"""
    das = build_data_availability_statement(_ae_item())
    assert "FASTQ" not in das["statement"]
    assert any("未核验" in m for m in das["missing"])
    assert any("ENA" in m for m in das["missing"])


def test_das_stamps_as_of_only_on_ledger_verified_negation() -> None:
    item = {**_rich_item(), "dataset_uid": "definitely-not-in-the-ledger"}
    das = build_data_availability_statement(item)
    # 台账里没有 → NOT_CHECKED → 不写日期、不下结论
    assert "as captured on" not in das["statement"]


def test_das_never_fabricates_missing_fields() -> None:
    das = build_data_availability_statement({"dataset_name": "X"})
    stmt = das["statement"]
    assert "accession" not in stmt  # no uid -> no accession clause
    assert " at http" not in stmt  # no url -> no url clause
    assert das["missing"]  # gaps recorded
    # honest wording when no recognized public source
    assert "publicly available" not in stmt


def test_das_singular_plural_file() -> None:
    one = build_data_availability_statement({**_rich_item(), "n_files": 1})["statement"]
    assert "1 downloadable file." in one
    many = build_data_availability_statement({**_rich_item(), "n_files": 3})["statement"]
    assert "3 downloadable files." in many


def test_das_tech_phrase_uses_modality() -> None:
    """修复前 `_web_item_from_record` 漏传 modality → 全库 5667/5667 退化成泛泛的 'dataset'。"""
    assert "single-cell dataset" in build_data_availability_statement(_rich_item())["statement"]
    spatial = build_data_availability_statement({**_rich_item(), "modality": "spatial"})["statement"]
    assert "spatial transcriptomics dataset" in spatial
    # 没有 modality 时诚实兜底，不瞎猜
    plain = build_data_availability_statement({**_rich_item(), "modality": ""})["statement"]
    assert "The dataset" in plain


def test_das_boilerplate_is_pure_english() -> None:
    """`statement` 会被**原样粘进稿件** → 我们的样板文案里不许混入一个中文字。

    这条是真事故的回归网：`scope`（中文，给界面）曾被拼进英文句子，产出
    `"...are listed in the 10x Genomics 官方下载页的文件清单."`
    """
    for item in (_rich_item(), _cxg_item(), _ae_item(), {"dataset_name": "X"},
                 {**_rich_item(), "n_files": 0}, {**_rich_item(), "modality": "spatial"}):
        stmt = build_data_availability_statement(item)["statement"]
        assert not [ch for ch in stmt if "一" <= ch <= "鿿"], stmt


def test_chinese_source_name_degrades_instead_of_crashing() -> None:
    """用户上传的记录来源名可能是中文 —— 那时**降级成中性说法**，绝不能崩。

    这里刻意不做「整句禁止中文」的一刀切：那会在合法数据上炸掉 /api/fair。
    要管的是**我们的样板文案**，不是用户的数据值。
    """
    das = build_data_availability_statement({**_rich_item(), "source": "用户上传"})
    stmt = das["statement"]
    assert "用户上传" not in stmt                    # 中文来源名不进英文句子
    assert "the source repository" in stmt          # 退回中性说法
    assert not [ch for ch in stmt if "一" <= ch <= "鿿"]


def test_chinese_dataset_name_is_kept_verbatim() -> None:
    """数据集**真叫**中文名时，英文声明里出现它是对的 —— 那是它的名字，不是漏进去的样板。"""
    stmt = build_data_availability_statement({**_rich_item(), "dataset_name": "小鼠肾脏图谱"})["statement"]
    assert "小鼠肾脏图谱" in stmt


def test_fair_report_shape() -> None:
    report = build_fair_report(_rich_item())
    assert set(report) == {"dataset_name", "source", "fair", "data_availability"}
    assert "checks" in report["fair"] and "summary" in report["fair"]
    assert "statement" in report["data_availability"]


def test_all_registered_sources_pass_f3_and_das():
    """对账钉：检索侧 SOURCE_ALIASES 登记的每个来源，
    F3 必须 PASS、DAS 必须用 "is publicly available from"——否则就是又一次
    「两份手抄清单漂移」（此前 6 源正则 vs 11 源别名，新源恒 PARTIAL 还附误导性怀疑）。"""
    from dataset_recommender.retrieval.search_request import SOURCE_ALIASES

    assert len(SOURCE_ALIASES) >= 11, "清单收缩过？对账钉应随检索侧来源只增不减"
    base = _rich_item()
    for canonical, _aliases in SOURCE_ALIASES:
        item = {**base, "source": canonical}
        assert _status(assess_fair(item), "F3") == PASS, canonical
        stmt = build_data_availability_statement(item)["statement"]
        assert f"is publicly available from {canonical}" in stmt, canonical
