# -*- coding: utf-8 -*-
"""机械端点契约门：每个 /api 端点的响应必须带上 MODULES.md 承诺、前端/MCP 消费的字段。

**为什么需要它**：`scripts/web_smoke_test.py` 只对前端 JS 做**静态
字符串**检查、从不执行任何一行 JS。改了后端响应字段名却漏改前端消费点 → 三门全绿、浏览器**静默
空白**（同类事故记过多次）。这道门用**真实响应**（FastAPI TestClient）断言每个必需键存在，
把这类静默契约打断变成红灯。

**设计（收敛自四方 + 自审）**：契约是本文件里的**显式声明**（单一机器可核真源），与 MODULES.md
「`/api/recommend` 响应字段 → 前端消费点映射」人读版一一对应。**刻意不从 MODULES.md 解析生成**——
那会把文档漂移搬进生成器（实现「matrix 保证不了任何东西」的反面是「matrix 生成守卫」，但守卫要
自己稳）。断言 `required ⊆ actual`：**删/改**字段名立刻红；**加**字段不误伤（加字段是有意的，届时
同步扩这里）。加/改任一端点响应字段 → 必须同步改本文件与 MODULES.md 那张表。
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (str(_ROOT), str(_ROOT / "src")):
    # /api/files 等端点在调用期做裸 `from dataset_recommender import ...`，需要 src 在 path 上；
    # MCP 平价断言导入 `dataset_recommender.app.mcp_server`（入包）同样需要 src。
    if _p not in sys.path:
        sys.path.insert(0, _p)

from fastapi.testclient import TestClient

from dataset_recommender.app import webapp as webapp_module
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")


def _missing(required, actual):
    return sorted(set(required) - set(actual))


# ---- 契约声明（与 MODULES.md「字段→前端消费点映射」对应）----

# /api/recommend 顶层：前端/MCP 有文档消费点的字段（顺序无关）。
RECOMMEND_TOP = {
    "ok", "results", "resolution_status", "result_total",
    "facets", "relaxation_options", "query_constraints",
    "coverage_caveats", "applied_lenient", "unused_query_terms",   # 诚实层三件套
    "identifier_lookup",                                            # 标识符精确反查（非标识符时为 null）
    "applied_facets", "applied_suppressed",
    # 规则模型新增的两个字段。本文件自己写明「加/改任一端点响应字段 → 必须
    # 同步改本文件」——漏同步时 API 层会把它们整个丢掉，全量套件仍然全绿。
    "degraded_search",      # 未收录词弃权时的降级建议（无建议时为 null，键必须在）
    "action_markers",       # 执行类说法回显（没说时为空数组，键必须在）
    "or_handling",          # 「A 或 B」实际怎么执行的（没写「或」时为空 dict，键必须在）
    "interpretation", "search_trace", "markdown", "strategy",
    "clarification", "warnings", "pipeline",
    "llm_response_used", "provider", "fallback", "fallback_reason",
    "policy_id", "policy_id_str",  # 结构体 + 稳定紧凑串；组装失败均为 null
    "experiment",                   # 完整实验三件套；普通流量为 null
}

# results[] 每条卡片：改任一字段名 → cards.js 对应位静默空白（见 MODULES.md「/api/recommend 响应字段 → 前端消费点映射」）。
RECOMMEND_CARD = {
    "dataset_name", "species", "tissue", "disease", "chemistry", "platform",
    "assay", "sample_size", "gene_count", "raw_data_status", "published_date", "source",
    "url", "download_url", "dataset_uid", "n_files", "reason",
    "reachability",   # 国内可达性启发（非实测；值可能 None，键必在）
}

# introduction dict：cards.js renderIntroduction / MCP get_dataset_introduction 共用（含样本量提醒字段）。
INTRO_KEYS = {"summary", "facts", "caveat", "sample_size_caveats", "summary_source", "source_label"}

# 其它端点
DATASETS_TOP = {"ok", "records", "facets", "count", "unknown_year_count"}
SOURCES_TOP = {"ok", "sources"}
FAIR_TOP = {"ok", "fair_report"}
FILES_TOP = {"ok", "files", "count"}
HEALTH_TOP = {"ok", "version", "install_root"}

# /api/board/plan 顶层：board.js 每个状态都读同一批键，缺一个就得靠 try/except 判断状态。
BOARD_PLAN_TOP = {
    "ok", "schema", "status", "op", "dim", "message", "detail",
    "next_request", "removed_text", "dropped_terms", "verify",
    "choices", "suggestions", "board_view", "echoed",
}
# next_request 是直接拿去调 /api/recommend 的，字段名必须与 RecommendRequest 对得上。
BOARD_NEXT_REQUEST = {
    "query", "suppressed_constraints", "lenient_dims", "facet_filters", "date_from", "date_to",
}


def _recommend(query, **extra):
    body = {"query": query, "sources": ["10x Genomics"], "use_llm": False}
    body.update(extra)
    return client.post("/api/recommend", json=body).json()


def test_recommend_results_shape():
    r = _recommend("人类肺癌数据")
    assert r["resolution_status"] == "results" and r["results"]
    assert not _missing(RECOMMEND_TOP, r), f"/api/recommend 顶层缺字段：{_missing(RECOMMEND_TOP, r)}"
    for i, item in enumerate(r["results"]):
        assert not _missing(RECOMMEND_CARD, item), f"results[{i}] 缺卡片字段：{_missing(RECOMMEND_CARD, item)}"


def test_recommend_zero_result_keeps_same_top_shape():
    """0 结果/弃权时顶层形状必须一致——前端空态分流靠这些键，缺了就静默空白。"""
    r = _recommend("火星章鱼组织")
    # 2026-09-12 起：解析弃权不再沉默——vector_fallback（全库语义召回+未经条件核验标注）
    # 加入合法状态集；嵌入器不可用的环境才回退 abstained。
    assert r["resolution_status"] in ("no_match", "abstained", "clarification_required", "vector_fallback")
    assert not _missing(RECOMMEND_TOP, r), f"0 结果顶层缺字段：{_missing(RECOMMEND_TOP, r)}"


def test_recommend_policy_id_shape_is_stable():
    """：policy_id 是前端遥测包的检索配置锚——形状漂移=分析端静默断链。"""
    r = _recommend("人类肺癌数据")
    pid = r["policy_id"]
    assert pid is not None, "policy_id 组装失败降级 null（应只在语料装载异常时发生）"
    assert pid["schema"] == "biodata-policy-id/1"
    assert set(pid) == {"schema", "corpus", "sources", "ranking", "model",
                        "app_version", "router_version"}
    assert not _missing({"snapshot_id", "n_records"}, pid["corpus"])
    assert pid["corpus"]["snapshot_id"] and isinstance(pid["corpus"]["n_records"], int)
    assert not _missing({"strategy", "rerank", "recall"}, pid["ranking"])
    assert pid["sources"] == ["10x Genomics"]  # _recommend 固定传该来源
    assert pid["app_version"] and pid["router_version"] == "turn-route/v1"
    assert r["policy_id_str"].startswith("bpol1:snap=")
    assert ";strategy=" in r["policy_id_str"] and ";h=" in r["policy_id_str"]
    # 同参数再查一次：snapshot_id 逐位一致（内容寻址，非时间戳）
    r2 = _recommend("人类肺癌数据")
    assert r2["policy_id"]["corpus"]["snapshot_id"] == pid["corpus"]["snapshot_id"]
    assert r2["policy_id_str"] == r["policy_id_str"]


def test_utterance_search_route_carries_policy_id():
    """：/api/utterance route=="search" 时 policy_id 随响应走（agent 关、规则直达，
    零网络）；非 search 路线不带该键。"""
    res = client.post("/api/utterance", json={"utterance": "找人类肺癌数据"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["route"] == "search", body
    pid = body.get("policy_id")
    assert pid is not None and pid["schema"] == "biodata-policy-id/1"
    assert pid["ranking"]["strategy"] in ("fixed", "auto")
    assert body["policy_id_str"].startswith("bpol1:")


def test_coverage_caveats_item_shape_when_present():
    """诚实降级 caveat 若非空，每项必须有 dim/label/count/by_source（results.js 据此渲染）。"""
    r = _recommend("人类肺癌免疫细胞", sources=["10x Genomics", "cellxgene", "arrayexpress"], lenient_dims=[])
    for cav in r.get("coverage_caveats") or []:
        assert not _missing({"dim", "label", "count", "by_source"}, cav), f"coverage_caveat 缺键：{cav}"


def test_query_constraints_item_shape_when_present():
    r = _recommend("人类肺癌数据")
    for qc in r.get("query_constraints") or []:
        assert not _missing({"filter_id", "polarity", "dim", "label", "values"}, qc), f"query_constraint 缺键：{qc}"


def test_introduction_endpoint_shape():
    r = _recommend("人类肺癌数据")
    uid = r["results"][0]["dataset_uid"]
    gi = client.get("/api/introduction", params={"uid": uid}).json()
    assert gi["ok"] and isinstance(gi["introduction"], dict)
    assert not _missing(INTRO_KEYS, gi["introduction"]), f"introduction 缺字段：{_missing(INTRO_KEYS, gi['introduction'])}"


def test_datasets_sources_fair_files_health_shapes():
    assert not _missing(DATASETS_TOP, client.get("/api/datasets").json())
    assert not _missing(SOURCES_TOP, client.get("/api/sources").json())
    assert not _missing(HEALTH_TOP, client.get("/api/health").json())
    uid = _recommend("人类肺癌数据")["results"][0]["dataset_uid"]
    assert not _missing(FAIR_TOP, client.get("/api/fair", params={"uid": uid}).json())
    assert not _missing(FILES_TOP, client.get("/api/files", params={"uid": uid}).json())


def test_datasets_limit_offset_page_only():
    """limit/offset（additive）只截 records 当前页；count/facets 恒按全库算。"""
    full = client.get("/api/datasets").json()
    page = client.get("/api/datasets", params={"limit": 5, "offset": 10}).json()
    assert not _missing(DATASETS_TOP, page)
    assert len(page["records"]) == 5
    assert page["records"] == full["records"][10:15]
    assert page["count"] == full["count"]
    assert page["facets"] == full["facets"]
    # 只给 offset 等价于尾部切片；越界 offset 给空页而非报错
    assert client.get("/api/datasets", params={"offset": 3}).json()["records"] == full["records"][3:]
    assert client.get("/api/datasets", params={"offset": full["count"] + 100}).json()["records"] == []
    # 非法参数 400，绝不静默忽略
    assert client.get("/api/datasets", params={"limit": 0}).status_code == 400
    assert client.get("/api/datasets", params={"offset": -1}).status_code == 400


def test_datasets_full_response_cache_etag_and_gzip(monkeypatch):
    """默认整拉响应缓存序列化 bytes，ETag 命中 304，GZip 实际显著缩小传输。"""
    webapp_module._reset_datasets_response_cache()
    identity = client.get("/api/datasets", headers={"Accept-Encoding": "identity"})
    assert identity.status_code == 200
    etag = identity.headers.get("ETag")
    assert etag and etag.startswith('W/"')
    assert "no-cache" in identity.headers.get("Cache-Control", "")
    identity_bytes = int(identity.headers["Content-Length"])

    # 缓存命中不应再调展示 item 投影；条件请求直接 304 且无 body。
    monkeypatch.setattr(
        webapp_module, "_web_item_from_record",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("cache miss")),
    )
    not_modified = client.get(
        "/api/datasets", headers={"Accept-Encoding": "gzip", "If-None-Match": etag}
    )
    assert not_modified.status_code == 304 and not not_modified.content
    assert not_modified.headers.get("ETag") == etag

    compressed = client.get("/api/datasets", headers={"Accept-Encoding": "gzip"})
    assert compressed.status_code == 200
    assert compressed.headers.get("Content-Encoding") == "gzip"
    assert "Accept-Encoding" in compressed.headers.get("Vary", "")
    compressed_bytes = int(compressed.headers["Content-Length"])
    assert compressed_bytes < identity_bytes * 0.5
    assert compressed.headers.get("ETag") == etag


def test_html_pages_force_revalidation():
    """HTML 骨架必须每次回源再验证（p11）：缺了 `Cache-Control: no-cache`，浏览器会启发式缓存
    旧骨架——旧挂点 + 新 JS（查询串被 StaticFiles 忽略、照样给新内容）混跑，新功能静默退回旧样式。
    ETag 仍在，未变则 304，成本只是一次往返。"""
    for path in ("/", "/dataset"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "no-cache" in (r.headers.get("Cache-Control") or ""), f"{path} 缺 Cache-Control: no-cache"


def _board_plan(utterance, **extra):
    body = {
        "query": "找人类肺组织的单细胞数据",
        "utterance": utterance,
        "current_filters": _recommend("找人类肺组织的单细胞数据")["query_constraints"],
    }
    body.update(extra)
    return client.post("/api/board/plan", json=body).json()


def test_board_plan_shape_is_identical_across_every_status():
    """六种状态的形状必须完全一样——只有值不同。缺键会逼消费者靠异常判断状态。"""
    cases = {
        "auto_apply": ("去掉组织限制", {}),
        "needs_confirm": ("换成小鼠", {}),
        "needs_choice": ("再加一条：小鼠", {}),
        "not_understood": ("今天天气不错", {}),
        # 载荷用「优先不要 X」：这一档是**永久** fail-closed（系统表达不了软性排除，
        # 做出来就是硬排除）。原来用的「人类或者小鼠的肺数据」自 起可执行了
        #（同维度多值本来就是「或」），拿它当 rejected 的载荷会让这条门空转。
        "rejected": ("换成小鼠", {"candidate_override": "优先不要小鼠的肺数据"}),
        "suggest": ("", {"forced_op": "suggest", "dim": "species"}),
    }
    seen = set()
    for expected, (utterance, extra) in cases.items():
        plan = _board_plan(utterance, **extra)
        assert not _missing(BOARD_PLAN_TOP, plan), f"{expected} 缺字段：{_missing(BOARD_PLAN_TOP, plan)}"
        seen.add(plan["status"])
        assert plan["status"] == expected, f"{utterance!r} 预期 {expected}，实得 {plan['status']}"
        if plan["next_request"] is not None:
            assert not _missing(BOARD_NEXT_REQUEST, plan["next_request"])
        else:
            assert plan["status"] not in ("auto_apply", "needs_confirm")
    assert len(seen) == 6, f"没有覆盖全部六种状态：{sorted(seen)}"


def test_board_next_request_field_names_match_the_recommend_request_model():
    """规划出来的下一步请求要能原样喂给 /api/recommend——字段名对不上就是白规划。"""
    from dataset_recommender.app.webapp import RecommendRequest

    plan = _board_plan("去掉组织限制")
    assert plan["next_request"]
    unknown = set(plan["next_request"]) - set(RecommendRequest.model_fields)
    assert not unknown, f"next_request 里有 /api/recommend 不认识的字段：{sorted(unknown)}"


def test_board_plan_rejects_bad_input_with_400_not_500():
    response = client.post("/api/board/plan", json={"query": "x", "utterance": ""})
    assert response.status_code == 400


def test_web_mcp_introduction_same_source():
    """Web /api/introduction 与 MCP get_dataset_introduction 同源（都走 build_dataset_introduction）：
    两者 introduction 的键集必须一致——这是「mcp 和前端同步」的机械证据。"""
    from dataset_recommender.app import mcp_server as M

    r = _recommend("人类肺癌数据")
    uid = r["results"][0]["dataset_uid"]
    web_intro = client.get("/api/introduction", params={"uid": uid}).json()["introduction"]
    mcp_intro = M.get_dataset_introduction(uid=uid)["introduction"]
    assert set(web_intro.keys()) == set(mcp_intro.keys()), (
        f"Web 与 MCP 的 introduction 字段漂移：web-only={set(web_intro)-set(mcp_intro)} "
        f"mcp-only={set(mcp_intro)-set(web_intro)}"
    )
    assert "sample_size_caveats" in mcp_intro   # 样本量提醒字段必须两侧都有


def test_datasets_limit_single_shared_constant_across_web_and_mcp():
    """ 收口：Web /api/datasets 与 MCP
    browse_datasets 上限必须同源同一常量（值 100），任一入口漂移立即红。"""
    from dataset_recommender.app import mcp_server as M

    from dataset_recommender.app.limits import MAX_DATASETS_LIMIT
    from dataset_recommender.app.webapp import _MAX_DATASETS_LIMIT

    assert MAX_DATASETS_LIMIT == 100
    assert _MAX_DATASETS_LIMIT == MAX_DATASETS_LIMIT
    assert M._MAX_BROWSE_LIMIT == MAX_DATASETS_LIMIT
    # 超限错误语义一致：Web 422 / MCP bad_param（isError=true），阈值同源
    assert client.get("/api/datasets", params={"limit": MAX_DATASETS_LIMIT + 1}).status_code == 422
    import pytest
    from mcp.server.fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="bad_param"):
        M.browse_datasets(limit=MAX_DATASETS_LIMIT + 1)
