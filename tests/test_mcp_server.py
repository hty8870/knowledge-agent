# -*- coding: utf-8 -*-
"""MCP 层单测（此前零覆盖）：空查询守卫 + 诚实 advisory + 工具永不崩降级。

背景：`mcp_server.py` 封装确定性管线暴露成 MCP 工具，但一直没有 pytest 覆盖——
试跑发现「空查询返回 767 条任意结果」「宽泛查询（免疫细胞等未建模词被丢）静默返回不精确结果」
等诚实性问题后，补上守卫 + advisory，并用本文件把行为钉住。纯 MCP 层，不碰确定性评测。

v1.3.0 契约变更：**非法请求**（空 query / 枚举越界 / 非正 top_k / 未知来源 / 坏 uid）由「返回
{ok:false} dict」改为 **raise ToolError**（客户端 isError=true）。合法业务结果仍 ok=true。"""
import os

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from dataset_recommender.app import mcp_server as M


# ---- _advisory 纯函数（不触 I/O）----
def test_advisory_abstain_returns_none():
    assert M._advisory({"abstain": True, "constraints": {}}, 0) is None


def test_advisory_no_constraints_warns():
    a = M._advisory({"abstain": False, "constraints": {}, "has_raw_data_required": None}, 767)
    assert a and "未从查询中识别到" in a


def test_advisory_broad_species_platform_only_warns():
    intent = {"abstain": False,
              "constraints": {"species": ["human"], "platform": ["chromium"]},
              "has_raw_data_required": None}
    a = M._advisory(intent, 500)
    assert a and "宽泛" in a


def test_advisory_precise_query_is_none():
    intent = {"abstain": False,
              "constraints": {"species": ["human"], "disease": ["lung cancer"], "platform": ["chromium"]},
              "has_raw_data_required": True}
    assert M._advisory(intent, 13) is None


def test_advisory_broad_but_small_result_is_none():
    intent = {"abstain": False, "constraints": {"species": ["human"]}, "has_raw_data_required": None}
    assert M._advisory(intent, 50) is None  # result_total < 100 → 不提示，避免噪音


# ---- 工具级行为（懒加载真实管线）----
def test_recommend_empty_query_raises():
    """空 query = 非法请求 → raise ToolError（→ isError=true），消息带机器码 empty_query。"""
    for q in ["", "   ", "\n\t"]:
        with pytest.raises(ToolError, match="empty_query"):
            M.recommend_datasets(q)


def test_recommend_precise_query_ok_no_advisory():
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=3)
    assert r["ok"] is True
    assert r["advisory"] is None
    assert r["returned"] >= 1


def test_recommend_vague_query_has_advisory():
    r = M.recommend_datasets("我想要一些数据", top_k=3)
    assert r["ok"] is True
    assert r["advisory"] and "未从查询中识别到" in r["advisory"]


def test_manifest_bogus_uid_raises():
    """坏 uid = 非法请求 → raise ToolError（bad_uid），而非静默当成「无文件」。"""
    with pytest.raises(ToolError, match="bad_uid"):
        M.get_file_manifest("___definitely_not_a_uid___")


def test_parse_and_status_ok():
    assert M.parse_constraints("小鼠大脑")["ok"] is True
    st = M.biodata_status()
    assert st["ok"] is True and st["corpus_total"] > 0
    assert "llm" in st and "configured" in st["llm"]


def test_llm_status_default_is_local_and_secret_free(monkeypatch):
    """Default status must not call the network and must never expose a key or env-file path."""
    import dataset_recommender.llm.llm_client as lc

    monkeypatch.setattr(lc, "call_llm", lambda *a, **k: pytest.fail("default status must stay offline"))
    result = M.biodata_llm_status(check_connection=False)
    assert result["ok"] is True and result["checked"] is False
    serialized = repr(result).lower()
    assert "api_key" not in serialized and "llm_api_key" not in serialized
    assert "secrets_file" not in serialized and "llm.env" not in serialized


def test_llm_status_live_check_success_is_sanitized(monkeypatch):
    from dataset_recommender.llm.llm_client import LLMConfig, LLMResult
    import dataset_recommender.llm.llm_client as lc

    monkeypatch.setattr(
        lc,
        "load_llm_config",
        lambda project_root=None: LLMConfig(provider="openai-compatible", api_key="super-secret", model="test"),
    )
    monkeypatch.setattr(
        lc,
        "call_llm",
        lambda prompt, config: LLMResult(
            text="OK", attempted=True, succeeded=True, response_used=False,
            provider=config.provider, model=config.model,
        ),
    )
    result = M.biodata_llm_status(check_connection=True)
    assert result["connection"] == "success" and result["configured"] is True
    assert "super-secret" not in repr(result)


def test_llm_status_live_check_mock_reports_mock_without_network(monkeypatch):
    """mock 配置走 check_connection=True 必须报独立 mock 状态、不触网，且 --llm-check 退出 0。

    回归：此前 mock 的连通探测会调 call_mock_llm(retrieved_records=[]) → succeeded=false →
    被映射成 provider_error/connection=failed，与真·坏 provider 无法区分。"""
    from dataset_recommender.llm.llm_client import LLMConfig
    import dataset_recommender.llm.llm_client as lc

    monkeypatch.setattr(
        lc,
        "load_llm_config",
        lambda project_root=None: LLMConfig(provider="mock", mock_llm=True, api_key=None, model="mock-curator"),
    )
    monkeypatch.setattr(lc, "call_llm", lambda *a, **k: pytest.fail("mock status must not touch the network"))
    result = M.biodata_llm_status(check_connection=True)
    assert result["connection"] == "mock"
    assert result["configured"] is True and result["mock_mode"] is True
    assert result["error_code"] is None
    # 退出码契约：mock 与 success 同视为可用 → 0
    assert M._print_llm_check() == 0


# ---- 质量杠杆参数（recall / use_llm / rerank）：透传 + meta 如实标注 ----
class _FakeRes:
    """最小 WorkflowResult 替身：只提供 recommend_datasets 读取的字段（不触网络/模型）。"""
    def __init__(self, llm_response_used=False):
        self.result_total = 3
        self.retrieved_data = [{"dataset_uid": "x", "score": 1.0}]
        self.facets = []
        self.relaxation_options = []
        self.coverage_caveats = []   # 诚实降级：recommend_datasets 响应读取 res.coverage_caveats
        self.answer = "ok"
        self.llm_response_used = llm_response_used
        self.llm_attempted = False


class _RecordingWF:
    """记录 run_with_meta 收到的 kwargs，返回可配置替身（测参数管道，不测重排/LLM 质量）。"""
    def __init__(self, res=None):
        self.calls = []
        self._res = res or _FakeRes()

    def run_with_meta(self, p=None, **kwargs):
        # 生产调用点传 RecommendParams（位置参数）；兼容 kwargs 以防旧风格。
        self.calls.append(vars(p) if p is not None else kwargs)
        return self._res


def test_recommend_default_meta_deterministic():
    """默认调用（不传新参）→ 确定性/离线/无 LLM，candidates 非空（复刻旧行为）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=3)
    assert r["ok"] is True
    m = r["meta"]
    assert m["deterministic"] is True
    assert m["llm_used"] is False
    assert m["offline"] is True
    assert m["recall_used"] == "off" and m["rerank_used"] == "off"
    assert "notes" not in m  # 默认路径无告诫
    assert r["candidates"]   # 非空
    assert all(c.get("introduction", {}).get("summary") for c in r["candidates"])
    assert all(isinstance(c.get("introduction", {}).get("facts"), list) for c in r["candidates"])


def test_auto_llm_requires_explicit_permission_and_config(monkeypatch):
    from types import SimpleNamespace
    import dataset_recommender.llm.llm_client as lc

    wf = _RecordingWF()
    monkeypatch.setattr(M, "_workflow", lambda: wf)
    monkeypatch.setattr(lc, "load_llm_config", lambda **_kw: SimpleNamespace(api_key="configured"))

    M.recommend_datasets("immune response human data", strategy="auto")
    assert wf.calls[-1]["llm_available"] is False

    M.recommend_datasets("immune response human data", strategy="auto", auto_allow_llm=True)
    assert wf.calls[-1]["llm_available"] is True


def test_recommend_cross_encoder_unwarmed_falls_back(monkeypatch):
    """未预热 recall=cross_encoder → 稳定性护栏回退规则序（绝不在请求内加载 torch，避免 stdio 死锁）。"""
    wf = _RecordingWF()
    monkeypatch.setattr(M, "_workflow", lambda: wf)
    import dataset_recommender.retrieval.vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_ready", lambda b: False)   # 明确未预热
    r = M.recommend_datasets("小鼠大脑单细胞", recall="cross_encoder")
    assert r["ok"] is True
    assert wf.calls[0]["recall_backend"] == "off"          # 被护栏降级，绝不把 torch 后端透传进请求
    m = r["meta"]
    assert m["recall_used"] == "off"
    assert m["deterministic"] is True
    assert any("BIODATA_MCP_RECALL" in n and "回退" in n for n in m.get("notes", []))


def test_recommend_cross_encoder_warmed_passthrough(monkeypatch):
    """已预热（recall_backend_ready→True）→ recall=cross_encoder 透传、deterministic False、仍离线、无回退告诫。"""
    wf = _RecordingWF()
    monkeypatch.setattr(M, "_workflow", lambda: wf)
    import dataset_recommender.retrieval.vector_recall as vr
    monkeypatch.setattr(vr, "recall_backend_ready", lambda b: True)    # 模拟已在启动期预热
    r = M.recommend_datasets("小鼠大脑单细胞", recall="cross_encoder")
    assert r["ok"] is True
    assert wf.calls[0]["recall_backend"] == "cross_encoder"
    assert wf.calls[0]["use_llm"] is False and wf.calls[0]["rerank_backend"] == "off"
    m = r["meta"]
    assert m["deterministic"] is False
    assert m["recall_used"] == "cross_encoder"
    assert m["offline"] is True  # 本地重排仍离线
    assert not any("回退" in n for n in m.get("notes", []))


def test_recommend_bad_recall_param():
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("小鼠大脑", recall="bogus")


def test_recommend_bad_rerank_param():
    with pytest.raises(ToolError, match="bad_param"):
        M.recommend_datasets("小鼠大脑", rerank="bogus")


def test_recommend_use_llm_no_key_fail_open(monkeypatch):
    """use_llm=True 无 key → run_with_meta 收到 use_llm=True；fail-open 回退→ ok True、
    结果非空、llm_used False（润色未真用上）、deterministic False（请求了 LLM 路径）。
    用替身模拟无 key 回退，避免依赖真实 key / 触发联网。"""
    wf = _RecordingWF(res=_FakeRes(llm_response_used=False))
    monkeypatch.setattr(M, "_workflow", lambda: wf)
    r = M.recommend_datasets("人类肺癌的单细胞数据", top_k=3, use_llm=True)
    assert r["ok"] is True
    assert wf.calls[0]["use_llm"] is True and wf.calls[0]["mock_llm"] is False
    assert r["returned"] >= 1
    m = r["meta"]
    assert m["llm_used"] is False      # 无 key → 润色未真用上
    assert m["deterministic"] is False # 请求了 LLM 路径 → 非确定性意图
    assert m["offline"] is False
    assert any("LLM 路径" in n for n in m.get("notes", []))


# ---- 内置自检 / 版本 / 帮助（--selfcheck / --version / --help）----
def test_status_reports_version_and_tools_single_source():
    """biodata_status 的 tools 与 server_version 与模块常量单一真源，不得漂移
    （self-check 期望的工具集 = status 上报的工具集）。v1.9.0 加 browse_datasets /
    get_dataset_introduction；v1.10.0 加 upload_dataset（第 8 个）；v1.13.0 加 build_reuse_pack
    （第 10 个）→ 必须同步 _EXPECTED_TOOLS，本测钉死。v1.18.0 加 lookup_identifier（第 11 个）。
    v1.28.0 加 plan_action（第 17 个：一句话 → 封闭动词表里的一个动作，只出计划不执行）。
    v1.29.0 加 provision_dataset（第 18 个：按需真下载到调用方指定 dest_dir，写工具 ②）。
    v1.30.0 加 curate_datasets（第 19 个：对话式数据库管护，写工具 ③，plan 零写盘 apply 才写盘/联网）。"""
    st = M.biodata_status()
    assert st["ok"] is True
    assert st["server_version"] == M._SERVER_VERSION
    assert st["tools"] == list(M._EXPECTED_TOOLS)
    assert set(M._EXPECTED_TOOLS) == {
        "recommend_datasets", "get_file_manifest", "parse_constraints",
        "browse_datasets", "get_dataset_introduction", "assess_dataset_fair",
        "build_reuse_pack", "lookup_identifier", "find_compatible_datasets",
        "assess_feasibility", "plan_query_edit", "plan_action", "build_task_pack",
        "verify_local_assets", "provision_dataset", "curate_datasets", "upload_dataset",
        "biodata_llm_status", "biodata_status",
    }


def test_plan_action_tool_only_plans_and_never_returns_prose_claims(monkeypatch):
    """第 17 工具的两条硬约束：

    1. **只出计划**——返回体里不许出现任何产物字段。
    2. **不回成句中文断言**——`has_results` / `result_total` 是调用方自述的，服务端无从核实；
       拿没核实的数字发「已为你打包 20 条」这种断言就是编造。

    `ENABLE_LLM=false` 是**必需的**：`resolve_enable_llm` 的默认值是「有 key 就开」，
    而 `plan_action` 一被调用就会去打 provider。本条第一版没关它，于是在配了 key 的机器上
    真发了一次网络请求——失败得快，看起来照样绿，注释里那句「无 key 时走规则档」也就成了假话
    （真相是「网络调用失败了才走规则档」）。单元测试不许依赖外部服务，也不许烧别人的额度。
    """
    monkeypatch.setenv("ENABLE_LLM", "false")
    out = M.plan_action("帮我打包前20条", has_results=True, result_total=42)
    assert out["ok"] is True
    plan = out["plan"]
    assert plan["source"] == "rule" and plan["llm_status"] == "disabled", (
        f"本条必须零网络，实际 source={plan['source']} llm_status={plan['llm_status']}"
    )
    assert plan["verb"] == "pack.preview"          # 规则档绝不直接产文件
    for forbidden in ("plan_token", "pack", "zip", "download_script", "files", "results", "uids"):
        assert forbidden not in plan
    # 结构化字段可以有数字，但不许出现「已…42…」这类成句断言
    import json as _json
    text = _json.dumps(plan, ensure_ascii=False)
    assert "已为你" not in text and "已打包" not in text


def test_plan_action_tool_refuses_empty_and_oversized_input():
    import pytest as _pytest
    with _pytest.raises(Exception) as e1:
        M.plan_action("   ")
    assert "empty_input" in str(e1.value)
    with _pytest.raises(Exception) as e2:
        M.plan_action("打" * 501)
    assert "too_large" in str(e2.value)


def test_print_version_and_usage(capsys):
    rc = M._print_version()
    out = capsys.readouterr().out
    assert rc == 0
    assert M._SERVER_VERSION in out and "MCP SDK" in out
    # 帮助必须点名内置自检，且 CLI flag 集合与 __main__ 分派保持一致
    assert "--selfcheck" in M._USAGE and "--llm-check" in M._USAGE
    assert M._CLI_FLAGS == {"--help", "-h", "--version", "-V", "--selfcheck", "--llm-check"}


def test_selfcheck_real_protocol_passes():
    """真协议自检集成测试：spawn 子进程走 initialize→tools/list→biodata_status→parse_constraints。
    库存/下载索引未就绪的裸环境自动跳过（自检按设计会判 FAIL，非本测试关注点）。"""
    import pytest
    st = M.biodata_status()
    if not (st.get("ok") and st.get("download_index_ready") and st.get("corpus_total", 0) > 0):
        pytest.skip("库存/下载索引未就绪，跳过 --selfcheck 集成测试")
    assert M._selfcheck() == 0


def test_cli_flag_dispatch_exit_codes(tmp_path):
    """__main__ 参数分派：--help/-h、--version/-V 退出 0；任何未识别参数（含**漏横杠**的 `selfcheck`）
    退出 2 且**绝不挂起**——不能静默起服务器挂死在 stdin 上。stdin=DEVNULL + timeout 双保险：
    即便回归成服务器模式也会读到 EOF 快退（码≠2）而非挂死。"""
    import subprocess
    import sys as _sys
    from pathlib import Path as _P
    server = str(_P(M.__file__).resolve())
    no_key_env = tmp_path / "mcp-no-key.env"
    no_key_env.write_text(
        "ENABLE_LLM=false\n"
        "LLM_PROVIDER=openai-compatible\n"
        "LLM_API_KEY=your_api_key_here\n"
        "LLM_BASE_URL=http://127.0.0.1:9/v1\n"
        "LLM_MODEL=test-no-network\n",
        encoding="utf-8",
    )
    child_env = dict(os.environ)
    child_env["BIODATA_LLM_ENV_FILE"] = str(no_key_env.resolve())
    child_env["LLM_API_KEY"] = "your_api_key_here"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    for alias in ("OPENAI_API_KEY", "ZAI_API_KEY", "ZHIPUAI_API_KEY", "ZHIPUAI_TOKEN"):
        child_env.pop(alias, None)

    def _run(*flags):
        # 子进程在 CLI 分支下把输出转 UTF-8，故这里也以 UTF-8 解码（errors=replace 防残留字节炸线程）。
        # stdin=DEVNULL：万一某参数回归成服务器模式，读到 EOF 会立刻退出而非挂在 stdin 上。
        return subprocess.run(
            [_sys.executable, "-B", server, *flags],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
            env=child_env,
        )

    assert _run("--help").returncode == 0
    assert _run("-h").returncode == 0
    assert _run("--version").returncode == 0
    assert _run("-V").returncode == 0
    llm = _run("--llm-check")
    assert llm.returncode == 1 and '"error_code": "missing_key"' in llm.stdout
    assert _run("--totally-bogus-flag").returncode == 2   # 未知 dash 参数
    assert _run("selfcheck").returncode == 2              # 漏横杠的拼写：也报错、绝不挂起（防挂死不限 dash）


def test_server_mode_stdout_is_pure():
    """神圣不变量守卫（数据无关、不可跳过）：服务器模式（无参数）**绝不写 stdout**——MCP 协议独占它。
    以无参数 spawn 服务器 + stdin=DEVNULL（读到 EOF 即退），断言 returncode==0 且 stdout 为空字节。
    任何预热/日志只能进 stderr；若有人往服务器分支或懒加载路径误加 print(stdout) 会当场被本测抓到。"""
    import subprocess
    import sys as _sys
    from pathlib import Path as _P
    server = str(_P(M.__file__).resolve())
    r = subprocess.run(
        [_sys.executable, "-B", server],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )
    assert r.returncode == 0, f"服务器未在 stdin EOF 时干净退出：code={r.returncode} stderr={r.stderr[:400]!r}"
    assert r.stdout == b"", f"服务器模式污染了 stdout（协议流）：{r.stdout[:400]!r}"


def test_registered_tools_match_expected_constant():
    """真单一真源：把 FastMCP **实际注册**的工具名集与 _EXPECTED_TOOLS 对齐（不是常量与自身比）。
    删 @mcp.tool() 装饰器、或新增第 5 个工具却忘了同步常量，本测都能抓到（补 status DRY 测的盲区）。"""
    import asyncio
    names = {t.name for t in asyncio.run(M.mcp.list_tools())}
    assert names == set(M._EXPECTED_TOOLS), f"注册工具集与 _EXPECTED_TOOLS 漂移：注册={sorted(names)}"


def test_selfcheck_import_failure_returns_1(monkeypatch, capsys):
    """_selfcheck 的 FAIL 路径护栏：mcp 客户端 API 导入失败 → 必须返回 1 并打印 SELFCHECK_FAIL。
    纯进程内、不依赖数据/子进程；防「护栏被写反（如 return 0）却无人察觉」。"""
    import sys as _sys
    monkeypatch.setitem(_sys.modules, "mcp", None)   # 令 _selfcheck 内 `from mcp import ...` 抛 ImportError
    rc = M._selfcheck()
    out = capsys.readouterr().out
    assert rc == 1
    assert "SELFCHECK_FAIL" in out


# ---- 活台账露出：get_file_manifest 逐文件状态 + 问题清单 + provisioning（v1.2.0）----
def _pick_uid_by_problem(want_problem: bool):
    """从活台账 current.json 挑一个（有/无）问题文件的多文件数据集 uid；无则 None（测试跳过）。"""
    import json
    from pathlib import Path
    from dataset_recommender.corpus import inspection
    try:
        cur = json.loads(Path(inspection._DATA_PATH).read_text(encoding="utf-8"))
    except Exception:
        return None
    for uid, r in cur.get("by_uid", {}).items():
        has_prob = r.get("np", 0) > 0
        if has_prob == want_problem and r.get("nf", 0) > 1:
            return uid
    return None


def test_manifest_attaches_status_and_problems():
    """问题数据集：每个文件挂 status；problems 清单条数 == provisioning.n_problem；
    verification 升级为真实快照 + 问题计数，且**保留**旧键 links_snapshot（additive）。"""
    import pytest
    uid = _pick_uid_by_problem(want_problem=True)
    if not uid:
        pytest.skip("活台账无问题数据集")
    r = M.get_file_manifest(uid)
    assert r["ok"] is True
    prov = r["provisioning"]
    assert isinstance(prov, dict) and prov["n_problem"] >= 1
    assert isinstance(r["problems"], list) and len(r["problems"]) == prov["n_problem"]
    assert all(p.get("reason") for p in r["problems"])
    assert all("status" in f for f in r["files"] if isinstance(f, dict))
    v = r["verification"]
    assert v["links_snapshot"] == M._LEDGER_SNAPSHOT          # 旧键仍在
    assert v["n_problem_files"] == prov["n_problem"] and v["snapshot_id"]


def test_manifest_clean_dataset_no_problems():
    """干净数据集：problems 为空、provisioning_ok True、每个文件 status.problem 为 False。"""
    import pytest
    uid = _pick_uid_by_problem(want_problem=False)
    if not uid:
        pytest.skip("活台账无干净多文件数据集")
    r = M.get_file_manifest(uid)
    assert r["ok"] is True and r["problems"] == []
    assert r["provisioning"]["provisioning_ok"] is True
    assert all((f["status"] is None or f["status"]["problem"] is False)
               for f in r["files"] if isinstance(f, dict))


def test_status_reports_inspection_ready():
    """biodata_status 附活台账就绪与快照日期（additive，不动既有键）。"""
    st = M.biodata_status()
    assert st["ok"] is True
    assert st["inspection_ready"] is True
    assert st["inspection_snapshot"]


# ============ v1.9.0 前端能力同步：recommend 新参数 + browse + introduction ============
def test_recommend_new_params_passthrough_and_echo(monkeypatch):
    """facet_filters / suppressed_constraints / date_from/to 净化后透传给 run_with_meta，
    并按净化结果回显 applied_facets / applied_suppressed（与 Web 共用 sanitize，单一真源）。"""
    wf = _RecordingWF()
    monkeypatch.setattr(M, "_workflow", lambda: wf)
    r = M.recommend_datasets(
        "人类肺癌",
        facet_filters=[{"dim": "species", "value": "Homo Sapiens"},   # 自由文本维度净化为小写
                       {"dim": "bogus", "value": "x"}],                # 未知维度净化丢弃
        suppressed_constraints=["include:species", "not_a_filter"],    # 非法项净化丢弃
        lenient_dims=["tissue", "bogus", "disease"],                   # 诚实降级：净化白名单 + 排序回显
        date_from="2020-05-01",
        date_to="2021-12-31",                                          # 合法 ISO → 原样透传
    )
    call = wf.calls[-1]
    assert call["facet_filters"] == [{"dim": "species", "value": "homo sapiens"}]
    assert call["suppressed_constraints"] == ["include:species"]
    assert call["lenient_dims"] == ["disease", "tissue"]              # 净化去 bogus + 排序透传
    assert call["date_from"] == "2020-05-01" and call["date_to"] == "2021-12-31"
    assert r["applied_facets"] == [{"dim": "species", "value": "homo sapiens"}]
    assert r["applied_suppressed"] == ["include:species"]
    assert r["applied_lenient"] == ["disease", "tissue"]             # 回显净化结果
    assert isinstance(r["coverage_caveats"], list)                    # 覆盖缺口字段恒在


def test_recommend_default_new_params_are_noop():
    """不传新参 → applied_* 为空、query_constraints 恒在（默认逐位复刻旧行为）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=3)
    assert r["ok"] is True
    assert r["applied_facets"] == [] and r["applied_suppressed"] == [] and r["applied_lenient"] == []
    assert isinstance(r["query_constraints"], list) and isinstance(r["coverage_caveats"], list)
    assert all("filter_id" in x for x in r["query_constraints"])


def test_recommend_facet_filter_narrows_to_facet_count():
    """分面细化：挑一个 facets 值回传 → result_total 收窄到该分面计数（计数与过滤同源）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=20)
    picked = None
    for g in r["facets"]:
        if g.get("values"):
            picked = ({"dim": g["dim"], "value": g["values"][0]["value"]}, g["values"][0]["count"])
            break
    assert picked, "需要至少一个分面值来验证"
    ff, want = picked
    rr = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=20, facet_filters=[ff])
    assert rr["ok"] is True and rr["applied_facets"] == [ff]
    assert rr["result_total"] == want


def test_recommend_suppress_broadens_or_equal():
    """忽略一个已命中维度 → 放宽约束，命中总数不减（放宽后是超集）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=3)
    fid = r["query_constraints"][0]["filter_id"]
    rs = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=3, suppressed_constraints=[fid])
    assert rs["ok"] is True and rs["applied_suppressed"] == [fid]
    assert rs["result_total"] >= r["result_total"]


def test_recommend_explicit_date_overrides_and_malformed_rejected():
    """显式 ISO 时间范围覆盖 NL 相对时间；非法日期不再是「忽略=不限」——
    拉齐为严格档：与网页端同口径，bad_param 点名参数。"""
    rd = M.recommend_datasets("人类单细胞数据", top_k=3, date_from="2024-01-01", date_to="2025-12-31")
    assert rd["ok"] is True
    assert rd["understood"]["date_from"] == "2024-01-01" and rd["understood"]["date_to"] == "2025-12-31"
    with pytest.raises(ToolError, match=r"bad_param: date_from 需要 YYYY-MM-DD 格式的日期"):
        M.recommend_datasets("人类单细胞数据", top_k=3, date_from="notadate")


# ---- 严格日期档：非法格式 / 日历不存在 / 倒挂窗口 / 边界 from==to ----
def test_recommend_date_invalid_format_and_nonexistent_calendar_day_rejected():
    """非 YYYY-MM-DD、日历不存在的日期 → bad_param，消息点名参数与问题（措辞对齐 Web 400）。"""
    with pytest.raises(ToolError, match=r"bad_param: date_to 需要 YYYY-MM-DD 格式的日期"):
        M.recommend_datasets("人类单细胞数据", top_k=3, date_to="2024/01/01")
    with pytest.raises(ToolError, match=r"bad_param: date_from 不是真实存在的日期"):
        M.recommend_datasets("人类单细胞数据", top_k=3, date_from="2026-02-30")


def test_recommend_inverted_date_window_rejected():
    """from > to 倒挂窗口 → bad_param 点名颠倒（措辞对齐 Web「发表时间范围颠倒」400）。"""
    with pytest.raises(ToolError, match=r"bad_param: 发表时间范围颠倒"):
        M.recommend_datasets("人类单细胞数据", top_k=3, date_from="2025-01-01", date_to="2024-01-01")


def test_recommend_date_boundary_equal_from_to_ok():
    """边界：from == to 是合法单天窗，不误伤。"""
    r = M.recommend_datasets("人类单细胞数据", top_k=3, date_from="2020-06-01", date_to="2020-06-01")
    assert r["ok"] is True
    assert r["understood"]["date_from"] == "2020-06-01" and r["understood"]["date_to"] == "2020-06-01"


def test_browse_datasets_basic_and_self_consistent():
    """browse：全库并列 + 分面自洽（source 分面计数之和 = total）+ 精简记录（不含介绍）。"""
    b = M.browse_datasets()
    assert b["ok"] is True and b["total"] > 0
    assert b["returned"] <= b["limit"] == 50
    assert sum(f["count"] for f in b["facets"]["source"]) == b["total"]
    assert b["records"] and all(r.get("dataset_uid") is not None for r in b["records"])
    assert "introduction" not in b["records"][0]   # 列表精简、不内嵌介绍


def test_browse_datasets_filter_and_pagination():
    """按来源过滤 total == 该来源分面计数；分页 offset 生效；来源过滤大小写不敏感。"""
    b = M.browse_datasets()
    src = b["facets"]["source"][0]["value"]
    cnt = b["facets"]["source"][0]["count"]
    bf = M.browse_datasets(source=src, limit=5)
    assert bf["ok"] is True and bf["total"] == cnt
    assert all(r["source"] == src for r in bf["records"]) and bf["returned"] <= 5
    assert M.browse_datasets(source=src.upper(), limit=1)["total"] == cnt  # 大小写不敏感
    p2 = M.browse_datasets(source=src, limit=5, offset=5)
    assert p2["offset"] == 5


@pytest.mark.parametrize("kw", [{"limit": 0}, {"limit": 999}, {"offset": -1}])
def test_browse_datasets_bad_params_raise(kw):
    with pytest.raises(ToolError, match="bad_param"):
        M.browse_datasets(**kw)


def test_browse_unknown_filter_is_zero_not_error():
    """未知过滤取值 → 命中 0 条属正常业务结果、非报错（0 与写错来源在 browse 语义下不区分）。"""
    b = M.browse_datasets(species="不存在的物种xyz")
    assert b["ok"] is True and b["total"] == 0 and b["records"] == []


def test_get_dataset_introduction_by_uid():
    """recommend 拿 uid → get_dataset_introduction 回同口径确定性介绍（summary + facts）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=1)
    uid = r["candidates"][0]["dataset_uid"]
    gi = M.get_dataset_introduction(uid=uid)
    assert gi["ok"] is True and gi["matched"]["dataset_uid"] == uid
    assert gi["introduction"]["summary"]
    assert isinstance(gi["introduction"]["facts"], list) and len(gi["introduction"]["facts"]) >= 6


def test_get_dataset_introduction_empty_key_raises():
    with pytest.raises(ToolError, match="empty_key"):
        M.get_dataset_introduction()


def test_get_dataset_introduction_not_found_raises():
    with pytest.raises(ToolError, match="not_found"):
        M.get_dataset_introduction(uid="___definitely_not_a_uid___")


def test_assess_dataset_fair_by_uid():
    """recommend 拿 uid → assess_dataset_fair 回 13 项 FAIR 自检 + 英文 DAS（确定性、离线）。"""
    r = M.recommend_datasets("人类肺癌的单细胞数据，要有FASTQ", top_k=1)
    uid = r["candidates"][0]["dataset_uid"]
    fa = M.assess_dataset_fair(uid=uid)
    assert fa["ok"] is True and fa["matched"]["dataset_uid"] == uid
    fair = fa["fair_report"]["fair"]
    assert len(fair["checks"]) == 13
    s = fair["summary"]
    assert s["pass"] + s["partial"] + s["unknown"] == s["total"] == 13
    das = fa["fair_report"]["data_availability"]
    assert isinstance(das["statement"], str) and das["statement"].endswith(".")


def test_assess_dataset_fair_empty_key_raises():
    with pytest.raises(ToolError, match="empty_key"):
        M.assess_dataset_fair()


def test_assess_dataset_fair_not_found_raises():
    with pytest.raises(ToolError, match="not_found"):
        M.assess_dataset_fair(uid="___definitely_not_a_uid___")


# ---- 工具 8：upload_dataset（v1.10.0；唯一写工具，写目标重定向到 tmp，绝不污染真实 external/）----
import json as _json  # noqa: E402
from pathlib import Path  # noqa: E402


@pytest.fixture
def mcp_tmp_root(tmp_path, monkeypatch):
    """把 upload_dataset 的写目标重定向到临时仓库根：s.project_root=tmp → 写入 tmp/database/external/，
    绝不碰真实 database/external/ 或 database/base/。upload 工具只读 s.project_root，故 SimpleNamespace 足够。"""
    from types import SimpleNamespace
    base = tmp_path / "database" / "base"
    base.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(M, "_settings", lambda: SimpleNamespace(project_root=tmp_path, data_dir=base))
    return tmp_path


def test_upload_dataset_records_writes_external_not_base(mcp_tmp_root):
    """records（结构化数组）上传：落 database/external/（upload_ 前缀）+ 逐条打标 source + base 一尘不染 + MCP 溯源 note。"""
    r = M.upload_dataset(
        records=[{"dataset_name": "MCP 上传集", "species": "Human", "url": "https://e/x"}],
        filename="my.json", source="张三实验室",
    )
    assert r["ok"] is True and r["record_count"] == 1
    assert r["saved_to"].startswith("database/external/")
    assert Path(r["saved_to"]).name.startswith("upload_") and r["saved_to"].endswith("my.json")
    assert r["sources"] == {"张三实验室": 1}
    assert "next" in r  # 易用性：给出「怎么查到刚上传的数据」的下一步提示
    saved = mcp_tmp_root / r["saved_to"]
    assert saved.is_file()
    # 冻结安全：base 目录一尘不染（上传绝不落 base）
    assert list((mcp_tmp_root / "database" / "base").iterdir()) == []
    disk = _json.loads(saved.read_text(encoding="utf-8"))
    assert disk["records"][0]["source"] == "张三实验室"   # 逐条打标
    assert "MCP" in disk["note"]                           # MCP 溯源备注（区别于网页端）


def test_upload_dataset_accepts_wrapper_object(mcp_tmp_root):
    """records 也接受包裹对象 { records:[…] }（extract_records 兼容，与 Web 同口径）。"""
    r = M.upload_dataset(
        records={"records": [{"dataset_name": "包裹集", "species": "Mouse"}]}, source="包裹来源",
    )
    assert r["ok"] is True and r["record_count"] == 1 and r["sources"] == {"包裹来源": 1}


def test_upload_dataset_visible_after_ingest(mcp_tmp_root):
    """上传后 invalidate_external_cache → load_full_corpus 立刻看到该来源（即时可检索，端到端易用性）。"""
    from dataset_recommender.corpus.corpus import load_full_corpus, source_of
    r = M.upload_dataset(records=[{"dataset_name": "即时可见集", "species": "Mouse"}], source="即时来源")
    assert r["ok"] is True
    full = load_full_corpus(mcp_tmp_root / "database" / "base", mcp_tmp_root)
    assert any(source_of(rec) == "即时来源" for rec in full)


def test_upload_dataset_by_path_reads_file(tmp_path, mcp_tmp_root):
    """path 入参：从本地 JSON 文件读入字节（默认落盘名取 path 叶子），写入仍落 external/。"""
    src = tmp_path / "incoming.json"
    src.write_text(_json.dumps([{"dataset_name": "来自文件", "species": "Human"}]), encoding="utf-8")
    r = M.upload_dataset(path=str(src), source="文件来源")
    assert r["ok"] is True and r["record_count"] == 1
    assert Path(r["saved_to"]).name.startswith("upload_") and r["saved_to"].endswith("incoming.json")


def test_upload_dataset_warnings_surface_missing_name_and_bad_species(mcp_tmp_root):
    """易用性：缺 dataset_name / 物种非通用名 → 可读 warnings 当场可见（格式错误不被误读）。"""
    r = M.upload_dataset(
        records=[{"species": "Homo sapiens"}, {"dataset_name": "有名字", "species": "外星生物"}],
        filename="w.json",
    )
    assert r["ok"] is True and r["record_count"] == 2
    joined = " ".join(r["warnings"])
    assert "dataset_name" in joined and "通用名" in joined


def test_upload_dataset_empty_input_raises(mcp_tmp_root):
    with pytest.raises(ToolError, match="empty_input"):
        M.upload_dataset()
    with pytest.raises(ToolError, match="empty_input"):
        M.upload_dataset(path="   ")   # 纯空白 path 视为未提供


def test_upload_dataset_both_records_and_path_raises(tmp_path, mcp_tmp_root):
    with pytest.raises(ToolError, match="bad_param"):
        M.upload_dataset(records=[{"dataset_name": "a"}], path=str(tmp_path / "x.json"))


def test_upload_dataset_path_not_found_raises(tmp_path, mcp_tmp_root):
    with pytest.raises(ToolError, match="not_found"):
        M.upload_dataset(path=str(tmp_path / "does_not_exist.json"))


def test_upload_dataset_non_json_filename_raises(mcp_tmp_root):
    with pytest.raises(ToolError, match="bad_file"):
        M.upload_dataset(records=[{"dataset_name": "a"}], filename="data.txt")


def test_upload_dataset_invalid_json_path_raises(tmp_path, mcp_tmp_root):
    """path 文件是合法 UTF-8 但非合法 JSON → invalid_json。"""
    bad = tmp_path / "bad.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ToolError, match="invalid_json"):
        M.upload_dataset(path=str(bad))


def test_upload_dataset_no_records_raises(mcp_tmp_root):
    """合法 JSON 但无数据集记录（空数组）→ no_records（非静默写空文件）。"""
    with pytest.raises(ToolError, match="no_records"):
        M.upload_dataset(records=[], filename="x.json")


def test_upload_dataset_does_not_touch_base_on_error(mcp_tmp_root):
    """稳定性/隔离：非法上传报错后，tmp/database/external 与 base 都不留半个文件（写只在校验全过后发生）。"""
    ext = mcp_tmp_root / "database" / "external"
    with pytest.raises(ToolError, match="no_records"):
        M.upload_dataset(records=[], filename="x.json")        # 过 _settings/进 ingest，但校验失败不写
    with pytest.raises(ToolError, match="empty_input"):
        M.upload_dataset(filename="x.json")                    # 连 _settings 都不到
    assert not ext.exists() or list(ext.iterdir()) == []
    assert list((mcp_tmp_root / "database" / "base").iterdir()) == []
