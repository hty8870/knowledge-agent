# -*- coding: utf-8 -*-
"""成功经验 few-shot 库（Vanna auto_train 式）的确定性门。

两级结构：机械收录只进**候选池**（`curate_example_candidates.jsonl`，不注入）；
用户在记忆模块勾选后才迁入**正式库**（`curate_examples.jsonl`，注入侧只读它）。

钉死：一遍过才进候选池（失败/取消/非管护/被闸修过不录）；逐字重复不录；旋转上界；
勾选入库/忽略/去重/分区隔离；关键词重叠检索；prompt 注入段格式与 fail-open
（空账 → 空串，build_action_prompt 与历史逐位一致）。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dataset_recommender.agent import action_plan as AP  # noqa: E402
from dataset_recommender.agent import agent_exec as AX  # noqa: E402


def _step(verb, ok=True, slots=None):
    return {"verb": verb, "ok": ok, "slots": dict(slots or {})}


def _rows(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _cand_rows(root: Path) -> list[dict]:
    return _rows(root / ".userdata" / AX._EXAMPLE_CANDIDATES_NAME)


def _ledger_rows(root: Path) -> list[dict]:
    return _rows(root / ".userdata" / AX._EXAMPLES_LEDGER_NAME)


def test_success_is_recorded(tmp_path):
    """干净成功 → 进候选池（带 id），**正式库保持空**（用户没勾选就不注入）。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.remove", slots={"target": "upload_a.json"})])
    rows = _cand_rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["utterance"] == "把 upload_a.json 删掉"
    assert rows[0]["steps"][0]["verb"] == "curate.remove"
    assert "upload_a.json" in rows[0]["steps"][0]["args"]
    assert rows[0]["id"], "候选行必须带 id（勾选/忽略凭它定位）"
    assert _ledger_rows(tmp_path) == [], "候选≠入库：勾选前正式库必须是空的"


def test_failed_or_cancelled_or_non_curate_not_recorded(tmp_path):
    AX._maybe_record_success(tmp_path, "联网搜 human lung 入库", {},
                             [_step("curate.search_online", ok=False)])          # 失败步
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {"cancelled": True},
                             [_step("curate.remove")])                            # 取消态
    AX._maybe_record_success(tmp_path, "把结果打包", {},
                             [_step("pack.download")])                            # 非管护动词
    AX._maybe_record_success(tmp_path, "", {}, [_step("curate.db_status")])       # 空原话
    AX._maybe_record_success(tmp_path, "库里有什么", {}, [])                      # 零步骤
    assert _cand_rows(tmp_path) == []


def test_readonly_curate_session_is_recorded(tmp_path):
    """纯只读管护会话（db_status）也是成功样例——「库里有什么」→ db_status 的表达对齐正缺它。"""
    AX._maybe_record_success(tmp_path, "库里现在有什么", {}, [_step("curate.db_status")])
    rows = _cand_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["steps"][0]["verb"] == "curate.db_status"


def test_verbatim_duplicate_is_not_recorded(tmp_path):
    for _ in range(2):
        AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                                 [_step("curate.remove", slots={"target": "upload_a.json"})])
    assert len(_cand_rows(tmp_path)) == 1


def test_rotation_keeps_most_recent(tmp_path):
    path = tmp_path / ".userdata" / AX._EXAMPLE_CANDIDATES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for i in range(AX._EXAMPLES_MAX_ROWS):
            fh.write(json.dumps({"utterance": f"第{i}句", "steps": [{"verb": "curate.db_status"}]},
                                ensure_ascii=False) + "\n")
    AX._maybe_record_success(tmp_path, "最新一句", {}, [_step("curate.db_status")])
    rows = _cand_rows(tmp_path)
    assert len(rows) == AX._EXAMPLES_MAX_ROWS
    assert rows[-1]["utterance"] == "最新一句"
    assert rows[0]["utterance"] == "第1句", "最旧的一行被旋掉"


def test_retrieval_ranks_by_overlap(tmp_path):
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    seed = [
        {"utterance": "把 upload_a.json 删掉", "steps": [{"verb": "curate.remove", "args": "curate.remove（target=upload_a.json）"}]},
        {"utterance": "检查数据库来源有没有更新", "steps": [{"verb": "curate.check_updates", "args": "curate.check_updates"}]},
        {"utterance": "把 upload_b.json 也删掉", "steps": [{"verb": "curate.remove", "args": "curate.remove（target=upload_b.json）"}]},
    ]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in seed), encoding="utf-8")
    hits = AX._load_success_examples(tmp_path, "把 upload_c.json 删掉")
    assert hits and all("删" in h["utterance"] for h in hits)
    assert not any("更新" in h["utterance"] for h in hits), "零重叠样例不许注入"
    # 阈值闸：与原话几乎零重叠时不返回
    assert AX._load_success_examples(tmp_path, "斑马鱼") == []


def test_prompt_section_format_and_failopen(tmp_path):
    assert AX._examples_prompt_zh(tmp_path, "随便一句") == "", "空账 → 空串（fail-open）"
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"utterance": "把 upload_a.json 删掉",
         "steps": [{"verb": "curate.remove", "args": "curate.remove（target=upload_a.json）"}]},
        ensure_ascii=False) + "\n", encoding="utf-8")
    zh = AX._examples_prompt_zh(tmp_path, "把 upload_b.json 删掉")
    assert "历史成功操作" in zh and "把 upload_a.json 删掉" in zh and "curate.remove" in zh
    assert "以当前这句为准" in zh


def test_build_action_prompt_examples_zh_is_additive():
    base = AP.build_action_prompt("把结果打包", has_results=True, result_total=5)
    same = AP.build_action_prompt("把结果打包", has_results=True, result_total=5, examples_zh="")
    assert base == same, "examples_zh 缺省/空串必须与历史逐位一致"
    richer = AP.build_action_prompt("把结果打包", has_results=True, result_total=5,
                                    examples_zh="----- 历史成功操作 -----\n用户说：「x」→ 正确动作：curate.list")
    assert "历史成功操作" in richer and richer.startswith(base[:200])


def test_context_zh_places_examples_before_the_utterance():
    ctx = AX._context_zh("把 upload_b.json 删掉", has_results=False, result_total=0,
                         retrieval=None, current_query="", current_filters=None,
                         examples_zh="----- 历史成功操作 -----\n用户说：「a」→ 正确动作：curate.list")
    assert ctx.index("历史成功操作") < ctx.index("----- 用户这一句 -----")


# ---------------------------------------------------------------- 分区（验证）
#
# 原话是隐私面：账本行打（principal 会话账户 + endpoint_fp 端点指纹）双键标，注入只取同分区行。
# 存量行（分区前落盘、无字段）按 ("anonymous","") 计——宁可少注不泄漏。

def test_partitioned_record_and_load(tmp_path):
    """同分区才可见/可入库/可注入：换账户/换端点都看不见也入不了；缺省（内部直调）不过滤。"""
    steps = [_step("curate.remove", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps,
                             principal="u-alice", endpoint_fp="ep-1")
    # 勾选前：同分区也检索不到（候选≠入库）
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp="ep-1") == []
    # 跨账户勾选：入不了（别人的候选不归你批）
    assert AX.approve_example_candidates(tmp_path, [r["id"] for r in _cand_rows(tmp_path)],
                                         principal="u-bob", endpoint_fp="ep-1") == {"approved": 0, "duplicated": 0}
    assert _ledger_rows(tmp_path) == [] and len(_cand_rows(tmp_path)) == 1
    # 同分区勾选 → 入库 → 同分区可注入
    ids = [r["id"] for r in AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp="ep-1")]
    assert AX.approve_example_candidates(tmp_path, ids,
                                         principal="u-alice", endpoint_fp="ep-1") == {"approved": 1, "duplicated": 0}
    hit = AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                    principal="u-alice", endpoint_fp="ep-1")
    assert hit and hit[0]["utterance"] == "把 upload_a.json 删掉"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-bob", endpoint_fp="ep-1") == [], "跨账户不得注入"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp="ep-2") == [], "跨端点不得注入"
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉"), "缺省不过滤（内部直调旧行为）"
    rows = _ledger_rows(tmp_path)
    assert rows[0]["principal"] == "u-alice" and rows[0]["endpoint_fp"] == "ep-1"
    assert _cand_rows(tmp_path) == [], "迁入后候选池不留残影"


def test_legacy_rows_only_serve_anonymous_noendpoint_partition(tmp_path):
    """分区前落盘的存量行按 ("anonymous","") 计：实名/有端点指纹的调用不注入它们
    （宁可少注不泄漏）；匿名 + 空端点分区仍可见。"""
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"utterance": "把 upload_a.json 删掉",
                                "steps": [{"verb": "curate.remove", "args": "curate.remove（target=upload_a.json）"}]},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="u-alice", endpoint_fp="ep-1") == []
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉",
                                     principal="anonymous", endpoint_fp=""), "存量行只回灌匿名+空端点分区"


def test_same_utterance_different_partitions_both_recorded(tmp_path):
    """同一句原话分属两个分区 → 两行各录各的（跨分区的逐字重复不误杀）。"""
    steps = [_step("curate.db_status")]
    AX._maybe_record_success(tmp_path, "库里有什么", {}, steps, principal="u-alice", endpoint_fp="ep-1")
    AX._maybe_record_success(tmp_path, "库里有什么", {}, steps, principal="u-bob", endpoint_fp="ep-1")
    assert len(_cand_rows(tmp_path)) == 2


def test_endpoint_fp_from_config_discriminates_and_ignores_key():
    """端点指纹：base_url/model 变则变；api_key 永不参与（换 key 不换分区）。"""

    class _Cfg:
        base_url = "https://api-a.example.com"
        model = "m1"
        api_key = "sk-" + "A" * 24

    fp = AX._endpoint_fp_from_config(_Cfg)
    assert AX._endpoint_fp_from_config(_Cfg) == fp
    class _Cfg2(_Cfg):
        api_key = "sk-" + "B" * 24
    assert AX._endpoint_fp_from_config(_Cfg2) == fp, "换 key 不得换分区"
    class _Cfg3(_Cfg):
        model = "m2"
    assert AX._endpoint_fp_from_config(_Cfg3) != fp
    class _Cfg4(_Cfg):
        base_url = "https://api-b.example.com"
    assert AX._endpoint_fp_from_config(_Cfg4) != fp


# ---------------------------------------------------------------- 收录质量闸
#
# 「跑通」不等于「干得漂亮」：被机械闸修好/打回/掐停/降级兜底的执行，其「原话→动作」
# 映射正是会教偏模型的毒样例——一遍过才录，宁可少录不录脏。kw-only 缺省全为「干净」，
# 旧有用例逐位不变。

def test_quality_gate_blocks_tainted_success(tmp_path):
    """六类「跑通但不漂亮」各试一次：全部连候选池都不进。"""
    good = [_step("curate.remove", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, repairs=1)        # 首步被修
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, finish_vetoes=1)  # 收尾被打回
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, reask_write_count=1)
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, truncated=True)   # 掐停
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, mode="json")      # 跌兜底
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, good, checklist_dropped=1)
    assert _cand_rows(tmp_path) == []


def test_quality_gate_clean_run_still_recorded(tmp_path):
    """全部质量信号显式为零 → 照录进候选池（与缺省旧行为逐位一致）。"""
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.remove", slots={"target": "upload_a.json"})],
                             mode="tools", repairs=0, finish_vetoes=0,
                             reask_write_count=0, truncated=False, checklist_dropped=0)
    assert len(_cand_rows(tmp_path)) == 1


# ---------------------------------------------------------------- 用户挑选入库
#
# 机械收录只进候选池；用户勾选才迁入正式库（注入侧只读正式库）。入库去重、忽略清池、
# 跨分区不可见也不可批。

def test_approve_moves_candidate_into_ledger(tmp_path):
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.remove", slots={"target": "upload_a.json"})])
    cand = AX.list_example_candidates(tmp_path)
    assert len(cand) == 1, "内部直调（principal=None）不过滤可见候选"
    out = AX.approve_example_candidates(tmp_path, [cand[0]["id"]])
    assert out == {"approved": 1, "duplicated": 0}
    assert _cand_rows(tmp_path) == [] and len(_ledger_rows(tmp_path)) == 1
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉"), "入库后才可注入"


def test_approve_dedupes_against_ledger(tmp_path):
    """同分区同句同动作序列已在正式库 → 再勾只计 duplicated，不重复入库。"""
    steps = [_step("curate.remove", slots={"target": "upload_a.json"})]
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps)
    first = AX.list_example_candidates(tmp_path)[0]["id"]
    assert AX.approve_example_candidates(tmp_path, [first])["approved"] == 1
    # 同一句话再跑一遍（时间戳不同 → 是新候选行）：再勾 → duplicated
    import time as _t
    _t.sleep(0.01)
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {}, steps)
    second = [r["id"] for r in AX.list_example_candidates(tmp_path)]
    out = AX.approve_example_candidates(tmp_path, second)
    assert out == {"approved": 0, "duplicated": 1}
    assert len(_ledger_rows(tmp_path)) == 1 and _cand_rows(tmp_path) == []


def test_dismiss_removes_from_pool_only(tmp_path):
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.remove", slots={"target": "upload_a.json"})])
    cid = AX.list_example_candidates(tmp_path)[0]["id"]
    assert AX.dismiss_example_candidates(tmp_path, [cid]) == {"dismissed": 1}
    assert _cand_rows(tmp_path) == [] and _ledger_rows(tmp_path) == []
    assert AX._load_success_examples(tmp_path, "把 upload_b.json 删掉") == [], "忽略的不得注入"


def test_candidates_invisible_across_partitions(tmp_path):
    """候选展示同区过滤：换账户/换端点都看不见（与注入侧同一套分区纪律）。"""
    AX._maybe_record_success(tmp_path, "库里有什么", {}, [_step("curate.db_status")],
                             principal="u-alice", endpoint_fp="ep-1")
    assert AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp="ep-1")
    assert AX.list_example_candidates(tmp_path, principal="u-bob", endpoint_fp="ep-1") == []
    assert AX.list_example_candidates(tmp_path, principal="u-alice", endpoint_fp="ep-2") == []


# ---------------------------------------------------------------- 观测留痕

def test_corrupted_candidates_pool_warns_and_returns_empty(tmp_path, capsys):
    """候选池文件损坏 → 仍按空表返回（口径不变，不掀翻主流程），
    但 stderr 必须留一行——「库损坏」与「本来没候选」事后必须可区分。"""
    AX._WARNED.clear()
    path = tmp_path / ".userdata" / AX._EXAMPLE_CANDIDATES_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("这不是json\n{bad json}\n", encoding="utf-8")
    assert AX.list_example_candidates(tmp_path) == []
    assert "无法解析的行" in capsys.readouterr().err


def test_corrupted_ledger_warns_and_injects_nothing(tmp_path, capsys):
    """正式库损坏 → 注入侧静默降级回纯静态 few-shot 的口径不变，
    但降级原因可观测（此前「账本坏了所以没注入」与「没有匹配样例」完全同形）。"""
    AX._WARNED.clear()
    path = tmp_path / ".userdata" / AX._EXAMPLES_LEDGER_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{{{\n", encoding="utf-8")
    assert AX._load_success_examples(tmp_path, "把 upload_a.json 删掉") == []
    assert "无法解析的行" in capsys.readouterr().err


def test_candidate_write_failure_warns(tmp_path, capsys, monkeypatch):
    """候选池写盘失败不再零痕迹——收录语义不变（不掀翻主流程），
    stderr 留一行可定位的痕迹。"""
    from dataset_recommender.corpus import corpus_curation as CC

    def _boom(path, row):
        raise OSError("disk full")

    monkeypatch.setattr(CC, "_append_jsonl", _boom)
    AX._WARNED.clear()
    AX._maybe_record_success(tmp_path, "把 upload_a.json 删掉", {},
                             [_step("curate.remove", slots={"target": "upload_a.json"})])
    assert "候选池写盘失败" in capsys.readouterr().err


def test_audit_write_failures_warn(tmp_path, capsys, monkeypatch):
    """联网账本与降级审计账本的写盘失败统一 `_warn_once` 纪律——
    观测设施自身故障必须可观测（同一原因只打一行，不刷屏）。"""
    from dataset_recommender.corpus import corpus_curation as CC

    def _boom(path, row):
        raise OSError("disk full")

    monkeypatch.setattr(CC, "_append_jsonl", _boom)
    AX._WARNED.clear()
    AX._audit_loop_tool(tmp_path, "curate.check_updates", {}, True, "")
    AX._audit_loop_tool(tmp_path, "curate.check_updates", {}, True, "")
    AX._audit_fallback(tmp_path, "decide", "channel_down", "原话", "model-x")
    err = capsys.readouterr().err
    assert err.count("联网账本写盘失败") == 1, "同一原因只打一行（warn once 纪律）"
    assert "降级审计账本写盘失败" in err
