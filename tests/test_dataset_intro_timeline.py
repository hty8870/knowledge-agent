from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.content.introduction import build_dataset_introduction  # noqa: E402
from dataset_recommender.app.webapp import (  # noqa: E402
    _rows_from_retrieved,
    api_datasets,
    api_introduction,
)


def _body(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def test_introduction_prefers_plain_source_metadata_and_strips_html():
    intro = build_dataset_introduction(
        {
            "dataset_name": "Demo",
            "description": "<p>Human <b>breast</b> atlas &amp; validation.</p>",
            "source": "Example",
            "species": "Human",
            "has_raw_data": True,
        }
    )
    assert intro["summary"] == "Human breast atlas & validation."
    assert intro["summary_source"] == "source_metadata"
    assert intro["source_label"] == "来源元数据"
    assert "<" not in intro["summary"] and ">" not in intro["summary"]
    assert any(f["label"] == "原始数据" and "FASTQ" in f["value"] for f in intro["facts"])


def test_introduction_falls_back_to_existing_fields_without_inference():
    intro = build_dataset_introduction(
        {
            "dataset_name": "Minimal",
            "source": "Upload",
            "species": "Human",
            "tissue": "breast",
            "count": "12",
            "unit": "samples",
        }
    )
    assert intro["summary_source"] == "field_summary"
    assert "Human" in intro["summary"] and "breast" in intro["summary"] and "12 samples" in intro["summary"]
    assert "不推断缺失信息" in intro["caveat"]
    assert any(f["label"] == "疾病" and f["value"] == "未说明" for f in intro["facts"])


def test_introduction_hides_missing_value_sentinels():
    intro = build_dataset_introduction(
        {
            "source": "Example",
            "species": "Human",
            "tissue": "unknown",
            "disease": "not provided",
            "platform": "n/a",
            "platform_family": "Chromium",
            "published_date": "-",
        }
    )
    facts = {fact["label"]: fact["value"] for fact in intro["facts"]}
    assert facts["组织"] == facts["疾病"] == facts["发表时间"] == "未说明"
    assert facts["技术与平台"] == "Chromium"
    assert "unknown" not in intro["summary"].lower()


def test_recommend_rows_carry_structured_introduction():
    # source 用 HCA（其 ingest 契约 = description 为来源散文原文）。
    # 原夹具用 "10x Genomics"，但 base 的 description 按契约是**索引器自拼的模板**，
    # 起体裁路由不再把它当来源摘要展示 —— 本测的意图是「行携带结构化介绍」，
    # 与体裁无关，故换一个散文源保持意图不变。
    row = _rows_from_retrieved(
        [
            {
                "dataset_name": "X",
                "species": "Human",
                "description": "A source description.",
                "published_date": "2025-04-01",
                "source": "Human Cell Atlas",
            }
        ]
    )[0]
    assert row["published_date"] == "2025-04-01"
    assert row["introduction"]["summary"] == "A source description."
    assert len(row["introduction"]["facts"]) >= 8


def test_introduction_sample_size_accepts_workflow_dict_shape():
    """MCP 富化把 workflow 序列化后的候选（sample_size 为结构化 dict）直接喂给同一个 builder；
    样本量事实必须落到 display 字符串，绝不能泄成 Python 字典 repr —— 否则 MCP 与 Web 口径分叉，
    破坏 T7「Web/MCP 同源」。此测锁住该契约（门禁此前照不到）。"""
    dict_shaped = build_dataset_introduction(
        {
            "dataset_name": "Y",
            "source": "10x Genomics",
            "species": "Human",
            "sample_size": {"count": "5000", "unit": "cells", "display": "5000 cells", "unit_explanation": "细胞数"},
        }
    )
    flat_shaped = build_dataset_introduction(
        {"dataset_name": "Y", "source": "10x Genomics", "species": "Human", "sample_size": "5000 cells"}
    )
    dict_fact = next(f for f in dict_shaped["facts"] if f["label"] == "样本量")["value"]
    flat_fact = next(f for f in flat_shaped["facts"] if f["label"] == "样本量")["value"]
    assert dict_fact == "5000 cells"
    assert "{" not in dict_fact and "count" not in dict_fact and "unit_explanation" not in dict_fact
    assert dict_fact == flat_fact  # 两条入参形状 → 同一样本量口径


def test_introduction_preserves_bare_comparison_operators():
    """剥标签正则只剥"看起来像标签"的片段，不得吞掉科研描述里的裸比较符（FDR<0.01 / log2FC>1）。"""
    intro = build_dataset_introduction(
        {
            "dataset_name": "Stats",
            "source": "Example",
            "description": "DEGs at FDR<0.01 and log2FC>1 across <b>breast</b> tissue.",
        }
    )
    assert "FDR<0.01" in intro["summary"] and "log2FC>1" in intro["summary"]
    assert "<b>" not in intro["summary"] and "breast" in intro["summary"]  # 真标签仍被剥、正文保留


def test_browse_api_exposes_dates_and_year_facets_with_on_demand_introductions():
    body = _body(api_datasets())
    assert body["ok"] is True and body["count"] == len(body["records"])
    years = body["facets"]["published_year"]
    assert years and all(len(str(f["value"])) == 4 for f in years)
    assert sum(int(f["count"]) for f in years) + int(body["unknown_year_count"]) == body["count"]
    sample = body["records"][0]
    assert "published_date" in sample
    assert "description" not in sample and "introduction" not in sample

    intro = _body(
        api_introduction(
            uid=sample["dataset_uid"],
            url=sample["url"],
            name=sample["dataset_name"],
            source=sample["source"],
        )
    )
    assert intro["ok"] is True
    assert intro["introduction"]["summary"]


def test_frontend_intro_opens_dataset_page_and_browse_timeline_contract():
    html = (ROOT / "web/static/index.html").read_text(encoding="utf-8")
    cards = (ROOT / "web/static/js/search/cards.js").read_text(encoding="utf-8")
    browse = (ROOT / "web/static/js/search/browse.js").read_text(encoding="utf-8")
    interactions = (ROOT / "web/static/js/core/interactions.js").read_text(encoding="utf-8")
    core = (ROOT / "web/static/js/core/core.js").read_text(encoding="utf-8")
    css = (ROOT / "web/static/css/app.css").read_text(encoding="utf-8")
    for token in ('id="browseTimeline"', 'id="fYearFrom"', 'id="fYearTo"', 'id="timelineBars"'):
        assert token in html
    # 介绍模态弹窗退役——「数据集详情」（原「查看介绍」）改为 <a target=_blank> 打开独立标签页 /dataset；
    # 介绍**内容**渲染函数（renderIntroduction/loadIntroductionInto）仍在 cards.js、被详情页复用。
    assert "数据集详情" in cards and "/dataset?uid=" in cards and "escapeHtml(intro.summary" in cards
    assert "API.introduction" in cards and "loadIntroductionInto" in cards
    assert "function openIntroModal" not in cards, "openIntroModal 定义应已随介绍模态弹窗退役"
    assert "openIntroModal(it" not in cards, "openIntroModal 调用应已移除（改开新标签页）"
    assert "closeIntroModal(" not in interactions, "closeIntroModal 接线应已随介绍模态弹窗退役"
    assert "trapDialogFocus" in interactions and "activeFocusContainer" in interactions
    assert "MISSING_DISPLAY_VALUES" in core and "displayText(it.disease)" in core
    assert 'id="settings" role="dialog" aria-modal="true"' in html and 'aria-hidden="true" inert' in html
    # 前端 sampleFrom 与后端 units.format_sample_size 逐位一致（仅 count → "{count}"，不再加「未说明单位」）；
    # 卡脚不再把「未说明 {unit}」私删成「未说明」——卡脚 / 介绍关键事实 / 冻结 markdown 表三处同格式。
    assert 'if (count) return count;' in core
    assert 'startsWith("未说明 ")' not in core
    assert ".timeline-bars" in css and "overflow-y: hidden" in css
    assert "function validPublishedYear" in browse
    assert "(bs.yearFrom || bs.yearTo)" in browse
    assert "year === null" in browse  # 时间筛选启用时，未知日期不会混入
    # 单一真源：前端过滤+柱计数都读后端下发的 published_year（yearOf 兜底解析老响应）。
    assert "function yearOf(" in browse and "it.published_year" in browse
    # 时间线按当前筛选态实时重算（算年份分面时忽略年份筛选自身，避免选中某年后其它柱清零）。
    assert "ignoreYear" in browse and "function liveYearCounts(" in browse
