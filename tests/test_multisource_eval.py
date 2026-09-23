"""多源检索质量评测台（group-2）的 CI 护栏：把 scripts/evaluate_multisource.py 钉成回归门。

定位：**非冻结门**（冻结门是 base-only 的 scripts/evaluate_recommendation.py，另有 test 守护）。
本护栏跑**全部来源**（~8022），确保词表扩充让外部平台库真正可检出，且跨源检索仍：
  1) 硬违规 = 0%（返回项必满足期望约束——0% 违规保证在多源下也成立）；
  2) 该弃权的全弃权、该空集的全空集（fail-closed 语义跨源不破）；
  3) Hit@5 不塌、五个来源都能被检出（词表扩充的召回成果不回退）。
纯只读，不改任何状态，不影响冻结门/确定性。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from evaluate_multisource import run_eval  # noqa: E402

# 基线来源：**按名字钉**，不按个数钉。
#
# 原写法是 `n_sources == 5` / `len(sources_seen) == 5`。它想保护的是「词表扩充的召回成果
# 不回退」——即这五个来源**每一个都仍然检得出来**；而它实际写成的是「来源总数恰好是 5」。
# 两者在只有五个来源时等价，一旦要接入第六个来源就分道扬镳：数据侧做完了合规的候选，
# 却被一条与数据质量无关的计数断言挡住。
#
# 放开的方式必须**不削弱**原有保护：
#   · 五个基线来源仍必须一个不少地被检出（真正的回归护栏）；
#   · 新来源只允许**增加**，不允许顶替（n_sources 只增不减）；
#   · 新来源的可检出性**不由本护栏背书**——评测查询集没有覆盖它，就不能假装测过了。
#     这一条由 test_new_source_reachability_is_not_silently_claimed 显式钉住。
BASELINE_SOURCES = {
    "10x Genomics",
    "CELLxGENE Discover",
    "ArrayExpress",
    "Human Cell Atlas",
    "EBI Single Cell Expression Atlas",
}


def test_multisource_quality_gate():
    metrics, rows = run_eval(top_k=5)
    raw = metrics["_raw"]

    # 语料确实是多源全量（base 784 + 外部 ≥4900）
    assert raw["n_records"] > 5000, f"期望多源全量语料, got {raw['n_records']}"
    assert raw["n_sources"] >= len(BASELINE_SOURCES), (
        f"来源数少于基线：期望 ≥{len(BASELINE_SOURCES)}, got {raw['n_sources']}——"
        "这是**丢了来源**，不是扩容")

    # 1) 0% 硬违规（多源下也成立）
    assert metrics["Constraint_Violation_%"] == 0.0, f"出现约束违规: {rows}"

    # 2) fail-closed 语义跨源不破：该弃权全弃权、该空集全空集
    assert raw["abstain_correct"] == raw["abstain_total"], "有该弃权未弃权的查询"
    assert raw["noresult_correct"] == raw["noresult_total"], "有该空集未空集的查询"

    # 3) 召回不塌：期望有结果的查询 Hit@5 全中，且**五个基线来源一个不少**地被检出
    assert raw["hit_hit"] == raw["hit_total"], f"Hit@5 未全中: {raw['hit_hit']}/{raw['hit_total']}"
    seen = set(raw["sources_seen"])
    missing = BASELINE_SOURCES - seen
    assert not missing, f"基线来源检不出来了（召回回退）: {sorted(missing)}；实际检出 {sorted(seen)}"


def test_new_source_reachability_is_not_silently_claimed():
    """新接入的来源如果没被评测查询覆盖，必须**说出来**，不能靠上面那条门混过去。

    上面那条门只保证五个基线来源不回退。若日后接了第六个来源而评测集没有一条查询能命中它，
    「多源质量门全绿」这句话就会被理解成「新来源也测过了」——那是没做的事被说成做了。
    本条不阻断扩容（新来源未覆盖不判失败），只把「哪些来源没被本护栏覆盖」显式打印出来，
    逼后续任务要么补评测查询、要么在报告里如实写明这一块没有护栏。
    """
    metrics, _rows = run_eval(top_k=5)
    raw = metrics["_raw"]
    seen = set(raw["sources_seen"])
    uncovered = sorted(set(raw.get("all_sources") or []) - seen - BASELINE_SOURCES)
    if uncovered:
        print(f"[多源护栏] 以下来源未被任何评测查询命中，本护栏**不为其可检出性背书**：{uncovered}")
    # 断言的是「基线之外的来源都被如实归类」，不是「必须覆盖」——扩容不该被卡。
    assert isinstance(uncovered, list)


def test_new_vocab_external_records_reachable():
    """抽查 group-1 新词表：拟南芥/胸腺/新冠/Smart-seq2 这类此前会弃权的查询现在能在多源里检出结果。"""
    metrics, rows = run_eval(top_k=5)
    by_q = {r["query"]: r for r in rows}
    for q in ["拟南芥的单细胞数据", "人类胸腺的单细胞数据", "新冠患者的肺单细胞数据", "Smart-seq2 的人类数据"]:
        assert q in by_q, f"评测集缺 {q}"
        assert by_q[q]["hits"] >= 1 and not by_q[q]["abstain"], f"{q} 应有结果, got {by_q[q]}"
