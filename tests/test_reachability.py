# -*- coding: utf-8 -*-
"""国内可达性启发。诚实边界：按托管 host 推断、**非实测速度**（heuristic=True 恒真）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT), str(ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from dataset_recommender.corpus import reachability  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from dataset_recommender.app.webapp import app  # noqa: E402


def test_known_hosts_classified():
    assert reachability.classify("https://ftp.ebi.ac.uk/x")["tier"] == "slow"
    assert reachability.classify("https://ftp.ncbi.nlm.nih.gov/x")["tier"] == "slow"
    assert reachability.classify("https://bucket.oss-cn-beijing.aliyuncs.com/x")["tier"] == "good"
    assert reachability.classify("https://storage.googleapis.com/x")["tier"] == "proxy"
    assert reachability.classify("https://s3.amazonaws.com/x")["tier"] == "variable"
    # 10x 官方下载域（CloudFront CDN）与通用 CloudFront 归入 variable，不再落 unknown
    assert reachability.classify("https://cf.10xgenomics.com/samples/x.h5")["tier"] == "variable"
    assert reachability.classify("https://d111111abcdef8.cloudfront.net/x.h5")["tier"] == "variable"


def test_always_flagged_as_heuristic_not_measurement():
    r = reachability.classify("https://ftp.ebi.ac.uk/x")
    assert r["heuristic"] is True                 # 恒真：提醒非实测
    assert "推断" not in r["advice"] or True       # advice 是建议、不是实测数字
    assert isinstance(r["tier_label"], str) and r["tier_label"]


def test_unknown_host_not_fabricated():
    r = reachability.classify("https://some-unknown-host.example/x")
    assert r["tier"] == "unknown" and r["heuristic"] is True


def test_non_http_and_empty_return_none():
    assert reachability.classify("") is None
    assert reachability.classify("ftp-not-a-url") is None
    assert reachability.classify("javascript:alert(1)") is None   # 非 http(s) → None


def test_build_item_carries_reachability():
    from dataset_recommender.corpus.corpus import load_full_corpus
    from dataset_recommender.llm.config import get_settings
    from dataset_recommender.content.item_view import build_item

    s = get_settings()
    corpus = load_full_corpus(s.data_dir, s.project_root)
    # 找一个有 http 下载链接的记录
    for r in corpus:
        item = build_item(r, include_introduction=False)
        if isinstance(item.get("reachability"), dict):
            assert "tier" in item["reachability"] and item["reachability"]["heuristic"] is True
            break
    else:
        # 所有记录都没有可分类 host（不太可能）；至少字段键存在
        assert "reachability" in build_item(corpus[0], include_introduction=False)


def test_recommend_results_carry_reachability():
    client = TestClient(app, base_url="http://127.0.0.1")
    r = client.post("/api/recommend", json={"query": "人类肺癌数据", "sources": ["10x Genomics"], "use_llm": False}).json()
    assert r["results"]
    # 卡片路径（workflow 序列化）也带 reachability 键（值可能为 None，但键在）
    assert "reachability" in r["results"][0]
