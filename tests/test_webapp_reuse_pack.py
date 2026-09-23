# -*- coding: utf-8 -*-
"""`POST /api/reuse-pack` 的 HTTP 契约 + 两条不可回退的设计约束。

约束一：**必须是 POST，不能是 GET**。GET 会把用户勾选的每个 dataset_uid 打进 uvicorn 的
access log —— 那是**事实上的埋点**，而「要不要做使用数据采集」是用户明确保留给自己的
战略决定。这条测试就是那个决定的守卫。

约束二：**keys-only**。只收 dataset_uid、不收数据集内容。一旦开了「把数据集描述贴进来」
的口子，产品就有了吃进未发表工作的路径。入参是键不是内容 → IP 红线是结构性的。
"""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dataset_recommender.app.webapp import app  # noqa: E402

# base_url 必须是 loopback：`_require_loopback_host` 中间件会把 TestClient 默认的
# `http://testserver` 当 DNS-rebinding 挡掉（403）。这是既有安全约束，不是本轮引入。
client = TestClient(app, base_url="http://127.0.0.1")


def _a_real_uid() -> str:
    from dataset_recommender.llm.config import get_settings
    from dataset_recommender.corpus.corpus import load_full_corpus

    s = get_settings()
    for record in load_full_corpus(s.data_dir, s.project_root):
        raw = record.raw if isinstance(record.raw, dict) else {}
        uid = str(raw.get("dataset_uid") or "")
        if uid:
            return uid
    raise AssertionError("语料里一条带 uid 的记录都没有")


def test_get_is_not_allowed_so_uids_never_reach_the_access_log() -> None:
    """GET 必须 405 —— 不是风格问题，是「不许把用户行为落进日志」的硬约束。"""
    res = client.get("/api/reuse-pack", params={"uids": "cxg:x"})
    assert res.status_code == 405


def test_post_returns_pack_and_markdown() -> None:
    uid = _a_real_uid()
    res = client.post("/api/reuse-pack", json={"uids": [uid]})
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    pack = body["pack"]
    assert pack["n_datasets"] == 1
    assert "boundary" not in pack   # ：置顶那段体裁边界陈述已整段删除（见 test_reuse_pack 同名断言）
    assert pack["paragraph"].startswith("This study reuses 1 publicly available dataset")
    assert len(pack["table"]) == 1
    assert pack["unresolved"] == []
    assert "Reused public datasets" in body["markdown"]


def test_unknown_uid_is_tombstoned_not_404() -> None:
    """查不到 ≠ 整个请求失败：其余数据集照常出，缺的那个显式喊出来。"""
    uid = _a_real_uid()
    res = client.post("/api/reuse-pack", json={"uids": [uid, "definitely-not-a-uid"]})
    assert res.status_code == 200
    pack = res.json()["pack"]
    assert pack["n_datasets"] == 1
    assert pack["unresolved"] == ["definitely-not-a-uid"]
    assert "未纳入本清单" in pack["gaps"][0]


def test_bad_payloads_are_400_not_500() -> None:
    for payload in ({}, {"uids": []}, {"uids": "cxg:x"}, {"uids": [1]}, {"uids": None}, []):
        res = client.post("/api/reuse-pack", json=payload)
        assert res.status_code == 400, payload


def test_non_json_body_is_400() -> None:
    res = client.post("/api/reuse-pack", content=b"not json", headers={"Content-Type": "application/json"})
    assert res.status_code == 400


def test_english_paragraph_carries_no_internal_prefix() -> None:
    """端到端复核反编造：真实语料 → HTTP → 段落里不许有内部主键。"""
    from dataset_recommender.llm.config import get_settings
    from dataset_recommender.corpus.corpus import load_full_corpus

    s = get_settings()
    uids = []
    seen = set()
    for record in load_full_corpus(s.data_dir, s.project_root):
        raw = record.raw if isinstance(record.raw, dict) else {}
        src = str(raw.get("source") or "10x Genomics")
        if src not in seen:
            seen.add(src)
            uids.append(str(raw.get("dataset_uid") or ""))
    res = client.post("/api/reuse-pack", json={"uids": uids})
    assert res.status_code == 200
    body = res.json()
    para = body["pack"]["paragraph"]
    for bad in ("cxg:", "ae:", "hca:", "ebi:"):
        assert bad not in para, bad
    assert not [c for c in para if "一" <= c <= "鿿"], para
    # 补充表（注释之前的部分）同样不许漏内部前缀
    table_part = body["markdown"].split("<!--")[0]
    for bad in ("cxg:", "ae:", "hca:", "ebi:"):
        assert bad not in table_part, bad
