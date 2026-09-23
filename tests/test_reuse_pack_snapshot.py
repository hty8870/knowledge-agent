# -*- coding: utf-8 -*-
"""N9 快照可复现：复用出处清单带**确定性语料快照 + 检索日期 + 条数**，让重跑可对比、可复现。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.corpus.corpus import corpus_snapshot, load_normalized_corpus  # noqa: E402
from dataset_recommender.llm.config import get_settings  # noqa: E402
from dataset_recommender.content.reuse_pack import (  # noqa: E402
    build_reuse_pack,
    build_pack_for_uids,
    to_markdown,
)


def _base_corpus():
    s = get_settings()
    return load_normalized_corpus(s.data_dir, s.project_root)  # sources=None → 仅 base


def test_corpus_snapshot_deterministic_and_order_independent():
    recs = [{"dataset_uid": "a", "source": "X"}, {"dataset_uid": "b", "source": "Y"}]
    s1 = corpus_snapshot(recs)
    s2 = corpus_snapshot(list(reversed(recs)))
    assert s1["snapshot_id"] == s2["snapshot_id"]      # 顺序无关（先排序再哈希）
    assert s1["n_records"] == 2 and len(s1["snapshot_id"]) == 12


def test_corpus_snapshot_changes_on_add_or_remove():
    recs = [{"dataset_uid": "a", "source": "X"}]
    s1 = corpus_snapshot(recs)
    s2 = corpus_snapshot(recs + [{"dataset_uid": "c", "source": "X"}])
    assert s1["snapshot_id"] != s2["snapshot_id"]      # 增删即变


def test_reuse_pack_retrieval_none_without_snapshot():
    """向后兼容：直接 build_reuse_pack 不给快照 → retrieval 为 None。"""
    pack = build_reuse_pack([], [])
    assert pack["retrieval"] is None


def test_reuse_pack_retrieval_present_with_snapshot():
    snap = {"snapshot_id": "abc123def456", "n_records": 5667, "sources": {"10x Genomics": 767}}
    pack = build_reuse_pack([], [], snapshot=snap, retrieval_date="2026-07-18")
    r = pack["retrieval"]
    assert r["snapshot_id"] == "abc123def456"
    assert r["date"] == "2026-07-18"
    assert r["n_corpus_records"] == 5667
    assert "abc123def456" in r["note_en"] and "2026-07-18" in r["note_en"]
    assert "abc123def456" in r["note_zh"]


def test_build_pack_for_uids_stamps_snapshot_date_count():
    corpus = _base_corpus()
    uid = str(corpus[0].raw.get("dataset_uid"))
    pack = build_pack_for_uids([uid], corpus, today="2026-07-18")
    r = pack["retrieval"]
    assert r and r["date"] == "2026-07-18"
    expected_n = len([c for c in corpus if str((c.raw or {}).get("dataset_uid") or "").strip()])
    assert r["n_corpus_records"] == expected_n
    assert len(r["snapshot_id"]) == 12
    # markdown：note_en 可见（Methods 复现句）、note_zh 作为注释保留
    md = to_markdown(pack)
    assert r["note_en"] in md
    assert r["note_zh"] in md
    assert "复现信息（不随稿件提交）" in md


def test_pack_for_uids_snapshot_matches_corpus_snapshot():
    """pack 里的 snapshot_id 必须等于对被检索语料直接算的快照——同源、可核。"""
    corpus = _base_corpus()
    uid = str(corpus[0].raw.get("dataset_uid"))
    pack = build_pack_for_uids([uid], corpus, today="2026-07-18")
    assert pack["retrieval"]["snapshot_id"] == corpus_snapshot(corpus)["snapshot_id"]
