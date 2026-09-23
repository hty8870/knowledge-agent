# -*- coding: utf-8 -*-
"""对抗用例扩增器（scripts/augment_agent_cases.py）离线单测。

全部离线：LLM 调用一律 monkeypatch 掉，绝不发真实请求。钉的是生成器的
机械纪律——expect 继承/合并不许 LLM 染指：
1. 换措辞/加噪/劣质化变体的 expect、tools 与原例**逐位一致**（深拷贝非引用）；
2. 混合意图合并规则：must_steps 并集保序去重、max_steps 求和封顶 8、
   zero_writes 取与、ideal 超 max 丢弃、unordered 不升格成有序、tools 冲突放弃；
3. id 生成规则（k01→k01a、k01+k05→k01_k05x）；
4. 形状自检不合的变体丢弃并计数；
5. --dry-run 零 LLM 调用。
"""
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "scripts"))

import augment_agent_cases as aug  # noqa: E402

# harness 经 aug._harness() 延迟取用（工作树他人未提交改动可能暂坏 agent_exec
# import——本测试文件不该因此连坐 collection）。


@pytest.fixture(autouse=True)
def _no_throttle(monkeypatch):
    monkeypatch.setattr(aug, "_MIN_INTERVAL", 0.0)


def _fake_llm(text_map=None):
    """返回一个 call_llm 替身：按 prompt 里出现的原句决定回复；
    计数器保证同例多次调用给出不同 utterance（例内去重不会误伤）。"""
    counter = {"n": 0}

    def _res(utt, diff="medium"):
        return type("R", (), {"succeeded": True, "text": json.dumps(
            {"utterance": utt, "difficulty": diff}, ensure_ascii=False)})()

    def _call(prompt, cfg, retrieved_records=None):
        counter["n"] += 1
        if text_map:
            for key, utt in text_map.items():
                if key in prompt:
                    return _res(utt, "hard")
        return _res(f"改写后的指令{counter['n']}")

    return _call


def _case_a():
    return {
        "id": "a01", "cat": "A单步路由", "utterance": "库里现在有多少条数据",
        "tools": {"curate.db_status": {"const": "db_status_ok"}},
        "expect": {"first": "curate.db_status",
                   "must_steps": ["curate.db_status"],
                   "forbid_steps": ["curate.search_online", "curate.sync_updates"],
                   "max_steps": 1, "zero_writes": True},
    }


def _case_k(cid="k01", must=None, max_steps=None, ideal=None, zero=True,
            tools=None, unordered=False):
    exp = {"first": "curate.check_updates"}
    key = "must_steps_unordered" if unordered else "must_steps"
    exp[key] = must if must is not None else [
        {"verb": "curate.check_updates", "source": "arrayexpress"}]
    if max_steps is not None:
        exp["max_steps"] = max_steps
    if ideal is not None:
        exp["ideal_steps"] = ideal
    if zero:
        exp["zero_writes"] = True
    return {
        "id": cid, "cat": "K长程任务", "utterance": f"{cid} 的原句",
        "tools": tools if tools is not None else
        {"curate.check_updates": {"const": "check_zero"}},
        "expect": exp,
    }


# ------------------------------------------------------------------ 机械继承

def test_paraphrase_inherits_expect_bitwise(monkeypatch):
    monkeypatch.setattr(aug, "call_llm", _fake_llm())
    case = _case_a()
    stats = {k: 0 for k in ("llm_retries", "llm_failed", "shape_dropped",
                            "merge_conflict", "mix_fallback", "family_skipped",
                            "duplicate_utterance")}
    variants = aug.run([case], per_case=2, seed=42, dry_run=False, cfg=None,
                       stats=stats)
    assert len(variants) == 2  # a 族：paraphrase + noise
    for v in variants:
        assert v["expect"] == case["expect"]  # 逐位一致
        assert v["expect"] is not case["expect"]  # 深拷贝
        assert v["tools"] == case["tools"]
        assert v["cat"] == case["cat"]
        assert v["note"].startswith(f"aug:")
        assert "_of:a01" in v["note"]
        assert "difficulty:" in v["note"]
    assert [v["id"] for v in variants] == ["a01a", "a01b"]


def test_degrade_rewrites_cat_but_keeps_expect(monkeypatch):
    monkeypatch.setattr(aug, "call_llm", _fake_llm())
    case = _case_k("k01")
    stats = {k: 0 for k in ("llm_retries", "llm_failed", "shape_dropped",
                            "merge_conflict", "mix_fallback", "family_skipped",
                            "duplicate_utterance")}
    variants = aug.run([case], per_case=1, seed=42, dry_run=False, cfg=None,
                       stats=stats)  # k 族首个类型 = degrade
    assert len(variants) == 1
    assert variants[0]["cat"] == "J劣质指令"
    assert variants[0]["expect"] == case["expect"]
    assert variants[0]["note"].startswith("aug:degrade_of:k01")


# ------------------------------------------------------------------ 混合意图机械合并

def test_merge_must_union_ordered_dedupe_and_max_cap():
    c1 = _case_k("k01", must=[{"verb": "curate.check_updates", "source": "arrayexpress"},
                              "curate.db_status"], max_steps=3)
    c2 = _case_k("k05", must=[{"verb": "curate.check_updates", "source": "arrayexpress"},
                              {"verb": "curate.check_updates", "source": "encode"}],
                 max_steps=6)
    merged = aug.merge_cases(c1, c2)
    assert merged is not None
    # 并集保序去重：c1 的两条在前，c2 的重复条不重现
    assert merged["expect"]["must_steps"] == [
        {"verb": "curate.check_updates", "source": "arrayexpress"},
        "curate.db_status",
        {"verb": "curate.check_updates", "source": "encode"}]
    assert merged["expect"]["max_steps"] == 8  # 3+6=9 封顶 8
    assert merged["expect"]["zero_writes"] is True  # 两例皆 true
    assert merged["expect"]["first"] == ["curate.check_updates"]  # 去重


def test_merge_max_sum_below_cap_and_zero_writes_and_semantics():
    c1 = _case_k("k01", max_steps=2, zero=True)
    c2 = _case_k("k05", max_steps=3, zero=False)
    merged = aug.merge_cases(c1, c2)
    assert merged["expect"]["max_steps"] == 5  # 求和不触顶
    assert "zero_writes" not in merged["expect"]  # 取与：一例无则不保留


def test_merge_ideal_sum_kept_when_within_max():
    c1 = _case_k("k01", max_steps=2, ideal=2)
    c2 = _case_k("k05", max_steps=2, ideal=2)
    merged = aug.merge_cases(c1, c2)
    assert merged["expect"]["max_steps"] == 4
    assert merged["expect"]["ideal_steps"] == 4  # 2+2=4<=4 保留


def test_merge_single_side_max_omitted():
    # 单边 max_steps 不继承——l02 的 max=1 若继承会让 l01 的三步 must 永不可满足
    c1 = _case_k("l01", max_steps=None, ideal=3)
    c2 = _case_k("l02", max_steps=1)
    merged = aug.merge_cases(c1, c2)
    assert "max_steps" not in merged["expect"]


def test_merge_must_semantic_dedupe_str_vs_object():
    # 字符串形与无约束对象形同动词 = 同一步——去重，否则 unordered 会要求两个搜索步
    c1 = _case_k("l01", must=[{"verb": "curate.search_online"}], unordered=True)
    c2 = _case_k("l02", must=["curate.search_online"])
    merged = aug.merge_cases(c1, c2)
    assert merged["expect"]["must_steps_unordered"] == [
        {"verb": "curate.search_online"}]
    # 有 source 约束的对象形与无约束不同键 → 都保留
    c3 = _case_k("k01", must=[{"verb": "curate.check_updates", "source": "10x"}])
    c4 = _case_k("k05", must=["curate.check_updates"])
    merged2 = aug.merge_cases(c3, c4)
    assert merged2["expect"]["must_steps"] == [
        {"verb": "curate.check_updates", "source": "10x"}, "curate.check_updates"]


def test_merge_unordered_not_promoted_to_ordered():
    c1 = _case_k("l01", unordered=True)
    c2 = _case_k("l03")
    merged = aug.merge_cases(c1, c2)
    assert "must_steps_unordered" in merged["expect"]
    assert "must_steps" not in merged["expect"]


def test_merge_tools_conflict_returns_none():
    c1 = _case_k("k01", tools={"curate.check_updates": {"const": "check_zero"}})
    c2 = _case_k("k05", tools={"curate.check_updates": {"const": "check_ae2"}})
    assert aug.merge_cases(c1, c2) is None  # 同动词不同结局 = 语义冲突，放弃


def test_merge_tools_by_source_table_union():
    c1 = _case_k("k01", tools={"curate.check_updates": {
        "by_source": {"arrayexpress": {"const": "check_zero"}},
        "default": {"const": "check_zero"}}})
    c2 = _case_k("k05", tools={"curate.check_updates": {
        "by_source": {"encode": {"const": "check_zero"}},
        "default": {"const": "check_zero"}},
        "curate.db_status": {"const": "db_status_ok"}})
    merged = aug.merge_cases(c1, c2)
    table = merged["tools"]["curate.check_updates"]["by_source"]
    assert set(table) == {"arrayexpress", "encode"}
    assert "curate.db_status" in merged["tools"]


def test_mix_eligible_gate():
    assert aug.mix_eligible(_case_k("k01")) is True
    bad = _case_k("k02")
    bad["expect"]["steps_exact"] = 3  # 截断钉法无安全合并规则
    assert aug.mix_eligible(bad) is False
    bad2 = _case_k("k03")
    bad2["context"] = {"has_results": True}
    assert aug.mix_eligible(bad2) is False


# ------------------------------------------------------------------ id 规则

def test_id_rules():
    assert aug.variant_id("k01", 0) == "k01a"
    assert aug.variant_id("k01", 1) == "k01b"
    assert aug.mix_id("k01", "k05") == "k01_k05x"


# ------------------------------------------------------------------ 形状自检丢弃计数

def test_shape_check_drops_and_counts(monkeypatch):
    monkeypatch.setattr(aug, "call_llm", _fake_llm())
    monkeypatch.setattr(aug, "shape_check", lambda case: False)  # 全部判废
    stats = {k: 0 for k in ("llm_retries", "llm_failed", "shape_dropped",
                            "merge_conflict", "mix_fallback", "family_skipped",
                            "duplicate_utterance")}
    variants = aug.run([_case_a()], per_case=2, seed=42, dry_run=False,
                       cfg=None, stats=stats)
    assert variants == []
    assert stats["shape_dropped"] == 2


def test_shape_check_real_rejects_bad_case():
    good = _case_a()
    good["id"] = "a01a"
    assert aug.shape_check(good) is True
    bad = _case_a()
    bad["id"] = "a01b"
    bad["expect"]["bogus_field"] = True  # expect 未知键
    assert aug.shape_check(bad) is False


def test_same_case_variants_deduped(monkeypatch):
    # LLM 两次给出近乎同文（只差句号）→ 第二条按撞车丢弃计数
    class _Res:
        succeeded = True
        text = '{"utterance": "库里一共多少条数据。", "difficulty": "easy"}'

    class _Res2:
        succeeded = True
        text = '{"utterance": "库里一共多少条数据", "difficulty": "easy"}'

    seq = [_Res(), _Res2()]

    def _call(prompt, cfg, retrieved_records=None):
        return seq.pop(0) if seq else _Res2()

    monkeypatch.setattr(aug, "call_llm", _call)
    stats = {k: 0 for k in ("llm_retries", "llm_failed", "shape_dropped",
                            "merge_conflict", "mix_fallback", "family_skipped",
                            "duplicate_utterance")}
    variants = aug.run([_case_a()], per_case=2, seed=42, dry_run=False,
                       cfg=None, stats=stats)
    assert len(variants) == 1
    assert stats["duplicate_utterance"] == 1


# ------------------------------------------------------------------ LLM 失败纪律

def test_llm_failure_skips_case_not_run(monkeypatch):
    calls = {"n": 0}

    def _boom(prompt, cfg, retrieved_records=None):
        calls["n"] += 1
        raise RuntimeError("network down")

    monkeypatch.setattr(aug, "call_llm", _boom)
    stats = {k: 0 for k in ("llm_retries", "llm_failed", "shape_dropped",
                            "merge_conflict", "mix_fallback", "family_skipped",
                            "duplicate_utterance")}
    variants = aug.run([_case_a()], per_case=2, seed=42, dry_run=False,
                       cfg=None, stats=stats)
    assert variants == []
    assert stats["llm_failed"] == 2  # 每个变体重试 1 次后放弃
    assert calls["n"] == 4  # 2 变体 × (首调 + 重试 1 次)


# ------------------------------------------------------------------ dry-run 零 LLM

def test_dry_run_zero_llm_calls(monkeypatch):
    def _forbidden(*a, **kw):
        raise AssertionError("dry-run 不得调用 LLM")

    monkeypatch.setattr(aug, "call_llm", _forbidden)
    rc = aug.main(["--dry-run", "--limit", "5"])
    assert rc == 0


# ------------------------------------------------------------------ 端到端（离线，LLM 替身）

def test_end_to_end_offline(tmp_path, monkeypatch):
    monkeypatch.setattr(aug, "call_llm", _fake_llm())
    out = tmp_path / "aug.jsonl"
    rc = aug.main(["--out", str(out), "--limit", "12", "--per-case", "2",
                   "--seed", "42"])
    assert rc == 0
    cases = aug._harness().load_cases(out)  # 产物过 harness 启动自检（不 SystemExit 即过）
    assert cases
    ids = [c["id"] for c in cases]
    assert len(ids) == len(set(ids))
    for c in cases:
        assert c["note"].startswith("aug:")


def test_select_cases_limit_covers_families():
    master = aug.load_master(_ROOT / "eval" / "agent_live_cases_v1.jsonl")
    picked = aug.select_cases(master, only=None, limit=12)
    assert len(picked) == 12
    fams = {c["id"][0] for c in picked}
    assert len(fams) >= 10  # 轮转取样：前 12 例覆盖绝大多数族
