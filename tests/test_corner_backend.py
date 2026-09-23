# -*- coding: utf-8 -*-
"""后端离线 corner-case 验证。

**只发现与复现，不改 src/**。每一类钉「不 500 + 诚实口径」的底线；断言写的是
*应然*（诚实方向），跑出红的就是真缺陷，逐条记录待修。

覆盖面：
1. /api/utterance 输入边界（空/纯白/超长/emoji 混排/控制字符/HTML 注入/多换行）；
2. /api/recommend 参数边界（top_k 越界与错型、日期倒挂与非法、未知来源、
   strategy 非法、facet_filters 自相矛盾）；
3. corpus_curation 管护逻辑边界（tmp 项目根直调真源，零网络零真实库写入）；
4. action_plan/turn 边界（limit 钳制、否定极性、quoted 逐字护栏、无 LLM 规则档）；
5. identifiers 形态识别边界（小写 gse、无数字、超大号、DOI 奇异字符与前缀变体）。

TestClient 必须 base_url='http://127.0.0.1'（Host 守卫）；LLM 一律 mock/关，
绝不发真请求（/api/utterance 用 agent=false 走规则直达档，不碰 LLM 分流）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from dataset_recommender.agent import action_plan as ap
from dataset_recommender.corpus import corpus_curation as cc
from dataset_recommender.content import identifiers
from dataset_recommender.agent import turn
from dataset_recommender.app import webapp
from dataset_recommender.llm.llm_client import LLMConfig
from dataset_recommender.app.webapp import app

client = TestClient(app, base_url="http://127.0.0.1")

#: 规则直达档（agent=false）：不碰 LLM 分流、不跑规则匹配概览，纯离线。
_AGENT_OFF = {"agent": False, "provider": "mock", "use_llm": False}


# ====================================================================== 1. /api/utterance 输入边界

def test_utterance_empty_string_rejected():
    """空串：pydantic min_length=1 → 422，不许 500。"""
    res = client.post("/api/utterance", json={"utterance": "", **_AGENT_OFF})
    assert res.status_code == 422


def test_utterance_whitespace_only_rejected_400():
    """纯空白：过了 min_length，normalize_utterance 判 empty_input → 400 人话。"""
    for blank in ("   ", "\n\t \n", "　　　"):  # 含全角空格
        res = client.post("/api/utterance", json={"utterance": blank, **_AGENT_OFF})
        assert res.status_code in (400, 422), blank
        assert res.status_code != 500


def test_utterance_oversized_rejected_not_500():
    """10KB 长文：MAX_UTTERANCE_CHARS=500 → 400 too_large，不许 500 也不许截断硬吞。"""
    res = client.post("/api/utterance", json={"utterance": "肺" * 10240, **_AGENT_OFF})
    assert res.status_code == 400
    assert "太长" in res.json()["detail"] or "too" in res.json()["detail"].lower()


def test_utterance_emoji_cjk_mixed_routes_cleanly():
    """emoji/中日英混排的操作句：规则检出操作词 → 降级气泡（needs_agent），不 500。"""
    res = client.post("/api/utterance",
                      json={"utterance": "把 🧬 人类 lung データ 打包给我", **_AGENT_OFF})
    assert res.status_code == 200
    body = res.json()
    assert body["route"] in ("none", "search", "tool")
    if body["route"] == "none":
        assert body["needs_agent"] is True  # 操作句不许静默当检索


def test_utterance_control_characters_no_500():
    """控制字符（NUL/ESC/BEL 混入）：不许 500；要么 400 要么正常路由。"""
    res = client.post("/api/utterance",
                      json={"utterance": "ab\x00\x1b\x07 cd 人类肺", **_AGENT_OFF})
    assert res.status_code in (200, 400, 422)
    assert res.status_code != 500


def test_utterance_html_injection_not_reflected_as_html():
    """HTML/<script> 注入串：可以按原话路由（JSON 回显合法），但响应必须是 JSON、
    绝不允许以 text/html 形态把注入串吐回去。"""
    payload = "<script>alert(document.cookie)</script>"
    res = client.post("/api/utterance", json={"utterance": payload, **_AGENT_OFF})
    assert res.status_code in (200, 400, 422)
    assert "application/json" in res.headers.get("content-type", "")
    if res.status_code == 200:
        # JSON 反序列化后原样等于输入 = 未做 HTML 渲染态回显（前端 escapeHtml 侧职责不变）。
        echoed = json.dumps(res.json(), ensure_ascii=False)
        assert "<script>" not in res.headers.get("content-type", "")
        assert echoed  # 体不为空即可；逐字回显在 JSON 通道内是安全的


def test_utterance_newline_flood_rejected():
    """换行极多的输入：超长 → 400；纯换行 → 空判 400。都不许 500。"""
    res = client.post("/api/utterance", json={"utterance": "肺\n" * 600, **_AGENT_OFF})
    assert res.status_code == 400
    res2 = client.post("/api/utterance", json={"utterance": "\n" * 200, **_AGENT_OFF})
    assert res2.status_code in (400, 422)


def test_utterance_full_pipeline_weird_input_no_500():
    """agent=true + mock provider 的完整管线（规则匹配概览真跑一遍本地语料，离线）：
    怪异输入不许炸 500。"""
    res = client.post("/api/utterance", json={
        "utterance": "🧬🧬 !!! ???", "provider": "mock", "use_llm": False, "agent": True,
    })
    assert res.status_code in (200, 400)
    assert res.status_code != 500


# ====================================================================== 2. /api/recommend 参数边界

def _recommend(**over):
    payload = {"query": "human liver", "provider": "mock", "use_llm": False}
    payload.update(over)
    return client.post("/api/recommend", json=payload)


@pytest.mark.parametrize("bad_top_k", (0, -1, 51, 999))
def test_recommend_top_k_out_of_range_rejected(bad_top_k):
    """top_k 越界（Field ge=1 le=50， 由 le=20 放宽）：必须 4xx 拒掉，不许 500 也不许静默当默认。"""
    res = _recommend(top_k=bad_top_k)
    assert res.status_code in (400, 422), bad_top_k


def test_recommend_top_k_boundary_50_works():
    """边界值 top_k=50（放宽后的新上界）正常跑，不 500。"""
    res = _recommend(top_k=50)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(res.json()["results"]) <= 50


def test_recommend_top_k_non_numeric_rejected():
    res = _recommend(top_k="abc")
    assert res.status_code in (400, 422)


def test_recommend_top_k_boundary_1_works():
    """边界值 top_k=1 正常跑（离线规则检索），不 500。"""
    res = _recommend(top_k=1)
    assert res.status_code == 200
    assert res.json()["ok"] is True
    assert len(res.json()["results"]) <= 1


def test_recommend_inverted_date_range_is_flagged():
    """date_from > date_to：倒挂窗口**必须**被拒（400/422）或在 warnings/trace 里
    明确点出「起晚于止」——实测现状是 200 静默跑完、结果恒 0、还把不可能的窗口
    当作合法生效条件（date:range）回显上屏，用户会误读成「这个区间真没数据」。"""
    res = _recommend(date_from="2024-01-01", date_to="2020-01-01")
    if res.status_code == 200:
        body = res.json()
        flagged = any("倒" in w or "起" in w and "止" in w or "inverted" in w.lower()
                      for w in body.get("warnings") or [])
        assert flagged, (
            f"倒挂日期窗被静默接受：200、result_total={body.get('result_total')}、"
            f"warnings={body.get('warnings')}，无一字指出窗口不可能成立"
        )
    else:
        assert res.status_code in (400, 422)


@pytest.mark.parametrize("bad_date", ("not-a-date", "2020-13-45", "2020-1-1", "2020-02-30", "今天"))
def test_recommend_illegal_dates_400(bad_date):
    """非法日期格式/不存在的日历日 → 400，不静默忽略、不冒充生效条件。"""
    res = _recommend(date_from=bad_date)
    assert res.status_code == 400, bad_date
    assert "date_from" in res.json()["detail"]


def test_recommend_unknown_source_is_flagged():
    """sources 含未知来源：必须 400 显式拒绝并点名（对齐 MCP `_validate_sources` 的
    bad_source 口径）——旧行为是 200 + no_match 冒充「查过了没有」，用户无法区分
    「来源写错」与「来源里真没数据」。空/空白来源名同样 400。"""
    res = _recommend(sources=["不存在的来源XYZ"])
    assert res.status_code == 400, res.text[:200]
    detail = res.json()["detail"]
    assert "不存在的来源XYZ" in detail and "收录" in detail, detail
    # 合法来源照常 200（对照组，证明没把好来源也毙掉）
    ok = _recommend(sources=["10x Genomics"])
    assert ok.status_code == 200
    # 空白来源名：本想过滤却给了空串，同样显式拒绝（不回退默认池）
    blank = _recommend(sources=["  "])
    assert blank.status_code == 400


def test_recommend_empty_sources_array_no_500():
    """sources=[]：空数组不许 500；语义要么等同 None（仅基础语料）要么如实说明。"""
    res = _recommend(sources=[])
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_recommend_invalid_strategy_clamped_safely():
    """strategy 非法值 → 安全默认 fixed（注释写明的设计），不 500、不回显非法策略。"""
    res = _recommend(strategy="banana")
    assert res.status_code == 200
    # fixed 档不回 strategy 决策块（仅 auto 非 None）；绝不允许把 "banana" 透传出去
    assert res.json().get("strategy") in (None, {})


def test_recommend_contradictory_facet_filters_no_500():
    """facet_filters 自相矛盾（同维度两个互斥值）：允许零结果，不许 500。"""
    res = _recommend(facet_filters=[
        {"dim": "species", "value": "homo sapiens"},
        {"dim": "species", "value": "mus musculus"},
    ])
    assert res.status_code == 200
    assert res.json()["ok"] is True


def test_recommend_garbage_facet_filters_sanitized():
    """facet_filters 形状垃圾（非 dict / 缺 value / 维度不在白名单）→ 收敛为空，不 500。"""
    res = _recommend(facet_filters=["not-a-dict", {"dim": "evil'}, {\"x\": 1}"}, {"dim": "species"}])
    assert res.status_code in (200, 422)
    assert res.status_code != 500


def test_recommend_blank_query_rejected():
    """query 空串 422 / 纯空白 400，不许 500。"""
    assert _recommend(query="").status_code == 422
    assert _recommend(query="   ").status_code == 400


# ====================================================================== 3. curate 逻辑边界（tmp 项目根直调真源）

@pytest.fixture
def curate_root(tmp_path):
    """离线 tmp 项目根：external 库 + 回收站目录，绝不碰真实 database/。"""
    (tmp_path / "database" / "external").mkdir(parents=True)
    (tmp_path / "database" / "base").mkdir(parents=True)
    return tmp_path


def _records_payload(records):
    return json.dumps(records, ensure_ascii=False).encode("utf-8")


_REC_A = [{"dataset_name": "边角测试集A", "species": "Human"}]
_REC_B = [{"dataset_name": "边角测试集B", "species": "Mouse"}]


def _import_one(root, records=_REC_A, filename="corner_a.json"):
    payload = _records_payload(records)
    token = cc.plan_import(payload, filename, project_root=root)["confirm_token"]
    return cc.apply_import(payload, filename, confirm_token=token, project_root=root)


def test_curate_restore_from_empty_recycle_rejected(curate_root):
    """空回收站 restore：unknown_file 人话，不崩、不凭空造文件。"""
    with pytest.raises(cc.CurateError) as ei:
        cc.plan_restore("20260101_000000_000000_upload_ghost.json", project_root=curate_root)
    assert ei.value.code == "unknown_file"
    # 缺 filename 也要 fail-closed（bad_param），不许 None 崩进路径拼接
    with pytest.raises(cc.CurateError) as ei2:
        cc.run_curate_action("restore", dry_run=True, project_root=curate_root)
    assert ei2.value.code == "bad_param"


def test_curate_remove_nonexistent_and_official_rejected(curate_root):
    """remove 不存在的文件 → unknown_file；官方快照（非 upload_*）→ not_curatable；
    路径穿越（../base/...）只能落到叶子名、结构性够不到 base。"""
    with pytest.raises(cc.CurateError) as ei:
        cc.plan_remove("upload_ghost.json", project_root=curate_root)
    assert ei.value.code == "unknown_file"

    official = curate_root / "database" / "external" / "official_snapshot.json"
    official.write_text(json.dumps(_REC_A), encoding="utf-8")
    with pytest.raises(cc.CurateError) as ei2:
        cc.plan_remove("official_snapshot.json", project_root=curate_root)
    assert ei2.value.code == "not_curatable"

    with pytest.raises(cc.CurateError) as ei3:
        cc.plan_remove("../base/official_snapshot.json", project_root=curate_root)
    # 现状（好于预期）：_leaf_name 直接拒绝任何带路径的输入（bad_param 人话），
    # 不是静默取叶子名——路径穿越在入参闸就死了，base 结构性不可达。
    assert ei3.value.code == "bad_param"
    assert (curate_root / "database" / "base").exists()  # base 一根毫毛没动


@pytest.mark.parametrize("payload,code", (
    (b"{not json at all", "invalid_json"),
    (b"[]", "no_records"),
    (b'{"records": []}', "no_records"),
    (b'"just a string"', "no_records"),
    (b"12345", "no_records"),
))
def test_curate_import_malformed_json_rejected(curate_root, payload, code):
    """畸形 JSON（坏语法/空数组/错类型）：一律 CurateError 人话码，零落盘。"""
    with pytest.raises(cc.CurateError) as ei:
        cc.plan_import(payload, "bad.json", project_root=curate_root)
    assert ei.value.code == code, payload[:30]
    assert list((curate_root / "database" / "external").iterdir()) == []


def test_curate_import_missing_fields_warns_but_previews(curate_root):
    """缺 dataset_name 的记录：preview 照常出 + warnings 点名，不静默。"""
    res = cc.plan_import(_records_payload([{"foo": 1}]), "noname.json", project_root=curate_root)
    assert res["record_count"] == 1
    assert any("dataset_name" in w for w in res["warnings"])


def test_curate_import_oversized_payload_rejected(curate_root):
    """超大 payload：webapp 层 64MB 闸（_curate_payload_bytes）→ bad_param。
    注意闸在 strip() 判空**之后**——纯空白 65MB 会走「空 payload」分支（同样 bad_param，
    无害）；这里钉的是非空白超限体。"""
    huge = "x" * (65 * 1024 * 1024)
    with pytest.raises(cc.CurateError) as ei:
        webapp._curate_payload_bytes(huge)
    assert ei.value.code == "bad_param"
    # 纯空白超限体走空判分支 → None（由真源报「缺 payload」），不进体积闸——现状钉住。
    assert webapp._curate_payload_bytes(" " * (65 * 1024 * 1024)) is None


def test_curate_import_non_json_filename_rejected(curate_root):
    with pytest.raises(cc.CurateError) as ei:
        cc.plan_import(_records_payload(_REC_A), "evil.txt", project_root=curate_root)
    assert ei.value.code == "bad_file"


def test_curate_duplicate_import_same_content(curate_root):
    """同内容重复导入：plan 如实标 duplicate；apply 无 force → duplicate_content 零写入；
    force → 放行且如实标 forced。"""
    first = _import_one(curate_root)
    payload = _records_payload(_REC_A)
    preview = cc.plan_import(payload, "corner_a.json", project_root=curate_root)
    assert preview["duplicate"]["is_duplicate"] is True
    assert first["filename"] in preview["duplicate"]["matched_files"]
    with pytest.raises(cc.CurateError) as ei:
        cc.apply_import(payload, "corner_a.json",
                        confirm_token=preview["confirm_token"], project_root=curate_root)
    assert ei.value.code == "duplicate_content"
    forced = cc.apply_import(payload, "corner_a.json",
                             confirm_token=preview["confirm_token"], force=True,
                             project_root=curate_root)
    assert forced["forced"] is True


def test_curate_recycle_same_name_conflict_and_double_remove(curate_root):
    """回收站同名冲突：删→外部库再造同名→restore 必须拒绝覆盖（bad_param）；
    同一原名删两次 → 回收站两份共存（时间戳防冲突），谁都不覆盖谁。"""
    res = _import_one(curate_root)
    fname = res["filename"]

    # 第一次删除
    tok1 = cc.plan_remove(fname, project_root=curate_root)["confirm_token"]
    rm1 = cc.apply_remove(fname, confirm_token=tok1, project_root=curate_root)
    recycle_name1 = rm1["moved_to"].split("/")[-1]

    # 外部库重新出现同名文件（同名冲突的埋雷）
    conflict = curate_root / "database" / "external" / fname
    conflict.write_text(json.dumps(_REC_B), encoding="utf-8")

    preview = cc.plan_restore(recycle_name1, project_root=curate_root)
    assert preview["will_conflict"] is True
    with pytest.raises(cc.CurateError) as ei:
        cc.apply_restore(recycle_name1, confirm_token=preview["confirm_token"],
                         project_root=curate_root)
    assert ei.value.code == "bad_param"
    # 冲突防护之后：回收站原件还在、外部库新文件也没被覆盖
    assert json.loads(conflict.read_text(encoding="utf-8"))[0]["dataset_name"] == "边角测试集B"

    # 同名第二次删除（移走冲突文件）→ 回收站两份并存
    tok2 = cc.plan_remove(fname, project_root=curate_root)["confirm_token"]
    rm2 = cc.apply_remove(fname, confirm_token=tok2, project_root=curate_root)
    recycle_name2 = rm2["moved_to"].split("/")[-1]
    listing = cc.list_curations(project_root=curate_root)
    names = [e["recycle_name"] for e in listing["recycle"]]
    assert recycle_name1 in names and recycle_name2 in names
    assert recycle_name1 != recycle_name2
    assert listing["recycle_count"] == 2


def test_curate_list_on_empty_root(curate_root):
    """空库空回收站 list：零崩、计数为零。"""
    res = cc.list_curations(project_root=curate_root)
    assert res["file_count"] == 0 and res["recycle_count"] == 0


def test_curate_recycle_entry_with_broken_json_does_not_crash_list(curate_root):
    """回收站里躺着一个坏 JSON：list_curations 必须宽容（record_count=None），不崩。"""
    rec_dir = curate_root / ".userdata" / "recycle"
    rec_dir.mkdir(parents=True)
    (rec_dir / "20260101_000000_000000_upload_broken.json").write_text(
        "{broken", encoding="utf-8")
    res = cc.list_curations(project_root=curate_root)
    assert res["recycle_count"] == 1
    assert res["recycle"][0]["record_count"] is None


# ====================================================================== 4. action_plan / turn 边界

def _raw(verb, **kw):
    base = {"verb": verb, "confidence": "high", "reason": "探针"}
    base.update(kw)
    return base


def test_limit_zero_dropped_with_delta():
    """「前0条」：limit=0 不是合法条数 → dropped + delta 如实说按默认口径。"""
    plan = ap.build_plan_from_raw(
        _raw("pack.download", limit=0, quoted="打包"), "打包前0条",
        has_results=True, result_total=10)
    assert plan["slots"]["limit"] == 0
    assert plan["slot_sources"]["limit"] == "dropped"
    assert any(d["slot"] == "limit" for d in plan["deltas"])


def test_limit_999_clamped_to_50_with_delta():
    """「前999条」：超 MAX_LIMIT=50 → 钳到 50 + delta 如实说明，不静默按 999 办。"""
    plan = ap.build_plan_from_raw(
        _raw("pack.download", limit=999, quoted="打包"), "打包前999条",
        has_results=True, result_total=2000)
    assert plan["slots"]["limit"] == 50
    assert plan["slot_sources"]["limit"] == "clamped"
    assert any("50" in d["used"] for d in plan["deltas"])


def test_limit_negative_dropped():
    """「前-3条」：负数条数 → dropped，绝不进 slots 当合法值。"""
    plan = ap.build_plan_from_raw(
        _raw("pack.download", limit=-3, quoted="打包"), "打包前-3条",
        has_results=True, result_total=10)
    assert plan["slots"]["limit"] == 0
    assert plan["slot_sources"]["limit"] == "dropped"


def test_limit_non_numeric_dropped():
    plan = ap.build_plan_from_raw(
        _raw("pack.download", limit="abc", quoted="打包"), "打包前abc条",
        has_results=True, result_total=10)
    assert plan["slots"]["limit"] == 0
    assert plan["slot_sources"]["limit"] == "dropped"


def test_limit_none_is_default_silently_safe():
    """没说条数：limit=0 走默认口径，不该有 delta（没说≠说错）。"""
    plan = ap.build_plan_from_raw(
        _raw("pack.download", quoted="打包"), "把结果打包",
        has_results=True, result_total=10)
    assert plan["slots"]["limit"] == 0
    assert plan["slot_sources"]["limit"] == "default"
    assert plan["deltas"] == []


@pytest.mark.parametrize("utterance,quoted", (
    ("不要打包", "打包"),
    ("别下载了", "下载"),
    ("先不导入这份数据", "导入"),
))
def test_negation_polarity_marks_cancelled(utterance, quoted):
    """否定极性：「不要打包」「别下载了」「先不导入」→ 动词照判 + cancelled=True。"""
    verb = {"打包": "pack.download", "下载": "pack.download", "导入": "curate.import"}[quoted]
    plan = ap.build_plan_from_raw(
        _raw(verb, quoted=quoted), utterance, has_results=True, result_total=10)
    assert plan["cancelled"] is True, utterance
    assert plan["verb"] == verb  # 动词照留，不许降 none 装没听懂


@pytest.mark.parametrize("utterance,quoted", (
    ("能不能打包一下", "打包"),      # 征询不是否定
    ("要不要导出引文", "导出"),      # 征询掩码
    ("请删掉我上传的文件", "删掉"),  # 动作词本身不是否定语素
    ("帮我移除这份数据", "移除"),
))
def test_polarity_does_not_misfire(utterance, quoted):
    """极性门误伤侧：征询掩码 / 动作词剔除——这些都**不许**被标 cancelled。"""
    verb = ("curate.remove" if quoted in ("删掉", "移除")
            else "cite.export" if quoted == "导出" else "pack.download")
    plan = ap.build_plan_from_raw(
        _raw(verb, quoted=quoted), utterance, has_results=True, result_total=10)
    assert plan["cancelled"] is False, utterance


def test_non_verbatim_quoted_is_blocked():
    """quoted 非逐字子串（LLM 改写/加字）→ 护栏清空 → 执行类降 none + 如实理由。"""
    plan = ap.build_plan_from_raw(
        _raw("pack.download", quoted="打包这批数据（改写版）"), "打包这批数据",
        has_results=True, result_total=10)
    assert plan["verb"] == "none"
    assert "原文依据" in plan["reason_zh"]


def test_rule_fallback_without_llm_no_crash():
    """无 key / LLM 关 / mock：规则档路由不崩——pack.preview + 如实 caveat，不接落盘动作。"""
    plan = ap.plan_action("把结果打包", has_results=True, result_total=3,
                          config=LLMConfig(enable_llm=False))
    assert plan["source"] == "rule"
    assert plan["verb"] == "pack.preview"  # 规则档只开清单，不接 download
    assert plan["llm_status"] == "disabled"
    assert "没有接上" in plan["caveat_zh"]

    plan2 = ap.plan_action("把结果打包", has_results=True, result_total=3,
                           config=LLMConfig(enable_llm=True, mock_llm=True, api_key="sk-x"))
    assert plan2["llm_status"] == "mock_not_used"
    assert plan2["source"] == "rule"


def test_turn_agent_off_operation_marker_gives_bubble():
    """「AI 执行」关 + 规则检出操作意图 → 降级气泡（needs_agent），不静默当检索。"""
    res = turn.route_turn("把结果打包下载", use_agent=False)
    assert res["route"] == turn.ROUTE_NONE
    assert res["needs_agent"] is True
    assert "AI 执行" in res["echo_zh"]


def test_turn_agent_off_plain_query_goes_search():
    res = turn.route_turn("human lung atlas", use_agent=False)
    assert res["route"] == turn.ROUTE_SEARCH
    assert res["via"] == "rule_direct"
    assert res["query"] == "human lung atlas"


def test_turn_blank_utterance_raises_typed_error():
    """turn 入口的空白句：typed ActionPlanError（上层翻 400），不是裸 ValueError/崩。"""
    with pytest.raises(ap.ActionPlanError):
        turn.route_turn("   ", use_agent=False)


# ====================================================================== 5. identifiers 边界

def _loader_must_not_run():
    raise AssertionError("GEO/SRA 必须 fail-closed 在装载语料之前")


def test_identifier_lowercase_gsm_fail_closed_before_load():
    """gsm123（小写）：识别为 GEO Sample、不索引 → fail-closed 指路，语料装载绝不能被触发。
    （GSE 已入库走反查；Sample 级 GSM 仍结构性不索引。）"""
    res = identifiers.lookup("gsm123", _loader_must_not_run)
    assert res["indexed"] is False and res["kind"] == "geo_sample"
    assert "GEO" in res["message"]
    assert res["match"] is None


def test_identifier_gse_without_digits_is_not_identifier():
    """「GSE」（无数字）：不是标识符 → lookup 返回 None（按普通查询处理），不许误指路。"""
    assert identifiers.classify("GSE") is None
    assert identifiers.lookup("GSE", _loader_must_not_run) is None


def test_identifier_huge_gsm_number_fail_closed():
    res = identifiers.lookup("GSM999999999", _loader_must_not_run)
    assert res["indexed"] is False and res["kind"] == "geo_sample"
    assert res["external_url"].endswith("GSM999999999")


def test_identifier_doi_with_weird_chars_truncated_at_quote():
    """DOI 含引号/尖括号：正则词表在奇异字符前截断，注入残片不进 value。"""
    hit = identifiers.classify('10.1234/abc"onclick="evil')
    assert hit is not None and hit["kind"] == "doi"
    assert '"' not in hit["value"] and "<" not in hit["value"]

    hit2 = identifiers.classify("10.1101/2021.01.01.123<script>alert(1)")
    assert "<" not in hit2["value"] and "script" not in hit2["value"].lower()


def test_identifier_doi_prefix_variants():
    """DOI 前缀变体：整段 URL 里仍能认出 DOI 本体；「10.xxxx/」非数字段 → 不是 DOI。"""
    hit = identifiers.classify("https://doi.org/10.1101/abc123")
    assert hit is not None and hit["kind"] == "doi"
    assert hit["value"] == "10.1101/abc123"
    assert identifiers.classify("10.xxxx/yyy") is None


def test_identifier_shared_doi_lists_all_candidates():
    """共享 DOI（一篇论文挂多库）：如实列全部候选，绝不静默任取第一条。"""
    records = [
        {"dataset_uid": "ae:E-MTAB-1", "dataset_name": "肺图谱",
         "collection_doi": "10.1101/abc123", "source": "ArrayExpress"},
        {"dataset_uid": "cxg:uuid-2", "dataset_name": "肝图谱",
         "collection_doi": "https://doi.org/10.1101/ABC123", "source": "CELLxGENE Discover"},
    ]
    res = identifiers.lookup("10.1101/ABC123", lambda: records)  # 大小写变体照判等
    assert res["match"] is None
    assert len(res["candidates"]) == 2
    assert "2 条" in res["message"]


def test_identifier_indexed_doi_no_match_is_honest():
    """本目录应含但未命中的 DOI：如实说「未匹配」，不谎称搜过全库后有或没有。"""
    res = identifiers.lookup("10.1101/zz999", lambda: [])
    assert res["indexed"] is True and res["match"] is None
    assert "未匹配" in res["message"]


# ---------------------------------------------------------------- MCP 修复回归门

def test_mcp_curate_payload_json_is_plain_str_annotation():
    """D 路 ：FastMCP `pre_parse_json` 会对**非纯 str 注解**的字符串入参一律 json.loads——
    `payload_json: str | None` 被预解析成 dict/list 后被 pydantic 以 string_type 拒掉，
    curate_datasets 的 import / search_online-apply 经真 MCP 协议必挂（垃圾 JSON 反而过得了）。
    修法=注解纯 str（SDK 跳过预解析）。本条钉住签名不再回退。"""
    import typing

    from dataset_recommender.app import mcp_server
    # 模块有 `from __future__ import annotations`（PEP 563），inspect 只给字符串——必须 resolve。
    hints = typing.get_type_hints(mcp_server.curate_datasets)
    assert hints["payload_json"] is str, (
        f"payload_json 注解回退为 {hints['payload_json']}——"
        "经真 MCP 协议传 JSON 文本会被 SDK 预解析后拒收")


def test_mcp_plan_action_uses_strict_scalar_types():
    """D 路 ：plan_action 的 has_results/result_total 此前是普通 bool/int——"5"→5、
    "yes"→True 静默强转，与 recommend 已采纳的 StrictInt 口径不一致。钉住严格类型。"""
    import typing

    from dataset_recommender.app import mcp_server
    from pydantic import StrictBool, StrictInt
    hints = typing.get_type_hints(mcp_server.plan_action, include_extras=True)
    assert hints["has_results"] is StrictBool
    assert hints["result_total"] is StrictInt


# ---------------------------------------------------------------- HTTP 修复回归门

def test_recommend_query_length_cap():
    """E 路 ：recommend query 无上限（200KB 照跑）→ 与 MCP bad_query 同口径 2000 字闸。"""
    res = _recommend(query="肺" * 2001)
    assert res.status_code == 400 and "过长" in res.json()["detail"]
    ok = _recommend(query="肺" * 500)
    assert ok.status_code == 200


def test_import_record_count_cap(tmp_path):
    """E 路 ：150 万条合法 JSON 曾能入库、随后 /api/datasets 37.8s 级联拖死全站——
    条数上限钉死在摄取层（三入口共用），超限 too_large、零落盘。"""
    from dataset_recommender.corpus import corpus_curation as cc
    records = [{"dataset_name": f"ds{i}", "species": "Human"} for i in range(cc.MAX_IMPORT_RECORDS + 1)]
    payload = json.dumps(records).encode("utf-8")
    try:
        cc.plan_import(payload, "huge.json", project_root=tmp_path)
        raise AssertionError("超上限导入没有被拦")
    except cc.CurateError as exc:
        assert exc.code == "too_large"
    assert not list(tmp_path.glob("database/external/*.json")), "被拒的导入必须零落盘"


def test_import_external_total_budget_cap(tmp_path, monkeypatch):
    """单文件 20 万条上限挡不住「连续导入多个 20 万」——
    全库累计闸钉死：existing + new > 上限 → too_large、零落盘、如实报现状与出路。"""
    from dataset_recommender.corpus import corpus_curation as cc
    monkeypatch.setattr(cc, "EXTERNAL_TOTAL_MAX_RECORDS", 5)
    ext = tmp_path / "database" / "external"
    ext.mkdir(parents=True)
    (ext / "upload_old.json").write_text(json.dumps(
        [{"dataset_name": f"old{i}", "species": "Human"} for i in range(4)]), encoding="utf-8")
    # 现有 4 条 + 新 2 条 = 6 > 5 → 拒
    payload = json.dumps([{"dataset_name": "new1"}, {"dataset_name": "new2"}]).encode("utf-8")
    try:
        cc.plan_import(payload, "more.json", project_root=tmp_path)
        raise AssertionError("超累计上限的导入没有被拦")
    except cc.CurateError as exc:
        assert exc.code == "too_large"
        assert "累计" in str(exc) and "回收站" in str(exc)
    assert sorted(p.name for p in ext.glob("*.json")) == ["upload_old.json"], "被拒的导入必须零落盘"
    # 现有 4 条 + 新 1 条 = 5 ≤ 5 → 放行（闸是上限不是降格）
    ok_payload = json.dumps([{"dataset_name": "new1"}]).encode("utf-8")
    plan = cc.plan_import(ok_payload, "ok.json", project_root=tmp_path)
    assert plan["record_count"] == 1


def test_static_cache_control_headers():
    """E 路 ：/static 带 ?v= 指纹 → immutable 长缓存（内容变则令牌必变，契约门保证）；
    无指纹 → no-cache 回源再验证。"""
    from fastapi.testclient import TestClient
    from dataset_recommender.app.webapp import app
    client = TestClient(app, base_url="http://127.0.0.1")
    versioned = client.get("/static/js/core/shell.js?v=test-token")   # 任意非空 v 即视为指纹资源
    plain = client.get("/static/js/core/shell.js")
    assert "immutable" in versioned.headers.get("cache-control", "")
    assert "no-cache" in plain.headers.get("cache-control", "")


# ---------------------------------------------------------------- 前端功能修复回归门

def test_unified_box_user_searches_carry_usersubmit_flag():
    """根因：统一框路由来的检索没带「亲手提交」标记 → usageLogSearch 闸全拒，
    「检索 N 次」恒 0（速度/弃权原话等 v2 维度全饿死）。钉住 board.js 四处用户提交型
    runRecommend 调用都显式带 handSubmit:true——分面芯片/放宽/撤销等自动路径不得带。
    同时钉住已退役的 userSubmit 标识不得复活（与 test_unified_box/test_act_frontend 同口径）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    board = (root / "web/static/js/panel/board.js").read_text(encoding="utf-8")
    # final a 档（环内采纳 result_payload 换屏）也是用户亲手提交的落地——
    # 它的 prefetched runRecommend 共用一份 `_a`（handSubmit:true），故 3 → 4；漏带会让 a 档
    # 落地被 usageLogSearch 闸拒，「检索 N 次」少记一类用户检索。
    # （tool 档消费补链）：三 flag ON 时检索由环内 rank/rerank 工具完成、
    # 整轮 route=tool，tool 分支把环内上屏批 prefetched 落地；这同样是用户亲手提交那句引发的
    # 检索落地。仅 preliminary 批镜像只摘徽标不落地，不新增计数。
    # （覆盖策略修复）：search a 档与 route=tool 档的落地统一收进 `_applyBatchDecision`，
    # 两者共用同一份 `_a`（handSubmit:true）——故 5 → 4（不再是 a 档与 tool 档各一份）。
    assert board.count("handSubmit: true") == 4, (
        "_applyBatchDecision 的 _a（search a 档与 tool 档共用）+ c 档两路 + ubDispatchAction 先搜后执行一路，"
        "必须恰四处带 handSubmit:true；多了会把自动重跑也算成用户检索，少了则漏记")
    assert "userSubmit" not in board, "userSubmit 旧档已随统一路由退役，不得复活"
    core = (root / "web/static/js/act/act_core.js").read_text(encoding="utf-8")
    assert "a.restored_name" in core, "移回回执必须优先显示 restored_name（原名），不显示回收站时间戳名"
