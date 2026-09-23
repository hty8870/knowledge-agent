# -*- coding: utf-8 -*-
"""遥测导出管线端到端测试（含 join 语义回归）。

用 SQLite 文件库造数（沿用接收端 fixture 模式：注入 app 模块建表 → 直接插入
ingest_packets + ingest_packet_receipts），跑通 scripts/telemetry_export.py 与
scripts/telemetry_delete.py 的完整链路：产物形状、隐私遮蔽与 quarantine、
--accepted 过滤、--incremental 增量续跑幂等、--yes 删除与 dry-run。

 新语义（本文件断言的契约）：
- impressions 主来源 = usage kind:"imp" 事件与自带 iid 的 search 事件（跨包精确键
  (install_id,sid,tid,iid)）；kind:"view" 事件补 seen/dwell_ms；无 iid 的旧 search
  事件合成 iid 并标 join_status:"legacy"。benchfb 记录**不再**制造 impression。
- 交互事件 uid 优先取事件自带值、pos 只作校验（不符计 pos_mismatch）；自带 iid 的
  跨包精确 join（join_status:"ok"）；join 不上标 "orphan" 但自带 iid/tid/uid 仍透传；
  无 iid 的旧事件走同 install 时间近邻并标 "legacy_inferred"。
- explicit_labels 双源合并（键 = (install_id,tid,recId)）：kind:"label" 事件
  （载荷带 recId，同键多 rev 取最高 rev）优先于 benchfb 内嵌 rating（键 = 记录内 tid + record id，
  同键去重，label_source 区分）；老数据缺 recId/tid 空串兜底、与对侧不匹配属预期降级。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

FAKE_API_KEY = "sk-" + "abcdefghijklmnopqrstuvwxyz0123"

ROOT = Path(__file__).resolve().parents[1]
_SERVICE_DIR = ROOT / "services" / "telemetry-receiver"
if str(_SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVICE_DIR))

os.environ.setdefault("INGEST_TOKEN", "test-token")
os.environ.setdefault("STATS_TOKEN", "test-stats-token")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

from app import Settings, create_app, ingest_packets  # noqa: E402
from telemetry_idempotency import packet_receipts  # noqa: E402


def _load_script(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


export = _load_script("telemetry_export", "scripts/telemetry_export.py")
delete = _load_script("telemetry_delete", "scripts/telemetry_delete.py")


def _imp_id(install_id: str, event_id: str) -> str:
    return "imp-" + hashlib.sha256(f"{install_id}|{event_id}".encode("utf-8")).hexdigest()[:16]


@pytest.fixture()
def db(tmp_path):
    """SQLite 文件库（create_app 建全量表）；返回 (db 路径, engine)。"""
    path = tmp_path / "telemetry.db"
    app = create_app(Settings(ingest_token="t", database_url=f"sqlite:///{path}"))
    return path, app.state.engine


def _insert(engine, payload, *, received_at=None):
    """插入一个已完整落库的主包 + 对应 receipt（模拟接收端成功路径）。"""
    with engine.begin() as conn:
        r = conn.execute(ingest_packets.insert().values(
            install_id=payload["install_id"],
            schema=payload.get("schema", "biodata-telemetry/1"),
            n_usage=len(payload.get("usage_events") or []),
            n_benchfb=len(payload.get("benchfb_records") or []),
            payload=payload,
            received_at=received_at or datetime.now(timezone.utc),
        ))
        pid = int(r.inserted_primary_key[0])
        packet_id = payload.get("packet_id") or f"pkt-{pid}"
        conn.execute(packet_receipts.insert().values(
            packet_id=packet_id, identity="profile-test", row_id=pid))
    return pid


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_export_tolerates_scalar_nested_benchfb_fields(db, tmp_path):
    """接收端允许的旧版/畸形嵌套标量不得让整库导出因 `.get` 崩溃。"""
    path, engine = db
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-scalar-nested",
        "install_id": "inst-scalar", "usage_events": [], "mcp_records": [],
        "benchfb_records": [{
            "id": "r-scalar", "kind": "search", "t": 1,
            "rating": 1, "route": 2, "search": 3, "env": 4, "action": 5,
        }],
    })
    out = tmp_path / "scalar-export"
    assert export.main(["--dsn", str(path), "--out", str(out)]) == 0
    assert _read_jsonl(out / "turns.jsonl")[0]["rating"] == 1
    candidate = _read_jsonl(out / "benchmark_candidates.jsonl")[0]
    assert candidate["rating"]["stars"] is None
    assert candidate["effective_query"] == "" and candidate["action_verb"] == ""


def _pkt_v3_imp(ts: int) -> dict:
    """v3 包 A：imp 曝光 + view 追踪 + benchfb 轮次（检索轮 old rating / 工具轮 new rating）+ mcp。"""
    return {
        "schema": "biodata-telemetry/1",
        "packet_id": "pkt-v3-imp",
        "install_id": "inst-e2e",
        "client_id": "client-e2e-0001",
        "profile_id": "profile-e2e-0001",
        "exported_at": "2026-08-20T08:00:00Z",
        "usage_events": [
            {"event_id": "u-imp1", "t": ts, "k": "imp", "sid": "s-1", "tid": "t-1", "iid": "i-1",
             "q": "10x 空间转录组", "policy": "auto/llm/cross_encoder@20260820-01",
             "items": [{"uid": "d1001", "pos": 1, "score": 0.91},
                       {"uid": "d1002", "pos": 2, "score": 0.87},
                       {"uid": "d1003", "pos": 3}]},
            {"event_id": "u-view1", "t": ts + 3000, "k": "view", "sid": "s-1", "tid": "t-1", "iid": "i-1",
             "seen": [1, 2], "dwell_ms": 4500},
        ],
        "benchfb_records": [
            {
                "id": "r-search-1", "tid": "t-1", "kind": "search", "t": ts, "q": "10x 空间转录组",
                # 当前客户端实际会写入这些轮次字段；导出器应识别它们而非报 schema 漂移。
                "src": "hero", "conv": "conv-1", "end": ts + 850, "route_ms": 120, "truncated": False,
                "route": {"route": "search", "via": "rules", "query": "10x 空间转录组",
                          "plan": {"trace": [{"id": "route", "status": "used", "ok": True, "ms": 12,
                                                "detail": "联系方式 13800138000"}]}},
                "search": {"req": {"query": "10x 空间转录组"}, "ms": 320,
                           "res": {"resolution_status": "resolved", "result_total": 42,
                                   "search_trace": {"steps": [{"id": "lexical", "status": "used", "duration_ms": 8}]},
                                   "results": [
                                       {"dataset_uid": "d1001", "dataset_name": "数据集A", "source": "10x"},
                                       {"dataset_uid": "d1002", "dataset_name": "数据集B", "source": "10x"},
                                       {"dataset_uid": "d1003", "dataset_name": "数据集C", "source": "10x"},
                                   ]}},
                "rating": {"stars": 4, "useful_idx": [1, 2], "comment": "前两个很有用，联系方式 13800138000"},
                "env": {"model": "m1", "provider": "p1", "endpoint_host": "api.example.com"},
                "ms": 850, "err": "",
            },
            {
                "id": "r-tool-1", "tid": "t-tool", "kind": "tool", "t": ts + 20000, "q": "下载 d1001",
                "rating": {"completion": "partial", "reasons": ["源站无直链"], "useful_idx": [], "comment": ""},
                "action": {"verb": "download", "cancelled": False,
                           "trace": [{"verb": "download", "state": "done", "ok": True}]},
                "env": {"model": "m1"},
            },
        ],
        "mcp_records": [
            {"call_id": "mcp-1", "ts": ts + 30000, "tool": "recommend_datasets", "args": {"query": "10x"}, "ok": True, "ms": 120},
            {"call_id": "mcp-2", "ts": ts + 31000, "tool": "get_file_manifest", "ok": False, "error": "uid missing"},
        ],
    }


def _pkt_v3_act(ts: int) -> dict:
    """v3 包 B：交互事件（跨包 join 包 A 的曝光）+ label 事件（多 rev 合并 / 与 benchfb 双源去重）。"""
    return {
        "schema": "biodata-telemetry/1",
        "packet_id": "pkt-v3-act",
        "install_id": "inst-e2e",
        "client_id": "client-e2e-0001",
        "profile_id": "profile-e2e-0001",
        "exported_at": "2026-08-20T08:05:00Z",
        "usage_events": [
            # 自带 uid+pos：uid 优先、pos 与曝光 items 一致 → 不计 mismatch
            {"event_id": "u-o1", "t": ts + 5000, "k": "open", "sid": "s-1", "tid": "t-1", "iid": "i-1",
             "uid": "d1002", "pos": 2},
            # 不带 uid：按 pos 从曝光 items 解析；dl what=cite → cite
            {"event_id": "u-d1", "t": ts + 6000, "k": "dl", "what": "cite",
             "sid": "s-1", "tid": "t-1", "iid": "i-1", "pos": 1},
            # 自带 uid 与 pos 对不上（pos=1 是 d1001，事件说 d1003）→ pos_mismatch，uid 仍取事件值
            {"event_id": "u-f1", "t": ts + 7000, "k": "fav", "sid": "s-1", "tid": "t-1", "iid": "i-1",
             "uid": "d1003", "pos": 1},
            # open what=files → view 交互（与 k:"view" 曝光追踪是两回事）
            {"event_id": "u-v1", "t": ts + 8000, "k": "open", "what": "files",
             "sid": "s-1", "tid": "t-1", "iid": "i-1", "pos": 1},
            # join 不上的自描述事件 → orphan，但自带 iid/tid/uid 全部透传
            {"event_id": "u-o9", "t": ts + 9000, "k": "open", "sid": "s-1", "tid": "t-9", "iid": "i-9",
             "uid": "d9999", "pos": 1},
            # 找不到对应曝光的 view 事件 → views_orphan
            {"event_id": "u-view9", "t": ts + 9500, "k": "view", "sid": "s-1", "tid": "t-9", "iid": "i-9",
             "seen": [1], "dwell_ms": 100, "mystery_key": 1},   # mystery_key：schema 漂移统计
            # label 事件（带 recId）：同 (tid,recId) 两个 rev，rev 2 胜出；
            # recId=r-search-1 与 benchfb 记录 r-search-1 同键 → 双源去重，label 事件优先
            {"event_id": "u-l1", "t": ts + 10000, "k": "label", "tid": "t-1", "recId": "r-search-1", "rev": 1,
             "completion": "done", "reasons": ["数据全"], "useful_uids": ["d1001"], "useful_idx": [1],
             "comment": "第一版评语"},
            {"event_id": "u-l2", "t": ts + 11000, "k": "label", "tid": "t-1", "recId": "r-search-1", "rev": 2,
             "completion": "partial", "reasons": ["源站无直链"], "useful_uids": ["d1002"], "useful_idx": [2],
             "comment": "改口后的评语"},
            # 老数据无 recId 的 label 事件：键 recId=""，与带 recId 的事件/benchfb 记录不合并（降级）
            {"event_id": "u-l3", "t": ts + 12000, "k": "label", "tid": "t-1", "rev": 1,
             "completion": "done", "reasons": ["数据全"], "useful_uids": ["d1001"], "useful_idx": [1],
             "comment": "老版评语"},
            # 同轮次另一条记录（记录已上传删除、台账续评）：同 tid 不同 recId → 不互相吞并（1c）
            {"event_id": "u-l4", "t": ts + 13000, "k": "label", "tid": "t-1", "recId": "r-gone", "rev": 1,
             "completion": "partial", "reasons": ["执行没完成"], "useful_uids": [], "useful_idx": [],
             "comment": "续评"},
        ],
        "benchfb_records": [], "mcp_records": [],
    }


def _pkt_legacy(ts: int) -> dict:
    """旧数据包：无 iid 的 search 事件（v2 有 items）+ 无 iid 的交互 → legacy / legacy_inferred。"""
    return {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-legacy",
        "install_id": "inst-legacy", "client_id": "", "profile_id": "",
        "exported_at": "2026-08-20T09:00:00Z",
        "usage_events": [
            {"event_id": "u-s-old", "t": ts + 3600000, "k": "search", "q": "旧数据",
             "items": [{"uid": "dL1", "pos": 1}]},
            {"event_id": "u-o-old", "t": ts + 3605000, "k": "open", "r": 1},
            {"event_id": "u-o-old2", "t": ts + 3606000, "k": "open", "r": 5},   # pos 超范围：join 上但 uid 解析不出
        ],
        "benchfb_records": [], "mcp_records": [],
    }


def test_export_end_to_end(db, tmp_path):
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, _pkt_v3_imp(ts))
    _insert(engine, _pkt_v3_act(ts))
    _insert(engine, _pkt_legacy(ts))

    # 孤儿包：只有交互、同 install 无任何曝光 → join_status orphan
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-orphan",
        "install_id": "inst-orphan", "client_id": "client-orphan-01", "profile_id": "profile-orphan-01",
        "exported_at": "2026-08-20T08:10:00Z",
        "usage_events": [{"event_id": "u-orphan", "t": ts + 60000, "k": "open", "r": 1}],
        "benchfb_records": [], "mcp_records": [],
    })
    # 敏感包：身份证进查询 → quarantine；整条为手机号的 query → quarantine
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-idcard",
        "install_id": "inst-bad", "exported_at": "2026-08-20T10:00:00Z",
        "usage_events": [],
        "benchfb_records": [{"id": "r-idcard", "kind": "search", "t": ts + 7200000, "q": "11010119900307857X 的样本"}],
        "mcp_records": [],
    })
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-phone",
        "install_id": "inst-bad2", "exported_at": "2026-08-20T11:00:00Z",
        "usage_events": [{"event_id": "u-phone", "t": ts + 10800000, "k": "search", "q": "13800138000"}],
        "benchfb_records": [], "mcp_records": [],
    })

    out = tmp_path / "export"
    rc = export.main(["--dsn", str(path), "--out", str(out)])
    assert rc == 0

    # --- impressions：主来源 = imp 事件；benchfb 不再制造曝光；旧 search 合成 legacy ---
    impressions = _read_jsonl(out / "impressions.jsonl")
    assert len(impressions) == 2                    # imp(i-1) + legacy(u-s-old)；benchfb 检索轮不再产出
    imp = next(i for i in impressions if i["iid"] == "i-1")
    assert imp["join_status"] == "ok"
    assert imp["tid"] == "t-1" and imp["sid"] == "s-1" and imp["profile"] == "profile-e2e-0001"
    assert imp["q"] == "10x 空间转录组" and imp["policy"] == "auto/llm/cross_encoder@20260820-01"
    assert [it["uid"] for it in imp["items"]] == ["d1001", "d1002", "d1003"]
    assert [it["pos"] for it in imp["items"]] == [1, 2, 3]
    assert imp["seen"] == [1, 2] and imp["dwell_ms"] == 4500          # view 事件补的
    assert not any(i["tid"] == "r-search-1" for i in impressions)     # benchfb 只作 turn/label 来源
    legacy_imp = next(i for i in impressions if i["install_id"] == "inst-legacy")
    assert legacy_imp["join_status"] == "legacy"
    assert legacy_imp["iid"] == _imp_id("inst-legacy", "u-s-old")     # 合成 iid 稳定
    assert legacy_imp["q"] == "旧数据" and legacy_imp["items"][0]["uid"] == "dL1"

    # --- interactions：跨包精确 join / 自带 uid 优先 / pos 校验 / legacy_inferred / orphan ---
    interactions = _read_jsonl(out / "interactions.jsonl")
    assert len(interactions) == 8
    by_eid = {i["event_id"]: i for i in interactions}
    o1 = by_eid["u-o1"]
    assert o1["join_status"] == "ok" and o1["iid"] == "i-1" and o1["tid"] == "t-1"
    assert o1["uid"] == "d1002" and o1["pos"] == 2                    # 事件自带 uid
    cite = by_eid["u-d1"]
    assert cite["type"] == "cite" and cite["uid"] == "d1001"          # pos 从曝光 items 解析
    fav = by_eid["u-f1"]
    assert fav["uid"] == "d1003" and fav["join_status"] == "ok"       # pos 校验不符仍取事件自带 uid
    view_int = by_eid["u-v1"]
    assert view_int["type"] == "view" and view_int["uid"] == "d1001"  # open what=files → view
    orphan_v3 = by_eid["u-o9"]
    assert orphan_v3["join_status"] == "orphan"
    assert orphan_v3["iid"] == "i-9" and orphan_v3["tid"] == "t-9" and orphan_v3["uid"] == "d9999"  # 透传
    old1 = by_eid["u-o-old"]
    assert old1["join_status"] == "legacy_inferred" and old1["uid"] == "dL1"
    assert old1["iid"] == legacy_imp["iid"]
    old2 = by_eid["u-o-old2"]
    assert old2["join_status"] == "legacy_inferred" and old2["uid"] is None and old2["uid_ok"] is False
    orphan = by_eid["u-orphan"]
    assert orphan["join_status"] == "orphan" and orphan["iid"] is None and orphan["uid"] is None

    # --- turns（benchfb 轮次，遮蔽后全量；记录内 tid 透出为轮次 id，record id 仍在 id 字段）---
    turns = _read_jsonl(out / "turns.jsonl")
    assert {t["tid"] for t in turns} == {"t-1", "t-tool"}            # r-idcard 在 quarantine
    search_turn = next(t for t in turns if t["tid"] == "t-1")
    assert search_turn["id"] == "r-search-1"
    assert search_turn["iid"] is None
    assert search_turn["rating"]["comment"] == "前两个很有用，联系方式 [手机号]"   # 夹在文本里 → 遮蔽后放行

    # --- explicit_labels（合并键 = (install_id,tid,recId)，双源去重 + 多 rev + 同轮不吞并）---
    labels = _read_jsonl(out / "explicit_labels.jsonl")
    by_key = {(l["tid"], l.get("recId")): l for l in labels}
    assert set(by_key) == {("t-1", "r-search-1"), ("t-1", None), ("t-1", "r-gone"), ("t-tool", "r-tool-1")}
    rs1 = by_key[("t-1", "r-search-1")]                               # 同键双源去重 → 事件行，多 rev 取最高
    assert rs1["label_source"] == "event" and rs1["rev"] == 2
    assert rs1["completion"] == "partial" and rs1["reasons"] == ["源站无直链"]
    assert rs1["useful_uids"] == ["d1002"] and rs1["useful_idx"] == [2]
    assert rs1["comment"] == "改口后的评语"
    old3 = by_key[("t-1", None)]                                      # 老数据无 recId：独立成行（降级不匹配）
    assert old3["label_source"] == "event" and old3["rev"] == 1 and old3["recId"] is None
    gone = by_key[("t-1", "r-gone")]                                  # 1c：同轮次另一条记录不被吞
    assert gone["label_source"] == "event" and gone["rev"] == 1 and gone["recId"] == "r-gone"
    rt1 = by_key[("t-tool", "r-tool-1")]                              # benchfb 兜底源
    assert rt1["label_source"] == "benchfb" and rt1["label_shape"] == "new"
    assert rt1["completion"] == "partial" and rt1["reasons"] == ["源站无直链"]

    # --- mcp_calls 摊平 ---
    mcp = _read_jsonl(out / "mcp_calls.jsonl")
    assert {m["call_id"] for m in mcp} == {"mcp-1", "mcp-2"}
    assert mcp[0]["tool"] == "recommend_datasets" and mcp[0]["install_id"] == "inst-e2e"

    # --- benchmark_candidates（兼容 benchfb_ingest 字段；iid 不再合成、置 null）---
    cands = _read_jsonl(out / "benchmark_candidates.jsonl")
    by_rid = {c["record_id"]: c for c in cands}
    assert set(by_rid) == {"r-search-1", "r-tool-1"}
    c1 = by_rid["r-search-1"]
    assert c1["query"] == "10x 空间转录组" and c1["route"] == "search"
    assert c1["system_topk_uids"] == ["d1001", "d1002", "d1003"]
    assert c1["rating"]["stars"] == 4 and c1["rating"]["useful_uids"] == ["d1001", "d1002"]
    assert c1["rating"]["completion"] is None and c1["rating"]["reasons"] == []
    assert c1["tid"] == "t-1" and c1["iid"] == "i-1"               # 仅 exact tid 的已有曝光可回填
    assert c1["record_id"] == "r-search-1" and c1["route"] == "search"
    assert c1["policy"] == "auto/llm/cross_encoder@20260820-01"      # route 不能再冒充 policy
    assert by_rid["r-tool-1"]["tid"] == "t-tool"
    assert by_rid["r-tool-1"]["iid"] is None and by_rid["r-tool-1"]["policy"] is None
    assert by_rid["r-tool-1"]["rating"]["completion"] == "partial"
    assert by_rid["r-tool-1"]["rating"]["reasons"] == ["源站无直链"]

    # --- agent trajectories：只复用已采集且已遮蔽的真 step，带训练授权/模型/结果与可校验摘要 ---
    trajectories = {row["record_id"]: row for row in _read_jsonl(out / "agent_trajectories.jsonl")}
    assert set(trajectories) == {"r-search-1", "r-tool-1"}
    search_traj = trajectories["r-search-1"]
    assert search_traj["iid"] == "i-1" and search_traj["step_count"] == 2
    assert [step["source"] for step in search_traj["steps"]] == ["route.plan.trace", "search.trace"]
    assert search_traj["steps"][0]["payload"]["detail"] == "联系方式 [手机号]"
    assert len(search_traj["steps"][0]["sha256"]) == 64
    assert search_traj["model"] == "m1" and search_traj["training_consent"] is False
    tool_traj = trajectories["r-tool-1"]
    assert tool_traj["steps"][0]["source"] == "action.trace"

    # --- quarantine：敏感/无法处理不进正常产物 ---
    quarantined = _read_jsonl(out / "quarantine.jsonl")
    reasons = {q["reason"] for q in quarantined}
    assert "idcard" in reasons and "phone-only" in reasons
    assert len(quarantined) == 2
    assert "r-idcard" not in by_rid and "r-idcard" not in {t["tid"] for t in turns}
    # 原始 PII 不出现在任何产物（quarantine 样本也是遮蔽后）
    raw_phone, raw_idcard = "13800138000", "11010119900307857X"
    for fname in ("impressions.jsonl", "interactions.jsonl", "turns.jsonl", "explicit_labels.jsonl",
                  "mcp_calls.jsonl", "benchmark_candidates.jsonl", "agent_trajectories.jsonl",
                  "quality_report.md", "review.html",
                  "quarantine.jsonl"):
        text = (out / fname).read_text(encoding="utf-8")
        assert raw_phone not in text and raw_idcard not in text, f"{fname} 泄漏原始 PII"

    # --- quality_report ---
    report = (out / "quality_report.md").read_text(encoding="utf-8")
    for marker in ("关联完整率", "可标注率", "重复率", "上传延迟分布", "schema 漂移", "mcp 调用统计"):
        assert marker in report
    assert "mystery_key" in report  # schema 漂移统计里出现未知键
    assert "13800138000" not in report

    # --- 汇总计数（直接调 _export 拿 report dict 断言）---
    summary = export._export(export._make_engine(str(path)), tmp_path / "export2",
                             since=None, until=None, incremental=False, accepted_ids=None)
    rep = summary["report"]
    assert rep["interactions_total"] == 8
    assert rep["interactions_joined"] == 4            # u-o1/u-d1/u-f1/u-v1
    assert rep["interactions_legacy_inferred"] == 2   # u-o-old/u-o-old2
    assert rep["interactions_orphan"] == 2            # u-o9/u-orphan
    assert rep["interactions_pos_mismatch"] == 1      # u-f1
    assert rep["interactions_uid_ok"] == 6            # 除 u-o-old2、u-orphan 外都有 uid
    assert rep["impressions_total"] == 2
    assert rep["impressions_from_imp"] == 1 and rep["impressions_legacy"] == 1
    assert rep["views_total"] == 2 and rep["views_joined"] == 1 and rep["views_orphan"] == 1
    assert rep["labels_total"] == 4
    assert rep["labels_from_events"] == 3 and rep["labels_from_benchfb"] == 1
    assert rep["labels_benchfb_deduped"] == 1
    assert "benchfb_records" not in rep["unknown_keys"]              # src/conv/end/route_ms/truncated 都是已知真实字段

    # --- review.html ---
    review = (out / "review.html").read_text(encoding="utf-8")
    assert "遥测 benchmark 审阅" in review
    assert "tid t-1" in review and "tid t-tool" in review and "[手机号]" in review


def test_export_label_keys_dedup_and_old_data_degrade(db, tmp_path):
    """：显式标注双源合并键 = (install_id, tid, recId)。

    - 同一轮次两条记录 + 各带 label 事件（同 tid 不同 recId）→ 各自保留、各自与 benchfb 兜底
      去重（1c：旧键 (install_id, tid) 会吞掉同轮另一条；键含 recId 后不吞）。
    - 老数据降级：无 recId 的 label 事件（键 recId=""）与无 tid 字段的 benchfb 记录（键 tid=""）
      相互不匹配 → 双行并存（降级不匹配，可接受）；带 recId 的新事件只与同键 benchfb 记录去重。
    """
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-keys",
        "install_id": "inst-keys", "exported_at": "2026-08-20T08:00:00Z",
        "usage_events": [
            {"event_id": "e-a1", "t": ts, "k": "label", "tid": "t-x", "recId": "r-a", "rev": 1,
             "completion": "done", "reasons": ["数据全"], "useful_idx": [1], "comment": ""},
            {"event_id": "e-b1", "t": ts + 1000, "k": "label", "tid": "t-x", "recId": "r-b", "rev": 1,
             "completion": "partial", "reasons": ["排序不对"], "useful_idx": [], "comment": ""},
            {"event_id": "e-old1", "t": ts + 2000, "k": "label", "tid": "t-y", "rev": 1,
             "completion": "done", "reasons": [], "useful_idx": [], "comment": "老事件"},
        ],
        "benchfb_records": [
            {"id": "r-a", "tid": "t-x", "kind": "search", "t": ts, "q": "q",
             "rating": {"completion": "done", "reasons": ["数据全"], "useful_idx": [1], "comment": ""}},
            {"id": "r-b", "tid": "t-x", "kind": "tool", "t": ts + 1000, "q": "q2",
             "rating": {"completion": "partial", "reasons": ["排序不对"], "useful_idx": [], "comment": ""}},
            {"id": "r-old", "kind": "search", "t": ts + 3000, "q": "q3",   # 无 tid 字段（老 benchfb 记录）
             "rating": {"stars": 3, "useful_idx": [1], "comment": "老评分"}},
        ],
        "mcp_records": [],
    })
    out = tmp_path / "keys"
    assert export.main(["--dsn", str(path), "--out", str(out)]) == 0
    labels = _read_jsonl(out / "explicit_labels.jsonl")
    by_key = {(l["tid"], l.get("recId")): l for l in labels}
    assert set(by_key) == {("t-x", "r-a"), ("t-x", "r-b"), ("t-y", None), (None, "r-old")}
    # 1a：同键两源都有 → 只留 label 事件行（benchfb 兜底被真正去重）
    assert by_key[("t-x", "r-a")]["label_source"] == "event"
    assert by_key[("t-x", "r-b")]["label_source"] == "event"
    # 1c：同 tid 两条记录各自保留（旧按 (install_id, tid) 只留一条）
    assert by_key[("t-x", "r-a")]["rev"] == 1 and by_key[("t-x", "r-b")]["rev"] == 1
    # 老数据降级：无 recId 事件 (t-y,None) 与无 tid benchfb 记录 (None,r-old) 互不匹配 → 双行并存
    assert by_key[("t-y", None)]["label_source"] == "event" and by_key[("t-y", None)]["comment"] == "老事件"
    assert by_key[(None, "r-old")]["label_source"] == "benchfb"
    assert by_key[(None, "r-old")]["stars"] == 3
    # 计数：3 事件键 + 3 benchfb 键 − 2 去重 = 4 行；labels_from_benchfb = 存活兜底行数
    rep = export._export(export._make_engine(str(path)), tmp_path / "keys2",
                         since=None, until=None, incremental=False, accepted_ids=None)["report"]
    assert rep["labels_total"] == 4
    assert rep["labels_from_events"] == 3 and rep["labels_from_benchfb"] == 1
    assert rep["labels_benchfb_deduped"] == 2


def test_export_candidate_policy_and_iid_are_exact_and_never_object_stringified(db, tmp_path):
    """候选只能由同 install+tid 的曝光回填；污染/歧义显式降级，不删原包。"""
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    payload = {
        "schema": "biodata-telemetry/1", "contract_version": 2,
        "prompt_version": "route-p7", "experiment_id": "rank-e1", "experiment_arm": "candidate",
        "propensity": 0.2, "training_consent": True,
        "packet_id": "pkt-candidate-context", "install_id": "inst-context",
        "usage_events": [
            {"event_id": "imp-good", "t": ts, "k": "imp", "sid": "s-1", "tid": "t-good", "iid": "i-good",
             "policy": "bpol1:snap=good", "items": []},
            {"event_id": "imp-dirty", "t": ts + 1, "k": "imp", "sid": "s-1", "tid": "t-dirty", "iid": "i-dirty",
             "policy": "[object Object]", "items": []},
            {"event_id": "imp-a1", "t": ts + 2, "k": "imp", "sid": "s-1", "tid": "t-amb", "iid": "i-a1",
             "policy": "bpol1:snap=a", "items": []},
            {"event_id": "imp-a2", "t": ts + 3, "k": "imp", "sid": "s-2", "tid": "t-amb", "iid": "i-a2",
             "policy": "bpol1:snap=b", "items": []},
        ],
        "benchfb_records": [
            {"id": "r-good", "tid": "t-good", "kind": "search", "t": ts, "q": "good",
             "rating": {"completion": "done", "reasons": ["已完成"]}},
            {"id": "r-dirty", "tid": "t-dirty", "kind": "search", "t": ts + 1, "q": "dirty",
             "rating": {"completion": "partial", "reasons": ["原因"]}},
            {"id": "r-amb", "tid": "t-amb", "kind": "search", "t": ts + 2, "q": "amb",
             "rating": {"completion": "failed", "reasons": []}},
            # 新后端有紧凑串时，直接从 record 的 search.res 取，不依赖曝光是否同包。
            {"id": "r-record", "tid": "t-record", "kind": "search", "t": ts + 4, "q": "record",
             "search": {"res": {"policy_id_str": "bpol1:snap=record"}}, "rating": {}},
        ],
        "mcp_records": [],
    }
    _insert(engine, payload)
    out = tmp_path / "candidate-context"
    assert export.main(["--dsn", str(path), "--out", str(out)]) == 0
    rows = {row["record_id"]: row for row in _read_jsonl(out / "benchmark_candidates.jsonl")}
    assert rows["r-good"]["iid"] == "i-good" and rows["r-good"]["policy"] == "bpol1:snap=good"
    assert rows["r-dirty"]["iid"] == "i-dirty" and rows["r-dirty"]["policy"] == "policy_unknown"
    assert rows["r-amb"]["iid"] is None and rows["r-amb"]["policy"] == "policy_unknown"
    assert rows["r-record"]["iid"] is None and rows["r-record"]["policy"] == "bpol1:snap=record"
    assert rows["r-good"]["contract_version"] == 2 and rows["r-good"]["prompt_version"] == "route-p7"
    assert rows["r-good"]["experiment_arm"] == "candidate" and rows["r-good"]["propensity"] == 0.2
    assert rows["r-good"]["training_consent"] is True
    assert rows["r-good"]["rating"]["completion"] == "done"
    assert rows["r-good"]["rating"]["reasons"] == ["已完成"]
    # 导出是只读：原始包里的污染字面量仍在，只有导出物被安全归一化。
    assert payload["usage_events"][1]["policy"] == "[object Object]"
    assert "[object Object]" not in (out / "benchmark_candidates.jsonl").read_text(encoding="utf-8")


def test_export_incremental_resume(db, tmp_path):
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, _pkt_v3_imp(ts))
    out = tmp_path / "inc"
    assert export.main(["--dsn", str(path), "--out", str(out), "--incremental"]) == 0
    first_count = len(_read_jsonl(out / "impressions.jsonl"))
    assert first_count == 1                            # 只有 imp 事件（view 不是曝光）
    state = json.loads((out / ".telemetry_export_state.json").read_text(encoding="utf-8"))
    assert isinstance(state["watermark"], int)

    # 新增一包后增量续跑：全量扫描 + 行键 merge → 只多出新数据；再跑一次不重复产出（幂等）
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-inc-2",
        "install_id": "inst-inc", "exported_at": "2026-08-20T12:00:00Z",
        "usage_events": [{"event_id": "u-inc1", "t": ts + 14400000, "k": "search", "q": "新增查询"}],
        "benchfb_records": [], "mcp_records": [],
    })
    assert export.main(["--dsn", str(path), "--out", str(out), "--incremental"]) == 0
    second_imps = _read_jsonl(out / "impressions.jsonl")
    assert len(second_imps) == first_count + 1
    assert second_imps[-1]["q"] == "新增查询"
    state2 = json.loads((out / ".telemetry_export_state.json").read_text(encoding="utf-8"))
    assert state2["watermark"] > state["watermark"]

    assert export.main(["--dsn", str(path), "--out", str(out), "--incremental"]) == 0
    assert len(_read_jsonl(out / "impressions.jsonl")) == len(second_imps)  # 重复跑 → 不重复产出


def test_export_incremental_view_enrichment_updates_impression(db, tmp_path):
    """view 事件晚于曝光落库（分属两次导出）：增量续跑用 prefer_enriched 把 seen/dwell 补进旧行。"""
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-imp-only",
        "install_id": "inst-late", "exported_at": "2026-08-20T08:00:00Z",
        "usage_events": [{"event_id": "u-imp-late", "t": ts, "k": "imp", "sid": "s-l", "tid": "t-l",
                          "iid": "i-late", "q": "晚到的 view", "items": [{"uid": "dL9", "pos": 1}]}],
        "benchfb_records": [], "mcp_records": [],
    })
    out = tmp_path / "late"
    assert export.main(["--dsn", str(path), "--out", str(out), "--incremental"]) == 0
    assert "seen" not in _read_jsonl(out / "impressions.jsonl")[0]

    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-view-late",
        "install_id": "inst-late", "exported_at": "2026-08-20T08:30:00Z",
        "usage_events": [{"event_id": "u-view-late", "t": ts + 1800000, "k": "view", "sid": "s-l",
                          "tid": "t-l", "iid": "i-late", "seen": [1], "dwell_ms": 2000}],
        "benchfb_records": [], "mcp_records": [],
    })
    assert export.main(["--dsn", str(path), "--out", str(out), "--incremental"]) == 0
    imps = _read_jsonl(out / "impressions.jsonl")
    assert len(imps) == 1
    assert imps[0]["seen"] == [1] and imps[0]["dwell_ms"] == 2000     # 旧行被 enrich 覆盖，不重复


def test_export_accepted_filter(db, tmp_path):
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, _pkt_v3_imp(ts))
    ids = tmp_path / "accepted_ids.txt"
    ids.write_text("# 人工接受名单\nr-search-1\n", encoding="utf-8")
    out = tmp_path / "acc"
    rc = export.main(["--dsn", str(path), "--out", str(out), "--accepted", str(ids)])
    assert rc == 0
    final = _read_jsonl(out / "benchmark_candidates.final.jsonl")
    assert [c["record_id"] for c in final] == ["r-search-1"]
    # 全量 candidates 不受影响
    assert len(_read_jsonl(out / "benchmark_candidates.jsonl")) == 2


def test_export_quarantine_unprocessable_and_schema_other(db, tmp_path):
    path, engine = db
    _insert(engine, {"schema": "biodata-telemetry/1", "packet_id": "pkt-ugly",
                     "install_id": "inst-ugly", "usage_events": ["not-a-dict"],
                     "benchfb_records": [], "mcp_records": []})
    _insert(engine, {"packet_id": "pkt-noschema", "install_id": "inst-old",   # 缺 schema：旧 pending 容错
                     "usage_events": [{"event_id": "u-old", "t": 1, "k": "search", "q": "老包"}],
                     "benchfb_records": [], "mcp_records": []})
    out = tmp_path / "ugly"
    assert export.main(["--dsn", str(path), "--out", str(out)]) == 0
    quarantined = _read_jsonl(out / "quarantine.jsonl")
    assert any(q["reason"] == "unprocessable" for q in quarantined)
    imps = _read_jsonl(out / "impressions.jsonl")
    assert any(i["q"] == "老包" and i["join_status"] == "legacy" for i in imps)   # 缺 schema 包容错处理
    report = (out / "quality_report.md").read_text(encoding="utf-8")
    assert "packets_schema_other" in report.replace("\n", " ") or "缺失/其他值" in report


def test_export_feedback_jsonl(db, tmp_path):
    """：反馈包 → feedback.jsonl 产物（遮蔽后形态；按 feedback_id 幂等去重）。"""
    path, engine = db
    ts = int(datetime(2026, 8, 22, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    # 接收端落库形态：明文已解密并过值级遮蔽（sk-… → [API Key]）
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-fb-1",
        "install_id": "inst-fb", "client_id": "client-fb-0001", "profile_id": "profile-fb-0001",
        "exported_at": "2026-08-22T08:00:00Z",
        "usage_events": [], "benchfb_records": [], "mcp_records": [],
        "feedback_records": [
            {"feedback_id": "fb-a1", "identity": "profile-fb-0001", "with_diag": True,
             "authorized_at": "2026-08-22T07:00:00Z",
             "text": f"建议结果页加导出按钮，key 是 {FAKE_API_KEY} 别外传",
             "diag": {"available": True, "errors": 1, "features": {"search": 3}}},
            {"feedback_id": "fb-a2", "identity": "profile-fb-0001", "with_diag": False,
             "authorized_at": "2026-08-22T07:30:00Z", "text": "没有诊断信息的意见", "diag": None},
        ],
    })
    # 同一 feedback_id 重传（不同 packet）→ 导出侧只留一行（与接收端幂等同键）
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-fb-2",
        "install_id": "inst-fb", "client_id": "client-fb-0001", "profile_id": "profile-fb-0001",
        "exported_at": "2026-08-22T08:10:00Z",
        "usage_events": [], "benchfb_records": [], "mcp_records": [],
        "feedback_records": [
            {"feedback_id": "fb-a1", "identity": "profile-fb-0001", "with_diag": True,
             "authorized_at": "2026-08-22T07:00:00Z",
             "text": f"建议结果页加导出按钮，key 是 {FAKE_API_KEY} 别外传",
             "diag": {"available": True, "errors": 1, "features": {"search": 3}}},
        ],
    })
    # 未知键：照常遮蔽进产物，仅在 schema 漂移统计出现
    _insert(engine, {
        "schema": "biodata-telemetry/1", "packet_id": "pkt-fb-3",
        "install_id": "inst-fb2", "exported_at": "2026-08-22T08:20:00Z",
        "usage_events": [], "benchfb_records": [], "mcp_records": [],
        "feedback_records": [
            {"feedback_id": "fb-b1", "identity": "profile-fb-0002", "with_diag": False,
             "authorized_at": "2026-08-22T08:00:00Z", "text": "第三条", "diag": None, "mystery_key": "x"},
        ],
    })

    out = tmp_path / "export"
    rc = export.main(["--dsn", str(path), "--out", str(out)])
    assert rc == 0

    rows = _read_jsonl(out / "feedback.jsonl")
    by_id = {r["feedback_id"]: r for r in rows}
    assert set(by_id) == {"fb-a1", "fb-a2", "fb-b1"}
    a1 = by_id["fb-a1"]
    assert a1["text"] == "建议结果页加导出按钮，key 是 [API Key] 别外传"   # 遮蔽后形态
    assert a1["with_diag"] is True and a1["diag"]["errors"] == 1
    assert a1["identity"] == "profile-fb-0001" and a1["install_id"] == "inst-fb"
    assert a1["authorized_at"] == "2026-08-22T07:00:00Z"
    assert by_id["fb-a2"]["with_diag"] is False and by_id["fb-a2"]["diag"] is None
    # 原始 API Key 不出现在任何产物
    for fname in ("feedback.jsonl", "quality_report.md", "review.html"):
        text = (out / fname).read_text(encoding="utf-8")
        assert FAKE_API_KEY not in text, f"{fname} 泄漏原始 API Key"

    # report 计数（处理条数口径，与 mcp_total 一致：含跨包重传行）与 schema 漂移
    summary = export._export(export._make_engine(str(path)), tmp_path / "export2",
                             since=None, until=None, incremental=False, accepted_ids=None)
    rep = summary["report"]
    assert rep["feedback_total"] == 4      # 3 包共 4 行（fb-a1 重传一次也算处理）
    assert rep["feedback_with_diag"] == 2  # 两条 with_diag=True（fb-a1 首次 + 重传）
    assert "mystery_key" in rep["unknown_keys"].get("feedback_records", [])

    # 增量续跑：重复跑不重复产出（行键 = install+feedback_id）
    rc2 = export.main(["--dsn", str(path), "--out", str(out), "--incremental"])
    assert rc2 == 0
    assert len(_read_jsonl(out / "feedback.jsonl")) == 3


def test_delete_dry_run_then_yes(db, tmp_path):
    path, engine = db
    ts = int(datetime(2026, 8, 20, 8, 0, tzinfo=timezone.utc).timestamp() * 1000)
    _insert(engine, _pkt_v3_imp(ts))                 # inst-e2e：1 包（usage 2 + benchfb 2 + mcp 2）
    _insert(engine, {"schema": "biodata-telemetry/1", "packet_id": "pkt-keep",
                     "install_id": "inst-keep", "exported_at": "2026-08-20T08:30:00Z",
                     "usage_events": [{"event_id": "u-k", "t": ts + 1800000, "k": "search", "q": "保留"}],
                     "benchfb_records": [], "mcp_records": []})
    # 直接造数不经过 ingest 路由，这里手动补 event receipts 以验证级联删除
    from telemetry_idempotency import event_receipts
    with engine.begin() as conn:
        conn.execute(event_receipts.insert().values(
            event_key="ev-e2e-1", packet_id="pkt-v3-imp", kind="usage"))
        conn.execute(event_receipts.insert().values(
            event_key="ev-e2e-2", packet_id="pkt-v3-imp", kind="mcp"))
        conn.execute(event_receipts.insert().values(
            event_key="ev-keep-1", packet_id="pkt-keep", kind="usage"))

    # dry-run：只报告不删除
    assert delete.main(["--dsn", str(path), "--install-id", "inst-e2e"]) == 0
    with engine.connect() as conn:
        assert conn.execute(select(ingest_packets.c.install_id)).scalars().all() == ["inst-e2e", "inst-keep"]

    # --yes：删主包 + 两张 receipts；他机数据不受影响
    assert delete.main(["--dsn", str(path), "--install-id", "inst-e2e", "--yes"]) == 0
    with engine.connect() as conn:
        assert conn.execute(select(ingest_packets.c.install_id)).scalars().all() == ["inst-keep"]
        assert conn.execute(select(event_receipts.c.event_key)).scalars().all() == ["ev-keep-1"]
        assert conn.execute(select(packet_receipts.c.packet_id)).scalars().all() == ["pkt-keep"]
