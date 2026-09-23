# -*- coding: utf-8 -*-
"""Web 与 MCP 的展示层投影必须是**同一个函数**，不是两份「据说同口径」的手抄。

## 这条测试守的是什么

 之前，`webapp._web_item_from_record` 与 `mcp_server._browse_item` 是两份手抄实现。
MCP 那份的 docstring 白纸黑字写着「webapp._web_item_from_record 的 MCP 侧同口径投影」——
而它缺 `modality` 和 `collection_doi`。后果实测：修好 Web 的 `/api/fair` 之后，
MCP 的 `assess_dataset_fair` **仍然** 5667/5667 印泛泛的 "dataset"、DOI 句永远印不出。
同一个 bug 只修了一半，而全套门禁（pytest / 冻结评测 / web smoke / mcp selfcheck）全绿。

这与「两份 denylist 手抄从未对账」一类事故同根因：**两份手抄的东西迟早漂移，且漂移时没人知道**。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from dataset_recommender.content import item_view  # noqa: E402


def test_web_and_mcp_share_one_implementation() -> None:
    """不是「输出恰好相等」，而是**同一个函数对象** —— 结构上无处漂移。"""
    from dataset_recommender.app import webapp

    from dataset_recommender.app import mcp_server

    assert webapp._web_item_from_record is item_view.build_item

    record = _fake_record()
    assert mcp_server._browse_item(record) == item_view.build_item(record)
    assert mcp_server._browse_item(record, include_introduction=True) == item_view.build_item(
        record, include_introduction=True
    )


def test_projection_carries_the_fields_fair_depends_on() -> None:
    """`fair` 需要而投影层曾经漏掉的三个字段 —— 漏一个就退化成编造或泛泛措辞。"""
    item = item_view.build_item(_fake_record())
    for key in ("modality", "collection_doi", "filesize", "dataset_uid", "source", "has_raw_data"):
        assert key in item, key
    assert item["modality"] == "single-cell"
    assert item["collection_doi"] == "10.1038/s41586-020-2157-4"


def test_published_year_single_source() -> None:
    assert item_view.published_year("2024-05-01") == 2024
    assert item_view.published_year("1999-01-01") == 1999
    assert item_view.published_year("") is None
    assert item_view.published_year("not-a-date") is None
    assert item_view.published_year("1899-01-01") is None      # 只认 19xx/20xx


def _fake_record():
    from types import SimpleNamespace

    return SimpleNamespace(
        dataset_name="Fake dataset",
        species="Human",
        tissue="Lung",
        disease="unknown",
        platform_family="chromium",
        assay="scRNA-seq",
        chemistry="v3",
        has_raw_data=False,
        count="1000",
        unit="Cells",
        url="https://cellxgene.cziscience.com/e/abc",
        description="Some description",
        family_id="fam-1",
        modality="single-cell",
        raw={
            "dataset_uid": "cxg:abc",
            "source": "CELLxGENE Discover",
            "collection_doi": "10.1038/s41586-020-2157-4",
            "filesize": 12345,
            "n_files": 0,
            "published_date": "2024-05-01",
        },
    )
