# -*- coding: utf-8 -*-
"""MCP 遥测中继端点 + 接入引导端点契约测试（扩：分页/CAS/确定性）。

覆盖 `tests/conftest.py` 全局夹具之外的四件事：
- `GET /api/telemetry/mcp-calls`：行号增量语义（offset=after）、半截尾行不计数、文件不存在 → 空、
  非法 after → 400；分页——limit/max_bytes 截断（next_offset 只到最后一条已消费行）、
  since_ts 过滤（无 ts 的 legacy 行保留、被滤行视为已消费）、无 call_id 旧行合成稳定 legacy 键；
- `POST /api/telemetry/mcp-calls/ack`：游标持久化（.userdata/mcp_calls_uploaded.json）、幂等、
  CAS（回退 ack 不落盘、响应 max(新,旧)）、非法 offset → 422、跨源 → 403；
- `GET /api/guide/agent-prompt`：text/markdown 全文 + 缺失 404 + 跨源 403；
- `GET /api/guide/skill.zip`：内存 zip 附件（内含 SKILL.md）+ 确定性（两次构建字节一致、
  X-SHA256 = 内容 sha256）+ 缺失 404 + 跨源 403。

**hermetic**：端点读写的 `.userdata` 文件全部 monkeypatch 重定向到 per-test tmp（绝不碰真实
`<仓库>/.userdata/`）；guide 端点读 resource_root，缺失用例用 monkeypatch 指向空目录模拟。
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient  # noqa: E402

from dataset_recommender.app.webapp import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

_EVIL = {"Origin": "https://evil.example"}


def _redirect_userdata(monkeypatch, tmp_path):
    """把遥测两个落盘文件重定向到 per-test tmp。"""
    log = tmp_path / "mcp_calls.jsonl"
    cursor = tmp_path / "mcp_calls_uploaded.json"
    monkeypatch.setattr("dataset_recommender.app.webapp._mcp_calls_log_path", lambda: log)
    monkeypatch.setattr("dataset_recommender.app.webapp._mcp_calls_upload_cursor_path", lambda: cursor)
    return log, cursor


def _write_log(path: Path, lines: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")


# ---------------------------------------------------------------- GET /api/telemetry/mcp-calls

def test_telemetry_mcp_calls_missing_file_returns_empty(monkeypatch, tmp_path):
    _redirect_userdata(monkeypatch, tmp_path)
    r = client.get("/api/telemetry/mcp-calls")
    assert r.status_code == 200
    body = r.json()
    assert body == {"ok": True, "records": [], "next_offset": 0, "truncated": False}


def test_telemetry_mcp_calls_incremental_line_number_semantics(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    rows = [
        {"schema": "biodata-mcp-calls/v1", "call_id": "a" * 32, "ts": "2026-08-01T00:00:00.000Z", "tool": "biodata_status"},
        {"schema": "biodata-mcp-calls/v1", "call_id": "b" * 32, "ts": "2026-08-01T00:00:01.000Z", "tool": "parse_constraints"},
        {"schema": "biodata-mcp-calls/v1", "call_id": "c" * 32, "ts": "2026-08-01T00:00:02.000Z", "tool": "recommend_datasets"},
    ]
    _write_log(log, [json.dumps(r, ensure_ascii=False) + "\n" for r in rows])

    # 从头拉：三条全回，next_offset = 3（行号）
    body = client.get("/api/telemetry/mcp-calls").json()
    assert body["ok"] is True
    assert [r["tool"] for r in body["records"]] == ["biodata_status", "parse_constraints", "recommend_datasets"]
    assert body["next_offset"] == 3
    # after=1：只回第 2、3 行
    body = client.get("/api/telemetry/mcp-calls", params={"after": 1}).json()
    assert [r["tool"] for r in body["records"]] == ["parse_constraints", "recommend_datasets"]
    assert body["next_offset"] == 3
    # after=3（已消费到末尾）：空记录、offset 不前进
    body = client.get("/api/telemetry/mcp-calls", params={"after": 3}).json()
    assert body["records"] == [] and body["next_offset"] == 3
    # after 超过文件末尾：空记录、offset 幂等不回退
    body = client.get("/api/telemetry/mcp-calls", params={"after": 99}).json()
    assert body["records"] == [] and body["next_offset"] == 99
    # after=0 等价于从头
    body = client.get("/api/telemetry/mcp-calls", params={"after": 0}).json()
    assert len(body["records"]) == 3


def test_telemetry_mcp_calls_skips_half_written_tail_and_garbage_lines(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    good = '{"schema": "biodata-mcp-calls/v1", "call_id": "%s", "tool": "biodata_status"}\n' % ("a" * 32)
    _write_log(log, [good, '{"schema": "biodata-mcp-calls/v1", "tool": "parse_'])  # 尾行半截 JSON
    body = client.get("/api/telemetry/mcp-calls").json()
    assert len(body["records"]) == 1
    assert body["next_offset"] == 1          # 半截尾行不计数、不前进
    # 坏行夹在中间：跳过不计数，后续行照常（与 summarize 脚本同容忍口径）
    _write_log(log, [good, "not json at all\n", '{"schema": "biodata-mcp-calls/v1", "call_id": "%s", "tool": "x"}\n' % ("c" * 32)])
    body = client.get("/api/telemetry/mcp-calls").json()
    assert [r["tool"] for r in body["records"]] == ["biodata_status", "x"]
    assert body["next_offset"] == 3


def test_telemetry_mcp_calls_rejects_negative_after(monkeypatch, tmp_path):
    _redirect_userdata(monkeypatch, tmp_path)
    assert client.get("/api/telemetry/mcp-calls", params={"after": -1}).status_code == 400


def test_telemetry_mcp_calls_rejects_evil_origin(monkeypatch, tmp_path):
    _redirect_userdata(monkeypatch, tmp_path)
    assert client.get("/api/telemetry/mcp-calls", headers=_EVIL).status_code == 403


# ------------------------------------------------ 分页 / since_ts / legacy 键

def _v1_rows(n: int, *, ts0: str = "2026-08-01T00:00:%02d.000Z") -> list:
    return [
        {"schema": "biodata-mcp-calls/v1", "call_id": chr(ord("a") + i) * 32,
         "ts": ts0 % i, "tool": f"tool_{i}"}
        for i in range(n)
    ]


def test_telemetry_mcp_calls_limit_truncation_offset_semantics(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    _write_log(log, [json.dumps(r, ensure_ascii=False) + "\n" for r in _v1_rows(5)])
    # limit=2：只回前 2 条，truncated=true，next_offset=2（只到最后一条已消费行）
    body = client.get("/api/telemetry/mcp-calls", params={"limit": 2}).json()
    assert [r["tool"] for r in body["records"]] == ["tool_0", "tool_1"]
    assert body["truncated"] is True and body["next_offset"] == 2
    # 续拉：从 after=2 起拿剩余 3 条，truncated=false
    body = client.get("/api/telemetry/mcp-calls", params={"after": 2, "limit": 2}).json()
    assert [r["tool"] for r in body["records"]] == ["tool_2", "tool_3"]
    assert body["truncated"] is True and body["next_offset"] == 4
    body = client.get("/api/telemetry/mcp-calls", params={"after": 4, "limit": 2}).json()
    assert [r["tool"] for r in body["records"]] == ["tool_4"]
    assert body["truncated"] is False and body["next_offset"] == 5
    # 非法 limit → 400
    assert client.get("/api/telemetry/mcp-calls", params={"limit": 0}).status_code == 400
    assert client.get("/api/telemetry/mcp-calls", params={"limit": 201}).status_code == 400


def test_telemetry_mcp_calls_max_bytes_truncation(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    rows = _v1_rows(4)
    lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in rows]
    _write_log(log, lines)
    budget = len(lines[0].encode("utf-8")) + len(lines[1].encode("utf-8"))
    body = client.get("/api/telemetry/mcp-calls", params={"max_bytes": budget}).json()
    assert [r["tool"] for r in body["records"]] == ["tool_0", "tool_1"]
    assert body["truncated"] is True and body["next_offset"] == 2
    # 非法 max_bytes → 400
    assert client.get("/api/telemetry/mcp-calls", params={"max_bytes": 0}).status_code == 400
    assert client.get("/api/telemetry/mcp-calls", params={"max_bytes": 3 * 1024 * 1024}).status_code == 400


def test_telemetry_mcp_calls_since_ts_filtering(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    old = {"schema": "biodata-mcp-calls/v1", "call_id": "a" * 32, "ts": "2026-08-01T00:00:00.000Z", "tool": "old_tool"}
    legacy_no_ts = {"schema": "biodata-mcp-calls/v0", "tool": "legacy_tool"}  # 无 ts 无 call_id
    new = {"schema": "biodata-mcp-calls/v1", "call_id": "c" * 32, "ts": "2026-08-20T00:00:00.000Z", "tool": "new_tool"}
    _write_log(log, [json.dumps(r, ensure_ascii=False) + "\n" for r in (old, legacy_no_ts, new)])
    body = client.get("/api/telemetry/mcp-calls", params={"since_ts": "2026-08-10T00:00:00Z"}).json()
    tools = [r["tool"] for r in body["records"]]
    # old 被滤掉（视为已消费）；无 ts 的 legacy 行保留；new 保留
    assert tools == ["legacy_tool", "new_tool"]
    assert body["next_offset"] == 3  # 滤掉的行 offset 照常前进
    # 非法 since_ts → 400
    assert client.get("/api/telemetry/mcp-calls", params={"since_ts": "not-a-date"}).status_code == 400


def test_telemetry_mcp_calls_legacy_lines_get_stable_call_id(monkeypatch, tmp_path):
    log, _cursor = _redirect_userdata(monkeypatch, tmp_path)
    legacy = json.dumps({"schema": "biodata-mcp-calls/v0", "tool": "recommend_datasets"}, ensure_ascii=False)
    _write_log(log, [legacy + "\n"])
    body1 = client.get("/api/telemetry/mcp-calls").json()
    rec = body1["records"][0]
    assert rec["legacy"] is True
    assert rec["call_id"].startswith("legacy-") and len(rec["call_id"]) == len("legacy-") + 32
    # 同一行两次拉取合成键逐位一致（接收端幂等去重的前提）
    body2 = client.get("/api/telemetry/mcp-calls").json()
    assert body2["records"][0]["call_id"] == rec["call_id"]
    # 合成键确为 sha256(行原文.strip()) 前缀
    import hashlib
    assert rec["call_id"] == "legacy-" + hashlib.sha256(legacy.strip().encode("utf-8")).hexdigest()[:32]
    # 有 call_id 的 v1 行原样透传、不标 legacy
    _write_log(log, [json.dumps({"schema": "biodata-mcp-calls/v1", "call_id": "d" * 32, "tool": "x"}) + "\n"])
    body3 = client.get("/api/telemetry/mcp-calls").json()
    assert body3["records"][0]["call_id"] == "d" * 32
    assert "legacy" not in body3["records"][0]


# ---------------------------------------------------------------- POST /api/telemetry/mcp-calls/ack

def test_telemetry_ack_persists_cursor_idempotently(monkeypatch, tmp_path):
    _log, cursor = _redirect_userdata(monkeypatch, tmp_path)
    r1 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 5})
    assert r1.status_code == 200 and r1.json() == {"ok": True, "offset": 5}
    value = json.loads(cursor.read_text(encoding="utf-8"))
    assert value["schema"] == "biodata-mcp-upload-cursor/v1"
    assert value["offset"] == 5
    assert value["updated_at"]
    # 幂等：重复 ack 覆盖，不报错
    r2 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 8})
    assert r2.status_code == 200 and r2.json() == {"ok": True, "offset": 8}
    assert json.loads(cursor.read_text(encoding="utf-8"))["offset"] == 8
    assert not (cursor.parent / "mcp_calls_uploaded.json.tmp").exists()


def test_telemetry_ack_rejects_bad_offset(monkeypatch, tmp_path):
    _redirect_userdata(monkeypatch, tmp_path)
    assert client.post("/api/telemetry/mcp-calls/ack", json={"offset": -1}).status_code == 422
    assert client.post("/api/telemetry/mcp-calls/ack", json={}).status_code == 422


def test_telemetry_ack_cas_never_moves_cursor_backwards(monkeypatch, tmp_path):
    """CAS：游标只前进——回退 ack 不落盘，响应恒为 max(请求, 已存)。"""
    _log, cursor = _redirect_userdata(monkeypatch, tmp_path)
    r1 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 10})
    assert r1.status_code == 200 and r1.json() == {"ok": True, "offset": 10}
    mtime_payload = cursor.read_text(encoding="utf-8")
    # 回退请求：视为已达成——响应报已存值，盘上不动
    r2 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 4})
    assert r2.status_code == 200 and r2.json() == {"ok": True, "offset": 10}
    assert json.loads(cursor.read_text(encoding="utf-8"))["offset"] == 10
    assert cursor.read_text(encoding="utf-8") == mtime_payload  # 回退未落盘（连 updated_at 都没动）
    # 持平请求：同样不落盘
    r3 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 10})
    assert r3.json() == {"ok": True, "offset": 10}
    assert cursor.read_text(encoding="utf-8") == mtime_payload
    # 前进：正常落盘
    r4 = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 12})
    assert r4.json() == {"ok": True, "offset": 12}
    assert json.loads(cursor.read_text(encoding="utf-8"))["offset"] == 12


def test_telemetry_ack_tolerates_corrupt_cursor_file(monkeypatch, tmp_path):
    """CAS：游标文件损坏按 0 计，本次 ack 照旧推进修复。"""
    _log, cursor = _redirect_userdata(monkeypatch, tmp_path)
    cursor.parent.mkdir(parents=True, exist_ok=True)
    cursor.write_text("{corrupted", encoding="utf-8")
    r = client.post("/api/telemetry/mcp-calls/ack", json={"offset": 7})
    assert r.status_code == 200 and r.json() == {"ok": True, "offset": 7}
    assert json.loads(cursor.read_text(encoding="utf-8"))["offset"] == 7


def test_telemetry_ack_rejects_evil_origin(monkeypatch, tmp_path):
    _redirect_userdata(monkeypatch, tmp_path)
    assert client.post("/api/telemetry/mcp-calls/ack", json={"offset": 1}, headers=_EVIL).status_code == 403


# ---------------------------------------------------------------- GET /api/guide/agent-prompt

def test_guide_agent_prompt_returns_markdown(monkeypatch, tmp_path):
    prompt_dir = tmp_path / "使用教程" / "MCP安装"
    prompt_dir.mkdir(parents=True)
    prompt_dir.joinpath("agent接入提示词.md").write_text("# 接入提示词\n请帮我接入 BioData MCP。\n", encoding="utf-8")
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    r = client.get("/api/guide/agent-prompt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "请帮我接入 BioData MCP" in r.text


def test_guide_agent_prompt_missing_file_404(monkeypatch, tmp_path):
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)  # 空目录
    assert client.get("/api/guide/agent-prompt").status_code == 404


def test_guide_agent_prompt_rejects_evil_origin(monkeypatch, tmp_path):
    prompt_dir = tmp_path / "使用教程" / "MCP安装"
    prompt_dir.mkdir(parents=True)
    prompt_dir.joinpath("agent接入提示词.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    assert client.get("/api/guide/agent-prompt", headers=_EVIL).status_code == 403


# ---------------------------------------------------------------- GET /api/guide/online-prompt

def test_guide_online_prompt_returns_markdown(monkeypatch, tmp_path):
    """在线接入提示词模板（2026-08-30 任务2）：占位符随模板全文下发，前端铸币后代入真实值。"""
    prompt_dir = tmp_path / "使用教程" / "MCP安装"
    prompt_dir.mkdir(parents=True)
    prompt_dir.joinpath("在线接入提示词模板.md").write_text(
        "URL=__BIODATA_MCP_URL__ T=__BIODATA_MCP_TOKEN__\n", encoding="utf-8")
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    r = client.get("/api/guide/online-prompt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "__BIODATA_MCP_URL__" in r.text and "__BIODATA_MCP_TOKEN__" in r.text


def test_guide_online_prompt_missing_file_404(monkeypatch, tmp_path):
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)  # 空目录
    assert client.get("/api/guide/online-prompt").status_code == 404


def test_guide_online_prompt_rejects_evil_origin(monkeypatch, tmp_path):
    prompt_dir = tmp_path / "使用教程" / "MCP安装"
    prompt_dir.mkdir(parents=True)
    prompt_dir.joinpath("在线接入提示词模板.md").write_text("x", encoding="utf-8")
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    assert client.get("/api/guide/online-prompt", headers=_EVIL).status_code == 403


# ---------------------------------------------------------------- GET /api/guide/skill.zip

def _fake_skill_dir(tmp_path: Path) -> Path:
    skill_dir = tmp_path / ".agents" / "skills" / "biodata-dataset-discovery"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: biodata-dataset-discovery\n---\n", encoding="utf-8")
    (skill_dir / "references" / "honesty-invariants.md").write_text("honesty", encoding="utf-8")
    return skill_dir


def test_guide_skill_zip_bundles_skill_folder(monkeypatch, tmp_path):
    _fake_skill_dir(tmp_path)
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    r = client.get("/api/guide/skill.zip")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/zip")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "biodata-dataset-discovery.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        names = set(zf.namelist())
        assert "biodata-dataset-discovery/SKILL.md" in names
        assert "biodata-dataset-discovery/references/honesty-invariants.md" in names
        content = zf.read("biodata-dataset-discovery/SKILL.md").decode("utf-8")
        assert "name: biodata-dataset-discovery" in content   # 行尾无关（Windows CRLF）


def test_guide_skill_zip_missing_dir_404(monkeypatch, tmp_path):
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    assert client.get("/api/guide/skill.zip").status_code == 404


def test_guide_skill_zip_deterministic_bytes_and_sha256_header(monkeypatch, tmp_path):
    """同一棵目录树两次构建字节完全一致；X-SHA256 == 内容 sha256。"""
    import hashlib
    _fake_skill_dir(tmp_path)
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    r1 = client.get("/api/guide/skill.zip")
    r2 = client.get("/api/guide/skill.zip")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.content == r2.content  # 确定性：固定 date_time/排序/权限位
    digest = hashlib.sha256(r1.content).hexdigest()
    assert r1.headers.get("x-sha256") == digest
    assert r2.headers.get("x-sha256") == digest
    # 条目按 arcname 排序、元数据固定（可复现性的另一面）
    with zipfile.ZipFile(io.BytesIO(r1.content)) as zf:
        infos = zf.infolist()
        assert [i.filename for i in infos] == sorted(i.filename for i in infos)
        assert all(i.date_time == (1980, 1, 1, 0, 0, 0) for i in infos)


def test_guide_skill_zip_rejects_evil_origin(monkeypatch, tmp_path):
    _fake_skill_dir(tmp_path)
    monkeypatch.setattr("dataset_recommender.app.webapp.RESOURCE_ROOT", tmp_path)
    assert client.get("/api/guide/skill.zip", headers=_EVIL).status_code == 403
