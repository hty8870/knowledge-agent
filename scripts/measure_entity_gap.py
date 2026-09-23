# -*- coding: utf-8 -*-
"""细粒度实体检索缺口测量。

**只测不建**：把「细粒度实体类查询的漏召」按性质分四类定量，回答「缺口在哪一层」，
不为任何施工分支背书（postings/倒排索引分支已被 验证砍掉——当前检索是全量硬过滤
后排序，补召回通道要么无效要么绕开 fail-closed）。

四类口径（验证）：
  C1 parser/词表未识别实体——查询可满足（库里有支持），但解析器弃权/抽错导致检索没跑或跑歪；
  C2 目录根本没有该实体（或字段未标注）——弃权/空结果是**正确**的；
  C3 条目存在且相关，但被解析出的硬过滤条件误滤（字段覆盖/标注不足）；
  C4 条目过了硬过滤但 Top-K 排名失败（支持 >0、结果非空、Top5 无命中）。

样本：由 `eval/evaluation-manifest.json` 显式列出的人工标注集（自带
must_match/must_not_match = 现成的查询—相关条目对），按查询文本去重。private manifest
保留私有 holdout；public manifest 使用独立公开验证集，二者都对缺文件和样本静默退化 fail-closed。
（方案 v2 提到的「历史查询」只作补充——它们存在浏览器 localStorage，服务端不可得，
本测量以三个人工标注集为全样本，此处如实注明偏差。）

外部裁判复用 `evaluate_recommendation.py` 的 satisfies_expected/constraint_satisfied
（同一套独立裁判，不回读 normalizer 内部字段）。

用法：
  PYTHONPATH=src py scripts/measure_entity_gap.py                # 跑全部三集，打印摘要 + 落报告
  PYTHONPATH=src py scripts/measure_entity_gap.py --out eval/entity_gap_report.json
  PYTHONPATH=src py scripts/measure_entity_gap.py --oov-report   # OOV 词表生长候选报告（第二段）
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(AGENT_ROOT / "src"))
sys.path.insert(0, str(AGENT_ROOT / "scripts"))

_ER = None


def _er():
    """`evaluate_recommendation` 的**惰性**导入：

    该模块导入期会包一层 `sys.stdout`（脚本直跑的 utf-8 口径）——pytest 里 import 本文件
    测 OOV 报告时，这层包装会顶坏 pytest 的 stdout 捕获。OOV 报告路径根本不需要外部裁判，
    故四类缺口测量（C1-C4）首次真用时才导入；脚本直跑行为逐位不变（import 缓存由 sys.modules
    保证，_ER 只做取用 shortcut）。"""
    global _ER
    if _ER is None:
        import evaluate_recommendation as er  # （复用外部裁判与管线加载；stdout utf-8 包装由它做）
        _ER = er
    return _ER

EVALUATION_MANIFEST = AGENT_ROOT / "eval" / "evaluation-manifest.json"

CLASS_ZH = {
    "C1": "解析器/词表未识别实体（可满足的查询被弃权或抽错挡下）",
    "C2": "目录缺口（库里根本没有该实体或未标注——弃权/空结果是对的）",
    "C3": "硬过滤误滤（相关条目存在，被解析出的条件滤掉——字段覆盖/标注不足）",
    "C4": "Top-K 排名失败（相关条目过了硬过滤，但 Top5 无命中）",
    "OK": "无缺口（Top5 命中，或标了无结果且实返空）",
}


def wilson_interval(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 区间（比例, 比例）；n=0 → (0.0, 0.0)。"""
    if n <= 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5) / denom
    return max(0.0, center - half), min(1.0, center + half)


def _constraint_failing_dims(record, constraints: dict) -> list[str]:
    """相关记录被哪些解析出的硬条件误滤（C3 归因）：
    用外部裁判的字段映射逐维核对——记录不满足哪一维，哪一维就是误滤凶手。"""
    bad = []
    for dim, values in (constraints or {}).items():
        text = _er()._record_field_text(record, dim)
        terms = values if isinstance(values, list) else [values]
        if not any(str(t).strip().lower() in text for t in terms if str(t.strip())):
            bad.append(dim)
    return bad


def classify_query(q: dict, records, settings, top_k: int = 5) -> dict:
    """单查询四类归因。返回 {id, query, category, cls, support, n_returned, detail}。"""
    from dataset_recommender.retrieval.query_parser import parse_query
    from dataset_recommender.retrieval.retriever import DatasetRetriever

    must = q.get("must_match", {}) or {}
    must_not = q.get("must_not_match", {}) or {}
    expect_empty = bool(q.get("no_result_expected", False))
    query = q["query"]

    intent = parse_query(query, settings.keyword_mapping)
    results = DatasetRetriever(top_k=top_k).retrieve(records, intent, top_k=top_k)
    support = sum(1 for r in records if _er().satisfies_expected(r, must, must_not))

    row = {
        "id": q.get("id"), "query": query, "category": q.get("category"),
        "support": support, "n_returned": len(results), "expect_empty": expect_empty,
        "cls": None, "detail": "",
    }

    if expect_empty:
        # 无结果题：返空 = 正确（其 ground truth 已由冻结评测 NoResult 闸覆盖，不在四类缺口内）。
        row["cls"] = "OK" if not results else "C2"
        row["detail"] = "无结果题：实返空" if not results else (
            f"无结果题但返回 {len(results)} 条（冻结评测 nr 组会抓；若库里确有支持则属目录演进而非缺口）")
        return row

    abstained = bool(getattr(intent, "abstain", False))
    parse_status = str(getattr(intent, "parse_status", "") or "")
    unresolved = [str(t) for t in (getattr(intent, "unresolved_terms", None) or [])]
    unused = [str(t) for t in (getattr(intent, "unused_query_terms", None) or [])]

    if abstained or parse_status not in ("executable", ""):
        if support > 0:
            row["cls"] = "C1"
            row["detail"] = (f"弃权/不可执行（{parse_status}；{getattr(intent, 'abstain_reason', '') or ''}）"
                             f"但库里有 {support} 条支持；未解析词：{unresolved or unused or '—'}")
        else:
            row["cls"] = "C2"
            row["detail"] = f"弃权且库里 0 支持——弃权正确；未解析词：{unresolved or unused or '—'}"
        return row

    if not results:
        if support == 0:
            row["cls"] = "C2"
            row["detail"] = "可执行但 0 命中，库里也 0 支持——目录缺口"
        else:
            row["cls"] = "C3"
            # C3 归因：相关记录被解析出的哪些维度误滤
            failing: dict[str, int] = {}
            for r in records:
                if not _er().satisfies_expected(r, must, must_not):
                    continue
                for dim in _constraint_failing_dims(r, getattr(intent, "constraints", {}) or {}):
                    failing[dim] = failing.get(dim, 0) + 1
            row["detail"] = (f"可执行但 0 命中，库里有 {support} 条支持；"
                             f"误滤维度：{failing or '（解析条件与裁判不一致，需人工核）'}")
        return row

    top5_ok = any(_er().satisfies_expected(c.record, must, must_not) for c in results[:5])
    if top5_ok:
        row["cls"] = "OK"
        row["detail"] = "Top5 命中"
    elif support > 0:
        row["cls"] = "C4"
        row["detail"] = f"返回 {len(results)} 条但 Top5 无命中，库里有 {support} 条支持"
    else:
        row["cls"] = "C2"
        row["detail"] = "返回非空但无 must_match 命中且库里 0 支持——结果本身可疑（冻结违规率闸覆盖）"
    return row


def load_evaluation_manifest(path: Path | None = None) -> dict:
    """读取实体缺口输入合同；坏 schema/路径/阈值一律 fail-closed。"""
    path = EVALUATION_MANIFEST if path is None else Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing evaluation manifest: {path}") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid evaluation manifest: {path}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise RuntimeError("evaluation manifest schema_version must be 1")
    entity_gap = data.get("entity_gap")
    if not isinstance(entity_gap, dict):
        raise RuntimeError("evaluation manifest is missing entity_gap")
    names = entity_gap.get("query_sets")
    minimum = entity_gap.get("expected_min_unique_queries")
    if not isinstance(names, list) or not names or not all(isinstance(x, str) for x in names):
        raise RuntimeError("entity_gap.query_sets must be a non-empty string list")
    if len(names) != len(set(names)):
        raise RuntimeError("entity_gap.query_sets contains duplicates")
    if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum <= 0:
        raise RuntimeError("entity_gap.expected_min_unique_queries must be a positive integer")
    for name in names:
        if Path(name).name != name or not name.endswith(".json"):
            raise RuntimeError(f"unsafe evaluation query-set path: {name!r}")
    return data


def load_all_queries(manifest_path: Path | None = None) -> list[dict]:
    manifest_path = EVALUATION_MANIFEST if manifest_path is None else Path(manifest_path)
    manifest = load_evaluation_manifest(manifest_path)
    eval_dir = manifest_path.parent
    seen: set[str] = set()
    out: list[dict] = []
    for name in manifest["entity_gap"]["query_sets"]:
        path = eval_dir / name
        if not path.exists():
            raise RuntimeError(f"evaluation manifest input is missing: eval/{name}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"invalid evaluation query set: eval/{name}") from exc
        queries = data.get("queries") if isinstance(data, dict) else None
        if not isinstance(queries, list):
            raise RuntimeError(f"evaluation query set has no queries list: eval/{name}")
        for q in queries:
            if not isinstance(q, dict):
                raise RuntimeError(f"evaluation query set contains a non-object row: eval/{name}")
            key = " ".join(str(q.get("query", "")).split()).lower()
            if not key or key in seen:
                continue
            seen.add(key)
            q2 = dict(q)
            q2["_set"] = name
            out.append(q2)
    minimum = manifest["entity_gap"]["expected_min_unique_queries"]
    if len(out) < minimum:
        raise RuntimeError(
            f"evaluation query sample degraded: unique={len(out)} expected_min={minimum}"
        )
    return out


# ---------------------------------------------------------------- OOV 词表生长候选报告（闭环第二段）

def _term_in_vocabulary(term: str) -> bool:
    """该词现在是否已被词表收录（display 或 alias 精确命中，大小写不敏感）。

    只读 `vocabulary.CATALOG`——日志落盘时未收录的词可能后来进了词表，
    报告必须如实标注「已收录」而不是把旧账当成待办。"""
    from dataset_recommender.domains.biodata.vocabulary import CATALOG

    t = str(term or "").strip().lower()
    if not t:
        return False
    for entries in CATALOG.values():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            if str(e.get("display") or "").strip().lower() == t:
                return True
            for a in e.get("aliases", []) or []:
                if str(a).strip().lower() == t:
                    return True
    return False


def oov_vocabulary_report(log_path: Path, out_base: Path) -> int:
    """OOV 日志 → vocabulary 候选报告（词表生长闭环的第二段）。

    读 `.userdata/oov_terms.jsonl`（/api/recommend 的未收录词弃权日志钩子所落），
    按词聚合频次与例句，逐词对照当前 CATALOG 标注「仍 OOV / 已收录」，
    产出候选清单（md + json）。**只产候选、不改词表**——vocabulary.py 在冻结门上，
    进词表必须走人工/LLM 审核 + 常规改动流程（别名碰撞守卫测试会核）。
    """
    entries: list[dict] = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue  # 坏行跳过，不毒化整份报告
            terms = [str(t).strip() for t in (row.get("terms") or []) if str(t).strip()]
            if terms:
                entries.append({"ts": str(row.get("ts") or ""), "query": str(row.get("query") or "")[:120],
                                "terms": terms})

    freq: dict[str, dict] = {}
    for e in entries:
        for t in e["terms"]:
            slot = freq.setdefault(t, {"term": t, "count": 0, "examples": []})
            slot["count"] += 1
            if e["query"] and e["query"] not in slot["examples"] and len(slot["examples"]) < 3:
                slot["examples"].append(e["query"])

    rows = sorted(freq.values(), key=lambda r: (-r["count"], r["term"]))
    for r in rows:
        r["still_oov"] = not _term_in_vocabulary(r["term"])

    n_events = len(entries)
    payload = {
        "log": str(log_path), "n_events": n_events, "n_distinct_terms": len(rows),
        "terms": rows,
    }
    out_json = out_base.with_suffix(".json")
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# OOV 词表生长候选报告", ""]
    md.append(f"- 日志：`{log_path}`（事件 {n_events} 条，去重后候选词 {len(rows)} 个）")
    md.append("- 口径：/api/recommend 未收录词弃权（unresolved_term）日志；「仍 OOV」= 对照当前 CATALOG 仍未收录")
    md.append("- **只产候选、不改词表**：进 vocabulary.py 必须走人工/LLM 审核 + 常规改动流程（别名碰撞守卫会核）")
    md.append("")
    if not rows:
        md.append("（暂无弃权词日志——还没有真实用户查询触发过未收录词弃权，或日志已被清理。）")
    else:
        md.append("| 候选词 | 弃权次数 | 仍 OOV | 例句（至多 3 条） |")
        md.append("|---|---|---|---|")
        for r in rows:
            examples = "<br>".join(r["examples"]) if r["examples"] else "—"
            md.append(f"| {r['term']} | {r['count']} | {'是' if r['still_oov'] else '否（已收录）'} | {examples} |")
    out_md = out_base.with_suffix(".md")
    out_md.write_text("\n".join(md) + "\n", encoding="utf-8", newline="")

    print(f"OOV 日志 {n_events} 条事件 / {len(rows)} 个候选词（仍 OOV {sum(1 for r in rows if r['still_oov'])} 个）")
    print(f"报告：{out_json} / {out_md}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(AGENT_ROOT / "eval" / "entity_gap_report.json"))
    ap.add_argument("--oov-report", action="store_true",
                    help="OOV 词表生长候选报告：读弃权词日志聚合成 vocabulary 候选（不动四类缺口测量）")
    ap.add_argument("--oov-log", default=str(AGENT_ROOT / ".userdata" / "oov_terms.jsonl"),
                    help="弃权词日志路径（默认 .userdata/oov_terms.jsonl）")
    ap.add_argument("--oov-out", default=str(AGENT_ROOT / "eval" / "oov_vocabulary_candidates"),
                    help="OOV 报告输出基名（.json/.md 后缀自动补）")
    args = ap.parse_args()

    if args.oov_report:
        return oov_vocabulary_report(Path(args.oov_log), Path(args.oov_out))

    settings, records = _er().load_pipeline()
    queries = load_all_queries()
    rows = [classify_query(q, records, settings) for q in queries]

    classes: dict[str, list[dict]] = {}
    for row in rows:
        classes.setdefault(row["cls"], []).append(row)

    n = len(rows)
    summary = {"n_queries": n, "n_records": len(records), "classes": {}}
    for cls in ("C1", "C2", "C3", "C4", "OK"):
        items = classes.get(cls, [])
        lo, hi = wilson_interval(len(items), n)
        summary["classes"][cls] = {
            "count": len(items),
            "rate_%": round(100.0 * len(items) / n, 1) if n else 0.0,
            "wilson95_%": [round(100.0 * lo, 1), round(100.0 * hi, 1)],
            "zh": CLASS_ZH[cls],
        }

    # 裁决（验证口径：5%/15% 是待验证假设不是门；区间跨阈值 → 继续采样/人工复核）
    def _interval_over(cls: str, pct: float) -> bool:
        lo, hi = summary["classes"][cls]["wilson95_%"]
        return hi >= pct

    gap = {c: summary["classes"][c] for c in ("C1", "C3", "C4")}
    verdict_lines = []
    if all(v["count"] == 0 for v in gap.values()):
        verdict_lines.append("三类真实缺口（C1/C3/C4）计数均为 0——测量样本内无实体缺口证据，结题不立项。")
    else:
        for cls, v in gap.items():
            if v["count"] == 0:
                continue
            lo, hi = v["wilson95_%"]
            band = "区间跨 5%/15% 假设阈值，建议扩大采样或人工复核后再决定" if (lo < 5 <= hi or lo < 15 <= hi) else "区间不跨阈值"
            verdict_lines.append(f"{cls}：{v['count']} 条（{v['rate_%']}%，95% 区间 {lo}–{hi}%），{band}。")
        if gap["C1"]["count"] >= max(gap["C3"]["count"], gap["C4"]["count"]) and gap["C1"]["count"] > 0:
            verdict_lines.append("C1（解析器/词表）为主导缺口 → 后续若立项，方向是词表/解析器覆盖，不是新召回通道。")
        elif gap["C3"]["count"] + gap["C4"]["count"] > 0:
            verdict_lines.append("C3/C4 存在缺口 → 方向是字段覆盖/排序层，同样不是新召回通道。")
    summary["verdict"] = verdict_lines

    report = {"summary": summary, "per_query": rows}
    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    md_lines = ["# 细粒度实体检索缺口测量报告", ""]
    md_lines.append(f"- 样本：{n} 条人工标注查询（三集去重）；语料 {len(records)} 条")
    for cls in ("C1", "C2", "C3", "C4", "OK"):
        v = summary["classes"][cls]
        md_lines.append(f"- **{cls}**（{v['zh']}）：{v['count']} 条，{v['rate_%']}%，Wilson 95% {v['wilson95_%'][0]}–{v['wilson95_%'][1]}%")
    md_lines.append("")
    for line in verdict_lines:
        md_lines.append(f"- {line}")
    md_lines.append("")
    md_lines.append("## 逐条归因")
    md_lines.append("")
    md_lines.append("| id | 集 | 类 | 支持 | 返回 | 查询 | 归因 |")
    md_lines.append("|---|---|---|---|---|---|---|")
    for row in rows:
        md_lines.append(
            f"| {row['id']} | {row.get('_set', '')} | {row['cls']} | {row['support']} | "
            f"{row['n_returned']} | {row['query']} | {row['detail']} |")
    md_path = out_path.with_suffix(".md")
    md_path.write_text("\n".join(md_lines) + "\n", encoding="utf-8", newline="")

    print(f"样本 {n} 条 / 语料 {len(records)} 条")
    for cls in ("C1", "C2", "C3", "C4", "OK"):
        v = summary["classes"][cls]
        print(f"  {cls}: {v['count']:3d} 条  {v['rate_%']:5.1f}%  95%CI {v['wilson95_%']}")
    for line in verdict_lines:
        print("  " + line)
    print(f"报告：{out_path} / {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
