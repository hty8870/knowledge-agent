# -*- coding: utf-8 -*-
"""MCP 非法请求 → isError=true 契约（试用反馈驱动）+ 候选 caveats 露出。

v1.3.0：非正 top_k / 未知来源 / 空 query / 坏 uid 等**非法请求**由「ok=false dict」改为 raise
ToolError → 客户端拿到协议错误位 isError=true（消息带机器码+提示）。**合法业务结果**（无匹配/
弃权/澄清）仍 ok=true、isError=false。含一条**真 stdio 协议**测试（QUALITY_GATES 要求），
证明非法调用 isError=true 且进程仍存活（永不崩）。"""
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from dataset_recommender.app import mcp_server as M


# ---------- 单测：非法请求 raise ----------
@pytest.mark.parametrize("tk", [-1, 0])
def test_non_positive_top_k_raises(tk):
    """top_k=-1/0 此前被 retriever max(1,) 静默钳成 1 条；现边界显式报 bad_param。"""
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("人类肺癌", top_k=tk)


def test_non_positive_rerank_top_n_raises():
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("人类肺癌", rerank_top_n=-3)


def test_retired_llm_bypass_parameters_are_rejected():
    """查询改写/动作判断只走 Agent；MCP 不再接受三条环外 LLM 旁路参数。"""
    for name in ("rerank_audit", "degrade_with_llm", "action_audit"):
        with pytest.raises(TypeError, match=name):
            M.recommend_datasets("人类肺癌", **{name: True})


def test_unknown_source_raises_bad_source():
    """未知来源此前被静默过滤成 0 命中（与真·无命中难分）；现报 bad_source。"""
    with pytest.raises(ToolError, match="bad_source"):
        M.recommend_datasets("人类肺癌", sources=["NotARealSource"])


def test_mixed_unknown_source_raises():
    """混入未知来源（此前静默只用好的）也报错，不放过写错的来源名。"""
    with pytest.raises(ToolError, match="bad_source"):
        M.recommend_datasets("人类肺癌", sources=["10x Genomics", "NotARealSource"])


@pytest.mark.parametrize("srcs", [[""], ["   "], ["", "10x Genomics"]])
def test_blank_source_raises_bad_source(srcs):
    """空/空白来源名（sources=[''] 等）此前被静默滤除→回退默认结果（与真按来源筛难分）；
    现报 bad_source。注意：sources=[] / None 仍是「不过滤=默认」，不算错误（见下）。"""
    with pytest.raises(ToolError, match="bad_source"):
        M.recommend_datasets("人类肺癌", sources=srcs)


def test_empty_sources_list_is_default_not_error():
    """sources=[] 等同省略（None）：不按来源过滤、用默认 10x，正常返回——空列表不是误传。"""
    r = M.recommend_datasets("人类肺癌", sources=[], top_k=3)
    assert r["ok"] is True and r["returned"] >= 1


@pytest.mark.parametrize("q", ["人类\x00肺癌", "人类肺癌\x07", "\x1b[2J人类肺癌"])
def test_control_char_query_raises_bad_query(q):
    """含 NUL/控制字符的 query 此前被静默带过；现报 bad_query，不让脏输入穿过解析链。"""
    with pytest.raises(ToolError, match="bad_query"):
        M.recommend_datasets(q)


def test_oversized_query_raises_bad_query():
    """超长 query（>2000 字符）此前无上限；现报 bad_query。"""
    with pytest.raises(ToolError, match="bad_query"):
        M.recommend_datasets("人" * 3000)


def test_normal_whitespace_in_query_is_ok():
    """制表/换行/普通空格是合法查询字符，不得被控制字符校验误伤。"""
    r = M.parse_constraints("人类\t肺癌\n单细胞")
    assert r["ok"] is True


@pytest.mark.parametrize("tk", [101, 999999999])
def test_top_k_over_cap_raises(tk):
    """top_k 硬上限 100：防响应放大（验证：top_k=999999999→155万字符）。"""
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("人类肺癌", top_k=tk)


def test_rerank_top_n_over_cap_raises():
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("人类肺癌", rerank_top_n=500)


@pytest.mark.parametrize("q", ["!!!???，。；", "🧬🧫🔬", "   ...   ", "———"])
def test_no_content_query_raises_bad_query(q):
    """纯符号/emoji/无字母数字汉字的查询此前判 executable 返回通用结果 → 拒绝（验证）。"""
    with pytest.raises(ToolError, match="bad_query"):
        M.recommend_datasets(q)


@pytest.mark.parametrize("q", ["人类​肺癌", "人类‮肺癌", "﻿人类肺癌"])
def test_invisible_or_bidi_char_query_raises_bad_query(q):
    """零宽空格 U+200B / 双向控制 U+202E / BOM U+FEFF 等不可见字符 → 拒绝（验证）。"""
    with pytest.raises(ToolError, match="bad_query"):
        M.recommend_datasets(q)


def test_uid_with_surrounding_whitespace_is_trimmed():
    """合法 uid 前后带空格（复制粘贴常见）此前误报 bad_uid；现自动 strip 解析（验证）。"""
    r = M.recommend_datasets("人类肺癌", top_k=1)
    uid = r["candidates"][0]["dataset_uid"]
    m = M.get_file_manifest(f"  {uid}  ")
    assert m["ok"] is True and m["dataset_uid"] == uid


def test_empty_uid_raises():
    with pytest.raises(ToolError, match="empty_uid"):
        M.get_file_manifest("   ")


def test_parse_constraints_empty_raises():
    with pytest.raises(ToolError, match="empty_query"):
        M.parse_constraints("")


# ---------- 合法请求仍 ok=true ----------
def test_valid_source_ok():
    r = M.recommend_datasets("人类肺癌", sources=["10x Genomics"], top_k=3)
    assert r["ok"] is True and r["returned"] >= 1


def test_candidates_carry_caveats_key():
    """每条候选都带 caveats（数据一致性建议信号；空=无问题）——加性字段，恒在。"""
    r = M.recommend_datasets("人类肾脏", sources=["10x Genomics", "CELLxGENE Discover"], top_k=8)
    assert r["ok"] is True
    assert all(isinstance(c.get("caveats"), list) for c in r["candidates"])


def test_relative_date_query_ok_via_mcp():
    """相对时间经 MCP 端可解析（#1 在接口层的回归）：近三年 → 具体区间、ok=true。"""
    r = M.recommend_datasets("近三年的人类数据", top_k=3)
    assert r["ok"] is True
    u = r["understood"]
    assert u["date_from"] and u["date_to"] and u["parse_status"] == "executable"


def test_source_and_time_auto_parse_via_mcp_without_manual_source_name():
    r = M.recommend_datasets("CELLxGENE Discover 2020年以后的人类肝脏数据", top_k=3)
    assert r["ok"] is True and r["candidates"]
    assert r["interpretation"]["effective_sources"] == ["CELLxGENE Discover"]
    assert r["understood"]["date_from"] == "2020-01-01"
    assert {x.get("source") for x in r["candidates"]} == {"CELLxGENE Discover"}
    assert r["meta"]["search_trace"]["steps"]


def test_parse_constraints_uses_same_source_resolution_as_recommend():
    r = M.parse_constraints("Human Cell Atlas 近三年的人类数据")
    u = r["understood"]
    assert u["source_resolution"]["detected_sources"] == ["Human Cell Atlas"]
    assert u["effective_sources"] == ["Human Cell Atlas"]
    assert u["date_from"] and u["date_to"]


def test_source_negation_guard_does_not_reverse_into_named_source():
    u = M.parse_constraints("除了 10x Genomics 以外的人类数据")["understood"]
    assert u["source_resolution"]["automatic_skipped_reason"] == "source_negation_guard"
    assert u["effective_sources"] == ["10x Genomics"]


# ---------- 真 stdio 协议：isError=true + 进程存活（QUALITY_GATES 设计约定）----------
def test_real_protocol_invalid_request_sets_isError_and_survives():
    import asyncio
    import os
    import sys
    from pathlib import Path

    st = M.biodata_status()
    if not (st.get("ok") and st.get("corpus_total", 0) > 0):
        pytest.skip("库存未就绪，跳过真协议测试")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> tuple[bool, str, bool]:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-B", str(Path(M.__file__).resolve())],
            env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                bad = await session.call_tool("recommend_datasets", {"query": "人类肺癌", "top_k": -1})
                text = bad.content[0].text if bad.content else ""
                # 未知参数名（extra=forbid）与 "3"/true 强转（StrictInt）只在协议/pydantic 层生效，须真协议验。
                typo = await session.call_tool("recommend_datasets", {"query": "人类肺癌", "topk": 1})
                coerce = await session.call_tool("recommend_datasets", {"query": "人类肺癌", "top_k": "3"})
                # 非法请求后再调 status：进程必须仍活着且无错（永不崩）
                ok = await session.call_tool("biodata_status", {})
                return {
                    "bad": (bool(bad.isError), text),
                    "typo": bool(typo.isError),
                    "coerce": bool(coerce.isError),
                    "status": bool(ok.isError),
                }

    res = asyncio.run(asyncio.wait_for(_run(), timeout=90))
    assert res["bad"][0] is True, "非法 top_k 应置 isError=true"
    assert "bad_param" in res["bad"][1]
    assert res["typo"] is True, "未知参数名 topk 应触发校验错误 isError=true（extra=forbid）"
    assert res["coerce"] is True, 'top_k="3" 字符串应被 StrictInt 拒绝、isError=true（不再强转成 3）'
    assert res["status"] is False, "非法调用后进程应仍存活、status 不报错（永不崩）"


# ---------- v1.9.0 新工具真 stdio 协议：合法 ok=true / 非法 isError=true / 进程存活 ----------
def test_real_protocol_new_tools_usable_and_stable():
    """真 MCP 协议下端到端验证新增能力（稳定性/可用性）：browse_datasets / get_dataset_introduction /
    recommend_datasets(facet_filters) 合法调用 isError=false 且结构正确；非法调用 isError=true；
    穿插非法调用后 biodata_status 仍存活（永不崩）。"""
    import asyncio
    import json
    import os
    import sys
    from pathlib import Path

    st = M.biodata_status()
    if not (st.get("ok") and st.get("corpus_total", 0) > 0 and st.get("download_index_ready")):
        pytest.skip("库存/下载索引未就绪，跳过新工具真协议测试")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _run() -> dict:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        params = StdioServerParameters(
            command=sys.executable, args=["-B", str(Path(M.__file__).resolve())], env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}

                br = await session.call_tool("browse_datasets", {"limit": 3})
                br_payload = json.loads(br.content[0].text) if br.content else {}
                br_bad = await session.call_tool("browse_datasets", {"limit": 0})

                # 用一次 recommend 拿真实 uid，喂给 get_dataset_introduction
                rec = await session.call_tool("recommend_datasets", {"query": "人类肺癌单细胞", "top_k": 1})
                rec_payload = json.loads(rec.content[0].text) if rec.content else {}
                uid = (rec_payload.get("candidates") or [{}])[0].get("dataset_uid", "")
                intro = await session.call_tool("get_dataset_introduction", {"uid": uid})
                intro_payload = json.loads(intro.content[0].text) if intro.content else {}
                intro_empty = await session.call_tool("get_dataset_introduction", {})

                # 分面细化经真协议透传
                facets = rec_payload.get("facets") or []
                ff = None
                for g in facets:
                    if g.get("values"):
                        ff = {"dim": g["dim"], "value": g["values"][0]["value"]}
                        break
                rec_ff = await session.call_tool(
                    "recommend_datasets",
                    {"query": "人类肺癌单细胞", "top_k": 20, "facet_filters": [ff] if ff else []},
                )
                rec_ff_payload = json.loads(rec_ff.content[0].text) if rec_ff.content else {}

                ok = await session.call_tool("biodata_status", {})
                return {
                    "names": names,
                    "browse_ok": (not br.isError) and br_payload.get("ok") is True and "facets" in br_payload,
                    "browse_bad": bool(br_bad.isError),
                    "intro_ok": (not intro.isError) and intro_payload.get("ok") is True,
                    "intro_empty_err": bool(intro_empty.isError),
                    "rec_ff_ok": (not rec_ff.isError) and rec_ff_payload.get("applied_facets") == ([ff] if ff else []),
                    "status_ok": not ok.isError,
                }

    res = asyncio.run(asyncio.wait_for(_run(), timeout=120))
    assert {"browse_datasets", "get_dataset_introduction"} <= res["names"], "新工具应在 tools/list 可见"
    assert res["browse_ok"], "browse_datasets 合法调用应 ok=true 且带 facets"
    assert res["browse_bad"], "browse_datasets limit=0 应 isError=true"
    assert res["intro_ok"], "get_dataset_introduction(uid) 合法调用应 ok=true"
    assert res["intro_empty_err"], "get_dataset_introduction() 空键应 isError=true"
    assert res["rec_ff_ok"], "recommend_datasets facet_filters 应经真协议透传并回显 applied_facets"
    assert res["status_ok"], "穿插非法调用后 biodata_status 仍应存活、不报错（永不崩）"


# ------ v1.10.0 upload_dataset 真 stdio 协议：合法写 ok=true / 非法 isError=true / 进程存活 ------
def test_real_protocol_upload_dataset_usable_and_stable():
    """真 MCP 协议下端到端验证唯一写工具 upload_dataset（可用性/稳定性/隔离）：
    合法上传 ok=true 且落 database/external/（upload_ 前缀、逐条打标 source）；空数组→no_records、
    无输入→empty_input 均 isError=true；穿插非法调用后 biodata_status 仍存活（永不崩）。
    **精确清理**：唯一 marker 命名 + finally 删除本次落盘文件，绝不污染真实 database/external/。"""
    import asyncio
    import json
    import os
    import sys
    import uuid
    from pathlib import Path

    st = M.biodata_status()
    if not (st.get("ok") and st.get("corpus_total", 0) > 0 and st.get("download_index_ready")):
        pytest.skip("库存/下载索引未就绪，跳过 upload 真协议测试")

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    marker = f"mcp_stdio_upload_{uuid.uuid4().hex}"
    ext_dir = M._settings().project_root / "database" / "external"

    async def _run() -> dict:
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        params = StdioServerParameters(
            command=sys.executable, args=["-B", str(Path(M.__file__).resolve())], env=env,
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}

                # records 传**结构化数组**（不是 JSON 字符串——那样会被框架 pre_parse 成 list、与 str 冲突）。
                up = await session.call_tool(
                    "upload_dataset",
                    {
                        "records": [{"dataset_name": f"{marker} 数据集", "species": "Human", "url": "https://e/x"}],
                        "filename": f"{marker}.json",
                        "source": marker,
                    },
                )
                up_payload = json.loads(up.content[0].text) if (up.content and not up.isError) else {}
                # 非法：空数组 → no_records；无输入 → empty_input（均 isError=true，且不落盘）
                up_bad = await session.call_tool("upload_dataset", {"records": [], "filename": "x.json"})
                up_empty = await session.call_tool("upload_dataset", {})

                ok = await session.call_tool("biodata_status", {})
                return {
                    "names": names,
                    "up_ok": (not up.isError) and up_payload.get("ok") is True,
                    "saved_to": up_payload.get("saved_to", ""),
                    "record_count": up_payload.get("record_count"),
                    "sources": up_payload.get("sources", {}),
                    "up_bad": bool(up_bad.isError),
                    "up_empty": bool(up_empty.isError),
                    "status_ok": not ok.isError,
                }

    try:
        res = asyncio.run(asyncio.wait_for(_run(), timeout=120))
    finally:
        # 精确清理：只删本次 marker 落盘文件，绝不动其它外部库快照。
        if ext_dir.exists():
            for p in ext_dir.glob(f"upload_*{marker}*.json"):
                try:
                    p.unlink()
                except OSError:
                    pass

    assert "upload_dataset" in res["names"], "upload_dataset 应在 tools/list 可见"
    assert res["up_ok"], "upload_dataset 合法上传应 ok=true"
    assert res["saved_to"].startswith("database/external/"), "上传必须落 database/external/（绝不进 base）"
    assert res["saved_to"].endswith(f"{marker}.json"), "落盘名应保留原文件名叶子"
    assert res["record_count"] == 1 and res["sources"] == {marker: 1}, "逐条打标 source + 计数正确"
    assert res["up_bad"], "upload_dataset 空数组应 no_records → isError=true"
    assert res["up_empty"], "upload_dataset 无输入应 empty_input → isError=true"
    assert res["status_ok"], "写工具穿插非法调用后 biodata_status 仍应存活（永不崩）"
