# -*- coding: utf-8 -*-
"""curate.rollback（回滚动词化）的专项钉。

- **正常回滚**：伪造带快照的写步 → 机械闸选中最新未回滚步 → 新文件移回收站
  （manifest 如实标 trace_rollback）、结果过 RollbackResult 形状闸；
- **顺序回退**：最新步已回过 → 重复调用自然往更早一步退；无步可回 → 如实拒绝；
- **fail-closed**：最近可用锚未 finalize → 拒绝，绝不越过它回更早的步；
- **越界拒绝**：steps 无 snapshot_id / ctx 缺席 → rolled_back=False + 如实句
  （拒绝是数据不是故障，search.rerun adopted=False 同哲学）；
- **execute 联动**：真跑写步 → step 实录落 snapshot_id（只读步不落，字节契约不变）→
  下一步 curate.rollback 经 ctx.steps 够到锚、自己也有快照锚 + tool_call/state_snapshot 留痕；
- **注册**：三件套（VERB_SPECS/LOOP_TOOLS/LOOP_RESULT_MODELS）+ 套件面 + 豁免/排除表。

全离线：tmp 项目根 + 真 SnapshotStore，绝不碰真实库、绝不联网。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：execute 联动钉跳过")

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec as AX  # noqa: E402
from dataset_recommender.agent import agent_schemas as SC  # noqa: E402
from dataset_recommender.agent.trace import (  # noqa: E402
    TraceRecorder, bind_recorder, snapshot_store, trace_root)
from dataset_recommender.corpus import corpus_curation as _cc  # noqa: E402


def _make_ext(root):
    ext = root / "database" / "external"
    ext.mkdir(parents=True)
    return ext


def _snap_write(root, store, verb, name, content):
    """模拟一次写步留锚：capture → 真写文件 → finalize，返回 snapshot_id。"""
    sid = store.capture(verb, preimage_paths=[])
    (root / "database" / "external" / name).write_text(content, encoding="utf-8")
    store.finalize(sid)
    return sid


def _step(verb, sid, verb_zh="写操作"):
    return {"verb": verb, "verb_zh": verb_zh, "ok": True, "snapshot_id": sid}


# ---------------------------------------------------------------- 正常回滚

def test_rollback_picks_latest_snapshot_step_and_recycles(tmp_path):
    ext = _make_ext(tmp_path)
    store = snapshot_store(tmp_path)
    sid = _snap_write(tmp_path, store, "curate.sync_updates", "upload_new.json", "{}")
    steps = [_step("curate.sync_updates", sid, "检查更新并同步入库")]

    result = AX._loop_curate_rollback({}, tmp_path, {"steps": steps})
    SC.RollbackResult.model_validate(result)  # 与 execute 出口同一道形状闸
    assert result["rolled_back"] is True and result["reason"] == "rolled_back"
    assert result["snapshot_id"] == sid and result["verb"] == "curate.sync_updates"
    assert result["recycled"] == ["upload_new.json"] and result["restored"] == []
    assert "已回滚「检查更新并同步入库」" in result["note_zh"]
    assert "1 个新文件移入回收站" in result["note_zh"]
    assert not (ext / "upload_new.json").exists(), "新文件已移出 external"
    recycle = tmp_path / ".userdata" / "recycle"
    assert recycle.is_dir() and any(p.name.endswith("upload_new.json")
                                    for p in recycle.iterdir())
    # 回收站 manifest 如实标 trace_rollback（移动而非删除的纪律留痕）
    manifest = _cc._recycle_manifest(tmp_path)
    rows = [json.loads(ln) for ln in manifest.read_text(encoding="utf-8").splitlines()
            if ln.strip()]
    assert any(r.get("action") == "trace_rollback" and r.get("snapshot_id") == sid
               for r in rows)


def test_rollback_repeated_calls_walk_backwards_through_execute(monkeypatch, tmp_path):
    """ 生产组合钉：两次写入→回滚→再回滚，必须依次退 b、a。

    旧用例直调私有 helper 并永远复用原 steps，漏掉 execute 会给 rollback 自己创建快照的事实；
    本钉走四次真实 execute、累积真实 steps，确保 rollback 步快照不会反过来成为候选。
    """
    monkeypatch.setattr(AX, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(AX, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    ext = _make_ext(tmp_path)

    def write_run(slots, root):
        name = str(slots["name"])
        (ext / name).write_text(name, encoding="utf-8")
        return dict(SEARCH_OK, filename=name, record_count=1)

    monkeypatch.setattr(AX, "LOOP_TOOLS", {
        "curate.search_online": {"run": write_run, "label_zh": "联网搜索入库",
                                  "card_kind": "search_online", "readonly": False},
        "curate.rollback": {"run": AX._loop_curate_rollback, "label_zh": "回滚写操作",
                             "card_kind": "rollback", "readonly": False,
                             "needs_context": True},
    })
    steps = []
    rec = TraceRecorder(tmp_path, "test", "rb-walk", enabled=True)
    with bind_recorder(rec):
        for name in ("a.json", "b.json"):
            out = AX.execute(_state("curate.search_online", steps, {"name": name}),
                             runtime=_runtime())
            steps.extend(out["steps"])
        first = AX.execute(_state("curate.rollback", steps), runtime=_runtime())
        steps.extend(first["steps"])
        second = AX.execute(_state("curate.rollback", steps), runtime=_runtime())
        steps.extend(second["steps"])

    r1 = first["steps"][0]["result"]
    r2 = second["steps"][0]["result"]
    assert r1["verb"] == "curate.search_online" and r1["rolled_back"] is True
    assert r2["verb"] == "curate.search_online" and r2["rolled_back"] is True
    assert not (ext / "b.json").exists() and not (ext / "a.json").exists()
    assert [s["verb"] for s in steps] == [
        "curate.search_online", "curate.search_online", "curate.rollback", "curate.rollback"]


# ---------------------------------------------------------------- 越界 / fail-closed 拒绝

def test_rollback_refuses_without_snapshot_step(tmp_path):
    _make_ext(tmp_path)
    for ctx in ({"steps": [{"verb": "curate.sync_updates", "ok": True}]},
                {"steps": []}, None, {}):
        result = AX._loop_curate_rollback({}, tmp_path, ctx)
        SC.RollbackResult.model_validate(result)
        assert result["rolled_back"] is False
        assert result["reason"] == "no_rollbackable_step"
        assert result["snapshot_id"] is None
        assert "没有可回滚" in result["note_zh"]


def test_rollback_fail_closed_on_unfinalized_snapshot(tmp_path):
    """最近可用锚未 finalize → 拒绝，且不越过它回更早的步（乱序回退比不回更糟）。"""
    ext = _make_ext(tmp_path)
    store = snapshot_store(tmp_path)
    sid_old = _snap_write(tmp_path, store, "curate.sync_updates", "old.json", "1")
    sid_new = store.capture("curate.sync_updates", preimage_paths=[])  # 刻意不 finalize
    steps = [_step("curate.sync_updates", sid_old), _step("curate.sync_updates", sid_new)]

    result = AX._loop_curate_rollback({}, tmp_path, {"steps": steps})
    SC.RollbackResult.model_validate(result)
    assert result["rolled_back"] is False
    assert result["reason"] == "snapshot_not_finalized"
    assert result["snapshot_id"] == sid_new
    assert (ext / "old.json").is_file(), "fail-closed：未 finalize 的锚挡住乱序回退"


def test_rollback_fail_closed_on_missing_or_corrupt_latest_snapshot(tmp_path):
    """ 越序钉：最新锚缺失/损坏与未 finalize 同档，绝不跳去回更早写步。"""
    ext = _make_ext(tmp_path)
    store = snapshot_store(tmp_path)
    sid_old = _snap_write(tmp_path, store, "curate.sync_updates", "old.json", "1")
    for sid_new in ("missing-snapshot", store.capture("curate.search_online", preimage_paths=[])):
        if sid_new != "missing-snapshot":
            (store.dir / sid_new / "meta.json").write_text("{broken", encoding="utf-8")
        result = AX._loop_curate_rollback({}, tmp_path, {"steps": [
            _step("curate.sync_updates", sid_old),
            _step("curate.search_online", sid_new),
        ]})
        assert result["rolled_back"] is False
        assert result["reason"] == "snapshot_unavailable"
        assert result["snapshot_id"] == sid_new
        assert (ext / "old.json").is_file(), "最新锚不可读必须挡住更早快照"


def test_rollback_incomplete_is_not_reported_or_marked_as_rolled_back(tmp_path):
    """ 如实性钉：零实际恢复或 unrestorable 非空时 rolled_back=false。"""
    ext = _make_ext(tmp_path)
    victim = ext / "existing.json"
    victim.write_text("before", encoding="utf-8")
    store = snapshot_store(tmp_path)
    sid = store.capture("curate.sync_updates", preimage_paths=[])
    victim.write_text("after", encoding="utf-8")
    store.finalize(sid)

    result = AX._loop_curate_rollback(
        {}, tmp_path, {"steps": [_step("curate.sync_updates", sid)]})
    assert result["rolled_back"] is False
    assert result["reason"] == "rollback_incomplete"
    assert result["recycled"] == result["restored"] == []
    assert [x["name"] for x in result["unrestorable"]] == ["existing.json"]
    assert "没有完成回滚" in result["note_zh"] and "1 项未能恢复" in result["note_zh"]
    assert not store.load(sid).get("rolled_back_at"), "未完整回滚不能伪装成已回过后跳过"


def test_rollback_has_separate_budget_from_forward_writes():
    """ 预算钉：正向写满 2 次仍可回滚；回滚自己每轮至多 2 次。"""
    forward_steps = [
        {"verb": "curate.search_online", "ok": True, "result": {"record_count": 1}},
        {"verb": "curate.sync_updates", "ok": True, "result": {"imported_total": 1}},
    ]
    state = {"route_scope": "general", "utterance": "回滚",
             "steps": forward_steps}
    raw, _note, _declined, violation = AX._adjudicate_decide_obj(
        {"verb": "curate.rollback", "quoted": "回滚"}, state)
    assert raw is not None and violation == "", "正向写预算不能挡恢复动作"

    state["steps"] = forward_steps + [
        {"verb": "curate.rollback", "ok": True},
        {"verb": "curate.rollback", "ok": True},
    ]
    raw2, note2, declined2, violation2 = AX._adjudicate_decide_obj(
        {"verb": "curate.rollback", "quoted": "回滚"}, state)
    assert raw2 is None and violation2 == ""
    assert "回滚预算" in note2 and "2 次" in declined2


# ---------------------------------------------------------------- execute 联动（真 recorder + 真快照链）

SEARCH_OK = {"source_label": "ArrayExpress", "query": "人类肺", "species": "人类",
             "sample_titles": ["human lung atlas"], "record_count": 2,
             "filename": "upload_rb.json", "warnings": []}


def _runtime():
    return SimpleNamespace(context=SimpleNamespace(on_progress=None, chat_model=None))


def _state(verb, steps, slots=None):
    return {"utterance": "撤销刚才那步",
            "plan": {"verb": verb, "slots": slots or {"quoted": "撤销刚才那步"}},
            "loop_plan": None, "steps": steps, "pending_reask_write": False}


def test_execute_chains_snapshot_id_and_rollback_step_gets_own_anchor(monkeypatch, tmp_path):
    monkeypatch.setattr(AX, "_agent_project_root", lambda: tmp_path)
    monkeypatch.setattr(AX, "_task_checklist_call", lambda *a, **k: ([], 0, ""))
    ext = _make_ext(tmp_path)
    fname = SEARCH_OK["filename"]

    def write_run(slots, root):
        (ext / fname).write_text(json.dumps({"rows": ["x"]}, ensure_ascii=False),
                                 encoding="utf-8")
        return dict(SEARCH_OK)

    rec = TraceRecorder(tmp_path, "test", "rb-loop", enabled=True)
    with bind_recorder(rec):
        # 第一步：写动词真跑——step 实录必须落 snapshot_id（回滚锚入链）
        monkeypatch.setattr(AX, "LOOP_TOOLS", {
            "curate.search_online": {"run": write_run, "label_zh": "联网搜索入库",
                                     "card_kind": "search_online", "readonly": False},
        })
        out1 = AX.execute(_state("curate.search_online", []), runtime=_runtime())
        write_step = out1["steps"][0]
        assert write_step["ok"] is True and (ext / fname).is_file()
        assert write_step.get("snapshot_id"), "写步落快照锚（只读步不落，既有钉在案）"

        # 第二步：curate.rollback 经 ctx.steps 够到上一步的锚
        monkeypatch.setattr(AX, "LOOP_TOOLS", {
            "curate.rollback": {"run": AX._loop_curate_rollback, "label_zh": "回滚写操作",
                                "card_kind": "rollback", "readonly": False,
                                "needs_context": True},
        })
        out2 = AX.execute(_state("curate.rollback", list(out1["steps"])),
                          runtime=_runtime())
    rb_step = out2["steps"][0]
    assert rb_step["ok"] is True, rb_step.get("error")
    result = rb_step["result"]
    assert result["rolled_back"] is True
    assert result["snapshot_id"] == write_step["snapshot_id"]
    assert not (ext / fname).exists(), "上一步的写入被回退"
    assert rb_step.get("snapshot_id"), "回滚本身是写且留 trace 锚；2026-08-18 起候选闸明确跳过 rollback 步"

    # trace 留痕：两个写步各一条 state_snapshot；tool_call 有 curate.rollback
    path = trace_root(tmp_path) / "test" / "rb-loop.jsonl"
    events = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
              if ln.strip()]
    snaps = [e for e in events if e["kind"] == "state_snapshot"]
    assert [e["payload"]["verb"] for e in snaps] == [
        "curate.search_online", "curate.rollback"]
    tool_verbs = [e["payload"]["verb"] for e in events if e["kind"] == "tool_call"]
    assert tool_verbs == ["curate.search_online", "curate.rollback"]


# ---------------------------------------------------------------- 注册钉

def test_rollback_registration():
    spec = AP.VERB_BY_NAME["curate.rollback"]
    assert spec.kind == AP.EXEC and spec.slots == (), "零槽位——模型发明不了快照 id"
    assert spec.requires_results is False
    assert spec.zh == "回滚写操作"

    entry = AX.LOOP_TOOLS["curate.rollback"]
    assert entry["readonly"] is False, "回滚本身会改文件——自动获得 trace 快照锚"
    assert entry["needs_context"] is True and entry["card_kind"] == "rollback"
    assert callable(entry["run"]) and entry["decide_zh"]
    assert SC.LOOP_RESULT_MODELS["curate.rollback"] is SC.RollbackResult

    assert "curate.rollback" in AX._DECIDE_VERB_ORDER
    names = [t["function"]["name"] for t in AX._DECIDE_TOOL_SPECS]
    assert "curate_rollback" in names, "decide 工具面含回滚"
    # 套件归属：动作 + 全能；检索面没有写操作可回滚，刻意不进 search。
    assert "curate.rollback" in AX._SUITE_LOOP_VERBS["action"]
    assert "curate.rollback" in AX._SUITE_LOOP_VERBS["general"]
    assert "curate.rollback" not in AX._SUITE_LOOP_VERBS["search"]
    # 钉字：回滚不占正向写预算，改走独立 MAX_ROLLBACK 预算。
    assert "curate.rollback" not in AX._WRITE_LOOP_TOOLS
    assert AX._ROLLBACK_LOOP_TOOL == "curate.rollback" and AX.MAX_ROLLBACK == 2
    # 环内专属两面墙（差集钉在 test_action_plan.py，这里钉成员资格）。
    assert "curate.rollback" in AP.FRONTEND_UNWIRED_EXEC_VERBS
    assert "curate.rollback" in AP.PLAN_ACTION_EXCLUDED_VERBS
