"""「查看全部文件」入口：/api/files 按需返回某数据集的全部真实文件直链。

钉死四点：
1) 已知 uid → 返回该数据集全部 files（条数与库内一致），主文件(primary)被标记且排在最前。
2) 缺失/未知 key → 空列表（降级，永不崩），与 downloads 模块合同一致。
3) 每条 file 携带前端渲染所需字段（title/filename/size_human/download_url/is_primary）。
4) 推荐结果 payload 带 dataset_uid + n_files，前端据此决定是否显示入口。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.corpus import downloads  # noqa: E402
from dataset_recommender.app.webapp import _rows_from_retrieved, api_files  # noqa: E402


def _pick_uid_with_many_files() -> tuple[str, int]:
    """从随包下载直链库里挑一个文件数 > 1 的 uid（避免硬编码、数据变动也不脆）。"""
    path = Path(downloads._DATA_PATH)
    data = json.loads(path.read_text(encoding="utf-8"))
    for uid, rec in data.items():
        files = [f for f in rec.get("files", []) if isinstance(f, dict) and f.get("download_url")]
        if len(files) > 1:
            return uid, len(files)
    raise AssertionError("下载直链库里没有任何文件数 > 1 的数据集")


def _body(resp) -> dict:
    return json.loads(resp.body.decode("utf-8"))


def test_api_files_returns_full_list_primary_first():
    uid, n = _pick_uid_with_many_files()
    body = _body(api_files(uid=uid, url=""))
    assert body["ok"] is True
    assert body["count"] == n  # 全部文件都在，不是只给 primary
    files = body["files"]
    # 恰有一个主文件，且排在最前
    primaries = [f for f in files if f["is_primary"]]
    assert len(primaries) == 1
    assert files[0]["is_primary"] is True
    # primary 的直链 == 卡片「下载数据」按钮那份
    assert files[0]["download_url"] == downloads.primary_url(uid)


def test_api_files_rows_have_render_fields():
    uid, _ = _pick_uid_with_many_files()
    f = _body(api_files(uid=uid, url=""))["files"][0]
    for field in ("title", "filename", "size_human", "download_url", "is_primary"):
        assert field in f
    assert f["download_url"].startswith("http")


def test_api_files_degrades_on_unknown_key():
    for bad in ("", "no-such-dataset-uid-xyz"):
        body = _body(api_files(uid=bad, url=""))
        assert body["ok"] is True
        assert body["count"] == 0
        assert body["files"] == []


def test_api_files_accepts_url_fallback_key():
    """uid 缺失时可用页面 url 作为回退键（downloads.get 支持 by_url）。"""
    uid, n = _pick_uid_with_many_files()
    rec = downloads.get(uid)
    page_url = rec.get("url")
    if not page_url:
        return  # 该记录无页面 url，跳过（不是所有记录都有）
    body = _body(api_files(uid="", url=page_url))
    assert body["count"] == n


def test_recommend_rows_carry_uid_and_nfiles():
    uid, n = _pick_uid_with_many_files()
    # 模拟检索器结构化候选（_serialize_retrieved_data 的形态子集）
    payload = [{"dataset_name": "X", "url": "", "dataset_uid": uid, "n_files": n}]
    row = _rows_from_retrieved(payload)[0]
    assert row["dataset_uid"] == uid
    assert row["n_files"] == n


def test_file_count_matches_library():
    uid, n = _pick_uid_with_many_files()
    assert downloads.file_count(uid) == n
    assert downloads.file_count("no-such-uid") == 0


# ---- 活台账露出：/api/files 追加 problem / problem_reason / last_verified（additive）----
def test_api_files_carries_inspection_fields():
    uid, _ = _pick_uid_with_many_files()
    f = _body(api_files(uid=uid, url=""))["files"][0]
    for field in ("problem", "problem_reason", "last_verified"):
        assert field in f
    assert isinstance(f["problem"], bool)


def test_api_files_marks_problem_file():
    """有问题文件的数据集：problem=True 的条数与活台账 np 一致，且每条带 reason。"""
    import json as _json
    from pathlib import Path as _P
    import pytest
    from dataset_recommender.corpus import inspection
    try:
        cur = _json.loads(_P(inspection._DATA_PATH).read_text(encoding="utf-8"))
    except Exception:
        pytest.skip("活台账不可用")
    entry = next(((u, r) for u, r in cur.get("by_uid", {}).items() if r.get("np", 0) > 0), None)
    if not entry:
        pytest.skip("活台账无问题数据集")
    uid, r = entry
    files = _body(api_files(uid=uid, url=""))["files"]
    probs = [f for f in files if f["problem"]]
    assert len(probs) == r["np"]
    assert all(p["problem_reason"] for p in probs)
