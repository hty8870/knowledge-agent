# -*- coding: utf-8 -*-
"""依赖占位批量计划 v2（2026-08-20 批）的确定性门。**全离线**（FakeModel + 假注册表
+ 合成记录）。设计：`设计文档（v2 权威，§1-§3、§7）。

覆盖清单（逐项）：
- 正则/矩阵校验：唯一形状 `$<N>.top[<i>].dataset_uid` 收、内嵌/路径错/缺段/前导零拒；
  流向矩阵（槽位/N 越界/生产者必须 rank-rerank/元槽位禁占位）逐条拦；
- 越界截断：`_batch_readonly_extras` 对非法占位**整尾截断**回炉（不丢事，decide 带新状态重判）；
- 解析后全闸同口径：deferred 续步 execute 执行前解析，解析后实参过 _validate_raw +
  build_plan_from_raw + 政策闸（含取消态跳过），与单步同口径一步不少；
- 主步禁占位：单步/主步提议带占位 → violation → 一次重问后干净恢复；
- resolver_error 不进熔断：top 下标越界/条目无 uid → 跳过、不写假 ok=False step、
  不进死路账（不触发 _failed_tool_ban）、留 trace（批 id/计划位置/依赖位置/原引用/原因）；
- dependency_unavailable 级联跳过留 trace：生产者失败 → 依赖步跳过且 trace 如实交代；
- top digest 新字段：dataset_uid + rank（1 起序号）——依赖占位的解析源；
- cite.export uids 真实消费：按提供清单导出（保序、不叠加 limit、≤20）；
- batch_results 不跨批：解析源是 execute 局部 dict——跨调用/跨批零残留。
"""
import pytest

pytest.importorskip("langgraph", reason="langchain 扩展未安装：批量测试跳过")
from langchain_core.messages import AIMessage  # noqa: E402

from dataset_recommender.agent import agent_exec  # noqa: E402
from dataset_recommender.llm.llm_client import LLMConfig  # noqa: E402
from dataset_recommender.retrieval.normalizer import DatasetRecord  # noqa: E402

CFG = LLMConfig(enable_llm=True, api_key="sk-pbt1")

UTTER = "先看下库里多少条，再搜人类肺数据并对比前两条"

# ---------------------------------------------------------------- 合成记录（cite 消费用）

def _rec(uid, name, **over):
    base = dict(
        species="Human", tissue="Lung", disease="Lung cancer",
        chemistry="3p v2", count="12000", unit="cells", has_raw_data=True,
        url=f"https://example.com/{uid}", source_file="", description="",
        raw={"dataset_uid": uid, "source": "10x Genomics", "n_files": 5,
             "published_date": "2021-03-15", "filesize": 0, "collection_doi": ""},
        platform_family="Visium", assay="spatial", modality="spatial",
    )
    base.update(over)
    raw = base.pop("raw")
    return DatasetRecord(
        dataset_name=name, species=base.pop("species"), tissue=base.pop("tissue"),
        disease=base.pop("disease"), chemistry=base.pop("chemistry"),
        count=base.pop("count"), unit=base.pop("unit"),
        has_raw_data=base.pop("has_raw_data"), url=base.pop("url"),
        source_file=base.pop("source_file"), description=base.pop("description"),
        raw=raw, **base,
    )


# ---------------------------------------------------------------- FakeModel 与假注册表

class _FakeModel:
    def __init__(self, *answers):
        self.answers = list(answers)

    def bind_tools(self, tools, tool_choice=None, parallel_tool_calls=None):
        return self

    def invoke(self, messages):
        return self.answers.pop(0)


def _tool_call(verb, **args):
    return AIMessage(content="", tool_calls=[
        {"name": verb.replace(".", "_"), "args": args, "id": "t1"}])


def _batch(*calls):
    return AIMessage(content="", tool_calls=[
        {"name": str(name).replace(".", "_"), "args": args, "id": f"t{i + 1}"}
        for i, (name, args) in enumerate(calls)])


def _finish(report):
    return AIMessage(content="", tool_calls=[
        {"name": "finish", "args": {"completion_report": report}, "id": "tf"}])


def _db_result():
    return {"generated_at": "t", "sources": [], "total_records": 0,
            "external_files": [], "recycle": [],
            "ledger": {"entries": 0, "by_endpoint": {}, "recent": []}}


def _rank_result(*uids):
    return {"query": "人类肺", "total": len(uids),
            "top": [{"dataset_uid": uid, "dataset_name": f"DS-{uid}", "species": "Human",
                     "tissue": "Lung", "disease": "", "source": "10x Genomics", "rank": i + 1}
                    for i, uid in enumerate(uids)],
            "displayed": True, "batch": None}


def _compare_result(a, b):
    return {"a": {"dataset_uid": a}, "b": {"dataset_uid": b},
            "assumption_zh": "", "fields": [], "n_same": 0, "n_diff": 0, "n_unknown": 0,
            "identical": False, "comparison_zh": "对比完成", "wording_source": "deterministic",
            "degraded": False, "degrade_reason": "", "caveat_zh": ""}


def _install_registry(monkeypatch, *, rank_run=None, rank_top=None, fail_rank=False,
                      cmp_run=None, cite_run=None):
    """假注册表：db_status + rank（可钉 top/失败）+ compare + cite.export。"""
    def _rank(slots, root, ctx=None):
        if fail_rank:
            err = RuntimeError("network_error: 网络抖动")
            err.code, err.hint = "network_error", "网络抖动"
            raise err
        return _rank_result(*(rank_top or ["UID-A", "UID-B"]))

    table = {
        "curate.db_status": {
            "run": lambda slots, root: _db_result(),
            "label_zh": "汇报数据库状态", "card_kind": "db_status",
            "readonly": True, "report": True, "observation": True},
        "rank": {
            "run": rank_run or _rank, "label_zh": "检索数据集", "card_kind": "rank",
            "readonly": True, "needs_context": True},
        "compare.datasets": {
            "run": cmp_run or (lambda slots, root, ctx=None:
                               _compare_result(slots.get("a"), slots.get("b"))),
            "label_zh": "对比数据集", "card_kind": "compare",
            "readonly": True, "needs_context": True},
        "cite.export": {
            "run": cite_run or (lambda slots, root, ctx=None: {
                "n_datasets": len(slots.get("uids") or []),
                "uids": list(slots.get("uids") or []), "files": [], "out_dir": "",
                "note_zh": "已导出"}),
            "label_zh": "导出引文", "card_kind": "cite_export",
            "readonly": False, "needs_context": True},
    }
    monkeypatch.setattr(agent_exec, "LOOP_TOOLS", table)


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr(agent_exec, "_agent_project_root", lambda: tmp_path)
    # 与 conftest 全局 stub 同口径：图内测试不预置分流投票应答。
    monkeypatch.setattr(agent_exec, "_run_route_consensus", lambda *a, **k: ("general", []))
    monkeypatch.setattr(agent_exec, "_task_checklist_call", lambda *a, **k: ([], 0, ""))


def _run(utterance, model):
    return agent_exec.plan_with_agent(
        utterance, has_results=False, result_total=0, config=CFG, retrieval=None,
        current_query="", current_filters=None, chat_model=model)


def _nodes(trace, node):
    return [t for t in trace if t["node"] == node]


def _ph_trace(trace, label_prefix):
    return [t for t in trace if str(t.get("label_zh") or "").startswith(label_prefix)]


# ---------------------------------------------------------------- 正则 / 矩阵校验

def test_placeholder_regex_accepts_only_unique_shape():
    ph = agent_exec._placeholder_ref
    assert ph("$1.top[0].dataset_uid") == (1, 0)
    assert ph("$12.top[3].dataset_uid") == (12, 3)
    assert ph("$1.top[9].dataset_uid") == (1, 9)
    # 前导零 / 0 序号 / 其它路径 / 缺段 / 内嵌：全部是形似占位（_PH_BAD）
    assert ph("$0.top[0].dataset_uid") is agent_exec._PH_BAD
    assert ph("$01.top[0].dataset_uid") is agent_exec._PH_BAD
    assert ph("$1.top[0].dataset_name") is agent_exec._PH_BAD
    assert ph("$1.top[0]") is agent_exec._PH_BAD
    assert ph("prefix$1.top[0].dataset_uid") is agent_exec._PH_BAD
    assert ph("$1.top[0].dataset_uid-suffix") is agent_exec._PH_BAD
    # 普通字面量
    assert ph("GSE123456") is None
    assert ph("") is None


def test_static_violations_matrix():
    def chk(verb, raw, batch_verbs, pos):
        return agent_exec._placeholder_static_violations(verb, raw, batch_verbs, pos)

    # 合法：compare 的 a/b 引用批内第 1 个（rank）
    raw = {"a": "$1.top[0].dataset_uid", "b": "$1.top[1].dataset_uid"}
    assert chk("compare.datasets", raw, ["rank", "compare.datasets"], 2) == []
    # cite.export 的 uids 数组：占位与字面量混用合法
    assert chk("cite.export", {"uids": ["$1.top[0].dataset_uid", "GSE1"]},
               ["rank", "cite.export"], 2) == []
    # 槽位不在矩阵（fair.check 只有 uid；a 槽非法）
    assert chk("fair.check", {"a": "$1.top[0].dataset_uid"},
               ["rank", "fair.check"], 2)
    # 未登记占位工具的槽（rank 的 query 带占位——矩阵外流向）
    assert chk("rank", {"query": "$1.top[0].dataset_uid"}, ["rank", "rank"], 2)
    # N 越界（N >= 当前批内序号）
    assert chk("compare.datasets", {"a": "$2.top[0].dataset_uid"},
               ["rank", "compare.datasets"], 2)
    # 生产者不是 rank/rerank（check_updates 没有可引用的 top）
    assert chk("compare.datasets", {"a": "$1.top[0].dataset_uid"},
               ["curate.check_updates", "compare.datasets"], 2)
    # 元槽位禁占位
    assert chk("compare.datasets", {"quoted": "$1.top[0].dataset_uid"},
               ["rank", "compare.datasets"], 2)
    # 形似但不合规（路径错）
    assert chk("compare.datasets", {"a": "$1.top[0].dataset_name"},
               ["rank", "compare.datasets"], 2)
    # 自引用（主步位置 pos=1：N=1 >= 1 → 越界）
    assert chk("compare.datasets", {"a": "$1.top[0].dataset_uid"},
               ["compare.datasets"], 1)


def test_static_violations_rejects_self_reference_as_main_step():
    """主步/单步（pos=1）任何占位都越界——`_adjudicate_decide_obj` 据此拦下主步占位。"""
    raw = {"verb": "compare.datasets", "quoted": "对比前两条",
           "a": "$1.top[0].dataset_uid"}
    nxt, note, declined, fb = agent_exec._adjudicate_decide_obj(
        raw, {"steps": [], "utterance": UTTER})
    assert nxt is None and fb, "主步占位必须进 violation（第四件反馈非空）"
    assert "执行序号" in fb


# ---------------------------------------------------------------- 越界截断

def test_batch_filter_truncates_illegal_placeholder(monkeypatch):
    """批 = [rank, compare($2…N 越界), fair]：非法占位出现即**截断整尾**（fair 也回炉）——
    模型的依赖链已不可信，不冒险只剔一个。"""
    _install_registry(monkeypatch)
    calls = [
        {"name": "rank", "args": {"query": "人类肺"}},
        {"name": "compare_datasets",
         "args": {"quoted": "对比前两条", "a": "$2.top[0].dataset_uid"}},
        {"name": "fair_check", "args": {"quoted": "给第一条做FAIR", "uid": "$1.top[0].dataset_uid"}},
    ]
    accepted, dropped = agent_exec._batch_readonly_extras(
        calls, {"verb": "rank", "query": "人类肺"},
        {"steps": [], "utterance": UTTER})
    assert accepted == [] and dropped == 2, "N 越界（$2 >= 位置 2）→ 整尾截断"


def test_batch_filter_truncates_out_of_matrix_flow(monkeypatch):
    """矩阵外流向截断：check_updates 的 source 槽带占位（非矩阵槽）→ 截断整尾。"""
    _install_registry(monkeypatch)
    calls = [
        {"name": "rank", "args": {"query": "人类肺"}},
        {"name": "curate_check_updates",
         "args": {"quoted": "检查更新", "source": "$1.top[0].dataset_uid"}},
    ]
    accepted, dropped = agent_exec._batch_readonly_extras(
        calls, {"verb": "rank", "query": "人类肺"},
        {"steps": [], "utterance": UTTER})
    assert accepted == [] and dropped == 1


# ---------------------------------------------------------------- 端到端：解析执行

def test_single_call_placeholder_resolves_on_main_step_end_to_end(monkeypatch):
    """真模型自然形态（实测）：understand 先 rank（第 1 步），decide **单发**
    compare(a=\"$1.top[0]…\")——主步带占位，execute 在节点头部解析后过全闸再执行；
    slots 存**解析后 uid**；trace 留「批内依赖解析」行（批 id/计划位置/依赖位置/原引用 → 真实 uid）。"""
    seen: list = []
    def _cmp(slots, root, ctx=None):
        seen.append(dict(slots))
        return _compare_result(slots.get("a"), slots.get("b"))
    _install_registry(monkeypatch, cmp_run=_cmp)
    model = _FakeModel(
        _tool_call("rank", query="人类肺", quoted="搜人类肺数据"),
        _tool_call("compare.datasets", quoted="对比前两条",
                   a="$1.top[0].dataset_uid", b="$1.top[1].dataset_uid"),
        _finish("1. 检索：已做（第1步）。\n2. 对比前两条：已做（第2步）。"),
    )
    plan, trace = _run("搜人类肺数据，对比前两条", model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["rank", "compare.datasets"]
    assert steps[1]["slots"] == {"a": "UID-A", "b": "UID-B"}, "实录存解析后值"
    assert seen == [{"a": "UID-A", "b": "UID-B"}], "工具收到解析后的真实 uid"
    resolve = _ph_trace(trace, "批内依赖解析")
    assert len(resolve) == 1
    d = resolve[0]["detail"]
    assert "batch-" in d and "$1.top[0].dataset_uid → UID-A" in d
    assert "计划位置 2" in d and "依赖位置 1" in d


def test_batch_rank_compare_resolves_placeholders_end_to_end(monkeypatch):
    """同批形态：decide 一次给 [rank(主步), compare($2)]——主步 rank 真跑（序号 2）、
    compare 延迟解析后同批执行（$2 = 同批主步）。"""
    seen: list = []
    def _cmp(slots, root, ctx=None):
        seen.append(dict(slots))
        return _compare_result(slots.get("a"), slots.get("b"))
    _install_registry(monkeypatch, cmp_run=_cmp)
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="看下库里多少条"),
        _batch(("rank", {"query": "人类肺", "quoted": "搜人类肺数据"}),
               ("compare.datasets", {"quoted": "对比前两条", "a": "$2.top[0].dataset_uid",
                                     "b": "$2.top[1].dataset_uid"})),
        _finish("1. 看库里多少条：已做（第1步）。\n2. 检索：已做（第2步）。\n"
                "3. 对比前两条：已做（第3步）。"),
    )
    plan, trace = _run(UTTER, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == [
        "curate.db_status", "rank", "compare.datasets"]
    assert steps[2]["slots"] == {"a": "UID-A", "b": "UID-B"}, "实录存解析后值"
    assert seen == [{"a": "UID-A", "b": "UID-B"}]
    resolve = _ph_trace(trace, "批内依赖解析")
    assert len(resolve) == 1 and "$2.top[0].dataset_uid → UID-A" in resolve[0]["detail"]
    decides = _nodes(trace, "decide")
    assert any("通过占位依赖检查" in t["detail"] for t in decides)
    validates = _nodes(trace, "validate")
    assert any("占位接地，执行时解析" in t["detail"] for t in validates)


def test_dependency_unavailable_cascades_skip_with_trace(monkeypatch):
    """级联跳过（dependency_unavailable）：主步 rank 真失败 → compare（$1）引用
    第 1 步无值 → 不执行、不记假 ok=False step、trace 留「批内依赖跳过」（含批 id/位置/
    依赖/原引用/原因）；跳过不吃失败预算（dead_ends 只记 rank 自身）。"""
    _install_registry(monkeypatch, fail_rank=True)
    model = _FakeModel(
        _tool_call("rank", query="人类肺", quoted="搜人类肺数据"),
        _tool_call("compare.datasets", quoted="对比前两条",
                   a="$1.top[0].dataset_uid", b="$1.top[1].dataset_uid"),
        _finish("1. 检索：做不到（网络失败）。\n2. 对比：没做。"),
    )
    plan, trace = _run("搜人类肺数据，对比前两条", model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["rank"], "依赖步跳过不产生假 ok=False step（不执行不记步）"
    assert [s["ok"] for s in steps] == [False]
    skips = _ph_trace(trace, "批内依赖跳过")
    assert len(skips) == 1 and skips[0]["ok"] is False
    d = skips[0]["detail"]
    assert "依赖不可用" in d and "$1.top[0].dataset_uid" in d
    assert "batch-" in d and "依赖位置 1" in d and "计划位置 2" in d


def test_resolver_error_index_out_of_range_not_banned(monkeypatch):
    """resolver_error 不进熔断：top 只有 1 条、引用 $1.top[1] → 解析失败跳过
    不触发 _failed_tool_ban（后续同一 compare 单步仍可执行）、不写假 step。"""
    _install_registry(monkeypatch, rank_top=["UID-A"])
    model = _FakeModel(
        _tool_call("rank", query="人类肺", quoted="搜人类肺数据"),
        _tool_call("compare.datasets", quoted="对比前两条",
                   a="$1.top[0].dataset_uid", b="$1.top[1].dataset_uid"),
        # 下一轮：compare 不带占位单步重提（resolver_error 不算工具失败——不被禁提）
        _tool_call("compare.datasets", quoted="对比前两条"),
        _finish("1. 检索：已做（第1步）。\n2. 对比：没做（结果只有一条）。\n"
                "3. 对比：已做（第2步）。"),
    )
    plan, trace = _run("搜人类肺数据，对比前两条", model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["rank", "compare.datasets"]
    assert steps[1]["slots"] == {}, "重提的 compare 无槽位（默认对象）——resolver_error 不连坐禁提"
    skips = _ph_trace(trace, "批内依赖跳过")
    assert len(skips) == 1 and "越界" in skips[0]["detail"]
    assert not any("禁提" in t.get("detail", "") for t in trace), \
        "resolver_error 不算工具失败，不进 _failed_tool_ban"


# ---------------------------------------------------------------- 解析后全闸同口径

def test_cancelled_placeholder_extra_rejected_at_decide(monkeypatch):
    """解析前闸（decide 裁决层）先拦：占位续步原始 raw 带 cancelled=true 且原话无否定语素
    → 幻觉取消镜像闸在 `_validate_raw` 直接拦下（比 execute 更早）——该调用剔除回炉，
    compare 不执行、不记步；execute 侧的取消跳过分支是防御性的（正常批次到不了）。"""
    _install_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="看下库里多少条"),
        _batch(("rank", {"query": "人类肺", "quoted": "搜人类肺数据"}),
               ("compare.datasets", {"quoted": "对比前两条", "cancelled": True,
                                     "a": "$2.top[0].dataset_uid"})),
        _tool_call("compare.datasets", quoted="对比前两条"),
        _finish("1. 看库里多少条：已做（第1步）。\n2. 检索：已做（第2步）。\n"
                "3. 对比：你说了不做，已取消。"),
    )
    plan, trace = _run(UTTER, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["curate.db_status", "rank", "compare.datasets"]
    assert steps[2]["slots"] == {}, "取消的占位 compare 不当批执行，下一轮干净重提"
    decides = _nodes(trace, "decide")
    # 取消的 compare 在 `_batch_readonly_extras` 裁决层被剔除（幻觉取消镜像闸），
    # 零采纳 → 留痕措辞是的「按顺序先执行第一个」（既有语义）。
    assert any("按顺序先执行第一个" in t["detail"] for t in decides)


def test_main_step_placeholder_rejected_when_producer_missing(monkeypatch):
    """主步占位校验（施工修正）：占位只能引用**已执行**的 rank/rerank 步——decide 单发
    compare($1) 但第 1 步是 db_status（非生产者）→ violation → decide 带反馈重问一次 →
    模型改提无占位 compare → 正常执行。"""
    _install_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("curate.db_status", quoted="看下库里多少条"),
        _tool_call("compare.datasets", quoted="对比前两条",
                   a="$1.top[0].dataset_uid", b="$1.top[1].dataset_uid"),
        _tool_call("compare.datasets", quoted="对比前两条"),
        _finish("1. 看库里多少条：已做（第1步）。\n2. 对比：已做（第2步）。"),
    )
    plan, trace = _run(UTTER, model)
    steps = plan.get("steps") or []
    assert [s["verb"] for s in steps] == ["curate.db_status", "compare.datasets"]
    decides = _nodes(trace, "decide")
    assert any("没通过检查" in t["detail"] for t in decides), "主步占位（生产者缺失）先被拦下一次"


# ---------------------------------------------------------------- top digest / cite.export

def test_top_digest_carries_dataset_uid_and_rank():
    rows = [{"dataset_name": "A", "species": "Human", "tissue": "Lung",
             "disease": "", "source": "10x Genomics", "dataset_uid": "UID-A"},
            {"dataset_name": "B", "dataset_uid": "UID-B"}]
    digest = agent_exec._top_digest(rows, n=3)
    assert [d["dataset_uid"] for d in digest] == ["UID-A", "UID-B"]
    assert [d["rank"] for d in digest] == [1, 2]
    assert digest[0]["dataset_name"] == "A"


def test_cite_export_consumes_explicit_uids(monkeypatch, tmp_path):
    """cite.export uids 真实消费：提供 uids 清单 → 按清单导出（保序、不叠加
    limit、去空去重、≤20）；未提供 → 保持现状（当前结果集）。"""
    from dataset_recommender.content import reuse_pack as _rp

    records = [_rec("fake-a", "A"), _rec("fake-b", "B")]
    monkeypatch.setattr(agent_exec, "_full_corpus", lambda root: records)
    monkeypatch.setattr(_rp, "to_ris", lambda pack: "ris-text")
    monkeypatch.setattr(_rp, "to_bibtex", lambda pack: "bib-text")
    captured: list = []

    def _fake_pack(uids, records_):
        captured.append(list(uids))
        return {"items": [], "unresolved": [], "retrieval": {}, "uids": list(uids)}

    monkeypatch.setattr(_rp, "build_pack_for_uids", _fake_pack)

    res = agent_exec._loop_cite_export({"uids": ["fake-b", "fake-a"], "limit": 1}, tmp_path)
    assert res["uids"] == ["fake-b", "fake-a"], "按提供顺序导出，limit 不叠加"
    assert res["n_datasets"] == 2 and captured == [["fake-b", "fake-a"]]

    # 清洗：去空/去重/上限 20
    many = [f"u{i}" for i in range(21)]
    res2 = agent_exec._loop_cite_export({"uids": many + ["", "u0"]}, tmp_path)
    assert len(res2["uids"]) == 20 and res2["uids"][0] == "u0"
    assert "u20" not in res2["uids"]


def test_batch_results_do_not_carry_over_across_batches():
    """batch_results 局部化：解析源是 execute 局部 dict——同一函数两次调用
    （模拟两个批次）零残留：第二次空 resolved 即 dependency_unavailable，不引用上一批的值。"""
    rank_result = _rank_result("UID-A", "UID-B")
    # 第一批：有结果 → 解析成功
    slots, note, skip, resolved = agent_exec._resolve_placeholder_slots(
        {"a": "$1.top[0].dataset_uid"}, {1: rank_result}, "batch-1", 2)
    assert skip is None and slots == {"a": "UID-A"} and note
    assert resolved == ["a"], "只标记被解析的占位槽"

    # 第二批（新 execute 局部 dict）：无前序结果 → 依赖不可用，看不到第一批的值
    slots2, note2, skip2, _ = agent_exec._resolve_placeholder_slots(
        {"a": "$1.top[0].dataset_uid"}, {}, "batch-2", 2)
    assert skip2 == "dependency_unavailable" and "batch-2" in note2
    assert slots2 == {}, "跨批零残留"


def test_batch_id_is_per_execute_local(monkeypatch):
    """批 id 每次 execute 递增且只用于 trace——同一会话两批各得各的 id（不跨批复用）。"""
    _install_registry(monkeypatch)
    model = _FakeModel(
        _tool_call("rank", query="人类肺", quoted="搜人类肺数据"),
        _tool_call("compare.datasets", quoted="对比前两条",
                   a="$1.top[0].dataset_uid", b="$1.top[1].dataset_uid"),
        _finish("1. 检索：已做（第1步）。\n2. 对比前两条：已做（第2步）。"),
    )
    _, trace = _run("搜人类肺数据，对比前两条", model)
    resolves = _ph_trace(trace, "批内依赖解析")
    assert len(resolves) == 1
    assert "batch-" in resolves[0]["detail"]
